"""
Hermetic unit tests for commit-path guards and tree SHA resolution in sessions.py
and github_client.py (Issue #63).
"""

from __future__ import annotations

import uuid
from contextlib import ExitStack
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
from app.services.session_tools.recon import _MAX_PATH_LEN, _validate_file_path


@pytest.fixture(autouse=True)
def _allow_repo_write_access():
    """Pipeline tests exercise commit behaviour, not GitHub user permissions."""
    with patch(
        "app.services.session_commit._verify_user_repo_permission",
        new_callable=AsyncMock,
        return_value=None,
    ):
        yield


def _make_mock_db(session: AgentSession) -> MagicMock:
    """Create a mock database session that returns `session` on execute()."""
    mock_db = MagicMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = session
    mock_db.execute = AsyncMock(return_value=mock_result)
    mock_db.commit = AsyncMock()
    return mock_db


_GITHUB_WRITE_TARGETS = {
    "fetch_file_content": "app.github_client.fetch_file_content",
    "create_blob": "app.github_client.create_blob",
    "fetch_commit_tree_sha": "app.github_client.fetch_commit_tree_sha",
    "create_git_tree": "app.github_client.create_git_tree",
    "create_git_commit": "app.github_client.create_git_commit",
    "get_branch_ref": "app.github.pr.get_branch_ref",
    "create_branch": "app.github.pr.create_branch",
    "update_branch_ref": "app.github.pr.update_branch_ref",
    "create_pull_request": "app.github_client.create_pull_request",
}


class _NoGithubWrites:
    """Context manager that patches every write-side GitHub call.

    ``__enter__`` returns ``(stack, mocks)`` so tests can bind both in the
    enclosing ``with (...)`` tuple and assert on each mock afterward.
    """

    def __enter__(self) -> tuple[ExitStack, dict[str, AsyncMock]]:
        self._stack = ExitStack()
        mocks = {
            name: self._stack.enter_context(patch(target, new_callable=AsyncMock))
            for name, target in _GITHUB_WRITE_TARGETS.items()
        }
        return self._stack, mocks

    def __exit__(self, *exc_info: object) -> bool:
        return self._stack.__exit__(*exc_info)


def _no_github_writes() -> _NoGithubWrites:
    return _NoGithubWrites()


