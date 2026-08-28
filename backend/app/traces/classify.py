"""
Failure classifier for Phase 9 observability.

Rule-based, computed on the fly from existing DB fields — no migration or new
columns required.

Classification priority (first match wins):
  sandbox_error        — Cloud Build TIMEOUT/CANCELLED/INTERNAL_ERROR or a
                         verifier exception (failure_reason prefix "[SANDBOX]")
  wrong_diagnosis      — run ended in error/fallback with no attempts at all,
                         OR the context_gatherer_error step exists with no
                         successful context_gatherer step after it
  wrong_fix            — attempts exist but ALL were patch-sanity-rejected
                         (verification_status is None/"skipped"/unset, i.e.
                         never reached the sandbox), indicating fix_generator
                         produced invalid patches
  tests_still_failing  — all attempts reached the sandbox and all have
                         verification_status == "fail"

Returns None for runs that are not in a failed terminal state (completed /
pending_pr / still in progress).
"""

from __future__ import annotations

from app.models import Attempt, Run, RunStep

# Cloud Build terminal statuses that indicate infrastructure failure rather
# than a test failure caused by the generated patch.
_SANDBOX_INFRA_STATUSES: frozenset[str] = frozenset(
    {"TIMEOUT", "CANCELLED", "INTERNAL_ERROR", "EXPIRED"}
)

# Step names that signal an error in the context-gathering phase.
_GATHER_ERROR_STEP: str = "context_gatherer_error"
_GATHER_OK_STEP: str = "context_gatherer"

# Prefix injected by the verifier when an unexpected exception occurred
# (as opposed to a normal build failure).
_SANDBOX_EXCEPTION_PREFIX: str = "[SANDBOX]"


def classify_failure(
    run: Run,
    steps: list[RunStep],
    attempts: list[Attempt],
) -> str | None:
    """
    Return a failure classification tag or None if the run is not in a
    failed terminal state.

    Args:
        run:      Run ORM object (status must already be loaded).
        steps:    RunStep rows for this run, any order.
        attempts: Attempt rows for this run, any order.

    Returns:
        One of: "sandbox_error", "wrong_diagnosis", "wrong_fix",
                "tests_still_failing", or None.

    Notes:
        When the orchestrator has written `run.failure_reason` (Phase 15), it
        carries the specific stage that failed (e.g. "context_gatherer:
        TimeoutError: ..."). The coarse classifier labels would contradict or
        be redundant with that, so we return None and let the UI render
        failure_reason directly.
    """
    # Only classify terminal failure states.
    if run.status not in ("fallback", "error"):
        return None

    # Phase 15: defer to the orchestrator-written failure_reason when present.
    # The classifier's coarse label is less informative than a specific
    # "<stage>: <ExcType>: <message>" string and showing both is noisy.
    if getattr(run, "failure_reason", None):
        return None

    step_names: set[str] = {s.step_name for s in steps}

    # -------------------------------------------------------------------------
    # 1. sandbox_error — Cloud Build infra failure or verifier exception
    # -------------------------------------------------------------------------
    for attempt in attempts:
        # Verifier exception: failure_reason starts with the sentinel prefix.
        if attempt.failure_reason and attempt.failure_reason.startswith(
            _SANDBOX_EXCEPTION_PREFIX
        ):
            return "sandbox_error"

        # Cloud Build returned a non-test-failure terminal status embedded in
        # failure_reason as a first line like "BUILD_STATUS: TIMEOUT".
        if attempt.failure_reason:
            first_line = attempt.failure_reason.split("\n", 1)[0].upper()
            for infra_status in _SANDBOX_INFRA_STATUSES:
                if infra_status in first_line:
                    return "sandbox_error"

        # verification_status itself can carry the Cloud Build status string.
        if attempt.verification_status and attempt.verification_status.upper() in (
            _SANDBOX_INFRA_STATUSES
        ):
            return "sandbox_error"

    # -------------------------------------------------------------------------
    # 2. wrong_diagnosis — no attempts generated at all, OR gatherer errored
    # -------------------------------------------------------------------------
    if not attempts:
        return "wrong_diagnosis"

    # Gatherer error step present and no successful gather step recorded.
    if _GATHER_ERROR_STEP in step_names and _GATHER_OK_STEP not in step_names:
        return "wrong_diagnosis"

    # -------------------------------------------------------------------------
    # 3. wrong_fix — attempts exist but none ever reached sandbox verification
    #    (verification_status is None means patch was rejected before submission)
    # -------------------------------------------------------------------------
    sandbox_reached = [
        a for a in attempts if a.verification_status is not None
    ]
    if not sandbox_reached:
        return "wrong_fix"

    # -------------------------------------------------------------------------
    # 4. tests_still_failing — all sandbox-reaching attempts failed tests
    # -------------------------------------------------------------------------
    if all(a.verification_status == "fail" for a in sandbox_reached):
        return "tests_still_failing"

    # Partial success (some passed) shouldn't reach here from a failed run,
    # but guard with None rather than an incorrect label.
    return None
