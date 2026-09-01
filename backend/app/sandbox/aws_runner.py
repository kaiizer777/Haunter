"""
AWS CodeBuild sandbox adapter — Phase 13.

Implements SandboxRunner using AWS CodeBuild (EC2 general1.small tier).
EC2 tier chosen over Lambda tier because:
  - Lambda has a hard 15-minute cap and no Docker-in-Docker support.
  - general1.small supports DinD via privileged mode when the target repo
    has its own Dockerfile (HAUNTER.md:131).

Security invariants:
  - GITHUB_TOKEN resolved from SSM Parameter Store via CodeBuild env var
    binding — never passed as environmentVariablesOverride.
  - PATCH_B64, REPO_OWNER, REPO_NAME validated against allowlist before
    being passed as environmentVariablesOverride (mirrors build_config.py:38).
  - All boto3 calls run in asyncio.run_in_executor (never blocks event loop).
  - Poll-only (no callback URL) — batch_get_builds uses AWS SigV4.
  - failure reason sanitized via shared _sanitize_failure_reason before return.
  - Per-build timeouts (build_timeout=10, queued_timeout=5) are configured at
    the CodeBuild PROJECT level in infra/aws/codebuild.tf — they are NOT
    overridable per StartBuild call. The StartBuild API only accepts the
    "Override" suffix variants (timeoutInMinutesOverride,
    queuedTimeoutInMinutesOverride); passing the bare names was triggering
    ParamValidationError and aborting every build before it could start.
  - Lazy boto3 import — zero overhead when SANDBOX_PROVIDER=gcp.
  - Non-retryable AWS errors (AccountLimitExceededException, etc.) are
    flagged in the failure_reason so the orchestrator's existing fallback
    path produces a clear diagnosis-only comment instead of burning fix
    attempts on a quota issue that only AWS Support can resolve.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from typing import Optional

from app.sandbox.runner import SandboxInput, SandboxResult, SandboxRunner, make_result
from app.sandbox.runner import _sanitize_failure_reason

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_POLL_INTERVAL_SECONDS: float = 5.0
_OVERALL_TIMEOUT_SECONDS: float = 900.0  # wall-clock cap (build itself is 10 min)

_CODEBUILD_TERMINAL: frozenset[str] = frozenset(
    {"SUCCEEDED", "FAILED", "FAULT", "TIMED_OUT", "STOPPED"}
)

# Allowlist for env var values sent via environmentVariablesOverride.
# Only alphanumeric, dash, underscore, dot, slash, @ — no shell metacharacters.
_ENV_VAR_VALUE_RE: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9_.\-/@=+]+$")

# Maximum length for individual env var values (set to 10M for testing / large patches).
_MAX_ENV_VAR_VALUE_LEN: int = 10_000_000

# ---------------------------------------------------------------------------
# BuildSpec — embedded as a Python constant to avoid deploy-time file lookup.
# Language detection mirrors build_config.py step logic (HAUNTER.md:131).
# ---------------------------------------------------------------------------

_BUILDSPEC = """version: 0.2
phases:
  install:
    runtime-versions:
      # Node 20 is the highest LTS available on aws/codebuild/standard:7.0
      # (Ubuntu 22.04). Pinned here so the image's pre-installed Node cannot
      # silently regress between builds. Mirrors the project-level
      # runtime-versions in infra/aws/codebuild.tf (NICE-3 / Phase 4).
      nodejs: 20
      python: 3.12
  build:
    commands:
      - git clone --depth 1 "https://x-access-token:${GITHUB_TOKEN}@github.com/${REPO_OWNER}/${REPO_NAME}.git" .
      - echo "${PATCH_B64}" | base64 -d > /tmp/haunter.patch
      - git apply --check /tmp/haunter.patch
      - git apply /tmp/haunter.patch
      - |
        if [ -f Dockerfile ]; then
          docker build -t haunter-sandbox-image .
        elif [ -f requirements.txt ]; then
          pip install -q -r requirements.txt
        elif [ -f package.json ]; then
          npm ci --ignore-scripts
        else
          echo "No recognised dependency manifest — skipping install."
        fi
      - |
        if [ -f Dockerfile ]; then
          docker run --rm haunter-sandbox-image pytest -q 2>&1 || \
          docker run --rm haunter-sandbox-image python -m pytest -q 2>&1
        elif [ -f pytest.ini ] || [ -f pyproject.toml ] || [ -f setup.cfg ] || [ -f requirements.txt ]; then
          pip install -q pytest && pytest -q
        elif [ -f package.json ]; then
          npm test
        else
          python -m pytest -q
        fi
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_env_var_value(value: str, field: str) -> None:
    """Raise ValueError if *value* is outside the allowed character set."""
    if len(value) > _MAX_ENV_VAR_VALUE_LEN:
        raise ValueError(
            f"CodeBuild env var {field!r} exceeds max length "
            f"({len(value)} > {_MAX_ENV_VAR_VALUE_LEN})"
        )
    # Base64 strings use A-Z a-z 0-9 + / = — all within allowlist.
    # REPO_OWNER and REPO_NAME are already validated in SandboxInput.repo_ref.
    if not _ENV_VAR_VALUE_RE.fullmatch(value):
        raise ValueError(
            f"CodeBuild env var {field!r} contains disallowed characters"
        )


