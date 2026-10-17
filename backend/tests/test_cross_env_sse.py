"""
Tests for Issue #96: DB-backed shared SSE replay across multi-env AWS Lambda environments.

Covers:
1. SseQueue mirror_sink invocation & fault tolerance.
2. SessionOrchestrator turn_id initialization and mirror_sink attachment.
3. PostgreSQL locking semantics (FOR NO KEY UPDATE NOWAIT).
4. _stream_from_db replay of already-completed turns without polling delay.
5. _stream_from_db in-flight polling across worker environments.
6. chat_session cross-environment resumption from DB when local _ACTIVE_TURNS is empty.
7. chat_session gap detection (HTTP 410 GONE).
8. chat_session fallback when no turn exists (HTTP 409 vs 410).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.models import AgentSession, SessionStreamEvent, User
from app.routers.sessions import (
    _ACTIVE_TURNS,
    _format_chunk,
    _stream_from_db,
    chat_session,
)
from app.schemas import SessionChatIn
from app.services.session_orchestrator import SessionOrchestrator
from app.services.session_streamer import SseQueue


class _FakeEngine:
    """Presents a connection fake as the shared `app.db.engine` seam (#165)."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def connect(self) -> Any:
        return self._conn


# ---------------------------------------------------------------------------
# 1. SseQueue mirror_sink tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sse_queue_mirror_sink_called() -> None:
    """SseQueue schedules mirror_sink callback for every emitted event."""
    mirrored_events: list[tuple[int, str, str]] = []

    async def mock_sink(event_id: int, event: str, chunk: str) -> None:
        mirrored_events.append((event_id, event, chunk))

    queue = SseQueue(mirror_sink=mock_sink)
    await queue.put_thought("Analyzing requirements...")
    await queue.put_done("session-1", staged_files_count=1)

    # Allow background scheduled tasks to execute
    await asyncio.sleep(0.01)

    assert len(mirrored_events) == 2
    assert mirrored_events[0][0] == 1
    assert mirrored_events[0][1] == "thought"
    assert "Analyzing requirements..." in mirrored_events[0][2]
    assert mirrored_events[1][0] == 2
    assert mirrored_events[1][1] == "done"


@pytest.mark.asyncio
async def test_sse_queue_mirror_sink_error_does_not_break_queue() -> None:
    """Failing mirror_sink does not crash put_event or prevent consumer streaming."""

    async def failing_sink(event_id: int, event: str, chunk: str) -> None:
        raise RuntimeError("Neon database connection dropped")

    queue = SseQueue(mirror_sink=failing_sink)
    # Should not raise
    await queue.put_thought("Safe thought")
    await queue.close()

    chunks = [c async for c in queue.stream()]
    assert len(chunks) == 1
    assert "Safe thought" in chunks[0]


# ---------------------------------------------------------------------------
# 2. SessionOrchestrator turn_id & PostgreSQL locking tests
# ---------------------------------------------------------------------------


def test_session_orchestrator_compiles_for_no_key_update() -> None:
    """PostgreSQL lock compiles to FOR NO KEY UPDATE NOWAIT (permitting child FK inserts)."""
    stmt = (
        select(AgentSession)
        .where(AgentSession.id == uuid.uuid4())
        .with_for_update(nowait=True, key_share=True)
    )
    compiled_sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "FOR NO KEY UPDATE NOWAIT" in compiled_sql


@pytest.mark.asyncio
async def test_session_orchestrator_initializes_turn_id_and_sink() -> None:
    """SessionOrchestrator.run generates turn_id and attaches _mirror_event sink to queue."""
    mock_db = AsyncMock()
    orch = SessionOrchestrator(
        session_id=uuid.uuid4(),
        db=mock_db,
    )
    assert orch.turn_id is None

    queue = SseQueue()
    assert queue.mirror_sink is None

    with patch.object(orch, "_execute", new_callable=AsyncMock) as mock_exec:
        await orch.run("Hello test", queue)
        assert orch.turn_id is not None
        assert len(orch.turn_id) == 36  # Valid UUID4 string
        assert queue.mirror_sink == orch._mirror_event
        mock_exec.assert_awaited_once()


# ---------------------------------------------------------------------------
# 3. _format_chunk helper tests
# ---------------------------------------------------------------------------


def test_format_chunk_preserves_wire_format() -> None:
    wire = "event: thought\nid: 1\ndata: {\"delta\":\"test\"}\n\n"
    assert _format_chunk(1, "thought", wire) == wire

    raw_data = '{"delta":"test"}'
    formatted = _format_chunk(2, "thought", raw_data)
    assert formatted == f"id: 2\nevent: thought\ndata: {raw_data}\n\n"


# ---------------------------------------------------------------------------
# 4. _stream_from_db replay tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_from_db_completed_turn() -> None:
    """A completed turn is replayed from DB in one batch without sleeping or polling."""
    session_id = uuid.uuid4()
    turn_id = str(uuid.uuid4())

    events = [
        SessionStreamEvent(
            id=1,
            session_id=session_id,
            turn_id=turn_id,
            event_id=1,
            event="thought",
            chunk="event: thought\nid: 1\ndata: {\"delta\":\"step 1\"}\n\n",
        ),
        SessionStreamEvent(
            id=2,
            session_id=session_id,
            turn_id=turn_id,
            event_id=2,
            event="thought",
            chunk="event: thought\nid: 2\ndata: {\"delta\":\"step 2\"}\n\n",
        ),
        SessionStreamEvent(
            id=3,
            session_id=session_id,
            turn_id=turn_id,
            event_id=3,
            event="done",
            chunk="event: done\nid: 3\ndata: {\"session_id\":\"...\"}\n\n",
        ),
    ]

    class _MockResult:
        def scalars(self):
            class _Scalars:
                def all(self):
                    return events[1:]  # event_id > 1 -> events 2 and 3
            return _Scalars()

    class _MockDb:
        async def execute(self, stmt):
            return _MockResult()

        async def rollback(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    with patch("app.db.async_session_maker", return_value=_MockDb()):
        yielded: list[str] = []
        async for chunk in _stream_from_db(session_id, turn_id, last_event_id=1):
            yielded.append(chunk)

        assert len(yielded) == 2
        assert "step 2" in yielded[0]
        assert "event: done" in yielded[1]


@pytest.mark.asyncio
async def test_stream_from_db_inflight_polling() -> None:
    """In-flight turn streams initial events, polls DB for new events, and completes on done."""
    session_id = uuid.uuid4()
    turn_id = str(uuid.uuid4())

    event1 = SessionStreamEvent(
        id=1,
        session_id=session_id,
        turn_id=turn_id,
        event_id=1,
        event="thought",
        chunk="event: thought\nid: 1\ndata: {\"delta\":\"analyzing\"}\n\n",
    )
    event2 = SessionStreamEvent(
        id=2,
        session_id=session_id,
        turn_id=turn_id,
        event_id=2,
        event="done",
        chunk="event: done\nid: 2\ndata: {\"session_id\":\"...\"}\n\n",
    )

    query_count = 0

    class _MockResult:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            class _Scalars:
                def __init__(self, r):
                    self._r = r
                def all(self):
                    return self._r
            return _Scalars(self._rows)

    class _MockDb:
        async def execute(self, stmt):
            nonlocal query_count
            query_count += 1
            if query_count == 1:
                # First query (initial batch): only event 1 is available
                return _MockResult([event1])
            # Second query (polling tick): event 2 arrives
            return _MockResult([event2])

        async def rollback(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    with (
        patch("app.db.async_session_maker", return_value=_MockDb()),
        patch("app.routers.sessions._is_turn_active_elsewhere", new_callable=AsyncMock, return_value=True),
    ):
        yielded: list[str] = []
        async for chunk in _stream_from_db(session_id, turn_id, last_event_id=0):
            yielded.append(chunk)

        assert len(yielded) == 2
        assert "analyzing" in yielded[0]
        assert "event: done" in yielded[1]


# ---------------------------------------------------------------------------
# 5. chat_session cross-env resumption tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_session_cross_env_replays_completed_turn() -> None:
    """Worker B (empty _ACTIVE_TURNS) replays completed turn from DB instead of returning 410."""
    session_id = uuid.uuid4()
    turn_id = str(uuid.uuid4())
    user_id = uuid.uuid4()

    mock_user = MagicMock(spec=User)
    mock_user.id = user_id

    mock_session = MagicMock(spec=AgentSession)
    mock_session.id = session_id
    mock_session.user_id = user_id
    mock_session.status = "active"
    mock_session.active_turn_id = turn_id

    # Ensure worker B has empty _ACTIVE_TURNS
    _ACTIVE_TURNS.pop(str(session_id), None)

    class _BoundsResult:
        def first(self):
            return (1, 3)  # min=1, max=3

    class _SessionResult:
        def scalars(self):
            class _S:
                def first(self):
                    return mock_session
            return _S()

    class _MockDb:
        async def execute(self, stmt):
            stmt_str = str(stmt)
            if "agent_sessions" in stmt_str:
                return _SessionResult()
            if "min" in stmt_str.lower():
                return _BoundsResult()
            return _SessionResult()

        async def commit(self):
            pass

        async def rollback(self):
            pass

    events = [
        SessionStreamEvent(
            id=2,
            session_id=session_id,
            turn_id=turn_id,
            event_id=2,
            event="thought",
            chunk="event: thought\nid: 2\ndata: {\"delta\":\"step 2\"}\n\n",
        ),
        SessionStreamEvent(
            id=3,
            session_id=session_id,
            turn_id=turn_id,
            event_id=3,
            event="done",
            chunk="event: done\nid: 3\ndata: {\"session_id\":\"123\"}\n\n",
        ),
    ]

    class _ReplayDb:
        async def execute(self, stmt):
            class _MockRes:
                def scalars(self):
                    class _S:
                        def all(self):
                            return events
                    return _S()
            return _MockRes()

        async def rollback(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    body = SessionChatIn(message="Resume")
    with patch("app.db.async_session_maker", return_value=_ReplayDb()):
        resp = await chat_session(
            session_id=session_id,
            body=body,
            current_user=mock_user,
            db=_MockDb(),  # type: ignore[arg-type]
            last_event_id_header="1",
        )

        assert resp.status_code == 200
        assert resp.media_type == "text/event-stream"
        chunks = [c async for c in resp.body_iterator]
        assert len(chunks) == 2
        assert "event: thought" in chunks[0]
        assert "event: done" in chunks[1]


@pytest.mark.asyncio
async def test_chat_session_cross_env_rejects_gap() -> None:
    """Worker B returns 410 GONE when Last-Event-ID predates the retained DB event window."""
    session_id = uuid.uuid4()
    turn_id = str(uuid.uuid4())
    user_id = uuid.uuid4()

    mock_user = MagicMock(spec=User)
    mock_user.id = user_id

    mock_session = MagicMock(spec=AgentSession)
    mock_session.id = session_id
    mock_session.user_id = user_id
    mock_session.status = "active"
    mock_session.active_turn_id = turn_id

    _ACTIVE_TURNS.pop(str(session_id), None)

    class _BoundsResult:
        def first(self):
            return (10, 15)  # Earliest available event is 10; events 1..9 evicted

    class _SessionResult:
        def scalars(self):
            class _S:
                def first(self):
                    return mock_session
            return _S()

    class _MockDb:
        async def execute(self, stmt):
            stmt_str = str(stmt)
            if "agent_sessions" in stmt_str:
                return _SessionResult()
            if "min" in stmt_str.lower():
                return _BoundsResult()
            return _SessionResult()

    body = SessionChatIn(message="Resume")
    with pytest.raises(HTTPException) as exc_info:
        await chat_session(
            session_id=session_id,
            body=body,
            current_user=mock_user,
            db=_MockDb(),  # type: ignore[arg-type]
            last_event_id_header="5",  # 5 < 10 - 1 -> GAP!
        )

    assert exc_info.value.status_code == 410
    assert "RESUME_GAP" in exc_info.value.detail


@pytest.mark.asyncio
async def test_chat_session_cross_env_no_turn_active_elsewhere() -> None:
    """Worker B returns 409 TURN_ACTIVE_ELSEWHERE when active_turn_id is None but row lock is held."""
    session_id = uuid.uuid4()
    user_id = uuid.uuid4()

    mock_user = MagicMock(spec=User)
    mock_user.id = user_id

    mock_session = MagicMock(spec=AgentSession)
    mock_session.id = session_id
    mock_session.user_id = user_id
    mock_session.status = "active"
    mock_session.active_turn_id = None

    _ACTIVE_TURNS.pop(str(session_id), None)

    class _SessionResult:
        def scalars(self):
            class _S:
                def first(self):
                    return mock_session
            return _S()

    class _EmptyResult:
        def scalars(self):
            class _S:
                def first(self):
                    return None
            return _S()

    class _MockDb:
        async def execute(self, stmt):
            stmt_str = str(stmt).lower()
            if "session_stream_events" in stmt_str:
                return _EmptyResult()
            return _SessionResult()

    body = SessionChatIn(message="Resume")
    with (
        patch("app.routers.sessions._is_turn_active_elsewhere", new_callable=AsyncMock, return_value=True),
        pytest.raises(HTTPException) as exc_info,
    ):
        await chat_session(
            session_id=session_id,
            body=body,
            current_user=mock_user,
            db=_MockDb(),  # type: ignore[arg-type]
            last_event_id_header="1",
        )

    assert exc_info.value.status_code == 409
    assert "TURN_ACTIVE_ELSEWHERE" in exc_info.value.detail


@pytest.mark.asyncio
async def test_stream_from_db_filters_out_turn_start_events() -> None:
    """_stream_from_db filters out internal event_id=0 / turn_start events."""
    session_id = uuid.uuid4()
    turn_id = str(uuid.uuid4())

    init_event = SessionStreamEvent(
        id=1,
        session_id=session_id,
        turn_id=turn_id,
        event_id=0,
        event="turn_start",
        chunk="",
    )
    thought_event = SessionStreamEvent(
        id=2,
        session_id=session_id,
        turn_id=turn_id,
        event_id=1,
        event="thought",
        chunk="event: thought\nid: 1\ndata: {\"delta\":\"hello\"}\n\n",
    )
    done_event = SessionStreamEvent(
        id=3,
        session_id=session_id,
        turn_id=turn_id,
        event_id=2,
        event="done",
        chunk="event: done\nid: 2\ndata: {\"session_id\":\"...\"}\n\n",
    )

    class _MockResult:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            class _Scalars:
                def __init__(self, r):
                    self._r = r
                def all(self):
                    return self._r
            return _Scalars(self._rows)

    class _MockDb:
        async def execute(self, stmt):
            return _MockResult([init_event, thought_event, done_event])

        async def rollback(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    with patch("app.db.async_session_maker", return_value=_MockDb()):
        yielded: list[str] = []
        async for chunk in _stream_from_db(session_id, turn_id, last_event_id=0):
            yielded.append(chunk)

        assert len(yielded) == 2
        assert "turn_start" not in yielded[0]
        assert "hello" in yielded[0]
        assert "event: done" in yielded[1]


@pytest.mark.asyncio
async def test_chat_session_cross_env_reconnect_retry_discovery_success() -> None:
    """Worker B reconnect retries discovery when turn is active elsewhere and discovers new turn_id."""
    session_id = uuid.uuid4()
    user_id = uuid.uuid4()
    new_turn_id = str(uuid.uuid4())

    mock_user = MagicMock(spec=User)
    mock_user.id = user_id

    mock_session = MagicMock(spec=AgentSession)
    mock_session.id = session_id
    mock_session.user_id = user_id
    mock_session.status = "active"
    mock_session.active_turn_id = None

    _ACTIVE_TURNS.pop(str(session_id), None)

    class _SessionResult:
        def scalars(self):
            class _S:
                def first(self):
                    return mock_session
            return _S()

    query_attempt = 0

    class _TurnResult:
        def __init__(self, val):
            self._val = val
        def scalars(self):
            class _S:
                def __init__(self, v):
                    self._v = v
                def first(self):
                    return self._v
            return _S(self._val)

    class _BoundsResult:
        def first(self):
            return (0, 0)  # Only turn_start event 0 exists so far

    class _MockDb:
        async def execute(self, stmt):
            nonlocal query_attempt
            stmt_str = str(stmt).lower()
            if "agent_sessions" in stmt_str:
                return _SessionResult()
            if "min" in stmt_str:
                return _BoundsResult()
            if "session_stream_events" in stmt_str:
                query_attempt += 1
                if query_attempt == 1:
                    # Initially, turn_start event is still being written
                    return _TurnResult(None)
                # On retry, turn_id is discovered
                return _TurnResult(new_turn_id)
            return _SessionResult()

    body = SessionChatIn(message="Resume")
    with (
        patch("app.routers.sessions._is_turn_active_elsewhere", new_callable=AsyncMock, return_value=True),
        patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        resp = await chat_session(
            session_id=session_id,
            body=body,
            current_user=mock_user,
            db=_MockDb(),  # type: ignore[arg-type]
            last_event_id_header="0",
        )

        assert resp.status_code == 200
        assert mock_sleep.await_count >= 1


@pytest.mark.asyncio
async def test_stream_from_db_deduplicates_duplicate_event_ids() -> None:
    """_stream_from_db guarantees monotonic event_id order and deduplicates identical event_ids."""
    session_id = uuid.uuid4()
    turn_id = str(uuid.uuid4())

    events = [
        SessionStreamEvent(
            id=1,
            session_id=session_id,
            turn_id=turn_id,
            event_id=1,
            event="thought",
            chunk='event: thought\nid: 1\ndata: {"delta":"first"}\n\n',
        ),
        SessionStreamEvent(
            id=2,
            session_id=session_id,
            turn_id=turn_id,
            event_id=1,  # Duplicate event_id from ambiguous commit retry
            event="thought",
            chunk='event: thought\nid: 1\ndata: {"delta":"duplicate"}\n\n',
        ),
        SessionStreamEvent(
            id=3,
            session_id=session_id,
            turn_id=turn_id,
            event_id=2,
            event="done",
            chunk='event: done\nid: 2\ndata: {"session_id":"..."}\n\n',
        ),
    ]

    class _MockResult:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            class _Scalars:
                def __init__(self, r):
                    self._r = r

                def all(self):
                    return self._r

            return _Scalars(self._rows)

    class _MockSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, stmt):
            return _MockResult(events)

        async def rollback(self):
            pass

    from app.routers.sessions import _stream_from_db

    with patch("app.db.async_session_maker", return_value=_MockSession()):
        chunks = [
            chunk
            async for chunk in _stream_from_db(
                session_id=session_id,
                turn_id=turn_id,
                last_event_id=0,
                request=None,
            )
        ]

    # Only 2 events should be yielded (id: 1 and id: 2), duplicate id: 1 must be discarded
    assert len(chunks) == 2
    assert "id: 1" in chunks[0]
    assert "first" in chunks[0]
    assert "id: 2" in chunks[1]


@pytest.mark.asyncio
async def test_flush_mirror_batch_idempotent_fallback() -> None:
    """_flush_mirror_batch handles ambiguous commit failures by falling back per-event with conflict handling."""
    from app.services.session_orchestrator import SessionOrchestrator
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.dialects.postgresql import Insert as PGInsert

    session_id = uuid.uuid4()
    turn_id = str(uuid.uuid4())

    orch = SessionOrchestrator(
        session_id=session_id,
        db=AsyncMock(),
        gh_token="dummy",
        turn_id=turn_id,
    )

    batch = [
        (1, "thought", 'event: thought\nid: 1\ndata: {"delta":"hi"}\n\n'),
        (2, "done", 'event: done\nid: 2\ndata: {"ok":true}\n\n'),
    ]

    # Simulate batch commit failure, followed by per-event fallback where events are
    # inserted with PostgreSQL on_conflict_do_nothing.
    class _FailingBatchDb:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def add_all(self, items):
            pass

        async def execute(self, stmt):
            pass

        async def commit(self):
            raise RuntimeError("Connection dropped right after commit")

    class _FallbackDb:
        def __init__(self):
            self.executed = []
            self.commit_count = 0
            bind_mock = MagicMock()
            bind_mock.dialect.name = "postgresql"
            self.bind = bind_mock

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def add(self, item):
            self.item = item

        async def execute(self, stmt):
            self.executed.append(stmt)

        async def commit(self):
            self.commit_count += 1

    failing_db = _FailingBatchDb()
    fallback_db = _FallbackDb()
    call_count = 0

    def _session_factory():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return failing_db
        return fallback_db

    with patch("app.db.async_session_maker", side_effect=_session_factory):
        await orch._flush_mirror_batch(batch)

    # Batch should be cleared and execution must succeed without raising
    assert len(batch) == 0
    assert len(fallback_db.executed) == 2
    assert fallback_db.commit_count == 2
    for stmt in fallback_db.executed:
        assert isinstance(stmt, PGInsert)
        compiled_sql = str(stmt.compile(dialect=postgresql.dialect()))
        assert "ON CONFLICT (session_id, turn_id, event_id) DO NOTHING" in compiled_sql


# ---------------------------------------------------------------------------
# Issue #117: bounded turn-discovery retries on chat resumption
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_discovery_bounded_attempts_on_persistent_failure() -> None:
    """Persistent discovery failure retries at most MAX times, then surfaces 409."""
    from app.routers.sessions import (
        _TURN_DISCOVERY_BASE_DELAY_S,
        _TURN_DISCOVERY_DEADLINE_S,
        _TURN_DISCOVERY_MAX_ATTEMPTS,
        _TURN_DISCOVERY_MAX_DELAY_S,
    )

    assert _TURN_DISCOVERY_MAX_ATTEMPTS <= 5
    assert _TURN_DISCOVERY_DEADLINE_S <= 2.5

    session_id = uuid.uuid4()
    user_id = uuid.uuid4()

    mock_user = MagicMock(spec=User)
    mock_user.id = user_id

    mock_session = MagicMock(spec=AgentSession)
    mock_session.id = session_id
    mock_session.user_id = user_id
    mock_session.status = "active"
    mock_session.active_turn_id = None

    _ACTIVE_TURNS.pop(str(session_id), None)

    class _SessionResult:
        def scalars(self):
            class _S:
                def first(self):
                    return mock_session
            return _S()

    class _EmptyResult:
        def scalars(self):
            class _S:
                def first(self):
                    return None
            return _S()

    stream_reads = 0

    class _MockDb:
        async def execute(self, stmt):
            nonlocal stream_reads
            stmt_str = str(stmt).lower()
            if "session_stream_events" in stmt_str:
                stream_reads += 1
                return _EmptyResult()
            return _SessionResult()

    jitter_caps: list[float] = []
    real_uniform = __import__("random").uniform

    def _recording_uniform(a: float, b: float) -> float:
        jitter_caps.append(b)
        return real_uniform(a, b)

    body = SessionChatIn(message="Resume")
    with (
        patch("app.routers.sessions._is_turn_active_elsewhere", new_callable=AsyncMock, return_value=True),
        patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        patch("app.routers.sessions._random.uniform", side_effect=_recording_uniform),
        pytest.raises(HTTPException) as exc_info,
    ):
        await chat_session(
            session_id=session_id,
            body=body,
            current_user=mock_user,
            db=_MockDb(),  # type: ignore[arg-type]
            last_event_id_header="1",
        )

    # User-visible error surfaces on exhaustion.
    assert exc_info.value.status_code == 409
    assert "TURN_ACTIVE_ELSEWHERE" in exc_info.value.detail
    # 1 initial read + at most MAX discovery retries (was 1 + 25).
    assert stream_reads <= 1 + _TURN_DISCOVERY_MAX_ATTEMPTS
    assert mock_sleep.await_count <= _TURN_DISCOVERY_MAX_ATTEMPTS
    # Exponential backoff caps: base * 2^n, clamped at the max delay.
    expected_caps = [
        min(_TURN_DISCOVERY_BASE_DELAY_S * (2**n), _TURN_DISCOVERY_MAX_DELAY_S)
        for n in range(_TURN_DISCOVERY_MAX_ATTEMPTS)
    ]
    assert jitter_caps == pytest.approx(expected_caps)


@pytest.mark.asyncio
async def test_double_resume_persistent_409_issues_no_writes() -> None:
    """Persistent-failure resume path: two retries surface 409 with zero writes on the chat db."""
    session_id = uuid.uuid4()
    user_id = uuid.uuid4()

    mock_user = MagicMock(spec=User)
    mock_user.id = user_id

    mock_session = MagicMock(spec=AgentSession)
    mock_session.id = session_id
    mock_session.user_id = user_id
    mock_session.status = "active"
    mock_session.active_turn_id = None

    _ACTIVE_TURNS.pop(str(session_id), None)

    class _SessionResult:
        def scalars(self):
            class _S:
                def first(self):
                    return mock_session
            return _S()

    class _EmptyResult:
        def scalars(self):
            class _S:
                def first(self):
                    return None
            return _S()

    class _MockDb:
        def __init__(self) -> None:
            self.add = MagicMock()
            self.delete = MagicMock()
            self.commit = AsyncMock()
            self.flush = AsyncMock()

        async def execute(self, stmt):
            stmt_str = str(stmt).lower()
            if "session_stream_events" in stmt_str:
                return _EmptyResult()
            return _SessionResult()

    mock_db = _MockDb()
    body = SessionChatIn(message="Resume")
    with (
        patch("app.routers.sessions._is_turn_active_elsewhere", new_callable=AsyncMock, return_value=True),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        for _ in range(2):
            with pytest.raises(HTTPException) as exc_info:
                await chat_session(
                    session_id=session_id,
                    body=body,
                    current_user=mock_user,
                    db=mock_db,  # type: ignore[arg-type]
                    last_event_id_header="1",
                )
            assert exc_info.value.status_code == 409

    mock_db.add.assert_not_called()
    mock_db.delete.assert_not_called()
    mock_db.commit.assert_not_awaited()
    mock_db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_replay_drains_streams_with_zero_writes() -> None:
    """Successful resume: second discovery read yields a turn_id and the resulting
    streams replay end-to-end while both the chat `db` and the inner stream session
    observe zero writes (no double-apply of turn effects)."""
    from app.routers.sessions import (
        _TURN_DISCOVERY_DEADLINE_S,
        _TURN_DISCOVERY_MAX_DELAY_S,
    )

    session_id = uuid.uuid4()
    user_id = uuid.uuid4()
    new_turn_id = str(uuid.uuid4())

    mock_user = MagicMock(spec=User)
    mock_user.id = user_id

    mock_session = MagicMock(spec=AgentSession)
    mock_session.id = session_id
    mock_session.user_id = user_id
    mock_session.status = "active"
    mock_session.active_turn_id = None

    _ACTIVE_TURNS.pop(str(session_id), None)

    # Discovery read counter — used to make the *second* `session_stream_events`
    # read return the discovered turn_id while the first returns None.
    turn_lookup_attempts = 0

    class _SessionResult:
        def scalars(self):
            class _S:
                def first(self):
                    return mock_session
            return _S()

    class _TurnResult:
        def __init__(self, val):
            self._val = val

        def scalars(self):
            class _S:
                def __init__(self, v):
                    self._v = v
                def first(self):
                    return self._v
            return _S(self._val)

    class _BoundsResult:
        def first(self):
            return (1, 2)

    # The chat-level DB — the one passed directly into chat_session. Must never
    # see a write (add/commit/flush/delete) from the resume path.
    class _ChatDb:
        def __init__(self) -> None:
            self.add = MagicMock()
            self.delete = MagicMock()
            self.commit = AsyncMock()
            self.flush = AsyncMock()
            self.execute = AsyncMock(side_effect=self._execute)

        async def _execute(self, stmt):
            stmt_str = str(stmt).lower()
            nonlocal turn_lookup_attempts
            # Bounds stmt includes both "min" and "session_stream_events" — must
            # be matched *first* so the latest_turn_stmt (turn_id lookup) does
            # not steal the bounds response.
            if "min(" in stmt_str:
                return _BoundsResult()
            if "session_stream_events" in stmt_str:
                turn_lookup_attempts += 1
                if turn_lookup_attempts == 1:
                    return _TurnResult(None)
                return _TurnResult(new_turn_id)
            return _SessionResult()

    # The inner stream-replay DB — opened inside `_stream_from_db`. Must also
    # stay write-free; we record every mutating call so the test can assert it.
    class _ReplayDb:
        def __init__(self) -> None:
            self.add = MagicMock()
            self.delete = MagicMock()
            self.commit = AsyncMock()
            self.flush = AsyncMock()
            self.rollback = AsyncMock()

        async def execute(self, stmt):
            class _MockRes:
                def scalars(self):
                    class _S:
                        def all(self_inner):
                            return [
                                SessionStreamEvent(
                                    id=10,
                                    session_id=session_id,
                                    turn_id=new_turn_id,
                                    event_id=2,
                                    event="done",
                                    chunk='event: done\nid: 2\ndata: {"ok":true}\n\n',
                                ),
                            ]
                    return _S()
            return _MockRes()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    chat_db = _ChatDb()
    replay_dbs: list[_ReplayDb] = []

    def _replay_factory() -> _ReplayDb:
        db = _ReplayDb()
        replay_dbs.append(db)
        return db

    body = SessionChatIn(message="Resume")

    # Drive two resumes back-to-back; each should succeed once the second
    # discovery read returns the turn_id, and each StreamingResponse should
    # drain end-to-end without any writes touching either DB session.
    with (
        patch("app.routers.sessions._is_turn_active_elsewhere", new_callable=AsyncMock, return_value=True),
        patch("asyncio.sleep", new_callable=AsyncMock),
        patch("app.db.async_session_maker", side_effect=_replay_factory),
    ):
        for _ in range(2):
            resp = await chat_session(
                session_id=session_id,
                body=body,
                current_user=mock_user,
                db=chat_db,  # type: ignore[arg-type]
                last_event_id_header="0",
            )
            assert resp.status_code == 200
            assert resp.media_type == "text/event-stream"
            chunks = [c async for c in resp.body_iterator]
            assert any("event: done" in c for c in chunks)

    # Sanity on the retry math: deadline and caps still match the constants.
    assert _TURN_DISCOVERY_DEADLINE_S >= _TURN_DISCOVERY_MAX_DELAY_S

    # No writes from chat_session on the chat db.
    chat_db.add.assert_not_called()
    chat_db.delete.assert_not_called()
    chat_db.commit.assert_not_awaited()
    chat_db.flush.assert_not_awaited()

    # No writes from `_stream_from_db` on the inner stream db — one per resume.
    assert len(replay_dbs) == 2
    for db in replay_dbs:
        db.add.assert_not_called()
        db.delete.assert_not_called()
        db.commit.assert_not_awaited()
        db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_discovery_aborts_when_db_read_exceeds_deadline() -> None:
    """When a single `db.execute` blocks longer than the discovery deadline, the
    loop must exit on the next iteration check and surface 409 instead of
    silently returning a stale value."""
    from app.routers.sessions import (
        _TURN_DISCOVERY_DEADLINE_S,
        _TURN_DISCOVERY_MAX_ATTEMPTS,
    )

    session_id = uuid.uuid4()
    user_id = uuid.uuid4()

    mock_user = MagicMock(spec=User)
    mock_user.id = user_id

    mock_session = MagicMock(spec=AgentSession)
    mock_session.id = session_id
    mock_session.user_id = user_id
    mock_session.status = "active"
    mock_session.active_turn_id = None

    _ACTIVE_TURNS.pop(str(session_id), None)

    class _SessionResult:
        def scalars(self):
            class _S:
                def first(self):
                    return mock_session
            return _S()

    class _EmptyResult:
        def scalars(self):
            class _S:
                def first(self):
                    return None
            return _S()

    # Track real wall-clock progression so we can advance our fake
    # `time.monotonic()` past the deadline *after* one slow `db.execute`.
    real_monotonic = time.monotonic
    start = real_monotonic()
    fake_now = {"t": start}

    def _fake_monotonic() -> float:
        return fake_now["t"]

    class _SlowDb:
        def __init__(self) -> None:
            self.add = MagicMock()
            self.delete = MagicMock()
            self.commit = AsyncMock()
            self.flush = AsyncMock()
            self.stream_reads = 0

        async def execute(self, stmt):
            stmt_str = str(stmt).lower()
            if "session_stream_events" in stmt_str:
                self.stream_reads += 1
                # The initial read (the first `session_stream_events` call)
                # happens BEFORE the deadline is recorded, so it must NOT
                # advance the fake clock — otherwise `deadline` would be set
                # *after* the jump and the loop would still have budget left.
                # Starting with the second read (the first inside the retry
                # loop), simulate a slow read by jumping the fake clock past
                # the deadline so the next iteration's `remaining` check breaks.
                if self.stream_reads >= 2:
                    fake_now["t"] = start + _TURN_DISCOVERY_DEADLINE_S + 0.5
                return _EmptyResult()
            return _SessionResult()

    mock_db = _SlowDb()
    body = SessionChatIn(message="Resume")

    with (
        patch("app.routers.sessions._is_turn_active_elsewhere", new_callable=AsyncMock, return_value=True),
        patch("asyncio.sleep", new_callable=AsyncMock),
        patch("app.routers.sessions.time.monotonic", side_effect=_fake_monotonic),
        pytest.raises(HTTPException) as exc_info,
    ):
        await chat_session(
            session_id=session_id,
            body=body,
            current_user=mock_user,
            db=mock_db,  # type: ignore[arg-type]
            last_event_id_header="1",
        )

    # The loop must surface a user-visible 409 once the deadline elapses.
    assert exc_info.value.status_code == 409
    assert "TURN_ACTIVE_ELSEWHERE" in exc_info.value.detail
    # The deadline check must prevent further reads after the slow one — at
    # most the initial read plus one (slow) discovery read.
    assert mock_db.stream_reads <= 2
    assert mock_db.stream_reads <= 1 + _TURN_DISCOVERY_MAX_ATTEMPTS
    # No writes from the resume path.
    mock_db.add.assert_not_called()
    mock_db.delete.assert_not_called()
    mock_db.commit.assert_not_awaited()
    mock_db.flush.assert_not_awaited()

