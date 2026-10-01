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
import json
import logging
import zipfile
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote

import httpx

from app.config import settings
from app.log_hygiene import sanitize_log_value


logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_TEXT_RESPONSE_BYTES = 2_000_000
MAX_API_RESPONSE_BYTES = 5_000_000
MAX_LOG_ARCHIVE_BYTES = 20_000_000
MAX_ZIP_ENTRIES = 1_000
MAX_ZIP_ENTRY_BYTES = 2_000_000
MAX_ZIP_TOTAL_BYTES = 20_000_000
MAX_ZIP_COMPRESSION_RATIO = 200.0
#: Content types that promise a zip container. A body that fails to unzip under
#: one of these is a broken or forged archive, never a log stream.
ARCHIVE_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "application/zip",
        "application/x-zip",
        "application/x-zip-compressed",
        "application/zip-compressed",
        "application/x-zip-compressed-multi-part",
    }
)


class GitHubClientError(Exception):
    """Base exception for GitHub client errors."""


class GitHubNetworkError(GitHubClientError):
    """Raised when GitHub cannot be reached."""


class GitHubAuthError(GitHubClientError):
    """Raised on 401 Unauthorized or 403 Forbidden from GitHub API."""


class GitHubResourceNotFoundError(GitHubClientError):
    """Raised on 404 Not Found from GitHub API."""


class GitHubRateLimitError(GitHubClientError):
    """Raised when GitHub API rate limits are hit (403/429 with rate limit headers)."""


class GitHubResponseLimitError(GitHubClientError):
    """Raised when a GitHub response exceeds a configured resource limit."""


class GitHubArchiveError(GitHubClientError):
    """Raised when a GitHub archive is unsafe or malformed."""


@dataclass(frozen=True)
class BoundedResponse:
    status_code: int
    headers: httpx.Headers
    content: bytes

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    @property
    def is_error(self) -> bool:
        return self.status_code >= 400

    def json(self) -> Any:
        return json.loads(self.content)


async def _bounded_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    params: Optional[dict[str, Any]] = None,
    max_bytes: int = MAX_API_RESPONSE_BYTES,
) -> BoundedResponse:
    content_length = None
    chunks = bytearray()
    async with client.stream(
        "GET", url, headers=headers, params=params, follow_redirects=True
    ) as response:
        raw_length = response.headers.get("content-length")
        if raw_length is not None:
            try:
                content_length = int(raw_length)
            except ValueError:
                content_length = None
        if content_length is not None and content_length > max_bytes:
            logger.warning(
                "github response_limit resource=http content_length=%d byte_cap=%d",
                content_length,
                max_bytes,
            )
            raise GitHubResponseLimitError(
                f"GitHub response exceeded {max_bytes} bytes"
            )
        async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
            if len(chunks) + len(chunk) > max_bytes:
                logger.warning(
                    "github response_limit resource=http streamed_bytes=%d byte_cap=%d",
                    len(chunks) + len(chunk),
                    max_bytes,
                )
                raise GitHubResponseLimitError(
                    f"GitHub response exceeded {max_bytes} bytes"
                )
            chunks.extend(chunk)
        return BoundedResponse(
            status_code=response.status_code,
            headers=response.headers,
            content=bytes(chunks),
        )


def _media_type(content_type: Optional[str]) -> str:
    """The bare media type of a Content-Type header, lowercased and bounded."""
    if not isinstance(content_type, str):
        return ""
    return content_type.split(";", 1)[0].strip().lower()[:120]


def _verified_text_log(content: bytes) -> str:
    """Decode a body as text, or raise when it is not verifiably text.

    The previous fallback decoded any non-zip body with `errors="replace"`, so a
    truncated archive, an HTML error page, or a binary blob all reached the
    auditor as "log output" carrying U+FFFD replacement characters and NUL
    bytes. The auditor grounds conclusions in that text, so the fallback now
    requires the bytes to actually be text: a strict UTF-8 decode with no NUL.
    """
    if b"\x00" in content:
        raise GitHubArchiveError("GitHub log response is binary, not text")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GitHubArchiveError("GitHub log response is not valid UTF-8 text") from exc


