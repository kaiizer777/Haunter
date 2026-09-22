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


async def fetch_repo_tree_paths(
    owner: str,
    repo: str,
    sha: str,
    token: Optional[str] = None,
    max_paths: int = 60,
) -> list[str]:
    """
    Fetch repository file tree paths via Git Trees API (recursive).
    Filters out hidden directories/files (.git, .github) and common vendor dirs.
    Returns prioritized source files up to max_paths.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/trees/{sha}?recursive=1"
    headers = _build_headers(token=token, accept="application/vnd.github+json")

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True) as client:
        try:
            response = await client.get(url, headers=headers)
        except httpx.RequestError as exc:
            logger.error("Network error fetching git tree for %s/%s @ %s: %s", owner, repo, sha, exc)
            return []

    if response.is_error:
        logger.warning("GitHub API error fetching git tree for %s/%s @ %s: %s", owner, repo, sha, response.status_code)
        return []

    data = response.json()
    tree = data.get("tree", [])
    paths = []
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
    _INTERESTING_EXTS = (
        ".py",
        ".ts",
        ".js",
        ".tsx",
        ".jsx",
        ".go",
        ".rs",
        ".java",
        ".rb",
        ".json",
        ".toml",
        ".yaml",
        ".yml",
        ".ini",
        ".cfg",
    )

    for item in tree:
        if item.get("type") != "blob":
            continue
        p = item.get("path", "")
        if any(p.startswith(ign) or f"/{ign}" in f"/{p}" for ign in _IGNORE_PREFIXES):
            continue
        if p.endswith(_INTERESTING_EXTS) or "." not in p:
            paths.append(p)
            if len(paths) >= max_paths:
                break

    return paths


async def fetch_file_content(
    owner: str,
    repo: str,
    path: str,
    sha: str,
    token: Optional[str] = None,
) -> Optional[str]:
    """
    Fetch raw file content from GitHub at a specific commit SHA.
    Returns plain text or None on 404 / errors.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{path}?ref={sha}"
    headers = _build_headers(token=token, accept="application/vnd.github.v3.raw")

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True) as client:
        try:
            response = await client.get(url, headers=headers)
        except httpx.RequestError as exc:
            logger.warning("Network error fetching file %s for %s/%s @ %s: %s", path, owner, repo, sha, exc)
            return None

    if response.status_code == 404:
        logger.debug("File %s not found in %s/%s @ %s", path, owner, repo, sha)
        return None
    if response.is_error:
        logger.warning(
            "GitHub API error (%s) fetching file %s for %s/%s @ %s",
            response.status_code,
            path,
            owner,
            repo,
            sha,
        )
        return None

    return response.text


async def fetch_pr_comments(
    owner: str,
    repo: str,
    pr_number: int,
    token: Optional[str] = None,
) -> list[dict[str, Any]]:
    """
    Fetch all comments on a pull request / issue thread via GitHub Issues API.
    GET /repos/{owner}/{repo}/issues/{pr_number}/comments
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/issues/{pr_number}/comments"
    headers = _build_headers(token=token, accept="application/vnd.github+json")

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True) as client:
        try:
            response = await client.get(url, headers=headers)
        except httpx.RequestError as exc:
            logger.error("Network error fetching PR comments for %s/%s PR #%s", owner, repo, pr_number)
            raise GitHubClientError(f"Network error connecting to GitHub: {exc.__class__.__name__}") from exc

    if response.status_code == 404:
        raise GitHubResourceNotFoundError(f"PR comments not found for {owner}/{repo} PR #{pr_number}")
    if response.status_code in (401, 403):
        if "rate limit" in response.text.lower():
            raise GitHubRateLimitError("GitHub API rate limit exceeded")
        raise GitHubAuthError(f"GitHub authentication failure ({response.status_code})")
    if response.is_error:
        raise GitHubClientError(f"GitHub API returned error {response.status_code}")

    return response.json()


async def post_pr_comment(
    owner: str,
    repo: str,
    pr_number: int,
    body: str,
    token: Optional[str] = None,
) -> dict[str, Any]:
    """
    Post a comment to a pull request / issue thread via GitHub Issues API.
    POST /repos/{owner}/{repo}/issues/{pr_number}/comments
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/issues/{pr_number}/comments"
    headers = _build_headers(token=token, accept="application/vnd.github+json")

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True) as client:
        try:
            response = await client.post(url, headers=headers, json={"body": body})
        except httpx.RequestError as exc:
            logger.error("Network error posting PR comment for %s/%s PR #%s", owner, repo, pr_number)
            raise GitHubClientError(f"Network error connecting to GitHub: {exc.__class__.__name__}") from exc

    if response.status_code == 404:
        raise GitHubResourceNotFoundError(f"PR not found for {owner}/{repo} PR #{pr_number} to post comment")
    if response.status_code in (401, 403):
        if "rate limit" in response.text.lower():
            raise GitHubRateLimitError("GitHub API rate limit exceeded")
        raise GitHubAuthError(f"GitHub authentication failure ({response.status_code})")
    if response.is_error:
        raise GitHubClientError(f"GitHub API returned error {response.status_code}")

    return response.json()