# Substrings in boto3 ClientError that indicate a deterministic,
# non-retryable AWS-side failure. When we see these we should fail
# fast instead of letting the orchestrator burn fix_generator
# attempts on something only an AWS Support case (or quota raise)
# can resolve.
#
# AccountLimitExceededException is the production case from the
# account-level concurrent-build quota = 0 in us-east-1. The other
# entries are defensive for sibling errors a new account might
# surface.
_NON_RETRYABLE_AWS_ERROR_PHRASES: tuple[str, ...] = (
    "accountlimitexceededexception",
    "cannot have more than 0 builds",
    "cannot have more than 0 active builds",
    "requestlimitexceeded",
    "unauthorizedoperation",
    "accessdenied",
)


def _extract_failure_reason_aws(build: dict) -> str:
    """
    Extract a short failure summary from a CodeBuild build dict.

    Tries (in order):
      1. Phase-level quota / limit message in any phase
      2. phases[last].contexts[last].message
      3. buildStatus string

    Always sanitizes via _sanitize_failure_reason before returning.
    """
    raw: Optional[str] = None

    # Check all phases for a quota / limit message first — this is rare
    # but happens when the build starts and is reaped for over-quota.
    phases = build.get("phases") or []
    for phase in phases:
        contexts = phase.get("contexts") or []
        for ctx in contexts:
            msg = (ctx.get("message") or "").strip()
            msg_lc = msg.lower()
            if msg and any(
                phrase in msg_lc
                for phrase in _NON_RETRYABLE_AWS_ERROR_PHRASES
            ):
                return _sanitize_failure_reason(
                    f"[quota/limit] [{phase.get('phaseType', '?')}] {msg}"
                )

    for phase in reversed(phases):
        contexts = phase.get("contexts") or []
        for ctx in reversed(contexts):
            msg = ctx.get("message", "").strip()
            if msg:
                raw = f"[{phase.get('phaseType', '?')}] {msg}"
                break
        if raw:
            break

    if not raw:
        raw = f"Build ended with status: {build.get('buildStatus', 'UNKNOWN')}"

    return _sanitize_failure_reason(raw)


# ---------------------------------------------------------------------------
# AWS CodeBuild runner
# ---------------------------------------------------------------------------


