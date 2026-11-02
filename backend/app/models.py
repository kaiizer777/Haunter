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

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN.value or self.role == UserRole.ADMIN


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
    # GitHub Actions workflow_run id. NULL for conversational follow-up runs
    # triggered by an issue_comment / pull_request_review_comment — those carry
    # `trigger_comment_id` instead. Also NULL for one-click retry children
    # (routers/traces.py POST /runs/{id}/retry): they inherit the failure
    # coordinates of their source run and correspond to no workflow run of
    # their own, so the effective id is resolved by walking parent_run_id to
    # the root. Storing a fabricated id would squat on the real GitHub id
    # space and cause a genuine workflow_run delivery to be silently discarded
    # as a duplicate. Kept as its own UNIQUE index because it is the
    # idempotency key for autonomous CI-failure deliveries; overloading it
    # with comment ids would put two unrelated GitHub id namespaces (and two
    # unrelated uniqueness domains) in one column.
    github_run_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, unique=True, index=True, nullable=True
    )
    # GitHub issue_comment / pull_request_review_comment id that carried the
    # `@haunter` command. UNIQUE so a re-delivered comment (or a duplicated
    # comment across GitHub's two comment id spaces) can never spawn a second
    # agent run. NULL for autonomous workflow_run runs.
    trigger_comment_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, unique=True, index=True, nullable=True
    )
    # Review thread the follow-up must be answered in. GitHub's
    # `POST /pulls/{n}/comments/{comment_id}/replies` only accepts the
    # *top-level* review comment id, so a command posted as a reply to an
    # earlier comment (e.g. to Haunter's own) has to address the ancestor.
    # Deliberately NOT unique: several commands in one thread share a root,
    # and uniqueness there would drop every one after the first as a duplicate.
    reply_to_comment_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
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

    # Feature 1 — one-click retry lineage kind.
    #
    # parent_run_id alone is NOT a sufficient discriminator between the two
    # kinds of child run, because the two take opposite branches downstream:
    #
    #   * Interactive PR refinement (webhooks.py) sets pr_number/pr_branch and
    #     expects the patch to be committed back onto the existing PR branch.
    #   * One-click retry (routers/traces.py) has no PR at all and MUST open a
    #     fresh haunter/fix-* PR via the normal PR-Writer path.
    #
    # If a retry child were treated as a refinement child, orchestrator.py
    # would resolve target_branch = run.pr_branch or run.head_branch — i.e.
    # the user's own branch — and push the patch straight to it, violating the
    # Human Merge Gate Invariant (HAUNTER.md §1.1: never push to a protected
    # branch; the human is the sole merge authority).
    #
    # NULL/False => legacy refinement child (pre-dates this column).
    # True        => one-click retry child.
    is_retry_child: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
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

    __table_args__ = (
        # Issue #110: composite for the traces list query
        # (routers/traces.py: filters repo_id + status, range on created_at,
        # ORDER BY created_at DESC). Equality columns first, range/order last.
        Index("ix_runs_repo_status_created", "repo_id", "status", "created_at"),
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


class ModelConfig(Base):
    """LLM model configuration (global)."""

    __tablename__ = "model_configs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    provider: Mapped[str] = mapped_column(String(255), nullable=False)
    model_name: Mapped[str] = mapped_column(
        String(255), nullable=False, default="nemotron-3.5-lightning-free"
    )
    base_url: Mapped[str] = mapped_column(
        String(255), nullable=False, default="https://opencode.ai/inference/go/openai/v1"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = ()


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
    # Issue #131: snapshot of the team review preferences that shaped this
    # review - {prefs, settings_version, truncated}. Nullable so rows written
    # before the c2d3e4f5a6b7 migration stay valid; populated by the review
    # orchestrator at analyze time.
    applied_review_prefs: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB, nullable=True, default=None
    )
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    repo: Mapped["Repo"] = relationship("Repo", back_populates="code_reviews")

    __table_args__ = (
        # Issue #110: composite for the review dedup lookup
        # (webhooks.py: repo_id + commit_sha + pr_number, all equality).
        Index("ix_code_reviews_repo_commit_pr", "repo_id", "commit_sha", "pr_number"),
    )


class RepoSettings(Base):
    __tablename__ = "repo_settings"
    __table_args__ = (
        UniqueConstraint("repo_id", name="repo_settings_repo_id_key"),
        Index("ix_repo_settings_repo_id", "repo_id", unique=True),
        CheckConstraint(
            "max_cost_per_run_cents >= 0",
            name="ck_repo_settings_cost_bounds",
        ),
        CheckConstraint(
            "review_strictness IN ('low', 'medium', 'high')",
            name="ck_repo_settings_review_strictness",
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
    # Monorepo scope for the sandbox verification runner. Both are optional and
    # NULL means "no override" — the language detected from the repository tree
    # runs at the repo root with that language's default test command.
    #   working_dir:  repo-relative POSIX directory, e.g. "packages/api".
    #   test_command: shell command replacing the language-default test step.
    # Validated by app.services.repo_settings.validate_working_dir /
    # validate_test_command on every write; NULL/empty clears the override.
    working_dir: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    test_command: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Issue #131: configurable team review preferences. Source of truth for
    # the PR review bot; injected into the review prompt at analyze time and
    # snapshotted per review onto CodeReview.applied_review_prefs.
    review_guidelines_text: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    review_strictness: Mapped[str] = mapped_column(
        String(20), nullable=False, default="medium", server_default="medium"
    )
    review_focus_areas: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="'[]'::jsonb"
    )
    review_checklist: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="'[]'::jsonb"
    )

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

    An AMBIUOUS registration is the one pre-resolution decision that does write a
    row, and it writes it with `repo_id = NULL`: `repos` is unique per
    (user_id, owner, name), so several tenants may register the same owner/name
    and a payload carrying no tenant cannot be attributed to one of them.
    Attributing the row to any single match would both invent that tenancy and
    place the decision in that tenant's history, so the row stays unattributed
    and operator-facing. See _resolve_repo_for_delivery in app/webhooks.py.

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
        # DB-level backstop for the replay buffer. The general path refuses to
        # encode a body over _WEBHOOK_PAYLOAD_MAX_BYTES (128 KiB -> 174,764
        # base64 chars), while the auditor ack paths lift the cap to the 2 MiB
        # ingestion limit (MAX_PAYLOAD_SIZE_BYTES -> 2,796,204 base64 chars) so
        # an accepted audit is never recorded unreplayable. That guarantee lived
        # in exactly one Python `if`; without this constraint a future refactor
        # of _encode_replay_buffer could write an arbitrarily large body into an
        # unbounded Text column and Postgres would accept up to ~1 GB. Mirrors
        # migration d5e6f7a8b9c0.
        #
        # octet_length, not char_length: the bound is on BYTES stored, which is
        # what the storage cost is. The explicit `payload IS NULL` arm is
        # redundant — a CHECK passes on NULL — and is kept so the allowance for
        # swept/oversized rows is legible at the point the rule is declared.
        CheckConstraint(
            "payload IS NULL OR octet_length(payload) <= 2796204",
            name="ck_webhook_deliveries_payload_octet_length",
        ),
        # Issue #110: composite for the health list with event filter
        # (webhooks.py list_webhook_deliveries: repo_id + event equality,
        # created_at DESC ordering). Equality columns first, order column last.
        Index(
            "ix_webhook_deliveries_repo_event_created",
            "repo_id",
            "event",
            "created_at",
        ),
    )


