"""
Pydantic request/response schemas for Haunter API.

All external input crossing a trust boundary is validated through these schemas.
No free-text injection vectors — provider and model_name use Literal allowlists.
"""

import uuid
from datetime import datetime
from typing import Literal, Optional

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


# ---------------------------------------------------------------------------
# ModelConfig schemas
# ---------------------------------------------------------------------------

# Allowlisted providers — no free-text to prevent base_url injection.
# Extend this list when a new provider is vetted and approved.
AllowedProvider = Literal["opencode_zen", "openai", "anthropic"]

# Allowlisted model names — must match a provider's supported models.
AllowedModelName = Literal[
    "nemotron-3.5-lightning-free",
    "gpt-4o",
    "gpt-4o-mini",
    "claude-sonnet-4-5",
    "claude-haiku-3-5",
]


class ModelConfigUpdate(BaseModel):
    """
    Used by PUT /config/model/{repo_id} to update a repo's active model config.
    Provider and model_name are allowlisted — no free-text base_url injection.
    base_url is derived server-side from the provider allowlist, never from the client.
    """

    provider: AllowedProvider
    model_name: AllowedModelName


class ModelConfigOut(BaseModel):
    id: uuid.UUID
    provider: str
    model_name: str
    base_url: str
    is_active: bool

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Run schemas
# ---------------------------------------------------------------------------


class RunOut(BaseModel):
    id: uuid.UUID
    repo_id: uuid.UUID
    github_run_id: int
    head_sha: str
    head_branch: str
    status: str
    conclusion: Optional[str]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
