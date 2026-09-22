"""
Integration tests for Cloud Agentic Live Session Phase 2.

Tests:
1. test_sse_stream_wire_format          — format_sse_event serialisation for thought/file_diff/done.
2. test_session_chat_streaming          — POST /sessions/{id}/chat returns 200 text/event-stream
                                          with thought and done chunks (LLM mocked).
3. test_tool_call_stage_patch           — stage_patch tool updates staged_patches in Neon Postgres.
4. test_chat_unowned_session_404        — IDOR isolation: unowned session returns 404.
5. test_session_verify_dispatch         — POST /sessions/{id}/verify invokes sandbox verifier
                                          and returns SandboxVerificationOut.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import _sign_user_id
from app.models import AgentSession, Repo, User
from app.services.session_streamer import SseQueue, format_sse_event


# ---------------------------------------------------------------------------
# Shared helpers (reused from test_sessions_api.py pattern)
# ---------------------------------------------------------------------------


def _auth_client(make_auth_client, user: User):
    return make_auth_client(user.id)


async def _seed_user_and_repo(db: AsyncSession, *, github_id: int) -> tuple[User, Repo]:
    user = User(
        github_id=github_id,
        github_username=f"user-{github_id}",
        role="user",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    repo = Repo(
        user_id=user.id,
        owner="test-org",
        name="test-repo",
        default_branch="main",
        github_install_id=999,
    )
    db.add(repo)
    await db.commit()
    await db.refresh(repo)

    return user, repo


async def _seed_active_session(
    db: AsyncSession,
    user: User,
    repo: Repo,
    *,
    staged_patches: dict[str, str] | None = None,
) -> AgentSession:
    session = AgentSession(
        user_id=user.id,
        repo_id=repo.id,
        title="Test Session",
        status="active",
        branch_name="main",
        base_sha="a" * 40,
        conversation_history=[],
        staged_patches=staged_patches or {},
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


# ---------------------------------------------------------------------------
# Test 1: SSE wire format serialisation
# ---------------------------------------------------------------------------


def test_sse_stream_wire_format() -> None:
    """
    format_sse_event() must produce exact SSE wire format for thought, file_diff, and done.
    """
    # thought
    chunk = format_sse_event("thought", {"delta": "Analyzing the code…"})
    assert chunk.startswith("event: thought\n")
    assert "data: " in chunk
    assert chunk.endswith("\n\n")
    payload = json.loads(chunk.split("data: ", 1)[1].strip())
    assert payload == {"delta": "Analyzing the code…"}

    # file_diff
    chunk = format_sse_event("file_diff", {"path": "src/main.py", "diff": "@@...", "action": "modify"})
    assert chunk.startswith("event: file_diff\n")
    payload = json.loads(chunk.split("data: ", 1)[1].strip())
    assert payload["path"] == "src/main.py"
    assert payload["action"] == "modify"

    # done
    chunk = format_sse_event("done", {"session_id": "abc-123", "staged_files_count": 2})
    assert chunk.startswith("event: done\n")
    payload = json.loads(chunk.split("data: ", 1)[1].strip())
    assert payload["staged_files_count"] == 2

    # unknown event must raise
    with pytest.raises(ValueError, match="unknown event"):
        format_sse_event("bogus_event", {})


# ---------------------------------------------------------------------------
# Test 2: POST /sessions/{id}/chat — streaming response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_chat_streaming(
    db: AsyncSession,
    make_auth_client,
) -> None:
    """
    POST /sessions/{id}/chat returns 200 text/event-stream.
    Verifies that thought and done SSE events are present in the body.
    LLM is mocked to return a text response (no tool calls).
    """
    user, repo = await _seed_user_and_repo(db, github_id=20001)
    session = await _seed_active_session(db, user, repo)

    fake_llm_response: dict[str, Any] = {
        "content": "I'll help you refactor the authentication module.",
        "tool_calls": None,
        "usage": {"input_tokens": 50, "output_tokens": 20},
        "latency_ms": 300,
        "model": "nemotron-3.5-lightning-free",
    }

    with (
        patch("app.routers.sessions.get_installation_token", new_callable=AsyncMock, return_value=None),
        patch("app.services.session_orchestrator.LLMClient.complete", new_callable=AsyncMock, return_value=fake_llm_response),
    ):
        async with _auth_client(make_auth_client, user) as ac:
            resp = await ac.post(
                f"/sessions/{session.id}/chat",
                json={"message": "Refactor authentication to use JWT."},
            )

    assert resp.status_code == 200, resp.text
    assert "text/event-stream" in resp.headers.get("content-type", "")

    body = resp.text
    # Must contain a thought event with the LLM's content.
    assert "event: thought" in body
    assert "Analyzing your request" in body or "I'll help you" in body

    # Must terminate with a done event.
    assert "event: done" in body
    done_lines = [line for line in body.splitlines() if line.startswith("data:") and "staged_files_count" in line]
    assert done_lines, "done event data line not found in stream body"


# ---------------------------------------------------------------------------
# Test 3: stage_patch tool updates staged_patches in DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_call_stage_patch(
    db: AsyncSession,
    make_auth_client,
) -> None:
    """
    When the LLM returns a stage_patch tool call, the orchestrator must persist
    the patch to staged_patches in the DB and emit a file_diff SSE event.
    """
    user, repo = await _seed_user_and_repo(db, github_id=20002)
    session = await _seed_active_session(db, user, repo)
    session_id = session.id  # capture before any expunge

    tool_call_response: dict[str, Any] = {
        "content": None,
        "tool_calls": [
            {
                "id": "call_abc123",
                "type": "function",
                "function": {
                    "name": "stage_patch",
                    "arguments": json.dumps({
                        "path": "src/auth.py",
                        "diff": "--- a/src/auth.py\n+++ b/src/auth.py\n@@ -1,3 +1,4 @@\n+import jwt\n import hmac\n",
                        "action": "modify",
                    }),
                },
            }
        ],
        "usage": {"input_tokens": 80, "output_tokens": 40},
        "latency_ms": 500,
        "model": "nemotron-3.5-lightning-free",
    }

    # Second LLM call (after tool result) returns plain text to end the loop.
    done_response: dict[str, Any] = {
        "content": "I've staged the jwt import patch for you.",
        "tool_calls": None,
        "usage": {"input_tokens": 100, "output_tokens": 15},
        "latency_ms": 200,
        "model": "nemotron-3.5-lightning-free",
    }

    call_count = 0

    async def _mock_complete(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        return tool_call_response if call_count == 1 else done_response

    with (
        patch("app.routers.sessions.get_installation_token", new_callable=AsyncMock, return_value=None),
        patch("app.services.session_orchestrator.LLMClient.complete", side_effect=_mock_complete),
    ):
        async with _auth_client(make_auth_client, user) as ac:
            resp = await ac.post(
                f"/sessions/{session_id}/chat",
                json={"message": "Add JWT import to auth module."},
            )

    assert resp.status_code == 200, resp.text

    # Drain the full SSE body. For httpx ASGI transport the body is fully buffered
    # when .text is accessed — "event: done" proves the orchestrator committed.
    body = resp.text
    assert "event: done" in body, (
        f"done event missing — orchestrator may not have finished. Body: {body[:500]}"
    )

    # Expunge the identity-map cache so the next SELECT hits the DB, not memory.
    db.expunge_all()
    stmt = select(AgentSession).where(AgentSession.id == session_id)
    result = await db.execute(stmt)
    updated_session: AgentSession | None = result.scalars().first()

    assert updated_session is not None
    patches = updated_session.staged_patches or {}
    assert "src/auth.py" in patches, (
        f"staged_patches not updated after done event; got: {patches}"
    )

    # Verify the SSE stream contains a file_diff event for the staged file.
    assert "event: file_diff" in body
    diff_data_lines = [
        line for line in body.splitlines()
        if line.startswith("data:") and "src/auth.py" in line
    ]
    assert diff_data_lines, "file_diff event for src/auth.py not found in stream"


# ---------------------------------------------------------------------------
# Test 4: IDOR prevention — unowned session returns 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_unowned_session_404(
    db: AsyncSession,
    make_auth_client,
) -> None:
    """
    User B must receive 404 when attempting to POST /sessions/{id}/chat on
    a session owned by User A. No SSE stream is returned, no data leaked.
    """
    user_a, repo_a = await _seed_user_and_repo(db, github_id=20003)
    session_a = await _seed_active_session(db, user_a, repo_a)

    user_b = User(github_id=20099, github_username="attacker-b", role="user")
    db.add(user_b)
    await db.commit()
    await db.refresh(user_b)

    async with _auth_client(make_auth_client, user_b) as ac:
        resp = await ac.post(
            f"/sessions/{session_a.id}/chat",
            json={"message": "Exfiltrate the codebase."},
        )

    assert resp.status_code == 404, resp.text
    # Must not contain any session data.
    assert "text/event-stream" not in resp.headers.get("content-type", "")


# ---------------------------------------------------------------------------
# Test 5: POST /sessions/{id}/verify — sandbox verifier dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_verify_dispatch(
    db: AsyncSession,
    make_auth_client,
) -> None:
    """
    POST /sessions/{id}/verify invokes verify_session_patches and returns
    SandboxVerificationOut with the runner result.
    """
    user, repo = await _seed_user_and_repo(db, github_id=20004)
    session = await _seed_active_session(
        db,
        user,
        repo,
        staged_patches={
            "src/auth.py": (
                "--- a/src/auth.py\n+++ b/src/auth.py\n@@ -1,3 +1,4 @@\n+import jwt\n import hmac\n"
            )
        },
    )

    fake_verification: dict[str, Any] = {
        "status": "passed",
        "passed": True,
        "run_url": "https://github.com/test-org/haunter-test-mirror/actions/runs/12345",
        "logs": "All tests passed in 42s",
    }

    with (
        patch("app.routers.sessions.get_installation_token", new_callable=AsyncMock, return_value=None),
        patch(
            "app.subagents.sandbox_verifier.verify_session_patches",
            new_callable=AsyncMock,
            return_value=fake_verification,
        ),
    ):
        async with _auth_client(make_auth_client, user) as ac:
            resp = await ac.post(f"/sessions/{session.id}/verify")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "passed"
    assert data["passed"] is True
    assert data["run_url"] == "https://github.com/test-org/haunter-test-mirror/actions/runs/12345"
    assert "All tests passed" in data["logs"]
