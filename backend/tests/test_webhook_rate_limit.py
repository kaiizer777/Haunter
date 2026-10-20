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

import asyncio
import hashlib
import hmac
import json
import time
import uuid

import httpx
import pytest
from pydantic import ValidationError

import app.config
import app.db
from app.config import settings
from app.models import MAX_BUCKET_KEY_LENGTH, WebhookRateLimitHit
from app.services import rate_limit
from app.services.rate_limit import (
    BUCKET_KEY_MAX_LENGTH,
    InMemoryRateLimitBackend,
    PostgresRateLimitBackend,
    bound_bucket_key,
    bucket_lock_id,
    normalize_bucket_key,
)
from app.webhooks import _WEBHOOK_RATE_KEY_MAX_CHARS, _reset_webhook_rate_limiter

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


@pytest.fixture(autouse=True)
def _forbid_database_io(monkeypatch: pytest.MonkeyPatch):
    """Enforce this module's hermeticity claim: no test here may run SQL.

    Session *objects* are still created — the `client` fixture drives the real
    app, whose `get_db` dependency opens a lazy session per request — but any
    attempt to execute a statement explodes. Tests that need the failure path
    install their own `app.db.async_session_maker` double on top of this guard.
    """
    real_maker = app.db.async_session_maker

    def _guarded_maker(*args, **kwargs):
        session = real_maker(*args, **kwargs)

        async def _explode_on_execute(*args, **kwargs):  # pragma: no cover
            raise AssertionError(
                "hermetic test performed database I/O via session.execute"
            )

        session.execute = _explode_on_execute  # type: ignore[method-assign]
        return session

    monkeypatch.setattr(app.db, "async_session_maker", _guarded_maker)


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
def test_webhook_rate_limit_backend_defaults_to_memory(
    monkeypatch: pytest.MonkeyPatch,
):
    """No env override means the process-local store.

    The default has to stay `memory` because this suite (and a developer with
    no database) must run with zero external I/O. Reads a fresh Settings with
    the env var removed rather than asserting ambient runner state, which a
    stray export in the runner's environment would silently flip.
    """
    monkeypatch.delenv("WEBHOOK_RATE_LIMIT_BACKEND", raising=False)
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    assert app.config.Settings().webhook_rate_limit_backend == "memory"
    assert rate_limit.DEFAULT_RATE_LIMIT_BACKEND == "memory"
    assert isinstance(
        rate_limit.get_rate_limit_backend("memory"), InMemoryRateLimitBackend
    )


@pytest.mark.fast
def test_backend_selector_normalizes_and_rejects_at_load(
    monkeypatch: pytest.MonkeyPatch,
):
    """Typos fail at startup; case/whitespace variants are accepted."""
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)

    monkeypatch.setenv("WEBHOOK_RATE_LIMIT_BACKEND", "  POSTGRES ")
    assert app.config.Settings().webhook_rate_limit_backend == "postgres"

    monkeypatch.setenv("WEBHOOK_RATE_LIMIT_BACKEND", "rediss")
    with pytest.raises(ValidationError):
        app.config.Settings()


@pytest.mark.fast
def test_lambda_refuses_to_boot_with_the_per_process_limiter(
    monkeypatch: pytest.MonkeyPatch,
):
    """Lost Terraform wiring on the orchestrator fails the deploy, not silently 10x the budget."""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "haunter-orchestrator")

    monkeypatch.delenv("WEBHOOK_RATE_LIMIT_BACKEND", raising=False)
    with pytest.raises(ValidationError, match="must be 'postgres' on Lambda"):
        app.config.Settings()

    monkeypatch.setenv("WEBHOOK_RATE_LIMIT_BACKEND", "postgres")
    assert app.config.Settings().webhook_rate_limit_backend == "postgres"


