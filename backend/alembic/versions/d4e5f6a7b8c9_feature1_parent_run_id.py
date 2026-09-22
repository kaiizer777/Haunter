"""feature1_parent_run_id

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-09-22 03:15:00.000000

Adds parent_run_id foreign key column to the runs table to support interactive
PR feedback loop lineages (Feature 1).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "parent_run_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index(
        op.f("ix_runs_parent_run_id"),
        "runs",
        ["parent_run_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_runs_parent_run_id"), table_name="runs")
    op.drop_column("runs", "parent_run_id")
