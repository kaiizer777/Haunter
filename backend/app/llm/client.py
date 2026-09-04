"""
Unified LLM Client interface for Haunter.

Provides a provider-agnostic, single ``.complete()`` entrypoint for all downstream
subagents and orchestrators. Resolves the active provider and model dynamically
from Postgres with fallback to environment defaults.

Dynamic free-tier model discovery + dual-policy retry architecture:
- Dynamic Discovery: Discovers available free-tier models via GET /models with 15-min TTL caching.
- Policy 1 (Rate Limit): HTTP 429 retries 4-5 times per model with exponential backoff and jitter.
- Policy 2 (Outage / Unsupported): HTTP 5xx, timeouts, drops, per-model rejections, upstream 400s,
  and empty/malformed outputs execute exactly 1 attempt and immediately switch to the next fallback model.
- Policy 3 (Global Auth): Global 401/403 fails fast across all models.
"""

import asyncio
import json
import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.llm.config import get_active_model_config
from app.llm.discovery import BOOTSTRAP_FREE_MODELS, get_dynamic_free_models
from app.llm.exceptions import (
    LLMAuthenticationError,
    LLMError,
    LLMExhaustedFreeTierError,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from app.llm.opencode_zen import OpenCodeZenProvider

logger = logging.getLogger(__name__)

# Minimal bootstrap fallback set for backward compatibility
FREE_TIER_FALLBACK_ORDER: list[str] = list(BOOTSTRAP_FREE_MODELS)

# Attempt constants for backward compatibility & policy documentation
ATTEMPTS_PER_MODEL: int = 1
RATE_LIMIT_ATTEMPTS_PER_MODEL: int = 5

# Inter-model sleep to allow rate limit windows to clear before trying next model
INTER_MODEL_SLEEP_S: float = 2.5


class LLMClient:
    """Provider-agnostic LLM client for Haunter with dynamic free-tier fallback."""

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        db: AsyncSession | None = None,
        repo_id: uuid.UUID | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        Execute a chat completion against the active model provider.

        Constructs a dynamic fallback chain:
        1. Seed (active model from DB or per-repo config) first.
        2. All dynamically discovered '-free' models appended (de-duplicated).

        Decouples retries into two distinct policies:
        - Policy 1 (Rate Limit): 429 retries 4-5 times with exponential backoff.
          If 429 persists, switches to the next fallback model.
        - Policy 2 (Outage / Unsupported): 5xx, timeouts, transport errors, per-model
          rejections, upstream 400s, or empty text responses execute exactly 1 attempt
          and switch immediately to the next fallback model.
        - Policy 3 (Global Auth): Key-level 401 errors fail fast across all models.

        Args:
            messages: List of message dicts (e.g. ``[{"role": "user", "content": "..."}]``).
            tools: Optional tool definitions for tool-calling agents.
            db: Optional database session to resolve active model config.
            repo_id: Optional repo UUID to apply per-repo model overrides.
            **kwargs: Extra parameters (temperature, max_tokens, tool_choice, response_format, …)

        Returns:
            dict containing:
                - ``content`` (str | None): Model response text
                - ``tool_calls`` (list[dict] | None): Tool call definitions if requested
                - ``usage`` (dict): ``{"input_tokens": int, "output_tokens": int}``
                - ``latency_ms`` (int): Execution latency in milliseconds
                - ``model`` (str): Model identifier that produced the response
        """
        config = await get_active_model_config(db=db, repo_id=repo_id)
        target_provider = kwargs.pop("provider", config.provider)
        explicit_model = kwargs.pop("model", None)
        response_format = kwargs.get("response_format")

        if target_provider not in ("opencode_zen", "openai", "anthropic"):
            raise LLMError(f"Unsupported LLM provider: {target_provider}")

        seed_model = explicit_model or config.model_name

        # Construct dynamic fallback chain
        if target_provider == "opencode_zen":
            dynamic_models = await get_dynamic_free_models(
                base_url=config.base_url,
                api_key=settings.opencode_zen_api_key,
            )
        else:
            dynamic_models = []

        chain: list[str] = [seed_model]
        for m in dynamic_models:
            if m not in chain:
                chain.append(m)

        base_provider = OpenCodeZenProvider(
            base_url=config.base_url,
            timeout=self.timeout,
        )

        attempts_log: list[tuple[str, int, str]] = []
        cumulative_wasted_in = 0
        cumulative_wasted_out = 0

        for model_index, model_name in enumerate(chain):
            logger.info(
                "llm_client: trying model=%s (chain_index=%d/%d)",
                model_name,
                model_index + 1,
                len(chain),
            )
            try:
                response = await base_provider.complete(
                    messages=messages,
                    model=model_name,
                    tools=tools,
                    **kwargs,
                )
            except (LLMAuthenticationError, LLMInvalidRequestError) as fatal:
                # Policy 3 (Global Auth) & Client Schema Errors: fail fast across all models
                logger.error(
                    "llm_client: model=%s fatal %s — re-raising immediately across all models",
                    model_name,
                    type(fatal).__name__,
                )
                raise
            except LLMRateLimitError as exc:
                # Policy 1 (Rate Limit): 429 retried 4-5 times on this model and exhausted.
                # Record all attempts in attempts_log and switch to next model.
                att_count = getattr(exc, "attempts", RATE_LIMIT_ATTEMPTS_PER_MODEL)
                for att_i in range(1, att_count + 1):
                    attempts_log.append((model_name, att_i, f"LLMRateLimitError: {exc.message}"))
                logger.warning(
                    "llm_client: model=%s rate-limited after %d attempts — switching to next model",
                    model_name,
                    att_count,
                )
                if model_index < len(chain) - 1:
                    await asyncio.sleep(INTER_MODEL_SLEEP_S)
                continue
            except (LLMError, LLMTimeoutError) as exc:
                # Policy 2 (Outage / Unsupported): 5xx, timeout, transport drop, per-model rejection, upstream 400.
                # Attempt count is exactly 1 per model.
                attempts_log.append((model_name, 1, f"{type(exc).__name__}: {exc.message}"))
                logger.warning(
                    "llm_client: model=%s failed on attempt 1: %s — switching to next model",
                    model_name,
                    exc.message,
                )
                if model_index < len(chain) - 1:
                    await asyncio.sleep(INTER_MODEL_SLEEP_S)
                continue

            # 200 OK — validate the content
            # Usable if non-empty text OR tool_calls present
            content = response.get("content")
            tool_calls = response.get("tool_calls")
            text_empty = (content is None) or (
                isinstance(content, str) and not content.strip()
            )
            has_tool_calls = bool(tool_calls)
            is_empty = text_empty and not has_tool_calls
            json_invalid = self._is_json_invalid(content, response_format)

            if is_empty or json_invalid:
                # Policy 2: Empty text or malformed JSON fails on attempt 1 -> switch to next model
                reason = "empty content" if is_empty else "malformed JSON"
                attempts_log.append((model_name, 1, reason))
                wasted = response.get("usage") or {}
                cumulative_wasted_in += int(wasted.get("input_tokens", 0))
                cumulative_wasted_out += int(wasted.get("output_tokens", 0))
                logger.warning(
                    "llm_client: model=%s returned %s on attempt 1 — switching to next model",
                    model_name,
                    reason,
                )
                if model_index < len(chain) - 1:
                    await asyncio.sleep(INTER_MODEL_SLEEP_S)
                continue

            # Success
            if model_index > 0:
                logger.info(
                    "llm_client: recovered on fallback model=%s (chain_index=%d)",
                    model_name,
                    model_index + 1,
                )
            if cumulative_wasted_in or cumulative_wasted_out:
                logger.warning(
                    "llm_client: free-tier fallback chain wasted tokens — "
                    "input=%d output=%d across %d failed attempts before success on model=%s",
                    cumulative_wasted_in,
                    cumulative_wasted_out,
                    len(attempts_log),
                    model_name,
                )
            return response

        # All models exhausted
        logger.error(
            "llm_client: all %d free-tier models exhausted (%d total attempts)",
            len(chain),
            len(attempts_log),
        )
        raise LLMExhaustedFreeTierError(attempts=attempts_log)

    @staticmethod
    def _is_json_invalid(
        content: str | None,
        response_format: Any,
    ) -> bool:
        """
        When caller requested JSON-mode output, validate that returned content parses as JSON.
        Returns False for non-JSON calls, empty content, or valid JSON.
        """
        if not isinstance(response_format, dict):
            return False
        if response_format.get("type") not in ("json_object", "json_schema"):
            return False
        if not isinstance(content, str) or not content.strip():
            return False
        try:
            json.loads(content)
        except (ValueError, TypeError):
            return True
        return False
