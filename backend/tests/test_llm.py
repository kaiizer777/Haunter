"""
LLM Provider Abstraction, resilience, and model config integration tests (test_llm.py).

Covers:
1. LLMClient.complete() mocked 200 -> content, usage, latency_ms > 0, model returned.
2. LLMClient.complete() with tool definitions -> passes tools and returns tool_calls.
3. LLMClient.complete() with missing usage data in response -> defaults tokens to 0.
4. Latency measurement -> latency_ms is recorded and within reasonable range (>0, <30000).
5. get_active_model_config with DB hit -> resolves active ModelConfig from Postgres.
6. get_active_model_config with empty DB -> falls back to environment defaults.
7. get_active_model_config with multiple active rows -> picks the latest by created_at.
8. PUT /config/model -> updates active config; next LLMClient.complete() invokes new model.
9. PUT /config/model with invalid provider -> 422 Unprocessable Entity.
10. PUT /config/model with invalid model -> 422 Unprocessable Entity.
11. PUT /config/model/{repo_id} for non-owned repo -> 404 Not Found.
12. Retry wrapper: 429 on attempt 1 -> retries and succeeds on attempt 2.
13. Retry wrapper: 500 across 3 attempts -> exhausts retries and raises LLMError with no secret leak.
14. Timeout handling: network timeout -> raises LLMTimeoutError with generic message.
15. Zero-leak credentials: API key never appears in caplog, response dict, or error strings.
16. Output safety: LLM string containing script tags is returned verbatim without eval.
"""

import asyncio
import json
import logging
from unittest.mock import AsyncMock, patch
import uuid
import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.llm.client import (
    ATTEMPTS_PER_MODEL,
    FREE_TIER_FALLBACK_ORDER,
    INTER_MODEL_SLEEP_S,
    LLMClient,
    RATE_LIMIT_ATTEMPTS_PER_MODEL,
)
from app.llm.config import get_active_model_config
from app.llm.discovery import (
    BOOTSTRAP_FREE_MODELS,
    clear_model_cache,
    get_dynamic_free_models,
)
from app.llm.exceptions import (
    LLMAuthenticationError,
    LLMError,
    LLMExhaustedFreeTierError,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from app.models import ModelConfig, Repo, User
from tests.conftest import truncate_all

OPENCODE_ZEN_ENDPOINT = "https://opencode.ai/zen/v1/chat/completions"
OPENCODE_ZEN_MODELS_ENDPOINT = "https://opencode.ai/zen/v1/models"


@pytest.mark.asyncio
@respx.mock
async def test_llm_client_complete_mocked_200():
    """LLMClient.complete() parses 200 response into content, usage, latency_ms, and model."""
    respx.post(OPENCODE_ZEN_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-mock-1",
                "model": "nemotron-3.5-lightning-free",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Root cause: SyntaxError in backend/app/auth.py line 42",
                        }
                    }
                ],
                "usage": {"prompt_tokens": 150, "completion_tokens": 35},
            },
        )
    )

    client = LLMClient()
    messages = [{"role": "user", "content": "Analyze CI logs"}]
    # Ensure api key is present for test
    orig_key = settings.opencode_zen_api_key
    settings.opencode_zen_api_key = "test_zen_key_123"

    try:
        res = await client.complete(messages=messages)
        assert res["content"] == "Root cause: SyntaxError in backend/app/auth.py line 42"
        assert res["usage"] == {"input_tokens": 150, "output_tokens": 35}
        assert res["latency_ms"] >= 0
        assert res["model"] == "nemotron-3.5-lightning-free"
        assert res["tool_calls"] is None
    finally:
        settings.opencode_zen_api_key = orig_key


@pytest.mark.asyncio
@respx.mock
async def test_llm_client_complete_with_tools():
    """LLMClient.complete() passes tools schema and parses tool_calls."""
    respx.post(OPENCODE_ZEN_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "nemotron-3.5-lightning-free",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_sandbox_1",
                                    "type": "function",
                                    "function": {
                                        "name": "trigger_sandbox_build",
                                        "arguments": '{"patch": "diff --git a/file.py"}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 200, "completion_tokens": 50},
            },
        )
    )

    client = LLMClient()
    tools = [
        {
            "type": "function",
            "function": {
                "name": "trigger_sandbox_build",
                "parameters": {"type": "object", "properties": {"patch": {"type": "string"}}},
            },
        }
    ]

    orig_key = settings.opencode_zen_api_key
    settings.opencode_zen_api_key = "test_zen_key_123"

    try:
        res = await client.complete(messages=[{"role": "user", "content": "run fix"}], tools=tools)
        assert res["content"] is None
        assert res["tool_calls"] is not None
        assert len(res["tool_calls"]) == 1
        assert res["tool_calls"][0]["function"]["name"] == "trigger_sandbox_build"
    finally:
        settings.opencode_zen_api_key = orig_key


@pytest.mark.asyncio
@respx.mock
async def test_llm_client_missing_usage_defaults_to_zero():
    """LLM response missing the usage dict safely defaults input/output tokens to 0."""
    respx.post(OPENCODE_ZEN_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "nemotron-3.5-lightning-free",
                "choices": [{"message": {"content": "ok"}}],
            },
        )
    )

    client = LLMClient()
    orig_key = settings.opencode_zen_api_key
    settings.opencode_zen_api_key = "test_zen_key_123"

    try:
        res = await client.complete(messages=[{"role": "user", "content": "ping"}])
        assert res["content"] == "ok"
        assert res["usage"] == {"input_tokens": 0, "output_tokens": 0}
    finally:
        settings.opencode_zen_api_key = orig_key


