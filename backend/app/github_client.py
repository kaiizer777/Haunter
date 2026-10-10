"""
GitHub REST API client wrapper.

Provides typed async helpers to fetch workflow logs, commit diffs, and commit metadata
via httpx.AsyncClient. Designed for Phase 4 & Phase 5 subagents (Context Gatherer, PR Writer).

Security guarantees:
- Never logs auth tokens or request Authorization headers.
- Never persists tokens in DB models or run step traces.
- Enforces strict timeouts and handles HTTP error statuses cleanly.
"""

import hashlib
import io
import json
import logging
import time
import zipfile
from dataclasses import dataclass
from typing import Any, Optional, Sequence
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
#: Page size for the paginated PR review-comment fetch. 100 is the GitHub maximum.
REVIEW_COMMENTS_PER_PAGE = 100
#: Hard ceiling on review-comment pages walked per fetch, so an unbounded thread
#: cannot inflate the context window. 5 pages x 100 = 500 comments.
MAX_REVIEW_COMMENT_PAGES = 5
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


class GitHubUnprocessableEntityError(GitHubClientError):
    """Raised on 422 Unprocessable Entity from GitHub API.

    The only 422 the write paths produce is "GitHub rejected this payload":
    a review comment anchored to a line outside the diff, or a body past
    GitHub's 65 536-character ceiling. It is the one GitHub status a caller can
    act on by *changing the request* (drop the inline comments, clamp the body),
    which is why it is a distinct type rather than the base error: retrying a
    422 unchanged cannot succeed, while retrying an auth or network failure
    sometimes can. Additive — every existing ``except GitHubClientError`` still
    catches it.
    """


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


async def _bounded_post(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    json: dict[str, Any],
    max_bytes: int = MAX_API_RESPONSE_BYTES,
) -> BoundedResponse:
    """POST a JSON body with the same byte ceiling :func:`_bounded_get` applies.

    A write path has no smaller response than a read path: a GraphQL endpoint
    answers with an errors array whose messages come from the upstream service,
    and a diff/media endpoint with a body this module streams. Binding the cap at
    the transport rather than at the call site means no future caller can opt out
    of it by accident.

    Content-Length is treated as a hint, not a guarantee — it is checked first so
    an oversized body is refused before it is transferred, but the streamed total
    is what actually decides, because that is the only number a lying or absent
    header cannot understate.
    """
    content_length = None
    chunks = bytearray()
    async with client.stream("POST", url, headers=headers, json=json) as response:
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


def _parse_next_page_url(headers: httpx.Headers) -> Optional[str]:
    """Return the RFC5988 ``Link: rel="next"`` target, or ``None``.

    Takes raw headers rather than an ``httpx.Response`` so it also works with
    :class:`BoundedResponse` (a streamed response has no ``.links`` cache).

    Only ``https://api.github.com`` next links are honoured. The header is
    attacker-influenceable in principle and is used to build the next request
    URL, so an off-host ``next`` is discarded instead of followed.
    """
    link_header = headers.get("link")
    if not link_header:
        return None
    for part in link_header.split(","):
        sections = part.strip().split(";")
        if len(sections) < 2:
            continue
        url_part = sections[0].strip()
        if sections[1].strip() != 'rel="next"':
            continue
        if not (url_part.startswith("<") and url_part.endswith(">")):
            continue
        candidate = url_part[1:-1]
        if candidate.startswith(f"{GITHUB_API_BASE}/"):
            return candidate
        logger.warning(
            "Discarded off-host GitHub Link: rel=next target (%d chars)",
            len(candidate),
        )
        return None
    return None


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


async def create_issue_comment(
    owner: str,
    repo: str,
    issue_number: int,
    body: str,
    token: Optional[str] = None,
) -> dict[str, Any]:
    """
    Create a comment on an issue or pull request via GitHub Issues API.
    POST /repos/{owner}/{repo}/issues/{issue_number}/comments
    """
    return await post_pr_comment(
        owner=owner, repo=repo, pr_number=issue_number, body=body, token=token
    )


