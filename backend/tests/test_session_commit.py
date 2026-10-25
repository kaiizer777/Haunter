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
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import _sign_user_id
from app.github_client import GitHubClientError, GitHubResourceNotFoundError
from app.models import AgentSession, Repo, User
from tests.conftest import patch_token_mint
from tests.test_session_commit_guards import (
    _assert_no_github_writes,
    _no_github_writes,
)


@pytest.fixture()
def _allow_repo_write_access():
    """Opt-in stub: tests exercising commit behaviour past the write-access guard."""
    with patch(
        "app.services.session_commit._verify_user_repo_permission",
        new_callable=AsyncMock,
        return_value=None,
    ):
        yield


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
        name=f"commit-repo-{github_id}",
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
@pytest.mark.usefixtures("_allow_repo_write_access")
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
        patch_token_mint("mock-gh-token"),
        patch(
            "app.github_client.create_blob",
            new_callable=AsyncMock,
            return_value=fake_blob_sha,
        ),
        patch(
            "app.github_client.fetch_commit_tree_sha",
            new_callable=AsyncMock,
            return_value="resolved_tree_" + "a" * 26,
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
            "app.github.pr.get_branch_ref",
            new_callable=AsyncMock,
            return_value="existing_sha",
        ),
        patch(
            "app.github.pr.update_branch_ref",
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
                json={
                    "title": "Fix: return correct value",
                    "body": "Detailed description.",
                },
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
    session_id = session.id
    db.expire_all()
    stmt = select(AgentSession).where(AgentSession.id == session_id)
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
@pytest.mark.usefixtures("_allow_repo_write_access")
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
        patch_token_mint(),
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
@pytest.mark.usefixtures("_allow_repo_write_access")
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
@pytest.mark.usefixtures("_allow_repo_write_access")
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

        assert (
            resp.status_code == 400
        ), f"Expected 400 for status={terminal_status!r}, got {resp.status_code}: {resp.text}"
        assert "active" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Test 5: protected branch rejected with zero HTTP ref calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("protected_branch", ["main", "master", "dev", "develop"])
@pytest.mark.usefixtures("_allow_repo_write_access")
async def test_commit_protected_branch_rejected_with_zero_ref_calls(
    db: AsyncSession,
    make_auth_client,
    protected_branch: str,
) -> None:
    """
    POST /sessions/{id}/commit returns 400 and makes zero ref calls when targeting a protected branch.
    Parametrized over {main, master, dev, develop}.
    """
    user, repo = await _seed_user_and_repo(
        db, github_id=99050 + abs(hash(protected_branch)) % 1000
    )
    session = await _seed_session(
        db,
        user,
        repo,
        status="active",
        staged_patches={"app/utils.py": _SAMPLE_PATCH},
        branch=protected_branch,
    )

    mock_update_ref = AsyncMock()
    mock_create_branch = AsyncMock()
    mock_create_blob = AsyncMock()
    mock_create_commit = AsyncMock()

    with (
        patch_token_mint(),
        patch("app.github.pr.update_branch_ref", mock_update_ref),
        patch("app.github_client.update_branch_ref", mock_update_ref),
        patch("app.github.pr.create_branch", mock_create_branch),
        patch("app.github_client.create_blob", mock_create_blob),
        patch("app.github_client.create_git_commit", mock_create_commit),
    ):
        async with _auth_client(make_auth_client, user) as ac:
            resp = await ac.post(
                f"/sessions/{session.id}/commit",
                json={"title": "Commit to protected branch"},
            )

    assert (
        resp.status_code == 400
    ), f"Expected 400 for {protected_branch}, got {resp.status_code}: {resp.text}"
    detail = resp.json()["detail"].lower()
    assert (
        "protected" in detail
        or "cannot target" in detail
        or "cannot commit directly" in detail
    )
    mock_update_ref.assert_not_called()
    mock_create_branch.assert_not_called()
    mock_create_blob.assert_not_called()
    mock_create_commit.assert_not_called()


# ---------------------------------------------------------------------------
# Test 6: PR creation failure deletes newly created topic branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_allow_repo_write_access")
async def test_commit_pr_failure_cleans_up_newly_created_branch(
    db: AsyncSession,
    make_auth_client,
) -> None:
    """
    When create_pull_request fails, the newly created branch ref is deleted via delete_branch_ref
    so no commit is left on a non-PR branch.
    """
    user, repo = await _seed_user_and_repo(db, github_id=99088)
    topic_branch = f"haunter/session-{uuid.uuid4().hex[:8]}"
    session = await _seed_session(
        db,
        user,
        repo,
        status="active",
        staged_patches={"app/utils.py": _SAMPLE_PATCH},
        branch=topic_branch,
    )

    fake_blob_sha = "blob" + "b" * 36
    fake_tree_sha = "tree" + "c" * 36
    fake_commit_sha = "cmmt" + "d" * 36

    mock_delete_ref = AsyncMock()
    mock_create_branch = AsyncMock()

    with (
        patch_token_mint(),
        patch(
            "app.github_client.create_blob",
            new_callable=AsyncMock,
            return_value=fake_blob_sha,
        ),
        patch(
            "app.github_client.fetch_commit_tree_sha",
            new_callable=AsyncMock,
            return_value="resolved_tree_" + "a" * 26,
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
            "app.github_client.fetch_file_content",
            new_callable=AsyncMock,
            return_value=_ORIGINAL_FILE,
        ),
        patch(
            "app.github.pr.get_branch_ref",
            new_callable=AsyncMock,
            side_effect=[GitHubResourceNotFoundError("Branch does not exist"), fake_commit_sha],
        ),
        patch("app.github.pr.create_branch", mock_create_branch),
        patch("app.github.pr.delete_branch_ref", mock_delete_ref),
        patch(
            "app.github_client.create_pull_request",
            new_callable=AsyncMock,
            side_effect=GitHubClientError("Simulated PR creation failure"),
        ),
    ):
        async with _auth_client(make_auth_client, user) as ac:
            resp = await ac.post(
                f"/sessions/{session.id}/commit",
                json={"title": "Test PR rollback"},
            )

    assert resp.status_code == 502, resp.text
    mock_create_branch.assert_called_once()
    mock_delete_ref.assert_called_once_with(
        owner=repo.owner,
        repo=repo.name,
        branch=topic_branch,
        token="tok",
        expected_sha=fake_commit_sha,
    )


# ---------------------------------------------------------------------------
# Test 7: PR creation failure reverts existing topic branch to original_target_sha
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_allow_repo_write_access")
async def test_commit_pr_failure_reverts_existing_branch(
    db: AsyncSession,
    make_auth_client,
) -> None:
    """
    When create_pull_request fails on an existing branch, the branch ref is rolled back to original_target_sha
    via restore_branch_ref so no commit is left on a non-PR branch, preserving prior commits.
    """
    user, repo = await _seed_user_and_repo(db, github_id=99089)
    topic_branch = "feat/existing-topic"
    session = await _seed_session(
        db,
        user,
        repo,
        status="active",
        staged_patches={"app/utils.py": _SAMPLE_PATCH},
        branch=topic_branch,
        sha="base" + "0" * 36,
    )

    fake_blob_sha = "blob" + "b" * 36
    fake_tree_sha = "tree" + "c" * 36
    fake_commit_sha = "cmmt" + "d" * 36
    original_target_sha = "orig" + "1" * 36

    mock_update_ref = AsyncMock()
    mock_restore_ref = AsyncMock()

    with (
        patch_token_mint(),
        patch(
            "app.github_client.create_blob",
            new_callable=AsyncMock,
            return_value=fake_blob_sha,
        ),
        patch(
            "app.github_client.fetch_commit_tree_sha",
            new_callable=AsyncMock,
            return_value="resolved_tree_" + "a" * 26,
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
            "app.github_client.fetch_file_content",
            new_callable=AsyncMock,
            return_value=_ORIGINAL_FILE,
        ),
        patch(
            "app.github.pr.get_branch_ref",
            new_callable=AsyncMock,
            side_effect=[original_target_sha, fake_commit_sha],
        ),
        patch("app.github.pr.update_branch_ref", mock_update_ref),
        patch("app.github.pr.restore_branch_ref", mock_restore_ref),
        patch(
            "app.github_client.create_pull_request",
            new_callable=AsyncMock,
            side_effect=GitHubClientError("Simulated PR creation failure"),
        ),
    ):
        async with _auth_client(make_auth_client, user) as ac:
            resp = await ac.post(
                f"/sessions/{session.id}/commit",
                json={"title": "Test PR rollback existing branch"},
            )

    assert resp.status_code == 502, resp.text
    # mock_update_ref advanced the ref once to fake_commit_sha
    mock_update_ref.assert_called_once_with(
        owner=repo.owner,
        repo=repo.name,
        branch=topic_branch,
        sha=fake_commit_sha,
        token="tok",
        force=False,
    )
    # mock_restore_ref restored the ref to original_target_sha with expected_sha guard
    mock_restore_ref.assert_called_once_with(
        owner=repo.owner,
        repo=repo.name,
        branch=topic_branch,
        sha=original_target_sha,
        token="tok",
        expected_sha=fake_commit_sha,
    )


# ---------------------------------------------------------------------------
# Test 8: PR creation failure skips rollback if concurrent writer advanced ref
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_allow_repo_write_access")
async def test_commit_pr_failure_skips_rollback_if_concurrent_writer_advanced_branch(
    db: AsyncSession,
    make_auth_client,
) -> None:
    """
    When create_pull_request fails and another writer has advanced the branch ref past commit_sha,
    rollback is skipped to avoid overwriting concurrent work.
    """
    user, repo = await _seed_user_and_repo(db, github_id=99090)
    topic_branch = "feat/concurrent-topic"
    session = await _seed_session(
        db,
        user,
        repo,
        status="active",
        staged_patches={"app/utils.py": _SAMPLE_PATCH},
        branch=topic_branch,
        sha="base" + "0" * 36,
    )

    fake_blob_sha = "blob" + "b" * 36
    fake_tree_sha = "tree" + "c" * 36
    fake_commit_sha = "cmmt" + "d" * 36
    original_target_sha = "orig" + "1" * 36
    concurrent_sha = "conc" + "9" * 36

    mock_update_ref = AsyncMock()
    mock_restore_ref = AsyncMock()
    mock_delete_ref = AsyncMock()

    with (
        patch_token_mint(),
        patch(
            "app.github_client.create_blob",
            new_callable=AsyncMock,
            return_value=fake_blob_sha,
        ),
        patch(
            "app.github_client.fetch_commit_tree_sha",
            new_callable=AsyncMock,
            return_value="resolved_tree_" + "a" * 26,
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
            "app.github_client.fetch_file_content",
            new_callable=AsyncMock,
            return_value=_ORIGINAL_FILE,
        ),
        patch(
            "app.github.pr.get_branch_ref",
            new_callable=AsyncMock,
            side_effect=[original_target_sha, concurrent_sha],
        ),
        patch("app.github.pr.update_branch_ref", mock_update_ref),
        patch("app.github.pr.restore_branch_ref", mock_restore_ref),
        patch("app.github.pr.delete_branch_ref", mock_delete_ref),
        patch(
            "app.github_client.create_pull_request",
            new_callable=AsyncMock,
            side_effect=GitHubClientError("Simulated PR creation failure"),
        ),
    ):
        async with _auth_client(make_auth_client, user) as ac:
            resp = await ac.post(
                f"/sessions/{session.id}/commit",
                json={"title": "Test PR rollback concurrent conflict"},
            )

    assert resp.status_code == 502, resp.text
    # mock_restore_ref and mock_delete_ref must NOT be called when ref was advanced by another writer
    mock_restore_ref.assert_not_called()
    mock_delete_ref.assert_not_called()


# ---------------------------------------------------------------------------
# Test 9: caller without repository write access -> 403 (fails closed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_without_repo_write_access_forbidden(
    db: AsyncSession,
    make_auth_client,
) -> None:
    """
    A GitHub write-access denial surfaces as 403 before any Git Data write,
    and the session remains active so the user can retry or close it.
    """
    user, repo = await _seed_user_and_repo(db, github_id=99091)
    session = await _seed_session(
        db,
        user,
        repo,
        status="active",
        staged_patches={"app/utils.py": _SAMPLE_PATCH},
        branch="feat/no-write-access",
    )

    with (
        patch_token_mint(),
        patch(
            "app.services.session_commit._verify_user_repo_permission",
            new_callable=AsyncMock,
            side_effect=HTTPException(status_code=403, detail="no write access"),
        ),
        _no_github_writes() as (stack, mocks),
    ):
        with stack:
            async with _auth_client(make_auth_client, user) as ac:
                resp = await ac.post(
                    f"/sessions/{session.id}/commit",
                    json={"title": "No access"},
                )

    assert resp.status_code == 403, resp.text
    assert "write access" in resp.json()["detail"].lower()
    _assert_no_github_writes(mocks)

    await db.commit()
    session_id = session.id
    db.expire_all()
    result = await db.execute(
        select(AgentSession).where(AgentSession.id == session_id)
    )
    persisted = result.scalars().first()
    assert persisted is not None
    assert persisted.status == "active"


