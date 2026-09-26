"""phase31_model_config_scope

Revision ID: e7f8a9b0c1d2
Revises: c1d2e3f4a5b6
Create Date: 2026-09-25 00:00:00.000000

Per-Repo vs Global Model Config Isolation (future.md §3.2 Issue 1).

Problem: PUT /config/model executed
  UPDATE model_configs SET is_active=false WHERE is_active=true,
wiping repo overrides. Table lacked a scope/repo_id discriminator,
so global lookups could also pick up repo rows.

Expand/contract-safe:
- ADD COLUMN scope VARCHAR(16) NOT NULL SERVER DEFAULT 'global'
  (existing rows become global; no table rewrite lock beyond default).
- ADD COLUMN repo_id / user_id UUID NULLABLE + FK SET NULL (no backfill
  required for the ADD itself).
- Backfill: rows referenced by repos.active_model_config_id become
  scope='repo' with repo_id/user_id pinned from the owning repo.
- Indexes on scope / repo_id / user_id for the new hot-path filters.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7f8a9b0c1d2"
down_revision: Union[str, Sequence[str], None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Discriminator + ownership pins (expand step — nullable/additive only).
    op.add_column(
        "model_configs",
        sa.Column("scope", sa.String(length=16), nullable=False, server_default="global"),
    )
    op.add_column(
        "model_configs",
        sa.Column("repo_id", sa.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "model_configs",
        sa.Column("user_id", sa.UUID(as_uuid=True), nullable=True),
    )

    # 2. FK constraints (SET NULL — deleting a repo/user never deletes history).
    op.create_foreign_key(
        "fk_model_configs_repo_id_repos",
        "model_configs",
        "repos",
        ["repo_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_model_configs_user_id_users",
        "model_configs",
        "users",
        ["user_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # 3. Indexes for scope-filtered hot paths.
    op.create_index("ix_model_configs_scope", "model_configs", ["scope"])
    op.create_index("ix_model_configs_repo_id", "model_configs", ["repo_id"])
    op.create_index("ix_model_configs_user_id", "model_configs", ["user_id"])

    # 4. Backfill: rows linked from repos.active_model_config_id are repo
    # overrides — promote them from the 'global' default to scope='repo'.
    op.execute(
        sa.text(
            "UPDATE model_configs SET scope='repo', repo_id=r.id, user_id=r.user_id "
            "FROM repos r WHERE model_configs.id = r.active_model_config_id "
            "AND r.active_model_config_id IS NOT NULL"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_model_configs_user_id", table_name="model_configs")
    op.drop_index("ix_model_configs_repo_id", table_name="model_configs")
    op.drop_index("ix_model_configs_scope", table_name="model_configs")
    op.drop_constraint("fk_model_configs_user_id_users", "model_configs", type_="foreignkey")
    op.drop_constraint("fk_model_configs_repo_id_repos", "model_configs", type_="foreignkey")
    op.drop_column("model_configs", "user_id")
    op.drop_column("model_configs", "repo_id")
    op.drop_column("model_configs", "scope")
