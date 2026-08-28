"""
Unified LLM Client interface for Haunter.

Provides a provider-agnostic, single `.complete()` entrypoint for all downstream
subagents and orchestrators. Resolves active provider and model dynamically from Postgres
with fallback to environment defaults.
"""

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.config import get_active_model_config
from app.llm.exceptions import LLMError
from app.llm.opencode_zen import OpenCodeZenProvider

logger = logging.getLogger(__name__)


class LLMClient:
    """Provider-agnostic LLM client for Haunter."""

    def __init__(self, timeout: float = 120.0) -> None:
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

        Args:
            messages: List of message dicts (e.g. [{"role": "user", "content": "..."}]).
            tools: Optional tool definitions for tool-calling agents.
            db: Optional database session to resolve active model config.
            repo_id: Optional repo UUID to apply per-repo model overrides.
            **kwargs: Extra parameters (temperature, max_tokens, tool_choice, etc.)

        Returns:
            dict containing:
                - content (str | None): Model response text
                - tool_calls (list[dict] | None): Tool call definitions if requested
                - usage (dict): {"input_tokens": int, "output_tokens": int}
                - latency_ms (int): Execution latency in milliseconds
                - model (str): Active model identifier
        """
        config = await get_active_model_config(db=db, repo_id=repo_id)
        target_model = kwargs.pop("model", config.model_name)
        target_provider = kwargs.pop("provider", config.provider)

        # Dispatch based on provider
        if target_provider == "opencode_zen":
            provider = OpenCodeZenProvider(
                base_url=config.base_url,
                timeout=self.timeout,
            )
            return await provider.complete(
                messages=messages,
                model=target_model,
                tools=tools,
                **kwargs,
            )

        elif target_provider in ("openai", "anthropic"):
            # OpenAI-compatible fallback protocol
            provider = OpenCodeZenProvider(
                base_url=config.base_url,
                timeout=self.timeout,
            )
            return await provider.complete(
                messages=messages,
                model=target_model,
                tools=tools,
                **kwargs,
            )

        raise LLMError(f"Unsupported LLM provider: {target_provider}")