@pytest.mark.fast
@pytest.mark.parametrize(
    "worker_function_name",
    [
        "haunter-audit-dispatcher",
        "haunter-pipeline-outbox-sweeper",
        "haunter-outbox-sweeper",
        "HAUNTER-AUDIT-DISPATCHER",
        "HAUNTER-PIPELINE-OUTBOX-SWEEPER",
    ],
)
def test_lambda_sibling_workers_boot_with_memory_backend(
    monkeypatch: pytest.MonkeyPatch, worker_function_name: str
):
    """Sibling background workers do not serve webhooks and boot cleanly with default memory backend."""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", worker_function_name)
    monkeypatch.delenv("WEBHOOK_RATE_LIMIT_BACKEND", raising=False)
    monkeypatch.delenv("WEBHOOK_RATE_LIMIT_STRICT_LAMBDA", raising=False)

    s = app.config.Settings()
    assert s.webhook_rate_limit_backend == "memory"


@pytest.mark.fast
def test_lambda_strict_flag_enforces_postgres_even_on_workers(
    monkeypatch: pytest.MonkeyPatch,
):
    """When WEBHOOK_RATE_LIMIT_STRICT_LAMBDA is set, even worker functions require postgres."""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "haunter-audit-dispatcher")
    monkeypatch.delenv("WEBHOOK_RATE_LIMIT_BACKEND", raising=False)
    monkeypatch.setenv("WEBHOOK_RATE_LIMIT_STRICT_LAMBDA", "1")

    with pytest.raises(ValidationError, match="must be 'postgres' on Lambda"):
        app.config.Settings()

    monkeypatch.setenv("WEBHOOK_RATE_LIMIT_BACKEND", "postgres")
    assert app.config.Settings().webhook_rate_limit_backend == "postgres"


@pytest.mark.fast
def test_webhook_rate_limit_backend_selection_follows_setting(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """The setting picks the store. Boot-time config fails closed (raising
    ValidationError on typos), while the runtime factory provides defense-in-depth."""
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

    # Note: Invalid backend names in environment configuration fail closed at boot time
    # with a pydantic ValidationError (see test_backend_selector_normalizes_and_rejects_at_load),
    # preventing accidental misconfigurations from booting or silently falling back.
    # If an unknown backend value is bypassed or injected directly into the runtime builder
    # (as simulated here via monkeypatching), the runtime factory acts as a defense-in-depth
    # fallback: it logs loudly (ERROR, alertable) and falls back to in-memory rather than
    # raising a 5xx on requests or silently switching the rate limit control off.

    monkeypatch.setattr(settings, "webhook_rate_limit_backend", "rediss")
    with caplog.at_level("ERROR", logger="app.services.rate_limit"):
        assert isinstance(
            rate_limit.build_rate_limit_backend(settings.webhook_rate_limit_backend),
            InMemoryRateLimitBackend,
        )
    assert "Unknown webhook rate limit backend" in caplog.text


@pytest.mark.fast
@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_backend_name_degrades_to_memory(blank: str):
    """Runtime factory defense-in-depth: blank backend strings fall back to memory if bypassed past boot validation."""
    assert isinstance(
        rate_limit.build_rate_limit_backend(blank), InMemoryRateLimitBackend
    )


@pytest.mark.fast
@pytest.mark.asyncio
async def test_webhook_rate_limit_fails_open_when_budget_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
):
    """maximum<=0 and window<=0 disable throttling, on ANY store.

    Also proves the disabled check happens before the store is reached: with
    the Postgres backend selected AND resolved (no stub standing in for it),
    a zero budget must not open a session. The sentinel session maker raises
    if it is ever called.
    """
    from app.webhooks import _consume_webhook_rate_limit

    async def _explode():  # pragma: no cover - must never be called
        raise AssertionError("the limiter opened a database session")

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
        """A store with a wedged connection: an infra error, not a store bug."""

        async def consume(self, key, *, maximum, window, now=None):
            raise OSError("store is down")

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
@pytest.mark.parametrize(
    "error_factory",
    [
        lambda: pytest.importorskip("sqlalchemy.exc").OperationalError(
            "connection closed", params=None, orig=Exception("connection closed")
        ),
        lambda: pytest.importorskip("sqlalchemy.exc").SQLAlchemyError("query failed"),
        lambda: pytest.importorskip("sqlalchemy.exc").DBAPIError(
            "dbapi failure", params=None, orig=Exception("dbapi error")
        ),
        lambda: pytest.importorskip("asyncpg").PostgresError("connection terminated"),
    ],
    ids=["operational_error", "sqlalchemy_error", "dbapi_error", "asyncpg_postgres_error"],
)
async def test_webhook_rate_limit_degrades_on_database_exceptions(
    monkeypatch: pytest.MonkeyPatch, error_factory
):
    """Real database and driver connection drops degrade gracefully to in-memory store."""
    from app.webhooks import _consume_webhook_rate_limit

    exc_to_raise = error_factory()

    class _DatabaseFailingStore:
        async def consume(self, key, *, maximum, window, now=None):
            raise exc_to_raise

    monkeypatch.setattr(settings, "webhook_rate_limit_backend", "postgres")
    monkeypatch.setattr(settings, "webhook_rate_limit_max_deliveries", 2)
    monkeypatch.setattr(settings, "webhook_rate_limit_window_seconds", 10)
    monkeypatch.setattr(
        rate_limit, "get_rate_limit_backend", lambda name: _DatabaseFailingStore()
    )

    assert rate_limit._consecutive_store_failures == 0
    # First delivery: allowed, breaker failure counter increments to 1
    assert await _consume_webhook_rate_limit("db-deg/repo|s", now=1000.0) == 0
    assert rate_limit._consecutive_store_failures == 1

    # Second delivery: allowed, breaker failure counter increments to 2
    assert await _consume_webhook_rate_limit("db-deg/repo|s", now=1001.0) == 0
    assert rate_limit._consecutive_store_failures == 2

    # Third delivery: in-memory fallback store budget exhausted, returns Retry-After 9
    assert await _consume_webhook_rate_limit("db-deg/repo|s", now=1002.0) == 9
    assert rate_limit._consecutive_store_failures == 3


