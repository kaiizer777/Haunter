"""
Unit and security tests for SandboxInput validation (Phase 13).

Covers input sanitization, path traversal defense, shell injection prevention,
and payload boundary enforcement.
"""

from __future__ import annotations

import uuid
import pytest
from pydantic import ValidationError

from app.sandbox.runner import SandboxInput, make_result

VALID_PATCH = "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-x\n+y"


# ---------------------------------------------------------------------------
# Valid inputs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "repo_ref",
    [
        "owner/repo",
        "owner/repo@abc123def456",
        "my.org/my.repo",
        "org-name/repo_name-123",
        "owner/repo@v1.0.0",
    ],
)
def test_sandbox_input_valid(repo_ref: str) -> None:
    """Standard valid repo_ref and patch inputs must be accepted."""
    inp = SandboxInput(
        run_id=uuid.uuid4(),
        repo_ref=repo_ref,
        patch=VALID_PATCH,
    )
    assert inp.repo_ref == repo_ref
    assert inp.patch == VALID_PATCH


# ---------------------------------------------------------------------------
# Path traversal & banned patterns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "traversal_ref",
    [
        "../etc/passwd",
        "owner/../../etc/passwd",
        "owner/.git/config",
        "owner/.github/workflows/ci.yml",
        "owner//repo",
    ],
)
def test_sandbox_input_rejects_path_traversal(traversal_ref: str) -> None:
    """Path traversal and reserved git/github paths must be rejected."""
    with pytest.raises((ValidationError, ValueError)):
        SandboxInput(
            run_id=uuid.uuid4(),
            repo_ref=traversal_ref,
            patch=VALID_PATCH,
        )


# ---------------------------------------------------------------------------
# Shell metacharacters & injection prevention
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "malicious_ref",
    [
        "owner/repo$(id)",
        "owner;malicious",
        "owner/`id`",
        "owner/repo|cmd",
        "owner/repo name",
        "owner/repo&whoami",
        "owner/repo>out",
        "owner/repo\nnewline",
    ],
)
def test_sandbox_input_rejects_shell_metacharacters(malicious_ref: str) -> None:
    """Shell metacharacters in repo_ref must be rejected."""
    with pytest.raises((ValidationError, ValueError)):
        SandboxInput(
            run_id=uuid.uuid4(),
            repo_ref=malicious_ref,
            patch=VALID_PATCH,
        )


# ---------------------------------------------------------------------------
# Boundary & size limits
# ---------------------------------------------------------------------------


def test_sandbox_input_rejects_oversized_patch() -> None:
    """Patches exceeding 512 KB must be rejected."""
    oversized = "x" * (512 * 1024 + 1)
    with pytest.raises((ValidationError, ValueError)):
        SandboxInput(
            run_id=uuid.uuid4(),
            repo_ref="owner/repo",
            patch=oversized,
        )


def test_sandbox_input_rejects_oversized_repo_ref() -> None:
    """Repo refs exceeding 200 characters must be rejected."""
    oversized_ref = "a" * 201
    with pytest.raises((ValidationError, ValueError)):
        SandboxInput(
            run_id=uuid.uuid4(),
            repo_ref=oversized_ref,
            patch=VALID_PATCH,
        )


@pytest.mark.parametrize("empty_patch", ["", "   ", "\n\t  "])
def test_sandbox_input_rejects_empty_patch(empty_patch: str) -> None:
    """Empty or whitespace-only patches must be rejected."""
    with pytest.raises((ValidationError, ValueError)):
        SandboxInput(
            run_id=uuid.uuid4(),
            repo_ref="owner/repo",
            patch=empty_patch,
        )


# ---------------------------------------------------------------------------
# Phase 5 extensions: strict validation & make_result shape
# ---------------------------------------------------------------------------


def test_sandbox_input_patch_exceeding_512kb_raises_validation_error() -> None:
    """SandboxInput with patch > 512 KB raises ValidationError."""
    oversized = "a" * (512 * 1024 + 1)
    with pytest.raises(ValidationError):
        SandboxInput(
            run_id=uuid.uuid4(),
            repo_ref="owner/repo",
            patch=oversized,
        )


def test_sandbox_input_repo_ref_containing_dot_dot_rejected() -> None:
    """repo_ref containing '..' must be rejected with ValidationError."""
    with pytest.raises(ValidationError):
        SandboxInput(
            run_id=uuid.uuid4(),
            repo_ref="owner/..repo",
            patch=VALID_PATCH,
        )


def test_sandbox_input_repo_ref_starting_with_slash_rejected() -> None:
    """repo_ref starting with '/' must be rejected with ValidationError."""
    with pytest.raises(ValidationError):
        SandboxInput(
            run_id=uuid.uuid4(),
            repo_ref="/owner/repo",
            patch=VALID_PATCH,
        )


def test_sandbox_input_repo_ref_containing_semicolon_rejected() -> None:
    """repo_ref containing ';' (shell metachar) must be rejected with ValidationError."""
    with pytest.raises(ValidationError):
        SandboxInput(
            run_id=uuid.uuid4(),
            repo_ref="owner/repo;echo evil",
            patch=VALID_PATCH,
        )


def test_sandbox_input_repo_ref_longer_than_200_chars_rejected() -> None:
    """repo_ref longer than 200 chars must be rejected with ValidationError."""
    long_ref = "a" * 201
    with pytest.raises(ValidationError):
        SandboxInput(
            run_id=uuid.uuid4(),
            repo_ref=long_ref,
            patch=VALID_PATCH,
        )


def test_sandbox_input_valid_owner_name_at_sha_accepted() -> None:
    """Valid owner/name@sha repo_ref must be accepted."""
    repo_ref = "octocat/Hello-World@7fd1a60b01f91b314f59955a4e4d4e80d8edf11d"
    inp = SandboxInput(
        run_id=uuid.uuid4(),
        repo_ref=repo_ref,
        patch=VALID_PATCH,
    )
    assert inp.repo_ref == repo_ref
    assert inp.patch == VALID_PATCH


def test_make_result_returns_expected_dict_shape() -> None:
    """make_result(passed=True, reason='ok', duration_ms=10) returns the expected dict shape."""
    res = make_result(passed=True, reason="ok", duration_ms=10)
    assert res == {
        "passed": True,
        "reason": "ok",
        "duration_ms": 10,
    }
    assert res["passed"] is True
    assert res["reason"] == "ok"
    assert res["duration_ms"] == 10

    # Also test failure shape
    fail_res = make_result(passed=False, reason="error occurred", duration_ms=250)
    assert fail_res["passed"] is False
    assert fail_res["reason"] == "error occurred"
    assert fail_res["duration_ms"] == 250

