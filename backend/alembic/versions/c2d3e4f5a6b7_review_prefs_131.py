"""issue 131: configurable team review preferences columns

Revision ID: c2d3e4f5a6b7
Revises: d1e2f3a4b5c6
Create Date: 2026-10-17 00:00:00.000000

Adds per-repo review preference storage (source of truth is the
repo_settings row) plus a per-review trace snapshot:

* repo_settings.review_guidelines_text (nullable TEXT, no default)
* repo_settings.review_strictness (VARCHAR(20) NOT NULL, default 'medium',
  check-constrained to low|medium|high)
* repo_settings.review_focus_areas (JSONB NOT NULL, default '[]')
* repo_settings.review_checklist (JSONB NOT NULL, default '[]')
* code_reviews.applied_review_prefs (nullable JSONB, no default)

Additive-only, backward-compatible: every new NOT NULL column carries a
server_default so existing rows backfill inline, and the trace column is
nullable. Downgrade drops the strictness check constraint first (precedent
d8e9f0a1b2c3), then the columns.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c2d3e4f5a6b7"
down_revision: Union[str, Sequence[str], None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "repo_settings",
        sa.Column(
            "review_guidelines_text",
            sa.Text(),
            nullable=True,
        ),
    )
    op.add_column(
        "repo_settings",
        sa.Column(
            "review_strictness",
            sa.String(length=20),
            server_default="medium",
            nullable=False,
        ),
    )
    op.add_column(
        "repo_settings",
        sa.Column(
            "review_focus_areas",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "repo_settings",
        sa.Column(
            "review_checklist",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "code_reviews",
        sa.Column(
            "applied_review_prefs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_repo_settings_review_strictness",
        "repo_settings",
        "review_strictness IN ('low', 'medium', 'high')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_repo_settings_review_strictness", "repo_settings", type_="check"
    )
    op.drop_column("code_reviews", "applied_review_prefs")
    op.drop_column("repo_settings", "review_checklist")
    op.drop_column("repo_settings", "review_focus_areas")
    op.drop_column("repo_settings", "review_strictness")
    op.drop_column("repo_settings", "review_guidelines_text")
