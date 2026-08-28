"""
Eval endpoints for Haunter Phase 10.

Exposes:
  GET  /eval-results              — List eval results (admin-only).
  GET  /eval-results/{eval_id}    — Get single eval result (admin-only).
  POST /eval/run                  — Trigger eval runner (admin-only, rate-limited).

Security invariants:
  - ALL endpoints require get_current_user (signed session cookie).
  - Admin check: settings.admin_user_id MUST match current_user.id — else 403.
    If admin_user_id is not configured, all authenticated requests are denied
    (fail-closed: no admin_user_id → no access to sensitive eval data).
  - POST /eval/run does NOT accept repo_id or any external repo reference.
    Fixtures are a server-side allowlist; the client may only specify IDs that
    exist in that allowlist.
  - Pydantic input validation with Literal-allowlisted fixture IDs (derived
    server-side at startup) — prevents free-text injection.
  - Rate-limited: 5 eval runs per minute per IP (runner is expensive).
  - No prompts, patch text, or API keys are returned to any client.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import settings
from app.db import get_db
from app.limiter import limiter
from app.models import EvalResult, ModelConfig, User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["eval"])


# ---------------------------------------------------------------------------
# Admin gate helper
# ---------------------------------------------------------------------------


def _require_admin(current_user: User) -> None:
    """
    Raise HTTP 403 if the current user is not the configured admin.

    Fail-closed: if ADMIN_USER_ID is not set, nobody gets access.
    This prevents accidental exposure of eval data in misconfigured envs.
    """
    if not settings.admin_user_id:
        logger.warning(
            "Eval endpoint accessed but ADMIN_USER_ID not configured — denying user=%s",
            current_user.id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    if str(current_user.id) != settings.admin_user_id:
        logger.warning(
            "Non-admin user %s attempted to access eval endpoint", current_user.id
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )


# ---------------------------------------------------------------------------
# Response schemas (no prompts, no patch text exposed)
# ---------------------------------------------------------------------------


class EvalResultOut(BaseModel):
    """Public-safe EvalResult representation — no prompts or raw LLM content."""

    id: uuid.UUID
    run_id: Optional[uuid.UUID]
    overall_accuracy: Optional[float]
    model_config_id: Optional[uuid.UUID]
    created_at: Any  # datetime — serialised as ISO string by FastAPI

    # Selective subagent score exposure: only aggregate scores, no raw fixture details.
    context_gatherer_avg: Optional[float] = None
    fix_generator_avg: Optional[float] = None
    overall_pass_rate: Optional[float] = None
    total_fixtures: Optional[int] = None
    passed_fixtures: Optional[int] = None
    failed_fixtures: Optional[int] = None
    mode: Optional[str] = None
    provider: Optional[str] = None
    model_name: Optional[str] = None
    fixture_scores: Optional[list[dict[str, Any]]] = None
    confidence_correlation: Optional[float] = None
    confidence_correlation_n: Optional[int] = None

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_safe(
        cls,
        er: EvalResult,
        model_config: Optional[ModelConfig] = None,
    ) -> "EvalResultOut":
        scores = er.per_subagent_scores or {}
        overall = scores.get("overall", {})
        cg = scores.get("context_gatherer", {})
        fg = scores.get("fix_generator", {})

        cg_scores = {
            item["fixture_id"]: item.get("score")
            for item in cg.get("scores_per_fixture", [])
            if isinstance(item, dict) and "fixture_id" in item
        }
        fg_scores = {
            item["fixture_id"]: item.get("score")
            for item in fg.get("scores_per_fixture", [])
            if isinstance(item, dict) and "fixture_id" in item
        }
        all_fids = sorted(list(set(list(cg_scores.keys()) + list(fg_scores.keys()))))
        fixture_scores = [
            {
                "fixture_id": fid,
                "context_score": cg_scores.get(fid, 0.0),
                "fix_score": fg_scores.get(fid, 0.0),
            }
            for fid in all_fids
        ] if all_fids else None

        return cls(
            id=er.id,
            run_id=er.run_id,
            overall_accuracy=er.overall_accuracy,
            model_config_id=er.model_config_id,
            created_at=er.created_at,
            context_gatherer_avg=cg.get("average_score"),
            fix_generator_avg=fg.get("average_score"),
            overall_pass_rate=overall.get("pass_rate"),
            total_fixtures=overall.get("total_fixtures"),
            passed_fixtures=overall.get("passed"),
            failed_fixtures=overall.get("failed"),
            mode=scores.get("mode"),
            provider=model_config.provider if model_config else None,
            model_name=model_config.model_name if model_config else None,
            fixture_scores=fixture_scores,
            confidence_correlation=scores.get("confidence_correlation"),
            confidence_correlation_n=scores.get("confidence_correlation_n"),
        )


# ---------------------------------------------------------------------------
# Request schema for POST /eval/run
# ---------------------------------------------------------------------------

# Load allowlist at module init — prevents late failures and makes the schema stable.
def _load_fixture_allowlist() -> list[str]:
    """Return all fixture IDs from the server-side allowlist file."""
    from pathlib import Path
    import json
    # Primary path (from routers/eval.py → backend/eval/fixtures)
    candidates = [
        Path(__file__).parent.parent.parent / "eval" / "fixtures" / "golden_cases.json",
        Path(__file__).parent.parent / "eval" / "fixtures" / "golden_cases.json",
        Path(__file__).parent / "fixtures" / "golden_cases.json",
    ]
    for fixtures_path in candidates:
        try:
            if fixtures_path.exists():
                with fixtures_path.open() as f:
                    data = json.load(f)
                return [item["id"] for item in data if isinstance(item, dict) and "id" in item]
        except Exception:  # noqa: BLE001
            logger.exception("Failed to load eval fixture allowlist from %s", fixtures_path)
            continue
    logger.error("No golden_cases.json found in any candidate path: %s", candidates)
    return []


_FIXTURE_ALLOWLIST: list[str] = _load_fixture_allowlist()


class EvalRunRequest(BaseModel):
    """
    Request body for POST /eval/run.

    fixture_ids: subset of server-side allowlist IDs to evaluate.
                 Empty list / None → evaluate all fixtures.
    model_config_id: optional FK to model_configs row for provenance.
    dry_run: if True, no LLM calls are made (stubs used). Default True.
    """

    fixture_ids: Optional[list[str]] = Field(
        default=None,
        description="Fixture IDs from server-side allowlist. None = all.",
        max_length=20,  # bounded list — max 20 items (one per fixture)
    )
    model_config_id: Optional[uuid.UUID] = Field(
        default=None,
        description="model_configs.id for provenance tracking",
    )
    dry_run: bool = Field(
        default=True,
        description="If true, use stubs (no LLM). If false, real LLM calls.",
    )

    def validate_fixture_ids(self) -> None:
        """Raise ValueError if any fixture_id is not in the server-side allowlist."""
        if not self.fixture_ids:
            return
        unknown = [fid for fid in self.fixture_ids if fid not in _FIXTURE_ALLOWLIST]
        if unknown:
            raise ValueError(f"Unknown fixture IDs (not in server allowlist): {unknown!r}")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/eval-results", response_model=list[EvalResultOut])
async def list_eval_results(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[EvalResultOut]:
    """
    List all eval results — admin-only.
    Returns aggregate scores only; no prompts or raw LLM content.
    """
    _require_admin(current_user)

    result = await db.execute(
        select(EvalResult).order_by(EvalResult.created_at.desc()).limit(100)
    )
    rows = result.scalars().all()

    # Preload ModelConfigs if any exist
    mc_ids = [r.model_config_id for r in rows if r.model_config_id is not None]
    mc_map: dict[uuid.UUID, ModelConfig] = {}
    if mc_ids:
        mc_result = await db.execute(
            select(ModelConfig).where(ModelConfig.id.in_(mc_ids))
        )
        for mc in mc_result.scalars().all():
            mc_map[mc.id] = mc

    return [
        EvalResultOut.from_orm_safe(row, mc_map.get(row.model_config_id) if row.model_config_id else None)
        for row in rows
    ]


@router.get("/eval-results/{eval_id}", response_model=EvalResultOut)
async def get_eval_result(
    eval_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> EvalResultOut:
    """
    Get a single eval result by ID — admin-only.
    Returns aggregate scores only; no prompts or raw LLM content.
    """
    _require_admin(current_user)

    result = await db.execute(select(EvalResult).where(EvalResult.id == eval_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Eval result not found")

    mc = None
    if row.model_config_id is not None:
        mc_res = await db.execute(
            select(ModelConfig).where(ModelConfig.id == row.model_config_id)
        )
        mc = mc_res.scalar_one_or_none()

    return EvalResultOut.from_orm_safe(row, mc)


@router.post("/eval/run", response_model=EvalResultOut, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
async def trigger_eval_run(
    request: Request,
    body: EvalRunRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> EvalResultOut:
    """
    Trigger an eval run — admin-only, rate-limited to 5/minute per IP.

    The runner uses a server-side fixture allowlist only — no external repo
    references are accepted from the client. fixture_ids must exist in the
    allowlist or the request is rejected with 422.

    Returns the persisted EvalResult row (aggregate scores only).
    """
    _require_admin(current_user)

    # Validate fixture IDs against server-side allowlist
    try:
        body.validate_fixture_ids()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    from eval.runner import run_eval  # local import — avoids heavy startup at module load

    try:
        eval_result = await run_eval(
            golden_ids=body.fixture_ids or None,
            model_config_id=body.model_config_id,
            dry_run=body.dry_run,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("Eval run failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Eval run failed — see server logs",
        ) from exc

    return EvalResultOut.from_orm_safe(eval_result)
