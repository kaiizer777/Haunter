"""Issue #142 — mirror batch-commit fallback reuses a single session.

Hermetic (fast suite, zero external I/O): `_flush_mirror_batch` is driven
with fake sessions by patching `app.db.async_session_maker`, which the
function imports locally, so the patch is visible. Covers the #142
acceptance criteria:

- a failed batch of N events opens exactly one fallback session;
- a per-event failure still does not drop the remaining events
  (rollback between events isolates the poisoned transaction);
- per-event idempotency (`on_conflict_do_nothing`) is preserved.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.services.session_orchestrator import SessionOrchestrator


class _FailingBatchDb:
    """First session opened by `_flush_mirror_batch`: the bulk commit fails."""

    def __init__(self, dialect_name: str = "") -> None:
        self.dialect_name = dialect_name
        if dialect_name:
            self.bind = _FakeBind(dialect_name)

    def add_all(self, objs: list[Any]) -> None:
        pass

    async def execute(self, stmt: Any) -> None:
        raise RuntimeError("simulated batch commit failure")

    async def commit(self) -> None:
        raise RuntimeError("simulated batch commit failure")

    async def rollback(self) -> None:
        pass

    async def __aenter__(self) -> _FailingBatchDb:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeBind:
    def __init__(self, dialect_name: str) -> None:
        self.dialect = _FakeDialect(dialect_name)


class _FakeDialect:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeFallbackDb:
    """Shared fallback session: per-event add/commit with poison support.

    A poisoned `commit` leaves the bad row staged (as a real failed
    transaction would) so only an explicit `rollback()` clears it. Without
    rollback-between-events the poison would fail every later commit too,
    which is exactly what the isolation test pins down.
    """

    def __init__(
        self, poison_ids: set[int] | None = None, dialect_name: str = ""
    ) -> None:
        self.persisted: list[Any] = []
        self._staged: list[Any] = []
        self.statements: list[Any] = []
        self.commits = 0
        self.rollbacks = 0
        self._poison = poison_ids or set()
        if dialect_name:
            self.bind = _FakeBind(dialect_name)

    def add(self, obj: Any) -> None:
        self._staged.append(obj)

    def add_all(self, objs: list[Any]) -> None:
        self._staged.extend(objs)

    async def execute(self, stmt: Any) -> None:
        self.statements.append(stmt)

    async def commit(self) -> None:
        poisoned = [o for o in self._staged if o.event_id in self._poison]
        if poisoned:
            raise RuntimeError(
                "simulated poison event "
                f"{sorted(o.event_id for o in poisoned)}"
            )
        self.persisted.extend(self._staged)
        self._staged = []
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1
        self._staged = []

    async def __aenter__(self) -> _FakeFallbackDb:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _make_orchestrator() -> SessionOrchestrator:
    return SessionOrchestrator(
        session_id=uuid.uuid4(),
        db=AsyncMock(),
        gh_token=None,
        turn_id="turn-1",
    )


async def _flush_with_fakes(
    batch: list[tuple[int, str, str]],
    *,
    poison_ids: set[int] | None = None,
    dialect_name: str = "",
) -> tuple[list[Any], list[_FakeFallbackDb], SessionOrchestrator]:
    """Drive `_flush_mirror_batch` to its fallback path with fake sessions.

    Returns (all sessions opened, fallback sessions opened, orchestrator).
    """
    sessions: list[Any] = []
    fallbacks: list[_FakeFallbackDb] = []

    def _factory() -> Any:
        if not sessions:
            db: Any = _FailingBatchDb(dialect_name=dialect_name)
        else:
            db = _FakeFallbackDb(
                poison_ids=poison_ids, dialect_name=dialect_name
            )
            fallbacks.append(db)
        sessions.append(db)
        return db

    orch = _make_orchestrator()
    with patch("app.db.async_session_maker", side_effect=_factory):
        await orch._flush_mirror_batch(batch)
    return sessions, fallbacks, orch


@pytest.mark.fast
async def test_fallback_opens_single_session_for_failed_batch() -> None:
    batch = [(i, "message", f"chunk-{i}") for i in range(1, 6)]
    sessions, fallbacks, _ = await _flush_with_fakes(batch)

    assert len(sessions) == 2  # 1 failed batch attempt + 1 fallback session
    assert len(fallbacks) == 1
    assert fallbacks[0].commits == 5
    assert fallbacks[0].rollbacks == 0
    assert sorted(o.event_id for o in fallbacks[0].persisted) == [1, 2, 3, 4, 5]
    assert batch == []  # batch is drained even on the fallback path


@pytest.mark.fast
async def test_fallback_rollback_isolates_poison_event() -> None:
    batch = [(i, "message", f"chunk-{i}") for i in range(1, 6)]
    sessions, fallbacks, _ = await _flush_with_fakes(batch, poison_ids={3})

    assert len(fallbacks) == 1  # still exactly one fallback session
    assert fallbacks[0].rollbacks == 1  # poisoned transaction rolled back
    assert fallbacks[0].commits == 4
    assert sorted(o.event_id for o in fallbacks[0].persisted) == [1, 2, 4, 5]


@pytest.mark.fast
async def test_fallback_preserves_on_conflict_idempotency() -> None:
    from sqlalchemy.dialects import sqlite

    batch = [(i, "message", f"chunk-{i}") for i in range(1, 4)]
    sessions, fallbacks, _ = await _flush_with_fakes(
        batch, dialect_name="sqlite"
    )

    assert len(fallbacks) == 1
    assert len(fallbacks[0].statements) == 3
    assert fallbacks[0].commits == 3
    for stmt in fallbacks[0].statements:
        sql = str(
            stmt.compile(
                dialect=sqlite.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        ).lower()
        assert "on conflict" in sql and "do nothing" in sql
