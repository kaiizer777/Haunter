"""
GitHub Actions sandbox runner — Phase 2 (github.md §5).

Implements ``SandboxRunner`` using a Haunter-org test mirror + GitHub
Actions. Polls the check-runs API instead of using webhooks (the test
repo has no inbound webhook by design — avoids a public surface area).

Flow (github.md §5.2):
  1. Mint a GitHub App installation token (cached 1h, refreshed at <5m
     from expiry). PEM is loaded lazily from SSM on first mint.
  2. Get-or-create the test mirror (org/repo_name).
  3. Resolve the test mirror's default branch HEAD as ``base_sha`` if
     the caller didn't supply one in ``SandboxInput.base_sha``.
  4. Push the language-specific workflow file (``workflow_templates/``)
     to the test mirror. Idempotent but re-pushed every attempt for
     MVP simplicity (caching is a small optimization per github.md §10).
  5. Push the patch as a commit on ``haunter-attempt-{N}`` via the
     Git Data API (see ``app.sandbox.mirror``).
  6. Poll the check-runs API every ``poll_interval_seconds`` up to
     ``poll_timeout_seconds``.
  7. Return ``make_result(passed, reason, duration_ms)``.

Security invariants:
  - PEM loaded at first token mint from SSM via boto3. Never logged,
    never stored outside the in-process ``_PEM_CACHE``.
  - Installation token cached in ``_TOKEN_CACHE`` until <5 min from
    expiry. Keyed by ``f"{app_id}:{installation_id}"`` to isolate
    multiple Apps in the same process.
  - Failure reason always sanitized via ``_sanitize_failure_reason``
    before being placed in the ``SandboxResult`` — secrets stripped,
    length-capped.
  - Fast-fail on 403 / rate-limit / installation-revoked (non-retryable)
    so the orchestrator's existing fallback path posts a diagnosis
    comment instead of burning fix attempts on a deterministic failure.
  - All API calls go through ``httpx.AsyncClient`` with a 30s timeout.
  - Lazy imports: ``pyjwt``, ``boto3`` are imported inside the helper
    that needs them. Zero overhead when ``SANDBOX_PROVIDER`` is anything
    other than ``"github_actions"``.

Wiring note for the orchestrator (github.md §5.2):
  The runner reads ``user_github_id`` and ``attempt_number`` from
  ``SandboxInput``. The current orchestrator does NOT populate these
  on the SandboxInput. As a temporary MVP workaround, the runner
  falls back to a DB lookup for ``user_github_id`` via
  ``_resolve_user_github_id``. The proper fix is to add the fields
  to the dispatcher's ``verify()`` signature and have the
  orchestrator pass them; tracked for follow-up, NOT scoped to this
  Phase 2 review (per the spec's "do not touch the orchestrator" rule).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any, Optional

import httpx

from app.sandbox.mirror import (
    detect_language,
    get_or_create_test_repo,
    push_patch_as_commit,
    test_repo_name,
)
from app.sandbox.runner import SandboxInput, SandboxResult, SandboxRunner, make_result
from app.sandbox.verifier import _sanitize_failure_reason

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GITHUB_API_BASE: str = "https://api.github.com"
_API_TIMEOUT_SECONDS: float = 30.0
_PEM_LOAD_TIMEOUT_SECONDS: float = 10.0

# Token cache: GitHub installation tokens are 1h; we refresh
# conservatively 5 min before expiry.
_TOKEN_REFRESH_MARGIN_SECONDS: int = 5 * 60
_TOKEN_VALIDITY_SECONDS: int = 60 * 60

# GitHub App JWT: must be ≤ 10 minutes, per GitHub docs.
_JWT_VALIDITY_SECONDS: int = 10 * 60
_JWT_BACKDATE_SECONDS: int = 30  # avoid clock skew at the issuer

# Module-level token cache. Key: f"{app_id}:{installation_id}" -> (token, expires_mono).
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}

# Module-level PEM cache. Key: SSM path -> PEM string.
# Never re-fetched within the process lifetime; cleared only on
# unexpected error to allow one retry on the next call.
_PEM_CACHE: dict[str, str] = {}

# Non-retryable GitHub API error phrases. When matched, the failure
# reason is prefixed "[non-retryable]" and the orchestrator's existing
# fallback path (diagnosis comment) handles it — no point burning
# fix_generator attempts on a quota/permission issue.
_NON_RETRYABLE_GITHUB_PHRASES: tuple[str, ...] = (
    "api rate limit exceeded",
    "abuse detection mechanism",
    "secondary rate limit",
    "installation not found",
    "installation suspended",
    "this installation has been suspended",
    "app installation has been suspended",
    "resource not accessible",
    "bad credentials",
    "token expired",
    "ssl certificate problem",
    "forbidden",
)

# Retry policy for transient API errors (network blips, 5xx, 429).
_RETRY_MAX_ATTEMPTS: int = 3
_RETRY_BACKOFF_BASE_SECONDS: float = 1.0
_RETRY_BACKOFF_CAP_SECONDS: float = 10.0

# File path extractor for the patch-fallback language detection.
_FILE_PATH_RE: re.Pattern[str] = re.compile(r"^\+\+\+\s+(?:[ab]/)?(\S+)")


# ---------------------------------------------------------------------------
# Token minting
# ---------------------------------------------------------------------------


def _clear_token_cache_for_tests() -> None:
    """Reset module-level caches. Test-only — never call from production."""
    _TOKEN_CACHE.clear()
    _PEM_CACHE.clear()


async def _load_pem_from_ssm(ssm_path: str) -> str:
    """
    Load the GitHub App private key (PEM) from SSM Parameter Store.

    Cached in ``_PEM_CACHE`` for the lifetime of the process. The
    blocking ``boto3`` call is run via ``asyncio.to_thread`` so the
    event loop is never blocked.

    Raises ``RuntimeError`` (with the SSM API error wrapped) if the
    parameter is missing or inaccessible — the IAM policy on the
    Lambda role (see aws.md §4) is the only realistic failure mode.
    """
    if ssm_path in _PEM_CACHE:
        return _PEM_CACHE[ssm_path]

    def _sync_load() -> str:
        import boto3

        client = boto3.client("ssm")
        resp = client.get_parameter(Name=ssm_path, WithDecryption=True)
        return resp["Parameter"]["Value"]

    try:
        pem = await asyncio.wait_for(
            asyncio.to_thread(_sync_load),
            timeout=_PEM_LOAD_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise RuntimeError(
            f"SSM get_parameter timed out after {_PEM_LOAD_TIMEOUT_SECONDS}s "
            f"(path={ssm_path})"
        ) from None

    _PEM_CACHE[ssm_path] = pem
    logger.info(
        "github_actions_runner: loaded PEM from SSM (path=%s, len=%d)",
        ssm_path,
        len(pem),
    )
    return pem


async def mint_installation_token(
    app_id: str,
    installation_id: str,
    ssm_path: str,
) -> str:
    """
    Mint (or return cached) a short-lived GitHub App installation token.

    Steps:
      1. Return the cached token if it's still valid (with refresh margin).
      2. Load the PEM from SSM (cached after first load).
      3. Build an App JWT (RS256, 10 min validity, 30s back-dating for
         clock-skew safety) using ``pyjwt``.
      4. POST ``/app/installations/{installation_id}/access_tokens`` to
         exchange the JWT for an installation access token.
      5. Cache the token for 1h (``_TOKEN_VALIDITY_SECONDS``).
    """
    cache_key = f"{app_id}:{installation_id}"
    now = time.monotonic()
    cached = _TOKEN_CACHE.get(cache_key)
    if cached is not None:
        token, expires_mono = cached
        if now < expires_mono - _TOKEN_REFRESH_MARGIN_SECONDS:
            return token
        logger.info(
            "github_actions_runner: cached token near expiry for "
            "installation %s, re-minting",
            installation_id,
        )

    pem = await _load_pem_from_ssm(ssm_path)

    import jwt  # PyJWT — only imported on the cold path of the cache miss

    now_unix = int(time.time())
    app_jwt: str = jwt.encode(
        {
            "iat": now_unix - _JWT_BACKDATE_SECONDS,
            "exp": now_unix + _JWT_VALIDITY_SECONDS,
            "iss": app_id,
        },
        pem,
        algorithm="RS256",
    )

    async with httpx.AsyncClient(timeout=_API_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            f"{_GITHUB_API_BASE}/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    token: str = data["token"]
    expires_at_mono = now + _TOKEN_VALIDITY_SECONDS
    _TOKEN_CACHE[cache_key] = (token, expires_at_mono)
    logger.info(
        "github_actions_runner: minted installation token (installation=%s, "
        "expires_at=%s)",
        installation_id,
        data.get("expires_at", "?"),
    )
    return token


# ---------------------------------------------------------------------------
# Generic API helpers
# ---------------------------------------------------------------------------


def _auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _is_non_retryable(exc_text: str) -> bool:
    return any(phrase in exc_text for phrase in _NON_RETRYABLE_GITHUB_PHRASES)


def _non_retryable_reason(exc: Exception, prefix: str = "GitHub API") -> str:
    """
    Build a sanitized failure reason marked non-retryable when applicable.

    The phrase check runs over BOTH ``str(exc)`` and the response body
    (if ``exc`` is an ``httpx.HTTPStatusError`` and the body is
    readable). This matters because GitHub's error bodies carry the
    discriminators (``"API rate limit exceeded"``, ``"installation
    suspended"``, etc.) — ``str(exc)`` only carries the status code and
    URL, never the body, so without this we'd silently treat every 4xx
    as retryable.

    The body is also appended (truncated to 300 chars) to the displayed
    reason so the dashboard surfaces the actual GitHub-side error
    message, not just "HTTPStatusError: 403".
    """
    text = (str(exc) or "").lower()
    body_text = ""
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            body_text = response.text or ""
        except Exception:
            pass
    combined = f"{text} {body_text.lower()}"
    is_non_retryable = _is_non_retryable(combined)
    head = f"[non-retryable] {prefix}: " if is_non_retryable else f"{prefix}: "
    base = f"{type(exc).__name__}: {str(exc)[:500]}"
    if body_text:
        # Truncate and sanitize the body before adding it.
        body_snip = body_text[:300].replace("\n", " ").strip()
        return f"{head}{base} | body: {body_snip}"
    return f"{head}{base}"


# ---------------------------------------------------------------------------
# Workflow file push
# ---------------------------------------------------------------------------


async def _push_workflow_file(
    client: httpx.AsyncClient,
    repo_full: str,
    *,
    base_sha: str,
    workflow_filename: str,
    workflow_content: str,
    token: str,
    fallback_token: Optional[str] = None,
) -> str:
    """
    Push the test workflow file to the test mirror via the Contents API.

    Uses ``PUT /repos/{owner}/{repo}/contents/{path}`` (single-file
    Contents API) instead of the Git Data API (blobs → trees → commits →
    refs) because:
      1. Single-file write — Contents API is the right tool here.
      2. Avoids the ``base_tree`` object-database issue: GitHub auto-init
         tree SHAs cannot be used as ``base_tree`` via a PAT (POST /git/trees
         returns 404 even though GET on that tree SHA returns 200).
      3. Handles create-vs-update transparently via the optional blob
         ``sha`` field — idempotent across re-runs.

    GitHub Apps need ``workflows: write`` permission to write to
    ``.github/workflows/``. If the App token gets 403, retries with
    ``fallback_token`` (``settings.github_token`` PAT). Returns the new
    commit SHA so the patch branch can fork off the correct HEAD.

    Permanent fix: add ``workflows: write`` to the App at
    github.com/settings/apps.
    """
    import base64 as _base64

    workflow_path = f".github/workflows/{workflow_filename}"
    write_token = token

    # ------------------------------------------------------------------
    # 1. Check if the workflow file already exists so we can pass its
    #    blob SHA in the update payload (Contents API requires it on PUT
    #    for existing files; omit on creates).
    # ------------------------------------------------------------------
    existing_blob_sha: Optional[str] = None
    get_resp = await client.get(
        f"{_GITHUB_API_BASE}/repos/{repo_full}/contents/{workflow_path}",
        headers=_auth_headers(write_token),
    )
    if get_resp.status_code == 200:
        existing_blob_sha = get_resp.json().get("sha")

    # ------------------------------------------------------------------
    # 2. PUT the workflow file.
    # ------------------------------------------------------------------
    encoded_content = _base64.b64encode(workflow_content.encode("utf-8")).decode("ascii")
    put_payload: dict = {
        "message": f"haunter: add {workflow_filename}",
        "content": encoded_content,
    }
    if existing_blob_sha:
        put_payload["sha"] = existing_blob_sha

    put_resp = await client.put(
        f"{_GITHUB_API_BASE}/repos/{repo_full}/contents/{workflow_path}",
        headers=_auth_headers(write_token),
        json=put_payload,
    )

    # On 403 (App lacks workflows:write), retry with fallback PAT.
    if put_resp.status_code == 403 and fallback_token:
        logger.warning(
            "github_actions_runner: workflow file PUT 403 with App token — "
            "retrying with fallback PAT (App needs 'workflows: write' "
            "permission — add it at github.com/settings/apps)"
        )
        write_token = fallback_token
        put_resp = await client.put(
            f"{_GITHUB_API_BASE}/repos/{repo_full}/contents/{workflow_path}",
            headers=_auth_headers(write_token),
            json=put_payload,
        )

    put_resp.raise_for_status()
    new_commit_sha: str = put_resp.json()["commit"]["sha"]
    logger.info(
        "github_actions_runner: pushed workflow %s to %s (commit=%s, token=%s)",
        workflow_filename,
        repo_full,
        new_commit_sha,
        "fallback_pat" if write_token == fallback_token else "app_token",
    )
    return new_commit_sha


# ---------------------------------------------------------------------------
# Check-runs polling
# ---------------------------------------------------------------------------


async def _list_workflow_runs(
    client: httpx.AsyncClient,
    repo_full: str,
    head_sha: str,
    *,
    token: str,
) -> list[dict[str, Any]]:
    """
    List workflow runs for a specific commit SHA via the Actions API.

    Uses ``GET /repos/{owner}/{repo}/actions/runs?head_sha={sha}`` which
    requires ``actions: read`` permission — a permission the App already
    has. This replaces the previous ``check-runs`` endpoint which requires
    the separate ``checks: read`` permission that the App currently lacks.

    Returns the list of workflow run objects (may be empty if no workflow
    has been triggered yet).
    """
    resp = await client.get(
        f"{_GITHUB_API_BASE}/repos/{repo_full}/actions/runs",
        headers=_auth_headers(token),
        params={"head_sha": head_sha, "per_page": 10},
    )
    resp.raise_for_status()
    return resp.json().get("workflow_runs", []) or []


async def _get_workflow_run_log_tail(
    client: httpx.AsyncClient,
    repo_full: str,
    run_id: int,
    *,
    token: str,
    max_bytes: int = 2048,
) -> str:
    """
    Return a brief failure summary from a workflow run's jobs.

    Uses ``GET /repos/{owner}/{repo}/actions/runs/{run_id}/jobs``
    (actions: read) to find failed steps, then fetches the first
    failed job's name + step conclusions as a text summary.

    The full log download (``/actions/runs/{id}/logs``) is a 302 redirect
    to a short-lived S3 URL — not supported by httpx without follow_redirects.
    This summary approach is sufficient for the failure_reason field.
    """
    try:
        resp = await client.get(
            f"{_GITHUB_API_BASE}/repos/{repo_full}/actions/runs/{run_id}/jobs",
            headers=_auth_headers(token),
            params={"per_page": 20},
        )
        resp.raise_for_status()
        jobs = resp.json().get("jobs", []) or []
        if not jobs:
            return f"Workflow run #{run_id}: no job details available."

        # Summarise failed jobs/steps.
        lines: list[str] = []
        for job in jobs:
            job_name = job.get("name", f"job#{job.get('id')})")
            conclusion = job.get("conclusion") or "unknown"
            lines.append(f"Job '{job_name}': {conclusion}")
            for step in job.get("steps", []):
                step_conclusion = step.get("conclusion") or step.get("status", "")
                if step_conclusion not in ("success", "skipped", ""):
                    lines.append(
                        f"  Step '{step.get('name', '?')}': {step_conclusion}"
                    )

        summary = "\n".join(lines)
        return summary[-max_bytes:] if len(summary) > max_bytes else summary
    except Exception as exc:
        return f"Workflow run #{run_id}: log fetch failed ({type(exc).__name__}: {str(exc)[:200]})"



# ---------------------------------------------------------------------------
# user_github_id fallback lookup
# ---------------------------------------------------------------------------


async def _resolve_user_github_id(run_id: Any) -> Optional[int]:
    """
    DB fallback for ``user_github_id`` when not in ``SandboxInput``.

    Walks the chain ``run -> repo -> user -> github_id`` using a fresh
    session. Returns ``None`` on any error so the caller can raise a
    clear message instead of a 500.

    MVP workaround: the orchestrator currently doesn't populate
    ``user_github_id`` on the ``SandboxInput``. The proper fix is to
    add the field to the dispatcher's ``verify()`` signature and have
    the orchestrator pass it. Tracked as follow-up work, NOT in scope
    for the Phase 2 review (which is "do not touch the orchestrator").
    """
    try:
        from sqlalchemy import select
        from uuid import UUID as _UUID

        from app.db import async_session_maker
        from app.models import Repo, Run, User

        run_uuid = _UUID(str(run_id))
        async with async_session_maker() as session:
            run = await session.get(Run, run_uuid)
            if run is None:
                return None
            repo = await session.get(Repo, run.repo_id)
            if repo is None:
                return None
            user = await session.get(User, repo.user_id)
            if user is None:
                return None
            return int(user.github_id)
    except Exception as exc:
        logger.warning(
            "github_actions_runner: user_github_id DB fallback failed for "
            "run_id=%s: %s: %s",
            run_id,
            type(exc).__name__,
            str(exc)[:200],
        )
        return None


# ---------------------------------------------------------------------------
# Test-mirror seeding with the user's repo tree
# ---------------------------------------------------------------------------


# Path prefixes to skip when seeding the test mirror. .github/workflows/ is
# skipped because Haunter manages the haunter-test-{py,ts}.yml file itself;
# copying the user's workflow would clobber the sandbox one.
_SEED_SKIP_PREFIXES: tuple[str, ...] = (".git/", ".github/workflows/")


async def _seed_test_mirror_with_user_tree(
    *,
    client: httpx.AsyncClient,
    mirror_full: str,
    user_repo_full: str,
    user_sha: str,
    token: str,
    fallback_token: Optional[str],
    max_files: int,
) -> bool:
    """
    Seed the test mirror's default branch with the user's repo tree at
    user_sha. This is what makes verification meaningful: the test mirror
    is otherwise a fresh empty repo (just the auto_init README + the
    Haunter-pushed workflow), so pytest has no tests to actually run and
    the fix can never be validated.

    Best-effort: any failure is logged and the runner continues with the
    old "fresh mirror" behaviour. Returns True on success, False on any
    failure. Falls back to the PAT for tree/commit/ref calls if the App
    token hits 403 (e.g. on a private user-repo the App can't see).
    """
    # Decide which token to use for the seed ops. The App token is the
    # default; the PAT is the fallback if the App can't see the user's
    # private repo (e.g. App not installed on the user's account).
    seed_token = token
    seed_fallback = fallback_token

    def _hdr(tok: str) -> dict[str, str]:
        return _auth_headers(tok)

    try:
        # 1. Get the user's tree SHA at the failing commit.
        commit_resp = await client.get(
            f"{_GITHUB_API_BASE}/repos/{user_repo_full}/git/commits/{user_sha}",
            headers=_hdr(seed_token),
        )
        if commit_resp.status_code == 403 and seed_fallback and seed_fallback != seed_token:
            logger.warning(
                "github_actions_runner: seed step 1 (user commit) 403 with App token — retrying with PAT"
            )
            commit_resp = await client.get(
                f"{_GITHUB_API_BASE}/repos/{user_repo_full}/git/commits/{user_sha}",
                headers=_hdr(seed_fallback),
            )
            seed_token = seed_fallback
        commit_resp.raise_for_status()
        user_tree_sha = commit_resp.json()["tree"]["sha"]

        # 2. Recursive tree entries from the user repo.
        tree_resp = await client.get(
            f"{_GITHUB_API_BASE}/repos/{user_repo_full}/git/trees/{user_tree_sha}?recursive=1",
            headers=_hdr(seed_token),
        )
        tree_resp.raise_for_status()
        entries = tree_resp.json().get("tree", [])

        # 3. Filter: blobs only, skip blocked prefixes, cap at max_files.
        files_to_copy: list[dict] = []
        for e in entries:
            if e.get("type") != "blob":
                continue
            if any(e["path"].startswith(p) for p in _SEED_SKIP_PREFIXES):
                continue
            files_to_copy.append(e)
            if len(files_to_copy) >= max_files:
                break

        if not files_to_copy:
            logger.info(
                "github_actions_runner: nothing to seed into %s (no user files after filter)",
                mirror_full,
            )
            return True

        # 4. Get the current HEAD on the mirror (its auto_init commit).
        mirror_repo_resp = await client.get(
            f"{_GITHUB_API_BASE}/repos/{mirror_full}",
            headers=_hdr(seed_token),
        )
        mirror_repo_resp.raise_for_status()
        default_branch: str = mirror_repo_resp.json()["default_branch"]
        ref_resp = await client.get(
            f"{_GITHUB_API_BASE}/repos/{mirror_full}/git/refs/heads/{default_branch}",
            headers=_hdr(seed_token),
        )
        ref_resp.raise_for_status()
        parent_sha: str = ref_resp.json()["object"]["sha"]
        parent_commit_resp = await client.get(
            f"{_GITHUB_API_BASE}/repos/{mirror_full}/git/commits/{parent_sha}",
            headers=_hdr(seed_token),
        )
        parent_commit_resp.raise_for_status()
        parent_tree_sha: str = parent_commit_resp.json()["tree"]["sha"]

        # 5. Create a new tree on the mirror: base = parent tree, with
        #    entries that reference the user's blob SHAs (cross-repo).
        new_tree_resp = await client.post(
            f"{_GITHUB_API_BASE}/repos/{mirror_full}/git/trees",
            headers=_hdr(seed_token),
            json={
                "base_tree": parent_tree_sha,
                "tree": [
                    {
                        "path": e["path"],
                        "mode": e.get("mode", "100644"),
                        "type": "blob",
                        "sha": e["sha"],
                    }
                    for e in files_to_copy
                ],
            },
        )
        if new_tree_resp.status_code == 403 and seed_fallback and seed_fallback != seed_token:
            logger.warning(
                "github_actions_runner: seed step 5 (create tree) 403 with App token — retrying with PAT"
            )
            new_tree_resp = await client.post(
                f"{_GITHUB_API_BASE}/repos/{mirror_full}/git/trees",
                headers=_hdr(seed_fallback),
                json={
                    "base_tree": parent_tree_sha,
                    "tree": [
                        {
                            "path": e["path"],
                            "mode": e.get("mode", "100644"),
                            "type": "blob",
                            "sha": e["sha"],
                        }
                        for e in files_to_copy
                    ],
                },
            )
            seed_token = seed_fallback
        new_tree_resp.raise_for_status()
        new_tree_sha: str = new_tree_resp.json()["sha"]

        # 6. Create the commit.
        new_commit_resp = await client.post(
            f"{_GITHUB_API_BASE}/repos/{mirror_full}/git/commits",
            headers=_hdr(seed_token),
            json={
                "message": f"haunter: seed test mirror with {len(files_to_copy)} file(s) from {user_repo_full}@{user_sha[:12]}",
                "tree": new_tree_sha,
                "parents": [parent_sha],
            },
        )
        new_commit_resp.raise_for_status()
        new_commit_sha: str = new_commit_resp.json()["sha"]

        # 7. Fast-forward the default branch to the new commit.
        update_resp = await client.patch(
            f"{_GITHUB_API_BASE}/repos/{mirror_full}/git/refs/heads/{default_branch}",
            headers=_hdr(seed_token),
            json={"sha": new_commit_sha, "force": True},
        )
        update_resp.raise_for_status()

        logger.info(
            "github_actions_runner: seeded %s with %d file(s) from %s @ %s (new HEAD: %s)",
            mirror_full,
            len(files_to_copy),
            user_repo_full,
            user_sha[:12],
            new_commit_sha[:12],
        )
        return True
    except Exception as exc:
        logger.warning(
            "github_actions_runner: failed to seed %s from %s @ %s (%s: %s) — continuing with fresh mirror",
            mirror_full,
            user_repo_full,
            user_sha[:12],
            type(exc).__name__,
            exc,
        )
        return False


# ---------------------------------------------------------------------------
# File-path extraction from patch
# ---------------------------------------------------------------------------


def _extract_file_paths_from_patch(patch_text: str) -> list[str]:
    """
    Extract touched file paths from a unified diff (fallback when
    ``SandboxInput.file_paths`` is not populated).

    Used to drive ``detect_language`` when the orchestrator didn't pass
    a file list. Only considers ``+++ b/...`` headers; ``/dev/null``
    (deletions) is skipped.
    """
    paths: list[str] = []
    for line in (patch_text or "").splitlines():
        m = _FILE_PATH_RE.match(line)
        if not m:
            continue
        path = m.group(1)
        if path == "/dev/null":
            continue
        if path not in paths:
            paths.append(path)
    return paths


# ---------------------------------------------------------------------------
# Workflow file loader
# ---------------------------------------------------------------------------


def _load_workflow_template(filename: str) -> str:
    """
    Read a workflow template from ``app/sandbox/workflow_templates/``.

    Raises ``FileNotFoundError`` with a clear path so missing-template
    failures are obvious in the CloudWatch logs.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    template_path = os.path.join(here, "workflow_templates", filename)
    with open(template_path, "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------


class GitHubActionsSandboxRunner(SandboxRunner):
    """
    Sandbox adapter backed by a Haunter-org test mirror + GitHub Actions.

    See module docstring for the full flow + security invariants.
    """

    async def verify(self, inp: SandboxInput) -> SandboxResult:
        from app.config import settings

        t_start = time.monotonic()

        # ----------------------------------------------------------------
        # 1. Read settings (with clear errors for missing env vars).
        # Done first — matches AWSSandboxRunner.verify() — so a
        # misconfigured instance fails fast with the actionable
        # "GITHUB_SANDBOX_* not configured" reason rather than the
        # less-helpful "user_github_id is not set" reason.
        # ----------------------------------------------------------------
        org: str = (
            getattr(settings, "github_sandbox_org", None) or "haunter-sandboxes"
        )
        app_id: Optional[str] = getattr(settings, "github_sandbox_app_id", None)
        installation_id: Optional[str] = getattr(
            settings, "github_sandbox_installation_id", None
        )
        ssm_path: str = getattr(
            settings,
            "github_sandbox_app_private_key_ssm_path",
            "/haunter/GITHUB_SANDBOX_APP_PRIVATE_KEY",
        )
        poll_interval: float = float(
            getattr(settings, "github_sandbox_poll_interval_seconds", 10.0) or 10.0
        )
        poll_timeout: float = float(
            getattr(settings, "github_sandbox_poll_timeout_seconds", 120.0) or 120.0
        )
        workflow_filename_py: str = getattr(
            settings,
            "github_sandbox_workflow_filename_py",
            "haunter-test-py.yml",
        )
        workflow_filename_ts: str = getattr(
            settings,
            "github_sandbox_workflow_filename_ts",
            "haunter-test-ts.yml",
        )

        if not app_id:
            return make_result(
                passed=False,
                reason="[non-retryable] GITHUB_SANDBOX_APP_ID not configured.",
                duration_ms=int((time.monotonic() - t_start) * 1000),
            )
        if not installation_id:
            return make_result(
                passed=False,
                reason=(
                    "[non-retryable] GITHUB_SANDBOX_INSTALLATION_ID not "
                    "configured."
                ),
                duration_ms=int((time.monotonic() - t_start) * 1000),
            )

        # ----------------------------------------------------------------
        # 2. attempt_number is required for branch naming.
        # ----------------------------------------------------------------
        if inp.attempt_number is None:
            return make_result(
                passed=False,
                reason=(
                    "[non-retryable] GitHub Actions sandbox: attempt_number "
                    "is required on SandboxInput."
                ),
                duration_ms=int((time.monotonic() - t_start) * 1000),
            )

        # ----------------------------------------------------------------
        # 3. Resolve user_github_id (required).
        # MVP fallback: walk Run -> Repo -> User via the DB if the
        # orchestrator didn't populate it on SandboxInput. See
        # _resolve_user_github_id docstring.
        # ----------------------------------------------------------------
        user_github_id: Optional[int] = inp.user_github_id
        if user_github_id is None:
            user_github_id = await _resolve_user_github_id(inp.run_id)
            if user_github_id is None:
                return make_result(
                    passed=False,
                    reason=(
                        "[non-retryable] GitHub Actions sandbox: "
                        "user_github_id is not set on SandboxInput and the "
                        "DB fallback lookup failed. The orchestrator must "
                        "populate SandboxInput.user_github_id (or pass db "
                        "to sandbox.verify())."
                    ),
                    duration_ms=int((time.monotonic() - t_start) * 1000),
                )

        # ----------------------------------------------------------------
        # 4. Mint installation token (cached after first call)
        # ----------------------------------------------------------------
        try:
            token = await mint_installation_token(
                app_id=app_id,
                installation_id=installation_id,
                ssm_path=ssm_path,
            )
        except Exception as exc:
            return make_result(
                passed=False,
                reason=_sanitize_failure_reason(
                    f"Failed to mint installation token: "
                    f"{type(exc).__name__}: {str(exc)[:300]}"
                ),
                duration_ms=int((time.monotonic() - t_start) * 1000),
            )

        # ----------------------------------------------------------------
        # 5. Get or create the test mirror
        # fallback_token (settings.github_token PAT) handles the case where
        # the App installation lacks ``administration: write`` (403 on
        # POST /user/repos or POST /orgs/{org}/repos). The permanent fix
        # is to add ``Administration: write`` to the App at
        # github.com/settings/apps.
        # ----------------------------------------------------------------
        repo_name = test_repo_name(user_github_id)
        try:
            async with httpx.AsyncClient(timeout=_API_TIMEOUT_SECONDS) as client:
                repo_full = await get_or_create_test_repo(
                    client,
                    org,
                    repo_name,
                    token=token,
                    fallback_token=getattr(settings, "github_token", None),
                )

                # --------------------------------------------------------
                # 5b. Seed the test mirror with the user's repo tree at
                #     the failing commit, so verification can actually
                #     exercise the failing test (not just the patch in
                #     isolation). Best-effort: failures are logged and
                #     the runner continues with the old "fresh mirror"
                #     behaviour. This is what makes "does the LLM's fix
                #     actually work?" a meaningful question.
                # --------------------------------------------------------
                if inp.repo_ref and inp.head_sha:
                    # repo_ref may be "owner/repo@sha" — strip the @sha
                    user_repo_full = inp.repo_ref.split("@", 1)[0]
                    # NICE-3: cap mirrors via settings.seed_max_files so a
                    # large user repo doesn't blow up the CodeBuild build time.
                    await _seed_test_mirror_with_user_tree(
                        client=client,
                        mirror_full=repo_full,
                        user_repo_full=user_repo_full,
                        user_sha=inp.head_sha,
                        token=token,
                        fallback_token=getattr(settings, "github_token", None),
                        max_files=settings.seed_max_files,
                    )

                # --------------------------------------------------------
                # 6. Determine language and load workflow template
                # --------------------------------------------------------
                file_paths = inp.file_paths or _extract_file_paths_from_patch(
                    inp.patch
                )
                language = detect_language(file_paths)
                workflow_filename = (
                    workflow_filename_py if language == "py" else workflow_filename_ts
                )
                try:
                    workflow_content = _load_workflow_template(workflow_filename)
                except FileNotFoundError as exc:
                    return make_result(
                        passed=False,
                        reason=_sanitize_failure_reason(
                            f"Workflow template not found on disk: "
                            f"{workflow_filename} ({exc})"
                        ),
                        duration_ms=int((time.monotonic() - t_start) * 1000),
                    )

                # --------------------------------------------------------
                # 7. Resolve base_sha (caller-supplied or default branch HEAD)
                # --------------------------------------------------------
                base_sha = inp.base_sha
                if not base_sha:
                    repo_resp = await client.get(
                        f"{_GITHUB_API_BASE}/repos/{repo_full}",
                        headers=_auth_headers(token),
                    )
                    repo_resp.raise_for_status()
                    default_branch: str = repo_resp.json()["default_branch"]
                    ref_resp = await client.get(
                        f"{_GITHUB_API_BASE}/repos/{repo_full}/git/refs/heads/{default_branch}",
                        headers=_auth_headers(token),
                    )
                    ref_resp.raise_for_status()
                    base_sha = ref_resp.json()["object"]["sha"]

                # --------------------------------------------------------
                # 8. Push the workflow file to the test mirror.
                # fallback_token (settings.github_token PAT) handles the
                # case where the App lacks ``workflows: write`` permission
                # (403 on .github/workflows/ tree creation). The permanent
                # fix is to add ``workflows: write`` to the App at
                # github.com/settings/apps.
                # --------------------------------------------------------
                fallback_token: Optional[str] = getattr(
                    settings, "github_token", None
                )
                # workflow_base_sha is the new HEAD after the workflow file
                # is committed to the default branch. The patch commit must
                # branch off this SHA, not the pre-workflow SHA, so the
                # check-run applies to a consistent history.
                workflow_base_sha = await _push_workflow_file(
                    client,
                    repo_full,
                    base_sha=base_sha,
                    workflow_filename=workflow_filename,
                    workflow_content=workflow_content,
                    token=token,
                    fallback_token=fallback_token,
                )
                # Use the post-workflow HEAD as the base for the patch branch.
                base_sha = workflow_base_sha

                # --------------------------------------------------------
                # 9. Push the patch on a per-attempt branch
                # --------------------------------------------------------
                branch = f"haunter-attempt-{inp.attempt_number}"
                commit_message = f"haunter attempt {inp.attempt_number}"
                try:
                    head_sha = await push_patch_as_commit(
                        client,
                        repo_full,
                        branch=branch,
                        base_sha=base_sha,
                        patch_text=inp.patch,
                        commit_message=commit_message,
                        token=token,
                    )
                except ValueError as exc:
                    # Mirror module raises ValueError on bad patches (too
                    # large, no file changes, etc.) — these are
                    # config-style failures, not transient.
                    return make_result(
                        passed=False,
                        reason=_sanitize_failure_reason(
                            f"[non-retryable] {str(exc)[:500]}"
                        ),
                        duration_ms=int((time.monotonic() - t_start) * 1000),
                    )

                # --------------------------------------------------------
                # 10. Poll Actions workflow runs (requires actions:read, which
                #     the App already has). Previously polled check-runs
                #     which required checks:read (not granted on the App).
                # --------------------------------------------------------
                deadline = time.monotonic() + poll_timeout
                while time.monotonic() < deadline:
                    await asyncio.sleep(poll_interval)
                    runs = await _list_workflow_runs(
                        client, repo_full, head_sha, token=token
                    )
                    if not runs:
                        # No workflow run yet — the push may not have
                        # triggered the Actions workflow yet. Keep polling.
                        continue
                    if all(r.get("status") == "completed" for r in runs):
                        first = runs[0]
                        conclusion = (first.get("conclusion") or "").lower()
                        if conclusion == "success":
                            return make_result(
                                passed=True,
                                reason=None,
                                duration_ms=int(
                                    (time.monotonic() - t_start) * 1000
                                ),
                            )
                        # Failure / cancelled / timed_out — pull job summary.
                        log_tail = await _get_workflow_run_log_tail(
                            client, repo_full, first["id"], token=token
                        )
                        return make_result(
                            passed=False,
                            reason=_sanitize_failure_reason(
                                f"Workflow run concluded '{conclusion}': {log_tail}"
                            ),
                            duration_ms=int(
                                (time.monotonic() - t_start) * 1000
                            ),
                        )

                # Deadline reached without a terminal status.
                return make_result(
                    passed=False,
                    reason=_sanitize_failure_reason(
                        f"Sandbox verification timed out after "
                        f"{int(poll_timeout)}s (no terminal check-run)."
                    ),
                    duration_ms=int(poll_timeout * 1000),
                )

        except httpx.HTTPStatusError as exc:
            return make_result(
                passed=False,
                reason=_sanitize_failure_reason(
                    _non_retryable_reason(exc, prefix="GitHub API")
                ),
                duration_ms=int((time.monotonic() - t_start) * 1000),
            )
        except httpx.TimeoutException as exc:
            return make_result(
                passed=False,
                reason=_sanitize_failure_reason(
                    f"GitHub API timeout: {type(exc).__name__}: {str(exc)[:300]}"
                ),
                duration_ms=int((time.monotonic() - t_start) * 1000),
            )
        except (ValueError, RuntimeError) as exc:
            return make_result(
                passed=False,
                reason=_sanitize_failure_reason(
                    _non_retryable_reason(exc, prefix="GitHub Actions sandbox")
                ),
                duration_ms=int((time.monotonic() - t_start) * 1000),
            )
        except Exception as exc:
            # Last-resort safety net — never raise out of verify().
            return make_result(
                passed=False,
                reason=_sanitize_failure_reason(
                    _non_retryable_reason(exc, prefix="GitHub Actions sandbox")
                ),
                duration_ms=int((time.monotonic() - t_start) * 1000),
            )
