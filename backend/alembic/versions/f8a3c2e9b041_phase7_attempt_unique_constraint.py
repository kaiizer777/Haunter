"""phase7_attempt_unique_constraint

Revision ID: f8a3c2e9b041
Revises: e4a7f8b9c012
Create Date: 2026-08-28 00:00:00.000000

Adds UniqueConstraint(run_id, attempt_number) to the attempts table.
This enforces at the database level that the same attempt_number cannot
appear twice for the same run, preventing race conditions in the verify
retry loop from creating duplicate attempt rows.
"""
from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "f8a3c2e9b041"
down_revision = "e4a7f8b9c012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_attempt_run_number",
        "attempts",
        ["run_id", "attempt_number"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_attempt_run_number",
        "attempts",
        type_="unique",
    )
