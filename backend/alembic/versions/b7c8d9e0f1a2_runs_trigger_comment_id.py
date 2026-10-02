"""runs_trigger_comment_id

Splits the overloaded `runs.github_run_id` column.

Consecutive follow-up runs created from an `issue_comment` /
`pull_request_review_comment` payload stored the *comment* id in
`github_run_id`, a column documented (and typed) as the GitHub Actions
`workflow_run` id. The reply path only worked because of that coincidence,
and a single UNIQUE index was enforcing idempotency across two unrelated
GitHub id namespaces - so a comment id colliding with a workflow run id
silently dropped a legitimate `@haunter` request as a duplicate delivery.

This revision adds:
  * `trigger_comment_id` - the comment that carried the `@haunter` command,
    with its own UNIQUE index, so re-delivery is still deduplicated;
  * `reply_to_comment_id` - the review thread to answer in. GitHub's replies
    endpoint only accepts a *top-level* review comment id, so a command sent
    as a reply must address its ancestor. Intentionally NOT unique, because
    several commands in one thread legitimately share a thread root;
  * relaxes `github_run_id` to nullable so follow-up runs no longer
    impersonate a workflow run.

Expand-only and backward compatible:
  * adding a nullable column takes no lock that blocks reads/writes;
  * `DROP NOT NULL` only widens the accepted value set;
  * existing rows keep their `github_run_id` untouched, and rows written by
    the previous code (comment id in `github_run_id`) remain readable and are
    still deduplicated by `github_delivery_id`.

Revision ID: b7c8d9e0f1a2
Revises: e8f9a0b1c2d3
Create Date: 2026-10-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b7c8d9e0f1a2"
down_revision: Union[str, Sequence[str], None] = "e8f9a0b1c2d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "runs",
        sa.Column("trigger_comment_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "runs",
        sa.Column("reply_to_comment_id", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        op.f("ix_runs_trigger_comment_id"),
        "runs",
        ["trigger_comment_id"],
        unique=True,
    )
    # Backfill: rows written before this revision stored the triggering
    # comment id in `github_run_id`. A follow-up Run is exactly a Run with a
    # `parent_run_id`, so the lineage column identifies them precisely
    # without guessing from the conclusion string.
    op.execute(
        """
        UPDATE runs
           SET trigger_comment_id = github_run_id
         WHERE parent_run_id IS NOT NULL
           AND trigger_comment_id IS NULL
           AND github_run_id IS NOT NULL
        """
    )
    op.alter_column(
        "runs",
        "github_run_id",
        existing_type=sa.BigInteger(),
        nullable=True,
    )


def downgrade() -> None:
    """Downgrade schema.

    Order is load-bearing. Once the new code is deployed, follow-up runs carry
    ``github_run_id = NULL`` and their comment id in ``trigger_comment_id``, so
    the pre-split representation has to be restored *before* NOT NULL is
    re-imposed, and the source column has to be gone before that as well:

      1. copy ``trigger_comment_id`` back into the NULL ``github_run_id``s;
      2. drop the split columns, so nothing can repopulate a NULL;
      3. re-impose NOT NULL last.

    A row that cannot be represented in the pre-split schema (both columns
    NULL) makes step 3 fail loudly rather than silently discarding the row.

    Collapsing two id namespaces back into one is inherently lossy: a
    follow-up comment id that happens to equal an existing workflow run id
    trips ``ix_runs_github_run_id`` here. That ambiguity is exactly what the
    split exists to remove going forward, and it can only affect this
    downgrade path, so it is left to fail loudly rather than papered over.
    """
    op.execute(
        """
        UPDATE runs
           SET github_run_id = trigger_comment_id
         WHERE github_run_id IS NULL
           AND trigger_comment_id IS NOT NULL
        """
    )
    op.drop_index(op.f("ix_runs_trigger_comment_id"), table_name="runs")
    op.drop_column("runs", "trigger_comment_id")
    op.drop_column("runs", "reply_to_comment_id")
    op.alter_column(
        "runs",
        "github_run_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )