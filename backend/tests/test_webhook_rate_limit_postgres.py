"""Shared-store webhook ingress throttling (issue #161).

DB (requires TEST_DATABASE_URL, run in CI / `--all` / explicit target).

The in-process limiter used by tests/test_webhook_rate_limit.py is per
execution environment, so with reserved_concurrent_executions=10 it enforced ten
times the documented budget and a cold start reset it. These tests cover
PostgresRateLimitBackend against a real database, where the properties that fix
only hold against a real one:

- two independent instances (two Lambda environments) spend from ONE budget —
  the actual regression test for the 10x defect
- the sliding-window arithmetic matches the in-memory store exactly, down to
  the Retry-After value the 429 carries
- concurrent consumers on one bucket never overshoot `maximum`, which is what
  pg_advisory_xact_lock is there for
- expired rows for buckets that never post again are reclaimed

They live in their own module rather than alongside the hermetic ones because
conftest.py auto-marks anything using the `db` fixture as `db`, and a bare
`pytest` deselects those — putting them in test_webhook_rate_limit.py would
silently drop the whole 429 regression suite from the default run.
"""

import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import WebhookRateLimitHit
from app.services.rate_limit import (
    POSTGRES_PRUNE_INTERVAL_SECONDS,
    PostgresRateLimitBackend,
    _to_utc_datetime,
)


@pytest.fixture(autouse=True)
def _reset_cached_backend():
    """Keep the module-level backend cache from leaking between tests."""
    from app.webhooks import _reset_webhook_rate_limiter

    _reset_webhook_rate_limiter()
    yield
    _reset_webhook_rate_limiter()


async def _row_count(db: AsyncSession, key: str) -> int:
    return int(
        (
            await db.execute(
                select(func.count(WebhookRateLimitHit.id)).where(
                    WebhookRateLimitHit.bucket_key == key
                )
            )
        ).scalar_one()
    )


async def _total_rows(db: AsyncSession) -> int:
    return int(
        (await db.execute(select(func.count(WebhookRateLimitHit.id)))).scalar_one()
    )


@pytest.mark.asyncio
async def test_postgres_backend_matches_in_memory_sliding_window(
    db: AsyncSession,
):
    """Same timeline, same numbers — including the exact Retry-After of 9.

    That 9 is what tests/test_webhook_rate_limit.py pins for the in-memory
    store, and it is what the 429's Retry-After header carries. A drift between
    the two stores is a wire-contract change, not an implementation detail.
    """
    backend = PostgresRateLimitBackend()
    key = "exp-org/exp-repo|s"
    assert await backend.consume(key, maximum=2, window=10, now=1000.0) == 0
    assert await backend.consume(key, maximum=2, window=10, now=1001.0) == 0
    assert await backend.consume(key, maximum=2, window=10, now=1002.0) == 9
    # Window slid past both entries, so the budget is back — and only the one
    # surviving delivery is on record.
    assert await backend.consume(key, maximum=2, window=10, now=1011.0) == 0
    assert await _row_count(db, key) == 1


@pytest.mark.asyncio
async def test_two_instances_share_one_budget(db: AsyncSession):
    """Two independent limiter instances converge on ONE shared budget.

    The regression test for the issue #161 defect. Two objects with no shared
    Python state at all — the process-local analogue of two Lambda execution
    environments — must together spend `maximum` deliveries between them, not
    `2 * maximum`.
    """
    env_a = PostgresRateLimitBackend()
    env_b = PostgresRateLimitBackend()
    key = "shared-org/shared-repo|s"

    assert await env_a.consume(key, maximum=2, window=60, now=2000.0) == 0
    assert await env_b.consume(key, maximum=2, window=60, now=2001.0) == 0
    # Both instances agree the bucket is spent, asked from either side.
    assert await env_a.consume(key, maximum=2, window=60, now=2002.0) > 0
    assert await env_b.consume(key, maximum=2, window=60, now=2003.0) > 0
    # ...and no refused delivery was recorded.
    assert await _row_count(db, key) == 2