#: Single authoritative ceiling on the length of a webhook rate limit bucket key.
#: Matches the VARCHAR column size on WebhookRateLimitHit.bucket_key across the
#: database migration, SQLAlchemy model, and the rate limiting service.
MAX_BUCKET_KEY_LENGTH: int = 200
BUCKET_KEY_MAX_LENGTH: int = MAX_BUCKET_KEY_LENGTH


class WebhookRateLimitHit(Base):
    """
    Shared sliding-window buckets for webhook ingress throttling (issue #161).

    One row per accepted delivery, keyed by the same `repo-full-name|sender-login`
    bucket key the in-memory limiter used. The in-window count for a bucket is
    the number of its rows newer than `now - window`, so the budget is a count
    over rows rather than a per-process counter — which is the point: a row is
    visible to every process that can see the database, so every Lambda
    execution environment spends from one budget instead of each enforcing its
    own copy.

    Deliberately NOT a foreign-key'd, tenant-scoped table. `bucket_key` is derived
    best-effort from an attacker-supplied payload (see `_webhook_rate_limit_key`
    in app/webhooks.py) and is frequently `unknown-repo|unknown-sender` for
    deliveries that never resolve to a tenant, so it is an opaque throttle key
    and nothing else. No row written here is ever returned by any endpoint.

    Rows are reclaimed by time, not by a hard cap: see _POSTGRES_STORE_BOUND_NOTE
    in app/services/rate_limit.py for why evicting live buckets would be worse
    than letting the row count float with the number of active keys.

    `hit_at` is a wall-clock timestamp because these rows are shared. A monotonic
    clock would not be comparable between processes, and the in-memory backend
    uses one only because its buckets never leave the process that wrote them.
    """

    __tablename__ = "webhook_rate_limit_hits"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # MAX_BUCKET_KEY_LENGTH chars bounds the key across all stores and migrations.
    # ensuring that no code path can exceed this column width.
    # Postgres VARCHAR counts characters and Python len() counts code points, so the two agree.
    bucket_key: Mapped[str] = mapped_column(
        String(MAX_BUCKET_KEY_LENGTH), nullable=False
    )
    hit_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        # Every per-bucket statement is `WHERE bucket_key = :key` plus a
        # hit_at range (prune) or a bare min()/count(): equality column first,
        # range column second, so all three are index lookups.
        Index("ix_webhook_rate_limit_hits_key_hit_at", "bucket_key", "hit_at"),
        # Separate, and deliberately so: the batched global prune filters on
        # hit_at ALONE (`WHERE hit_at <= :cutoff LIMIT n`) to reclaim buckets
        # that will never post again. That predicate has no leading bucket_key,
        # so the composite above cannot serve it and a second index is the
        # difference between a bounded index scan and a seq scan of the whole
        # table on the hot path.
        Index("ix_webhook_rate_limit_hits_hit_at", "hit_at"),
    )
