"""
Sandbox package — Phase 13.

Public API:
    verify(attempt, run, repo, db=None)   → dict {passed, reason, duration_ms}

The orchestrator imports only this function; it never references
cloudbuild_v1 or boto3 directly.

Provider selection: SANDBOX_PROVIDER env var (default "gcp").
  "gcp"  → GCPSandboxRunner  (Cloud Build via google-cloud-build)
  "aws"  → AWSSandboxRunner  (CodeBuild via boto3)

SandboxInput Pydantic validation runs before the provider is invoked,
rejecting oversized patches and repo_ref with disallowed characters.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional
from uuid import UUID

from app.sandbox.runner import SandboxInput, SandboxResult, make_result

if TYPE_CHECKING:
    from app.models import Attempt, Repo, Run
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def _get_runner():
    """
    Return the configured SandboxRunner instance based on SANDBOX_PROVIDER.

    Lazy import so GCP/AWS SDKs are only loaded when actually needed.
    """
    from app.config import settings

    provider: str = getattr(settings, "sandbox_provider", "gcp").lower().strip()

    if provider == "aws":
        from app.sandbox.aws_runner import AWSSandboxRunner
        return AWSSandboxRunner()

    if provider == "gcp":
        from app.sandbox.verifier import GCPSandboxRunner
        return GCPSandboxRunner()

    raise ValueError(
        f"Unknown SANDBOX_PROVIDER={provider!r}. Must be 'gcp' or 'aws'."
    )


async def verify(
    attempt: "Attempt",
    run: "Run",
    repo: "Repo",
    db: "Optional[AsyncSession]" = None,
) -> dict:
    """
    Run sandbox verification for the given attempt.

    Drop-in replacement for the old `sandbox.verifier.verify_patch(attempt, run, repo)`.
    Return shape is backward-compatible with the orchestrator's existing field
    mapping:
        {
            "status":           "pass" | "fail",   # legacy key — derived from passed
            "failure_reason":   str | None,         # legacy key — from reason
            "build_duration_ms": int,               # legacy key — from duration_ms
        }

    Internally, GCPSandboxRunner.verify_orm() bypasses SandboxInput
    construction and calls verify_patch() with the real ORM objects directly
    (preserves exact Phase 7 behaviour including attempt_number logging).
    AWSSandboxRunner receives a SandboxInput built from the ORM objects.

    Never raises — any error is returned as status="fail" with a sanitized reason.
    """
    from app.config import settings

    provider: str = getattr(settings, "sandbox_provider", "gcp").lower().strip()

    # ----------------------------------------------------------------
    # GCP fast path — pass real ORM objects through to avoid constructing
    # a SandboxInput (which would require repo_ref string assembly).
    # ----------------------------------------------------------------
    if provider == "gcp":
        from app.sandbox.verifier import GCPSandboxRunner
        raw = await GCPSandboxRunner.verify_orm(
            attempt=attempt, run=run, repo=repo
        )
        return _to_legacy(raw)

    # ----------------------------------------------------------------
    # AWS path — construct SandboxInput from ORM objects
    # ----------------------------------------------------------------
    if provider == "aws":
        from app.sandbox.aws_runner import AWSSandboxRunner
        from app.sandbox.verifier import _sanitize_failure_reason

        repo_ref = f"{repo.owner}/{repo.name}"
        sha = getattr(run, "head_sha", None)
        if sha:
            repo_ref = f"{repo_ref}@{sha}"

        try:
            inp = SandboxInput(
                patch=attempt.patch_text or "",
                repo_ref=repo_ref,
                run_id=run.id,
            )
        except Exception as exc:
            logger.error(
                "sandbox: SandboxInput validation failed for run=%s: %s",
                run.id,
                exc,
            )
            return {
                "status": "fail",
                "failure_reason": _sanitize_failure_reason(
                    f"Input validation error: {str(exc)[:400]}"
                ),
                "build_duration_ms": 0,
            }

        runner = AWSSandboxRunner()
        raw = await runner.verify(inp)
        return _to_legacy(raw)

    # Unknown provider
    logger.error("sandbox: unknown SANDBOX_PROVIDER=%r for run=%s", provider, run.id)
    return {
        "status": "fail",
        "failure_reason": f"Unknown SANDBOX_PROVIDER={provider!r}. Must be 'gcp' or 'aws'.",
        "build_duration_ms": 0,
    }


def _to_legacy(result: dict) -> dict:
    """
    Map canonical SandboxResult keys to the legacy keys the orchestrator reads.

    Canonical:  {passed, reason, duration_ms}
    Legacy:     {status, failure_reason, build_duration_ms}
    """
    return {
        "status": "pass" if result.get("passed") else "fail",
        "failure_reason": result.get("reason"),
        "build_duration_ms": result.get("duration_ms", 0),
    }