async def fetch_pull_request(
    owner: str,
    repo: str,
    pr_number: int,
    token: Optional[str] = None,
) -> dict[str, Any]:
    """
    Fetch pull request metadata (head branch, base branch, head SHA) via GitHub Pulls API.
    GET /repos/{owner}/{repo}/pulls/{pr_number}
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}"
    headers = _build_headers(token=token, accept="application/vnd.github+json")

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True) as client:
        try:
            response = await client.get(url, headers=headers)
        except httpx.RequestError as exc:
            logger.error("Network error fetching pull request %s/%s PR #%s", owner, repo, pr_number)
            raise GitHubClientError(f"Network error connecting to GitHub: {exc.__class__.__name__}") from exc

    if response.status_code == 404:
        raise GitHubResourceNotFoundError(f"Pull request not found for {owner}/{repo} PR #{pr_number}")
    if response.status_code in (401, 403):
        if "rate limit" in response.text.lower():
            raise GitHubRateLimitError("GitHub API rate limit exceeded")
        raise GitHubAuthError(f"GitHub authentication failure ({response.status_code})")
    if response.is_error:
        raise GitHubClientError(f"GitHub API returned error {response.status_code}")

    return response.json()


async def create_pull_request_review(
    owner: str,
    repo: str,
    pr_number: int,
    commit_sha: str,
    body: str,
    comments: list[dict[str, Any]],
    event: str = "COMMENT",
    token: Optional[str] = None,
) -> dict[str, Any]:
    """
    Submit a formal pull request review via GitHub Pulls API.
    POST /repos/{owner}/{repo}/pulls/{pr_number}/reviews
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
    headers = _build_headers(token=token, accept="application/vnd.github+json")
    payload = {
        "commit_id": commit_sha,
        "body": body,
        "event": event,
        "comments": comments,
    }

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True) as client:
        try:
            response = await client.post(url, headers=headers, json=payload)
        except httpx.RequestError as exc:
            logger.error("Network error submitting PR review for %s/%s PR #%s", owner, repo, pr_number)
            raise GitHubClientError(f"Network error connecting to GitHub: {exc.__class__.__name__}") from exc

    if response.status_code == 404:
        raise GitHubResourceNotFoundError(f"PR not found for {owner}/{repo} PR #{pr_number} to submit review")
    if response.status_code in (401, 403):
        if "rate limit" in response.text.lower():
            raise GitHubRateLimitError("GitHub API rate limit exceeded")
        raise GitHubAuthError(f"GitHub authentication failure ({response.status_code})")
    if response.is_error:
        logger.error("GitHub API error %d submitting review: %s", response.status_code, response.text)
        raise GitHubClientError(f"GitHub API returned error {response.status_code}: {response.text}")

    return response.json()


async def create_commit_comment(
    owner: str,
    repo: str,
    commit_sha: str,
    body: str,
    token: Optional[str] = None,
) -> dict[str, Any]:
    """
    Create a comment on a commit. Wraps post_commit_comment.
    """
    return await post_commit_comment(owner=owner, repo=repo, sha=commit_sha, body=body, token=token)


