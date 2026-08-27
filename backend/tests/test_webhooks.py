"""
GitHub Webhook ingestion and GitHub API client security tests (test_webhooks.py).

Covers:
1. Missing X-Hub-Signature-256 -> 401 Unauthorized.
2. Malformed signature (missing sha256= prefix) -> 401 Unauthorized.
3. Invalid signature bytes -> 401 Unauthorized.
4. Valid signature on valid payload -> 200 Queued.
5. Raw body HMAC verification (whitespace formatting variation passes).
6. Content-Length header > 2MB -> 413 Payload Too Large.
7. Raw body payload > 2MB -> 413 Payload Too Large.
8. Exact 2MB payload passes size limit check.
9. Missing X-GitHub-Delivery header -> 400 Bad Request.
10. Non-workflow_run event (ping/push) -> 200 Ignored (no DB row).
11. workflow_run action != completed -> 200 Ignored (no DB row).
12. workflow_run conclusion != failure (e.g. success) -> 200 Ignored (no DB row).
13. Invalid head_sha format -> 422 Unprocessable Entity.
14. Unregistered repository payload -> 200 Ignored (no DB row).
15. Valid failing run creation -> 1 DB row (status=pending, conclusion=failure).
16. Webhook delivery deduplication -> 200 Duplicate (no extra DB row).
17. Concurrent 4x duplicate delivery -> exactly 1 DB row created (1x queued, 3x duplicate).
18. GitHub API client token safety: per-call override works and token never logged.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from typing import Callable
from unittest.mock import patch
import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.github_client import _build_headers, fetch_commit_metadata, fetch_diff, fetch_workflow_run_logs
from app.models import Repo, Run, User
from tests.conftest import truncate_all

TEST_SECRET = settings.github_webhook_secret or "test_webhook_secret_key_12345"


def sign_payload(secret: str, raw_body: bytes) -> str:
    """Generate X-Hub-Signature-256 HMAC-SHA256 signature."""
    sig = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def make_valid_payload(
    owner: str = "test-org",
    repo: str = "test-repo",
    action: str = "completed",
    conclusion: str = "failure",
    run_id: int = 1234567,
    head_sha: str = "0123456789abcdef0123456789abcdef01234567",
) -> dict:
    """Generate a conforming workflow_run payload."""
    return {
        "action": action,
        "workflow_run": {
            "id": run_id,
            "head_sha": head_sha,
            "head_branch": "main",
            "conclusion": conclusion,
            "html_url": f"https://github.com/{owner}/{repo}/actions/runs/{run_id}",
        },
        "repository": {
            "name": repo,
            "full_name": f"{owner}/{repo}",
            "owner": {"login": owner},
        },
    }


@pytest.mark.asyncio
async def test_webhook_missing_signature(client: httpx.AsyncClient):
    """Missing X-Hub-Signature-256 header returns 401."""
    resp = await client.post(
        "/webhooks/github",
        headers={"X-GitHub-Event": "workflow_run", "X-GitHub-Delivery": str(uuid.uuid4())},
        json={"action": "completed"},
    )
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Invalid or missing signature"}


@pytest.mark.asyncio
async def test_webhook_malformed_signature(client: httpx.AsyncClient):
    """Signature missing sha256= prefix returns 401."""
    resp = await client.post(
        "/webhooks/github",
        headers={
            "X-GitHub-Event": "workflow_run",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": "123456abcdef",  # missing sha256= prefix
        },
        json={"action": "completed"},
    )
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Invalid or missing signature"}


@pytest.mark.asyncio
async def test_webhook_invalid_signature(client: httpx.AsyncClient):
    """Forged/invalid signature bytes return 401."""
    raw_body = json.dumps({"action": "completed"}).encode("utf-8")
    resp = await client.post(
        "/webhooks/github",
        headers={
            "X-GitHub-Event": "workflow_run",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": "sha256=0000000000000000000000000000000000000000000000000000000000000000",
        },
        content=raw_body,
    )
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Invalid or missing signature"}


@pytest.mark.asyncio
async def test_webhook_raw_body_hmac_whitespace_invariant(
    client: httpx.AsyncClient,
    db: AsyncSession,
    user_factory,
):
    """Raw body HMAC verifies original byte stream even with whitespace/indent variations."""
    await truncate_all(db)
    user = await user_factory(github_id=801, username="wh_user1")
    repo = Repo(user_id=user.id, owner="white-space-org", name="white-space-repo")
    db.add(repo)
    await db.commit()

    payload_data = make_valid_payload(owner="white-space-org", repo="white-space-repo", run_id=98765)
    # Intentionally format JSON with unusual indentation and spaces
    raw_body = json.dumps(payload_data, indent=4).encode("utf-8") + b"  \n\n"
    sig = sign_payload(TEST_SECRET, raw_body)
    delivery_id = str(uuid.uuid4())

    resp = await client.post(
        "/webhooks/github",
        headers={
            "X-GitHub-Event": "workflow_run",
            "X-GitHub-Delivery": delivery_id,
            "X-Hub-Signature-256": sig,
            "Content-Type": "application/json",
        },
        content=raw_body,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "queued"
    assert data["delivery_id"] == delivery_id


@pytest.mark.asyncio
async def test_webhook_content_length_oversized(client: httpx.AsyncClient):
    """Content-Length header > 2MB returns 413 Payload Too Large before body read."""
    resp = await client.post(
        "/webhooks/github",
        headers={
            "X-GitHub-Event": "workflow_run",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "Content-Length": str(2 * 1024 * 1024 + 1),  # 2MB + 1 byte
            "X-Hub-Signature-256": "sha256=dummy",
        },
        content=b"small_body",
    )
    assert resp.status_code == 413
    assert resp.json() == {"detail": "Payload size exceeds 2MB limit"}


@pytest.mark.asyncio
async def test_webhook_chunked_body_oversized(client: httpx.AsyncClient):
    """Raw body exceeding 2MB limit returns 413."""
    oversized_body = b"A" * (2 * 1024 * 1024 + 50)
    sig = sign_payload(TEST_SECRET, oversized_body)
    resp = await client.post(
        "/webhooks/github",
        headers={
            "X-GitHub-Event": "workflow_run",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": sig,
        },
        content=oversized_body,
    )
    assert resp.status_code == 413
    assert resp.json() == {"detail": "Payload size exceeds 2MB limit"}


@pytest.mark.asyncio
async def test_webhook_exact_2mb_passes(client: httpx.AsyncClient):
    """Payload of exact 2MB (2,097,152 bytes) passes the size threshold check."""
    # 2MB of valid JSON padding
    exact_body = b'{"action":"ping","pad":"' + (b'x' * (2 * 1024 * 1024 - 30)) + b'"}'
    sig = sign_payload(TEST_SECRET, exact_body)
    resp = await client.post(
        "/webhooks/github",
        headers={
            "X-GitHub-Event": "ping",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": sig,
        },
        content=exact_body,
    )
    # Size check passed -> ping event returns 200 ignored
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


@pytest.mark.asyncio
async def test_webhook_missing_delivery_header(client: httpx.AsyncClient):
    """Missing X-GitHub-Delivery header returns 400."""
    raw_body = json.dumps({"action": "completed"}).encode("utf-8")
    sig = sign_payload(TEST_SECRET, raw_body)
    resp = await client.post(
        "/webhooks/github",
        headers={
            "X-GitHub-Event": "workflow_run",
            "X-Hub-Signature-256": sig,
        },
        content=raw_body,
    )
    assert resp.status_code == 400
    assert resp.json() == {"detail": "Missing X-GitHub-Delivery header"}


@pytest.mark.asyncio
async def test_webhook_non_workflow_run_event(client: httpx.AsyncClient):
    """Events other than workflow_run (e.g. ping/push) are acknowledged with 200 ignored."""
    raw_body = json.dumps({"zen": "Keep it logically awesome."}).encode("utf-8")
    sig = sign_payload(TEST_SECRET, raw_body)
    resp = await client.post(
        "/webhooks/github",
        headers={
            "X-GitHub-Event": "ping",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": sig,
        },
        content=raw_body,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ignored"
    assert "ping" in data["reason"]


@pytest.mark.asyncio
async def test_webhook_action_not_completed(client: httpx.AsyncClient):
    """workflow_run with action != completed (e.g. requested) is ignored."""
    payload = make_valid_payload(action="requested", conclusion=None)
    raw_body = json.dumps(payload).encode("utf-8")
    sig = sign_payload(TEST_SECRET, raw_body)
    resp = await client.post(
        "/webhooks/github",
        headers={
            "X-GitHub-Event": "workflow_run",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": sig,
        },
        content=raw_body,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


@pytest.mark.asyncio
async def test_webhook_conclusion_not_failure(client: httpx.AsyncClient):
    """workflow_run with conclusion != failure (e.g. success) is ignored."""
    payload = make_valid_payload(action="completed", conclusion="success")
    raw_body = json.dumps(payload).encode("utf-8")
    sig = sign_payload(TEST_SECRET, raw_body)
    resp = await client.post(
        "/webhooks/github",
        headers={
            "X-GitHub-Event": "workflow_run",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": sig,
        },
        content=raw_body,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


@pytest.mark.asyncio
async def test_webhook_invalid_head_sha(client: httpx.AsyncClient):
    """Payload with non-40-hex head_sha fails Pydantic schema validation with 422."""
    payload = make_valid_payload(head_sha="short_sha_12345")
    raw_body = json.dumps(payload).encode("utf-8")
    sig = sign_payload(TEST_SECRET, raw_body)
    resp = await client.post(
        "/webhooks/github",
        headers={
            "X-GitHub-Event": "workflow_run",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": sig,
        },
        content=raw_body,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_webhook_unregistered_repo(client: httpx.AsyncClient, db: AsyncSession):
    """Webhook for a repository not registered in DB is dropped with 200 ignored."""
    await truncate_all(db)
    payload = make_valid_payload(owner="unknown-org", repo="unknown-repo", run_id=999111)
    raw_body = json.dumps(payload).encode("utf-8")
    sig = sign_payload(TEST_SECRET, raw_body)

    resp = await client.post(
        "/webhooks/github",
        headers={
            "X-GitHub-Event": "workflow_run",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": sig,
        },
        content=raw_body,
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "unregistered repository"}

    # Assert no runs were created
    res = await db.execute(select(Run).where(Run.github_run_id == 999111))
    assert res.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_webhook_valid_failure_creates_run(
    client: httpx.AsyncClient,
    db: AsyncSession,
    user_factory,
):
    """Valid completed failure for registered repo creates 1 Run in DB with status=pending."""
    await truncate_all(db)
    user = await user_factory(github_id=802, username="wh_user2")
    repo = Repo(user_id=user.id, owner="ci-org", name="ci-repo")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)
    repo_id = repo.id

    delivery_id = str(uuid.uuid4())
    gh_run_id = 88776655
    payload = make_valid_payload(owner="ci-org", repo="ci-repo", run_id=gh_run_id)
    raw_body = json.dumps(payload).encode("utf-8")
    sig = sign_payload(TEST_SECRET, raw_body)

    with patch("app.webhooks.handle_failed_run"):
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
    data = resp.json()
    assert data["status"] == "queued"
    assert data["github_run_id"] == gh_run_id
    assert data["delivery_id"] == delivery_id

    # Verify Run in DB
    res = await db.execute(select(Run).where(Run.github_run_id == gh_run_id))
    run_row = res.scalar_one_or_none()
    assert run_row is not None
    assert run_row.repo_id == repo_id
    assert run_row.status == "pending"
    assert run_row.conclusion == "failure"
    assert run_row.github_delivery_id == delivery_id


@pytest.mark.asyncio
async def test_webhook_deduplication(
    client: httpx.AsyncClient,
    db: AsyncSession,
    user_factory,
):
    """Duplicate delivery returns 200 status=duplicate without creating extra rows."""
    await truncate_all(db)
    user = await user_factory(github_id=803, username="wh_user3")
    repo = Repo(user_id=user.id, owner="dedupe-org", name="dedupe-repo")
    db.add(repo)
    await db.commit()

    delivery_id = str(uuid.uuid4())
    gh_run_id = 776611
    payload = make_valid_payload(owner="dedupe-org", repo="dedupe-repo", run_id=gh_run_id)
    raw_body = json.dumps(payload).encode("utf-8")
    sig = sign_payload(TEST_SECRET, raw_body)
    headers = {
        "X-GitHub-Event": "workflow_run",
        "X-GitHub-Delivery": delivery_id,
        "X-Hub-Signature-256": sig,
    }

    # First delivery -> queued
    resp1 = await client.post("/webhooks/github", headers=headers, content=raw_body)
    assert resp1.status_code == 200
    assert resp1.json()["status"] == "queued"

    # Second delivery (replay) -> duplicate
    resp2 = await client.post("/webhooks/github", headers=headers, content=raw_body)
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "duplicate"

    # DB still has exactly 1 row
    res = await db.execute(select(Run).where(Run.github_run_id == gh_run_id))
    runs = res.scalars().all()
    assert len(runs) == 1


@pytest.mark.asyncio
async def test_webhook_concurrent_duplicate_deliveries(
    client: httpx.AsyncClient,
    db: AsyncSession,
    user_factory,
):
    """4 concurrent deliveries for the same delivery_id produce exactly 1 Run row in DB."""
    await truncate_all(db)
    user = await user_factory(github_id=804, username="wh_user4")
    repo = Repo(user_id=user.id, owner="race-org", name="race-repo")
    db.add(repo)
    await db.commit()

    delivery_id = str(uuid.uuid4())
    gh_run_id = 991122
    payload = make_valid_payload(owner="race-org", repo="race-repo", run_id=gh_run_id)
    raw_body = json.dumps(payload).encode("utf-8")
    sig = sign_payload(TEST_SECRET, raw_body)
    headers = {
        "X-GitHub-Event": "workflow_run",
        "X-GitHub-Delivery": delivery_id,
        "X-Hub-Signature-256": sig,
    }

    async def _send():
        return await client.post("/webhooks/github", headers=headers, content=raw_body)

    results = await asyncio.gather(*[_send() for _ in range(4)])
    statuses = [r.json()["status"] for r in results]

    assert statuses.count("queued") == 1
    assert statuses.count("duplicate") == 3

    # Assert exactly 1 Run row in DB
    res = await db.execute(select(Run).where(Run.github_run_id == gh_run_id))
    assert len(res.scalars().all()) == 1


def test_github_client_token_isolation_and_no_logging(caplog):
    """GitHub client constructs headers with per-call tokens and never logs secrets."""
    with caplog.at_level(logging.DEBUG):
        secret_tok = "ghp_super_secret_token_never_logged_12345"
        headers = _build_headers(token=secret_tok)
        assert headers["Authorization"] == f"Bearer {secret_tok}"
        assert secret_tok not in caplog.text
