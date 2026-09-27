"""pin audit comparison endpoints and add verified no-diff outcome

Revision ID: b6d8f0a2c4e6
Revises: a4b7c9d2e6f1
Create Date: 2026-09-25 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op

revision: str = "b6d8f0a2c4e6"
down_revision: Union[str, Sequence[str], None] = "a4b7c9d2e6f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "audit_jobs",
        sa.Column("base_sha", sa.String(length=40), nullable=True),
    )
    op.drop_constraint("ck_audit_jobs_status", "audit_jobs", type_="check")
    op.create_check_constraint(
        "ck_audit_jobs_status",
        "audit_jobs",
        "status IN ('queued', 'dispatching', 'running', 'completed', 'failed', 'skipped_no_diff')",
    )


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "offline downgrade is not supported because database row counts "
            "cannot be verified offline"
        )

    conn = op.get_bind()
    if conn is None:
        raise RuntimeError(
            "Cannot obtain database connection for downgrade verification"
        )

    conn.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    conn.execute(sa.text("LOCK TABLE audit_jobs IN EXCLUSIVE MODE"))

    row = conn.execute(
        sa.text("SELECT COUNT(*) FROM audit_jobs")
    ).scalar()
    if row:
        raise RuntimeError(
            "refusing to downgrade b6d8f0a2c4e6: database contains "
            f"{row} audit job(s). "
            "Dropping audit_jobs.base_sha would destroy the pinned comparison "
            "endpoints of existing audit jobs and re-enable unpinned PR audits. "
            "Roll forward, or manually resolve the affected rows before attempting a downgrade."
        )

    # ck_audit_jobs_pr_endpoints (added by c7a1b2c3d4e5) references base_sha;
    # PostgreSQL would drop it implicitly via DROP COLUMN cascade. Drop IF EXISTS
    # so a direct downgrade from b6d8f0a2c4e6 does not fail when c7 was never applied.
    op.execute(
        sa.text(
            "ALTER TABLE audit_jobs DROP CONSTRAINT IF EXISTS ck_audit_jobs_pr_endpoints"
        )
    )

    op.drop_column("audit_jobs", "base_sha")

    # skipped_no_diff is not in the pre-b6d8f0a2c4e6 allowed set; map those
    # terminal rows to completed before restoring the old constraint.
    op.execute(
        sa.text(
            "UPDATE audit_jobs SET status = 'completed' WHERE status = 'skipped_no_diff'"
        )
    )

    op.drop_constraint("ck_audit_jobs_status", "audit_jobs", type_="check")
    op.create_check_constraint(
        "ck_audit_jobs_status",
        "audit_jobs",
        "status IN ('queued', 'dispatching', 'running', 'completed', 'failed')",
    )
