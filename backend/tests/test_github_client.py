"""
Tests for GitHub REST API client (backend/app/github_client.py).

Covers:
- _build_headers with explicit token, settings.github_token, None, and custom accept header.
- fetch_workflow_run_logs: zip extraction, text fallback, 404, rate limit (401/403),
  auth error (401/403), 5xx error, ConnectError with class name, and follow_redirects 302 chain.
- fetch_diff: single commit vs compare base_sha, 200 verbatim, 404, 5xx, rate limit,
  auth error, and network error.
- fetch_commit_metadata: 200 parsed dict, 404, rate limit, auth error, 5xx, and network error.
- post_commit_comment: 201 response JSON, 404, network error, rate limit, auth error, and 5xx.
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

import httpx
import pytest
import respx

from app.config import settings
from app.github_client import (
    GITHUB_API_BASE,
    GitHubAuthError,
    GitHubClientError,
    GitHubRateLimitError,
    GitHubResourceNotFoundError,
    _build_headers,
    fetch_commit_metadata,
    fetch_diff,
    fetch_workflow_run_logs,
    post_commit_comment,
)

# Alias for Phase 3 spec terminology
fetch_workflow_logs = fetch_workflow_run_logs


def _create_zip_bytes(files: dict[str, str]) -> bytes:
    """Helper to build an in-memory zip archive with given {filename: content}."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for fname, content in files.items():
            zf.writestr(fname, content.encode("utf-8"))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# _build_headers tests
# ---------------------------------------------------------------------------


def test_build_headers_no_token(monkeypatch: pytest.MonkeyPatch):
    """_build_headers(None) without settings.github_token has no Authorization header."""
    monkeypatch.setattr(settings, "github_token", None)
    headers = _build_headers(None)
    assert "Authorization" not in headers
    assert headers["User-Agent"] == "Haunter-Autonomous-Agent/1.0"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert headers["Accept"] == "application/vnd.github+json"


def test_build_headers_explicit_token(monkeypatch: pytest.MonkeyPatch):
    """_build_headers('tok') sets Bearer tok, User-Agent, and X-GitHub-Api-Version."""
    monkeypatch.setattr(settings, "github_token", None)
    headers = _build_headers("tok")
    assert headers["Authorization"] == "Bearer tok"
    assert headers["User-Agent"] == "Haunter-Autonomous-Agent/1.0"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert headers["Accept"] == "application/vnd.github+json"


def test_build_headers_falls_back_to_settings_token(monkeypatch: pytest.MonkeyPatch):
    """_build_headers(None) uses settings.github_token when available."""
    monkeypatch.setattr(settings, "github_token", "env_secret_token")
    headers = _build_headers(None)
    assert headers["Authorization"] == "Bearer env_secret_token"


def test_build_headers_explicit_token_overrides_settings(monkeypatch: pytest.MonkeyPatch):
    """Explicit token takes precedence over settings.github_token."""
    monkeypatch.setattr(settings, "github_token", "env_secret_token")
    headers = _build_headers("override_token")
    assert headers["Authorization"] == "Bearer override_token"


def test_build_headers_custom_accept():
    """Custom accept header is preserved while standard headers are populated."""
    headers = _build_headers(token="tok", accept="application/vnd.github.v3.diff")
    assert headers["Accept"] == "application/vnd.github.v3.diff"
    assert headers["Authorization"] == "Bearer tok"
    assert headers["User-Agent"] == "Haunter-Autonomous-Agent/1.0"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"


# ---------------------------------------------------------------------------
# fetch_workflow_run_logs tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_fetch_workflow_logs_success_zip():
    """200 response with valid zip of 2 .txt files concatenates and includes filenames."""
    zip_data = _create_zip_bytes({
        "0_setup.txt": "Setting up runner...\nDone.\n",
        "1_test.txt": "pytest tests/ -v\nFAILED test_foo\n",
    })
    url = f"{GITHUB_API_BASE}/repos/owner/repo/actions/runs/12345/logs"
    respx.get(url).respond(
        status_code=200,
        content=zip_data,
        headers={"Content-Type": "application/zip"},
    )

    result = await fetch_workflow_logs(owner="owner", repo="repo", run_id=12345)

    assert "=== File: 0_setup.txt ===" in result
    assert "Setting up runner..." in result
    assert "=== File: 1_test.txt ===" in result
    assert "FAILED test_foo" in result


