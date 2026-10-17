"""Issue #147: _stream_from_db timeout anchored to Lambda remaining time.

The 900s guard inside ``_stream_from_db`` was unreachable under Lambda: the
hard kill counts from invoke start while the generator's clock started after
auth/queries/replay. The budget now comes from
``context.get_remaining_time_in_millis()`` (stashed by ``lambda_handler``),
with 900s kept only as the non-Lambda fallback.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from typing import Any, Iterator
from unittest.mock import patch

from app.lambda_runtime import (
    LAMBDA_TIMEOUT_FALLBACK_S,
    LAMBDA_TIMEOUT_SAFETY_MARGIN_S,
    reset_lambda_context,
    resolve_lambda_timeout_s,
    set_lambda_context,
)
from app.models import SessionStreamEvent
from app.routers.sessions import _stream_from_db


class _FakeLambdaContext:
    """Minimal Lambda invoke context: only the remaining-time method."""

    def __init__(self, remaining_ms: int) -> None:
        self._remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self._remaining_ms


@contextmanager
def _stashed_context(context: Any) -> Iterator[None]:
    token = set_lambda_context(context)
    try:
        yield
    finally:
        reset_lambda_context(token)


def test_resolve_timeout_falls_back_without_lambda_context() -> None:
    """Local dev / no invoke context keeps the 900s fallback."""
    assert resolve_lambda_timeout_s() == LAMBDA_TIMEOUT_FALLBACK_S == 900.0


def test_resolve_timeout_anchors_to_lambda_remaining_time() -> None:
    """Under Lambda the budget is remaining time minus the safety margin."""
    with _stashed_context(_FakeLambdaContext(600_000)):
        assert resolve_lambda_timeout_s() == 600.0 - LAMBDA_TIMEOUT_SAFETY_MARGIN_S


def test_resolve_timeout_falls_back_on_broken_context() -> None:
    """A context that raises, a non-numeric budget, or no time left → 900s."""

    class _Raising:
        def get_remaining_time_in_millis(self) -> int:
            raise RuntimeError("sandboxed")

    class _NonNumeric:
        def get_remaining_time_in_millis(self) -> str:
            return "soon"

    for broken in (_Raising(), _NonNumeric(), object(), None):
        with _stashed_context(broken):
            assert resolve_lambda_timeout_s() == LAMBDA_TIMEOUT_FALLBACK_S

    with _stashed_context(_FakeLambdaContext(0)):
        assert resolve_lambda_timeout_s() == LAMBDA_TIMEOUT_FALLBACK_S


def test_resolve_timeout_floors_tiny_remaining_at_zero() -> None:
    """Remaining time below the margin trips the guard immediately, not never."""
    with _stashed_context(_FakeLambdaContext(10_000)):
        assert resolve_lambda_timeout_s() == 0.0


def test_handler_stashes_context_and_resets_it() -> None:
    """lambda_handler.handler exposes the invoke context during dispatch only."""
    import lambda_handler

    seen: list[float] = []

    def _fake_dispatch(event: dict, context: Any) -> dict:
        seen.append(resolve_lambda_timeout_s())
        assert context is not None
        return {"status": "completed"}

    with patch.object(lambda_handler, "_handler", side_effect=_fake_dispatch) as dispatched:
        result = lambda_handler.handler({"ping": True}, _FakeLambdaContext(600_000))

    assert result == {"status": "completed"}
    dispatched.assert_called_once()
    assert seen == [600.0 - LAMBDA_TIMEOUT_SAFETY_MARGIN_S]
    # The invoke context must not leak into the next invocation.
    assert resolve_lambda_timeout_s() == LAMBDA_TIMEOUT_FALLBACK_S


def _thought_row(session_id: uuid.UUID, turn_id: str) -> SessionStreamEvent:
    return SessionStreamEvent(
        id=1,
        session_id=session_id,
        turn_id=turn_id,
        event_id=1,
        event="thought",
        chunk="event: thought\nid: 1\ndata: {\"delta\":\"step 1\"}\n\n",
    )


class _RowsDb:
    """DB double replaying one non-terminal event, then an empty poll window."""

    def __init__(self, rows: list[SessionStreamEvent]) -> None:
        self._rows = rows

    async def execute(self, stmt: Any) -> Any:
        rows = self._rows

        class _Result:
            def scalars(self) -> Any:
                class _Scalars:
                    def all(self) -> list[SessionStreamEvent]:
                        return rows

                return _Scalars()

        return _Result()

    async def rollback(self) -> None:
        pass

    async def __aenter__(self) -> "_RowsDb":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _FakeEngine:
    """One held connection replaying fixed rows for the whole stream (#165)."""

    def __init__(self, rows: list[SessionStreamEvent]) -> None:
        self._conn = _RowsDb(rows)

    def connect(self) -> _RowsDb:
        return self._conn


async def test_stream_breaks_on_lambda_anchored_budget(caplog: Any) -> None:
    """With ~0s of Lambda budget left, the poll loop breaks before any sleep."""
    session_id = uuid.uuid4()
    turn_id = str(uuid.uuid4())

    with (
        _stashed_context(_FakeLambdaContext(50)),
        patch("app.db.engine", _FakeEngine([_thought_row(session_id, turn_id)])),
    ):
        with caplog.at_level(logging.WARNING, logger="app.routers.sessions"):
            yielded = [chunk async for chunk in _stream_from_db(session_id, turn_id, last_event_id=0)]

    # The already-persisted replay is still delivered; only polling stops.
    assert len(yielded) == 1
    assert "step 1" in yielded[0]
    assert "reached timeout (0.0s)" in caplog.text


async def test_stream_uses_resolver_when_timeout_not_given(caplog: Any) -> None:
    """Without an explicit timeout the guard consults the Lambda resolver."""
    session_id = uuid.uuid4()
    turn_id = str(uuid.uuid4())

    with (
        patch("app.db.engine", _FakeEngine([_thought_row(session_id, turn_id)])),
        patch("app.routers.sessions.resolve_lambda_timeout_s", return_value=0.0),
    ):
        with caplog.at_level(logging.WARNING, logger="app.routers.sessions"):
            yielded = [chunk async for chunk in _stream_from_db(session_id, turn_id, last_event_id=0)]

    assert len(yielded) == 1
    assert "reached timeout (0.0s)" in caplog.text


async def test_stream_explicit_timeout_overrides_lambda_budget(caplog: Any) -> None:
    """An explicit timeout_s wins over a generous Lambda remaining budget."""
    session_id = uuid.uuid4()
    turn_id = str(uuid.uuid4())

    with (
        _stashed_context(_FakeLambdaContext(600_000)),
        patch("app.db.engine", _FakeEngine([_thought_row(session_id, turn_id)])),
    ):
        with caplog.at_level(logging.WARNING, logger="app.routers.sessions"):
            yielded = [
                chunk
                async for chunk in _stream_from_db(session_id, turn_id, last_event_id=0, timeout_s=0.0)
            ]

    assert len(yielded) == 1
    assert "reached timeout (0.0s)" in caplog.text
