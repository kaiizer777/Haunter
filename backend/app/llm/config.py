"""
Dynamic model config resolution for Haunter LLM subsystem.

Resolves active provider, model name, and base URL from Postgres (model_configs table)
or per-repo overrides, falling back gracefully to environment variable defaults.
"""

import logging
import time
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import async_session_maker
from app.models import ModelConfig, ModelConfigScope, Repo

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedModelConfig:
    provider: str
    model_name: str
    base_url: str


# Short-TTL read cache for near-static model config (db-perf #109,
# hardened by #134).
# ResolvedModelConfig is a frozen dataclass — safe to share across callers.
# Env-default fallbacks are never cached: caching "DB empty" would hide a
# row created seconds later for up to TTL.
# The cache is bounded: expired entries are pruned on every store and the
# size is capped (#134-4). Stores are guarded by an invalidation
# generation counter so an in-flight resolve cannot restore stale data
# after a concurrent write (#134-2).
_MODEL_CONFIG_TTL: float = 60.0
_MODEL_CONFIG_MAXSIZE: int = 512
_model_config_cache: dict[str, tuple[ResolvedModelConfig, float]] = {}
_model_config_gen: int = 0


def _model_config_cache_key(repo_id: uuid.UUID | None) -> str:
    return f"repo:{repo_id}" if repo_id is not None else "global"


def invalidate_model_config_cache(repo_id: uuid.UUID | None = None) -> None:
    """Drop cached model config — one repo entry, or everything.

    Called after every model-config write (PUT /config/model global and
    per-repo updates) and by test isolation helpers after truncate/fake
    store reset, so the next ``complete()`` re-queries instead of serving
    a stale model for up to TTL. Bumps the invalidation generation so
    in-flight resolves that started before this call do not repopulate
    the cache with stale data.
    """
    global _model_config_gen
    _model_config_gen += 1
    if repo_id is None:
        _model_config_cache.clear()
    else:
        _model_config_cache.pop(_model_config_cache_key(repo_id), None)
        # A repo write can also change which global row is "latest" for
        # legacy linkage rows — drop the global entry as well.
        _model_config_cache.pop(_model_config_cache_key(None), None)


def _store_model_config(
    cache_key: str,
    config: ResolvedModelConfig,
    generation: int,
    now: float,
) -> None:
    """Cache a resolution unless an invalidation landed mid-resolve."""
    if generation != _model_config_gen:
        return
    expired = [
        key
        for key, (_, cached_at) in _model_config_cache.items()
        if now - cached_at >= _MODEL_CONFIG_TTL
    ]
    for key in expired:
        del _model_config_cache[key]
    # Enforce the cap only for a new key: overwriting an existing
    # entry must not evict an unrelated live entry.
    if cache_key not in _model_config_cache:
        while len(_model_config_cache) >= _MODEL_CONFIG_MAXSIZE:
            _model_config_cache.pop(next(iter(_model_config_cache)))
    _model_config_cache[cache_key] = (config, now)


def default_base_url_for_provider(provider: str | None = None) -> str:
    """Derive the fallback base URL for a provider from live settings (Phase 3.2 Issue 5).

    Maps ``settings.default_provider`` (or an explicit provider name) to its
    configured base URL instead of hardcoding the OpenCode Zen URL for every
    provider. Unknown providers fall back to the OpenCode Zen base URL.
    """
    name = (provider or settings.default_provider or "opencode_zen").strip().lower()
    mapping: dict[str, str] = {
        "opencode_zen": settings.opencode_zen_base_url,
        "openai": settings.openai_base_url,
        "anthropic": settings.anthropic_base_url,
        "groq": settings.groq_base_url,
    }
    return mapping.get(name, settings.opencode_zen_base_url)


async def _resolve_from_db(
    session: AsyncSession,
    repo_id: uuid.UUID | None = None,
) -> ResolvedModelConfig | None:
    # 1. Check per-repo override if repo_id is provided (repo scope wins).
    if repo_id is not None:
        # 1a. Direct repo-scoped lookup: newest active scope='repo' row pinned
        # to this repo. Works even when repos.active_model_config_id is NULL.
        scoped_result = await session.execute(
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
            return ResolvedModelConfig(
                provider=scoped_cfg.provider,
                model_name=scoped_cfg.model_name,
                base_url=scoped_cfg.base_url,
            )

        # 1b. Legacy linkage fallback: repos.active_model_config_id pointing
        # at an active row (any scope). Preserves backward compatibility with
        # rows written before the scope discriminator existed.
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

    # 2. Query global active model config — strictly global scope so repo
    # overrides are never returned as the platform default.
    global_result = await session.execute(
        select(ModelConfig)
        .where(
            ModelConfig.is_active == True,  # noqa: E712
            ModelConfig.scope == ModelConfigScope.GLOBAL.value,
        )
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
    3. Environment variable defaults (DEFAULT_PROVIDER, DEFAULT_MODEL, and the
       provider-matched base URL via default_base_url_for_provider).

    DB resolutions are served from a 60s in-process TTL cache keyed by
    repo_id, so hot per-``complete()`` lookups cost at most one SELECT per
    minute per key instead of 2-3 SELECTs per call.
    """
    cache_key = _model_config_cache_key(repo_id)
    cached = _model_config_cache.get(cache_key)
    if cached is not None:
        resolved_cached, cached_at = cached
        if time.monotonic() - cached_at < _MODEL_CONFIG_TTL:
            return resolved_cached

    generation = _model_config_gen
    try:
        if db is not None:
            config = await _resolve_from_db(db, repo_id=repo_id)
            if config:
                _store_model_config(
                    cache_key, config, generation, time.monotonic()
                )
                return config
        else:
            async with async_session_maker() as session:
                config = await _resolve_from_db(session, repo_id=repo_id)
                if config:
                    _store_model_config(
                        cache_key, config, generation, time.monotonic()
                    )
                    return config
    except Exception as exc:
        logger.warning(
            "Failed to query model_configs from DB (%s), using env defaults", exc
        )

    # Fallback to environment defaults — base_url resolves dynamically from
    # settings.default_provider (Phase 3.2 Issue 5), not a hardcoded Zen URL.
    return ResolvedModelConfig(
        provider=settings.default_provider,
        model_name=settings.default_model,
        base_url=default_base_url_for_provider(settings.default_provider),
    )
