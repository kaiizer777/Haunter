"""Webhook per-repo/per-sender rate limiting (issue #161).

The webhook ingress path must throttle abusive or looping senders with a
429 path while legitimate deliveries pass through. These tests drive the
public `/webhooks/github` endpoint with `ping` events (cheap `ignored`
responses, no DB rows) so the suite stays hermetic (`fast` marker).
"""

import hashlib
import hmac
import json
import uuid

import httpx
import pytest

from app.config import settings
from app.webhooks import _reset_webhook_rate_limiter

TEST_SECRET = settings.github_webhook_secret or "test_webhook_secret_key_12345"


def _sign(raw_body: bytes) -> str:
    digest = hmac.new(TEST_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _ping_body(owner: str, repo: str, sender: str) -> bytes:
    return json.dumps(
        {
            "zen": "Keep it logically awesome.",
            "repository": {
                "name": repo,
                "full_name": f"{owner}/{repo}",
                "owner": {"login": owner},
            },
            "sender": {"login": sender, "type": "User"},
        }
    ).encode("utf-8")


async def _post_ping(
    client: httpx.AsyncClient, owner: str, repo: str, sender: str
) -> httpx.Response:
    raw_body = _ping_body(owner, repo, sender)
    return await client.post(
        "/webhooks/github",
        headers={
            "X-GitHub-Event": "ping",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": _sign(raw_body),
            "Content-Type": "application/json",
        },
        content=raw_body,
    )


@pytest.fixture(autouse=True)
def _clean_rate_limiter():
    _reset_webhook_rate_limiter()
    yield
    _reset_webhook_rate_limiter()


@pytest.mark.fast
@pytest.mark.asyncio
async def test_webhook_legitimate_deliveries_pass_through(client: httpx.AsyncClient):
    """A handful of deliveries under budget all succeed with 200."""
    for _ in range(5):
        resp = await _post_ping(client, "calm-org", "calm-repo", "normal-sender")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"


@pytest.mark.fast
@pytest.mark.asyncio
async def test_webhook_burst_from_one_sender_is_throttled(client: httpx.AsyncClient):
    """A tight burst from a single sender/repo must eventually hit 429."""
    statuses = []
    for _ in range(300):
        resp = await _post_ping(client, "burst-org", "burst-repo", "looping-sender")
        statuses.append(resp.status_code)
    assert 429 in statuses


@pytest.mark.fast
@pytest.mark.asyncio
async def test_webhook_throttle_is_per_sender(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """Throttling one sender leaves a different sender on the same repo untouched."""
    monkeypatch.setattr(settings, "webhook_rate_limit_max_deliveries", 3)
    monkeypatch.setattr(settings, "webhook_rate_limit_window_seconds", 60)

    for _ in range(3):
        resp = await _post_ping(client, "shared-org", "shared-repo", "abusive-sender")
        assert resp.status_code == 200
    throttled = await _post_ping(client, "shared-org", "shared-repo", "abusive-sender")
    assert throttled.status_code == 429
    assert throttled.headers.get("Retry-After") is not None
    assert "rate limit" in throttled.json()["detail"].lower()

    for _ in range(3):
        resp = await _post_ping(client, "shared-org", "shared-repo", "quiet-sender")
        assert resp.status_code == 200


@pytest.mark.fast
@pytest.mark.asyncio
async def test_webhook_throttle_is_per_repo(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """Throttling one repo leaves the same sender's other repo untouched."""
    monkeypatch.setattr(settings, "webhook_rate_limit_max_deliveries", 2)
    monkeypatch.setattr(settings, "webhook_rate_limit_window_seconds", 60)

    for _ in range(2):
        resp = await _post_ping(client, "noisy-org", "noisy-repo", "same-sender")
        assert resp.status_code == 200
    throttled = await _post_ping(client, "noisy-org", "noisy-repo", "same-sender")
    assert throttled.status_code == 429

    for _ in range(2):
        resp = await _post_ping(client, "other-org", "other-repo", "same-sender")
        assert resp.status_code == 200