async def fetch_pull_request_diff(
    owner: str,
    repo: str,
    pr_number: int,
    token: Optional[str] = None,
) -> str:
    """
    Fetch the unified git diff for a pull request.
    GET /repos/{owner}/{repo}/pulls/{pr_number} with diff accept header.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}"
    headers = _build_headers(token=token, accept="application/vnd.github.v3.diff")

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True) as client:
        try:
            response = await client.get(url, headers=headers)
        except httpx.RequestError as exc:
            logger.error("Network error fetching PR diff for %s/%s PR #%s", owner, repo, pr_number)
            raise GitHubClientError(f"Network error connecting to GitHub: {exc.__class__.__name__}") from exc

    if response.status_code == 404:
        raise GitHubResourceNotFoundError(f"Pull request diff not found for {owner}/{repo} PR #{pr_number}")
    if response.status_code in (401, 403):
        if "rate limit" in response.text.lower():
            raise GitHubRateLimitError("GitHub API rate limit exceeded")
        raise GitHubAuthError(f"GitHub authentication failure ({response.status_code})")
    if response.is_error:
        raise GitHubClientError(f"GitHub API returned error {response.status_code}")

    return response.text




async def fetch_git_tree(
    owner: str,
    repo: str,
    tree_sha: str,
    recursive: bool = True,
    token=None,
) -> "dict[str, Any]":
    """
    Fetch the full git tree for a given commit or tree SHA via GitHub Git Data API.

    GET /repos/{owner}/{repo}/git/trees/{tree_sha}?recursive=1

    Returns the raw GitHub response dict containing a "tree" list of blob/tree entries.
    Callers are responsible for filtering (binary blobs, ignored dirs, etc.).

    Raises:
        GitHubResourceNotFoundError: SHA or repo not found (404).
        GitHubRateLimitError: API rate limit exceeded (403/429 with rate-limit body).
        GitHubAuthError: Authentication failure (401/403 without rate-limit body).
        GitHubClientError: Network errors or other API failures.
    """
    params = "?recursive=1" if recursive else ""
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/trees/{tree_sha}{params}"
    headers = _build_headers(token=token, accept="application/vnd.github+json")

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True) as client:
        try:
            response = await client.get(url, headers=headers)
        except httpx.RequestError as exc:
            logger.error("Network error fetching git tree for %s/%s @ %s", owner, repo, tree_sha)
            raise GitHubClientError(f"Network error connecting to GitHub: {exc.__class__.__name__}") from exc

    if response.status_code == 404:
        raise GitHubResourceNotFoundError(f"Git tree not found for {owner}/{repo} @ {tree_sha}")
    if response.status_code in (401, 403):
        if "rate limit" in response.text.lower():
            raise GitHubRateLimitError("GitHub API rate limit exceeded")
        raise GitHubAuthError(f"GitHub authentication failure ({response.status_code})")
    if response.status_code == 429:
        raise GitHubRateLimitError("GitHub API rate limit exceeded (429)")
    if response.is_error:
        raise GitHubClientError(f"GitHub API returned error {response.status_code}")

    return response.json()


async def fetch_branch_sha(
    owner: str,
    repo: str,
    branch: str,
    token=None,
) -> str:
    """
    Fetch the current HEAD commit SHA for a given branch.

    GET /repos/{owner}/{repo}/branches/{branch}

    Returns the 40-character commit SHA string.

    Raises:
        GitHubResourceNotFoundError: Branch or repo not found (404).
        GitHubRateLimitError: API rate limit exceeded.
        GitHubAuthError: Authentication failure (401/403).
        GitHubClientError: Network errors, missing commit data, or other API failures.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/branches/{branch}"
    headers = _build_headers(token=token, accept="application/vnd.github+json")

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True) as client:
        try:
            response = await client.get(url, headers=headers)
        except httpx.RequestError as exc:
            logger.error("Network error fetching branch SHA for %s/%s branch %s", owner, repo, branch)
            raise GitHubClientError(f"Network error connecting to GitHub: {exc.__class__.__name__}") from exc

    if response.status_code == 404:
        raise GitHubResourceNotFoundError(f"Branch '{branch}' not found for {owner}/{repo}")
    if response.status_code in (401, 403):
        if "rate limit" in response.text.lower():
            raise GitHubRateLimitError("GitHub API rate limit exceeded")
        raise GitHubAuthError(f"GitHub authentication failure ({response.status_code})")
    if response.status_code == 429:
        raise GitHubRateLimitError("GitHub API rate limit exceeded (429)")
    if response.is_error:
        raise GitHubClientError(f"GitHub API returned error {response.status_code}")

    data = response.json()
    try:
        sha: str = data["commit"]["sha"]
    except (KeyError, TypeError) as exc:
        raise GitHubClientError(
            f"Unexpected branch API response structure for {owner}/{repo} branch '{branch}'"
        ) from exc

    return sha