async def fetch_pull_request_reviews(
    owner: str,
    repo: str,
    pr_number: int,
    token: Optional[str] = None,
) -> list[dict[str, Any]]:
    """
    Fetch pull request reviews via GitHub Pulls API.
    GET /repos/{owner}/{repo}/pulls/{pr_number}/reviews
    """
    url = (
        f"{GITHUB_API_BASE}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/pulls/{quote(str(pr_number), safe='')}/reviews"
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
                "Network error fetching PR reviews for %s/%s PR #%s",
                owner,
                repo,
                pr_number,
            )
            raise GitHubNetworkError(
                f"Network error connecting to GitHub: {exc.__class__.__name__}"
            ) from exc

    if response.status_code == 404:
        raise GitHubResourceNotFoundError(
            f"PR reviews not found for {owner}/{repo} PR #{pr_number}"
        )
    if response.status_code in (401, 403):
        if "rate limit" in response.text.lower():
            raise GitHubRateLimitError("GitHub API rate limit exceeded")
        raise GitHubAuthError(f"GitHub authentication failure ({response.status_code})")
    if response.is_error:
        raise GitHubClientError(f"GitHub API returned error {response.status_code}")

    try:
        data = response.json()
        if isinstance(data, list):
            return data
        return []
    except Exception as exc:
        raise GitHubClientError("Invalid JSON returned for PR reviews") from exc


async def fetch_pr_review_comments(
    owner: str,
    repo: str,
    pr_number: int,
    token: Optional[str] = None,
) -> list[dict[str, Any]]:
    """
    Fetch inline review comments on a pull request (review thread context).

    GET /repos/{owner}/{repo}/pulls/{pr_number}/comments

    Complements :func:`fetch_pr_comments` (Issues API conversation thread)
    with the diff-anchored review thread — path, line, diff_hunk, and
    ``in_reply_to_id`` linkage used by the conversational follow-up router.

    Paginated, because the consumer keeps only the *newest* comments while
    GitHub returns results oldest-first: without following ``Link`` a PR with
    more than one page of review comments would silently drop the most recent
    ``@haunter`` instruction, and the fix generator would act on a stale one.
    Capped at :data:`MAX_REVIEW_COMMENT_PAGES` pages so a pathological thread
    cannot inflate context without bound.

    Returns an empty list when the PR has no review comments. Raises the
    standard typed errors (auth / rate-limit / network) on failure so the
    caller can degrade gracefully to the issue-thread context alone.
    """
    url = (
        f"{GITHUB_API_BASE}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/pulls/{quote(str(pr_number), safe='')}/comments"
    )
    headers = _build_headers(token=token, accept="application/vnd.github+json")

    collected: list[dict[str, Any]] = []
    next_url: Optional[str] = url
    pages = 0
    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True
    ) as client:
        while next_url and pages < MAX_REVIEW_COMMENT_PAGES:
            pages += 1
            try:
                response = await _bounded_get(
                    client,
                    next_url,
                    headers=headers,
                    params={"per_page": REVIEW_COMMENTS_PER_PAGE}
                    if pages == 1
                    else None,
                    max_bytes=MAX_API_RESPONSE_BYTES,
                )
            except httpx.RequestError as exc:
                logger.error(
                    "Network error fetching PR review comments for %s/%s PR #%s",
                    owner,
                    repo,
                    pr_number,
                )
                raise GitHubNetworkError(
                    f"Network error connecting to GitHub: {exc.__class__.__name__}"
                ) from exc

            if response.status_code == 404:
                raise GitHubResourceNotFoundError(
                    f"PR review comments not found for {owner}/{repo} PR #{pr_number}"
                )
            if response.status_code in (401, 403):
                if "rate limit" in response.text.lower():
                    raise GitHubRateLimitError("GitHub API rate limit exceeded")
                raise GitHubAuthError(
                    f"GitHub authentication failure ({response.status_code})"
                )
            if response.is_error:
                raise GitHubClientError(
                    f"GitHub API returned error {response.status_code}"
                )

            data = response.json()
            if isinstance(data, list):
                collected.extend(item for item in data if isinstance(item, dict))
            next_url = _parse_next_page_url(response.headers)

    if next_url and pages >= MAX_REVIEW_COMMENT_PAGES:
        logger.info(
            "PR review comments for %s/%s PR #%s truncated at %d pages "
            "(%d comments); keeping the oldest window",
            owner,
            repo,
            pr_number,
            pages,
            len(collected),
        )
    return collected