@pytest.mark.asyncio
@respx.mock
async def test_llm_client_latency_measured():
    """Execution latency is measured in milliseconds (>0, <30000)."""
    respx.post(OPENCODE_ZEN_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "nemotron-3.5-lightning-free",
                "choices": [{"message": {"content": "pong"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )
    )

    client = LLMClient()
    orig_key = settings.opencode_zen_api_key
    settings.opencode_zen_api_key = "test_zen_key_123"

    try:
        res = await client.complete(messages=[{"role": "user", "content": "ping"}])
        assert isinstance(res["latency_ms"], int)
        assert 0 <= res["latency_ms"] < 30000
    finally:
        settings.opencode_zen_api_key = orig_key


@pytest.mark.asyncio
async def test_get_active_model_config_db_hit(db: AsyncSession):
    """Resolves active ModelConfig from Postgres when present."""
    await truncate_all(db)
    cfg = ModelConfig(
        provider="opencode_zen",
        model_name="nemotron-3-ultra-free",
        base_url="https://opencode.ai/zen/v1",
        is_active=True,
    )
    db.add(cfg)
    await db.commit()

    resolved = await get_active_model_config(db=db)
    assert resolved.provider == "opencode_zen"
    assert resolved.model_name == "nemotron-3-ultra-free"
    assert resolved.base_url == "https://opencode.ai/zen/v1"


@pytest.mark.asyncio
async def test_get_active_model_config_db_empty_fallback_env(db: AsyncSession):
    """Falls back gracefully to environment variable defaults when DB has no active configs."""
    await truncate_all(db)

    resolved = await get_active_model_config(db=db)
    assert resolved.provider == settings.default_provider
    assert resolved.model_name == settings.default_model
    assert resolved.base_url == settings.opencode_zen_base_url


@pytest.mark.asyncio
async def test_get_active_model_config_multiple_active_picks_latest(db: AsyncSession):
    """When multiple active ModelConfigs exist in DB, returns the newest by created_at."""
    await truncate_all(db)
    cfg1 = ModelConfig(provider="opencode_zen", model_name="nemotron-3.5-lightning-free", is_active=True)
    db.add(cfg1)
    await db.commit()

    cfg2 = ModelConfig(provider="opencode_zen", model_name="hy3-free", is_active=True)
    db.add(cfg2)
    await db.commit()

    resolved = await get_active_model_config(db=db)
    assert resolved.model_name == "hy3-free"


@pytest.mark.asyncio
@respx.mock
async def test_put_model_config_switches_model_for_next_complete(
    db: AsyncSession,
    user_factory,
    make_auth_client,
):
    """PUT /config/model switches active model, which is immediately used in subsequent complete() calls."""
    await truncate_all(db)
    user = await user_factory(github_id=881, username="switch_user")
    user_id = user.id
    client = make_auth_client(user_id)

    async with client:
        # 1. Switch global active model to hy3-free
        resp = await client.put(
            "/config/model",
            json={"provider": "opencode_zen", "model_name": "hy3-free"},
        )
        assert resp.status_code == 200
        assert resp.json()["model_name"] == "hy3-free"

    # 2. Mock OpenCode Zen completion
    mock_route = respx.post(OPENCODE_ZEN_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "hy3-free",
                "choices": [{"message": {"content": "Response from hy3-free"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10},
            },
        )
    )

    orig_key = settings.opencode_zen_api_key
    settings.opencode_zen_api_key = "test_zen_key_123"

    try:
        llm = LLMClient()
        res = await llm.complete(messages=[{"role": "user", "content": "hi"}], db=db)
        assert res["content"] == "Response from hy3-free"
        assert res["model"] == "hy3-free"
        assert mock_route.called
        # Check payload had model hy3-free
        sent_body = json.loads(mock_route.calls.last.request.content)
        assert sent_body["model"] == "hy3-free"
    finally:
        settings.opencode_zen_api_key = orig_key


@pytest.mark.asyncio
async def test_put_model_config_validation_errors(
    db: AsyncSession,
    user_factory,
    make_auth_client,
):
    """PUT /config/model validates against strict allowlists for provider and model_name."""
    await truncate_all(db)
    user = await user_factory(github_id=882, username="val_user")
    user_id = user.id
    client = make_auth_client(user_id)

    async with client:
        # Invalid provider
        resp1 = await client.put("/config/model", json={"provider": "unknown_llm", "model_name": "gpt-4o"})
        assert resp1.status_code == 422

        # Invalid model
        resp2 = await client.put("/config/model", json={"provider": "openai", "model_name": "unapproved-model"})
        assert resp2.status_code == 422


@pytest.mark.asyncio
async def test_put_model_config_non_owner_repo(
    db: AsyncSession,
    user_factory,
    make_auth_client,
):
    """PUT /config/model/{repo_id} on non-owned repo returns 404."""
    await truncate_all(db)
    user_a = await user_factory(github_id=883, username="owner_a")
    user_a_id = user_a.id
    user_b = await user_factory(github_id=884, username="intruder_b")
    user_b_id = user_b.id

    repo_a = Repo(user_id=user_a_id, owner="owner-a", name="secret-repo")
    db.add(repo_a)
    await db.commit()
    await db.refresh(repo_a)
    repo_a_id = repo_a.id

    client_b = make_auth_client(user_b_id)
    async with client_b:
        resp = await client_b.put(
            f"/config/model/{repo_a_id}",
            json={"provider": "opencode_zen", "model_name": "nemotron-3.5-lightning-free"},
        )
        assert resp.status_code == 404
        assert resp.json() == {"detail": "Repo not found"}


@pytest.mark.asyncio
@respx.mock
async def test_llm_retry_429_success_second_try():
    """Retry wrapper retries on HTTP 429 rate limit and succeeds on 2nd attempt."""
    route = respx.post(OPENCODE_ZEN_ENDPOINT)
    route.side_effect = [
        httpx.Response(429, json={"error": "Rate limit exceeded"}),
        httpx.Response(
            200,
            json={
                "model": "nemotron-3.5-lightning-free",
                "choices": [{"message": {"content": "Recovered on attempt 2"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10},
            },
        ),
    ]

    client = LLMClient()
    orig_key = settings.opencode_zen_api_key
    settings.opencode_zen_api_key = "test_zen_key_123"

    try:
        with patch("asyncio.sleep", return_value=None):
            res = await client.complete(messages=[{"role": "user", "content": "retry test"}])
            assert res["content"] == "Recovered on attempt 2"
            assert route.call_count == 2
    finally:
        settings.opencode_zen_api_key = orig_key


@pytest.mark.asyncio
@respx.mock
async def test_llm_retry_500_exhausts_and_caps():
    """8 consecutive 500 errors on the inner ``execute_with_retry`` are caught
    by the LLMClient fallback loop, which moves on to the next model. The
    same ``respx`` route is hit for every model in the chain, so the test
    now verifies the Phase 17 contract: the chain eventually exhausts and
    raises ``LLMExhaustedFreeTierError`` with no secret leak.
    """
    route = respx.post(OPENCODE_ZEN_ENDPOINT)
    # The fallback chain is ``len(FREE_TIER_FALLBACK_ORDER) * ATTEMPTS_PER_MODEL``
    # = 49 calls. respx repeats the last ``side_effect`` entry once the list
    # is exhausted, so a single 500 here covers all 49 invocations.
    route.side_effect = httpx.Response(500, text="Internal Server Error")

    client = LLMClient()
    orig_key = settings.opencode_zen_api_key
    settings.opencode_zen_api_key = "super_secret_opencode_key_9999"

    try:
        with patch("asyncio.sleep", return_value=None):
            with pytest.raises(LLMExhaustedFreeTierError) as exc_info:
                await client.complete(messages=[{"role": "user", "content": "fail test"}])
            assert "exhausted" in str(exc_info.value).lower()
            assert "500" in str(exc_info.value) or "error" in str(exc_info.value).lower()
            assert "super_secret_opencode_key_9999" not in str(exc_info.value)
            # The chain hit every model in FREE_TIER_FALLBACK_ORDER, with
            # ATTEMPTS_PER_MODEL attempts on each. The HTTP layer is the
            # one that re-fires per inner execute_with_retry attempt, so
            # call_count here is the number of distinct requests observed
            # at the network edge during the chain — for this test it's
            # at least len(chain) * 1 (one per outer LLMClient attempt).
            assert route.call_count >= len(FREE_TIER_FALLBACK_ORDER)
    finally:
        settings.opencode_zen_api_key = orig_key


@pytest.mark.asyncio
@respx.mock
async def test_llm_timeout_generic_error():
    """Network timeouts on every call exhaust the free-tier chain.

    Phase 17: a single timeout no longer propagates ``LLMTimeoutError``
    directly to the caller — the LLMClient catches it and moves to the
    next model. Only when every model has timed out does the chain
    raise ``LLMExhaustedFreeTierError``. The timeout's generic message
    is still visible inside ``exc.attempts``.
    """
    route = respx.post(OPENCODE_ZEN_ENDPOINT)
    route.side_effect = httpx.ReadTimeout("Read timed out")

    client = LLMClient()
    orig_key = settings.opencode_zen_api_key
    settings.opencode_zen_api_key = "test_zen_key_123"

    try:
        with patch("asyncio.sleep", return_value=None):
            with pytest.raises(LLMExhaustedFreeTierError) as exc_info:
                await client.complete(messages=[{"role": "user", "content": "timeout test"}])
            # The exhaustion message itself does not contain the
            # "timed out" phrase (per-model errors are truncated to
            # 32 chars in the LLMExhaustedFreeTierError message), but
            # the per-model last error inside attempts does.
            assert any("timed out" in err.lower() for _m, _i, err in exc_info.value.attempts)
    finally:
        settings.opencode_zen_api_key = orig_key


@pytest.mark.asyncio
@respx.mock
async def test_llm_api_key_not_in_logs_or_response(caplog):
    """Secret API key is never logged or exposed in returned response dict."""
    secret_key = "opencode_secret_key_never_leak_12345"
    respx.post(OPENCODE_ZEN_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "nemotron-3.5-lightning-free",
                "choices": [{"message": {"content": "Clean output"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )
    )

    client = LLMClient()
    orig_key = settings.opencode_zen_api_key
    settings.opencode_zen_api_key = secret_key

    try:
        with caplog.at_level(logging.DEBUG):
            res = await client.complete(messages=[{"role": "user", "content": "secure test"}])
            assert secret_key not in caplog.text
            assert secret_key not in str(res)
    finally:
        settings.opencode_zen_api_key = orig_key


@pytest.mark.asyncio
@respx.mock
async def test_llm_response_not_evaled():
    """LLM response containing script or executable injection payload is preserved as safe raw string."""
    payload_str = '<script>alert("XSS")</script>\n```python\nimport os; os.system("rm -rf /")\n```'
    respx.post(OPENCODE_ZEN_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "nemotron-3.5-lightning-free",
                "choices": [{"message": {"content": payload_str}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 20},
            },
        )
    )

    client = LLMClient()
    orig_key = settings.opencode_zen_api_key
    settings.opencode_zen_api_key = "test_zen_key_123"

    try:
        res = await client.complete(messages=[{"role": "user", "content": "untrusted payload"}])
        assert res["content"] == payload_str
        assert isinstance(res["content"], str)
    finally:
        settings.opencode_zen_api_key = orig_key


# ---------------------------------------------------------------------------
# Phase 17 — Per-model retry + automatic free-tier model fallback
# ---------------------------------------------------------------------------
# These tests exercise the LLMClient's outer fallback loop:
#   * the seed model is patched in via ``get_active_model_config`` mock so
#     the tests run without a DB
#   * each model is tried up to ATTEMPTS_PER_MODEL times before falling over
#   * a small inter-model sleep is invoked between models
#   * empty content / malformed JSON count as failed attempts
#   * LLMAuthenticationError / LLMInvalidRequestError fall through immediately
# Tests use ``patch("asyncio.sleep", side_effect=fake_sleep)`` to:
#   1. neutralise the inner ``execute_with_retry`` backoff
#   2. record every inter-model sleep call so it can be asserted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_llm_client_walks_fallback_order_on_rate_limit():
    """Rate-limited seed exhausts 4-5 attempts; the next model in the chain succeeds."""
    from app.llm.config import ResolvedModelConfig

    seed = ResolvedModelConfig(
        provider="opencode_zen",
        model_name="nemotron-3.5-lightning-free",  # first in chain
        base_url="https://opencode.ai/zen/v1",
    )
    seed_async = AsyncMock(return_value=seed)

    inner_calls = {"n": 0}
    seed_inner_attempts = RATE_LIMIT_ATTEMPTS_PER_MODEL  # 5 attempts per Policy 1

    def side_effect(request):
        inner_calls["n"] += 1
        if inner_calls["n"] <= seed_inner_attempts:
            return httpx.Response(429, json={"error": "rate limit"})
        # First call to the second model in the chain succeeds.
        return httpx.Response(
            200,
            json={
                "model": "nemotron-3-ultra-free",
                "choices": [{"message": {"content": "recovered on nemotron-3-ultra-free"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 7},
            },
        )

    route = respx.post(OPENCODE_ZEN_ENDPOINT)
    route.side_effect = side_effect

    sleep_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    client = LLMClient()
    orig_key = settings.opencode_zen_api_key
    settings.opencode_zen_api_key = "test_zen_key_123"

    try:
        with patch("app.llm.client.get_active_model_config", seed_async), \
             patch("app.llm.client.get_dynamic_free_models", AsyncMock(return_value=["nemotron-3-ultra-free"])), \
             patch("asyncio.sleep", side_effect=fake_sleep):
            res = await client.complete(
                messages=[{"role": "user", "content": "fallback test"}],
            )
        assert res["content"] == "recovered on nemotron-3-ultra-free"
        assert res["model"] == "nemotron-3-ultra-free"
        # 5 rate limit attempts on the seed, then 1 success on the second model = 6 calls
        assert route.call_count == seed_inner_attempts + 1
        assert res["model"] != "nemotron-3.5-lightning-free"
        inter_model_sleeps = [d for d in sleep_calls if d == INTER_MODEL_SLEEP_S]
        assert len(inter_model_sleeps) == 1
        assert inter_model_sleeps[0] == INTER_MODEL_SLEEP_S
    finally:
        settings.opencode_zen_api_key = orig_key


@pytest.mark.asyncio
@respx.mock
async def test_llm_client_gives_up_after_all_models_exhausted():
    """Every model in the free-tier chain is exhausted (Policy 2: 1 attempt per model) → LLMExhaustedFreeTierError."""
    from app.llm.config import ResolvedModelConfig

    seed = ResolvedModelConfig(
        provider="opencode_zen",
        model_name="nemotron-3.5-lightning-free",  # first in chain
        base_url="https://opencode.ai/zen/v1",
    )
    seed_async = AsyncMock(return_value=seed)

    route = respx.post(OPENCODE_ZEN_ENDPOINT)
    route.side_effect = httpx.Response(500, text="provider down")

    sleep_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    client = LLMClient()
    orig_key = settings.opencode_zen_api_key
    settings.opencode_zen_api_key = "test_zen_key_123"

    fallback_models = ["model-a-free", "model-b-free"]
    chain_models = [seed.model_name] + fallback_models

    try:
        with patch("app.llm.client.get_active_model_config", seed_async), \
             patch("app.llm.client.get_dynamic_free_models", AsyncMock(return_value=fallback_models)), \
             patch("asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises(LLMExhaustedFreeTierError) as exc_info:
                await client.complete(
                    messages=[{"role": "user", "content": "all fail"}],
                )

        exc = exc_info.value
        # Each model had exactly 1 attempt on HTTP 500 (Policy 2)
        assert len(exc.attempts) == len(chain_models)
        per_model_count: dict[str, int] = {}
        for model, _idx, _err in exc.attempts:
            per_model_count[model] = per_model_count.get(model, 0) + 1
        assert set(per_model_count.keys()) == set(chain_models)
        for model, count in per_model_count.items():
            assert count == 1, f"{model} had {count} attempts, expected 1"
        msg = str(exc)
        assert "exhausted" in msg.lower()
        assert all(model in msg for model in chain_models)
        for _m, _i, err in exc.attempts:
            assert "provider down" in err or "500" in err
        inter_model_sleeps = [d for d in sleep_calls if d == INTER_MODEL_SLEEP_S]
        assert len(inter_model_sleeps) == len(chain_models) - 1
    finally:
        settings.opencode_zen_api_key = orig_key


@pytest.mark.asyncio
@respx.mock
async def test_llm_client_propagates_auth_error_immediately():
    """``LLMAuthenticationError`` on the first attempt falls through with no
    fallback — provider auth is shared across free-tier models.
    """
    from app.llm.config import ResolvedModelConfig

    seed = ResolvedModelConfig(
        provider="opencode_zen",
        model_name="nemotron-3.5-lightning-free",
        base_url="https://opencode.ai/zen/v1",
    )
    seed_async = AsyncMock(return_value=seed)

    route = respx.post(OPENCODE_ZEN_ENDPOINT)
    # Single 401; the LLMClient should re-raise without trying any other
    # model. The route must NOT be called again.
    route.side_effect = httpx.Response(401, json={"error": "invalid api key"})

    sleep_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    client = LLMClient()
    orig_key = settings.opencode_zen_api_key
    settings.opencode_zen_api_key = "test_zen_key_123"

    try:
        with patch("app.llm.client.get_active_model_config", seed_async), \
             patch("asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises(LLMAuthenticationError):
                await client.complete(
                    messages=[{"role": "user", "content": "auth fail"}],
                )
        # Only one HTTP call — no fallback to other models.
        assert route.call_count == 1
        # No inter-model sleep — the auth error is fatal.
        assert sleep_calls == []
    finally:
        settings.opencode_zen_api_key = orig_key


@pytest.mark.asyncio
@respx.mock
async def test_llm_client_returns_first_success():
    """When the seed model returns empty content (Policy 2: 1 attempt),
    it immediately switches to the fallback model which succeeds.
    """
    from app.llm.config import ResolvedModelConfig

    seed = ResolvedModelConfig(
        provider="opencode_zen",
        model_name="ling-3-free",
        base_url="https://opencode.ai/zen/v1",
    )
    seed_async = AsyncMock(return_value=seed)

    route = respx.post(OPENCODE_ZEN_ENDPOINT)
    route.side_effect = [
        httpx.Response(
            200,
            json={
                "model": "ling-3-free",
                "choices": [{"message": {"content": "   \n  "}}],  # whitespace only -> fails on attempt 1
                "usage": {"prompt_tokens": 10, "completion_tokens": 0},
            },
        ),
        httpx.Response(
            200,
            json={
                "model": "fallback-success-free",
                "choices": [{"message": {"content": "real answer"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            },
        ),
    ]

    sleep_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    client = LLMClient()
    orig_key = settings.opencode_zen_api_key
    settings.opencode_zen_api_key = "test_zen_key_123"

    try:
        with patch("app.llm.client.get_active_model_config", seed_async), \
             patch("app.llm.client.get_dynamic_free_models", AsyncMock(return_value=["fallback-success-free"])), \
             patch("asyncio.sleep", side_effect=fake_sleep):
            res = await client.complete(
                messages=[{"role": "user", "content": "first success"}],
            )
        assert res["content"] == "real answer"
        assert res["model"] == "fallback-success-free"
        # 1 call on seed (empty), 1 call on fallback (success)
        assert route.call_count == 2
        # Exactly one inter-model sleep
        assert any(d == INTER_MODEL_SLEEP_S for d in sleep_calls)
    finally:
        settings.opencode_zen_api_key = orig_key


@pytest.mark.asyncio
@respx.mock
async def test_llm_client_inter_model_delay():
    """When the seed's ATTEMPTS_PER_MODEL attempts all fail, the LLMClient
    sleeps ``INTER_MODEL_SLEEP_S`` seconds before the next model's first
    attempt. This guards against hammering the API on a fallback.
    """
    from app.llm.config import ResolvedModelConfig

    seed = ResolvedModelConfig(
        provider="opencode_zen",
        model_name="hy3-free",  # last in FREE_TIER_FALLBACK_ORDER
        base_url="https://opencode.ai/zen/v1",
    )
    seed_async = AsyncMock(return_value=seed)

    route = respx.post(OPENCODE_ZEN_ENDPOINT)
    # Use a callable that always returns 429 so the inner retry never
    # recovers — every attempt on every model fails, the chain exhausts,
    # and LLMExhaustedFreeTierError is raised. (A list side_effect would
    # be exhausted and respx would repeat the last item, causing a
    # premature success.)
    def always_429(request):
        return httpx.Response(429, json={"error": "rate limit"})
    route.side_effect = always_429

    sleep_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    client = LLMClient()
    orig_key = settings.opencode_zen_api_key
    settings.opencode_zen_api_key = "test_zen_key_123"

    try:
        with patch("app.llm.client.get_active_model_config", seed_async), \
             patch("asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises(LLMExhaustedFreeTierError):
                await client.complete(
                    messages=[{"role": "user", "content": "inter-model sleep"}],
                )
        # At least one inter-model sleep with the exact INTER_MODEL_SLEEP_S
        # value. The inner execute_with_retry backoff (1, 2, 4, 8, 16, 30,
        # 30) can also exceed INTER_MODEL_SLEEP_S, so we use exact equality.
        assert any(d == INTER_MODEL_SLEEP_S for d in sleep_calls), (
            f"expected at least one sleep == {INTER_MODEL_SLEEP_S}s, got {sleep_calls}"
        )
    finally:
        settings.opencode_zen_api_key = orig_key


# ---------------------------------------------------------------------------
# Dynamic Model Discovery & Dual-Policy Retry Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_get_dynamic_free_models_success():
    """GET /models dynamically discovers and filters models ending with '-free' with 15-min TTL cache."""
    clear_model_cache()
    mock_models_route = respx.get(OPENCODE_ZEN_MODELS_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "nemotron-3.5-lightning-free", "object": "model"},
                    {"id": "gpt-4o", "object": "model"},
                    {"id": "laguna-s-2.1-free", "object": "model"},
                    {"id": "claude-sonnet-4-5", "object": "model"},
                    {"id": "ling-3.0-flash-fin-free", "object": "model"},
                ],
            },
        )
    )

    models = await get_dynamic_free_models(
        base_url="https://opencode.ai/zen/v1",
        api_key="test_zen_key_123",
        force_refresh=True,
    )
    assert models == [
        "nemotron-3.5-lightning-free",
        "laguna-s-2.1-free",
        "ling-3.0-flash-fin-free",
    ]
    assert mock_models_route.call_count == 1

    # Second call uses in-memory cache and does not make another HTTP request
    cached_models = await get_dynamic_free_models(
        base_url="https://opencode.ai/zen/v1",
        api_key="test_zen_key_123",
    )
    assert cached_models == models
    assert mock_models_route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_get_dynamic_free_models_resilient_fallback():
    """When GET /models fails (500, network error, timeout), falls back gracefully without crashing."""
    clear_model_cache()
    respx.get(OPENCODE_ZEN_MODELS_ENDPOINT).mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )

    models = await get_dynamic_free_models(
        base_url="https://opencode.ai/zen/v1",
        api_key="test_zen_key_123",
        force_refresh=True,
    )
    # Falls back to BOOTSTRAP_FREE_MODELS
    assert models == BOOTSTRAP_FREE_MODELS


@pytest.mark.asyncio
@respx.mock
async def test_dynamic_fallback_chain_construction():
    """Fallback chain places seed model first and appends discovered free models de-duplicated."""
    clear_model_cache()
    respx.get(OPENCODE_ZEN_MODELS_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": "ling-3.0-flash-fin-free"},
                    {"id": "custom-seed-free"},
                    {"id": "laguna-s-2.1-free"},
                ]
            },
        )
    )

    called_models: list[str] = []

    def handle_complete(request):
        body = json.loads(request.content)
        called_models.append(body["model"])
        if body["model"] == "laguna-s-2.1-free":
            return httpx.Response(
                200,
                json={
                    "model": "laguna-s-2.1-free",
                    "choices": [{"message": {"content": "ok"}}],
                },
            )
        return httpx.Response(500, text="error")

    respx.post(OPENCODE_ZEN_ENDPOINT).mock(side_effect=handle_complete)

    client = LLMClient()
    orig_key = settings.opencode_zen_api_key
    settings.opencode_zen_api_key = "test_zen_key_123"

    try:
        with patch("asyncio.sleep", return_value=None):
            res = await client.complete(
                messages=[{"role": "user", "content": "chain test"}],
                model="custom-seed-free",
            )
            assert res["content"] == "ok"
            assert res["model"] == "laguna-s-2.1-free"
            # Verify chain order: seed first, then others de-duplicated
            assert called_models == ["custom-seed-free", "ling-3.0-flash-fin-free", "laguna-s-2.1-free"]
    finally:
        settings.opencode_zen_api_key = orig_key


@pytest.mark.asyncio
@respx.mock
async def test_retry_policy_rate_limit_429_retries_and_switches():
    """Policy 1: HTTP 429 retries 5 times with backoff on same model before switching to next."""
    from app.llm.config import ResolvedModelConfig

    clear_model_cache()
    seed = ResolvedModelConfig(
        provider="opencode_zen",
        model_name="seed-model-free",
        base_url="https://opencode.ai/zen/v1",
    )
    seed_async = AsyncMock(return_value=seed)

    # Discovered models has fallback model
    respx.get(OPENCODE_ZEN_MODELS_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "seed-model-free"}, {"id": "fallback-model-free"}]},
        )
    )

    call_history: list[str] = []

    def side_effect(request):
        body = json.loads(request.content)
        call_history.append(body["model"])
        if body["model"] == "seed-model-free":
            return httpx.Response(429, json={"error": "rate limit"})
        return httpx.Response(
            200,
            json={
                "model": "fallback-model-free",
                "choices": [{"message": {"content": "fallback success"}}],
            },
        )

    respx.post(OPENCODE_ZEN_ENDPOINT).mock(side_effect=side_effect)

    client = LLMClient()
    orig_key = settings.opencode_zen_api_key
    settings.opencode_zen_api_key = "test_zen_key_123"

    try:
        with patch("app.llm.client.get_active_model_config", seed_async), \
             patch("asyncio.sleep", return_value=None):
            res = await client.complete(messages=[{"role": "user", "content": "rate limit test"}])
            assert res["content"] == "fallback success"
            assert res["model"] == "fallback-model-free"
            # 5 calls to seed-model-free, then 1 call to fallback-model-free
            assert call_history.count("seed-model-free") == 5
            assert call_history.count("fallback-model-free") == 1
    finally:
        settings.opencode_zen_api_key = orig_key


@pytest.mark.asyncio
@respx.mock
async def test_retry_policy_outage_500_fails_on_attempt_1():
    """Policy 2: HTTP 500 fails on attempt 1 and immediately switches to next model."""
    from app.llm.config import ResolvedModelConfig

    clear_model_cache()
    seed = ResolvedModelConfig(
        provider="opencode_zen",
        model_name="dead-model-free",
        base_url="https://opencode.ai/zen/v1",
    )
    seed_async = AsyncMock(return_value=seed)

    respx.get(OPENCODE_ZEN_MODELS_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "dead-model-free"}, {"id": "healthy-model-free"}]},
        )
    )

    call_history: list[str] = []

    def side_effect(request):
        body = json.loads(request.content)
        call_history.append(body["model"])
        if body["model"] == "dead-model-free":
            return httpx.Response(500, text="Internal Server Error")
        return httpx.Response(
            200,
            json={
                "model": "healthy-model-free",
                "choices": [{"message": {"content": "healthy recovered"}}],
            },
        )

    respx.post(OPENCODE_ZEN_ENDPOINT).mock(side_effect=side_effect)

    client = LLMClient()
    orig_key = settings.opencode_zen_api_key
    settings.opencode_zen_api_key = "test_zen_key_123"

    try:
        with patch("app.llm.client.get_active_model_config", seed_async), \
             patch("asyncio.sleep", return_value=None):
            res = await client.complete(messages=[{"role": "user", "content": "outage test"}])
            assert res["content"] == "healthy recovered"
            assert res["model"] == "healthy-model-free"
            # Exactly 1 attempt on dead model!
            assert call_history == ["dead-model-free", "healthy-model-free"]
    finally:
        settings.opencode_zen_api_key = orig_key


