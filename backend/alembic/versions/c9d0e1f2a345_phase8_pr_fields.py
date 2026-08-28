"""phase8_pr_fields

Revision ID: c9d0e1f2a345
Revises: f8a3c2e9b041
Create Date: 2026-08-28 06:00:00.000000

Adds four Phase 8 columns to the runs table for PR Writer tracking:
  - pr_url TEXT NULLABLE       — GitHub PR HTML URL after successful open
  - pr_number INTEGER NULLABLE — GitHub PR number after successful open
  - pr_branch VARCHAR(255) NULL — server-side haunter/fix-* branch name
  - final_summary TEXT NULLABLE — first 1000 chars of LLM PR body (html-escaped)
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c9d0e1f2a345"
down_revision = "f8a3c2e9b041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("pr_url", sa.Text(), nullable=True))
    op.add_column("runs", sa.Column("pr_number", sa.Integer(), nullable=True))
    op.add_column("runs", sa.Column("pr_branch", sa.String(255), nullable=True))
    op.add_column("runs", sa.Column("final_summary", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "final_summary")
    op.drop_column("runs", "pr_branch")
    op.drop_column("runs", "pr_number")
    op.drop_column("runs", "pr_url")
