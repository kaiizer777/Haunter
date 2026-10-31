"""
Dynamic model discovery helper for OpenCode Zen free-tier models.

Queries GET {base_url}/models with provider API key and filters for models
ending with '-free'. Uses in-memory caching with a 15-minute TTL to avoid
redundant HTTP calls on every completion request, and falls back gracefully
to the last known discovered list or a minimal bootstrap fallback set on failure.
"""

import asyncio
import logging
import time
import uuid
from typing import Any
from urllib.parse import urljoin

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Minimal bootstrap fallback set when /models is unreachable and no cache exists
BOOTSTRAP_FREE_MODELS: list[str] = [
    "ling-3.0-flash-fin-free",
    "laguna-s-2.1-free",
    "nemotron-3.5-lightning-free",
]

# Minimal bootstrap fallback set for OpenCode Go when discovery is unreachable
BOOTSTRAP_GO_MODELS: list[dict[str, Any]] = [
    {
        "id": "deepseek-v4.1-flash",
        "name": "DeepSeek V4.1 Flash",
        "context_window": 1000000,
        "capabilities": {"reasoning": True, "tools": True},
        "tag": "Production · Low Latency",
    },
    {
        "id": "glm-5.2",
        "name": "GLM 5.2",
        "context_window": 1000000,
        "capabilities": {"reasoning": True, "tools": True},
        "tag": "High Reasoning",
    },
    {
        "id": "mimo-v2.6-flash",
        "name": "Mimo V2.6 Flash",
        "context_window": 1000000,
        "capabilities": {"reasoning": True, "tools": True},
        "tag": "Fast",
    },
    {
        "id": "nemotron-3.5-lightning-free",
        "name": "Nemotron 3.5 Lightning",
        "context_window": 131072,
        "capabilities": {"reasoning": False, "tools": True},
        "tag": "Free Tier",
    },
]


def format_model_name(model_id: str) -> str:
    """Format model ID into human-readable label."""
    clean = model_id
    if clean.endswith("-free"):
        clean = clean[:-5]
    parts = clean.split("-")
    return " ".join(part.capitalize() for part in parts)

# In-memory cache state keyed by base_url: base_url -> (expires_at, models)
_dynamic_cache: dict[str, tuple[float, list[str]]] = {}
_last_known_cache: dict[str, list[str]] = {}

_dynamic_go_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_last_known_go_cache: dict[str, list[dict[str, Any]]] = {}

# Legacy single-entry globals maintained for backward compatibility with unit tests
_cached_free_models: list[str] | None = None
_cache_expires_at: float = 0.0
_last_known_free_models: list[str] = list(BOOTSTRAP_FREE_MODELS)
_cache_lock = asyncio.Lock()
_go_cache_lock = asyncio.Lock()

# 15-minute TTL (in seconds)
CACHE_TTL_SECONDS: float = 900.0


def clear_model_cache() -> None:
    """Clear in-memory cache and reset to bootstrap state. Primarily used in unit tests."""
    global _cached_free_models, _cache_expires_at, _last_known_free_models
    _dynamic_cache.clear()
    _last_known_cache.clear()
    _dynamic_go_cache.clear()
    _last_known_go_cache.clear()
    _cached_free_models = None
    _cache_expires_at = 0.0
    _last_known_free_models = list(BOOTSTRAP_FREE_MODELS)


