"""pin audit comparison endpoints and add verified no-diff outcome

Revision ID: b6d8f0a2c4e6
Revises: a4b7c9d2e6f1
Create Date: 2026-09-25 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

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
    # ck_audit_jobs_pr_endpoints (added by c7a1b2c3d4e5) references base_sha;
    # PostgreSQL would drop it implicitly via DROP COLUMN cascade. Make the
    # removal explicit so the intent is clear and auditable.
    op.drop_constraint("ck_audit_jobs_pr_endpoints", "audit_jobs", type_="check")

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
