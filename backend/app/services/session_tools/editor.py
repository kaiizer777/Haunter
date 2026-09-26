"""
Surgical Code Editing Tools for the Haunter session agent.

Eliminates brittle unified-diff line-offset errors by performing exact string
replacements and auto-generating verified unified diffs for Monaco and staged_patches.

Tools:
  - tool_str_replace:       Exact search-and-replace (fails closed on non-unique match).
  - tool_create_file:       Stage a new file from full content.
  - tool_delete_file:       Stage deletion of an existing file.
  - tool_apply_multi_patch: Atomic multi-file editing — validates all ops first,
                            commits only if every op succeeds.
"""

from __future__ import annotations

import difflib
import logging
from typing import Any

from app.github_client import GitHubClientError, fetch_file_content
from app.services.session_streamer import SseQueue
from app.services.session_tools.recon import _validate_file_path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _apply_staged_diff(base_content: str, diff_text: str) -> str:
    """
    Reconstruct the current working buffer by applying a staged unified diff
    on top of the base content.

    Extracts the new-side content from the unified diff by collecting context
    lines (prefixed with ' ') and addition lines (prefixed with '+'), skipping
    the unified diff header lines ('---', '+++', '@@').

    If the diff is empty or malformed, falls back to base_content so the
    downstream occurrence check will catch any real conflicts.
    """
    if not diff_text:
        return base_content

    result_lines: list[str] = []
    in_hunk = False
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("--- ") or line.startswith("+++ "):
            # Unified diff file headers -- skip.
            continue
        if line.startswith("@@ "):
            # Hunk header -- marks start of patch content.
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("+") and not line.startswith("+++ "):
            # Added line -- appears in new file.
            result_lines.append(line[1:])
        elif line.startswith(" "):
            # Context line -- appears in both old and new.
            result_lines.append(line[1:])
        # Lines starting with '-' are deletions -- skip them.

    if not result_lines:
        return base_content

    return "".join(result_lines)


async def _resolve_current_content(
    path: str,
    staged_patches: dict[str, str],
    repo_owner: str,
    repo_name: str,
    base_sha: str,
    gh_token: str | None,
) -> str | None:
    """
    Resolve the current working content of *path*.

    Priority:
      1. If path is in staged_patches — reconstruct from the staged diff.
      2. Otherwise fetch from GitHub at base_sha.

    Returns None if the file does not exist at base and is not staged.
    """
    if path in staged_patches:
        # Fetch base to apply the patch on top of it.
        try:
            base = await fetch_file_content(
                owner=repo_owner,
                repo=repo_name,
                path=path,
                sha=base_sha,
                token=gh_token,
            )
        except GitHubClientError:
            base = None
        base_str = base or ""
        return _apply_staged_diff(base_str, staged_patches[path])

    try:
        content = await fetch_file_content(
            owner=repo_owner, repo=repo_name, path=path, sha=base_sha, token=gh_token
        )
    except GitHubClientError as exc:
        logger.warning("editor: fetch_file_content error for %r: %s", path, exc)
        return None

    return content  # may be None if file doesn't exist