async def fetch_review_comment(
    owner: str,
    repo: str,
    comment_id: int,
    token: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """
    Fetch one pull request review comment by its id.

    GET /repos/{owner}/{repo}/pulls/comments/{comment_id}

    :func:`fetch_pr_review_comments` is deliberately bounded, so on a PR with
    a very long review history the comment that actually triggered a run can
    fall outside the fetched window. This single-comment lookup is how the
    caller recovers the exact instruction instead of silently falling back to
    an older, unrelated ``@haunter`` request.

    Returns ``None`` when the comment is not found (deleted, or not a review
    comment). Raises the standard typed errors on other failures.
    """
    url = (
        f"{GITHUB_API_BASE}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/pulls/comments/{quote(str(comment_id), safe='')}"
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
                "Network error fetching review comment %s on %s/%s",
                comment_id,
                owner,
                repo,
            )
            raise GitHubNetworkError(
                f"Network error connecting to GitHub: {exc.__class__.__name__}"
            ) from exc

    if response.status_code == 404:
        return None
    if response.status_code in (401, 403):
        if "rate limit" in response.text.lower():
            raise GitHubRateLimitError("GitHub API rate limit exceeded")
        raise GitHubAuthError(f"GitHub authentication failure ({response.status_code})")
    if response.is_error:
        raise GitHubClientError(f"GitHub API returned error {response.status_code}")

    data = response.json()
    return data if isinstance(data, dict) else None


async def post_review_thread_reply(
    owner: str,
    repo: str,
    pr_number: int,
    in_reply_to_comment_id: int,
    body: str,
    token: Optional[str] = None,
) -> dict[str, Any]:
    """
    Post a reply inside a pull request review thread.

    POST /repos/{owner}/{repo}/pulls/{pr_number}/comments/{comment_id}/replies

    Used by the conversational follow-up pipeline (``@haunter fix`` /
    ``test-fix``) to answer reviewer threads in place. Callers that only
    have an issue-thread context should use :func:`post_pr_comment` instead;
    on 404/422 the caller should fall back to :func:`post_pr_comment` so the
    verdict is never silently dropped.
    """
    url = (
        f"{GITHUB_API_BASE}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/pulls/{quote(str(pr_number), safe='')}"
        f"/comments/{quote(str(in_reply_to_comment_id), safe='')}/replies"
    )
    headers = _build_headers(token=token, accept="application/vnd.github+json")

    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True
    ) as client:
        try:
            response = await client.post(url, headers=headers, json={"body": body})
        except httpx.RequestError as exc:
            logger.error(
                "Network error posting review thread reply for %s/%s PR #%s",
                owner,
                repo,
                pr_number,
            )
            raise GitHubNetworkError(
                f"Network error connecting to GitHub: {exc.__class__.__name__}"
            ) from exc

    if response.status_code == 404:
        raise GitHubResourceNotFoundError(
            f"Review thread not found for {owner}/{repo} PR #{pr_number} "
            f"comment {in_reply_to_comment_id}"
        )
    if response.status_code in (401, 403):
        if "rate limit" in response.text.lower():
            raise GitHubRateLimitError("GitHub API rate limit exceeded")
        raise GitHubAuthError(f"GitHub authentication failure ({response.status_code})")
    if response.is_error:
        raise GitHubClientError(f"GitHub API returned error {response.status_code}")

    result = response.json()
    return result if isinstance(result, dict) else {}


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
    if response.status_code == 422:
        # The 422 body is upstream-controlled text, and it names the offending
        # field — which is the only thing that makes this error actionable. So it
        # is logged, but through `sanitize_log_value` like every other new line in
        # this module: a newline in the body forges a second log record, a
        # zero-width or bidi character hides the offending field from the
        # operator reading the line, a token-shaped literal in the echoed payload
        # persists a credential outside every rotation path, and the body is
        # unbounded. The exception message keeps the raw text because it is
        # consumed as data by the retry classifier, not written to a log.
        logger.error(
            "GitHub API rejected the review payload for %s/%s PR #%s: %s",
            owner,
            repo,
            pr_number,
            sanitize_log_value(response.text),
        )
        raise GitHubUnprocessableEntityError(
            f"GitHub API returned error 422: {response.text}"
        )
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