def _extract_log_archive(content: bytes, content_type: Optional[str] = None) -> str:
    """Unpack a GitHub Actions log archive, or return verified plain text.

    `content_type` is the media type the response declared. It decides what a
    body that will not unzip is allowed to be: a body advertised as a zip is a
    malformed archive and raises, because silently reinterpreting a corrupt
    archive as a log stream is how a truncated download turns into fabricated CI
    evidence. A text fallback is only permitted for a body that decodes as text.
    """
    if len(content) > MAX_LOG_ARCHIVE_BYTES:
        logger.warning(
            "github archive_limit resource=log_archive compressed_bytes=%d byte_cap=%d",
            len(content),
            MAX_LOG_ARCHIVE_BYTES,
        )
        raise GitHubResponseLimitError("GitHub log archive is too large")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ZIP_ENTRIES:
                logger.warning(
                    "github archive_limit resource=log_archive entries=%d entry_cap=%d",
                    len(entries),
                    MAX_ZIP_ENTRIES,
                )
                raise GitHubArchiveError("GitHub log archive has too many entries")
            declared_total = sum(entry.file_size for entry in entries)
            if declared_total > MAX_ZIP_TOTAL_BYTES:
                logger.warning(
                    "github archive_limit resource=log_archive expanded_bytes=%d byte_cap=%d",
                    declared_total,
                    MAX_ZIP_TOTAL_BYTES,
                )
                raise GitHubResponseLimitError(
                    "GitHub log archive expands beyond the size limit"
                )
            log_parts: list[str] = []
            actual_total = 0
            for entry in sorted(entries, key=lambda item: item.filename):
                # A zip entry name is chosen by whoever built the archive. It is
                # never interpolated into a log record or a log body without
                # going through the canonical log sanitizer first: a newline in
                # the name forges a second log line, and a multi-kilobyte name
                # floods the pipeline.
                entry_name = sanitize_log_value(entry.filename, 200)
                if entry.flag_bits & 0x1:
                    logger.warning(
                        "github archive_rejected resource=log_archive reason=encrypted filename=%r",
                        entry_name,
                    )
                    raise GitHubArchiveError("encrypted GitHub log archive entry")
                if entry.file_size > MAX_ZIP_ENTRY_BYTES:
                    logger.warning(
                        "github archive_limit resource=log_archive filename=%r expanded_bytes=%d byte_cap=%d",
                        entry_name,
                        entry.file_size,
                        MAX_ZIP_ENTRY_BYTES,
                    )
                    raise GitHubResponseLimitError(
                        "GitHub log archive entry is too large"
                    )
                if entry.file_size > 64 * 1024:
                    compressed = max(entry.compress_size, 1)
                    ratio = entry.file_size / compressed
                    if ratio > MAX_ZIP_COMPRESSION_RATIO:
                        logger.warning(
                            "github archive_rejected resource=log_archive reason=compression_ratio filename=%r ratio=%.2f ratio_cap=%.2f",
                            entry_name,
                            ratio,
                            MAX_ZIP_COMPRESSION_RATIO,
                        )
                        raise GitHubArchiveError(
                            "GitHub log archive compression ratio is unsafe"
                        )
                if not entry.filename.endswith(".txt"):
                    continue
                with archive.open(entry) as stream:
                    file_bytes = stream.read(MAX_ZIP_ENTRY_BYTES + 1)
                if len(file_bytes) > MAX_ZIP_ENTRY_BYTES:
                    logger.warning(
                        "github archive_limit resource=log_archive filename=%r expanded_bytes=%d byte_cap=%d",
                        entry_name,
                        len(file_bytes),
                        MAX_ZIP_ENTRY_BYTES,
                    )
                    raise GitHubResponseLimitError(
                        "GitHub log archive entry is too large"
                    )
                actual_total += len(file_bytes)
                if actual_total > MAX_ZIP_TOTAL_BYTES:
                    logger.warning(
                        "github archive_limit resource=log_archive expanded_bytes=%d byte_cap=%d",
                        actual_total,
                        MAX_ZIP_TOTAL_BYTES,
                    )
                    raise GitHubResponseLimitError(
                        "GitHub log archive expands beyond the size limit"
                    )
                log_text = file_bytes.decode("utf-8", errors="replace")
                log_parts.append(f"=== File: {entry_name} ===\n{log_text}")
            return "\n\n".join(log_parts)
    except zipfile.BadZipFile as exc:
        media_type = _media_type(content_type)
        logger.warning(
            "github archive_rejected resource=log_archive reason=not_a_zip content_type=%r bytes=%d",
            media_type or "absent",
            len(content),
        )
        if media_type in ARCHIVE_CONTENT_TYPES:
            raise GitHubArchiveError(
                "GitHub log archive is malformed for its declared content type"
            ) from exc
        return _verified_text_log(content)
    except (RuntimeError, OSError, NotImplementedError) as exc:
        logger.warning(
            "github archive_rejected resource=log_archive reason=read_error error_type=%s",
            type(exc).__name__,
        )
        raise GitHubArchiveError("GitHub log archive could not be read") from exc


