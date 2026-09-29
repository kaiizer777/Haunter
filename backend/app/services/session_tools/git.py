"""
Git history and provenance tools for the Haunter session agent.

All operations are backed by the GitHub REST/GraphQL API using the session's
installation token — no local git binary needed.

Exposed tools:
- git_log:   List commit history on the session branch (optionally for a file).
- git_blame: Line-level blame annotation for a file at the session SHA.
- git_show:  Fetch full metadata + diff for a single commit SHA.
- git_diff:  Unified diff between two refs (default: base_sha vs HEAD branch).
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Any

from app.github_client import (
    GitHubClientError,
    fetch_blame,
    fetch_commit_metadata,
    fetch_commits,
    fetch_diff,
)
from app.services.session_tools.recon import _validate_file_path

logger = logging.getLogger(__name__)

_MAX_LOG_ENTRIES = 30
_MAX_DIFF_CHARS = 40_000


async def tool_git_log(
    branch: str,
    path: str = "",
    limit: int = 20,
    owner: str = "",
    repo: str = "",
    token: str | None = None,
) -> str:
    """
    List recent commits on a branch, optionally scoped to a file path.

    Returns a formatted string:
      <sha>  <date>  <author>  <message>

    Capped at min(limit, 30) to prevent context bloat.
    """
    limit = max(1, min(limit, _MAX_LOG_ENTRIES))

    if path:
        try:
            path = _validate_file_path(path)
        except ValueError as exc:
            return f"Error: {exc}"

    try:
        commits = await fetch_commits(
            owner=owner,
            repo=repo,
            sha=branch,
            path=path or None,
            per_page=limit,
            token=token,
        )
    except GitHubClientError as exc:
        logger.warning(
            "git_log: GitHub error for %s/%s branch=%s: %s", owner, repo, branch, exc
        )
        return f"Error fetching git log: {exc}"

    if not commits:
        scope = f" for '{path}'" if path else ""
        return f"No commits found on branch '{branch}'{scope}."

    lines: list[str] = []
    for c in commits:
        date_short = c.get("date", "")[:10]
        lines.append(f"{c['sha']}  {date_short}  {c['author']:<20}  {c['message']}")
    return "\n".join(lines)


git_log = tool_git_log


async def tool_git_blame(
    path: str,
    ref: str,
    owner: str = "",
    repo: str = "",
    token: str | None = None,
) -> str:
    """
    Annotate each line range of a file with the commit that last modified it.

    Returns a formatted table:
      lines <start>-<end>  <sha>  <date>  <author>  <message>

    Falls back gracefully if the GraphQL blame API is unavailable.
    """
    try:
        path = _validate_file_path(path)
    except ValueError as exc:
        return f"Error: {exc}"

    ranges = await fetch_blame(
        owner=owner,
        repo=repo,
        path=path,
        ref=ref,
        token=token,
    )

    if not ranges:
        return (
            f"No blame data returned for '{path}' at '{ref}'. "
            "This may indicate the file does not exist at that ref, "
            "or the token lacks the 'repo' OAuth scope required by GitHub GraphQL."
        )

    lines: list[str] = []
    for r in ranges:
        start = r.get("start_line", "?")
        end = r.get("end_line", "?")
        sha = r.get("sha", "?")
        date_short = r.get("date", "")[:10]
        author = (r.get("author") or "unknown")[:18]
        msg = (r.get("message") or "")[:60]
        age = f"({r['age_days']}d ago)" if r.get("age_days") is not None else ""
        lines.append(
            f"lines {start:>4}-{end:<4}  {sha}  {date_short}  {author:<18}  {msg} {age}"
        )
    return "\n".join(lines)


git_blame = tool_git_blame


async def tool_git_show(
    commit_sha: str,
    owner: str = "",
    repo: str = "",
    token: str | None = None,
) -> str:
    """
    Show full metadata and unified diff for a single commit SHA.

    Returns author, date, message, files changed, and the patch.
    Diff is truncated at 40 000 chars to avoid context bloat.
    """
    if not commit_sha or len(commit_sha) < 6:
        return "Error: commit_sha must be at least 6 characters."

    try:
        meta = await fetch_commit_metadata(
            owner=owner,
            repo=repo,
            sha=commit_sha,
            token=token,
        )
    except GitHubClientError as exc:
        return f"Error fetching commit metadata: {exc}"

    commit_data = meta.get("commit", {})
    author_info = commit_data.get("author", {})
    message = commit_data.get("message", "").strip()
    stats = meta.get("stats", {})
    files_changed: list[dict[str, Any]] = meta.get("files", [])

    header_lines = [
        f"commit {meta.get('sha', commit_sha)}",
        f"Author: {author_info.get('name', 'unknown')} <{author_info.get('email', '')}>",
        f"Date:   {author_info.get('date', '')}",
        "",
        f"    {message.replace(chr(10), chr(10) + '    ')}",
        "",
        f"Stats: +{stats.get('additions', 0)} / -{stats.get('deletions', 0)} across {stats.get('total', 0)} change(s)",
        "Files:",
    ]
    for f in files_changed[:20]:
        header_lines.append(
            f"  {f.get('status', '?'):10}  +{f.get('additions', 0):>5} -{f.get('deletions', 0):>5}  {f.get('filename', '')}"
        )
    if len(files_changed) > 20:
        header_lines.append(f"  ... and {len(files_changed) - 20} more files")

    try:
        diff_text = await fetch_diff(
            owner=owner,
            repo=repo,
            sha=commit_sha,
            token=token,
        )
    except GitHubClientError as exc:
        diff_text = f"(diff unavailable: {exc})"

    if len(diff_text) > _MAX_DIFF_CHARS:
        diff_text = (
            diff_text[:_MAX_DIFF_CHARS]
            + f"\n\n[...diff truncated at {_MAX_DIFF_CHARS} chars]"
        )

    return "\n".join(header_lines) + "\n\n" + diff_text


git_show = tool_git_show


async def tool_git_diff(
    base: str,
    head: str,
    owner: str = "",
    repo: str = "",
    token: str | None = None,
    staged_patches: dict[str, str] | None = None,
) -> str:
    """
    Show a unified diff between two refs (branches, tags, or SHAs), or between a ref
    and the local working tree (use head='working' or head='staged').

    base and head can be any ref: branch names, commit SHAs, or tags.
    Set head='working' to inspect uncommitted changes in the local working tree,
    or head='staged' to inspect currently staged patches in the session.
    Diff is truncated at 40 000 chars.
    """
    if not base or not head:
        return "Error: both base and head refs are required."

    if base.strip().startswith("-") or head.strip().startswith("-"):
        bad_ref = base.strip() if base.strip().startswith("-") else head.strip()
        return f"Error: invalid ref '{bad_ref}'."

    head_lower = head.strip().lower()
    base_lower = base.strip().lower()

    # Working tree or staged query
    if head_lower in ("working", "worktree", "working_tree", "staged", "uncommitted") or base_lower in ("working", "worktree", "staged"):
        # 1. If explicit staged requested
        if head_lower in ("staged", "staged_patches") or base_lower in ("staged", "staged_patches"):
            if staged_patches:
                diff_text = "\n\n".join(
                    f"# Staged patch: {p}\n{d.strip()}"
                    for p, d in staged_patches.items()
                    if d and d.strip()
                )
                return diff_text if diff_text else "No staged patches currently in session."
            return "No staged patches currently in session."

        # 2. Check local repo checkout if available
        if repo:
            try:
                from app.services.session_tools.sandbox import resolve_repo_dir
                repo_root, _ = resolve_repo_dir(repo_name=repo, repo_owner=owner)
                if repo_root and os.path.isdir(repo_root):
                    ref_target = "HEAD" if base_lower in ("working", "worktree", "staged") else base
                    if ref_target.startswith("-"):
                        return f"Error: invalid ref '{ref_target}'."
                    cmd = ["git", "-C", repo_root, "diff", "--end-of-options", ref_target]
                    p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                    diff_parts: list[str] = []
                    if p.stdout and p.stdout.strip():
                        diff_parts.append(p.stdout.strip())

                    # Also include untracked newly created files in working-tree diff
                    real_root = os.path.realpath(repo_root)
                    status_cmd = ["git", "-C", repo_root, "status", "--porcelain"]
                    sp = subprocess.run(status_cmd, capture_output=True, text=True, timeout=5)
                    if sp.stdout:
                        for sline in sp.stdout.splitlines():
                            if sline.startswith("?? "):
                                untracked_path = sline[3:].strip()
                                target_file = os.path.normpath(os.path.join(real_root, untracked_path))
                                real_untracked = os.path.realpath(target_file)
                                real_parent = os.path.realpath(os.path.dirname(target_file))
                                if (
                                    os.path.commonpath([real_root, real_parent]) == real_root
                                    and os.path.commonpath([real_root, real_untracked]) == real_root
                                    and os.path.isfile(real_untracked)
                                    and not os.path.islink(target_file)
                                ):
                                    try:
                                        with open(real_untracked, "r", encoding="utf-8", errors="replace") as uf:
                                            u_content = uf.read()
                                        num_lines = len(u_content.splitlines()) or 1
                                        diff_parts.append(
                                            f"--- /dev/null\n+++ b/{untracked_path}\n@@ -0,0 +1,{num_lines} @@\n"
                                            + "".join(f"+{line}\n" for line in u_content.splitlines())
                                        )
                                    except Exception:
                                        pass

                    if diff_parts:
                        diff_text = "\n\n".join(diff_parts)
                        if len(diff_text) > _MAX_DIFF_CHARS:
                            return diff_text[:_MAX_DIFF_CHARS] + f"\n\n[...diff truncated at {_MAX_DIFF_CHARS} chars]"
                        return diff_text
            except Exception as exc:
                logger.debug("git_diff: local git diff failed: %s", exc)

        # 3. Fallback to staged_patches if available
        if staged_patches:
            diff_text = "\n\n".join(
                f"# Staged patch: {p}\n{d.strip()}"
                for p, d in staged_patches.items()
                if d and d.strip()
            )
            if diff_text:
                return diff_text

        return "No uncommitted working-tree differences found."

    if base.strip() == head.strip():
        if staged_patches:
            return (
                f"No differences between ref '{base}' and '{head}'. "
                f"Note: {len(staged_patches)} uncommitted file(s) are currently staged in the session. "
                "Use head='working' or head='staged' to inspect uncommitted working-tree edits."
            )
        return f"No differences between '{base}' and '{head}'."

    try:
        diff_text = await fetch_diff(
            owner=owner,
            repo=repo,
            sha=head,
            base_sha=base,
            token=token,
        )
    except GitHubClientError as exc:
        return f"Error fetching diff between '{base}' and '{head}': {exc}"

    if not diff_text.strip():
        if staged_patches:
            return (
                f"No differences between ref '{base}' and '{head}'. "
                f"Note: {len(staged_patches)} uncommitted file(s) are currently staged in the session. "
                "Use head='working' or head='staged' to inspect uncommitted working-tree edits."
            )
        return f"No differences between '{base}' and '{head}'."

    if len(diff_text) > _MAX_DIFF_CHARS:
        diff_text = (
            diff_text[:_MAX_DIFF_CHARS]
            + f"\n\n[...diff truncated at {_MAX_DIFF_CHARS} chars]"
        )

    return diff_text


git_diff = tool_git_diff
