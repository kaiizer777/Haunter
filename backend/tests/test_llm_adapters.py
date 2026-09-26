"""
Respx-based unit tests for OpenAIAdapter and AnthropicAdapter
(backend/app/llm/openai.py, backend/app/llm/anthropic.py).

Mirrors the GroqProvider tests in backend/tests/test_llm.py:
- adapter success posts to the expected endpoint with the expected auth
  headers, sends the expected model, and parses content/usage/latency.
- missing API key raises LLMAuthenticationError before any HTTP call.
- Anthropic native tool_use blocks are normalised back to OpenAI-style
  tool_calls for the shared downstream contract.

Mock only — no live network calls (all HTTP via respx).
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.config import settings
from app.llm.anthropic import ANTHROPIC_VERSION, AnthropicAdapter
from app.llm.exceptions import LLMAuthenticationError
from app.llm.openai import OpenAIAdapter


@pytest.mark.asyncio
@respx.mock
async def test_openai_adapter_success():
    """OpenAIAdapter posts to {base_url}/chat/completions with Bearer auth and parses response."""
    openai_endpoint = f"{settings.openai_base_url.rstrip('/')}/chat/completions"
    assert openai_endpoint.endswith("/v1/chat/completions")

    orig_key = settings.openai_api_key
    settings.openai_api_key = "sk-test-openai-key-123"

    route = respx.post(openai_endpoint).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-openai-mock-1",
                "model": "gpt-4o",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Fix generated via OpenAI",
                        }
                    }
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 45},
            },
        )
    )

    try:
        provider = OpenAIAdapter()
        res = await provider.complete(messages=[{"role": "user", "content": "hello openai"}])
        assert res["content"] == "Fix generated via OpenAI"
        assert res["usage"] == {"input_tokens": 120, "output_tokens": 45}
        assert res["latency_ms"] >= 0
        assert res["model"] == "gpt-4o"
        assert res["tool_calls"] is None
        assert route.called
        headers = route.calls.last.request.headers
        assert headers["Authorization"] == "Bearer sk-test-openai-key-123"
        sent_body = json.loads(route.calls.last.request.content)
        assert sent_body["model"] == "gpt-4o"
        assert str(route.calls.last.request.url).endswith("/v1/chat/completions")
    finally:
        settings.openai_api_key = orig_key


@pytest.mark.asyncio
async def test_openai_adapter_missing_api_key_raises_auth_error():
    """OpenAIAdapter raises LLMAuthenticationError when no API key is configured."""
    orig_key = settings.openai_api_key
    settings.openai_api_key = None
    try:
        provider = OpenAIAdapter()
        with pytest.raises(LLMAuthenticationError) as exc_info:
            await provider.complete(messages=[{"role": "user", "content": "hi"}])
        assert "OPENAI_API_KEY is not configured" in str(exc_info.value)
    finally:
        settings.openai_api_key = orig_key


@pytest.mark.asyncio
@respx.mock
async def test_anthropic_adapter_tool_use_normalization():
    """AnthropicAdapter posts to {base_url}/messages with x-api-key headers and normalises tool_use -> tool_calls."""
    anthropic_endpoint = f"{settings.anthropic_base_url.rstrip('/')}/messages"
    assert anthropic_endpoint.endswith("/v1/messages")

    orig_key = settings.anthropic_api_key
    settings.anthropic_api_key = "sk-ant-test-key-123"

    route = respx.post(anthropic_endpoint).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "msg_anthropic_mock_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-5",
                "content": [
                    {"type": "text", "text": "Calling sandbox build"},
                    {
                        "type": "tool_use",
                        "id": "toolu_sandbox_1",
                        "name": "trigger_sandbox_build",
                        "input": {"patch": "diff --git a/file.py"},
                    },
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 200, "output_tokens": 50},
            },
        )
    )

    tools = [
        {
            "type": "function",
            "function": {
                "name": "trigger_sandbox_build",
                "description": "Trigger a sandbox build",
                "parameters": {"type": "object", "properties": {"patch": {"type": "string"}}},
            },
        }
    ]

    try:
        provider = AnthropicAdapter()
        res = await provider.complete(
            messages=[{"role": "user", "content": "run fix"}],
            tools=tools,
        )
        assert res["content"] == "Calling sandbox build"
        assert res["usage"] == {"input_tokens": 200, "output_tokens": 50}
        assert res["latency_ms"] >= 0
        assert res["model"] == "claude-sonnet-4-5"
        assert res["tool_calls"] is not None
        assert len(res["tool_calls"]) == 1
        tool_call = res["tool_calls"][0]
        assert tool_call["id"] == "toolu_sandbox_1"
        assert tool_call["type"] == "function"
        assert tool_call["function"]["name"] == "trigger_sandbox_build"
        assert json.loads(tool_call["function"]["arguments"]) == {"patch": "diff --git a/file.py"}

        assert route.called
        headers = route.calls.last.request.headers
        assert headers["x-api-key"] == "sk-ant-test-key-123"
        assert headers["anthropic-version"] == ANTHROPIC_VERSION
        assert str(route.calls.last.request.url).endswith("/v1/messages")

        sent_body = json.loads(route.calls.last.request.content)
        assert sent_body["model"] == "claude-sonnet-4-5"
        assert "max_tokens" in sent_body
        assert sent_body["tools"][0]["name"] == "trigger_sandbox_build"
        assert "input_schema" in sent_body["tools"][0]
    finally:
        settings.anthropic_api_key = orig_key


@pytest.mark.asyncio
async def test_anthropic_adapter_missing_api_key_raises_auth_error():
    """AnthropicAdapter raises LLMAuthenticationError when no API key is configured."""
    orig_key = settings.anthropic_api_key
    settings.anthropic_api_key = None
    try:
        provider = AnthropicAdapter()
        with pytest.raises(LLMAuthenticationError) as exc_info:
            await provider.complete(messages=[{"role": "user", "content": "hi"}])
        assert "ANTHROPIC_API_KEY is not configured" in str(exc_info.value)
    finally:
        settings.anthropic_api_key = orig_key
