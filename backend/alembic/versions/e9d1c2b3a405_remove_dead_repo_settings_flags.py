"""remove dead repo settings flags enable_subagents and min_confidence_threshold

Revision ID: e9d1c2b3a405
Revises: d8e9f0a1b2c3
Create Date: 2026-09-28 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e9d1c2b3a405"
down_revision: Union[str, Sequence[str], None] = "d8e9f0a1b2c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_repo_settings_confidence_bounds", "repo_settings", type_="check"
    )
    op.drop_column("repo_settings", "min_confidence_threshold")
    op.drop_column("repo_settings", "enable_subagents")


def downgrade() -> None:
    op.add_column(
        "repo_settings",
        sa.Column(
            "enable_subagents",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
    )
    op.add_column(
        "repo_settings",
        sa.Column(
            "min_confidence_threshold",
            sa.Integer(),
            server_default="80",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_repo_settings_confidence_bounds",
        "repo_settings",
        "min_confidence_threshold >= 0 AND min_confidence_threshold <= 100",
    )
