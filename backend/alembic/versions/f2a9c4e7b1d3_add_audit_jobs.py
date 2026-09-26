"""add durable audit job outbox

Revision ID: f2a9c4e7b1d3
Revises: e7f8a9b0c1d2
Create Date: 2026-09-25 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f2a9c4e7b1d3"
down_revision: Union[str, Sequence[str], None] = "e7f8a9b0c1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audit_jobs",
        sa.Column("audit_id", sa.String(length=32), nullable=False),
        sa.Column("repo_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("delivery_id", sa.String(length=128), nullable=False),
        sa.Column("audit_type", sa.String(length=32), nullable=False),
        sa.Column("ref", sa.String(length=255), nullable=True),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("head_sha", sa.String(length=40), nullable=True),
        sa.Column("workflow_run_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["repo_id"], ["repos.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("audit_id"),
        sa.UniqueConstraint("delivery_id", name="uq_audit_jobs_delivery_id"),
        sa.CheckConstraint(
            "audit_type IN ('pr_audit', 'ci_failure_audit', 'ci_success_audit', 'manual_audit')",
            name="ck_audit_jobs_type",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed')",
            name="ck_audit_jobs_status",
        ),
    )
    op.create_index("ix_audit_jobs_repo_id", "audit_jobs", ["repo_id"])
    op.create_index(
        "ix_audit_jobs_repo_status",
        "audit_jobs",
        ["repo_id", "status"],
    )
    op.create_index(
        "ix_audit_jobs_workflow_run_id",
        "audit_jobs",
        ["workflow_run_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_jobs_workflow_run_id", table_name="audit_jobs")
    op.drop_index("ix_audit_jobs_repo_status", table_name="audit_jobs")
    op.drop_index("ix_audit_jobs_repo_id", table_name="audit_jobs")
    op.drop_table("audit_jobs")
