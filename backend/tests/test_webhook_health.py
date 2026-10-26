"""
Feature 8 — Webhook health + replay tests.

Fast (hermetic, no DB):
- reason truncation bounds
- response DTO validation
- _log_webhook_decision sanitization (no crash on hostile input)
- replay buffer encode/decode round-trip and size cap

DB (require TEST_DATABASE_URL, run in CI / --all / explicit target):
- GET /webhooks/deliveries requires auth, tenant-scoped, paginated
- GET /webhooks/deliveries never leaks another tenant's deliveries even when
  both tenants registered the same owner/name
- POST /webhooks/deliveries/{id}/replay re-enters the live handler and reports
  the decision it reached (duplicate on an already-queued run)
- replay of unknown / cross-tenant row -> 404 (no oracle)
- replay cooldown -> 409 + Retry-After
- corrupt / un-retained replay buffer -> 409, no handler invocation
- queued workflow_run persists a webhook_deliveries row WITH the replay buffer,
  and persists no signature header or secret
- two tenants registered on the same owner/name: the live delivery is refused
  with an unattributed diagnostic row and dispatches nothing, and redelivering it
  (in either insert order) yields the identical decision
"""

import base64
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, MissingGreenlet, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Repo, Run, WebhookDelivery
from app.webhooks import (
    _WEBHOOK_PAYLOAD_MAX_BYTES,
    WebhookDeliveryListOut,
    WebhookDeliveryOut,
    _decode_replay_buffer,
    _dispatch_review,
    _encode_replay_buffer,
    _handle_issue_trigger,
    _log_webhook_decision,
    _run_auditor_mention_background,
    _truncate_reason,
)
from tests.conftest import truncate_all

TEST_SECRET = settings.github_webhook_secret or "test_webhook_secret_key_12345"


def sign_payload(secret: str, raw_body: bytes) -> str:
    sig = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def workflow_run_payload(
    run_id: int = 555001, conclusion: str = "failure", branch: str = "main"
) -> dict:
    return {
        "action": "completed",
        "workflow_run": {
            "id": run_id,
            "head_sha": "0123456789abcdef0123456789abcdef01234567",
            "head_branch": branch,
            "conclusion": conclusion,
        },
        "repository": {
            "name": "persist-repo",
            "full_name": "persist-org/persist-repo",
            "owner": {"login": "persist-org"},
        },
    }


# ---------------------------------------------------------------------------
# Fast hermetic tests (run in default suite)
# ---------------------------------------------------------------------------


def test_truncate_reason_bounds_none_and_long():
    assert _truncate_reason(None) is None
    assert _truncate_reason("ok") == "ok"
    long_reason = "x" * 600
    truncated = _truncate_reason(long_reason)
    assert truncated is not None
    assert len(truncated) == 500
    assert truncated == "x" * 500


def test_truncate_reason_coerces_non_string():
    assert _truncate_reason(12345) == "12345"


def test_webhook_delivery_out_validates_explicit_fields():
    row_id = uuid.uuid4()
    repo_id = uuid.uuid4()
    out = WebhookDeliveryOut(
        id=row_id,
        event="workflow_run",
        delivery_id="del_123",
        status="queued",
        reason="ok",
        repo="acme/app",
        repo_id=repo_id,
        created_at="2026-09-30T00:00:00+00:00",  # type: ignore[arg-type]
    )
    assert out.event == "workflow_run"
    assert out.status == "queued"
    assert out.repo_id == repo_id


def test_webhook_delivery_out_never_exposes_the_replay_buffer():
    """The base64 replay buffer must not be reachable through any response model."""
    row = WebhookDelivery(
        id=uuid.uuid4(),
        event="workflow_run",
        delivery_id="del_123",
        status="queued",
        created_at=datetime.now(timezone.utc),
        payload=base64.b64encode(b'{"secret":"body"}').decode("ascii"),
    )
    out = WebhookDeliveryOut.from_row(row)
    assert out.replayable is True
    assert "payload" not in out.model_dump()
    assert "secret" not in json.dumps(out.model_dump(mode="json"))


def test_webhook_delivery_out_from_row_marks_missing_payload_unreplayable():
    row = WebhookDelivery(
        id=uuid.uuid4(),
        event="push",
        delivery_id="del_124",
        status="queued",
        created_at=datetime.now(timezone.utc),
        payload=None,
    )
    assert WebhookDeliveryOut.from_row(row).replayable is False


def test_webhook_delivery_list_out_forbids_extra():
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        WebhookDeliveryListOut(deliveries=[], total=0, unexpected="nope")  # type: ignore[call-arg]


def test_log_webhook_decision_sanitizes_hostile_input(caplog):
    # Newlines / control chars in attacker-influenced fields must not forge log lines
    # and must never raise.
    with caplog.at_level("INFO"):
        _log_webhook_decision(
            event="workflow_run\nEVIL",
            delivery_id="del\n123",
            status="duplicate",
            reason="reason\nwith\nnewlines",
            repo="owner\n/name",
        )
    assert "webhook_decision" in caplog.text


def test_replay_reason_format_is_bounded():
    original_id = uuid.uuid4()
    reason = _truncate_reason(f"replay of {original_id}")
    assert reason == f"replay of {original_id}"
    assert len(reason) <= 500


def test_replay_buffer_round_trips_exact_bytes():
    # Byte-exactness is the whole point: replay re-signs these bytes and the
    # handler re-verifies that signature, so any round-trip drift fails closed.
    raw = b'{"action":"completed","unicode":"\xe2\x9c\x93\xf0\x9f\x94\xa5"}'
    encoded = _encode_replay_buffer(raw)
    assert encoded is not None
    assert _decode_replay_buffer(encoded) == raw


def test_replay_buffer_is_dropped_when_oversized():
    raw = b"x" * (_WEBHOOK_PAYLOAD_MAX_BYTES + 1)
    assert _encode_replay_buffer(raw) is None
    assert _encode_replay_buffer(None) is None


def test_decode_replay_buffer_rejects_corrupt_input():
    assert _decode_replay_buffer(None) is None
    assert _decode_replay_buffer("not base64 !!!") is None


# ---------------------------------------------------------------------------
# DB tests (CI / explicit target)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_deliveries_requires_auth(client: httpx.AsyncClient):
    resp = await client.get("/webhooks/deliveries")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_replay_requires_auth(client: httpx.AsyncClient):
    resp = await client.post(f"/webhooks/deliveries/{uuid.uuid4()}/replay")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_deliveries_empty_for_new_user(
    client: httpx.AsyncClient, db: AsyncSession, user_factory, make_auth_client
):
    await truncate_all(db)
    user = await user_factory(github_id=9101, username="wh_health1")
    auth_client = make_auth_client(user.id)
    try:
        resp = await auth_client.get("/webhooks/deliveries")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 0
        assert body["deliveries"] == []
    finally:
        await auth_client.aclose()


