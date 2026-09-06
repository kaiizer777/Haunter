"""
Unit and integration tests for app.sandbox.github_actions_runner (Phase 5).

Covers:
  - _load_pem_from_ssm (caching, timeout, boto3 exception)
  - mint_installation_token (fresh mint, caching, near-expiry re-mint)
  - JWT format & RS256 signature verification
  - _is_non_retryable & _non_retryable_reason (phrase matching, truncation, newlines)
  - _push_workflow_file (first PUT success, 403 PAT fallback, 403 without fallback)
  - _list_workflow_runs (empty list, populated)
  - _get_workflow_run_log_tail (failed steps summary, empty jobs, error fallback)
  - _resolve_user_github_id (valid chain, bad UUID, missing entities, DB error)
  - _extract_file_paths_from_patch (dedup, /dev/null skip, empty patch)
  - _load_workflow_template (content load, missing template FileNotFoundError)
  - GitHubActionsSandboxRunner.verify() fast-fail paths
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.sandbox.github_actions_runner import (
    _NON_RETRYABLE_GITHUB_PHRASES,
    _clear_token_cache_for_tests,
    _extract_file_paths_from_patch,
    _get_workflow_run_log_tail,
    _is_non_retryable,
    _list_workflow_runs,
    _load_pem_from_ssm,
    _load_workflow_template,
    _non_retryable_reason,
    _push_workflow_file,
    _resolve_user_github_id,
    GitHubActionsSandboxRunner,
    mint_installation_token,
)
from app.sandbox.runner import SandboxInput


# ---------------------------------------------------------------------------
# Test fixtures & RSA key generation
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rsa_keys() -> tuple[str, str]:
    """Generate a valid RSA private/public key pair (PEM format) for JWT tests."""
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    priv_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    pub_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    return priv_pem, pub_pem


@pytest.fixture(autouse=True)
def reset_module_caches() -> None:
    """Clear runner token and PEM caches before and after every test."""
    _clear_token_cache_for_tests()
    yield
    _clear_token_cache_for_tests()


# ---------------------------------------------------------------------------
# _load_pem_from_ssm tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_pem_from_ssm_caching(rsa_keys: tuple[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    """_load_pem_from_ssm caches PEM; second call returns cached value without boto3."""
    priv_pem, _ = rsa_keys
    mock_client = MagicMock()
    mock_client.get_parameter.return_value = {"Parameter": {"Value": priv_pem}}

    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = mock_client
    monkeypatch.setattr("boto3.client", mock_boto3.client)

    # First call loads from SSM
    pem1 = await _load_pem_from_ssm("/test/key")
    assert pem1 == priv_pem
    assert mock_client.get_parameter.call_count == 1

    # Second call hits _PEM_CACHE directly
    pem2 = await _load_pem_from_ssm("/test/key")
    assert pem2 == priv_pem
    assert mock_client.get_parameter.call_count == 1  # No additional SSM call


@pytest.mark.asyncio
async def test_load_pem_from_ssm_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """SSM asyncio.TimeoutError raises RuntimeError."""
    async def _mock_wait_for(coro, timeout):
        try:
            coro.close()
        except Exception:
            pass
        raise asyncio.TimeoutError()

    monkeypatch.setattr(asyncio, "wait_for", _mock_wait_for)
    with pytest.raises(RuntimeError, match="SSM get_parameter timed out"):
        await _load_pem_from_ssm("/test/timeout_key")


@pytest.mark.asyncio
async def test_load_pem_from_ssm_boto3_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """SSM boto3 exception raises RuntimeError with wrapped error."""
    mock_client = MagicMock()
    mock_client.get_parameter.side_effect = Exception("SSM ParameterNotFound")

    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = mock_client
    monkeypatch.setattr("boto3.client", mock_boto3.client)

    with pytest.raises(RuntimeError, match="SSM get_parameter failed"):
        await _load_pem_from_ssm("/test/missing_key")


# ---------------------------------------------------------------------------
# mint_installation_token tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mint_installation_token_fresh_mint(
    rsa_keys: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh mint POSTs to /app/installations/{id}/access_tokens with Bearer <jwt>."""
    priv_pem, pub_pem = rsa_keys
    monkeypatch.setattr(
        "app.sandbox.github_actions_runner._load_pem_from_ssm",
        AsyncMock(return_value=priv_pem),
    )

    with respx.mock(base_url="https://api.github.com") as respx_mock:
        route = respx_mock.post("/app/installations/12345/access_tokens").respond(
            status_code=201,
            json={"token": "ghs_fresh_token", "expires_at": "2026-09-06T15:00:00Z"},
        )

        token = await mint_installation_token("app_99", "12345", "/ssm/key")
        assert token == "ghs_fresh_token"
        assert route.call_count == 1

        # Check Authorization header format
        auth_hdr = route.calls.last.request.headers["Authorization"]
        assert auth_hdr.startswith("Bearer ")
        raw_jwt = auth_hdr.split(" ", 1)[1]

        # Check JWT format & signature
        header = jwt.get_unverified_header(raw_jwt)
        assert header == {"alg": "RS256", "typ": "JWT"}

        payload = jwt.decode(raw_jwt, pub_pem, algorithms=["RS256"])
        assert payload["iss"] == "app_99"
        assert "iat" in payload
        assert "exp" in payload
        assert payload["exp"] > payload["iat"]


