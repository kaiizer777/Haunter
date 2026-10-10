"""
Unit and integration tests for Session Time Machine & Pre-Commit Security (Phase 7).

Tests:
1.  test_create_checkpoint                   — Creates snapshot with correct turn, description, staged_patches copy.
2.  test_checkpoint_cap_at_20               — Capping at 20 entries drops oldest.
3.  test_checkpoint_restore_success         — Restores prior patches and truncates conversation history.
4.  test_checkpoint_restore_not_found       — Returns descriptive error on invalid checkpoint_id.
5.  test_security_scan_detects_aws_key      — Flags AKIAIOSFODNN7EXAMPLE with line number.
6.  test_security_scan_detects_github_pat   — Flags ghp_0123456789abcdef0123456789abcdef0123.
7.  test_security_scan_detects_sql_injection — Flags execute(f"SELECT * FROM users WHERE id = {user_id}").
8.  test_security_scan_clean_code_passes    — Verifies clean code produces zero warnings.
9.  test_restore_endpoint_success           — POST /sessions/{id}/checkpoints/{cp_id}/restore validates DB state.
10. test_restore_endpoint_idor              — User B cannot restore User A's checkpoints.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentSession, Repo, User
from app.services.session_orchestrator import SessionOrchestrator, _build_system_prompt
from app.services.session_streamer import SseQueue
from app.services.session_tools.checkpoints import (
    create_checkpoint,
    tool_checkpoint_list,
    tool_checkpoint_restore,
    tool_scan_security_vulnerabilities,
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
    db.add(user)
    await db.commit()
    await db.refresh(user)

    repo = Repo(
        user_id=user.id,
        owner="test-org",
        name=f"test-repo-{github_id}",
        default_branch="main",
        github_install_id=999,
    )
    db.add(repo)
    await db.commit()
    await db.refresh(repo)
    return user, repo


def _make_session(
    user: User,
    repo: Repo,
    *,
    staged_patches: dict[str, str] | None = None,
    conversation_history: list[dict[str, Any]] | None = None,
    checkpoints: list[dict[str, Any]] | None = None,
) -> AgentSession:
    s = AgentSession(
        user_id=user.id,
        repo_id=repo.id,
        title="Test Session",
        status="active",
        branch_name="main",
        base_sha="a" * 40,
        staged_patches=staged_patches or {},
        conversation_history=conversation_history or [],
        checkpoints=checkpoints or [],
        plan=[],
        waiting_input=None,
    )
    return s


async def _seed_session(
    db: AsyncSession,
    user: User,
    repo: Repo,
    **kwargs: Any,
) -> AgentSession:
    s = _make_session(user, repo, **kwargs)
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return s


# ---------------------------------------------------------------------------
# Test 1: create_checkpoint — basic
# ---------------------------------------------------------------------------


def test_create_checkpoint() -> None:
    """create_checkpoint builds correct snapshot and appends to session.checkpoints."""
    user = MagicMock(spec=User)
    repo = MagicMock(spec=Repo)

    session = MagicMock(spec=AgentSession)
    session.staged_patches = {
        "src/auth.py": "--- a/src/auth.py\n+++ b/src/auth.py\n@@ -1 +1 @@\n-old\n+new\n"
    }
    session.conversation_history = [{"role": "user", "content": "fix auth"}] * 5
    session.checkpoints = []

    cp = create_checkpoint(session, description="Staged auth fix", turn=3)

    assert cp["turn"] == 3
    assert cp["description"] == "Staged auth fix"
    assert "checkpoint_id" in cp
    assert cp["checkpoint_id"].startswith("cp_")
    assert cp["history_length"] == 5
    assert cp["staged_patches"] == dict(session.staged_patches)
    # Checkpoint appended.
    assert len(session.checkpoints) == 1
    assert session.checkpoints[0]["checkpoint_id"] == cp["checkpoint_id"]


# ---------------------------------------------------------------------------
# Test 2: checkpoint cap at 20
# ---------------------------------------------------------------------------


def test_checkpoint_cap_at_20() -> None:
    """create_checkpoint caps at 20 entries, dropping oldest."""
    session = MagicMock(spec=AgentSession)
    session.staged_patches = {}
    session.conversation_history = []
    session.checkpoints = []

    for i in range(25):
        session.staged_patches = {f"file{i}.py": f"+line {i}"}
        create_checkpoint(session, description=f"Turn {i}", turn=i)

    assert len(session.checkpoints) == 20
    # Oldest (turn 0-4) should be dropped.
    turns = [c["turn"] for c in session.checkpoints]
    assert 0 not in turns
    assert 24 in turns


# ---------------------------------------------------------------------------
# Test 3: checkpoint_restore_success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_restore_success() -> None:
    """tool_checkpoint_restore reverts staged_patches and truncates conversation_history."""
    session = MagicMock(spec=AgentSession)
    session.staged_patches = {"new_file.py": "+new line"}
    session.conversation_history = [
        {"role": "user", "content": "msg1"},
        {"role": "assistant", "content": "resp1"},
        {"role": "user", "content": "msg2"},
    ]
    cp_id = "cp_abcd1234"
    session.checkpoints = [
        {
            "checkpoint_id": cp_id,
            "turn": 2,
            "timestamp": "2026-09-23T10:00:00+00:00",
            "description": "After msg1",
            "staged_patches": {"src/main.py": "+original patch"},
            "history_length": 2,
        }
    ]

    queue = MagicMock(spec=SseQueue)
    queue.put_checkpoint_restored = AsyncMock()

    db = AsyncMock(spec=AsyncSession)
    db.flush = AsyncMock()
    db.commit = AsyncMock()

    result = await tool_checkpoint_restore(
        checkpoint_id=cp_id,
        session=session,
        queue=queue,
        db=db,
    )

    # Patches reverted.
    assert session.staged_patches == {"src/main.py": "+original patch"}
    # History truncated to length 2.
    assert len(session.conversation_history) == 2
    # SSE emitted.
    queue.put_checkpoint_restored.assert_awaited_once_with(
        checkpoint_id=cp_id,
        staged_patches={"src/main.py": "+original patch"},
    )
    # DB flushed in active transaction (not committed mid-turn).
    db.flush.assert_awaited_once()
    db.commit.assert_not_called()
    assert "Successfully restored" in result
    assert cp_id in result


# ---------------------------------------------------------------------------
# Test 4: checkpoint_restore_not_found
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_restore_not_found() -> None:
    """tool_checkpoint_restore returns descriptive error for unknown checkpoint_id."""
    session = MagicMock(spec=AgentSession)
    session.staged_patches = {}
    session.conversation_history = []
    session.checkpoints = []

    queue = MagicMock(spec=SseQueue)
    db = AsyncMock(spec=AsyncSession)

    result = await tool_checkpoint_restore(
        checkpoint_id="cp_nonexistent",
        session=session,
        queue=queue,
        db=db,
    )

    assert result.startswith("Error:")
    assert "cp_nonexistent" in result
    # No commit on failure.
    db.commit.assert_not_called()


# ---------------------------------------------------------------------------
# Test 5: security scan detects AWS key
# ---------------------------------------------------------------------------


def test_security_scan_detects_aws_key() -> None:
    """tool_scan_security_vulnerabilities flags fake AWS access key."""
    session = MagicMock(spec=AgentSession)
    fake_key = "AKIAIOSFODNN7EXAMPLE"
    session.staged_patches = {"config.py": f"+AWS_ACCESS_KEY = '{fake_key}'\n"}

    result = tool_scan_security_vulnerabilities(
        paths=["config.py"],
        session=session,
        repo_owner="org",
        repo_name="repo",
        base_sha="a" * 40,
    )

    assert "AWS_ACCESS_KEY" in result
    assert "CRITICAL" in result
    assert fake_key[:4] in result  # redacted but first 4 chars visible
    assert ":1" in result  # line number


# ---------------------------------------------------------------------------
# Test 6: security scan detects GitHub PAT
# ---------------------------------------------------------------------------


def test_security_scan_detects_github_pat() -> None:
    """tool_scan_security_vulnerabilities flags fake GitHub PAT."""
    session = MagicMock(spec=AgentSession)
    fake_pat = "ghp_0123456789abcdef0123456789abcdef0123"
    session.staged_patches = {"deploy.py": f"+TOKEN = '{fake_pat}'\n"}

    result = tool_scan_security_vulnerabilities(
        paths=["deploy.py"],
        session=session,
        repo_owner="org",
        repo_name="repo",
        base_sha="a" * 40,
    )

    assert "GITHUB_PAT" in result
    assert "CRITICAL" in result
    assert "ghp_" in result


# ---------------------------------------------------------------------------
# Test 7: security scan detects SQL injection
# ---------------------------------------------------------------------------


def test_security_scan_detects_sql_injection() -> None:
    """tool_scan_security_vulnerabilities flags SQL f-string injection pattern."""
    session = MagicMock(spec=AgentSession)
    session.staged_patches = {
        "db.py": '+    cursor.execute(f"SELECT * FROM users WHERE id = {user_id}")\n'
    }

    result = tool_scan_security_vulnerabilities(
        paths=["db.py"],
        session=session,
        repo_owner="org",
        repo_name="repo",
        base_sha="a" * 40,
    )

    assert "SQL_INJECTION" in result
    assert "HIGH" in result


# ---------------------------------------------------------------------------
# Test 8: security scan clean code passes
# ---------------------------------------------------------------------------


def test_security_scan_clean_code_passes() -> None:
    """tool_scan_security_vulnerabilities returns pass message for clean code."""
    session = MagicMock(spec=AgentSession)
    session.staged_patches = {
        "clean.py": "+def add(a: int, b: int) -> int:\n+    return a + b\n"
    }

    result = tool_scan_security_vulnerabilities(
        paths=["clean.py"],
        session=session,
        repo_owner="org",
        repo_name="repo",
        base_sha="a" * 40,
    )

    assert "passed" in result.lower()
    assert "0 secrets" in result


# ---------------------------------------------------------------------------
# Test 9: restore_endpoint_success (integration — requires TEST_DATABASE_URL)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_endpoint_success(
    db: AsyncSession,
    make_auth_client,
) -> None:
    """POST /sessions/{id}/checkpoints/{cp_id}/restore reverts DB state."""
    user, repo = await _seed_user_and_repo(db, github_id=90001)

    cp_id = "cp_test1234"
    original_patches = {
        "src/main.py": "--- a/src/main.py\n+++ b/src/main.py\n@@ -1 +1 @@\n-old\n+original\n"
    }
    later_patches = {
        "src/main.py": "--- a/src/main.py\n+++ b/src/main.py\n@@ -1 +1 @@\n-old\n+later\n",
        "new_file.py": "+new file",
    }

    session = await _seed_session(
        db,
        user,
        repo,
        staged_patches=later_patches,
        conversation_history=[
            {"role": "user", "content": "msg1"},
            {"role": "assistant", "content": "resp1"},
            {"role": "user", "content": "msg2"},
        ],
        checkpoints=[
            {
                "checkpoint_id": cp_id,
                "turn": 2,
                "timestamp": "2026-09-23T10:00:00+00:00",
                "description": "After first turn",
                "staged_patches": original_patches,
                "history_length": 2,
            }
        ],
    )

    async with _auth_client(make_auth_client, user) as ac:
        resp = await ac.post(f"/sessions/{session.id}/checkpoints/{cp_id}/restore")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["staged_patches"] == original_patches
    assert len(data["conversation_history"]) == 2

    # Verify persisted to DB.
    await db.refresh(session)
    assert session.staged_patches == original_patches
    assert len(session.conversation_history) == 2


# ---------------------------------------------------------------------------
# Test 10: restore_endpoint_idor — User B cannot restore User A's checkpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_endpoint_idor(
    db: AsyncSession,
    make_auth_client,
) -> None:
    """User B receives 404 when attempting to restore User A's session checkpoint."""
    user_a, repo_a = await _seed_user_and_repo(db, github_id=90002)
    user_b, _ = await _seed_user_and_repo(db, github_id=90003)

    cp_id = "cp_idortest"
    session = await _seed_session(
        db,
        user_a,
        repo_a,
        checkpoints=[
            {
                "checkpoint_id": cp_id,
                "turn": 1,
                "timestamp": "2026-09-23T10:00:00+00:00",
                "description": "Turn 1",
                "staged_patches": {},
                "history_length": 1,
            }
        ],
    )

    # User B attempts to restore User A's checkpoint.
    async with _auth_client(make_auth_client, user_b) as ac:
        resp = await ac.post(f"/sessions/{session.id}/checkpoints/{cp_id}/restore")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Test 11: checkpoint_list — empty and populated
