"""sandbox language packs + monorepo scoping (Feature 7)

Revision ID: a1b2c3d4e5f7
Revises: f3a4b5c6d7e8
Create Date: 2026-09-30 12:00:00.000000

Feature 7 — Sandbox language packs + monorepo scoping:

  - repo_settings.test_command TEXT NULLABLE
      Custom test command replacing the detected language's default test step
      (pytest / npm test / go test / cargo test / mvn test). NULL, empty or
      unset means the language default.
  - repo_settings.working_dir VARCHAR(255) NULLABLE
      Repo-relative POSIX monorepo subdirectory (e.g. "packages/api") that
      the sandbox workflow runs in. NULL or "." means the repository root.

Expand-only: two nullable columns, no backfill, no constraint change, no
rewrite of an existing column. Safe to deploy ahead of the code that reads
them (rolling Lambda deploys) and safe to roll back — dropping the columns
loses only an optional override the user can re-enter.

Values are validated in app/services/repo_settings.py on every write
(validate_working_dir / validate_test_command) and re-validated on read in
app/sandbox/github_actions_runner.py:resolve_sandbox_overrides.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f7"
down_revision: Union[str, Sequence[str], None] = "f3a4b5c6d7e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "repo_settings",
        sa.Column("working_dir", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "repo_settings",
        sa.Column("test_command", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("repo_settings", "test_command")
    op.drop_column("repo_settings", "working_dir")