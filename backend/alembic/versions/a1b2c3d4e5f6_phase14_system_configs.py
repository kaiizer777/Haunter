"""phase14_system_configs

Revision ID: a1b2c3d4e5f6
Revises: f8a3c2e9b041
Create Date: 2026-08-28 09:40:00

Adds system_configs key-value table for hot-switchable system configuration
(HOSTING_PROVIDER, SANDBOX_PROVIDER) without redeploy.

Design:
- Primary key on `key` (VARCHAR 255) — upsert via INSERT ON CONFLICT DO UPDATE.
- No FK constraints — this is a global, non-tenant-scoped config store.
- `updated_at` tracked for audit trail (admin-only writes).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "c9d0e1f2a345"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "system_configs",
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("value", sa.String(length=255), nullable=False),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("system_configs")