@pytest.mark.asyncio
@respx.mock
async def test_fetch_workflow_logs_success_zip_ignores_non_txt():
    """Zip extraction includes only .txt files, sorted by name."""
    zip_data = _create_zip_bytes({
        "manifest.json": '{"jobs": 1}',
        "build.txt": "Compiling assets...",
    })
    url = f"{GITHUB_API_BASE}/repos/owner/repo/actions/runs/12345/logs"
    respx.get(url).respond(status_code=200, content=zip_data)

    result = await fetch_workflow_logs(owner="owner", repo="repo", run_id=12345)

    assert "=== File: build.txt ===" in result
    assert "Compiling assets..." in result
    assert "manifest.json" not in result


@pytest.mark.asyncio
@respx.mock
async def test_fetch_workflow_logs_fallback_non_zip():
    """200 response with non-zip plain text body falls back to returning text."""
    plain_text = "Raw log stream output without zip container"
    url = f"{GITHUB_API_BASE}/repos/owner/repo/actions/runs/12345/logs"
    respx.get(url).respond(status_code=200, text=plain_text)

    result = await fetch_workflow_logs(owner="owner", repo="repo", run_id=12345)
    assert result == plain_text


@pytest.mark.asyncio
@respx.mock
async def test_fetch_workflow_logs_404():
    """404 status code raises GitHubResourceNotFoundError."""
    url = f"{GITHUB_API_BASE}/repos/owner/repo/actions/runs/12345/logs"
    respx.get(url).respond(status_code=404, text="Not Found")

    with pytest.raises(GitHubResourceNotFoundError) as exc_info:
        await fetch_workflow_logs(owner="owner", repo="repo", run_id=12345)

    assert "Workflow run logs not found" in str(exc_info.value)
    assert "12345" in str(exc_info.value)


@pytest.mark.asyncio
@respx.mock
async def test_fetch_workflow_logs_rate_limit_401():
    """401 with 'rate limit' in body raises GitHubRateLimitError."""
    url = f"{GITHUB_API_BASE}/repos/owner/repo/actions/runs/12345/logs"
    respx.get(url).respond(
        status_code=401,
        text="API rate limit exceeded for user ID 123",
    )

    with pytest.raises(GitHubRateLimitError) as exc_info:
        await fetch_workflow_logs(owner="owner", repo="repo", run_id=12345)

    assert "rate limit exceeded" in str(exc_info.value).lower()


@pytest.mark.asyncio
@respx.mock
async def test_fetch_workflow_logs_rate_limit_403():
    """403 with 'rate limit' in body raises GitHubRateLimitError."""
    url = f"{GITHUB_API_BASE}/repos/owner/repo/actions/runs/12345/logs"
    respx.get(url).respond(
        status_code=403,
        text="Primary rate limit reached. Please wait.",
    )

    with pytest.raises(GitHubRateLimitError) as exc_info:
        await fetch_workflow_logs(owner="owner", repo="repo", run_id=12345)

    assert "rate limit exceeded" in str(exc_info.value).lower()


@pytest.mark.asyncio
@respx.mock
async def test_fetch_workflow_logs_auth_error_401():
    """401 without rate limit phrase raises GitHubAuthError."""
    url = f"{GITHUB_API_BASE}/repos/owner/repo/actions/runs/12345/logs"
    respx.get(url).respond(status_code=401, text="Bad credentials")

    with pytest.raises(GitHubAuthError) as exc_info:
        await fetch_workflow_logs(owner="owner", repo="repo", run_id=12345)

    assert "authentication failure (401)" in str(exc_info.value)


