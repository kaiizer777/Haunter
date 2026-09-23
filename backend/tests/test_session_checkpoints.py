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

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentSession, Repo, User
from app.services.session_streamer import SseQueue
from app.services.session_tools.checkpoints import (
    create_checkpoint,
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
        user=user,
        repo=repo,
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
    try:
        await db.refresh(s)
    except Exception:
        pass
    return s


# ---------------------------------------------------------------------------
# Test 1: create_checkpoint — basic
# ---------------------------------------------------------------------------


def test_create_checkpoint() -> None:
    """create_checkpoint builds correct snapshot and appends to session.checkpoints."""
    user = MagicMock(spec=User)
    repo = MagicMock(spec=Repo)

    session = MagicMock(spec=AgentSession)
    session.staged_patches = {"src/auth.py": "--- a/src/auth.py\n+++ b/src/auth.py\n@@ -1 +1 @@\n-old\n+new\n"}
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
    # DB committed.
    db.commit.assert_awaited_once()
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
    session.staged_patches = {
        "config.py": f"+AWS_ACCESS_KEY = '{fake_key}'\n"
    }

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
    session.staged_patches = {
        "deploy.py": f"+TOKEN = '{fake_pat}'\n"
    }

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
        "db.py": "+    cursor.execute(f\"SELECT * FROM users WHERE id = {user_id}\")\n"
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
    original_patches = {"src/main.py": "--- a/src/main.py\n+++ b/src/main.py\n@@ -1 +1 @@\n-old\n+original\n"}
    later_patches = {"src/main.py": "--- a/src/main.py\n+++ b/src/main.py\n@@ -1 +1 @@\n-old\n+later\n", "new_file.py": "+new file"}

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
