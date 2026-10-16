"""issue 110: composite indexes for hot read paths

Revision ID: d1e2f3a4b5c6
Revises: b8c9d0e1f2a3
Create Date: 2026-10-16 00:00:00.000000

Adds three composite indexes (equality columns first, range/ORDER BY last):

* ix_runs_repo_status_created on runs(repo_id, status, created_at)
  — traces list (routers/traces.py) filters repo_id + status with a
  created_at range and ORDER BY created_at DESC.
* ix_code_reviews_repo_commit_pr on code_reviews(repo_id, commit_sha, pr_number)
  — review dedup lookup (webhooks.py), all-equality predicate.
* ix_webhook_deliveries_repo_event_created
  on webhook_deliveries(repo_id, event, created_at)
  — health list (webhooks.py list_webhook_deliveries) filters repo_id
  (+ optional event) with ORDER BY created_at DESC.

Deliberately additive-only: the three UNIQUE idempotency guards on runs
(github_run_id, trigger_comment_id, github_delivery_id) are separate
uniqueness domains (workflow-run ids vs comment ids vs delivery ids) and
are asserted by test_db_models.py + the double-delivery webhook tests, so
consolidating them into one column would conflate namespaces and allow
duplicates. Expand-only, no table rewrite. Each index is built with
CREATE INDEX CONCURRENTLY outside any transaction block
(autocommit_block), so live webhook writes are not blocked while the
builds run.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, Sequence[str], None] = "b8c9d0e1f2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # CONCURRENTLY cannot run inside a transaction block: env.py wraps
    # migrations in begin_transaction(), so each build runs in an
    # autocommit block instead. Non-blocking for live webhook writes.
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_runs_repo_status_created",
            "runs",
            ["repo_id", "status", "created_at"],
            unique=False,
            postgresql_concurrently=True,
        )
        op.create_index(
            "ix_code_reviews_repo_commit_pr",
            "code_reviews",
            ["repo_id", "commit_sha", "pr_number"],
            unique=False,
            postgresql_concurrently=True,
        )
        op.create_index(
            "ix_webhook_deliveries_repo_event_created",
            "webhook_deliveries",
            ["repo_id", "event", "created_at"],
            unique=False,
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_webhook_deliveries_repo_event_created",
            table_name="webhook_deliveries",
            postgresql_concurrently=True,
        )
        op.drop_index(
            "ix_code_reviews_repo_commit_pr",
            table_name="code_reviews",
            postgresql_concurrently=True,
        )
        op.drop_index(
            "ix_runs_repo_status_created",
            table_name="runs",
            postgresql_concurrently=True,
        )
