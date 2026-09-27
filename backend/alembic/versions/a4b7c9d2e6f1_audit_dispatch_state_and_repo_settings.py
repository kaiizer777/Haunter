"""add operational repo settings and recoverable audit dispatch state

Revision ID: a4b7c9d2e6f1
Revises: f2a9c4e7b1d3
Create Date: 2026-09-25 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a4b7c9d2e6f1"
down_revision: Union[str, Sequence[str], None] = "f2a9c4e7b1d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "repos",
        sa.Column("auditor_github_install_id", sa.Integer(), nullable=True),
    )
    op.create_table(
        "repo_settings",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("repo_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "enable_auditor_mode",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column(
            "audit_trigger_on_pr",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column(
            "audit_trigger_on_ci_failure",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column(
            "audit_trigger_on_ci_success",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column(
            "audit_trigger_on_manual_mention",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["repo_id"], ["repos.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("repo_id"),
    )
    op.create_index("ix_repo_settings_repo_id", "repo_settings", ["repo_id"], unique=True)

    op.add_column(
        "audit_jobs",
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "audit_jobs",
        sa.Column(
            "dispatch_attempts",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "audit_jobs",
        sa.Column("next_attempt_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.execute(sa.text("UPDATE audit_jobs SET next_attempt_at = created_at"))
    op.alter_column(
        "audit_jobs",
        "next_attempt_at",
        existing_type=sa.TIMESTAMP(timezone=True),
        nullable=False,
    )
    op.add_column(
        "audit_jobs",
        sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "audit_jobs",
        sa.Column("last_error", sa.String(length=64), nullable=True),
    )
    op.drop_constraint("ck_audit_jobs_status", "audit_jobs", type_="check")
    op.create_check_constraint(
        "ck_audit_jobs_status",
        "audit_jobs",
        "status IN ('queued', 'dispatching', 'running', 'completed', 'failed')",
    )
    op.create_check_constraint(
        "ck_audit_jobs_attempts",
        "audit_jobs",
        "attempts >= 0",
    )
    op.create_check_constraint(
        "ck_audit_jobs_dispatch_attempts",
        "audit_jobs",
        "dispatch_attempts >= 0",
    )
    op.create_index(
        "ix_audit_jobs_dispatch_queue",
        "audit_jobs",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_audit_jobs_lease_expires_at",
        "audit_jobs",
        ["lease_expires_at"],
    )


def downgrade() -> None:
    conn = op.get_bind()
    if conn is None:
        return

    conn.execute(sa.text("LOCK TABLE repos, repo_settings, audit_jobs IN EXCLUSIVE MODE"))

    row = conn.execute(
        sa.text(
            "SELECT (SELECT COUNT(*) FROM repo_settings) + "
            "(SELECT COUNT(*) FROM audit_jobs) + "
            "(SELECT COUNT(*) FROM repos WHERE auditor_github_install_id IS NOT NULL) AS total"
        )
    ).scalar()
    if row:
        raise RuntimeError(
            "refusing to downgrade a4b7c9d2e6f1: database contains "
            f"{row} row(s) across repo_settings / audit_jobs / auditor repos. "
            "Dropping these tables and columns would destroy every repository's "
            "auditor trigger configuration (repo_settings) and all in-flight dispatch state with "
            "no recovery path. Roll forward, or export the affected rows first."
        )

    op.drop_index("ix_audit_jobs_lease_expires_at", table_name="audit_jobs")
    op.drop_index("ix_audit_jobs_dispatch_queue", table_name="audit_jobs")
    op.drop_constraint("ck_audit_jobs_dispatch_attempts", "audit_jobs", type_="check")
    op.drop_constraint("ck_audit_jobs_attempts", "audit_jobs", type_="check")
    op.drop_constraint("ck_audit_jobs_status", "audit_jobs", type_="check")

    # Requeue any dispatching rows before restoring the old constraint, which
    # does not include 'dispatching'. On a guarded-empty DB this is a no-op.
    op.execute(
        sa.text(
            "UPDATE audit_jobs SET status = 'queued' WHERE status = 'dispatching'"
        )
    )

    op.create_check_constraint(
        "ck_audit_jobs_status",
        "audit_jobs",
        "status IN ('queued', 'running', 'completed', 'failed')",
    )
    op.drop_column("audit_jobs", "last_error")
    op.drop_column("audit_jobs", "lease_expires_at")
    op.drop_column("audit_jobs", "next_attempt_at")
    op.drop_column("audit_jobs", "dispatch_attempts")
    op.drop_column("audit_jobs", "attempts")

    op.drop_index("ix_repo_settings_repo_id", table_name="repo_settings")
    op.drop_table("repo_settings")
    op.drop_column("repos", "auditor_github_install_id")

