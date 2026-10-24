"""
Repository recon and surgical navigation tools for the Haunter session agent.

Provides fast, token-efficient repository exploration:
- grep_search: regex and substring search across repository files at base_sha.
- glob_files: pattern-based file discovery.
- read_file_slice: surgical line-range reader with 1-based indexing.
- list_directory: hierarchical directory tree explorer with depth limits.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Any

from app.github_client import (
    GitHubClientError,
    fetch_file_content,
    fetch_git_tree,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path traversal validation
# ---------------------------------------------------------------------------

_SAFE_PATH_RE: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9_./ \-\[\]@+#]+$")
_MAX_PATH_LEN = 500


def _is_diff_header_line(line: str) -> bool:
    """True only for unified-diff file header lines (``--- ``/``+++ ``).

    Hunk content lines may legitimately begin with ``--``/``++`` (e.g. a
    deleted CSS ``--var`` line renders as ``---var``); those must be counted.
    """
    return line.startswith(("--- ", "+++ ")) or line in ("---", "+++")


def _is_staged_deletion(diff: str) -> bool:
    """True if a staged unified diff deletes the file (+++ header targets /dev/null).

    Parses only the ``+++ `` header line — never the patch body — so a modified
    file whose added lines embed example diff text is not misclassified.
    """
    for line in diff.splitlines():
        if line.startswith("+++ "):
            return line[4:].split("\t")[0].strip() == "/dev/null"
    return False


def _is_staged_creation(diff: str) -> bool:
    """True if a staged unified diff creates the file (--- header is /dev/null).

    Parses only the ``--- `` header line — never the patch body.
    """
    for line in diff.splitlines():
        if line.startswith("--- "):
            return line[4:].split("\t")[0].strip() == "/dev/null"
    return False


def _count_diff_lines(diff: str) -> tuple[int, int]:
    """Count (insertions, deletions) in a staged unified diff.

    File headers (``--- ``/``+++ ``) are skipped only before the first hunk
    (``@@ ``). After hunks begin, header-shaped lines are real content (e.g.
    an added ``++ x`` line renders as ``+++ x``) and must be counted.
    """
    ins = 0
    dels = 0
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("@@ "):
            in_hunk = True
            continue
        if line.startswith("diff --git "):
            in_hunk = False
            continue
        if not in_hunk and _is_diff_header_line(line):
            continue
        if line.startswith("+"):
            ins += 1
        elif line.startswith("-"):
            dels += 1
    return ins, dels


def _is_session_scoped_root(root_real: str, session_id: str) -> bool:
    """True when a resolved checkout path is session-isolated for ``session_id``.

    Mirrors the layouts in ``sandbox._resolve_session_root`` without touching
    disk: ``.haunter_sessions/<sid>[/<repo>]``, ``.worktrees/session-<sid>``,
    or ``session-<sid>``. A main-checkout fallback never matches.
    """
    sid = (session_id or "").strip()
    if not sid:
        return False
    cur = os.path.normpath(root_real)
    for _ in range(6):
        base = os.path.basename(cur)
        parent = os.path.dirname(cur)
        if base == f"session-{sid}":
            return True
        if base == sid and os.path.basename(parent) == ".haunter_sessions":
            return True
        if parent == cur:
            break
        cur = parent
    return False


def _validate_file_path(path: str) -> str:
    """
    Validate a file or directory path supplied by the LLM tool call.

    Rejects:
      - Paths containing ".." (directory traversal).
      - Paths starting with "/" (absolute path injection).
      - Paths with characters outside alphanumerics plus _ . / space - [ ] @ + #.
      - Paths exceeding 500 characters.

    Returns the path unchanged if valid.
    Raises ValueError with a descriptive message on violation.
    """
    if len(path) > _MAX_PATH_LEN:
        raise ValueError(
            f"File path exceeds maximum length ({len(path)} > {_MAX_PATH_LEN})"
        )
    if path.startswith("/"):
        raise ValueError(f"Absolute path rejected: {path!r}")
    if ".." in path:
        raise ValueError(f"Directory traversal rejected: {path!r}")
    if not _SAFE_PATH_RE.fullmatch(path):
        raise ValueError(f"Path contains disallowed characters: {path!r}")
    return path


def _normalize_rel_path(path: str) -> str:
    """Normalize a validated relative path: backslashes to slashes, strip one leading ./ and /."""
    norm = path.replace("\\", "/").strip()
    if norm.startswith("./"):
        norm = norm[2:]
    return norm.removeprefix("/")


def _ensure_canonical_staged_path(path: str) -> str:
    """Reject staging paths the publisher would refuse at commit time.

    The publisher requires keys to already be canonical (``_normalize_rel_path``
    must not change them, no empty/``.``/``..`` segments, no trailing slash).
    Failing fast here keeps staging and publish in agreement so a successful
    edit can never block the whole publish.
    """
    if path != _normalize_rel_path(path):
        raise ValueError(f"Non-canonical file path rejected: {path!r}. Use the canonical relative path.")
    if not path or path.startswith("/") or path.endswith("/"):
        raise ValueError(f"Absolute or directory file path rejected: {path!r}")
    if any(segment in ("", ".", "..") for segment in path.split("/")):
        raise ValueError(f"Invalid path segment in {path!r}")
    return path


validate_file_path = _validate_file_path


def _read_clean_base(
    repo_root: str | None, path: str, base_sha: str
) -> str | None:
    """
    Best-effort read of the clean base content for ``path`` at ``base_sha``.

    Uses ``git show base_sha:path`` against the local checkout. Returns None
    when unavailable (no checkout, no base_sha, unknown ref) so callers fall
    back to heuristic handling. Never raises.
    """
    if not repo_root or not base_sha or not base_sha.strip():
        return None
    try:
        git_path = path.replace("\\", "/")
        res = subprocess.run(
            ["git", "-C", repo_root, "show", f"{base_sha.strip()}:{git_path}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0:
            return res.stdout
    except Exception as e:
        logger.debug("recon: clean-base read failed for %s: %s", path, e)
    return None


# ---------------------------------------------------------------------------
# Glob pattern matching helper
# ---------------------------------------------------------------------------


def _glob_to_regex(pat: str) -> re.Pattern[str]:
    """
    Convert a git-style glob pattern to a compiled regex.

    Supports:
      - '**/' matching zero or more directory levels.
      - '*' matching characters within a path segment (excluding '/').
      - '?' matching a single character within a path segment.
      - Standard regex characters safely escaped.
    """
    pat = pat.replace("\\", "/").strip()
    tokens = pat.split("/")
    regex_parts: list[str] = []
    n = len(tokens)
    for i, t in enumerate(tokens):
        if t == "**":
            if i == 0 and n == 1:
                regex_parts.append(".*")
            elif i == 0:
                regex_parts.append("(?:.+/)?")
            elif i == n - 1:
                regex_parts.append("/?.*")
            else:
                regex_parts.append("(?:/.+)?/")
        else:
            seg: list[str] = []
            for ch in t:
                if ch == "*":
                    seg.append("[^/]*")
                elif ch == "?":
                    seg.append("[^/]")
                elif ch in r".+^$()|{}[]\\":
                    seg.append(re.escape(ch))
                else:
                    seg.append(ch)
            regex_parts.append("".join(seg))
            if i < n - 1 and tokens[i + 1] != "**":
                regex_parts.append("/")
    return re.compile("^" + "".join(regex_parts) + "$")


# ---------------------------------------------------------------------------
# Shared Content Resolver (Recon & AST Intelligence)
# ---------------------------------------------------------------------------


async def resolve_read_content(
    path: str,
    *,
    repo_owner: str = "",
    repo_name: str = "",
    base_sha: str = "",
    staged_patches: dict[str, str] | None = None,
    gh_token: str | None = None,
    session_id: str | None = None,
) -> str | None:
    """
    Resolve file content by prioritizing:
    1. Staged deletions -> returns None.
    2. Local workspace checkout on disk (if exists and file present on disk).
    3. Remote repository at base_sha from GitHub API, with staged_patches unified diff
       overlay applied via apply_unified_diff.

    Returns the full string content, or None if the file does not exist or was deleted.
    """
    try:
        path = _validate_file_path(path)
    except ValueError:
        return None

    # 1. Staged deletion check (header-line parse only — patch bodies may embed
    #    example diff text containing "/dev/null" literals).
    if staged_patches and path in staged_patches:
        if _is_staged_deletion(staged_patches[path]):
            return None

    content: str | None = None

    # 2. Check local workspace checkout on disk — but NOT when the file has a
    #    staged patch: the staged patch is authoritative (the disk sync in
    #    _tool_stage_patch swallows sync failures, and the checkout may be a
    #    shared/unsynced copy), so a disk hit here would silently omit staged
    #    changes. Staged files always resolve via base + overlay below.
    has_staged_patch = bool(staged_patches and path in staged_patches)
    if repo_name and repo_name.strip() and not has_staged_patch:
        try:
            from app.services.session_tools.sandbox import resolve_repo_dir

            repo_root, _ = resolve_repo_dir(
                repo_name=repo_name, repo_owner=repo_owner, session_id=session_id
            )
            if repo_root and os.path.isdir(repo_root):
                real_root = os.path.realpath(repo_root)
                if (
                    session_id
                    and session_id.strip()
                    and not _is_session_scoped_root(real_root, session_id)
                ):
                    # Session flow without a session-isolated checkout:
                    # resolve_repo_dir fell back to the main checkout, whose
                    # uncommitted edits may belong to another workflow. Skip
                    # disk and resolve from base_sha plus staged patches below.
                    logger.debug(
                        "recon: ignoring non-session checkout for session %s",
                        session_id,
                    )
                else:
                    local_path = os.path.normpath(os.path.join(real_root, path))
                    real_target = os.path.realpath(local_path)
                    if (
                        os.path.commonpath([real_root, real_target]) == real_root
                        and os.path.isfile(real_target)
                        and not os.path.islink(local_path)
                    ):
                        with open(
                            real_target, "r", encoding="utf-8", errors="replace"
                        ) as f:
                            content = f.read()
        except Exception as exc:
            logger.debug("recon: failed reading file from local checkout: %s", exc)

    # 3. If not on local disk, fetch from GitHub and apply staged diff overlay
    if content is None:
        try:
            content = await fetch_file_content(
                owner=repo_owner,
                repo=repo_name,
                path=path,
                sha=base_sha,
                token=gh_token,
            )
        except GitHubClientError as exc:
            logger.warning("recon: fetch_file_content error for %s: %s", path, exc)
            if not (staged_patches and path in staged_patches):
                return None

        if staged_patches and path in staged_patches:
            from app.sandbox.mirror import apply_unified_diff

            diff = staged_patches[path]
            if content is None and not _is_staged_creation(diff):
                return None
            content = apply_unified_diff(content or "", diff)

    return content


# ---------------------------------------------------------------------------
# Recon Tool Handlers
# ---------------------------------------------------------------------------


async def tool_read_file(
    path: str,
    owner: str = "",
    repo: str = "",
    base_sha: str = "",
    token: str | None = None,
    staged_patches: dict[str, str] | None = None,
    session_id: str | None = None,
    fetcher: Any = None,
    truncate: bool = True,
) -> str:
    """
    Read file contents with support for local checkout synchronization,
    staged patches overlay, symlink containment, and deletion detection.
    """
    try:
        path = _validate_file_path(path)
    except ValueError as exc:
        return f"Error: {exc}"

    # If file is staged for deletion, report deleted
    if staged_patches and path in staged_patches:
        diff = staged_patches[path]
        if _is_staged_deletion(diff):
            return f"File not found: {path!r} (deleted in staged changes)"

    content: str | None = None
    read_from_disk = False
    disk_root: str | None = None

    # 1. Check local checkout if available
    if repo and repo.strip():
        try:
            from app.services.session_tools.sandbox import resolve_repo_dir

            repo_root, _ = resolve_repo_dir(
                repo_name=repo, repo_owner=owner, session_id=session_id
            )
            if repo_root:
                real_root = os.path.realpath(repo_root)
                local_path = os.path.normpath(os.path.join(real_root, path))
                real_target = os.path.realpath(local_path)
                # Symlink / traversal check
                if os.path.commonpath([real_root, real_target]) == real_root:
                    if os.path.isfile(real_target):
                        with open(real_target, "r", encoding="utf-8", errors="replace") as f:
                            content = f.read()
                        read_from_disk = True
                        disk_root = real_root
        except Exception as e:
            logger.debug("Failed reading file from local checkout: %s", e)

    # 2. If not on local disk, fetch from GitHub
    if content is None:
        _fetch = fetcher or fetch_file_content
        try:
            content = await _fetch(
                owner=owner,
                repo=repo,
                path=path,
                sha=base_sha,
                token=token,
            )
        except GitHubClientError as exc:
            logger.warning(
                "recon: read_file GitHub error for path=%s: %s",
                path,
                exc,
            )
            if not (staged_patches and path in staged_patches):
                return f"Error reading file: {exc}"

    # 3. If staged_patches has an entry for path, overlay via apply_unified_diff
    if staged_patches and path in staged_patches:
        from app.sandbox.mirror import apply_unified_diff

        diff = staged_patches[path]
        if content is None and not _is_staged_creation(diff):
            return f"Error reading file: base content for {path!r} is unavailable."
        if read_from_disk and content:
            if _is_staged_creation(diff):
                # Staged creation while the file already exists on disk (synced
                # creation or terminal-created file): disk is authoritative.
                # Overlaying a creation patch here would insert its lines into
                # the existing file instead of returning what the verifier runs.
                pass
            else:
                clean_base = _read_clean_base(disk_root, path, base_sha)
                if clean_base is not None:
                    from app.services.session_tools.editor import _is_diff_applied

                    if content == clean_base:
                        # Disk still holds the clean base (never synced):
                        # overlay the staged modify so reads observe staged content.
                        content = apply_unified_diff(content, diff)
                    elif _is_diff_applied(
                        content, diff, base_content=clean_base
                    ):
                        # Already synchronized: return disk as-is.
                        pass
                    # Else disk diverged from both base and staged result
                    # (terminal/formatter edits): disk is authoritative,
                    # leave content untouched.
                else:
                    from app.services.session_tools.editor import _is_diff_applied
                    if not _is_diff_applied(content, diff):
                        content = apply_unified_diff(content, diff)
        else:
            content = apply_unified_diff(content or "", diff)

    if content is None:
        return f"File not found: {path!r}"

    # Truncate very large files to avoid bloating the context window (unless truncate=False).
    if truncate:
        max_chars = 50_000
        if len(content) > max_chars:
            content = (
                content[:max_chars]
                + f"\n\n[...truncated at {max_chars} chars]"
            )
    return content


read_file = tool_read_file


async def tool_read_file_slice(
    path: str,
    start_line: int,
    end_line: int,
    owner: str = "",
    repo: str = "",
    base_sha: str = "",
    token: str | None = None,
    staged_patches: dict[str, str] | None = None,
    session_id: str | None = None,
) -> str:
    """
    Read a specific line range from a repository file (1-based, inclusive).
    Overlays in-memory staged patches and local disk modifications onto the base commit.

    Returns a line-numbered content string (e.g. '42: def my_func():').
    Raises ValueError on invalid line ranges or path traversal attempts.
    """
    path = _validate_file_path(path)
    if start_line < 1:
        raise ValueError(
            f"Invalid start_line={start_line}. Line numbers are 1-based and must be >= 1."
        )
    if start_line > end_line:
        raise ValueError(
            f"Invalid line range: start_line ({start_line}) cannot exceed end_line ({end_line})."
        )

    # If staged_patches or local disk might exist, obtain content with overlays and NO truncation
    if staged_patches or repo:
        content_res = await tool_read_file(
            path=path,
            owner=owner,
            repo=repo,
            base_sha=base_sha,
            token=token,
            staged_patches=staged_patches,
            session_id=session_id,
            truncate=False,
        )
        if (
            content_res.startswith("File not found: ")
            or content_res.startswith("Error: ")
            or content_res.startswith("Error reading file: ")
        ):
            return content_res
        content: str | None = content_res
    else:
        content = await fetch_file_content(
            owner=owner, repo=repo, path=path, sha=base_sha, token=token
        )
        if content is None:
            return f"File not found: {path!r}"

    lines = content.splitlines()
    total_lines = len(lines)
    if start_line > total_lines:
        raise ValueError(
            f"start_line ({start_line}) exceeds total line count ({total_lines}) of {path!r}."
        )

    effective_end = min(end_line, total_lines)
    sliced = lines[start_line - 1 : effective_end]
    formatted = [f"{start_line + idx}: {line}" for idx, line in enumerate(sliced)]
    return "\n".join(formatted)


read_file_slice = tool_read_file_slice


async def tool_glob_files(
    pattern: str,
    exclude_hidden: bool = True,
    owner: str = "",
    repo: str = "",
    base_sha: str = "",
    staged_patches: dict[str, str] | None = None,
    token: str | None = None,
) -> list[str]:
    """
    Find repository file paths matching a wildcard glob pattern.
    Reflects staged created files and excludes staged deleted files.

    Returns a sorted list of relative file paths.
    Raises ValueError on path traversal attempts.
    """
    if not pattern:
        raise ValueError("Glob pattern cannot be empty.")
    if ".." in pattern:
        raise ValueError(f"Directory traversal rejected in pattern: {pattern!r}")
    if pattern.startswith("/"):
        raise ValueError(f"Absolute path rejected in pattern: {pattern!r}")

    pattern_re = _glob_to_regex(pattern)

    tree_data = await fetch_git_tree(
        owner=owner, repo=repo, tree_sha=base_sha, recursive=True, token=token
    )
    items: list[dict[str, Any]] = tree_data.get("tree", [])

    candidate_paths: set[str] = set()
    for item in items:
        if item.get("type") != "blob":
            continue
        p = item.get("path", "")
        if p:
            candidate_paths.add(p)

    if staged_patches:
        for staged_path, diff in staged_patches.items():
            if _is_staged_deletion(diff):
                candidate_paths.discard(staged_path)
            else:
                candidate_paths.add(staged_path)

    matches: list[str] = []
    for p in candidate_paths:
        if exclude_hidden and any(part.startswith(".") for part in p.split("/")):
            continue
        if pattern_re.match(p):
            matches.append(p)

    matches.sort()
    return matches


glob_files = tool_glob_files


async def tool_grep_search(
    query: str,
    path_prefix: str = "",
    case_sensitive: bool = False,
    max_results: int = 25,
    owner: str = "",
    repo: str = "",
    base_sha: str = "",
    staged_patches: dict[str, str] | None = None,
    token: str | None = None,
    session_id: str | None = None,
) -> str:
    """
    Search matching lines with regex or case-insensitive substring across repository files.
    Overlays staged in-memory modifications and creations, and excludes staged deletions.

    Returns formatted matches: 'file_path:line_number: content' capped at max_results.
    Raises ValueError on path traversal attempts or invalid queries.
    """
    if not query:
        raise ValueError("Search query cannot be empty.")
    if path_prefix:
        path_prefix = _validate_file_path(path_prefix)
        norm_prefix = _normalize_rel_path(path_prefix)
    else:
        norm_prefix = ""

    max_results = max(1, min(max_results, 100))

    tree_data = await fetch_git_tree(
        owner=owner, repo=repo, tree_sha=base_sha, recursive=True, token=token
    )
    items: list[dict[str, Any]] = tree_data.get("tree", [])

    _IGNORE_PREFIXES = (
        ".git/",
        ".github/",
        "node_modules/",
        ".venv/",
        "venv/",
        "__pycache__/",
        "dist/",
        "build/",
        ".pytest_cache/",
        ".mypy_cache/",
    )
    _BINARY_EXTS = (
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".ico",
        ".svg",
        ".pdf",
        ".zip",
        ".tar",
        ".gz",
        ".exe",
        ".bin",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".mp4",
        ".mp3",
        ".wav",
        ".pyc",
        ".so",
        ".dylib",
        ".dll",
        ".wasm",
    )

    candidate_files_set: set[str] = set()
    for item in items:
        if item.get("type") != "blob":
            continue
        p = item.get("path", "")
        if not p:
            continue
        candidate_files_set.add(p)

    if staged_patches:
        for staged_path, diff in staged_patches.items():
            if _is_staged_deletion(diff):
                candidate_files_set.discard(staged_path)
            else:
                candidate_files_set.add(staged_path)

    candidate_files: list[str] = []
    for p in sorted(candidate_files_set):
        if norm_prefix and not p.startswith(norm_prefix):
            continue
        if any(p.startswith(ign) or f"/{ign}" in f"/{p}" for ign in _IGNORE_PREFIXES):
            continue
        if p.lower().endswith(_BINARY_EXTS):
            continue
        candidate_files.append(p)

    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        regex_pattern = re.compile(query, flags)
        use_regex = True
    except re.error:
        use_regex = False
        plain_query = query if case_sensitive else query.lower()

    matches: list[str] = []
    for file_path in candidate_files:
        content = await resolve_read_content(
            path=file_path,
            repo_owner=owner,
            repo_name=repo,
            base_sha=base_sha,
            staged_patches=staged_patches,
            gh_token=token,
            session_id=session_id,
        )
        if not content:
            continue

        for line_num, line in enumerate(content.splitlines(), start=1):
            if use_regex:
                matched = bool(regex_pattern.search(line))
            else:
                target = line if case_sensitive else line.lower()
                matched = plain_query in target

            if matched:
                matches.append(f"{file_path}:{line_num}: {line}")
                if len(matches) >= max_results:
                    break
        if len(matches) >= max_results:
            break

    if not matches:
        return f"No matches found for query: {query!r}"
    return "\n".join(matches)


grep_search = tool_grep_search


async def tool_list_directory(
    path: str = ".",
    depth: int = 2,
    owner: str = "",
    repo: str = "",
    base_sha: str = "",
    staged_patches: dict[str, str] | None = None,
    token: str | None = None,
) -> list[str]:
    """
    Traverse repository directory hierarchy up to specified depth.
    Reflects staged created files and directory hierarchy changes.

    Returns a list of relative directory (with trailing slash) and file paths.
    Raises ValueError on path traversal attempts.
    """
    if not path or path == ".":
        norm_path = ""
    else:
        path = _validate_file_path(path)
        norm_path = _normalize_rel_path(path).strip("/")
        if norm_path == ".":
            norm_path = ""

    depth = max(1, min(depth, 10))

    tree_data = await fetch_git_tree(
        owner=owner, repo=repo, tree_sha=base_sha, recursive=True, token=token
    )
    items: list[dict[str, Any]] = tree_data.get("tree", [])

    all_dirs: set[str] = set()
    all_files: set[str] = set()

    for item in items:
        p = item.get("path", "")
        if not p:
            continue
        itype = item.get("type")
        if itype == "tree":
            all_dirs.add(p)
        elif itype == "blob":
            all_files.add(p)
            parts = p.split("/")
            for k in range(1, len(parts)):
                all_dirs.add("/".join(parts[:k]))

    if staged_patches:
        for staged_path, diff in staged_patches.items():
            if _is_staged_deletion(diff):
                all_files.discard(staged_path)
            else:
                all_files.add(staged_path)
                parts = staged_path.split("/")
                for k in range(1, len(parts)):
                    all_dirs.add("/".join(parts[:k]))
        # Prune directories left empty after staged deletions: git never tracks
        # empty dirs, so any dir with no remaining file beneath it is stale.
        all_dirs = {
            d
            for d in all_dirs
            if any(f == d or f.startswith(d + "/") for f in all_files)
        }

    result_entries: list[str] = []
    seen: set[str] = set()

    for d in sorted(all_dirs):
        if norm_path:
            if not d.startswith(norm_path + "/"):
                continue
            rel = d[len(norm_path) + 1 :]
        else:
            rel = d
        if not rel:
            continue
        if len(rel.split("/")) <= depth:
            entry = f"{rel}/"
            if entry not in seen:
                seen.add(entry)
                result_entries.append(entry)

    for f in sorted(all_files):
        if norm_path:
            if not f.startswith(norm_path + "/"):
                continue
            rel = f[len(norm_path) + 1 :]
        else:
            rel = f
        if not rel:
            continue
        if len(rel.split("/")) <= depth:
            if rel not in seen:
                seen.add(rel)
                result_entries.append(rel)

    result_entries.sort()
    return result_entries


list_directory = tool_list_directory
