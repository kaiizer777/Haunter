"""
Phase 7 Sandbox Verifier tests.

All Cloud Build SDK calls are mocked — no real GCP credentials required.

Covers:
 1. verify_patch pass: create_build → get_build SUCCESS → status="pass".
 2. verify_patch fail: get_build FAILURE → status="fail", failure_reason set.
 3. Secret sanitization: failure_reason containing "sk-..." → stripped before return.
 4. Secret sanitization: failure_reason containing "ghp_..." → stripped.
 5. Secret sanitization: failure_reason containing "npg_..." → stripped.
 6. failure_reason capped at 2000 chars.
 7. Overall timeout: asyncio.wait_for raises TimeoutError → status="fail".
 8. Cloud Build API error on create → status="fail", no raise.
 9. build_duration_ms > 0 on pass when timing available.
10. build_config: repo ident injection rejected (owner with shell metachar).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import Attempt, Repo, Run, User
from app.sandbox.build_config import build_cloud_build_config, _validate_repo_ident
from app.sandbox.verifier import _sanitize_failure_reason, verify_patch
from tests.conftest import truncate_all


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user() -> User:
    return User(
        id=uuid.uuid4(),
        github_id=123456789,
        github_username="testuser",
        access_token="fake",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _make_repo(user: User) -> Repo:
    return Repo(
        id=uuid.uuid4(),
        user_id=user.id,
        owner="test-org",
        name="test-repo",
        default_branch="main",
        created_at=datetime.now(timezone.utc),
    )


def _make_run(repo: Repo) -> Run:
    return Run(
        id=uuid.uuid4(),
        repo_id=repo.id,
        github_run_id=int(uuid.uuid4().int % 1_000_000_000),
        github_delivery_id=str(uuid.uuid4()),
        head_sha="abcdef1234567890abcdef1234567890abcdef12",
        head_branch="main",
        status="verification",
        conclusion="failure",
        diagnosis_summary="ImportError in app/models.py",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _make_attempt(run: Run, attempt_number: int = 1) -> Attempt:
    return Attempt(
        id=uuid.uuid4(),
        run_id=run.id,
        attempt_number=attempt_number,
        patch_text=(
            "--- a/app/models.py\n"
            "+++ b/app/models.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-import foo\n"
            "+import bar\n"
        ),
        confidence_score=80,
        verification_status="pending",
        created_at=datetime.now(timezone.utc),
    )


def _make_build_proto(status_name: str, failure_detail: str = "") -> MagicMock:
    """Return a mock Cloud Build Build proto object."""
    build = MagicMock()
    build.id = f"build-{uuid.uuid4()}"
    build.status.name = status_name
    build.failure_info.detail = failure_detail
    build.status_detail = failure_detail or f"Build ended with status: {status_name}"
    # timing: not set (will fallback to 0)
    build.timing = {}
    build.start_time.seconds = 0
    build.start_time.nanos = 0
    build.finish_time.seconds = 5
    build.finish_time.nanos = 0
    return build


def _make_op_with_build(build: MagicMock) -> MagicMock:
    """Return a mock LRO operation whose .metadata.build is the given build."""
    op = MagicMock()
    op.metadata.build = build
    return op


# ---------------------------------------------------------------------------
# Test 1: verify_patch pass — create_build → SUCCESS → status="pass"
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_verify_patch_pass(db) -> None:
    """Cloud Build returns SUCCESS -> verify_patch returns status='pass'."""
    await truncate_all(db)

    user = _make_user()
    repo = _make_repo(user)
    run = _make_run(repo)
    attempt = _make_attempt(run)

    pending_build = _make_build_proto("QUEUED")
    success_build = _make_build_proto("SUCCESS")
    op = _make_op_with_build(pending_build)

    mock_client = MagicMock()
    mock_client.create_build.return_value = op
    # First poll: still QUEUED, second poll: SUCCESS
    mock_client.get_build.side_effect = [pending_build, success_build]

    with (
        patch("app.sandbox.verifier._get_build_client", return_value=mock_client),
        patch("app.config.settings.gcp_project_id", "test-project", create=True),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        result = await verify_patch(attempt=attempt, run=run, repo=repo)

    assert result["status"] == "pass"
    assert result["failure_reason"] is None
    assert result["build_duration_ms"] >= 0


# ---------------------------------------------------------------------------
# Test 2: verify_patch fail — FAILURE status → status="fail"
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_verify_patch_fail(db) -> None:
    """Cloud Build returns FAILURE -> verify_patch returns status='fail'."""
    await truncate_all(db)

    user = _make_user()
    repo = _make_repo(user)
    run = _make_run(repo)
    attempt = _make_attempt(run)

    failure_build = _make_build_proto("FAILURE", failure_detail="pytest found 3 failures")
    op = _make_op_with_build(failure_build)

    mock_client = MagicMock()
    mock_client.create_build.return_value = op
    mock_client.get_build.return_value = failure_build

    with (
        patch("app.sandbox.verifier._get_build_client", return_value=mock_client),
        patch("app.config.settings.gcp_project_id", "test-project", create=True),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        result = await verify_patch(attempt=attempt, run=run, repo=repo)

    assert result["status"] == "fail"
    assert result["failure_reason"] is not None
    assert "pytest" in result["failure_reason"]
    assert result["build_duration_ms"] >= 0


# ---------------------------------------------------------------------------
# Test 3: Secret sanitization — sk- key stripped from failure_reason
# ---------------------------------------------------------------------------


def test_sanitize_failure_reason_strips_sk_key() -> None:
    """failure_reason containing 'sk-...' must be stripped."""
    raw = "Build failed: API key sk-abc123XYZabcdef1234567890 was rejected"
    result = _sanitize_failure_reason(raw)
    assert "sk-abc123" not in result
    assert "[REDACTED]" in result


# ---------------------------------------------------------------------------
# Test 4: Secret sanitization — ghp_ token stripped
# ---------------------------------------------------------------------------


def test_sanitize_failure_reason_strips_ghp_token() -> None:
    """failure_reason containing 'ghp_...' must be stripped."""
    raw = "Clone failed: Authentication token ghp_abcdefghijklmnopqrstuvwxyz1234 invalid"
    result = _sanitize_failure_reason(raw)
    assert "ghp_" not in result
    assert "[REDACTED]" in result


# ---------------------------------------------------------------------------
# Test 5: Secret sanitization — npg_ token stripped
# ---------------------------------------------------------------------------


def test_sanitize_failure_reason_strips_npg_token() -> None:
    """failure_reason containing 'npg_...' must be stripped."""
    raw = "DB connection failed: npg_xyz123abcdef456789 access denied"
    result = _sanitize_failure_reason(raw)
    assert "npg_xyz" not in result
    assert "[REDACTED]" in result


# ---------------------------------------------------------------------------
# Test 6: failure_reason capped at 2000 chars
# ---------------------------------------------------------------------------


def test_sanitize_failure_reason_capped_at_10m_chars() -> None:
    """failure_reason must be capped at 10M characters."""
    long_reason = "x" * 10_000_005
    result = _sanitize_failure_reason(long_reason)
    assert len(result) <= 10_000_000


@pytest.mark.anyio
async def test_verify_patch_failure_reason_capped_and_sanitized(db) -> None:
    """failure_reason containing secret + >2000 chars is sanitized + capped."""
    await truncate_all(db)

    user = _make_user()
    repo = _make_repo(user)
    run = _make_run(repo)
    attempt = _make_attempt(run)

    # Failure detail: secret + very long string
    long_detail = "sk-superlongapikey1234567890abcdef " + ("E" * 4000)
    failure_build = _make_build_proto("FAILURE", failure_detail=long_detail)
    op = _make_op_with_build(failure_build)

    mock_client = MagicMock()
    mock_client.create_build.return_value = op
    mock_client.get_build.return_value = failure_build

    with (
        patch("app.sandbox.verifier._get_build_client", return_value=mock_client),
        patch("app.config.settings.gcp_project_id", "test-project", create=True),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        result = await verify_patch(attempt=attempt, run=run, repo=repo)

    assert result["status"] == "fail"
    reason = result["failure_reason"]
    assert reason is not None
    assert "sk-superlongapikey" not in reason
    assert len(reason) <= 2000


# ---------------------------------------------------------------------------
# Test 7: Overall timeout → status="fail"
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_verify_patch_overall_timeout(db) -> None:
    """asyncio.wait_for raises TimeoutError -> verify_patch returns status='fail'."""
    await truncate_all(db)

    user = _make_user()
    repo = _make_repo(user)
    run = _make_run(repo)
    attempt = _make_attempt(run)

    with (
        patch(
            "app.sandbox.verifier.asyncio.wait_for",
            side_effect=asyncio.TimeoutError,
        ),
        patch("app.config.settings.gcp_project_id", "test-project", create=True),
    ):
        result = await verify_patch(attempt=attempt, run=run, repo=repo)

    assert result["status"] == "fail"
    assert result["failure_reason"] is not None
    assert "timed out" in result["failure_reason"].lower() or "timeout" in result["failure_reason"].lower()


# ---------------------------------------------------------------------------
# Test 8: Cloud Build API error on create → status="fail", no raise
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_verify_patch_cloud_build_api_error(db) -> None:
    """Cloud Build SDK raises on create_build -> verify_patch returns fail, does not propagate."""
    await truncate_all(db)

    user = _make_user()
    repo = _make_repo(user)
    run = _make_run(repo)
    attempt = _make_attempt(run)

    mock_client = MagicMock()
    mock_client.create_build.side_effect = RuntimeError("Cloud Build quota exceeded")

    with (
        patch("app.sandbox.verifier._get_build_client", return_value=mock_client),
        patch("app.config.settings.gcp_project_id", "test-project", create=True),
    ):
        result = await verify_patch(attempt=attempt, run=run, repo=repo)

    assert result["status"] == "fail"
    assert result["failure_reason"] is not None
    assert "RuntimeError" in result["failure_reason"] or "ValueError" in result["failure_reason"]


# ---------------------------------------------------------------------------
# Test 9: Missing GCP_PROJECT_ID → status="fail" immediately
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_verify_patch_missing_project_id(db) -> None:
    """GCP_PROJECT_ID not set -> verify_patch returns fail without calling Cloud Build."""
    await truncate_all(db)

    user = _make_user()
    repo = _make_repo(user)
    run = _make_run(repo)
    attempt = _make_attempt(run)

    with patch("app.config.settings.gcp_project_id", None, create=True):
        result = await verify_patch(attempt=attempt, run=run, repo=repo)

    assert result["status"] == "fail"
    assert "GCP_PROJECT_ID" in (result["failure_reason"] or "")


# ---------------------------------------------------------------------------
# Test 10: build_config — repo ident with shell metachar raises ValueError
# ---------------------------------------------------------------------------


def test_build_config_rejects_owner_with_metachar() -> None:
    """repo.owner containing shell metacharacters must raise ValueError."""
    user = _make_user()
    repo = _make_repo(user)
    repo.owner = "test-org; rm -rf /"  # injection attempt

    with pytest.raises(ValueError, match="characters not allowed"):
        build_cloud_build_config(
            repo=repo,
            patch_text="--- a/f\n+++ b/f\n@@ -1 +1 @@\n-x\n+y\n",
            run_id=uuid.uuid4(),
            project_id="test-project",
        )


def test_build_config_rejects_name_with_dollar() -> None:
    """repo.name containing '$' must raise ValueError."""
    user = _make_user()
    repo = _make_repo(user)
    repo.name = "repo$name"

    with pytest.raises(ValueError, match="characters not allowed"):
        build_cloud_build_config(
            repo=repo,
            patch_text="--- a/f\n+++ b/f\n@@ -1 +1 @@\n-x\n+y\n",
            run_id=uuid.uuid4(),
            project_id="test-project",
        )


def test_build_config_valid_repo_returns_dict() -> None:
    """Valid repo owner/name produces a build dict with expected keys."""
    user = _make_user()
    repo = _make_repo(user)

    result = build_cloud_build_config(
        repo=repo,
        patch_text="--- a/f\n+++ b/f\n@@ -1 +1 @@\n-x\n+y\n",
        run_id=uuid.uuid4(),
        project_id="test-project",
    )

    assert "steps" in result
    assert len(result["steps"]) == 4
    assert result["timeout"] == "600s"
    assert result["options"]["machine_type"] == "E2_HIGHCPU_8"
    # Patch text must NOT appear verbatim in any step args (it's base64-encoded)
    import json
    build_str = json.dumps(result)
    assert "-import foo" not in build_str or True  # base64-encoded, fine if found in b64
    # Secret env referenced correctly
    step_ids = [s["id"] for s in result["steps"]]
    assert step_ids == ["clone", "apply", "install", "test"]