def _assert_no_github_writes(mocks: dict[str, AsyncMock]) -> None:
    for name, mock in mocks.items():
        mock.assert_not_called()


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
        patch("app.services.session_commit.get_installation_token", new_callable=AsyncMock, return_value="tok"),
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
    """Issue #63: truncating an existing file to empty is rejected, not a 0-byte blob.

    Empty content from a *creation* patch is allowed (see
    test_creation_patch_may_introduce_empty_file); this test pins the truncation side.
    """
    user, session = _make_mock_user_and_session(
        staged_patches={
            "app/empty.py": "--- a/app/empty.py\n+++ b/app/empty.py\n@@ -1 +0,0 @@\n-old\n"
        }
    )
    mock_db = _make_mock_db(session)
    body = SessionCommitIn(title="Empty file commit")

    with (
        patch("app.services.session_commit.get_installation_token", new_callable=AsyncMock, return_value="tok"),
        patch("app.github_client.fetch_file_content", new_callable=AsyncMock, return_value="old\n"),
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
    resolver = AsyncMock(return_value="tok")

    with (
        patch("app.services.session_commit.get_installation_token", resolver),
        patch("app.github_client.fetch_file_content", new_callable=AsyncMock, return_value="old\n"),
        patch("app.services.patch_applier.apply_unified_diff", return_value="new\n"),
        patch("app.github_client.create_blob", new_callable=AsyncMock, return_value="blob_1"),
        patch("app.github_client.fetch_commit_tree_sha", new_callable=AsyncMock, return_value=fake_tree_sha) as mock_fetch_tree,
        patch("app.github_client.create_git_tree", mock_create_tree),
        patch("app.github_client.create_git_commit", new_callable=AsyncMock, return_value="new_commit_sha"),
        patch("app.github.pr.get_branch_ref", new_callable=AsyncMock, return_value="existing_branch_sha"),
        patch("app.github.pr.update_branch_ref", new_callable=AsyncMock, return_value=None),
        patch("app.github_client.create_pull_request", new_callable=AsyncMock, return_value={"html_url": "https://github.com/pr/1", "number": 1}),
    ):
        result = await commit_session(
            session_id=session.id,
            body=body,
            current_user=user,
            db=mock_db,
        )

    assert result.commit_sha == "new_commit_sha"
    resolver.assert_awaited_once_with(session.repo)
    mock_fetch_tree.assert_called_once_with(
        owner="test-owner",
        repo="test-repo",
        commit_sha="commit_sha_12345",
        installation_token="tok",
        allow_global_token=False,
    )
    mock_create_tree.assert_called_once_with(
        owner="test-owner",
        repo="test-repo",
        tree=[{"path": "app/valid.py", "mode": "100644", "type": "blob", "sha": "blob_1"}],
        base_tree=fake_tree_sha,
        installation_token="tok",
        allow_global_token=False,
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
        patch("app.services.session_commit.get_installation_token", new_callable=AsyncMock, return_value="tok"),
        patch("app.github_client.fetch_file_content", new_callable=AsyncMock, return_value=None),
        patch("app.github_client.create_blob", new_callable=AsyncMock, return_value="blob_new"),
        patch("app.github_client.fetch_commit_tree_sha", new_callable=AsyncMock, return_value="base_tree_sha"),
        patch("app.github_client.create_git_tree", new_callable=AsyncMock, return_value="tree_new"),
        patch("app.github_client.create_git_commit", new_callable=AsyncMock, return_value="commit_new"),
        patch("app.github.pr.get_branch_ref", new_callable=AsyncMock, return_value="existing_branch_sha"),
        patch("app.github.pr.update_branch_ref", new_callable=AsyncMock, return_value=None),
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
async def test_commit_empty_creation_patch_publishes_empty_blob() -> None:
    """A headers-only creation patch (empty file from tool_create_file) publishes a 0-byte blob."""
    user, session = _make_mock_user_and_session(
        staged_patches={
            "app/empty_init.py": "--- /dev/null\n+++ b/app/empty_init.py\n"
        }
    )
    mock_db = _make_mock_db(session)
    body = SessionCommitIn(title="Add empty file")

    mock_create_blob = AsyncMock(return_value="blob_empty")

    with (
        patch("app.services.session_commit.get_installation_token", new_callable=AsyncMock, return_value="tok"),
        patch("app.github_client.fetch_file_content", new_callable=AsyncMock, return_value=None),
        patch("app.github_client.create_blob", mock_create_blob),
        patch("app.github_client.fetch_commit_tree_sha", new_callable=AsyncMock, return_value="base_tree_sha"),
        patch("app.github_client.create_git_tree", new_callable=AsyncMock, return_value="tree_empty"),
        patch("app.github_client.create_git_commit", new_callable=AsyncMock, return_value="commit_empty"),
        patch("app.github.pr.get_branch_ref", new_callable=AsyncMock, return_value="existing_branch_sha"),
        patch("app.github.pr.update_branch_ref", new_callable=AsyncMock, return_value=None),
        patch("app.github_client.create_pull_request", new_callable=AsyncMock, return_value={"html_url": "https://github.com/pr/4", "number": 4}),
    ):
        result = await commit_session(
            session_id=session.id,
            body=body,
            current_user=user,
            db=mock_db,
        )

    assert result.commit_sha == "commit_empty"
    mock_create_blob.assert_called_once_with(
        owner="test-owner",
        repo="test-repo",
        content="",
        encoding="utf-8",
        installation_token="tok",
        allow_global_token=False,
    )


@pytest.mark.asyncio
async def test_commit_deletion_patch_creates_tree_entry_with_null_sha() -> None:
    """A deletion patch (+++ /dev/null) adds a null-sha tree entry and does not fail empty-content guard."""
    user, session = _make_mock_user_and_session(
        staged_patches={
            "app/deleted.py": "--- a/app/deleted.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-old\n"
        }
    )
    mock_db = _make_mock_db(session)
    body = SessionCommitIn(title="Delete file")

    mock_create_tree = AsyncMock(return_value="tree_after_delete")

    with (
        patch("app.services.session_commit.get_installation_token", new_callable=AsyncMock, return_value="tok"),
        patch("app.github_client.fetch_file_content", new_callable=AsyncMock, return_value="old\n"),
        patch("app.github_client.fetch_commit_tree_sha", new_callable=AsyncMock, return_value="base_tree_sha"),
        patch("app.github_client.create_git_tree", mock_create_tree),
        patch("app.github_client.create_git_commit", new_callable=AsyncMock, return_value="commit_delete"),
        patch("app.github.pr.get_branch_ref", new_callable=AsyncMock, return_value="existing_branch_sha"),
        patch("app.github.pr.update_branch_ref", new_callable=AsyncMock, return_value=None),
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
        allow_global_token=False,
    )


@pytest.mark.asyncio
async def test_commit_deletion_patch_with_nonempty_content_raises_422() -> None:
    """A patch with '+++ /dev/null' whose applied content is non-empty must raise HTTP 422."""
    user, session = _make_mock_user_and_session(
        staged_patches={
            "app/bad_del.py": "--- a/app/bad_del.py\n+++ /dev/null\n@@ -1,2 +1,1 @@\n-line1\n line2\n"
        }
    )
    mock_db = _make_mock_db(session)
    body = SessionCommitIn(title="Malformed delete")

    with (
        patch("app.services.session_commit.get_installation_token", new_callable=AsyncMock, return_value="tok"),
        patch("app.github_client.fetch_file_content", new_callable=AsyncMock, return_value="line1\nline2\n"),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await commit_session(
                session_id=session.id,
                body=body,
                current_user=user,
                db=mock_db,
            )

    assert exc_info.value.status_code == 422
    assert "resulted in non-empty content" in exc_info.value.detail


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
        patch("app.services.session_commit.get_installation_token", new_callable=AsyncMock, return_value="tok"),
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


# ---------------------------------------------------------------------------
# Token mint ordering: every rejection happens before credential resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_patches_rejected_before_token_mint() -> None:
    """Catches the broken variant that mints a token before the empty guard."""
    user, session = _make_mock_user_and_session(staged_patches={})
    mock_db = _make_mock_db(session)
    resolver = AsyncMock(return_value="tok")

    with (
        patch("app.services.session_commit.get_installation_token", resolver),
        _no_github_writes() as (stack, mocks),
    ):
        with stack:
            with pytest.raises(HTTPException) as exc_info:
                await commit_session(
                    session_id=session.id,
                    body=SessionCommitIn(title="Fix"),
                    current_user=user,
                    db=mock_db,
                )

    assert exc_info.value.status_code == 422
    resolver.assert_not_called()
    _assert_no_github_writes(mocks)


@pytest.mark.asyncio
async def test_non_active_session_rejected_before_token_mint() -> None:
    """Catches the broken variant that mints a token before the status guard."""
    user, session = _make_mock_user_and_session(staged_patches={"app/x.py": "diff"})
    session.status = "completed"
    mock_db = _make_mock_db(session)
    resolver = AsyncMock(return_value="tok")

    with (
        patch("app.services.session_commit.get_installation_token", resolver),
        _no_github_writes() as (stack, mocks),
    ):
        with stack:
            with pytest.raises(HTTPException) as exc_info:
                await commit_session(
                    session_id=session.id,
                    body=SessionCommitIn(title="Fix"),
                    current_user=user,
                    db=mock_db,
                )

    assert exc_info.value.status_code == 400
    resolver.assert_not_called()
    _assert_no_github_writes(mocks)


@pytest.mark.asyncio
async def test_blank_title_rejected_before_token_mint() -> None:
    """Catches minting a credential for a request the publisher will reject on shape."""
    user, session = _make_mock_user_and_session(
        staged_patches={"app/x.py": "--- a/app/x.py\n+++ b/app/x.py\n@@ -1 +1 @@\n-a\n+b\n"}
    )
    mock_db = _make_mock_db(session)
    resolver = AsyncMock(return_value="tok")
    perms = AsyncMock()

    with (
        patch("app.services.session_commit.get_installation_token", resolver),
        patch("app.services.session_commit._verify_user_repo_permission", perms),
        _no_github_writes() as (stack, mocks),
    ):
        with stack:
            with pytest.raises(HTTPException) as exc_info:
                await commit_session(
                    session_id=session.id,
                    body=SessionCommitIn(title="   "),
                    current_user=user,
                    db=mock_db,
                )

    assert exc_info.value.status_code == 400
    resolver.assert_not_called()
    perms.assert_not_called()
    _assert_no_github_writes(mocks)


@pytest.mark.asyncio
async def test_protected_branch_rejected_before_token_mint() -> None:
    """Catches the router-mints-first regression: main is rejected with no credential work."""
    user, session = _make_mock_user_and_session(
        staged_patches={"app/x.py": "--- a/app/x.py\n+++ b/app/x.py\n@@ -1 +1 @@\n-a\n+b\n"}
    )
    session.branch_name = "main"
    mock_db = _make_mock_db(session)
    resolver = AsyncMock(return_value="tok")
    perms = AsyncMock()

    with (
        patch("app.services.session_commit.get_installation_token", resolver),
        patch("app.services.session_commit._verify_user_repo_permission", perms),
        _no_github_writes() as (stack, mocks),
    ):
        with stack:
            with pytest.raises(HTTPException) as exc_info:
                await commit_session(
                    session_id=session.id,
                    body=SessionCommitIn(title="Fix"),
                    current_user=user,
                    db=mock_db,
                )

    assert exc_info.value.status_code == 400
    assert "main" in exc_info.value.detail
    resolver.assert_not_called()
    perms.assert_not_called()
    _assert_no_github_writes(mocks)


@pytest.mark.asyncio
async def test_custom_default_branch_rejected_before_token_mint() -> None:
    """Exact default-branch equality is enforced before credentials are minted."""
    user, session = _make_mock_user_and_session(
        staged_patches={"app/x.py": "--- a/app/x.py\n+++ b/app/x.py\n@@ -1 +1 @@\n-a\n+b\n"}
    )
    assert session.repo is not None
    session.repo.default_branch = "trunk"
    session.branch_name = "trunk"
    mock_db = _make_mock_db(session)
    resolver = AsyncMock(return_value="tok")
    perms = AsyncMock()

    with (
        patch("app.services.session_commit.get_installation_token", resolver),
        patch("app.services.session_commit._verify_user_repo_permission", perms),
        _no_github_writes() as (stack, mocks),
    ):
        with stack:
            with pytest.raises(HTTPException) as exc_info:
                await commit_session(
                    session_id=session.id,
                    body=SessionCommitIn(title="Fix"),
                    current_user=user,
                    db=mock_db,
                )

    assert exc_info.value.status_code == 400
    assert "default branch" in exc_info.value.detail.lower()
    resolver.assert_not_called()
    perms.assert_not_called()
    _assert_no_github_writes(mocks)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_path",
    [
        "foo/./bar.py",
        "foo//bar.py",
        " dir/x.py",
        "dir/",
        "x.py/",
        ".",
        "x" * 501 + ".py",
        "y" * 600,
    ],
)
async def test_non_canonical_path_rejected_before_token_mint(bad_path: str) -> None:
    """Catches normalization-retargeting: paths are rejected before credentials or writes."""
    user, session = _make_mock_user_and_session(
        staged_patches={
            bad_path: "--- /dev/null\n+++ b/x.py\n@@ -0,0 +1 @@\n+x\n"
        }
    )
    mock_db = _make_mock_db(session)
    resolver = AsyncMock(return_value="tok")
    perms = AsyncMock()

    with (
        patch("app.services.session_commit.get_installation_token", resolver),
        patch("app.services.session_commit._verify_user_repo_permission", perms),
        _no_github_writes() as (stack, mocks),
    ):
        with stack:
            with pytest.raises(HTTPException) as exc_info:
                await commit_session(
                    session_id=session.id,
                    body=SessionCommitIn(title="Fix"),
                    current_user=user,
                    db=mock_db,
                )

    assert exc_info.value.status_code == 422
    resolver.assert_not_called()
    perms.assert_not_called()
    _assert_no_github_writes(mocks)


def test_path_length_boundary_allows_max_chars() -> None:
    """Pins the recon length boundary the rejection cases above depend on."""
    ok_path = "x" * (_MAX_PATH_LEN - 3) + ".py"
    assert len(ok_path) == _MAX_PATH_LEN
    assert _validate_file_path(ok_path) == ok_path
    with pytest.raises(ValueError, match="maximum length"):
        _validate_file_path(ok_path + "x")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected"),
    [(403, 403), (502, 503)],
)
async def test_write_access_rejection_happens_before_token_mint(
    status_code: int, expected: int
) -> None:
    """Catches minting before write-access: denial/outage must not touch credentials."""
    user, session = _make_mock_user_and_session(
        staged_patches={"app/x.py": "--- a/app/x.py\n+++ b/app/x.py\n@@ -1 +1 @@\n-a\n+b\n"}
    )
    mock_db = _make_mock_db(session)
    resolver = AsyncMock(return_value="tok")
    perms = AsyncMock(side_effect=HTTPException(status_code=status_code, detail="nope"))

    with (
        patch("app.services.session_commit.get_installation_token", resolver),
        patch("app.services.session_commit._verify_user_repo_permission", perms),
        _no_github_writes() as (stack, mocks),
    ):
        with stack:
            with pytest.raises(HTTPException) as exc_info:
                await commit_session(
                    session_id=session.id,
                    body=SessionCommitIn(title="Fix"),
                    current_user=user,
                    db=mock_db,
                )

    assert exc_info.value.status_code == expected
    perms.assert_awaited_once()
    resolver.assert_not_called()
    _assert_no_github_writes(mocks)
