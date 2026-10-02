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
from alembic import context, op

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
    # Preflight: re-imposing NOT NULL on github_run_id is impossible while any
    # retry child (or conversational follow-up) row still holds NULL. Fail with
    # an actionable message *before* touching anything, rather than letting
    # Postgres abort the ALTER with a bare not_null_violation.
    #
    # This deliberately refuses to invent an id or delete rows to make the
    # rollback succeed. A retry child is real diagnostic history: deleting one
    # would cascade to its run steps and attempts, and a fabricated workflow run
    # id would squat on the real GitHub id space. Resolving those rows is an
    # operator decision, so the migration stops and says so.
    #
    # In offline mode (`alembic downgrade --sql`) there is no connection to
    # inspect, so the check is skipped rather than refused — the generated SQL
    # is still emitted for review, and applying it remains the operator's call.
    if not context.is_offline_mode():
        bind = op.get_bind()
        if bind is not None:
            # Take the lock BEFORE counting, otherwise the check is advisory: a
            # retry child inserted between the SELECT and the ALTER below would
            # still abort the rollback with a bare not_null_violation, which is
            # exactly the outcome this preflight exists to prevent. EXCLUSIVE
            # conflicts with the row inserts on `runs` and with the ACCESS
            # EXCLUSIVE the ALTER needs, so it covers both statements.
            # lock_timeout keeps a busy table from parking the downgrade behind
            # someone else's long transaction — it fails fast and loudly.
            bind.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
            bind.execute(sa.text("LOCK TABLE runs IN EXCLUSIVE MODE"))
            null_rows = bind.execute(
                sa.text("SELECT count(*) FROM runs WHERE github_run_id IS NULL")
            ).scalar()
            if null_rows:
                raise RuntimeError(
                    f"refusing to downgrade a5c6d7e8f9b0: {null_rows} run row(s) "
                    "have github_run_id IS NULL (one-click retry children and "
                    "conversational follow-ups). Export or explicitly resolve "
                    "those runs before retrying the downgrade — this migration "
                    "will not fabricate a workflow run id or delete retry "
                    "history to make the rollback succeed."
                )

    op.alter_column(
        "runs", "github_run_id", existing_type=sa.BigInteger(), nullable=False
    )
    op.drop_column("runs", "is_retry_child")