async def get_dynamic_free_models(
    base_url: str | None = None,
    api_key: str | None = None,
    force_refresh: bool = False,
    timeout: float = 10.0,
) -> list[str]:
    """
    Retrieve available free-tier model identifiers ending with '-free'.

    - Checks in-memory cache first (15-minute TTL) keyed by base_url.
    - If cache expired or force_refresh=True, queries GET {base_url}/models.
    - Filters model IDs ending with '-free'.
    - On network error, timeout, or non-200 status, falls back to the last known
      cached list for the base_url or BOOTSTRAP_FREE_MODELS without raising an exception.

    Args:
        base_url: Base provider URL (defaults to settings.opencode_zen_base_url).
        api_key: Provider API key (defaults to settings.opencode_zen_api_key).
        force_refresh: If True, bypasses cache and forces an HTTP call.
        timeout: HTTP request timeout in seconds.

    Returns:
        list[str]: Ordered, de-duplicated list of model IDs ending with '-free'.
    """
    global _cached_free_models, _cache_expires_at, _last_known_free_models

    resolved_base_url = (base_url or settings.opencode_zen_base_url).rstrip("/") + "/"
    resolved_api_key = api_key or settings.opencode_zen_api_key

    now = time.monotonic()
    if not force_refresh:
        cached_entry = _dynamic_cache.get(resolved_base_url)
        if cached_entry is not None and now < cached_entry[0]:
            return list(cached_entry[1])

    if not resolved_api_key:
        logger.warning(
            "opencode_zen: no API key configured for model discovery; using fallback list"
        )
        return list(_last_known_cache.get(resolved_base_url, _last_known_free_models))

    endpoint = urljoin(resolved_base_url, "models")
    headers = {
        "Authorization": f"Bearer {resolved_api_key}",
        "Content-Type": "application/json",
        "User-Agent": "opencode/1.0.0",
        "x-session-id": f"sess_{uuid.uuid4().hex}",
    }

    async with _cache_lock:
        # Re-check cache inside lock to prevent thundering herd
        now = time.monotonic()
        if not force_refresh:
            cached_entry = _dynamic_cache.get(resolved_base_url)
            if cached_entry is not None and now < cached_entry[0]:
                return list(cached_entry[1])

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(endpoint, headers=headers)
                response.raise_for_status()
                payload = response.json()

            # Handle both {"data": [...]} (standard OpenAI) and raw list [...]
            raw_items: list[Any] = []
            if isinstance(payload, dict):
                raw_items = payload.get("data") or payload.get("models") or []
            elif isinstance(payload, list):
                raw_items = payload

            discovered: list[str] = []
            for item in raw_items:
                model_id = ""
                if isinstance(item, dict):
                    model_id = str(item.get("id") or item.get("name") or "")
                elif isinstance(item, str):
                    model_id = item

                model_id = model_id.strip()
                if model_id.endswith("-free") and model_id not in discovered:
                    discovered.append(model_id)

            if discovered:
                expires_at = time.monotonic() + CACHE_TTL_SECONDS
                _dynamic_cache[resolved_base_url] = (expires_at, list(discovered))
                _last_known_cache[resolved_base_url] = list(discovered)
                _cached_free_models = list(discovered)
                _last_known_free_models = list(discovered)
                _cache_expires_at = expires_at
                logger.info(
                    "opencode_zen: discovered %d free-tier models from %s: %s",
                    len(discovered),
                    endpoint,
                    discovered,
                )
                return list(discovered)

            logger.warning(
                "opencode_zen: GET %s returned no models ending with '-free'; using last known list",
                endpoint,
            )
            return list(_last_known_cache.get(resolved_base_url, _last_known_free_models))

        except Exception as exc:
            logger.warning(
                "opencode_zen: failed to query dynamic models from %s (%s: %s); falling back to cached/bootstrap list",
                endpoint,
                type(exc).__name__,
                exc,
            )
            return list(_last_known_cache.get(resolved_base_url, _last_known_free_models))



