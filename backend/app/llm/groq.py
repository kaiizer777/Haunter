"""
Groq LLM provider adapter for Haunter.

Targets Groq's OpenAI-compatible API (https://api.groq.com/openai/v1).
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


class GroqProvider:
    """OpenAI-compatible adapter for Groq endpoint."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        raw_base_url = base_url or settings.groq_base_url
        self.base_url = raw_base_url.rstrip("/") + "/"
        self._api_key = api_key or settings.groq_api_key
        self.default_model = model or settings.groq_model_name or "openai/gpt-oss-120b"
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
        Send a chat completion request to Groq API with retries.

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
            raise LLMAuthenticationError("GROQ_API_KEY is not configured in settings or environment")

        target_model = model or self.default_model
        endpoint = urljoin(self.base_url, "chat/completions")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        # Clamp max_tokens to avoid exceeding Groq provider output budget if high test values are passed
        if "max_tokens" in kwargs:
            requested = kwargs["max_tokens"]
            max_groq_tokens = 8192
            if requested > max_groq_tokens:
                logger.debug(
                    "groq: clamping max_tokens %d -> %d",
                    requested,
                    max_groq_tokens,
                )
                kwargs["max_tokens"] = max_groq_tokens

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
                raise LLMError("Groq returned response with empty choices")

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
