"""run retry lineage

Revision ID: a5c6d7e8f9b0
Revises: b7c8d9e0f1a2
Create Date: 2026-10-01 00:00:00.000000

One-click retry (Feature 1) support:

  - runs.is_retry_child BOOLEAN NOT NULL DEFAULT false
      Discriminates a retry child from an interactive PR-feedback refinement
      child. Both carry parent_run_id, but only a refinement child may be
      committed onto the source run's PR branch. Without this flag a retry
      child would resolve target_branch = pr_branch or head_branch and push
      the fix straight to the user's own branch, breaching the Human Merge
      Gate Invariant (HAUNTER.md §1.1).

  - runs.github_run_id DROP NOT NULL
      A retry child corresponds to no new GitHub Actions workflow run and no
      new webhook delivery, so it stores NULL rather than a fabricated id.
      context_gatherer.resolve_workflow_run_id walks parent_run_id to the root
      to recover the original workflow run id.

Expand-only: both changes are backward-compatible with currently running
code (the new column defaults to false for existing refinement children, and
existing rows all hold a non-null github_run_id). No column is renamed or
dropped, so no application deploy ordering constraint is introduced.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision = "a5c6d7e8f9b0"
down_revision = "b7c8d9e0f1a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "is_retry_child",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # NULL-able github_run_id for retry children. Safe on a large table: it
    # only relaxes a NOT NULL constraint, which takes a brief ACCESS EXCLUSIVE
    # lock but performs no table rewrite.
    op.alter_column("runs", "github_run_id", existing_type=sa.BigInteger(), nullable=True)


def downgrade() -> None:
    # Re-imposing NOT NULL on github_run_id is refused by Postgres if any
    # retry child (or conversational follow-up) row still holds NULL. That is
    # the correct outcome — this migration must not invent a workflow run id to
    # make a rollback succeed — so the operator deletes those child runs first.
    op.alter_column(
        "runs", "github_run_id", existing_type=sa.BigInteger(), nullable=False
    )
    op.drop_column("runs", "is_retry_child")