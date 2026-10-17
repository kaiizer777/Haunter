"""Issue #112 — SSE poll-loop connection churn + turn-start prune threshold.

Hermetic (fast suite, zero external I/O): every test below drives the code with
fake sessions and a virtual clock, so the assertions are exact counts against no
database and with no real sleeping.

PR #119 already shipped the mirror batching and the threshold-gated prune, so
these cover the two acceptance criteria that were still open:

  1. **AC1** — an idle SSE resume no longer scales its database cost 1:1 with
     wall-clock time. ``_stream_from_db`` opens one short-lived session per poll
     tick (mandatory under ``NullPool``), so the tick rate *is* the connection
     churn rate. The poll sleep now backs off exponentially (1s → 2s → 4s →
     8s → 15s cap) and resets as soon as events arrive, taking a 900s idle
     window from ~1082 sessions to ~77.
  2. **AC3** — the turn-start prune issues its ``DELETE`` only above
     ``_STREAM_PRUNE_THRESHOLD``; at or below the threshold no ``DELETE`` is
     issued at all, while the ``turn_start`` event is still published.
"""

from __future__ import annotations

import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.llm.exceptions import LLMError
from app.models import SessionStreamEvent
from app.routers.sessions import (
    _SSE_IDLE_BACKOFF_MAX_S,
    _SSE_LIVENESS_PROBE_EVERY_TICKS,
    _SSE_POLL_INTERVAL_S,
    _stream_from_db,
    _sse_idle_delay_s,
)
from app.services.session_orchestrator import (
    _STREAM_PRUNE_THRESHOLD,
    SessionOrchestrator,
)
from app.services.session_streamer import SseQueue

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
    """The short-lived session `_stream_from_db` opens per poll/probe/drain.

    Enforces the `session_id`/`turn_id` scope that `_pending_stmt` applies in
    production, and asserts that scope is still present in the rendered SQL. A
    fake that just returned `rows` would hide a dropped predicate entirely: the
    event-reset test would still pass on rows the real query could never
    return, so the scoping would be pinned by nothing.
    """

    def __init__(
        self,
        rows: list[SessionStreamEvent],
        *,
        session_id: uuid.UUID,
        turn_id: str,
    ) -> None:
        self.rows = rows
        self.session_id = session_id
        self.turn_id = turn_id

    async def execute(self, stmt):
        sql = str(stmt).lower()
        assert "session_stream_events.session_id =" in sql, (
            "poll query lost its session_id scope: " + sql
        )
        assert "session_stream_events.turn_id =" in sql, (
            "poll query lost its turn_id scope: " + sql
        )
        return _Result(
            [
                r
                for r in self.rows
                if r.session_id == self.session_id and r.turn_id == self.turn_id
            ]
        )

    async def rollback(self) -> None:
        pass

    async def __aenter__(self) -> _FakePollDb:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


async def _drive_idle_stream(
    rows_plan: list[list[SessionStreamEvent]],
    *,
    session_id: uuid.UUID | None = None,
    flat_cadence: bool = False,
) -> tuple[int, list[float], list[str]]:
    """Run `_stream_from_db` to completion on a virtual clock.

    Returns ``(sessions_opened, sleep_delays, yielded_chunks)``. Every
    `async_session_maker` call is counted through a factory (`return_value`
    would hand back the same object and hide the churn entirely —
    `app.db.async_session_maker` is imported *inside* the function, which is
    what makes the patch visible). `time.monotonic` and `asyncio.sleep` are
    virtual, so the full 900s idle window is reached in ~80 iterations with
    zero real waiting.

    `rows_plan` is consumed one entry per poll read (the initial replay read
    included); an exhausted plan yields zero rows, i.e. a fully idle stream.

    `session_id` is the session the stream is driven for — pass the same ID the
    `rows_plan` rows were built with. It is generated when omitted, which is
    only safe for fully idle plans (no rows to scope).
    """
    clock = {"t": 1_000.0}
    delays: list[float] = []
    chunks: list[str] = []
    sessions: list[_FakePollDb] = []
    plan = list(rows_plan)
    stream_session_id = session_id if session_id is not None else uuid.uuid4()

    def _monotonic() -> float:
        return clock["t"]

    async def _sleep(delay: float) -> None:
        delays.append(delay)
        clock["t"] += delay

    def _factory() -> _FakePollDb:
        rows = plan.pop(0) if plan else []
        db = _FakePollDb(rows, session_id=stream_session_id, turn_id="turn-1")
        sessions.append(db)
        return db

    with ExitStack() as stack:
        stack.enter_context(
            patch("app.routers.sessions.time.monotonic", side_effect=_monotonic)
        )
        stack.enter_context(patch("asyncio.sleep", new=_sleep))
        stack.enter_context(
            patch("app.db.async_session_maker", side_effect=_factory)
        )
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
        async for chunk in _stream_from_db(
            session_id=stream_session_id,
            turn_id="turn-1",
            last_event_id=0,
            request=None,
        ):
            chunks.append(chunk)

    return len(sessions), delays, chunks


