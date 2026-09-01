"""
Sandbox package — Phase 13.

Public API:
    verify(attempt, run, repo, db=None)   → dict {passed, reason, duration_ms}

The orchestrator imports only this function; it never references
cloudbuild_v1, boto3, pyjwt, or httpx directly.

Provider selection: SANDBOX_PROVIDER env var (default "gcp").
  "gcp"            → GCPSandboxRunner          (Cloud Build via google-cloud-build)
  "aws"            → AWSSandboxRunner          (CodeBuild via boto3)
  "github_actions" → GitHubActionsSandboxRunner
                     (Haunter-org test mirror + GitHub Actions via httpx + pyjwt)
                     Lazy-loaded — see _load_github_actions_runner below.

SandboxInput Pydantic validation runs before the provider is invoked,
rejecting oversized patches and repo_ref with disallowed characters.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Optional
from uuid import UUID

from app.sandbox.runner import SandboxInput, SandboxResult, make_result

if TYPE_CHECKING:
    from app.models import Attempt, Repo, Run
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider registry (informational; the actual dispatch uses _get_runner)
# ---------------------------------------------------------------------------
#
# Maps SANDBOX_PROVIDER string → fully-qualified class path. Defined as
# strings (not imported classes) so the module-level import of
# ``app.sandbox`` does NOT pull in optional dependencies. To resolve a
# class, use ``_load_github_actions_runner()`` (which caches both success
# and failure) or the direct lazy imports in ``_get_runner()``.
#
# Kept as a dict — not the source of truth for dispatch — because:
#   1. It documents the allowed values in one place.
#   2. It gives tests / future hot-reload code a single registry to
#      iterate.
#   3. The spec's STOP check runs `SANDBOX_PROVIDERS.keys()` to confirm
#      all three providers are registered.
SANDBOX_PROVIDERS: dict[str, str] = {
    "gcp": "app.sandbox.verifier.GCPSandboxRunner",
    "aws": "app.sandbox.aws_runner.AWSSandboxRunner",
    "github_actions": "app.sandbox.github_actions_runner.GitHubActionsSandboxRunner",
}


# ---------------------------------------------------------------------------
# Lazy provider registry
# ---------------------------------------------------------------------------
#
# The github_actions provider requires optional dependencies (pyjwt, httpx)
# that are not strictly needed for AWS or GCP. To keep the AWS/GCP paths
# resilient against a broken bundle (e.g. pyjwt missing from a future zip
# build), the github_actions runner is loaded via a guarded helper that
# caches BOTH the class on success AND the import error on failure. AWS
# and GCP imports are already lazy and isolated; this brings github_actions
# to the same level of isolation.
#
# Tested via the smoke check at module import time: importing app.sandbox
# must NOT pull in pyjwt. The first call to the github_actions branch
# triggers the import; if it fails, a clear RuntimeError is raised that
# the verify() outer try/except converts to a sanitized fail-result.

_GITHUB_ACTIONS_RUNNER_CLASS: Optional[type] = None
_GITHUB_ACTIONS_IMPORT_ERROR: Optional[BaseException] = None


def _load_github_actions_runner() -> type:
    """
    Lazy-load GitHubActionsSandboxRunner, with a cached-failure path.

    On first call, attempts the import. If it succeeds, caches the class
    and returns it. If it fails (e.g. ``pyjwt`` not in the bundle),
    caches the error and re-raises it on every subsequent call so the
    dispatcher surfaces a clear, stable failure rather than a fresh
    import error each time.

    The function intentionally raises rather than returning a stub —
    callers (the verify() outer try/except) are responsible for
    converting the failure into a SandboxResult.
    """
    global _GITHUB_ACTIONS_RUNNER_CLASS, _GITHUB_ACTIONS_IMPORT_ERROR
    if _GITHUB_ACTIONS_RUNNER_CLASS is not None:
        return _GITHUB_ACTIONS_RUNNER_CLASS
    if _GITHUB_ACTIONS_IMPORT_ERROR is not None:
        raise _GITHUB_ACTIONS_IMPORT_ERROR
    try:
        from app.sandbox.github_actions_runner import (
            GitHubActionsSandboxRunner,
        )
    except ImportError as exc:
        # Cache the error so subsequent calls are deterministic. Wrap
        # in a RuntimeError so callers get a stable exception type to
        # catch (ImportError leaks the original module name which is
        # internal detail).
        _GITHUB_ACTIONS_IMPORT_ERROR = RuntimeError(
            "GitHub Actions sandbox runner is unavailable — required "
            f"dependency missing ({exc}). Add 'pyjwt' to requirements.txt "
            "or set SANDBOX_PROVIDER=aws/gcp."
        )
        raise _GITHUB_ACTIONS_IMPORT_ERROR from exc
    _GITHUB_ACTIONS_RUNNER_CLASS = GitHubActionsSandboxRunner
    return _GITHUB_ACTIONS_RUNNER_CLASS


def _reset_github_actions_runner_cache_for_tests() -> None:
    """Test-only: clear the cached class / error so the next call re-imports."""
    global _GITHUB_ACTIONS_RUNNER_CLASS, _GITHUB_ACTIONS_IMPORT_ERROR
    _GITHUB_ACTIONS_RUNNER_CLASS = None
    _GITHUB_ACTIONS_IMPORT_ERROR = None


def _get_runner():
    """
    Return the configured SandboxRunner instance based on SANDBOX_PROVIDER.

    Lazy import per provider so unused SDKs are not loaded. The
    github_actions branch delegates to ``_load_github_actions_runner``
    so a missing pyjwt (or any other github_actions dep) does NOT
    affect the AWS or GCP paths.
    """
    from app.config import settings

    provider: str = getattr(settings, "sandbox_provider", "gcp").lower().strip()

    if provider == "aws":
        from app.sandbox.aws_runner import AWSSandboxRunner
        return AWSSandboxRunner()

    if provider == "gcp":
        from app.sandbox.verifier import GCPSandboxRunner
        return GCPSandboxRunner()

    if provider == "github_actions":
        return _load_github_actions_runner()()

    raise ValueError(
        f"Unknown SANDBOX_PROVIDER={provider!r}. "
        "Must be 'gcp', 'aws', or 'github_actions'."
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
    AWSSandboxRunner and GitHubActionsSandboxRunner both receive a
    SandboxInput built from the ORM objects.

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

    # ----------------------------------------------------------------
    # GitHub Actions path — construct SandboxInput with the per-attempt
    # context (attempt_number, base_sha, file_paths, user_github_id).
    # The runner does the GitHub App install-token mint, test-mirror
    # create-or-fetch, workflow + patch push, and check-runs polling.
    #
    # The runner is loaded lazily via _load_github_actions_runner so a
    # missing pyjwt in the bundle does not break the AWS/GCP paths.
    # If the lazy load fails, the outer try/except here converts it
    # to a sanitized fail-result (non-retryable).
    # ----------------------------------------------------------------
    if provider == "github_actions":
        from app.sandbox.verifier import _sanitize_failure_reason

        repo_ref = f"{repo.owner}/{repo.name}"
        sha = getattr(run, "head_sha", None)
        if sha:
            repo_ref = f"{repo_ref}@{sha}"

        try:
            runner_class = _load_github_actions_runner()
        except RuntimeError as exc:
            # Lazy import failed (e.g. pyjwt missing). Surface as a
            # sanitized non-retryable fail so the orchestrator's
            # existing fallback posts a diagnosis comment instead of
            # burning fix attempts on a deterministic config issue.
            logger.error(
                "sandbox: github_actions runner unavailable for run=%s: %s",
                run.id,
                exc,
            )
            return {
                "status": "fail",
                "failure_reason": _sanitize_failure_reason(
                    f"[non-retryable] GitHub Actions sandbox runner "
                    f"unavailable: {str(exc)[:400]}"
                ),
                "build_duration_ms": 0,
            }

        try:
            # Resolve user_github_id from the repo's owner. The runner's
            # _resolve_user_github_id fallback also does this DB walk, but
            # doing it here avoids a second session open inside the runner
            # when db is already live. Falls back to None → runner fallback.
            user_github_id: Optional[int] = None
            if db is not None:
                try:
                    from app.models import User
                    user = await db.get(User, getattr(repo, "user_id", None))
                    if user is not None:
                        user_github_id = int(user.github_id)
                except Exception as _ue:
                    logger.warning(
                        "sandbox: user_github_id lookup failed for run=%s: %s",
                        run.id,
                        _ue,
                    )

            inp = SandboxInput(
                patch=attempt.patch_text or "",
                repo_ref=repo_ref,
                run_id=run.id,
                attempt_number=getattr(attempt, "attempt_number", None),
                user_github_id=user_github_id,
                # head_sha: the failing commit on the user's repo. Used by
                # the GitHub Actions runner to seed the test mirror with
                # the user's code so verification actually exercises the
                # failing test (not just the patch in isolation).
                head_sha=getattr(run, "head_sha", None),
                # base_sha is left None — the runner fetches the test
                # mirror's default branch HEAD.
                # file_paths is left None — the runner extracts them
                # from the patch text as a fallback.
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

        runner = runner_class()
        raw = await runner.verify(inp)
        return _to_legacy(raw)

    # Unknown provider
    logger.error("sandbox: unknown SANDBOX_PROVIDER=%r for run=%s", provider, run.id)
    return {
        "status": "fail",
        "failure_reason": (
            f"Unknown SANDBOX_PROVIDER={provider!r}. "
            "Must be 'gcp', 'aws', or 'github_actions'."
        ),
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
