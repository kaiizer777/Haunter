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
import os
import re
import subprocess
from typing import Any

from app.github_client import GitHubClientError, fetch_file_content
from app.services.session_streamer import SseQueue
from app.services.session_tools.recon import _validate_file_path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sync_to_local_disk(
    path: str,
    content: str | None,
    repo_name: str | None,
    repo_owner: str | None,
    action: str = "write",
) -> None:
    """
    Synchronize staged modifications directly to the local working tree on disk if
    a local checkout exists. Eliminates runner/editor checkout mismatches.
    Enforces strict realpath and directory containment checks against symlink traversal.
    """
    if not repo_name or not repo_name.strip():
        return
    try:
        from app.services.session_tools.sandbox import resolve_repo_dir
        repo_root, err = resolve_repo_dir(repo_name=repo_name, repo_owner=repo_owner)
        if not repo_root or not os.path.isdir(repo_root):
            return

        real_root = os.path.realpath(repo_root)
        target_file = os.path.normpath(os.path.join(real_root, path))
        real_target = os.path.realpath(target_file)
        real_parent = os.path.realpath(os.path.dirname(target_file))

        # Defense-in-depth: Ensure target and its parent do not escape repo boundary via symlinks
        if os.path.commonpath([real_root, real_parent]) != real_root:
            logger.warning("editor: rejected path outside repository root: %r", path)
            return
        if os.path.commonpath([real_root, real_target]) != real_root:
            logger.warning("editor: rejected symlink target outside repository root: %r", path)
            return
        if os.path.islink(target_file):
            logger.warning("editor: rejected write to symlink: %r", path)
            return

        if action == "delete":
            if os.path.isfile(real_target):
                os.remove(real_target)
                logger.info("editor: deleted local file %r", real_target)
        elif action in ("write", "modify", "create") and content is not None:
            os.makedirs(os.path.dirname(target_file), exist_ok=True)
            with open(target_file, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
            logger.info("editor: synced updated content to local file %r", target_file)
    except Exception as exc:
        logger.warning("editor: failed to sync %r to disk: %s", path, exc)


def _invert_unified_diff(diff: str) -> str:
    """
    Invert a unified diff (swap additions and deletions, reverse hunk headers,
    and flip file paths). Applying the inverted diff to the patched content
    reconstructs the original base content.
    """
    if not diff or not diff.strip():
        return diff

    inverted_lines: list[str] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("--- "):
            inverted_lines.append("+++ " + line[4:])
        elif line.startswith("+++ "):
            inverted_lines.append("--- " + line[4:])
        elif line.startswith("@@"):
            m = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$", line)
            if m:
                old_s, old_c, new_s, new_c, rest = m.groups()
                old_part = f"-{new_s}" + (f",{new_c}" if new_c is not None else "")
                new_part = f"+{old_s}" + (f",{old_c}" if old_c is not None else "")
                inverted_lines.append(f"@@ {old_part} {new_part} @@{rest}")
            else:
                inverted_lines.append(line)
        elif line.startswith("-"):
            inverted_lines.append("+" + line[1:])
        elif line.startswith("+"):
            inverted_lines.append("-" + line[1:])
        else:
            inverted_lines.append(line)

    return "".join(inverted_lines)


def _is_diff_applied(
    content: str,
    diff_text: str,
    base_content: str | None = None,
) -> bool:
    """
    Check if a unified diff is already applied to content.
    Eliminates naive substring checks which produce false positives when added lines
    (e.g. 'pass', 'import sys') appear elsewhere in the file.

    If base_content is provided:
      Compares content directly against apply_unified_diff(base_content, diff_text).
    If base_content is not provided:
      Attempts to invert the diff and verify if applying the inverted diff produces
      a candidate base whose diff against content reproduces content.
    """
    if not diff_text or not diff_text.strip():
        return True

    from app.sandbox.mirror import apply_unified_diff

    if base_content is not None:
        expected = apply_unified_diff(base_content, diff_text)
        return content == expected

    try:
        inverted = _invert_unified_diff(diff_text)
        candidate_base = apply_unified_diff(content, inverted)
        if candidate_base != content:
            expected = apply_unified_diff(candidate_base, diff_text)
            return expected == content
    except Exception:
        pass

    return False


async def _get_clean_base(
    path: str,
    repo_owner: str,
    repo_name: str,
    base_sha: str,
    gh_token: str | None,
) -> str | None:
    """
    Retrieve the unmodified original clean base content for path bound to the
    session's base_sha (never local HEAD unless base_sha is unavailable).
    """
    if repo_name and repo_name.strip() and base_sha and base_sha.strip():
        try:
            from app.services.session_tools.sandbox import resolve_repo_dir

            repo_root, _ = resolve_repo_dir(repo_name=repo_name, repo_owner=repo_owner)
            if repo_root and os.path.isdir(repo_root):
                git_path = path.replace("\\", "/")
                res = subprocess.run(
                    ["git", "-C", repo_root, "show", f"{base_sha.strip()}:{git_path}"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if res.returncode == 0:
                    return res.stdout
        except Exception:
            pass

        try:
            return await fetch_file_content(
                owner=repo_owner,
                repo=repo_name,
                path=path,
                sha=base_sha,
                token=gh_token,
            )
        except Exception:
            pass
        return None

    if repo_name and repo_name.strip():
        try:
            from app.services.session_tools.sandbox import resolve_repo_dir

            repo_root, _ = resolve_repo_dir(repo_name=repo_name, repo_owner=repo_owner)
            if repo_root and os.path.isdir(repo_root):
                git_path = path.replace("\\", "/")
                res = subprocess.run(
                    ["git", "-C", repo_root, "show", f"HEAD:{git_path}"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if res.returncode == 0:
                    return res.stdout
        except Exception:
            pass

    if repo_owner and repo_name and base_sha:
        try:
            return await fetch_file_content(
                owner=repo_owner,
                repo=repo_name,
                path=path,
                sha=base_sha,
                token=gh_token,
            )
        except Exception:
            pass

    return None


def _apply_staged_diff(base_content: str, diff_text: str) -> str:
    """
    Reconstruct the current working buffer by applying a staged unified diff
    on top of the base content using the full-file diff applier.
    """
    if not diff_text or not diff_text.strip():
        return base_content
    from app.sandbox.mirror import apply_unified_diff
    return apply_unified_diff(base_content, diff_text)


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
      1. If path is deleted in staged_patches — return None.
      2. If path is newly created in staged_patches:
         - If local checkout disk exists, prioritize actual disk content as ground truth
           and update staged_patches[path] with diff from /dev/null to disk content.
         - Otherwise reconstruct from patch.
      3. If path is in staged_patches and modified:
         - Compute expected patched content from clean_base (bound to base_sha) +
           staged_patches[path].
         - If local disk equals expected, return local disk content.
         - If local disk differs from both clean_base and expected, the file was
           modified via terminal command: prioritize disk as ground truth, refresh
           staged_patches[path] from clean_base to disk content, and return disk.
         - Otherwise return expected (preserving staged patch).
       4. If path is not in staged_patches:
           - Return the local on-disk content when the file exists locally,
             so terminal modifications on unstaged files are preserved as
             ground truth. Fall back to the clean base bound to base_sha,
             then GitHub at base_sha.

    Returns None if the file does not exist at base and is not staged.
    """
    local_base: str | None = None
    if repo_name and repo_name.strip():
        try:
            from app.services.session_tools.sandbox import resolve_repo_dir
            repo_root, _ = resolve_repo_dir(repo_name=repo_name, repo_owner=repo_owner)
            if repo_root:
                real_root = os.path.realpath(repo_root)
                local_path = os.path.normpath(os.path.join(real_root, path))
                real_target = os.path.realpath(local_path)
                real_parent = os.path.realpath(os.path.dirname(local_path))
                if (
                    os.path.commonpath([real_root, real_parent]) == real_root
                    and os.path.commonpath([real_root, real_target]) == real_root
                    and os.path.isfile(real_target)
                    and not os.path.islink(local_path)
                ):
                    with open(real_target, "r", encoding="utf-8", errors="replace") as f:
                        local_base = f.read()
        except Exception:
            local_base = None

    if path in staged_patches:
        diff = staged_patches[path]
        if "+++ /dev/null" in diff:
            return None
        if "--- /dev/null" in diff:
            if local_base is not None:
                staged_patches[path] = "".join(
                    difflib.unified_diff(
                        [],
                        local_base.splitlines(keepends=True),
                        fromfile="/dev/null",
                        tofile=f"b/{path}",
                    )
                )
                return local_base
            from app.sandbox.mirror import apply_unified_diff
            return apply_unified_diff("", diff)

        clean_base = await _get_clean_base(
            path=path,
            repo_owner=repo_owner,
            repo_name=repo_name,
            base_sha=base_sha,
            gh_token=gh_token,
        )

        from app.sandbox.mirror import apply_unified_diff

        if clean_base is not None:
            expected = apply_unified_diff(clean_base, diff)
            if local_base is not None:
                if local_base == expected:
                    return local_base
                if local_base != clean_base and local_base != expected:
                    staged_patches[path] = _make_unified_diff(clean_base, local_base, path)
                    return local_base
            return expected

        if local_base is not None:
            # No clean base available: local disk is the only ground truth.
            # Editor tools synchronize every change to disk, so disk usually
            # already reflects the staged state — reapplying the diff on top
            # of synced content would duplicate lines via anchor matching.
            return local_base
        return None

    if local_base is not None:
        # Terminal and editor share the local checkout: an unstaged file's
        # on-disk content is ground truth (e.g. written via terminal command).
        # Returning the clean base here would silently discard those edits.
        return local_base

    clean_base = await _get_clean_base(
        path=path,
        repo_owner=repo_owner,
        repo_name=repo_name,
        base_sha=base_sha,
        gh_token=gh_token,
    )
    if clean_base is not None:
        return clean_base

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

    # Generate cumulative unified diff from clean base to new_content so staged_patches[path]
    # represents the complete delta from HEAD to current working state.
    if path in staged_patches and "--- /dev/null" in staged_patches[path]:
        diff = "".join(
            difflib.unified_diff(
                [],
                new_content.splitlines(keepends=True),
                fromfile="/dev/null",
                tofile=f"b/{path}",
            )
        )
    else:
        clean_base = await _get_clean_base(
            path=path,
            repo_owner=repo_owner,
            repo_name=repo_name,
            base_sha=base_sha,
            gh_token=gh_token,
        )
        if path in staged_patches and clean_base is None:
            return f"Error: Cannot resolve base revision for '{path}' to build cumulative diff. Staged patch preserved."

        diff = _make_unified_diff(clean_base if clean_base is not None else content, new_content, path)

    # Update staged patches.
    staged_patches[path] = diff

    # Sync to local disk if local checkout exists
    _sync_to_local_disk(path, new_content, repo_name, repo_owner, action="write")

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
    repo_owner: str = "",
    repo_name: str = "",
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

    # Sync to local disk if local checkout exists
    _sync_to_local_disk(path, content, repo_name, repo_owner, action="create")

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

    # Sync to local disk if local checkout exists
    _sync_to_local_disk(path, None, repo_name, repo_owner, action="delete")

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

    # Track initial resolved content per file (to validate operations against).
    initial_contents: dict[str, str | None] = {}

    # Track clean base content per file (to generate final base->final cumulative diff).
    clean_bases: dict[str, str | None] = {}

    # Track files that were already created in staged_patches prior to this batch
    is_created_files: dict[str, bool] = {}

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
            if path not in initial_contents:
                is_created_files[path] = (
                    path in scratch_patches and "--- /dev/null" in scratch_patches[path]
                )
                initial_contents[path] = await _resolve_current_content(
                    path=path,
                    staged_patches=scratch_patches,
                    repo_owner=repo_owner,
                    repo_name=repo_name,
                    base_sha=base_sha,
                    gh_token=gh_token,
                )
                if not is_created_files[path]:
                    clean_bases[path] = await _get_clean_base(
                        path=path,
                        repo_owner=repo_owner,
                        repo_name=repo_name,
                        base_sha=base_sha,
                        gh_token=gh_token,
                    )
                    if path in staged_patches and clean_bases[path] is None:
                        return (
                            f"Error in {label}: Cannot resolve base revision for "
                            f"'{path}' to build cumulative diff. Staged patch preserved."
                        )

            # Use working content if available (post-previous-edit state),
            # otherwise use the freshly fetched base.
            content = working_contents.get(path, initial_contents[path])
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

            initial_contents[path] = None  # new file -- no base
            clean_bases[path] = None
            is_created_files[path] = True
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

            if path not in initial_contents:
                is_created_files[path] = (
                    path in scratch_patches and "--- /dev/null" in scratch_patches[path]
                )
                initial_contents[path] = await _resolve_current_content(
                    path=path,
                    staged_patches=scratch_patches,
                    repo_owner=repo_owner,
                    repo_name=repo_name,
                    base_sha=base_sha,
                    gh_token=gh_token,
                )
                if not is_created_files[path]:
                    clean_bases[path] = await _get_clean_base(
                        path=path,
                        repo_owner=repo_owner,
                        repo_name=repo_name,
                        base_sha=base_sha,
                        gh_token=gh_token,
                    )
                    if path in staged_patches and clean_bases[path] is None:
                        return (
                            f"Error in {label}: Cannot resolve base revision for "
                            f"'{path}' to build cumulative diff. Staged patch preserved."
                        )

            if initial_contents.get(path) is None and working_contents.get(path) is None:
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

        if action == "create" or (action == "modify" and is_created_files.get(path)):
            diff = "".join(
                difflib.unified_diff(
                    [],
                    final_content.splitlines(keepends=True),
                    fromfile="/dev/null",
                    tofile=f"b/{path}",
                )
            )
            final_action = "create" if is_created_files.get(path) else action
        elif action == "delete":
            clean_base = clean_bases.get(path)
            if clean_base is None:
                _initial = initial_contents.get(path)
                clean_base = _initial if _initial is not None else ""
            diff = "".join(
                difflib.unified_diff(
                    clean_base.splitlines(keepends=True),
                    [],
                    fromfile=f"a/{path}",
                    tofile="/dev/null",
                )
            )
            final_action = "delete"
        else:  # modify
            clean_base = clean_bases.get(path)
            if clean_base is None:
                # Deterministic provenance only: never infer the base via
                # inverse-diff application (fuzzy matches hallucinate history).
                # Fall back to the resolved working content as the diff base.
                # Explicit `is not None`: an empty base ("") is a valid base
                # and must not fall through to the already-edited content.
                _initial = initial_contents.get(path)
                clean_base = _initial if _initial is not None else ""
            diff = _make_unified_diff(clean_base, final_content, path)
            final_action = "modify"

        final_committed.append((path, diff, final_action))

    for path, diff, action in final_committed:
        staged_patches[path] = diff
        final_content = working_contents.get(path, "")
        _sync_to_local_disk(
            path,
            final_content if action != "delete" else None,
            repo_name,
            repo_owner,
            action=action,
        )
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