@pytest.mark.fast
@pytest.mark.asyncio
async def test_breaker_skips_the_store_after_consecutive_sqlalchemy_errors(
    monkeypatch: pytest.MonkeyPatch,
):
    """Consecutive SQLAlchemyErrors trip the circuit breaker open."""
    from sqlalchemy.exc import SQLAlchemyError
    from app.webhooks import _consume_webhook_rate_limit

    calls = {"n": 0}

    class _SqlWedgedStore:
        async def consume(self, key, *, maximum, window, now=None):
            calls["n"] += 1
            raise SQLAlchemyError("connection pool exhausted")

    monkeypatch.setattr(settings, "webhook_rate_limit_backend", "postgres")
    monkeypatch.setattr(settings, "webhook_rate_limit_max_deliveries", 100)
    monkeypatch.setattr(settings, "webhook_rate_limit_window_seconds", 60)
    monkeypatch.setattr(
        rate_limit, "get_rate_limit_backend", lambda name: _SqlWedgedStore()
    )
    monkeypatch.setattr(rate_limit, "_BREAKER_THRESHOLD", 3)
    monkeypatch.setattr(rate_limit, "_BREAKER_COOLDOWN_SECONDS", 3600.0)

    for i in range(5):
        assert await _consume_webhook_rate_limit("sqlbrk/sqlbrk|s", now=1000.0 + i) == 0
    assert calls["n"] == 3, f"breaker did not open: store hit {calls['n']}x"
    assert rate_limit._breaker_open_until is not None


