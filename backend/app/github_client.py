"""
GitHub REST API client wrapper.

Provides typed async helpers to fetch workflow logs, commit diffs, and commit metadata
via httpx.AsyncClient. Designed for Phase 4 & Phase 5 subagents (Context Gatherer, PR Writer).

Security guarantees:
- Never logs auth tokens or request Authorization headers.
- Never persists tokens in DB models or run step traces.
- Enforces strict timeouts and handles HTTP error statuses cleanly.
"""

import io
import logging
import zipfile
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
DEFAULT_TIMEOUT_SECONDS = 30.0


class GitHubClientError(Exception):
    """Base exception for GitHub client errors."""


class GitHubAuthError(GitHubClientError):
    """Raised on 401 Unauthorized or 403 Forbidden from GitHub API."""


class GitHubResourceNotFoundError(GitHubClientError):
    """Raised on 404 Not Found from GitHub API."""


class GitHubRateLimitError(GitHubClientError):
    """Raised when GitHub API rate limits are hit (403/429 with rate limit headers)."""


def _build_headers(token: Optional[str] = None, accept: str = "application/vnd.github+json") -> dict[str, str]:
    """
    Construct safe request headers for GitHub API calls.

    Token resolution: explicit parameter -> settings.github_token.
    """
    headers = {
        "Accept": accept,
        "User-Agent": "Haunter-Autonomous-Agent/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    resolved_token = token or settings.github_token
    if resolved_token:
        headers["Authorization"] = f"Bearer {resolved_token}"
    return headers


async def fetch_workflow_run_logs(
    owner: str,
    repo: str,
    run_id: int,
    token: Optional[str] = None,
) -> str:
    """
    Fetch and extract plain text logs for a GitHub Actions workflow run.

    GitHub returns a 302 redirect to an archive URL containing a zip of individual job logs.
    This helper downloads and unzips all log files into a consolidated text output.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/actions/runs/{run_id}/logs"
    headers = _build_headers(token=token)

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True) as client:
        try:
            response = await client.get(url, headers=headers)
        except httpx.RequestError as exc:
            logger.error("Network error fetching workflow logs for %s/%s run %s", owner, repo, run_id)
            raise GitHubClientError(f"Network error connecting to GitHub: {exc.__class__.__name__}") from exc

    if response.status_code == 404:
        raise GitHubResourceNotFoundError(f"Workflow run logs not found for {owner}/{repo} run {run_id}")
    if response.status_code in (401, 403):
        if "rate limit" in response.text.lower():
            raise GitHubRateLimitError("GitHub API rate limit exceeded")
        raise GitHubAuthError(f"GitHub authentication failure ({response.status_code})")
    if response.is_error:
        raise GitHubClientError(f"GitHub API returned error {response.status_code}")

    # GitHub workflow logs archive is a zip file.
    # Extract all .txt log files and concatenate them.
    content = response.content
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            log_parts = []
            for filename in sorted(zf.namelist()):
                if filename.endswith(".txt"):
                    file_bytes = zf.read(filename)
                    log_text = file_bytes.decode("utf-8", errors="replace")
                    log_parts.append(f"=== File: {filename} ===\n{log_text}")
            return "\n\n".join(log_parts)
    except zipfile.BadZipFile:
        # Fallback if response was plain text
        return response.text


async def fetch_diff(
    owner: str,
    repo: str,
    sha: str,
    base_sha: Optional[str] = None,
    token: Optional[str] = None,
) -> str:
    """
    Fetch the unified git diff for a single commit or between two commits.
    """
    if base_sha:
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/compare/{base_sha}...{sha}"
    else:
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/commits/{sha}"

    headers = _build_headers(token=token, accept="application/vnd.github.v3.diff")

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True) as client:
        try:
            response = await client.get(url, headers=headers)
        except httpx.RequestError as exc:
            logger.error("Network error fetching diff for %s/%s @ %s", owner, repo, sha)
            raise GitHubClientError(f"Network error connecting to GitHub: {exc.__class__.__name__}") from exc

    if response.status_code == 404:
        raise GitHubResourceNotFoundError(f"Commit/diff not found for {owner}/{repo} @ {sha}")
    if response.status_code in (401, 403):
        if "rate limit" in response.text.lower():
            raise GitHubRateLimitError("GitHub API rate limit exceeded")
        raise GitHubAuthError(f"GitHub authentication failure ({response.status_code})")
    if response.is_error:
        raise GitHubClientError(f"GitHub API returned error {response.status_code}")

    return response.text


async def fetch_commit_metadata(
    owner: str,
    repo: str,
    sha: str,
    token: Optional[str] = None,
) -> dict[str, Any]:
    """
    Fetch commit metadata (author, message, stats, touched files) as JSON.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/commits/{sha}"
    headers = _build_headers(token=token, accept="application/vnd.github+json")

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True) as client:
        try:
            response = await client.get(url, headers=headers)
        except httpx.RequestError as exc:
            logger.error("Network error fetching commit metadata for %s/%s @ %s", owner, repo, sha)
            raise GitHubClientError(f"Network error connecting to GitHub: {exc.__class__.__name__}") from exc

    if response.status_code == 404:
        raise GitHubResourceNotFoundError(f"Commit not found for {owner}/{repo} @ {sha}")
    if response.status_code in (401, 403):
        if "rate limit" in response.text.lower():
            raise GitHubRateLimitError("GitHub API rate limit exceeded")
        raise GitHubAuthError(f"GitHub authentication failure ({response.status_code})")
    if response.is_error:
        raise GitHubClientError(f"GitHub API returned error {response.status_code}")

    return response.json()


async def post_commit_comment(
    owner: str,
    repo: str,
    sha: str,
    body: str,
    token: Optional[str] = None,
) -> dict[str, Any]:
    """
    Post a comment on a specific commit.
    Used for fallback notifications when all fix attempts fail.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/commits/{sha}/comments"
    headers = _build_headers(token=token, accept="application/vnd.github+json")
    
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True) as client:
        try:
            response = await client.post(url, headers=headers, json={"body": body})
        except httpx.RequestError as exc:
            logger.error("Network error posting commit comment for %s/%s @ %s", owner, repo, sha)
            raise GitHubClientError(f"Network error connecting to GitHub: {exc.__class__.__name__}") from exc

    if response.status_code == 404:
        raise GitHubResourceNotFoundError(f"Commit not found for {owner}/{repo} @ {sha} to post comment")
    if response.status_code in (401, 403):
        if "rate limit" in response.text.lower():
            raise GitHubRateLimitError("GitHub API rate limit exceeded")
        raise GitHubAuthError(f"GitHub authentication failure ({response.status_code})")
    if response.is_error:
        raise GitHubClientError(f"GitHub API returned error {response.status_code}")

    return response.json()
