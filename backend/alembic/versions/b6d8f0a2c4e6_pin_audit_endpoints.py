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
    op.drop_constraint("ck_audit_jobs_status", "audit_jobs", type_="check")
    op.create_check_constraint(
        "ck_audit_jobs_status",
        "audit_jobs",
        "status IN ('queued', 'dispatching', 'running', 'completed', 'failed')",
    )
    op.drop_column("audit_jobs", "base_sha")