@pytest.mark.fast
@pytest.mark.asyncio
async def test_programming_error_in_the_store_propagates(
    monkeypatch: pytest.MonkeyPatch,
):
    """A bug in the backend must fail loudly, never degrade into weak throttling.

    Degrading on programming errors would turn every store bug into a silently
    weaker control — the exact failure mode the ERROR log exists to surface.
    """
    from app.webhooks import _consume_webhook_rate_limit

    class _BuggyStore:
        async def consume(self, key, *, maximum, window, now=None):
            raise RuntimeError("bug in backend arithmetic")

    monkeypatch.setattr(settings, "webhook_rate_limit_backend", "postgres")
    monkeypatch.setattr(settings, "webhook_rate_limit_max_deliveries", 2)
    monkeypatch.setattr(settings, "webhook_rate_limit_window_seconds", 10)
    monkeypatch.setattr(
        rate_limit, "get_rate_limit_backend", lambda name: _BuggyStore()
    )

    with pytest.raises(RuntimeError, match="bug in backend arithmetic"):
        await _consume_webhook_rate_limit("bug-org/bug-repo|s", now=1000.0)


@pytest.mark.fast
@pytest.mark.asyncio
async def test_breaker_skips_the_store_after_consecutive_failures(
    monkeypatch: pytest.MonkeyPatch,
):
    """Past the threshold, deliveries skip the wedged store until cooldown."""
    from app.webhooks import _consume_webhook_rate_limit

    calls = {"n": 0}

    class _WedgedStore:
        async def consume(self, key, *, maximum, window, now=None):
            calls["n"] += 1
            raise OSError("connection refused")

    monkeypatch.setattr(settings, "webhook_rate_limit_backend", "postgres")
    monkeypatch.setattr(settings, "webhook_rate_limit_max_deliveries", 100)
    monkeypatch.setattr(settings, "webhook_rate_limit_window_seconds", 60)
    monkeypatch.setattr(
        rate_limit, "get_rate_limit_backend", lambda name: _WedgedStore()
    )
    monkeypatch.setattr(rate_limit, "_BREAKER_THRESHOLD", 3)
    monkeypatch.setattr(rate_limit, "_BREAKER_COOLDOWN_SECONDS", 3600.0)

    for i in range(5):
        assert await _consume_webhook_rate_limit("brk/brk|s", now=1000.0 + i) == 0
    assert calls["n"] == 3, f"breaker did not open: store hit {calls['n']}x"


@pytest.mark.fast
@pytest.mark.asyncio
async def test_breaker_cooldown_expiration_and_success_reset(
    monkeypatch: pytest.MonkeyPatch,
):
    """After cooldown expires, a successful store operation cleanly resets the breaker."""
    from app.webhooks import _consume_webhook_rate_limit

    simulated_mono = 1000.0
    monkeypatch.setattr(time, "monotonic", lambda: simulated_mono)

    calls = {"n": 0}
    failing = True

    class _RecoverableStore:
        async def consume(self, key, *, maximum, window, now=None):
            calls["n"] += 1
            if failing:
                raise OSError("connection refused")
            return 0

    monkeypatch.setattr(settings, "webhook_rate_limit_backend", "postgres")
    monkeypatch.setattr(settings, "webhook_rate_limit_max_deliveries", 100)
    monkeypatch.setattr(settings, "webhook_rate_limit_window_seconds", 60)
    monkeypatch.setattr(
        rate_limit, "get_rate_limit_backend", lambda name: _RecoverableStore()
    )

    threshold = rate_limit._CONSECUTIVE_STORE_FAILURES_THRESHOLD
    cooldown = rate_limit._BREAKER_COOLDOWN_SECONDS

    # a. Trip circuit breaker open by driving consecutive store failures to the threshold
    for i in range(threshold):
        assert await _consume_webhook_rate_limit("recover/test|s", now=1000.0 + i) == 0

    assert calls["n"] == threshold
    assert rate_limit._consecutive_store_failures == threshold
    assert rate_limit._breaker_open_until is not None
    assert rate_limit._degraded is not None

    # b. Verify breaker is open and subsequent calls route to degraded mode without querying primary store
    calls_before = calls["n"]
    for i in range(3):
        assert await _consume_webhook_rate_limit("recover/test|s", now=1010.0 + i) == 0
    assert calls["n"] == calls_before

    # c. Advance simulated time past cooldown period
    simulated_mono += cooldown + 1.0

    # d. Execute a successful consume call against primary store
    failing = False
    assert await _consume_webhook_rate_limit("recover/test|s", now=2000.0) == 0
    assert calls["n"] == calls_before + 1

    # e. Assert breaker state cleanly resets:
    assert rate_limit._consecutive_store_failures == 0
    assert rate_limit._breaker_open_until is None
    assert rate_limit._degraded is None

    # f. Verify subsequent calls continue to use primary store normally
    for i in range(3):
        assert await _consume_webhook_rate_limit("recover/test|s", now=2001.0 + i) == 0
    assert calls["n"] == calls_before + 1 + 3


