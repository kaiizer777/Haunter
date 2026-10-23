"""
Shared session-commit publisher.

One implementation behind both ``POST /sessions/{id}/commit`` and the
``create_pull_request`` session-agent tool, so the dashboard and the agent
cannot drift on branch safety, write authorization, or patch validation.

Trust boundaries enforced here, before any GitHub write:
  1. Request shape: title/body limits and non-empty staged patches.
  2. Session state: only ``active`` sessions may publish.
  3. Branch safety: normalized once, protected/default branches rejected.
  4. Staged-patch paths: re-validated at commit time. The database is a trust
     boundary; staging-time validation alone is not enough.
  5. Repository write access: verified against the caller's OAuth token.
  6. GitHub installation token: minted only after every guard above passes.

The caller owns the session row lock (the endpoint takes ``FOR UPDATE`` and a
turn holds its own lock). This module never opens a nested transaction lock.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from app import github_client
from app.github import pr as github_pr
from app.github.pr import get_installation_token
from app.repos import _verify_user_repo_permission
from app.schemas import (
    SESSION_PR_BODY_MAX_LEN,
    SESSION_PR_TITLE_MAX_LEN,
    _validate_git_branch,
)
from app.services import patch_applier
from app.services.session_tools.recon import _normalize_rel_path, _validate_file_path

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import AgentSession, Repo, User

logger = logging.getLogger(__name__)

_TITLE_MAX_LEN = SESSION_PR_TITLE_MAX_LEN
_BODY_MAX_LEN = SESSION_PR_BODY_MAX_LEN


class SessionCommitError(Exception):
    """Expected publish failure carrying the HTTP status the API should return."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class SessionCommitResult:
    """Outcome of a successful publish, including the history entry to persist."""

    pr_url: str
    pr_number: int
    commit_sha: str
    branch_name: str
    history_entry: dict[str, Any]


def _validate_title_and_body(
    title: str, body: str | None
) -> tuple[str, str | None]:
    """Mirror ``SessionCommitIn`` limits for callers that bypass Pydantic (tools)."""
    if not isinstance(title, str):
        raise SessionCommitError("Pull request title must be a string.", 400)
    clean_title = title.strip()
    if not clean_title:
        raise SessionCommitError("Pull request title must not be empty.", 400)
    if len(clean_title) > _TITLE_MAX_LEN:
        raise SessionCommitError(
            f"Pull request title must be {_TITLE_MAX_LEN} characters or fewer.", 400
        )
    if body is not None:
        if not isinstance(body, str):
            raise SessionCommitError("Pull request body must be a string.", 400)
        if len(body) > _BODY_MAX_LEN:
            raise SessionCommitError(
                f"Pull request body must be {_BODY_MAX_LEN} characters or fewer.", 400
            )
    return clean_title, body


def _validated_staged_paths(staged_patches: Any) -> dict[str, str]:
    """
    Re-validate every staged path against the same rules the tools use.

    Returns the mapping keyed by the exact staged relative paths. A key must
    already be canonical: :func:`_normalize_rel_path` may not change it. This
    prevents a commit from writing to a different path than the one the user
    reviewed. Rejects the whole commit when any path is empty, ``.``, absolute,
    drive-qualified, traverses (``..``), contains a backslash, NUL/control
    characters, a trailing slash, an empty path segment, or collides after
    normalization.
    """
    if not isinstance(staged_patches, dict):
        raise SessionCommitError("Staged patches are corrupted; cannot publish.", 422)

    normalized: dict[str, str] = {}
    offenders: list[str] = []

    for raw_path, patch_text in staged_patches.items():
        if not isinstance(raw_path, str) or not isinstance(patch_text, str):
            offenders.append(repr(raw_path))
            continue
        try:
            safe_path = _validated_staged_path(raw_path)
        except ValueError:
            offenders.append(raw_path)
            continue
        if safe_path in normalized:
            offenders.append(raw_path)
            continue
        normalized[safe_path] = patch_text

    if offenders:
        listed = ", ".join(repr(path) for path in sorted(offenders)[:10])
        raise SessionCommitError(
            "Refusing to publish staged patches with unsafe or duplicate file "
            f"paths: {listed}",
            422,
        )

    return normalized


