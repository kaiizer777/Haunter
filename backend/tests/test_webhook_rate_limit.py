"""Webhook per-repo/per-sender rate limiting (issue #161).

The webhook ingress path must throttle abusive or looping senders with a
429 path while legitimate deliveries pass through. These tests drive the
public `/webhooks/github` endpoint with `ping` events (cheap `ignored`
responses, no DB rows) so the suite stays hermetic (`fast` marker).

The limiter's store is pluggable (app.services.rate_limit); the end-to-end
429 path is exercised against the default in-memory store, and the shared
Postgres store — the one that actually bounds the budget across Lambda
execution environments — is covered by tests/test_webhook_rate_limit_postgres.py,
which needs a real database and so cannot live in this module without
deselecting the whole regression suite from a bare `pytest`.

Nothing here may touch the database or construct a cloud client: the store is
selected by settings/injection precisely so the hermetic run cannot.
"""

import hashlib
import hmac
import json
import uuid

import httpx
import pytest

from app.config import settings
from app.services import rate_limit
from app.services.rate_limit import (
    InMemoryRateLimitBackend,
    PostgresRateLimitBackend,
    bucket_lock_id,
)
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


@pytest.mark.fast
@pytest.mark.asyncio
async def test_webhook_rate_window_expiry(monkeypatch: pytest.MonkeyPatch):
    """Budget refills once the oldest entry slides out of the window."""
    from app.webhooks import _consume_webhook_rate_limit

    monkeypatch.setattr(settings, "webhook_rate_limit_max_deliveries", 2)
    monkeypatch.setattr(settings, "webhook_rate_limit_window_seconds", 10)

    assert await _consume_webhook_rate_limit("exp-org/exp-repo|s", now=1000.0) == 0
    assert await _consume_webhook_rate_limit("exp-org/exp-repo|s", now=1001.0) == 0
    retry_after = await _consume_webhook_rate_limit("exp-org/exp-repo|s", now=1002.0)
    assert retry_after == 9
    assert await _consume_webhook_rate_limit("exp-org/exp-repo|s", now=1011.0) == 0


@pytest.mark.fast
def test_webhook_rate_limit_key_building():
    """Key builder normalizes case, falls back, and never raises."""
    from app.webhooks import _webhook_rate_limit_key

    raw = _ping_body("Acme-Org", "Acme-Repo", "Some-Sender")
    assert _webhook_rate_limit_key("ping", raw) == "acme-org/acme-repo|some-sender"

    no_ids = json.dumps({"zen": "hi"}).encode("utf-8")
    assert _webhook_rate_limit_key("ping", no_ids) == "unknown-repo|unknown-sender"

    assert (
        _webhook_rate_limit_key("ping", b"not-json{{{")
        == "unknown-repo|unknown-sender"
    )


# ---------------------------------------------------------------------------
# Store selection + hermeticity. These guard the properties the shared store
# was introduced to get right: the right store is chosen, the wrong one is
# never opened by accident, and a misconfigured budget fails open instead of
# taking delivery down.
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_webhook_rate_limit_backend_defaults_to_memory():
    """No env override means the process-local store.

    The default has to stay `memory` because this suite (and a developer with
    no database) must run with zero external I/O.
    """
    assert settings.webhook_rate_limit_backend == "memory"
    assert isinstance(
        rate_limit.get_rate_limit_backend("memory"), InMemoryRateLimitBackend
    )


@pytest.mark.fast
def test_webhook_rate_limit_backend_selection_follows_setting(
    monkeypatch: pytest.MonkeyPatch,
):
    """The setting picks the store, and an unknown value degrades to memory."""
    assert isinstance(
        rate_limit.build_rate_limit_backend("postgres"), PostgresRateLimitBackend
    )
    assert isinstance(
        rate_limit.build_rate_limit_backend("  POSTGRES "), PostgresRateLimitBackend
    )

    monkeypatch.setattr(settings, "webhook_rate_limit_backend", "postgres")
    assert isinstance(
        rate_limit.get_rate_limit_backend(settings.webhook_rate_limit_backend),
        PostgresRateLimitBackend,
    )

    # A typo must not silently switch the control off, and must not 5xx either.
    monkeypatch.setattr(settings, "webhook_rate_limit_backend", "rediss")
    assert isinstance(
        rate_limit.build_rate_limit_backend(settings.webhook_rate_limit_backend),
        InMemoryRateLimitBackend,
    )


