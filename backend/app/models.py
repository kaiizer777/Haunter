import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UUID,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class UserRole(StrEnum):
    USER = "user"
    ADMIN = "admin"


class User(Base):
    """
    FastAPI-owned user table — populated/upserted by the GitHub OAuth callback.

    Design note: we use a SEPARATE GitHub OAuth App (read:user scope only) for login.
    The GitHub App used for repo installation and webhooks (Phase 3+) is a distinct
    credential with repo/admin scope so that login never requests destructive permissions.
    """

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'admin')", name="check_user_role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    github_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, nullable=False, index=True
    )
    github_username: Mapped[str] = mapped_column(String(255), nullable=False)
    avatar_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Stores the GitHub OAuth access token encrypted at rest with Fernet.
    # TOKEN_ENCRYPTION_KEY is required at startup (config.py raises RuntimeError if unset).
    # Encryption helpers: app/auth.py (_encrypt_token / _decrypt_token).
    access_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    role: Mapped[str] = mapped_column(
        String(32),
        default=UserRole.USER.value,
        server_default=UserRole.USER.value,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    repos: Mapped[list["Repo"]] = relationship(
        "Repo", back_populates="user", cascade="all, delete-orphan"
    )
    agent_sessions: Mapped[list["AgentSession"]] = relationship(
        "AgentSession", back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN


class Repo(Base):
    __tablename__ = "repos"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Multi-tenant ownership: every repo is scoped to a user.
    # Two users can independently track the same public repo — unique constraint is (user_id, owner, name).
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    default_branch: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    language_hint: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    github_install_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    auditor_github_install_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    active_model_config_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("model_configs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    user: Mapped["User"] = relationship("User", back_populates="repos")
    runs: Mapped[list["Run"]] = relationship(
        "Run", back_populates="repo", cascade="all, delete-orphan"
    )
    code_reviews: Mapped[list["CodeReview"]] = relationship(
        "CodeReview", back_populates="repo", cascade="all, delete-orphan"
    )
    audit_jobs: Mapped[list["AuditJob"]] = relationship(
        "AuditJob", back_populates="repo", cascade="all, delete-orphan"
    )
    settings: Mapped[Optional["RepoSettings"]] = relationship(
        "RepoSettings",
        back_populates="repo",
        cascade="all, delete-orphan",
        uselist=False,
    )
    agent_sessions: Mapped[list["AgentSession"]] = relationship(
        "AgentSession", back_populates="repo", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("user_id", "owner", "name", name="uq_repo_user_owner_name"),
    )


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    repo_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repos.id", ondelete="CASCADE"), index=True
    )
    github_run_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    github_delivery_id: Mapped[Optional[str]] = mapped_column(
        String(255), unique=True, nullable=True, index=True
    )
    head_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    head_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    conclusion: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    # Distilled root-cause summary written by the Context Gatherer subagent (Phase 5).
    # Only the redacted, token-bounded summary is stored — never raw CI logs or diffs.
    diagnosis_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Phase 8 — PR Writer results.
    # pr_url/pr_number: set when a PR is opened successfully (pr_opened status).
    # pr_branch: the haunter/fix-* branch created server-side, never from LLM.
    # final_summary: first 1000 chars of LLM-generated PR body — plain text only,
    #   html.escape'd before storage to prevent stored XSS when rendered.
    pr_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pr_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pr_branch: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    final_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Phase 15 — short, redacted reason a run ended in status=error or fallback.
    # Written by the orchestrator on every error path (context gatherer timeout,
    # fix generator rejection, PR writer failure, outer exception). Truncated to
    # 500 chars before storage. Surfaced on the run detail page so the user can
    # see *why* a run failed, not just that it did.
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Feature 1 — Interactive PR Feedback Loop lineage
    parent_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=True, index=True
    )

    # Feature 3 — Fallback to GitHub Issue lineage.
    # fallback_issue_url / fallback_issue_number: set when the orchestrator
    # files a tracking issue on the fix-exhaust path (repo setting
    # file_issue_on_fallback). Both stay None for runs that succeeded, are
    # still running, or whose repo opted out. Surfaced on the run detail page.
    fallback_issue_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    fallback_issue_number: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )

    repo: Mapped["Repo"] = relationship("Repo", back_populates="runs")
    parent_run: Mapped[Optional["Run"]] = relationship(
        "Run", remote_side=lambda: [Run.id], back_populates="children_runs"
    )
    children_runs: Mapped[list["Run"]] = relationship(
        "Run", back_populates="parent_run", cascade="all, delete-orphan"
    )
    run_steps: Mapped[list["RunStep"]] = relationship(
        "RunStep", back_populates="run", cascade="all, delete-orphan"
    )
    attempts: Mapped[list["Attempt"]] = relationship(
        "Attempt", back_populates="run", cascade="all, delete-orphan"
    )
    eval_result: Mapped[Optional["EvalResult"]] = relationship(
        "EvalResult", back_populates="run", uselist=False, cascade="all, delete-orphan"
    )