@pytest.mark.asyncio
async def test_buckets_stay_scoped_to_their_key(db: AsyncSession):
    """One noisy sender cannot starve another bucket — no accidental merge.

    Wall-clock pinned via explicit ``now`` so the window is deterministic
    rather than a function of when the suite happens to run.
    """
    env_a = PostgresRateLimitBackend()
    env_b = PostgresRateLimitBackend()

    assert await env_a.consume("noisy/noisy-repo|s", maximum=2, window=60, now=8000.0) == 0
    assert await env_a.consume("noisy/noisy-repo|s", maximum=2, window=60, now=8001.0) == 0
    assert await env_a.consume("noisy/noisy-repo|s", maximum=2, window=60, now=8002.0) > 0
    # Same repo, different sender, and different repo entirely: both fresh.
    assert await env_b.consume("noisy/noisy-repo|other", maximum=2, window=60, now=8002.0) == 0
    assert await env_b.consume("other-org/noisy-repo|s", maximum=2, window=60, now=8002.0) == 0
    assert await _row_count(db, "noisy/noisy-repo|s") == 2


@pytest.mark.asyncio
async def test_concurrent_consume_never_overshoots_the_budget(db: AsyncSession):
    """Racing instances cannot both pass the pre-insert count.

    Ten concurrent consumers against a budget of 5: exactly five must be
    admitted. This is what a bare transaction gets wrong — under READ COMMITTED
    two transactions can both read a count of 4 and both insert, overshooting by
    one per colliding pair. pg_advisory_xact_lock on the bucket key is what
    closes that window, and it has to close it for a key whose first delivery
    has no row to SELECT ... FOR UPDATE yet.
    """
    key = "race/race|s"
    instances = [PostgresRateLimitBackend() for _ in range(10)]

    results = await asyncio.gather(
        *(
            instance.consume(key, maximum=5, window=60, now=3000.0)
            for instance in instances
        )
    )
    admitted = [r for r in results if r == 0]
    assert len(admitted) == 5, f"expected exactly 5 admissions, got {results}"
    assert await _row_count(db, key) == 5


@pytest.mark.asyncio
async def test_live_bucket_is_bounded_by_pruning_on_every_consume(
    db: AsyncSession,
):
    """Expired rows for a bucket that comes back are pruned, not accumulated.

    Each cycle spends the whole budget, waits out the window, then spends it
    again. Without the per-consume prune every cycle would stack another
    `maximum` rows on top of the previous cycle's, so the table would grow
    without bound for a key that is still active.
    """
    # A prune interval no timeline in this test can reach -- the whole span is
    # 402s. Without it the GLOBAL pass fires at the start of every cycle (cycles
    # are 100s apart, the interval is 30s) and reclaims all three prior rows
    # before `_consume_locked` runs, so the per-consume DELETE is never
    # exercised and deleting it leaves every assertion here green. With the
    # global pass out of reach, that DELETE is the only mechanism that can keep
    # this bucket under `maximum`.
    backend = PostgresRateLimitBackend(prune_interval_seconds=10_000.0)
    key = "cycling/cycling|s"
    for cycle in range(5):
        base = 4000.0 + cycle * 100.0
        for offset in range(3):
            assert await backend.consume(key, maximum=3, window=10, now=base + offset) == 0
        assert await _row_count(db, key) == 3, f"cycle {cycle} leaked rows"


@pytest.mark.asyncio
async def test_global_prune_reclaims_buckets_that_never_post_again(
    db: AsyncSession,
):
    """Rows for a one-shot bucket are reclaimed by the batched global prune.

    Per-bucket pruning only runs when that exact key posts again, so without a
    global pass a sender that fires once would leave its row behind forever.
    The prune is gated on a key this process has not seen and on the interval,
    so the pass is driven explicitly rather than left to a side effect.
    """
    one_shot = PostgresRateLimitBackend()
    assert await one_shot.consume("one-shot/one-shot|s", maximum=5, window=10, now=100.0) == 0
    assert await _total_rows(db) == 1

    # A never-seen key whose interval has already elapsed triggers the pass:
    # the row from t=100 is well outside the window and goes, the fresh one
    # (written after the prune) stays.
    fresh = PostgresRateLimitBackend(prune_interval_seconds=0.0)
    assert await fresh.consume("fresh/fresh|s", maximum=5, window=10, now=1000.0) == 0
    assert await _total_rows(db) == 1


