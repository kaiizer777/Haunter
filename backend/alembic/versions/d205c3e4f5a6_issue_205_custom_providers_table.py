"""issue_205_custom_providers_table

Revision ID: d205c3e4f5a6
Revises: a2b3c4d5e6f7
Create Date: 2026-10-31 00:00:00.000000

Creates custom_providers table for issue #205 (user-registered third-party
model endpoints). Idempotency key is (name, base_url): re-registering the
same pair updates the existing row instead of creating a duplicate.
Secrets are stored encrypted at rest in api_key_encrypted.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d205c3e4f5a6"
down_revision: Union[str, Sequence[str], None] = "a2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "custom_providers",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("compat", sa.String(length=16), nullable=False),
        sa.Column("base_url", sa.String(length=1024), nullable=False),
        sa.Column("api_key_encrypted", sa.Text(), nullable=False),
        sa.Column("models_json", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "name", "base_url", name="uq_custom_provider_name_base_url"
        ),
    )


def downgrade() -> None:
    op.drop_table("custom_providers")