# ---------------------------------------------------------------------------


def test_checkpoint_list_empty() -> None:
    """tool_checkpoint_list returns clear notification when session has no checkpoints."""
    session = MagicMock(spec=AgentSession)
    session.checkpoints = []

    res = tool_checkpoint_list(session)
    assert "No checkpoints available" in res


def test_checkpoint_list_with_checkpoints() -> None:
    """tool_checkpoint_list surfaces all checkpoint IDs, turns, and staged files to agent."""
    session = MagicMock(spec=AgentSession)
    session.checkpoints = [
        {
            "checkpoint_id": "cp_1111aaaa",
            "turn": 1,
            "timestamp": "2026-10-01T12:00:00Z",
            "description": "Initial setup",
            "staged_patches": {"app/main.py": "+line"},
            "history_length": 2,
        },
        {
            "checkpoint_id": "cp_2222bbbb",
            "turn": 2,
            "timestamp": "2026-10-01T12:05:00Z",
            "description": "Added feature",
            "staged_patches": {"app/main.py": "+line", "app/util.py": "+util"},
            "history_length": 4,
        },
    ]

    res = tool_checkpoint_list(session)
    assert "Available checkpoints (2):" in res
    assert "cp_1111aaaa" in res
    assert "Turn 1" in res
    assert "Initial setup" in res
    assert "app/main.py" in res
    assert "cp_2222bbbb" in res
    assert "Turn 2" in res
    assert "Added feature" in res
    assert "app/util.py" in res


