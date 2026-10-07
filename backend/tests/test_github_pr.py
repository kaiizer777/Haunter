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
    GitHubResourceNotFoundError,
    _TOKEN_CACHE,
    _build_jwt,
    _clear_pem_cache_for_tests,
    _escape_pr_text,
    _parse_patch_files,
    _resolve_app_credentials,
    _resolve_write_credentials,
    commit_patch,
    create_branch,
    get_installation_token,
    open_pr,
    update_branch_ref,
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
            rx.post("https://api.github.com/app/installations/456/access_tokens").mock(
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
# Test 6b: _resolve_app_credentials — write-capable pair only, never auditor
# ---------------------------------------------------------------------------


def _mock_pr_settings(
    app_id=None,
    app_key=None,
    auditor_id=None,
    auditor_key=None,
    ssm_path="",
):
    """Patch app.github.pr.settings with fully explicit attrs (no MagicMock leakage)."""
    patcher = patch("app.github.pr.settings")
    mock_settings = patcher.start()
    mock_settings.github_app_id = app_id
    mock_settings.github_app_private_key = app_key
    mock_settings.github_auditor_app_id = auditor_id
    mock_settings.github_auditor_app_private_key = auditor_key
    mock_settings.github_app_private_key_ssm_path = ssm_path
    return patcher


def test_resolve_app_credentials_explicit_wins() -> None:
    """Explicit write pair set → source=github_app (auditor attrs ignored)."""
    patcher = _mock_pr_settings("explicit-id", "explicit-pem", "auditor-id", "auditor-pem")
    try:
        app_id, key, source = _resolve_app_credentials()
    finally:
        patcher.stop()
    assert (app_id, key, source) == ("explicit-id", "explicit-pem", "github_app")


def test_resolve_app_credentials_never_uses_auditor() -> None:
    """Auditor pair alone is NOT valid for writes → source=none (fail closed).

    The auditor App is read-only; its token lacks contents:write /
    pull_requests:write. PR writes must never be signed with it.
    """
    patcher = _mock_pr_settings(None, None, "auditor-id", "auditor-pem")
    try:
        app_id, key, source = _resolve_app_credentials()
    finally:
        patcher.stop()
    assert (app_id, key, source) == (None, None, "none")


def test_resolve_app_credentials_half_pair_fails_closed() -> None:
    """Half-configured explicit pair (id without key) → none, never auditor.

    Even with a fully configured auditor pair present, a half write pair
    must not silently become an auditor-signed write — that would mask the
    misconfiguration behind a 403 at GitHub.
    """
    patcher = _mock_pr_settings("explicit-id", "   ", "auditor-id", "auditor-pem")
    try:
        app_id, key, source = _resolve_app_credentials()
    finally:
        patcher.stop()
    assert (app_id, key, source) == (None, None, "none")


def test_resolve_app_credentials_none_when_all_missing() -> None:
    """Nothing configured → source=none so callers take the dev GITHUB_TOKEN path."""
    patcher = _mock_pr_settings(None, "", "  ", None)
    try:
        app_id, key, source = _resolve_app_credentials()
    finally:
        patcher.stop()
    assert (app_id, key, source) == (None, None, "none")


@pytest.mark.anyio
async def test_resolve_write_credentials_prefers_env_pair() -> None:
    """Env pair set (+ SSM path set) → env wins, SSM never read."""
    patcher = _mock_pr_settings("env-id", "env-pem", None, None, "/haunter/WRITE_KEY")
    try:
        with patch(
            "app.github.pr._load_pem_from_ssm",
            new_callable=AsyncMock,
        ) as mock_ssm:
            app_id, key, source = await _resolve_write_credentials()
    finally:
        patcher.stop()
        _clear_pem_cache_for_tests()
    assert (app_id, key, source) == ("env-id", "env-pem", "github_app")
    assert not mock_ssm.called


@pytest.mark.anyio
async def test_resolve_write_credentials_ssm_path() -> None:
    """App ID + SSM path (no env PEM) → PEM loaded from SSM (Lambda path)."""
    patcher = _mock_pr_settings("app-id", None, "auditor-id", "auditor-pem", "/haunter/WRITE_KEY")
    try:
        with patch(
            "app.github.pr._load_pem_from_ssm",
            new_callable=AsyncMock,
            return_value="ssm-pem",
        ) as mock_ssm:
            app_id, key, source = await _resolve_write_credentials()
    finally:
        patcher.stop()
        _clear_pem_cache_for_tests()
    assert (app_id, key, source) == ("app-id", "ssm-pem", "github_app_ssm")
    mock_ssm.assert_called_once_with("/haunter/WRITE_KEY")


@pytest.mark.anyio
async def test_resolve_write_credentials_auditor_only_is_none() -> None:
    """Auditor pair alone (no SSM) → none, so writes fail closed."""
    patcher = _mock_pr_settings(None, None, "auditor-id", "auditor-pem", "")
    try:
        app_id, key, source = await _resolve_write_credentials()
    finally:
        patcher.stop()
        _clear_pem_cache_for_tests()
    assert (app_id, key, source) == (None, None, "none")


@pytest.mark.anyio
async def test_get_installation_token_auditor_only_uses_dev_fallback() -> None:
    """Auditor-only config + GITHUB_TOKEN set → dev token, no JWT, no HTTP."""
    repo = _make_repo(install_id=321)
    _TOKEN_CACHE.clear()
    patcher = _mock_pr_settings(None, None, "auditor-id", "auditor-pem", "")
    mock_settings = patcher.start()
    mock_settings.github_token = "ghp_dev_fallback_token"
    try:
        with (
            patch("app.github.pr._build_jwt") as mock_jwt,
            respx.mock(),
        ):
            token = await get_installation_token(repo)
    finally:
        patcher.stop()
    assert token == "ghp_dev_fallback_token"
    assert not mock_jwt.called
    _TOKEN_CACHE.clear()


@pytest.mark.anyio
async def test_get_installation_token_auditor_only_no_token_fails_closed() -> None:
    """Auditor-only config + no GITHUB_TOKEN → explicit GitHubPRError (no silent auditor write)."""
    repo = _make_repo(install_id=322)
    _TOKEN_CACHE.clear()
    patcher = _mock_pr_settings(None, None, "auditor-id", "auditor-pem", "")
    mock_settings = patcher.start()
    mock_settings.github_token = None
    try:
        with pytest.raises(GitHubPRError, match="auditor"):
            await get_installation_token(repo)
    finally:
        patcher.stop()
    _TOKEN_CACHE.clear()


@pytest.mark.anyio
async def test_get_installation_token_uses_ssm_write_key() -> None:
    """App ID + SSM PEM → POSTs with JWT minted from the SSM key."""
    repo = _make_repo(install_id=323)
    _TOKEN_CACHE.clear()
    patcher = _mock_pr_settings("app-id", None, None, None, "/haunter/WRITE_KEY")
    try:
        with (
            patch(
                "app.github.pr._load_pem_from_ssm",
                new_callable=AsyncMock,
                return_value="ssm-pem",
            ),
            patch("app.github.pr._build_jwt", return_value="fake_jwt") as mock_jwt,
        ):
            with respx.mock(assert_all_called=True) as rx:
                rx.post(
                    "https://api.github.com/app/installations/323/access_tokens"
                ).mock(
                    return_value=httpx.Response(
                        201, json={"token": "ghs_ssm_token"}
                    )
                )
                token = await get_installation_token(repo)
    finally:
        patcher.stop()
        _clear_pem_cache_for_tests()
    assert token == "ghs_ssm_token"
    mock_jwt.assert_called_once_with("app-id", "ssm-pem", "github_app_ssm")
    _TOKEN_CACHE.clear()


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
            json={
                "html_url": "https://github.com/test-org/test-repo/pull/1",
                "number": 1,
            },
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


# ---------------------------------------------------------------------------
# _build_jwt: malformed PEM normalizes to GitHubPRError (never AuthError)
# ---------------------------------------------------------------------------


def test_build_jwt_malformed_pem_raises_github_pr_error() -> None:
    """Malformed PEM must raise GitHubPRError (not raw ValueError, not AuthError).

    Covers env + SSM paths: a truncated/rotated SSM value must surface as a
    clear config error with source context so orchestrator handling stays
    consistent. Key material must never appear in the message.
    """
    bad_pem = "not-a-valid-pem"
    with pytest.raises(GitHubPRError, match=r"source=github_app_ssm") as exc_info:
        _build_jwt("app-id", bad_pem, "github_app_ssm")
    assert not isinstance(exc_info.value, GitHubPRAuthError)
    assert bad_pem not in str(exc_info.value)


def test_build_jwt_malformed_env_pem_defaults_source() -> None:
    """Malformed env PEM without explicit source still raises GitHubPRError."""
    with pytest.raises(GitHubPRError, match=r"source=github_app"):
        _build_jwt("app-id", "bogus-key", None)


# ---------------------------------------------------------------------------
# Protected Branches & Tree Deletion Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("branch", ["main", "master", "dev", "develop", "refs/heads/main"])
async def test_create_branch_rejects_protected_branches(branch: str) -> None:
    """create_branch must reject direct creation or write targeting protected branches."""
    with pytest.raises(GitHubPRValidationError, match="protected branch"):
        await create_branch(
            owner="test-org",
            repo="test-repo",
            branch=branch,
            sha="a" * 40,
            token="fake_token",
        )


@pytest.mark.anyio
@pytest.mark.parametrize("branch", ["main", "master", "dev", "develop"])
async def test_commit_patch_rejects_protected_branches(branch: str) -> None:
    """commit_patch must reject commits targeting protected branches."""
    with pytest.raises(GitHubPRValidationError, match="protected branch"):
        await commit_patch(
            owner="test-org",
            repo="test-repo",
            branch=branch,
            patch_text="--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-a\n+b\n",
            commit_msg="fix: something",
            token="fake_token",
        )


@pytest.mark.anyio
@pytest.mark.parametrize("head_branch", ["main", "master", "dev", "develop"])
async def test_open_pr_rejects_protected_head_branch(head_branch: str) -> None:
    """open_pr must reject protected branches as head_branch."""
    with pytest.raises(GitHubPRValidationError, match="protected branch"):
        await open_pr(
            owner="test-org",
            repo="test-repo",
            head_branch=head_branch,
            base_branch="main",
            title="fix: something",
            body="Body text",
            token="fake_token",
        )


def test_parse_patch_files_with_deletion() -> None:
    """_parse_patch_files extracts deleted file path from +++ /dev/null."""
    patch_text = (
        "--- a/deleted_file.py\n"
        "+++ /dev/null\n"
        "@@ -1,3 +0,0 @@\n"
        "-line1\n"
        "-line2\n"
        "-line3\n"
    )
    parsed = _parse_patch_files(patch_text)
    assert "deleted_file.py" in parsed
    assert "+++ /dev/null" in parsed["deleted_file.py"]


@pytest.mark.anyio
async def test_commit_patch_with_deletion_produces_null_sha_tree_entry() -> None:
    """commit_patch for deleted file creates tree entry with sha=None without creating blob."""
    import base64

    deletion_patch = (
        "--- a/obsolete_module.py\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-old_code_1\n"
        "-old_code_2\n"
    )
    captured_tree_payload = []

    def _mock_trees(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        captured_tree_payload.append(data)
        return httpx.Response(201, json={"sha": "new_tree_sha_123"})

    with respx.mock(base_url="https://api.github.com") as rx:
        rx.get("/repos/test-org/test-repo/git/ref/heads/haunter/fix-test").mock(
            return_value=httpx.Response(200, json={"object": {"sha": "head_sha_111"}})
        )
        rx.get("/repos/test-org/test-repo/git/commits/head_sha_111").mock(
            return_value=httpx.Response(200, json={"tree": {"sha": "base_tree_sha_222"}})
        )
        rx.get("/repos/test-org/test-repo/contents/obsolete_module.py?ref=head_sha_111").mock(
            return_value=httpx.Response(
                200,
                json={"content": base64.b64encode(b"old_code_1\nold_code_2\n").decode("ascii")},
            )
        )
        rx.post("/repos/test-org/test-repo/git/trees").mock(
            side_effect=_mock_trees
        )
        rx.post("/repos/test-org/test-repo/git/commits").mock(
            return_value=httpx.Response(201, json={"sha": "new_commit_sha_333"})
        )
        rx.patch("/repos/test-org/test-repo/git/refs/heads/haunter/fix-test").mock(
            return_value=httpx.Response(200, json={"object": {"sha": "new_commit_sha_333"}})
        )

        commit_sha = await commit_patch(
            owner="test-org",
            repo="test-repo",
            branch="haunter/fix-test",
            patch_text=deletion_patch,
            commit_msg="chore: delete obsolete module",
            token="fake_token",
        )

    assert commit_sha == "new_commit_sha_333"
    assert len(captured_tree_payload) == 1
    tree_entries = captured_tree_payload[0]["tree"]
    assert len(tree_entries) == 1
    assert tree_entries[0] == {
        "path": "obsolete_module.py",
        "mode": "100644",
        "type": "blob",
        "sha": None,
    }


@pytest.mark.anyio
async def test_commit_patch_with_deletion_mismatched_hunk_falls_back() -> None:
    """commit_patch with mismatched deletion hunk falls back to haunter.patch."""
    import base64

    deletion_patch = (
        "--- a/obsolete_module.py\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-expected_code_1\n"
        "-expected_code_2\n"
    )
    captured_tree_payload = []

    def _mock_trees(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        captured_tree_payload.append(data)
        return httpx.Response(201, json={"sha": "new_tree_sha_fallback"})

    with respx.mock(base_url="https://api.github.com") as rx:
        rx.get("/repos/test-org/test-repo/git/ref/heads/haunter/fix-test").mock(
            return_value=httpx.Response(200, json={"object": {"sha": "head_sha_111"}})
        )
        rx.get("/repos/test-org/test-repo/git/commits/head_sha_111").mock(
            return_value=httpx.Response(200, json={"tree": {"sha": "base_tree_sha_222"}})
        )
        rx.get("/repos/test-org/test-repo/contents/obsolete_module.py?ref=head_sha_111").mock(
            return_value=httpx.Response(
                200,
                json={"content": base64.b64encode(b"completely_different_code\n").decode("ascii")},
            )
        )
        rx.post("/repos/test-org/test-repo/git/blobs").mock(
            return_value=httpx.Response(201, json={"sha": "blob_sha_fallback"})
        )
        rx.post("/repos/test-org/test-repo/git/trees").mock(
            side_effect=_mock_trees
        )
        rx.post("/repos/test-org/test-repo/git/commits").mock(
            return_value=httpx.Response(201, json={"sha": "new_commit_fallback"})
        )
        rx.patch("/repos/test-org/test-repo/git/refs/heads/haunter/fix-test").mock(
            return_value=httpx.Response(200, json={"object": {"sha": "new_commit_fallback"}})
        )

        commit_sha = await commit_patch(
            owner="test-org",
            repo="test-repo",
            branch="haunter/fix-test",
            patch_text=deletion_patch,
            commit_msg="chore: delete obsolete module",
            token="fake_token",
        )

    assert commit_sha == "new_commit_fallback"
    assert len(captured_tree_payload) == 1
    tree_entries = captured_tree_payload[0]["tree"]
    assert len(tree_entries) == 1
    assert tree_entries[0]["path"] == "haunter.patch"


@pytest.mark.anyio
async def test_update_branch_ref_404_raises_resource_not_found() -> None:
    """update_branch_ref maps 404 to GitHubResourceNotFoundError."""
    with respx.mock(base_url="https://api.github.com") as rx:
        rx.patch("/repos/test-org/test-repo/git/refs/heads/haunter/fix-1").mock(
            return_value=httpx.Response(404, json={"message": "Not Found"})
        )
        with pytest.raises(GitHubResourceNotFoundError, match="Branch ref not found"):
            await update_branch_ref(
                owner="test-org",
                repo="test-repo",
                branch="haunter/fix-1",
                sha="a" * 40,
                token="fake_token",
            )


@pytest.mark.anyio
async def test_update_branch_ref_422_reference_not_exist_raises_resource_not_found() -> None:
    """update_branch_ref maps 422 with 'Reference does not exist' to GitHubResourceNotFoundError."""
    with respx.mock(base_url="https://api.github.com") as rx:
        rx.patch("/repos/test-org/test-repo/git/refs/heads/haunter/fix-1").mock(
            return_value=httpx.Response(
                422, json={"message": "Reference does not exist", "documentation_url": "https://docs.github.com"}
            )
        )
        with pytest.raises(GitHubResourceNotFoundError, match="Branch ref not found"):
            await update_branch_ref(
                owner="test-org",
                repo="test-repo",
                branch="haunter/fix-1",
                sha="a" * 40,
                token="fake_token",
            )


@pytest.mark.anyio
@pytest.mark.parametrize("branch", ["main", "master", "dev", "develop", "refs/heads/main"])
async def test_update_branch_ref_rejects_protected_branches(branch: str) -> None:
    """update_branch_ref rejects direct updates to protected branches."""
    with pytest.raises(GitHubPRValidationError, match="protected branch"):
        await update_branch_ref(
            owner="test-org",
            repo="test-repo",
            branch=branch,
            sha="a" * 40,
            token="fake_token",
        )