@pytest.mark.asyncio
@respx.mock
async def test_fetch_workflow_logs_auth_error_403():
    """403 without rate limit phrase raises GitHubAuthError."""
    url = f"{GITHUB_API_BASE}/repos/owner/repo/actions/runs/12345/logs"
    respx.get(url).respond(status_code=403, text="Resource forbidden to caller")

    with pytest.raises(GitHubAuthError) as exc_info:
        await fetch_workflow_logs(owner="owner", repo="repo", run_id=12345)

    assert "authentication failure (403)" in str(exc_info.value)


@pytest.mark.asyncio
@respx.mock
async def test_fetch_workflow_logs_500_error():
    """5xx error raises generic GitHubClientError."""
    url = f"{GITHUB_API_BASE}/repos/owner/repo/actions/runs/12345/logs"
    respx.get(url).respond(status_code=500, text="Internal Server Error")

    with pytest.raises(GitHubClientError) as exc_info:
        await fetch_workflow_logs(owner="owner", repo="repo", run_id=12345)

    assert "GitHub API returned error 500" in str(exc_info.value)


@pytest.mark.asyncio
@respx.mock
async def test_fetch_workflow_logs_connect_error():
    """httpx.ConnectError raises GitHubClientError with exception class name in message."""
    url = f"{GITHUB_API_BASE}/repos/owner/repo/actions/runs/12345/logs"
    respx.get(url).mock(side_effect=httpx.ConnectError("Failed to resolve host"))

    with pytest.raises(GitHubClientError) as exc_info:
        await fetch_workflow_logs(owner="owner", repo="repo", run_id=12345)

    assert "ConnectError" in str(exc_info.value)
    assert "Network error connecting to GitHub" in str(exc_info.value)


@pytest.mark.asyncio
@respx.mock
async def test_fetch_workflow_logs_follows_redirects():
    """fetch_workflow_logs follows 302 redirect chain to archive download URL."""
    initial_url = f"{GITHUB_API_BASE}/repos/owner/repo/actions/runs/12345/logs"
    archive_url = "https://pipelines.actions.githubusercontent.com/download/archive.zip"
    zip_data = _create_zip_bytes({"test.txt": "Redirected test log content"})

    r1 = respx.get(initial_url).respond(
        status_code=302,
        headers={"Location": archive_url},
    )
    r2 = respx.get(archive_url).respond(
        status_code=200,
        content=zip_data,
        headers={"Content-Type": "application/zip"},
    )

    result = await fetch_workflow_logs(owner="owner", repo="repo", run_id=12345)

    assert r1.called
    assert r2.called
    assert "=== File: test.txt ===" in result
    assert "Redirected test log content" in result


# ---------------------------------------------------------------------------
# fetch_diff tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_fetch_diff_single_commit():
    """base_sha=None hits /repos/.../commits/{sha} with diff accept header."""
    sha = "1111222233334444555566667777888899990000"
    url = f"{GITHUB_API_BASE}/repos/owner/repo/commits/{sha}"
    diff_text = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"

    route = respx.get(url).respond(
        status_code=200,
        text=diff_text,
        headers={"Content-Type": "text/plain"},
    )

    result = await fetch_diff(owner="owner", repo="repo", sha=sha, base_sha=None)

    assert route.called
    assert route.calls.last.request.headers.get("accept") == "application/vnd.github.v3.diff"
    assert result == diff_text


@pytest.mark.asyncio
@respx.mock
async def test_fetch_diff_compare_base_sha():
    """base_sha='abc' hits /repos/.../compare/abc...{sha}."""
    base_sha = "abcdef12"
    head_sha = "98765432"
    url = f"{GITHUB_API_BASE}/repos/owner/repo/compare/{base_sha}...{head_sha}"
    diff_text = "diff --git a/b.py b/b.py\n+added\n"

    route = respx.get(url).respond(
        status_code=200,
        text=diff_text,
        headers={"Content-Type": "text/plain"},
    )

    result = await fetch_diff(owner="owner", repo="repo", sha=head_sha, base_sha=base_sha)

    assert route.called
    assert route.calls.last.request.headers.get("accept") == "application/vnd.github.v3.diff"
    assert result == diff_text


