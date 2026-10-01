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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Repo, WebhookDelivery
from app.webhooks import (
    _WEBHOOK_PAYLOAD_MAX_BYTES,
    WebhookDeliveryListOut,
    WebhookDeliveryOut,
    _decode_replay_buffer,
    _encode_replay_buffer,
    _log_webhook_decision,
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