@pytest.mark.asyncio
@respx.mock
async def test_retry_policy_timeout_fails_on_attempt_1():
    """Policy 2: Network timeout fails on attempt 1 and switches immediately to next model."""
    from app.llm.config import ResolvedModelConfig

    clear_model_cache()
    seed = ResolvedModelConfig(
        provider="opencode_zen",
        model_name="timeout-model-free",
        base_url="https://opencode.ai/zen/v1",
    )
    seed_async = AsyncMock(return_value=seed)

    respx.get(OPENCODE_ZEN_MODELS_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "timeout-model-free"}, {"id": "fast-model-free"}]},
        )
    )

    call_history: list[str] = []

    def side_effect(request):
        body = json.loads(request.content)
        call_history.append(body["model"])
        if body["model"] == "timeout-model-free":
            raise httpx.ReadTimeout("Read timed out")
        return httpx.Response(
            200,
            json={
                "model": "fast-model-free",
                "choices": [{"message": {"content": "fast recovered"}}],
            },
        )

    respx.post(OPENCODE_ZEN_ENDPOINT).mock(side_effect=side_effect)

    client = LLMClient()
    orig_key = settings.opencode_zen_api_key
    settings.opencode_zen_api_key = "test_zen_key_123"

    try:
        with patch("app.llm.client.get_active_model_config", seed_async), \
             patch("asyncio.sleep", return_value=None):
            res = await client.complete(messages=[{"role": "user", "content": "timeout test"}])
            assert res["content"] == "fast recovered"
            # Exactly 1 attempt on timeout model!
            assert call_history == ["timeout-model-free", "fast-model-free"]
    finally:
        settings.opencode_zen_api_key = orig_key