class RunStep(Base):
    __tablename__ = "run_steps"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    step_name: Mapped[str] = mapped_column(String(255), nullable=False)
    input_tokens: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    output_tokens: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    cost_estimate: Mapped[Optional[float]] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    run: Mapped["Run"] = relationship("Run", back_populates="run_steps")


class Attempt(Base):
    __tablename__ = "attempts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    patch_text: Mapped[str] = mapped_column(Text, nullable=False)
    confidence_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    strategy_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    verification_status: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True
    )
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    build_duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    run: Mapped["Run"] = relationship("Run", back_populates="attempts")

    __table_args__ = (
        UniqueConstraint("run_id", "attempt_number", name="uq_attempt_run_number"),
    )


class ModelConfigScope(StrEnum):
    GLOBAL = "global"
    REPO = "repo"


class ModelConfig(Base):
    """
    LLM model configuration with per-repo vs global isolation (Phase 3.1).

    - scope='global': platform-wide default. Only these rows are deactivated
      by PUT /config/model global updates and returned by global lookups.
    - scope='repo': per-repo override, optionally pinned to repo_id/user_id.
      Global updates never touch these rows; repo resolution prefers them
      and falls back to the global active row.
    """

    __tablename__ = "model_configs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    provider: Mapped[str] = mapped_column(String(255), nullable=False)
    model_name: Mapped[str] = mapped_column(
        String(255), nullable=False, default="nemotron-3.5-lightning-free"
    )
    base_url: Mapped[str] = mapped_column(
        String(255), nullable=False, default="https://opencode.ai/zen/v1"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    scope: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ModelConfigScope.GLOBAL.value,
        server_default="global",
    )
    repo_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repos.id", ondelete="SET NULL"), nullable=True
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        Index("ix_model_configs_scope", "scope"),
        Index("ix_model_configs_repo_id", "repo_id"),
        Index("ix_model_configs_user_id", "user_id"),
    )


class EvalResult(Base):
    __tablename__ = "eval_results"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), unique=True
    )
    overall_accuracy: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    per_subagent_scores: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB, nullable=True
    )
    model_config_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("model_configs.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    run: Mapped[Optional["Run"]] = relationship("Run", back_populates="eval_result")


class SystemConfig(Base):
    """
    Key-value store for hot-switchable system configuration.

    Used by the hosting adapter (Phase 14) to allow HOSTING_PROVIDER and
    SANDBOX_PROVIDER to be switched without redeploy via PUT /config/hosting.
    Values are read per-request with a 60s in-process TTL cache.

    Security:
    - Keys are allowlisted at the application layer (router validates against
      _ALLOWED_SYSTEM_CONFIG_KEYS before writing).
    - Values are allowlisted via Pydantic (AllowedHostingProvider Literal)
      before write — no free-text injection.
    - Table is admin-gated (same get_current_user + ADMIN_USER_ID check as
      PUT /config/model).
    """

    __tablename__ = "system_configs"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class CodeReview(Base):
    """
    Autonomous push-level code review and actionable remediation record.
    Tracks risk scores (0-100), structured AST/diff findings, and GitHub review/comment delivery.
    """

    __tablename__ = "code_reviews"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    repo_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repos.id", ondelete="CASCADE"), index=True
    )
    commit_sha: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    pr_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    risk_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    findings: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="completed")
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    repo: Mapped["Repo"] = relationship("Repo", back_populates="code_reviews")