#: Review-thread pages walked per fetch. A thread is only resolvable while the
#: PR is open, so this is bounded for the same reason MAX_REVIEW_COMMENT_PAGES
#: is: an unbounded thread list must not be able to inflate a request.
MAX_REVIEW_THREAD_PAGES = 5

_REVIEW_THREADS_QUERY = """
query HaunterReviewThreads(
  $owner: String!
  $repo: String!
  $number: Int!
  $cursor: String
) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          isOutdated
          isCollapsed
          path
          line
          originalLine
          comments(first: 1) {
            nodes {
              databaseId
              author { login }
            }
          }
        }
      }
    }
  }
}
"""

#: GitHub's ``resolveReviewThread`` takes exactly one thread per mutation, so
#: this is a per-thread round trip. Bounded so a PR with an unbounded number of
#: open Haunter threads cannot turn one publish into an unbounded request storm.
MAX_REVIEW_THREAD_RESOLVES = 25

_RESOLVE_REVIEW_THREAD_MUTATION = """
mutation HaunterResolveReviewThread($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
"""


def _graphql_headers(token: Optional[str]) -> dict[str, str]:
    resolved_token = token or settings.github_token
    headers = {
        "Accept": "application/json",
        "User-Agent": "Haunter-Autonomous-Agent/1.0",
        "Content-Type": "application/json",
    }
    if resolved_token:
        headers["Authorization"] = f"Bearer {resolved_token}"
    return headers


#: How long a resolved bot identity is reused, mirroring the model-discovery
#: TTL the repo already relies on (``app/llm/discovery.py:37``): long enough that
#: a PR reviewed N times costs one identity call rather than N, short enough that
#: a rotated App bot login is picked up without a redeploy.
BOT_IDENTITY_CACHE_TTL_SECONDS: float = 900.0

#: Ceiling on the identity cache. Entries are keyed per credential, and GitHub
#: expires installation tokens hourly, so this is a safety valve against
#: unbounded growth rather than a policy.
BOT_IDENTITY_CACHE_MAX_ENTRIES = 64

#: token-fingerprint -> (expiry_monotonic, login). Only the fingerprint is kept;
#: the credential itself is never stored, here or anywhere else.
_bot_identity_cache: dict[str, tuple[float, str]] = {}


def _identity_cache_key(token: Optional[str]) -> str:
    """Stable per-credential cache key.

    Keying per credential is what makes the cache sound: one warm Lambda
    container serves many installations, and a single-slot cache would hand one
    repo's identity to another repo's review — which would let it resolve threads
    that are not its own. The key is a truncated SHA-256 of the token, a one-way
    digest of a high-entropy secret, so the cache never holds the credential.
    """
    if not isinstance(token, str) or not token.strip():
        return "no-token"
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()[:32]