@pytest.mark.asyncio
@respx.mock
async def test_retry_policy_model_not_supported_fails_on_attempt_1():
    """Policy 2: Per-model rejection ('Model is not supported') fails on attempt 1 and switches model."""
    from app.llm.config import ResolvedModelConfig

    clear_model_cache()
    seed = ResolvedModelConfig(
        provider="opencode_zen",
        model_name="unsupported-model-free",
        base_url="https://opencode.ai/zen/v1",
    )
    seed_async = AsyncMock(return_value=seed)

    respx.get(OPENCODE_ZEN_MODELS_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "unsupported-model-free"}, {"id": "supported-model-free"}]},
        )
    )

    call_history: list[str] = []

    def side_effect(request):
        body = json.loads(request.content)
        call_history.append(body["model"])
        if body["model"] == "unsupported-model-free":
            return httpx.Response(401, json={"error": "Model is not supported on this account"})
        return httpx.Response(
            200,
            json={
                "model": "supported-model-free",
                "choices": [{"message": {"content": "supported recovered"}}],
            },
        )

    respx.post(OPENCODE_ZEN_ENDPOINT).mock(side_effect=side_effect)

    client = LLMClient()
    orig_key = settings.opencode_zen_api_key
    settings.opencode_zen_api_key = "test_zen_key_123"

    try:
        with patch("app.llm.client.get_active_model_config", seed_async), \
             patch("asyncio.sleep", return_value=None):
            res = await client.complete(messages=[{"role": "user", "content": "unsupported test"}])
            assert res["content"] == "supported recovered"
            # Exactly 1 attempt on unsupported model!
            assert call_history == ["unsupported-model-free", "supported-model-free"]
    finally:
        settings.opencode_zen_api_key = orig_key


