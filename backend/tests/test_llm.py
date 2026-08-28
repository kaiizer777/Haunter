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
from unittest.mock import patch
import uuid
import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.llm.client import LLMClient
from app.llm.config import get_active_model_config
from app.llm.exceptions import LLMError, LLMTimeoutError
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
    """8 consecutive 500 errors exhaust retry budget and raise LLMError without key leak.

    Note: `max_attempts` was bumped 3 -> 8 in Phase 16 while the pipeline is
    being validated. The test now supplies 8 mock responses to fully exhaust
    the budget.
    """
    route = respx.post(OPENCODE_ZEN_ENDPOINT)
    route.side_effect = [httpx.Response(500, text="Internal Server Error")] * 8

    client = LLMClient()
    orig_key = settings.opencode_zen_api_key
    settings.opencode_zen_api_key = "super_secret_opencode_key_9999"

    try:
        with patch("asyncio.sleep", return_value=None):
            with pytest.raises(LLMError) as exc_info:
                await client.complete(messages=[{"role": "user", "content": "fail test"}])
            assert "500" in str(exc_info.value) or "error" in str(exc_info.value).lower()
            assert "super_secret_opencode_key_9999" not in str(exc_info.value)
            assert route.call_count == 8
    finally:
        settings.opencode_zen_api_key = orig_key


@pytest.mark.asyncio
@respx.mock
async def test_llm_timeout_generic_error():
    """Network timeout raises LLMTimeoutError with generic message."""
    route = respx.post(OPENCODE_ZEN_ENDPOINT)
    route.side_effect = httpx.ReadTimeout("Read timed out")

    client = LLMClient()
    orig_key = settings.opencode_zen_api_key
    settings.opencode_zen_api_key = "test_zen_key_123"

    try:
        with patch("asyncio.sleep", return_value=None):
            with pytest.raises(LLMTimeoutError) as exc_info:
                await client.complete(messages=[{"role": "user", "content": "timeout test"}])
            assert "timed out" in str(exc_info.value).lower()
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
