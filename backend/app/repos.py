"""
Repo CRUD + model config stub endpoints for Haunter.

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
from app.models import ModelConfig, Repo, User
from app.schemas import ModelConfigOut, ModelConfigUpdate, RepoCreate, RepoOut

logger = logging.getLogger(__name__)

router = APIRouter(tags=["repos"])

# Provider → base_url allowlist — derived server-side, never accepted from client.
# Extend only after vetting the provider.
_PROVIDER_BASE_URLS: dict[str, str] = {
    "opencode_zen": "https://opencode.ai/zen/v1",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
}


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

    repo = Repo(
        user_id=current_user.id,
        owner=body.owner,
        name=body.name,
        default_branch=body.default_branch,
        language_hint=body.language_hint,
        active_model_config_id=body.active_model_config_id,
    )
    db.add(repo)
    await db.commit()
    await db.refresh(repo)

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


# ---------------------------------------------------------------------------
# Model config stub (Phase 4 will fill in the full LLM switcher logic)
# ---------------------------------------------------------------------------


@router.get("/config/model/{repo_id}", response_model=ModelConfigOut | None)
async def get_model_config(
    repo_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ModelConfigOut | None:
    """
    Return the active model config for a repo.
    Returns 404 if the repo doesn't exist or isn't owned by the caller.
    Returns null body (200) if no model config is set.
    """
    repo_result = await db.execute(
        select(Repo).where(Repo.id == repo_id, Repo.user_id == current_user.id)
    )
    repo = repo_result.scalar_one_or_none()
    if repo is None:
        raise HTTPException(status_code=404, detail="Repo not found")

    if repo.active_model_config_id is None:
        return None

    config_result = await db.execute(
        select(ModelConfig).where(ModelConfig.id == repo.active_model_config_id)
    )
    config = config_result.scalar_one_or_none()
    if config is None:
        return None

    return ModelConfigOut.model_validate(config)


@router.put("/config/model/{repo_id}", response_model=ModelConfigOut)
async def update_model_config(
    repo_id: uuid.UUID,
    body: ModelConfigUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ModelConfigOut:
    """
    Update or create the model config for a repo.

    Security invariants:
    - Caller must own the repo (404 otherwise — existence oracle prevention).
    - provider and model_name are Pydantic Literal allowlists — no free-text injection.
    - base_url is derived server-side from the provider allowlist — never accepted
      from the client. This prevents base_url injection to an attacker-controlled endpoint.
    """
    repo_result = await db.execute(
        select(Repo).where(Repo.id == repo_id, Repo.user_id == current_user.id)
    )
    repo = repo_result.scalar_one_or_none()
    if repo is None:
        raise HTTPException(status_code=404, detail="Repo not found")

    # base_url is derived server-side — never from client input.
    base_url = _PROVIDER_BASE_URLS[body.provider]

    if repo.active_model_config_id is not None:
        config_result = await db.execute(
            select(ModelConfig).where(ModelConfig.id == repo.active_model_config_id)
        )
        existing_config = config_result.scalar_one_or_none()
    else:
        existing_config = None

    if existing_config is not None:
        existing_config.provider = body.provider
        existing_config.model_name = body.model_name
        existing_config.base_url = base_url
        config = existing_config
    else:
        config = ModelConfig(
            provider=body.provider,
            model_name=body.model_name,
            base_url=base_url,
        )
        db.add(config)
        await db.flush()  # get the id before updating repo FK
        repo.active_model_config_id = config.id

    await db.commit()
    await db.refresh(config)

    logger.info(
        "Model config updated: user=%s repo=%s provider=%s model=%s",
        current_user.id, repo_id, body.provider, body.model_name,
    )
    return ModelConfigOut.model_validate(config)