def _validated_staged_path(raw_path: str) -> str:
    """Return ``raw_path`` only when it is already the canonical relative path."""
    _validate_file_path(raw_path)
    if not raw_path or raw_path == ".":
        raise ValueError(f"Empty file path rejected: {raw_path!r}")
    if "\\" in raw_path:
        raise ValueError(f"Non-canonical file path rejected: {raw_path!r}")
    if len(raw_path) >= 2 and raw_path[1] == ":" and raw_path[0].isalpha():
        raise ValueError(f"Drive-qualified file path rejected: {raw_path!r}")
    if raw_path.startswith("/") or raw_path.endswith("/"):
        raise ValueError(f"Absolute or directory file path rejected: {raw_path!r}")
    if raw_path != _normalize_rel_path(raw_path):
        raise ValueError(f"Non-canonical file path rejected: {raw_path!r}")
    if any(segment in ("", ".", "..") for segment in raw_path.split("/")):
        raise ValueError(f"Invalid path segment in {raw_path!r}")
    return raw_path


def minted_session_branch(session_id: object) -> str:
    """
    Mint the dedicated ``haunter/session-*`` topic branch for a session.

    Single source of truth for every mint site (session creation, the
    shared publisher fallback, the agent tool path, and the endpoint
    namespace check). Uses 12 hyphen-free hex chars (48 bits) from the
    session id: slicing ``str(uuid)`` directly would embed the
    ``8-4-4-4-12`` hyphen and carry only 11 hex chars, so hyphens are
    stripped first (``UUID.hex`` when available).
    """
    hex_id = getattr(session_id, "hex", None)
    if isinstance(hex_id, str):
        compact = hex_id
    else:
        compact = str(session_id).replace("-", "")
    return f"haunter/session-{compact[:12]}"


def _resolve_publish_branch(
    session: AgentSession, repo: Repo, target_branch: str | None
) -> tuple[str, str]:
    """
    Resolve and validate the publish branch exactly once.

    Returns ``(normalized_branch, default_branch)``. Protected branches and
    exact default-branch equality are rejected before any network call.
    """
    default_branch = (
        (repo.default_branch or "main").strip().removeprefix("refs/heads/") or "main"
    )
    raw_branch = (
        target_branch
        or session.branch_name
        or minted_session_branch(session.id)
    )

    try:
        # First the ref helper's protected-branch contract (its message is part
        # of the endpoint's public error contract).
        github_pr._validate_branch(raw_branch, allow_protected=False)
    except github_pr.GitHubPRValidationError as exc:
        raise SessionCommitError(str(exc), 400) from exc

    try:
        # Then the stricter schema validator: repeated refs/heads/ prefixes,
        # ".." components, trailing dots, and .lock components.
        # Precedence: the ref helper above owns the public error contract, so
        # these stricter git-specific rejections are normalized into its
        # message style (defense in depth, one error dialect).
        branch_name = _validate_git_branch(raw_branch, allow_protected=False)
    except ValueError as exc:
        raise SessionCommitError(
            f"Branch name {raw_branch!r} contains invalid characters. "
            r"Only [a-zA-Z0-9/_\-.] are allowed.",
            400,
        ) from exc
    if not branch_name:
        raise SessionCommitError(
            "A non-empty branch name is required to publish a pull request.", 400
        )

    if branch_name.lower() == default_branch.lower():
        raise SessionCommitError(
            f"Cannot commit directly to default branch '{branch_name}'. "
            "A topic branch must be used.",
            400,
        )

    return branch_name, default_branch