# ---------------------------------------------------------------------------
# Test 10: foreign branch_name override rejected before any GitHub ref work
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_allow_repo_write_access")
async def test_commit_foreign_branch_override_rejected(
    db: AsyncSession,
    make_auth_client,
) -> None:
    """
    A dashboard caller guessing another branch (e.g. a teammate's feat/ work)
    receives 400 and no branch ref is read or written.
    """
    user, repo = await _seed_user_and_repo(db, github_id=99092)
    session = await _seed_session(
        db,
        user,
        repo,
        status="active",
        staged_patches={"app/utils.py": _SAMPLE_PATCH},
        branch="feat/session-branch",
    )

    mock_get_ref = AsyncMock(return_value="existing_sha")
    mock_update_ref = AsyncMock()
    mock_create_branch = AsyncMock()
    mock_create_blob = AsyncMock()

    with (
        patch_token_mint(),
        patch("app.github.pr.get_branch_ref", mock_get_ref),
        patch("app.github.pr.update_branch_ref", mock_update_ref),
        patch("app.github.pr.create_branch", mock_create_branch),
        patch("app.github_client.create_blob", mock_create_blob),
    ):
        async with _auth_client(make_auth_client, user) as ac:
            resp = await ac.post(
                f"/sessions/{session.id}/commit",
                json={"title": "Hijack attempt", "branch_name": "feat/teammate-work"},
            )

    assert resp.status_code == 400, resp.text
    assert "only publishes to this session's branch" in resp.json()["detail"]
    mock_get_ref.assert_not_called()
    mock_update_ref.assert_not_called()
    mock_create_branch.assert_not_called()
    mock_create_blob.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_allow_repo_write_access")
