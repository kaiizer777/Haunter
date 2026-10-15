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

