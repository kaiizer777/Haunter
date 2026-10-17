"""Pipeline-outbox sweeper (#157).

Root cause it closes: the webhook commits the ``Run`` row as ``pending``
*before* the fire-and-forget async self-invoke (``AWSHostingAdapter`` →
``boto3.lambda.invoke(InvocationType='Event')``). A refused/crashed
self-invoke leaves the run accepted-but-never-executed: still ``pending``,
with no recovery path and nothing that ever re-scans for it.

This module is that re-scan. A periodic EventBridge trigger (see
``backend/pipeline_outbox_handler.py`` + ``infra/aws/lambda.tf``) calls
:func:`sweep_stale_pending_runs`, which:

* redrives ``pending`` runs older than ``stale_after_s`` via the hosting
  adapter (CAS-claimed first, so a concurrently starting child wins);
* marks ``pending`` runs older than ``exhaust_after_s`` as ``error`` with a
  ``failure_reason`` (exhausted — bounds redrives when the child keeps
  crashing post-accept);
* marks a run ``error`` when its redrive raises instead of leaving it
  ``pending``.

Guarantee: no run stays ``pending`` past ``exhaust_after_s`` without either
executing (child moves it past ``pending``) or being explicitly failed here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional
from uuid import UUID

logger = logging.getLogger(__name__)

#: Default: a pending run older than this never started and is redriven.
DEFAULT_STALE_AFTER_S: float = 300.0
#: Default: a pending run older than this is exhausted (failed, not redriven).
DEFAULT_EXHAUST_AFTER_S: float = 3600.0
#: Upper bound on rows claimed per sweep so one tick cannot stampede Lambda.
DEFAULT_SWEEP_LIMIT: int = 100

FAILURE_REASON_EXHAUSTED = "pipeline outbox exhausted: accepted but never executed"
FAILURE_REASON_REDRIVE_FAILED = "pipeline outbox redrive failed"


@dataclass
class PipelineOutboxSummary:
    """Outcome of one :func:`sweep_stale_pending_runs` tick."""

    scanned: int = 0
    redriven: list[str] = field(default_factory=list)
    exhausted: list[str] = field(default_factory=list)
    skipped: int = 0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _default_session_factory():
    from app.db import async_session_maker

    return async_session_maker()


async def _default_adapter_factory():
    from app.adapters.hosting import get_hosting_adapter

    return await get_hosting_adapter()


async def sweep_stale_pending_runs(
    session_factory: Optional[Callable[[], Any]] = None,
    adapter_factory: Optional[Callable[[], Awaitable[Any]]] = None,
    *,
    stale_after_s: Optional[float] = None,
    exhaust_after_s: Optional[float] = None,
    limit: int = DEFAULT_SWEEP_LIMIT,
    now: Optional[datetime] = None,
) -> PipelineOutboxSummary:
    """Scan for stale ``pending`` runs; redrive them or mark them exhausted.

    ``session_factory`` / ``adapter_factory`` are seams for hermetic tests;
    production passes nothing and the real DB session + hosting adapter are
    resolved lazily (mirrors ``app.services.audit_pipeline``). ``None``
    horizons resolve from ``app.config.settings`` so operators tune them via
    ``PIPELINE_PENDING_TIMEOUT_S`` / ``PIPELINE_PENDING_EXHAUST_AFTER_S``.
    """
    from app.models import Run
    from sqlalchemy import select

    if stale_after_s is None or exhaust_after_s is None:
        from app.config import settings

        if stale_after_s is None:
            stale_after_s = settings.pipeline_pending_timeout_s
        if exhaust_after_s is None:
            exhaust_after_s = settings.pipeline_pending_exhaust_after_s

    current = now or _utcnow()
    stale_cutoff = current - timedelta(seconds=stale_after_s)

    make_session = session_factory or _default_session_factory
    make_adapter = adapter_factory or _default_adapter_factory
    summary = PipelineOutboxSummary()
    async with make_session() as db:
        result = await db.execute(
            select(Run)
            .where(Run.status == "pending", Run.created_at <= stale_cutoff)
            .order_by(Run.created_at)
            .limit(limit)
        )
        stale_runs = list(result.scalars().all())
        summary.scanned = len(stale_runs)
        logger.info(
            "pipeline_outbox: sweep found %d stale pending runs", summary.scanned
        )
        for run in stale_runs:
            await _sweep_one(db, run, make_adapter, summary, current, exhaust_after_s)
            await db.commit()
    return summary


async def _sweep_one(db, run, make_adapter, summary, current, exhaust_after_s) -> None:
    """Redrive one stale run, or exhaust it when past the horizon."""
    from fastapi import BackgroundTasks

    from app.models import Run
    from sqlalchemy import update

    run_id = run.id
    created_at = run.created_at
    if created_at is not None and created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    exhaust_cutoff = current - timedelta(seconds=exhaust_after_s)

    if created_at is not None and created_at <= exhaust_cutoff:
        if await _fail_run(db, run_id, FAILURE_REASON_EXHAUSTED):
            summary.exhausted.append(str(run_id))
            logger.warning(
                "pipeline_outbox: run=%s exhausted (pending past horizon)", run_id
            )
        else:
            summary.skipped += 1
            logger.info(
                "pipeline_outbox: run=%s left pending at exhaust time, skipping",
                run_id,
            )
        return

    # CAS claim: only the sweeper (or nobody) owns this run. A rowcount of 0
    # means the child started concurrently — it wins, we skip quietly.
    # The updated_at bump gives the redriven child a full fresh window before
    # the next sweep can redrive it again (no hot-loop redrive every tick).
    claimed = await db.execute(
        update(Run)
        .where(Run.id == run_id, Run.status == "pending")
        .values(updated_at=current)
    )
    if claimed.rowcount == 0:
        summary.skipped += 1
        logger.info("pipeline_outbox: run=%s already started, skipping", run_id)
        return

    adapter = await make_adapter()
    try:
        await adapter.schedule_pipeline(run_id, BackgroundTasks())
    except Exception as exc:
        logger.warning(
            "pipeline_outbox: run=%s redrive failed (%s) — failing run",
            run_id,
            type(exc).__name__,
        )
        if await _fail_run(db, run_id, FAILURE_REASON_REDRIVE_FAILED):
            summary.exhausted.append(str(run_id))
        else:
            summary.skipped += 1
        return
    summary.redriven.append(str(run_id))
    logger.info("pipeline_outbox: run=%s redriven", run_id)


async def _fail_run(db, run_id: UUID, reason: str) -> bool:
    """CAS-transition a still-``pending`` run to ``error`` with a reason.

    Returns True when this call moved the run. Rowcount 0 means someone else
    moved it first (the child started, or another sweep exhausted it) — that
    outcome is already terminal-or-executing, which is what the sweeper wants,
    so the caller counts it as skipped, never as exhausted.
    """
    from app.models import Run
    from sqlalchemy import update

    result = await db.execute(
        update(Run)
        .where(Run.id == run_id, Run.status == "pending")
        .values(
            status="error",
            failure_reason=reason[:500],
            updated_at=_utcnow(),
        )
    )
    return result.rowcount > 0
