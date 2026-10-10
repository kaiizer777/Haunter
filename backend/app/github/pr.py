"""
GitHub REST API integration for PR Writer (Phase 8).

All write operations require a scoped installation token — never a broad PAT.
Token fetched via GitHub App JWT → POST /app/installations/{id}/access_tokens.
Cached for 50 min (GitHub expiry is 60 min, 10 min safety margin).

App permission requirements (contents:write, pull_requests:write only):
  - Contents: write  → create blobs, trees, commits, update refs
  - Pull requests: write → open PRs
  - NO administration → cannot force-push, cannot bypass branch protection

Security invariants:
  - owner/repo/branch validated against _REPO_IDENT_RE / _BRANCH_RE before HTTP
  - force=False always enforced on ref creation (no force-push)
  - PR title/body html.escape'd + secret-redacted + length-capped before POST
  - Installation token NEVER logged or persisted to DB
  - PEM private key NEVER logged under any code path
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import re
import time
from typing import Any, Optional

import httpx

from app.config import settings
from app.github_client import GitHubResourceNotFoundError
from app.schemas import _PROTECTED_BRANCHES, is_protected_branch, validate_repo_ident

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
DEFAULT_TIMEOUT_SECONDS = 30.0

# ---------------------------------------------------------------------------
# Pull request actions that make a PR reviewable
# ---------------------------------------------------------------------------
# One definition, shared by every consumer that has to answer "is this delivery
# a change to the PR's content?": the webhook's code-review branch
# (app.webhooks.github_webhook) and the auditor's PR trigger
# (app.services.audit_pipeline.evaluate_pr). Two hand-copied frozensets of
# GitHub action names is how `ready_for_review` ended up reviewed by neither:
# a PR opened as a draft is dropped by the draft guard, and then dropped again
# when it is promoted, so it is never reviewed at all.
#
# `edited` is deliberately absent: it fires on title/body/label edits, which do
# not change the diff, and reviewing on it would re-post the same findings for
# every typo fix in the description.
REVIEWABLE_PR_ACTIONS: frozenset[str] = frozenset(
    {"opened", "synchronize", "ready_for_review", "reopened"}
)


def is_reviewable_pr_action(action: Optional[str]) -> bool:
    """True when a ``pull_request`` action changes what a review must analyse."""
    return action in REVIEWABLE_PR_ACTIONS

# ---------------------------------------------------------------------------
# Validation regexes
# ---------------------------------------------------------------------------

# Allowlist for owner/repo name components.
# Matches GitHub's own rules: alphanumeric, hyphen, underscore, dot.
_REPO_IDENT_RE: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9_.\-]+$")

# Branch name allowlist. Rejects shell-injection chars (;, $, `, etc.).
_BRANCH_RE: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9/_\-\.]+$")

# Maximum branch name length (GitHub hard limit is 250 bytes; we enforce 255 chars).
_BRANCH_MAX_LEN = 255

# Protected base branches — Haunter must never push directly to these.
# Single source of truth imported from app.schemas.
_PROTECTED_BRANCHES: frozenset[str] = _PROTECTED_BRANCHES

# ---------------------------------------------------------------------------
# Secret redaction (import from context_gatherer to keep a single source of truth)
# ---------------------------------------------------------------------------

from app.subagents.context_gatherer import _redact_secrets  # noqa: E402

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GitHubPRError(Exception):
    """Base exception for Phase 8 GitHub write operations."""


class GitHubPRAuthError(GitHubPRError):
    """401/403 from GitHub during a write operation."""


class GitHubPRValidationError(GitHubPRError):
    """Input failed regex / length validation before any HTTP call."""


# ---------------------------------------------------------------------------
# Token cache — in-process, single-tenant (per install_id)
# ---------------------------------------------------------------------------

# {install_id: (token_str, expires_at_monotonic)}
_TOKEN_CACHE: dict[int, tuple[str, float]] = {}
_CACHE_TTL_SECONDS = 50 * 60  # 50 min (GitHub expires at 60 min)


def _validate_ident(value: str, label: str) -> None:
    """Validate an owner or repo name. Raises GitHubPRValidationError on mismatch."""
    try:
        validate_repo_ident(value, label)
    except ValueError as exc:
        raise GitHubPRValidationError(
            f"{label} {value!r} contains invalid characters or traversal segments. "
            "Only [a-zA-Z0-9_.-] are allowed."
        ) from exc


def _validate_branch(branch: str, allow_protected: bool = True) -> None:
    """Validate a branch name. Raises GitHubPRValidationError on mismatch, length excess, or protected branch."""
    if len(branch) > _BRANCH_MAX_LEN:
        raise GitHubPRValidationError(
            f"Branch name exceeds maximum length of {_BRANCH_MAX_LEN} characters."
        )
    if not _BRANCH_RE.match(branch):
        raise GitHubPRValidationError(
            f"Branch name {branch!r} contains invalid characters. "
            r"Only [a-zA-Z0-9/_\-.] are allowed."
        )
    clean = branch.removeprefix("refs/heads/")
    if not allow_protected and is_protected_branch(clean):
        raise GitHubPRValidationError(
            f"Cannot target protected branch {branch!r} directly."
        )


def _escape_pr_text(text: str, max_len: int) -> str:
    """
    Sanitise LLM-generated text before posting to GitHub.

    Steps (applied in this order so redaction never sees already-escaped HTML):
      1. Redact secrets (sk-, ghp_, npg_, PEM blocks, DB URLs).
      2. html.escape to prevent markdown injection / stored XSS on dashboard.
      3. Truncate to max_len.
    """
    sanitised = _redact_secrets(text)
    sanitised = html.escape(sanitised, quote=False)
    return sanitised[:max_len]


def _is_configured_str(value: object) -> bool:
    """True when a credential setting is a non-blank string (whitespace-only = unset)."""
    return isinstance(value, str) and bool(value.strip())


def _resolve_app_credentials() -> tuple[str | None, str | None, str]:
    """
    Resolve the write-capable GitHub App credentials from environment.

    ONLY the explicit write-capable pair (``settings.github_app_id`` +
    ``settings.github_app_private_key``) is accepted here. The read-only
    auditor App (``settings.github_auditor_app_*``) is NEVER a valid source
    for write operations — its installation token lacks
    ``contents:write`` / ``pull_requests:write``, so signing PR writes with
    it fails at GitHub with 403 while masking the real misconfiguration
    (a missing write App PEM).

    Both members of the explicit pair must be non-blank; a half-configured
    pair is treated as missing so we never sign with a mismatched id/key
    combination.

    The SSM-backed write key (``settings.github_app_private_key_ssm_path``)
    is resolved separately by ``_resolve_write_credentials()`` — this
    function stays sync and env-only so non-async callers keep working.

    Returns:
        (app_id, private_key, source) where source is ``"github_app"`` or
        ``"none"``. Values are never logged by callers — only ``source``.
    """
    if _is_configured_str(settings.github_app_id) and _is_configured_str(
        settings.github_app_private_key
    ):
        assert settings.github_app_id is not None
        assert settings.github_app_private_key is not None
        return settings.github_app_id.strip(), settings.github_app_private_key, "github_app"
    return None, None, "none"


# Module-level PEM cache for the SSM-backed write App key.
# Key: SSM path -> (PEM string, loaded_at_monotonic). TTL-bounded so a
# rotated key is picked up without a Lambda restart/redeploy: entries older
# than _PEM_CACHE_TTL_SECONDS are re-fetched from SSM, and any 401/403 from
# GitHub on the SSM path triggers an immediate invalidate + single retry
# (see _invalidate_pem_cache() call sites in get_installation_token() and
# resolve_installation_id()).
_PEM_CACHE: dict[str, tuple[str, float]] = {}

# PEM cache TTL — 10 min. Short enough that a key rotation propagates
# quickly on a warm Lambda, long enough to avoid an SSM call per webhook.
_PEM_CACHE_TTL_SECONDS: float = 10 * 60

# Timeout for the blocking boto3 get_parameter call (run via asyncio.to_thread
# so the event loop is never blocked — same pattern as the sandbox runner).
_PEM_LOAD_TIMEOUT_SECONDS: float = 5.0


def _clear_pem_cache_for_tests() -> None:
    """Reset the module-level PEM cache. Test-only — never call from production."""
    _PEM_CACHE.clear()


def _invalidate_pem_cache(ssm_path: str | None) -> None:
    """Drop one SSM path from the PEM cache so the next read re-fetches.

    Called when GitHub rejects the App JWT with 401/403 on the SSM path —
    the cached PEM is likely a rotated (stale) key. No-op for unknown or
    blank paths. Never logs key material — only the path.
    """
    if _is_configured_str(ssm_path):
        assert isinstance(ssm_path, str)
        if _PEM_CACHE.pop(ssm_path.strip(), None) is not None:
            logger.info(
                "github.pr: invalidated cached PEM (path=%s) after auth failure",
                ssm_path.strip(),
            )


async def _load_pem_from_ssm(ssm_path: str) -> str:
    """
    Load the write-capable GitHub App private key (PEM) from SSM Parameter Store.

    Cached in ``_PEM_CACHE`` for ``_PEM_CACHE_TTL_SECONDS`` (10 min) so a
    rotated key propagates without a Lambda restart. The blocking
    ``boto3`` call runs via ``asyncio.to_thread`` so the event loop is never
    blocked. The PEM is never logged — only the path and length.

    Raises:
        RuntimeError: If the parameter is missing, inaccessible, or the call
            times out (the IAM policy on the Lambda role is the only
            realistic failure mode — see infra/aws/lambda.tf).
    """
    cached = _PEM_CACHE.get(ssm_path)
    if cached is not None:
        pem, loaded_at = cached
        if time.monotonic() - loaded_at < _PEM_CACHE_TTL_SECONDS:
            return pem
        # Stale entry — drop it and re-fetch below (rotation window).

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
    except Exception as exc:
        raise RuntimeError(
            f"SSM get_parameter failed (path={ssm_path}): {exc}"
        ) from exc

    _PEM_CACHE[ssm_path] = (pem, time.monotonic())
    logger.info(
        "github.pr: loaded write App PEM from SSM (path=%s, len=%d)",
        ssm_path,
        len(pem),
    )
    return pem


async def _resolve_write_credentials() -> tuple[str | None, str | None, str]:
    """
    Resolve the write-capable GitHub App credentials for PR write operations.

    Order:
      1. Explicit env pair via ``_resolve_app_credentials()`` (local dev, tests).
      2. ``settings.github_app_id`` + PEM loaded from SSM at
         ``settings.github_app_private_key_ssm_path`` — the Lambda path,
         which keeps the ~1.6KB PEM out of the Lambda env-var block.
      3. ``(None, None, "none")`` — the caller falls back to
         ``settings.github_token`` (dev only) or fails closed.

    The read-only auditor App is NEVER consulted here (see
    ``_resolve_app_credentials``): its token cannot publish writes.

    Returns:
        (app_id, private_key, source) where source is one of
        ``"github_app"``, ``"github_app_ssm"``, or ``"none"``. Values are
        never logged by callers — only ``source``.
    """
    app_id, private_key, source = _resolve_app_credentials()
    if app_id and private_key:
        return app_id, private_key, source
    ssm_path = getattr(settings, "github_app_private_key_ssm_path", "")
    if _is_configured_str(settings.github_app_id) and _is_configured_str(ssm_path):
        assert settings.github_app_id is not None
        assert isinstance(ssm_path, str)
        pem = await _load_pem_from_ssm(ssm_path.strip())
        if _is_configured_str(pem):
            return settings.github_app_id.strip(), pem, "github_app_ssm"
    return None, None, "none"


async def _refresh_ssm_credentials_after_auth_failure(
    app_source: str,
) -> tuple[str | None, str | None, str]:
    """Invalidate the cached SSM PEM and re-resolve write credentials once.

    Called when GitHub rejects the App JWT with 401/403 and the failed
    request used the SSM-backed key (``source == "github_app_ssm"``): the
    cached PEM is likely a rotated (stale) key held by a warm Lambda.
    Drops the stale entry so the re-resolve below re-fetches from SSM.

    Returns fresh ``(app_id, private_key, source)``, or
    ``(None, None, "none")`` when a retry cannot help (env-pair source, or
    the re-resolve itself failed). Values are never logged — only ``source``.
    """
    if app_source != "github_app_ssm":
        return None, None, "none"
    ssm_path = getattr(settings, "github_app_private_key_ssm_path", "")
    _invalidate_pem_cache(ssm_path if isinstance(ssm_path, str) else None)
    try:
        return await _resolve_write_credentials()
    except Exception as exc:
        logger.warning(
            "github.pr: SSM credential refresh after auth failure failed: %s",
            exc,
        )
        return None, None, "none"


def _build_jwt(
    app_id: str | None = None,
    private_key: str | None = None,
    source: str | None = None,
) -> str:
    """
    Build a GitHub App JWT for authenticating as the App itself.

    Uses RS256 (RSA + SHA-256) as required by GitHub. When ``app_id`` /
    ``private_key`` are not passed, the explicit env pair is resolved via
    ``_resolve_app_credentials()``; SSM-backed callers resolve first via
    ``_resolve_write_credentials()`` and pass the values in. The key material
    is never logged here — only ``source``.

    Raises:
        GitHubPRError: If no write-capable App credentials are available,
            or the private key PEM is malformed (never logs key material).
        ImportError:   If 'cryptography' is not installed.
    """
    resolved_source = source
    if app_id is None or private_key is None:
        app_id, private_key, resolved_source = _resolve_app_credentials()
        if source is None:
            source = resolved_source
    if not app_id or not private_key:
        raise GitHubPRError(
            "GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY must be set to use "
            "installation token auth. Falling back to settings.github_token for dev."
        )
    logger.info("github.pr: building App JWT with source=%s", source or "github_app")

    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as exc:
        raise ImportError(
            "cryptography package is required for GitHub App JWT auth. "
            "Add cryptography>=43 to requirements.txt."
        ) from exc

    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iat": now - 60,  # allow 60s clock skew
        "exp": now + 600,  # 10 min max (GitHub enforces ≤ 10 min)
        "iss": app_id,
    }

    def _b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    header_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{header_b64}.{payload_b64}".encode()

    # Load PEM — never log the key object
    pem_bytes = private_key.encode()
    try:
        loaded_key = serialization.load_pem_private_key(pem_bytes, password=None)
    except (ValueError, TypeError):
        raise GitHubPRError(
            f"Invalid GitHub App private key (source={source or 'github_app'})."
        ) from None

    signature = loaded_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    sig_b64 = _b64url(signature)

    return f"{header_b64}.{payload_b64}.{sig_b64}"


async def get_installation_token(repo: Any) -> str:
    """
    Fetch (or return cached) a GitHub App installation token scoped to `repo`.

    Token is cached per installation_id for 50 minutes (GitHub expires at 60).
    Cache is in-process only — token is NEVER written to DB or logs.

    Write auth resolves via _resolve_write_credentials(): the explicit
    GITHUB_APP_ID / GITHUB_APP_PRIVATE_KEY env pair first, then the same App
    ID with the PEM loaded from SSM (Lambda path). The read-only auditor App
    is NEVER used here. Falls back to settings.github_token when no
    write-capable credentials exist (dev/test convenience only — not for prod).

    Args:
        repo: Repo ORM object — must have .github_install_id set.

    Returns:
        A GitHub installation access token string.

    Raises:
        GitHubPRError: On HTTP error, missing install_id, or missing auth.
    """
    install_id: Optional[int] = getattr(repo, "github_install_id", None)

    # Dev/test fallback — documented: not for prod
    app_id, private_key, app_source = await _resolve_write_credentials()
    if app_source == "none":
        logger.warning(
            "github.pr: no write-capable App credentials (GITHUB_APP_ID + "
            "GITHUB_APP_PRIVATE_KEY or GITHUB_APP_PRIVATE_KEY_SSM_PATH) — "
            "using settings.github_token (dev only)"
        )
        if settings.github_token:
            return settings.github_token
        raise GitHubPRError(
            "No GitHub auth configured for PR writes: set GITHUB_APP_ID + "
            "GITHUB_APP_PRIVATE_KEY (or GITHUB_APP_PRIVATE_KEY_SSM_PATH "
            "pointing at the write App PEM in SSM). The read-only auditor "
            "App cannot publish writes and is never used here."
        )
    logger.info("github.pr: resolving installation token with source=%s", app_source)

    if not install_id and getattr(repo, "owner", None) and getattr(repo, "name", None):
        try:
            discovery_jwt = _build_jwt(app_id, private_key, app_source)
            discovery_url = f"{GITHUB_API_BASE}/repos/{repo.owner}/{repo.name}/installation"
            discovery_headers = {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {discovery_jwt}",
                "User-Agent": "Haunter-Autonomous-Agent/1.0",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as disc_client:
                disc_resp = await disc_client.get(discovery_url, headers=discovery_headers)
                if disc_resp.status_code == 200:
                    discovered_id = disc_resp.json().get("id")
                    if discovered_id:
                        install_id = discovered_id
                        try:
                            repo.github_install_id = discovered_id
                        except Exception:
                            pass
        except Exception as disc_err:
            logger.warning(
                "github.pr: failed to auto-discover installation_id for %s/%s: %s",
                getattr(repo, "owner", ""),
                getattr(repo, "name", ""),
                disc_err,
            )

    if not install_id:
        raise GitHubPRError(
            f"repo {getattr(repo, 'id', '?')} has no github_install_id — "
            "cannot fetch installation token."
        )

    # Check in-process cache
    cached = _TOKEN_CACHE.get(install_id)
    if cached:
        token_str, expires_at = cached
        if time.monotonic() < expires_at:
            return token_str

    jwt_token = _build_jwt(app_id, private_key, app_source)
    url = f"{GITHUB_API_BASE}/app/installations/{install_id}/access_tokens"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {jwt_token}",
        "User-Agent": "Haunter-Autonomous-Agent/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async def _post_token(auth_headers: dict[str, str]) -> httpx.Response:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            try:
                return await client.post(url, headers=auth_headers)
            except httpx.RequestError as exc:
                raise GitHubPRError(
                    f"Network error fetching installation token: {exc.__class__.__name__}"
                ) from exc

    response = await _post_token(headers)

    if response.status_code in (401, 403) and app_source == "github_app_ssm":
        # The cached PEM may be a rotated (stale) key on a warm Lambda —
        # drop it, reload from SSM, and retry once with a fresh JWT.
        logger.warning(
            "github.pr: installation token auth failed (%s) on SSM key — "
            "refreshing PEM and retrying once",
            response.status_code,
        )
        retry_id, retry_key, retry_source = (
            await _refresh_ssm_credentials_after_auth_failure(app_source)
        )
        if retry_source != "none" and retry_id and retry_key:
            headers = {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {_build_jwt(retry_id, retry_key, retry_source)}",
                "User-Agent": "Haunter-Autonomous-Agent/1.0",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            response = await _post_token(headers)

    if response.status_code in (401, 403):
        raise GitHubPRAuthError(
            f"GitHub App auth failed ({response.status_code}). "
            "Check App ID, private key, and installation."
        )
    if response.is_error:
        raise GitHubPRError(
            f"GitHub returned {response.status_code} fetching installation token."
        )

    data = response.json()
    token_str = data["token"]
    expires_at = time.monotonic() + _CACHE_TTL_SECONDS

    _TOKEN_CACHE[install_id] = (token_str, expires_at)
    logger.info("github.pr: installation token fetched for install_id=%s", install_id)
    return token_str


async def resolve_installation_id(owner: str, repo: str) -> int:
    """
    Resolve the GitHub App installation id for ``owner/repo`` via the App JWT.

    Uses ``GET /repos/{owner}/{repo}/installation`` authenticated as the App
    itself (JWT bearer). Intended as a backfill path for repos connected
    before ``github_install_id`` was wired into the connect flow.

    Args:
        owner: Repository owner (validated against _REPO_IDENT_RE).
        repo:  Repository name (validated against _REPO_IDENT_RE).

    Returns:
        The installation id as a positive int.

    Raises:
        GitHubPRAuthError: On 401/403 (bad App credentials or App not installed).
        GitHubPRError: On missing App config, validation failure, network or HTTP error.
    """
    _validate_ident(owner, "owner")
    _validate_ident(repo, "repo")

    app_id, private_key, app_source = await _resolve_write_credentials()
    if app_source == "none":
        raise GitHubPRError(
            "GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY (or "
            "GITHUB_APP_PRIVATE_KEY_SSM_PATH) must be set to resolve "
            "the installation id. The read-only auditor App cannot "
            "publish writes and is never used here."
        )
    logger.info("github.pr: resolving installation id with source=%s", app_source)

    jwt_token = _build_jwt(app_id, private_key, app_source)
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/installation"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {jwt_token}",
        "User-Agent": "Haunter-Autonomous-Agent/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async def _get_installation(auth_headers: dict[str, str]) -> httpx.Response:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            try:
                return await client.get(url, headers=auth_headers)
            except httpx.RequestError as exc:
                raise GitHubPRError(
                    f"Network error resolving installation id: {exc.__class__.__name__}"
                ) from exc

    response = await _get_installation(headers)

    if response.status_code in (401, 403) and app_source == "github_app_ssm":
        # The cached PEM may be a rotated (stale) key on a warm Lambda —
        # drop it, reload from SSM, and retry once with a fresh JWT.
        logger.warning(
            "github.pr: installation lookup auth failed (%s) on SSM key — "
            "refreshing PEM and retrying once",
            response.status_code,
        )
        retry_id, retry_key, retry_source = (
            await _refresh_ssm_credentials_after_auth_failure(app_source)
        )
        if retry_source != "none" and retry_id and retry_key:
            headers = {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {_build_jwt(retry_id, retry_key, retry_source)}",
                "User-Agent": "Haunter-Autonomous-Agent/1.0",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            response = await _get_installation(headers)

    if response.status_code in (401, 403):
        raise GitHubPRAuthError(
            f"GitHub App auth failed resolving installation ({response.status_code})."
        )
    if response.status_code == 404:
        raise GitHubPRError(
            f"GitHub App is not installed on {owner}/{repo} (404)."
        )
    if response.is_error:
        raise GitHubPRError(
            f"GitHub returned {response.status_code} resolving installation id."
        )

    try:
        data = response.json()
        install_id = int(data["id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise GitHubPRError(
            "GitHub installation lookup returned an unexpected payload."
        ) from exc
    if install_id <= 0:
        raise GitHubPRError(
            "GitHub installation lookup returned an invalid installation id."
        )
    logger.info(
        "github.pr: resolved installation id for %s/%s",
        owner,
        repo,
    )
    return install_id


# ---------------------------------------------------------------------------
# Branch + commit helpers
# ---------------------------------------------------------------------------


def _build_auth_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Haunter-Autonomous-Agent/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _get_repo_default_branch_sha(
    owner: str, repo: str, branch: str, token: str
) -> str:
    """Fetch the HEAD SHA of `branch` in the repo."""
    _validate_ident(owner, "owner")
    _validate_ident(repo, "repo")
    _validate_branch(branch, allow_protected=True)
    clean_branch = branch.removeprefix("refs/heads/")
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/ref/heads/{clean_branch}"
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
        try:
            resp = await client.get(url, headers=_build_auth_headers(token))
        except httpx.RequestError as exc:
            raise GitHubPRError(
                f"Network error fetching ref heads/{clean_branch}: {exc.__class__.__name__}"
            ) from exc

    if resp.status_code == 404:
        raise GitHubResourceNotFoundError(
            f"Branch ref not found: {owner}/{repo}/heads/{clean_branch}"
        )
    if resp.status_code == 422:
        error_msg = resp.text
        if "reference does not exist" in error_msg.lower() or "not found" in error_msg.lower():
            raise GitHubResourceNotFoundError(
                f"Branch ref not found (422): {owner}/{repo}/heads/{clean_branch}"
            )
        raise GitHubPRError(
            f"Failed to fetch ref heads/{clean_branch} (422): {error_msg[:200]}"
        )
    if resp.status_code in (401, 403):
        raise GitHubPRAuthError(f"Auth failed fetching branch ref ({resp.status_code}).")
    if resp.is_error:
        raise GitHubPRError(
            f"Failed to fetch ref heads/{clean_branch}: HTTP {resp.status_code}"
        )
    return resp.json()["object"]["sha"]


async def update_branch_ref(
    owner: str,
    repo: str,
    branch: str,
    sha: str,
    token: str,
    force: bool = False,
) -> None:
    """
    Update a branch ref to point at a new commit SHA.

    PATCH /repos/{owner}/{repo}/git/refs/heads/{branch}

    Rejects protected branches directly (allow_protected=False).

    Raises:
        GitHubPRValidationError: On invalid owner, repo, or branch, or protected branch.
        GitHubResourceNotFoundError: On 404 or 422 ("Reference does not exist").
        GitHubPRAuthError: On 401/403.
        GitHubPRError: On other HTTP / network errors.
    """
    _validate_ident(owner, "owner")
    _validate_ident(repo, "repo")
    _validate_branch(branch, allow_protected=False)

    if force:
        raise GitHubPRValidationError("Force-updating branch ref is not permitted")

    clean_branch = branch.removeprefix("refs/heads/")
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/refs/heads/{clean_branch}"
    headers = _build_auth_headers(token)
    payload = {"sha": sha, "force": False}

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
        try:
            resp = await client.patch(url, headers=headers, json=payload)
        except httpx.RequestError as exc:
            raise GitHubPRError(
                f"Network error updating branch ref {branch!r}: {exc.__class__.__name__}"
            ) from exc

    if resp.status_code == 404:
        raise GitHubResourceNotFoundError(
            f"Branch ref not found: {owner}/{repo}/heads/{clean_branch}"
        )
    if resp.status_code == 422:
        error_msg = resp.text
        if "reference does not exist" in error_msg.lower() or "not found" in error_msg.lower():
            raise GitHubResourceNotFoundError(
                f"Branch ref not found (422): {owner}/{repo}/heads/{clean_branch}"
            )
        raise GitHubPRError(
            f"Failed to update ref heads/{clean_branch} (422): {error_msg[:200]}"
        )
    if resp.status_code in (401, 403):
        raise GitHubPRAuthError(f"Auth failed updating branch ref ({resp.status_code}).")
    if resp.is_error:
        raise GitHubPRError(
            f"Failed to update ref heads/{clean_branch}: HTTP {resp.status_code}"
        )

    logger.info(
        "github.pr: updated ref %s/%s:%s to sha=%s (force=%s)",
        owner,
        repo,
        clean_branch,
        sha[:8],
        force,
    )


async def create_branch(
    owner: str,
    repo: str,
    branch: str,
    sha: str,
    token: str,
) -> None:
    """
    Create a new branch at `sha` in the repo.

    Never uses force. Rejects protected branches. Raises GitHubPRValidationError on invalid owner/repo/branch.
    Raises GitHubPRError if the branch already exists (409) or on HTTP error.

    Args:
        owner:  Repository owner (validated against _REPO_IDENT_RE).
        repo:   Repository name (validated against _REPO_IDENT_RE).
        branch: New branch name (validated against _BRANCH_RE, max 255 chars, non-protected).
        sha:    Full 40-char commit SHA to branch from.
        token:  GitHub installation access token.
    """
    _validate_ident(owner, "owner")
    _validate_ident(repo, "repo")
    _validate_branch(branch, allow_protected=False)

    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/refs"
    payload = {"ref": f"refs/heads/{branch}", "sha": sha}
    # force=false is the default for POST /git/refs — never pass force:true

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
        try:
            resp = await client.post(
                url, headers=_build_auth_headers(token), json=payload
            )
        except httpx.RequestError as exc:
            raise GitHubPRError(
                f"Network error creating branch {branch!r}: {exc.__class__.__name__}"
            ) from exc

    if resp.status_code == 422:
        raise GitHubPRError(f"Branch {branch!r} already exists or SHA invalid.")
    if resp.status_code in (401, 403):
        raise GitHubPRAuthError(f"Auth failed creating branch ({resp.status_code}).")
    if resp.is_error:
        raise GitHubPRError(
            f"Failed to create branch {branch!r}: HTTP {resp.status_code}"
        )

    logger.info(
        "github.pr: created branch %s/%s:%s at sha=%s", owner, repo, branch, sha[:8]
    )


async def delete_branch_ref(
    owner: str,
    repo: str,
    branch: str,
    token: str,
) -> None:
    """
    Delete a branch ref from the repo.

    Never deletes protected branches (allow_protected=False). Safely handles 204 (deleted) and 404 (already gone).
    """
    _validate_ident(owner, "owner")
    _validate_ident(repo, "repo")
    _validate_branch(branch, allow_protected=False)

    clean_branch = branch.removeprefix("refs/heads/")
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/refs/heads/{clean_branch}"
    headers = _build_auth_headers(token)

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
        try:
            resp = await client.delete(url, headers=headers)
        except httpx.RequestError as exc:
            raise GitHubPRError(
                f"Network error deleting branch ref {branch!r}: {exc.__class__.__name__}"
            ) from exc

    if resp.status_code in (204, 404):
        logger.info(
            "github.pr: deleted branch ref %s/%s:%s (status %d)",
            owner,
            repo,
            clean_branch,
            resp.status_code,
        )
        return
    if resp.status_code in (401, 403):
        raise GitHubPRAuthError(f"Auth failed deleting branch ref ({resp.status_code}).")
    if resp.is_error:
        raise GitHubPRError(
            f"Failed to delete branch ref heads/{clean_branch}: HTTP {resp.status_code}"
        )


def _parse_patch_files(patch_text: str) -> dict[str, str]:
    """
    Split a unified diff into per-file patch segments.

    Returns {filepath: file_patch_text} where filepath is the target path (or source path for deletions).
    Returns {} if parsing fails or no valid hunks found.
    Validates each path against _REPO_IDENT_RE traversal checks — invalid paths are skipped.
    """
    files: dict[str, str] = {}
    # Split on diff --git markers to isolate per-file sections; fallback to global scan if absent
    # Use regex to find --- a/... / +++ b/... pairs
    import re as _re

    # Pattern for file headers: --- a/path  or  --- /dev/null, then +++ b/path  or  +++ /dev/null
    header_re = _re.compile(
        r"^---\s+(?:a/)?([^\n]+)\n\+\+\+\s+(?:b/)?([^\n]+)", _re.MULTILINE
    )
    # Find all header positions
    matches = list(header_re.finditer(patch_text))
    if not matches:
        return {}
    for idx, m in enumerate(matches):
        src = m.group(1).strip().split("\t")[0].strip()
        target = m.group(2).strip().split("\t")[0].strip()
        is_deletion = target == "/dev/null" or not target
        file_path = src if is_deletion else target
        if file_path == "/dev/null" or not file_path:
            continue
        # Validate path chars before accepting
        try:
            # Reuse _validate_ident logic for each path component
            for part in file_path.split("/"):
                if part in (".", "..", ""):
                    raise ValueError(f"invalid path component {part!r}")
                if part.startswith(".git") or part.startswith(".github"):
                    # Allow normal files but block .git/ and .github/workflows traversal checked later
                    pass
            if ".." in file_path or file_path.startswith("/") or "//" in file_path:
                continue
            if file_path.startswith(".git/") or file_path.startswith(".github/workflows/"):
                continue
        except Exception:
            continue
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(patch_text)
        content = patch_text[start:end]
        # Must contain at least one hunk header @@
        if "@@" not in content:
            continue
        # Store the full per-file patch (header + body) for applier
        files[file_path] = (m.group(0) + content).strip()
    return files


def _apply_unified_diff_to_content(original_text: str, file_patch: str) -> str | None:
    """
    Apply a per-file unified diff segment to original_text.

    Returns new_text on success, None if hunk context does not match (mismatch).
    Handles multiple hunks per file, context lines, additions, deletions.
    """
    orig_lines = original_text.splitlines()
    # Keep trailing newline info
    had_trailing_newline = original_text.endswith("\n") if original_text else True

    # Extract hunks: each @@ header + body lines
    import re as _re

    hunk_header_re = _re.compile(r"^@@\s+-(\d+),?(\d*)\s+\+(\d+),?(\d*)\s+@@")
    lines = file_patch.splitlines()
    # Skip file header lines (---/+++) until first @@
    hunk_start = None
    for i, ln in enumerate(lines):
        if ln.startswith("@@"):
            hunk_start = i
            break
    if hunk_start is None:
        return None
    # Build new content by walking original lines and hunks
    new_lines: list[str] = []
    orig_idx = 0  # 0-based index into orig_lines

    i = hunk_start
    while i < len(lines):
        line = lines[i]
        m = hunk_header_re.match(line)
        if m:
            # Parse old start and length
            old_start = int(m.group(1))
            _old_len = int(m.group(2)) if m.group(2) else 1
            # new_start = int(m.group(3))  # not needed for apply
            # Advance orig_idx to hunk start (1-based to 0-based)
            # old_start is 1-based line number in original
            target_idx = max(0, old_start - 1)
            # Copy unchanged lines before hunk
            while orig_idx < target_idx and orig_idx < len(orig_lines):
                new_lines.append(orig_lines[orig_idx])
                orig_idx += 1
            i += 1
            # Process hunk body until next @@ or EOF
            while i < len(lines) and not lines[i].startswith("@@"):
                hunk_line = lines[i]
                if hunk_line.startswith(" "):
                    # Context — must match original
                    if (
                        orig_idx >= len(orig_lines)
                        or orig_lines[orig_idx] != hunk_line[1:]
                    ):
                        return None
                    new_lines.append(orig_lines[orig_idx])
                    orig_idx += 1
                elif hunk_line.startswith("-"):
                    # Deletion — must match original then skip
                    if (
                        orig_idx >= len(orig_lines)
                        or orig_lines[orig_idx] != hunk_line[1:]
                    ):
                        return None
                    orig_idx += 1
                elif hunk_line.startswith("+"):
                    # Addition — insert
                    new_lines.append(hunk_line[1:])
                elif hunk_line.startswith("\\"):
                    # No newline at end of file marker — ignore
                    pass
                elif hunk_line == "":
                    # Empty line treated as context of empty string (rare)
                    if orig_idx < len(orig_lines) and orig_lines[orig_idx] == "":
                        new_lines.append("")
                        orig_idx += 1
                    else:
                        new_lines.append("")
                else:
                    # Unknown prefix — bail
                    return None
                i += 1
            continue
        else:
            i += 1

    # Copy remaining original lines after last hunk
    while orig_idx < len(orig_lines):
        new_lines.append(orig_lines[orig_idx])
        orig_idx += 1

    result = "\n".join(new_lines)
    if had_trailing_newline or result:
        result += "\n"
    return result


async def commit_patch(
    owner: str,
    repo: str,
    branch: str,
    patch_text: str,
    commit_msg: str,
    token: str,
) -> str:
    """
    Apply `patch_text` as a single commit on `branch` using the Git Data API.

    Strategy:
      1. Parse patch into per-file segments via _parse_patch_files.
      2. For each file, fetch current content via Contents API, apply hunks locally
         via _apply_unified_diff_to_content, create blob for new content.
      3. Create tree with updated blobs → create commit → update branch ref.
      Fallback: if parsing fails or any file apply fails, commits the raw diff as
      haunter.patch (legacy placeholder) so the PR still contains the diff artifact.

    Args:
        owner:      Repository owner.
        repo:       Repository name.
        branch:     Target branch (must already exist).
        patch_text: Raw unified diff text.
        commit_msg: Commit message — should be the PR title (already sanitised).
        token:      GitHub installation access token.

    Returns:
        The new commit SHA.
    """
    _validate_ident(owner, "owner")
    _validate_ident(repo, "repo")
    _validate_branch(branch, allow_protected=False)

    headers = _build_auth_headers(token)
    api = f"{GITHUB_API_BASE}/repos/{owner}/{repo}"
    clean_branch = branch.removeprefix("refs/heads/")

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
        # 1. Fetch the current HEAD SHA for branch
        try:
            ref_resp = await client.get(f"{api}/git/ref/heads/{clean_branch}", headers=headers)
        except httpx.RequestError as exc:
            raise GitHubPRError(
                f"Network error fetching HEAD for branch {branch!r}: {exc.__class__.__name__}"
            ) from exc

        if ref_resp.status_code == 404:
            raise GitHubResourceNotFoundError(
                f"Branch ref not found: {owner}/{repo}/heads/{clean_branch}"
            )
        if ref_resp.status_code == 422:
            if "reference does not exist" in ref_resp.text.lower() or "not found" in ref_resp.text.lower():
                raise GitHubResourceNotFoundError(
                    f"Branch ref not found (422): {owner}/{repo}/heads/{clean_branch}"
                )
        if ref_resp.status_code in (401, 403):
            raise GitHubPRAuthError(f"Auth failed fetching branch ref ({ref_resp.status_code}).")
        if ref_resp.is_error:
            raise GitHubPRError(
                f"Cannot fetch HEAD for branch {branch!r}: HTTP {ref_resp.status_code}"
            )
        head_sha = ref_resp.json()["object"]["sha"]

        # 2. Get the tree SHA of HEAD commit
        commit_resp = await client.get(f"{api}/git/commits/{head_sha}", headers=headers)
        if commit_resp.is_error:
            raise GitHubPRError(
                f"Cannot fetch commit {head_sha[:8]}: HTTP {commit_resp.status_code}"
            )
        base_tree_sha = commit_resp.json()["tree"]["sha"]

        # 3. Try to parse and apply patch per-file
        tree_entries: list[dict[str, Any]] = []
        per_file_patches = _parse_patch_files(patch_text)
        use_fallback = False

        if per_file_patches:
            for file_path, file_patch in per_file_patches.items():
                # Validate branch-safe file path
                if len(file_path) > 255 or not _BRANCH_RE.match(
                    file_path.replace("/", "_")
                ):
                    # Use looser check for file paths: allow slashes, dots, underscores, hyphens
                    if (
                        ".." in file_path
                        or file_path.startswith("/")
                        or "//" in file_path
                    ):
                        logger.warning(
                            "github.pr: skipping invalid file path %r from patch",
                            file_path,
                        )
                        use_fallback = True
                        break

                # Check for explicit file deletion
                is_deletion = False
                patch_lines = file_patch.splitlines()
                if len(patch_lines) >= 2:
                    second_line = patch_lines[1].strip()
                    if (
                        second_line in (
                            "+++ /dev/null",
                            "+++ b/dev/null",
                            "+++ b//dev/null",
                            "+++ dev/null",
                        )
                        or "/dev/null" in second_line
                    ):
                        is_deletion = True

                # Fetch current file content (may be new file → 404)
                content_resp = await client.get(
                    f"{api}/contents/{file_path}",
                    headers=headers,
                    params={"ref": head_sha},
                )
                if content_resp.status_code == 404:
                    if is_deletion:
                        logger.info(
                            "github.pr: deletion target %r not found (404) — already absent, skipping without fallback",
                            file_path,
                        )
                        continue
                    original_text = ""
                elif content_resp.is_error:
                    logger.warning(
                        "github.pr: failed to fetch %r for branch %s: HTTP %d — fallback to haunter.patch",
                        file_path,
                        branch,
                        content_resp.status_code,
                    )
                    use_fallback = True
                    break
                else:
                    try:
                        data = content_resp.json()
                        # Contents API returns base64-encoded content (may be list for dir)
                        if isinstance(data, list):
                            logger.warning(
                                "github.pr: path %r is a directory — fallback",
                                file_path,
                            )
                            use_fallback = True
                            break
                        b64 = data.get("content", "")
                        # Content may contain newlines; GitHub wraps at 60 chars
                        b64_clean = "".join(b64.split())
                        original_text = (
                            base64.b64decode(b64_clean).decode(
                                "utf-8", errors="replace"
                            )
                            if b64_clean
                            else ""
                        )
                    except Exception as exc:
                        logger.warning(
                            "github.pr: decode failed for %r: %s — fallback",
                            file_path,
                            exc,
                        )
                        use_fallback = True
                        break

                new_text = _apply_unified_diff_to_content(original_text, file_patch)
                if new_text is None:
                    logger.warning(
                        "github.pr: hunk apply failed for %r — fallback to haunter.patch",
                        file_path,
                    )
                    use_fallback = True
                    break

                if is_deletion:
                    tree_entries.append(
                        {
                            "path": file_path,
                            "mode": "100644",
                            "type": "blob",
                            "sha": None,
                        }
                    )
                    continue

                # Create blob for new file content
                blob_resp = await client.post(
                    f"{api}/git/blobs",
                    headers=headers,
                    json={"content": new_text, "encoding": "utf-8"},
                )
                if blob_resp.is_error:
                    logger.warning(
                        "github.pr: blob creation failed for %r: HTTP %d — fallback",
                        file_path,
                        blob_resp.status_code,
                    )
                    use_fallback = True
                    break
                blob_sha = blob_resp.json()["sha"]
                tree_entries.append(
                    {
                        "path": file_path,
                        "mode": "100644",
                        "type": "blob",
                        "sha": blob_sha,
                    }
                )

            # If per-file parsing succeeded but yielded no entries, fallback
            if not use_fallback and not tree_entries:
                use_fallback = True
        else:
            use_fallback = True

        if use_fallback:
            # Legacy fallback: commit raw diff as haunter.patch artifact
            logger.info(
                "github.pr: falling back to haunter.patch artifact for %s/%s:%s",
                owner,
                repo,
                branch,
            )
            blob_resp = await client.post(
                f"{api}/git/blobs",
                headers=headers,
                json={"content": patch_text, "encoding": "utf-8"},
            )
            if blob_resp.is_error:
                raise GitHubPRError(
                    f"Failed to create blob: HTTP {blob_resp.status_code}"
                )
            blob_sha = blob_resp.json()["sha"]
            tree_entries = [
                {
                    "path": "haunter.patch",
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob_sha,
                }
            ]

        # 4. Create a tree containing the updated files
        tree_resp = await client.post(
            f"{api}/git/trees",
            headers=headers,
            json={
                "base_tree": base_tree_sha,
                "tree": tree_entries,
            },
        )
        if tree_resp.is_error:
            raise GitHubPRError(f"Failed to create tree: HTTP {tree_resp.status_code}")
        new_tree_sha = tree_resp.json()["sha"]

        # 5. Create the commit
        new_commit_resp = await client.post(
            f"{api}/git/commits",
            headers=headers,
            json={
                "message": commit_msg[:72],  # cap to PR title max
                "tree": new_tree_sha,
                "parents": [head_sha],
            },
        )
        if new_commit_resp.is_error:
            raise GitHubPRError(
                f"Failed to create commit: HTTP {new_commit_resp.status_code}"
            )
        new_commit_sha = new_commit_resp.json()["sha"]

        # 6. Update the branch ref — force=False (default for PATCH)
        try:
            update_resp = await client.patch(
                f"{api}/git/refs/heads/{clean_branch}",
                headers=headers,
                json={"sha": new_commit_sha, "force": False},
            )
        except httpx.RequestError as exc:
            raise GitHubPRError(
                f"Network error updating branch ref {branch!r}: {exc.__class__.__name__}"
            ) from exc

        if update_resp.status_code == 404:
            raise GitHubResourceNotFoundError(
                f"Branch ref not found: {owner}/{repo}/heads/{clean_branch}"
            )
        if update_resp.status_code == 422:
            if "reference does not exist" in update_resp.text.lower() or "not found" in update_resp.text.lower():
                raise GitHubResourceNotFoundError(
                    f"Branch ref not found (422): {owner}/{repo}/heads/{clean_branch}"
                )
        if update_resp.status_code in (401, 403):
            raise GitHubPRAuthError(f"Auth failed updating branch ref ({update_resp.status_code}).")
        if update_resp.is_error:
            raise GitHubPRError(
                f"Failed to update ref heads/{clean_branch}: HTTP {update_resp.status_code}"
            )

    logger.info(
        "github.pr: committed patch to %s/%s:%s new_sha=%s (%d file(s))",
        owner,
        repo,
        branch,
        new_commit_sha[:8],
        len(tree_entries),
    )
    return new_commit_sha


async def open_pr(
    owner: str,
    repo: str,
    head_branch: str,
    base_branch: str,
    title: str,
    body: str,
    token: str,
) -> dict[str, Any]:
    """
    Open a pull request and return {html_url, number}.

    Title and body are sanitised (html.escape + secret-redact + length-capped)
    before posting. force is never set on the underlying branch.

    Args:
        owner:        Repository owner.
        repo:         Repository name.
        head_branch:  The Haunter fix branch (must exist).
        base_branch:  Target branch (repo default, e.g. 'main').
        title:        PR title — capped at 72 chars after sanitisation.
        body:         PR body — capped at 3000 chars after sanitisation.
        token:        GitHub installation access token.

    Returns:
        {"html_url": str, "number": int}

    Raises:
        GitHubPRValidationError: On invalid owner/repo/branch identifiers.
        GitHubPRAuthError:       On 401/403.
        GitHubPRError:           On any other HTTP error.
    """
    _validate_ident(owner, "owner")
    _validate_ident(repo, "repo")
    _validate_branch(head_branch, allow_protected=False)
    _validate_branch(base_branch, allow_protected=True)

    safe_title = _escape_pr_text(title, max_len=72)
    safe_body = _escape_pr_text(body, max_len=3000)

    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls"
    payload = {
        "title": safe_title,
        "body": safe_body,
        "head": head_branch,
        "base": base_branch,
    }

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
        try:
            resp = await client.post(
                url, headers=_build_auth_headers(token), json=payload
            )
        except httpx.RequestError as exc:
            raise GitHubPRError(
                f"Network error opening PR: {exc.__class__.__name__}"
            ) from exc

    if resp.status_code in (401, 403):
        raise GitHubPRAuthError(f"Auth failed opening PR ({resp.status_code}).")
    if resp.status_code == 422:
        raise GitHubPRError(f"PR validation failed (422): {resp.text[:200]}")
    if resp.is_error:
        raise GitHubPRError(f"Failed to open PR: HTTP {resp.status_code}")

    data = resp.json()
    logger.info(
        "github.pr: PR #%s opened %s/%s <- %s",
        data["number"],
        owner,
        repo,
        head_branch,
    )
    return {"html_url": data["html_url"], "number": data["number"]}
