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

import base64
import html
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
DEFAULT_TIMEOUT_SECONDS = 30.0

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
_PROTECTED_BRANCHES: frozenset[str] = frozenset({"main", "master", "develop", "dev"})

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
    if not _REPO_IDENT_RE.match(value):
        raise GitHubPRValidationError(
            f"{label} {value!r} contains invalid characters. "
            "Only [a-zA-Z0-9_.-] are allowed."
        )


def _validate_branch(branch: str) -> None:
    """Validate a branch name. Raises GitHubPRValidationError on mismatch or length excess."""
    if len(branch) > _BRANCH_MAX_LEN:
        raise GitHubPRValidationError(
            f"Branch name exceeds maximum length of {_BRANCH_MAX_LEN} characters."
        )
    if not _BRANCH_RE.match(branch):
        raise GitHubPRValidationError(
            f"Branch name {branch!r} contains invalid characters. "
            r"Only [a-zA-Z0-9/_\-.] are allowed."
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


def _build_jwt() -> str:
    """
    Build a GitHub App JWT for authenticating as the App itself.

    Uses RS256 (RSA + SHA-256) as required by GitHub. The private key PEM
    comes from settings.github_app_private_key — never logged here.

    Raises:
        GitHubPRError: If github_app_id or github_app_private_key are not configured.
        ImportError:   If 'cryptography' is not installed.
    """
    if not settings.github_app_id or not settings.github_app_private_key:
        raise GitHubPRError(
            "GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY must be set to use "
            "installation token auth. Falling back to settings.github_token for dev."
        )

    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        import struct
    except ImportError as exc:
        raise ImportError(
            "cryptography package is required for GitHub App JWT auth. "
            "Add cryptography>=43 to requirements.txt."
        ) from exc

    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iat": now - 60,   # allow 60s clock skew
        "exp": now + 600,  # 10 min max (GitHub enforces ≤ 10 min)
        "iss": settings.github_app_id,
    }

    def _b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    header_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{header_b64}.{payload_b64}".encode()

    # Load PEM — never log the key object
    pem_bytes = settings.github_app_private_key.encode()
    private_key = serialization.load_pem_private_key(pem_bytes, password=None)

    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    sig_b64 = _b64url(signature)

    return f"{header_b64}.{payload_b64}.{sig_b64}"


