"""
SandboxRunner abstraction — Phase 13.

Defines:
  - SandboxInput: Pydantic-validated inputs; rejects oversized patch and
    repo_ref with disallowed characters BEFORE any provider call.
  - SandboxResult: TypedDict returned by every adapter.
  - SandboxRunner: Abstract base class; both GCPSandboxRunner and
    AWSSandboxRunner implement verify().

Security invariants:
  - patch max 512 KB enforced at validation layer, not inside adapters.
  - repo_ref validated against a strict allowlist (a-z A-Z 0-9 _ . - /)
    and capped at 200 chars to prevent injection into any build API call.
  - reason is always sanitized (secrets stripped, capped 2000 chars) by
    the adapter before being placed in SandboxResult.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, field_validator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_PATCH_BYTES: int = 512 * 1024  # 512 KB
_MAX_REPO_REF_CHARS: int = 200

# repo_ref expected shape: "owner/name[@sha_or_branch]"
# Only alphanumeric, dash, dot, underscore, forward slash, @ are allowed.
# Absolute paths, "../", ".git/", ".github/", and shell metacharacters are
# all outside the character class and will fail fullmatch.
_REPO_REF_RE: re.Pattern[str] = re.compile(
    r"^[a-zA-Z0-9_.\-/]+(?:@[a-zA-Z0-9_.\-/]+)?$"
)

# Banned substrings within repo_ref that an allowlist regex alone won't catch.
_REPO_REF_BANNED: tuple[str, ...] = (
    "..",        # path traversal
    ".git/",     # git internals
    ".github/",  # CI workflow injection
    "//",        # double-slash abuse
)


# ---------------------------------------------------------------------------
# Validated input model
# ---------------------------------------------------------------------------


class SandboxInput(BaseModel):
    """
    Validated inputs for a sandbox verify call.

    Fields are validated before any provider-specific code runs so that
    the adapters can trust that patch and repo_ref are safe to use.

    Optional context fields (user_github_id, file_paths, attempt_number,
    base_sha) are populated by the dispatcher's github_actions branch
    (Phase 2) and ignored by AWSSandboxRunner / GCPSandboxRunner. They
    are optional so the existing dispatch paths keep working unchanged.
    """

    patch: str
    repo_ref: str  # "owner/repo" or "owner/repo@sha"
    run_id: UUID

    # --- Optional context fields (Phase 2: GitHub Actions sandbox) -------
    # user_github_id: needed by GitHubActionsSandboxRunner to derive a
    #   stable, per-user test-mirror repo name (test_repo_name).
    # file_paths: list of file paths from the user's failing run, used by
    #   detect_language() to pick the py / ts workflow template.
    # attempt_number: 1-based attempt index from the Attempt row, used to
    #   name the per-attempt branch (haunter-attempt-{N}).
    # base_sha: commit SHA the new branch should fork from. For the first
    #   attempt on a freshly-created test mirror this is the auto_init
    #   commit; for subsequent attempts it is the prior attempt's HEAD.
    user_github_id: Optional[int] = None
    file_paths: Optional[list[str]] = None
    attempt_number: Optional[int] = None
    base_sha: Optional[str] = None
    # head_sha: the failing commit SHA on the user's repo. Used by the
    #   GitHub Actions runner to seed the test mirror's main branch with the
    #   user's code at this SHA so verification can actually exercise the
    #   failing test (not just the patch in isolation).
    head_sha: Optional[str] = None

    @field_validator("patch")
    @classmethod
    def validate_patch(cls, v: str) -> str:
        raw_bytes = v.encode("utf-8")
        if len(raw_bytes) > _MAX_PATCH_BYTES:
            raise ValueError(
                f"patch exceeds maximum size ({len(raw_bytes)} bytes > "
                f"{_MAX_PATCH_BYTES} bytes)"
            )
        if not v.strip():
            raise ValueError("patch must not be empty")
        return v

    @field_validator("repo_ref")
    @classmethod
    def validate_repo_ref(cls, v: str) -> str:
        if len(v) > _MAX_REPO_REF_CHARS:
            raise ValueError(
                f"repo_ref too long ({len(v)} chars > {_MAX_REPO_REF_CHARS})"
            )
        for banned in _REPO_REF_BANNED:
            if banned in v:
                raise ValueError(
                    f"repo_ref contains disallowed substring: {banned!r}"
                )
        if not _REPO_REF_RE.fullmatch(v):
            raise ValueError(
                f"repo_ref {v!r} contains characters outside the allowed set "
                "[a-zA-Z0-9_.-/@]"
            )
        return v


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


class SandboxResult(dict):
    """
    Return value from SandboxRunner.verify().

    Shape (TypedDict-compatible):
        passed      bool        — True iff tests passed
        reason      str | None  — sanitized failure reason (max 2000 chars)
        duration_ms int         — wall-clock build time in milliseconds
    """


def make_result(
    *,
    passed: bool,
    reason: Optional[str],
    duration_ms: int,
) -> SandboxResult:
    """Construct a SandboxResult with the canonical key names."""
    return SandboxResult(
        passed=passed,
        reason=reason,
        duration_ms=duration_ms,
    )


# ---------------------------------------------------------------------------
# Abstract runner
# ---------------------------------------------------------------------------


class SandboxRunner(ABC):
    """
    Abstract sandbox adapter.

    Implementors must accept a validated SandboxInput and return a
    SandboxResult.  They MUST NOT raise — on any error, return a failed
    SandboxResult with a sanitized reason string.
    """

    @abstractmethod
    async def verify(self, inp: SandboxInput) -> SandboxResult:
        """
        Run the patch in an isolated sandbox and return structured result.

        Must never raise; must sanitize reason before returning.
        """
        ...