def _build_headers(
    token: Optional[str] = None,
    accept: str = "application/vnd.github+json",
    allow_global_token: bool = True,
) -> dict[str, str]:
    """
    Construct safe request headers for GitHub API calls.

    Token resolution: explicit parameter -> settings.github_token, unless
    `allow_global_token` is False.

    `allow_global_token=False` is required by every auditor read path. The
    personal access token in settings is a full-scope credential; an auditor
    that silently fell back to it would run with more authority than the
    read-only installation token it just validated, which defeats the entire
    point of the separate read-only GitHub App.
    """
    headers = {
        "Accept": accept,
        "User-Agent": "Haunter-Autonomous-Agent/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if allow_global_token:
        resolved_token = token or settings.github_token
    else:
        if not isinstance(token, str) or not token.strip():
            raise GitHubAuthError(
                "An explicit read-scoped token is required for this request"
            )
        resolved_token = token
    if resolved_token:
        headers["Authorization"] = f"Bearer {resolved_token}"
    return headers


async def fetch_workflow_run_logs(
    owner: str,
    repo: str,
    run_id: int,
    token: Optional[str] = None,
    allow_global_token: bool = True,
) -> str:
    """
    Fetch and extract plain text logs for a GitHub Actions workflow run.

    GitHub returns a 302 redirect to an archive URL containing a zip of individual job logs.
    This helper downloads and unzips all log files into a consolidated text output.
    """
    url = (
        f"{GITHUB_API_BASE}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/actions/runs/{quote(str(run_id), safe='')}/logs"
    )
    headers = _build_headers(token=token, allow_global_token=allow_global_token)

    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True
    ) as client:
        try:
            response = await _bounded_get(
                client,
                url,
                headers=headers,
                max_bytes=MAX_LOG_ARCHIVE_BYTES,
            )
        except httpx.RequestError as exc:
            logger.error(
                "Network error fetching workflow logs for %s/%s run %s",
                owner,
                repo,
                run_id,
            )
            raise GitHubNetworkError(
                f"Network error connecting to GitHub: {exc.__class__.__name__}"
            ) from exc

    if response.status_code == 404:
        raise GitHubResourceNotFoundError(
            f"Workflow run logs not found for {owner}/{repo} run {run_id}"
        )
    if response.status_code in (401, 403):
        if "rate limit" in response.text.lower():
            raise GitHubRateLimitError("GitHub API rate limit exceeded")
        raise GitHubAuthError(f"GitHub authentication failure ({response.status_code})")
    if response.is_error:
        raise GitHubClientError(f"GitHub API returned error {response.status_code}")

    return _extract_log_archive(response.content, response.headers.get("content-type"))


