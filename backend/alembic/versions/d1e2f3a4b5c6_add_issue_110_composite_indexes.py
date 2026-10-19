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
duplicates. Expand-only, no table rewrite.

Zero-downtime: each index is built with CREATE INDEX CONCURRENTLY outside
any transaction block (autocommit_block), so live webhook writes are not
blocked while the builds run. Retrying a partial run is safe: a valid
index left behind by an interrupted upgrade is reused, an INVALID
leftover from a failed concurrent build is dropped and rebuilt, and
downgrade drops tolerate already-dropped indexes (IF EXISTS).
"""

from __future__ import annotations

from typing import Optional, Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "d1e2f3a4b5c6"
# Rebased onto origin/main: e1f2a3b4c5d6 (uq_session_stream_events_session_turn_event,
# fix for #112) was merged after this branch forked, so this migration descends
# from the new head rather than its historical b8c9d0e1f2a3 parent.
down_revision: Union[str, Sequence[str], None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEXES: tuple[tuple[str, str, list[str]], ...] = (
    ("ix_runs_repo_status_created", "runs", ["repo_id", "status", "created_at"]),
    (
        "ix_code_reviews_repo_commit_pr",
        "code_reviews",
        ["repo_id", "commit_sha", "pr_number"],
    ),
    (
        "ix_webhook_deliveries_repo_event_created",
        "webhook_deliveries",
        ["repo_id", "event", "created_at"],
    ),
)


def _pg_index_valid(name: str) -> Optional[bool]:
    """Validity of an existing index, or None when absent.

    Returns None when the index does not exist in the current schema, and
    also on non-Postgres dialects (the pg_catalog check has no equivalent
    there) so the caller falls through to a plain CREATE INDEX. A failed
    CONCURRENTLY build leaves an INVALID index behind (indisvalid false)
    that the planner ignores and that blocks a same-name rebuild.

    Skipped entirely in offline (--sql) mode because pg_catalog cannot be
    queried there and the bound parameter :name would otherwise appear as
    NULL in the generated SQL. The safe offline default is to fall through
    to a fresh CREATE INDEX, which is exactly what alembic's op stack
    would emit anyway.
    """
    ctx = op.get_context()
    if ctx.as_sql:
        return None
    bind = ctx.bind
    if bind is None:
        return None
    if bind.dialect.name != "postgresql":
        return None
    row = bind.execute(
        text(
            "SELECT i.indisvalid FROM pg_class c "
            "JOIN pg_index i ON i.indexrelid = c.oid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relname = :name AND n.nspname = current_schema()"
        ),
        {"name": name},
    ).first()
    if row is None:
        return None
    return bool(row[0])


def upgrade() -> None:
    # CONCURRENTLY cannot run inside a transaction block: env.py wraps
    # migrations in begin_transaction(), so each build runs in an
    # autocommit block instead. Non-blocking for live webhook writes.
    with op.get_context().autocommit_block():
        for name, table, columns in _INDEXES:
            state = _pg_index_valid(name)
            if state is True:
                continue  # interrupted retry: already built and usable
            if state is False:
                op.drop_index(
                    name,
                    table_name=table,
                    postgresql_concurrently=True,
                )
            op.create_index(
                name,
                table,
                columns,
                unique=False,
                postgresql_concurrently=True,
            )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, table, _columns in reversed(_INDEXES):
            op.drop_index(
                name,
                table_name=table,
                postgresql_concurrently=True,
                if_exists=True,
            )