# ---------------------------------------------------------------------------
# Test 12: checkpoint_restore with 'latest'
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_restore_latest() -> None:
    """tool_checkpoint_restore('latest') rewinds to the most recent checkpoint."""
    session = MagicMock(spec=AgentSession)
    session.staged_patches = {"current.py": "+current"}
    session.conversation_history = [{"role": "user", "content": "1"}] * 6
    session.checkpoints = [
        {
            "checkpoint_id": "cp_first",
            "turn": 1,
            "timestamp": "2026-10-01T10:00:00Z",
            "description": "Turn 1",
            "staged_patches": {"f1.py": "+1"},
            "history_length": 2,
        },
        {
            "checkpoint_id": "cp_second",
            "turn": 2,
            "timestamp": "2026-10-01T10:05:00Z",
            "description": "Turn 2",
            "staged_patches": {"f2.py": "+2"},
            "history_length": 4,
        },
    ]

    queue = MagicMock(spec=SseQueue)
    queue.put_checkpoint_restored = AsyncMock()
    db = AsyncMock(spec=AsyncSession)

    res = await tool_checkpoint_restore(
        checkpoint_id="latest",
        session=session,
        queue=queue,
        db=db,
    )

    assert "Successfully restored session to checkpoint 'cp_second'" in res
    assert session.staged_patches == {"f2.py": "+2"}
    assert len(session.conversation_history) == 4
    queue.put_checkpoint_restored.assert_awaited_once_with(
        checkpoint_id="cp_second",
        staged_patches={"f2.py": "+2"},
    )


