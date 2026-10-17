"""Issue #112 — SSE poll-loop connection churn.

Hermetic (fast suite, zero external I/O): every test below drives the code with
fake sessions and a virtual clock, so the assertions are exact counts against no
database and with no real sleeping.

This covers AC1 — an idle SSE resume no longer scales its database cost 1:1 with
wall-clock time. ``_stream_from_db`` opens one short-lived session per poll tick
(mandatory under ``NullPool``), so the tick rate *is* the connection churn rate.
The poll sleep now backs off exponentially (1s → 2s → 4s → 8s → 15s cap) and
resets as soon as events arrive, taking a 900s idle window from ~1082 sessions
to ~77.
"""

from __future__ import annotations

import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from app.models import SessionStreamEvent
from app.routers.sessions import (
    _SSE_IDLE_BACKOFF_MAX_S,
    _SSE_LIVENESS_PROBE_EVERY_TICKS,
    _SSE_POLL_INTERVAL_S,
    _stream_from_db,
    _sse_idle_delay_s,
)

#: Window `_stream_from_db` waits before giving up on a turn (Lambda budget).
#: Mirrors the hard-coded `timeout_s` in the poll loop.
_IDLE_WINDOW_S = 900.0


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Result:
    """Stand-in for a SQLAlchemy result of `SELECT session_stream_events`."""

    def __init__(self, rows: list[SessionStreamEvent]) -> None:
        self._rows = rows

    def scalars(self):
        class _Scalars:
            def __init__(self, rows: list[SessionStreamEvent]) -> None:
                self._rows = rows

            def all(self) -> list[SessionStreamEvent]:
                return self._rows

        return _Scalars(self._rows)


class _FakePollDb:
    """The short-lived session `_stream_from_db` opens per poll/probe/drain."""

    def __init__(self, rows: list[SessionStreamEvent]) -> None:
        self.rows = rows

    async def execute(self, stmt):
        return _Result(self.rows)

    async def rollback(self) -> None:
        pass

    async def __aenter__(self) -> _FakePollDb:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


async def _drive_idle_stream(
    rows_plan: list[list[SessionStreamEvent]],
    *,
    flat_cadence: bool = False,
) -> tuple[int, list[float]]:
    """Run `_stream_from_db` to completion on a virtual clock.

    Returns ``(sessions_opened, sleep_delays)``. Every `async_session_maker`
    call is counted through a factory (`return_value` would hand back the same
    object and hide the churn entirely — `app.db.async_session_maker` is
    imported *inside* the function, which is what makes the patch visible).
    `time.monotonic` and `asyncio.sleep` are virtual, so the full 900s idle
    window is reached in ~80 iterations with zero real waiting.

    `rows_plan` is consumed one entry per poll read (the initial replay read
    included); an exhausted plan yields zero rows, i.e. a fully idle stream.
    """
    clock = {"t": 1_000.0}
    delays: list[float] = []
    sessions: list[_FakePollDb] = []
    plan = list(rows_plan)

    def _monotonic() -> float:
        return clock["t"]

    async def _sleep(delay: float) -> None:
        delays.append(delay)
        clock["t"] += delay

    def _factory() -> _FakePollDb:
        rows = plan.pop(0) if plan else []
        db = _FakePollDb(rows)
        sessions.append(db)
        return db

    with ExitStack() as stack:
        stack.enter_context(
            patch("app.routers.sessions.time.monotonic", side_effect=_monotonic)
        )
        stack.enter_context(patch("asyncio.sleep", new=_sleep))
        stack.enter_context(patch("app.db.async_session_maker", side_effect=_factory))
        # Turn never releases its row lock: the stream idles until the timeout.
        stack.enter_context(
            patch(
                "app.routers.sessions._is_turn_active_elsewhere",
                new_callable=AsyncMock,
                return_value=True,
            )
        )
        if flat_cadence:
            # The pre-#112 cadence: a flat `_SSE_POLL_INTERVAL_S` sleep per tick.
            stack.enter_context(
                patch(
                    "app.routers.sessions._sse_idle_delay_s",
                    side_effect=lambda _streak: _SSE_POLL_INTERVAL_S,
                )
            )
        async for _ in _stream_from_db(
            session_id=uuid.uuid4(), turn_id="turn-1", last_event_id=0, request=None
        ):
            pass

    return len(sessions), delays


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