@pytest.mark.asyncio
@respx.mock
async def test_fetch_diff_404():
    """404 on fetch_diff raises GitHubResourceNotFoundError."""
    sha = "deadbeef"
    url = f"{GITHUB_API_BASE}/repos/owner/repo/commits/{sha}"
    respx.get(url).respond(status_code=404, text="Commit not found")

    with pytest.raises(GitHubResourceNotFoundError) as exc_info:
        await fetch_diff(owner="owner", repo="repo", sha=sha)

    assert "Commit/diff not found" in str(exc_info.value)
    assert sha in str(exc_info.value)


@pytest.mark.asyncio
@respx.mock
async def test_fetch_diff_5xx_error():
    """5xx response raises GitHubClientError."""
    sha = "deadbeef"
    url = f"{GITHUB_API_BASE}/repos/owner/repo/commits/{sha}"
    respx.get(url).respond(status_code=502, text="Bad Gateway")

    with pytest.raises(GitHubClientError) as exc_info:
        await fetch_diff(owner="owner", repo="repo", sha=sha)

    assert "GitHub API returned error 502" in str(exc_info.value)


@pytest.mark.asyncio
@respx.mock
async def test_fetch_diff_rate_limit_and_auth_errors():
    """fetch_diff handles rate limits and auth errors correctly."""
    sha = "deadbeef"
    url = f"{GITHUB_API_BASE}/repos/owner/repo/commits/{sha}"

    # Rate limit check
    respx.get(url).respond(status_code=403, text="API rate limit exceeded")
    with pytest.raises(GitHubRateLimitError):
        await fetch_diff(owner="owner", repo="repo", sha=sha)

    # Auth error check
    respx.get(url).respond(status_code=401, text="Requires authentication")
    with pytest.raises(GitHubAuthError):
        await fetch_diff(owner="owner", repo="repo", sha=sha)


@pytest.mark.asyncio
@respx.mock
async def test_fetch_diff_network_error():
    """Network connection failure raises GitHubClientError."""
    sha = "deadbeef"
    url = f"{GITHUB_API_BASE}/repos/owner/repo/commits/{sha}"
    respx.get(url).mock(side_effect=httpx.ConnectTimeout("Connection timed out"))

    with pytest.raises(GitHubClientError) as exc_info:
        await fetch_diff(owner="owner", repo="repo", sha=sha)

    assert "ConnectTimeout" in str(exc_info.value)


# ---------------------------------------------------------------------------
# fetch_commit_metadata tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_fetch_commit_metadata_success():
    """200 response returns parsed commit metadata dict."""
    sha = "fedcba987654"
    url = f"{GITHUB_API_BASE}/repos/owner/repo/commits/{sha}"
    payload: dict[str, Any] = {
        "sha": sha,
        "commit": {
            "author": {"name": "Octocat", "email": "octocat@github.com"},
            "message": "fix: resolve off-by-one error",
        },
        "files": [{"filename": "app/main.py", "status": "modified"}],
    }

    route = respx.get(url).respond(status_code=200, json=payload)

    data = await fetch_commit_metadata(owner="owner", repo="repo", sha=sha)

    assert route.called
    assert data["sha"] == sha
    assert data["commit"]["message"] == "fix: resolve off-by-one error"
    assert len(data["files"]) == 1


@pytest.mark.asyncio
@respx.mock
async def test_fetch_commit_metadata_404():
    """404 raises GitHubResourceNotFoundError."""
    sha = "not_found_sha"
    url = f"{GITHUB_API_BASE}/repos/owner/repo/commits/{sha}"
    respx.get(url).respond(status_code=404, text="Commit not found")

    with pytest.raises(GitHubResourceNotFoundError) as exc_info:
        await fetch_commit_metadata(owner="owner", repo="repo", sha=sha)

    assert "Commit not found" in str(exc_info.value)
    assert sha in str(exc_info.value)