async def _request_bot_login(token: Optional[str]) -> str:
    """``GET /user`` and return the bot login. Raises a typed client error."""
    headers = _build_headers(token=token)
    url = f"{GITHUB_API_BASE}/user"
    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True
    ) as client:
        try:
            response = await _bounded_get(client, url, headers=headers)
        except httpx.RequestError as exc:
            raise GitHubNetworkError(
                f"Network error connecting to GitHub: {exc.__class__.__name__}"
            ) from exc

    if response.status_code in (401, 403):
        if "rate limit" in response.text.lower():
            raise GitHubRateLimitError("GitHub API rate limit exceeded")
        raise GitHubAuthError(f"GitHub authentication failure ({response.status_code})")
    if response.is_error:
        raise GitHubClientError(f"GitHub API returned error {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise GitHubClientError("GitHub GET /user returned a non-JSON body") from exc
    if not isinstance(payload, dict):
        raise GitHubClientError("GitHub GET /user returned a non-object body")
    # An installation access token authenticates as the App's bot user, whose
    # account type is "Bot". A personal access token (the dev fallback in
    # `app.github.pr.get_installation_token`) also authenticates successfully
    # here, and adopting a human's login would make the caller treat that human's
    # own review threads as ours to resolve. Only a bot identity is ever accepted.
    if payload.get("type") != "Bot":
        raise GitHubAuthError(
            "GitHub credential authenticates as a non-bot account type "
            f"{sanitize_log_value(payload.get('type'), 64)!r}; refusing to treat a "
            "human account as this pipeline's own identity"
        )
    login = payload.get("login")
    if not isinstance(login, str) or not login.strip():
        raise GitHubClientError("GitHub GET /user returned no login")
    # GitHub logins are capped at 39 chars; the bound only stops a malformed
    # body from smuggling an unbounded string into the cache and the log.
    return login.strip()[:128]


async def fetch_bot_identity(token: Optional[str] = None) -> Optional[str]:
    """Resolve the login the given GitHub credential authenticates as.

    There is no configured Haunter bot slug anywhere in this codebase, and none
    is needed: an installation access token *is* the App's bot user, so
    ``GET /user`` is the authoritative answer to "which account are we". That
    also keeps the caller off ``login.endswith("[bot]")``, which would match
    Dependabot, Codecov and every other bot whose threads are not ours.

    The result is cached per credential for
    :data:`BOT_IDENTITY_CACHE_TTL_SECONDS`, so a PR with several review runs
    costs one identity call rather than one per run. The cache is written
    without a lock on purpose: the value is idempotent, so the only thing a
    concurrent pair can do is duplicate one identical ``GET /user``, and an
    ``asyncio.Lock`` held across event loops is a hard ``RuntimeError`` — inside
    a path that must never fail a review.

    No ``force_refresh`` escape hatch, deliberately. This function's cache is keyed
    per credential, so a rotated installation token is a different key and cannot
    read a stale entry; the only staleness a caller could be defending against is
    GitHub changing an existing bot's login, which is not a thing that happens.
    A knob nothing passes is a knob nothing has reasoned about.

    Returns the bot login, or ``None`` when it cannot be established. Never
    raises: the caller skips thread resolution on ``None``, so a metadata
    endpoint GitHub is refusing can never fail a review.
    """
    cache_key = _identity_cache_key(token)
    now = time.monotonic()
    cached = _bot_identity_cache.get(cache_key)
    if cached is not None and now < cached[0]:
        return cached[1]

    try:
        login = await _request_bot_login(token)
    except Exception as exc:
        # Not cached: a transient refusal must be able to clear on the next run
        # rather than pinning this container to "no identity" for a full TTL.
        logger.info(
            "github bot_identity unavailable error_type=%s error=%s",
            type(exc).__name__,
            sanitize_log_value(exc),
        )
        return None

    if len(_bot_identity_cache) >= BOT_IDENTITY_CACHE_MAX_ENTRIES:
        for key in [
            key
            for key, (expires_at, _) in _bot_identity_cache.items()
            if now >= expires_at
        ]:
            _bot_identity_cache.pop(key, None)
        while len(_bot_identity_cache) >= BOT_IDENTITY_CACHE_MAX_ENTRIES:
            oldest = min(_bot_identity_cache.items(), key=lambda item: item[1][0])
            _bot_identity_cache.pop(oldest[0], None)
    _bot_identity_cache[cache_key] = (
        time.monotonic() + BOT_IDENTITY_CACHE_TTL_SECONDS,
        login,
    )
    logger.info("github bot_identity resolved login=%s", sanitize_log_value(login, 128))
    return login


async def fetch_review_threads(
    owner: str,
    repo: str,
    pr_number: int,
    token: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Fetch a PR's review threads through the GraphQL API.

    ``GET /pulls/{n}/comments`` returns ``PullRequestReviewComment`` rows, and a
    comment's ``node_id`` is **not** a ``PullRequestReviewThread`` id — they are
    different node types, and passing one where the other is expected fails the
    mutation. Resolving a thread is only possible with the real thread id, so
    the thread list has to come from GraphQL.

    Returns raw thread dicts (``id``, ``isResolved``, ``isOutdated``, ``path``,
    ``line``, ``originalLine``, and the first comment's ``databaseId`` /
    ``author.login``). Returns ``[]`` on any failure: thread resolution is a
    courtesy to the reviewer, and must never fail a publish. Raises nothing.
    """
    url = "https://api.github.com/graphql"
    headers = _graphql_headers(token)
    variables: dict[str, Any] = {
        "owner": owner,
        "repo": repo,
        "number": int(pr_number),
    }

    threads: list[dict[str, Any]] = []
    cursor: Optional[str] = None
    pages = 0
    # One client for the whole walk, not one per page: the pages are sequential
    # and the connection is reused between them, and a client-per-page loop makes
    # the number of TLS handshakes scale with the page cap for no benefit.
    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True
    ) as client:
        while pages < MAX_REVIEW_THREAD_PAGES:
            pages += 1
            try:
                response = await _bounded_post(
                    client,
                    url,
                    headers=headers,
                    json={
                        "query": _REVIEW_THREADS_QUERY,
                        "variables": {**variables, "cursor": cursor},
                    },
                )
            except (httpx.RequestError, GitHubResponseLimitError) as exc:
                logger.warning(
                    "github review_threads fetch_error repo=%s pr=%s error_type=%s",
                    owner,
                    repo,
                    type(exc).__name__,
                )
                return []
            if response.is_error:
                logger.warning(
                    "github review_threads http_error repo=%s pr=%s status=%s",
                    owner,
                    repo,
                    pr_number,
                    response.status_code,
                )
                return []
            try:
                payload = response.json()
            except ValueError:
                logger.warning(
                    "github review_threads malformed_body repo=%s pr=%s", owner, repo
                )
                return []
            if not isinstance(payload, dict):
                return []
            if payload.get("errors"):
                logger.warning(
                    "github review_threads graphql_errors repo=%s pr=%s",
                    owner,
                    repo,
                )
                return []
            try:
                connection = payload["data"]["repository"]["pullRequest"][
                    "reviewThreads"
                ]
                nodes = connection["nodes"]
            except (KeyError, TypeError):
                logger.warning(
                    "github review_threads unexpected_shape repo=%s pr=%s", owner, repo
                )
                return []
            threads.extend(node for node in nodes if isinstance(node, dict))
            page_info = connection.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            if not cursor:
                break
    if len(threads) >= MAX_REVIEW_THREAD_PAGES * 100:
        logger.info(
            "github review_threads truncated repo=%s pr=%s threads=%d",
            owner,
            repo,
            len(threads),
        )
    return threads


async def resolve_review_threads(
    owner: str,
    repo: str,
    thread_ids: Sequence[str],
    token: Optional[str] = None,
) -> int:
    """Resolve review threads through the GraphQL ``resolveReviewThread`` mutation.

    ``thread_ids`` must be real ``PullRequestReviewThread`` ids — see
    :func:`fetch_review_threads` for why a review-comment id will not do.

    Returns the number of threads GitHub confirmed resolved. Never raises: a
    failed resolve leaves the thread open, which is the state the reviewer can
    still act on, so it is strictly better than failing the publish.
    """
    unique_ids: list[str] = []
    seen: set[str] = set()
    for raw in thread_ids:
        thread_id = str(raw or "").strip()
        if thread_id and thread_id not in seen:
            seen.add(thread_id)
            unique_ids.append(thread_id)
    if not unique_ids:
        return 0
    if len(unique_ids) > MAX_REVIEW_THREAD_RESOLVES:
        logger.info(
            "github resolve_review_threads bounded repo=%s requested=%d cap=%d",
            owner,
            len(unique_ids),
            MAX_REVIEW_THREAD_RESOLVES,
        )
        unique_ids = unique_ids[:MAX_REVIEW_THREAD_RESOLVES]

    url = "https://api.github.com/graphql"
    headers = _graphql_headers(token)
    resolved = 0
    # One client for the whole sweep: `resolveReviewThread` takes a single thread
    # per mutation, so this is a per-thread round trip and a client-per-thread
    # loop pays a fresh TLS handshake for every one of them, bounded at
    # MAX_REVIEW_THREAD_RESOLVES. Reusing the connection changes nothing
    # observable.
    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True
    ) as client:
        for thread_id in unique_ids:
            try:
                response = await _bounded_post(
                    client,
                    url,
                    headers=headers,
                    json={
                        "query": _RESOLVE_REVIEW_THREAD_MUTATION,
                        "variables": {"threadId": thread_id},
                    },
                )
            except (httpx.RequestError, GitHubResponseLimitError) as exc:
                logger.warning(
                    "github resolve_review_threads fetch_error repo=%s error_type=%s",
                    owner,
                    type(exc).__name__,
                )
                return resolved
            if response.is_error:
                logger.warning(
                    "github resolve_review_threads http_error repo=%s status=%s",
                    owner,
                    response.status_code,
                )
                return resolved
            try:
                payload = response.json()
            except ValueError:
                logger.warning(
                    "github resolve_review_threads malformed_body repo=%s", owner
                )
                return resolved
            if not isinstance(payload, dict):
                return resolved
            if payload.get("errors"):
                # The id, not `len(thread_id)`. A GraphQL node id is a public,
                # non-secret handle on a review thread, so it is safe to log and is
                # the one thing an operator needs to act on the failure; its
                # length identifies nothing. Bounded and sanitized anyway, because
                # the value is echoed back from an upstream response.
                logger.warning(
                    "github resolve_review_threads graphql_errors repo=%s thread_id=%s",
                    owner,
                    sanitize_log_value(thread_id, 64),
                )
                return resolved
            thread = ((payload.get("data") or {}).get("resolveReviewThread") or {}).get(
                "thread"
            )
            if isinstance(thread, dict) and thread.get("isResolved") is True:
                resolved += 1
    return resolved


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


async def fetch_commit_tree_sha(
    owner: str,
    repo: str,
    commit_sha: str,
    installation_token: Optional[str] = None,
) -> str:
    """
    Resolve a commit SHA to its root tree SHA via GitHub Git Data API.

    GET /repos/{owner}/{repo}/git/commits/{commit_sha}
    Returns the tree SHA.

    Raises:
        GitHubAuthError, GitHubRateLimitError, GitHubResourceNotFoundError, GitHubClientError.
    """
    url = f"{GITHUB_API_BASE}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/git/commits/{quote(commit_sha, safe='')}"
    headers = _build_headers(
        token=installation_token, accept="application/vnd.github+json"
    )

    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True
    ) as client:
        try:
            response = await client.get(url, headers=headers)
        except httpx.RequestError as exc:
            logger.error(
                "Network error fetching git commit for %s/%s @ %s",
                owner,
                repo,
                commit_sha,
            )
            raise GitHubNetworkError(
                f"Network error connecting to GitHub: {exc.__class__.__name__}"
            ) from exc

    if response.status_code == 404:
        raise GitHubResourceNotFoundError(
            f"Commit not found for {owner}/{repo} @ {commit_sha}"
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
    tree = data.get("tree", {})
    if isinstance(tree, dict) and "sha" in tree:
        return str(tree["sha"])
    if isinstance(tree, str):
        return tree
    raise GitHubClientError(
        f"Tree SHA not found in commit object for {owner}/{repo} @ {commit_sha}"
    )


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
        GitHubResourceNotFoundError: Branch not found (404) or reference does not exist (422).
        GitHubAuthError, GitHubRateLimitError, GitHubClientError.
    """
    if force:
        raise GitHubClientError("Force update is not permitted")

    clean_branch = branch.removeprefix("refs/heads/")
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/refs/heads/{clean_branch}"
    headers = _build_headers(
        token=installation_token, accept="application/vnd.github+json"
    )
    payload = {"sha": commit_sha, "force": False}

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
            f"Branch ref not found: {owner}/{repo}/heads/{clean_branch}"
        )
    if response.status_code == 422:
        error_msg = response.text
        if (
            "reference does not exist" in error_msg.lower()
            or "not found" in error_msg.lower()
        ):
            raise GitHubResourceNotFoundError(
                f"Branch ref not found (422): {owner}/{repo}/heads/{clean_branch}"
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