async def get_dynamic_go_models(
    endpoint_url: str | None = None,
    api_key: str | None = None,
    force_refresh: bool = False,
    timeout: float = 10.0,
) -> list[dict[str, Any]]:
    """
    Retrieve available OpenCode Go models dynamically from https://opencode.ai/inference/go/v1/models.

    - Checks in-memory cache first (15-minute TTL) keyed by endpoint_url.
    - If cache expired or force_refresh=True, queries GET endpoint_url.
    - Sends x-opencode-session: ses_<id>, x-session-id, and Authorization: Bearer <key>.
    - Extracts model id, name, limit.context (as context_window), capabilities, etc.
    - On network error, timeout, or non-200 status, falls back gracefully to
      the last known discovered list or BOOTSTRAP_GO_MODELS without raising an exception.
    """
    resolved_endpoint = endpoint_url or getattr(
        settings, "opencode_go_discovery_url", "https://opencode.ai/inference/go/v1/models"
    )
    resolved_api_key = (
        api_key
        or settings.opencode_go_api_key
        or settings.opencode_zen_api_key
    )

    now = time.monotonic()
    if not force_refresh:
        cached_entry = _dynamic_go_cache.get(resolved_endpoint)
        if cached_entry is not None and now < cached_entry[0]:
            return [dict(m) for m in cached_entry[1]]

    if not resolved_api_key:
        logger.warning(
            "opencode_go: no API key configured for model discovery; using fallback list"
        )
        fallback = _last_known_go_cache.get(resolved_endpoint, BOOTSTRAP_GO_MODELS)
        return [dict(m) for m in fallback]

    session_id = f"ses_{uuid.uuid4().hex}"
    headers = {
        "Authorization": f"Bearer {resolved_api_key}",
        "Content-Type": "application/json",
        "User-Agent": "opencode/1.0.0",
        "x-session-id": session_id,
        "x-opencode-session": session_id,
    }

    async with _go_cache_lock:
        now = time.monotonic()
        if not force_refresh:
            cached_entry = _dynamic_go_cache.get(resolved_endpoint)
            if cached_entry is not None and now < cached_entry[0]:
                return [dict(m) for m in cached_entry[1]]

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(resolved_endpoint, headers=headers)
                response.raise_for_status()
                payload = response.json()

            raw_items: list[Any] = []
            if isinstance(payload, dict):
                raw_items = payload.get("data") or payload.get("models") or []
            elif isinstance(payload, list):
                raw_items = payload

            discovered: list[dict[str, Any]] = []
            seen_ids: set[str] = set()

            for item in raw_items:
                if isinstance(item, dict):
                    model_id = str(item.get("id") or item.get("name") or "").strip()
                    if not model_id or model_id in seen_ids:
                        continue
                    seen_ids.add(model_id)

                    name = str(item.get("name") or "").strip() or format_model_name(model_id)
                    limit = item.get("limit")
                    context_window = None
                    if isinstance(limit, dict):
                        context_window = limit.get("context")
                    if context_window is None:
                        context_window = item.get("context_window") or item.get("context_length")

                    capabilities = item.get("capabilities") or {}
                    tag = item.get("tag") or (
                        "Direct Gateway" if not model_id.endswith("-free") else "Free Tier"
                    )

                    discovered.append({
                        "id": model_id,
                        "name": name,
                        "context_window": context_window,
                        "capabilities": capabilities,
                        "tag": tag,
                    })
                elif isinstance(item, str):
                    model_id = item.strip()
                    if not model_id or model_id in seen_ids:
                        continue
                    seen_ids.add(model_id)
                    discovered.append({
                        "id": model_id,
                        "name": format_model_name(model_id),
                        "context_window": None,
                        "capabilities": {},
                        "tag": "Direct Gateway" if not model_id.endswith("-free") else "Free Tier",
                    })

            if discovered:
                expires_at = time.monotonic() + CACHE_TTL_SECONDS
                _dynamic_go_cache[resolved_endpoint] = (expires_at, list(discovered))
                _last_known_go_cache[resolved_endpoint] = list(discovered)
                logger.info(
                    "opencode_go: discovered %d models from %s: %s",
                    len(discovered),
                    resolved_endpoint,
                    [m["id"] for m in discovered],
                )
                return [dict(m) for m in discovered]

            logger.warning(
                "opencode_go: GET %s returned no models; using last known list",
                resolved_endpoint,
            )
            fallback = _last_known_go_cache.get(resolved_endpoint, BOOTSTRAP_GO_MODELS)
            _dynamic_go_cache[resolved_endpoint] = (time.monotonic() + 60.0, list(fallback))
            return [dict(m) for m in fallback]

        except Exception as exc:
            logger.warning(
                "opencode_go: failed to query dynamic models from %s (%s: %s); falling back to cached/bootstrap list",
                resolved_endpoint,
                type(exc).__name__,
                exc,
            )
            fallback = _last_known_go_cache.get(resolved_endpoint, BOOTSTRAP_GO_MODELS)
            _dynamic_go_cache[resolved_endpoint] = (time.monotonic() + 60.0, list(fallback))
            return [dict(m) for m in fallback]
