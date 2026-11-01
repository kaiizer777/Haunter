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
from app.models import ModelConfig

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


def _model_config_cache_key() -> str:
    return "global"


def invalidate_model_config_cache() -> None:
    """Drop cached model config.

    Called after every model-config write (PUT /config/model) and by test
    isolation helpers after truncate/fake store reset, so the next
    ``complete()`` re-queries instead of serving a stale model for up to
    TTL. Bumps the invalidation generation so in-flight resolves that
    started before this call do not repopulate the cache with stale data.
    """
    global _model_config_gen
    _model_config_gen += 1
    _model_config_cache.clear()


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
    # Issue #205: only opencode_go + groq are supported. Unknown providers
    # fall back to the opencode_go base URL.
    name = (provider or settings.default_provider or "opencode_go").strip().lower()
    mapping: dict[str, str] = {
        "opencode_go": settings.opencode_go_base_url,
        "groq": settings.groq_base_url,
    }
    return mapping.get(name, settings.opencode_go_base_url)


async def _resolve_from_db(session: AsyncSession) -> ResolvedModelConfig | None:
    result = await session.execute(
        select(ModelConfig)
        .where(ModelConfig.is_active == True)  # noqa: E712
        .order_by(ModelConfig.created_at.desc())
        .limit(1)
    )
    cfg = result.scalar_one_or_none()
    if cfg:
        return ResolvedModelConfig(
            provider=cfg.provider,
            model_name=cfg.model_name,
            base_url=cfg.base_url,
        )
    return None


async def get_active_model_config(
    db: AsyncSession | None = None,
) -> ResolvedModelConfig:
    """
    Resolve active model configuration.

    Order of precedence:
    1. Global active model config from DB (model_configs where is_active=true)
    2. Environment variable defaults (DEFAULT_PROVIDER, DEFAULT_MODEL, and the
       provider-matched base URL via default_base_url_for_provider).

    DB resolutions are served from a 60s in-process TTL cache, so hot
    per-``complete()`` lookups cost at most one SELECT per minute instead of
    2-3 SELECTs per call.
    """
    cache_key = _model_config_cache_key()
    cached = _model_config_cache.get(cache_key)
    if cached is not None:
        resolved_cached, cached_at = cached
        if time.monotonic() - cached_at < _MODEL_CONFIG_TTL:
            return resolved_cached

    generation = _model_config_gen
    try:
        if db is not None:
            config = await _resolve_from_db(db)
            if config:
                _store_model_config(
                    cache_key, config, generation, time.monotonic()
                )
                return config
        else:
            async with async_session_maker() as session:
                config = await _resolve_from_db(session)
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
