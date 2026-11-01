"""remove per-repository model config, keep everything global

Revision ID: a9b8c7d6e5f4
Revises: d205c3e4f5a6
Create Date: 2026-11-02 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a9b8c7d6e5f4"
down_revision: Union[str, Sequence[str], None] = "d205c3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Deactivate repo-scoped model config rows; do not promote to global.
    op.execute(
        sa.text(
            "UPDATE model_configs SET is_active = false "
            "WHERE repo_id IS NOT NULL OR scope = 'repo'"
        )
    )

    # 2. Clear legacy repo -> model config linkage.
    op.execute(sa.text("UPDATE repos SET active_model_config_id = NULL"))

    # 3. Drop repo linkage column.
    op.drop_column("repos", "active_model_config_id")

    # 4. Drop indexes added for per-repo scoping.
    for idx in ("ix_model_configs_user_id", "ix_model_configs_repo_id", "ix_model_configs_scope"):
        try:
            op.drop_index(idx, table_name="model_configs")
        except Exception:
            pass

    # 5. Drop FK constraints if present (best-effort; drop_column handles most).
    for fk in ("fk_model_configs_repo_id_repos", "fk_model_configs_user_id_users"):
        try:
            op.drop_constraint(fk, "model_configs", type_="foreignkey")
        except Exception:
            pass

    # 6. Drop scope/repo/user columns.
    for col in ("user_id", "repo_id", "scope"):
        try:
            op.drop_column("model_configs", col)
        except Exception:
            pass

    # 7. Drop repo settings model_override_scope.
    try:
        op.drop_column("repo_settings", "model_override_scope")
    except Exception:
        pass


def downgrade() -> None:
    # Best-effort, partially destructive: re-adds columns nullable with defaults
    # but does not restore deactivated/dropped repo-scoped rows or data.
    op.add_column(
        "model_configs",
        sa.Column("scope", sa.String(length=16), nullable=True, server_default="global"),
    )
    op.add_column(
        "model_configs",
        sa.Column("repo_id", sa.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "model_configs",
        sa.Column("user_id", sa.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "repos",
        sa.Column("active_model_config_id", sa.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "repo_settings",
        sa.Column(
            "model_override_scope",
            sa.String(length=50),
            nullable=True,
            server_default="inherit",
        ),
    )
    # Note: FK constraints and indexes are not restored in downgrade.