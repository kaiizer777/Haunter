"""Issue #165 — SSE poll loop holds one DB connection per stream.

Hermetic (fast suite, zero external I/O): drives ``_stream_from_db`` with a
fake engine and a virtual clock.

Under the ``NullPool`` engine every fresh session checkout is a brand-new
physical connection, so opening a session per poll tick churned one
TCP/TLS handshake per tick. ``_stream_from_db`` now holds a single shared
engine connection for the whole stream: this pins ``connects == 1`` across
a multi-poll stream while every poll still issues its own ``SELECT`` (and
rolls back right after the read so the held connection never sits
idle-in-transaction across sleeps).
"""

from __future__ import annotations

import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from app.models import SessionStreamEvent
from app.routers.sessions import _SSE_POLL_INTERVAL_S, _stream_from_db


class _Result:
    """Stand-in for a SQLAlchemy result of `SELECT session_stream_events`."""

    def __init__(self, rows: list[SessionStreamEvent]) -> None:
        self._rows = rows

    def scalars(self):
        rows = self._rows

        class _Scalars:
            def all(self) -> list[SessionStreamEvent]:
                return rows

        return _Scalars()


class _FakeConn:
    """One held connection: every poll reuses it, each read ends in rollback."""

    def __init__(
        self,
        plan: list[list[SessionStreamEvent]],
        *,
        session_id: uuid.UUID,
        turn_id: str,
    ) -> None:
        self._plan = list(plan)
        self._session_id = session_id
        self._turn_id = turn_id
        self.executes = 0
        self.rollbacks = 0

    async def execute(self, stmt):
        self.executes += 1
        rows = self._plan.pop(0) if self._plan else []
        return _Result(
            [
                r
                for r in rows
                if r.session_id == self._session_id and r.turn_id == self._turn_id
            ]
        )

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeEngine:
    """Counts physical checkouts: `connect()` is 1:1 with a DB connection."""

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn
        self.connects = 0

    def connect(self) -> _FakeConn:
        self.connects += 1
        return self._conn


def _thought(session_id: uuid.UUID, event_id: int) -> SessionStreamEvent:
    return SessionStreamEvent(
        id=event_id,
        session_id=session_id,
        turn_id="turn-1",
        event_id=event_id,
        event="thought",
        chunk=f"event: thought\nid: {event_id}\ndata: hello\n\n",
    )


def _done(session_id: uuid.UUID, event_id: int) -> SessionStreamEvent:
    return SessionStreamEvent(
        id=event_id,
        session_id=session_id,
        turn_id="turn-1",
        event_id=event_id,
        event="done",
        chunk=f"event: done\nid: {event_id}\ndata: " + '{"ok":true}\n\n',
    )


@pytest.mark.asyncio
async def test_poll_stream_holds_a_single_connection() -> None:
    """Many polls, one connection: the tick rate is no longer the churn rate."""
    session_id = uuid.uuid4()
    clock = {"t": 1_000.0}
    delays: list[float] = []
    chunks: list[str] = []
    conn = _FakeConn(
        [[], [], [_thought(session_id, 1)], [], [_done(session_id, 2)]],
        session_id=session_id,
        turn_id="turn-1",
    )
    engine = _FakeEngine(conn)

    def _monotonic() -> float:
        return clock["t"]

    async def _sleep(delay: float) -> None:
        delays.append(delay)
        clock["t"] += delay

    with ExitStack() as stack:
        stack.enter_context(
            patch("app.routers.sessions.time.monotonic", side_effect=_monotonic)
        )
        stack.enter_context(patch("asyncio.sleep", new=_sleep))
        stack.enter_context(patch("app.db.engine", engine))
        probe = stack.enter_context(
            patch(
                "app.routers.sessions._is_turn_active_elsewhere",
                new_callable=AsyncMock,
                return_value=True,
            )
        )
        async for chunk in _stream_from_db(
            session_id=session_id,
            turn_id="turn-1",
            last_event_id=0,
            request=None,
        ):
            chunks.append(chunk)

    # The whole stream — initial replay plus four polls — rode one checkout.
    assert engine.connects == 1
    assert conn.executes == 5
    # Every read batch ended its transaction: the held connection never sat
    # idle-in-transaction across a sleep or a client yield.
    assert conn.rollbacks == conn.executes
    # Events still flowed in order with the 1s active cadence intact.
    assert [c.splitlines()[0] for c in chunks] == [
        "event: thought",
        "event: done",
    ]
    assert delays == [
        _SSE_POLL_INTERVAL_S,
        _SSE_POLL_INTERVAL_S,
        _SSE_POLL_INTERVAL_S,
        2 * _SSE_POLL_INTERVAL_S,
    ]
    # Rows kept arriving, so the liveliness probe never needed to fire.
    probe.assert_not_awaited()
