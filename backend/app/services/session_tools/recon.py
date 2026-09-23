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
import re
from typing import Any

from app.github_client import (
    fetch_file_content,
    fetch_git_tree,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path traversal validation
# ---------------------------------------------------------------------------

_SAFE_PATH_RE: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9_./ \-]+$")
_MAX_PATH_LEN = 500


def _validate_file_path(path: str) -> str:
    """
    Validate a file or directory path supplied by the LLM tool call.

    Rejects:
      - Paths containing ".." (directory traversal).
      - Paths starting with "/" (absolute path injection).
      - Paths with characters outside [a-zA-Z0-9_./ -].
      - Paths exceeding 500 characters.

    Returns the path unchanged if valid.
    Raises ValueError with a descriptive message on violation.
    """
    if len(path) > _MAX_PATH_LEN:
        raise ValueError(f"File path exceeds maximum length ({len(path)} > {_MAX_PATH_LEN})")
    if path.startswith("/"):
        raise ValueError(f"Absolute path rejected: {path!r}")
    if ".." in path:
        raise ValueError(f"Directory traversal rejected: {path!r}")
    if not _SAFE_PATH_RE.fullmatch(path):
        raise ValueError(f"Path contains disallowed characters: {path!r}")
    return path


validate_file_path = _validate_file_path


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
# Recon Tool Handlers
# ---------------------------------------------------------------------------

async def tool_read_file_slice(
    path: str,
    start_line: int,
    end_line: int,
    owner: str = "",
    repo: str = "",
    base_sha: str = "",
    token: str | None = None,
) -> str:
    """
    Read a specific line range from a repository file (1-based, inclusive).

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
    token: str | None = None,
) -> list[str]:
    """
    Find repository file paths matching a wildcard glob pattern.

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

    matches: list[str] = []
    for item in items:
        if item.get("type") != "blob":
            continue
        p = item.get("path", "")
        if not p:
            continue
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
    token: str | None = None,
) -> str:
    """
    Search matching lines with regex or case-insensitive substring across repository files.

    Returns formatted matches: 'file_path:line_number: content' capped at max_results.
    Raises ValueError on path traversal attempts or invalid queries.
    """
    if not query:
        raise ValueError("Search query cannot be empty.")
    if path_prefix:
        path_prefix = _validate_file_path(path_prefix)
        norm_prefix = path_prefix.lstrip("./").lstrip("/")
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
        ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg",
        ".pdf", ".zip", ".tar", ".gz", ".exe", ".bin",
        ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mp3",
        ".wav", ".pyc", ".so", ".dylib", ".dll", ".wasm",
    )

    candidate_files: list[str] = []
    for item in items:
        if item.get("type") != "blob":
            continue
        p = item.get("path", "")
        if not p:
            continue
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
        content = await fetch_file_content(
            owner=owner, repo=repo, path=file_path, sha=base_sha, token=token
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
    token: str | None = None,
) -> list[str]:
    """
    Traverse repository directory hierarchy up to specified depth.

    Returns a list of relative directory (with trailing slash) and file paths.
    Raises ValueError on path traversal attempts.
    """
    if not path or path == ".":
        norm_path = ""
    else:
        path = _validate_file_path(path)
        norm_path = path.strip("./").strip("/")

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
