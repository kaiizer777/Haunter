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
    """Refuse: dropping `base_sha` re-opens the unpinned PR audit path.

    This migration is the point at which a stored PR audit stops being able to
    resolve which commits it was scheduled for, and a downgrade would silently
    discard that binding for every historical row. Rolling past this revision
    is a data-loss operation, so it is rejected with an explicit error rather
    than executed.
    """
    raise RuntimeError(
        "refusing to downgrade b6d8f0a2c4e6: dropping audit_jobs.base_sha would "
        "destroy the pinned comparison endpoints of existing audit jobs and "
        "re-enable unpinned PR audits. Roll forward, or manually resolve the "
        "affected rows before attempting a downgrade."
    )