# ---------------------------------------------------------------------------
# Git Data API -- used by the commit publisher (Phase 3 Cloud Agentic Session)
# ---------------------------------------------------------------------------


async def create_blob(
    owner: str,
    repo: str,
    content: str,
    encoding: str = "utf-8",
    installation_token=None,
) -> str:
    """
    Create a Git blob for a single file content.

    POST /repos/{owner}/{repo}/git/blobs
    Returns the blob SHA.

    Raises:
        GitHubAuthError: 401/403 from GitHub API.
        GitHubRateLimitError: Rate limit hit.
        GitHubClientError: Network errors or unexpected API failures.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/blobs"
    headers = _build_headers(token=installation_token, accept="application/vnd.github+json")
    payload = {"content": content, "encoding": encoding}

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True) as client:
        try:
            response = await client.post(url, headers=headers, json=payload)
        except httpx.RequestError as exc:
            logger.error("Network error creating blob for %s/%s", owner, repo)
            raise GitHubClientError(f"Network error connecting to GitHub: {exc.__class__.__name__}") from exc

    if response.status_code in (401, 403):
        if "rate limit" in response.text.lower():
            raise GitHubRateLimitError("GitHub API rate limit exceeded")
        raise GitHubAuthError(f"GitHub authentication failure ({response.status_code})")
    if response.status_code == 429:
        raise GitHubRateLimitError("GitHub API rate limit exceeded (429)")
    if response.is_error:
        raise GitHubClientError(f"GitHub API returned error {response.status_code}: {response.text[:200]}")

    data = response.json()
    sha: str = data["sha"]
    return sha


async def create_git_tree(
    owner: str,
    repo: str,
    tree: list,
    base_tree=None,
    installation_token=None,
) -> str:
    """
    Create a Git tree object.

    POST /repos/{owner}/{repo}/git/trees
    Returns the tree SHA.

    Each entry in tree must be a dict with: path, mode, type, sha.

    Raises:
        GitHubAuthError, GitHubRateLimitError, GitHubClientError.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/trees"
    headers = _build_headers(token=installation_token, accept="application/vnd.github+json")
    payload = {"tree": tree}
    if base_tree is not None:
        payload["base_tree"] = base_tree

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True) as client:
        try:
            response = await client.post(url, headers=headers, json=payload)
        except httpx.RequestError as exc:
            logger.error("Network error creating git tree for %s/%s", owner, repo)
            raise GitHubClientError(f"Network error connecting to GitHub: {exc.__class__.__name__}") from exc

    if response.status_code in (401, 403):
        if "rate limit" in response.text.lower():
            raise GitHubRateLimitError("GitHub API rate limit exceeded")
        raise GitHubAuthError(f"GitHub authentication failure ({response.status_code})")
    if response.status_code == 429:
        raise GitHubRateLimitError("GitHub API rate limit exceeded (429)")
    if response.is_error:
        raise GitHubClientError(f"GitHub API returned error {response.status_code}: {response.text[:200]}")

    data = response.json()
    sha: str = data["sha"]
    return sha