async def test_commit_minted_branch_override_accepted(
    db: AsyncSession,
    make_auth_client,
) -> None:
    """
    Passing this session's own minted haunter/session-<id hex[:12]> name is accepted
    past the namespace guard (the pipeline then runs; a forced PR failure
    proves the guard did not reject it).
    """
    user, repo = await _seed_user_and_repo(db, github_id=99093)
    session = await _seed_session(
        db,
        user,
        repo,
        status="active",
        staged_patches={"app/utils.py": _SAMPLE_PATCH},
        branch="feat/session-branch",
    )
    minted = f"haunter/session-{session.id.hex[:12]}"

    with (
        patch_token_mint(),
        patch(
            "app.github_client.create_blob",
            new_callable=AsyncMock,
            return_value="blob" + "b" * 36,
        ),
        patch(
            "app.github_client.fetch_commit_tree_sha",
            new_callable=AsyncMock,
            return_value="resolved_tree_" + "a" * 26,
        ),
        patch(
            "app.github_client.create_git_tree",
            new_callable=AsyncMock,
            return_value="tree" + "c" * 36,
        ),
        patch(
            "app.github_client.create_git_commit",
            new_callable=AsyncMock,
            return_value="cmmt" + "d" * 36,
        ),
        patch(
            "app.github_client.fetch_file_content",
            new_callable=AsyncMock,
            return_value=_ORIGINAL_FILE,
        ),
        patch(
            "app.github.pr.get_branch_ref",
            new_callable=AsyncMock,
            side_effect=[
                GitHubResourceNotFoundError("Branch does not exist"),
                "cmmt" + "d" * 36,
            ],
        ),
        patch(
            "app.github.pr.create_branch",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "app.github_client.create_pull_request",
            new_callable=AsyncMock,
            side_effect=GitHubClientError("Simulated PR creation failure"),
        ),
    ):
        async with _auth_client(make_auth_client, user) as ac:
            resp = await ac.post(
                f"/sessions/{session.id}/commit",
                json={"title": "Own branch", "branch_name": minted},
            )

    assert resp.status_code == 502, resp.text
    assert "only publishes to this session's branch" not in resp.text
