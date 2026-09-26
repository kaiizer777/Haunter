"""bind audit deliveries to payloads and fence dispatch attempts

Revision ID: c7a1b2c3d4e5
Revises: b6d8f0a2c4e6
Create Date: 2026-09-26 00:00:00.000000

Adds:
  - repo_settings.settings_version: monotonic trigger-settings counter
  - audit_jobs.settings_version: settings version that decided the trigger
  - audit_jobs.delivery_fingerprint: SHA-256 over the canonical audit payload
  - audit_jobs.dispatch_fence_token: HMAC fence for the current dispatch attempt

Plus check constraints that make the three guarantees structural rather than
application-only:
  - a pending PR audit always carries both comparison endpoints
  - a dispatching job always carries the fence for its current attempt
  - a persisted delivery always carries a 64-hex payload fingerprint

Legacy rows cannot have their original canonical payload recomputed, so each
historical delivery is bound to a stable digest of its own delivery id. Any
future replay of that id therefore conflicts instead of silently deduplicating
onto a job whose payload was never compared.

Legacy PR jobs are resolved before the endpoint constraint is added: rows that
already carry both endpoints are requeued, rows that do not are failed
terminally with `LegacyAuditEndpointsUnpinned` because the endpoints they were
supposed to compare cannot be recovered from the durable row alone. Auditing
whatever the PR head happens to be at execution time is exactly the behaviour
this migration removes.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c7a1b2c3d4e5"
down_revision: Union[str, Sequence[str], None] = "b6d8f0a2c4e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LEGACY_ACTIONS = "('queued', 'dispatching', 'running')"


def upgrade() -> None:
    op.add_column(
        "repo_settings",
        sa.Column("settings_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "audit_jobs",
        sa.Column("settings_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "audit_jobs",
        sa.Column("delivery_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "audit_jobs",
        sa.Column("dispatch_fence_token", sa.String(length=64), nullable=True),
    )
    op.execute(
        "UPDATE audit_jobs SET delivery_fingerprint = "
        "encode(sha256(convert_to('legacy:' || delivery_id, 'UTF8')), 'hex') "
        "WHERE delivery_fingerprint IS NULL"
    )
    op.alter_column(
        "audit_jobs",
        "delivery_fingerprint",
        existing_type=sa.String(length=64),
        nullable=False,
    )

    # A job that was mid-dispatch when this migration ran holds no fence, and
    # the audit child it may have already invoked can never be authenticated
    # against one. Return those rows to the queue; the in-flight child is
    # rejected on arrival and a fresh attempt is dispatched with a real fence.
    op.execute(
        "UPDATE audit_jobs SET status = 'queued', dispatch_fence_token = NULL, "
        "lease_expires_at = NULL, next_attempt_at = now(), "
        "last_error = 'DispatchFenceBackfill' "
        "WHERE status = 'dispatching'"
    )
    op.execute(
        "UPDATE audit_jobs SET status = 'queued', dispatch_fence_token = NULL, "
        "lease_expires_at = NULL, next_attempt_at = now(), last_error = NULL "
        f"WHERE pr_number IS NOT NULL AND base_sha IS NOT NULL AND head_sha IS NOT NULL "
        f"AND status IN {_LEGACY_ACTIONS}"
    )
    op.execute(
        "UPDATE audit_jobs SET status = 'failed', dispatch_fence_token = NULL, "
        "lease_expires_at = NULL, next_attempt_at = now(), "
        "last_error = 'LegacyAuditEndpointsUnpinned' "
        f"WHERE pr_number IS NOT NULL AND (base_sha IS NULL OR head_sha IS NULL) "
        f"AND status IN {_LEGACY_ACTIONS}"
    )

    op.create_check_constraint(
        "ck_audit_jobs_settings_version",
        "audit_jobs",
        "settings_version >= 1",
    )
    op.create_check_constraint(
        "ck_audit_jobs_delivery_fingerprint",
        "audit_jobs",
        "length(delivery_fingerprint) = 64",
    )
    op.create_check_constraint(
        "ck_audit_jobs_pr_endpoints",
        "audit_jobs",
        "status NOT IN ('queued', 'dispatching', 'running') "
        "OR pr_number IS NULL "
        "OR (base_sha IS NOT NULL AND head_sha IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_audit_jobs_dispatch_fence",
        "audit_jobs",
        "status <> 'dispatching' OR dispatch_fence_token IS NOT NULL",
    )


def downgrade() -> None:
    """Expand-only rollback: keep the added columns and constraints in place.

    Dropping them would strand dispatching jobs without a fence and re-open the
    unpinned-PR path this migration closed. The older code revision ignores the
    extra columns, so leaving them is the safe direction to roll back to.
    """
