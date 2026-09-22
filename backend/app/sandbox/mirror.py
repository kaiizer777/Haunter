"""
Test-mirror lifecycle for the GitHub Actions sandbox — Phase 2 (github.md §3).

The "test mirror" is a private, Haunter-org-owned repo (e.g. ``haunter-test-xxxxxxxx``)
that exists for the sole purpose of running the user's test suite in isolation
under our control. It is:

  - **Per user** — one test mirror per GitHub user, identified by the first 8
    chars of ``sha256(f"{user_github_id}:haunter-sandbox-v1")``. Stable across
    runs, so subsequent attempts reuse the same repo (cached branches,
    actions minutes on a fresh runner per attempt).
  - **Create-once** — first webhook for a new user creates the mirror; later
    runs idempotently fetch the existing one.
  - **Pushed, not webhook'd** — the mirror has no inbound webhook. Haunter
    polls the check-runs API after each push. This is by design: it avoids
    the Lambda URL being on the test-mirror's allowlist and keeps the data
    flow unidirectional.

Public API:
    get_universal_sandbox_repo(org)            -> str  (org/repo)
    get_or_create_test_mirror(gh, org, [name], *, token) -> str  (org/name)
    push_patch_to_mirror(gh, repo_full, *, branch, patch_text, workflow_filename,
                         workflow_content, commit_message, token, [seed_files]) -> str
    detect_language(file_paths)                -> str  ("py" | "ts")
    test_repo_name(user_github_id)             -> str  (deprecated)
    get_or_create_test_repo(...)               -> str  (deprecated alias)
    push_patch_as_commit(...)                  -> str  (deprecated wrapper)

Security invariants:
  - Tokens are passed in, never logged. Errors from the GitHub API are
    re-raised with ``raise_for_status()``; callers (the runner) are
    responsible for wrapping them with a sanitized failure reason.
  - Patch content is sent through the Git Data API (blobs → trees →
    commits → refs) so multi-file patches commit in a single SHA. We do
    not have a shell here, so we cannot ``git apply`` server-side; the
    unified diff is parsed and reconstructed into per-file new content.
  - Reconstruction is best-effort: full file-replacement diffs round-trip
    perfectly; partial hunk diffs (where only the changed hunks appear)
    produce an incomplete file and the workflow will fail. Fix Generator
    is expected to emit full file replacements; for partials, the
    future-work fallback is to use the Contents API per file or to
    fetch the base file and apply the patch in Python.

MVP scope (per github.md §3.3 + the task brief):
  - Patches with up to 5 files and ≤ 50 KB total are handled by parse +
    reconstruct. Larger patches raise a clear ValueError to surface the
    fallback path as future work — the runner can then choose to apply
    via the Contents API instead.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import re
from typing import Any, Iterable, Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Salt for the per-user test-repo name hash. Bump if the test-repo name
# format ever changes — but be aware that bumping invalidates all existing
# test mirrors and forces re-creation on the next webhook for every user.
_TEST_REPO_NAME_SALT: str = "haunter-sandbox-v1"

# 8 hex chars = 32 bits of entropy. The org is private and membership is
# gated by GitHub, so 32 bits is plenty of separation to avoid collisions
# across the realistic user base (<2^32 users before birthday paradox).
_TEST_REPO_NAME_HASH_LEN: int = 8

# MVP guardrails for push_patch_as_commit. Patches larger than either of
# these should use the future-work Contents-API fallback (see module
# docstring). Surfaced as ValueError so the runner can mark the attempt
# as non-retryable config rather than burning attempts.
#
# Bumped from 5 files / 50 KB -> 20 files / 200 KB in the Phase 2 review
# after looking at the realistic distribution of CI fixes (typical fix
# touches 1-3 files, but a refactor or a config-rollback fix can touch
# 10-15). 200 KB is still small enough for a single round-trip and keeps
# the Git Data API tree payload under 1 MB after the multi-file fan-out.
_MAX_PATCH_FILES: int = 20
_MAX_PATCH_BYTES: int = 200 * 1024

# GitHub REST API base. Hardcoded (not from config) — the API URL is not
# a deployable secret and pinning it here avoids a class of "wrong base
# URL injected via env" bugs that have hit us before.
_GITHUB_API_BASE: str = "https://api.github.com"

# Standard request headers for the GitHub REST API. The token is passed
# in per-call so callers can use distinct tokens (e.g. installation
# token for write, public-read token for read).
_GITHUB_API_ACCEPT: str = "application/vnd.github+json"
_GITHUB_API_VERSION: str = "2022-11-28"


def _auth_headers(token: str) -> dict[str, str]:
    """Build the standard Authorization + Accept headers for GitHub REST."""
    return {
        "Authorization": f"Bearer {token}",
        "Accept": _GITHUB_API_ACCEPT,
        "X-GitHub-Api-Version": _GITHUB_API_VERSION,
    }


# ---------------------------------------------------------------------------
# Public: get_universal_sandbox_repo & test_repo_name
# ---------------------------------------------------------------------------


def get_universal_sandbox_repo(org: str) -> str:
    """Return the full name '{org}/{github_sandbox_repo}' of the universal sandbox mirror."""
    from app.config import settings

    return f"{org}/{settings.github_sandbox_repo}"


def test_repo_name(user_github_id: int) -> str:
    """
    Deprecated: Haunter uses a single universal sandbox mirror repository
    (settings.github_sandbox_repo). Retained for backward compatibility.
    """
    h = hashlib.sha256(
        f"{user_github_id}:{_TEST_REPO_NAME_SALT}".encode("utf-8")
    ).hexdigest()[:_TEST_REPO_NAME_HASH_LEN]
    return f"haunter-test-{h}"


# ---------------------------------------------------------------------------
# Public: detect_language
# ---------------------------------------------------------------------------


def detect_language(file_paths: list[str]) -> str:
    """
    Return the workflow-template language key for the given file list.

    MVP: returns ``"py"`` or ``"ts"`` only (the two workflow templates we
    ship in ``workflow_templates/``). Defaults to ``"py"`` when both are
    present or when the list is empty/None-equivalent — Python is the more
    common case for the repos this is initially validated against.

    Rules:
      - Any ``.ts`` / ``.tsx`` file AND no ``.py`` file → ``"ts"``.
      - Otherwise → ``"py"`` (default).

    TODO(future-work): replace the "any .py file trumps .ts" heuristic
    with a "first primary manifest wins" rule:
      - If ``package.json`` is present and ``pyproject.toml`` is not,
        prefer ``"ts"`` (avoids the false positive where a tooling
        repo has one stray ``.py`` and many ``.ts`` files).
      - If both manifests are present, count file extensions in the
        actual diff and pick the majority.
    Out of scope for Phase 2 — the current heuristic works for every
    repo validated so far, and the fix_generator prompt can be tightened
    to emit a language hint that overrides this fallback.
    """
    has_py = any(f.endswith(".py") for f in (file_paths or []))
    has_ts = any(f.endswith((".ts", ".tsx")) for f in (file_paths or []))
    if has_ts and not has_py:
        return "ts"
    return "py"


# ---------------------------------------------------------------------------
# Public: get_or_create_test_mirror (and deprecated get_or_create_test_repo)
# ---------------------------------------------------------------------------


async def get_or_create_test_mirror(
    gh: httpx.AsyncClient,
    org: str,
    repo_name: Optional[str] = None,
    *,
    token: str,
    fallback_token: Optional[str] = None,
) -> str:
    """
    Verify existence of the universal sandbox mirror repository, creating it once if missing.

    Idempotent. Only verifies existence of ``{org}/{github_sandbox_repo}`` (via ``GET /repos/{org}/{repo}``).
    If missing (404), creates it once with ``private: true``, ``auto_init: true``.

    Handles both GitHub orgs and personal user accounts transparently:
      - Probes ``GET /orgs/{owner}`` to determine account type.
      - Org accounts: ``POST /orgs/{org}/repos`` (requires administration:write
        permission on the App or admin PAT; the App must be installed on the org).
      - Personal accounts: ``POST /user/repos`` (works whenever the token's
        installation covers the target user's repos; no extra org permission needed).

    Falls back to ``fallback_token`` (typically ``settings.github_token``, a
    PAT) on a 403 from the create call.
    """
    from app.config import settings

    target_repo = repo_name or getattr(settings, "github_sandbox_repo", "haunter-sandbox-runner")
    full = f"{org}/{target_repo}"
    headers = _auth_headers(token)

    # 1. Try to fetch the existing repo.
    resp = await gh.get(
        f"{_GITHUB_API_BASE}/repos/{full}",
        headers=headers,
    )
    if resp.status_code == 200:
        logger.info("mirror: universal sandbox mirror exists: %s", full)
        return full
    if resp.status_code != 404:
        # Anything other than "not found" is a real error — surface it.
        resp.raise_for_status()

    # 2. Not found — determine whether ``org`` is a GitHub org or a personal
    #    user account so we call the right creation endpoint.
    org_probe = await gh.get(
        f"{_GITHUB_API_BASE}/orgs/{org}",
        headers=headers,
    )
    is_org = org_probe.status_code == 200

    repo_payload = {
        "name": target_repo,
        "private": True,
        "auto_init": True,
        "description": "Haunter universal sandbox runner — auto-managed, do not edit.",
    }

    if is_org:
        create_url = f"{_GITHUB_API_BASE}/orgs/{org}/repos"
    else:
        create_url = f"{_GITHUB_API_BASE}/user/repos"

    create_resp = await gh.post(create_url, headers=headers, json=repo_payload)

    # If already exists (422 name already exists on this account due to concurrent race)
    if create_resp.status_code == 422:
        resp = await gh.get(f"{_GITHUB_API_BASE}/repos/{full}", headers=headers)
        if resp.status_code == 200:
            logger.info("mirror: universal sandbox mirror created concurrently: %s", full)
            return full

    # On 403, retry once with fallback PAT.
    if create_resp.status_code == 403 and fallback_token and fallback_token != token:
        logger.warning(
            "mirror: repo create 403 with App installation token — "
            "retrying with fallback PAT"
        )
        create_resp = await gh.post(
            create_url,
            headers=_auth_headers(fallback_token),
            json=repo_payload,
        )
        if create_resp.status_code == 422:
            resp = await gh.get(f"{_GITHUB_API_BASE}/repos/{full}", headers=headers)
            if resp.status_code == 200:
                logger.info("mirror: universal sandbox mirror created concurrently: %s", full)
                return full

    create_resp.raise_for_status()
    logger.info(
        "mirror: created universal sandbox mirror: %s (via %s endpoint, token=%s)",
        full,
        "orgs" if is_org else "user",
        "fallback_pat" if (fallback_token and create_resp.request.headers.get("Authorization") == f"Bearer {fallback_token}") else "app_token",
    )
    return full


async def get_or_create_test_repo(
    gh: httpx.AsyncClient,
    org: str,
    repo_name: Optional[str] = None,
    *,
    token: str,
    fallback_token: Optional[str] = None,
) -> str:
    """Deprecated alias for get_or_create_test_mirror."""
    return await get_or_create_test_mirror(
        gh,
        org,
        repo_name=repo_name,
        token=token,
        fallback_token=fallback_token,
    )


# ---------------------------------------------------------------------------
# Public: push_patch_as_commit
# ---------------------------------------------------------------------------


# Captures the file path out of a unified-diff file header.
# Examples that match (with capture group shown in []):
#   "--- a/path/to/file"     -> "path/to/file"
#   "+++ b/path/to/file"     -> "path/to/file"
#   "--- /dev/null"          -> "/dev/null"   (deletion marker)
#   "+++ /dev/null"          -> "/dev/null"   (deletion marker)
#   "--- path/to/file"       -> "path/to/file"  (some diffs omit a/b/)
_FILE_HEADER_RE: re.Pattern[str] = re.compile(r"^(?:---|\+\+\+)\s+(?:[ab]/)?(\S+)")


_HUNK_HEADER_RE: re.Pattern[str] = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@")


def resolve_file_path(target_path: str, existing_paths: Iterable[str]) -> str:
    """
    Resolve a target file path against known repository paths.

    1. If target_path is already in existing_paths, return as-is.
    2. Normalize slashes and strip leading 'a/' or 'b/'.
    3. Try exact suffix match: e.g. target_path == 'analytics.py' matches 'backend/app/core/analytics.py'.
    4. If there is a single unique match, return the resolved full path.
    5. If no match or ambiguous (>1 candidates), return target_path unchanged.
    """
    existing_set = set(existing_paths)
    if target_path in existing_set:
        return target_path

    norm = target_path.replace("\\", "/").strip()
    if norm.startswith("a/") or norm.startswith("b/"):
        norm = norm[2:]
    norm = norm.lstrip("/")

    if norm in existing_set:
        return norm

    # 1. Exact suffix match with leading slash
    suffix_matches = [p for p in existing_paths if p.endswith("/" + norm)]
    if len(suffix_matches) == 1:
        return suffix_matches[0]

    # 2. Basename match if norm has no directory components
    if "/" not in norm:
        base_matches = [p for p in existing_paths if os.path.basename(p) == norm]
        if len(base_matches) == 1:
            return base_matches[0]

    return target_path


def _split_patch_by_file(patch_text: str) -> tuple[dict[str, str], list[str]]:
    """
    Split a multi-file unified diff into {file_path: file_patch_text} and deleted_paths.
    """
    file_patches: dict[str, list[str]] = {}
    deleted_paths: list[str] = []
    current_path: Optional[str] = None
    old_path: Optional[str] = None

    for line in patch_text.splitlines():
        if (line.startswith("---") or line.startswith("+++")) and _FILE_HEADER_RE.match(line):
            m = _FILE_HEADER_RE.match(line)
            assert m is not None
            raw_path = m.group(1).strip()
            if line.startswith("---"):
                old_path = raw_path
                current_path = None
            elif line.startswith("+++"):
                if raw_path == "/dev/null":
                    if old_path and old_path != "/dev/null":
                        deleted_paths.append(old_path)
                    current_path = None
                else:
                    current_path = raw_path
                    if current_path not in file_patches:
                        file_patches[current_path] = []
            continue

        if current_path is not None:
            file_patches[current_path].append(line)

    result = {p: "\n".join(lines) for p, lines in file_patches.items()}
    return result, deleted_paths


def apply_unified_diff(base_content: str, patch_text: str) -> str:
    """
    Apply a unified diff (supports partial hunks) to base_content string.
    If base_content is empty (new file), reconstructs from '+' lines.
    """
    lines = patch_text.splitlines()
    hunks = []
    current_hunk = None

    for line in lines:
        if line.startswith("@@"):
            m = _HUNK_HEADER_RE.match(line)
            if m:
                old_start = int(m.group(1))
                old_len = int(m.group(2)) if m.group(2) is not None else 1
                new_start = int(m.group(3))
                new_len = int(m.group(4)) if m.group(4) is not None else 1
                current_hunk = {
                    "old_start": old_start,
                    "old_len": old_len,
                    "new_start": new_start,
                    "new_len": new_len,
                    "lines": [],
                }
                hunks.append(current_hunk)
                continue
        if current_hunk is not None:
            if line.startswith(("+", "-", " ")) or line == "":
                current_hunk["lines"].append(line)

    if not hunks:
        added = [ln[1:] for ln in lines if ln.startswith("+")]
        res = "\n".join(added) if added else base_content
        if base_content.endswith("\n") and not res.endswith("\n"):
            res += "\n"
        return res

    if not base_content.strip():
        # New file creation
        added = []
        for h in hunks:
            for ln in h["lines"]:
                if ln.startswith("+"):
                    added.append(ln[1:])
                elif ln.startswith(" ") or ln == "":
                    added.append(ln[1:] if ln.startswith(" ") else "")
        return "\n".join(added)

    base_lines = base_content.splitlines()
    offset = 0

    for h in hunks:
        old_lines = []
        new_lines = []
        for ln in h["lines"]:
            if ln.startswith(" "):
                old_lines.append(ln[1:])
                new_lines.append(ln[1:])
            elif ln.startswith("-"):
                old_lines.append(ln[1:])
            elif ln.startswith("+"):
                new_lines.append(ln[1:])
            elif ln == "":
                old_lines.append("")
                new_lines.append("")

        if not old_lines:
            insert_idx = max(0, min(len(base_lines), (h["old_start"] - 1) + offset))
            base_lines[insert_idx:insert_idx] = new_lines
            offset += len(new_lines)
            continue

        expected_idx = (h["old_start"] - 1) + offset
        match_idx = None

        if 0 <= expected_idx <= len(base_lines) - len(old_lines):
            if base_lines[expected_idx : expected_idx + len(old_lines)] == old_lines:
                match_idx = expected_idx

        if match_idx is None:
            for delta in range(1, 51):
                for candidate in (expected_idx - delta, expected_idx + delta):
                    if 0 <= candidate <= len(base_lines) - len(old_lines):
                        if base_lines[candidate : candidate + len(old_lines)] == old_lines:
                            match_idx = candidate
                            break
                if match_idx is not None:
                    break

        if match_idx is None:
            for idx in range(len(base_lines) - len(old_lines) + 1):
                if base_lines[idx : idx + len(old_lines)] == old_lines:
                    match_idx = idx
                    break

        if match_idx is not None:
            base_lines[match_idx : match_idx + len(old_lines)] = new_lines
            offset += len(new_lines) - len(old_lines)
        else:
            clean_old = [ln.strip() for ln in old_lines]
            for idx in range(len(base_lines) - len(old_lines) + 1):
                if [b.strip() for b in base_lines[idx : idx + len(old_lines)]] == clean_old:
                    match_idx = idx
                    base_lines[match_idx : match_idx + len(old_lines)] = new_lines
                    offset += len(new_lines) - len(old_lines)
                    break

        # Tier 3: Targeted remove-line matching (handles omitted/added blank lines in context)
        if match_idx is None:
            to_remove = [ln[1:] for ln in h["lines"] if ln.startswith("-")]
            to_add = [ln[1:] for ln in h["lines"] if ln.startswith("+")]
            if to_remove:
                clean_rem = [ln.strip() for ln in to_remove]
                candidates = [
                    idx for idx in range(len(base_lines) - len(to_remove) + 1)
                    if [b.strip() for b in base_lines[idx : idx + len(to_remove)]] == clean_rem
                ]
                if candidates:
                    best_cand = min(candidates, key=lambda c: abs(c - expected_idx))
                    # Check indentation of target base line
                    target_indent = len(base_lines[best_cand]) - len(base_lines[best_cand].lstrip())
                    if to_add and to_add[0].strip():
                        add_indent = len(to_add[0]) - len(to_add[0].lstrip())
                        indent_diff = target_indent - add_indent
                        if indent_diff != 0:
                            adjusted_add = []
                            for al in to_add:
                                if al.strip():
                                    if indent_diff > 0:
                                        adjusted_add.append(" " * indent_diff + al)
                                    else:
                                        strip_count = min(len(al) - len(al.lstrip()), -indent_diff)
                                        adjusted_add.append(al[strip_count:])
                                else:
                                    adjusted_add.append("")
                            to_add = adjusted_add

                    base_lines[best_cand : best_cand + len(to_remove)] = to_add
                    offset += len(to_add) - len(to_remove)
                    match_idx = best_cand
                    logger.info("mirror: tier-3 targeted match applied at line %d", best_cand + 1)
            elif to_add:
                # Pure insertion without deletion: match non-empty context anchor
                non_empty_ctx = [ln[1:].strip() for ln in h["lines"] if ln.startswith(" ") and ln[1:].strip()]
                if non_empty_ctx:
                    anchor = non_empty_ctx[0]
                    for idx, bl in enumerate(base_lines):
                        if bl.strip() == anchor:
                            insert_at = idx + 1
                            base_lines[insert_at:insert_at] = to_add
                            offset += len(to_add)
                            match_idx = insert_at
                            logger.info("mirror: tier-3 anchor insertion applied at line %d", insert_at + 1)
                            break

        if match_idx is None:
            logger.warning(
                "mirror: hunk starting at line %d could not be matched against file (%d lines)",
                h.get("old_start", 0),
                len(base_lines),
            )


    res = "\n".join(base_lines)
    if base_content.endswith("\n") and not res.endswith("\n"):
        res += "\n"
    return res


def _parse_patch(patch_text: str) -> tuple[dict[str, str], list[str]]:
    """
    Parse a unified diff into (files_dict, deleted_paths).

    files_dict: {path: new_content}
    deleted_paths: list of paths deleted by the patch (marked with +++ /dev/null)
    """
    if not patch_text or not patch_text.strip():
        raise ValueError("push_patch_to_mirror: patch_text is empty")

    files: dict[str, list[str]] = {}
    deleted_paths: list[str] = []
    current_path: Optional[str] = None
    old_path: Optional[str] = None

    for line in patch_text.splitlines():
        # ------------------------------------------------------------
        # File header line (--- a/path or +++ b/path, optional a/b prefix)
        # ------------------------------------------------------------
        if (line.startswith("---") or line.startswith("+++")) and _FILE_HEADER_RE.match(line):
            m = _FILE_HEADER_RE.match(line)
            assert m is not None  # narrowed by the match() guard above
            raw_path = m.group(1).strip()
            if line.startswith("---"):
                old_path = raw_path
            elif line.startswith("+++"):
                if raw_path == "/dev/null":
                    if old_path and old_path != "/dev/null":
                        deleted_paths.append(old_path)
                    current_path = None
                else:
                    current_path = raw_path
            continue

        if current_path is None:
            continue

        # ------------------------------------------------------------
        # Content lines
        # ------------------------------------------------------------
        if line.startswith("@@"):
            # Hunk header — skip.
            continue
        if line.startswith("\\"):
            # "\ No newline at end of file" marker — skip.
            continue
        if line.startswith("-"):
            # Removed line — skip.
            continue
        if line.startswith("+"):
            # Added line — strip the "+" prefix.
            files.setdefault(current_path, []).append(line[1:])
            continue
        if line.startswith(" "):
            # Context line — strip the leading space.
            files.setdefault(current_path, []).append(line[1:])
            continue
        if line == "":
            # Empty line between hunks (a real context line in some formats).
            files.setdefault(current_path, []).append("")
            continue
        # Anything else — treat as added content (defensive: some
        # diff formats omit the "+" prefix on the very first line).
        files.setdefault(current_path, []).append(line)

    files_dict = {path: "\n".join(content_lines) for path, content_lines in files.items()}
    return files_dict, deleted_paths


def _parse_patch_files(patch_text: str) -> dict[str, str]:
    """
    Parse a unified diff and reconstruct each touched file's new content.

    Returns a dict ``{file_path: new_content}``.
    """
    files, _ = _parse_patch(patch_text)
    if not files:
        raise ValueError(
            "push_patch_as_commit: patch did not parse to any file changes "
            "(no '+++ b/...' header found, or every file was a deletion)"
        )
    return files


async def push_patch_to_mirror(
    gh: httpx.AsyncClient,
    repo_full: str,
    *,
    branch: str,
    patch_text: str,
    commit_message: str,
    token: str,
    workflow_filename: str = "",
    workflow_content: str = "",
    seed_files: Optional[dict[str, bytes]] = None,
) -> str:
    """
    Construct an isolated orphan commit on ``refs/heads/{branch}`` containing:
      a) Seeded repository files (from tarball)
      b) .github/workflows/{workflow_filename}
      c) The patch diff modifications (with deletions pruned)

    Flow (Git Data API: tree -> commit -> ref):
      1. Combines seeded files, workflow file, and patch changes into a file map.
      2. Creates blobs (or inlines text entries <10KB) for all files.
      3. POST /git/trees with base_tree: None (fresh root tree).
      4. POST /git/commits with parents: [] (orphan root commit).
      5. POST /git/refs to refs/heads/{branch} (or PATCH force:true on 422).

    Never mutates or force-pushes to main or default branch.
    """
    if not branch or not branch.strip():
        raise ValueError("push_patch_to_mirror: branch is empty")

    files: dict[str, str] = {}
    deleted_paths: list[str] = []
    split_files: dict[str, str] = {}
    combined_deleted: set[str] = set()

    if patch_text and patch_text.strip():
        files, deleted_paths = _parse_patch(patch_text)
        split_files, split_deleted = _split_patch_by_file(patch_text)
        combined_deleted = set(deleted_paths) | set(split_deleted)

        if not files and not combined_deleted:
            raise ValueError("push_patch_to_mirror: patch did not parse to any file changes")

        # MVP guardrail — surface large patches as a clear ValueError so the
        # runner can mark the attempt as a config issue rather than burning
        # attempts on a silent no-op.
        if len(files) > _MAX_PATCH_FILES:
            raise ValueError(
                f"push_patch_to_mirror: patch touches {len(files)} files, "
                f"max supported in MVP is {_MAX_PATCH_FILES} (Contents-API "
                f"fallback is future work)"
            )
        if len(patch_text.encode("utf-8")) > _MAX_PATCH_BYTES:
            raise ValueError(
                f"push_patch_to_mirror: patch size exceeds MVP limit "
                f"({_MAX_PATCH_BYTES} bytes); Contents-API fallback is future work"
            )
    else:
        if not seed_files and not (workflow_filename and workflow_content):
            raise ValueError("push_patch_to_mirror: patch_text is empty and no seed_files or workflow provided")

    headers = _auth_headers(token)

    # 1. Combine all files:
    # a) Seeded repository files (from tarball)
    all_files: dict[str, bytes] = dict(seed_files) if seed_files else {}

    # Prune files explicitly deleted by the patch (resolving path against seed files)
    for del_p in combined_deleted:
        resolved_del = resolve_file_path(del_p, all_files.keys())
        if resolved_del != del_p:
            logger.info("mirror: auto-resolved deleted path '%s' -> '%s'", del_p, resolved_del)
        all_files.pop(resolved_del, None)

    # b) .github/workflows/{workflow_filename}
    if workflow_filename and workflow_content:
        wf_path = f".github/workflows/{workflow_filename}"
        all_files[wf_path] = workflow_content.encode("utf-8")

    # c) The patch diff modifications (with unified diff application & path resolution)
    for path, patch_for_file in split_files.items():
        resolved_path = resolve_file_path(path, all_files.keys())
        if resolved_path != path:
            logger.info(
                "mirror: auto-resolved patch path '%s' -> '%s' against seed files",
                path,
                resolved_path,
            )
        if resolved_path in all_files:
            try:
                base_str = all_files[resolved_path].decode("utf-8")
            except UnicodeDecodeError:
                base_str = all_files[resolved_path].decode("utf-8", errors="replace")
            new_content = apply_unified_diff(base_str, patch_for_file)
            all_files[resolved_path] = new_content.encode("utf-8")
        else:
            # New file
            new_content = apply_unified_diff("", patch_for_file)
            all_files[resolved_path] = new_content.encode("utf-8")

    # Fallback for any file captured in files that split_files missed
    for path, fallback_content in files.items():
        resolved_path = resolve_file_path(path, all_files.keys())
        if resolved_path not in all_files:
            all_files[resolved_path] = fallback_content.encode("utf-8")

    if not all_files:
        raise ValueError("push_patch_to_mirror: no files to commit")

    # 2. Build tree entries & blobs (inline small text < 10KB to avoid huge payloads)
    tree_entries: list[dict[str, Any]] = []
    blobs_to_create: list[tuple[str, bytes]] = []

    for path, content_bytes in all_files.items():
        if len(content_bytes) < 10_000:
            try:
                text_content = content_bytes.decode("utf-8")
                tree_entries.append({
                    "path": path,
                    "mode": "100644",
                    "type": "blob",
                    "content": text_content,
                })
                continue
            except UnicodeDecodeError:
                pass
        blobs_to_create.append((path, content_bytes))

    if blobs_to_create:
        async def _create_blob(p: str, c: bytes) -> tuple[str, str]:
            encoded = base64.b64encode(c).decode("ascii")
            resp = await gh.post(
                f"{_GITHUB_API_BASE}/repos/{repo_full}/git/blobs",
                headers=headers,
                json={"content": encoded, "encoding": "base64"},
            )
            resp.raise_for_status()
            return p, resp.json()["sha"]

        for i in range(0, len(blobs_to_create), 16):
            batch = blobs_to_create[i : i + 16]
            results = await asyncio.gather(*(_create_blob(p, c) for p, c in batch))
            for p, s in results:
                tree_entries.append({"path": p, "mode": "100644", "type": "blob", "sha": s})
            if i + 16 < len(blobs_to_create):
                await asyncio.sleep(0.5)

    # 3. Create fresh root tree (base_tree: None)
    tree_resp = await gh.post(
        f"{_GITHUB_API_BASE}/repos/{repo_full}/git/trees",
        headers=headers,
        json={"base_tree": None, "tree": tree_entries},
    )
    tree_resp.raise_for_status()
    new_tree_sha: str = tree_resp.json()["sha"]

    # 4. Create orphan root commit (parents: [])
    commit_resp = await gh.post(
        f"{_GITHUB_API_BASE}/repos/{repo_full}/git/commits",
        headers=headers,
        json={
            "message": commit_message,
            "tree": new_tree_sha,
            "parents": [],
        },
    )
    commit_resp.raise_for_status()
    new_commit_sha: str = commit_resp.json()["sha"]

    # 5. Create (or update) the branch ref pointing to refs/heads/{branch}
    clean_branch = (
        branch[len("refs/heads/"):] if branch.startswith("refs/heads/") else branch
    )
    ref = f"refs/heads/{clean_branch}"
    ref_resp = await gh.post(
        f"{_GITHUB_API_BASE}/repos/{repo_full}/git/refs",
        headers=headers,
        json={"ref": ref, "sha": new_commit_sha},
    )
    if ref_resp.status_code == 422:
        update_resp = await gh.patch(
            f"{_GITHUB_API_BASE}/repos/{repo_full}/git/refs/heads/{clean_branch}",
            headers=headers,
            json={"sha": new_commit_sha, "force": True},
        )
        update_resp.raise_for_status()
    else:
        ref_resp.raise_for_status()

    logger.info(
        "mirror: pushed orphan commit sha=%s on branch %s (files=%d)",
        new_commit_sha,
        clean_branch,
        len(all_files),
    )
    return new_commit_sha


async def push_patch_as_commit(
    gh: httpx.AsyncClient,
    repo_full: str,
    *,
    branch: str,
    base_sha: str = "",
    patch_text: str,
    commit_message: str,
    token: str,
    workflow_filename: str = "",
    workflow_content: str = "",
    seed_files: Optional[dict[str, bytes]] = None,
) -> str:
    """Deprecated compatibility wrapper for push_patch_to_mirror."""
    return await push_patch_to_mirror(
        gh,
        repo_full,
        branch=branch,
        patch_text=patch_text,
        workflow_filename=workflow_filename,
        workflow_content=workflow_content,
        commit_message=commit_message,
        token=token,
        seed_files=seed_files,
    )
