"""
OpenAI LLM provider adapter for Haunter (Phase 3.2 Issue 3).

Targets the OpenAI Chat Completions API (https://api.openai.com/v1/chat/completions)
with Bearer authentication. Dedicated adapter — never routed through
OpenCodeZenProvider, which uses a different base URL, auth scope, and model
namespace. Injects the API key per request, measures latency via perf_counter,
captures token usage, and supports tool-calling passthrough without exposing
secrets (never logged).
"""

import logging
import time
from typing import Any
from urllib.parse import urljoin

import httpx

from app.config import settings
from app.llm.exceptions import LLMAuthenticationError, LLMError
from app.llm.retry import execute_with_retry

logger = logging.getLogger(__name__)

# gpt-4o / gpt-4o-mini support up to 16k output tokens. Subagents may pass
# inflated test-mode max_tokens values — clamp to this ceiling so outgoing
# requests stay inside the model's accepted range (mirrors the Groq adapter).
OPENAI_MAX_OUTPUT_TOKENS: int = 16_384


class OpenAIAdapter:
    """OpenAI-compatible adapter for the OpenAI Chat Completions endpoint."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        raw_base_url = base_url or settings.openai_base_url
        self.base_url = raw_base_url.rstrip("/") + "/"
        self._api_key = api_key or settings.openai_api_key
        self.default_model = model or "gpt-4o"
        self.timeout = timeout

    async def complete(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        Send a chat completion request to the OpenAI API with retries.

        Returns:
            dict: {
                "content": str | None,
                "tool_calls": list[dict] | None,
                "usage": {"input_tokens": int, "output_tokens": int},
                "latency_ms": int,
                "model": str,
            }
        """
        if not self._api_key:
            raise LLMAuthenticationError(
                "OPENAI_API_KEY is not configured in settings or environment"
            )

        target_model = model or self.default_model
        endpoint = urljoin(self.base_url, "chat/completions")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        # Clamp max_tokens to the OpenAI output budget if inflated test values are passed.
        if "max_tokens" in kwargs:
            requested = kwargs["max_tokens"]
            if (
                isinstance(requested, (int, float))
                and requested > OPENAI_MAX_OUTPUT_TOKENS
            ):
                logger.debug(
                    "openai: clamping max_tokens %d -> %d",
                    requested,
                    OPENAI_MAX_OUTPUT_TOKENS,
                )
                kwargs["max_tokens"] = OPENAI_MAX_OUTPUT_TOKENS

        payload: dict[str, Any] = {
            "model": target_model,
            "messages": messages,
            **kwargs,
        }

        if tools is not None:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

        async def _make_request() -> dict[str, Any]:
            start_time = time.perf_counter()
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(endpoint, json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
            latency_ms = int((time.perf_counter() - start_time) * 1000)

            choices = data.get("choices", [])
            if not choices:
                raise LLMError("OpenAI returned response with empty choices")

            first_choice = choices[0]
            message = first_choice.get("message", {})
            content = message.get("content")
            tool_calls = message.get("tool_calls")

            usage_data = data.get("usage", {})
            input_tokens = usage_data.get("prompt_tokens", 0)
            output_tokens = usage_data.get("completion_tokens", 0)
            returned_model = data.get("model", target_model)

            return {
                "content": content,
                "tool_calls": tool_calls,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                },
                "latency_ms": latency_ms,
                "model": returned_model,
            }

        return await execute_with_retry(_make_request)
