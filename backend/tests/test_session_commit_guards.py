"""
Hermetic unit tests for commit-path guards and tree SHA resolution in sessions.py
and github_client.py (Issue #63).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import respx
import httpx
from fastapi import HTTPException

from app.models import AgentSession, Repo, User
from app.routers.sessions import SessionCommitIn, commit_session
from app.github_client import (
    GITHUB_API_BASE,
    fetch_commit_tree_sha,
    GitHubClientError,
    GitHubResourceNotFoundError,
)


def _make_mock_db(session: AgentSession) -> MagicMock:
    """Create a mock database session that returns `session` on execute()."""
    mock_db = MagicMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = session
    mock_db.execute = AsyncMock(return_value=mock_result)
    mock_db.commit = AsyncMock()
    return mock_db


def _make_mock_user_and_session(
    staged_patches: dict[str, str],
    base_sha: str = "commit_sha_" + "1" * 29,
) -> tuple[User, AgentSession]:
    user = User(
        id=uuid.uuid4(),
        github_id=12345,
        github_username="testuser",
        role="user",
    )
    repo = Repo(
        id=uuid.uuid4(),
        user_id=user.id,
        owner="test-owner",
        name="test-repo",
        default_branch="main",
    )
    session = AgentSession(
        id=uuid.uuid4(),
        user_id=user.id,
        repo_id=repo.id,
        title="Test Session",
        status="active",
        branch_name="haunter/session-test",
        base_sha=base_sha,
        conversation_history=[],
        staged_patches=staged_patches,
    )
    session.repo = repo
    return user, session


@pytest.mark.asyncio
async def test_commit_missing_base_raises_422() -> None:
    """Issue #63: sessions.py raises HTTP 422 if base content is missing for a non-creation patch."""
    user, session = _make_mock_user_and_session(
        staged_patches={
            "app/missing.py": "--- a/app/missing.py\n+++ b/app/missing.py\n@@ -1 +1 @@\n-old\n+new\n"
        }
    )
    mock_db = _make_mock_db(session)
    body = SessionCommitIn(title="Fix missing file")

    with (
        patch("app.routers.sessions.get_installation_token", new_callable=AsyncMock, return_value="tok"),
        patch("app.github_client.fetch_file_content", new_callable=AsyncMock, return_value=None),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await commit_session(
                session_id=session.id,
                body=body,
                current_user=user,
                db=mock_db,
            )

    assert exc_info.value.status_code == 422
    assert "Base file 'app/missing.py' not found at revision" in exc_info.value.detail


@pytest.mark.asyncio
async def test_commit_empty_patched_content_raises_422() -> None:
    """Issue #63: sessions.py rejects empty patched_content rather than creating a 0-byte blob."""
    user, session = _make_mock_user_and_session(
        staged_patches={
            "app/empty.py": "--- /dev/null\n+++ b/app/empty.py\n"
        }
    )
    mock_db = _make_mock_db(session)
    body = SessionCommitIn(title="Empty file commit")

    with (
        patch("app.routers.sessions.get_installation_token", new_callable=AsyncMock, return_value="tok"),
        patch("app.github_client.fetch_file_content", new_callable=AsyncMock, return_value=None),
        patch("app.services.patch_applier.apply_unified_diff", return_value=""),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await commit_session(
                session_id=session.id,
                body=body,
                current_user=user,
                db=mock_db,
            )

    assert exc_info.value.status_code == 422
    assert "Patched content for 'app/empty.py' is empty" in exc_info.value.detail


@pytest.mark.asyncio
async def test_commit_resolves_tree_sha_from_commit() -> None:
    """Issue #63: base_tree is passed a tree SHA resolved from the commit SHA."""
    user, session = _make_mock_user_and_session(
        staged_patches={
            "app/valid.py": "--- a/app/valid.py\n+++ b/app/valid.py\n@@ -1 +1 @@\n-old\n+new\n"
        },
        base_sha="commit_sha_12345",
    )
    mock_db = _make_mock_db(session)
    body = SessionCommitIn(title="Valid commit")

    fake_tree_sha = "resolved_tree_sha_67890"
    mock_create_tree = AsyncMock(return_value="new_tree_sha_99999")

    with (
        patch("app.routers.sessions.get_installation_token", new_callable=AsyncMock, return_value="tok"),
        patch("app.github_client.fetch_file_content", new_callable=AsyncMock, return_value="old\n"),
        patch("app.services.patch_applier.apply_unified_diff", return_value="new\n"),
        patch("app.github_client.create_blob", new_callable=AsyncMock, return_value="blob_1"),
        patch("app.github_client.fetch_commit_tree_sha", new_callable=AsyncMock, return_value=fake_tree_sha) as mock_fetch_tree,
        patch("app.github_client.create_git_tree", mock_create_tree),
        patch("app.github_client.create_git_commit", new_callable=AsyncMock, return_value="new_commit_sha"),
        patch("app.github_client.update_branch_ref", new_callable=AsyncMock, return_value=None),
        patch("app.github_client.create_pull_request", new_callable=AsyncMock, return_value={"html_url": "https://github.com/pr/1", "number": 1}),
    ):
        result = await commit_session(
            session_id=session.id,
            body=body,
            current_user=user,
            db=mock_db,
        )

    assert result.commit_sha == "new_commit_sha"
    mock_fetch_tree.assert_called_once_with(
        owner="test-owner",
        repo="test-repo",
        commit_sha="commit_sha_12345",
        installation_token="tok",
    )
    mock_create_tree.assert_called_once_with(
        owner="test-owner",
        repo="test-repo",
        tree=[{"path": "app/valid.py", "mode": "100644", "type": "blob", "sha": "blob_1"}],
        base_tree=fake_tree_sha,
        installation_token="tok",
    )


@pytest.mark.asyncio
async def test_commit_creation_patch_allows_none_base() -> None:
    """A creation patch with '--- /dev/null' legitimately has no base content at session.base_sha."""
    user, session = _make_mock_user_and_session(
        staged_patches={
            "app/new_file.py": "--- /dev/null\n+++ b/app/new_file.py\n@@ -0,0 +1 @@\n+print('hello')\n"
        }
    )
    mock_db = _make_mock_db(session)
    body = SessionCommitIn(title="Add new file")

    with (
        patch("app.routers.sessions.get_installation_token", new_callable=AsyncMock, return_value="tok"),
        patch("app.github_client.fetch_file_content", new_callable=AsyncMock, return_value=None),
        patch("app.github_client.create_blob", new_callable=AsyncMock, return_value="blob_new"),
        patch("app.github_client.fetch_commit_tree_sha", new_callable=AsyncMock, return_value="base_tree_sha"),
        patch("app.github_client.create_git_tree", new_callable=AsyncMock, return_value="tree_new"),
        patch("app.github_client.create_git_commit", new_callable=AsyncMock, return_value="commit_new"),
        patch("app.github_client.update_branch_ref", new_callable=AsyncMock, return_value=None),
        patch("app.github_client.create_pull_request", new_callable=AsyncMock, return_value={"html_url": "https://github.com/pr/2", "number": 2}),
    ):
        result = await commit_session(
            session_id=session.id,
            body=body,
            current_user=user,
            db=mock_db,
        )

    assert result.commit_sha == "commit_new"
    assert result.pr_number == 2


@pytest.mark.asyncio
async def test_commit_deletion_patch_creates_tree_entry_with_null_sha() -> None:
    """A deletion patch (+++ /dev/null) adds a tree entry with sha=None and does not fail empty-content guard."""
    user, session = _make_mock_user_and_session(
        staged_patches={
            "app/deleted.py": "--- a/app/deleted.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-old\n"
        }
    )
    mock_db = _make_mock_db(session)
    body = SessionCommitIn(title="Delete file")

    mock_create_tree = AsyncMock(return_value="tree_after_delete")

    with (
        patch("app.routers.sessions.get_installation_token", new_callable=AsyncMock, return_value="tok"),
        patch("app.github_client.fetch_file_content", new_callable=AsyncMock, return_value="old\n"),
        patch("app.github_client.fetch_commit_tree_sha", new_callable=AsyncMock, return_value="base_tree_sha"),
        patch("app.github_client.create_git_tree", mock_create_tree),
        patch("app.github_client.create_git_commit", new_callable=AsyncMock, return_value="commit_delete"),
        patch("app.github_client.update_branch_ref", new_callable=AsyncMock, return_value=None),
        patch("app.github_client.create_pull_request", new_callable=AsyncMock, return_value={"html_url": "https://github.com/pr/3", "number": 3}),
    ):
        result = await commit_session(
            session_id=session.id,
            body=body,
            current_user=user,
            db=mock_db,
        )

    assert result.commit_sha == "commit_delete"
    mock_create_tree.assert_called_once_with(
        owner="test-owner",
        repo="test-repo",
        tree=[{"path": "app/deleted.py", "mode": "100644", "type": "blob", "sha": None}],
        base_tree="base_tree_sha",
        installation_token="tok",
    )


@pytest.mark.asyncio
async def test_commit_tree_resolution_failure_raises_502() -> None:
    """If fetch_commit_tree_sha raises GitHubClientError, commit_session returns HTTP 502 instead of falling back to commit SHA."""
    user, session = _make_mock_user_and_session(
        staged_patches={
            "app/valid.py": "--- a/app/valid.py\n+++ b/app/valid.py\n@@ -1 +1 @@\n-old\n+new\n"
        }
    )
    mock_db = _make_mock_db(session)
    body = SessionCommitIn(title="Tree resolution failure")

    with (
        patch("app.routers.sessions.get_installation_token", new_callable=AsyncMock, return_value="tok"),
        patch("app.github_client.fetch_file_content", new_callable=AsyncMock, return_value="old\n"),
        patch("app.github_client.create_blob", new_callable=AsyncMock, return_value="blob_1"),
        patch("app.github_client.fetch_commit_tree_sha", new_callable=AsyncMock, side_effect=GitHubClientError("GitHub API 500")),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await commit_session(
                session_id=session.id,
                body=body,
                current_user=user,
                db=mock_db,
            )

    assert exc_info.value.status_code == 502
    assert "Failed to resolve Git tree SHA for commit" in exc_info.value.detail


@pytest.mark.asyncio
@respx.mock
async def test_fetch_commit_tree_sha_github_client_success() -> None:
    """fetch_commit_tree_sha calls GET /repos/{owner}/{repo}/git/commits/{sha} and returns tree sha."""
    owner = "org"
    repo = "repo"
    commit_sha = "c" * 40
    expected_tree_sha = "t" * 40

    respx.get(f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/commits/{commit_sha}").mock(
        return_value=httpx.Response(
            200,
            json={
                "sha": commit_sha,
                "tree": {"sha": expected_tree_sha, "url": "https://..."},
                "message": "feat: test",
            },
        )
    )

    resolved = await fetch_commit_tree_sha(owner, repo, commit_sha, installation_token="test-token")
    assert resolved == expected_tree_sha


@pytest.mark.asyncio
@respx.mock
async def test_fetch_commit_tree_sha_github_client_not_found() -> None:
    """fetch_commit_tree_sha raises GitHubResourceNotFoundError on 404."""
    owner = "org"
    repo = "repo"
    commit_sha = "missing_commit"

    respx.get(f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/commits/{commit_sha}").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )

    with pytest.raises(GitHubResourceNotFoundError):
        await fetch_commit_tree_sha(owner, repo, commit_sha)