class _FakeMetaDb:
    """Records the statements the turn-start block issues in `meta_db`.

    `scalar()` answers the prior-turn row COUNT the prune is gated on;
    `execute()` records every statement so the test can assert on the presence
    — or the absence — of a DELETE.
    """

    def __init__(self, stale_count: int) -> None:
        self._stale_count = stale_count
        self.count_statements: list[str] = []
        self.statements: list[str] = []
        self.added: list[object] = []
        self.commits = 0

    async def scalar(self, stmt):
        self.count_statements.append(str(stmt).lower())
        return self._stale_count

    async def execute(self, stmt) -> None:
        self.statements.append(str(stmt).lower())

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def __aenter__(self) -> _FakeMetaDb:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    @property
    def deletes(self) -> list[str]:
        return [s for s in self.statements if s.startswith("delete")]


async def _run_turn_start_block(stale_count: int) -> _FakeMetaDb:
    """Drive `SessionOrchestrator._execute` through its turn-start block only.

    The LLM call is stubbed to raise so the turn stops immediately after the
    prune/turn_start transaction: the threshold gate is the unit under test, the
    tool-calling loop is not.
    """
    session_id = uuid.uuid4()

    fake_session = MagicMock()
    fake_session.repo = MagicMock(owner="octocat", name="hello-world")
    fake_session.conversation_history = []
    fake_session.staged_patches = {}
    fake_session.checkpoints = []
    fake_session.branch_name = "main"
    fake_session.base_sha = "0" * 40
    fake_session.status = "active"

    meta_db = _FakeMetaDb(stale_count)
    orch = SessionOrchestrator(
        session_id=session_id,
        db=AsyncMock(),
        gh_token="gh-token",
        turn_id=str(uuid.uuid4()),
    )

    with (
        patch.object(orch, "_load_session", new=AsyncMock(return_value=fake_session)),
        patch("app.db.async_session_maker", side_effect=lambda: meta_db),
        patch(
            "app.services.session_orchestrator.LLMClient.complete",
            new_callable=AsyncMock,
            side_effect=LLMError("stop right after the turn_start block"),
        ),
    ):
        await orch._execute("hello", SseQueue())

    return meta_db


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
    assert [
        _sse_idle_delay_s(streak) for streak in range(0, 8)
    ] == [1.0, 1.0, 2.0, 4.0, 8.0, 15.0, 15.0, 15.0]
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
    sessions_backoff, delays_backoff, _ = await _drive_idle_stream([])
    sessions_flat, delays_flat, _ = await _drive_idle_stream([], flat_cadence=True)

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
    reads, delays, chunks = await _drive_idle_stream(
        [
            [],  # initial replay read — nothing yet
            [],  # empty poll
            [],  # empty poll
            [_thought(session_id, 1)],  # a thought lands
            [],  # empty poll
            [],  # empty poll
            [_done(session_id, 2)],  # terminal event ends the stream
        ],
        session_id=session_id,
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

    # The reset was driven by rows the *scoped* query actually returned: both
    # chunks the stream owns reached the client, in event_id order.
    assert [c.splitlines()[0] for c in chunks] == [
        "event: thought",
        "event: done",
    ]
    assert "id: 1" in chunks[0] and "id: 2" in chunks[1]


@pytest.mark.asyncio
async def test_poll_rows_are_scoped_to_the_streamed_session() -> None:
    """Rows belonging to another session are not delivered and do not un-backoff.

    Guards the test fixture itself. The fake poll DB used to ignore the query
    predicates and hand back whatever the rows plan contained, so a dropped
    `session_id` predicate in `_pending_stmt` — or rows carrying the wrong
    session — would still have produced a passing event-reset test. The fake
    now enforces the scope, and this is what proves the enforcement is live.
    """
    stream_session_id = uuid.uuid4()
    foreign_session_id = uuid.uuid4()

    reads, delays, chunks = await _drive_idle_stream(
        [
            [_thought(foreign_session_id, 1)],  # wrong session — filtered out
            [],  # empty poll
            [_thought(foreign_session_id, 2)],  # wrong session — filtered out
            [_thought(stream_session_id, 1)],  # ours — delivered, resets backoff
            [_done(stream_session_id, 2)],  # ours — terminal, stream ends
        ],
        session_id=stream_session_id,
    )

    # Exactly the two events the streamed session owns reached the client. The
    # foreign rows carry event_id 1 and 2 too, so had the fake not scoped the
    # query they would have displaced both of these — and no terminal event
    # would ever have arrived, since `_stream_from_db` skips event_ids it has
    # already yielded.
    assert [c.splitlines()[0] for c in chunks] == ["event: thought", "event: done"]
    assert "id: 1" in chunks[0]
    assert "id: 2" in chunks[1]

    # Foreign rows read as empty ticks, so the backoff kept ramping right up to
    # our own row — proof the filtered reads were genuinely empty, not passed
    # through with the delay reset.
    assert delays == [
        _SSE_POLL_INTERVAL_S,
        _SSE_POLL_INTERVAL_S,
        2 * _SSE_POLL_INTERVAL_S,
        _SSE_POLL_INTERVAL_S,
    ]

    # 1 initial read + 4 polls, then the terminal event returned.
    assert reads == 5


# ---------------------------------------------------------------------------
# AC3 — turn-start prune threshold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_count", [0, 1, _STREAM_PRUNE_THRESHOLD])
async def test_turn_start_prune_skips_delete_at_or_below_threshold(
    stale_count: int,
) -> None:
    """Negation condition: at or below the threshold, no DELETE is issued.

    AC3. Skipped rows are not lost — they still count toward the next check —
    so the whole point of the gate is that the common case (a few hundred rows
    from the last turn) issues zero DELETEs.
    """
    meta_db = await _run_turn_start_block(stale_count)

    # The prior-turn count IS read every turn, ...
    assert len(meta_db.count_statements) == 1
    assert "session_stream_events" in meta_db.count_statements[0]
    # ... but it is only acted on above the threshold.
    assert meta_db.deletes == []
    assert meta_db.statements == []

    # The turn still starts: turn_start is published and committed.
    assert len(meta_db.added) == 1
    init_event = meta_db.added[0]
    assert isinstance(init_event, SessionStreamEvent)
    assert init_event.event == "turn_start"
    assert init_event.event_id == 0
    assert meta_db.commits == 1


@pytest.mark.asyncio
async def test_turn_start_prune_deletes_once_above_threshold() -> None:
    """Above the threshold exactly one DELETE is issued for prior-turn rows."""
    meta_db = await _run_turn_start_block(_STREAM_PRUNE_THRESHOLD + 1)

    assert len(meta_db.count_statements) == 1
    assert len(meta_db.deletes) == 1
    assert "session_stream_events" in meta_db.deletes[0]
    assert len(meta_db.added) == 1
    assert meta_db.commits == 1

