"""
Pydantic request/response schemas for Haunter API.

All external input crossing a trust boundary is validated through these schemas.
No free-text injection vectors — provider and model_name use Literal allowlists.
"""

import uuid
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Repo schemas
# ---------------------------------------------------------------------------


class RepoCreate(BaseModel):
    owner: str = Field(..., min_length=1, max_length=255)
    name: str = Field(..., min_length=1, max_length=255)
    default_branch: Optional[str] = Field(None, max_length=255)
    language_hint: Optional[str] = Field(None, max_length=255)
    active_model_config_id: Optional[uuid.UUID] = None


class RepoOut(BaseModel):
    id: uuid.UUID
    owner: str
    name: str
    default_branch: Optional[str]
    language_hint: Optional[str]
    active_model_config_id: Optional[uuid.UUID]
    created_at: datetime

    model_config = {"from_attributes": True}


class AvailableRepoOut(BaseModel):
    owner: str
    name: str
    full_name: str
    default_branch: Optional[str] = None
    language: Optional[str] = None
    private: bool
    updated_at: Optional[str] = None
    already_connected: bool
    permissions_push: bool


# ---------------------------------------------------------------------------
# ModelConfig schemas
# ---------------------------------------------------------------------------

# Allowlisted providers — no free-text to prevent base_url injection.
# Extend this list when a new provider is vetted and approved.
AllowedProvider = Literal["opencode_zen", "openai", "anthropic"]

# Allowlisted hosting/sandbox providers — gcp|aws only, never free-text.
# Used by PUT /config/hosting and validated in the hosting adapter.
# Extending this requires both code review and policy justification.
AllowedHostingProvider = Literal["gcp", "aws"]
AllowedSandboxProvider = Literal["gcp", "aws"]

# Allowlisted model names — must match a provider's supported models.
# Free-tier models (suffix `-free`) are listed first so the UI surfaces them
# at the top of the model selector — these are the only models safe to use
# while we're validating the pipeline on a budget.
#
# When OpenCode Zen (or any other provider) publishes a new free model:
#   1. Add the exact model id below
#   2. Mirror it in frontend/src/app/config/page.tsx MODEL_OPTIONS_BY_PROVIDER
#   3. No other change needed — the allowlist is the single source of truth.
AllowedModelName = Literal[
    # OpenCode Zen — free tier
    "nemotron-3.5-lightning-free",
    "nemotron-3-ultra-free",
    "hy3-free",
    "ling-3-free",
    "qwen-3-coder-free",
    "deepseek-r1-free",
    "kimi-k2-free",
    # OpenAI — paid
    "gpt-4o",
    "gpt-4o-mini",
    # Anthropic — paid
    "claude-sonnet-4-5",
    "claude-haiku-3-5",
]


class ModelConfigUpdate(BaseModel):
    """
    Used by PUT /config/model (global) or PUT /config/model/{repo_id} (per-repo)
    to update active model config.
    Provider and model_name are allowlisted — no free-text base_url injection.
    base_url is derived server-side from the provider allowlist, never from the client.
    """

    provider: AllowedProvider
    model_name: AllowedModelName
    repo_id: Optional[uuid.UUID] = None


class ModelConfigOut(BaseModel):
    id: uuid.UUID
    provider: str
    model_name: str
    base_url: str
    is_active: bool

    model_config = {"from_attributes": True}


class LLMUsageOut(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class LLMResponseOut(BaseModel):
    content: Optional[str] = None
    tool_calls: Optional[list[dict[str, Any]]] = None
    usage: LLMUsageOut
    latency_ms: int
    model: str


# ---------------------------------------------------------------------------
# Run schemas
# ---------------------------------------------------------------------------


class RunOut(BaseModel):
    id: uuid.UUID
    repo_id: uuid.UUID
    github_run_id: int
    github_delivery_id: Optional[str] = None
    head_sha: str
    head_branch: str
    status: str
    conclusion: Optional[str]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Webhook schemas (GitHub workflow_run event)
# ---------------------------------------------------------------------------


class WorkflowRunRepoOwner(BaseModel):
    login: str = Field(..., min_length=1, max_length=255)

    model_config = {"extra": "ignore"}


class WorkflowRunRepo(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    full_name: str = Field(..., min_length=1, max_length=255)
    owner: WorkflowRunRepoOwner

    model_config = {"extra": "ignore"}


class WorkflowRunObj(BaseModel):
    id: int
    head_sha: str = Field(..., pattern=r"^[0-9a-fA-F]{40}$")
    head_branch: Optional[str] = Field(default="main", max_length=255)
    conclusion: Optional[str] = None
    html_url: Optional[str] = None

    model_config = {"extra": "ignore"}


class WorkflowRunWebhookPayload(BaseModel):
    action: str
    workflow_run: WorkflowRunObj
    repository: WorkflowRunRepo

    model_config = {"extra": "ignore"}


# ---------------------------------------------------------------------------
# Hosting/Sandbox provider config schemas (Phase 14)
# ---------------------------------------------------------------------------


class HostingConfigUpdate(BaseModel):
    """
    Used by PUT /config/hosting to switch HOSTING_PROVIDER and/or SANDBOX_PROVIDER.

    Both values are strict Literal allowlists — no free-text, no base_url injection,
    no provider values derived from request headers.
    Admin-gated endpoint: requires ADMIN_USER_ID match (same as PUT /config/model).
    """

    hosting_provider: AllowedHostingProvider
    sandbox_provider: AllowedSandboxProvider


class HostingConfigOut(BaseModel):
    """
    Current active hosting and sandbox provider configuration.
    Values are read from DB (system_configs) with 60s TTL cache, falling
    back to env var defaults (HOSTING_PROVIDER, SANDBOX_PROVIDER).
    """

    hosting_provider: str
    sandbox_provider: str
    source: str  # "db" | "env" — indicates where the active value came from