# ---------------------------------------------------------------------------
# AC1 — SSE poll loop session churn
# ---------------------------------------------------------------------------


def test_idle_delay_doubles_then_clamps_to_cap() -> None:
    """The delay doubles per empty streak and never exceeds the cap."""
    assert _SSE_IDLE_BACKOFF_MAX_S == 15.0
    assert _sse_idle_delay_s(-1) == _SSE_POLL_INTERVAL_S
    assert _sse_idle_delay_s(0) == _SSE_POLL_INTERVAL_S
    assert [_sse_idle_delay_s(streak) for streak in range(0, 8)] == [
        1.0,
        1.0,
        2.0,
        4.0,
        8.0,
        15.0,
        15.0,
        15.0,
    ]
    # A streak long enough to overflow the float exponent still clamps.
    assert _sse_idle_delay_s(5_000) == _SSE_IDLE_BACKOFF_MAX_S


@pytest.mark.asyncio
async def test_idle_stream_sessions_do_not_scale_with_wall_clock() -> None:
    """A 900s idle resume opens a bounded number of sessions, not one per second.

    This is AC1. Pre-#112 the loop slept a flat `_SSE_POLL_INTERVAL_S` and
    opened a session per tick, so an idle tab cost >1 session/sec for the whole
    15-minute Lambda window. The backoff must keep that cost proportional to
    `_SSE_IDLE_BACKOFF_MAX_S` instead, which is what makes it bounded.
    """
    sessions_backoff, delays_backoff = await _drive_idle_stream([])
    sessions_flat, delays_flat = await _drive_idle_stream([], flat_cadence=True)

    # Both runs really did idle for the whole 900s window.
    assert sum(delays_flat) >= _IDLE_WINDOW_S
    assert sum(delays_backoff) >= _IDLE_WINDOW_S

    # The defect being pinned: ~1.2 sessions/sec while idle.
    assert sessions_flat > _IDLE_WINDOW_S / _SSE_POLL_INTERVAL_S

    # Bounded now: ~77 sessions (~14x fewer) and ~0.09 sessions/sec.
    assert sessions_backoff <= sessions_flat // 10
    assert sessions_backoff <= 100
    assert sessions_backoff / _IDLE_WINDOW_S <= 0.1

    # The reduction comes from real backoff, not from a smaller constant sleep.
    mean_delay = sum(delays_backoff) / len(delays_backoff)
    assert mean_delay >= 5 * _SSE_POLL_INTERVAL_S
    assert delays_backoff[:6] == [
        _SSE_POLL_INTERVAL_S,
        _SSE_POLL_INTERVAL_S,
        2 * _SSE_POLL_INTERVAL_S,
        4 * _SSE_POLL_INTERVAL_S,
        8 * _SSE_POLL_INTERVAL_S,
        _SSE_IDLE_BACKOFF_MAX_S,
    ]
    # Once saturated every subsequent tick sleeps exactly the cap, so the worst
    # case added event latency on an idle stream is the cap.
    assert set(delays_backoff[5:]) == {_SSE_IDLE_BACKOFF_MAX_S}


@pytest.mark.asyncio
async def test_idle_delay_resets_when_events_arrive() -> None:
    """An active stream keeps the 1s cadence: the backoff resets on real rows."""
    session_id = uuid.uuid4()
    reads, delays = await _drive_idle_stream(
        [
            [],  # initial replay read — nothing yet
            [],  # empty poll
            [],  # empty poll
            [_thought(session_id, 1)],  # a thought lands
            [],  # empty poll
            [],  # empty poll
            [_done(session_id, 2)],  # terminal event ends the stream
        ]
    )

    # Ramp before the thought, reset to the base interval after it.
    assert delays[:3] == [1.0, 1.0, 2.0]
    assert delays[3] == _SSE_POLL_INTERVAL_S
    assert delays[3:6] == [
        _SSE_POLL_INTERVAL_S,
        _SSE_POLL_INTERVAL_S,
        2 * _SSE_POLL_INTERVAL_S,
    ]
    # One session per read (initial read + 6 polls) — the liveliness probe never
    # fired: the probe cadence is measured in ticks and resets on rows too.
    assert reads == 7
    assert reads - 1 < _SSE_LIVENESS_PROBE_EVERY_TICKS * 2

