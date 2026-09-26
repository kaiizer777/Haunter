"""
Haunter LLM Subsystem.
"""

from app.llm.anthropic import AnthropicAdapter
from app.llm.client import LLMClient
from app.llm.config import (
    ResolvedModelConfig,
    default_base_url_for_provider,
    get_active_model_config,
)
from app.llm.discovery import (
    BOOTSTRAP_FREE_MODELS,
    clear_model_cache,
    get_dynamic_free_models,
)
from app.llm.exceptions import (
    LLMAuthenticationError,
    LLMError,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from app.llm.groq import GroqProvider
from app.llm.opencode_zen import OpenCodeZenProvider
from app.llm.openai import OpenAIAdapter

__all__ = [
    "LLMClient",
    "ResolvedModelConfig",
    "default_base_url_for_provider",
    "get_active_model_config",
    "get_dynamic_free_models",
    "BOOTSTRAP_FREE_MODELS",
    "clear_model_cache",
    "OpenCodeZenProvider",
    "OpenAIAdapter",
    "AnthropicAdapter",
    "GroqProvider",
    "LLMError",
    "LLMTimeoutError",
    "LLMAuthenticationError",
    "LLMRateLimitError",
    "LLMInvalidRequestError",
]
