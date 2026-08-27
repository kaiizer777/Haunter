"""phase5_diagnosis_summary

Revision ID: e4a7f8b9c012
Revises: d13f45ced634
Create Date: 2026-08-28

Adds runs.diagnosis_summary (TEXT, nullable) — written by the Context Gatherer subagent
after each run's LLM call. Never stores raw CI logs; only the redacted distilled summary.
"""

from alembic import op
import sqlalchemy as sa

revision = "e4a7f8b9c012"
down_revision = "d13f45ced634"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("diagnosis_summary", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("runs", "diagnosis_summary")
