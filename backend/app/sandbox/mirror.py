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
import re
from typing import Any, Optional

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


def _parse_patch_files(patch_text: str) -> dict[str, str]:
    """
    Parse a unified diff and reconstruct each touched file's new content.

    MVP behavior:
      - Full file replacement diffs (the typical LLM output) round-trip
        perfectly: the new file is the concatenation of all ``+`` and
        context lines for that file.
      - Partial hunk diffs produce a best-effort reconstruction that
        will likely fail the workflow. The future-work fallback is the
        Contents API or a Contents-API-per-file commit.

    Returns a dict ``{file_path: new_content}``. A file appearing only
    in ``--- a/...`` (deletion) is omitted — the MVP does not support
    explicit deletes; deletions would need the future-work tree-entry
    with ``sha: null``.

    Raises:
        ValueError: if the patch is empty or does not contain a single
            ``+++ b/...`` header (i.e. nothing to commit).
    """
    if not patch_text or not patch_text.strip():
        raise ValueError("push_patch_as_commit: patch_text is empty")

    files: dict[str, list[str]] = {}
    current_path: Optional[str] = None

    for line in patch_text.splitlines():
        # ------------------------------------------------------------
        # File header line (--- a/path or +++ b/path, optional a/b prefix)
        # ------------------------------------------------------------
        if (line.startswith("---") or line.startswith("+++")) and _FILE_HEADER_RE.match(line):
            m = _FILE_HEADER_RE.match(line)
            assert m is not None  # narrowed by the match() guard above
            if line.startswith("+++"):
                path = m.group(1)
                # .strip() so CRLF/LF/tab/space-padded markers ("+++ /dev/null\r\n",
                # "+++ /dev/null\t") all classify as a deletion marker rather than
                # being mis-read as a file path. NICE-4.
                if path.strip() == "/dev/null":
                    # Deletion — leave current_path = None; this file
                    # contributes no content. Future-work: support
                    # explicit deletes via tree entry sha=null.
                    current_path = None
                else:
                    current_path = path
            # Either way, a file-header line is metadata, not content.
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

    if not files:
        raise ValueError(
            "push_patch_as_commit: patch did not parse to any file changes "
            "(no '+++ b/...' header found, or every file was a deletion)"
        )

    return {path: "\n".join(content_lines) for path, content_lines in files.items()}


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
      c) The patch diff modifications

    Flow (Git Data API: tree -> commit -> ref):
      1. Combines seeded files, workflow file, and patch changes into a file map.
      2. Creates blobs (or inlines text entries <50KB) for all files.
      3. POST /git/trees with base_tree: None (fresh root tree).
      4. POST /git/commits with parents: [] (orphan root commit).
      5. POST /git/refs to refs/heads/{branch} (or PATCH force:true on 422).

    Never mutates or force-pushes to main or default branch.

    Args:
        gh: shared httpx.AsyncClient.
        repo_full: ``org/repo_name`` of the universal mirror.
        branch: target branch name, e.g. ``sandbox/run-{run_id}-att-{attempt_number}``.
        patch_text: unified diff, possibly multi-file.
        commit_message: commit message (e.g. ``f"haunter attempt {n}"``).
        token: GitHub App installation token (write scope).
        workflow_filename: e.g. ``haunter-test-py.yml``.
        workflow_content: raw yaml text of the workflow.
        seed_files: optional {path: bytes} dict of repository files extracted from tarball.

    Returns:
        The new head commit SHA.

    Raises:
        ValueError: patch is empty, too large, or unparseable.
        httpx.HTTPStatusError: on any GitHub API error.
    """
    if not branch or not branch.strip():
        raise ValueError("push_patch_to_mirror: branch is empty")
    if not patch_text or not patch_text.strip():
        raise ValueError("push_patch_to_mirror: patch_text is empty")

    files = _parse_patch_files(patch_text)

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

    headers = _auth_headers(token)

    # 1. Combine all files:
    # a) Seeded repository files (from tarball)
    all_files: dict[str, bytes] = dict(seed_files) if seed_files else {}

    # b) .github/workflows/{workflow_filename}
    if workflow_filename and workflow_content:
        wf_path = f".github/workflows/{workflow_filename}"
        all_files[wf_path] = workflow_content.encode("utf-8")

    # c) The patch diff modifications
    for path, content_str in files.items():
        all_files[path] = content_str.encode("utf-8")

    if not all_files:
        raise ValueError("push_patch_to_mirror: no files to commit")

    # 2. Build tree entries & blobs
    tree_entries: list[dict[str, Any]] = []
    blobs_to_create: list[tuple[str, bytes]] = []

    for path, content_bytes in all_files.items():
        if len(content_bytes) < 50_000:
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
