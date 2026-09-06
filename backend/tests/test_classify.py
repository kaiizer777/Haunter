"""
Phase 6 Tests — Failure Trace Classifier (app/traces/classify.py).

Pins every classification branch of classify_failure():
1. Happy pass / non-terminal state (completed, pr_opened, pending, fix_generation) → None.
2. Orchestrator failure_reason set (truthy) → None (defers to orchestrator's specific stage reason).
3. Status fallback/error with empty failure_reason ("") or None → classified by rule priority.
4. sandbox_error branches:
   - attempt.failure_reason starts with "[SANDBOX]" prefix.
   - attempt.failure_reason first line contains TIMEOUT, CANCELLED, INTERNAL_ERROR, EXPIRED (case-insensitive).
   - attempt.verification_status is TIMEOUT (uppercase), expired (lowercase), CANCELLED, INTERNAL_ERROR.
5. wrong_diagnosis branches:
   - attempts list is empty.
   - context_gatherer_error step exists with no successful context_gatherer step.
   - context_gatherer_error with successful context_gatherer does NOT trigger wrong_diagnosis.
6. wrong_fix branches:
   - attempts exist but all have verification_status=None (rejected before reaching sandbox).
7. tests_still_failing branches:
   - all attempts that reached sandbox have verification_status="fail".
   - mix of verification_status="fail" and verification_status=None (one missing).
8. Ambiguous / Partial success fallback → None.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Optional

import pytest

from app.models import Attempt, Run, RunStep
from app.traces.classify import classify_failure


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(
    status: str = "fallback",
    failure_reason: Optional[str] = None,
) -> Run:
    run = Run(
        id=uuid.uuid4(),
        status=status,
    )
    run.failure_reason = failure_reason
    return run


def _step(name: str) -> RunStep:
    return RunStep(
        id=uuid.uuid4(),
        step_name=name,
        input_tokens=50,
        output_tokens=20,
    )


def _attempt(
    number: int = 1,
    verification_status: Optional[str] = None,
    failure_reason: Optional[str] = None,
) -> Attempt:
    return Attempt(
        id=uuid.uuid4(),
        attempt_number=number,
        patch_text="--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n",
        verification_status=verification_status,
        failure_reason=failure_reason,
    )


# ---------------------------------------------------------------------------
# 1. Non-terminal / Happy pass states → None
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status",
    [
        "completed",
        "pr_opened",
        "pending",
        "context_gathering",
        "fix_generation",
        "verification",
        "pending_pr",
    ],
)
def test_classify_non_terminal_statuses_return_none(status: str) -> None:
    """Non-terminal and successful statuses always return None."""
    run = _run(status=status)
    steps = [_step("context_gatherer")]
    attempts = [_attempt(number=1, verification_status="pass")]
    assert classify_failure(run, steps, attempts) is None


# ---------------------------------------------------------------------------
# 2. Orchestrator failure_reason deferral
# ---------------------------------------------------------------------------

def test_classify_defers_to_orchestrator_failure_reason() -> None:
    """When run.failure_reason is set (truthy), classify_failure returns None."""
    run = _run(status="error", failure_reason="context_gatherer: TimeoutError: request timed out")
    steps = [_step("context_gatherer_error")]
    attempts: list[Attempt] = []
    assert classify_failure(run, steps, attempts) is None


def test_classify_defers_to_auth_failure_reason() -> None:
    """Auth failures recorded in failure_reason return None."""
    run = _run(status="error", failure_reason="github: GitHubAuthError: Bad credentials (401)")
    assert classify_failure(run, [], []) is None


def test_classify_empty_string_failure_reason_is_falsy() -> None:
    """When failure_reason='' (empty string is falsy), classifier proceeds to rule priority."""
    run = _run(status="fallback", failure_reason="")
    # With no attempts, should classify as wrong_diagnosis
    assert classify_failure(run, [], []) == "wrong_diagnosis"


def test_classify_none_failure_reason_proceeds() -> None:
    """When failure_reason is None, classifier proceeds to rule priority."""
    run = _run(status="fallback", failure_reason=None)
    assert classify_failure(run, [], []) == "wrong_diagnosis"


def test_classify_missing_failure_reason_attribute() -> None:
    """When run object lacks failure_reason attribute entirely, classifier proceeds cleanly."""
    fake_run = SimpleNamespace(status="error")
    assert classify_failure(fake_run, [], []) == "wrong_diagnosis"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 3. sandbox_error branches
# ---------------------------------------------------------------------------

def test_classify_sandbox_error_prefix_in_failure_reason() -> None:
    """attempt.failure_reason starting with '[SANDBOX]' returns sandbox_error."""
    run = _run(status="fallback")
    attempts = [
        _attempt(
            number=1,
            verification_status="fail",
            failure_reason="[SANDBOX] AWS CodeBuild fleet failed to provision container",
        )
    ]
    assert classify_failure(run, [], attempts) == "sandbox_error"


@pytest.mark.parametrize(
    "first_line_content",
    [
        "BUILD_STATUS: TIMEOUT\nDetails: build exceeded 600 seconds",
        "BUILD_STATUS: CANCELLED\nBuild was stopped by admin",
        "INTERNAL_ERROR in test execution environment",
        "EXPIRED runner lease",
        # Lowercase variants to test case-insensitivity (.upper() in implementation)
        "build_status: timeout\nrunner timed out",
        "internal_error occurred during container setup",
        "build was cancelled by user",
        "lease expired during run",
    ],
)
def test_classify_sandbox_error_infra_status_in_failure_reason_first_line(
    first_line_content: str,
) -> None:
    """Cloud Build infra statuses in the first line of attempt.failure_reason classify as sandbox_error."""
    run = _run(status="fallback")
    attempts = [
        _attempt(
            number=1,
            verification_status="fail",
            failure_reason=first_line_content,
        )
    ]
    assert classify_failure(run, [], attempts) == "sandbox_error"


@pytest.mark.parametrize(
    "status_str",
    [
        "TIMEOUT",       # uppercase per test.md
        "expired",       # lowercase per test.md
        "CANCELLED",
        "INTERNAL_ERROR",
        "timeout",
        "EXPIRED",
        "cancelled",
        "internal_error",
    ],
)
def test_classify_sandbox_error_verification_status_infra_status(status_str: str) -> None:
    """attempt.verification_status carrying infra status (upper or lower case) returns sandbox_error."""
    run = _run(status="fallback")
    attempts = [_attempt(number=1, verification_status=status_str)]
    assert classify_failure(run, [], attempts) == "sandbox_error"


# ---------------------------------------------------------------------------
# 4. wrong_diagnosis branches
# ---------------------------------------------------------------------------

def test_classify_wrong_diagnosis_no_attempts() -> None:
    """Failed run with 0 attempts returns wrong_diagnosis."""
    run = _run(status="error")
    steps = [_step("context_gatherer")]
    attempts: list[Attempt] = []
    assert classify_failure(run, steps, attempts) == "wrong_diagnosis"


def test_classify_wrong_diagnosis_gather_error_without_success() -> None:
    """context_gatherer_error step present with no successful context_gatherer step returns wrong_diagnosis."""
    run = _run(status="error")
    steps = [_step("context_gatherer_error")]
    # Even if an attempt exists (e.g. malformed attempt from prior retry), gatherer errored
    attempts = [_attempt(number=1, verification_status=None)]
    assert classify_failure(run, steps, attempts) == "wrong_diagnosis"


def test_classify_gather_error_recovered_by_success_does_not_trigger_wrong_diagnosis() -> None:
    """context_gatherer_error step present BUT context_gatherer OK step also present does NOT trigger wrong_diagnosis."""
    run = _run(status="fallback")
    steps = [_step("context_gatherer_error"), _step("context_gatherer")]
    attempts = [_attempt(number=1, verification_status="fail")]
    # Recovered gatherer moves to tests_still_failing, not wrong_diagnosis
    assert classify_failure(run, steps, attempts) == "tests_still_failing"


# ---------------------------------------------------------------------------
# 5. wrong_fix branches
# ---------------------------------------------------------------------------

def test_classify_wrong_fix_attempts_with_verification_status_none() -> None:
    """Attempts exist but all have verification_status=None (rejected before sandbox) returns wrong_fix."""
    run = _run(status="fallback")
    steps = [_step("context_gatherer"), _step("fix_generator")]
    attempts = [
        _attempt(number=1, verification_status=None),
        _attempt(number=2, verification_status=None),
    ]
    assert classify_failure(run, steps, attempts) == "wrong_fix"


# ---------------------------------------------------------------------------
# 6. tests_still_failing branches
# ---------------------------------------------------------------------------

def test_classify_tests_still_failing_all_fail() -> None:
    """All sandbox-reached attempts have verification_status='fail' returns tests_still_failing."""
    run = _run(status="fallback")
    steps = [_step("context_gatherer"), _step("fix_generator")]
    attempts = [
        _attempt(number=1, verification_status="fail"),
        _attempt(number=2, verification_status="fail"),
        _attempt(number=3, verification_status="fail"),
    ]
    assert classify_failure(run, steps, attempts) == "tests_still_failing"


def test_classify_tests_still_failing_mix_of_fail_and_none() -> None:
    """Mix of verification_status='fail' and one missing (None) returns tests_still_failing."""
    run = _run(status="fallback")
    steps = [_step("context_gatherer"), _step("fix_generator")]
    # Attempt 1 reached sandbox and failed tests; Attempt 2 failed patch format/sanity before sandbox
    attempts = [
        _attempt(number=1, verification_status="fail"),
        _attempt(number=2, verification_status=None),
    ]
    assert classify_failure(run, steps, attempts) == "tests_still_failing"


# ---------------------------------------------------------------------------
# 7. Ambiguous / Partial / Unknown case
# ---------------------------------------------------------------------------

def test_classify_ambiguous_partial_pass_in_fallback_returns_none() -> None:
    """An unexpected mix (e.g. one passed attempt in a fallback run) guards with None."""
    run = _run(status="fallback")
    attempts = [
        _attempt(number=1, verification_status="pass"),
        _attempt(number=2, verification_status="unknown_status"),
    ]
    assert classify_failure(run, [], attempts) is None