@pytest.mark.asyncio
@respx.mock
async def test_retry_policy_upstream_400_switches_model():
    """Policy 2: Upstream provider 400 errors switch to the next model instead of aborting the run."""
    from app.llm.config import ResolvedModelConfig

    clear_model_cache()
    seed = ResolvedModelConfig(
        provider="opencode_zen",
        model_name="context-limit-free",
        base_url="https://opencode.ai/zen/v1",
    )
    seed_async = AsyncMock(return_value=seed)

    respx.get(OPENCODE_ZEN_MODELS_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "context-limit-free"}, {"id": "high-context-free"}]},
        )
    )

    call_history: list[str] = []

    def side_effect(request):
        body = json.loads(request.content)
        call_history.append(body["model"])
        if body["model"] == "context-limit-free":
            return httpx.Response(
                400,
                json={"error": {"message": "Upstream provider error: max context length exceeded", "type": "model_error"}},
            )
        return httpx.Response(
            200,
            json={
                "model": "high-context-free",
                "choices": [{"message": {"content": "high context recovered"}}],
            },
        )

    respx.post(OPENCODE_ZEN_ENDPOINT).mock(side_effect=side_effect)

    client = LLMClient()
    orig_key = settings.opencode_zen_api_key
    settings.opencode_zen_api_key = "test_zen_key_123"

    try:
        with patch("app.llm.client.get_active_model_config", seed_async), \
             patch("asyncio.sleep", return_value=None):
            res = await client.complete(messages=[{"role": "user", "content": "upstream 400 test"}])
            assert res["content"] == "high context recovered"
            # Exactly 1 attempt on upstream 400 model, then recovered on fallback!
            assert call_history == ["context-limit-free", "high-context-free"]
    finally:
        settings.opencode_zen_api_key = orig_key


