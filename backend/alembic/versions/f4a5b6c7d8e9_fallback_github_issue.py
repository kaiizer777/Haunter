"""fallback to github issue on exhaust path

Revision ID: f4a5b6c7d8e9
Revises: a1b2c3d4e5f7
Create Date: 2026-09-30 12:00:00.000000

Feature 3 — Fallback to GitHub Issue:
  - repo_settings.file_issue_on_fallback BOOLEAN NOT NULL DEFAULT true
      Opt-in flag gating issue creation on the orchestrator exhaust path.
      server_default backfills existing rows as enabled.
  - runs.fallback_issue_url TEXT NULLABLE
  - runs.fallback_issue_number INTEGER NULLABLE
      Link the filed tracking issue on the run detail page.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "f4a5b6c7d8e9"
down_revision = "a1b2c3d4e5f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "repo_settings",
        sa.Column(
            "file_issue_on_fallback",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
    )
    op.add_column(
        "runs",
        sa.Column("fallback_issue_url", sa.Text(), nullable=True),
    )
    op.add_column(
        "runs",
        sa.Column("fallback_issue_number", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("runs", "fallback_issue_number")
    op.drop_column("runs", "fallback_issue_url")
    op.drop_column("repo_settings", "file_issue_on_fallback")
