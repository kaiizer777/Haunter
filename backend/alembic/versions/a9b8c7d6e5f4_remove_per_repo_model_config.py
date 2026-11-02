"""remove per-repository model config, keep everything global

Revision ID: a9b8c7d6e5f4
Revises: d205c3e4f5a6
Create Date: 2026-11-02 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.exc import NoInspectionAvailable


revision: str = "a9b8c7d6e5f4"
down_revision: Union[str, Sequence[str], None] = "d205c3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _bind():
    return op.get_bind()


def _has_column(table: str, column: str, default: bool = True) -> bool:
    try:
        inspector = sa.inspect(_bind())
        return column in {c["name"] for c in inspector.get_columns(table)}
    except NoInspectionAvailable:
        # Offline mode (MockConnection) has no inspection; use caller-provided
        # default (upgrade assumes present, downgrade assumes absent).
        return default


def _has_index(table: str, index: str, default: bool = True) -> bool:
    try:
        inspector = sa.inspect(_bind())
        return index in {i["name"] for i in inspector.get_indexes(table)}
    except NoInspectionAvailable:
        return default


def _has_fk(table: str, name: str, default: bool = True) -> bool:
    try:
        inspector = sa.inspect(_bind())
        return any(fk.get("name") == name for fk in inspector.get_foreign_keys(table))
    except NoInspectionAvailable:
        return default


def _drop_fks_on_columns(table: str, columns: set[str]) -> None:
    try:
        inspector = sa.inspect(_bind())
    except NoInspectionAvailable:
        return
    for fk in inspector.get_foreign_keys(table):
        if set(fk.get("constrained_columns") or ()) & columns:
            name = fk.get("name")
            if name:
                op.drop_constraint(name, table, type_="foreignkey")


def upgrade() -> None:
    # 1. Deactivate repo-scoped model config rows; do not promote to global.
    if _has_column("model_configs", "repo_id") or _has_column("model_configs", "scope"):
        op.execute(
            sa.text(
                "UPDATE model_configs SET is_active = false "
                "WHERE repo_id IS NOT NULL OR scope = 'repo'"
            )
        )

    # 2. Clear legacy repo -> model config linkage.
    if _has_column("repos", "active_model_config_id"):
        op.execute(sa.text("UPDATE repos SET active_model_config_id = NULL"))

    # 3. Drop repo linkage column.
    if _has_column("repos", "active_model_config_id"):
        op.drop_column("repos", "active_model_config_id")

    # 4. Drop indexes added for per-repo scoping.
    for idx in ("ix_model_configs_user_id", "ix_model_configs_repo_id", "ix_model_configs_scope"):
        if _has_index("model_configs", idx):
            op.drop_index(idx, table_name="model_configs")

    # 5. Drop FK constraints if present.
    for fk in ("fk_model_configs_repo_id_repos", "fk_model_configs_user_id_users"):
        if _has_fk("model_configs", fk):
            op.drop_constraint(fk, "model_configs", type_="foreignkey")
    _drop_fks_on_columns("model_configs", {"repo_id", "user_id"})

    # 6. Drop scope/repo/user columns.
    for col in ("user_id", "repo_id", "scope"):
        if _has_column("model_configs", col):
            op.drop_column("model_configs", col)

    # 7. Drop repo settings model_override_scope.
    if _has_column("repo_settings", "model_override_scope"):
        op.drop_column("repo_settings", "model_override_scope")


def downgrade() -> None:
    # Best-effort, partially destructive: re-adds columns nullable with defaults
    # but does not restore deactivated/dropped repo-scoped rows or data.
    if not _has_column("model_configs", "scope", default=False):
        op.add_column(
            "model_configs",
            sa.Column("scope", sa.String(length=16), nullable=False, server_default="global"),
        )
    if not _has_column("model_configs", "repo_id", default=False):
        op.add_column(
            "model_configs",
            sa.Column("repo_id", sa.UUID(as_uuid=True), nullable=True),
        )
    if not _has_column("model_configs", "user_id", default=False):
        op.add_column(
            "model_configs",
            sa.Column("user_id", sa.UUID(as_uuid=True), nullable=True),
        )
    if not _has_column("repos", "active_model_config_id", default=False):
        op.add_column(
            "repos",
            sa.Column("active_model_config_id", sa.UUID(as_uuid=True), nullable=True),
        )
    if not _has_column("repo_settings", "model_override_scope", default=False):
        op.add_column(
            "repo_settings",
            sa.Column(
                "model_override_scope",
                sa.String(length=50),
                nullable=True,
                server_default="inherit",
            ),
        )

    # Restore FK constraints (best-effort).
    if _has_column("repos", "active_model_config_id") and not _has_fk(
        "repos", "fk_repos_active_model_config_id_model_configs", default=False
    ):
        op.create_foreign_key(
            "fk_repos_active_model_config_id_model_configs",
            "repos",
            "model_configs",
            ["active_model_config_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if _has_column("model_configs", "repo_id") and not _has_fk(
        "model_configs", "fk_model_configs_repo_id_repos", default=False
    ):
        op.create_foreign_key(
            "fk_model_configs_repo_id_repos",
            "model_configs",
            "repos",
            ["repo_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if _has_column("model_configs", "user_id") and not _has_fk(
        "model_configs", "fk_model_configs_user_id_users", default=False
    ):
        op.create_foreign_key(
            "fk_model_configs_user_id_users",
            "model_configs",
            "users",
            ["user_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # Restore indexes (best-effort).
    if _has_column("model_configs", "scope") and not _has_index(
        "model_configs", "ix_model_configs_scope", default=False
    ):
        op.create_index("ix_model_configs_scope", "model_configs", ["scope"])
    if _has_column("model_configs", "repo_id") and not _has_index(
        "model_configs", "ix_model_configs_repo_id", default=False
    ):
        op.create_index("ix_model_configs_repo_id", "model_configs", ["repo_id"])
    if _has_column("model_configs", "user_id") and not _has_index(
        "model_configs", "ix_model_configs_user_id", default=False
    ):
        op.create_index("ix_model_configs_user_id", "model_configs", ["user_id"])
