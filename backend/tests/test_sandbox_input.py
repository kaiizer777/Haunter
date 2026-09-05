"""
Unit and security tests for SandboxInput validation (Phase 13).

Covers input sanitization, path traversal defense, shell injection prevention,
and payload boundary enforcement.
"""

from __future__ import annotations

import uuid
import pytest
from pydantic import ValidationError

from app.sandbox.runner import SandboxInput

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
