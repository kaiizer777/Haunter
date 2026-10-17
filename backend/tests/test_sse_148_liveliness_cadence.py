"""Issue #148 — SSE liveliness probe holds a ~5s wall-clock cadence under idle backoff.

Hermetic (fast suite, zero external I/O): both tests drive ``_stream_from_db``
with fake sessions and a virtual clock, so probe timings are exact.

Background: the probe used to be tick-counted (every 5th poll tick). Once #112
added idle backoff (up to 15s per tick), a fully idle stream probed every
5 x 15s = ~75s, so a turn worker that died without mirroring a terminal event
left the client tab hanging ~75s. The probe is now driven by elapsed time
(``last_probe_at`` vs ``_SSE_LIVENESS_PROBE_INTERVAL_S``) and the poll sleep is
capped so the loop always wakes when a probe is due.
"""

from __future__ import annotations

import uuid
from contextlib import ExitStack
from unittest.mock import patch

import pytest

from app.models import SessionStreamEvent
from app.routers.sessions import (
    _SSE_DRAIN_INTERVAL_S,
    _SSE_IDLE_BACKOFF_MAX_S,
    _SSE_LIVENESS_PROBE_INTERVAL_S,
    _stream_from_db,
    _sse_idle_delay_s,
)

#: Mirrors the hard-coded `timeout_s` in the poll loop (Lambda budget).
_IDLE_WINDOW_S = 900.0


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


class _FakeDb:
    """Short-lived session stand-in: every poll reads empty (fully idle)."""

    async def execute(self, stmt):
        return _Result([])

    async def rollback(self) -> None:
        pass

    async def __aenter__(self) -> _FakeDb:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


async def _run_idle_stream(
    release_at: float | None,
) -> tuple[list[float], list[float], float]:
    """Drive `_stream_from_db` to completion on a virtual clock starting at t=1000.

    Returns ``(probe_times, sleep_delays, end_time)``. The turn lock reads as
    held until the virtual clock reaches `release_at` (`None` = never, so the
    stream idles until the 900s timeout); the drain then finds no rows and the
    stream closes.
    """
    clock = {"t": 1_000.0}
    probe_times: list[float] = []
    delays: list[float] = []
    session_id = uuid.uuid4()

    def _monotonic() -> float:
        return clock["t"]

    async def _sleep(delay: float) -> None:
        delays.append(delay)
        clock["t"] += delay

    async def _probe(db, sid) -> bool:
        probe_times.append(clock["t"])
        return release_at is None or clock["t"] < release_at

    with ExitStack() as stack:
        stack.enter_context(
            patch("app.routers.sessions.time.monotonic", side_effect=_monotonic)
        )
        stack.enter_context(patch("asyncio.sleep", new=_sleep))
        stack.enter_context(
            patch("app.db.async_session_maker", side_effect=lambda: _FakeDb())
        )
        stack.enter_context(
            patch("app.routers.sessions._is_turn_active_elsewhere", new=_probe)
        )
        async for _chunk in _stream_from_db(
            session_id=session_id,
            turn_id="turn-1",
            last_event_id=0,
            request=None,
        ):
            pass

    return probe_times, delays, clock["t"]


@pytest.mark.asyncio
async def test_probe_holds_5s_cadence_under_full_idle_backoff() -> None:
    """The probe fires every ~5s even once the poll sleep saturates at 15s."""
    probe_times, delays, end_time = await _run_idle_stream(None)

    # The stream really did idle out the whole 900s window.
    assert end_time - 1_000.0 >= _IDLE_WINDOW_S

    # First probe after one interval — not after 5 backoff ticks (~16s) — and
    # steady-state gaps stay at the interval instead of stretching to ~75s.
    assert probe_times[0] - 1_000.0 == pytest.approx(_SSE_LIVENESS_PROBE_INTERVAL_S)
    gaps = [b - a for a, b in zip(probe_times, probe_times[1:], strict=False)]
    assert gaps, "probe never fired on a fully idle stream"
    assert max(gaps) == pytest.approx(_SSE_LIVENESS_PROBE_INTERVAL_S)
    assert min(gaps) == pytest.approx(_SSE_LIVENESS_PROBE_INTERVAL_S)

    # The test really exercises the backoff interaction: without the probe cap
    # the 4th sleep would be the 4s backoff step, and the poll sleep would keep
    # ramping to the 15s cap. Instead the probe cap cuts it short and every
    # later wake is probe-driven.
    assert _sse_idle_delay_s(3) == 4.0  # what backoff alone would sleep
    assert delays[3] < _sse_idle_delay_s(3)
    assert _SSE_IDLE_BACKOFF_MAX_S not in delays


@pytest.mark.asyncio
async def test_dead_worker_recovers_within_one_probe_interval() -> None:
    """A lock release mid-backoff closes the stream ~5s later, not ~75s later."""
    release_at = 1_000.0 + 202.0  # deliberately mid-probe-interval

    probe_times, _delays, end_time = await _run_idle_stream(release_at)

    assert probe_times, "probe never fired before the lock released"
    recovery = end_time - release_at
    # Detection at the next probe (<= one interval) plus the bounded terminal
    # drain (3 x 0.5s). Under the old tick-counted probe this took up to ~75s
    # of detection alone.
    assert recovery <= _SSE_LIVENESS_PROBE_INTERVAL_S + 3 * _SSE_DRAIN_INTERVAL_S
    assert recovery >= 0
