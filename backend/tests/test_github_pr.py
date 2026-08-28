"""
Phase 8 — GitHub PR integration tests (github/pr.py).

Covers:
1. owner with ";" → GitHubPRValidationError (injection rejected before HTTP).
2. branch with "rm -rf" space chars → GitHubPRValidationError.
3. branch > 255 chars → GitHubPRValidationError.
4. get_installation_token: POSTs to /app/installations/{install_id}/access_tokens.
5. get_installation_token: caches token for 50 min (second call skips HTTP).
6. get_installation_token: falls back to settings.github_token when App not configured.
7. create_branch: force is always False (never force-push).
8. open_pr: title + body are html.escape'd + secret-redacted before POST.
9. open_pr: body capped at 3000 chars.
10. _escape_pr_text: html entities in output for XSS content.
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from app.github.pr import (
    GitHubPRAuthError,
    GitHubPRError,
    GitHubPRValidationError,
    _TOKEN_CACHE,
    _escape_pr_text,
    create_branch,
    get_installation_token,
    open_pr,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_repo(install_id: int = 123, default_branch: str = "main"):
    """Minimal Repo-like object for testing without DB."""
    repo = MagicMock()
    repo.id = "test-repo-id"
    repo.github_install_id = install_id
    repo.owner = "test-org"
    repo.name = "test-repo"
    repo.default_branch = default_branch
    return repo


# ---------------------------------------------------------------------------
# Test 1: owner injection → GitHubPRValidationError
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_create_branch_invalid_owner_rejected() -> None:
    """owner containing ';' must be rejected before any HTTP call."""
    with pytest.raises(GitHubPRValidationError, match="owner"):
        await create_branch(
            owner="test-org; rm -rf /",
            repo="test-repo",
            branch="haunter/fix-abc12345-1",
            sha="a" * 40,
            token="fake_token",
        )


@pytest.mark.anyio
async def test_open_pr_invalid_repo_name_rejected() -> None:
    """repo name containing '$' must be rejected before any HTTP call."""
    with pytest.raises(GitHubPRValidationError, match="repo"):
        await open_pr(
            owner="test-org",
            repo="test$repo",
            head_branch="haunter/fix-abc-1",
            base_branch="main",
            title="fix: something",
            body="Body text here.",
            token="fake_token",
        )


# ---------------------------------------------------------------------------
# Test 2: branch with invalid chars → GitHubPRValidationError
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_create_branch_injection_branch_name_rejected() -> None:
    """Branch name with semicolons or spaces must be rejected."""
    with pytest.raises(GitHubPRValidationError, match="[Bb]ranch"):
        await create_branch(
            owner="test-org",
            repo="test-repo",
            branch="hehe; rm -rf",
            sha="a" * 40,
            token="fake_token",
        )


# ---------------------------------------------------------------------------
# Test 3: branch > 255 chars → GitHubPRValidationError
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_create_branch_too_long_rejected() -> None:
    """Branch name > 255 chars must be rejected."""
    with pytest.raises(GitHubPRValidationError, match="length"):
        await create_branch(
            owner="test-org",
            repo="test-repo",
            branch="a" * 256,
            sha="a" * 40,
            token="fake_token",
        )


# ---------------------------------------------------------------------------
# Test 4: get_installation_token POSTs to correct URL with JWT auth
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_get_installation_token_posts_to_correct_url() -> None:
    """get_installation_token() must POST to /app/installations/{id}/access_tokens."""
    repo = _make_repo(install_id=456)
    _TOKEN_CACHE.clear()  # ensure no stale cache

    fake_token = "ghs_test_installation_token"

    with (
        patch("app.github.pr.settings") as mock_settings,
        patch("app.github.pr._build_jwt", return_value="fake_jwt"),
    ):
        mock_settings.github_app_id = "app_123"
        mock_settings.github_app_private_key = "fake_pem"
        mock_settings.github_token = None

        with respx.mock(assert_all_called=True) as rx:
            rx.post(
                "https://api.github.com/app/installations/456/access_tokens"
            ).mock(
                return_value=httpx.Response(
                    201,
                    json={"token": fake_token, "expires_at": "2099-01-01T00:00:00Z"},
                )
            )

            token = await get_installation_token(repo)

    assert token == fake_token
    _TOKEN_CACHE.clear()


# ---------------------------------------------------------------------------
# Test 5: get_installation_token caches token (second call skips HTTP)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_get_installation_token_cached() -> None:
    """Second call within 50 min must return cached token without HTTP."""
    repo = _make_repo(install_id=789)
    _TOKEN_CACHE.clear()
    cached_token = "ghs_cached_token"

    # Pre-populate cache
    _TOKEN_CACHE[789] = (cached_token, time.monotonic() + 3000)  # expires in 50 min

    with (
        patch("app.github.pr.settings") as mock_settings,
        patch("app.github.pr._build_jwt", return_value="fake_jwt"),
    ):
        mock_settings.github_app_id = "app_123"
        mock_settings.github_app_private_key = "fake_pem"

        with respx.mock():
            # If HTTP is called, the test will fail (no route registered)
            token = await get_installation_token(repo)

    assert token == cached_token
    _TOKEN_CACHE.clear()


# ---------------------------------------------------------------------------
# Test 6: get_installation_token falls back to github_token for dev
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_get_installation_token_dev_fallback() -> None:
    """When App credentials not set, falls back to settings.github_token."""
    repo = _make_repo(install_id=999)
    _TOKEN_CACHE.clear()

    with patch("app.github.pr.settings") as mock_settings:
        mock_settings.github_app_id = None
        mock_settings.github_app_private_key = None
        mock_settings.github_token = "ghp_dev_fallback_token"

        # Must not make any HTTP call
        token = await get_installation_token(repo)

    assert token == "ghp_dev_fallback_token"


# ---------------------------------------------------------------------------
# Test 7: create_branch always sends force=False (never force-push)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_create_branch_force_false() -> None:
    """POST payload to /git/refs must not include force:true."""
    captured_body: list[dict] = []

    def _capture_request(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured_body.append(body)
        return httpx.Response(201, json={"ref": "refs/heads/haunter/fix-abc-1"})

    with respx.mock() as rx:
        rx.post("https://api.github.com/repos/test-org/test-repo/git/refs").mock(
            side_effect=_capture_request
        )
        await create_branch(
            owner="test-org",
            repo="test-repo",
            branch="haunter/fix-abc12345-1",
            sha="a" * 40,
            token="fake_token",
        )

    assert len(captured_body) == 1
    assert captured_body[0]["ref"] == "refs/heads/haunter/fix-abc12345-1"
    # force must not be set — POST /git/refs has no force field (only PATCH does)
    assert "force" not in captured_body[0]


# ---------------------------------------------------------------------------
# Test 8: open_pr escapes XSS in title + body
# ---------------------------------------------------------------------------

def test_escape_pr_text_html_escapes_xss() -> None:
    """_escape_pr_text must html.escape < > & chars."""
    raw = "<script>alert('xss')</script> fix: something"
    escaped = _escape_pr_text(raw, max_len=3000)
    assert "<script>" not in escaped
    assert "&lt;script&gt;" in escaped


def test_escape_pr_text_redacts_secrets() -> None:
    """_escape_pr_text must redact sk-... tokens."""
    raw = "Fix: removed sk-abc123XYZ789abcdefghijklmno from config"
    escaped = _escape_pr_text(raw, max_len=3000)
    assert "sk-abc123" not in escaped
    assert "[REDACTED]" in escaped


# ---------------------------------------------------------------------------
# Test 9: open_pr body capped at 3000 chars
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_open_pr_body_capped_at_3000() -> None:
    """PR body > 3000 chars must be truncated to 3000 in the POST payload."""
    captured_body: list[dict] = []

    def _capture_request(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured_body.append(body)
        return httpx.Response(
            201,
            json={"html_url": "https://github.com/test-org/test-repo/pull/1", "number": 1},
        )

    with respx.mock() as rx:
        rx.post("https://api.github.com/repos/test-org/test-repo/pulls").mock(
            side_effect=_capture_request
        )
        await open_pr(
            owner="test-org",
            repo="test-repo",
            head_branch="haunter/fix-abc-1",
            base_branch="main",
            title="fix: valid title",
            body="x" * 5000,  # intentionally too long
            token="fake_token",
        )

    assert len(captured_body) == 1
    assert len(captured_body[0]["body"]) <= 3000


# ---------------------------------------------------------------------------
# Test 10: _escape_pr_text html entities for XSS
# ---------------------------------------------------------------------------

def test_escape_pr_text_entities() -> None:
    """Ampersand, angle brackets → HTML entities."""
    raw = "Fix <foo> & 'bar' injection"
    escaped = _escape_pr_text(raw, max_len=3000)
    assert "&amp;" in escaped
    assert "&lt;foo&gt;" in escaped
    # Quotes are NOT escaped (quote=False in html.escape)
    assert "'" in escaped