@pytest.mark.fast
@pytest.mark.asyncio
async def test_wedged_store_call_is_abandoned_after_the_timeout(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """A store that never answers costs the timeout, not the delivery.

    Not just 'it returned': the degraded store must actually be spending the
    budget. A blanket `except Exception: return 0` satisfies a single-call
    assertion and would leave the limiter completely off during an outage.
    Degraded mode must actively enforce the in-memory rate limit and not fail open.
    """
    from app.webhooks import _consume_webhook_rate_limit

    class _HangingStore:
        async def consume(self, key, *, maximum, window, now=None):
            await asyncio.sleep(30.0)
            return 0  # pragma: no cover - abandoned at the timeout

    monkeypatch.setattr(settings, "webhook_rate_limit_backend", "postgres")
    monkeypatch.setattr(settings, "webhook_rate_limit_max_deliveries", 2)
    monkeypatch.setattr(settings, "webhook_rate_limit_window_seconds", 10)
    monkeypatch.setattr(
        rate_limit, "get_rate_limit_backend", lambda name: _HangingStore()
    )
    monkeypatch.setattr(rate_limit, "STORE_ROUND_TRIP_TIMEOUT_SECONDS", 0.01)

    assert rate_limit._consecutive_store_failures == 0
    assert rate_limit._degraded is None

    with caplog.at_level("WARNING", logger="app.services.rate_limit"):
        # First delivery: store hangs -> timeout triggers degraded mode.
        # InMemoryRateLimitBackend admits delivery (allowed=True, returns 0).
        # Remaining budget in degraded bucket decreases from 2 to 1.
        retry_1 = await _consume_webhook_rate_limit("hang/hang|s", now=1000.0)
        assert retry_1 == 0, "first request should be allowed (returns retry_after=0)"
        assert rate_limit._consecutive_store_failures == 1
        assert rate_limit._degraded is not None
        assert isinstance(rate_limit._degraded, rate_limit.InMemoryRateLimitBackend)
        assert len(rate_limit._degraded._buckets["hang/hang|s"]) == 1

        # Second delivery: store hangs -> timeout. Degraded limiter admits delivery (allowed=True).
        # Remaining budget in degraded bucket reaches 0 (maximum 2 exhausted).
        retry_2 = await _consume_webhook_rate_limit("hang/hang|s", now=1001.0)
        assert retry_2 == 0, "second request should be allowed (returns retry_after=0)"
        assert rate_limit._consecutive_store_failures == 2
        assert len(rate_limit._degraded._buckets["hang/hang|s"]) == 2

        # Third delivery (limit + 1 within window): store hangs -> timeout.
        # Degraded limiter must actively enforce the in-memory rate limit and NOT fail open.
        # Bucket budget is exhausted -> allowed is False (throttled with retry_after=9).
        retry_3 = await _consume_webhook_rate_limit("hang/hang|s", now=1002.0)
        assert retry_3 == 9, "third request (limit + 1) must be throttled with retry_after > 0"
        assert rate_limit._consecutive_store_failures == 3
        # Bucket count remains 2 (rejected delivery is not appended)
        assert len(rate_limit._degraded._buckets["hang/hang|s"]) == 2

    # Verify log indicates degraded mode / in-process limiter was used on timeout
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert "throttling with the in-process limiter" in errors[0].getMessage()
    assert "TimeoutError" in errors[0].getMessage()

    # Finding 9 (review comment 4305214461): verify degraded mode logs relaxed ceiling warning
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "operating in degraded in-memory mode with per-instance budget (relaxed global ceiling)" in warnings[0].getMessage()


# Review comment 4305214447 alias: verify degraded mode enforces memory budget and does not fail open
test_store_timeout_triggers_degraded_mode = test_wedged_store_call_is_abandoned_after_the_timeout


@pytest.mark.fast
@pytest.mark.asyncio
async def test_degraded_mode_logs_warning_about_relaxed_ceiling(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """When store degradation is triggered, log explicit warning on relaxed ceiling.

    Degraded mode falls back to InMemoryRateLimitBackend, relaxing the global
    aggregate ceiling to an isolated per-environment budget. Operators must
    receive an explicit structured warning in logs.
    """
    from app.webhooks import _consume_webhook_rate_limit

    class _FailingStore:
        async def consume(self, key, *, maximum, window, now=None):
            raise OSError("Postgres connection dropped")

    monkeypatch.setattr(settings, "webhook_rate_limit_backend", "postgres")
    monkeypatch.setattr(settings, "webhook_rate_limit_max_deliveries", 2)
    monkeypatch.setattr(settings, "webhook_rate_limit_window_seconds", 10)
    monkeypatch.setattr(
        rate_limit, "get_rate_limit_backend", lambda name: _FailingStore()
    )

    with caplog.at_level("WARNING", logger="app.services.rate_limit"):
        retry = await _consume_webhook_rate_limit("degraded/relaxed|s", now=1000.0)
        assert retry == 0

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "Postgres rate limit store unavailable" in msg
    assert "degraded in-memory mode" in msg
    assert "per-instance budget" in msg
    assert "relaxed global ceiling" in msg


@pytest.mark.fast
@pytest.mark.asyncio
async def test_store_outage_logs_one_error_per_interval(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """An outage pages once, not once per delivery."""
    from app.webhooks import _consume_webhook_rate_limit

    class _DownStore:
        async def consume(self, key, *, maximum, window, now=None):
            raise OSError("connection refused")

    monkeypatch.setattr(settings, "webhook_rate_limit_backend", "postgres")
    monkeypatch.setattr(settings, "webhook_rate_limit_max_deliveries", 100)
    monkeypatch.setattr(settings, "webhook_rate_limit_window_seconds", 60)
    monkeypatch.setattr(
        rate_limit, "get_rate_limit_backend", lambda name: _DownStore()
    )
    monkeypatch.setattr(rate_limit, "_BREAKER_THRESHOLD", 1000)

    with caplog.at_level("ERROR", logger="app.services.rate_limit"):
        for i in range(4):
            assert await _consume_webhook_rate_limit("log/log|s", now=1000.0 + i) == 0

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1, f"expected one ERROR, got {len(errors)}"
    assert "in-process limiter" in errors[0].getMessage()


@pytest.mark.fast
@pytest.mark.asyncio
async def test_request_path_consults_the_configured_store(
    monkeypatch: pytest.MonkeyPatch,
):
    """The settings value must reach the store, not just select a class.

    Build-only selection tests pass even if the request path ignores the
    shared store entirely; this spy fails if _consume_webhook_rate_limit never
    calls consume_rate_limit with the configured name.
    """
    import app.webhooks
    from app.webhooks import _consume_webhook_rate_limit

    seen: dict[str, object] = {}

    async def _spy(name, key, *, maximum, window, now=None):
        seen["name"] = name
        seen["maximum"] = maximum
        return 0

    monkeypatch.setattr(app.webhooks, "consume_rate_limit", _spy)
    monkeypatch.setattr(settings, "webhook_rate_limit_backend", "postgres")
    monkeypatch.setattr(settings, "webhook_rate_limit_max_deliveries", 5)
    monkeypatch.setattr(settings, "webhook_rate_limit_window_seconds", 60)
    assert await _consume_webhook_rate_limit("spy/spy|s", now=1.0) == 0
    assert seen == {"name": "postgres", "maximum": 5}


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
    # Pinned by value: cross-process determinism means the SAME input maps to
    # the SAME lock in every process, which in-process equality alone cannot show.
    assert first == 484089977861849321
    assert first != bucket_lock_id("acme/acme-repo|other-sender")
    assert first != bucket_lock_id("acme/other-repo|some-sender")
    # Signed 64-bit, which is the range pg_advisory_xact_lock(bigint) takes.
    assert -(2**63) <= first < 2**63
    assert bucket_lock_id("acme/acme-repo|some-sender") != bucket_lock_id(
        "acme/acme-repo|some-sender "
    )


@pytest.mark.fast
def test_bucket_lock_id_lone_surrogates():
    """Lone surrogates must not raise UnicodeEncodeError and must hash deterministically.

    Malicious or malformed payloads can supply lone surrogate code points
    (\\ud800 - \\udfff) in repo names or user logins. Hashing with errors="replace"
    ensures bucket_lock_id never throws UnicodeEncodeError and always produces
    a valid, deterministic signed 64-bit integer.
    """
    surrogate_keys = [
        "\ud800",
        "\udfff",
        "repo/\ud83d\ude00/user\ud800",
        "repo/\ud800|user\udfff",
        "\ud83d\ude00\ud800\udfff",
    ]
    for key in surrogate_keys:
        lock_id_1 = bucket_lock_id(key)
        lock_id_2 = bucket_lock_id(key)

        # Determinism: calling twice with the same surrogate string returns the exact same int
        assert lock_id_1 == lock_id_2

        # Valid signed 64-bit integer range (pg_advisory_xact_lock range)
        assert -(2**63) <= lock_id_1 < 2**63
        assert isinstance(lock_id_1, int)


@pytest.mark.fast
@pytest.mark.asyncio
async def test_consume_rate_limit_with_surrogate_key():
    """Consuming with a key containing lone surrogates succeeds without crashing."""
    backend = InMemoryRateLimitBackend()
    surrogate_key = "repo/\ud800|user\udfff"

    assert await backend.consume(surrogate_key, maximum=2, window=10, now=1000.0) == 0
    assert await backend.consume(surrogate_key, maximum=2, window=10, now=1001.0) == 0
    assert await backend.consume(surrogate_key, maximum=2, window=10, now=1002.0) == 9

    # Via consume_rate_limit entry point
    assert (
        await rate_limit.consume_rate_limit(
            "memory", surrogate_key, maximum=2, window=10, now=1000.0
        )
        == 0
    )


@pytest.mark.fast
@pytest.mark.asyncio
async def test_webhook_rate_limit_with_surrogate_payload_end_to_end(
    client: httpx.AsyncClient,
):
    """End-to-end webhook request with surrogate characters in repo/sender."""
    raw_body = (
        b'{"zen": "surrogate test", '
        b'"repository": {"name": "repo\\ud800", "full_name": "owner/repo\\ud800", "owner": {"login": "owner"}}, '
        b'"sender": {"login": "user\\udfff", "type": "User"}}'
    )
    resp = await client.post(
        "/webhooks/github",
        headers={
            "X-GitHub-Event": "ping",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": _sign(raw_body),
            "Content-Type": "application/json",
        },
        content=raw_body,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


@pytest.mark.fast
def test_max_bucket_key_length_constants_and_model_alignment():
    """Verify MAX_BUCKET_KEY_LENGTH is 200 and matches model and webhook definitions."""
    from app.models import MAX_BUCKET_KEY_LENGTH as MODEL_MAX_KEY
    from app.models import BUCKET_KEY_MAX_LENGTH as MODEL_MAX_KEY_ALIAS
    from app.services.rate_limit import MAX_BUCKET_KEY_LENGTH as SERVICE_MAX_KEY
    from app.services.rate_limit import BUCKET_KEY_MAX_LENGTH as SERVICE_MAX_KEY_ALIAS

    assert MODEL_MAX_KEY == 200
    assert MODEL_MAX_KEY_ALIAS == 200
    assert SERVICE_MAX_KEY == 200
    assert SERVICE_MAX_KEY_ALIAS == 200
    assert _WEBHOOK_RATE_KEY_MAX_CHARS == 200

    # Ensure WebhookRateLimitHit.bucket_key column length matches the constant exactly
    assert WebhookRateLimitHit.bucket_key.type.length == 200
    assert WebhookRateLimitHit.bucket_key.type.length == MODEL_MAX_KEY
    assert WebhookRateLimitHit.bucket_key.type.length == SERVICE_MAX_KEY


@pytest.mark.fast
def test_normalize_bucket_key_bounds_oversized_keys():
    """Verify keys are bounded to <= MAX_BUCKET_KEY_LENGTH characters."""
    # Under or equal to max length: preserved verbatim
    short_key = "owner/repo|sender"
    assert normalize_bucket_key(short_key) == short_key
    assert bound_bucket_key(short_key) == short_key

    exact_200_key = "a" * 200
    assert normalize_bucket_key(exact_200_key) == exact_200_key
    assert len(normalize_bucket_key(exact_200_key)) == 200

    # Oversized keys: 201, 300, 500, 2000 chars are bounded to <= MAX_BUCKET_KEY_LENGTH
    for length in (201, 250, 300, 500, 2000):
        oversized = "x" * length
        bounded = normalize_bucket_key(oversized)
        assert len(bounded) <= MAX_BUCKET_KEY_LENGTH
        assert bound_bucket_key(oversized) == bounded
        # Deterministic
        assert normalize_bucket_key(oversized) == bounded

    # Distinct oversized keys sharing a prefix produce distinct bounded keys (hash prevents collisions)
    key_a = "prefix" * 40 + "_variant_a"
    key_b = "prefix" * 40 + "_variant_b"
    assert len(key_a) > 200
    assert len(key_b) > 200
    bounded_a = normalize_bucket_key(key_a)
    bounded_b = normalize_bucket_key(key_b)
    assert len(bounded_a) <= 200
    assert len(bounded_b) <= 200
    assert bounded_a != bounded_b

    # Lone surrogates in oversized keys are handled safely
    surrogate_oversized = "org/repo" + "\ud800" * 300
    bounded_surrogate = normalize_bucket_key(surrogate_oversized)
    assert len(bounded_surrogate) <= 200


@pytest.mark.fast
@pytest.mark.asyncio
async def test_in_memory_backend_with_oversized_keys():
    """In-memory rate limiter seamlessly handles keys longer than 200 chars."""
    backend = InMemoryRateLimitBackend()
    key_300 = "org/oversized-repository-name-that-is-exceptionally-long" * 5 + "|user"
    assert len(key_300) > 200

    assert await backend.consume(key_300, maximum=2, window=10, now=1000.0) == 0
    assert await backend.consume(key_300, maximum=2, window=10, now=1001.0) == 0
    # Throttled on third attempt
    assert await backend.consume(key_300, maximum=2, window=10, now=1002.0) == 9

    # Via consume_rate_limit entry point
    assert (
        await rate_limit.consume_rate_limit(
            "memory", key_300, maximum=2, window=10, now=1000.0
        )
        == 0
    )


@pytest.mark.fast
def test_bucket_lock_id_with_oversized_key():
    """Advisory lock ID computation works deterministically with oversized keys."""
    key_500 = "long_key_" * 55
    assert len(key_500) > 200

    lock_id_1 = bucket_lock_id(key_500)
    lock_id_2 = bucket_lock_id(key_500)
    assert lock_id_1 == lock_id_2
    assert -(2**63) <= lock_id_1 < 2**63

