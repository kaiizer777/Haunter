"""Split-window retention sweep for `webhook_deliveries` (issue #36).

Fast (hermetic, no DB):
- retention windows are read from Settings, never hardcoded in the sweeper
- Settings refuses a row window shorter than the payload window
- the sweeper identifier is what the cron workflow computes its HMAC over
- the cron workflow's identifier/kind/url match the backend constants, it holds
  no DATABASE_URL, and it cannot echo the token
- POST /webhooks/deliveries/retention-sweep rejects a missing / malformed /
  wrong-kind token with 401, and never runs a sweep for an unauthenticated call

DB (require TEST_DATABASE_URL, run in CI / --all / explicit target):
- payload past the window is NULLed while the row and every metadata column
  survive, and the row reports replayable=false
- rows past the ROW window are deleted, and only those
- in-window rows are left fully intact
- the window boundaries are exclusive: created_at exactly at the cutoff is kept
- configured windows are honoured (explicit RetentionWindows override)
- batching drains a backlog larger than one batch, and max_batches bounds it
- a second sweep over the same data changes nothing (idempotent)
- the CHECK constraint rejects an oversized payload, allows NULL and the bound
"""

import base64
import pathlib
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, settings
from app.models import Repo, User, WebhookDelivery
from app.self_invocation import (
    KIND_AUDIT,
    KIND_RETENTION,
    self_invocation_token,
    verify_self_invocation,
)
from app.services.webhook_retention import (
    RETENTION_SWEEP_IDENTIFIER,
    RetentionWindows,
    retention_windows,
    sweep_webhook_delivery_retention,
)
from app.webhooks import WebhookDeliveryOut

SWEEP_PATH = "/webhooks/deliveries/retention-sweep"

# Mirrors CHECK (payload IS NULL OR octet_length(payload) <= 262144) from
# migration d5e6f7a8b9c0. Duplicated here on purpose: this is the number the
# schema promises, and a test that read the bound back out of the migration
# would still pass if the migration were loosened past what the app honours.
DB_PAYLOAD_MAX_BYTES = 262_144

NOW = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)


def _secret() -> str:
    return settings.audit_self_invoke_secret or "test_self_invoke_secret"


def _windows(**overrides) -> RetentionWindows:
    """Explicit windows so tests never depend on the ambient environment."""
    base = RetentionWindows(
        payload_days=90, row_days=365, batch_size=1000, max_batches=20
    )
    return replace(base, **overrides)


def _b64_payload(marker: str, size: int = 64) -> str:
    return base64.b64encode(f"{marker}:".encode() + b"x" * size).decode("ascii")


# Sentinel so `payload=None` can mean "insert a genuine NULL" instead of
# colliding with the default of "generate a body".
_AUTO_PAYLOAD = object()


async def _insert(
    db: AsyncSession,
    *,
    age_days: float,
    marker: str,
    payload: str | None | object = _AUTO_PAYLOAD,
    repo_id: uuid.UUID | None = None,
) -> WebhookDelivery:
    """Insert one delivery row aged `age_days` into the past.

    `created_at` is written explicitly (the model default is now()) so the
    retention predicate is exercised against real stored values rather than
    against a frozen clock the sweeper never reads.
    """
    row = WebhookDelivery(
        id=uuid.uuid4(),
        event="issue_comment",
        delivery_id=f"del_{marker}",
        status="queued",
        reason=f"reason-{marker}",
        repo=f"acme/{marker}",
        repo_id=repo_id,
        payload=_b64_payload(marker) if payload is _AUTO_PAYLOAD else payload,
        created_at=NOW - timedelta(days=age_days),
    )
    db.add(row)
    await db.commit()
    return row


