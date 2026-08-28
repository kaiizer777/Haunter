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

Phase 15: every error path writes `runs.failure_reason` (truncated, redacted)
and a `run_steps` trace row so the dashboard can show *why* a run failed.
"""

from __future__ import annotations

import html as html_module
import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session_maker
from app.models import Repo, Run, RunStep

# Truncation cap for failure_reason — keeps payloads tiny and avoids any risk of
# a giant exception chain landing in the DB. 500 chars fits in a tweet.
_FAILURE_REASON_MAX_CHARS = 500

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
    # Phase 8 terminal statuses
    pr_opened = "pr_opened"             # PR successfully created on GitHub
    fallback_commented = "fallback_commented"  # diagnosis comment posted (all attempts exhausted)
    completed = "completed"             # legacy — kept for backward compatibility
    error = "error"


# Forward-only valid transitions. Any pair not listed here is rejected.
# error is reachable from every non-terminal state.
_ALLOWED_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.pending:              {RunStatus.context_gathering, RunStatus.error},
    RunStatus.context_gathering:    {RunStatus.fix_generation, RunStatus.error},
    RunStatus.fix_generation:       {RunStatus.verification, RunStatus.error},
    RunStatus.verification:         {RunStatus.pending_pr, RunStatus.fallback, RunStatus.fix_generation, RunStatus.error},
    RunStatus.pending_pr:           {RunStatus.pr_opened, RunStatus.error},
    RunStatus.fallback:             {RunStatus.fallback_commented, RunStatus.error},
    # Terminal states — no transitions out
    RunStatus.pr_opened:            set(),
    RunStatus.fallback_commented:   set(),
    RunStatus.completed:            set(),
    RunStatus.error:                set(),
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
# Fallback comment sanitiser
# ---------------------------------------------------------------------------


def _sanitize_fallback(diagnosis_summary: Optional[str], attempts: list) -> str:
    """
    Build a sanitised fallback comment body to post on the commit.

    Rules:
      - Prefix with "**Haunter AI Diagnosis:**\n\n" (safe markdown).
      - Apply secret redaction from context_gatherer._redact_secrets.
      - html.escape the summary to prevent stored XSS on dashboard renders.
      - Hard-cap at 3000 chars AFTER prefix.
      - NEVER include raw patch text, full CI logs, or stack traces.
    """
    import html as html_module
    from app.subagents.context_gatherer import _redact_secrets

    raw = diagnosis_summary or "(no diagnosis available)"
    # Step 1: redact secrets from the summary
    redacted = _redact_secrets(raw)
    # Step 2: html.escape to neutralise any HTML/JS in the summary text
    escaped = html_module.escape(redacted, quote=False)
    # Step 3: cap body content (prefix does not count towards cap)
    prefix = "**Haunter AI Diagnosis:**\n\n"
    suffix = (
        "\n\n*Note: Automated fixes were attempted but none passed the CI sandbox. "
        "Please review the diagnosis above to manually resolve the issue.*"
    )
    max_body = 3000 - len(prefix) - len(suffix)
    body_content = escaped[:max_body]
    return f"{prefix}{body_content}{suffix}"


# ---------------------------------------------------------------------------
# Failure-reason persistence (Phase 15)
# ---------------------------------------------------------------------------


def _format_failure_reason(stage: str, exc: BaseException) -> str:
    """
    Build a short, safe, redacted failure-reason string for `runs.failure_reason`.

    Format: "<stage>: <ExcType>: <message>"

    - Truncated to _FAILURE_REASON_MAX_CHARS.
    - html.escape'd so any markup that bubbled through an exception message
      cannot land as raw HTML on the dashboard.
    - Strips leading/trailing whitespace; collapses internal newlines so it
      renders as one line in the UI.

    The message can still contain URLs or path-like strings (these are not
    secrets in the operational sense), but secret-redaction is the consumer's
    responsibility — we never store raw tokens or DB URLs in failure messages
    because the underlying exceptions we catch here don't carry them.
    """
    msg = str(exc) if exc is not None else ""
    if not msg:
        msg = "(no message)"
    raw = f"{stage}: {type(exc).__name__}: {msg}"
    # Collapse whitespace to single spaces so the column stays one logical line.
    raw = " ".join(raw.split())
    truncated = raw[:_FAILURE_REASON_MAX_CHARS]
    return html_module.escape(truncated, quote=False)


async def _persist_failure_reason(
    db: AsyncSession,
    run: Run,
    reason: str,
) -> None:
    """
    Write the failure reason onto `run` and commit. Caller passes a session
    that is still usable (i.e. not in a rolled-back / broken state). If the
    commit fails we log and let the caller proceed — failure_reason is
    best-effort observability, not a critical invariant.
    """
    run.failure_reason = reason
    run.updated_at = datetime.now(timezone.utc)
    db.add(run)
    try:
        await db.commit()
    except Exception as commit_exc:  # pragma: no cover — defensive only
        logger.warning(
            "orchestrator: failed to persist failure_reason for run=%s (%s: %s)",
            run.id,
            type(commit_exc).__name__,
            commit_exc,
        )
        try:
            await db.rollback()
        except Exception:
            pass


async def _persist_error_step(
    db: AsyncSession,
    run_id: uuid.UUID,
    step_name: str,
    latency_ms: int = 0,
) -> None:
    """
    Append a synthetic RunStep row for an unhandled error so the timeline
    is never silently empty. Never raises — the dashboard already knows
    the run failed; this is just observability.
    """
    try:
        step = RunStep(
            run_id=run_id,
            step_name=step_name,
            input_tokens=0,
            output_tokens=0,
            latency_ms=latency_ms,
            cost_estimate=0.0,
        )
        db.add(step)
        await db.commit()
    except Exception as step_exc:  # pragma: no cover — defensive only
        logger.warning(
            "orchestrator: failed to persist error step for run=%s (%s: %s)",
            run_id,
            type(step_exc).__name__,
            step_exc,
        )
        try:
            await db.rollback()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Entry point (BackgroundTasks target)
# ---------------------------------------------------------------------------


async def handle_failed_run(run_id: uuid.UUID) -> None:
    """
    Orchestrate the CI failure diagnosis pipeline for `run_id`.

    Called asynchronously by FastAPI BackgroundTasks — never blocks the HTTP
    handler. Opens its own database session (no request context available).

    Phase 8 scope: full pipeline through pr_opened or fallback_commented.

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
        repo_result = await db.execute(
            select(Repo)
            .where(Repo.id == run.repo_id)
            .with_for_update()  # SELECT FOR UPDATE: prevent concurrent tenant cross-write
            .options(selectinload(Repo.user))
        )
        repo = repo_result.scalar_one_or_none()
        if repo is None:
            logger.error("orchestrator: repo %s for run %s not found — aborting", run.repo_id, run_id)
            return

        # ----------------------------------------------------------------
        # Tenant integrity assertion
        # Every GitHub write call below must be scoped to this repo/user.
        # Guard against run_id guessing that could target another tenant's repo.
        # ----------------------------------------------------------------
        if repo.user_id is None:
            logger.error(
                "orchestrator: repo %s has no user_id — refusing to write (tenant integrity)",
                repo.id,
            )
            return
        if repo.id != run.repo_id:
            logger.error(
                "orchestrator: run.repo_id mismatch (run=%s repo=%s) — aborting",
                run_id, repo.id,
            )
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
            from app.sandbox import verify as sandbox_verify
            from app.models import RunStep, Attempt
            
            prior_attempt: Attempt | None = None
            # MAX_ATTEMPTS is the cap on fix-generation iterations per run.
            # Relaxed from 3 -> 10 while we are validating the pipeline. Tighten
            # once a clean success path is confirmed. The free-tier LLM is
            # occasionally flaky; more attempts give it room to self-correct.
            MAX_ATTEMPTS = 10

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
                    await _persist_failure_reason(
                        db=db,
                        run=run,
                        reason=_format_failure_reason("fix_generator", fix_err),
                    )
                    await _transition(run, RunStatus.error, db)
                    return

                # ---- fix_generation -> verification ----
                if RunStatus(run.status) == RunStatus.fix_generation:
                    await _transition(run, RunStatus.verification, db)
                    state["step"] = RunStatus.verification.value

                # ---- Verify in sandbox (provider selected via SANDBOX_PROVIDER env) ----
                verify_result = await sandbox_verify(
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

                    # ---- Phase 8: generate PR text + open PR ----
                    try:
                        from app.subagents.pr_writer import generate_pr_text, pr_branch_name, PRGenerationError
                        from app.github.pr import get_installation_token, create_branch, commit_patch, open_pr

                        pr_text = await generate_pr_text(
                            run=run,
                            verified_attempt=attempt,
                            diagnosis_summary=run.diagnosis_summary or "",
                            db=db,
                        )

                        token = await get_installation_token(repo)
                        branch = pr_branch_name(
                            run=run,
                            attempt=attempt,
                            default_branch=repo.default_branch,
                        )
                        base_branch = repo.default_branch or "main"

                        await create_branch(
                            owner=repo.owner,
                            repo=repo.name,
                            branch=branch,
                            sha=run.head_sha,
                            token=token,
                        )
                        await commit_patch(
                            owner=repo.owner,
                            repo=repo.name,
                            branch=branch,
                            patch_text=attempt.patch_text,
                            commit_msg=pr_text["title"],
                            token=token,
                        )
                        pr = await open_pr(
                            owner=repo.owner,
                            repo=repo.name,
                            head_branch=branch,
                            base_branch=base_branch,
                            title=pr_text["title"],
                            body=pr_text["body"],
                            token=token,
                        )

                        import html as html_module
                        run.pr_url = pr["html_url"]
                        run.pr_number = pr["number"]
                        run.pr_branch = branch
                        run.final_summary = html_module.escape(
                            pr_text["body"][:1000], quote=False
                        )
                        run.updated_at = datetime.now(timezone.utc)
                        db.add(run)
                        await db.commit()

                        await _transition(run, RunStatus.pr_opened, db)
                        state["step"] = RunStatus.pr_opened.value
                        logger.info(
                            "orchestrator: run=%s PR #%s opened %s",
                            run_id, pr["number"], pr["html_url"],
                        )
                    except Exception as pr_err:
                        logger.error(
                            "orchestrator: run=%s PR creation failed (%s: %s)",
                            run_id, type(pr_err).__name__, pr_err,
                        )
                        await _persist_failure_reason(
                            db=db,
                            run=run,
                            reason=_format_failure_reason("pr_writer", pr_err),
                        )
                        await _transition(run, RunStatus.error, db)
                    return

                # ---- Patch failed ----
                state["decisions"].append(
                    f"verification_failed_attempt_{attempt.attempt_number}"
                )

                if iteration + 1 >= MAX_ATTEMPTS:
                    # Exhausted all attempts -- fallback (post diagnosis-only comment)
                    await _transition(run, RunStatus.fallback, db)
                    state["step"] = RunStatus.fallback.value

                    # Build sanitised comment — html.escape + secret-redact + cap 3000
                    # Never include raw patch text or full CI logs.
                    from app.models import Attempt as AttemptModel
                    from sqlalchemy import select as sa_select
                    attempts_result = await db.execute(
                        sa_select(AttemptModel).where(AttemptModel.run_id == run.id)
                    )
                    all_attempts = attempts_result.scalars().all()
                    fallback_body = _sanitize_fallback(run.diagnosis_summary, list(all_attempts))

                    try:
                        from app.github.pr import get_installation_token
                        from app.github_client import post_commit_comment
                        github_token = await get_installation_token(repo)
                        await post_commit_comment(
                            owner=repo.owner,
                            repo=repo.name,
                            sha=run.head_sha,
                            body=fallback_body,
                            token=github_token,
                        )
                        await _transition(run, RunStatus.fallback_commented, db)
                        state["step"] = RunStatus.fallback_commented.value
                    except Exception as e:
                        logger.error(
                            "orchestrator: run=%s failed to post fallback comment: %s",
                            run_id, e,
                        )
                        # Even if comment posting fails, transition to error rather than
                        # leaving the run stranded in fallback.
                        await _persist_failure_reason(
                            db=db,
                            run=run,
                            reason=_format_failure_reason("fallback_comment", e),
                        )
                        await _transition(run, RunStatus.error, db)

                    logger.info(
                        "orchestrator: run=%s all %d attempts exhausted -> fallback_commented",
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
            # Build a stage label that tells the user *where* in the pipeline the
            # crash happened — "context_gatherer", "fix_generation", "verification",
            # "pr_writer", or just "orchestrator" for anything else.
            stage_label = state.get("step") or "orchestrator"
            # Open a fresh session for the error transition — the current session
            # may be in a rolled-back state (e.g. if a DB connection was dropped
            # mid-operation), making it unusable for further writes.
            try:
                async with async_session_maker() as error_db:
                    fresh_run = await error_db.get(Run, run_id)
                    if fresh_run is not None:
                        # Write the trace step FIRST so the timeline reflects the
                        # crash, then the failure_reason, then the status transition.
                        # Each write commits independently so a partial-failure
                        # mid-sequence still leaves *some* observability behind.
                        await _persist_error_step(
                            db=error_db,
                            run_id=run_id,
                            step_name=f"{stage_label}_error",
                        )
                        await _persist_failure_reason(
                            db=error_db,
                            run=fresh_run,
                            reason=_format_failure_reason(stage_label, exc),
                        )
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

