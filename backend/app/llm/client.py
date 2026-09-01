"""
Unified LLM Client interface for Haunter.

Provides a provider-agnostic, single ``.complete()`` entrypoint for all downstream
subagents and orchestrators. Resolves the active provider and model dynamically
from Postgres with fallback to environment defaults.

Phase 17 — Per-model retry + automatic free-tier model fallback:

The OpenCode Zen free-tier models rotate their rate-limit windows
independently, so a single-model 429 on one model often resolves within
seconds on a sibling. ``LLMClient.complete()`` walks ``FREE_TIER_FALLBACK_ORDER``
when the active model fails (rate-limit / 5xx / timeout / empty content /
syntactically malformed JSON), retrying each model ``ATTEMPTS_PER_MODEL``
times before moving on. Fatal errors (``LLMAuthenticationError``,
``LLMInvalidRequestError``) fall through immediately — the provider auth is
shared across free-tier models, so failing over wouldn't help.

The public ``complete()`` signature is unchanged; subagents (context_gatherer,
fix_generator, pr_writer) do not need to be modified.
"""

import asyncio
import json
import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.config import get_active_model_config
from app.llm.exceptions import (
    LLMAuthenticationError,
    LLMError,
    LLMExhaustedFreeTierError,
    LLMInvalidRequestError,
)
from app.llm.opencode_zen import OpenCodeZenProvider

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Phase 17 constants — free-tier fallback chain
# ---------------------------------------------------------------------------
# Order matters: the active/seed model is prepended at runtime, then this
# list (minus the seed) is appended.
#
# Only models that actually respond to this project's OPENCODE_ZEN_API_KEY
# belong here. As of the last probe (see backend/scratch/probe_free_models.py):
#   * nemotron-3.5-lightning-free — OK
#   * nemotron-3-ultra-free       — OK (occasional upstream 502, retry succeeds)
#   * hy3-free                    — OK
#   * ling-3-free / qwen-3-coder-free / deepseek-r1-free / kimi-k2-free
#     all return 401 "Model is not supported" for this key — they're listed in
#     ``AllowedModelName`` for the UI but MUST NOT appear in the fallback chain
#     because they waste 7 attempts × INTER_MODEL_SLEEP_S on a guaranteed 401.
#
# Re-run the probe after rotating the API key or upgrading the OpenCode Zen
# tier, and prune/extend this list to match. The set is the free-tier subset
# of ``AllowedModelName`` in ``app/schemas.py``; do not add paid models here.
FREE_TIER_FALLBACK_ORDER: list[str] = [
    "nemotron-3.5-lightning-free",
    "nemotron-3-ultra-free",
    "hy3-free",
]

# Per-model attempt cap. Lowered from 7 to 3 on 2026-09-01: with the
# per-attempt timeout at 30s, 3 attempts = 90s ceiling per model. Combined
# with 3 fallback models and 2.5s inter-model sleep, the absolute worst
# case for LLMClient.complete() is ~280s, well under the 800s orchestrator
# wall-clock limit. 7 was right when the per-attempt timeout was 120s, but
# no longer.
ATTEMPTS_PER_MODEL: int = 3

# Inter-model sleep. Async so it composes with the event loop. 2.5s gives
# the previous model's rate-limit window a moment to clear before the
# next model fires its first request.
INTER_MODEL_SLEEP_S: float = 2.5

# Safety hard cap on (model, attempt) iterations to prevent unbounded loops
# if a future change accidentally widens the chain.
MAX_TOTAL_ATTEMPTS: int = len(FREE_TIER_FALLBACK_ORDER) * ATTEMPTS_PER_MODEL