# ---------------------------------------------------------------------------
# Test 13: checkpoint_restore mutates caller's turn-local dict & list in-place
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_restore_mutates_turn_local_structures() -> None:
    """tool_checkpoint_restore directly updates turn-local staged_patches and conversation_history."""
    session = MagicMock(spec=AgentSession)
    cp_id = "cp_local_sync"
    session.checkpoints = [
        {
            "checkpoint_id": cp_id,
            "turn": 1,
            "timestamp": "2026-10-01T10:00:00Z",
            "description": "Baseline",
            "staged_patches": {"base.py": "+base patch"},
            "history_length": 2,
        }
    ]
    session.staged_patches = {"corrupted.py": "+bad"}
    session.conversation_history = [{"role": "user", "content": "msg"}] * 5

    turn_staged_patches = {"corrupted.py": "+bad"}
    turn_conversation_history = [{"role": "user", "content": "msg"}] * 5

    queue = MagicMock(spec=SseQueue)
    queue.put_checkpoint_restored = AsyncMock()
    db = AsyncMock(spec=AsyncSession)

    res = await tool_checkpoint_restore(
        checkpoint_id=cp_id,
        session=session,
        queue=queue,
        db=db,
        staged_patches=turn_staged_patches,
        conversation_history=turn_conversation_history,
    )

    assert "Successfully restored" in res
    # Turn-local dict must be mutated in-place to prevent post-turn clobber.
    assert turn_staged_patches == {"base.py": "+base patch"}
    assert len(turn_conversation_history) == 2
    assert session.staged_patches == {"base.py": "+base patch"}
    assert len(session.conversation_history) == 2


