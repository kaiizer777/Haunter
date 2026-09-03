"""
OpenCode Zen OpenAI-compatible LLM provider adapter for Haunter.

Targets OpenCode Zen API (https://opencode.ai/zen/v1) with default model nemotron-3.5-lightning-free.
Injects API key per request, measures latency via perf_counter, captures token usage,
and supports tool-calling passthrough without exposing secrets.
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


class OpenCodeZenProvider:
    """OpenAI-compatible adapter for OpenCode Zen endpoint."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = (base_url or settings.opencode_zen_base_url).rstrip("/") + "/"
        self._api_key = api_key or settings.opencode_zen_api_key
        self.timeout = timeout

    async def complete(
        self,
        messages: list[dict[str, Any]],
        model: str = "nemotron-3.5-lightning-free",
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        Send a chat completion request to OpenCode Zen API with retries.

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
            raise LLMAuthenticationError("OPENCODE_ZEN_API_KEY is not configured in settings or environment")

        endpoint = urljoin(self.base_url, "chat/completions")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        # Clamp `max_tokens` to the configured ceiling. Subagents currently pass
        # max_tokens=10_000_000 for test mode, but the free-tier endpoints
        # reject the request with HTTP 400 when max_tokens is counted as part of
        # the model's context budget (Nemotron-3.5-Lightning: 1M total, and
        # max_tokens is reserved against it). The cap here is the last line of
        # defence so the high test value doesn't break the workflow. Subagents
        # still see their requested value in the trace — only the wire payload
        # is clamped.
        if "max_tokens" in kwargs:
            ceiling = settings.opencode_zen_max_output_tokens
            requested = kwargs["max_tokens"]
            if requested > ceiling:
                logger.debug(
                    "opencode_zen: clamping max_tokens %d -> %d (configured ceiling)",
                    requested,
                    ceiling,
                )
                kwargs["max_tokens"] = ceiling

        # Strip response_format — free-tier models on OpenCode Zen reject it with HTTP 400
        kwargs.pop("response_format", None)

        payload: dict[str, Any] = {
            "model": model,
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

            # Safely parse response choices and usage
            choices = data.get("choices", [])
            if not choices:
                raise LLMError("OpenCode Zen returned response with empty choices")

            first_choice = choices[0]
            message = first_choice.get("message", {})
            content = message.get("content")
            tool_calls = message.get("tool_calls")

            usage_data = data.get("usage", {})
            input_tokens = usage_data.get("prompt_tokens", 0)
            output_tokens = usage_data.get("completion_tokens", 0)
            returned_model = data.get("model", model)

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