async def fetch_diff(
    owner: str,
    repo: str,
    sha: str,
    base_sha: Optional[str] = None,
    token: Optional[str] = None,
    allow_global_token: bool = True,
) -> str:
    """
    Fetch the unified git diff for a single commit or between two commits.
    """
    if base_sha:
        url = (
            f"{GITHUB_API_BASE}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
            f"/compare/{quote(base_sha, safe='')}...{quote(sha, safe='')}"
        )
    else:
        url = (
            f"{GITHUB_API_BASE}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
            f"/commits/{quote(sha, safe='')}"
        )

    headers = _build_headers(
        token=token,
        accept="application/vnd.github.v3.diff",
        allow_global_token=allow_global_token,
    )

    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True
    ) as client:
        try:
            response = await _bounded_get(
                client,
                url,
                headers=headers,
                max_bytes=MAX_TEXT_RESPONSE_BYTES,
            )
        except httpx.RequestError as exc:
            logger.error("Network error fetching diff for %s/%s @ %s", owner, repo, sha)
            raise GitHubNetworkError(
                f"Network error connecting to GitHub: {exc.__class__.__name__}"
            ) from exc

    if response.status_code == 404:
        raise GitHubResourceNotFoundError(
            f"Commit/diff not found for {owner}/{repo} @ {sha}"
        )
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
    url = (
        f"{GITHUB_API_BASE}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/commits/{quote(sha, safe='')}"
    )
    headers = _build_headers(token=token, accept="application/vnd.github+json")

    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True
    ) as client:
        try:
            response = await _bounded_get(
                client,
                url,
                headers=headers,
                max_bytes=MAX_API_RESPONSE_BYTES,
            )
        except httpx.RequestError as exc:
            logger.error(
                "Network error fetching commit metadata for %s/%s @ %s",
                owner,
                repo,
                sha,
            )
            raise GitHubNetworkError(
                f"Network error connecting to GitHub: {exc.__class__.__name__}"
            ) from exc

    if response.status_code == 404:
        raise GitHubResourceNotFoundError(
            f"Commit not found for {owner}/{repo} @ {sha}"
        )
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
    path: Optional[str] = None,
    position: Optional[int] = None,
    line: Optional[int] = None,
    token: Optional[str] = None,
    allow_global_token: bool = True,
) -> dict[str, Any]:
    """
    Post a comment on a specific commit.
    Used for fallback notifications when all fix attempts fail or for commit audits.
    """
    url = (
        f"{GITHUB_API_BASE}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/commits/{quote(sha, safe='')}/comments"
    )
    headers = _build_headers(
        token=token,
        accept="application/vnd.github+json",
        allow_global_token=allow_global_token,
    )
    payload: dict[str, Any] = {"body": body}
    if path is not None:
        payload["path"] = path
    if position is not None:
        payload["position"] = position
    if line is not None:
        payload["line"] = line

    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True
    ) as client:
        try:
            response = await client.post(url, headers=headers, json=payload)
        except httpx.RequestError as exc:
            logger.error(
                "Network error posting commit comment for %s/%s @ %s", owner, repo, sha
            )
            raise GitHubNetworkError(
                f"Network error connecting to GitHub: {exc.__class__.__name__}"
            ) from exc

    if response.status_code == 404:
        raise GitHubResourceNotFoundError(
            f"Commit not found for {owner}/{repo} @ {sha} to post comment"
        )
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

    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True
    ) as client:
        try:
            response = await _bounded_get(
                client,
                url,
                headers=headers,
                max_bytes=MAX_API_RESPONSE_BYTES,
            )
        except httpx.RequestError as exc:
            logger.error(
                "Network error fetching git tree for %s/%s @ %s: %s",
                owner,
                repo,
                sha,
                exc,
            )
            return []

    if response.is_error:
        logger.warning(
            "GitHub API error fetching git tree for %s/%s @ %s: %s",
            owner,
            repo,
            sha,
            response.status_code,
        )
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
    allow_global_token: bool = True,
) -> Optional[str]:
    """
    Fetch raw file content from GitHub at a specific commit SHA.
    Returns plain text or None on 404 / errors.
    """
    url = (
        f"{GITHUB_API_BASE}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/contents/{quote(path, safe='')}"
    )
    headers = _build_headers(
        token=token,
        accept="application/vnd.github.v3.raw",
        allow_global_token=allow_global_token,
    )

    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True
    ) as client:
        try:
            response = await _bounded_get(
                client,
                url,
                headers=headers,
                params={"ref": sha},
                max_bytes=MAX_TEXT_RESPONSE_BYTES,
            )
        except httpx.RequestError as exc:
            logger.warning(
                "Network error fetching file %s for %s/%s @ %s: %s",
                path,
                owner,
                repo,
                sha,
                exc,
            )
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

    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True
    ) as client:
        try:
            response = await _bounded_get(
                client,
                url,
                headers=headers,
                max_bytes=MAX_API_RESPONSE_BYTES,
            )
        except httpx.RequestError as exc:
            logger.error(
                "Network error fetching PR comments for %s/%s PR #%s",
                owner,
                repo,
                pr_number,
            )
            raise GitHubNetworkError(
                f"Network error connecting to GitHub: {exc.__class__.__name__}"
            ) from exc

    if response.status_code == 404:
        raise GitHubResourceNotFoundError(
            f"PR comments not found for {owner}/{repo} PR #{pr_number}"
        )
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

    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True
    ) as client:
        try:
            response = await client.post(url, headers=headers, json={"body": body})
        except httpx.RequestError as exc:
            logger.error(
                "Network error posting PR comment for %s/%s PR #%s",
                owner,
                repo,
                pr_number,
            )
            raise GitHubNetworkError(
                f"Network error connecting to GitHub: {exc.__class__.__name__}"
            ) from exc

    if response.status_code == 404:
        raise GitHubResourceNotFoundError(
            f"PR not found for {owner}/{repo} PR #{pr_number} to post comment"
        )
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
    allow_global_token: bool = True,
) -> dict[str, Any]:
    """
    Fetch pull request metadata (head branch, base branch, head SHA) via GitHub Pulls API.
    GET /repos/{owner}/{repo}/pulls/{pr_number}
    """
    url = (
        f"{GITHUB_API_BASE}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/pulls/{quote(str(pr_number), safe='')}"
    )
    headers = _build_headers(
        token=token,
        accept="application/vnd.github+json",
        allow_global_token=allow_global_token,
    )

    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True
    ) as client:
        try:
            response = await _bounded_get(
                client,
                url,
                headers=headers,
                max_bytes=MAX_API_RESPONSE_BYTES,
            )
        except httpx.RequestError as exc:
            logger.error(
                "Network error fetching pull request %s/%s PR #%s",
                owner,
                repo,
                pr_number,
            )
            raise GitHubNetworkError(
                f"Network error connecting to GitHub: {exc.__class__.__name__}"
            ) from exc

    if response.status_code == 404:
        raise GitHubResourceNotFoundError(
            f"Pull request not found for {owner}/{repo} PR #{pr_number}"
        )
    if response.status_code in (401, 403):
        if "rate limit" in response.text.lower():
            raise GitHubRateLimitError("GitHub API rate limit exceeded")
        raise GitHubAuthError(f"GitHub authentication failure ({response.status_code})")
    if response.is_error:
        raise GitHubClientError(f"GitHub API returned error {response.status_code}")

    return response.json()