# ---------------------------------------------------------------------------
# Test 14: _build_system_prompt surfaces checkpoints to model context
# ---------------------------------------------------------------------------


def test_build_system_prompt_surfaces_available_checkpoints() -> None:
    """_build_system_prompt lists available checkpoints for model discovery."""
    checkpoints = [
        {
            "checkpoint_id": "cp_disc123",
            "turn": 2,
            "description": "Pre-refactor state",
            "staged_patches": {"app/core.py": "+diff"},
        }
    ]
    prompt = _build_system_prompt(
        repo_owner="test-owner",
        repo_name="test-repo",
        branch_name="feat/time-machine",
        base_sha="abcdef1234567890",
        staged_patches={},
        checkpoints=checkpoints,
    )

    assert "Available checkpoints:" in prompt
    assert "cp_disc123" in prompt
    assert "Turn 2" in prompt
    assert "Pre-refactor state" in prompt
    assert "1 file(s) staged" in prompt
    assert "checkpoint_list()" in prompt
    assert "checkpoint_restore(checkpoint_id)" in prompt


# ---------------------------------------------------------------------------
# Test 15: Regression for Issue #64 — End-of-turn persistence preserves restored state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_restore_invalidates_subsequent_checkpoints() -> None:
    """Restoring an earlier checkpoint invalidates/prunes all checkpoints created after it."""
    session = MagicMock(spec=AgentSession)
    session.staged_patches = {"current.py": "+current"}
    session.conversation_history = [{"role": "user", "content": "1"}] * 6
    session.checkpoints = [
        {
            "checkpoint_id": "cp_1",
            "turn": 1,
            "timestamp": "2026-10-01T10:00:00Z",
            "description": "Turn 1",
            "staged_patches": {"f1.py": "+1"},
            "history_length": 2,
        },
        {
            "checkpoint_id": "cp_2",
            "turn": 2,
            "timestamp": "2026-10-01T10:05:00Z",
            "description": "Turn 2",
            "staged_patches": {"f2.py": "+2"},
            "history_length": 4,
        },
        {
            "checkpoint_id": "cp_3",
            "turn": 3,
            "timestamp": "2026-10-01T10:10:00Z",
            "description": "Turn 3",
            "staged_patches": {"f3.py": "+3"},
            "history_length": 6,
        },
    ]

    queue = MagicMock(spec=SseQueue)
    queue.put_checkpoint_restored = AsyncMock()
    db = AsyncMock(spec=AsyncSession)

    res = await tool_checkpoint_restore(
        checkpoint_id="cp_1",
        session=session,
        queue=queue,
        db=db,
    )

    assert "Successfully restored session to checkpoint 'cp_1'" in res
    assert len(session.checkpoints) == 1
    assert session.checkpoints[0]["checkpoint_id"] == "cp_1"


