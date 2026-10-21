"""issue 161: shared webhook ingress rate limit store

Revision ID: a2b3c4d5e6f7
Revises: d5e6f7a8b9c0
Create Date: 2026-10-17 00:00:00.000000

Adds webhook_rate_limit_hits, the cross-execution-environment backing store
for the issue #161 ingress throttler.

The throttler previously kept its sliding-window buckets in module-level
globals, which under Lambda are per execution environment: with
reserved_concurrent_executions=10 the effective ceiling was ten times the
documented webhook_rate_limit_max_deliveries, and every cold start reset all
buckets. One row per accepted delivery makes the budget a count over shared
state instead of a per-process counter.

bucket_key is the same `repo-full-name|sender-login` string the in-memory
limiter used, VARCHAR(200) to match MAX_BUCKET_KEY_LENGTH in app.models / app.services.rate_limit
so no code path can produce a key the column cannot hold. It is an opaque
throttle key derived from an attacker-influenced payload, never a tenant
boundary, so this table carries no foreign key and no row written here is ever
returned by an endpoint.

Indexes, equality column first and range column second:

* ix_webhook_rate_limit_hits_key_hit_at on (bucket_key, hit_at)
  — every per-bucket statement: the per-delivery prune
  (`bucket_key = :key AND hit_at <= :cutoff`), and the count()/min() that
  decides the budget.
* ix_webhook_rate_limit_hits_hit_at on (hit_at)
  — the batched global prune, which filters on hit_at alone to reclaim buckets
  that will never post again. That predicate has no leading bucket_key, so the
  composite cannot serve it.

Unlike the issue #110 index migration, these are plain CREATE INDEX rather than
CREATE INDEX CONCURRENTLY. That migration added indexes to populated, live
tables; this one creates a brand-new table that no running code has ever
written to, so both builds scan zero rows and the only lock taken is a
catalog-only ACCESS EXCLUSIVE lock on a table with no other sessions attached.
Splitting out of the transaction to satisfy CONCURRENTLY's restrictions would
buy nothing and would add an INVALID-index retry surface to the downgrade /
re-upgrade cycle CI runs.

Expand-only: new table, no changes to existing tables.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a2b3c4d5e6f7"
down_revision: Union[str, Sequence[str], None] = "d5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "webhook_rate_limit_hits"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        # Length 200 matches MAX_BUCKET_KEY_LENGTH in app.models / app.services.rate_limit
        sa.Column("bucket_key", sa.String(length=200), nullable=False),
        sa.Column(
            "hit_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_webhook_rate_limit_hits_key_hit_at",
        TABLE,
        ["bucket_key", "hit_at"],
        unique=False,
    )
    op.create_index(
        "ix_webhook_rate_limit_hits_hit_at",
        TABLE,
        ["hit_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_rate_limit_hits_hit_at", table_name=TABLE)
    op.drop_index("ix_webhook_rate_limit_hits_key_hit_at", table_name=TABLE)
    op.drop_table(TABLE)