async def create_pr_review(
    owner: str,
    repo: str,
    pr_number: int,
    commit_sha: str,
    body: str,
    comments: Optional[list[dict[str, Any]]] = None,
    event: str = "COMMENT",
    token: Optional[str] = None,
    allow_global_token: bool = True,
) -> dict[str, Any]:
    """
    Submit a formal pull request review via GitHub Pulls API.
    POST /repos/{owner}/{repo}/pulls/{pr_number}/reviews
    """
    url = (
        f"{GITHUB_API_BASE}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/pulls/{quote(str(pr_number), safe='')}/reviews"
    )
    headers = _build_headers(
        token=token,
        accept="application/vnd.github+json",
        allow_global_token=allow_global_token,
    )
    payload: dict[str, Any] = {
        "commit_id": commit_sha,
        "body": body,
        "event": event,
    }
    if comments is not None:
        payload["comments"] = comments

    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True
    ) as client:
        try:
            response = await client.post(url, headers=headers, json=payload)
        except httpx.RequestError as exc:
            logger.error(
                "Network error submitting PR review for %s/%s PR #%s",
                owner,
                repo,
                pr_number,
            )
            raise GitHubNetworkError(
                f"Network error connecting to GitHub: {exc.__class__.__name__}"
            ) from exc

    if response.status_code == 404:
        raise GitHubResourceNotFoundError(
            f"PR not found for {owner}/{repo} PR #{pr_number} to submit review"
        )
    if response.status_code in (401, 403):
        if "rate limit" in response.text.lower():
            raise GitHubRateLimitError("GitHub API rate limit exceeded")
        raise GitHubAuthError(f"GitHub authentication failure ({response.status_code})")
    if response.is_error:
        logger.error(
            "GitHub API error %d submitting review: %s",
            response.status_code,
            response.text,
        )
        raise GitHubClientError(
            f"GitHub API returned error {response.status_code}: {response.text}"
        )

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
    allow_global_token: bool = True,
) -> dict[str, Any]:
    """Submit a formal pull request review via GitHub Pulls API (alias for create_pr_review)."""
    return await create_pr_review(
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        commit_sha=commit_sha,
        body=body,
        comments=comments,
        event=event,
        token=token,
        allow_global_token=allow_global_token,
    )


async def create_commit_comment(
    owner: str,
    repo: str,
    commit_sha: str,
    body: str,
    path: Optional[str] = None,
    position: Optional[int] = None,
    line: Optional[int] = None,
    token: Optional[str] = None,
    allow_global_token: bool = True,
) -> dict[str, Any]:
    """
    Create a comment on a commit. Wraps post_commit_comment.
    """
    return await post_commit_comment(
        owner=owner,
        repo=repo,
        sha=commit_sha,
        body=body,
        path=path,
        position=position,
        line=line,
        token=token,
        allow_global_token=allow_global_token,
    )


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
    url = (
        f"{GITHUB_API_BASE}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/pulls/{quote(str(pr_number), safe='')}"
    )
    headers = _build_headers(token=token, accept="application/vnd.github.v3.diff")

    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True
    ) as client:
        try:
            response = await _bounded_get(
                client,
                url,
                headers=headers,
                max_bytes=MAX_TEXT_RESPONSE_BYTES,
            )
        except httpx.RequestError as exc:
            logger.error(
                "Network error fetching PR diff for %s/%s PR #%s",
                owner,
                repo,
                pr_number,
            )
            raise GitHubNetworkError(
                f"Network error connecting to GitHub: {exc.__class__.__name__}"
            ) from exc

    if response.status_code == 404:
        raise GitHubResourceNotFoundError(
            f"Pull request diff not found for {owner}/{repo} PR #{pr_number}"
        )
    if response.status_code in (401, 403):
        if "rate limit" in response.text.lower():
            raise GitHubRateLimitError("GitHub API rate limit exceeded")
        raise GitHubAuthError(f"GitHub authentication failure ({response.status_code})")
    if response.is_error:
        raise GitHubClientError(f"GitHub API returned error {response.status_code}")

    return response.text