async def _require_write_access(current_user: User | None, repo: Repo) -> None:
    """
    Verify the caller can push to the repository before any Git write.

    Fails closed: denials become 403 and transport/unknown failures become 503.
    """
    if current_user is None:
        raise SessionCommitError(
            "Repository write access is required to publish a pull request.", 403
        )

    try:
        await _verify_user_repo_permission(current_user, repo.owner, repo.name)
    except HTTPException as exc:
        if exc.status_code == 400:
            raise SessionCommitError(str(exc.detail), 400) from exc
        if exc.status_code == 403:
            raise SessionCommitError(
                "Write access to this repository is required to publish a pull request.",
                403,
            ) from exc
        logger.warning(
            "session_commit: write-access check unavailable for %s/%s (status=%d)",
            repo.owner,
            repo.name,
            exc.status_code,
        )
        raise SessionCommitError(
            "Could not verify repository write access with GitHub. Try again later.",
            503,
        ) from exc
    except Exception as exc:
        logger.warning(
            "session_commit: write-access check failed for %s/%s (%s)",
            repo.owner,
            repo.name,
            exc.__class__.__name__,
        )
        raise SessionCommitError(
            "Could not verify repository write access with GitHub. Try again later.",
            503,
        ) from exc


_PR_ALREADY_EXISTS_RE = re.compile(
    r"a pull request already exists for\s+\S+", re.IGNORECASE
)


def _is_pr_already_exists_error(exc: Exception) -> bool:
    """Detect GitHub's 422 "A pull request already exists for owner:branch".

    Only that exact validation message skips rollback. Broader matches (any
    "already exists", or any 422 mentioning pull requests) would strand a
    branch created for a PR that was never opened.
    """
    return _PR_ALREADY_EXISTS_RE.search(str(exc)) is not None


