"""
Code Review API endpoints — Feature 1.

Exposes:
  GET /repos/{repo_id}/reviews — Paginated list of code reviews for a specific repo.
  GET /reviews                 — Filterable, paginated list of code reviews across all user's repos.
  GET /reviews/{review_id}     — Detail view for a specific code review.

Security invariants:
- Requires authentication via get_current_user.
- Enforces multi-tenant isolation: every query is scoped to repos owned by current_user.id.
- Returns 404 for non-existent or unowned resources to prevent existence oracle leaks.
"""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Annotated, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import get_current_user
from app.db import get_db
from app.models import CodeReview, Repo, User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["reviews"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ReviewFindingOut(BaseModel):
    file_path: str
    line_start: int
    line_end: int
    category: str
    severity: str
    critique: str
    suggested_patch: Optional[str] = None


class CodeReviewOut(BaseModel):
    id: uuid.UUID
    repo_id: uuid.UUID
    repo_owner: Optional[str] = None
    repo_name: Optional[str] = None
    commit_sha: str
    pr_number: Optional[int] = None
    risk_score: int
    summary: str
    findings: list[ReviewFindingOut]
    status: str
    input_tokens: int
    output_tokens: int
    created_at: datetime

    model_config = {"from_attributes": True}


class CodeReviewListOut(BaseModel):
    reviews: list[CodeReviewOut]
    total: int


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _map_review_to_out(review: CodeReview, repo: Optional[Repo] = None) -> CodeReviewOut:
    owner = repo.owner if repo else (review.repo.owner if review.repo else None)
    name = repo.name if repo else (review.repo.name if review.repo else None)

    findings_list: list[ReviewFindingOut] = []
    if isinstance(review.findings, list):
        for f in review.findings:
            if isinstance(f, dict):
                findings_list.append(
                    ReviewFindingOut(
                        file_path=f.get("file_path", ""),
                        line_start=f.get("line_start", 1),
                        line_end=f.get("line_end", 1),
                        category=f.get("category", "logic"),
                        severity=f.get("severity", "low"),
                        critique=f.get("critique", ""),
                        suggested_patch=f.get("suggested_patch"),
                    )
                )

    return CodeReviewOut(
        id=review.id,
        repo_id=review.repo_id,
        repo_owner=owner,
        repo_name=name,
        commit_sha=review.commit_sha,
        pr_number=review.pr_number,
        risk_score=review.risk_score,
        summary=review.summary,
        findings=findings_list,
        status=review.status,
        input_tokens=review.input_tokens,
        output_tokens=review.output_tokens,
        created_at=review.created_at,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/repos/{repo_id}/reviews", response_model=CodeReviewListOut)
async def get_repo_reviews(
    repo_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    min_risk: Optional[int] = Query(default=None, ge=0, le=100),
    severity: Optional[str] = Query(default=None),
) -> CodeReviewListOut:
    """
    List reviews for a specific repository owned by the authenticated caller.
    """
    repo_stmt = select(Repo).where(Repo.id == repo_id, Repo.user_id == current_user.id)
    repo_res = await db.execute(repo_stmt)
    repo = repo_res.scalars().first()
    if not repo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repository not found")

    base_query = select(CodeReview).where(CodeReview.repo_id == repo_id)
    count_query = select(func.count()).select_from(CodeReview).where(CodeReview.repo_id == repo_id)

    if min_risk is not None:
        base_query = base_query.where(CodeReview.risk_score >= min_risk)
        count_query = count_query.where(CodeReview.risk_score >= min_risk)

    total = await db.scalar(count_query) or 0

    query = (
        base_query.options(selectinload(CodeReview.repo))
        .order_by(CodeReview.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    res = await db.execute(query)
    reviews = res.scalars().all()

    mapped_reviews = [_map_review_to_out(r, repo=repo) for r in reviews]

    if severity:
        sev_lower = severity.lower()
        mapped_reviews = [
            r for r in mapped_reviews if any(f.severity.lower() == sev_lower for f in r.findings)
        ]

    return CodeReviewListOut(reviews=mapped_reviews, total=total)


@router.get("/reviews", response_model=CodeReviewListOut)
async def list_all_reviews(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    repo_id: Optional[uuid.UUID] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    min_risk: Optional[int] = Query(default=None, ge=0, le=100),
    severity: Optional[str] = Query(default=None),
) -> CodeReviewListOut:
    """
    List code reviews across all repositories owned by the authenticated caller.
    Supports optional repo_id, min_risk, and severity filtering.
    """
    user_repos_stmt = select(Repo.id).where(Repo.user_id == current_user.id)
    user_repos_res = await db.execute(user_repos_stmt)
    user_repo_ids = user_repos_res.scalars().all()

    if not user_repo_ids:
        return CodeReviewListOut(reviews=[], total=0)

    base_query = select(CodeReview).where(CodeReview.repo_id.in_(user_repo_ids))
    count_query = select(func.count()).select_from(CodeReview).where(CodeReview.repo_id.in_(user_repo_ids))

    if repo_id is not None:
        if repo_id not in user_repo_ids:
            return CodeReviewListOut(reviews=[], total=0)
        base_query = base_query.where(CodeReview.repo_id == repo_id)
        count_query = count_query.where(CodeReview.repo_id == repo_id)

    if min_risk is not None:
        base_query = base_query.where(CodeReview.risk_score >= min_risk)
        count_query = count_query.where(CodeReview.risk_score >= min_risk)

    total = await db.scalar(count_query) or 0

    query = (
        base_query.options(selectinload(CodeReview.repo))
        .order_by(CodeReview.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    res = await db.execute(query)
    reviews = res.scalars().all()

    mapped_reviews = [_map_review_to_out(r) for r in reviews]

    if severity:
        sev_lower = severity.lower()
        mapped_reviews = [
            r for r in mapped_reviews if any(f.severity.lower() == sev_lower for f in r.findings)
        ]

    return CodeReviewListOut(reviews=mapped_reviews, total=total)


@router.get("/reviews/{review_id}", response_model=CodeReviewOut)
async def get_review_detail(
    review_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CodeReviewOut:
    """
    Fetch a single code review record. Scoped strictly to caller's repos.
    """
    stmt = (
        select(CodeReview)
        .join(Repo, CodeReview.repo_id == Repo.id)
        .options(selectinload(CodeReview.repo))
        .where(CodeReview.id == review_id, Repo.user_id == current_user.id)
    )
    res = await db.execute(stmt)
    review = res.scalars().first()

    if not review:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Code review not found")

    return _map_review_to_out(review)
