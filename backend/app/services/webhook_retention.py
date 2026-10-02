"""Split-window retention sweep for `webhook_deliveries` (Feature 8 health log).

WHY TWO WINDOWS
---------------
`webhook_deliveries` mixes two things with completely different retention
needs:

  * the ROW is the audit record. `GET /webhooks/deliveries` queries it to answer
    "did Haunter see this event, what did it decide, why, and for which repo".
    It costs ~100 bytes.
  * the PAYLOAD is the replay buffer: base64 of the exact raw body that HMAC
    verification passed over, kept only so `POST /webhooks/deliveries/{id}/replay`
    can re-drive the decision. It is ~99% of the bytes — a 128 KiB body is
    174,764 base64 characters (see _WEBHOOK_PAYLOAD_MAX_BYTES in app.webhooks).
    It also carries user-authored text from `issue_comment` /
    `pull_request_review_comment` events, so it is the part with a real
    data-minimisation obligation attached.

A single window forces a false choice: delete early and throw away the audit
trail, or keep forever and pay for the bodies indefinitely. So the sweep does
two things at two different ages:

  * older than `webhook_payload_retention_days` (default 90d, matching the traces
    range cap in app/routers/traces.py) -> set `payload = NULL`.
    The row, and every metadata column on it, survives untouched. Because
    `WebhookDeliveryOut.replayable` is derived from `payload is not None`, a
    swept row reports `replayable=false` and `POST .../replay` answers its
    existing 409 "cannot be replayed" path — no new state, no new UI branch.
  * older than `webhook_row_retention_days` (default 365d) -> DELETE the row.

The delete predicate is a superset of the NULL-out predicate (row_days >=
payload_days, enforced by Settings._validate_webhook_retention_windows), so
rows are deleted before their payload is pointless-cleared: each batch deletes
first, then NULLs whatever is left.

BATCHING
--------
Neither statement runs unbounded. A single UPDATE or DELETE across the whole
table holds row locks for the duration and generates a large burst of dead
tuples in one transaction, which stalls the webhook inserts running
concurrently. Each iteration therefore takes at most `batch_size` rows via an
id subquery (an index-friendly range scan on ix_webhook_deliveries_created_at),
commits it, and stops after `max_batches` iterations. The ceiling means one
invocation can never run unbounded, however much has accumulated.

SCHEDULING
----------
There is no in-process scheduler — the backend is Lambda behind a Function URL
(`authorization_type = "NONE"`, see infra/aws/lambda.tf), which is stateless:
an interval-based task cannot survive between invocations, and N concurrent
cold starts would each start their own. The sweep is therefore triggered from
outside by .github/workflows/webhook-retention.yml (a `schedule:` cron) calling
POST /webhooks/deliveries/retention-sweep, authenticated with the existing
app.self_invocation HMAC construction under the `retention` kind. That keeps the
production database URL out of CI: the runner holds one domain-separated token
that can trigger exactly one idempotent operation, instead of credentials with
full read/write on every table.

Both windows come from Settings; this module never hardcodes a day count.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import WebhookDelivery

logger = logging.getLogger(__name__)

# Identifier half of the self-invocation token message ("retention:<identifier>").
# The cron workflow imports this exact value, so it lives here next to the
# verifier rather than being retyped in YAML.
RETENTION_SWEEP_IDENTIFIER = "webhook-delivery-retention"


@dataclass(frozen=True)
class RetentionWindows:
    """Resolved sweep parameters — every field is operator-configurable."""

    payload_days: int
    row_days: int
    batch_size: int
    max_batches: int


@dataclass(frozen=True)
class RetentionSweepResult:
    """What one sweep actually changed.

    `batches_run` is reported so an operator can tell "nothing left to do" (1
    batch, zero rows) from "hit the ceiling with work remaining" (max_batches).
    """

    payloads_cleared: int
    rows_deleted: int
    payload_cutoff: datetime
    row_cutoff: datetime
    batches_run: int


def retention_windows() -> RetentionWindows:
    """Read the four retention knobs off the Settings singleton."""
    return RetentionWindows(
        payload_days=settings.webhook_payload_retention_days,
        row_days=settings.webhook_row_retention_days,
        batch_size=settings.webhook_retention_sweep_batch_size,
        max_batches=settings.webhook_retention_sweep_max_batches,
    )


def _payload_batch_ids(cutoff: datetime, batch_size: int):
    """Ids of up to `batch_size` rows whose replay buffer is past the cutoff.

    Oldest first, id as tie-breaker: the sweep drains the table from the front
    so a partially-completed sweep never re-selects the same rows forever.
    `payload.is_(None)` is excluded so a row is only ever considered once.
    """
    return (
        select(WebhookDelivery.id)
        .where(
            WebhookDelivery.created_at < cutoff,
            WebhookDelivery.payload.is_not(None),
        )
        .order_by(WebhookDelivery.created_at, WebhookDelivery.id)
        .limit(batch_size)
    )


def _row_batch_ids(cutoff: datetime, batch_size: int):
    """Ids of up to `batch_size` rows older than the row-retention cutoff."""
    return (
        select(WebhookDelivery.id)
        .where(WebhookDelivery.created_at < cutoff)
        .order_by(WebhookDelivery.created_at, WebhookDelivery.id)
        .limit(batch_size)
    )


async def sweep_webhook_delivery_retention(
    db: AsyncSession,
    *,
    windows: Optional[RetentionWindows] = None,
    now: Optional[datetime] = None,
) -> RetentionSweepResult:
    """Apply both retention windows. Idempotent — safe to run any number of times.

    Cutoffs are inclusive-exclusive on age: a row whose `created_at` equals the
    cutoff exactly is INSIDE the window and is left alone, so the boundary is
    `created_at < cutoff` and never `created_at <= cutoff`.

    Each batch commits on its own. Progress therefore survives a mid-sweep
    failure, and no single transaction grows with the size of the backlog. A
    failure still propagates — a sweep that cannot run must not report success
    to the cron — but the session is rolled back first so it is never returned
    to the pool holding an aborted transaction.
    """
    windows = windows or retention_windows()
    now = now or datetime.now(timezone.utc)
    payload_cutoff = now - timedelta(days=windows.payload_days)
    row_cutoff = now - timedelta(days=windows.row_days)

    payloads_cleared = 0
    rows_deleted = 0
    batches_run = 0

    try:
        for _ in range(windows.max_batches):
            # Deletes run first: anything past the row cutoff was going to be
            # discarded anyway, so NULL-ing its payload first would only churn
            # TOAST pages for no benefit.
            deleted = await db.execute(
                delete(WebhookDelivery).where(
                    WebhookDelivery.id.in_(_row_batch_ids(row_cutoff, windows.batch_size))
                )
            )
            rows_deleted += deleted.rowcount or 0

            cleared = await db.execute(
                update(WebhookDelivery)
                .where(WebhookDelivery.id.in_(_payload_batch_ids(payload_cutoff, windows.batch_size)))
                .values(payload=None)
            )
            payloads_cleared += cleared.rowcount or 0

            batches_run += 1
            await db.commit()

            # A batch that came back short is the end of the backlog: the ids are
            # taken oldest-first with a LIMIT, so a full batch means "there may be
            # more" and a short one means "there is not". Stopping here avoids a
            # pointless extra empty round trip (and its commit) at the tail of
            # every sweep, and makes `batches_run` mean "batches that did work".
            deleted_count = deleted.rowcount or 0
            cleared_count = cleared.rowcount or 0
            if max(deleted_count, cleared_count) < windows.batch_size:
                break
    except Exception:
        try:
            await db.rollback()
        except Exception:
            # A session whose rollback itself fails is already unusable; the
            # caller's get_db() closes it. Swallow so the original sweep error
            # is what propagates.
            logger.warning(
                "webhook retention sweep rollback failed", exc_info=True
            )
        logger.error(
            "webhook retention sweep failed after %d batch(es): %d payload(s) "
            "cleared, %d row(s) deleted",
            batches_run,
            payloads_cleared,
            rows_deleted,
            exc_info=True,
        )
        raise

    return RetentionSweepResult(
        payloads_cleared=payloads_cleared,
        rows_deleted=rows_deleted,
        payload_cutoff=payload_cutoff,
        row_cutoff=row_cutoff,
        batches_run=batches_run,
    )