async def fetch_commits(
    owner: str,
    repo: str,
    sha: str,
    path: Optional[str] = None,
    per_page: int = 20,
    token: Optional[str] = None,
) -> list[dict[str, Any]]:
    """
    List commits on a branch (optionally filtered to a file path).

    Maps to GET /repos/{owner}/{repo}/commits?sha=<sha>&path=<path>&per_page=<n>.
    Returns abbreviated commit dicts: {sha, message, author, date, url}.
    Capped at per_page (max 100).
    """
    per_page = max(1, min(per_page, 100))
    params: dict[str, Any] = {"sha": sha, "per_page": per_page}
    if path:
        params["path"] = path

    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/commits"
    headers = _build_headers(token=token, accept="application/vnd.github+json")

    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True
    ) as client:
        try:
            response = await _bounded_get(
                client,
                url,
                headers=headers,
                params=params,
                max_bytes=MAX_API_RESPONSE_BYTES,
            )
        except httpx.RequestError as exc:
            logger.error(
                "Network error fetching commits for %s/%s: %s", owner, repo, exc
            )
            raise GitHubNetworkError(
                f"Network error connecting to GitHub: {exc.__class__.__name__}"
            ) from exc

    if response.status_code == 404:
        raise GitHubResourceNotFoundError(
            f"Commits not found for {owner}/{repo} @ {sha}"
        )
    if response.status_code in (401, 403):
        if "rate limit" in response.text.lower():
            raise GitHubRateLimitError("GitHub API rate limit exceeded")
        raise GitHubAuthError(f"GitHub authentication failure ({response.status_code})")
    if response.is_error:
        raise GitHubClientError(f"GitHub API returned error {response.status_code}")

    raw: list[dict[str, Any]] = response.json()
    commits: list[dict[str, Any]] = []
    for item in raw:
        commit_data = item.get("commit", {})
        author_data = commit_data.get("author", {})
        full_message: str = commit_data.get("message", "")
        first_line = full_message.split("\n", 1)[0]
        commits.append(
            {
                "sha": item.get("sha", "")[:12],
                "full_sha": item.get("sha", ""),
                "message": first_line,
                "author": author_data.get("name", "unknown"),
                "date": author_data.get("date", ""),
                "url": item.get("html_url", ""),
            }
        )
    return commits


