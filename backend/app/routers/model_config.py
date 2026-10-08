"""
Model Configuration endpoints for Haunter (Phase 4).

Supports reading and updating active model configurations globally and per-repo.
All endpoints are gated by get_current_user and strictly validated with Pydantic allowlists.

Security invariants:
- base_url is derived server-side from the provider allowlist map (settings-driven) — never accepted
  from client to prevent SSRF and endpoint hijacking.
- Per-repo updates enforce tenant ownership: returns 404 (not 403) on non-owned repos to
  prevent existence oracle leakage.
- Global model config switcher is open to any authenticated user (Phase 3.2 Issue 2).
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
from app.llm.discovery import get_dynamic_free_models
from app.models import ModelConfig, ModelConfigScope, Repo, User
from app.schemas import (
    AvailableModelItem,
    AvailableModelsOut,
    ModelConfigOut,
    ModelConfigUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/config/model", tags=["model_config"])


# Server-derived base URLs — prevents SSRF / redirect to malicious endpoints.
# Resolved live from settings (Phase 3.2 Issue 5) so env overrides apply;
# never accepted from the client.
def _base_url_for_provider(provider: str) -> str | None:
    """Return the configured base URL for an allowlisted provider, or None."""
    return {
        "opencode_zen": settings.opencode_zen_base_url,
        "openai": settings.openai_base_url,
        "anthropic": settings.anthropic_base_url,
        "groq": settings.groq_base_url,
    }.get(provider)


def _default_base_url() -> str:
    """Fallback base URL derived from settings.default_provider (Phase 3.2 Issue 5)."""
    return (
        _base_url_for_provider(settings.default_provider)
        or settings.opencode_zen_base_url
    )


@router.get("", response_model=ModelConfigOut)
async def get_active_model_config_endpoint(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    repo_id: Annotated[
        Optional[uuid.UUID],
        Query(description="Optional repo ID for repo-specific config"),
    ] = None,
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
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Repo not found"
            )

        # 1a. Direct repo-scoped lookup (mirrors LLM runtime _resolve_from_db):
        # newest active scope='repo' row pinned to this repo. Works even when
        # repos.active_model_config_id is NULL.
        scoped_result = await db.execute(
            select(ModelConfig)
            .where(
                ModelConfig.is_active == True,  # noqa: E712
                ModelConfig.scope == ModelConfigScope.REPO.value,
                ModelConfig.repo_id == repo_id,
            )
            .order_by(ModelConfig.created_at.desc())
            .limit(1)
        )
        scoped_cfg = scoped_result.scalar_one_or_none()
        if scoped_cfg is not None:
            return ModelConfigOut.model_validate(scoped_cfg)

        # 1b. Legacy linkage fallback: repos.active_model_config_id pointing
        # at an active row. Inactive linked rows fall through to global.
        if repo.active_model_config_id is not None:
            config_result = await db.execute(
                select(ModelConfig).where(
                    ModelConfig.id == repo.active_model_config_id,
                    ModelConfig.is_active == True,  # noqa: E712
                )
            )
            config = config_result.scalar_one_or_none()
            if config is not None:
                return ModelConfigOut.model_validate(config)

    # Global active config lookup — strictly global scope so repo overrides
    # are never surfaced as the platform default.
    global_result = await db.execute(
        select(ModelConfig)
        .where(
            ModelConfig.is_active == True,  # noqa: E712
            ModelConfig.scope == ModelConfigScope.GLOBAL.value,
        )
        .order_by(ModelConfig.created_at.desc())
        .limit(1)
    )
    active_config = global_result.scalar_one_or_none()

    if active_config is not None:
        return ModelConfigOut.model_validate(active_config)

    # Fallback to default configuration (base_url follows settings.default_provider).
    return ModelConfigOut(
        id=uuid.uuid4(),
        provider=settings.default_provider,
        model_name=settings.default_model,
        base_url=_default_base_url(),
        is_active=True,
        scope=ModelConfigScope.GLOBAL.value,
        repo_id=None,
        user_id=None,
    )


def _require_admin(current_user: User) -> None:
    """Raise 403 if caller is not an admin."""
    is_admin = current_user.is_admin or bool(
        settings.admin_user_id and str(current_user.id) == settings.admin_user_id
    )
    if not is_admin:
        logger.warning(
            "model_config: non-admin user %s attempted global model config write",
            current_user.id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin permissions required to update global model config",
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
    - If body.repo_id is null: updates global active model config (admin-only).
    """
    base_url = _base_url_for_provider(body.provider)
    if not base_url:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid provider"
        )

    # 1. Per-repo model config update
    if body.repo_id is not None:
        repo_result = await db.execute(
            select(Repo).where(Repo.id == body.repo_id, Repo.user_id == current_user.id)
        )
        repo = repo_result.scalar_one_or_none()
        if repo is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Repo not found"
            )

        if repo.active_model_config_id is not None:
            cfg_res = await db.execute(
                select(ModelConfig).where(ModelConfig.id == repo.active_model_config_id)
            )
            existing_cfg = cfg_res.scalar_one_or_none()
        else:
            existing_cfg = None

        if (
            existing_cfg is not None
            and existing_cfg.scope != ModelConfigScope.GLOBAL.value
        ):
            existing_cfg.provider = body.provider
            existing_cfg.model_name = body.model_name
            existing_cfg.base_url = base_url
            existing_cfg.is_active = True
            # Stamp legacy rows into repo scope so global queries never pick
            # them up and global deactivation never touches them.
            existing_cfg.scope = ModelConfigScope.REPO.value
            existing_cfg.repo_id = repo.id
            existing_cfg.user_id = current_user.id
            config = existing_cfg
        else:
            # existing_cfg is None, or is a shared GLOBAL row: create a new
            # repo-scoped row instead of mutating the platform default.
            config = ModelConfig(
                provider=body.provider,
                model_name=body.model_name,
                base_url=base_url,
                is_active=True,
                scope=ModelConfigScope.REPO.value,
                repo_id=repo.id,
                user_id=current_user.id,
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

    # 2. Global model config update — admin-only (matching hosting_config.py)
    _require_admin(current_user)

    # Deactivate currently active global configs only — repo overrides
    # (scope='repo') must survive global switches.
    await db.execute(
        update(ModelConfig)
        .where(
            ModelConfig.is_active == True,  # noqa: E712
            ModelConfig.scope == ModelConfigScope.GLOBAL.value,
        )
        .values(is_active=False)
    )

    new_config = ModelConfig(
        provider=body.provider,
        model_name=body.model_name,
        base_url=base_url,
        is_active=True,
        scope=ModelConfigScope.GLOBAL.value,
        repo_id=None,
        user_id=None,
    )
    db.add(new_config)
    await db.flush()
    await db.commit()
    await db.refresh(new_config)

    logger.info(
        "Global model config switched: user=%s provider=%s model=%s",
        current_user.id,
        body.provider,
        body.model_name,
    )
    return ModelConfigOut.model_validate(new_config)


def _format_model_name(model_id: str) -> str:
    """Format model ID into human-readable label."""
    clean = model_id
    if clean.endswith("-free"):
        clean = clean[:-5]
    parts = clean.split("-")
    return " ".join(part.capitalize() for part in parts)


MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    # OpenCode Zen Free Models
    "space-bunny-free": 1048576,
    "longcat-2.5-preview-free": 1048576,
    "ling-3.0-flash-fin-free": 1048576,
    "mimo-v2.6-flash-free": 262144,
    "mimo-v2.5-free": 262144,
    "claude-sonnet-4-5": 200000,
    "claude-haiku-3-5": 200000,
    "laguna-s-2.1-free": 65536,
    "deepseek-r1-0528-free": 65536,
    "gemma2-9b-it": 8192,
    "nemotron-3.5-lightning-free": 131072,
    "nemotron-3-ultra-free": 131072,
    "jev-1.13-free": 131072,
    "deepseek-v4-flash-free": 131072,
    "muse-spark-1.3-contributor-free": 131072,
    "muse-spark-1.2-contributor-free": 131072,
    "qwen-2.5-coder-32b-instruct-free": 131072,
    "deepseek-r1-distill-qwen-32b-free": 131072,
    "llama-3.3-70b-instruct-free": 131072,
    "gpt-4o": 128000,
    "gpt-4o-mini": 128000,
    "openai/gpt-oss-120b": 131072,
    "llama-3.3-70b-versatile": 131072,
    "llama-3.1-8b-instant": 131072,
    "deepseek-r1-distill-llama-70b": 131072,
}


