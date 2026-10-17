"""
Code Review API endpoints — Feature 1.

Exposes:
  GET /repos/{repo_id}/reviews — Paginated list of code reviews for a specific repo.
  GET /reviews                 — Filterable, paginated list of code reviews across all user's repos.
  GET /reviews/{review_id}     — Detail view for a specific code review.
  GET /reviews/{review_id}/associated-pr — On-demand associated-PR lookup for a review.

Security invariants:
- Requires authentication via get_current_user.
- Enforces multi-tenant isolation: every query is scoped to repos owned by current_user.id.
- Returns 404 for non-existent or unowned resources to prevent existence oracle leaks.
"""

from __future__ import annotations

from datetime import datetime
import asyncio
import logging
from typing import Annotated, Optional
from urllib.parse import quote
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, and_, case, cast, column, func, literal, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import get_current_user
from app.config import settings
from app.db import get_db
from app.github.pr import get_installation_token
from app.github_client import GITHUB_API_BASE, _bounded_get, _build_headers
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
    failure_reason: Optional[str] = None
    input_tokens: int
    output_tokens: int
    created_at: datetime

    model_config = {"from_attributes": True}


class CodeReviewListOut(BaseModel):
    """
    A page of reviews plus the size of the whole matching result set.

    ``total`` counts every review matching the request's filters across all
    pages, never the length of ``reviews``: the dashboard paginates on it
    (``frontend/src/app/reviews/page.tsx`` — ``totalCount`` at :432, the
    pagination block rendered only while ``totalCount > PAGE_SIZE`` at :661,
    ``Next`` disabled at ``(page + 1) * PAGE_SIZE >= totalCount`` at :679).
    """

    reviews: list[CodeReviewOut]
    total: int


class AssociatedPROut(BaseModel):
    number: int
    url: str
    title: Optional[str] = None


class AssociatedPRResponse(BaseModel):
    associated_pr: Optional[AssociatedPROut] = None


class AssociatedPRBatchRequest(BaseModel):
    """One dashboard page worth of associated-PR lookups (issue #116)."""

    review_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)


class AssociatedPRBatchResponse(BaseModel):
    """Map of requested review id to its PR (null when none can be established)."""

    associated_prs: dict[uuid.UUID, Optional[AssociatedPROut]]


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _severity_predicate(severity: str) -> ColumnElement[bool]:
    """
    SQL predicate for "this review has at least one finding of ``severity``".

    ``severity`` sits one key deep inside the ``findings`` JSONB array
    (app/models.py:438-440), so it is not a column comparison. Postgres can still
    evaluate it *before* LIMIT/OFFSET — the only place a count of matching
    reviews can be produced — by expanding the array and testing each element,
    so this needs no new column, no migration and no backfill.

    The normalisation mirrors the Python post-filter this replaced
    (``any(f.severity.lower() == severity.lower())`` over findings built by
    :func:`_map_review_to_out`, where a missing key becomes ``"low"``):

    * ``severity.lower()`` — the caller's value, case-insensitive;
    * ``lower(... ->> 'severity')`` — the stored value, case-insensitive;
    * ``coalesce(..., 'low')`` — a finding with no severity counts as low;
    * ``jsonb_typeof(finding) = 'object'`` — non-dict elements are skipped
      rather than matched, mirroring ``isinstance(f, dict)``;
    * ``jsonb_typeof(findings) = 'array'`` — a non-array value is skipped, which
      is what the Python filter did with a non-list ``findings``: no findings,
      no match.

    The array expansion substitutes ``'[]'`` for a non-array value rather than
    relying on the guard above to be evaluated first: Postgres does not promise
    an evaluation order for ``AND`` operands, and ``jsonb_array_elements`` on a
    jsonb scalar is an error, not an empty set.
    """
    finding = func.jsonb_array_elements(
        case(
            (func.jsonb_typeof(CodeReview.findings) == "array", CodeReview.findings),
            else_=cast(literal("[]"), JSONB),
        )
    ).table_valued(column("finding", JSONB), name="sev_finding")
    element = finding.c["finding"]
    return and_(
        func.jsonb_typeof(CodeReview.findings) == "array",
        select(literal(1))
        .select_from(finding)
        .where(
            func.jsonb_typeof(element) == "object",
            func.lower(func.coalesce(element["severity"].astext, "low"))
            == severity.lower(),
        )
        .exists(),
    )