class RepoSettings(Base):
    __tablename__ = "repo_settings"
    __table_args__ = (
        UniqueConstraint("repo_id", name="repo_settings_repo_id_key"),
        Index("ix_repo_settings_repo_id", "repo_id", unique=True),
        CheckConstraint(
            "max_cost_per_run_cents >= 0",
            name="ck_repo_settings_cost_bounds",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    repo_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repos.id", ondelete="CASCADE"), nullable=False
    )
    preset: Mapped[str] = mapped_column(
        String(50), nullable=False, default="autonomous", server_default="autonomous"
    )
    enable_auto_fix: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    enable_auditor_mode: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    enable_sandbox_verification: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    enable_pr_comments: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    enable_live_sessions: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    enable_webcontainer_preview: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    # Feature 3 — opt-in flag gating creation of a GitHub tracking issue on
    # the fix-exhaust path. Independent of enable_pr_comments: the diagnosis
    # comment and the issue are two channels of the same fallback, and either
    # can be switched off without affecting the other. Defaults to enabled —
    # an exhausted run with no diagnosis anywhere is the case this exists for.
    file_issue_on_fallback: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    audit_trigger_on_pr: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    audit_trigger_on_ci_failure: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    audit_trigger_on_ci_success: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    audit_trigger_on_manual_mention: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    allowed_branches: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=lambda: ["main", "master"],
        server_default='["main", "master"]',
    )
    ignore_draft_prs: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    max_cost_per_run_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100, server_default="100"
    )
    model_override_scope: Mapped[str] = mapped_column(
        String(50), nullable=False, default="inherit", server_default="inherit"
    )

    # Monorepo scope for the sandbox verification runner. Both are optional and
    # NULL means "no override" — the language detected from the repository tree
    # runs at the repo root with that language's default test command.
    #   working_dir:  repo-relative POSIX directory, e.g. "packages/api".
    #   test_command: shell command replacing the language-default test step.
    # Validated by app.services.repo_settings.validate_working_dir /
    # validate_test_command on every write; NULL/empty clears the override.
    working_dir: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    test_command: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Monotonic counter bumped whenever trigger semantics change. Persisted on
    # each audit job so a replayed delivery under changed settings is detected
    # as a payload conflict instead of silently deduplicating.
    settings_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    repo: Mapped["Repo"] = relationship("Repo", back_populates="settings")

    @property
    def preset_profile(self) -> str:
        return self.preset

    @preset_profile.setter
    def preset_profile(self, val: str) -> None:
        self.preset = val

    @property
    def enable_auto_fixer(self) -> bool:
        return self.enable_auto_fix

    @enable_auto_fixer.setter
    def enable_auto_fixer(self, val: bool) -> None:
        self.enable_auto_fix = val

    @property
    def enable_ci_sandbox(self) -> bool:
        return self.enable_sandbox_verification

    @enable_ci_sandbox.setter
    def enable_ci_sandbox(self, val: bool) -> None:
        self.enable_sandbox_verification = val

    @property
    def monitored_branches(self) -> list[str]:
        return self.allowed_branches

    @monitored_branches.setter
    def monitored_branches(self, val: list[str]) -> None:
        self.allowed_branches = val


class AuditJob(Base):
    __tablename__ = "audit_jobs"

    audit_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    repo_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    delivery_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # SHA-256 over the canonical audit payload. A replayed delivery_id whose
    # payload does not hash to this value is a conflict, not a duplicate.
    delivery_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=""
    )
    settings_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    audit_type: Mapped[str] = mapped_column(String(32), nullable=False)
    ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    pr_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    base_sha: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    head_sha: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    workflow_run_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued", server_default="queued"
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    dispatch_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # HMAC fence for the current dispatch attempt. Present only while the job
    # is in `dispatching`; cleared on release, recovery, or processing claim.
    dispatch_fence_token: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    last_error: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    repo: Mapped["Repo"] = relationship("Repo", back_populates="audit_jobs")

    __table_args__ = (
        UniqueConstraint("delivery_id", name="uq_audit_jobs_delivery_id"),
        CheckConstraint(
            "audit_type IN ('pr_audit', 'ci_failure_audit', 'ci_success_audit', 'manual_audit')",
            name="ck_audit_jobs_type",
        ),
        CheckConstraint(
            "status IN ('queued', 'dispatching', 'running', 'completed', 'failed', 'skipped_no_diff')",
            name="ck_audit_jobs_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_audit_jobs_attempts"),
        CheckConstraint(
            "dispatch_attempts >= 0", name="ck_audit_jobs_dispatch_attempts"
        ),
        CheckConstraint("settings_version >= 1", name="ck_audit_jobs_settings_version"),
        CheckConstraint(
            "length(delivery_fingerprint) = 64",
            name="ck_audit_jobs_delivery_fingerprint",
        ),
        CheckConstraint(
            "status NOT IN ('queued', 'dispatching', 'running') "
            "OR pr_number IS NULL "
            "OR (base_sha IS NOT NULL AND head_sha IS NOT NULL)",
            name="ck_audit_jobs_pr_endpoints",
        ),
        CheckConstraint(
            "status <> 'dispatching' OR dispatch_fence_token IS NOT NULL",
            name="ck_audit_jobs_dispatch_fence",
        ),
        Index("ix_audit_jobs_repo_status", "repo_id", "status"),
        Index("ix_audit_jobs_dispatch_queue", "status", "next_attempt_at"),
        Index("ix_audit_jobs_lease_expires_at", "lease_expires_at"),
        Index("ix_audit_jobs_workflow_run_id", "workflow_run_id"),
    )