@router.get("/available", response_model=AvailableModelsOut)
async def get_available_models_endpoint(
    current_user: Annotated[User, Depends(get_current_user)],
) -> AvailableModelsOut:
    """
    Get live list of available models per provider.
    For opencode_zen: dynamically queries /models with TTL caching, filtered to '-free'.
    For openai, anthropic, and groq: returns approved production models with context window metadata.
    """
    dynamic_zen_models = await get_dynamic_free_models()

    zen_items: list[AvailableModelItem] = []
    for mid in dynamic_zen_models:
        tag = "Default · Free" if mid == settings.default_model else "Free"
        ctx = MODEL_CONTEXT_WINDOWS.get(mid)
        zen_items.append(
            AvailableModelItem(
                id=mid,
                name=_format_model_name(mid),
                tag=tag,
                context_window=ctx,
            )
        )

    openai_items = [
        AvailableModelItem(
            id="gpt-4o",
            name="GPT-4o",
            tag="Flagship",
            context_window=MODEL_CONTEXT_WINDOWS.get("gpt-4o", 128000),
        ),
        AvailableModelItem(
            id="gpt-4o-mini",
            name="GPT-4o Mini",
            tag="Fast",
            context_window=MODEL_CONTEXT_WINDOWS.get("gpt-4o-mini", 128000),
        ),
    ]

    anthropic_items = [
        AvailableModelItem(
            id="claude-sonnet-4-5",
            name="Claude Sonnet 4.5",
            tag="SOTA Fixes",
            context_window=MODEL_CONTEXT_WINDOWS.get("claude-sonnet-4-5", 200000),
        ),
        AvailableModelItem(
            id="claude-haiku-3-5",
            name="Claude Haiku 3.5",
            tag="Low Latency",
            context_window=MODEL_CONTEXT_WINDOWS.get("claude-haiku-3-5", 200000),
        ),
    ]

    groq_items = [
        AvailableModelItem(
            id="openai/gpt-oss-120b",
            name="GPT-OSS 120B",
            tag="High Reasoning · Fallback",
            context_window=MODEL_CONTEXT_WINDOWS.get("openai/gpt-oss-120b", 131072),
        ),
        AvailableModelItem(
            id="llama-3.3-70b-versatile",
            name="Llama 3.3 70B Versatile",
            tag="Fast · Production",
            context_window=MODEL_CONTEXT_WINDOWS.get("llama-3.3-70b-versatile", 131072),
        ),
        AvailableModelItem(
            id="llama-3.1-8b-instant",
            name="Llama 3.1 8B Instant",
            tag="Ultra Fast",
            context_window=MODEL_CONTEXT_WINDOWS.get("llama-3.1-8b-instant", 131072),
        ),
    ]

    return AvailableModelsOut(
        opencode_zen=zen_items,
        openai=openai_items,
        anthropic=anthropic_items,
        groq=groq_items,
    )


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