@pytest.mark.fast
@pytest.mark.asyncio
async def test_webhook_rate_limit_fails_open_when_budget_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
):
    """maximum<=0 and window<=0 disable throttling, on ANY store.

    Also proves the disabled check happens before the store is reached: with
    the Postgres backend selected, a zero budget must not open a session. The
    sentinel session maker raises if it is ever called.
    """
    from app.webhooks import _consume_webhook_rate_limit

    async def _explode():  # pragma: no cover - must never be called
        raise AssertionError("the limiter opened a database session")

    monkeypatch.setattr(rate_limit, "get_rate_limit_backend", lambda name: None)
    monkeypatch.setattr("app.db.async_session_maker", _explode)

    monkeypatch.setattr(settings, "webhook_rate_limit_backend", "postgres")
    for maximum, window in ((0, 60), (-1, 60), (120, 0), (120, -5)):
        monkeypatch.setattr(settings, "webhook_rate_limit_max_deliveries", maximum)
        monkeypatch.setattr(settings, "webhook_rate_limit_window_seconds", window)
        assert await _consume_webhook_rate_limit("off-org/off-repo|s", now=1.0) == 0
        assert await _consume_webhook_rate_limit("off-org/off-repo|s", now=2.0) == 0


@pytest.mark.fast
@pytest.mark.asyncio
async def test_webhook_rate_limit_never_opens_a_session_in_memory_mode(
    monkeypatch: pytest.MonkeyPatch,
):
    """The in-memory store must be usable with no database at all.

    This is the hermeticity guarantee the whole store selection exists for:
    the default configuration can run the limiter end to end without a
    connection.
    """
    from app.webhooks import _consume_webhook_rate_limit

    async def _explode():  # pragma: no cover - must never be called
        raise AssertionError("the limiter opened a database session")

    monkeypatch.setattr(settings, "webhook_rate_limit_backend", "memory")
    monkeypatch.setattr(settings, "webhook_rate_limit_max_deliveries", 2)
    monkeypatch.setattr(settings, "webhook_rate_limit_window_seconds", 10)
    monkeypatch.setattr("app.db.async_session_maker", _explode)

    assert await _consume_webhook_rate_limit("herm-org/herm-repo|s", now=1000.0) == 0
    assert await _consume_webhook_rate_limit("herm-org/herm-repo|s", now=1001.0) == 0
    assert await _consume_webhook_rate_limit("herm-org/herm-repo|s", now=1002.0) == 9


@pytest.mark.fast
@pytest.mark.asyncio
async def test_webhook_rate_limit_degrades_when_the_store_raises(
    monkeypatch: pytest.MonkeyPatch,
):
    """A broken store costs the shared budget, never the delivery.

    The fallback has to be a real, reused in-process limiter: handing out a
    fresh bucket on each failure would grant an unlimited budget, which is the
    opposite of degraded mode.
    """
    from app.webhooks import _consume_webhook_rate_limit

    class _BrokenStore:
        async def consume(self, key, *, maximum, window, now=None):
            raise RuntimeError("store is down")

    monkeypatch.setattr(settings, "webhook_rate_limit_backend", "postgres")
    monkeypatch.setattr(settings, "webhook_rate_limit_max_deliveries", 2)
    monkeypatch.setattr(settings, "webhook_rate_limit_window_seconds", 10)
    monkeypatch.setattr(
        rate_limit, "get_rate_limit_backend", lambda name: _BrokenStore()
    )

    # First call primes the shared degraded limiter, second and third see it.
    assert await _consume_webhook_rate_limit("deg-org/deg-repo|s", now=1000.0) == 0
    assert await _consume_webhook_rate_limit("deg-org/deg-repo|s", now=1001.0) == 0
    assert await _consume_webhook_rate_limit("deg-org/deg-repo|s", now=1002.0) == 9


