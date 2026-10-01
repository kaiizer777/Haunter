"""
Observability endpoints — Phase 9.

Exposes:
  GET /runs/{run_id}/trace   — Full chronological timeline for a single run.
  GET /runs                  — Filtered, paginated run list (scoped to caller).
  GET /repos/{repo_id}/stats — Aggregate success/cost/latency stats for a repo.
  DELETE /runs/{run_id}      — Delete a single run (scoped to caller).
  POST /runs/batch-delete    — Batch delete runs (scoped to caller).
  POST /runs/{run_id}/retry  — One-click retry: clone a settled run into a
                               fresh pending child (linked via parent_run_id +
                               is_retry_child) and re-dispatch the
                               orchestrator pipeline (Feature 1).

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

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Response,
    status,
)
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_db
from app.failure_signature import (
    MAX_SIGNATURE_INPUT_CHARS,
    normalize_failure_signature,
)
from app.models import Attempt, Repo, Run, RunStep, User
from app.schemas import BatchDeleteRunsRequest, BatchDeleteRunsResponse, RunOut
from app.traces.classify import classify_failure

logger = logging.getLogger(__name__)

router = APIRouter(tags=["traces"])

# Upper bound on the rows list_runs will scan to compute cross-page failure
# signature counts. See the clustering block in list_runs for why exceeding it
# disables the counts instead of truncating them.
SIGNATURE_CLUSTER_MAX_RUNS = 5000

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
    # Feature 1 — one-click retry lineage. parent_run_id is None for root runs
    # and set to the source run id for retry/refinement children.
    parent_run_id: Optional[uuid.UUID] = None
    # Phase 8 — PR Writer results (optional, populated after a PR is opened).
    # Optional here so older runs that pre-date Phase 8 still serialize cleanly.
    pr_url: Optional[str] = None
    pr_number: Optional[int] = None
    pr_branch: Optional[str] = None
    final_summary: Optional[str] = None
    # Phase 15 — short redacted reason a run ended in error/fallback.
    # None for runs that succeeded or are still in progress.
    failure_reason: Optional[str] = None
    # Feature 3 - fallback tracking issue filed on the exhaust path.
    # Both None until the issue is created; then the link to the issue.
    fallback_issue_url: Optional[str] = None
    fallback_issue_number: Optional[int] = None

    model_config = {"from_attributes": True}


class TraceOut(BaseModel):
    run: RunSummaryOut
    steps: list[RunStepOut]
    attempts: list[AttemptOut]
    total_cost: float
    total_latency_ms: int
    failure_classification: Optional[str]
    # Feature 1 — retry/refinement thread: the source run this run descends
    # from (None for root runs or when the parent is not visible to the
    # caller) plus every direct child, oldest first.
    parent: Optional[RunSummaryOut] = None
    children: list[RunSummaryOut] = Field(default_factory=list)


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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Run not found"
        )

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

    # Feature 1 — retry thread. Both lookups re-enforce ownership in the SQL
    # WHERE clause rather than filtering in Python, so a crafted or
    # cross-tenant parent_run_id can never leak another tenant's run summary.
    parent_out: RunSummaryOut | None = None
    if run.parent_run_id is not None:
        parent_result = await db.execute(
            select(Run)
            .join(Repo, Run.repo_id == Repo.id)
            .where(
                Run.id == run.parent_run_id,
                Repo.user_id == current_user.id,
            )
        )
        parent_run = parent_result.scalar_one_or_none()
        if parent_run is not None:
            parent_out = RunSummaryOut.model_validate(parent_run)

    children_result = await db.execute(
        select(Run)
        .join(Repo, Run.repo_id == Repo.id)
        .where(
            Run.parent_run_id == run_id,
            Repo.user_id == current_user.id,
        )
        .order_by(Run.created_at.asc())
    )
    children_out = [
        RunSummaryOut.model_validate(child)
        for child in children_result.scalars().all()
    ]

    return TraceOut(
        run=RunSummaryOut.model_validate(run),
        steps=[RunStepOut.model_validate(s) for s in steps],
        attempts=[AttemptOut.model_validate(a) for a in attempts],
        total_cost=round(total_cost, 8),
        total_latency_ms=total_latency_ms,
        failure_classification=failure_classification,
        parent=parent_out,
        children=children_out,
    )


# ---------------------------------------------------------------------------
# One-click retry (Feature 1)
# ---------------------------------------------------------------------------

# Only a settled (terminal) run may be cloned. Re-cloning a run that the
# orchestrator is still mutating would fork the pipeline and race the
# in-flight transitions on the source row.
_RETRYABLE_STATUSES: frozenset[str] = frozenset(
    {
        "pr_opened",
        "fallback_commented",
        "flaky_detected",
        "completed",
        "error",
    }
)

# A child in any of these statuses has settled, so a further retry of the same
# source run is permitted. Same membership as _RETRYABLE_STATUSES — kept as a
# named alias because the in-flight guard reads as the inverse condition.
_SETTLED_STATUSES: frozenset[str] = _RETRYABLE_STATUSES

# Upper bound on total children (retry + interactive PR refinement) per source
# run. Matches the interactive refinement cap in app/webhooks.py so a single
# run can never fan out into an unbounded pipeline storm.
_MAX_RUN_CHILDREN = 5


def build_retry_child(source: Run) -> Run:
    """
    Pure clone constructor for one-click retry. No I/O — hermetic and unit-testable.

    Copies the tenant scope (repo_id) and the failure coordinates
    (head_sha / head_branch / conclusion) from the source run, links lineage
    via ``parent_run_id`` + ``is_retry_child``, and resets every
    pipeline-owned field so the child starts clean in ``pending``.

    ``github_run_id`` and ``github_delivery_id`` are deliberately left NULL:
    the child corresponds to no new GitHub workflow run or webhook delivery.
    Fabricating either would squat on real GitHub's id space and cause a
    genuine delivery to be discarded as a duplicate by the UNIQUE index on
    ``runs.github_run_id``. The context gatherer resolves the effective
    workflow run id by walking ``parent_run_id`` to the root run.

    PR coordinates are cleared so the child opens a fresh PR through the
    normal PR-Writer path rather than committing onto the source run's branch.
    """
    return Run(
        repo_id=source.repo_id,
        parent_run_id=source.id,
        is_retry_child=True,
        github_run_id=None,
        github_delivery_id=None,
        head_sha=source.head_sha,
        head_branch=source.head_branch,
        status="pending",
        conclusion=source.conclusion,
        diagnosis_summary=None,
        pr_url=None,
        pr_number=None,
        pr_branch=None,
        final_summary=None,
        failure_reason=None,
    )


async def _mark_dispatch_failed(db: AsyncSession, child: Run) -> None:
    """
    Move an undispatchable retry child to the terminal `error` state.

    `_transition` is bypassed deliberately: the child never entered the
    orchestrator's state machine, so this is not a pipeline transition. Writing
    the terminal status directly keeps the run out of the in-flight set that
    would otherwise block every subsequent retry of the source run.
    """
    child.status = "error"
    child.failure_reason = "Retry dispatch failed: pipeline could not be scheduled"
    child.updated_at = datetime.now(timezone.utc)
    db.add(child)
    try:
        await db.commit()
    except Exception as exc:  # pragma: no cover - DB write already failed above
        await db.rollback()
        logger.error(
            "retry: failed to persist dispatch failure for run=%s (%s: %s)",
            child.id,
            type(exc).__name__,
            exc,
        )


@router.post(
    "/runs/{run_id}/retry",
    response_model=RunOut,
    status_code=status.HTTP_201_CREATED,
)
async def retry_run(
    run_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RunOut:
    """
    One-click retry: clone a settled run into a fresh ``pending`` child.

    The child copies repo_id/head_sha/head_branch/conclusion from the source,
    links lineage via ``parent_run_id`` + ``is_retry_child``, and is dispatched
    to the orchestrator through the hosting adapter so the pipeline runs
    asynchronously (on Lambda via async self-invoke; locally via
    BackgroundTasks) and the HTTP response returns immediately.

    Guards:
      - 404 on non-owned / non-existent run_id (no existence oracle).
      - 409 when the source run is still in flight (not settled).
      - 409 when a child of this run is still in flight.
      - 429 when the run already has 5 children.
    """
    # SELECT FOR UPDATE on the source row (runs only, not repos) serialises
    # concurrent retries of the same run. Without it two simultaneous requests
    # both read child_count below, both pass the cap, and both insert — the
    # check-then-act race the backend concurrency rules call out. The lock is
    # released at the commit below, well before the pipeline is dispatched.
    run_result = await db.execute(
        select(Run)
        .join(Repo, Run.repo_id == Repo.id)
        .where(Run.id == run_id, Repo.user_id == current_user.id)
        .with_for_update(of=Run)
    )
    source = run_result.scalar_one_or_none()
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Run not found"
        )

    if source.status not in _RETRYABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Run status {source.status!r} is not retryable — "
                "wait until the run settles before retrying."
            ),
        )

    child_count = await db.scalar(
        select(func.count(Run.id)).where(Run.parent_run_id == run_id)
    )
    if (child_count or 0) >= _MAX_RUN_CHILDREN:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Retry limit reached (max {_MAX_RUN_CHILDREN} per run).",
        )

    in_flight = await db.execute(
        select(Run.id)
        .where(
            Run.parent_run_id == run_id,
            Run.status.notin_(list(_SETTLED_STATUSES)),
        )
        .limit(1)
    )
    if in_flight.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A child run of this run is already in progress.",
        )

    child = build_retry_child(source)
    db.add(child)
    await db.commit()
    await db.refresh(child)

    # Re-dispatch through the hosting adapter so the pipeline runs out of band:
    # on Lambda via async self-invoke (BackgroundTasks never execute there),
    # locally via BackgroundTasks.
    from app.adapters.hosting import get_hosting_adapter

    try:
        adapter = await get_hosting_adapter()
        await adapter.schedule_pipeline(child.id, background_tasks)
    except Exception as exc:
        # The child is already committed but nothing will ever process it, so it
        # would sit in `pending` forever and count as a phantom in-flight child
        # that blocks every future retry of this run. Mark it terminal with a
        # reason rather than leaving a stuck row or pretending the retry worked.
        logger.error(
            "retry: dispatch failed for child_run=%s source_run=%s (%s: %s)",
            child.id,
            run_id,
            type(exc).__name__,
            exc,
        )
        await _mark_dispatch_failed(db, child)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Retry could not be dispatched. Please try again.",
        ) from exc

    logger.info(
        "retry: user=%s source_run=%s child_run=%s dispatched",
        current_user.id,
        run_id,
        child.id,
    )
    return RunOut.model_validate(child)


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

    user_repo_ids = select(Repo.id).where(Repo.user_id == current_user.id)

    filters = [Run.repo_id.in_(user_repo_ids)]
    if params.repo_id is not None:
        filters.append(Run.repo_id == params.repo_id)
    if params.status is not None:
        filters.append(Run.status == params.status)
    if params.from_ is not None:
        from_utc = (
            params.from_.replace(tzinfo=timezone.utc)
            if params.from_.tzinfo is None
            else params.from_
        )
        filters.append(Run.created_at >= from_utc)
    if params.to is not None:
        to_utc = (
            params.to.replace(tzinfo=timezone.utc)
            if params.to.tzinfo is None
            else params.to
        )
        filters.append(Run.created_at <= to_utc)

    # Count total matching rows (same filters, no limit/offset).
    count_stmt = select(func.count(Run.id)).where(*filters)
    total_result = await db.execute(count_stmt)
    total: int = total_result.scalar_one() or 0

    # Failure-signature clustering. Cross-page `signature_count` /
    # `sample_run_id` need the whole filtered set, and the grouping key is a
    # Python regex pipeline that SQL cannot express, so this costs one extra
    # bounded statement - not an N+1, and no per-row query. It runs only when
    # the matched set fits inside the cap: past the cap the counts would be
    # silently truncated, so we skip the scan entirely and leave
    # signature_count at 1, which tells the client to group the page it has.
    # `left()` caps the bytes each row puts on the wire - failure_reason is an
    # unbounded Text column holding sanitized CI output, and the normalizer only
    # ever reads the first MAX_SIGNATURE_INPUT_CHARS characters of it.
    # Ordered ASC so the earliest run wins sample_run_id.
    signature_counts: dict[str, int] = {}
    signature_samples: dict[str, uuid.UUID] = {}
    if total <= SIGNATURE_CLUSTER_MAX_RUNS:
        cluster_stmt = (
            select(
                Run.id,
                func.left(Run.failure_reason, MAX_SIGNATURE_INPUT_CHARS),
            )
            .where(*filters)
            .order_by(Run.created_at.asc())
            .limit(SIGNATURE_CLUSTER_MAX_RUNS)
        )
        cluster_rows = (await db.execute(cluster_stmt)).all()
        for row_id, row_reason in cluster_rows:
            sig = normalize_failure_signature(row_reason)
            signature_counts[sig] = signature_counts.get(sig, 0) + 1
            signature_samples.setdefault(sig, row_id)

    # Fetch paginated results with aggregated cost and tokens.
    cost_expr = func.coalesce(func.sum(RunStep.cost_estimate), 0.0).label("cost")
    tokens_expr = func.coalesce(
        func.sum(RunStep.input_tokens + RunStep.output_tokens), 0
    ).label("tokens")

    paginated_stmt = (
        select(Run, cost_expr, tokens_expr)
        .outerjoin(RunStep, RunStep.run_id == Run.id)
        .where(*filters)
        .group_by(Run.id)
        .order_by(Run.created_at.desc())
        .limit(params.limit)
        .offset(params.offset)
    )
    runs_result = await db.execute(paginated_stmt)
    rows = runs_result.all()

    runs: list[RunOut] = []
    for run, cost, tokens in rows:
        run_cost = float(cost or 0.0)
        run_tokens = int(tokens or 0)
        run.cost = run_cost
        run.tokens = run_tokens
        run_out = RunOut.model_validate(run)
        run_out.cost = run_cost
        run_out.tokens = run_tokens
        sig = normalize_failure_signature(run.failure_reason)
        run_out.signature = sig
        run_out.signature_count = signature_counts.get(sig, 1)
        run_out.sample_run_id = signature_samples.get(sig, run.id)
        runs.append(run_out)

    return RunListOut(
        runs=runs,
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Repo not found"
        )

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
        select(
            func.avg(
                select(func.count(Attempt.id))
                .where(Attempt.run_id == Run.id)
                .correlate(Run)
                .scalar_subquery()
            )
        ).where(Run.repo_id == repo_id)
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


@router.delete("/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_run(
    run_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """
    Delete a single run.

    Ownership enforced at SQL level: JOIN repos WHERE repos.user_id = :uid.
    Returns 404 (never 403) on non-owned or non-existent run_id to prevent
    existence oracle leakage to non-owners.
    """
    result = await db.execute(
        select(Run)
        .join(Repo, Run.repo_id == Repo.id)
        .where(Run.id == run_id, Repo.user_id == current_user.id)
    )
    run = result.scalar_one_or_none()
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Run not found",
        )

    await db.delete(run)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/runs/batch-delete",
    response_model=BatchDeleteRunsResponse,
    status_code=status.HTTP_200_OK,
)
async def batch_delete_runs(
    payload: BatchDeleteRunsRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BatchDeleteRunsResponse:
    """
    Batch delete runs owned by the current user.

    Only runs owned by current_user are deleted; non-owned or non-existent
    run IDs are ignored. Returns the count of deleted runs.
    """
    if not payload.run_ids:
        return BatchDeleteRunsResponse(deleted_count=0)

    user_repo_ids = select(Repo.id).where(Repo.user_id == current_user.id)
    result = await db.execute(
        select(Run).where(
            Run.id.in_(payload.run_ids),
            Run.repo_id.in_(user_repo_ids),
        )
    )
    matching_runs = list(result.scalars().all())

    if not matching_runs:
        return BatchDeleteRunsResponse(deleted_count=0)

    matching_run_ids = [r.id for r in matching_runs]

    # Explicit cascade cleanup for RunStep and Attempt (RunAttempt) records
    # in case DB-level foreign key cascade is not handled.
    await db.execute(delete(RunStep).where(RunStep.run_id.in_(matching_run_ids)))
    await db.execute(delete(Attempt).where(Attempt.run_id.in_(matching_run_ids)))

    for run in matching_runs:
        await db.delete(run)
    await db.commit()

    return BatchDeleteRunsResponse(deleted_count=len(matching_runs))
