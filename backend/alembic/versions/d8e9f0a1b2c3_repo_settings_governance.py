"""add repository operational governance settings and feature toggles

Revision ID: d8e9f0a1b2c3
Revises: c7a1b2c3d4e5
Create Date: 2026-09-26 12:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d8e9f0a1b2c3"
down_revision: Union[str, Sequence[str], None] = "c7a1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "repo_settings",
        sa.Column(
            "preset",
            sa.String(length=50),
            server_default="autonomous",
            nullable=False,
        ),
    )
    op.add_column(
        "repo_settings",
        sa.Column(
            "enable_auto_fix",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
    )
    op.add_column(
        "repo_settings",
        sa.Column(
            "enable_sandbox_verification",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
    )
    op.add_column(
        "repo_settings",
        sa.Column(
            "enable_pr_comments",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
    )
    op.add_column(
        "repo_settings",
        sa.Column(
            "enable_live_sessions",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
    )
    op.add_column(
        "repo_settings",
        sa.Column(
            "enable_webcontainer_preview",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
    )
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
            "allowed_branches",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[\"main\", \"master\"]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "repo_settings",
        sa.Column(
            "ignore_draft_prs",
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
    op.add_column(
        "repo_settings",
        sa.Column(
            "max_cost_per_run_cents",
            sa.Integer(),
            server_default="100",
            nullable=False,
        ),
    )
    op.add_column(
        "repo_settings",
        sa.Column(
            "model_override_scope",
            sa.String(length=50),
            server_default="inherit",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_repo_settings_confidence_bounds",
        "repo_settings",
        "min_confidence_threshold >= 0 AND min_confidence_threshold <= 100",
    )
    op.create_check_constraint(
        "ck_repo_settings_cost_bounds",
        "repo_settings",
        "max_cost_per_run_cents >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_repo_settings_cost_bounds", "repo_settings", type_="check")
    op.drop_constraint("ck_repo_settings_confidence_bounds", "repo_settings", type_="check")
    op.drop_column("repo_settings", "model_override_scope")
    op.drop_column("repo_settings", "max_cost_per_run_cents")
    op.drop_column("repo_settings", "min_confidence_threshold")
    op.drop_column("repo_settings", "ignore_draft_prs")
    op.drop_column("repo_settings", "allowed_branches")
    op.drop_column("repo_settings", "enable_subagents")
    op.drop_column("repo_settings", "enable_webcontainer_preview")
    op.drop_column("repo_settings", "enable_live_sessions")
    op.drop_column("repo_settings", "enable_pr_comments")
    op.drop_column("repo_settings", "enable_sandbox_verification")
    op.drop_column("repo_settings", "enable_auto_fix")
    op.drop_column("repo_settings", "preset")
