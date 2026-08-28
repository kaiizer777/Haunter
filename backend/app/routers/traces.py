"""
Observability endpoints — Phase 9.

Exposes:
  GET /runs/{run_id}/trace   — Full chronological timeline for a single run.
  GET /runs                  — Filtered, paginated run list (scoped to caller).
  GET /repos/{repo_id}/stats — Aggregate success/cost/latency stats for a repo.

Security invariants (match WORK.md Phase 9 spec):
  - Every endpoint requires get_current_user (signed session cookie).
  - Ownership is enforced at the SQL WHERE clause — never fetch-then-filter.
  - Non-owned / non-existent resources → 404, not 403 (no existence oracle).
  - All filter parameters are Pydantic-bounded:
      limit  ∈ [1, 100]
      status ∈ exact allowlist (Literal)
      from/to datetime range ≤ 90 days, to ≥ from
      repo_id must be UUID (auto-validated by FastAPI path param type)
  - run_steps rows contain only token counts / latency (Phase 5 stores no raw
    logs) — redaction is upstream; we assert this at test time.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_db
from app.models import Attempt, Repo, Run, RunStep, User
from app.schemas import RunOut
from app.traces.classify import classify_failure

logger = logging.getLogger(__name__)

router = APIRouter(tags=["traces"])

# ---------------------------------------------------------------------------
# Status allowlist (mirrors RunStatus enum values)
# ---------------------------------------------------------------------------

RunStatusLiteral = Literal[
    "pending",
    "context_gathering",
    "fix_generation",
    "verification",
    "pending_pr",
    "fallback",
    "completed",
    "error",
]

# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class RunStepOut(BaseModel):
    step_name: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost_estimate: float
    created_at: datetime

    model_config = {"from_attributes": True}


class AttemptOut(BaseModel):
    attempt_number: int
    confidence_score: Optional[int]
    verification_status: Optional[str]
    failure_reason: Optional[str]
    build_duration_ms: Optional[int]
    created_at: datetime

    model_config = {"from_attributes": True}


class RunSummaryOut(BaseModel):
    id: uuid.UUID
    repo_id: uuid.UUID
    status: str
    diagnosis_summary: Optional[str]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TraceOut(BaseModel):
    run: RunSummaryOut
    steps: list[RunStepOut]
    attempts: list[AttemptOut]
    total_cost: float
    total_latency_ms: int
    failure_classification: Optional[str]


class RunListOut(BaseModel):
    runs: list[RunOut]
    total: int


class RepoStatsOut(BaseModel):
    success_rate: float
    total_runs: int
    avg_attempts: float
    avg_cost: float
    avg_latency_ms: float


# ---------------------------------------------------------------------------
# Validated query parameter models
# ---------------------------------------------------------------------------


class RunListParams(BaseModel):
    """
    Validated query params for GET /runs.

    Bounds:
      limit  ∈ [1, 100]           — prevents unbounded SELECT DoS
      offset ≥ 0
      from/to datetime range ≤ 90d, and to ≥ from when both supplied
      status in Literal allowlist  — rejects free-text SQL injection vector
    """

    repo_id: Optional[uuid.UUID] = None
    status: Optional[RunStatusLiteral] = None
    from_: Optional[datetime] = Field(None, alias="from")
    to: Optional[datetime] = None
    limit: int = Field(20, ge=1, le=100)
    offset: int = Field(0, ge=0)

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _validate_date_range(self) -> "RunListParams":
        from_ = self.from_
        to = self.to
        if from_ is not None and to is not None:
            if to < from_:
                raise ValueError("'to' must be >= 'from'")
            if (to - from_) > timedelta(days=90):
                raise ValueError("date range must not exceed 90 days")
        return self


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/runs/{run_id}/trace", response_model=TraceOut)
async def get_run_trace(
    run_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TraceOut:
    """
    Full chronological trace for a single run.

    Ownership enforced at SQL level: JOIN repos WHERE repos.user_id = :uid.
    Returns 404 (not 403) on non-owned or non-existent run_id.
    """
    # Single query that fetches the run AND validates ownership in one shot.
    run_result = await db.execute(
        select(Run)
        .join(Repo, Run.repo_id == Repo.id)
        .where(Run.id == run_id, Repo.user_id == current_user.id)
    )
    run = run_result.scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    # Fetch steps ordered ASC by created_at (chronological pipeline timeline).
    steps_result = await db.execute(
        select(RunStep)
        .where(RunStep.run_id == run_id)
        .order_by(RunStep.created_at.asc())
    )
    steps: list[RunStep] = list(steps_result.scalars().all())

    # Fetch attempts ordered ASC by attempt_number.
    attempts_result = await db.execute(
        select(Attempt)
        .where(Attempt.run_id == run_id)
        .order_by(Attempt.attempt_number.asc())
    )
    attempts: list[Attempt] = list(attempts_result.scalars().all())

    total_cost: float = sum(s.cost_estimate or 0.0 for s in steps)
    total_latency_ms: int = sum(s.latency_ms or 0 for s in steps)
    failure_classification: str | None = classify_failure(run, steps, attempts)

    return TraceOut(
        run=RunSummaryOut.model_validate(run),
        steps=[RunStepOut.model_validate(s) for s in steps],
        attempts=[AttemptOut.model_validate(a) for a in attempts],
        total_cost=round(total_cost, 8),
        total_latency_ms=total_latency_ms,
        failure_classification=failure_classification,
    )


@router.get("/runs", response_model=RunListOut)
async def list_runs(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    # FastAPI cannot inject Pydantic models with aliases from Query params
    # directly, so we declare them individually and validate manually.
    repo_id: Annotated[Optional[uuid.UUID], Query()] = None,
    status_filter: Annotated[Optional[str], Query(alias="status")] = None,
    from_: Annotated[Optional[datetime], Query(alias="from")] = None,
    to: Annotated[Optional[datetime], Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RunListOut:
    """
    List runs owned by the current user, with optional filters.

    All filters are Pydantic/FastAPI-validated (see bounds above).
    SQL WHERE always includes repo.user_id = :uid — no cross-tenant leakage.
    """
    # Validate via the Pydantic model to catch cross-field constraints
    # (date range ≤ 90d, to ≥ from) and status allowlist.
    try:
        params = RunListParams(
            repo_id=repo_id,
            status=status_filter,  # type: ignore[arg-type]
            **{"from": from_},
            to=to,
            limit=limit,
            offset=offset,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    # Base query — always scope to current user's repos.
    base_stmt = (
        select(Run)
        .join(Repo, Run.repo_id == Repo.id)
        .where(Repo.user_id == current_user.id)
    )

    if params.repo_id is not None:
        base_stmt = base_stmt.where(Run.repo_id == params.repo_id)
    if params.status is not None:
        base_stmt = base_stmt.where(Run.status == params.status)
    if params.from_ is not None:
        from_utc = params.from_.replace(tzinfo=timezone.utc) if params.from_.tzinfo is None else params.from_
        base_stmt = base_stmt.where(Run.created_at >= from_utc)
    if params.to is not None:
        to_utc = params.to.replace(tzinfo=timezone.utc) if params.to.tzinfo is None else params.to
        base_stmt = base_stmt.where(Run.created_at <= to_utc)

    # Count total matching rows (same filters, no limit/offset).
    count_stmt = select(func.count()).select_from(base_stmt.subquery())
    total_result = await db.execute(count_stmt)
    total: int = total_result.scalar_one()

    # Fetch paginated results.
    paginated_stmt = (
        base_stmt
        .order_by(Run.created_at.desc())
        .limit(params.limit)
        .offset(params.offset)
    )
    runs_result = await db.execute(paginated_stmt)
    runs: list[Run] = list(runs_result.scalars().all())

    return RunListOut(
        runs=[RunOut.model_validate(r) for r in runs],
        total=total,
    )


@router.get("/repos/{repo_id}/stats", response_model=RepoStatsOut)
async def get_repo_stats(
    repo_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RepoStatsOut:
    """
    Aggregate success/cost/latency statistics for a single repo.

    Ownership enforced at SQL level: WHERE repo.id = :rid AND repo.user_id = :uid.
    Returns 404 on non-owned or non-existent repo.
    """
    # Validate ownership first.
    repo_result = await db.execute(
        select(Repo).where(Repo.id == repo_id, Repo.user_id == current_user.id)
    )
    repo = repo_result.scalar_one_or_none()
    if repo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repo not found")

    # Aggregate run-level stats.
    run_agg_result = await db.execute(
        select(
            func.count(Run.id).label("total_runs"),
            func.sum(
                func.cast(Run.status == "completed", type_=func.count(Run.id).type)
            ).label("completed_count"),
        ).where(Run.repo_id == repo_id)
    )
    run_row = run_agg_result.one()
    total_runs: int = run_row.total_runs or 0
    # COUNT(status == completed) via a FILTER aggregate.
    # Re-query more explicitly for cross-DB compatibility:
    completed_result = await db.execute(
        select(func.count(Run.id)).where(
            Run.repo_id == repo_id, Run.status == "completed"
        )
    )
    completed_count: int = completed_result.scalar_one() or 0

    success_rate: float = (completed_count / total_runs) if total_runs > 0 else 0.0

    # Average attempts per run.
    avg_attempts_result = await db.execute(
        select(func.avg(
            select(func.count(Attempt.id))
            .where(Attempt.run_id == Run.id)
            .correlate(Run)
            .scalar_subquery()
        )).where(Run.repo_id == repo_id)
    )
    avg_attempts: float = float(avg_attempts_result.scalar_one() or 0.0)

    # Average cost and latency per run (summed per run, then averaged).
    cost_lat_result = await db.execute(
        select(
            func.avg(
                select(func.coalesce(func.sum(RunStep.cost_estimate), 0))
                .where(RunStep.run_id == Run.id)
                .correlate(Run)
                .scalar_subquery()
            ),
            func.avg(
                select(func.coalesce(func.sum(RunStep.latency_ms), 0))
                .where(RunStep.run_id == Run.id)
                .correlate(Run)
                .scalar_subquery()
            ),
        ).where(Run.repo_id == repo_id)
    )
    cost_lat_row = cost_lat_result.one()
    avg_cost: float = float(cost_lat_row[0] or 0.0)
    avg_latency_ms: float = float(cost_lat_row[1] or 0.0)

    return RepoStatsOut(
        success_rate=round(success_rate, 4),
        total_runs=total_runs,
        avg_attempts=round(avg_attempts, 4),
        avg_cost=round(avg_cost, 8),
        avg_latency_ms=round(avg_latency_ms, 2),
    )
