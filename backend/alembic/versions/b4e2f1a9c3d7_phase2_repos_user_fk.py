"""phase2_repos_user_fk

Adds repos.user_id UUID FK → users.id (NOT NULL, CASCADE delete, indexed).
Drops old uq_repo_owner_name unique constraint.
Adds new uq_repo_user_owner_name (user_id, owner, name) so two users can
independently track the same public repo.

No backfill needed — repos table was empty before Phase 2 (no users existed
in Phase 1; OAuth flow is introduced here).

Revision ID: b4e2f1a9c3d7
Revises: 3c55fe5848f2
Create Date: 2026-08-27 23:03:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4e2f1a9c3d7"
down_revision: Union[str, Sequence[str], None] = "3c55fe5848f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. Drop the old (owner, name) unique constraint before altering the table.
    op.drop_constraint("uq_repo_owner_name", "repos", type_="unique")

    # 2. Add user_id column — nullable first so the statement doesn't fail on
    #    any pre-existing rows (even if the table is empty this is safer).
    op.add_column(
        "repos",
        sa.Column("user_id", sa.UUID(), nullable=True),
    )

    # 3. Add FK constraint.
    op.create_foreign_key(
        "fk_repos_user_id_users",
        "repos",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # 4. Make user_id NOT NULL now that the FK is in place.
    #    (Table is empty in Phase 2; no backfill needed.)
    op.alter_column("repos", "user_id", nullable=False)

    # 5. Index for fast per-user repo lookups (WHERE user_id = ?).
    op.create_index("ix_repos_user_id", "repos", ["user_id"])

    # 6. New unique constraint: (user_id, owner, name).
    op.create_unique_constraint(
        "uq_repo_user_owner_name", "repos", ["user_id", "owner", "name"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("uq_repo_user_owner_name", "repos", type_="unique")
    op.drop_index("ix_repos_user_id", table_name="repos")
    op.drop_constraint("fk_repos_user_id_users", "repos", type_="foreignkey")
    op.drop_column("repos", "user_id")
    op.create_unique_constraint("uq_repo_owner_name", "repos", ["owner", "name"])
