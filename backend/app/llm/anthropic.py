"""
Anthropic LLM provider adapter for Haunter (Phase 3.2 Issue 3).

Targets the Anthropic Messages API (https://api.anthropic.com/v1/messages)
with native payload semantics: ``x-api-key`` + ``anthropic-version`` headers,
top-level ``system`` string, ``max_tokens`` (required), and ``input_schema``
tool definitions. Dedicated adapter — never routed through
OpenCodeZenProvider, which speaks the OpenAI-compatible schema with Bearer
auth. Converts OpenAI-style messages/tools to the Anthropic native format on
the way in and normalises tool_use blocks back to OpenAI-style tool_calls on
the way out so downstream subagents keep a single contract. Secrets are
injected per request and never logged.
"""

import json
import logging
import time
from typing import Any
from urllib.parse import urljoin

import httpx

from app.config import settings
from app.llm.exceptions import LLMAuthenticationError, LLMError
from app.llm.retry import execute_with_retry

logger = logging.getLogger(__name__)

ANTHROPIC_VERSION: str = "2023-06-01"

# Anthropic requires max_tokens on every /messages call. Default when callers
# omit it; ceiling guards inflated test-mode values (mirrors sibling adapters).
ANTHROPIC_DEFAULT_MAX_TOKENS: int = 4096
ANTHROPIC_MAX_OUTPUT_TOKENS: int = 8192


def _to_anthropic_messages(messages: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
    """Convert OpenAI-style messages to Anthropic native (system, messages).

    - ``role="system"`` entries are hoisted into the top-level ``system`` string.
    - ``role="tool"`` entries become ``user`` messages with ``tool_result`` blocks.
    - Assistant entries carrying OpenAI ``tool_calls`` become ``assistant``
      messages with ``tool_use`` blocks.
    - Everything else passes through as ``user``/``assistant`` text.
    """
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []

    for msg in messages:
        role = msg.get("role")

        if role == "system":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                system_parts.append(content)
            continue

        if role == "tool":
            converted.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": str(msg.get("tool_call_id", "")),
                            "content": str(msg.get("content", "")),
                        }
                    ],
                }
            )
            continue

        if role == "assistant" and msg.get("tool_calls"):
            blocks: list[dict[str, Any]] = []
            text = msg.get("content")
            if isinstance(text, str) and text.strip():
                blocks.append({"type": "text", "text": text})
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                try:
                    tool_input = json.loads(fn.get("arguments", "{}"))
                except (ValueError, TypeError, AttributeError):
                    tool_input = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(tc.get("id", "")) if isinstance(tc, dict) else "",
                        "name": str(fn.get("name", "")),
                        "input": tool_input if isinstance(tool_input, dict) else {},
                    }
                )
            converted.append({"role": "assistant", "content": blocks})
            continue

        content = msg.get("content")
        text = content if isinstance(content, str) else str(content or "")
        converted.append(
            {
                "role": "assistant" if role == "assistant" else "user",
                "content": text,
            }
        )

    system = "\n\n".join(system_parts) if system_parts else None
    return system, converted


def _to_anthropic_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Convert OpenAI-style function tools to Anthropic native tool definitions."""
    converted: list[dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function")
        if tool.get("type") == "function" and isinstance(fn, dict):
            converted.append(
                {
                    "name": str(fn.get("name", "")),
                    "description": str(fn.get("description", "")),
                    "input_schema": fn.get("parameters", {"type": "object"}),
                }
            )
        elif "name" in tool and "input_schema" in tool:
            converted.append(tool)
    return converted


class AnthropicAdapter:
    """Native adapter for the Anthropic Messages endpoint."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        raw_base_url = base_url or settings.anthropic_base_url
        self.base_url = raw_base_url.rstrip("/") + "/"
        self._api_key = api_key or settings.anthropic_api_key
        self.default_model = model or "claude-sonnet-4-5"
        self.timeout = timeout

    async def complete(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        Send a messages request to the Anthropic API with retries.

        Returns (normalised to the shared LLM contract):
            dict: {
                "content": str | None,
                "tool_calls": list[dict] | None (OpenAI-style),
                "usage": {"input_tokens": int, "output_tokens": int},
                "latency_ms": int,
                "model": str,
            }
        """
        if not self._api_key:
            raise LLMAuthenticationError("ANTHROPIC_API_KEY is not configured in settings or environment")

        target_model = model or self.default_model
        endpoint = urljoin(self.base_url, "messages")
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }

        # Anthropic requires max_tokens — default it, clamp inflated values.
        requested_max = kwargs.pop("max_tokens", ANTHROPIC_DEFAULT_MAX_TOKENS)
        try:
            max_tokens = int(requested_max)
        except (TypeError, ValueError):
            max_tokens = ANTHROPIC_DEFAULT_MAX_TOKENS
        if max_tokens < 1:
            max_tokens = ANTHROPIC_DEFAULT_MAX_TOKENS
        if max_tokens > ANTHROPIC_MAX_OUTPUT_TOKENS:
            logger.debug(
                "anthropic: clamping max_tokens %d -> %d",
                max_tokens,
                ANTHROPIC_MAX_OUTPUT_TOKENS,
            )
            max_tokens = ANTHROPIC_MAX_OUTPUT_TOKENS

        # Anthropic has no response_format / tool_choice(OpenAI-style) knobs.
        kwargs.pop("response_format", None)
        kwargs.pop("tool_choice", None)

        system, anthropic_messages = _to_anthropic_messages(messages)
        anthropic_tools = _to_anthropic_tools(tools)

        payload: dict[str, Any] = {
            "model": target_model,
            "max_tokens": max_tokens,
            "messages": anthropic_messages,
            **kwargs,
        }
        if system:
            payload["system"] = system
        if anthropic_tools:
            payload["tools"] = anthropic_tools

        async def _make_request() -> dict[str, Any]:
            start_time = time.perf_counter()
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(endpoint, json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
            latency_ms = int((time.perf_counter() - start_time) * 1000)

            blocks = data.get("content")
            if not isinstance(blocks, list) or not blocks:
                raise LLMError("Anthropic returned response with empty content")

            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text" and isinstance(block.get("text"), str):
                    text_parts.append(block["text"])
                elif btype == "tool_use":
                    tool_input = block.get("input", {})
                    try:
                        arguments = json.dumps(tool_input if isinstance(tool_input, dict) else {})
                    except (TypeError, ValueError):
                        arguments = "{}"
                    tool_calls.append(
                        {
                            "id": str(block.get("id", "")),
                            "type": "function",
                            "function": {
                                "name": str(block.get("name", "")),
                                "arguments": arguments,
                            },
                        }
                    )

            content = "\n".join(text_parts) if text_parts else None

            usage_data = data.get("usage", {}) if isinstance(data.get("usage"), dict) else {}
            input_tokens = usage_data.get("input_tokens", 0)
            output_tokens = usage_data.get("output_tokens", 0)
            returned_model = data.get("model", target_model)

            return {
                "content": content,
                "tool_calls": tool_calls or None,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                },
                "latency_ms": latency_ms,
                "model": returned_model,
            }

        return await execute_with_retry(_make_request)