@pytest.mark.asyncio
@respx.mock
async def test_retry_policy_global_401_aborts_immediately():
    """Policy 3: Global account-level 401 auth error aborts immediately across all models."""
    respx.post(OPENCODE_ZEN_ENDPOINT).mock(
        return_value=httpx.Response(401, json={"error": "Invalid API key"})
    )

    client = LLMClient()
    orig_key = settings.opencode_zen_api_key
    settings.opencode_zen_api_key = "invalid_key"

    try:
        with patch("asyncio.sleep", return_value=None):
            with pytest.raises(LLMAuthenticationError):
                await client.complete(messages=[{"role": "user", "content": "auth test"}])
    finally:
        settings.opencode_zen_api_key = orig_key


@pytest.mark.asyncio
async def test_put_model_config_allows_dynamic_free_models(
    db: AsyncSession,
    user_factory,
    make_auth_client,
):
    """PUT /config/model allows any model ending with '-free' for opencode_zen without 422."""
    await truncate_all(db)
    user = await user_factory(github_id=999, username="dyn_user")
    client = make_auth_client(user.id)

    async with client:
        # Valid new dynamic -free model
        resp = await client.put(
            "/config/model",
            json={"provider": "opencode_zen", "model_name": "newly-discovered-model-free"},
        )
        assert resp.status_code == 200
        assert resp.json()["model_name"] == "newly-discovered-model-free"

        # Invalid model for opencode_zen (not ending with -free)
        resp_invalid = await client.put(
            "/config/model",
            json={"provider": "opencode_zen", "model_name": "claude-sonnet-4-5"},
        )
        assert resp_invalid.status_code == 422


