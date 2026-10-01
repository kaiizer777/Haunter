"""
Feature 8 — Webhook health + replay tests.

Fast (hermetic, no DB):
- reason truncation bounds
- response DTO validation
- _log_webhook_decision sanitization (no crash on hostile input)

DB (require TEST_DATABASE_URL, run in CI / --all / explicit target):
- GET /webhooks/deliveries requires auth, tenant-scoped, paginated
- POST /webhooks/deliveries/{id}/replay creates audit-only replay row
- replay of unknown / cross-tenant row -> 404 (no oracle)
- queued workflow_run persists a webhook_deliveries row
"""

import hashlib
import hmac
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Repo, WebhookDelivery
from app.webhooks import (
    WebhookDeliveryListOut,
    WebhookDeliveryOut,
    _log_webhook_decision,
    _truncate_reason,
)
from tests.conftest import truncate_all

TEST_SECRET = settings.github_webhook_secret or "test_webhook_secret_key_12345"


def sign_payload(secret: str, raw_body: bytes) -> str:
    sig = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


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


# ---------------------------------------------------------------------------
# DB tests (CI / explicit target)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_deliveries_requires_auth(client: httpx.AsyncClient):
    resp = await client.get("/webhooks/deliveries")
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
async def test_replay_creates_audit_only_row(
    client: httpx.AsyncClient, db: AsyncSession, user_factory, make_auth_client
):
    await truncate_all(db)
    user = await user_factory(github_id=9104, username="wh_health_replay")
    repo = Repo(user_id=user.id, owner="acme", name="replay-app")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)
    original = WebhookDelivery(
        event="workflow_run",
        delivery_id="del-replay-1",
        status="queued",
        reason="original",
        repo="acme/replay-app",
        repo_id=repo.id,
    )
    db.add(original)
    await db.commit()
    await db.refresh(original)

    auth_client = make_auth_client(user.id)
    try:
        resp = await auth_client.post(f"/webhooks/deliveries/{original.id}/replay")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "replayed"
        assert body["delivery_id"] == "del-replay-1"
        assert body["repo"] == "acme/replay-app"
        assert f"replay of {original.id}" in (body["reason"] or "")

        # Original row untouched; replay is a NEW row (audit-only, no Run created).
        rows = (
            (await db.execute(select(WebhookDelivery).where(WebhookDelivery.delivery_id == "del-replay-1")))
            .scalars()
            .all()
        )
        assert len(rows) == 2
        statuses = sorted(r.status for r in rows)
        assert statuses == ["queued", "replayed"]
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
    payload = {
        "action": "completed",
        "workflow_run": {
            "id": 555001,
            "head_sha": "0123456789abcdef0123456789abcdef01234567",
            "head_branch": "main",
            "conclusion": "failure",
        },
        "repository": {
            "name": "persist-repo",
            "full_name": "persist-org/persist-repo",
            "owner": {"login": "persist-org"},
        },
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = sign_payload(TEST_SECRET, raw_body)
    with patch(
        "app.adapters.hosting.get_hosting_adapter", new_callable=AsyncMock
    ) as mock_get:
        mock_adapter = MagicMock()
        mock_adapter.schedule_pipeline = AsyncMock()
        mock_get.return_value = mock_adapter
        resp = await client.post(
            "/webhooks/github",
            headers={
                "X-GitHub-Event": "workflow_run",
                "X-GitHub-Delivery": delivery_id,
                "X-Hub-Signature-256": sig,
            },
            content=raw_body,
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"

    rows = (
        (await db.execute(select(WebhookDelivery).where(WebhookDelivery.delivery_id == delivery_id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].event == "workflow_run"
    assert rows[0].status == "queued"
    assert rows[0].repo == "persist-org/persist-repo"