class AWSSandboxRunner(SandboxRunner):
    """
    Sandbox adapter backed by AWS CodeBuild (general1.small EC2 tier).

    Configuration (from settings):
      - aws_codebuild_project_name: pre-provisioned CodeBuild project name
      - aws_region: AWS region (default us-east-1)

    IAM role attached to the CodeBuild project must have ONLY:
      codebuild:StartBuild, codebuild:BatchGetBuilds,
      logs:CreateLogGroup, logs:CreateLogStream, logs:PutLogEvents,
      ssm:GetParameters (scoped to /haunter/GITHUB_TOKEN only)
    Explicit deny on secretsmanager:GetSecretValue enforced in Terraform.
    """

    async def verify(self, inp: SandboxInput) -> SandboxResult:
        from app.config import settings

        project_name: Optional[str] = getattr(
            settings, "aws_codebuild_project_name", None
        )
        if not project_name:
            logger.error(
                "aws_runner: AWS_CODEBUILD_PROJECT_NAME not set for run=%s", inp.run_id
            )
            return make_result(
                passed=False,
                reason="AWS_CODEBUILD_PROJECT_NAME not configured.",
                duration_ms=0,
            )

        # Parse owner/repo from repo_ref (validated by SandboxInput)
        repo_ref_clean = inp.repo_ref.split("@")[0]  # strip @sha if present
        parts = repo_ref_clean.split("/")
        if len(parts) < 2:
            return make_result(
                passed=False,
                reason=f"repo_ref {inp.repo_ref!r} is not in owner/repo format.",
                duration_ms=0,
            )
        repo_owner, repo_name = parts[0], parts[1]

        # Encode patch as base64 — eliminates all shell-metacharacter risk.
        patch_b64 = base64.b64encode(inp.patch.encode("utf-8")).decode("ascii")

        # Validate env var values before submission (allowlist check)
        try:
            _validate_env_var_value(repo_owner, "REPO_OWNER")
            _validate_env_var_value(repo_name, "REPO_NAME")
            _validate_env_var_value(patch_b64, "PATCH_B64")
        except ValueError as exc:
            logger.error(
                "aws_runner: env var validation failed for run=%s: %s", inp.run_id, exc
            )
            return make_result(
                passed=False,
                reason=_sanitize_failure_reason(
                    f"Input validation error: {str(exc)[:300]}"
                ),
                duration_ms=0,
            )

        env_overrides = [
            {"name": "REPO_OWNER", "value": repo_owner, "type": "PLAINTEXT"},
            {"name": "REPO_NAME", "value": repo_name, "type": "PLAINTEXT"},
            {"name": "PATCH_B64", "value": patch_b64, "type": "PLAINTEXT"},
        ]

        try:
            result = await asyncio.wait_for(
                self._run_build(
                    project_name=project_name,
                    region=getattr(settings, "aws_region", "us-east-1"),
                    env_overrides=env_overrides,
                    run_id=inp.run_id,
                ),
                timeout=_OVERALL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.error(
                "aws_runner: overall timeout (%ss) exceeded for run=%s",
                _OVERALL_TIMEOUT_SECONDS,
                inp.run_id,
            )
            return make_result(
                passed=False,
                reason=f"Sandbox verification timed out after {int(_OVERALL_TIMEOUT_SECONDS)}s.",
                duration_ms=int(_OVERALL_TIMEOUT_SECONDS * 1000),
            )
        except Exception as exc:
            # Detect non-retryable AWS-side errors (quota, limit, auth)
            # BEFORE the generic handler swallows them. The orchestrator
            # already routes to the fallback comment on a non-retryable
            # sandbox result; we just need to mark it clearly so the
            # dashboard shows the real cause instead of 10 identical
            # "Sandbox fail" rows.
            exc_text = (str(exc) or "").lower()
            is_non_retryable = any(
                phrase in exc_text
                for phrase in _NON_RETRYABLE_AWS_ERROR_PHRASES
            )
            log_level = logger.error if is_non_retryable else logger.warning
            log_level(
                "aws_runner: CodeBuild API error for run=%s (non_retryable=%s): %s: %s",
                inp.run_id,
                is_non_retryable,
                type(exc).__name__,
                str(exc)[:300],
            )
            prefix = (
                "CodeBuild quota/limit exceeded (non-retryable; "
                "AWS Support case required to raise account quota): "
                if is_non_retryable
                else "CodeBuild API error: "
            )
            return make_result(
                passed=False,
                reason=_sanitize_failure_reason(
                    f"{prefix}{type(exc).__name__}: {str(exc)[:500]}"
                ),
                duration_ms=0,
            )

        return result

    async def _run_build(
        self,
        *,
        project_name: str,
        region: str,
        env_overrides: list[dict],
        run_id: object,
    ) -> SandboxResult:
        """Submit build + poll until terminal. Runs blocking boto3 in executor."""
        import boto3  # lazy — only imported when SANDBOX_PROVIDER=aws

        loop = asyncio.get_running_loop()

        client = await loop.run_in_executor(
            None,
            lambda: boto3.client("codebuild", region_name=region),
        )

        # ----------------------------------------------------------------
        # Submit build
        # ----------------------------------------------------------------
        t_submit = time.monotonic()

        def _start() -> str:
            # Per-build timeouts are NOT passed here — they are configured at
            # the CodeBuild project level in infra/aws/codebuild.tf
            # (build_timeout=10, queued_timeout=5). The StartBuild API only
            # accepts the *Override variants (timeoutInMinutesOverride,
            # queuedTimeoutInMinutesOverride); the bare names are project-level
            # only and were causing ParamValidationError on every build.
            resp = client.start_build(
                projectName=project_name,
                buildspecOverride=_BUILDSPEC,
                environmentVariablesOverride=env_overrides,
            )
            return resp["build"]["id"]

        build_id: str = await loop.run_in_executor(None, _start)

        logger.info(
            "aws_runner: run=%s submitted CodeBuild build_id=%s",
            run_id,
            build_id,
            # NEVER log patch content or GITHUB_TOKEN
        )

        # ----------------------------------------------------------------
        # Poll until terminal
        # ----------------------------------------------------------------
        def _get_build() -> dict:
            resp = client.batch_get_builds(ids=[build_id])
            builds = resp.get("builds") or []
            if not builds:
                return {"buildStatus": "UNKNOWN"}
            return builds[0]

        build: dict = {}
        while True:
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

            build = await loop.run_in_executor(None, _get_build)
            status: str = build.get("buildStatus", "IN_PROGRESS")

            logger.debug(
                "aws_runner: run=%s build_id=%s status=%s", run_id, build_id, status
            )

            if status in _CODEBUILD_TERMINAL:
                break

        # ----------------------------------------------------------------
        # Duration
        # ----------------------------------------------------------------
        t_end = time.monotonic()
        elapsed_ms = int((t_end - t_submit) * 1000)

        # Prefer CodeBuild's own start/end timestamps
        try:
            cb_start = build.get("startTime")
            cb_end = build.get("endTime")
            if cb_start and cb_end:
                import datetime

                # boto3 returns datetime objects for timestamps
                if hasattr(cb_start, "timestamp") and hasattr(cb_end, "timestamp"):
                    delta_ms = int((cb_end.timestamp() - cb_start.timestamp()) * 1000)
                    if delta_ms > 0:
                        elapsed_ms = delta_ms
        except Exception:
            pass  # fall back to wall-clock

        # ----------------------------------------------------------------
        # Map to SandboxResult
        # ----------------------------------------------------------------
        if status == "SUCCEEDED":
            logger.info(
                "aws_runner: run=%s build PASSED (build_id=%s, duration_ms=%d)",
                run_id,
                build_id,
                elapsed_ms,
            )
            return make_result(passed=True, reason=None, duration_ms=elapsed_ms)

        reason = _extract_failure_reason_aws(build)
        logger.info(
            "aws_runner: run=%s build FAILED status=%s (build_id=%s)",
            run_id,
            status,
            build_id,
        )
        return make_result(passed=False, reason=reason, duration_ms=elapsed_ms)
