"""
Unit tests for app.sandbox public dispatcher (Phase 5).

Covers:
  - verify() with unknown provider returns fail with "Unknown SANDBOX_PROVIDER..."
  - verify() with github_actions provider lazy-loads runner and returns
    legacy-shape result {status, failure_reason, build_duration_ms}
  - verify() handles runner lazy-import failure ([non-retryable] fail result)
  - verify() handles SandboxInput validation failure (legacy fail result)
  - verify() user_github_id lookup with passed db session (success and exception)
  - _to_legacy() key mapping helper
  - _get_runner() and SANDBOX_PROVIDERS registry
"""

from __future__ import annotations

import uuid
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.sandbox
from app.sandbox import (
    SANDBOX_PROVIDERS,
    _get_runner,
    _load_github_actions_runner,
    _reset_github_actions_runner_cache_for_tests,
    _to_legacy,
    verify,
)
from app.sandbox.runner import make_result


@pytest.fixture(autouse=True)
def reset_runner_cache() -> None:
    """Reset lazy-import cache before and after every test."""
    _reset_github_actions_runner_cache_for_tests()
    yield
    _reset_github_actions_runner_cache_for_tests()


# ---------------------------------------------------------------------------
# Unknown provider test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_unknown_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """app.sandbox.verify() with unknown provider returns fail with expected message."""
    from app.config import settings

    monkeypatch.setattr(settings, "sandbox_provider", "unsupported_cloud")

    mock_attempt = MagicMock()
    mock_attempt.patch_text = "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b"
    mock_run = MagicMock()
    mock_run.id = uuid.uuid4()
    mock_run.head_sha = "abc1234"
    mock_repo = MagicMock()
    mock_repo.owner = "octocat"
    mock_repo.name = "hello"

    result = await verify(attempt=mock_attempt, run=mock_run, repo=mock_repo)

    assert result["status"] == "fail"
    assert "Unknown SANDBOX_PROVIDER='unsupported_cloud'" in result["failure_reason"]
    assert "Must be 'github_actions'." in result["failure_reason"]
    assert result["build_duration_ms"] == 0


# ---------------------------------------------------------------------------
# github_actions provider verification tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_github_actions_success_legacy_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    github_actions provider lazy-loads runner and returns legacy-shape result:
    status='pass', failure_reason=None, build_duration_ms=int.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "sandbox_provider", "github_actions")

    mock_runner_instance = MagicMock()
    mock_runner_instance.verify = AsyncMock(
        return_value=make_result(passed=True, reason=None, duration_ms=1234)
    )
    mock_runner_class = MagicMock(return_value=mock_runner_instance)
    monkeypatch.setattr(app.sandbox, "_load_github_actions_runner", lambda: mock_runner_class)

    mock_attempt = MagicMock()
    mock_attempt.patch_text = "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-a\n+b"
    mock_attempt.attempt_number = 1
    mock_run = MagicMock()
    mock_run.id = uuid.uuid4()
    mock_run.head_sha = "sha12345"
    mock_repo = MagicMock()
    mock_repo.owner = "octocat"
    mock_repo.name = "sandbox-test"

    result = await verify(attempt=mock_attempt, run=mock_run, repo=mock_repo)

    assert result == {
        "status": "pass",
        "failure_reason": None,
        "build_duration_ms": 1234,
    }
    assert mock_runner_instance.verify.called
    inp_passed = mock_runner_instance.verify.call_args[0][0]
    assert inp_passed.repo_ref == "octocat/sandbox-test@sha12345"
    assert inp_passed.attempt_number == 1


@pytest.mark.asyncio
async def test_verify_github_actions_failure_legacy_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """github_actions provider returns status='fail' with reason when tests fail."""
    from app.config import settings

    monkeypatch.setattr(settings, "sandbox_provider", "github_actions")

    mock_runner_instance = MagicMock()
    mock_runner_instance.verify = AsyncMock(
        return_value=make_result(
            passed=False,
            reason="Step 'run pytest': failure",
            duration_ms=4500,
        )
    )
    mock_runner_class = MagicMock(return_value=mock_runner_instance)
    monkeypatch.setattr(app.sandbox, "_load_github_actions_runner", lambda: mock_runner_class)

    mock_attempt = MagicMock()
    mock_attempt.patch_text = "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-a\n+b"
    mock_attempt.attempt_number = 2
    mock_run = MagicMock()
    mock_run.id = uuid.uuid4()
    mock_run.head_sha = None
    mock_repo = MagicMock()
    mock_repo.owner = "octocat"
    mock_repo.name = "sandbox-test"

    result = await verify(attempt=mock_attempt, run=mock_run, repo=mock_repo)

    assert result == {
        "status": "fail",
        "failure_reason": "Step 'run pytest': failure",
        "build_duration_ms": 4500,
    }


