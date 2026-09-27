"""
Pydantic request/response schemas for Haunter API.

All external input crossing a trust boundary is validated through these schemas.
No free-text injection vectors — provider and model_name use Literal allowlists.
"""

import uuid
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models import UserRole


class UserRoleUpdate(BaseModel):
    role: UserRole


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
AllowedProvider = Literal["opencode_zen", "openai", "anthropic", "groq"]

# Allowlisted hosting/sandbox providers — never free-text, never derived
# from request headers. Used by PUT /config/hosting and validated in the
# hosting / sandbox adapter. Extending this requires both code review and
# policy justification.
#   "aws"             — Lambda hosting
#   "github_actions"  — GitHub Actions sandbox (per github.md)
AllowedHostingProvider = Literal["aws"]
AllowedSandboxProvider = Literal["github_actions", "aws"]

OPENAI_MODELS: set[str] = {"gpt-4o", "gpt-4o-mini"}
ANTHROPIC_MODELS: set[str] = {"claude-sonnet-4-5", "claude-haiku-3-5"}

# Preserved for backward compatibility
AllowedModelName = str


class ModelConfigUpdate(BaseModel):
    """
    Used by PUT /config/model (global) or PUT /config/model/{repo_id} (per-repo)
    to update active model config.
    Provider and model_name are validated against provider rules.
    - For 'opencode_zen': allows any model name ending with '-free'.
    - For 'openai': allows approved OpenAI models (gpt-4o, gpt-4o-mini).
    - For 'anthropic': allows approved Anthropic models (claude-sonnet-4-5, claude-haiku-3-5).
    - For 'groq': allows non-empty model names.
    base_url is derived server-side from the provider allowlist, never from the client.
    """

    provider: AllowedProvider
    model_name: str
    repo_id: Optional[uuid.UUID] = None

    @model_validator(mode="after")
    def validate_model_name_for_provider(self) -> "ModelConfigUpdate":
        provider = self.provider
        model = self.model_name.strip()

        if provider == "opencode_zen":
            if not model.endswith("-free"):
                raise ValueError(
                    f"Invalid model '{model}' for provider 'opencode_zen'. "
                    "OpenCode Zen models must end with '-free'."
                )
        elif provider == "openai":
            if model not in OPENAI_MODELS:
                raise ValueError(
                    f"Invalid model '{model}' for provider 'openai'. "
                    f"Allowed models: {sorted(OPENAI_MODELS)}"
                )
        elif provider == "anthropic":
            if model not in ANTHROPIC_MODELS:
                raise ValueError(
                    f"Invalid model '{model}' for provider 'anthropic'. "
                    f"Allowed models: {sorted(ANTHROPIC_MODELS)}"
                )
        elif provider == "groq":
            if not model:
                raise ValueError("Model name cannot be empty for provider 'groq'.")
        else:
            raise ValueError(f"Unsupported provider: {provider}")

        return self


class AvailableModelItem(BaseModel):
    id: str
    name: str
    tag: str
    context_window: Optional[int] = None


class AvailableModelsOut(BaseModel):
    opencode_zen: list[AvailableModelItem]
    openai: list[AvailableModelItem]
    anthropic: list[AvailableModelItem]
    groq: list[AvailableModelItem] = []


class ModelConfigOut(BaseModel):
    id: uuid.UUID
    provider: str
    model_name: str
    base_url: str
    is_active: bool
    scope: str = "global"
    repo_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None

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
    parent_run_id: Optional[uuid.UUID] = None
    github_run_id: Optional[int] = None
    github_delivery_id: Optional[str] = None
    head_sha: str
    head_branch: str
    status: str
    conclusion: Optional[str]
    cost: float = 0.0
    tokens: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class BatchDeleteRunsRequest(BaseModel):
    run_ids: list[uuid.UUID] = Field(default_factory=list)


class BatchDeleteRunsResponse(BaseModel):
    deleted_count: int


