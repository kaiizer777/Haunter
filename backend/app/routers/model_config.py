"""
Model Configuration endpoints for Haunter (Phase 4).

Supports reading and updating active model configurations globally and per-repo.
All endpoints are gated by get_current_user and strictly validated with Pydantic allowlists.

Security invariants:
- base_url is derived server-side from allowlist map (_PROVIDER_BASE_URLS) — never accepted
  from client to prevent SSRF and endpoint hijacking.
- Per-repo updates enforce tenant ownership: returns 404 (not 403) on non-owned repos to
  prevent existence oracle leakage.
- Global model config switcher is restricted to ADMIN_USER_ID if configured.
- SQL queries use parameterised SQLAlchemy ORM constructs.
"""

import logging
import uuid
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import settings
from app.db import get_db
from app.models import ModelConfig, Repo, User
from app.schemas import ModelConfigOut, ModelConfigUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/config/model", tags=["model_config"])

# Server-derived base URLs — prevents SSRF / redirect to malicious endpoints
_PROVIDER_BASE_URLS: dict[str, str] = {
    "opencode_zen": "https://opencode.ai/zen/v1",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
}


@router.get("", response_model=ModelConfigOut)
async def get_active_model_config_endpoint(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    repo_id: Annotated[Optional[uuid.UUID], Query(description="Optional repo ID for repo-specific config")] = None,
) -> ModelConfigOut:
    """
    Get the currently active model configuration.
    If repo_id is provided, returns the repo's active config (or 404 if repo not owned).
    If repo_id is omitted, returns the global active model config from DB or env defaults.
    """
    if repo_id is not None:
        repo_result = await db.execute(
            select(Repo).where(Repo.id == repo_id, Repo.user_id == current_user.id)
        )
        repo = repo_result.scalar_one_or_none()
        if repo is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repo not found")

        if repo.active_model_config_id is not None:
            config_result = await db.execute(
                select(ModelConfig).where(ModelConfig.id == repo.active_model_config_id)
            )
            config = config_result.scalar_one_or_none()
            if config is not None:
                return ModelConfigOut.model_validate(config)

    # Global active config lookup
    global_result = await db.execute(
        select(ModelConfig)
        .where(ModelConfig.is_active == True)  # noqa: E712
        .order_by(ModelConfig.created_at.desc())
        .limit(1)
    )
    active_config = global_result.scalar_one_or_none()

    if active_config is not None:
        return ModelConfigOut.model_validate(active_config)

    # Fallback to default configuration
    return ModelConfigOut(
        id=uuid.uuid4(),
        provider=settings.default_provider,
        model_name=settings.default_model,
        base_url=settings.opencode_zen_base_url,
        is_active=True,
    )


@router.put("", response_model=ModelConfigOut)
async def update_model_config_endpoint(
    body: ModelConfigUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ModelConfigOut:
    """
    Update active model configuration.
    - If body.repo_id is supplied: updates model config for that repo (enforces ownership).
    - If body.repo_id is null: updates global active model config (admin-restricted if ADMIN_USER_ID is set).
    """
    base_url = _PROVIDER_BASE_URLS.get(body.provider)
    if not base_url:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid provider")

    # 1. Per-repo model config update
    if body.repo_id is not None:
        repo_result = await db.execute(
            select(Repo).where(Repo.id == body.repo_id, Repo.user_id == current_user.id)
        )
        repo = repo_result.scalar_one_or_none()
        if repo is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repo not found")

        if repo.active_model_config_id is not None:
            cfg_res = await db.execute(
                select(ModelConfig).where(ModelConfig.id == repo.active_model_config_id)
            )
            existing_cfg = cfg_res.scalar_one_or_none()
        else:
            existing_cfg = None

        if existing_cfg is not None:
            existing_cfg.provider = body.provider
            existing_cfg.model_name = body.model_name
            existing_cfg.base_url = base_url
            existing_cfg.is_active = True
            config = existing_cfg
        else:
            config = ModelConfig(
                provider=body.provider,
                model_name=body.model_name,
                base_url=base_url,
                is_active=True,
            )
            db.add(config)
            await db.flush()
            repo.active_model_config_id = config.id

        await db.commit()
        await db.refresh(config)
        logger.info(
            "Repo model config updated: user=%s repo=%s provider=%s model=%s",
            current_user.id,
            body.repo_id,
            body.provider,
            body.model_name,
        )
        return ModelConfigOut.model_validate(config)

    # 2. Global model config update
    if settings.admin_user_id and str(current_user.id) != settings.admin_user_id:
        logger.warning(
            "Non-admin user %s attempted to update global model config", current_user.id
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin permissions required to update global model config",
        )

    # Deactivate currently active global configs
    await db.execute(
        update(ModelConfig)
        .where(ModelConfig.is_active == True)  # noqa: E712
        .values(is_active=False)
    )

    new_config = ModelConfig(
        provider=body.provider,
        model_name=body.model_name,
        base_url=base_url,
        is_active=True,
    )
    db.add(new_config)
    await db.commit()
    await db.refresh(new_config)

    logger.info(
        "Global model config switched: user=%s provider=%s model=%s",
        current_user.id,
        body.provider,
        body.model_name,
    )
    return ModelConfigOut.model_validate(new_config)


@router.get("/{repo_id}", response_model=Optional[ModelConfigOut])
async def get_repo_model_config(
    repo_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Optional[ModelConfigOut]:
    """
    Get the active model config for a specific repo.
    Returns 404 if the repo is not found or not owned by caller.
    """
    return await get_active_model_config_endpoint(
        current_user=current_user,
        db=db,
        repo_id=repo_id,
    )


@router.put("/{repo_id}", response_model=ModelConfigOut)
async def update_repo_model_config(
    repo_id: uuid.UUID,
    body: ModelConfigUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ModelConfigOut:
    """
    Update the active model config for a specific repo.
    Returns 404 if the repo is not found or not owned by caller.
    """
    body.repo_id = repo_id
    return await update_model_config_endpoint(
        body=body,
        current_user=current_user,
        db=db,
    )
