"""code_reviews_failure_reason

Revision ID: f3a4b5c6d7e8
Revises: e9d1c2b3a405
Create Date: 2026-09-30 00:00:00.000000

Adds failure_reason TEXT NULLABLE to code_reviews so publish failures
(GitHub POST errors after analysis) are visible instead of silently
logged with status=completed.

  - failure_reason TEXT NULLABLE
      Short human-readable reason written when the review pipeline
      finishes analysis but fails to publish to GitHub.
      Truncated to 500 chars before write. NULL on success paths.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "f3a4b5c6d7e8"
down_revision = "e9d1c2b3a405"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "code_reviews", sa.Column("failure_reason", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("code_reviews", "failure_reason")