@pytest.mark.asyncio
@respx.mock
async def test_fetch_commit_metadata_errors():
    """Rate limits, auth errors, 5xx, and network errors raise corresponding client errors."""
    sha = "error_sha"
    url = f"{GITHUB_API_BASE}/repos/owner/repo/commits/{sha}"

    # Rate limit check
    respx.get(url).respond(status_code=403, text="rate limit exceeded")
    with pytest.raises(GitHubRateLimitError):
        await fetch_commit_metadata(owner="owner", repo="repo", sha=sha)

    # 401 Auth error
    respx.get(url).respond(status_code=401, text="Unauthorized")
    with pytest.raises(GitHubAuthError):
        await fetch_commit_metadata(owner="owner", repo="repo", sha=sha)

    # 500 Error
    respx.get(url).respond(status_code=500, text="Internal Server Error")
    with pytest.raises(GitHubClientError):
        await fetch_commit_metadata(owner="owner", repo="repo", sha=sha)

    # Network Error
    respx.get(url).mock(side_effect=httpx.ReadTimeout("Read timed out"))
    with pytest.raises(GitHubClientError) as exc_info:
        await fetch_commit_metadata(owner="owner", repo="repo", sha=sha)
    assert "ReadTimeout" in str(exc_info.value)


# ---------------------------------------------------------------------------
# post_commit_comment tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_post_commit_comment_success():
    """201 response returns created comment JSON."""
    sha = "abc123commit"
    url = f"{GITHUB_API_BASE}/repos/owner/repo/commits/{sha}/comments"
    body_text = "Haunter diagnosis: test failed due to timeout."
    response_payload = {
        "id": 9876,
        "body": body_text,
        "commit_id": sha,
    }

    route = respx.post(url).respond(status_code=201, json=response_payload)

    result = await post_commit_comment(
        owner="owner",
        repo="repo",
        sha=sha,
        body=body_text,
        token="test_tok",
    )

    assert route.called
    sent_json = route.calls.last.request.read().decode("utf-8")
    assert body_text in sent_json
    assert route.calls.last.request.headers.get("authorization") == "Bearer test_tok"
    assert result["id"] == 9876
    assert result["body"] == body_text


@pytest.mark.asyncio
@respx.mock
async def test_post_commit_comment_404():
    """404 raises GitHubResourceNotFoundError."""
    sha = "missing_sha"
    url = f"{GITHUB_API_BASE}/repos/owner/repo/commits/{sha}/comments"
    respx.post(url).respond(status_code=404, text="Not Found")

    with pytest.raises(GitHubResourceNotFoundError) as exc_info:
        await post_commit_comment(
            owner="owner",
            repo="repo",
            sha=sha,
            body="diagnostic comment",
        )

    assert "Commit not found" in str(exc_info.value)
    assert "to post comment" in str(exc_info.value)


@pytest.mark.asyncio
@respx.mock
async def test_post_commit_comment_network_error():
    """Network failure raises GitHubClientError with class name."""
    sha = "net_err_sha"
    url = f"{GITHUB_API_BASE}/repos/owner/repo/commits/{sha}/comments"
    respx.post(url).mock(side_effect=httpx.ConnectError("Connection refused"))

    with pytest.raises(GitHubClientError) as exc_info:
        await post_commit_comment(
            owner="owner",
            repo="repo",
            sha=sha,
            body="diagnostic comment",
        )

    assert "ConnectError" in str(exc_info.value)
    assert "Network error connecting to GitHub" in str(exc_info.value)


@pytest.mark.asyncio
@respx.mock
async def test_post_commit_comment_errors():
    """post_commit_comment handles rate limits, auth errors, and 5xx."""
    sha = "err_sha"
    url = f"{GITHUB_API_BASE}/repos/owner/repo/commits/{sha}/comments"

    # Rate limit
    respx.post(url).respond(status_code=403, text="rate limit exceeded")
    with pytest.raises(GitHubRateLimitError):
        await post_commit_comment(owner="owner", repo="repo", sha=sha, body="msg")

    # Auth error
    respx.post(url).respond(status_code=401, text="Bad credentials")
    with pytest.raises(GitHubAuthError):
        await post_commit_comment(owner="owner", repo="repo", sha=sha, body="msg")

    # 500 error
    respx.post(url).respond(status_code=500, text="Server Error")
    with pytest.raises(GitHubClientError):
        await post_commit_comment(owner="owner", repo="repo", sha=sha, body="msg")
