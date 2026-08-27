"""
Repo CRUD endpoints for Haunter.

All endpoints are gated by get_current_user. Every query and mutation is scoped
to the authenticated user's user.id — no endpoint trusts a client-supplied repo_id
without an ownership check against the current user.

Security invariant (multi-tenant isolation):
- DELETE/mutating endpoints return 404 (not 403) when a repo exists but isn't owned
  by the caller — prevents existence oracle leakage to non-owners.
- GET /repos lists ONLY repos owned by the current user (WHERE user_id = current_user.id).
- All SQL uses parameterised ORM constructs — no raw string interpolation.
"""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_db
from app.models import Repo, User
from app.schemas import RepoCreate, RepoOut

logger = logging.getLogger(__name__)

router = APIRouter(tags=["repos"])


# ---------------------------------------------------------------------------
# Repo CRUD
# ---------------------------------------------------------------------------


@router.post("/repos", response_model=RepoOut, status_code=201)
async def add_repo(
    body: RepoCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RepoOut:
    """
    Add a repo to the current user's workspace.
    Enforces (user_id, owner, name) uniqueness — same public repo can be tracked
    by two different tenants independently.
    """
    # Check for duplicate under this user before insert.
    existing = await db.execute(
        select(Repo).where(
            Repo.user_id == current_user.id,
            Repo.owner == body.owner,
            Repo.name == body.name,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="Repo already connected")

    from sqlalchemy.exc import IntegrityError

    repo = Repo(
        user_id=current_user.id,
        owner=body.owner,
        name=body.name,
        default_branch=body.default_branch,
        language_hint=body.language_hint,
        active_model_config_id=body.active_model_config_id,
    )
    db.add(repo)
    try:
        await db.commit()
        await db.refresh(repo)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Repo already connected")

    logger.info("Repo added: user=%s repo=%s/%s", current_user.id, body.owner, body.name)
    return RepoOut.model_validate(repo)


@router.get("/repos", response_model=list[RepoOut])
async def list_repos(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[RepoOut]:
    """
    List all repos owned by the current user.
    Scoped strictly to WHERE user_id = current_user.id — no cross-tenant leakage.
    """
    result = await db.execute(
        select(Repo).where(Repo.user_id == current_user.id).order_by(Repo.created_at.desc())
    )
    repos = result.scalars().all()
    return [RepoOut.model_validate(r) for r in repos]


@router.delete("/repos/{repo_id}", status_code=204)
async def remove_repo(
    repo_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """
    Remove a repo. Returns 404 whether the repo doesn't exist OR isn't owned by
    the current user — prevents existence oracle leakage to non-owners.
    """
    result = await db.execute(
        select(Repo).where(Repo.id == repo_id, Repo.user_id == current_user.id)
    )
    repo = result.scalar_one_or_none()
    if repo is None:
        raise HTTPException(status_code=404, detail="Repo not found")

    await db.delete(repo)
    await db.commit()
    logger.info("Repo removed: user=%s repo_id=%s", current_user.id, repo_id)