# ---------------------------------------------------------------------------
# Webhook schemas (GitHub workflow_run, issue_comment, pull_request_review_comment)
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


class IssueCommentUser(BaseModel):
    login: str = Field(..., min_length=1, max_length=255)

    model_config = {"extra": "ignore"}


class IssueCommentObj(BaseModel):
    id: int
    body: str = Field(..., min_length=1)
    author_association: str = Field(default="NONE", max_length=50)
    user: Optional[IssueCommentUser] = None

    model_config = {"extra": "ignore"}


class IssuePullRequestRef(BaseModel):
    url: Optional[str] = None
    html_url: Optional[str] = None

    model_config = {"extra": "ignore"}


class IssueObj(BaseModel):
    number: int
    pull_request: Optional[IssuePullRequestRef] = None

    model_config = {"extra": "ignore"}


class PullRequestBranchRef(BaseModel):
    ref: str = Field(..., max_length=255)
    sha: Optional[str] = None

    model_config = {"extra": "ignore"}


class PullRequestObj(BaseModel):
    number: int
    head: PullRequestBranchRef
    base: Optional[PullRequestBranchRef] = None

    model_config = {"extra": "ignore"}


class IssueCommentWebhookPayload(BaseModel):
    action: str
    comment: IssueCommentObj
    issue: IssueObj
    repository: WorkflowRunRepo

    model_config = {"extra": "ignore"}


class PullRequestReviewCommentWebhookPayload(BaseModel):
    action: str
    comment: IssueCommentObj
    pull_request: PullRequestObj
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
    Admin-gated endpoint: requires ADMIN_USER_ID match.
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


# ---------------------------------------------------------------------------
# User-scoped eval metrics — reliability & evals dashboard
# ---------------------------------------------------------------------------
#
# Aggregates over the caller's repos/runs/attempts. Computed by the
# /eval/user-metrics endpoint in routers/eval.py. Used by the user-facing
# reliability dashboard (see WORK.md — Reliability & Evals API).
# ---------------------------------------------------------------------------


class ConfidenceBucket(BaseModel):
    """
    One confidence-range bucket for the calibration histogram.

    `bucket` is a human label ("0-50%", "50-75%", "75-90%", "90-100%") and
    follows the LLM confidence scale (0-100 integer, see
    subagents/fix_generator.py FixOutput.confidence: int = Field(ge=0, le=100)).
    """

    bucket: str
    total_attempts: int
    passed_attempts: int
    accuracy_pct: float

    model_config = {"from_attributes": True}


class UserEvalMetricsOut(BaseModel):
    """
    User-scoped reliability & calibration rollup for the dashboard.

    Every aggregate is scoped to the caller's repos via Repo.user_id == current_user.id.
    `confidence_calibration` is a fixed 4-element list in bucket order:
    0-50%, 50-75%, 75-90%, 90-100% — see routers/eval.py get_user_eval_metrics.
    """

    total_runs: int
    healed_runs: int
    success_rate_pct: float
    avg_duration_seconds: float
    total_cost: float
    avg_cost_per_run: float
    confidence_calibration: list[ConfidenceBucket]

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Agent Session schemas (Phase 1 — Cloud Agentic Live Session)
# ---------------------------------------------------------------------------


class SessionCreateIn(BaseModel):
    """
    Request body for POST /sessions.

    branch_name defaults to repo.default_branch server-side if omitted.
    title defaults to "Pairing Session" if omitted.
    extra="forbid" prevents mass-assignment injection.
    """

    repo_id: uuid.UUID
    branch_name: Optional[str] = Field(None, max_length=255)
    title: Optional[str] = Field(None, max_length=255)

    model_config = {"extra": "forbid"}


class PlanTask(BaseModel):
    """A single step in the agent's live execution plan."""

    id: str = Field(..., min_length=1, max_length=100)
    title: str = Field(..., min_length=1, max_length=500)
    status: Literal["pending", "in_progress", "completed", "failed"]

    model_config = {"extra": "forbid"}


