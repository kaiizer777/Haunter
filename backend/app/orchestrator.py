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
    pr_or_fallback = "pr_or_fallback"
    completed = "completed"
    error = "error"


# Forward-only valid transitions. Any pair not listed here is rejected.
# error is reachable from every non-terminal state.
_ALLOWED_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.pending:           {RunStatus.context_gathering, RunStatus.error},
    RunStatus.context_gathering: {RunStatus.fix_generation, RunStatus.error},
    RunStatus.fix_generation:    {RunStatus.verification, RunStatus.error},
    RunStatus.verification:      {RunStatus.pr_or_fallback, RunStatus.error},
    RunStatus.pr_or_fallback:    {RunStatus.completed, RunStatus.error},
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

        repo_result = await db.execute(select(Repo).where(Repo.id == run.repo_id))
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
            await _transition(run, RunStatus.fix_generation, db)
            state["step"] = RunStatus.fix_generation.value

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
            try:
                await _transition(run, RunStatus.error, db)
            except InvalidTransitionError:
                # Already in a terminal state — nothing to do.
                pass
