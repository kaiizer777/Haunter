import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, String, Text, UUID, UniqueConstraint, Float
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    """
    FastAPI-owned user table — populated/upserted by the GitHub OAuth callback.

    Design note: we use a SEPARATE GitHub OAuth App (read:user scope only) for login.
    The GitHub App used for repo installation and webhooks (Phase 3+) is a distinct
    credential with repo/admin scope so that login never requests destructive permissions.
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    github_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    github_username: Mapped[str] = mapped_column(String(255), nullable=False)
    avatar_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Stores the GitHub OAuth access token (read:user scope only).
    # TODO(security): encrypt access_token at rest with Fernet before prod.
    # Encryption helpers live in app/auth.py (_encrypt_token / _decrypt_token).
    # TOKEN_ENCRYPTION_KEY env var must be set — see config.py for startup warning.
    access_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    repos: Mapped[list["Repo"]] = relationship("Repo", back_populates="user", cascade="all, delete-orphan")


class Repo(Base):
    __tablename__ = "repos"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
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
    active_model_config_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("model_configs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    user: Mapped["User"] = relationship("User", back_populates="repos")
    runs: Mapped[list["Run"]] = relationship("Run", back_populates="repo", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("user_id", "owner", "name", name="uq_repo_user_owner_name"),
    )


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"), index=True)
    github_run_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    github_delivery_id: Mapped[Optional[str]] = mapped_column(String(255), unique=True, nullable=True, index=True)
    head_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    head_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    conclusion: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
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

    repo: Mapped["Repo"] = relationship("Repo", back_populates="runs")
    run_steps: Mapped[list["RunStep"]] = relationship("RunStep", back_populates="run", cascade="all, delete-orphan")
    attempts: Mapped[list["Attempt"]] = relationship("Attempt", back_populates="run", cascade="all, delete-orphan")
    eval_result: Mapped[Optional["EvalResult"]] = relationship("EvalResult", back_populates="run", uselist=False, cascade="all, delete-orphan")


class RunStep(Base):
    __tablename__ = "run_steps"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
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

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    patch_text: Mapped[str] = mapped_column(Text, nullable=False)
    confidence_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    strategy_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    verification_status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
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
    __tablename__ = "model_configs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(255), nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False, default="nemotron-3.5-lightning-free")
    base_url: Mapped[str] = mapped_column(String(255), nullable=False, default="https://opencode.ai/zen/v1")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class EvalResult(Base):
    __tablename__ = "eval_results"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"), unique=True)
    overall_accuracy: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    per_subagent_scores: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    model_config_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("model_configs.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    run: Mapped[Optional["Run"]] = relationship("Run", back_populates="eval_result")
