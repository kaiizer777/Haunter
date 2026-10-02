"""bound webhook_deliveries payload at the database level (issue #36)

Revision ID: d5e6f7a8b9c0
Revises: a5c6d7e8f9b0
Create Date: 2026-10-02 00:00:00.000000

Adds CHECK (payload IS NULL OR octet_length(payload) <= 262144) to
`webhook_deliveries`.

WHY
---
The 128 KiB replay-buffer cap is enforced today by exactly one Python `if`,
inside `_encode_replay_buffer()` (app/webhooks.py). That is the whole
guarantee: the column is `sa.Text()`, which Postgres will accept up to ~1 GB.
Nothing in the schema stops a future refactor of the write path from
reintroducing unbounded bodies, and nothing rejects a row that some other code
path inserted. Making the invariant a constraint moves it from "a convention
nobody can see" to something the database itself enforces.

The bound is 262,144 bytes (256 KiB) rather than 131,072: the stored value is
base64, which inflates the raw body by 4/3, so the largest body the application
can ever encode is 4 * ceil(131072/3) = 174,764 bytes. 256 KiB leaves ~47%
headroom above that without becoming loose enough to be meaningless.

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
written through `_encode_replay_buffer`, so every one of them is at most 174,764
bytes and validation cannot fail on a table this migration ships against — and
when it does, no row is modified or deleted either way.

`octet_length` is Postgres-specific (bytes, not characters), which is the
correct unit: the storage cost of the column is bytes.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "d5e6f7a8b9c0"
down_revision: Union[str, Sequence[str], None] = "a5c6d7e8f9b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CONSTRAINT_NAME = "ck_webhook_deliveries_payload_octet_length"
CHECK_EXPRESSION = "payload IS NULL OR octet_length(payload) <= 262144"


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