class AgentSession(Base):
    """
    Cloud Agentic Live Session — persistent pairing session between a user and a repo.

    Tracks conversation history, staged patches, and the pinned base SHA used
    for all file fetches and diff applications within the session.

    Lifecycle: active → completed | closed.
    Concurrency: at most 2 active sessions per user (enforced at API layer).
    """

    __tablename__ = "agent_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    repo_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(
        String(255), nullable=False, default="Pairing Session"
    )
    # Allowed statuses: "active" | "completed" | "closed"
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="active")
    branch_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Pinned commit SHA — all file fetches and diffs within this session reference this SHA.
    base_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    # Append-only list of {role, content, timestamp} dicts.
    conversation_history: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    # Mapping of file_path -> unified patch string for staged (uncommitted) changes.
    staged_patches: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    # Phase 6 — Interactive Planning & Clarification
    # Live multi-step task checklist: list of {id, title, status} dicts
    plan: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="'[]'::jsonb"
    )
    # Active clarification request when status="awaiting_clarification"
    waiting_input: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, default=None
    )
    # Phase 7 — Session Time Machine: turn-by-turn snapshots for instant rollback.
    # Capped at 20 entries (oldest dropped when limit reached).
    # Schema: [{checkpoint_id, turn, timestamp, description, staged_patches, history_length}]
    checkpoints: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="'[]'::jsonb"
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    user: Mapped["User"] = relationship("User", back_populates="agent_sessions")
    repo: Mapped["Repo"] = relationship("Repo", back_populates="agent_sessions")

    __table_args__ = (
        # Composite index for fast active-session lookups per user.
        # Column order: equality first (user_id), then status.
        Index("ix_agent_sessions_user_id_status", "user_id", "status"),
    )


class WebhookDelivery(Base):
    """
    Feature 8 — Webhook health log.

    Record of the webhook decisions that reached a registered repository,
    written best-effort via _record_webhook_delivery() alongside the structured
    _log_webhook_decision() log line — a logging failure must never break
    webhook ingestion, and a DB failure must never break the 2xx response. The
    log-only rejections that happen before a repository is resolved write no
    row: there is no repo to attribute them to, and `repo_id` is the tenant
    boundary the read endpoint scopes on.

    Replay (POST /webhooks/deliveries/{id}/replay) re-drives the ORIGINAL raw
    payload back through the normal `github_webhook` path — signature
    verification, registration guards, branch guards, trigger evaluation and
    the idempotency constraints all run again, so a replay reaches the same
    decision the live path would reach today. `replay_of` links the row the
    re-run appended back to the row that was replayed.

    `payload` is the base64 of the exact bytes HMAC verification passed over.
    It is the replay buffer: without it the decision cannot be re-evaluated,
    so oversized deliveries are recorded with payload=NULL and report as not
    replayable rather than silently replaying a truncated body. The signature
    header itself is NEVER persisted — only the verified body, which contains
    no credentials.
    """

    __tablename__ = "webhook_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    delivery_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Display-only denormalization of Repo.owner/Repo.name. Never an authz
    # input: repos allows several users to register the same owner/name
    # (uq_repo_user_owner_name is scoped per user), so matching on this string
    # would let one tenant read another tenant's deliveries.
    repo: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    repo_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("repos.id", ondelete="SET NULL"), nullable=True
    )
    # Base64 of the raw body HMAC verification passed over. Replay buffer —
    # never returned by any response model.
    payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Set on the row appended by a replay, pointing at the replayed row.
    replay_of: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("webhook_deliveries.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        Index("ix_webhook_deliveries_delivery_id", "delivery_id"),
        Index("ix_webhook_deliveries_created_at", "created_at"),
        Index("ix_webhook_deliveries_repo_id", "repo_id"),
        # Composite (repo_id, created_at) so the health list stays an index
        # scan under the tenant scope + created_at DESC ordering it actually
        # uses; the single-column repo_id index above still serves FK cascades.
        Index("ix_webhook_deliveries_repo_created", "repo_id", "created_at"),
        Index("ix_webhook_deliveries_replay_of", "replay_of"),
    )