@pytest.mark.asyncio
async def test_mint_installation_token_cache_and_near_expiry_remint(
    rsa_keys: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cached token returned without network call; re-minted when near expiry."""
    priv_pem, _ = rsa_keys
    monkeypatch.setattr(
        "app.sandbox.github_actions_runner._load_pem_from_ssm",
        AsyncMock(return_value=priv_pem),
    )

    base_time = 1000.0
    current_time = base_time

    def fake_monotonic() -> float:
        return current_time

    monkeypatch.setattr(time, "monotonic", fake_monotonic)

    with respx.mock(base_url="https://api.github.com") as respx_mock:
        route = respx_mock.post("/app/installations/789/access_tokens")
        route.side_effect = [
            httpx.Response(201, json={"token": "ghs_token_1"}),
            httpx.Response(201, json={"token": "ghs_token_2"}),
        ]

        # 1. Fresh mint
        token1 = await mint_installation_token("app_1", "789", "/ssm/key")
        assert token1 == "ghs_token_1"
        assert route.call_count == 1

        # 2. Immediate second call -> cached token, 0 network calls
        token2 = await mint_installation_token("app_1", "789", "/ssm/key")
        assert token2 == "ghs_token_1"
        assert route.call_count == 1

        # 3. Advance time to near expiry (validity is 3600s, margin is 300s -> advance 3350s)
        current_time += 3350.0

        # Should re-mint
        token3 = await mint_installation_token("app_1", "789", "/ssm/key")
        assert token3 == "ghs_token_2"
        assert route.call_count == 2


# ---------------------------------------------------------------------------
# _is_non_retryable & _non_retryable_reason tests
# ---------------------------------------------------------------------------


def test_non_retryable_phrases_classification() -> None:
    """Every phrase in _NON_RETRYABLE_GITHUB_PHRASES triggers [non-retryable]."""
    for phrase in _NON_RETRYABLE_GITHUB_PHRASES:
        assert _is_non_retryable(phrase) is True

        req = httpx.Request("GET", "https://api.github.com/test")
        resp = httpx.Response(403, request=req, text=f"Error: {phrase}")
        exc = httpx.HTTPStatusError("Forbidden", request=req, response=resp)

        reason = _non_retryable_reason(exc, prefix="GitHub API")
        assert reason.startswith("[non-retryable] GitHub API:"), (
            f"Phrase {phrase!r} failed to produce [non-retryable] prefix"
        )


def test_unrelated_error_not_marked_non_retryable() -> None:
    """Unrelated error body (e.g. 404 not found) is not marked [non-retryable]."""
    req = httpx.Request("GET", "https://api.github.com/repos/unknown")
    resp = httpx.Response(404, request=req, text="Not Found")
    exc = httpx.HTTPStatusError("Not Found", request=req, response=resp)

    reason = _non_retryable_reason(exc, prefix="GitHub API")
    assert not reason.startswith("[non-retryable]")
    assert reason.startswith("GitHub API: HTTPStatusError")


def test_non_retryable_reason_truncation_and_newlines() -> None:
    """_non_retryable_reason truncates body at 300 chars and replaces newlines with space."""
    long_body = ("line1\nline2\nline3 " + ("x" * 400))
    req = httpx.Request("POST", "https://api.github.com/test")
    resp = httpx.Response(400, request=req, text=long_body)
    exc = httpx.HTTPStatusError("Bad Request", request=req, response=resp)

    reason = _non_retryable_reason(exc, prefix="GitHub API")
    assert "\n" not in reason
    # Body snippet must be truncated
    body_part = reason.split("| body: ", 1)[1]
    assert len(body_part) <= 300


# ---------------------------------------------------------------------------
# _push_workflow_file tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_workflow_file_first_put_success() -> None:
    """_push_workflow_file returns new commit SHA on 200 OK."""
    with respx.mock(base_url="https://api.github.com") as respx_mock:
        # GET contents: file does not exist yet (404)
        respx_mock.get("/repos/owner/repo/contents/.github/workflows/ci.yml").respond(404)
        # PUT contents: creates file (201/200)
        respx_mock.put("/repos/owner/repo/contents/.github/workflows/ci.yml").respond(
            200,
            json={"commit": {"sha": "sha_new_commit_123"}},
        )

        async with httpx.AsyncClient() as client:
            commit_sha = await _push_workflow_file(
                client=client,
                repo_full="owner/repo",
                base_sha="base_sha",
                workflow_filename="ci.yml",
                workflow_content="name: CI\n",
                token="token_app",
            )

        assert commit_sha == "sha_new_commit_123"


@pytest.mark.asyncio
async def test_push_workflow_file_403_fallback_to_pat() -> None:
    """_push_workflow_file retries with fallback_token on 403."""
    with respx.mock(base_url="https://api.github.com") as respx_mock:
        # GET contents: existing blob
        respx_mock.get("/repos/owner/repo/contents/.github/workflows/ci.yml").respond(
            200, json={"sha": "existing_blob_sha"}
        )
        # First PUT with App token: 403
        # Second PUT with PAT: 200
        put_route = respx_mock.put(
            "/repos/owner/repo/contents/.github/workflows/ci.yml"
        )
        put_route.side_effect = [
            httpx.Response(403, json={"message": "Resource not accessible by integration"}),
            httpx.Response(200, json={"commit": {"sha": "sha_pat_commit_456"}}),
        ]

        async with httpx.AsyncClient() as client:
            commit_sha = await _push_workflow_file(
                client=client,
                repo_full="owner/repo",
                base_sha="base_sha",
                workflow_filename="ci.yml",
                workflow_content="name: CI\n",
                token="token_app",
                fallback_token="token_pat",
            )

        assert commit_sha == "sha_pat_commit_456"
        assert put_route.call_count == 2
        # Verify second call used PAT
        second_call = put_route.calls[1]
        assert second_call.request.headers["Authorization"] == "Bearer token_pat"


@pytest.mark.asyncio
async def test_push_workflow_file_403_without_fallback_raises() -> None:
    """_push_workflow_file raises HTTPStatusError when 403 occurs without fallback_token."""
    with respx.mock(base_url="https://api.github.com") as respx_mock:
        respx_mock.get("/repos/owner/repo/contents/.github/workflows/ci.yml").respond(404)
        respx_mock.put("/repos/owner/repo/contents/.github/workflows/ci.yml").respond(
            403, json={"message": "Forbidden"}
        )

        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.HTTPStatusError):
                await _push_workflow_file(
                    client=client,
                    repo_full="owner/repo",
                    base_sha="base_sha",
                    workflow_filename="ci.yml",
                    workflow_content="name: CI\n",
                    token="token_app",
                    fallback_token=None,
                )


# ---------------------------------------------------------------------------
# _list_workflow_runs tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_workflow_runs() -> None:
    """_list_workflow_runs returns empty list or populated list."""
    with respx.mock(base_url="https://api.github.com") as respx_mock:
        route = respx_mock.get("/repos/owner/repo/actions/runs").side_effect = [
            httpx.Response(200, json={"workflow_runs": []}),
            httpx.Response(200, json={"workflow_runs": [{"id": 1001, "status": "completed"}]}),
        ]

        async with httpx.AsyncClient() as client:
            # 1. Empty list
            runs1 = await _list_workflow_runs(client, "owner/repo", "sha1", token="tok")
            assert runs1 == []

            # 2. Populated list
            runs2 = await _list_workflow_runs(client, "owner/repo", "sha2", token="tok")
            assert len(runs2) == 1
            assert runs2[0]["id"] == 1001


# ---------------------------------------------------------------------------
# _get_workflow_run_log_tail tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_workflow_run_log_tail_success_and_failures() -> None:
    """_get_workflow_run_log_tail summarizes failed steps and handles errors gracefully."""
    with respx.mock(base_url="https://api.github.com") as respx_mock:
        # Case 1: Jobs with failed step
        respx_mock.get("/repos/owner/repo/actions/runs/1/jobs").respond(
            200,
            json={
                "jobs": [
                    {
                        "name": "build-job",
                        "conclusion": "failure",
                        "steps": [
                            {"name": "checkout", "conclusion": "success"},
                            {"name": "run pytest", "conclusion": "failure"},
                        ],
                    }
                ]
            },
        )
        # Case 2: Empty jobs list
        respx_mock.get("/repos/owner/repo/actions/runs/2/jobs").respond(
            200, json={"jobs": []}
        )
        # Case 3: Network error
        respx_mock.get("/repos/owner/repo/actions/runs/3/jobs").respond(500)

        async with httpx.AsyncClient() as client:
            # 1. Failed steps summary
            summary1 = await _get_workflow_run_log_tail(client, "owner/repo", 1, token="tok")
            assert "Job 'build-job': failure" in summary1
            assert "Step 'run pytest': failure" in summary1
            assert "checkout" not in summary1

            # 2. Empty jobs
            summary2 = await _get_workflow_run_log_tail(client, "owner/repo", 2, token="tok")
            assert summary2 == "Workflow run #2: no job details available."

            # 3. Exception path: truncated error message
            summary3 = await _get_workflow_run_log_tail(client, "owner/repo", 3, token="tok")
            assert "Workflow run #3: log fetch failed (" in summary3


# ---------------------------------------------------------------------------
# _resolve_user_github_id tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_user_github_id_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test valid chain, bad UUID, missing entities, and DB error handling."""
    # Bad UUID -> returns None
    assert await _resolve_user_github_id("invalid-uuid") is None

    # Valid UUID but DB lookup variations
    valid_uuid = uuid.uuid4()
    repo_uuid = uuid.uuid4()
    user_uuid = uuid.uuid4()

    mock_run = MagicMock()
    mock_run.repo_id = repo_uuid

    mock_repo = MagicMock()
    mock_repo.user_id = user_uuid

    mock_user = MagicMock()
    mock_user.github_id = 987654

    # 1. Valid chain
    class FakeSessionSuccess:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

        async def get(self, model, ident):
            if ident == valid_uuid:
                return mock_run
            if ident == repo_uuid:
                return mock_repo
            if ident == user_uuid:
                return mock_user
            return None

    monkeypatch.setattr(
        "app.db.async_session_maker",
        lambda: FakeSessionSuccess(),
    )
    result = await _resolve_user_github_id(valid_uuid)
    assert result == 987654

    # 2. Missing run -> None
    class FakeSessionMissingRun(FakeSessionSuccess):
        async def get(self, model, ident):
            return None

    monkeypatch.setattr("app.db.async_session_maker", lambda: FakeSessionMissingRun())
    assert await _resolve_user_github_id(valid_uuid) is None

    # 3. Missing repo -> None
    class FakeSessionMissingRepo(FakeSessionSuccess):
        async def get(self, model, ident):
            if ident == valid_uuid:
                return mock_run
            return None

    monkeypatch.setattr("app.db.async_session_maker", lambda: FakeSessionMissingRepo())
    assert await _resolve_user_github_id(valid_uuid) is None

    # 4. Missing user -> None
    class FakeSessionMissingUser(FakeSessionSuccess):
        async def get(self, model, ident):
            if ident == valid_uuid:
                return mock_run
            if ident == repo_uuid:
                return mock_repo
            return None

    monkeypatch.setattr("app.db.async_session_maker", lambda: FakeSessionMissingUser())
    assert await _resolve_user_github_id(valid_uuid) is None

    # 5. DB error -> None
    class FakeSessionError:
        async def __aenter__(self):
            raise RuntimeError("Database connection pool exhausted")

        async def __aexit__(self, exc_type, exc, tb):
            pass

    monkeypatch.setattr("app.db.async_session_maker", lambda: FakeSessionError())
    assert await _resolve_user_github_id(valid_uuid) is None


# ---------------------------------------------------------------------------
# _extract_file_paths_from_patch tests
# ---------------------------------------------------------------------------


def test_extract_file_paths_from_patch() -> None:
    """_extract_file_paths_from_patch deduplicates, skips /dev/null, ignores non-+++ lines."""
    patch_text = """
diff --git a/app/main.py b/app/main.py
--- a/app/main.py
+++ b/app/main.py
@@ -1,3 +1,4 @@
+print("hi")
diff --git a/deleted.py b/dev/null
--- a/deleted.py
+++ /dev/null
@@ -1 +0,0 @@
diff --git a/app/main.py b/app/main.py
--- a/app/main.py
+++ b/app/main.py
@@ -10,3 +11,4 @@
+print("duplicate")
diff --git a/src/lib.py b/src/lib.py
--- a/src/lib.py
+++ src/lib.py
@@ -1 +1 @@
"""
    paths = _extract_file_paths_from_patch(patch_text)
    assert paths == ["app/main.py", "src/lib.py"]

    # Empty patch returns empty list
    assert _extract_file_paths_from_patch("") == []
    assert _extract_file_paths_from_patch(None) == []


# ---------------------------------------------------------------------------
# _load_workflow_template tests
# ---------------------------------------------------------------------------


def test_load_workflow_template() -> None:
    """_load_workflow_template loads template content; missing file raises FileNotFoundError."""
    py_content = _load_workflow_template("haunter-test-py.yml")
    assert "pytest" in py_content

    missing_name = "nonexistent_workflow_template_12345.yml"
    with pytest.raises(FileNotFoundError) as exc_info:
        _load_workflow_template(missing_name)
    assert missing_name in str(exc_info.value)


# ---------------------------------------------------------------------------
# GitHubActionsSandboxRunner.verify() fast-fail paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_fast_fail_missing_app_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing github_sandbox_app_id returns [non-retryable] reason."""
    from app.config import settings

    monkeypatch.setattr(settings, "github_sandbox_app_id", None)
    monkeypatch.setattr(settings, "github_sandbox_installation_id", "inst_123")

    runner = GitHubActionsSandboxRunner()
    inp = SandboxInput(
        run_id=uuid.uuid4(),
        repo_ref="owner/repo",
        patch="diff",
        attempt_number=1,
        user_github_id=123,
    )
    result = await runner.verify(inp)
    assert result["passed"] is False
    assert "[non-retryable] GITHUB_SANDBOX_APP_ID not configured." in result["reason"]


@pytest.mark.asyncio
async def test_verify_fast_fail_missing_installation_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing github_sandbox_installation_id returns [non-retryable] reason."""
    from app.config import settings

    monkeypatch.setattr(settings, "github_sandbox_app_id", "app_123")
    monkeypatch.setattr(settings, "github_sandbox_installation_id", None)

    runner = GitHubActionsSandboxRunner()
    inp = SandboxInput(
        run_id=uuid.uuid4(),
        repo_ref="owner/repo",
        patch="diff",
        attempt_number=1,
        user_github_id=123,
    )
    result = await runner.verify(inp)
    assert result["passed"] is False
    assert "[non-retryable] GITHUB_SANDBOX_INSTALLATION_ID not configured." in result["reason"]


@pytest.mark.asyncio
async def test_verify_fast_fail_token_mint_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token mint raises HTTPStatusError -> result with [non-retryable] GitHub API: ... and passed=False."""
    from app.config import settings

    monkeypatch.setattr(settings, "github_sandbox_app_id", "app_123")
    monkeypatch.setattr(settings, "github_sandbox_installation_id", "inst_123")

    # Mock mint_installation_token to raise a 401 Bad credentials error
    req = httpx.Request("POST", "https://api.github.com/app/installations/123/access_tokens")
    resp = httpx.Response(401, request=req, text='{"message": "Bad credentials"}')
    status_error = httpx.HTTPStatusError("Bad credentials", request=req, response=resp)

    monkeypatch.setattr(
        "app.sandbox.github_actions_runner.mint_installation_token",
        AsyncMock(side_effect=status_error),
    )

    runner = GitHubActionsSandboxRunner()
    inp = SandboxInput(
        run_id=uuid.uuid4(),
        repo_ref="owner/repo",
        patch="diff",
        attempt_number=1,
        user_github_id=123,
    )
    result = await runner.verify(inp)
    assert result["passed"] is False
    assert result["reason"].startswith("[non-retryable] GitHub API:")
    assert "Bad credentials" in result["reason"]
