"""add_user_role

Revision ID: c3d4e5f6a7b8
Revises: 15a7b8c9d0e1
Create Date: 2026-09-07 14:35:00.000000

Adds a 'role' column with a DB CHECK constraint to the users table:
  - role: VARCHAR(32), NOT NULL, default 'user'
  - CHECK constraint: role IN ('user', 'admin')
Existing rows automatically default to 'user'.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c3d4e5f6a7b8"
down_revision = "15a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("role", sa.String(length=32), server_default="user", nullable=False),
    )
    op.create_check_constraint(
        "check_user_role",
        "users",
        "role IN ('user', 'admin')",
    )


def downgrade() -> None:
    op.drop_constraint("check_user_role", "users", type_="check")
    op.drop_column("users", "role")