async def fetch_blame(
    owner: str,
    repo: str,
    path: str,
    ref: str,
    token: Optional[str] = None,
) -> list[dict[str, Any]]:
    """
    Fetch git blame annotations for a file via GitHub GraphQL API.

    Returns a list of blame range dicts:
      {start_line, end_line, sha, message, author, date, age_days}

    Falls back to an empty list on auth or network failure.
    """
    query = """
query Blame($owner: String!, $repo: String!, $ref: String!, $path: String!) {
  repository(owner: $owner, name: $repo) {
    object(expression: $ref) {
      ... on Commit {
        blame(path: $path) {
          ranges {
            startingLine
            endingLine
            commit {
              oid
              messageHeadline
              committedDate
              author { name }
            }
          }
        }
      }
    }
  }
}
"""
    payload = {
        "query": query,
        "variables": {"owner": owner, "repo": repo, "ref": ref, "path": path},
    }
    resolved_token = token or settings.github_token
    gql_headers = {
        "Accept": "application/json",
        "User-Agent": "Haunter-Autonomous-Agent/1.0",
        "Content-Type": "application/json",
    }
    if resolved_token:
        gql_headers["Authorization"] = f"Bearer {resolved_token}"

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
        try:
            response = await client.post(
                "https://api.github.com/graphql", headers=gql_headers, json=payload
            )
        except httpx.RequestError as exc:
            logger.warning(
                "Network error fetching blame for %s/%s %s: %s", owner, repo, path, exc
            )
            return []

    if response.is_error:
        logger.warning(
            "GitHub GraphQL error %s fetching blame for %s/%s %s",
            response.status_code,
            owner,
            repo,
            path,
        )
        return []

    data = response.json()
    if data.get("errors"):
        logger.warning(
            "GitHub GraphQL blame errors for %s/%s %s: %s",
            owner,
            repo,
            path,
            data["errors"],
        )
        return []

    try:
        ranges_raw: list[dict[str, Any]] = data["data"]["repository"]["object"][
            "blame"
        ]["ranges"]
    except (KeyError, TypeError):
        return []

    import datetime as _dt

    now = _dt.datetime.now(_dt.timezone.utc)
    result: list[dict[str, Any]] = []
    for r in ranges_raw:
        commit = r.get("commit", {})
        committed_date_str: str = commit.get("committedDate", "")
        age_days: Optional[int] = None
        if committed_date_str:
            try:
                committed_dt = _dt.datetime.fromisoformat(
                    committed_date_str.replace("Z", "+00:00")
                )
                age_days = (now - committed_dt).days
            except ValueError:
                pass
        result.append(
            {
                "start_line": r.get("startingLine"),
                "end_line": r.get("endingLine"),
                "sha": commit.get("oid", "")[:12],
                "message": commit.get("messageHeadline", ""),
                "author": (commit.get("author") or {}).get("name", "unknown"),
                "date": committed_date_str,
                "age_days": age_days,
            }
        )
    return result


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

    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True
    ) as client:
        try:
            response = await _bounded_get(
                client,
                url,
                headers=headers,
                max_bytes=MAX_API_RESPONSE_BYTES,
            )
        except httpx.RequestError as exc:
            logger.error(
                "Network error fetching git tree for %s/%s @ %s", owner, repo, tree_sha
            )
            raise GitHubNetworkError(
                f"Network error connecting to GitHub: {exc.__class__.__name__}"
            ) from exc

    if response.status_code == 404:
        raise GitHubResourceNotFoundError(
            f"Git tree not found for {owner}/{repo} @ {tree_sha}"
        )
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

    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True
    ) as client:
        try:
            response = await _bounded_get(
                client,
                url,
                headers=headers,
                max_bytes=MAX_API_RESPONSE_BYTES,
            )
        except httpx.RequestError as exc:
            logger.error(
                "Network error fetching branch SHA for %s/%s branch %s",
                owner,
                repo,
                branch,
            )
            raise GitHubNetworkError(
                f"Network error connecting to GitHub: {exc.__class__.__name__}"
            ) from exc

    if response.status_code == 404:
        raise GitHubResourceNotFoundError(
            f"Branch '{branch}' not found for {owner}/{repo}"
        )
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
    headers = _build_headers(
        token=installation_token, accept="application/vnd.github+json"
    )
    payload = {"content": content, "encoding": encoding}

    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True
    ) as client:
        try:
            response = await client.post(url, headers=headers, json=payload)
        except httpx.RequestError as exc:
            logger.error("Network error creating blob for %s/%s", owner, repo)
            raise GitHubNetworkError(
                f"Network error connecting to GitHub: {exc.__class__.__name__}"
            ) from exc

    if response.status_code in (401, 403):
        if "rate limit" in response.text.lower():
            raise GitHubRateLimitError("GitHub API rate limit exceeded")
        raise GitHubAuthError(f"GitHub authentication failure ({response.status_code})")
    if response.status_code == 429:
        raise GitHubRateLimitError("GitHub API rate limit exceeded (429)")
    if response.is_error:
        raise GitHubClientError(
            f"GitHub API returned error {response.status_code}: {response.text[:200]}"
        )

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
    headers = _build_headers(
        token=installation_token, accept="application/vnd.github+json"
    )
    payload = {"tree": tree}
    if base_tree is not None:
        payload["base_tree"] = base_tree

    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True
    ) as client:
        try:
            response = await client.post(url, headers=headers, json=payload)
        except httpx.RequestError as exc:
            logger.error("Network error creating git tree for %s/%s", owner, repo)
            raise GitHubNetworkError(
                f"Network error connecting to GitHub: {exc.__class__.__name__}"
            ) from exc

    if response.status_code in (401, 403):
        if "rate limit" in response.text.lower():
            raise GitHubRateLimitError("GitHub API rate limit exceeded")
        raise GitHubAuthError(f"GitHub authentication failure ({response.status_code})")
    if response.status_code == 429:
        raise GitHubRateLimitError("GitHub API rate limit exceeded (429)")
    if response.is_error:
        raise GitHubClientError(
            f"GitHub API returned error {response.status_code}: {response.text[:200]}"
        )

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
    headers = _build_headers(
        token=installation_token, accept="application/vnd.github+json"
    )
    payload = {"message": message, "tree": tree_sha, "parents": parents}

    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True
    ) as client:
        try:
            response = await client.post(url, headers=headers, json=payload)
        except httpx.RequestError as exc:
            logger.error("Network error creating git commit for %s/%s", owner, repo)
            raise GitHubNetworkError(
                f"Network error connecting to GitHub: {exc.__class__.__name__}"
            ) from exc

    if response.status_code in (401, 403):
        if "rate limit" in response.text.lower():
            raise GitHubRateLimitError("GitHub API rate limit exceeded")
        raise GitHubAuthError(f"GitHub authentication failure ({response.status_code})")
    if response.status_code == 429:
        raise GitHubRateLimitError("GitHub API rate limit exceeded (429)")
    if response.is_error:
        raise GitHubClientError(
            f"GitHub API returned error {response.status_code}: {response.text[:200]}"
        )

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
    headers = _build_headers(
        token=installation_token, accept="application/vnd.github+json"
    )
    payload = {"sha": commit_sha, "force": force}

    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True
    ) as client:
        try:
            response = await client.patch(url, headers=headers, json=payload)
        except httpx.RequestError as exc:
            logger.error(
                "Network error updating branch ref for %s/%s branch %s",
                owner,
                repo,
                branch,
            )
            raise GitHubNetworkError(
                f"Network error connecting to GitHub: {exc.__class__.__name__}"
            ) from exc

    if response.status_code == 404:
        raise GitHubResourceNotFoundError(
            f"Branch ref not found: {owner}/{repo}/heads/{branch}"
        )
    if response.status_code in (401, 403):
        if "rate limit" in response.text.lower():
            raise GitHubRateLimitError("GitHub API rate limit exceeded")
        raise GitHubAuthError(f"GitHub authentication failure ({response.status_code})")
    if response.status_code == 429:
        raise GitHubRateLimitError("GitHub API rate limit exceeded (429)")
    if response.is_error:
        raise GitHubClientError(
            f"GitHub API returned error {response.status_code}: {response.text[:200]}"
        )


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
    headers = _build_headers(
        token=installation_token, accept="application/vnd.github+json"
    )
    payload: dict = {"title": title, "head": head, "base": base}
    if body is not None:
        payload["body"] = body

    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True
    ) as client:
        try:
            response = await client.post(url, headers=headers, json=payload)
        except httpx.RequestError as exc:
            logger.error("Network error creating pull request for %s/%s", owner, repo)
            raise GitHubNetworkError(
                f"Network error connecting to GitHub: {exc.__class__.__name__}"
            ) from exc

    if response.status_code in (401, 403):
        if "rate limit" in response.text.lower():
            raise GitHubRateLimitError("GitHub API rate limit exceeded")
        raise GitHubAuthError(f"GitHub authentication failure ({response.status_code})")
    if response.status_code == 429:
        raise GitHubRateLimitError("GitHub API rate limit exceeded (429)")
    if response.is_error:
        raise GitHubClientError(
            f"GitHub API returned error {response.status_code}: {response.text[:200]}"
        )

    return response.json()