# ---------------------------------------------------------------------------
# Lazy-import error and validation error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_lazy_load_import_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """When _load_github_actions_runner raises RuntimeError, verify returns non-retryable fail."""
    from app.config import settings

    monkeypatch.setattr(settings, "sandbox_provider", "github_actions")

    def mock_load_error():
        raise RuntimeError("Missing pyjwt dependency")

    monkeypatch.setattr(app.sandbox, "_load_github_actions_runner", mock_load_error)

    mock_attempt = MagicMock()
    mock_attempt.patch_text = "diff"
    mock_run = MagicMock()
    mock_run.id = uuid.uuid4()
    mock_repo = MagicMock()
    mock_repo.owner = "owner"
    mock_repo.name = "repo"

    result = await verify(attempt=mock_attempt, run=mock_run, repo=mock_repo)
    assert result["status"] == "fail"
    assert "[non-retryable] GitHub Actions sandbox runner unavailable" in result["failure_reason"]
    assert result["build_duration_ms"] == 0


@pytest.mark.asyncio
async def test_verify_sandbox_input_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When SandboxInput validation fails, verify returns sanitized fail result."""
    from app.config import settings

    monkeypatch.setattr(settings, "sandbox_provider", "github_actions")

    mock_attempt = MagicMock()
    mock_attempt.patch_text = ""  # Empty patch -> raises validation error
    mock_run = MagicMock()
    mock_run.id = uuid.uuid4()
    mock_repo = MagicMock()
    mock_repo.owner = "owner"
    mock_repo.name = "repo"

    result = await verify(attempt=mock_attempt, run=mock_run, repo=mock_repo)
    assert result["status"] == "fail"
    assert "Input validation error:" in result["failure_reason"]
    assert result["build_duration_ms"] == 0


# ---------------------------------------------------------------------------
# db session user_github_id resolution in verify()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_db_session_user_github_id_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When db session is passed to verify(), user_github_id is populated from User model."""
    from app.config import settings

    monkeypatch.setattr(settings, "sandbox_provider", "github_actions")

    captured_inputs = []

    class MockRunner:
        async def verify(self, inp):
            captured_inputs.append(inp)
            return make_result(passed=True, reason=None, duration_ms=10)

    monkeypatch.setattr(app.sandbox, "_load_github_actions_runner", lambda: MockRunner)

    mock_db = MagicMock()
    mock_user = MagicMock()
    mock_user.github_id = 554433
    mock_db.get = AsyncMock(return_value=mock_user)

    mock_attempt = MagicMock()
    mock_attempt.patch_text = "diff"
    mock_attempt.attempt_number = 1
    mock_run = MagicMock()
    mock_run.id = uuid.uuid4()
    mock_run.head_sha = "sha"
    mock_repo = MagicMock()
    mock_repo.owner = "owner"
    mock_repo.name = "repo"
    mock_repo.user_id = uuid.uuid4()

    res = await verify(attempt=mock_attempt, run=mock_run, repo=mock_repo, db=mock_db)
    assert res["status"] == "pass"
    assert len(captured_inputs) == 1
    assert captured_inputs[0].user_github_id == 554433

    # Exception in db.get logs warning and falls back to None
    mock_db.get = AsyncMock(side_effect=Exception("DB lookup failed"))
    res_err = await verify(attempt=mock_attempt, run=mock_run, repo=mock_repo, db=mock_db)
    assert res_err["status"] == "pass"
    assert captured_inputs[1].user_github_id is None


# ---------------------------------------------------------------------------
# Registry and helper functions
# ---------------------------------------------------------------------------


def test_to_legacy_key_mapping() -> None:
    """_to_legacy correctly translates canonical keys to legacy keys."""
    canonical_pass = {"passed": True, "reason": None, "duration_ms": 100}
    assert _to_legacy(canonical_pass) == {
        "status": "pass",
        "failure_reason": None,
        "build_duration_ms": 100,
    }

    canonical_fail = {"passed": False, "reason": "failed", "duration_ms": 200}
    assert _to_legacy(canonical_fail) == {
        "status": "fail",
        "failure_reason": "failed",
        "build_duration_ms": 200,
    }


def test_sandbox_providers_registry() -> None:
    """SANDBOX_PROVIDERS registry contains github_actions."""
    assert "github_actions" in SANDBOX_PROVIDERS
    assert SANDBOX_PROVIDERS["github_actions"] == (
        "app.sandbox.github_actions_runner.GitHubActionsSandboxRunner"
    )


def test_get_runner_and_load_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """_get_runner returns runner instance for github_actions; unknown raises ValueError."""
    from app.config import settings

    monkeypatch.setattr(settings, "sandbox_provider", "github_actions")
    runner = _get_runner()
    from app.sandbox.github_actions_runner import GitHubActionsSandboxRunner
    assert isinstance(runner, GitHubActionsSandboxRunner)

    monkeypatch.setattr(settings, "sandbox_provider", "invalid_provider")
    with pytest.raises(ValueError, match="Unknown SANDBOX_PROVIDER='invalid_provider'"):
        _get_runner()
