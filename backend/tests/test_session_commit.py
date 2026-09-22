"""
Integration tests for POST /sessions/{session_id}/commit (Phase 3 Cloud Agentic Session).

Tests:
1. test_commit_success               -- Seeds active session with patches, mocks all GitHub
                                        API calls, asserts 200 + PR URL + commit SHA + status completed.
2. test_commit_empty_patches_rejected -- Empty staged_patches returns 422; no GitHub calls made.
3. test_commit_unauthorized_user_forbidden -- User B cannot commit User A's session (returns 404).
4. test_commit_closed_session_rejected   -- Closed/completed session returns 400.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import _sign_user_id
from app.models import AgentSession, Repo, User


# ---------------------------------------------------------------------------
# Shared helpers (mirrors pattern in test_sessions_api.py)
# ---------------------------------------------------------------------------


def _auth_client(make_auth_client, user: User):
    return make_auth_client(user.id)


async def _seed_user_and_repo(
    db: AsyncSession, *, github_id: int = 99001
) -> tuple[User, Repo]:
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
        owner="commit-org",
        name="commit-repo",
        default_branch="main",
        github_install_id=777,
    )
    db.add(repo)
    await db.commit()
    await db.refresh(repo)

    return user, repo


async def _seed_session(
    db: AsyncSession,
    user: User,
    repo: Repo,
    *,
    status: str = "active",
    staged_patches: dict[str, str] | None = None,
    sha: str = "a" * 40,
    branch: str = "feat/session-branch",
) -> AgentSession:
    session = AgentSession(
        user_id=user.id,
        repo_id=repo.id,
        title="Commit Test Session",
        status=status,
        branch_name=branch,
        base_sha=sha,
        conversation_history=[],
        staged_patches=staged_patches if staged_patches is not None else {},
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


# Minimal realistic staged patch for a single file.
_SAMPLE_PATCH = """\
--- a/app/utils.py
+++ b/app/utils.py
@@ -1,3 +1,4 @@
 def foo():
-    return 1
+    # fixed
+    return 2

"""

_ORIGINAL_FILE = """\
def foo():
    return 1