async def create_issue(
    owner: str,
    repo: str,
    title: str,
    body: str,
    labels: Optional[list[str]] = None,
    token: Optional[str] = None,
    allow_global_token: bool = True,
) -> dict[str, Any]:
    """
    Open a GitHub issue.

    Used by the orchestrator exhaust path (Feature 3: fallback to GitHub
    Issue) to file a tracking issue after all fix attempts fail verification.
    POST /repos/{owner}/{repo}/issues
    Returns the created issue dict (contains html_url, number, etc.).

    Raises:
        GitHubResourceNotFoundError: Repo not found (404).
        GitHubAuthError: Authentication failure (401/403).
        GitHubRateLimitError: API rate limit exceeded.
        GitHubClientError: Network errors, validation failures (422),
            or unexpected response structures.
    """
    url = (
        f"{GITHUB_API_BASE}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        "/issues"
    )
    headers = _build_headers(
        token=token,
        accept="application/vnd.github+json",
        allow_global_token=allow_global_token,
    )
    payload: dict[str, Any] = {"title": title, "body": body}
    if labels:
        payload["labels"] = list(labels)

    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True
    ) as client:
        try:
            response = await client.post(url, headers=headers, json=payload)
        except httpx.RequestError as exc:
            logger.error("Network error creating issue for %s/%s", owner, repo)
            raise GitHubNetworkError(
                f"Network error connecting to GitHub: {exc.__class__.__name__}"
            ) from exc

    if response.status_code == 404:
        raise GitHubResourceNotFoundError(
            f"Repo not found for {owner}/{repo} to create issue"
        )
    if response.status_code in (401, 403):
        if "rate limit" in response.text.lower():
            raise GitHubRateLimitError("GitHub API rate limit exceeded")
        raise GitHubAuthError(f"GitHub authentication failure ({response.status_code})")
    if response.status_code == 429:
        raise GitHubRateLimitError("GitHub API rate limit exceeded (429)")
    if response.is_error:
        raise GitHubClientError(
            f"GitHub API returned error {response.status_code}: {response.text[:200]}"
        )

    data = response.json()
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("html_url"), str)
        or not isinstance(data.get("number"), int)
    ):
        raise GitHubClientError(
            "Unexpected create-issue response structure from GitHub API"
        )
    return data
