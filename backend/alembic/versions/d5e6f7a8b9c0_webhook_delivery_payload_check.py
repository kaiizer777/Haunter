"""bound webhook_deliveries payload at the database level (issue #36)

Revision ID: d5e6f7a8b9c0
Revises: c2d3e4f5a6b7
Create Date: 2026-10-02 00:00:00.000000

Adds CHECK (payload IS NULL OR octet_length(payload) <= 2796204) to
`webhook_deliveries`.

WHY
---
The replay-buffer caps are enforced today by Python `if` checks inside
`_encode_replay_buffer()` (app/webhooks.py). That is the whole guarantee: the
column is `sa.Text()`, which Postgres will accept up to ~1 GB. Nothing in the
schema stops a future refactor of the write path from reintroducing unbounded
bodies, and nothing rejects a row that some other code path inserted. Making
the invariant a constraint moves it from "a convention nobody can see" to
something the database itself enforces.

THE BOUND
---------
2,796,204 bytes is the base64 length of the 2 MiB ingestion cap
(MAX_PAYLOAD_SIZE_BYTES): 4 * ceil(2097152 / 3). Two write paths feed this
column and the bound has to admit the widest one:

  * the general health log caps the replay buffer at _WEBHOOK_PAYLOAD_MAX_BYTES
    (128 KiB raw -> 174,764 base64 bytes);
  * the auditor ack paths (`retain_full_payload=True`, `auditor_queued` /
    `audit_accepted`) deliberately lift the cap to the full ingestion limit so
    an accepted audit is never recorded unreplayable. Any body that passed
    ingestion is at most 2 MiB, so 2,796,204 is the largest value the
    application can legitimately store.

The CHECK is therefore the ingestion-cap backstop, not the 128 KiB rule: a
tighter bound rejects valid auditor-ack rows (IntegrityError -> 503 -> GitHub
redelivery loop), and a looser one stops tracking the ingestion cap. The
128 KiB capping behaviour itself stays where it is, in `_encode_replay_buffer`.

`payload IS NULL` is carried explicitly even though a CHECK passes on NULL:
rows for oversized bodies are recorded with payload=NULL by design, and the
retention sweeper NULLs payloads as it ages them out, so NULL has to stay a
first-class value here.

MIGRATION SHAPE
---------------
Follows the repo's zero-downtime expand/contract discipline: ADD ... NOT VALID
takes only a brief ACCESS EXCLUSIVE lock and does NOT scan the table, then
VALIDATE CONSTRAINT takes the weaker SHARE UPDATE EXCLUSIVE lock and scans. A
plain ADD CONSTRAINT would hold ACCESS EXCLUSIVE for the whole scan and block
every concurrent insert from webhook ingestion.

VALIDATE is where an existing violating row would surface, and that is
intentional: it fails loudly at deploy time with a named constraint instead of
silently keeping a bound that is not actually true. Existing rows are all
written through `_encode_replay_buffer`, so every one of them is at most
2,796,204 bytes and validation cannot fail on a table this migration ships
against — and when it does, no row is modified or deleted either way.

`octet_length` is Postgres-specific (bytes, not characters), which is the
correct unit: the storage cost of the column is bytes.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "d5e6f7a8b9c0"
down_revision: Union[str, Sequence[str], None] = "c2d3e4f5a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CONSTRAINT_NAME = "ck_webhook_deliveries_payload_octet_length"
CHECK_EXPRESSION = "payload IS NULL OR octet_length(payload) <= 2796204"


def upgrade() -> None:
    # NOT VALID: enforced for all new/updated rows from this point on, but no
    # scan of existing rows yet, and no ACCESS EXCLUSIVE lock held across one.
    op.execute(
        f"ALTER TABLE webhook_deliveries ADD CONSTRAINT {CONSTRAINT_NAME} "
        f"CHECK ({CHECK_EXPRESSION}) NOT VALID"
    )
    # Now prove the existing rows already satisfy it. SHARE UPDATE EXCLUSIVE
    # still allows reads and writes, so ingestion is not blocked by the scan.
    op.execute(
        f"ALTER TABLE webhook_deliveries VALIDATE CONSTRAINT {CONSTRAINT_NAME}"
    )


def downgrade() -> None:
    # IF EXISTS because a downgrade that was interrupted between the two
    # statements above may find only one of them applied; failing here would
    # strand the operator with a half-applied revision.
    op.execute(
        f"ALTER TABLE webhook_deliveries DROP CONSTRAINT IF EXISTS {CONSTRAINT_NAME}"
    )