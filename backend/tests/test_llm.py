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
from app.llm.client import LLMClient
from app.llm.client import (
    ATTEMPTS_PER_MODEL,
    FREE_TIER_FALLBACK_ORDER,
    INTER_MODEL_SLEEP_S,
    LLMClient,
)
from app.llm.config import get_active_model_config
from app.llm.exceptions import (
    LLMAuthenticationError,
    LLMError,
    LLMExhaustedFreeTierError,
    LLMTimeoutError,
)
from app.models import ModelConfig, Repo, User
from tests.conftest import truncate_all

OPENCODE_ZEN_ENDPOINT = "https://opencode.ai/zen/v1/chat/completions"


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
    """Rate-limited seed exhausts; the next model in the chain succeeds.

    The seed is ``nemotron-3.5-lightning-free`` (first in
    ``FREE_TIER_FALLBACK_ORDER``). The chain becomes
    ``[seed, nemotron-3-ultra-free, ling-3-free, ...]``. We return
    429 for the first 16 inner attempts (covering the seed's 7 LLMClient
    attempts × 8 inner attempts) and 200 thereafter. The LLMClient
    falls over to ``nemotron-3-ultra-free`` which succeeds on its first
    inner attempt. Asserts the response model is the second model in the
    chain (not the seed) and exactly one inter-model sleep fired.
    """
    from app.llm.config import ResolvedModelConfig

    seed = ResolvedModelConfig(
        provider="opencode_zen",
        model_name="nemotron-3.5-lightning-free",  # first in chain
        base_url="https://opencode.ai/zen/v1",
    )
    seed_async = AsyncMock(return_value=seed)

    inner_calls = {"n": 0}
    seed_inner_attempts = ATTEMPTS_PER_MODEL * 8  # 7 LLMClient × 8 inner

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
             patch("asyncio.sleep", side_effect=fake_sleep):
            res = await client.complete(
                messages=[{"role": "user", "content": "fallback test"}],
            )
        assert res["content"] == "recovered on nemotron-3-ultra-free"
        assert res["model"] == "nemotron-3-ultra-free"
        # 56 inner calls on the seed (7 LLMClient × 8 inner), then 1 success on
        # the second model. Total 57 calls.
        assert route.call_count == seed_inner_attempts + 1
        # The response model is NOT the seed — the fallback fired.
        assert res["model"] != "nemotron-3.5-lightning-free"
        # Exactly one inter-model sleep should have fired (between
        # nemotron-3.5-lightning-free and nemotron-3-ultra-free). Use
        # exact equality because inner execute_with_retry backoff sleeps
        # (1, 2, 4, 8, 16, 30, 30) can also exceed INTER_MODEL_SLEEP_S.
        inter_model_sleeps = [d for d in sleep_calls if d == INTER_MODEL_SLEEP_S]
        assert len(inter_model_sleeps) == 1
        assert inter_model_sleeps[0] == INTER_MODEL_SLEEP_S
    finally:
        settings.opencode_zen_api_key = orig_key


@pytest.mark.asyncio
@respx.mock
async def test_llm_client_gives_up_after_all_models_exhausted():
    """Every model in the free-tier chain is exhausted → ``LLMExhaustedFreeTierError``.

    The exception's ``attempts`` log must contain every (model, attempt,
    error) tuple — ``len(attempts) == 49`` and each of the 7 free-tier
    models appears with ``ATTEMPTS_PER_MODEL`` entries.
    """
    from app.llm.config import ResolvedModelConfig

    seed = ResolvedModelConfig(
        provider="opencode_zen",
        model_name="nemotron-3.5-lightning-free",  # first in chain
        base_url="https://opencode.ai/zen/v1",
    )
    seed_async = AsyncMock(return_value=seed)

    route = respx.post(OPENCODE_ZEN_ENDPOINT)
    # respx repeats the last side_effect entry once the list is exhausted.
    # Use a single 500 to cover all 49 invocations.
    route.side_effect = httpx.Response(500, text="provider down")

    sleep_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    client = LLMClient()
    orig_key = settings.opencode_zen_api_key
    settings.opencode_zen_api_key = "test_zen_key_123"

    try:
        with patch("app.llm.client.get_active_model_config", seed_async), \
             patch("asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises(LLMExhaustedFreeTierError) as exc_info:
                await client.complete(
                    messages=[{"role": "user", "content": "all fail"}],
                )

        exc = exc_info.value
        # Total attempts = 7 models x 7 attempts = 49.
        assert len(exc.attempts) == len(FREE_TIER_FALLBACK_ORDER) * ATTEMPTS_PER_MODEL
        # Every model name appears exactly ATTEMPTS_PER_MODEL times.
        per_model_count: dict[str, int] = {}
        for model, _idx, _err in exc.attempts:
            per_model_count[model] = per_model_count.get(model, 0) + 1
        assert set(per_model_count.keys()) == set(FREE_TIER_FALLBACK_ORDER)
        for model, count in per_model_count.items():
            assert count == ATTEMPTS_PER_MODEL, f"{model} had {count} attempts, expected {ATTEMPTS_PER_MODEL}"
        # The exception message lists every model with a per-model last error
        # — verify the message body is compact (under 500 chars after the
        # ``context_gatherer: LLMExhaustedFreeTierError: `` orchestrator prefix).
        msg = str(exc)
        assert "exhausted" in msg.lower()
        assert all(model in msg for model in FREE_TIER_FALLBACK_ORDER)
        # The full per-model error text lives in exc.attempts.
        for _m, _i, err in exc.attempts:
            assert "provider down" in err or "500" in err
        # Inter-model sleeps fired once between every pair of models —
        # 6 sleeps for 7 models. Use exact equality because inner
        # execute_with_retry backoff sleeps (1, 2, 4, 8, 16, 30, 30) can
        # also exceed INTER_MODEL_SLEEP_S.
        inter_model_sleeps = [d for d in sleep_calls if d == INTER_MODEL_SLEEP_S]
        assert len(inter_model_sleeps) == len(FREE_TIER_FALLBACK_ORDER) - 1
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
    """When the seed model succeeds on attempt 3 (after 2 empties), no other
    model is tried and the returned response carries the seed's model name.
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
                "choices": [{"message": {"content": "   \n  "}}],  # whitespace only
                "usage": {"prompt_tokens": 10, "completion_tokens": 0},
            },
        ),
        httpx.Response(
            200,
            json={
                "model": "ling-3-free",
                "choices": [{"message": {"content": None}}],  # None
                "usage": {"prompt_tokens": 10, "completion_tokens": 0},
            },
        ),
        httpx.Response(
            200,
            json={
                "model": "ling-3-free",
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
             patch("asyncio.sleep", side_effect=fake_sleep):
            res = await client.complete(
                messages=[{"role": "user", "content": "first success"}],
            )
        assert res["content"] == "real answer"
        assert res["model"] == "ling-3-free"
        # 3 calls — all to ling-3-free. No fallback.
        assert route.call_count == 3
        # No inter-model sleep — ling-3-free succeeded.
        assert sleep_calls == []
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