@pytest.mark.asyncio
async def test_list_deliveries_tenant_scoped(
    client: httpx.AsyncClient, db: AsyncSession, user_factory, make_auth_client
):
    await truncate_all(db)
    user_a = await user_factory(github_id=9102, username="wh_health_a")
    user_b = await user_factory(github_id=9103, username="wh_health_b")
    repo_a = Repo(user_id=user_a.id, owner="acme", name="app-a")
    repo_b = Repo(user_id=user_b.id, owner="acme", name="app-b")
    db.add_all([repo_a, repo_b])
    await db.commit()
    await db.refresh(repo_a)
    await db.refresh(repo_b)
    db.add_all(
        [
            WebhookDelivery(
                event="workflow_run",
                delivery_id="del-a-1",
                status="queued",
                reason="ok",
                repo="acme/app-a",
                repo_id=repo_a.id,
            ),
            WebhookDelivery(
                event="workflow_run",
                delivery_id="del-b-1",
                status="queued",
                reason="ok",
                repo="acme/app-b",
                repo_id=repo_b.id,
            ),
        ]
    )
    await db.commit()

    auth_a = make_auth_client(user_a.id)
    try:
        resp = await auth_a.get("/webhooks/deliveries")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["deliveries"][0]["repo"] == "acme/app-a"
    finally:
        await auth_a.aclose()


@pytest.mark.asyncio
async def test_list_deliveries_ignores_same_named_repo_of_another_tenant(
    client: httpx.AsyncClient, db: AsyncSession, user_factory, make_auth_client
):
    """Two users may register the SAME owner/name — that must not merge histories.

    repos enforces uniqueness per (user_id, owner, name), not globally, so
    matching deliveries on the denormalized repo string would leak one tenant's
    webhook history to another. Scope is repo_id only.
    """
    await truncate_all(db)
    owner = await user_factory(github_id=9110, username="wh_health_same_owner")
    intruder = await user_factory(github_id=9111, username="wh_health_same_intruder")
    repo_owner = Repo(user_id=owner.id, owner="acme", name="shared-name")
    repo_intruder = Repo(user_id=intruder.id, owner="acme", name="shared-name")
    db.add_all([repo_owner, repo_intruder])
    await db.commit()
    await db.refresh(repo_owner)
    await db.refresh(repo_intruder)
    db.add(
        WebhookDelivery(
            event="workflow_run",
            delivery_id="del-owner-1",
            status="queued",
            reason="ok",
            repo="acme/shared-name",
            repo_id=repo_owner.id,
        )
    )
    await db.commit()

    intruder_client = make_auth_client(intruder.id)
    try:
        resp = await intruder_client.get("/webhooks/deliveries")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 0, body
        assert body["deliveries"] == []
    finally:
        await intruder_client.aclose()