class ClarificationIn(BaseModel):
    """Request body for POST /sessions/{id}/clarify."""

    response: str = Field(..., min_length=1, max_length=2000)

    model_config = {"extra": "forbid"}


class CheckpointOut(BaseModel):
    """Single checkpoint snapshot metadata for the Time Machine UI."""

    checkpoint_id: str
    turn: int
    timestamp: str
    description: str
    files_count: int

    model_config = {"extra": "forbid"}


class SessionOut(BaseModel):
    """
    Full session DTO returned by all session endpoints.

    repo_owner and repo_name are populated server-side via join —
    never derived from client input.
    """

    id: uuid.UUID
    user_id: uuid.UUID
    repo_id: uuid.UUID
    repo_owner: str
    repo_name: str
    title: str
    status: str
    branch_name: str
    base_sha: str
    conversation_history: list[dict[str, Any]]
    staged_patches: dict[str, str]
    plan: list[dict[str, Any]] = Field(default_factory=list)
    waiting_input: Optional[dict[str, Any]] = None
    checkpoints: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SessionListOut(BaseModel):
    sessions: list[SessionOut]
    total: int


class SessionCloseIn(BaseModel):
    """
    Optional request body for POST /sessions/{session_id}/close.

    reason is informational only — stored nowhere in Phase 1.
    extra="forbid" prevents injection via unexpected fields.
    """

    reason: Optional[str] = Field(None, max_length=1000)

    model_config = {"extra": "forbid"}


class SessionChatIn(BaseModel):
    """
    Request body for POST /sessions/{session_id}/chat.

    message: The user's natural-language prompt to the pairing agent.
    extra="forbid" prevents mass-assignment injection.
    """

    message: str = Field(..., min_length=1, max_length=32_000)
    model: Optional[str] = Field(default=None, max_length=255)
    provider: Optional[str] = Field(default=None, max_length=100)

    model_config = {"extra": "forbid"}


class SandboxVerificationOut(BaseModel):
    """
    Response DTO for POST /sessions/{session_id}/verify.

    status:  One of "queued" | "running" | "passed" | "failed".
    passed:  True iff sandbox tests passed.
    run_url: Optional GitHub Actions workflow run URL.
    logs:    Optional truncated test output.
    """

    status: str
    passed: bool
    run_url: Optional[str] = None
    logs: Optional[str] = None


class SessionCommitIn(BaseModel):
    """
    Request body for POST /sessions/{session_id}/commit.

    title: PR title string.
    body: Optional PR description markdown.
    extra="forbid" prevents mass-assignment injection.
    """

    title: str = Field(..., min_length=1, max_length=255)
    body: str | None = Field(None, max_length=65535)

    model_config = {"extra": "forbid"}


class SessionCommitOut(BaseModel):
    """
    Response DTO for POST /sessions/{session_id}/commit.

    pr_url:    Direct GitHub URL to the newly opened pull request.
    pr_number: Pull request number in the repository.
    commit_sha: The SHA of the commit pushed to the session branch.
    """

    pr_url: str
    pr_number: int
    commit_sha: str


# ---------------------------------------------------------------------------
# RepoSettings schemas — Phase 6 (Operational Governance & Feature Toggles)
# ---------------------------------------------------------------------------