async def publish_session_patches(
    *,
    session: AgentSession,
    repo: Repo,
    title: str,
    body: str | None,
    current_user: User | None,
    db: AsyncSession,
    target_branch: str | None = None,
    gh_token: str | None = None,
    persist_history: bool = True,
) -> SessionCommitResult:
    """
    Commit staged patches to a topic branch and open a pull request.

    The session must already be locked by the caller. Raises
    :class:`SessionCommitError` for every expected failure; unexpected errors
    (DB failures after the PR is opened, etc.) propagate loudly.

    Persistence: on success the session row is marked ``completed``. When
    ``persist_history`` is true (the HTTP endpoint path) the PR entry is also
    appended to ``conversation_history`` in the same transaction. The agent
    tool passes ``persist_history=False`` and lets the orchestrator's
    ``_persist`` append ``result.history_entry`` after the turn's entries, so
    there is exactly one history writer per path.
    """
    staged_patches_raw = session.staged_patches or {}
    if not staged_patches_raw:
        raise SessionCommitError(
            "No staged patches to commit. Stage at least one file patch via the chat agent.",
            422,
        )

    if session.status != "active":
        raise SessionCommitError(
            f"Cannot commit a session with status={session.status!r}. "
            "Only active sessions can be committed.",
            400,
        )

    clean_title, clean_body = _validate_title_and_body(title, body)
    branch_name, default_branch = _resolve_publish_branch(
        session, repo, target_branch
    )
    staged_patches = _validated_staged_paths(staged_patches_raw)

    await _require_write_access(current_user, repo)

    # Token minting is deliberately last: a request rejected for a bad branch,
    # unsafe path, or missing write access must never trigger credential work.
    if gh_token is None:
        try:
            gh_token = await get_installation_token(repo)
        except Exception as exc:
            # Fail closed: a mint outage must never become an unauthenticated
            # run. Proceeding with None would silently fall back to the
            # global PAT on write calls and send `Bearer None` on ref calls.
            logger.error(
                "session_commit: installation token mint failed in session %s: %s",
                session.id,
                exc,
            )
            raise SessionCommitError(
                "Could not mint a GitHub installation token for publishing. "
                "Try again later.",
                503,
            ) from exc

    session_id = session.id

    # ------------------------------------------------------------------
    # Apply patches: fetch base content, apply diff, create blobs.
    # ------------------------------------------------------------------
    tree_entries: list[dict[str, Any]] = []

    for file_path, patch_text in staged_patches.items():
        base_content: str | None = await github_client.fetch_file_content(
            owner=repo.owner,
            repo=repo.name,
            path=file_path,
            sha=session.base_sha,
            token=gh_token,
            allow_global_token=False,
        )
        if base_content is None:
            # A creation patch legitimately has no base content; anything else
            # means the base revision no longer contains the file.
            if "--- /dev/null" not in patch_text:
                logger.error(
                    "session_commit: base content missing for '%s' at sha %s in session %s",
                    file_path,
                    session.base_sha,
                    session_id,
                )
                raise SessionCommitError(
                    f"Base file '{file_path}' not found at revision {session.base_sha}.",
                    422,
                )
            original = ""
        else:
            original = base_content

        try:
            patched_content = patch_applier.apply_unified_diff(original, patch_text)
        except ValueError as exc:
            logger.error(
                "session_commit: patch application failed for %s in session %s: %s",
                file_path,
                session_id,
                exc,
            )
            raise SessionCommitError(
                f"Patch for '{file_path}' could not be applied cleanly: {exc}", 422
            ) from exc

        is_deletion_patch = any(
            line.strip() == "+++ /dev/null" for line in patch_text.splitlines()
        )
        if is_deletion_patch:
            if patched_content:
                logger.error(
                    "session_commit: deletion patch for '%s' in session %s resulted in non-empty content",
                    file_path,
                    session_id,
                )
                raise SessionCommitError(
                    f"Deletion patch for '{file_path}' resulted in non-empty content.",
                    422,
                )
            # Deletion entries omit blob SHA/mode: GitHub deletes the path.
            tree_entries.append(
                {
                    "path": file_path,
                    "type": "blob",
                }
            )
            continue

        # A creation patch may legitimately introduce an empty file; only a
        # truncation of an existing file to empty is rejected.
        is_creation_patch = base_content is None
        if not patched_content and not is_creation_patch:
            logger.error(
                "session_commit: truncating patch empties '%s' in session %s",
                file_path,
                session_id,
            )
            raise SessionCommitError(
                f"Patched content for '{file_path}' is empty. "
                "Existing files cannot be truncated to 0 bytes.",
                422,
            )

        try:
            blob_sha = await github_client.create_blob(
                owner=repo.owner,
                repo=repo.name,
                content=patched_content,
                encoding="utf-8",
                installation_token=gh_token,
                allow_global_token=False,
            )
        except github_client.GitHubClientError as exc:
            logger.error(
                "session_commit: create_blob failed for %s in session %s: %s",
                file_path,
                session_id,
                exc,
            )
            raise SessionCommitError(
                f"Failed to create Git blob for '{file_path}' via GitHub API: {exc}",
                502,
            ) from exc

        tree_entries.append(
            {
                "path": file_path,
                "mode": "100644",
                "type": "blob",
                "sha": blob_sha,
            }
        )

    # ------------------------------------------------------------------
    # Create Git tree (rooted at the tree SHA resolved from base_sha).
    # ------------------------------------------------------------------
    try:
        base_tree_sha = await github_client.fetch_commit_tree_sha(
            owner=repo.owner,
            repo=repo.name,
            commit_sha=session.base_sha,
            installation_token=gh_token,
            allow_global_token=False,
        )
    except github_client.GitHubClientError as exc:
        logger.error(
            "session_commit: could not resolve tree SHA for commit %s in session %s: %s",
            session.base_sha,
            session_id,
            exc,
        )
        raise SessionCommitError(
            "Failed to resolve Git tree SHA for commit "
            f"'{session.base_sha}' via GitHub API: {exc}",
            502,
        ) from exc

    try:
        tree_sha = await github_client.create_git_tree(
            owner=repo.owner,
            repo=repo.name,
            tree=tree_entries,
            base_tree=base_tree_sha,
            installation_token=gh_token,
            allow_global_token=False,
        )
    except github_client.GitHubClientError as exc:
        logger.error(
            "session_commit: create_git_tree failed for session %s: %s",
            session_id,
            exc,
        )
        raise SessionCommitError(
            f"Failed to create Git tree via GitHub API: {exc}", 502
        ) from exc

    # ------------------------------------------------------------------
    # Create Git commit.
    # ------------------------------------------------------------------
    commit_message = clean_title
    if clean_body:
        commit_message = f"{clean_title}\n\n{clean_body}"

    try:
        commit_sha = await github_client.create_git_commit(
            owner=repo.owner,
            repo=repo.name,
            message=commit_message,
            tree_sha=tree_sha,
            parents=[session.base_sha],
            installation_token=gh_token,
            allow_global_token=False,
        )
    except github_client.GitHubClientError as exc:
        logger.error(
            "session_commit: create_git_commit failed for session %s: %s",
            session_id,
            exc,
        )
        raise SessionCommitError(
            f"Failed to create Git commit via GitHub API: {exc}", 502
        ) from exc

    # ------------------------------------------------------------------
    # Update or create the topic branch ref.
    # ------------------------------------------------------------------
    branch_was_created = False
    original_target_sha: str | None = None

    try:
        original_target_sha = await github_pr.get_branch_ref(
            owner=repo.owner,
            repo=repo.name,
            branch=branch_name,
            token=gh_token,
        )
    except github_client.GitHubResourceNotFoundError:
        original_target_sha = None
    except Exception as exc:
        logger.error(
            "session_commit: get_branch_ref failed for session %s branch %s: %s",
            session_id,
            branch_name,
            exc,
        )
        raise SessionCommitError(
            f"Failed to inspect branch ref via GitHub API: {exc}", 502
        ) from exc

    if original_target_sha is None:
        try:
            await github_pr.create_branch(
                owner=repo.owner,
                repo=repo.name,
                branch=branch_name,
                sha=commit_sha,
                token=gh_token,
            )
            branch_was_created = True
        except Exception as exc:
            # A concurrent creator may have won the race between our 404 and
            # this create (422 already-exists). Re-read once; if the branch
            # now resolves, continue down the update path instead of 502.
            reread_sha: str | None = None
            try:
                reread_sha = await github_pr.get_branch_ref(
                    owner=repo.owner,
                    repo=repo.name,
                    branch=branch_name,
                    token=gh_token,
                )
            except Exception:
                reread_sha = None
            if not reread_sha:
                logger.error(
                    "session_commit: create_branch failed for session %s branch %s: %s",
                    session_id,
                    branch_name,
                    exc,
                )
                raise SessionCommitError(
                    f"Failed to create branch ref via GitHub API: {exc}", 502
                ) from exc
            logger.warning(
                "session_commit: branch %s appeared mid-publish for session %s; "
                "taking the update path",
                branch_name,
                session_id,
            )
            original_target_sha = reread_sha
    if not branch_was_created:
        try:
            await github_pr.update_branch_ref(
                owner=repo.owner,
                repo=repo.name,
                branch=branch_name,
                sha=commit_sha,
                token=gh_token,
                force=False,
            )
        except Exception as exc:
            logger.error(
                "session_commit: update_branch_ref failed for session %s branch %s: %s",
                session_id,
                branch_name,
                exc,
            )
            raise SessionCommitError(
                f"Failed to update branch ref via GitHub API: {exc}", 502
            ) from exc

    # ------------------------------------------------------------------
    # Open the pull request against the repository default branch.
    # ------------------------------------------------------------------
    try:
        pr_data: dict[str, Any] = await github_client.create_pull_request(
            owner=repo.owner,
            repo=repo.name,
            title=clean_title,
            head=branch_name,
            base=default_branch,
            body=clean_body,
            installation_token=gh_token,
            allow_global_token=False,
        )
    except Exception as exc:
        logger.error(
            "session_commit: create_pull_request failed for session %s: %s",
            session_id,
            exc,
        )
        # A live PR already exists for this head. Deleting or rewinding the
        # branch here would break that PR, so surface the conflict untouched.
        if _is_pr_already_exists_error(exc):
            raise SessionCommitError(
                f"A pull request already exists for branch '{branch_name}'. "
                "The branch was left unchanged; close or merge the existing PR first.",
                409,
            ) from exc

        # Rollback: ensure no commit is left on a non-PR branch without a PR.
        # Only touch the ref when it still points at this session's commit;
        # a concurrent writer advancing the branch must keep its work.
        try:
            current_ref_sha: str | None = await github_pr.get_branch_ref(
                owner=repo.owner,
                repo=repo.name,
                branch=branch_name,
                token=gh_token,
            )
        except Exception as ref_exc:
            logger.warning(
                "session_commit: failed to inspect current ref for %s during rollback: %s",
                branch_name,
                ref_exc,
            )
            current_ref_sha = None

        if current_ref_sha is not None and current_ref_sha != commit_sha:
            logger.warning(
                "session_commit: skipping branch rollback for %s; ref advanced to %s (expected %s)",
                branch_name,
                current_ref_sha,
                commit_sha,
            )
        elif branch_was_created:
            try:
                await github_pr.delete_branch_ref(
                    owner=repo.owner,
                    repo=repo.name,
                    branch=branch_name,
                    token=gh_token,
                    expected_sha=commit_sha,
                )
            except Exception as del_exc:
                logger.warning(
                    "session_commit: cleanup delete_branch_ref failed for %s: %s",
                    branch_name,
                    del_exc,
                )
        elif original_target_sha is not None:
            try:
                await github_pr.restore_branch_ref(
                    owner=repo.owner,
                    repo=repo.name,
                    branch=branch_name,
                    sha=original_target_sha,
                    token=gh_token,
                    expected_sha=commit_sha,
                )
            except Exception as reset_exc:
                logger.warning(
                    "session_commit: rollback restore_branch_ref failed for %s: %s",
                    branch_name,
                    reset_exc,
                )

        raise SessionCommitError(
            f"Failed to open pull request via GitHub API: {exc}", 502
        ) from exc

    pr_url = pr_data.get("html_url", "") if isinstance(pr_data, dict) else ""
    pr_number = pr_data.get("number", 0) if isinstance(pr_data, dict) else 0

    # A 200 with an unexpected shape ({} or an error envelope) must not be
    # recorded as a completed publish: the PR exists on GitHub but the
    # record is unusable, and the status gate would block any republish.
    if (
        not isinstance(pr_url, str)
        or not pr_url
        or not isinstance(pr_number, int)
        or isinstance(pr_number, bool)
        or pr_number <= 0
    ):
        logger.error(
            "session_commit: unexpected pull request response for session %s: %r",
            session_id,
            pr_data,
        )
        raise SessionCommitError(
            "GitHub returned an unexpected pull request response. "
            "The branch was left unchanged.",
            502,
        )

    # ------------------------------------------------------------------
    # Persist: mark the session completed and record PR metadata.
    # ------------------------------------------------------------------
    # Record the resolved branch only after the PR is open: assigning it
    # before any network call would leak through the tool path's later
    # commit even when publishing fails midway.
    session.branch_name = branch_name
    now = datetime.now(timezone.utc)
    history_entry: dict[str, Any] = {
        "role": "system",
        "content": (
            f"Commit published. PR #{pr_number} opened: {pr_url}\n"
            f"Commit SHA: {commit_sha}"
        ),
        "pr_url": pr_url,
        "pr_number": pr_number,
        "commit_sha": commit_sha,
        "timestamp": now.isoformat(),
    }

    session.status = "completed"
    session.updated_at = now
    if persist_history:
        history = list(session.conversation_history or [])
        history.append(history_entry)
        session.conversation_history = history

    await db.commit()

    logger.info(
        "session_commit: session %s committed -- PR #%s %s commit=%s",
        session_id,
        pr_number,
        pr_url,
        commit_sha,
    )

    return SessionCommitResult(
        pr_url=pr_url,
        pr_number=pr_number,
        commit_sha=commit_sha,
        branch_name=branch_name,
        history_entry=history_entry,
    )
