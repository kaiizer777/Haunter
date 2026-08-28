"""phase15_run_failure_reason

Revision ID: 15a7b8c9d0e1
Revises: c9d0e1f2a345
Create Date: 2026-08-29 02:55:00.000000

Adds one Phase 15 column to the runs table so the orchestrator can record
*why* a run ended in error/fallback — surfaced on the run detail page.

  - failure_reason TEXT NULLABLE
      Short, redacted human-readable reason. Written on every error path:
        - context_gatherer timeout / LLM error
        - fix_generator cap exceeded / patch rejected / schema validation fail
        - PR writer / GitHub API error
        - outer orchestrator exception
      Truncated to 500 chars before write. Nullable: NULL on success paths.
      Plain text only — html.escape'd on render to neutralise any LLM-emitted
      markup that bubbled up via an exception message.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "15a7b8c9d0e1"
down_revision = "c9d0e1f2a345"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("failure_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "failure_reason")