class RepoSettingsOut(BaseModel):
    id: Optional[uuid.UUID] = None
    repo_id: uuid.UUID
    preset: str = "autonomous"
    preset_profile: str = "autonomous"
    enable_auto_fix: bool = True
    enable_auto_fixer: bool = True
    enable_auditor_mode: bool = False
    enable_sandbox_verification: bool = True
    enable_ci_sandbox: bool = True
    enable_pr_comments: bool = True
    enable_live_sessions: bool = True
    enable_webcontainer_preview: bool = True
    enable_subagents: bool = True
    audit_trigger_on_pr: bool = True
    audit_trigger_on_ci_failure: bool = True
    audit_trigger_on_ci_success: bool = False
    audit_trigger_on_manual_mention: bool = True
    allowed_branches: list[str] = Field(default_factory=lambda: ["main", "master"])
    monitored_branches: list[str] = Field(default_factory=lambda: ["main", "master"])
    ignore_draft_prs: bool = True
    min_confidence_threshold: int = 80
    max_cost_per_run_cents: int = 100
    model_override_scope: str = "inherit"
    settings_version: int = 1
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True, extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def sync_aliases_before(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "preset" in data and "preset_profile" not in data:
                data["preset_profile"] = data["preset"]
            elif "preset_profile" in data and "preset" not in data:
                data["preset"] = data["preset_profile"]

            if "enable_auto_fix" in data and "enable_auto_fixer" not in data:
                data["enable_auto_fixer"] = data["enable_auto_fix"]
            elif "enable_auto_fixer" in data and "enable_auto_fix" not in data:
                data["enable_auto_fix"] = data["enable_auto_fixer"]

            if (
                "enable_sandbox_verification" in data
                and "enable_ci_sandbox" not in data
            ):
                data["enable_ci_sandbox"] = data["enable_sandbox_verification"]
            elif (
                "enable_ci_sandbox" in data
                and "enable_sandbox_verification" not in data
            ):
                data["enable_sandbox_verification"] = data["enable_ci_sandbox"]

            if "allowed_branches" in data and "monitored_branches" not in data:
                data["monitored_branches"] = data["allowed_branches"]
            elif "monitored_branches" in data and "allowed_branches" not in data:
                data["allowed_branches"] = data["monitored_branches"]
        return data


class RepoSettingsUpdate(BaseModel):
    preset: Optional[str] = None
    preset_profile: Optional[str] = None
    enable_auto_fix: Optional[bool] = None
    enable_auto_fixer: Optional[bool] = None
    enable_auditor_mode: Optional[bool] = None
    enable_sandbox_verification: Optional[bool] = None
    enable_ci_sandbox: Optional[bool] = None
    enable_pr_comments: Optional[bool] = None
    enable_live_sessions: Optional[bool] = None
    enable_webcontainer_preview: Optional[bool] = None
    enable_subagents: Optional[bool] = None
    audit_trigger_on_pr: Optional[bool] = None
    audit_trigger_on_ci_failure: Optional[bool] = None
    audit_trigger_on_ci_success: Optional[bool] = None
    audit_trigger_on_manual_mention: Optional[bool] = None
    allowed_branches: Optional[list[str]] = None
    monitored_branches: Optional[list[str]] = None
    ignore_draft_prs: Optional[bool] = None
    min_confidence_threshold: Optional[int] = None
    max_cost_per_run_cents: Optional[int] = None
    model_override_scope: Optional[str] = None

    # Nested structures for future02.md payload compatibility
    features: Optional[dict[str, bool]] = None
    audit_triggers: Optional[dict[str, bool]] = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def unpack_nested_and_validate(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        out = dict(data)
        # Unpack nested features if present
        if "features" in out and isinstance(out["features"], dict):
            feats = out["features"]
            if "auto_fixer" in feats:
                out["enable_auto_fix"] = feats["auto_fixer"]
            if "auto_fix" in feats:
                out["enable_auto_fix"] = feats["auto_fix"]
            if "auditor_mode" in feats:
                out["enable_auditor_mode"] = feats["auditor_mode"]
            if "ci_sandbox" in feats:
                out["enable_sandbox_verification"] = feats["ci_sandbox"]
            if "sandbox_verification" in feats:
                out["enable_sandbox_verification"] = feats["sandbox_verification"]
            if "live_sessions" in feats:
                out["enable_live_sessions"] = feats["live_sessions"]
            if "webcontainer_preview" in feats:
                out["enable_webcontainer_preview"] = feats["webcontainer_preview"]
            if "subagents" in feats:
                out["enable_subagents"] = feats["subagents"]
            if "pr_comments" in feats:
                out["enable_pr_comments"] = feats["pr_comments"]

        # Unpack nested audit_triggers if present
        if "audit_triggers" in out and isinstance(out["audit_triggers"], dict):
            trigs = out["audit_triggers"]
            if "on_pr" in trigs:
                out["audit_trigger_on_pr"] = trigs["on_pr"]
            if "on_ci_failure" in trigs:
                out["audit_trigger_on_ci_failure"] = trigs["on_ci_failure"]
            if "on_ci_success" in trigs:
                out["audit_trigger_on_ci_success"] = trigs["on_ci_success"]
            if "on_manual_mention" in trigs:
                out["audit_trigger_on_manual_mention"] = trigs["on_manual_mention"]

        # Sync aliases
        if "preset_profile" in out and out.get("preset") is None:
            out["preset"] = out["preset_profile"]
        if "enable_auto_fixer" in out and out.get("enable_auto_fix") is None:
            out["enable_auto_fix"] = out["enable_auto_fixer"]
        if (
            "enable_ci_sandbox" in out
            and out.get("enable_sandbox_verification") is None
        ):
            out["enable_sandbox_verification"] = out["enable_ci_sandbox"]
        if "monitored_branches" in out and out.get("allowed_branches") is None:
            out["allowed_branches"] = out["monitored_branches"]

        # Bounds validation
        if (
            "max_cost_per_run_cents" in out
            and out["max_cost_per_run_cents"] is not None
        ):
            cost = out["max_cost_per_run_cents"]
            if not isinstance(cost, int) or isinstance(cost, bool) or cost < 0:
                raise ValueError(
                    "max_cost_per_run_cents must be a non-negative integer."
                )

        if (
            "min_confidence_threshold" in out
            and out["min_confidence_threshold"] is not None
        ):
            thresh = out["min_confidence_threshold"]
            if (
                not isinstance(thresh, int)
                or isinstance(thresh, bool)
                or not (0 <= thresh <= 100)
            ):
                raise ValueError("min_confidence_threshold must be between 0 and 100.")

        if "preset" in out and out["preset"] is not None:
            from app.services.repo_settings import normalize_preset_name

            try:
                out["preset"] = normalize_preset_name(out["preset"])
            except ValueError as exc:
                raise ValueError(str(exc)) from exc

        if "allowed_branches" in out and out["allowed_branches"] is not None:
            from app.services.repo_settings import validate_branches

            try:
                out["allowed_branches"] = validate_branches(out["allowed_branches"])
            except ValueError as exc:
                raise ValueError(str(exc)) from exc

        return out


class PresetApplyIn(BaseModel):
    preset: str = Field(..., min_length=1, max_length=50)

    model_config = ConfigDict(extra="forbid")

    @field_validator("preset")
    @classmethod
    def validate_preset_field(cls, v: str) -> str:
        from app.services.repo_settings import normalize_preset_name

        return normalize_preset_name(v)


class RepoWithSettingsOut(BaseModel):
    repo_id: uuid.UUID
    repo_full_name: str
    preset_profile: str
    features: dict[str, bool]
    audit_triggers: dict[str, bool] = Field(
        ...,
        description="Audit triggers mapping: on_pr, on_ci_failure, on_ci_success, on_manual_mention",
    )
    monitored_branches: list[str]

    model_config = ConfigDict(from_attributes=True, extra="ignore")

    @field_validator("audit_triggers")
    @classmethod
    def ensure_audit_triggers(cls, v: dict[str, bool]) -> dict[str, bool]:
        if isinstance(v, dict) and "on_manual_mention" not in v:
            v["on_manual_mention"] = True
        return v