@pytest.mark.asyncio
async def test_global_prune_is_interval_throttled(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """The global prune execution is strictly gated by the interval.

    Unthrottled, every fresh attacker-chosen key would trigger a table-wide
    DELETE on the request path. We spy on `_run_global_prune` to strictly verify
    that the query execution obeys the interval gate rather than relying only on
    row counts (which can be masked by per-bucket pruning or unexpired rows).
    """
    backend = PostgresRateLimitBackend()  # default 30s interval
    prune_calls = 0
    real_run_global_prune = backend._run_global_prune

    async def _spy_run_global_prune(*args, **kwargs):
        nonlocal prune_calls
        prune_calls += 1
        return await real_run_global_prune(*args, **kwargs)

    monkeypatch.setattr(backend, "_run_global_prune", _spy_run_global_prune)

    # First ever key: the pass runs, and there is nothing expired to reclaim.
    assert await backend.consume("gate/k1|s", maximum=5, window=10, now=5000.0) == 0
    assert prune_calls == 1, "global prune query should execute on the first call"
    assert await _total_rows(db) == 1

    # Multiple subsequent calls across various keys within PRUNE_INTERVAL must NOT
    # execute global prune again; the interval gate must skip execution.
    for i, key in enumerate(("gate/k2|s", "gate/k3|s", "gate/k4|s", "gate/k1|s")):
        assert (
            await backend.consume(
                key, maximum=5, window=10, now=5000.0 + 2.0 * (i + 1)
            )
            == 0
        )
        assert (
            prune_calls == 1
        ), f"global prune executed despite interval gate not elapsing on {key}"

    # Past the interval it does: older rows outside the window are reclaimed.
    assert (
        await backend.consume(
            "gate/k5|s",
            maximum=5,
            window=10,
            now=5000.0 + POSTGRES_PRUNE_INTERVAL_SECONDS,
        )
        == 0
    )
    assert (
        prune_calls == 2
    ), "global prune query did not execute after interval elapsed"

    # ...and the interval resets, so the next new key is throttled again.
    assert (
        await backend.consume(
            "gate/k6|s",
            maximum=5,
            window=10,
            now=5000.0 + POSTGRES_PRUNE_INTERVAL_SECONDS + 10.0,
        )
        == 0
    )
    assert (
        prune_calls == 2
    ), "global prune query ran within second interval"


@pytest.mark.asyncio
async def test_global_prune_interval_gate_call_count_strict(
    monkeypatch: pytest.MonkeyPatch,
):
    """Strictly verify global prune query execution count obeys the interval gate.

    Verifies:
    1. Call count = 1 on the first call.
    2. Multiple subsequent calls across various keys within PRUNE_INTERVAL stay at 1.
    3. Advancing simulated time past PRUNE_INTERVAL increments call count to 2.
    4. Removing/bypassing the interval gate causes immediate test failure.
    """
    backend = PostgresRateLimitBackend(prune_interval_seconds=30.0)
    prune_calls = 0

    async def _mock_run_global_prune(db, *, now, window):
        nonlocal prune_calls
        prune_calls += 1

    monkeypatch.setattr(backend, "_run_global_prune", _mock_run_global_prune)
    dummy_db = None

    # Initial call: prune runs once.
    await backend._prune_expired_if_due(dummy_db, key="initial/key|s", now=1000.0, window=10)
    assert prune_calls == 1

    # Multiple subsequent calls across various keys within PRUNE_INTERVAL (30s) stay at 1.
    keys_within_interval = ["sub/k1|s", "sub/k2|s", "sub/k3|s", "initial/key|s"]
    for i, k in enumerate(keys_within_interval):
        await backend._prune_expired_if_due(
            dummy_db, key=k, now=1000.0 + 5.0 * (i + 1), window=10
        )
        assert prune_calls == 1, f"global prune executed unexpectedly on {k} within interval"

    # Advance simulated time past PRUNE_INTERVAL (1000.0 + 30.0 = 1030.0): increments to 2.
    await backend._prune_expired_if_due(
        dummy_db, key="past/interval|s", now=1031.0, window=10
    )
    assert prune_calls == 2, "global prune did not execute after interval elapsed"

    # Subsequent call within the new interval stays at 2.
    await backend._prune_expired_if_due(
        dummy_db, key="second/interval|s", now=1040.0, window=10
    )
    assert prune_calls == 2, "global prune executed during second interval"


@pytest.mark.asyncio
async def test_prune_failure_does_not_break_throttling(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """The global pass is hygiene, not correctness — it must never 5xx.

    Fails the housekeeping transaction's commit and asserts the delivery is
    still admitted, still recorded, and that the failure was logged rather than
    swallowed silently.
    """
    backend = PostgresRateLimitBackend()
    calls = {"n": 0}
    real_commit = AsyncSession.commit

    async def _flaky_commit(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:  # the prune's commit, which runs first
            raise RuntimeError("simulated commit failure")
        return await real_commit(self, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "commit", _flaky_commit)

    with caplog.at_level("WARNING", logger="app.services.rate_limit"):
        assert await backend.consume("flaky/flaky|s", maximum=5, window=10, now=6000.0) == 0

    assert await _row_count(db, "flaky/flaky|s") == 1
    assert "prune failed" in caplog.text


@pytest.mark.asyncio
async def test_disabled_budget_writes_no_rows(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """maximum<=0 / window<=0 disable throttling before the store is reached.

    The check lives in _consume_webhook_rate_limit rather than inside a backend,
    so no store can be configured into recording rows for a disabled limiter.
    """
    from app.config import settings
    from app.webhooks import _consume_webhook_rate_limit

    monkeypatch.setattr(settings, "webhook_rate_limit_backend", "postgres")
    for maximum, window in ((0, 60), (-1, 60), (5, 0), (5, -1)):
        monkeypatch.setattr(settings, "webhook_rate_limit_max_deliveries", maximum)
        monkeypatch.setattr(settings, "webhook_rate_limit_window_seconds", window)
        assert await _consume_webhook_rate_limit("off/off|s", now=1.0) == 0
        assert await _consume_webhook_rate_limit("off/off|s", now=2.0) == 0
    assert await _total_rows(db) == 0


@pytest.mark.asyncio
async def test_retry_after_survives_the_microsecond_round_trip(db: AsyncSession):
    """hit_at is stored to the microsecond, and the Retry-After depends on it.

    A timestamp that came back at less than microsecond resolution would make
    `oldest + window - current` a float subtraction, and int() could truncate
    8.9999999 to 8 where the contract promises 9.
    """
    backend = PostgresRateLimitBackend()
    key = "micro/micro|s"
    assert await backend.consume(key, maximum=1, window=10, now=7000.123456) == 0

    stored = (
        (
            await db.execute(
                select(WebhookRateLimitHit.hit_at).where(
                    WebhookRateLimitHit.bucket_key == key
                )
            )
        )
        .scalars()
        .one()
    )
    assert stored == _to_utc_datetime(7000.123456)
    assert stored.microsecond == 123456

    # Computed from what the DATABASE returned — not from the float that was
    # sent — and still a whole number of seconds.
    assert await backend.consume(key, maximum=1, window=10, now=7002.123456) == 9


@pytest.mark.asyncio
async def test_postgres_rate_limit_surrogate_key_degrades_gracefully(db: AsyncSession):
    """A bucket key with lone surrogates does not crash with unhandled UnicodeEncodeError."""
    from app.services.rate_limit import consume_rate_limit

    surrogate_key = "surrogate-org/surrogate-\ud800|user-\udfff"
    result = await consume_rate_limit(
        "postgres", surrogate_key, maximum=2, window=10, now=1000.0
    )
    assert result == 0


@pytest.mark.asyncio
async def test_postgres_rate_limit_degrades_on_operational_error(
    monkeypatch: pytest.MonkeyPatch,
):
    """OperationalError (e.g. PgBouncer drop, connection reset) degrades to in-memory store."""
    from sqlalchemy.exc import OperationalError
    from app.services import rate_limit
    from app.services.rate_limit import consume_rate_limit

    async def _mock_execute(*args, **kwargs):
        raise OperationalError(
            "connection closed", params=None, orig=Exception("connection closed")
        )

    monkeypatch.setattr(AsyncSession, "execute", _mock_execute)

    key = "operr/test|s"
    # First consume: DB fails, degraded in-memory limiter admits, breaker failure count is 1
    assert await consume_rate_limit("postgres", key, maximum=2, window=10, now=1000.0) == 0
    assert rate_limit._consecutive_store_failures == 1
    assert rate_limit._breaker_open_until is None

    # Second consume: degraded limiter admits, breaker failure count is 2
    assert await consume_rate_limit("postgres", key, maximum=2, window=10, now=1001.0) == 0
    assert rate_limit._consecutive_store_failures == 2

    # Third consume: degraded limiter budget exhausted, returns Retry-After 9
    retry_after = await consume_rate_limit("postgres", key, maximum=2, window=10, now=1002.0)
    assert retry_after == 9
    assert rate_limit._consecutive_store_failures == 3


@pytest.mark.asyncio
async def test_postgres_rate_limit_degrades_on_sqlalchemy_error(
    monkeypatch: pytest.MonkeyPatch,
):
    """Generic SQLAlchemyError degrades to in-memory store and increments breaker counter."""
    from sqlalchemy.exc import SQLAlchemyError
    from app.services import rate_limit
    from app.services.rate_limit import consume_rate_limit

    async def _mock_execute(*args, **kwargs):
        raise SQLAlchemyError("query failed")

    monkeypatch.setattr(AsyncSession, "execute", _mock_execute)

    key = "sqlerr/test|s"
    assert await consume_rate_limit("postgres", key, maximum=2, window=10, now=1000.0) == 0
    assert rate_limit._consecutive_store_failures == 1
    assert rate_limit._breaker_open_until is None


@pytest.mark.asyncio
async def test_postgres_rate_limit_degrades_on_dbapi_error(
    monkeypatch: pytest.MonkeyPatch,
):
    """DBAPIError degrades to in-memory store and increments breaker counter."""
    from sqlalchemy.exc import DBAPIError
    from app.services import rate_limit
    from app.services.rate_limit import consume_rate_limit

    async def _mock_execute(*args, **kwargs):
        raise DBAPIError(
            "statement execution failed", params=None, orig=Exception("dbapi error")
        )

    monkeypatch.setattr(AsyncSession, "execute", _mock_execute)

    key = "dbapi/test|s"
    assert await consume_rate_limit("postgres", key, maximum=2, window=10, now=1000.0) == 0
    assert rate_limit._consecutive_store_failures == 1
    assert rate_limit._breaker_open_until is None


@pytest.mark.asyncio
async def test_postgres_rate_limit_degrades_on_asyncpg_error(
    monkeypatch: pytest.MonkeyPatch,
):
    """Direct asyncpg.PostgresError degrades to in-memory store and increments breaker counter."""
    import asyncpg
    from app.services import rate_limit
    from app.services.rate_limit import consume_rate_limit

    async def _mock_execute(*args, **kwargs):
        raise asyncpg.PostgresError("terminating connection due to administrator command")

    monkeypatch.setattr(AsyncSession, "execute", _mock_execute)

    key = "asyncpg/test|s"
    assert await consume_rate_limit("postgres", key, maximum=2, window=10, now=1000.0) == 0
    assert rate_limit._consecutive_store_failures == 1
    assert rate_limit._breaker_open_until is None


@pytest.mark.asyncio
async def test_postgres_rate_limit_breaker_trips_on_consecutive_sqlalchemy_errors(
    monkeypatch: pytest.MonkeyPatch,
):
    """Consecutive SQLAlchemyErrors trip the breaker open and subsequent calls skip the store."""
    from sqlalchemy.exc import SQLAlchemyError
    from app.services import rate_limit
    from app.services.rate_limit import consume_rate_limit

    calls = {"count": 0}

    async def _mock_execute(*args, **kwargs):
        calls["count"] += 1
        raise SQLAlchemyError("pool connection exhausted")

    monkeypatch.setattr(AsyncSession, "execute", _mock_execute)
    threshold = rate_limit._BREAKER_THRESHOLD

    key = "brk-sql/test|s"
    for i in range(threshold):
        res = await consume_rate_limit("postgres", key, maximum=100, window=60, now=2000.0 + i)
        assert res == 0
        assert rate_limit._consecutive_store_failures == i + 1

    # Breaker is now tripped open
    assert rate_limit._breaker_open_until is not None
    calls_after_tripped = calls["count"]

    # Next call skips store entirely
    res = await consume_rate_limit(
        "postgres", key, maximum=100, window=60, now=2000.0 + threshold
    )
    assert res == 0
    assert calls["count"] == calls_after_tripped, "store was queried while circuit breaker was open"


@pytest.mark.asyncio
async def test_postgres_rate_limit_backend_consume_propagates_operational_error(
    monkeypatch: pytest.MonkeyPatch,
):
    """PostgresRateLimitBackend.consume propagates OperationalError so caller orchestrator handles it."""
    from sqlalchemy.exc import OperationalError

    async def _mock_execute(*args, **kwargs):
        raise OperationalError(
            "connection closed", params=None, orig=Exception("connection closed")
        )

    monkeypatch.setattr(AsyncSession, "execute", _mock_execute)

    backend = PostgresRateLimitBackend()
    with pytest.raises(OperationalError, match="connection closed"):
        await backend.consume("raw/test|s", maximum=2, window=10, now=1000.0)