# ---------------------------------------------------------------------------
# Test 16: Regression for Issue #64 — End-of-turn persistence via SessionOrchestrator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_restore_end_of_turn_persistence_regression(
    db: AsyncSession,
) -> None:
    """
    Regression test for Issue #64:
    Verifies that when a turn modifies staged_patches then restores to a prior checkpoint,
    the post-turn persistence preserves the restored snapshot in the DB without clobbering.
    Drives the full orchestration loop through SessionOrchestrator.run_turn with stubbed LLM.
    """
    user, repo = await _seed_user_and_repo(db, github_id=90005)

    cp_orig_id = "cp_orig_snapshot"
    original_staged = {
        "src/stable.py": "--- a/src/stable.py\n+++ b/src/stable.py\n@@ -1 +1 @@\n+stable\n"
    }

    session = await _seed_session(
        db,
        user,
        repo,
        staged_patches=original_staged,
        conversation_history=[
            {"role": "user", "content": "turn 1 request"},
            {"role": "assistant", "content": "turn 1 response"},
        ],
        checkpoints=[
            {
                "checkpoint_id": cp_orig_id,
                "turn": 1,
                "timestamp": "2026-10-01T10:00:00Z",
                "description": "Turn 1 stable",
                "staged_patches": original_staged,
                "history_length": 2,
            }
        ],
    )
    session_id = session.id

    call_count = 0

    tool_call_stage_broken = {
        "content": None,
        "tool_calls": [
            {
                "id": "call_stage_bad",
                "type": "function",
                "function": {
                    "name": "stage_patch",
                    "arguments": json.dumps(
                        {
                            "path": "src/broken.py",
                            "diff": "--- /dev/null\n+++ b/src/broken.py\n@@ -0,0 +1 @@\n+broken\n",
                            "action": "create",
                        }
                    ),
                },
            }
        ],
        "usage": {"input_tokens": 50, "output_tokens": 30},
        "latency_ms": 200,
        "model": "nemotron-3.5-lightning-free",
    }

    tool_call_restore = {
        "content": None,
        "tool_calls": [
            {
                "id": "call_restore_cp",
                "type": "function",
                "function": {
                    "name": "checkpoint_restore",
                    "arguments": json.dumps({"checkpoint_id": cp_orig_id}),
                },
            }
        ],
        "usage": {"input_tokens": 50, "output_tokens": 30},
        "latency_ms": 200,
        "model": "nemotron-3.5-lightning-free",
    }

    done_response = {
        "content": "Restored stable state.",
        "tool_calls": None,
        "usage": {"input_tokens": 50, "output_tokens": 20},
        "latency_ms": 100,
        "model": "nemotron-3.5-lightning-free",
    }

    async def _mock_complete(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return tool_call_stage_broken
        elif call_count == 2:
            return tool_call_restore
        return done_response

    queue = SseQueue()
    orchestrator = SessionOrchestrator(
        session_id=session_id,
        db=db,
        gh_token=None,
    )

    with patch.object(orchestrator._llm, "complete", side_effect=_mock_complete):
        await orchestrator.run(
            user_message="break something then restore",
            queue=queue,
        )

    # Expunge identity map and reload freshly from DB to verify persistence.
    db.expunge_all()
    stmt = select(AgentSession).where(AgentSession.id == session_id)
    res = await db.execute(stmt)
    persisted_session = res.scalars().first()

    assert persisted_session is not None
    # Verify persisted staged_patches matches the checkpoint's content, NOT the broken patch.
    assert persisted_session.staged_patches == original_staged
    assert "src/broken.py" not in (persisted_session.staged_patches or {})
    # Verify conversation history preserves turn 1 plus the restore turn interaction
    assert len(persisted_session.conversation_history or []) >= 4

