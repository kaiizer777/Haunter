"""
Hermetic tests for the shared session-commit publisher (issue #70).

The publisher is the single implementation behind ``POST /sessions/{id}/commit``
and the ``create_pull_request`` session-agent tool. DB access is mocked with
real ORM objects; every GitHub call is patched at its module attribute so a
regression that falls through to the network fails loudly.
"""

from __future__ import annotations

import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import respx
from fastapi import HTTPException

from app.github_client import GitHubClientError, GitHubResourceNotFoundError
from app.models import AgentSession, Repo, User
from app.schemas import SESSION_PR_TITLE_MAX_LEN
from app.services.session_commit import SessionCommitError, publish_session_patches

_OWNER = "test-owner"
_REPO = "test-repo"
_SHA = "a" * 40
_SAMPLE_PATCH = (
    "--- a/app/utils.py\n"
    "+++ b/app/utils.py\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "+new\n"
)

_GH = "app.services.session_commit.github_client"
_GP = "app.services.session_commit.github_pr"


def _make_user() -> User:
    return User(
        id=uuid.uuid4(),
        github_id=12345,
        github_username="testuser",
        role="user",
    )


def _make_repo(*, default_branch: str = "main") -> Repo:
    return Repo(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        owner=_OWNER,
        name=_REPO,
        default_branch=default_branch,
    )


def _make_session(
    repo: Repo,
    *,
    status: str = "active",
    branch_name: str = "haunter/session-abcd1234",
    staged_patches: dict[str, str] | None = None,
) -> AgentSession:
    session = AgentSession(
        id=uuid.uuid4(),
        user_id=repo.user_id,
        repo_id=repo.id,
        title="Commit Test Session",
        status=status,
        branch_name=branch_name,
        base_sha=_SHA,
        conversation_history=[],
        staged_patches=(
            {"app/utils.py": _SAMPLE_PATCH}
            if staged_patches is None
            else staged_patches
        ),
    )
    session.repo = repo
    return session


def _make_db() -> MagicMock:
    db = MagicMock()
    db.commit = AsyncMock()
    return db


def _ref_calls() -> list[str]:
    return [str(call.request.url) for call in respx.calls if "/git/refs" in str(call.request.url)]


def _install_pipeline(
    stack: ExitStack,
    *,
    get_ref: AsyncMock,
    create_branch: AsyncMock,
    update_ref: AsyncMock,
    create_pr: AsyncMock,
    delete_ref: AsyncMock | None = None,
    restore_ref: AsyncMock | None = None,
) -> None:
    stack.enter_context(
        patch(f"{_GH}.fetch_file_content", AsyncMock(return_value="old\n"))
    )
    stack.enter_context(
        patch(
            "app.services.session_commit.patch_applier.apply_unified_diff",
            return_value="new\n",
        )
    )
    stack.enter_context(patch(f"{_GH}.create_blob", AsyncMock(return_value="blobsha")))
    stack.enter_context(
        patch(f"{_GH}.fetch_commit_tree_sha", AsyncMock(return_value="basetree"))
    )
    stack.enter_context(patch(f"{_GH}.create_git_tree", AsyncMock(return_value="newtree")))
    stack.enter_context(
        patch(f"{_GH}.create_git_commit", AsyncMock(return_value="commit1"))
    )
    stack.enter_context(patch(f"{_GP}.get_branch_ref", get_ref))
    stack.enter_context(patch(f"{_GP}.create_branch", create_branch))
    stack.enter_context(patch(f"{_GP}.update_branch_ref", update_ref))
    stack.enter_context(patch(f"{_GH}.create_pull_request", create_pr))
    if delete_ref is not None:
        stack.enter_context(patch(f"{_GP}.delete_branch_ref", delete_ref))
    if restore_ref is not None:
        stack.enter_context(patch(f"{_GP}.restore_branch_ref", restore_ref))


# ---------------------------------------------------------------------------
# Shape guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_staged_patches_rejected_422() -> None:
    """Catches guard reordering: empty sessions must fail before any GitHub work."""
    repo = _make_repo()
    session = _make_session(repo, staged_patches={})
    db = _make_db()

    with patch("app.services.session_commit._verify_user_repo_permission", AsyncMock()) as perms:
        with pytest.raises(SessionCommitError) as exc_info:
            await publish_session_patches(
                session=session,
                repo=repo,
                title="Fix",
                body=None,
                current_user=_make_user(),
                db=db,
                gh_token="tok",
            )

    assert exc_info.value.status_code == 422
    assert "no staged patches" in str(exc_info.value).lower()
    perms.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_active_session_rejected_400() -> None:
    """Catches publishing a completed/closed row after it was already published."""
    repo = _make_repo()
    session = _make_session(repo, status="completed")

    with patch("app.services.session_commit._verify_user_repo_permission", AsyncMock()) as perms:
        with pytest.raises(SessionCommitError) as exc_info:
            await publish_session_patches(
                session=session,
                repo=repo,
                title="Fix",
                body=None,
                current_user=_make_user(),
                db=_make_db(),
                gh_token="tok",
            )

    assert exc_info.value.status_code == 400
    assert "active" in str(exc_info.value).lower()
    perms.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["", "   ", "x" * (SESSION_PR_TITLE_MAX_LEN + 1)])
async def test_invalid_title_rejected_400(title: str) -> None:
    repo = _make_repo()
    session = _make_session(repo)

    with patch("app.services.session_commit._verify_user_repo_permission", AsyncMock()) as perms:
        with pytest.raises(SessionCommitError) as exc_info:
            await publish_session_patches(
                session=session,
                repo=repo,
                title=title,
                body=None,
                current_user=_make_user(),
                db=_make_db(),
                gh_token="tok",
            )

    assert exc_info.value.status_code == 400
    perms.assert_not_called()


# ---------------------------------------------------------------------------
# Branch safety: protected/default branches never touch the network
# ---------------------------------------------------------------------------


