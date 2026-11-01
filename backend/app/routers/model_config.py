"""
Model Configuration endpoints for Haunter (Phase 4).

Supports reading and updating active model configurations globally and per-repo.
All endpoints are gated by get_current_user and strictly validated with Pydantic allowlists.

Security invariants:
- base_url is derived server-side from the provider allowlist map (settings-driven) — never accepted
  from client to prevent SSRF and endpoint hijacking.
- Per-repo updates enforce tenant ownership: returns 404 (not 403) on non-owned repos to
  prevent existence oracle leakage.
- Global model config reads and writes are admin-only.
- SQL queries use parameterised SQLAlchemy ORM constructs.
"""

import logging
import uuid
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, is_admin_user
from app.config import settings
from app.db import get_db
from app.llm.config import invalidate_model_config_cache
from app.llm.discovery import format_model_name, get_dynamic_go_models
from app.models import ModelConfig, Repo, User
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
    # Issue #205: only opencode_go + groq are supported. Retired providers
    # resolve to None so writes fail closed with an explicit 422.
    return {
        "opencode_go": settings.opencode_go_base_url,
        "groq": settings.groq_base_url,
    }.get(provider)


def _default_base_url() -> str:
    """Fallback base URL derived from settings.default_provider (Phase 3.2 Issue 5)."""
    return (
        _base_url_for_provider(settings.default_provider)
        or settings.opencode_go_base_url
    )


@router.get("", response_model=ModelConfigOut)
async def get_active_model_config_endpoint(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ModelConfigOut:
    """
    Get the currently active global model configuration.
    Admin-only.
    """
    if not is_admin_user(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin permissions required to read model config",
        )

    global_result = await db.execute(
        select(ModelConfig)
        .where(ModelConfig.is_active == True)  # noqa: E712
        .order_by(ModelConfig.created_at.desc())
        .limit(1)
    )
    active_config = global_result.scalar_one_or_none()

    if active_config is not None and _base_url_for_provider(
        active_config.provider
    ):
        return ModelConfigOut.model_validate(active_config)
    if active_config is not None:
        logger.warning(
            "model_config: active global row uses retired provider %s; "
            "serving env defaults until an admin saves a supported provider",
            active_config.provider,
        )

    # Fallback to default configuration (base_url follows settings.default_provider).
    return ModelConfigOut(
        id=uuid.uuid4(),
        provider=settings.default_provider,
        model_name=settings.default_model,
        base_url=_default_base_url(),
        is_active=True,
    )


async def _require_model_admin(
    current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    """Dependency that 403s non-admins before request-body validation runs."""
    _require_admin(current_user)
    return current_user


def _require_admin(current_user: User) -> None:
    """Raise 403 if caller is not an admin. Closed by default: no open bypass."""
    if not is_admin_user(current_user):
        logger.warning(
            "model_config: non-admin user %s attempted model config write",
            current_user.id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin permissions required to update global model config",
        )


@router.put("", response_model=ModelConfigOut)
async def update_model_config_endpoint(
    body: ModelConfigUpdate,
    current_user: Annotated[User, Depends(_require_model_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ModelConfigOut:
    """
    Update active global model configuration (admin-only).
    """
    # Gate before any request handling: body validation and the provider
    # allowlist check below must not leak details to non-admins.
    _require_admin(current_user)
    base_url = _base_url_for_provider(body.provider)
    if not base_url:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid provider: expected 'opencode_go' or 'groq'",
        )

    # Deactivate currently active global configs.
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
    await db.flush()
    await db.commit()
    # Invalidate before the refresh() await (#134-3).
    invalidate_model_config_cache()
    await db.refresh(new_config)

    logger.info(
        "Global model config switched: user=%s provider=%s model=%s",
        current_user.id,
        body.provider,
        body.model_name,
    )
    return ModelConfigOut.model_validate(new_config)


MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    # Static context-window catalog for known model IDs.
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
    "deepseek-v4.1-flash": 1000000,
    "glm-5.2": 1000000,
    "mimo-v2.6-flash": 1000000,
}


@router.get("/available", response_model=AvailableModelsOut)
async def get_available_models_endpoint(
    current_user: Annotated[User, Depends(get_current_user)],
) -> AvailableModelsOut:
    """
    Admin-only: full model vocabulary is never exposed to non-admins.
    Get live list of available models per provider (issue #205: only
    opencode_go + groq are default providers).
    For opencode_go: dynamically queries /models with TTL caching.
    For groq: returns approved production models with context window metadata.
    """
    if not is_admin_user(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin permissions required to list available models",
        )
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
        AvailableModelItem(
            id="deepseek-r1-distill-llama-70b",
            name="DeepSeek R1 Distill Llama 70B",
            tag="Reasoning Distill",
            context_window=MODEL_CONTEXT_WINDOWS.get(
                "deepseek-r1-distill-llama-70b", 131072
            ),
        ),
    ]

    dynamic_go_models = await get_dynamic_go_models()
    go_items: list[AvailableModelItem] = []
    for item in dynamic_go_models:
        mid = item["id"]
        tag = item.get("tag") or ("Default · Direct Gateway" if mid == settings.default_model else "Direct Gateway")
        ctx = item.get("context_window") or MODEL_CONTEXT_WINDOWS.get(mid, 1000000)
        go_items.append(
            AvailableModelItem(
                id=mid,
                name=item.get("name") or format_model_name(mid),
                tag=tag,
                context_window=ctx,
            )
        )

    return AvailableModelsOut(
        opencode_go=go_items,
        groq=groq_items,
    )

