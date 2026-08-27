# Better Auth tables (user/session/account/verification) are owned by Next.js/Better Auth, not Alembic — see WORK.md:8
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UUID, UniqueConstraint, Float
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Repo(Base):
    __tablename__ = "repos"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
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

    runs = relationship("Run", back_populates="repo", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("owner", "name", name="uq_repo_owner_name"),
    )


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"), index=True)
    github_run_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
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

    repo = relationship("Repo", back_populates="runs")
    run_steps = relationship("RunStep", back_populates="run", cascade="all, delete-orphan")
    attempts = relationship("Attempt", back_populates="run", cascade="all, delete-orphan")
    eval_result = relationship("EvalResult", back_populates="run", uselist=False, cascade="all, delete-orphan")


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

    run = relationship("Run", back_populates="run_steps")


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

    run = relationship("Run", back_populates="attempts")


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

    run = relationship("Run", back_populates="eval_result")