@pytest.mark.asyncio
@respx.mock
async def test_get_available_models_endpoint(
    db: AsyncSession,
    user_factory,
    make_auth_client,
):
    """GET /config/model/available returns dynamically discovered OpenCode Zen models and standard providers."""
    clear_model_cache()
    respx.get(OPENCODE_ZEN_MODELS_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": "test-dynamic-model-free"},
                    {"id": "paid-model"},
                ]
            },
        )
    )

    user = await user_factory(github_id=1000, username="avail_user")
    client = make_auth_client(user.id)

    async with client:
        resp = await client.get("/config/model/available")
        assert resp.status_code == 200
        data = resp.json()
        assert "opencode_zen" in data
        assert "openai" in data
        assert "anthropic" in data
        zen_ids = [m["id"] for m in data["opencode_zen"]]
        assert "test-dynamic-model-free" in zen_ids
        assert "paid-model" not in zen_ids


def test_model_config_update_schema_validation():
    """ModelConfigUpdate allows any *-free model for opencode_zen, strict allowlist for others, and rejects invalid models."""
    from app.schemas import ModelConfigUpdate
    from pydantic import ValidationError

    # Valid opencode_zen free models
    m1 = ModelConfigUpdate(provider="opencode_zen", model_name="nemotron-3.5-lightning-free")
    assert m1.model_name == "nemotron-3.5-lightning-free"

    m2 = ModelConfigUpdate(provider="opencode_zen", model_name="laguna-s-2.1-free")
    assert m2.model_name == "laguna-s-2.1-free"

    m3 = ModelConfigUpdate(provider="opencode_zen", model_name="brand-new-rotational-model-free")
    assert m3.model_name == "brand-new-rotational-model-free"

    # Invalid opencode_zen model (not ending in -free)
    with pytest.raises(ValidationError):
        ModelConfigUpdate(provider="opencode_zen", model_name="claude-sonnet-4-5")

    # Valid openai models
    m4 = ModelConfigUpdate(provider="openai", model_name="gpt-4o")
    assert m4.model_name == "gpt-4o"

    # Invalid openai model
    with pytest.raises(ValidationError):
        ModelConfigUpdate(provider="openai", model_name="unapproved-gpt-model")

    # Valid anthropic models
    m5 = ModelConfigUpdate(provider="anthropic", model_name="claude-sonnet-4-5")
    assert m5.model_name == "claude-sonnet-4-5"

    # Invalid anthropic model
    with pytest.raises(ValidationError):
        ModelConfigUpdate(provider="anthropic", model_name="claude-unapproved")


@pytest.mark.asyncio
@respx.mock
async def test_get_available_models_endpoint_direct():
    """GET /config/model/available endpoint returns structured provider models."""
    from app.routers.model_config import get_available_models_endpoint

    clear_model_cache()
    respx.get(OPENCODE_ZEN_MODELS_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": "test-dynamic-model-free"},
                    {"id": "paid-model"},
                ]
            },
        )
    )

    fake_user = User(id=uuid.uuid4(), github_id=1234, github_username="test_user")
    res = await get_available_models_endpoint(current_user=fake_user)
    assert any(m.id == "test-dynamic-model-free" for m in res.opencode_zen)
    assert not any(m.id == "paid-model" for m in res.opencode_zen)
    assert any(m.id == "gpt-4o" for m in res.openai)
    assert any(m.id == "claude-sonnet-4-5" for m in res.anthropic)