async def _reload(db: AsyncSession, row_id: uuid.UUID) -> WebhookDelivery | None:
    """Re-read a row by id, refreshing any copy already in the identity map.

    `populate_existing` is required: the sweeper commits per batch (and, via the
    endpoint, on a *different* session), so the ORM instance this test already
    holds can still carry the pre-sweep payload in memory. Without the refresh
    the query returns that stale instance and the assertions read old data.
    """
    result = await db.execute(
        select(WebhookDelivery)
        .where(WebhookDelivery.id == row_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Fast hermetic tests (run in default suite)
# ---------------------------------------------------------------------------


def test_retention_windows_come_from_settings():
    windows = retention_windows()
    assert windows.payload_days == settings.webhook_payload_retention_days
    assert windows.row_days == settings.webhook_row_retention_days
    assert windows.batch_size == settings.webhook_retention_sweep_batch_size
    assert windows.max_batches == settings.webhook_retention_sweep_max_batches


def test_retention_defaults_match_the_traces_range_cap():
    """Drift guard: the payload window must stay aligned with the traces cap.

    If these diverge, a user can page back further in the health tab than in the
    trace view — the exact inconsistency this default exists to prevent.
    """
    assert settings.webhook_payload_retention_days == 90
    assert settings.webhook_row_retention_days > settings.webhook_payload_retention_days


def _settings_kwargs(**overrides) -> dict:
    base = {
        "database_url": "postgresql://u:p@h:5432/d",
        "database_url_unpooled": "postgresql://u:p@h:5432/d",
        "github_client_id": "id",
        "github_client_secret": "secret",
        "callback_url": "http://localhost:7555/auth/callback",
        "session_secret_key": "sess",
        "frontend_url": "http://localhost:3011",
        "token_encryption_key": "k",
    }
    base.update(overrides)
    return base


def test_settings_reject_row_window_shorter_than_payload_window():
    """Ordering is validated at boot, not discovered at sweep time."""
    with pytest.raises(ValueError, match="must be >="):
        Settings(
            **_settings_kwargs(
                webhook_payload_retention_days=30, webhook_row_retention_days=7
            )
        )


@pytest.mark.parametrize(
    "field",
    [
        "webhook_payload_retention_days",
        "webhook_row_retention_days",
        "webhook_retention_sweep_batch_size",
        "webhook_retention_sweep_max_batches",
    ],
)
@pytest.mark.parametrize("value", [0, -1])
def test_settings_reject_non_positive_windows(field: str, value: int):
    with pytest.raises(ValueError):
        Settings(**_settings_kwargs(**{field: value}))


def test_settings_accept_equal_windows():
    """Equal windows are legal: the payload is NULLed and then the row deleted."""
    cfg = Settings(
        **_settings_kwargs(
            webhook_payload_retention_days=7, webhook_row_retention_days=7
        )
    )
    assert cfg.webhook_row_retention_days == 7


def test_retention_token_is_domain_separated_from_other_kinds():
    """A token minted for another kind must not authenticate the sweep."""
    secret = _secret()
    audit_token = self_invocation_token(KIND_AUDIT, RETENTION_SWEEP_IDENTIFIER, secret)
    retention_token = self_invocation_token(
        KIND_RETENTION, RETENTION_SWEEP_IDENTIFIER, secret
    )
    assert audit_token != retention_token
    assert (
        verify_self_invocation(
            KIND_RETENTION, RETENTION_SWEEP_IDENTIFIER, audit_token, secret
        )
        is False
    )
    assert (
        verify_self_invocation(
            KIND_RETENTION, RETENTION_SWEEP_IDENTIFIER, retention_token, secret
        )
        is True
    )


def test_retention_verifier_fails_closed_without_a_secret():
    assert (
        verify_self_invocation(KIND_RETENTION, RETENTION_SWEEP_IDENTIFIER, "a" * 64, None)
        is False
    )
    assert (
        verify_self_invocation(
            KIND_RETENTION, RETENTION_SWEEP_IDENTIFIER, "a" * 64, "   "
        )
        is False
    )


@pytest.mark.parametrize(
    "token", [None, "", "not-hex", "a" * 63, "A" * 64, "0" * 64, " " * 64]
)
@pytest.mark.asyncio
async def test_sweep_endpoint_rejects_unauthenticated_or_wrong_token(
    client: httpx.AsyncClient, token: str | None
):
    """No valid token -> 401.

    "0"*64 is well-formed hex of the right length but was minted from a different
    secret, so it must fail the constant-time comparison rather than the regex.
    """
    headers = {} if token is None else {"X-Haunter-Retention-Token": token}
    response = await client.post(SWEEP_PATH, headers=headers)
    assert response.status_code == 401
    assert "payloads_cleared" not in response.text


@pytest.mark.asyncio
async def test_sweep_endpoint_rejects_audit_kind_token(
    client: httpx.AsyncClient, db: AsyncSession
):
    """A valid token for the WRONG kind must not drive the sweep.

    Also proves the 401 path has a side-effect-free guarantee: no row is
    touched when the caller is unauthenticated.
    """
    row = await _insert(db, age_days=500, marker="wrongkind")
    token = self_invocation_token(KIND_AUDIT, RETENTION_SWEEP_IDENTIFIER, _secret())

    response = await client.post(
        SWEEP_PATH, headers={"X-Haunter-Retention-Token": token}
    )

    assert response.status_code == 401
    survivor = await _reload(db, row.id)
    assert survivor is not None, "an unauthenticated call must not delete rows"
    assert survivor.payload is not None


# ---------------------------------------------------------------------------
# DB tests (require TEST_DATABASE_URL)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_nulls_expired_payload_but_keeps_row_and_metadata(
    db: AsyncSession, user_factory
):
    """The core split: the bytes go, the audit record stays."""
    user: User = await user_factory()
    repo = Repo(user_id=user.id, owner="acme", name="split-window")
    db.add(repo)
    await db.commit()

    old = await _insert(db, age_days=91, marker="old", repo_id=repo.id)

    result = await sweep_webhook_delivery_retention(
        db, windows=_windows(payload_days=90), now=NOW
    )

    assert result.payloads_cleared == 1
    assert result.rows_deleted == 0
    assert result.batches_run == 1

    survivor = await _reload(db, old.id)
    assert survivor is not None, "payload expiry must NOT delete the row"
    assert survivor.payload is None
    assert survivor.event == "issue_comment"
    assert survivor.status == "queued"
    assert survivor.reason == "reason-old"
    assert survivor.delivery_id == "del_old"
    assert survivor.repo == "acme/old"
    assert survivor.repo_id == repo.id
    assert survivor.created_at == NOW - timedelta(days=91)


@pytest.mark.asyncio
async def test_swept_row_reports_not_replayable_through_the_response_dto(
    db: AsyncSession,
):
    """No new UI state: a swept row flows through the existing replayable flag."""
    row = await _insert(db, age_days=91, marker="dto")
    assert WebhookDeliveryOut.from_row(row).replayable is True

    await sweep_webhook_delivery_retention(
        db, windows=_windows(payload_days=90), now=NOW
    )

    swept = await _reload(db, row.id)
    assert WebhookDeliveryOut.from_row(swept).replayable is False


@pytest.mark.asyncio
async def test_sweep_deletes_only_rows_past_the_row_window(db: AsyncSession):
    ancient = await _insert(db, age_days=400, marker="ancient")
    between = await _insert(db, age_days=200, marker="between")
    recent = await _insert(db, age_days=1, marker="recent")

    result = await sweep_webhook_delivery_retention(
        db, windows=_windows(payload_days=90, row_days=365), now=NOW
    )

    assert result.rows_deleted == 1
    # 200d is past the payload window but inside the row window: payload gone,
    # row kept. That is the entire reason the two windows are separate.
    assert result.payloads_cleared == 1
    assert await _reload(db, ancient.id) is None
    kept = await _reload(db, between.id)
    assert kept is not None and kept.payload is None
    untouched = await _reload(db, recent.id)
    assert untouched is not None and untouched.payload is not None


@pytest.mark.asyncio
async def test_sweep_leaves_in_window_rows_completely_intact(db: AsyncSession):
    row = await _insert(db, age_days=1, marker="fresh")
    before = WebhookDeliveryOut.from_row(await _reload(db, row.id))

    result = await sweep_webhook_delivery_retention(
        db, windows=_windows(payload_days=90, row_days=365), now=NOW
    )

    assert (result.payloads_cleared, result.rows_deleted) == (0, 0)
    after = await _reload(db, row.id)
    assert WebhookDeliveryOut.from_row(after) == before
    assert after.payload == _b64_payload("fresh")


@pytest.mark.asyncio
async def test_sweep_boundaries_are_exclusive_at_both_windows(db: AsyncSession):
    """created_at exactly AT the cutoff is INSIDE the window and must survive.

    Pins the comparison as `created_at < cutoff`. A `<=` regression here would
    sweep or delete a row one instant early; this is the test that catches it.
    """
    at_payload_edge = await _insert(db, age_days=90, marker="edge-payload")
    at_row_edge = await _insert(db, age_days=365, marker="edge-row")
    past_payload_edge = await _insert(db, age_days=90.5, marker="past-payload")
    past_row_edge = await _insert(db, age_days=365.5, marker="past-row")

    await sweep_webhook_delivery_retention(
        db, windows=_windows(payload_days=90, row_days=365), now=NOW
    )

    assert (await _reload(db, at_payload_edge.id)).payload is not None
    assert await _reload(db, at_row_edge.id) is not None
    assert (await _reload(db, past_payload_edge.id)).payload is None
    assert await _reload(db, past_row_edge.id) is None


@pytest.mark.asyncio
async def test_sweep_honours_a_narrower_configured_window(db: AsyncSession):
    """Nothing is hardcoded: a 7-day payload window clears a 10-day-old body."""
    row = await _insert(db, age_days=10, marker="narrow")
    still_fresh = await _insert(db, age_days=3, marker="toofresh")

    result = await sweep_webhook_delivery_retention(
        db, windows=_windows(payload_days=7, row_days=30), now=NOW
    )

    assert result.payloads_cleared == 1
    assert result.payload_cutoff == NOW - timedelta(days=7)
    assert result.row_cutoff == NOW - timedelta(days=30)
    assert (await _reload(db, row.id)).payload is None
    assert (await _reload(db, still_fresh.id)).payload is not None


@pytest.mark.asyncio
async def test_sweep_batches_drain_a_backlog_larger_than_one_batch(db: AsyncSession):
    """A backlog wider than batch_size needs more than one pass."""
    rows = [await _insert(db, age_days=200, marker=f"batch{i}") for i in range(7)]

    result = await sweep_webhook_delivery_retention(
        db,
        windows=_windows(payload_days=90, row_days=365, batch_size=2, max_batches=10),
        now=NOW,
    )

    assert result.payloads_cleared == 7
    # 7 rows at 2 per batch = 4 batches (2+2+2+1). The last came back short,
    # which is the signal that the backlog is drained, so the loop stops there
    # instead of spending a fifth empty round trip.
    assert result.batches_run == 4
    for row in rows:
        assert (await _reload(db, row.id)).payload is None


@pytest.mark.asyncio
async def test_sweep_max_batches_caps_the_work_per_invocation(db: AsyncSession):
    """The ceiling holds even with a backlog far larger than it can clear."""
    for i in range(5):
        await _insert(db, age_days=200, marker=f"capped{i}")
    capped = _windows(payload_days=90, row_days=365, batch_size=2, max_batches=2)

    result = await sweep_webhook_delivery_retention(db, windows=capped, now=NOW)

    assert result.batches_run == 2
    assert result.payloads_cleared == 4, "only batch_size * max_batches may be touched"

    # A follow-up invocation resumes where the capped one stopped.
    resumed = await sweep_webhook_delivery_retention(db, windows=capped, now=NOW)
    assert resumed.payloads_cleared == 1


@pytest.mark.asyncio
async def test_sweep_is_idempotent(db: AsyncSession):
    await _insert(db, age_days=200, marker="idem")
    windows = _windows(payload_days=90)

    first = await sweep_webhook_delivery_retention(db, windows=windows, now=NOW)
    second = await sweep_webhook_delivery_retention(db, windows=windows, now=NOW)

    assert first.payloads_cleared == 1
    assert second.payloads_cleared == 0
    assert second.rows_deleted == 0
    # A sweep with nothing to do still runs one batch — that batch is how it
    # learns there is nothing to do.
    assert second.batches_run == 1


@pytest.mark.asyncio
async def test_sweep_ignores_rows_that_already_have_no_payload(db: AsyncSession):
    """An oversized delivery recorded with payload=NULL is not re-counted."""
    await _insert(db, age_days=200, marker="nulled", payload=None)
    result = await sweep_webhook_delivery_retention(
        db, windows=_windows(payload_days=90), now=NOW
    )
    assert result.payloads_cleared == 0


@pytest.mark.asyncio
async def test_sweep_endpoint_runs_the_sweep_when_authenticated(
    client: httpx.AsyncClient, db: AsyncSession
):
    row = await _insert(db, age_days=200, marker="endpoint")
    token = self_invocation_token(KIND_RETENTION, RETENTION_SWEEP_IDENTIFIER, _secret())

    response = await client.post(
        SWEEP_PATH, headers={"X-Haunter-Retention-Token": token}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["payloads_cleared"] == 1
    assert body["rows_deleted"] == 0
    assert body["batches_run"] == 1
    # Counts and cutoffs only — the response model must never echo stored content.
    assert set(body) == {
        "payloads_cleared",
        "rows_deleted",
        "payload_cutoff",
        "row_cutoff",
        "batches_run",
    }
    assert (await _reload(db, row.id)).payload is None


@pytest.mark.asyncio
async def test_db_check_constraint_rejects_oversized_payload(db: AsyncSession):
    """The 128 KiB invariant is now enforced by Postgres, not just by Python."""
    db.add(
        WebhookDelivery(
            id=uuid.uuid4(),
            event="push",
            delivery_id="del_oversized",
            status="queued",
            reason=None,
            repo=None,
            repo_id=None,
            payload="A" * (DB_PAYLOAD_MAX_BYTES + 1),
            created_at=NOW,
        )
    )
    with pytest.raises((IntegrityError, DBAPIError)):
        await db.commit()
    await db.rollback()


@pytest.mark.asyncio
async def test_db_check_constraint_allows_exactly_the_bound(db: AsyncSession):
    """Boundary of the constraint itself: the bound is inclusive."""
    row = WebhookDelivery(
        id=uuid.uuid4(),
        event="push",
        delivery_id="del_at_bound",
        status="queued",
        payload="A" * DB_PAYLOAD_MAX_BYTES,
        created_at=NOW,
    )
    db.add(row)
    await db.commit()
    assert (await _reload(db, row.id)).payload is not None


@pytest.mark.asyncio
async def test_db_check_constraint_allows_null_payload(db: AsyncSession):
    """NULL must stay legal — it is both the swept state and the oversized state."""
    row = WebhookDelivery(
        id=uuid.uuid4(),
        event="push",
        delivery_id="del_null_payload",
        status="queued",
        payload=None,
        created_at=NOW,
    )
    db.add(row)
    await db.commit()
    assert (await _reload(db, row.id)).payload is None


@pytest.mark.asyncio
async def test_db_check_constraint_is_present_and_validated(db: AsyncSession):
    """Not merely present — `convalidated` proves VALIDATE ran, not just NOT VALID."""
    rows = (
        await db.execute(
            text(
                "SELECT conname, convalidated FROM pg_constraint "
                "WHERE conrelid = 'webhook_deliveries'::regclass AND contype = 'c'"
            )
        )
    ).all()
    by_name = dict(rows)
    assert "ck_webhook_deliveries_payload_octet_length" in by_name
    assert by_name["ck_webhook_deliveries_payload_octet_length"] is True


def test_app_written_payload_always_fits_the_db_constraint():
    """The contract between the encoder's cap and the constraint's bound.

    If either moves independently of the other, this fails: it pins the largest
    base64 the app can ever produce (174,764 bytes) below the DB bound.
    """
    from app.webhooks import _WEBHOOK_PAYLOAD_MAX_BYTES, _encode_replay_buffer

    encoded = _encode_replay_buffer(b"x" * _WEBHOOK_PAYLOAD_MAX_BYTES)
    assert encoded is not None
    assert len(encoded.encode("ascii")) == 174_764
    assert len(encoded.encode("ascii")) < DB_PAYLOAD_MAX_BYTES

    # One byte over the app cap is stored as NULL, never as an oversized string.
    assert _encode_replay_buffer(b"x" * (_WEBHOOK_PAYLOAD_MAX_BYTES + 1)) is None


# ---------------------------------------------------------------------------
# Cron-workflow drift guard
# ---------------------------------------------------------------------------

_WORKFLOW = (
    pathlib.Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "webhook-retention.yml"
)


@pytest.mark.skipif(not _WORKFLOW.exists(), reason="cron workflow not present")
def test_cron_workflow_identifier_matches_the_backend_constant():
    """The cron mints its HMAC over an identifier hardcoded in YAML.

    The token is HMAC(secret, "retention:<identifier>"), so if the identifier in
    the workflow drifts from RETENTION_SWEEP_IDENTIFIER the cron keeps minting a
    well-formed token that the verifier rejects — the sweep then 401s forever and
    nothing surfaces it except payloads quietly ageing out. There is no import
    between a YAML string and a Python constant, so this is the only guard.
    """
    text = _WORKFLOW.read_text(encoding="utf-8")
    assert f"RETENTION_SWEEP_IDENTIFIER: {RETENTION_SWEEP_IDENTIFIER}" in text, (
        "the workflow's RETENTION_SWEEP_IDENTIFIER env value must equal "
        f"RETENTION_SWEEP_IDENTIFIER ({RETENTION_SWEEP_IDENTIFIER!r})"
    )


@pytest.mark.skipif(not _WORKFLOW.exists(), reason="cron workflow not present")
def test_cron_workflow_kind_matches_the_backend_kind():
    """`kind` is the domain separator — a wrong one yields a rejected token.

    Pins the exact message the workflow signs: "retention:<identifier>".
    """
    assert '"retention:${RETENTION_SWEEP_IDENTIFIER}"' in _WORKFLOW.read_text(
        encoding="utf-8"
    )


@pytest.mark.skipif(not _WORKFLOW.exists(), reason="cron workflow not present")
def test_cron_workflow_calls_the_sweep_endpoint_and_carries_no_database_url():
    """Two invariants about what the cron is allowed to hold.

    1. It must target the real path, or the 2xx it reports is somebody else's.
    2. It must NOT receive DATABASE_URL: running the SQL from CI would mean
       giving a third-party runner full read/write on every production table,
       which is exactly what the token-based endpoint exists to avoid.
    """
    text = _WORKFLOW.read_text(encoding="utf-8")
    assert SWEEP_PATH in text
    assert "secrets.DATABASE_URL" not in text
    assert "DATABASE_URL_UNPOOLED" not in text


@pytest.mark.skipif(not _WORKFLOW.exists(), reason="cron workflow not present")
def test_cron_workflow_never_echoes_the_token():
    """`-v` on curl would print the request headers, token included."""
    text = _WORKFLOW.read_text(encoding="utf-8")
    assert "set -x" not in text
    assert "--verbose" not in text
    assert 'echo "${TOKEN}"' not in text
    assert 'echo "TOKEN=' not in text
