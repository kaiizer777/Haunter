"""add webhook_deliveries health log (Feature 8)

Revision ID: e8f9a0b1c2d3
Revises: f4a5b6c7d8e9
Create Date: 2026-09-30 00:00:00.000000

Adds append-only webhook_deliveries table backing:
  - GET /webhooks/deliveries (health history)
  - POST /webhooks/{id}/replay (audit-only replay marker)

Expand-only: new table, no changes to existing tables.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e8f9a0b1c2d3"
down_revision: Union[str, Sequence[str], None] = "f4a5b6c7d8e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("delivery_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("repo", sa.String(length=255), nullable=True),
        sa.Column(
            "repo_id", sa.UUID(), sa.ForeignKey("repos.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_webhook_deliveries_delivery_id", "webhook_deliveries", ["delivery_id"]
    )
    op.create_index(
        "ix_webhook_deliveries_created_at", "webhook_deliveries", ["created_at"]
    )
    op.create_index(
        "ix_webhook_deliveries_repo_id", "webhook_deliveries", ["repo_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_deliveries_repo_id", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_created_at", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_delivery_id", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