def _map_review_to_out(
    review: CodeReview, repo: Optional[Repo] = None
) -> CodeReviewOut:
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
        failure_reason=getattr(review, "failure_reason", None),
        input_tokens=review.input_tokens,
        output_tokens=review.output_tokens,
        created_at=review.created_at,
    )


# ---------------------------------------------------------------------------
# Associated-PR lookup
# ---------------------------------------------------------------------------


#: Timeout for the on-demand associated-PR GitHub call (seconds). Deliberately
#: short: this endpoint backs a per-row dashboard call, so a slow GitHub
#: answers ``null`` instead of stalling the page.
_ASSOCIATED_PR_TIMEOUT_SECONDS = 5.0

#: Timeout for minting the repo-scoped installation token before the lookup
#: (seconds). The mint path (``get_installation_token``) uses the 30s
#: ``DEFAULT_TIMEOUT_SECONDS`` transport, which would blow the 5s lookup
#: budget above on a cold cache + slow GitHub/SSM; a slow mint answers
#: ``None`` so the caller falls through to the server PAT / null path.
_MINT_TIMEOUT_SECONDS = 3.0


async def _lookup_commit_pr(
    owner: str, repo: str, sha: str, token: Optional[str] = None
) -> Optional[AssociatedPROut]:
    """
    Return the PR associated with ``sha`` via
    ``GET /repos/{owner}/{repo}/commits/{sha}/pulls``.

    Auth prefers the caller's ``token`` — a repo-scoped installation token
    minted by :func:`app.github.pr.get_installation_token` — and falls back to
    the server ``settings.github_token`` PAT for local dev. This mirrors the
    try-installation-then-PAT pattern ``app/routers/sessions.py`` uses
    (:200-202, :459-461, :714-716, :830-832, :983-985); :func:`_build_headers`
    applies the same fallback internally, so there is one HTTP convention.

    Never raises: a missing token, any non-200 status, a network/timeout
    failure, an over-large body, or an unparseable body all answer ``None``
    so the caller renders the row as a plain push. No DB writes. The 5s
    timeout keeps a slow GitHub from stalling the page.

    Log levels: auth/rate-limit/server failures (401/403/429/5xx) log at
    warning — those signal a fixable credential or quota problem; expected
    misses (404/422), a missing token, timeouts, and unparseable bodies log
    at debug — this endpoint backs a per-row dashboard call, so a warning
    per row would spam the logs.
    """
    effective_token = token or settings.github_token
    if not effective_token or not effective_token.strip():
        logger.debug(
            "associated-pr lookup skipped (no token) for %s/%s @ %s",
            owner,
            repo,
            sha,
        )
        return None
    url = (
        f"{GITHUB_API_BASE}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/commits/{quote(sha, safe='')}/pulls"
    )
    headers = _build_headers(token=effective_token)
    try:
        async with httpx.AsyncClient(timeout=_ASSOCIATED_PR_TIMEOUT_SECONDS) as client:
            response = await _bounded_get(client, url, headers=headers)
    except httpx.RequestError as exc:
        logger.debug(
            "associated-pr lookup network error for %s/%s @ %s: %s",
            owner,
            repo,
            sha,
            type(exc).__name__,
        )
        return None
    except Exception as exc:
        logger.debug(
            "associated-pr lookup degraded for %s/%s @ %s: %s",
            owner,
            repo,
            sha,
            type(exc).__name__,
        )
        return None
    if response.status_code != 200:
        if response.status_code in (401, 403, 429) or response.status_code >= 500:
            logger.warning(
                "associated-pr lookup GitHub status %s for %s/%s @ %s",
                response.status_code,
                owner,
                repo,
                sha,
            )
        else:
            logger.debug(
                "associated-pr lookup GitHub status %s for %s/%s @ %s",
                response.status_code,
                owner,
                repo,
                sha,
            )
        return None
    try:
        items = response.json()
    except ValueError:
        logger.debug(
            "associated-pr lookup unparseable body for %s/%s @ %s",
            owner,
            repo,
            sha,
        )
        return None
    if not isinstance(items, list) or not items:
        return None
    candidates = [
        p for p in items if isinstance(p, dict) and isinstance(p.get("number"), int)
    ]
    if not candidates:
        return None
    open_prs = [p for p in candidates if p.get("state") == "open"]
    pick = max(open_prs or candidates, key=lambda p: p["number"])
    number = pick["number"]
    html_url = pick.get("html_url")
    pr_url = (
        html_url
        if isinstance(html_url, str) and html_url
        else f"https://github.com/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/pull/{number}"
    )
    title = pick.get("title")
    return AssociatedPROut(
        number=number,
        url=pr_url,
        title=title if isinstance(title, str) else None,
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Repository not found"
        )

    base_query = select(CodeReview).where(CodeReview.repo_id == repo_id)
    count_query = (
        select(func.count())
        .select_from(CodeReview)
        .where(CodeReview.repo_id == repo_id)
    )

    if min_risk is not None:
        base_query = base_query.where(CodeReview.risk_score >= min_risk)
        count_query = count_query.where(CodeReview.risk_score >= min_risk)

    if severity:
        predicate = _severity_predicate(severity)
        base_query = base_query.where(predicate)
        count_query = count_query.where(predicate)

    # `total` is the number of reviews matching every filter above, counted by
    # the COUNT query — not the length of the page below. The severity predicate
    # is on both statements, so the two cannot disagree, and the dashboard (which
    # renders pagination only while total > PAGE_SIZE) can still reach page 2.
    total = await db.scalar(count_query) or 0

    query = (
        base_query.options(selectinload(CodeReview.repo))
        .order_by(CodeReview.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    res = await db.execute(query)
    reviews = res.scalars().all()

    # The severity predicate lives in SQL (``_severity_predicate`` on both the
    # page and COUNT queries above): it is the single source of truth, so every
    # row the database returned already matches and must reach the client.
    mapped_reviews = [_map_review_to_out(r, repo=repo) for r in reviews]

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
    count_query = (
        select(func.count())
        .select_from(CodeReview)
        .where(CodeReview.repo_id.in_(user_repo_ids))
    )

    if repo_id is not None:
        if repo_id not in user_repo_ids:
            return CodeReviewListOut(reviews=[], total=0)
        base_query = base_query.where(CodeReview.repo_id == repo_id)
        count_query = count_query.where(CodeReview.repo_id == repo_id)

    if min_risk is not None:
        base_query = base_query.where(CodeReview.risk_score >= min_risk)
        count_query = count_query.where(CodeReview.risk_score >= min_risk)

    if severity:
        predicate = _severity_predicate(severity)
        base_query = base_query.where(predicate)
        count_query = count_query.where(predicate)

    # Same contract as get_repo_reviews: `total` counts every matching review
    # across all pages, and the severity predicate is on the COUNT as well as the
    # page, so the count and the rows cannot contradict each other.
    total = await db.scalar(count_query) or 0

    query = (
        base_query.options(selectinload(CodeReview.repo))
        .order_by(CodeReview.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    res = await db.execute(query)
    reviews = res.scalars().all()

    # Same single-source-of-truth contract as get_repo_reviews: the SQL
    # severity predicate already decided which rows match.
    mapped_reviews = [_map_review_to_out(r) for r in reviews]

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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Code review not found"
        )

    return _map_review_to_out(review)


@router.get("/reviews/{review_id}/associated-pr", response_model=AssociatedPRResponse)
async def get_review_associated_pr(
    review_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AssociatedPRResponse:
    """
    On-demand associated-PR lookup for a single code review.

    Read-only: no DB writes, no ingestion/dedup side effects. Answers
    ``{"associated_pr": null}`` (HTTP 200) whenever no PR can be established —
    direct-to-main pushes genuinely have none — so the dashboard row keeps
    rendering as a plain push instead of failing the page.

    Resolution order: the row's own ``pr_number`` first (no GitHub call),
    else ``GET /repos/{owner}/{repo}/commits/{sha}/pulls`` with the repo
    installation token (server PAT fallback), preferring the highest open PR
    number and falling back to the highest PR number.
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Code review not found"
        )

    repo = review.repo
    owner = repo.owner if repo is not None else None
    name = repo.name if repo is not None else None

    if review.pr_number is not None and owner and name:
        return AssociatedPRResponse(
            associated_pr=AssociatedPROut(
                number=review.pr_number,
                url=(
                    f"https://github.com/{quote(owner, safe='')}"
                    f"/{quote(name, safe='')}/pull/{review.pr_number}"
                ),
                title=None,
            )
        )
    if not owner or not name or not review.commit_sha:
        return AssociatedPRResponse(associated_pr=None)
    # Prefer the repo-scoped installation token, as the sibling sessions
    # router does; get_installation_token already falls back to the server PAT
    # in dev. The mint runs under a shorter timeout than the lookup itself:
    # its transport allows 30s (DEFAULT_TIMEOUT_SECONDS), which would blow
    # the 5s lookup budget on a cold cache — a slow mint answers None so the
    # lookup falls through to the PAT / null path instead of stalling the row.
    try:
        gh_token: Optional[str] = await asyncio.wait_for(
            get_installation_token(repo), timeout=_MINT_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        logger.debug(
            "associated-pr lookup installation-token mint timed out after %.1fs",
            _MINT_TIMEOUT_SECONDS,
        )
        gh_token = None
    except Exception as exc:
        logger.debug(
            "associated-pr lookup installation-token mint failed: %s",
            type(exc).__name__,
        )
        gh_token = None
    associated = await _lookup_commit_pr(owner, name, review.commit_sha, token=gh_token)
    return AssociatedPRResponse(associated_pr=associated)


async def _batch_installation_tokens(
    pending: list[tuple[uuid.UUID, CodeReview, str, str]],
) -> dict[uuid.UUID, Optional[str]]:
    """Mint one repo-scoped token per distinct repo (issue #116 batch)."""
    tokens: dict[uuid.UUID, Optional[str]] = {}
    seen: dict[uuid.UUID, CodeReview] = {}
    for _, review, _, _ in pending:
        if review.repo_id not in seen:
            seen[review.repo_id] = review
    for repo_id, review in seen.items():
        repo = review.repo
        if repo is None:
            tokens[repo_id] = None
            continue
        try:
            tokens[repo_id] = await asyncio.wait_for(
                get_installation_token(repo), timeout=_MINT_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            logger.debug(
                "batch associated-pr installation-token mint timed out after %.1fs",
                _MINT_TIMEOUT_SECONDS,
            )
            tokens[repo_id] = None
        except Exception as exc:
            logger.debug(
                "batch associated-pr installation-token mint failed: %s",
                type(exc).__name__,
            )
            tokens[repo_id] = None
    return tokens


@router.post("/reviews/associated-pr/batch", response_model=AssociatedPRBatchResponse)
async def batch_review_associated_pr(
    payload: AssociatedPRBatchRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AssociatedPRBatchResponse:
    """
    Batched associated-PR lookup for a dashboard page (issue #116).

    One ownership-scoped ``SELECT ... WHERE id IN (...)`` answers every row
    the database can (``pr_number`` set resolves directly, no GitHub call);
    the rest resolve concurrently through the same commit-pulls lookup the
    single-row endpoint uses. Unknown or unowned ids answer ``null`` (never
    404) so one bad id cannot fail the page. Read-only: no DB writes.
    """
    ordered_ids: list[uuid.UUID] = list(dict.fromkeys(payload.review_ids))
    resolved: dict[uuid.UUID, Optional[AssociatedPROut]] = {}
    if not ordered_ids:
        return AssociatedPRBatchResponse(associated_prs=resolved)

    stmt = (
        select(CodeReview)
        .join(Repo, CodeReview.repo_id == Repo.id)
        .options(selectinload(CodeReview.repo))
        .where(CodeReview.id.in_(ordered_ids), Repo.user_id == current_user.id)
    )
    res = await db.execute(stmt)
    rows = res.scalars().all()
    by_id = {row.id: row for row in rows}

    pending: list[tuple[uuid.UUID, CodeReview, str, str]] = []
    for rid in ordered_ids:
        review = by_id.get(rid)
        if review is None:
            resolved[rid] = None
            continue
        repo = review.repo
        owner = repo.owner if repo is not None else None
        name = repo.name if repo is not None else None
        if review.pr_number is not None and owner and name:
            resolved[rid] = AssociatedPROut(
                number=review.pr_number,
                url=(
                    f"https://github.com/{quote(owner, safe='')}"
                    f"/{quote(name, safe='')}/pull/{review.pr_number}"
                ),
                title=None,
            )
        elif not owner or not name or not review.commit_sha:
            resolved[rid] = None
        else:
            pending.append((rid, review, owner, name))

    if pending:
        tokens = await _batch_installation_tokens(pending)
        lookups = [
            _lookup_commit_pr(
                owner, name, review.commit_sha, token=tokens.get(review.repo_id)
            )
            for _, review, owner, name in pending
        ]
        for (rid, _, _, _), associated in zip(pending, await asyncio.gather(*lookups)):
            resolved[rid] = associated

    return AssociatedPRBatchResponse(associated_prs=resolved)