@pytest.mark.asyncio
async def test_replay_cross_tenant_same_named_repo_returns_404(
    client: httpx.AsyncClient, db: AsyncSession, user_factory, make_auth_client
):
    await truncate_all(db)
    owner = await user_factory(github_id=9112, username="wh_health_rep_owner")
    intruder = await user_factory(github_id=9113, username="wh_health_rep_intruder")
    repo_owner = Repo(user_id=owner.id, owner="acme", name="dup-name")
    repo_intruder = Repo(user_id=intruder.id, owner="acme", name="dup-name")
    db.add_all([repo_owner, repo_intruder])
    await db.commit()
    await db.refresh(repo_owner)
    await db.refresh(repo_intruder)
    original = WebhookDelivery(
        event="workflow_run",
        delivery_id="del-dup-1",
        status="queued",
        reason="ok",
        repo="acme/dup-name",
        repo_id=repo_owner.id,
        payload=_encode_replay_buffer(b"{}"),
    )
    db.add(original)
    await db.commit()
    await db.refresh(original)

    intruder_client = make_auth_client(intruder.id)
    try:
        resp = await intruder_client.post(f"/webhooks/deliveries/{original.id}/replay")
        assert resp.status_code == 404
        # No replay row was appended on the owner's behalf.
        rows = (
            (
                await db.execute(
                    select(WebhookDelivery).where(
                        WebhookDelivery.delivery_id == "del-dup-1"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [r.status for r in rows] == ["queued"]
    finally:
        await intruder_client.aclose()


@pytest.mark.asyncio
async def test_replay_redrives_handler_and_reports_duplicate(
    client: httpx.AsyncClient, db: AsyncSession, user_factory, make_auth_client
):
    """Replay of an already-queued workflow_run re-runs the real handler.

    The unique constraint on runs.github_run_id is what stops a second Run, and
    reaching it proves github_webhook() itself executed — a marker insert could
    never produce this decision.
    """
    await truncate_all(db)
    user = await user_factory(github_id=9114, username="wh_health_redrive")
    repo = Repo(user_id=user.id, owner="replay-org", name="redrive-repo")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)

    delivery_id = str(uuid.uuid4())
    payload = workflow_run_payload(run_id=555101)
    payload["repository"]["name"] = "redrive-repo"
    payload["repository"]["full_name"] = "replay-org/redrive-repo"
    payload["repository"]["owner"]["login"] = "replay-org"
    raw_body = json.dumps(payload).encode("utf-8")

    adapter = _mock_adapter()
    with patch(
        "app.adapters.hosting.get_hosting_adapter", new_callable=AsyncMock
    ) as mock_get:
        mock_get.return_value = adapter
        first = await client.post(
            "/webhooks/github",
            headers=_gh_headers("workflow_run", delivery_id, sign_payload(TEST_SECRET, raw_body)),
            content=raw_body,
        )
    assert first.status_code == 200
    assert first.json()["status"] == "queued"

    original = (
        await db.execute(
            select(WebhookDelivery).where(WebhookDelivery.delivery_id == delivery_id)
        )
    ).scalar_one()
    assert original.payload is not None

    auth_client = make_auth_client(user.id)
    try:
        with patch(
            "app.adapters.hosting.get_hosting_adapter", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = adapter
            resp = await auth_client.post(f"/webhooks/deliveries/{original.id}/replay")
        assert resp.status_code == 200
        body = resp.json()
        assert body["original_id"] == str(original.id)
        assert body["delivery_id"] == delivery_id
        # The decision body is github_webhook()'s own response, not "ok".
        assert body["decision"]["status"] == "duplicate"
        assert body["replay_id"] is not None
    finally:
        await auth_client.aclose()

    # Exactly one Run exists — the replay did not create a second.
    from app.models import Run

    runs = (
        (await db.execute(select(Run).where(Run.repo_id == repo.id))).scalars().all()
    )
    assert len(runs) == 1
    # The replayed row is linked back to the original.
    replay_row = (
        await db.execute(
            select(WebhookDelivery).where(WebhookDelivery.id == body["replay_id"])
        )
    ).scalar_one()
    assert replay_row.replay_of == original.id
    assert replay_row.status == "duplicate"


@pytest.mark.asyncio
async def test_replay_cooldown_returns_409_with_retry_after(
    client: httpx.AsyncClient, db: AsyncSession, user_factory, make_auth_client
):
    await truncate_all(db)
    user = await user_factory(github_id=9115, username="wh_health_cooldown")
    repo = Repo(user_id=user.id, owner="cooldown-org", name="cooldown-repo")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)

    delivery_id = str(uuid.uuid4())
    payload = workflow_run_payload(run_id=555201)
    payload["repository"]["name"] = "cooldown-repo"
    payload["repository"]["full_name"] = "cooldown-org/cooldown-repo"
    payload["repository"]["owner"]["login"] = "cooldown-org"
    raw_body = json.dumps(payload).encode("utf-8")

    with patch(
        "app.adapters.hosting.get_hosting_adapter", new_callable=AsyncMock
    ) as mock_get:
        mock_get.return_value = _mock_adapter()
        await client.post(
            "/webhooks/github",
            headers=_gh_headers("workflow_run", delivery_id, sign_payload(TEST_SECRET, raw_body)),
            content=raw_body,
        )

    original = (
        await db.execute(
            select(WebhookDelivery).where(WebhookDelivery.delivery_id == delivery_id)
        )
    ).scalar_one()

    auth_client = make_auth_client(user.id)
    try:
        with patch(
            "app.adapters.hosting.get_hosting_adapter", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = _mock_adapter()
            first = await auth_client.post(f"/webhooks/deliveries/{original.id}/replay")
        assert first.status_code == 200

        with patch(
            "app.adapters.hosting.get_hosting_adapter", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = _mock_adapter()
            second = await auth_client.post(f"/webhooks/deliveries/{original.id}/replay")
        assert second.status_code == 409
        assert "Retry-After" in second.headers
        assert second.json()["detail"]
    finally:
        await auth_client.aclose()


@pytest.mark.asyncio
async def test_replay_unretained_payload_returns_409(
    client: httpx.AsyncClient, db: AsyncSession, user_factory, make_auth_client
):
    await truncate_all(db)
    user = await user_factory(github_id=9116, username="wh_health_nopayload")
    repo = Repo(user_id=user.id, owner="nopayload-org", name="nopayload-repo")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)
    original = WebhookDelivery(
        event="push",
        delivery_id="del-nopayload-1",
        status="queued",
        reason="ok",
        repo="nopayload-org/nopayload-repo",
        repo_id=repo.id,
        payload=None,
    )
    db.add(original)
    await db.commit()
    await db.refresh(original)

    auth_client = make_auth_client(user.id)
    try:
        resp = await auth_client.post(f"/webhooks/deliveries/{original.id}/replay")
        assert resp.status_code == 409
        assert "replayed" in resp.json()["detail"].lower()
    finally:
        await auth_client.aclose()


@pytest.mark.asyncio
async def test_replay_refuses_corrupt_replay_buffer(
    client: httpx.AsyncClient, db: AsyncSession, user_factory, make_auth_client
):
    """A buffer that is not valid base64 must fail closed, before any work."""
    await truncate_all(db)
    user = await user_factory(github_id=9117, username="wh_health_corrupt")
    repo = Repo(user_id=user.id, owner="corrupt-org", name="corrupt-repo")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)
    original = WebhookDelivery(
        event="workflow_run",
        delivery_id="del-corrupt-1",
        status="queued",
        reason="ok",
        repo="corrupt-org/corrupt-repo",
        repo_id=repo.id,
        payload="this is not base64 !!!",
    )
    db.add(original)
    await db.commit()
    await db.refresh(original)

    auth_client = make_auth_client(user.id)
    try:
        with patch(
            "app.webhooks.github_webhook", new_callable=AsyncMock
        ) as mock_handler:
            resp = await auth_client.post(f"/webhooks/deliveries/{original.id}/replay")
        assert resp.status_code == 409
        assert "cannot be replayed" in resp.json()["detail"]
        mock_handler.assert_not_called()
    finally:
        await auth_client.aclose()


@pytest.mark.asyncio
async def test_replay_unknown_id_returns_404(
    client: httpx.AsyncClient, db: AsyncSession, user_factory, make_auth_client
):
    await truncate_all(db)
    user = await user_factory(github_id=9105, username="wh_health_404")
    auth_client = make_auth_client(user.id)
    try:
        resp = await auth_client.post(f"/webhooks/deliveries/{uuid.uuid4()}/replay")
        assert resp.status_code == 404
    finally:
        await auth_client.aclose()


@pytest.mark.asyncio
async def test_replay_cross_tenant_returns_404(
    client: httpx.AsyncClient, db: AsyncSession, user_factory, make_auth_client
):
    await truncate_all(db)
    owner = await user_factory(github_id=9106, username="wh_health_owner")
    intruder = await user_factory(github_id=9107, username="wh_health_intruder")
    repo = Repo(user_id=owner.id, owner="acme", name="private-app")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)
    original = WebhookDelivery(
        event="push",
        delivery_id="del-private-1",
        status="queued",
        reason="ok",
        repo="acme/private-app",
        repo_id=repo.id,
        payload=_encode_replay_buffer(b"{}"),
    )
    db.add(original)
    await db.commit()
    await db.refresh(original)

    intruder_client = make_auth_client(intruder.id)
    try:
        resp = await intruder_client.post(f"/webhooks/deliveries/{original.id}/replay")
        assert resp.status_code == 404
    finally:
        await intruder_client.aclose()


@pytest.mark.asyncio
async def test_queued_workflow_run_persists_delivery_row(
    client: httpx.AsyncClient, db: AsyncSession, user_factory
):
    await truncate_all(db)
    user = await user_factory(github_id=9108, username="wh_health_persist")
    repo = Repo(user_id=user.id, owner="persist-org", name="persist-repo")
    db.add(repo)
    await db.commit()

    delivery_id = str(uuid.uuid4())
    raw_body = json.dumps(workflow_run_payload()).encode("utf-8")
    with patch(
        "app.adapters.hosting.get_hosting_adapter", new_callable=AsyncMock
    ) as mock_get:
        mock_get.return_value = _mock_adapter()
        resp = await client.post(
            "/webhooks/github",
            headers=_gh_headers("workflow_run", delivery_id, sign_payload(TEST_SECRET, raw_body)),
            content=raw_body,
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"

    rows = (
        (
            await db.execute(
                select(WebhookDelivery).where(WebhookDelivery.delivery_id == delivery_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].event == "workflow_run"
    assert rows[0].status == "queued"
    assert rows[0].repo == "persist-org/persist-repo"
    # The replay buffer is the verified body, and nothing else.
    assert rows[0].payload == base64.b64encode(raw_body).decode("ascii")
    # No credential material is persisted anywhere on the row.
    serialized = json.dumps(
        {
            "event": rows[0].event,
            "delivery_id": rows[0].delivery_id,
            "status": rows[0].status,
            "reason": rows[0].reason,
            "repo": rows[0].repo,
        }
    )
    assert "sha256=" not in serialized
    assert TEST_SECRET not in serialized
    assert sign_payload(TEST_SECRET, raw_body) not in serialized


@pytest.mark.asyncio
async def test_replay_of_log_only_branch_is_anchored_and_rate_limited(
    client: httpx.AsyncClient, db: AsyncSession, user_factory, make_auth_client
):
    """A replay whose decision branch records no row of its own is still audited.

    The re-run below takes the "haunter fix branch" feedback-loop guard, which
    fires before the repo lookup and records nothing. Replay must still anchor a
    row, otherwise nothing would ever satisfy `replay_of == <original>` and the
    cooldown could be replayed forever.
    """
    await truncate_all(db)
    user = await user_factory(github_id=9118, username="wh_health_anchor")
    repo = Repo(user_id=user.id, owner="anchor-org", name="anchor-repo")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)

    payload = workflow_run_payload(run_id=555301, branch="haunter/fix-thing")
    payload["repository"]["name"] = "anchor-repo"
    payload["repository"]["full_name"] = "anchor-org/anchor-repo"
    payload["repository"]["owner"]["login"] = "anchor-org"
    raw_body = json.dumps(payload).encode("utf-8")

    original = WebhookDelivery(
        event="workflow_run",
        delivery_id=str(uuid.uuid4()),
        status="ignored",
        reason="unregistered repository",
        repo="anchor-org/anchor-repo",
        repo_id=repo.id,
        payload=_encode_replay_buffer(raw_body),
    )
    db.add(original)
    await db.commit()
    await db.refresh(original)

    auth_client = make_auth_client(user.id)
    try:
        first = await auth_client.post(f"/webhooks/deliveries/{original.id}/replay")
        assert first.status_code == 200
        body = first.json()
        assert body["decision"]["status"] == "ignored"
        assert "haunter fix branch" in body["decision"]["reason"]
        # The anchor row exists, so the audit trail and the cooldown both hold.
        assert body["replay_id"] is not None
        anchor = (
            await db.execute(
                select(WebhookDelivery).where(WebhookDelivery.id == body["replay_id"])
            )
        ).scalar_one()
        assert anchor.replay_of == original.id
        assert anchor.status == "replayed"

        second = await auth_client.post(f"/webhooks/deliveries/{original.id}/replay")
        assert second.status_code == 409
        assert "Retry-After" in second.headers
    finally:
        await auth_client.aclose()


@pytest.mark.asyncio
async def test_replay_stays_pinned_to_the_authorized_repo(
    client: httpx.AsyncClient, db: AsyncSession, user_factory, make_auth_client
):
    """Replay must not resolve another tenant's Repo for the same owner/name.

    `repos` allows two users to register the SAME owner/name, so the handler's
    owner/name lookup could land on the other user's row — which would then pick
    their settings, queue their work, and persist into their repo. The authorized
    repo id is pinned into the re-run, so the replay stays on the owner's own row
    and the run it queues belongs to that repo.
    """
    await truncate_all(db)
    owner = await user_factory(github_id=9119, username="wh_health_pin_owner")
    intruder = await user_factory(github_id=9120, username="wh_health_pin_intruder")
    # Same owner/name for both tenants — the ambiguity that used to leak.
    repo_owner = Repo(user_id=owner.id, owner="pin-org", name="pin-repo")
    repo_intruder = Repo(user_id=intruder.id, owner="pin-org", name="pin-repo")
    # The intruder's row is inserted FIRST so that the handler's unordered
    # `select(Repo).where(owner, name).scalars().first()` would pick it if the
    # authorized id were not pinned in. That is precisely the ambiguity being
    # pinned away — without the pin this test fails.
    db.add_all([repo_intruder, repo_owner])
    await db.commit()
    await db.refresh(repo_owner)
    await db.refresh(repo_intruder)

    payload = workflow_run_payload(run_id=555401)
    payload["repository"]["name"] = "pin-repo"
    payload["repository"]["full_name"] = "pin-org/pin-repo"
    payload["repository"]["owner"]["login"] = "pin-org"
    raw_body = json.dumps(payload).encode("utf-8")

    original = WebhookDelivery(
        event="workflow_run",
        delivery_id=str(uuid.uuid4()),
        status="ignored",
        reason="prior decision",
        repo="pin-org/pin-repo",
        repo_id=repo_owner.id,
        payload=_encode_replay_buffer(raw_body),
    )
    db.add(original)
    await db.commit()
    await db.refresh(original)

    auth_client = make_auth_client(owner.id)
    try:
        with patch(
            "app.adapters.hosting.get_hosting_adapter", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = _mock_adapter()
            resp = await auth_client.post(f"/webhooks/deliveries/{original.id}/replay")
        assert resp.status_code == 200
        assert resp.json()["decision"]["status"] == "queued"
    finally:
        await auth_client.aclose()

    from app.models import Run

    # The queued Run belongs to the owner's repo, never the intruder's.
    owner_runs = (
        (await db.execute(select(Run).where(Run.repo_id == repo_owner.id))).scalars().all()
    )
    intruder_runs = (
        (await db.execute(select(Run).where(Run.repo_id == repo_intruder.id)))
        .scalars()
        .all()
    )
    assert len(owner_runs) == 1
    assert intruder_runs == []


@pytest.mark.asyncio
async def test_queued_workflow_run_survives_a_failing_health_log_insert(
    client: httpx.AsyncClient, db: AsyncSession, user_factory
):
    """A health-log insert failure must not turn a scheduled run into a 500.

    _record_webhook_delivery() rolls the session back when the insert fails, and
    with production's `expire_on_commit=False` sessionmaker a rollback still
    expires every attribute of every loaded instance — including primary keys —
    so reading `new_run.<attr>` afterwards to build the response raises
    MissingGreenlet. The pipeline would already be scheduled at that point, so a
    500 also invites GitHub to redeliver and double the work.

    This asserts the response contract under that failure: the identifiers the
    caller needs are present. Because the test harness uses standard AsyncSession
    matching production, a rollback expires all instance attributes, meaning this
    test directly validates that pre-rollback snapshotting prevents MissingGreenlet.
    """
    await truncate_all(db)
    user = await user_factory(github_id=9121, username="wh_health_recorder_fail")
    db.add(Repo(user_id=user.id, owner="recorder-org", name="recorder-repo"))
    await db.commit()

    delivery_id = str(uuid.uuid4())
    payload = workflow_run_payload(run_id=555501)
    payload["repository"]["name"] = "recorder-repo"
    payload["repository"]["full_name"] = "recorder-org/recorder-repo"
    payload["repository"]["owner"]["login"] = "recorder-org"
    raw_body = json.dumps(payload).encode("utf-8")

    with (
        patch(
            "app.adapters.hosting.get_hosting_adapter", new_callable=AsyncMock
        ) as mock_get,
        patch(
            "app.webhooks._record_webhook_delivery", new_callable=AsyncMock
        ) as mock_record,
    ):
        mock_get.return_value = _mock_adapter()
        # Reproduce the real failure mode: the insert raises, and the handler's
        # own recovery path rolls the session back.
        async def _failing_record(session: AsyncSession, **kwargs: object) -> None:
            await session.rollback()

        mock_record.side_effect = _failing_record

        resp = await client.post(
            "/webhooks/github",
            headers=_gh_headers(
                "workflow_run", delivery_id, sign_payload(TEST_SECRET, raw_body)
            ),
            content=raw_body,
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued"
    # Both identifiers survived the rollback.
    assert body["run_id"]
    assert body["github_run_id"] == 555501
    run_in_db = await db.get(Run, uuid.UUID(body["run_id"]))
    assert run_in_db is not None
    assert run_in_db.github_run_id == 555501


@pytest.mark.asyncio
async def test_unsnapshotted_orm_attribute_access_after_rollback_raises_missing_greenlet(
    db: AsyncSession, user_factory
):
    """Explicit regression test proving that accessing un-snapshotted model attributes
    post-rollback raises MissingGreenlet.

    In SQLAlchemy 2.0 with standard AsyncSession (and expire_on_commit=False), calling
    await session.rollback() unconditionally expires all loaded ORM attributes across all
    instances in the session. Outside of an active greenlet or async refresh, accessing an
    ORM attribute (including primary keys or columns) triggers lazy loading via the sync
    accessor, which fails immediately with MissingGreenlet. This proves that snapshotting
    run_id and github_run_id before invoking _record_webhook_delivery() is strictly necessary
    to prevent turning health-log recording failures into 500 errors.
    """
    await truncate_all(db)
    user = await user_factory(github_id=9122, username="wh_greenlet_test_user")
    repo = Repo(user_id=user.id, owner="greenlet-org", name="greenlet-repo")
    db.add(repo)
    await db.commit()

    run = Run(
        repo_id=repo.id,
        github_run_id=555502,
        github_delivery_id=str(uuid.uuid4()),
        head_sha="0123456789abcdef0123456789abcdef01234567",
        head_branch="main",
        status="pending",
        conclusion="failure",
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    # Pre-rollback: attributes are present in memory and safely accessible
    assert run.id is not None
    assert run.github_run_id == 555502

    # Snapshot values beforehand (as the webhook handler does at app/webhooks.py:2760)
    snapshotted_run_id = str(run.id)
    snapshotted_github_run_id = run.github_run_id

    # Simulate what _record_webhook_delivery() does on any DB insertion failure:
    await db.rollback()

    # The snapshotted scalars are unaffected by the rollback:
    assert snapshotted_run_id
    assert snapshotted_github_run_id == 555502

    # But accessing un-snapshotted ORM attributes on the post-rollback instance
    # triggers lazy loading on the async session, raising MissingGreenlet:
    with pytest.raises(MissingGreenlet):
        _ = run.id

    with pytest.raises(MissingGreenlet):
        _ = run.github_run_id


@pytest.mark.asyncio
async def test_queued_workflow_run_survives_real_health_log_recording_failure(
    client: httpx.AsyncClient, db: AsyncSession, user_factory, caplog
):
    """Production-shaped coverage: webhook delivery survives real health-log DB failure.

    Unlike test_queued_workflow_run_survives_a_failing_health_log_insert which mocks
    _record_webhook_delivery entirely, this test exercises the real _record_webhook_delivery()
    code path with an unmodified AsyncSession.

    When inserting the WebhookDelivery row fails at commit time, _record_webhook_delivery()
    catches the DB exception, logs a warning, and executes await db.rollback(). The rollback
    unconditionally expires all loaded ORM instances in the session (including the newly created
    Run). Because run_id and github_run_id were snapshotted into local scalar variables prior to
    invoking _record_webhook_delivery(), the handler successfully builds and returns a 200 queued
    response without triggering MissingGreenlet.
    """
    await truncate_all(db)
    user = await user_factory(github_id=9123, username="wh_real_health_recorder_fail")
    db.add(Repo(user_id=user.id, owner="real-fail-org", name="real-fail-repo"))
    await db.commit()

    delivery_id = str(uuid.uuid4())
    payload = workflow_run_payload(run_id=555503)
    payload["repository"]["name"] = "real-fail-repo"
    payload["repository"]["full_name"] = "real-fail-org/real-fail-repo"
    payload["repository"]["owner"]["login"] = "real-fail-org"
    raw_body = json.dumps(payload).encode("utf-8")

    real_commit = AsyncSession.commit

    async def _failing_health_commit(session_self: AsyncSession, *args, **kwargs):
        # Allow Run insert commit to succeed; fail specifically when WebhookDelivery is pending
        for obj in session_self.new:
            if isinstance(obj, WebhookDelivery):
                raise OperationalError(
                    "COMMIT", {}, Exception("simulated DB health log failure")
                )
        return await real_commit(session_self, *args, **kwargs)

    with (
        patch(
            "app.adapters.hosting.get_hosting_adapter", new_callable=AsyncMock
        ) as mock_get,
        patch.object(AsyncSession, "commit", _failing_health_commit),
        caplog.at_level("WARNING", logger="app.webhooks"),
    ):
        mock_get.return_value = _mock_adapter()

        resp = await client.post(
            "/webhooks/github",
            headers=_gh_headers(
                "workflow_run", delivery_id, sign_payload(TEST_SECRET, raw_body)
            ),
            content=raw_body,
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued"
    assert body["run_id"]
    assert body["github_run_id"] == 555503

    # Verify the Run row was committed to the DB
    run_in_db = await db.get(Run, uuid.UUID(body["run_id"]))
    assert run_in_db is not None
    assert run_in_db.github_run_id == 555503

    # Verify WebhookDelivery was rolled back and never persisted
    delivery_row = (
        await db.execute(
            select(WebhookDelivery).where(WebhookDelivery.delivery_id == delivery_id)
        )
    ).scalar_one_or_none()
    assert delivery_row is None

    # Verify _record_webhook_delivery logged the expected warning on rollback
    assert "webhook delivery health-log insert failed" in caplog.text


@pytest.mark.asyncio
async def test_public_webhook_endpoint_exposes_no_repository_pin(
    client: httpx.AsyncClient, db: AsyncSession, user_factory
):
    """The replay Repo pin must not be reachable as a query parameter.

    Any plain parameter on a FastAPI route becomes an attacker-controlled query
    parameter. `/webhooks/github` is public and secured only by HMAC, so a
    `replay_repo_id` parameter would let anyone pair a valid signed payload for
    repo A with repo B's id and drive B's settings and writes — with no
    authentication at all. The pin is passed internally via a ContextVar, so the
    OpenAPI schema must not advertise it.
    """
    from main import app

    params = app.openapi()["paths"]["/webhooks/github"]["post"].get("parameters", [])
    assert [p["name"] for p in params] == [
        "X-GitHub-Delivery",
        "X-GitHub-Event",
        "X-Hub-Signature-256",
    ]

    # And behaviourally: the query string is ignored, so the delivery resolves by
    # its own payload's owner/name like any other live delivery.
    await truncate_all(db)
    owner = await user_factory(github_id=9122, username="wh_health_no_pin")
    intruder = await user_factory(github_id=9123, username="wh_health_no_pin_intruder")
    repo_owner = Repo(user_id=owner.id, owner="nopin-org", name="nopin-repo")
    # A DIFFERENT owner/name, so the payload's identity matches only repo_owner.
    # If the query parameter were honoured and forced resolution onto
    # repo_intruder, the run would land there and the assertions below fail —
    # which an intruder sharing repo_owner's name could not detect, because the
    # unordered owner/name lookup would have returned repo_owner anyway.
    repo_intruder = Repo(user_id=intruder.id, owner="nopin-intruder", name="other-repo")
    db.add_all([repo_owner, repo_intruder])
    await db.commit()
    await db.refresh(repo_owner)
    await db.refresh(repo_intruder)

    payload = workflow_run_payload(run_id=555601)
    payload["repository"]["name"] = "nopin-repo"
    payload["repository"]["full_name"] = "nopin-org/nopin-repo"
    payload["repository"]["owner"]["login"] = "nopin-org"
    raw_body = json.dumps(payload).encode("utf-8")

    with patch(
        "app.adapters.hosting.get_hosting_adapter", new_callable=AsyncMock
    ) as mock_get:
        mock_get.return_value = _mock_adapter()
        resp = await client.post(
            f"/webhooks/github?replay_repo_id={repo_intruder.id}",
            headers=_gh_headers(
                "workflow_run", str(uuid.uuid4()), sign_payload(TEST_SECRET, raw_body)
            ),
            content=raw_body,
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"

    from app.models import Run

    # The intruder-supplied pin had no effect. The payload names
    # nopin-org/nopin-repo, which resolves to repo_owner, so the run must be
    # there and nowhere else — a strict assertion, not "either repo".
    runs = (
        (await db.execute(select(Run).where(Run.github_run_id == 555601))).scalars().all()
    )
    assert len(runs) == 1
    assert runs[0].repo_id == repo_owner.id
    assert (
        await db.execute(
            select(Run).where(Run.repo_id == repo_intruder.id)
        )
    ).scalars().all() == []


@pytest.mark.asyncio
async def test_ambiguous_registration_is_refused_and_dispatches_nothing(
    client: httpx.AsyncClient, db: AsyncSession, user_factory
):
    """Two tenants on the SAME owner/name must not be resolved by guesswork.

    Against a real database, so the row set the unordered lookup returns — and
    therefore the insert order the pre-fix `.scalars().first()` would have
    favoured — is genuinely whatever Postgres produced. `repos` is unique per
    (user_id, owner, name), so this state is legal; the delivery must be refused,
    not delivered into whichever row came back first.
    """
    await truncate_all(db)
    tenant_a = await user_factory(github_id=9130, username="wh_health_amb_a")
    tenant_b = await user_factory(github_id=9131, username="wh_health_amb_b")
    repo_a = Repo(user_id=tenant_a.id, owner="amb-org", name="amb-repo")
    repo_b = Repo(user_id=tenant_b.id, owner="amb-org", name="amb-repo")
    db.add_all([repo_a, repo_b])
    await db.commit()
    await db.refresh(repo_a)
    await db.refresh(repo_b)

    delivery_id = str(uuid.uuid4())
    payload = workflow_run_payload(run_id=555701)
    payload["repository"]["name"] = "amb-repo"
    payload["repository"]["full_name"] = "amb-org/amb-repo"
    payload["repository"]["owner"]["login"] = "amb-org"
    raw_body = json.dumps(payload).encode("utf-8")

    adapter = _mock_adapter()
    with patch(
        "app.adapters.hosting.get_hosting_adapter", new_callable=AsyncMock
    ) as mock_get:
        mock_get.return_value = adapter
        resp = await client.post(
            "/webhooks/github",
            headers=_gh_headers(
                "workflow_run", delivery_id, sign_payload(TEST_SECRET, raw_body)
            ),
            content=raw_body,
        )

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ignored",
        "reason": "ambiguous repository registration",
    }
    adapter.schedule_pipeline.assert_not_called()

    from app.models import Run

    assert (
        await db.execute(select(Run).where(Run.github_run_id == 555701))
    ).scalars().all() == []
    for repo in (repo_a, repo_b):
        assert (
            await db.execute(select(Run).where(Run.repo_id == repo.id))
        ).scalars().all() == []

    # One diagnostic row, attributed to no tenant — neither tenant may see it in
    # its own delivery history, and it must name neither of them.
    rows = (
        (
            await db.execute(
                select(WebhookDelivery).where(
                    WebhookDelivery.delivery_id == delivery_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].status == "ignored"
    assert rows[0].reason == "ambiguous repository registration"
    assert rows[0].repo_id is None
    assert rows[0].payload is None
    assert str(repo_a.id) not in (rows[0].reason or "")
    assert str(repo_b.id) not in (rows[0].reason or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse_insert_order", [False, True])
async def test_ambiguous_registration_outcome_is_stable_across_redeliveries(
    client: httpx.AsyncClient,
    db: AsyncSession,
    user_factory,
    reverse_insert_order: bool,
):
    """Redelivering the same ambiguous delivery N times changes nothing.

    Run for both insert orders, because the pre-fix lookup had no ORDER BY: which
    tenant won was whatever Postgres returned first, so the losing tenant could
    change from one delivery to the next. Refusing is deterministic, so every
    redelivery must produce the identical decision and the identical absence of
    work, whichever row was inserted first.
    """
    await truncate_all(db)
    tenant_a = await user_factory(github_id=9132, username="wh_health_stable_a")
    tenant_b = await user_factory(github_id=9133, username="wh_health_stable_b")
    repos = [
        Repo(user_id=tenant_a.id, owner="stable-org", name="stable-repo"),
        Repo(user_id=tenant_b.id, owner="stable-org", name="stable-repo"),
    ]
    db.add_all(list(reversed(repos)) if reverse_insert_order else repos)
    await db.commit()

    decisions = []
    for _ in range(5):
        delivery_id = str(uuid.uuid4())
        payload = workflow_run_payload(run_id=555702)
        payload["repository"]["name"] = "stable-repo"
        payload["repository"]["full_name"] = "stable-org/stable-repo"
        payload["repository"]["owner"]["login"] = "stable-org"
        raw_body = json.dumps(payload).encode("utf-8")
        with patch(
            "app.adapters.hosting.get_hosting_adapter", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = _mock_adapter()
            resp = await client.post(
                "/webhooks/github",
                headers=_gh_headers(
                    "workflow_run", delivery_id, sign_payload(TEST_SECRET, raw_body)
                ),
                content=raw_body,
            )
        decisions.append((resp.status_code, json.dumps(resp.json(), sort_keys=True)))

    assert len(set(decisions)) == 1, decisions
    assert decisions[0][1] == json.dumps(
        {"reason": "ambiguous repository registration", "status": "ignored"},
        sort_keys=True,
    )

    # Nothing ran, on either tenant, in any of the five deliveries.
    from app.models import Run

    assert (await db.execute(select(Run))).scalars().all() == []


def _mock_adapter() -> MagicMock:
    adapter = MagicMock()
    adapter.schedule_pipeline = AsyncMock()
    adapter.schedule_review = AsyncMock()
    return adapter


def _gh_headers(event: str, delivery_id: str, signature: str) -> dict[str, str]:
    return {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery_id,
        "X-Hub-Signature-256": signature,
    }


# ---------------------------------------------------------------------------
# Rollback pre-snapshot regression tests (issue #194 / PR #196, Greptile P2)
# ---------------------------------------------------------------------------
# One failure mode sits behind all four pre-snapshot fixes on this branch:
# _record_webhook_delivery() rolls the session back when its insert fails, and
# a rollback expires every loaded ORM instance — so reading an ORM attribute
# afterwards raises MissingGreenlet and turns a duplicate/queued answer into a
# 500. Each fix snapshots the needed columns into locals BEFORE the recorder.
#
# These hermetic tests drive the real handlers with an expiring stand-in whose
# attribute reads raise the real MissingGreenlet after rollback. Each test
# fails on the pre-fix code (post-rollback ORM read) and passes with the
# snapshot in place. They run in the default fast suite (no TEST_DATABASE_URL).


class _ExpiringAttrs:
    """Minimal stand-in for a loaded ORM instance with rollback-expiry semantics.

    Attribute reads behave like column access until expire() is called (the
    test's equivalent of the recorder's rollback path), after which they raise
    the real MissingGreenlet — exactly what SQLAlchemy raises for expired
    attributes outside a greenlet. Writes never raise, matching ORM behaviour.
    """

    def __init__(self, **attrs: object) -> None:
        self.__dict__["_attrs"] = dict(attrs)
        self.__dict__["_expired"] = False

    def expire(self) -> None:
        self.__dict__["_expired"] = True

    def __getattr__(self, name: str) -> object:
        if self.__dict__["_expired"]:
            raise MissingGreenlet(f"expired attribute {name!r}")
        try:
            return self.__dict__["_attrs"][name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name: str, value: object) -> None:
        self.__dict__["_attrs"][name] = value


def _issue_trigger_gate_mocks() -> tuple[MagicMock, MagicMock]:
    """Read-only gates ahead of the duplicate branches of _handle_issue_trigger."""
    mock_audit = MagicMock()
    mock_audit.is_manual_audit_comment.return_value = False
    mock_enforcement = MagicMock()
    mock_enforcement.is_auto_fix_allowed.return_value = MagicMock(
        allowed=True, reason="ok"
    )
    mock_enforcement.check_cost_ceiling.return_value = MagicMock(
        allowed=True, reason="ok"
    )
    return mock_audit, mock_enforcement


@pytest.mark.asyncio
async def test_duplicate_issue_trigger_survives_failing_health_log_insert():
    """Pre-rollback snapshot (de77a60): the throttled-duplicate answer keeps ids.

    The 6b branch snapshots existing_issue_run's id/pr_url/pr_number before
    _record_webhook_delivery(); its failure path rolls back and expires the
    row. Pre-fix, building the reason/response afterwards raised
    MissingGreenlet and turned a duplicate into a 500.
    """
    from types import SimpleNamespace

    existing_id = uuid.uuid4()
    existing = _ExpiringAttrs(
        id=existing_id, pr_url="https://example/pr/1", pr_number=1
    )
    repo = SimpleNamespace(id=uuid.uuid4(), default_branch="main")
    db = MagicMock()
    db.rollback = AsyncMock(side_effect=lambda: existing.expire())

    async def _failing_record(session: AsyncSession, **kwargs: object) -> None:
        await session.rollback()

    mock_audit, mock_enforcement = _issue_trigger_gate_mocks()
    with (
        patch("app.webhooks.has_issue_trigger", return_value=True),
        patch("app.webhooks.has_auditor_mention", return_value=False),
        patch("app.webhooks.audit_pipeline", mock_audit),
        patch(
            "app.webhooks._resolve_repo_for_delivery", new_callable=AsyncMock
        ) as mock_resolve,
        patch("app.webhooks.get_repo_settings", new_callable=AsyncMock) as mock_settings,
        patch("app.webhooks.feature_enforcement", mock_enforcement),
        patch(
            "app.webhooks._find_existing_issue_root_run", new_callable=AsyncMock
        ) as mock_find,
        patch("app.webhooks._post_issue_trigger_ack", new_callable=AsyncMock),
        patch(
            "app.webhooks._record_webhook_delivery", new_callable=AsyncMock
        ) as mock_record,
    ):
        mock_resolve.return_value = (repo, None)
        mock_settings.return_value = SimpleNamespace(
            enable_pr_comments=True, enable_auto_fix=True, max_cost_per_run_cents=10000
        )
        mock_find.return_value = existing
        mock_record.side_effect = _failing_record
        result = await _handle_issue_trigger(
            db,
            MagicMock(),
            event="issues",
            delivery_id="del-dup-issue-1",
            repo_owner="acme",
            repo_name="app",
            issue_number=7,
            trigger_text="@haunter fix",
            trigger_comment_id=99,
            author_association="MEMBER",
            author_login="alice",
            sender={},
            raw_body=b"{}",
            replay_repo_id=None,
        )

    assert result["status"] == "duplicate"
    assert result["run_id"] == str(existing_id)
    assert result["pr_url"] == "https://example/pr/1"
    assert result["pr_number"] == 1


@pytest.mark.asyncio
async def test_integrity_error_duplicate_issue_trigger_survives_failing_health_log():
    """Pre-rollback snapshot (d6587e0): the IntegrityError duplicate keeps ids.

    Same failure mode as above on the second duplicate branch: after the
    colliding insert rolls back, the re-selected row is snapshotted before the
    recorder, whose own failure rolls back again. Pre-fix, the response read
    the expired row and raised MissingGreenlet.
    """
    from types import SimpleNamespace

    existing_id = uuid.uuid4()
    existing = _ExpiringAttrs(
        id=existing_id, pr_url="https://example/pr/2", pr_number=2
    )
    repo = SimpleNamespace(id=uuid.uuid4(), default_branch="main")
    db = MagicMock()
    rollbacks = {"n": 0}

    async def _rollback() -> None:
        # First rollback is the IntegrityError recovery (row not yet loaded);
        # the recorder's rollback afterwards must expire it.
        rollbacks["n"] += 1
        if rollbacks["n"] >= 2:
            existing.expire()

    async def _failing_record(session: AsyncSession, **kwargs: object) -> None:
        await session.rollback()

    scalars = MagicMock()
    scalars.first.return_value = existing
    result_proxy = MagicMock()
    result_proxy.scalars.return_value = scalars
    db.rollback = AsyncMock(side_effect=_rollback)
    db.commit = AsyncMock(
        side_effect=IntegrityError("INSERT", {}, Exception("dup delivery"))
    )
    db.execute = AsyncMock(return_value=result_proxy)

    mock_audit, mock_enforcement = _issue_trigger_gate_mocks()
    with (
        patch("app.webhooks.has_issue_trigger", return_value=True),
        patch("app.webhooks.has_auditor_mention", return_value=False),
        patch("app.webhooks.audit_pipeline", mock_audit),
        patch(
            "app.webhooks._resolve_repo_for_delivery", new_callable=AsyncMock
        ) as mock_resolve,
        patch("app.webhooks.get_repo_settings", new_callable=AsyncMock) as mock_settings,
        patch("app.webhooks.feature_enforcement", mock_enforcement),
        patch(
            "app.webhooks._find_existing_issue_root_run", new_callable=AsyncMock
        ) as mock_find,
        patch(
            "app.github.pr.get_installation_token", new_callable=AsyncMock
        ) as mock_token,
        patch(
            "app.github_client.fetch_branch_sha", new_callable=AsyncMock
        ) as mock_sha,
        patch(
            "app.webhooks._record_webhook_delivery", new_callable=AsyncMock
        ) as mock_record,
    ):
        mock_resolve.return_value = (repo, None)
        mock_settings.return_value = SimpleNamespace(
            enable_pr_comments=True, enable_auto_fix=True, max_cost_per_run_cents=10000
        )
        mock_find.return_value = None
        mock_token.return_value = "tok"
        mock_sha.return_value = "0" * 40
        mock_record.side_effect = _failing_record
        result = await _handle_issue_trigger(
            db,
            MagicMock(),
            event="issue_comment",
            delivery_id="del-dup-issue-2",
            repo_owner="acme",
            repo_name="app",
            issue_number=8,
            trigger_text="@haunter fix",
            trigger_comment_id=100,
            author_association="MEMBER",
            author_login="alice",
            sender={},
            raw_body=b"{}",
            replay_repo_id=None,
        )

    assert result["status"] == "duplicate"
    assert result["run_id"] == str(existing_id)
    assert result["pr_url"] == "https://example/pr/2"
    assert result["pr_number"] == 2


@pytest.mark.asyncio
async def test_auditor_background_worker_survives_failing_health_log_insert():
    """Pre-rollback snapshot (d79a7ae): the auditor worker uses copied columns.

    The worker snapshots the repo's id/install/owner/name before the running
    recorder, whose failure path expires the ORM row. Pre-fix, the mention
    handler and the terminal recorder read the expired row and raised
    MissingGreenlet, losing the answer; post-fix both use the snapshot.
    """
    repo_id = uuid.uuid4()
    repo = _ExpiringAttrs(
        id=repo_id, github_install_id=4242, owner="acme", name="app"
    )
    db = MagicMock()
    db.get = AsyncMock(return_value=repo)
    db.rollback = AsyncMock(side_effect=lambda: repo.expire())
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=db)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    record_calls: list[dict] = []

    async def _failing_record(session: AsyncSession, **kwargs: object) -> None:
        record_calls.append(dict(kwargs))
        await session.rollback()

    captured: dict = {}

    async def _fake_mention(
        session: AsyncSession, repo_arg: object, *args: object, **kwargs: object
    ) -> dict:
        captured["repo"] = repo_arg
        return {"status": "auditor_replied", "channel": "pr_comment"}

    with (
        patch("app.db.async_session_maker", return_value=session_cm),
        patch(
            "app.webhooks._record_webhook_delivery", new_callable=AsyncMock
        ) as mock_record,
        patch("app.webhooks._handle_auditor_mention", side_effect=_fake_mention),
    ):
        mock_record.side_effect = _failing_record
        result = await _run_auditor_mention_background(
            repo_id=repo_id,
            repo_owner="acme",
            repo_name="app",
            pr_number=1,
            comment_id=2,
            comment_body="hello",
            comment_login="alice",
            author_association="MEMBER",
            in_reply_to_id=None,
            event="issue_comment",
            delivery_id="del-audit-1",
        )

    assert result is None
    # Running row + terminal success row; pre-fix the terminal write blew up on
    # the expired row and the worker fell into the error path instead.
    assert len(record_calls) == 2
    assert record_calls[1]["status_value"] == "auditor_replied"
    assert all(call["repo_id"] == repo_id for call in record_calls)
    # Whatever the handler received stays readable with the right values —
    # pre-fix this was the expired ORM row and every read raised.
    snapshot = captured["repo"]
    assert snapshot.id == repo_id
    assert snapshot.github_install_id == 4242
    assert snapshot.owner == "acme"
    assert snapshot.name == "app"


@pytest.mark.asyncio
async def test_dispatch_review_failure_survives_rollback_expiry():
    """Pre-rollback snapshot (e5b2543): dispatch failure logs the copied id.

    schedule_review raises, the terminal write's commit fails, and the rollback
    expires `review`. Pre-fix, the log lines read review.id afterwards and
    raised MissingGreenlet instead of the retryable 503.
    """
    review_id = uuid.uuid4()
    review = _ExpiringAttrs(id=review_id, status="pending", failure_reason=None)
    adapter = MagicMock()
    adapter.schedule_review = AsyncMock(side_effect=RuntimeError("lambda 500"))
    db = MagicMock()
    db.commit = AsyncMock(side_effect=Exception("commit failed"))
    db.rollback = AsyncMock(side_effect=lambda: review.expire())
    logged: dict = {}

    def _capture_log(*args: object, reason: object = None, **kwargs: object) -> None:
        logged["reason"] = reason

    with (
        patch(
            "app.adapters.hosting.get_hosting_adapter", new_callable=AsyncMock
        ) as mock_get,
        patch("app.webhooks._log_webhook_decision", side_effect=_capture_log),
    ):
        mock_get.return_value = adapter
        with pytest.raises(HTTPException) as exc_info:
            await _dispatch_review(
                db,
                review,
                MagicMock(),
                event="pull_request",
                delivery_id="del-rev-1",
                owner="acme",
                repo_name="app",
            )

    assert exc_info.value.status_code == 503
    assert f"review_id={review_id}" in str(logged["reason"])
    assert "RuntimeError" in str(logged["reason"])