def _make_unified_diff(old_content: str, new_content: str, path: str) -> str:
    """Generate a unified diff string between old and new content for *path*."""
    return "".join(
        difflib.unified_diff(
            old_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


# ---------------------------------------------------------------------------
# Tool: str_replace
# ---------------------------------------------------------------------------


async def tool_str_replace(
    path: str,
    old_str: str,
    new_str: str,
    repo_owner: str,
    repo_name: str,
    base_sha: str,
    staged_patches: dict[str, str],
    queue: SseQueue,
    gh_token: str | None = None,
) -> str:
    """
    Perform an exact, unique string replacement in *path*.

    Validates path and old_str, checks occurrence count (must be exactly 1),
    performs the replacement, generates a verified unified diff, updates
    staged_patches, and emits a file_diff SSE event.

    Returns a human-readable confirmation or a descriptive error string.
    """
    # Validate path.
    try:
        path = _validate_file_path(path)
    except ValueError as exc:
        return f"Error: {exc}"

    # Reject empty old_str — would match every position.
    if not old_str:
        return "Error: old_str must not be empty."

    # Resolve current content.
    content = await _resolve_current_content(
        path=path,
        staged_patches=staged_patches,
        repo_owner=repo_owner,
        repo_name=repo_name,
        base_sha=base_sha,
        gh_token=gh_token,
    )
    if content is None:
        return f"Error: File not found: '{path}'."

    # Occurrence check.
    count = content.count(old_str)
    if count == 0:
        return f"Error: old_str not found in '{path}'."
    if count > 1:
        return (
            f"Error: old_str found {count} times in '{path}'. "
            "Provide more surrounding lines for unique context."
        )

    # Perform replacement.
    new_content = content.replace(old_str, new_str, 1)

    # Generate unified diff.
    diff = _make_unified_diff(content, new_content, path)

    # Update staged patches.
    staged_patches[path] = diff

    # Emit SSE event.
    try:
        await queue.put_file_diff(path=path, diff=diff, action="modify")
    except Exception as exc:
        logger.error(
            "editor: failed to emit file_diff for str_replace on %r: %s", path, exc
        )

    return f"Successfully replaced code in '{path}'."


# ---------------------------------------------------------------------------
# Tool: create_file
# ---------------------------------------------------------------------------


async def tool_create_file(
    path: str,
    content: str,
    staged_patches: dict[str, str],
    queue: SseQueue,
) -> str:
    """
    Stage a new file by generating a unified diff from /dev/null to the new content.

    Returns a confirmation or error string.
    """
    try:
        path = _validate_file_path(path)
    except ValueError as exc:
        return f"Error: {exc}"

    # Diff from empty (creation).
    diff = "".join(
        difflib.unified_diff(
            [],
            content.splitlines(keepends=True),
            fromfile="/dev/null",
            tofile=f"b/{path}",
        )
    )

    staged_patches[path] = diff

    try:
        await queue.put_file_diff(path=path, diff=diff, action="create")
    except Exception as exc:
        logger.error(
            "editor: failed to emit file_diff for create_file on %r: %s", path, exc
        )

    return f"Successfully staged new file '{path}'."


# ---------------------------------------------------------------------------
# Tool: delete_file
# ---------------------------------------------------------------------------


async def tool_delete_file(
    path: str,
    repo_owner: str,
    repo_name: str,
    base_sha: str,
    staged_patches: dict[str, str],
    queue: SseQueue,
    gh_token: str | None = None,
) -> str:
    """
    Stage deletion of an existing file.

    Fetches the current content, generates a deletion diff (file to /dev/null),
    updates staged_patches, and emits a file_diff SSE event.

    Returns a confirmation or error string.
    """
    try:
        path = _validate_file_path(path)
    except ValueError as exc:
        return f"Error: {exc}"

    content = await _resolve_current_content(
        path=path,
        staged_patches=staged_patches,
        repo_owner=repo_owner,
        repo_name=repo_name,
        base_sha=base_sha,
        gh_token=gh_token,
    )
    if content is None:
        return f"Error: File not found: '{path}'."

    diff = "".join(
        difflib.unified_diff(
            content.splitlines(keepends=True),
            [],
            fromfile=f"a/{path}",
            tofile="/dev/null",
        )
    )

    staged_patches[path] = diff

    try:
        await queue.put_file_diff(path=path, diff=diff, action="delete")
    except Exception as exc:
        logger.error(
            "editor: failed to emit file_diff for delete_file on %r: %s", path, exc
        )

    return f"Successfully staged deletion of '{path}'."


# ---------------------------------------------------------------------------
# Tool: apply_multi_patch (atomic)
# ---------------------------------------------------------------------------


async def tool_apply_multi_patch(
    patches: list[dict[str, Any]],
    repo_owner: str,
    repo_name: str,
    base_sha: str,
    staged_patches: dict[str, str],
    queue: SseQueue,
    gh_token: str | None = None,
) -> str:
    """
    Atomically apply a batch of file edits.

    Validates ALL operations first against working copies (using a scratch
    copy of staged_patches). If any validation fails, staged_patches is left
    untouched and a descriptive error is returned.

    For sequential edits on the same file, each edit sees the post-previous-edit
    content. The final committed diff for each file is generated from the original
    base content to the final working content, so it applies cleanly against the
    unmodified file at commit time.

    Supported entry types:
      - {"type": "str_replace", "path": ..., "old_str": ..., "new_str": ...}
      - {"type": "create_file", "path": ..., "content": ...}
      - {"type": "delete_file", "path": ...}

    Returns a confirmation string or an error string detailing which item failed.
    """
    if not patches:
        return "Error: patches list is empty."

    # Work on a scratch copy so we don't mutate staged_patches on failure.
    scratch_patches: dict[str, str] = dict(staged_patches)

    # Track original base content per file (first fetch from GitHub).
    # Used to generate the final base->final diff at commit time.
    base_contents: dict[str, str | None] = {}

    # Track evolving working content per file across sequential edits.
    working_contents: dict[str, str] = {}

    # Accumulate (path, action) for committed items (diff computed after loop).
    committed_paths: list[tuple[str, str]] = []  # (path, action)

    # Validate and dry-run every operation sequentially (order matters for
    # multi-edit on the same file within one batch).
    for idx, entry in enumerate(patches):
        op_type = entry.get("type", "")
        label = f"patch[{idx}] (type={op_type!r})"

        if op_type == "str_replace":
            path = str(entry.get("path", ""))
            old_str = str(entry.get("old_str", ""))
            new_str = str(entry.get("new_str", ""))

            try:
                path = _validate_file_path(path)
            except ValueError as exc:
                return f"Error in {label}: {exc}"

            if not old_str:
                return f"Error in {label}: old_str must not be empty."

            # Fetch base content on first access to this path.
            if path not in base_contents:
                base_contents[path] = await _resolve_current_content(
                    path=path,
                    staged_patches=scratch_patches,
                    repo_owner=repo_owner,
                    repo_name=repo_name,
                    base_sha=base_sha,
                    gh_token=gh_token,
                )

            # Use working content if available (post-previous-edit state),
            # otherwise use the freshly fetched base.
            content = working_contents.get(path, base_contents[path])
            if content is None:
                return f"Error in {label}: File not found: '{path}'."

            count = content.count(old_str)
            if count == 0:
                return f"Error in {label}: old_str not found in '{path}'."
            if count > 1:
                return (
                    f"Error in {label}: old_str found {count} times in '{path}'. "
                    "Provide more surrounding lines for unique context."
                )

            new_content = content.replace(old_str, new_str, 1)
            working_contents[path] = new_content
            if path not in [p for p, _ in committed_paths]:
                committed_paths.append((path, "modify"))

        elif op_type == "create_file":
            path = str(entry.get("path", ""))
            content_str = str(entry.get("content", ""))

            try:
                path = _validate_file_path(path)
            except ValueError as exc:
                return f"Error in {label}: {exc}"

            base_contents[path] = None  # new file -- no base
            working_contents[path] = content_str
            if path not in [p for p, _ in committed_paths]:
                committed_paths.append((path, "create"))
            else:
                # Overwrite existing action with create for this path.
                committed_paths = [
                    (p, "create" if p == path else a) for p, a in committed_paths
                ]

        elif op_type == "delete_file":
            path = str(entry.get("path", ""))

            try:
                path = _validate_file_path(path)
            except ValueError as exc:
                return f"Error in {label}: {exc}"

            if path not in base_contents:
                base_contents[path] = await _resolve_current_content(
                    path=path,
                    staged_patches=scratch_patches,
                    repo_owner=repo_owner,
                    repo_name=repo_name,
                    base_sha=base_sha,
                    gh_token=gh_token,
                )
            if base_contents.get(path) is None and working_contents.get(path) is None:
                return f"Error in {label}: File not found: '{path}'."

            working_contents[path] = ""  # deletion: final content is empty
            if path not in [p for p, _ in committed_paths]:
                committed_paths.append((path, "delete"))
            else:
                committed_paths = [
                    (p, "delete" if p == path else a) for p, a in committed_paths
                ]

        else:
            return (
                f"Error in {label}: Unknown operation type {op_type!r}. "
                "Expected 'str_replace', 'create_file', or 'delete_file'."
            )

    # All validations passed -- build final diffs from base->final and commit.
    # Use an ordered dict to deduplicate paths while preserving first-seen order.
    seen_paths: set[str] = set()
    final_committed: list[tuple[str, str, str]] = []  # (path, diff, action)

    for path, action in committed_paths:
        if path in seen_paths:
            continue
        seen_paths.add(path)

        final_content = working_contents.get(path, "")
        base_content = base_contents.get(path)

        if action == "create":
            diff = "".join(
                difflib.unified_diff(
                    [],
                    final_content.splitlines(keepends=True),
                    fromfile="/dev/null",
                    tofile=f"b/{path}",
                )
            )
        elif action == "delete":
            src = base_content or ""
            diff = "".join(
                difflib.unified_diff(
                    src.splitlines(keepends=True),
                    [],
                    fromfile=f"a/{path}",
                    tofile="/dev/null",
                )
            )
        else:  # modify
            src = base_content or ""
            diff = _make_unified_diff(src, final_content, path)

        final_committed.append((path, diff, action))

    for path, diff, action in final_committed:
        staged_patches[path] = diff
        try:
            await queue.put_file_diff(path=path, diff=diff, action=action)
        except Exception as exc:
            logger.error(
                "editor: failed to emit file_diff for apply_multi_patch on %r: %s",
                path,
                exc,
            )

    n = len(patches)
    return f"Successfully applied {n} file edit{'' if n == 1 else 's'} atomically."


# ---------------------------------------------------------------------------
# Public aliases (consistent with recon.py style)
# ---------------------------------------------------------------------------

str_replace = tool_str_replace
create_file = tool_create_file
delete_file = tool_delete_file
apply_multi_patch = tool_apply_multi_patch