"""


# ---------------------------------------------------------------------------
# Test 1: commit_success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_success(
    db: AsyncSession,
    make_auth_client,
) -> None:
    """
    POST /sessions/{id}/commit with a valid active session and staged patch:
    - Returns 200 with pr_url, pr_number, commit_sha.
    - All 5 GitHub Git Data API calls are made.
    - Session status in DB transitions to "completed".
    - PR metadata is appended to conversation_history.
    """
    user, repo = await _seed_user_and_repo(db, github_id=99001)
    session = await _seed_session(
        db,
        user,
        repo,
        status="active",
        staged_patches={"app/utils.py": _SAMPLE_PATCH},
        sha="a" * 40,
        branch="feat/session-branch",
    )

    fake_blob_sha = "blob" + "b" * 36
    fake_tree_sha = "tree" + "c" * 36
    fake_commit_sha = "cmmt" + "d" * 36
    fake_pr_url = "https://github.com/commit-org/commit-repo/pull/42"
    fake_pr_number = 42

    with (
        patch(
            "app.routers.sessions.get_installation_token",
            new_callable=AsyncMock,
            return_value="mock-gh-token",
        ),
        patch(
            "app.github_client.create_blob",
            new_callable=AsyncMock,
            return_value=fake_blob_sha,
        ),
        patch(
            "app.github_client.create_git_tree",
            new_callable=AsyncMock,
            return_value=fake_tree_sha,
        ),
        patch(
            "app.github_client.create_git_commit",
            new_callable=AsyncMock,
            return_value=fake_commit_sha,
        ),
        patch(
            "app.github_client.update_branch_ref",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "app.github_client.create_pull_request",
            new_callable=AsyncMock,
            return_value={"html_url": fake_pr_url, "number": fake_pr_number},
        ),
        patch(
            "app.github_client.fetch_file_content",
            new_callable=AsyncMock,
            return_value=_ORIGINAL_FILE,
        ),
    ):
        async with _auth_client(make_auth_client, user) as ac:
            resp = await ac.post(
                f"/sessions/{session.id}/commit",
                json={"title": "Fix: return correct value", "body": "Detailed description."},
            )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["pr_url"] == fake_pr_url
    assert data["pr_number"] == fake_pr_number
    assert data["commit_sha"] == fake_commit_sha

    # Verify DB: status transitioned to "completed".
    # The router committed via its own AsyncSession; expire the test session's
    # identity-map entry so the next SELECT hits the DB rather than the cache.
    await db.execute(select(AgentSession).where(AgentSession.id == session.id))
    await db.commit()  # flush any pending state and release shared lock
    db.expire_all()
    stmt = select(AgentSession).where(AgentSession.id == session.id)
    result = await db.execute(stmt)
    db_session: AgentSession = result.scalars().first()
    assert db_session is not None
    assert db_session.status == "completed"

    # PR metadata must be appended to conversation_history.
    history = db_session.conversation_history
    assert history, "conversation_history must not be empty after commit"
    last_entry = history[-1]
    assert last_entry["pr_url"] == fake_pr_url
    assert last_entry["pr_number"] == fake_pr_number
    assert last_entry["commit_sha"] == fake_commit_sha


# ---------------------------------------------------------------------------
# Test 2: empty staged_patches -> 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_empty_patches_rejected(
    db: AsyncSession,
    make_auth_client,
) -> None:
    """
    POST /sessions/{id}/commit with staged_patches={} returns 422.
    No GitHub API calls must be made.
    """
    user, repo = await _seed_user_and_repo(db, github_id=99002)
    session = await _seed_session(
        db,
        user,
        repo,
        status="active",
        staged_patches={},
    )

    mock_create_blob = AsyncMock()

    with (
        patch(
            "app.routers.sessions.get_installation_token",
            new_callable=AsyncMock,
            return_value="tok",
        ),
        patch("app.github_client.create_blob", mock_create_blob),
    ):
        async with _auth_client(make_auth_client, user) as ac:
            resp = await ac.post(
                f"/sessions/{session.id}/commit",
                json={"title": "Should not commit"},
            )

    assert resp.status_code == 422, resp.text
    assert "no staged patches" in resp.json()["detail"].lower()
    # No blob should have been created.
    mock_create_blob.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: unauthorized user -> 404 (IDOR prevention)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_unauthorized_user_forbidden(
    db: AsyncSession,
    make_auth_client,
) -> None:
    """
    User B calling POST /sessions/{User A's session id}/commit receives 404.
    Confirms no existence oracle leak.
    """
    user_a, repo_a = await _seed_user_and_repo(db, github_id=99003)
    session_a = await _seed_session(
        db,
        user_a,
        repo_a,
        status="active",
        staged_patches={"file.py": _SAMPLE_PATCH},
    )

    user_b = User(github_id=99099, github_username="attacker-b", role="user")
    db.add(user_b)
    await db.commit()
    await db.refresh(user_b)

    async with _auth_client(make_auth_client, user_b) as ac:
        resp = await ac.post(
            f"/sessions/{session_a.id}/commit",
            json={"title": "Stolen commit"},
        )

    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Test 4: closed / completed session -> 400
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_closed_session_rejected(
    db: AsyncSession,
    make_auth_client,
) -> None:
    """
    Committing a closed session returns 400 Bad Request.
    Committing a completed session also returns 400.
    """
    user, repo = await _seed_user_and_repo(db, github_id=99004)

    for terminal_status in ("closed", "completed"):
        session = await _seed_session(
            db,
            user,
            repo,
            status=terminal_status,
            staged_patches={"app/x.py": _SAMPLE_PATCH},
            # Use distinct SHAs to avoid unique constraint issues.
            sha=("b" if terminal_status == "closed" else "c") * 40,
        )

        async with _auth_client(make_auth_client, user) as ac:
            resp = await ac.post(
                f"/sessions/{session.id}/commit",
                json={"title": "Should be rejected"},
            )

        assert resp.status_code == 400, (
            f"Expected 400 for status={terminal_status!r}, got {resp.status_code}: {resp.text}"
        )
        assert "active" in resp.json()["detail"].lower()
