"""
Integration and unit tests for Interactive Planning & Clarification UI (Phase 6).

Tests:
1. test_update_plan_valid                     — Validates task checklist, updates session.plan and emits SSE.
2. test_update_plan_invalid_status            — Rejects invalid task status with descriptive error.
3. test_ask_user_clarification_pauses_session — Sets status='awaiting_clarification', records waiting_input.
4. test_clarify_endpoint_success              — POST /sessions/{id}/clarify resumes to 'active', clears waiting_input.
5. test_clarify_endpoint_not_awaiting         — Rejects clarification when session is not in 'awaiting_clarification' (400).
6. test_clarify_endpoint_idor_protection      — Other users receive 404 on unowned session clarification (IDOR).
7. test_sse_plan_update_and_clarification_events — Verifies SSE wire formatting and SseQueue helpers.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentSession, Repo, User
from app.services.session_streamer import SseQueue, format_sse_event
from app.services.session_tools.planning import (
    tool_ask_user_clarification,
    tool_update_plan,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth_client(make_auth_client, user: User) -> httpx.AsyncClient:
    return make_auth_client(user.id)


async def _seed_user_and_repo(db: AsyncSession, *, github_id: int) -> tuple[User, Repo]:
    user = User(
        github_id=github_id,
        github_username=f"user-{github_id}",
        role="user",
    )
    repo = Repo(
        user=user,
        owner="test-org",
        name=f"test-repo-{github_id}",
        default_branch="main",
        github_install_id=999,
    )
    db.add(user)
    db.add(repo)
    await db.commit()
    await db.refresh(user)
    await db.refresh(repo)

    return user, repo


async def _seed_session(
    db: AsyncSession,
    user: User,
    repo: Repo,
    *,
    status: str = "active",
    plan: list[dict[str, Any]] | None = None,
    waiting_input: dict[str, Any] | None = None,
) -> AgentSession:
    session = AgentSession(
        user=user,
        repo=repo,
        user_id=user.id,
        repo_id=repo.id,
        title="Planning Session",
        status=status,
        branch_name="main",
        base_sha="a" * 40,
        conversation_history=[],
        staged_patches={},
        plan=plan or [],
        waiting_input=waiting_input,
    )
    db.add(session)
    await db.commit()
    try:
        await db.refresh(session)
    except Exception:
        pass
    return session


# ---------------------------------------------------------------------------
# Test 1: test_update_plan_valid
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_plan_valid(db: AsyncSession) -> None:
    """Validates task list, updates session.plan, and emits plan_update SSE event."""
    user, repo = await _seed_user_and_repo(db, github_id=60001)
    session = await _seed_session(db, user, repo)

    queue = SseQueue()
    tasks = [
        {"id": "1", "title": "Locate auth handler", "status": "completed"},
        {"id": "2", "title": "Apply surgical patch with str_replace", "status": "in_progress"},
        {"id": "3", "title": "Run pytest verification", "status": "pending"},
    ]

    result = await tool_update_plan(tasks, session, queue, db)
    assert "Plan updated with 3 tasks." in result
    assert len(session.plan) == 3
    assert session.plan[0]["status"] == "completed"
    assert session.plan[1]["status"] == "in_progress"
    assert session.plan[2]["status"] == "pending"

    # Verify event on queue
    event_str = await queue._q.get()
    assert "event: plan_update\n" in event_str
    data = json.loads(event_str.split("data: ", 1)[1].strip())
    assert len(data["tasks"]) == 3
    assert data["tasks"][0]["title"] == "Locate auth handler"


# ---------------------------------------------------------------------------
# Test 2: test_update_plan_invalid_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_plan_invalid_status(db: AsyncSession) -> None:
    """Rejects invalid task status with a descriptive error and preserves prior plan."""
    user, repo = await _seed_user_and_repo(db, github_id=60002)
    session = await _seed_session(db, user, repo, plan=[{"id": "0", "title": "Initial", "status": "pending"}])

    queue = SseQueue()
    invalid_tasks = [
        {"id": "1", "title": "Step 1", "status": "unknown_status"},
    ]

    result = await tool_update_plan(invalid_tasks, session, queue, db)
    assert result.startswith("Error:")
    assert "invalid status 'unknown_status'" in result
    # Original plan unmodified
    assert len(session.plan) == 1
    assert session.plan[0]["id"] == "0"


# ---------------------------------------------------------------------------
# Test 3: test_ask_user_clarification_pauses_session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_user_clarification_pauses_session(db: AsyncSession) -> None:
    """Sets status='awaiting_clarification', populates waiting_input, and emits SSE event."""
    user, repo = await _seed_user_and_repo(db, github_id=60003)
    session = await _seed_session(db, user, repo)

    queue = SseQueue()
    question = "Should we use asyncpg or psycopg for the new connection pool?"
    options = ["asyncpg (recommended for async performance)", "psycopg 3"]

    result = await tool_ask_user_clarification(question, options, session, queue, db)
    assert "Clarification requested from user. Execution paused." in result
    assert session.status == "awaiting_clarification"
    assert session.waiting_input is not None
    assert session.waiting_input["question"] == question
    assert session.waiting_input["options"] == options
    assert "timestamp" in session.waiting_input

    # Verify event on queue
    event_str = await queue._q.get()
    assert "event: clarification_requested\n" in event_str
    data = json.loads(event_str.split("data: ", 1)[1].strip())
    assert data["question"] == question
    assert data["options"] == options


# ---------------------------------------------------------------------------
# Test 4: test_clarify_endpoint_success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clarify_endpoint_success(
    db: AsyncSession,
    make_auth_client,
) -> None:
    """POST /sessions/{id}/clarify resumes status to 'active', clears waiting_input, and appends to history."""
    user, repo = await _seed_user_and_repo(db, github_id=60004)
    session = await _seed_session(
        db,
        user,
        repo,
        status="awaiting_clarification",
        waiting_input={"question": "Pick an architecture", "options": ["Option A", "Option B"]},
    )

    async with _auth_client(make_auth_client, user) as ac:
        resp = await ac.post(
            f"/sessions/{session.id}/clarify",
            json={"response": "Option A"},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "active"
    assert data["waiting_input"] is None

    # Verify DB persistence
    await db.refresh(session)
    assert session.status == "active"
    assert session.waiting_input is None
    assert len(session.conversation_history) == 1
    last_msg = session.conversation_history[-1]
    assert last_msg["role"] == "user"
    assert last_msg["content"] == "[User Clarification Response]: Option A"


# ---------------------------------------------------------------------------
# Test 5: test_clarify_endpoint_not_awaiting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clarify_endpoint_not_awaiting(
    db: AsyncSession,
    make_auth_client,
) -> None:
    """Returns 400 Bad Request when session is not in status 'awaiting_clarification'."""
    user, repo = await _seed_user_and_repo(db, github_id=60005)
    session = await _seed_session(db, user, repo, status="active")

    async with _auth_client(make_auth_client, user) as ac:
        resp = await ac.post(
            f"/sessions/{session.id}/clarify",
            json={"response": "Some choice"},
        )

    assert resp.status_code == 400, resp.text
    assert "not awaiting clarification" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Test 6: test_clarify_endpoint_idor_protection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clarify_endpoint_idor_protection(
    db: AsyncSession,
    make_auth_client,
) -> None:
    """User B receives 404 when attempting to clarify User A's session."""
    user_a, repo_a = await _seed_user_and_repo(db, github_id=60006)
    session_a = await _seed_session(
        db,
        user_a,
        repo_a,
        status="awaiting_clarification",
        waiting_input={"question": "A's question", "options": ["1", "2"]},
    )

    user_b = User(github_id=60007, github_username="user-b", role="user")
    db.add(user_b)
    await db.commit()
    await db.refresh(user_b)

    async with _auth_client(make_auth_client, user_b) as ac:
        resp = await ac.post(
            f"/sessions/{session_a.id}/clarify",
            json={"response": "1"},
        )

    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Test 7: test_sse_plan_update_and_clarification_events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sse_plan_update_and_clarification_events() -> None:
    """Verifies SSE wire format and SseQueue helper methods for planning events."""
    # format_sse_event for plan_update
    tasks = [{"id": "1", "title": "Check types", "status": "pending"}]
    plan_chunk = format_sse_event("plan_update", {"tasks": tasks})
    assert plan_chunk.startswith("event: plan_update\n")
    assert plan_chunk.endswith("\n\n")
    data = json.loads(plan_chunk.split("data: ", 1)[1].strip())
    assert data["tasks"] == tasks

    # format_sse_event for clarification_requested
    clarify_chunk = format_sse_event("clarification_requested", {
        "question": "Use Redis?",
        "options": ["Yes", "No"],
    })
    assert clarify_chunk.startswith("event: clarification_requested\n")
    assert clarify_chunk.endswith("\n\n")
    data2 = json.loads(clarify_chunk.split("data: ", 1)[1].strip())
    assert data2["question"] == "Use Redis?"
    assert data2["options"] == ["Yes", "No"]

    # SseQueue typed helpers
    queue = SseQueue()
    await queue.put_plan_update(tasks)
    await queue.put_clarification_requested("Use Redis?", ["Yes", "No"])

    item1 = await queue._q.get()
    assert "event: plan_update" in item1
    item2 = await queue._q.get()
    assert "event: clarification_requested" in item2
