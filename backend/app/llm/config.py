"""
Dynamic model config resolution for Haunter LLM subsystem.

Resolves active provider, model name, and base URL from Postgres (model_configs table)
or per-repo overrides, falling back gracefully to environment variable defaults.
"""

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import async_session_maker
from app.models import ModelConfig, Repo

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedModelConfig:
    provider: str
    model_name: str
    base_url: str


async def _resolve_from_db(
    session: AsyncSession,
    repo_id: uuid.UUID | None = None,
) -> ResolvedModelConfig | None:
    # 1. Check per-repo override if repo_id is provided
    if repo_id is not None:
        repo_result = await session.execute(select(Repo).where(Repo.id == repo_id))
        repo = repo_result.scalar_one_or_none()
        if repo and repo.active_model_config_id:
            cfg_result = await session.execute(
                select(ModelConfig).where(
                    ModelConfig.id == repo.active_model_config_id,
                    ModelConfig.is_active == True,  # noqa: E712
                )
            )
            cfg = cfg_result.scalar_one_or_none()
            if cfg:
                return ResolvedModelConfig(
                    provider=cfg.provider,
                    model_name=cfg.model_name,
                    base_url=cfg.base_url,
                )

    # 2. Query global active model config
    global_result = await session.execute(
        select(ModelConfig)
        .where(ModelConfig.is_active == True)  # noqa: E712
        .order_by(ModelConfig.created_at.desc())
        .limit(1)
    )
    global_cfg = global_result.scalar_one_or_none()
    if global_cfg:
        return ResolvedModelConfig(
            provider=global_cfg.provider,
            model_name=global_cfg.model_name,
            base_url=global_cfg.base_url,
        )

    return None


async def get_active_model_config(
    db: AsyncSession | None = None,
    repo_id: uuid.UUID | None = None,
) -> ResolvedModelConfig:
    """
    Resolve active model configuration.

    Order of precedence:
    1. Repo-specific active model config (if repo_id provided and linked)
    2. Global active model config from DB (model_configs where is_active=true)
    3. Environment variable defaults (DEFAULT_PROVIDER, DEFAULT_MODEL, OPENCODE_ZEN_BASE_URL)
    """
    try:
        if db is not None:
            config = await _resolve_from_db(db, repo_id=repo_id)
            if config:
                return config
        else:
            async with async_session_maker() as session:
                config = await _resolve_from_db(session, repo_id=repo_id)
                if config:
                    return config
    except Exception as exc:
        logger.warning("Failed to query model_configs from DB (%s), using env defaults", exc)

    # Fallback to environment defaults
    return ResolvedModelConfig(
        provider=settings.default_provider,
        model_name=settings.default_model,
        base_url=settings.opencode_zen_base_url,
    )