@pytest.mark.fast
@pytest.mark.asyncio
async def test_memory_backend_budgets_are_per_instance():
    """Why the shared store exists: two in-process limiters are two budgets.

    Each standalone ``InMemoryRateLimitBackend`` hands out its own budget, which
    is exactly the defect under Lambda — ten execution environments, ten
    budgets, ten times the configured ceiling. The Postgres backend is covered
    by tests/test_webhook_rate_limit_postgres.py, which asserts the opposite
    for two instances against one table.
    """
    env_a = InMemoryRateLimitBackend()
    env_b = InMemoryRateLimitBackend()
    key = "split/s|s"

    # Environment A spends its whole budget for this bucket...
    assert await env_a.consume(key, maximum=2, window=10, now=1000.0) == 0
    assert await env_a.consume(key, maximum=2, window=10, now=1001.0) == 0
    assert await env_a.consume(key, maximum=2, window=10, now=1002.0) == 9

    # ...and environment B, which has seen none of them, still hands out two
    # more. Two instances therefore accept four deliveries against a
    # maximum=2 budget — the exact multiplier that made the control ~10x weaker
    # than configured under reserved_concurrent_executions=10.
    assert await env_b.consume(key, maximum=2, window=10, now=1002.0) == 0
    assert await env_b.consume(key, maximum=2, window=10, now=1003.0) == 0
    assert await env_b.consume(key, maximum=2, window=10, now=1004.0) == 9


@pytest.mark.fast
@pytest.mark.asyncio
async def test_in_memory_backend_evicts_oldest_bucket_at_the_cap(
    monkeypatch: pytest.MonkeyPatch,
):
    """The memory bound still holds after the extraction.

    A webhook-secret holder can mint unlimited distinct keys, so the bucket
    table must stay capped. The cap is shrunk to 3 here rather than filled 10k
    times — the eviction path is what is under test, not the size of the cap.
    """
    backend = InMemoryRateLimitBackend()
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_MAX_BUCKETS", 3)

    for i in range(3):
        assert await backend.consume(f"evict/{i}", maximum=1, window=60) == 0
    assert len(backend._buckets) == 3

    # A fourth key evicts the oldest-inserted one, not an arbitrary one.
    assert await backend.consume("evict/3", maximum=1, window=60) == 0
    assert len(backend._buckets) == 3
    assert "evict/0" not in backend._buckets
    assert set(backend._buckets) == {"evict/1", "evict/2", "evict/3"}

    # The evicted bucket's budget really was dropped, not just the key.
    assert await backend.consume("evict/0", maximum=1, window=60) == 0


@pytest.mark.fast
def test_bucket_lock_ids_are_stable_and_key_scoped():
    """The per-bucket serialisation contract, without a database.

    Two environments must derive the SAME lock for the same key (that is what
    makes the budget shared) and DIFFERENT locks for different keys (that is
    what stops unrelated senders blocking each other). A hash collision would
    only over-serialise, never merge budgets, so distinctness here is an
    efficiency property rather than a correctness one.
    """
    first = bucket_lock_id("acme/acme-repo|some-sender")
    assert first == bucket_lock_id("acme/acme-repo|some-sender")
    assert first != bucket_lock_id("acme/acme-repo|other-sender")
    assert first != bucket_lock_id("acme/other-repo|some-sender")
    # Signed 64-bit, which is the range pg_advisory_xact_lock(bigint) takes.
    assert -(2**63) <= first < 2**63
    assert bucket_lock_id("acme/acme-repo|some-sender") != bucket_lock_id(
        "acme/acme-repo|some-sender "
    )
