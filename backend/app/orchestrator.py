"""
Pipeline orchestrator for autonomous CI failure diagnosis and fix.

Implements a forward-only state machine over RunStatus transitions. Each
transition is validated against _ALLOWED_TRANSITIONS — skipping states or
reversing direction raises InvalidTransitionError.

Compact run state dict held in memory: {run_id, repo_id, step, decisions,
confidence}. Raw logs, diffs, and LLM responses are NEVER held in orchestrator
memory — they pass through subagents only and are discarded after summarisation.

Entry point: handle_failed_run(run_id) is called via FastAPI BackgroundTasks
(no HTTP request context — opens its own AsyncSession from async_session_maker).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session_maker
from app.models import Repo, Run

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class RunStatus(str, Enum):
    pending = "pending"
    context_gathering = "context_gathering"
    fix_generation = "fix_generation"
    verification = "verification"
    pending_pr = "pending_pr"
    fallback = "fallback"
    completed = "completed"
    error = "error"


# Forward-only valid transitions. Any pair not listed here is rejected.
# error is reachable from every non-terminal state.
_ALLOWED_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.pending:           {RunStatus.context_gathering, RunStatus.error},
    RunStatus.context_gathering: {RunStatus.fix_generation, RunStatus.error},
    RunStatus.fix_generation:    {RunStatus.verification, RunStatus.error},
    RunStatus.verification:      {RunStatus.pending_pr, RunStatus.fallback, RunStatus.fix_generation, RunStatus.error},
    RunStatus.pending_pr:        {RunStatus.completed, RunStatus.error},
    RunStatus.fallback:          set(),
    RunStatus.completed:         set(),
    RunStatus.error:             set(),
}


class InvalidTransitionError(Exception):
    """Raised when a state transition is not in _ALLOWED_TRANSITIONS."""

    def __init__(self, from_status: RunStatus, to_status: RunStatus) -> None:
        super().__init__(
            f"Invalid transition: {from_status.value!r} → {to_status.value!r}"
        )
        self.from_status = from_status
        self.to_status = to_status


def _validate_transition(current: RunStatus, next_status: RunStatus) -> None:
    """Raise InvalidTransitionError if the transition is not allowed."""
    allowed = _ALLOWED_TRANSITIONS.get(current, set())
    if next_status not in allowed:
        raise InvalidTransitionError(current, next_status)


async def _transition(
    run: Run,
    new_status: RunStatus,
    db: AsyncSession,
) -> None:
    """
    Validate and persist a status transition on `run`.

    Updates runs.status and runs.updated_at atomically. Raises
    InvalidTransitionError before touching the DB if the transition is invalid.
    """
    current = RunStatus(run.status)
    _validate_transition(current, new_status)

    run.status = new_status.value
    run.updated_at = datetime.now(timezone.utc)
    db.add(run)
    await db.commit()

    logger.info(
        "orchestrator: run=%s %s → %s",
        run.id,
        current.value,
        new_status.value,
    )


# ---------------------------------------------------------------------------
# Entry point (BackgroundTasks target)
# ---------------------------------------------------------------------------


async def handle_failed_run(run_id: uuid.UUID) -> None:
    """
    Orchestrate the CI failure diagnosis pipeline for `run_id`.

    Called asynchronously by FastAPI BackgroundTasks — never blocks the HTTP
    handler. Opens its own database session (no request context available).

    Phase 5 scope: context_gathering only.
      pending → context_gathering → fix_generation (next phase picks up)

    On any unhandled error: transition to error, persist failure indicator.
    """
    # Late import to avoid circular import at module load time.
    from app.subagents.context_gatherer import gather_context

    async with async_session_maker() as db:
        # ----------------------------------------------------------------
        # Load Run + Repo
        # ----------------------------------------------------------------
        run_result = await db.execute(select(Run).where(Run.id == run_id))
        run = run_result.scalar_one_or_none()
        if run is None:
            logger.error("orchestrator: run %s not found — aborting", run_id)
            return

        from sqlalchemy.orm import selectinload
        repo_result = await db.execute(select(Repo).where(Repo.id == run.repo_id).options(selectinload(Repo.user)))
        repo = repo_result.scalar_one_or_none()
        if repo is None:
            logger.error("orchestrator: repo %s for run %s not found — aborting", run.repo_id, run_id)
            return

        # Compact in-memory state — NO raw logs or diffs here
        state: dict[str, Any] = {
            "run_id": str(run_id),
            "repo_id": str(run.repo_id),
            "step": RunStatus.pending.value,
            "decisions": [],
            "confidence": None,
        }

        try:
            # ----------------------------------------------------------------
            # pending → context_gathering
            # ----------------------------------------------------------------
            await _transition(run, RunStatus.context_gathering, db)
            state["step"] = RunStatus.context_gathering.value

            # ----------------------------------------------------------------
            # Invoke Context Gatherer
            # ----------------------------------------------------------------
            summary = await gather_context(run=run, repo=repo, db=db)

            # Persist distilled summary on runs — raw logs never touch this col
            run.diagnosis_summary = summary
            run.updated_at = datetime.now(timezone.utc)
            db.add(run)
            await db.commit()

            state["decisions"].append("context_gathered")
            logger.info(
                "orchestrator: run=%s diagnosis_summary length=%d",
                run_id,
                len(summary),
            )

            # ----------------------------------------------------------------
            # context_gathering → fix_generation
            # (Fix Generator is Phase 6 — transition status now so Phase 6
            #  can pick up runs WHERE status='fix_generation')
            # ----------------------------------------------------------------
            # context_gathering -> fix_generation
            # ----------------------------------------------------------------
            await _transition(run, RunStatus.fix_generation, db)
            state["step"] = RunStatus.fix_generation.value

            # ----------------------------------------------------------------
            # Verification retry loop (max MAX_ATTEMPTS = 3)
            # ----------------------------------------------------------------
            from app.subagents.fix_generator import generate_fix, AttemptCapExceeded, PatchRejected, FixGenerationError
            from app.sandbox.verifier import verify_patch
            from app.models import RunStep, Attempt
            
            prior_attempt: Attempt | None = None
            MAX_ATTEMPTS = 3

            for iteration in range(MAX_ATTEMPTS):
                # ---- Generate fix ----
                try:
                    attempt = await generate_fix(
                        run=run,
                        diagnosis_summary=run.diagnosis_summary or "",
                        prior_attempt=prior_attempt,
                        db=db,
                    )
                    state["decisions"].append(
                        f"fix_generated_attempt_{attempt.attempt_number}"
                    )
                    state["confidence"] = attempt.confidence_score
                    logger.info(
                        "orchestrator: run=%s fix generated attempt=%d confidence=%d",
                        run_id,
                        attempt.attempt_number,
                        attempt.confidence_score or 0,
                    )
                except (AttemptCapExceeded, PatchRejected, FixGenerationError) as fix_err:
                    error_step = RunStep(
                        run_id=run.id,
                        step_name="fix_generator_error",
                        input_tokens=0,
                        output_tokens=0,
                        latency_ms=0,
                        cost_estimate=0.0,
                    )
                    db.add(error_step)
                    await db.commit()

                    logger.error(
                        "orchestrator: run=%s fix_generator failed (%s: %s)",
                        run_id,
                        type(fix_err).__name__,
                        fix_err,
                    )
                    await _transition(run, RunStatus.error, db)
                    return

                # ---- fix_generation -> verification ----
                if RunStatus(run.status) == RunStatus.fix_generation:
                    await _transition(run, RunStatus.verification, db)
                    state["step"] = RunStatus.verification.value

                # ---- Verify in Cloud Build sandbox ----
                verify_result = await verify_patch(
                    attempt=attempt,
                    run=run,
                    repo=repo,
                )

                # Persist verification result
                v_status: str = verify_result["status"]        # "pass" | "fail"
                failure_reason: str | None = verify_result["failure_reason"]
                build_duration_ms: int = verify_result["build_duration_ms"]

                attempt.verification_status = v_status
                attempt.failure_reason = failure_reason
                attempt.build_duration_ms = build_duration_ms
                db.add(attempt)
                await db.commit()

                logger.info(
                    "orchestrator: run=%s attempt=%d verification=%s duration_ms=%d",
                    run_id,
                    attempt.attempt_number,
                    v_status,
                    build_duration_ms,
                )

                if v_status == "pass":
                    # ---- Patch verified -> pending_pr ----
                    await _transition(run, RunStatus.pending_pr, db)
                    state["step"] = RunStatus.pending_pr.value
                    state["decisions"].append("verification_passed")
                    logger.info(
                        "orchestrator: run=%s -> pending_pr (Phase 8 will open PR)",
                        run_id,
                    )
                    return

                # ---- Patch failed ----
                state["decisions"].append(
                    f"verification_failed_attempt_{attempt.attempt_number}"
                )

                if iteration + 1 >= MAX_ATTEMPTS:
                    # Exhausted all attempts -- fallback (Phase 8: post diagnosis comment)
                    await _transition(run, RunStatus.fallback, db)
                    state["step"] = RunStatus.fallback.value
                    
                    fallback_msg = (
                        "**Haunter AI Diagnosis:**\n\n"
                        f"{run.diagnosis_summary}\n\n"
                        "*Note: Automated fixes were attempted but none passed the CI sandbox. "
                        "Please review the diagnosis above to manually resolve the issue.*"
                    )
                    
                    try:
                        from app.github_client import post_commit_comment
                        github_token = repo.user.access_token if repo.user.access_token else None
                        await post_commit_comment(
                            owner=repo.owner,
                            repo=repo.name,
                            sha=run.head_sha,
                            body=fallback_msg,
                            token=github_token
                        )
                    except Exception as e:
                        logger.error("orchestrator: run=%s failed to post fallback comment: %s", run_id, e)

                    logger.info(
                        "orchestrator: run=%s all %d attempts exhausted -> fallback",
                        run_id,
                        MAX_ATTEMPTS,
                    )
                    return

                # ---- Loop: verification -> fix_generation for retry ----
                await _transition(run, RunStatus.fix_generation, db)
                state["step"] = RunStatus.fix_generation.value
                prior_attempt = attempt  # feed failure_reason into next generate_fix call

                logger.info(
                    "orchestrator: run=%s attempt=%d failed -- retrying fix_generation with failure context",
                    run_id,
                    attempt.attempt_number,
                )

        except InvalidTransitionError:
            # Already logged in _transition; do not re-transition to error
            # since the run may be in a terminal state already.
            logger.error(
                "orchestrator: invalid transition for run %s — check concurrent task delivery",
                run_id,
            )
            raise

        except Exception as exc:
            logger.error(
                "orchestrator: run=%s failed at step=%s (%s: %s)",
                run_id,
                state["step"],
                type(exc).__name__,
                exc,
                # Do not include exc_info=True — stack traces can contain secrets
                # from exception message chains (e.g. DB URLs in SQLAlchemy errors).
            )
            # Open a fresh session for the error transition — the current session
            # may be in a rolled-back state (e.g. if a DB connection was dropped
            # mid-operation), making it unusable for further writes.
            try:
                async with async_session_maker() as error_db:
                    fresh_run = await error_db.get(Run, run_id)
                    if fresh_run is not None:
                        await _transition(fresh_run, RunStatus.error, error_db)
            except InvalidTransitionError:
                # Already in a terminal state — nothing to do.
                pass
            except Exception as inner_exc:
                logger.error(
                    "orchestrator: failed to persist error state for run=%s (%s: %s)",
                    run_id,
                    type(inner_exc).__name__,
                    inner_exc,
                )