async def create_git_commit(
    owner: str,
    repo: str,
    message: str,
    tree_sha: str,
    parents: list,
    installation_token=None,
) -> str:
    """
    Create a Git commit object.

    POST /repos/{owner}/{repo}/git/commits
    Returns the commit SHA.

    Raises:
        GitHubAuthError, GitHubRateLimitError, GitHubClientError.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/commits"
    headers = _build_headers(token=installation_token, accept="application/vnd.github+json")
    payload = {"message": message, "tree": tree_sha, "parents": parents}

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True) as client:
        try:
            response = await client.post(url, headers=headers, json=payload)
        except httpx.RequestError as exc:
            logger.error("Network error creating git commit for %s/%s", owner, repo)
            raise GitHubClientError(f"Network error connecting to GitHub: {exc.__class__.__name__}") from exc

    if response.status_code in (401, 403):
        if "rate limit" in response.text.lower():
            raise GitHubRateLimitError("GitHub API rate limit exceeded")
        raise GitHubAuthError(f"GitHub authentication failure ({response.status_code})")
    if response.status_code == 429:
        raise GitHubRateLimitError("GitHub API rate limit exceeded (429)")
    if response.is_error:
        raise GitHubClientError(f"GitHub API returned error {response.status_code}: {response.text[:200]}")

    data = response.json()
    sha: str = data["sha"]
    return sha


async def update_branch_ref(
    owner: str,
    repo: str,
    branch: str,
    commit_sha: str,
    force: bool = False,
    installation_token=None,
) -> None:
    """
    Update a branch ref to point at a new commit SHA.

    PATCH /repos/{owner}/{repo}/git/refs/heads/{branch}

    Raises:
        GitHubResourceNotFoundError: Branch not found (404).
        GitHubAuthError, GitHubRateLimitError, GitHubClientError.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/refs/heads/{branch}"
    headers = _build_headers(token=installation_token, accept="application/vnd.github+json")
    payload = {"sha": commit_sha, "force": force}

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True) as client:
        try:
            response = await client.patch(url, headers=headers, json=payload)
        except httpx.RequestError as exc:
            logger.error("Network error updating branch ref for %s/%s branch %s", owner, repo, branch)
            raise GitHubClientError(f"Network error connecting to GitHub: {exc.__class__.__name__}") from exc

    if response.status_code == 404:
        raise GitHubResourceNotFoundError(f"Branch ref not found: {owner}/{repo}/heads/{branch}")
    if response.status_code in (401, 403):
        if "rate limit" in response.text.lower():
            raise GitHubRateLimitError("GitHub API rate limit exceeded")
        raise GitHubAuthError(f"GitHub authentication failure ({response.status_code})")
    if response.status_code == 429:
        raise GitHubRateLimitError("GitHub API rate limit exceeded (429)")
    if response.is_error:
        raise GitHubClientError(f"GitHub API returned error {response.status_code}: {response.text[:200]}")


async def create_pull_request(
    owner: str,
    repo: str,
    title: str,
    head: str,
    base: str,
    body=None,
    installation_token=None,
) -> "dict[str, Any]":
    """
    Open a pull request.

    POST /repos/{owner}/{repo}/pulls
    Returns the full PR dict (contains html_url, number, etc.).

    Raises:
        GitHubAuthError, GitHubRateLimitError, GitHubClientError.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls"
    headers = _build_headers(token=installation_token, accept="application/vnd.github+json")
    payload: dict = {"title": title, "head": head, "base": base}
    if body is not None:
        payload["body"] = body

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True) as client:
        try:
            response = await client.post(url, headers=headers, json=payload)
        except httpx.RequestError as exc:
            logger.error("Network error creating pull request for %s/%s", owner, repo)
            raise GitHubClientError(f"Network error connecting to GitHub: {exc.__class__.__name__}") from exc

    if response.status_code in (401, 403):
        if "rate limit" in response.text.lower():
            raise GitHubRateLimitError("GitHub API rate limit exceeded")
        raise GitHubAuthError(f"GitHub authentication failure ({response.status_code})")
    if response.status_code == 429:
        raise GitHubRateLimitError("GitHub API rate limit exceeded (429)")
    if response.is_error:
        raise GitHubClientError(f"GitHub API returned error {response.status_code}: {response.text[:200]}")

    return response.json()
