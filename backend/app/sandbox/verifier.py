"""
Cloud Build sandbox verifier — Phase 7.

Submits a patch to Cloud Build, polls until terminal state, and returns
a structured result dict that the orchestrator persists to attempts.

Security invariants:
  - Authentication ONLY via google.auth.default() ADC (SA workload identity
    on Cloud Run). No unauthenticated webhook/callback URL for build status
    (prevents spoofed result injection).
  - GITHUB_TOKEN and patch_text are never logged at any level.
  - failure_reason is sanitized (secrets stripped, capped 2000 chars) before
    being stored or returned to the orchestrator.
  - Overall timeout via asyncio.wait_for (15 min) prevents hung polling.
  - PROJECT_ID sourced exclusively from config.py / env — never from request.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Optional
from uuid import UUID

from app.models import Attempt, Repo, Run
from app.sandbox.build_config import build_cloud_build_config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Cloud Build terminal statuses (string form matching the proto enum names).
_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"SUCCESS", "FAILURE", "INTERNAL_ERROR", "TIMEOUT", "CANCELLED", "EXPIRED"}
)

# Polling interval (seconds) between get_build calls.
_POLL_INTERVAL_SECONDS: float = 5.0

# Overall cap: 15 minutes. Cloud Build step timeout is 600s; we add headroom
# for queue wait + startup.
_OVERALL_TIMEOUT_SECONDS: float = 900.0

# Secrets patterns to strip from failure_reason before storage / LLM re-feed.
# Order matters: more specific first.
_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Private keys
    (re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----.*?-----END [A-Z ]+PRIVATE KEY-----", re.DOTALL), "[REDACTED_PRIVATE_KEY]"),
    # API key prefixes — extended to include common token shapes
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{10,}"), "[REDACTED]"),
    (re.compile(r"\bghp_[A-Za-z0-9]{10,}"), "[REDACTED]"),
    (re.compile(r"\bnpg_[A-Za-z0-9_]{8,}"), "[REDACTED]"),
    (re.compile(r"\bghr_[A-Za-z0-9_]{10,}"), "[REDACTED]"),
    # DATABASE_URL / connection strings
    (re.compile(r"(?:DATABASE_URL|postgresql://|postgres://)[^\s\"']+"), "[REDACTED_DB_URL]"),
    # Generic Bearer / Authorization tokens
    (re.compile(r"(?i)(?:bearer|authorization:?\s*bearer)\s+[A-Za-z0-9._\-/+]{20,}"), "[REDACTED_TOKEN]"),
]

_MAX_FAILURE_REASON_CHARS: int = 2000


def _sanitize_failure_reason(raw: str) -> str:
    """
    Strip secret patterns and cap failure_reason at 2000 chars.

    Applied before DB storage AND before passing to the next generate_fix call.
    """
    result = raw
    for pattern, replacement in _SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    if len(result) > _MAX_FAILURE_REASON_CHARS:
        result = result[-_MAX_FAILURE_REASON_CHARS:]
    return result


# ---------------------------------------------------------------------------
# Internal: Cloud Build client helpers
# ---------------------------------------------------------------------------


def _get_build_client():  # type: ignore[return]
    """
    Return a google.cloud.devtools.cloudbuild_v1.services.cloud_build.CloudBuildClient.

    Uses ADC (Application Default Credentials) — on Cloud Run this resolves
    to the attached service account. No credentials are hardcoded.

    Lazy import so tests can mock without installing the SDK.
    """
    from google.cloud.devtools.cloudbuild_v1.services.cloud_build import (
        CloudBuildClient,
    )

    return CloudBuildClient()


def _build_status_str(build) -> str:
    """Extract terminal-comparable status string from a Build proto object."""
    # Build.status is a proto enum; .name gives the string like "SUCCESS".
    try:
        return build.status.name
    except AttributeError:
        return str(build.status)


def _extract_failure_reason(build) -> Optional[str]:
    """
    Extract a short failure summary from a Build object.

    Tries:
      1. build.failure_info.detail (proto field)
      2. build.status_detail (string field)
      3. Last step's logs (unavailable without fetching log URL — skip for now
         to avoid unauthenticated HTTP call; logUrl requires ADC-authenticated fetch)

    Always sanitizes the result before returning.
    """
    raw: Optional[str] = None

    try:
        detail = getattr(build.failure_info, "detail", None)
        if detail:
            raw = str(detail)
    except AttributeError:
        pass

    if not raw:
        try:
            status_detail = getattr(build, "status_detail", None)
            if status_detail:
                raw = str(status_detail)
        except AttributeError:
            pass

    if not raw:
        status = _build_status_str(build)
        raw = f"Build ended with status: {status}"

    return _sanitize_failure_reason(raw)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def verify_patch(
    attempt: Attempt,
    run: Run,
    repo: Repo,
) -> dict:
    """
    Submit patch to Cloud Build and poll until terminal state.

    Returns:
        {
            "status": "pass" | "fail",
            "failure_reason": str | None,   # sanitized, capped 2000 chars
            "build_duration_ms": int,
        }

    Never raises — on Cloud Build API error returns status="fail" with
    failure_reason describing the error so the orchestrator can persist it.
    """
    from app.config import settings

    project_id: Optional[str] = getattr(settings, "gcp_project_id", None)
    if not project_id:
        logger.error(
            "sandbox.verifier: GCP_PROJECT_ID not set — cannot submit Cloud Build job for run=%s",
            run.id,
        )
        return {
            "status": "fail",
            "failure_reason": "GCP_PROJECT_ID not configured on this instance.",
            "build_duration_ms": 0,
        }

    build_config = build_cloud_build_config(
        repo=repo,
        patch_text=attempt.patch_text,
        run_id=run.id,
        project_id=project_id,
    )

    try:
        result = await asyncio.wait_for(
            _run_build(
                project_id=project_id,
                build_config=build_config,
                run_id=run.id,
                attempt_number=attempt.attempt_number,
            ),
            timeout=_OVERALL_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.error(
            "sandbox.verifier: overall timeout (%ss) exceeded for run=%s attempt=%d",
            _OVERALL_TIMEOUT_SECONDS,
            run.id,
            attempt.attempt_number,
        )
        return {
            "status": "fail",
            "failure_reason": f"Sandbox verification timed out after {int(_OVERALL_TIMEOUT_SECONDS)}s.",
            "build_duration_ms": int(_OVERALL_TIMEOUT_SECONDS * 1000),
        }
    except Exception as exc:
        # Cloud Build API error — do not propagate; persist failure.
        # Do NOT log patch_text or GITHUB_TOKEN.
        logger.error(
            "sandbox.verifier: Cloud Build API error for run=%s attempt=%d: %s",
            run.id,
            attempt.attempt_number,
            type(exc).__name__,
        )
        return {
            "status": "fail",
            "failure_reason": _sanitize_failure_reason(
                f"Cloud Build API error: {type(exc).__name__}: {str(exc)[:500]}"
            ),
            "build_duration_ms": 0,
        }

    return result


async def _run_build(
    project_id: str,
    build_config: dict,
    run_id: UUID,
    attempt_number: int,
) -> dict:
    """
    Submit build + poll until terminal. Returns structured result dict.

    Authentication: CloudBuildClient uses ADC (google.auth.default()).
    Polling: asyncio.sleep(_POLL_INTERVAL_SECONDS) between get_build calls.
    All Cloud Build SDK calls are blocking — run in executor to avoid blocking
    the asyncio event loop.
    """
    import functools

    from google.cloud.devtools.cloudbuild_v1 import Build, CreateBuildRequest
    from google.cloud.devtools.cloudbuild_v1.services.cloud_build import (
        CloudBuildClient,
    )

    loop = asyncio.get_running_loop()
    client: CloudBuildClient = await loop.run_in_executor(None, _get_build_client)

    # ----------------------------------------------------------------
    # Submit build
    # ----------------------------------------------------------------
    t_submit = time.monotonic()

    def _create() -> Build:
        req = CreateBuildRequest(project_id=project_id, build=build_config)
        op = client.create_build(request=req)
        # op is a long-running Operation; initial metadata contains the build
        return op.metadata.build

    build: Build = await loop.run_in_executor(None, _create)
    build_id: str = build.id

    logger.info(
        "sandbox.verifier: run=%s attempt=%d submitted build_id=%s",
        run_id,
        attempt_number,
        build_id,
        # Do NOT log patch_text or any substitution values
    )

    # ----------------------------------------------------------------
    # Poll until terminal
    # ----------------------------------------------------------------
    def _get_build() -> Build:
        return client.get_build(project_id=project_id, id=build_id)

    while True:
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)

        build = await loop.run_in_executor(None, _get_build)
        status = _build_status_str(build)

        logger.debug(
            "sandbox.verifier: run=%s build_id=%s status=%s",
            run_id,
            build_id,
            status,
        )

        if status in _TERMINAL_STATUSES:
            break

    # ----------------------------------------------------------------
    # Compute duration
    # ----------------------------------------------------------------
    t_end = time.monotonic()
    elapsed_ms = int((t_end - t_submit) * 1000)

    # Prefer Cloud Build's own timing if available
    try:
        cb_duration_ms = _parse_build_duration_ms(build)
        if cb_duration_ms > 0:
            elapsed_ms = cb_duration_ms
    except Exception:
        pass  # fall back to wall-clock measurement

    # ----------------------------------------------------------------
    # Map terminal status to pass/fail
    # ----------------------------------------------------------------
    if status == "SUCCESS":
        logger.info(
            "sandbox.verifier: run=%s attempt=%d build PASSED (build_id=%s, duration_ms=%d)",
            run_id,
            attempt_number,
            build_id,
            elapsed_ms,
        )
        return {
            "status": "pass",
            "failure_reason": None,
            "build_duration_ms": elapsed_ms,
        }

    reason = _extract_failure_reason(build)
    logger.info(
        "sandbox.verifier: run=%s attempt=%d build FAILED status=%s (build_id=%s)",
        run_id,
        attempt_number,
        status,
        build_id,
    )
    return {
        "status": "fail",
        "failure_reason": reason,
        "build_duration_ms": elapsed_ms,
    }


def _parse_build_duration_ms(build) -> int:
    """
    Extract build duration_ms from Cloud Build proto timing fields.

    Uses build.timing["BUILD"].end_time - build.timing["BUILD"].start_time
    if available, otherwise falls back to finish_time - start_time.
    """
    from google.protobuf.duration_pb2 import Duration  # noqa: F401

    try:
        timing = build.timing
        if timing and "BUILD" in timing:
            build_timing = timing["BUILD"]
            start = build_timing.start_time
            end = build_timing.end_time
            delta_s = (end.seconds - start.seconds) + (end.nanos - start.nanos) / 1e9
            return max(0, int(delta_s * 1000))
    except (AttributeError, KeyError, TypeError):
        pass

    try:
        start = build.start_time
        finish = build.finish_time
        delta_s = (finish.seconds - start.seconds) + (finish.nanos - start.nanos) / 1e9
        return max(0, int(delta_s * 1000))
    except (AttributeError, TypeError):
        pass

    return 0