class LLMClient:
    """Provider-agnostic LLM client for Haunter with free-tier fallback."""

    def __init__(self, timeout: float = 30.0) -> None:
        # Lowered from 120s on 2026-09-01: free-tier models should respond in <30s
        # on healthy state. 120s lets a single hanging model burn 2520s in retries
        # (3 models x 7 attempts x 120s), which exceeds the orchestrator's 800s
        # wall-clock limit and causes the whole run to time out. 30s is a hard
        # ceiling — slower responses fail fast and the client falls back to the
        # next model in FREE_TIER_FALLBACK_ORDER.
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

        On retryable failure (rate-limit, 5xx, timeout, empty content,
        syntactically malformed JSON when the caller requested JSON mode),
        walks ``FREE_TIER_FALLBACK_ORDER`` — retrying each model
        ``ATTEMPTS_PER_MODEL`` times before moving to the next — until a
        non-empty, valid response is produced. If every model is exhausted,
        raises ``LLMExhaustedFreeTierError`` carrying the full attempt log
        so the orchestrator can surface per-model last-errors on the
        dashboard.

        Fatal errors (``LLMAuthenticationError``, ``LLMInvalidRequestError``)
        propagate immediately — provider auth is shared across free-tier
        models, so falling over wouldn't help.

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
        # Peek (do not pop) — the inner provider.complete() still needs it
        # to set OpenAI-style response_format on the HTTP payload.
        response_format = kwargs.get("response_format")

        if target_provider not in ("opencode_zen", "openai", "anthropic"):
            raise LLMError(f"Unsupported LLM provider: {target_provider}")

        # Build the ordered fallback chain. Seed (active model) first; the
        # rest of FREE_TIER_FALLBACK_ORDER minus the seed follows. Stable,
        # de-duplicated, env-var default works as a seed.
        seed_model = explicit_model or config.model_name
        chain: list[str] = [seed_model]
        for m in FREE_TIER_FALLBACK_ORDER:
            if m != seed_model:
                chain.append(m)

        base_provider = OpenCodeZenProvider(
            base_url=config.base_url,
            timeout=self.timeout,
        )

        attempts_log: list[tuple[str, int, str]] = []
        cumulative_wasted_in = 0
        cumulative_wasted_out = 0
        total_attempts = 0

        for model_index, model_name in enumerate(chain):
            for attempt_idx in range(1, ATTEMPTS_PER_MODEL + 1):
                if total_attempts >= MAX_TOTAL_ATTEMPTS:
                    # Defensive: should be unreachable given the for-loop bounds.
                    break
                total_attempts += 1

                logger.info(
                    "llm_client: model=%s attempt=%d/%d",
                    model_name,
                    attempt_idx,
                    ATTEMPTS_PER_MODEL,
                )
                try:
                    response = await base_provider.complete(
                        messages=messages,
                        model=model_name,
                        tools=tools,
                        **kwargs,
                    )
                except (LLMAuthenticationError, LLMInvalidRequestError) as fatal:
                    # Same auth across all free-tier models; falling over
                    # wouldn't help. Raise immediately so the user sees the
                    # real error.
                    logger.error(
                        "llm_client: model=%s attempt=%d fatal %s — no fallback, re-raising",
                        model_name,
                        attempt_idx,
                        type(fatal).__name__,
                    )
                    raise
                except LLMError as exc:
                    # Rate-limit / 5xx / timeout / network. Count as a
                    # failed attempt on this model; the inner execute_with_retry
                    # already burned its own per-call budget.
                    attempts_log.append(
                        (model_name, attempt_idx, f"{type(exc).__name__}: {exc.message}")
                    )
                    logger.warning(
                        "llm_client: model=%s attempt=%d/%d failed: %s",
                        model_name,
                        attempt_idx,
                        ATTEMPTS_PER_MODEL,
                        exc.message,
                    )
                    continue

                # 200 — validate the content.
                # A response is "usable" if it has non-empty text OR
                # tool_calls. A tool-call-only response (content=None,
                # tool_calls=[...]) is the common case for agents like
                # fix_generator and must NOT be retried.
                content = response.get("content")
                tool_calls = response.get("tool_calls")
                text_empty = (content is None) or (
                    isinstance(content, str) and not content.strip()
                )
                has_tool_calls = bool(tool_calls)
                is_empty = text_empty and not has_tool_calls
                json_invalid = self._is_json_invalid(content, response_format)

                if is_empty or json_invalid:
                    reason = "empty content" if is_empty else "malformed JSON"
                    attempts_log.append((model_name, attempt_idx, reason))
                    wasted = response.get("usage") or {}
                    cumulative_wasted_in += int(wasted.get("input_tokens", 0))
                    cumulative_wasted_out += int(wasted.get("output_tokens", 0))
                    logger.warning(
                        "llm_client: model=%s attempt=%d/%d %s",
                        model_name,
                        attempt_idx,
                        ATTEMPTS_PER_MODEL,
                        reason,
                    )
                    continue

                # Success.
                if model_index > 0 or attempt_idx > 1:
                    logger.info(
                        "llm_client: recovered on model=%s (chain_index=%d, attempt=%d)",
                        model_name,
                        model_index,
                        attempt_idx,
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

            # All ATTEMPTS_PER_MODEL attempts on this model failed.
            logger.warning(
                "llm_client: model=%s exhausted after %d attempts — moving to next model",
                model_name,
                ATTEMPTS_PER_MODEL,
            )
            if model_index < len(chain) - 1:
                await asyncio.sleep(INTER_MODEL_SLEEP_S)

        # Every model exhausted.
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
        When the caller asked for JSON-mode output via
        ``response_format={"type": "json_object"}`` (or ``"json_schema"``),
        validate that the returned ``content`` parses as JSON. Returns
        ``False`` for non-JSON-mode calls, empty content (handled
        separately by the empty-content check), or valid JSON.

        Note: this catches only *syntactic* errors. Schema-level
        validation (e.g. Pydantic ``confidence=150`` out of range) is
        the caller's responsibility — ``fix_generator._call_and_parse``
        handles that with its own one-shot ValidationError retry.
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
