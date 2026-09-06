"""
Tests for dynamic model discovery helper (backend/app/llm/discovery.py).

Covers:
1. clear_model_cache() resets in-memory cache and last-known to BOOTSTRAP_FREE_MODELS.
2. get_dynamic_free_models() with no API key returns fallback list without network call.
3. Successful discovery filters models ending in '-free' from {"data": [...]} and raw lists.
4. HTTP 500 server error falls back gracefully to BOOTSTRAP_FREE_MODELS without raising.
5. Network httpx.ConnectError falls back gracefully without raising.
6. In-memory TTL caching prevents redundant HTTP calls within CACHE_TTL_SECONDS.
7. force_refresh=True bypasses the TTL cache and triggers a fresh request.
"""

import httpx
import pytest
import respx

from app.config import settings
from app.llm.discovery import (
    BOOTSTRAP_FREE_MODELS,
    clear_model_cache,
    get_dynamic_free_models,
)

OPENCODE_ZEN_MODELS_URL = "https://opencode.ai/zen/v1/models"


@pytest.fixture(autouse=True)
def _reset_discovery_cache() -> None:
    clear_model_cache()
    yield
    clear_model_cache()


def test_clear_model_cache_resets_state() -> None:
    import app.llm.discovery as disc

    disc._cached_free_models = ["custom-model-free"]
    disc._cache_expires_at = 999999.0
    disc._last_known_free_models = ["custom-model-free"]

    clear_model_cache()

    assert disc._cached_free_models is None
    assert disc._cache_expires_at == 0.0
    assert disc._last_known_free_models == list(BOOTSTRAP_FREE_MODELS)


@pytest.mark.asyncio
async def test_get_dynamic_free_models_no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "opencode_zen_api_key", "")
    result = await get_dynamic_free_models(api_key=None)
    assert result == BOOTSTRAP_FREE_MODELS


@pytest.mark.asyncio
@respx.mock
async def test_get_dynamic_free_models_filters_free_tier() -> None:
    route = respx.get(OPENCODE_ZEN_MODELS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": "nemotron-3.5-lightning-free"},
                    {"id": "claude-sonnet-4-5"},
                    {"id": "qwen-2.5-coder-free"},
                    {"id": "gpt-4o-mini"},
                ]
            },
        )
    )

    result = await get_dynamic_free_models(api_key="test_key_123")

    assert route.call_count == 1
    assert result == ["nemotron-3.5-lightning-free", "qwen-2.5-coder-free"]


@pytest.mark.asyncio
@respx.mock
async def test_get_dynamic_free_models_supports_alternative_payload_shapes() -> None:
    # 1. Payload with {"models": [...]}
    respx.get("https://custom.ai/zen/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={
                "models": [
                    {"name": "alpha-free"},
                    {"name": "beta-paid"},
                ]
            },
        )
    )
    res1 = await get_dynamic_free_models(base_url="https://custom.ai/zen/v1", api_key="key1")
    assert res1 == ["alpha-free"]

    clear_model_cache()

    # 2. Payload as raw list with string and dict items
    respx.get("https://custom2.ai/zen/v1/models").mock(
        return_value=httpx.Response(
            200,
            json=[
                "gamma-free",
                {"id": "delta-free"},
                "epsilon-pro",
                {"id": "gamma-free"},  # deduplication check
            ],
        )
    )
    res2 = await get_dynamic_free_models(base_url="https://custom2.ai/zen/v1", api_key="key2")
    assert res2 == ["gamma-free", "delta-free"]


@pytest.mark.asyncio
@respx.mock
async def test_get_dynamic_free_models_no_free_models_fallback() -> None:
    respx.get(OPENCODE_ZEN_MODELS_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "gpt-4o"}, {"id": "claude-sonnet"}]},
        )
    )

    result = await get_dynamic_free_models(api_key="test_key_123")
    assert result == BOOTSTRAP_FREE_MODELS


@pytest.mark.asyncio
@respx.mock
async def test_get_dynamic_free_models_http_500_fallback() -> None:
    route = respx.get(OPENCODE_ZEN_MODELS_URL).mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )

    result = await get_dynamic_free_models(api_key="test_key_123")
    assert route.call_count == 1
    assert result == BOOTSTRAP_FREE_MODELS


@pytest.mark.asyncio
@respx.mock
async def test_get_dynamic_free_models_connect_error_fallback() -> None:
    route = respx.get(OPENCODE_ZEN_MODELS_URL).mock(
        side_effect=httpx.ConnectError("Connection refused")
    )

    result = await get_dynamic_free_models(api_key="test_key_123")
    assert route.call_count == 1
    assert result == BOOTSTRAP_FREE_MODELS


@pytest.mark.asyncio
@respx.mock
async def test_get_dynamic_free_models_ttl_cache_avoids_second_call() -> None:
    route = respx.get(OPENCODE_ZEN_MODELS_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "nemotron-3.5-lightning-free"}]},
        )
    )

    res1 = await get_dynamic_free_models(api_key="test_key_123")
    assert route.call_count == 1
    assert res1 == ["nemotron-3.5-lightning-free"]

    res2 = await get_dynamic_free_models(api_key="test_key_123")
    assert route.call_count == 1
    assert res2 == ["nemotron-3.5-lightning-free"]


@pytest.mark.asyncio
@respx.mock
async def test_get_dynamic_free_models_force_refresh_bypasses_cache() -> None:
    route = respx.get(OPENCODE_ZEN_MODELS_URL).mock(
        side_effect=[
            httpx.Response(200, json={"data": [{"id": "model-1-free"}]}),
            httpx.Response(200, json={"data": [{"id": "model-2-free"}]}),
        ]
    )

    res1 = await get_dynamic_free_models(api_key="test_key_123")
    assert route.call_count == 1
    assert res1 == ["model-1-free"]

    res2 = await get_dynamic_free_models(api_key="test_key_123", force_refresh=True)
    assert route.call_count == 2
    assert res2 == ["model-2-free"]