async def get_installation_token(repo: Any) -> str:
    """
    Fetch (or return cached) a GitHub App installation token scoped to `repo`.

    Token is cached per installation_id for 50 minutes (GitHub expires at 60).
    Cache is in-process only — token is NEVER written to DB or logs.

    Falls back to settings.github_token when GITHUB_APP_ID / GITHUB_APP_PRIVATE_KEY
    are not configured (dev/test convenience only — not for prod).

    Args:
        repo: Repo ORM object — must have .github_install_id set.

    Returns:
        A GitHub installation access token string.

    Raises:
        GitHubPRError: On HTTP error or missing install_id.
    """
    install_id: Optional[int] = getattr(repo, "github_install_id", None)

    # Dev/test fallback — documented: not for prod
    if not settings.github_app_id or not settings.github_app_private_key:
        logger.warning(
            "github.pr: GITHUB_APP_ID/PRIVATE_KEY not set — using settings.github_token (dev only)"
        )
        if settings.github_token:
            return settings.github_token
        raise GitHubPRError(
            "No GitHub auth configured: set GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY "
            "or GITHUB_TOKEN for dev."
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

    jwt_token = _build_jwt()
    url = f"{GITHUB_API_BASE}/app/installations/{install_id}/access_tokens"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {jwt_token}",
        "User-Agent": "Haunter-Autonomous-Agent/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
        try:
            response = await client.post(url, headers=headers)
        except httpx.RequestError as exc:
            raise GitHubPRError(
                f"Network error fetching installation token: {exc.__class__.__name__}"
            ) from exc

    if response.status_code in (401, 403):
        raise GitHubPRAuthError(
            f"GitHub App auth failed ({response.status_code}). "
            "Check App ID, private key, and installation."
        )
    if response.is_error:
        raise GitHubPRError(f"GitHub returned {response.status_code} fetching installation token.")

    data = response.json()
    token_str = data["token"]
    expires_at = time.monotonic() + _CACHE_TTL_SECONDS

    _TOKEN_CACHE[install_id] = (token_str, expires_at)
    logger.info("github.pr: installation token fetched for install_id=%s", install_id)
    return token_str


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
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/ref/heads/{branch}"
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
        resp = await client.get(url, headers=_build_auth_headers(token))
    if resp.is_error:
        raise GitHubPRError(
            f"Failed to fetch ref heads/{branch}: HTTP {resp.status_code}"
        )
    return resp.json()["object"]["sha"]


async def create_branch(
    owner: str,
    repo: str,
    branch: str,
    sha: str,
    token: str,
) -> None:
    """
    Create a new branch at `sha` in the repo.

    Never uses force. Raises GitHubPRValidationError on invalid owner/repo/branch.
    Raises GitHubPRError if the branch already exists (409) or on HTTP error.

    Args:
        owner:  Repository owner (validated against _REPO_IDENT_RE).
        repo:   Repository name (validated against _REPO_IDENT_RE).
        branch: New branch name (validated against _BRANCH_RE, max 255 chars).
        sha:    Full 40-char commit SHA to branch from.
        token:  GitHub installation access token.
    """
    _validate_ident(owner, "owner")
    _validate_ident(repo, "repo")
    _validate_branch(branch)

    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/refs"
    payload = {"ref": f"refs/heads/{branch}", "sha": sha}
    # force=false is the default for POST /git/refs — never pass force:true

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
        try:
            resp = await client.post(url, headers=_build_auth_headers(token), json=payload)
        except httpx.RequestError as exc:
            raise GitHubPRError(
                f"Network error creating branch {branch!r}: {exc.__class__.__name__}"
            ) from exc

    if resp.status_code == 422:
        raise GitHubPRError(f"Branch {branch!r} already exists or SHA invalid.")
    if resp.status_code in (401, 403):
        raise GitHubPRAuthError(f"Auth failed creating branch ({resp.status_code}).")
    if resp.is_error:
        raise GitHubPRError(f"Failed to create branch {branch!r}: HTTP {resp.status_code}")

    logger.info("github.pr: created branch %s/%s:%s at sha=%s", owner, repo, branch, sha[:8])


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

    Strategy: create blob from patch → create tree with single blob file
    "haunter.patch" → create commit → update branch ref.

    This is the safe approach for arbitrary patch content — it does not require
    applying the diff on disk and avoids shell injection risks.

    Args:
        owner:      Repository owner.
        repo:       Repository name.
        branch:     Target branch (must already exist).
        patch_text: Raw unified diff text — stored as haunter.patch in the commit.
        commit_msg: Commit message — should be the PR title (already sanitised).
        token:      GitHub installation access token.

    Returns:
        The new commit SHA.
    """
    _validate_ident(owner, "owner")
    _validate_ident(repo, "repo")
    _validate_branch(branch)

    headers = _build_auth_headers(token)
    api = f"{GITHUB_API_BASE}/repos/{owner}/{repo}"

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
        # 1. Fetch the current HEAD SHA for branch
        ref_resp = await client.get(f"{api}/git/ref/heads/{branch}", headers=headers)
        if ref_resp.is_error:
            raise GitHubPRError(
                f"Cannot fetch HEAD for branch {branch!r}: HTTP {ref_resp.status_code}"
            )
        head_sha = ref_resp.json()["object"]["sha"]

        # 2. Get the tree SHA of HEAD commit
        commit_resp = await client.get(f"{api}/git/commits/{head_sha}", headers=headers)
        if commit_resp.is_error:
            raise GitHubPRError(f"Cannot fetch commit {head_sha[:8]}: HTTP {commit_resp.status_code}")
        base_tree_sha = commit_resp.json()["tree"]["sha"]

        # 3. Create a blob for the patch content
        blob_resp = await client.post(
            f"{api}/git/blobs",
            headers=headers,
            json={"content": patch_text, "encoding": "utf-8"},
        )
        if blob_resp.is_error:
            raise GitHubPRError(f"Failed to create blob: HTTP {blob_resp.status_code}")
        blob_sha = blob_resp.json()["sha"]

        # 4. Create a tree containing the patch file
        tree_resp = await client.post(
            f"{api}/git/trees",
            headers=headers,
            json={
                "base_tree": base_tree_sha,
                "tree": [
                    {
                        "path": "haunter.patch",
                        "mode": "100644",
                        "type": "blob",
                        "sha": blob_sha,
                    }
                ],
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
            raise GitHubPRError(f"Failed to create commit: HTTP {new_commit_resp.status_code}")
        new_commit_sha = new_commit_resp.json()["sha"]

        # 6. Update the branch ref — force=False (default for PATCH)
        update_resp = await client.patch(
            f"{api}/git/refs/heads/{branch}",
            headers=headers,
            json={"sha": new_commit_sha, "force": False},
        )
        if update_resp.is_error:
            raise GitHubPRError(
                f"Failed to update ref heads/{branch}: HTTP {update_resp.status_code}"
            )

    logger.info(
        "github.pr: committed patch to %s/%s:%s new_sha=%s",
        owner, repo, branch, new_commit_sha[:8],
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
    _validate_branch(head_branch)
    _validate_branch(base_branch)

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
            resp = await client.post(url, headers=_build_auth_headers(token), json=payload)
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
        data["number"], owner, repo, head_branch,
    )
    return {"html_url": data["html_url"], "number": data["number"]}