_PROTECTED_TARGETS = [
    "main",
    "master",
    "dev",
    "develop",
    "MAIN",
    "refs/heads/main",
    "refs/heads/refs/heads/dev",
    "main/../main",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("target_branch", _PROTECTED_TARGETS)
# Mock-hygiene canary: every publisher call site in this module is patched
# at function level, so no httpx traffic can occur. This sole respx tripwire
# pins that invariant; the per-target mock asserts below are the real guards.
@respx.mock
async def test_protected_branches_rejected_with_zero_ref_calls(
    target_branch: str,
) -> None:
    repo = _make_repo(default_branch="main")
    session = _make_session(repo, branch_name=target_branch)
    db = _make_db()

    mock_create_branch = AsyncMock()
    mock_update_ref = AsyncMock()
    mock_create_blob = AsyncMock()
    mock_create_tree = AsyncMock()
    mock_create_commit = AsyncMock()
    mock_create_pr = AsyncMock()

    with (
        patch("app.services.session_commit._verify_user_repo_permission", AsyncMock()) as perms,
        patch(f"{_GP}.create_branch", mock_create_branch),
        patch(f"{_GP}.update_branch_ref", mock_update_ref),
        patch(f"{_GH}.create_blob", mock_create_blob),
        patch(f"{_GH}.create_git_tree", mock_create_tree),
        patch(f"{_GH}.create_git_commit", mock_create_commit),
        patch(f"{_GH}.create_pull_request", mock_create_pr),
    ):
        with pytest.raises(SessionCommitError) as exc_info:
            await publish_session_patches(
                session=session,
                repo=repo,
                title="Fix",
                body=None,
                current_user=_make_user(),
                db=db,
                gh_token="tok",
            )

    assert exc_info.value.status_code == 400, target_branch
    # Branch validation runs before write-access or any HTTP call.
    perms.assert_not_called()
    assert len(respx.calls) == 0
    assert _ref_calls() == []
    mock_create_branch.assert_not_called()
    mock_update_ref.assert_not_called()
    mock_create_blob.assert_not_called()
    mock_create_tree.assert_not_called()
    mock_create_commit.assert_not_called()
    mock_create_pr.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_exact_default_branch_rejected_even_when_not_protected() -> None:
    """A custom default branch (trunk) is rejected by equality, not the set."""
    repo = _make_repo(default_branch="trunk")
    session = _make_session(repo, branch_name="trunk")
    mock_create_blob = AsyncMock()

    with (
        patch("app.services.session_commit._verify_user_repo_permission", AsyncMock()) as perms,
        patch(f"{_GH}.create_blob", mock_create_blob),
    ):
        with pytest.raises(SessionCommitError) as exc_info:
            await publish_session_patches(
                session=session,
                repo=repo,
                title="Fix",
                body=None,
                current_user=_make_user(),
                db=_make_db(),
                gh_token="tok",
            )

    assert exc_info.value.status_code == 400
    assert "default branch" in str(exc_info.value).lower()
    perms.assert_not_called()
    assert _ref_calls() == []
    mock_create_blob.assert_not_called()


_GIT_SPECIFIC_BRANCHES = [
    "feat/component.lock",
    "feat/trailing-dot.",
    "feat/double..dot",
    ".leading-dot",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("target_branch", _GIT_SPECIFIC_BRANCHES)
async def test_git_specific_branch_rejections_use_single_error_dialect(
    target_branch: str,
) -> None:
    """Stricter git-specific rejections share the ref helper's error dialect."""
    repo = _make_repo(default_branch="main")
    session = _make_session(repo, branch_name=target_branch)
    mock_create_blob = AsyncMock()

    with (
        patch("app.services.session_commit._verify_user_repo_permission", AsyncMock()) as perms,
        patch(f"{_GH}.create_blob", mock_create_blob),
    ):
        with pytest.raises(SessionCommitError) as exc_info:
            await publish_session_patches(
                session=session,
                repo=repo,
                title="Fix",
                body=None,
                current_user=_make_user(),
                db=_make_db(),
                gh_token="tok",
            )

    assert exc_info.value.status_code == 400
    assert "contains invalid characters" in str(exc_info.value)
    perms.assert_not_called()
    mock_create_blob.assert_not_called()


@pytest.mark.asyncio
async def test_mints_session_branch_when_none_configured() -> None:
    repo = _make_repo(default_branch="main")
    session = _make_session(repo, branch_name="")
    expected_branch = f"haunter/session-{session.id.hex[:12]}"

    mock_get_ref = AsyncMock(side_effect=GitHubResourceNotFoundError("missing"))
    mock_create_branch = AsyncMock()
    mock_update_ref = AsyncMock()
    mock_create_pr = AsyncMock(
        return_value={"html_url": "https://github.com/o/r/pull/7", "number": 7}
    )

    with (
        ExitStack() as stack,
        patch("app.services.session_commit._verify_user_repo_permission", AsyncMock()),
    ):
        _install_pipeline(
            stack,
            get_ref=mock_get_ref,
            create_branch=mock_create_branch,
            update_ref=mock_update_ref,
            create_pr=mock_create_pr,
        )
        result = await publish_session_patches(
            session=session,
            repo=repo,
            title="Fix",
            body=None,
            current_user=_make_user(),
            db=_make_db(),
            gh_token="tok",
        )

    mock_create_branch.assert_awaited_once_with(
        owner=_OWNER,
        repo=_REPO,
        branch=expected_branch,
        sha="commit1",
        token="tok",
    )
    mock_update_ref.assert_not_called()
    mock_create_pr.assert_awaited_once()
    assert mock_create_pr.await_args.kwargs["head"] == expected_branch
    assert mock_create_pr.await_args.kwargs["head"] != "main"
    assert mock_create_pr.await_args.kwargs["base"] == "main"
    assert result.branch_name == expected_branch
    assert session.branch_name == expected_branch


@pytest.mark.asyncio
async def test_explicit_topic_branch_overrides_user_controlled_session_branch() -> None:
    """Catches the tool-adopts-session-branch bug at the ref-creation boundary.

    The agent passes an explicit ``haunter/session-*`` branch; the publisher
    must create that ref and open the PR with that head, never the session's
    user-controlled ``feat/x`` value.
    """
    repo = _make_repo(default_branch="main")
    session = _make_session(repo, branch_name="feat/x")
    expected_branch = f"haunter/session-{session.id.hex[:12]}"
    mock_create_branch = AsyncMock()
    mock_create_pr = AsyncMock(
        return_value={"html_url": "https://github.com/o/r/pull/8", "number": 8}
    )

    with (
        ExitStack() as stack,
        patch("app.services.session_commit._verify_user_repo_permission", AsyncMock()),
    ):
        _install_pipeline(
            stack,
            get_ref=AsyncMock(side_effect=GitHubResourceNotFoundError("missing")),
            create_branch=mock_create_branch,
            update_ref=AsyncMock(),
            create_pr=mock_create_pr,
        )
        result = await publish_session_patches(
            session=session,
            repo=repo,
            title="Fix",
            body=None,
            current_user=_make_user(),
            db=_make_db(),
            target_branch=expected_branch,
            gh_token="tok",
        )

    created = mock_create_branch.await_args.kwargs
    assert created["branch"] == expected_branch
    assert created["branch"] != "feat/x"
    assert mock_create_pr.await_args.kwargs["head"] == expected_branch
    assert result.branch_name == expected_branch
    assert session.branch_name == expected_branch


# ---------------------------------------------------------------------------
# Write access
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_access_denied_403_with_zero_writes() -> None:
    repo = _make_repo()
    session = _make_session(repo)
    mock_create_blob = AsyncMock()
    mock_create_pr = AsyncMock()

    with (
        patch(
            "app.services.session_commit._verify_user_repo_permission",
            AsyncMock(side_effect=HTTPException(status_code=403, detail="denied")),
        ),
        patch(f"{_GH}.create_blob", mock_create_blob),
        patch(f"{_GH}.create_pull_request", mock_create_pr),
    ):
        with pytest.raises(SessionCommitError) as exc_info:
            await publish_session_patches(
                session=session,
                repo=repo,
                title="Fix",
                body=None,
                current_user=_make_user(),
                db=_make_db(),
                gh_token="tok",
            )

    assert exc_info.value.status_code == 403
    mock_create_blob.assert_not_called()
    mock_create_pr.assert_not_called()


@pytest.mark.asyncio
async def test_write_access_transport_failure_503_with_zero_writes() -> None:
    repo = _make_repo()
    session = _make_session(repo)
    mock_create_blob = AsyncMock()

    with (
        patch(
            "app.services.session_commit._verify_user_repo_permission",
            AsyncMock(side_effect=HTTPException(status_code=502, detail="network")),
        ),
        patch(f"{_GH}.create_blob", mock_create_blob),
    ):
        with pytest.raises(SessionCommitError) as exc_info:
            await publish_session_patches(
                session=session,
                repo=repo,
                title="Fix",
                body=None,
                current_user=_make_user(),
                db=_make_db(),
                gh_token="tok",
            )

    assert exc_info.value.status_code == 503
    mock_create_blob.assert_not_called()


# ---------------------------------------------------------------------------
# Staged patch path trust boundary
# ---------------------------------------------------------------------------


_BAD_PATHS = [
    "../etc/passwd",
    "/abs.py",
    "C:\\x.py",
    "C:/x.py",
    "a\x00b.py",
    " x.py",
    "x.py ",
    "foo/./bar.py",
    "foo//bar.py",
    "./app/utils.py",
    ".",
    "",
    "dir/",
    "x.py/",
    "foo\\bar.py",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_path", _BAD_PATHS)
async def test_unsafe_staged_paths_rejected_before_any_github_call(
    bad_path: str,
) -> None:
    """Catches normalized DB paths reaching contents/blob/tree/PR API calls."""
    repo = _make_repo()
    session = _make_session(repo, staged_patches={bad_path: _SAMPLE_PATCH})
    mock_fetch = AsyncMock()
    mock_create_blob = AsyncMock()
    mock_create_pr = AsyncMock()

    with (
        patch("app.services.session_commit._verify_user_repo_permission", AsyncMock()) as perms,
        patch(f"{_GH}.fetch_file_content", mock_fetch),
        patch(f"{_GH}.create_blob", mock_create_blob),
        patch(f"{_GH}.create_pull_request", mock_create_pr),
    ):
        with pytest.raises(SessionCommitError) as exc_info:
            await publish_session_patches(
                session=session,
                repo=repo,
                title="Fix",
                body=None,
                current_user=_make_user(),
                db=_make_db(),
                gh_token="tok",
            )

    assert exc_info.value.status_code == 422
    perms.assert_not_called()
    mock_fetch.assert_not_called()
    mock_create_blob.assert_not_called()
    mock_create_pr.assert_not_called()


@pytest.mark.asyncio
async def test_duplicate_paths_after_normalization_rejected() -> None:
    """Catches ``x`` and ``./x`` collapsing into one tree entry overwriting another."""
    repo = _make_repo()
    session = _make_session(
        repo,
        staged_patches={
            "app/utils.py": _SAMPLE_PATCH,
            "./app/utils.py": _SAMPLE_PATCH,
        },
    )
    mock_create_blob = AsyncMock()

    with (
        patch("app.services.session_commit._verify_user_repo_permission", AsyncMock()) as perms,
        patch(f"{_GH}.create_blob", mock_create_blob),
    ):
        with pytest.raises(SessionCommitError) as exc_info:
            await publish_session_patches(
                session=session,
                repo=repo,
                title="Fix",
                body=None,
                current_user=_make_user(),
                db=_make_db(),
                gh_token="tok",
            )

    assert exc_info.value.status_code == 422
    perms.assert_not_called()
    mock_create_blob.assert_not_called()


@pytest.mark.asyncio
async def test_canonical_staged_path_is_used_verbatim() -> None:
    """Catches a normalizer that rewrites a legitimate key and writes elsewhere."""
    repo = _make_repo()
    canonical_path = "app/space name.py"
    session = _make_session(
        repo,
        staged_patches={canonical_path: _SAMPLE_PATCH},
    )
    mock_fetch = AsyncMock(return_value="old\n")

    with (
        ExitStack() as stack,
        patch("app.services.session_commit._verify_user_repo_permission", AsyncMock()),
    ):
        _install_pipeline(
            stack,
            get_ref=AsyncMock(return_value="origsha"),
            create_branch=AsyncMock(),
            update_ref=AsyncMock(),
            create_pr=AsyncMock(
                return_value={"html_url": "https://github.com/o/r/pull/13", "number": 13}
            ),
        )
        stack.enter_context(patch(f"{_GH}.fetch_file_content", mock_fetch))
        await publish_session_patches(
            session=session,
            repo=repo,
            title="Fix",
            body=None,
            current_user=_make_user(),
            db=_make_db(),
            gh_token="tok",
        )

    assert mock_fetch.await_args.kwargs["path"] == canonical_path


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_creates_branch_in_order_and_persists_result() -> None:
    repo = _make_repo(default_branch="main")
    session = _make_session(repo, branch_name="feat/session-branch")
    db = _make_db()
    calls: list[str] = []

    async def _get_ref(**kwargs):
        calls.append("get_ref")
        raise GitHubResourceNotFoundError("missing")

    async def _create_branch(**kwargs):
        calls.append("create_ref")

    async def _blob(**kwargs):
        calls.append("blob")
        return "blobsha"

    async def _tree_lookup(**kwargs):
        calls.append("tree_lookup")
        return "basetree"

    async def _tree(**kwargs):
        calls.append("tree")
        return "newtree"

    async def _commit(**kwargs):
        calls.append("commit")
        return "commit1"

    async def _pr(**kwargs):
        calls.append("pr")
        return {"html_url": "https://github.com/o/r/pull/9", "number": 9}

    with (
        ExitStack() as stack,
        patch("app.services.session_commit._verify_user_repo_permission", AsyncMock()) as perms,
    ):
        stack.enter_context(patch(f"{_GH}.fetch_file_content", AsyncMock(return_value="old\n")))
        stack.enter_context(
            patch(
                "app.services.session_commit.patch_applier.apply_unified_diff",
                return_value="new\n",
            )
        )
        stack.enter_context(patch(f"{_GH}.create_blob", AsyncMock(side_effect=_blob)))
        stack.enter_context(
            patch(f"{_GH}.fetch_commit_tree_sha", AsyncMock(side_effect=_tree_lookup))
        )
        stack.enter_context(patch(f"{_GH}.create_git_tree", AsyncMock(side_effect=_tree)))
        stack.enter_context(patch(f"{_GH}.create_git_commit", AsyncMock(side_effect=_commit)))
        stack.enter_context(patch(f"{_GP}.get_branch_ref", AsyncMock(side_effect=_get_ref)))
        stack.enter_context(
            patch(f"{_GP}.create_branch", AsyncMock(side_effect=_create_branch))
        )
        stack.enter_context(patch(f"{_GP}.update_branch_ref", AsyncMock()))
        stack.enter_context(patch(f"{_GH}.create_pull_request", AsyncMock(side_effect=_pr)))

        result = await publish_session_patches(
            session=session,
            repo=repo,
            title="Fix session publish",
            body="Body",
            current_user=_make_user(),
            db=db,
            gh_token="tok",
        )

    perms.assert_awaited_once()
    assert calls == [
        "blob",
        "tree_lookup",
        "tree",
        "commit",
        "get_ref",
        "create_ref",
        "pr",
    ]
    assert result.pr_url == "https://github.com/o/r/pull/9"
    assert result.pr_number == 9
    assert result.commit_sha == "commit1"
    assert result.branch_name == "feat/session-branch"
    assert result.history_entry["pr_url"] == "https://github.com/o/r/pull/9"
    assert session.status == "completed"
    assert session.conversation_history[-1] is result.history_entry
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_tool_path_does_not_write_history_inside_publisher() -> None:
    """Catches two writers: the agent tool lets _persist own history ordering.

    With ``persist_history=False`` the publisher still marks the row completed
    and returns the entry, but must not append it to conversation_history.

    Counterpart test_pr_entry_is_appended_after_turn_entries_exactly_once
    proves ordering given the completed state produced here.
    """
    repo = _make_repo()
    session = _make_session(repo, branch_name="feat/history")
    session.conversation_history = [{"role": "user", "content": "pre-turn"}]

    with (
        ExitStack() as stack,
        patch("app.services.session_commit._verify_user_repo_permission", AsyncMock()),
    ):
        _install_pipeline(
            stack,
            get_ref=AsyncMock(return_value="origsha"),
            create_branch=AsyncMock(),
            update_ref=AsyncMock(),
            create_pr=AsyncMock(
                return_value={"html_url": "https://github.com/o/r/pull/12", "number": 12}
            ),
        )
        result = await publish_session_patches(
            session=session,
            repo=repo,
            title="Fix",
            body=None,
            current_user=_make_user(),
            db=_make_db(),
            gh_token="tok",
            persist_history=False,
        )

    assert session.status == "completed"
    assert session.conversation_history == [{"role": "user", "content": "pre-turn"}]
    assert result.history_entry["pr_url"] == "https://github.com/o/r/pull/12"


@pytest.mark.asyncio
async def test_publish_updates_existing_branch_without_force() -> None:
    repo = _make_repo()
    session = _make_session(repo, branch_name="feat/existing")
    mock_update_ref = AsyncMock()
    mock_create_branch = AsyncMock()
    mock_create_pr = AsyncMock(
        return_value={"html_url": "https://github.com/o/r/pull/10", "number": 10}
    )

    with (
        ExitStack() as stack,
        patch("app.services.session_commit._verify_user_repo_permission", AsyncMock()),
    ):
        _install_pipeline(
            stack,
            get_ref=AsyncMock(return_value="origsha"),
            create_branch=mock_create_branch,
            update_ref=mock_update_ref,
            create_pr=mock_create_pr,
        )
        await publish_session_patches(
            session=session,
            repo=repo,
            title="Fix",
            body=None,
            current_user=_make_user(),
            db=_make_db(),
            gh_token="tok",
        )

    mock_create_branch.assert_not_called()
    mock_update_ref.assert_awaited_once_with(
        owner=_OWNER,
        repo=_REPO,
        branch="feat/existing",
        sha="commit1",
        token="tok",
        force=False,
    )


# ---------------------------------------------------------------------------
# PR failure handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pr_already_exists_does_not_delete_or_rewind_branch() -> None:
    repo = _make_repo()
    session = _make_session(repo, branch_name="haunter/session-deadbeef")
    mock_get_ref = AsyncMock(side_effect=GitHubResourceNotFoundError("missing"))
    mock_create_branch = AsyncMock()
    mock_delete_ref = AsyncMock()
    mock_restore_ref = AsyncMock()
    mock_create_pr = AsyncMock(
        side_effect=GitHubClientError(
            "GitHub API returned error 422: A pull request already exists for "
            "test-owner:haunter/session-deadbeef"
        )
    )

    with (
        ExitStack() as stack,
        patch("app.services.session_commit._verify_user_repo_permission", AsyncMock()),
    ):
        _install_pipeline(
            stack,
            get_ref=mock_get_ref,
            create_branch=mock_create_branch,
            update_ref=AsyncMock(),
            create_pr=mock_create_pr,
            delete_ref=mock_delete_ref,
            restore_ref=mock_restore_ref,
        )
        with pytest.raises(SessionCommitError) as exc_info:
            await publish_session_patches(
                session=session,
                repo=repo,
                title="Fix",
                body=None,
                current_user=_make_user(),
                db=_make_db(),
                gh_token="tok",
            )

    assert exc_info.value.status_code == 409
    assert "already exists" in str(exc_info.value).lower()
    # Only the pre-write ref lookup happened; no rollback lookup/delete/rewind.
    assert mock_get_ref.await_count == 1
    mock_delete_ref.assert_not_called()
    mock_restore_ref.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_text",
    [
        "GitHub API returned error 422: Validation Failed: base branch already exists",
        "GitHub API returned error 422: pull request body is invalid",
        "GitHub API returned error 422: A pull request is not mergeable",
    ],
)
async def test_unrelated_422_still_rolls_back_branch(error_text: str) -> None:
    """Catches the over-broad matcher that treats any 422/"already exists" as a live PR.

    Only GitHub's exact "A pull request already exists for <head>" means the
    branch belongs to a real PR; everything else must delete the ref it made.
    """
    repo = _make_repo()
    session = _make_session(repo, branch_name="haunter/session-new")
    mock_get_ref = AsyncMock(
        side_effect=[GitHubResourceNotFoundError("missing"), "commit1"]
    )
    mock_create_branch = AsyncMock()
    mock_delete_ref = AsyncMock()
    mock_restore_ref = AsyncMock()

    with (
        ExitStack() as stack,
        patch("app.services.session_commit._verify_user_repo_permission", AsyncMock()),
    ):
        _install_pipeline(
            stack,
            get_ref=mock_get_ref,
            create_branch=mock_create_branch,
            update_ref=AsyncMock(),
            create_pr=AsyncMock(side_effect=GitHubClientError(error_text)),
            delete_ref=mock_delete_ref,
            restore_ref=mock_restore_ref,
        )
        with pytest.raises(SessionCommitError) as exc_info:
            await publish_session_patches(
                session=session,
                repo=repo,
                title="Fix",
                body=None,
                current_user=_make_user(),
                db=_make_db(),
                gh_token="tok",
            )

    assert exc_info.value.status_code == 502
    mock_delete_ref.assert_awaited_once_with(
        owner=_OWNER,
        repo=_REPO,
        branch="haunter/session-new",
        token="tok",
        expected_sha="commit1",
    )
    mock_restore_ref.assert_not_called()


@pytest.mark.asyncio
async def test_pr_already_exists_json_payload_matches_without_rollback() -> None:
    """GitHub's real JSON 422 body (not just the plain phrase) maps to 409."""
    repo = _make_repo()
    session = _make_session(repo, branch_name="haunter/session-deadbeef")
    mock_get_ref = AsyncMock(side_effect=GitHubResourceNotFoundError("missing"))
    mock_delete_ref = AsyncMock()
    mock_create_pr = AsyncMock(
        side_effect=GitHubClientError(
            'GitHub API returned error 422: {"message":"Validation Failed",'
            '"errors":[{"message":"A pull request already exists for '
            'test-owner:haunter/session-deadbeef."}]}'
        )
    )

    with (
        ExitStack() as stack,
        patch("app.services.session_commit._verify_user_repo_permission", AsyncMock()),
    ):
        _install_pipeline(
            stack,
            get_ref=mock_get_ref,
            create_branch=AsyncMock(),
            update_ref=AsyncMock(),
            create_pr=mock_create_pr,
            delete_ref=mock_delete_ref,
        )
        with pytest.raises(SessionCommitError) as exc_info:
            await publish_session_patches(
                session=session,
                repo=repo,
                title="Fix",
                body=None,
                current_user=_make_user(),
                db=_make_db(),
                gh_token="tok",
            )

    assert exc_info.value.status_code == 409
    mock_delete_ref.assert_not_called()


@pytest.mark.asyncio
async def test_pr_failure_deletes_newly_created_branch() -> None:
    repo = _make_repo()
    session = _make_session(repo, branch_name="haunter/session-new")
    mock_get_ref = AsyncMock(
        side_effect=[GitHubResourceNotFoundError("missing"), "commit1"]
    )
    mock_create_branch = AsyncMock()
    mock_delete_ref = AsyncMock()
    mock_restore_ref = AsyncMock()
    mock_create_pr = AsyncMock(side_effect=GitHubClientError("Simulated failure"))

    with (
        ExitStack() as stack,
        patch("app.services.session_commit._verify_user_repo_permission", AsyncMock()),
    ):
        _install_pipeline(
            stack,
            get_ref=mock_get_ref,
            create_branch=mock_create_branch,
            update_ref=AsyncMock(),
            create_pr=mock_create_pr,
            delete_ref=mock_delete_ref,
            restore_ref=mock_restore_ref,
        )
        with pytest.raises(SessionCommitError) as exc_info:
            await publish_session_patches(
                session=session,
                repo=repo,
                title="Fix",
                body=None,
                current_user=_make_user(),
                db=_make_db(),
                gh_token="tok",
            )

    assert exc_info.value.status_code == 502
    mock_create_branch.assert_awaited_once()
    mock_delete_ref.assert_awaited_once_with(
        owner=_OWNER,
        repo=_REPO,
        branch="haunter/session-new",
        token="tok",
        expected_sha="commit1",
    )
    mock_restore_ref.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_malformed_pr_response_rolls_back_new_branch() -> None:
    repo = _make_repo()
    session = _make_session(repo, branch_name="haunter/session-new")
    mock_get_ref = AsyncMock(
        side_effect=[GitHubResourceNotFoundError("missing"), "commit1"]
    )
    mock_delete_ref = AsyncMock()
    mock_restore_ref = AsyncMock()
    mock_create_pr = AsyncMock(return_value={})

    with (
        ExitStack() as stack,
        patch("app.services.session_commit._verify_user_repo_permission", AsyncMock()),
    ):
        _install_pipeline(
            stack,
            get_ref=mock_get_ref,
            create_branch=AsyncMock(),
            update_ref=AsyncMock(),
            create_pr=mock_create_pr,
            delete_ref=mock_delete_ref,
            restore_ref=mock_restore_ref,
        )
        with pytest.raises(SessionCommitError) as exc_info:
            await publish_session_patches(
                session=session,
                repo=repo,
                title="Fix",
                body=None,
                current_user=_make_user(),
                db=_make_db(),
                gh_token="tok",
            )

    assert exc_info.value.status_code == 502
    assert "rolled back" in str(exc_info.value)
    mock_delete_ref.assert_awaited_once()
    mock_restore_ref.assert_not_called()


@pytest.mark.asyncio
async def test_pr_failure_restores_existing_branch_with_expected_sha() -> None:
    repo = _make_repo()
    session = _make_session(repo, branch_name="feat/existing")
    mock_get_ref = AsyncMock(side_effect=["origsha", "commit1"])
    mock_delete_ref = AsyncMock()
    mock_restore_ref = AsyncMock()
    mock_create_pr = AsyncMock(side_effect=GitHubClientError("Simulated failure"))

    with (
        ExitStack() as stack,
        patch("app.services.session_commit._verify_user_repo_permission", AsyncMock()),
    ):
        _install_pipeline(
            stack,
            get_ref=mock_get_ref,
            create_branch=AsyncMock(),
            update_ref=AsyncMock(),
            create_pr=mock_create_pr,
            delete_ref=mock_delete_ref,
            restore_ref=mock_restore_ref,
        )
        with pytest.raises(SessionCommitError) as exc_info:
            await publish_session_patches(
                session=session,
                repo=repo,
                title="Fix",
                body=None,
                current_user=_make_user(),
                db=_make_db(),
                gh_token="tok",
            )

    assert exc_info.value.status_code == 502
    mock_restore_ref.assert_awaited_once_with(
        owner=_OWNER,
        repo=_REPO,
        branch="feat/existing",
        sha="origsha",
        token="tok",
        expected_sha="commit1",
    )
    mock_delete_ref.assert_not_called()


@pytest.mark.asyncio
async def test_pr_failure_skips_rollback_when_writer_advanced_branch() -> None:
    repo = _make_repo()
    session = _make_session(repo, branch_name="feat/existing")
    mock_get_ref = AsyncMock(side_effect=["origsha", "concurrentsha"])
    mock_delete_ref = AsyncMock()
    mock_restore_ref = AsyncMock()
    mock_create_pr = AsyncMock(side_effect=GitHubClientError("Simulated failure"))

    with (
        ExitStack() as stack,
        patch("app.services.session_commit._verify_user_repo_permission", AsyncMock()),
    ):
        _install_pipeline(
            stack,
            get_ref=mock_get_ref,
            create_branch=AsyncMock(),
            update_ref=AsyncMock(),
            create_pr=mock_create_pr,
            delete_ref=mock_delete_ref,
            restore_ref=mock_restore_ref,
        )
        with pytest.raises(SessionCommitError) as exc_info:
            await publish_session_patches(
                session=session,
                repo=repo,
                title="Fix",
                body=None,
                current_user=_make_user(),
                db=_make_db(),
                gh_token="tok",
            )

    assert exc_info.value.status_code == 502
    mock_restore_ref.assert_not_called()
    mock_delete_ref.assert_not_called()


@pytest.mark.asyncio
async def test_db_commit_failure_after_pr_open_leaves_half_completed_publish() -> None:
    """Pins current behavior: if db.commit() raises after the PR is open,
    GitHub already holds a live PR+branch while the row still says active.

    The raw error propagates without the PR URL, so a retry 409s toward a PR
    this same session created. Surfacing the URL or persisting a
    publish_attempt marker needs design sign-off (tracked follow-up).
    """
    repo = _make_repo()
    session = _make_session(repo)
    db = _make_db()
    db.commit = AsyncMock(side_effect=RuntimeError("db down"))
    mock_create_pr = AsyncMock(
        return_value={"html_url": "https://github.com/o/r/pull/7", "number": 7}
    )

    with (
        ExitStack() as stack,
        patch("app.services.session_commit._verify_user_repo_permission", AsyncMock()),
    ):
        _install_pipeline(
            stack,
            get_ref=AsyncMock(side_effect=GitHubResourceNotFoundError("missing")),
            create_branch=AsyncMock(),
            update_ref=AsyncMock(),
            create_pr=mock_create_pr,
        )
        with pytest.raises(RuntimeError, match="db down"):
            await publish_session_patches(
                session=session,
                repo=repo,
                title="Fix",
                body=None,
                current_user=_make_user(),
                db=db,
                gh_token="tok",
            )

    # The PR is live on GitHub even though persistence failed.
    mock_create_pr.assert_awaited_once()


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_installation_token_never_logged_or_returned(caplog) -> None:
    repo = _make_repo()
    session = _make_session(repo, branch_name="feat/secret-check")
    # Intentionally fake ghs-shaped canary: it exercises the real token
    # shape yet cannot collide with a redaction filter's "[REDACTED]" output.
    secret = "ghs_supersecrettoken"
    mock_create_pr = AsyncMock(
        return_value={"html_url": "https://github.com/o/r/pull/11", "number": 11}
    )

    with (
        ExitStack() as stack,
        patch("app.services.session_commit._verify_user_repo_permission", AsyncMock()),
        caplog.at_level("DEBUG", logger="app.services.session_commit"),
    ):
        _install_pipeline(
            stack,
            get_ref=AsyncMock(return_value="origsha"),
            create_branch=AsyncMock(),
            update_ref=AsyncMock(),
            create_pr=mock_create_pr,
        )
        result = await publish_session_patches(
            session=session,
            repo=repo,
            title="Fix",
            body=None,
            current_user=_make_user(),
            db=_make_db(),
            gh_token=secret,
        )

    assert secret not in caplog.text
    assert secret not in repr(result)
    assert secret not in str(result.history_entry)


# ---------------------------------------------------------------------------
# Malformed PR response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_response", [{}, {"html_url": "", "number": 0}])
async def test_malformed_pr_response_rejected_502(bad_response: dict) -> None:
    """A 200 with an unexpected shape must not be recorded as completed."""
    repo = _make_repo()
    session = _make_session(repo)
    db = _make_db()
    mock_create_pr = AsyncMock(return_value=bad_response)

    with (
        ExitStack() as stack,
        patch("app.services.session_commit._verify_user_repo_permission", AsyncMock()),
    ):
        _install_pipeline(
            stack,
            get_ref=AsyncMock(return_value="origsha"),
            create_branch=AsyncMock(),
            update_ref=AsyncMock(),
            create_pr=mock_create_pr,
        )
        with pytest.raises(SessionCommitError) as exc_info:
            await publish_session_patches(
                session=session,
                repo=repo,
                title="Fix",
                body=None,
                current_user=_make_user(),
                db=db,
                gh_token="tok",
            )

    assert exc_info.value.status_code == 502
    assert "unexpected pull request response" in str(exc_info.value).lower()
    assert session.status == "active"
    db.commit.assert_not_awaited()
    assert exc_info.value.status_code == 502
    assert "unexpected pull request response" in str(exc_info.value).lower()
    assert session.status == "active"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_tool_path_publish_leaves_branch_name_untouched() -> None:
    """A mid-pipeline failure must not dirty branch_name for the tool path's later commit."""
    repo = _make_repo()
    session = _make_session(repo, branch_name="feat/original")
    db = _make_db()

    with patch("app.services.session_commit._verify_user_repo_permission", AsyncMock()):
        with patch(
            f"{_GH}.fetch_file_content",
            AsyncMock(side_effect=GitHubClientError("boom")),
        ):
            with pytest.raises(GitHubClientError):
                await publish_session_patches(
                    session=session,
                    repo=repo,
                    title="Fix",
                    body=None,
                    current_user=_make_user(),
                    db=db,
                    target_branch="feat/attempted",
                    persist_history=False,
                    gh_token="tok",
                )

    assert session.branch_name == "feat/original"
    assert session.status == "active"
    db.commit.assert_not_awaited()
    assert session.branch_name == "feat/original"
    assert session.status == "active"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_installation_token_failure_fails_closed_503() -> None:
    """A mint outage must fail closed with zero GitHub calls, never None-token writes."""
    repo = _make_repo()
    session = _make_session(repo)
    db = _make_db()

    targets = [
        f"{_GH}.fetch_file_content",
        f"{_GH}.create_blob",
        f"{_GH}.fetch_commit_tree_sha",
        f"{_GH}.create_git_tree",
        f"{_GH}.create_git_commit",
        f"{_GH}.create_pull_request",
        f"{_GP}.get_branch_ref",
        f"{_GP}.create_branch",
        f"{_GP}.update_branch_ref",
    ]
    with (
        ExitStack() as stack,
        patch("app.services.session_commit._verify_user_repo_permission", AsyncMock()),
    ):
        mocks = [stack.enter_context(patch(t, AsyncMock())) for t in targets]
        stack.enter_context(
            patch(
                "app.services.session_commit.get_installation_token",
                AsyncMock(side_effect=Exception("mint outage")),
            )
        )
        with pytest.raises(SessionCommitError) as exc_info:
            await publish_session_patches(
                session=session,
                repo=repo,
                title="Fix",
                body=None,
                current_user=_make_user(),
                db=db,
            )

    assert exc_info.value.status_code == 503
    assert "installation token" in str(exc_info.value).lower()
    for mock in mocks:
        mock.assert_not_awaited()
    assert session.status == "active"
    db.commit.assert_not_awaited()


_CREATION_EMPTY_PATCH = (
    "--- /dev/null\n"
    "+++ b/pkg/__init__.py\n"
    "@@ -0,0 +0,0 @@\n"
)


@pytest.mark.asyncio
async def test_creation_patch_may_introduce_empty_file() -> None:
    """A creation patch may legitimately produce empty content (empty __init__.py)."""
    repo = _make_repo()
    session = _make_session(repo, staged_patches={"pkg/__init__.py": _CREATION_EMPTY_PATCH})
    db = _make_db()
    mock_blob = AsyncMock(return_value="blobsha")

    async def _get_ref(**kwargs):
        raise GitHubResourceNotFoundError("missing")

    async def _pr(**kwargs):
        return {"html_url": "https://github.com/o/r/pull/9", "number": 9}

    with (
        ExitStack() as stack,
        patch("app.services.session_commit._verify_user_repo_permission", AsyncMock()),
    ):
        stack.enter_context(patch(f"{_GH}.fetch_file_content", AsyncMock(return_value=None)))
        stack.enter_context(
            patch(
                "app.services.session_commit.patch_applier.apply_unified_diff",
                return_value="",
            )
        )
        stack.enter_context(patch(f"{_GH}.create_blob", mock_blob))
        stack.enter_context(
            patch(f"{_GH}.fetch_commit_tree_sha", AsyncMock(return_value="basetree"))
        )
        stack.enter_context(
            patch(f"{_GH}.create_git_tree", AsyncMock(return_value="newtree"))
        )
        stack.enter_context(
            patch(f"{_GH}.create_git_commit", AsyncMock(return_value="commit1"))
        )
        stack.enter_context(patch(f"{_GP}.get_branch_ref", AsyncMock(side_effect=_get_ref)))
        stack.enter_context(patch(f"{_GP}.create_branch", AsyncMock()))
        stack.enter_context(patch(f"{_GP}.update_branch_ref", AsyncMock()))
        stack.enter_context(patch(f"{_GH}.create_pull_request", AsyncMock(side_effect=_pr)))

        result = await publish_session_patches(
            session=session,
            repo=repo,
            title="Add empty init",
            body=None,
            current_user=_make_user(),
            db=db,
            gh_token="tok",
        )

    mock_blob.assert_awaited_once()
    assert mock_blob.await_args.kwargs["content"] == ""
    assert session.status == "completed"
    assert result.pr_number == 9


@pytest.mark.asyncio
async def test_truncation_of_existing_file_to_empty_rejected_422() -> None:
    """Emptying an existing file is still refused even though empty creation is allowed."""
    repo = _make_repo()
    session = _make_session(repo)
    db = _make_db()

    with (
        ExitStack() as stack,
        patch("app.services.session_commit._verify_user_repo_permission", AsyncMock()),
        patch(f"{_GH}.fetch_file_content", AsyncMock(return_value="old\n")),
        patch(
            "app.services.session_commit.patch_applier.apply_unified_diff",
            return_value="",
        ),
        patch(f"{_GH}.create_blob", AsyncMock()) as mock_blob,
    ):
        with pytest.raises(SessionCommitError) as exc_info:
            await publish_session_patches(
                session=session,
                repo=repo,
                title="Truncate",
                body=None,
                current_user=_make_user(),
                db=db,
                gh_token="tok",
            )

    assert exc_info.value.status_code == 422
    assert "truncate" in str(exc_info.value).lower()
    mock_blob.assert_not_awaited()
    assert session.status == "active"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_branch_create_race_recovers_via_update_path() -> None:
    """A 404-then-422 race (concurrent creator) re-reads once and updates."""
    repo = _make_repo()
    session = _make_session(repo, branch_name="haunter/session-race")
    mock_get_ref = AsyncMock(
        side_effect=[GitHubResourceNotFoundError("missing"), "concurrent-sha"]
    )
    mock_create_branch = AsyncMock(
        side_effect=GitHubClientError("Reference already exists")
    )
    mock_update_ref = AsyncMock()

    with (
        ExitStack() as stack,
        patch("app.services.session_commit._verify_user_repo_permission", AsyncMock()),
    ):
        _install_pipeline(
            stack,
            get_ref=mock_get_ref,
            create_branch=mock_create_branch,
            update_ref=mock_update_ref,
            create_pr=AsyncMock(
                return_value={"html_url": "https://github.com/o/r/pull/9", "number": 9}
            ),
        )
        result = await publish_session_patches(
            session=session,
            repo=repo,
            title="Fix",
            body=None,
            current_user=_make_user(),
            db=_make_db(),
            gh_token="tok",
        )

    assert session.status == "completed"
    assert result.pr_number == 9
    assert mock_get_ref.await_count == 2
    mock_update_ref.assert_awaited_once_with(
        owner=_OWNER,
        repo=_REPO,
        branch="haunter/session-race",
        sha="commit1",
        token="tok",
        force=False,
    )


@pytest.mark.asyncio
async def test_branch_create_failure_without_race_raises_502() -> None:
    """A genuine create failure (branch still absent on re-read) stays a 502."""
    repo = _make_repo()
    session = _make_session(repo, branch_name="haunter/session-race")
    mock_get_ref = AsyncMock(side_effect=GitHubResourceNotFoundError("missing"))
    mock_create_branch = AsyncMock(
        side_effect=GitHubClientError("Failed to create ref")
    )
    mock_update_ref = AsyncMock()

    with (
        ExitStack() as stack,
        patch("app.services.session_commit._verify_user_repo_permission", AsyncMock()),
    ):
        _install_pipeline(
            stack,
            get_ref=mock_get_ref,
            create_branch=mock_create_branch,
            update_ref=mock_update_ref,
            create_pr=AsyncMock(),
        )
        with pytest.raises(SessionCommitError) as exc_info:
            await publish_session_patches(
                session=session,
                repo=repo,
                title="Fix",
                body=None,
                current_user=_make_user(),
                db=_make_db(),
                gh_token="tok",
            )

    assert exc_info.value.status_code == 502
    mock_update_ref.assert_not_awaited()
    assert session.status == "active"
