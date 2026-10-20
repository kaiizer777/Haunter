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
async def test_global_prune_is_interval_throttled(db: AsyncSession):
    """The prune does not run on every new key.

    Unthrottled, every fresh attacker-chosen key would trigger a table-wide
    DELETE on the request path.
    """
    backend = PostgresRateLimitBackend()  # default 30s interval

    # First ever key: the pass runs, and there is nothing expired to reclaim.
    assert await backend.consume("gate/k1|s", maximum=5, window=10, now=5000.0) == 0
    assert await _total_rows(db) == 1

    # A different, never-seen key 10s later must NOT prune. `now` is 5010.0, not
    # 5001.0, and that is the whole point: the pass reclaims `hit_at <= now - window`,
    # so at now=5010.0 the cutoff is 5000.0 and the row written at 5000.0 IS expired
    # and WOULD be reclaimed if the pass ran -- the assertion below would see 1 row,
    # not 2. At now=5001.0 the cutoff is 4991.0, nothing is expired, and the
    # assertion is satisfied whether the pass ran or not: it cannot see the throttle.
    assert await backend.consume("gate/k2|s", maximum=5, window=10, now=5010.0) == 0
    assert await _total_rows(db) == 2, "prune ran despite the interval not elapsing"

    # Past the interval it does: the two older rows are now outside the window.
    assert (
        await backend.consume(
            "gate/k3|s",
            maximum=5,
            window=10,
            now=5000.0 + POSTGRES_PRUNE_INTERVAL_SECONDS,
        )
        == 0
    )
    assert await _total_rows(db) == 1, "prune did not run after the interval elapsed"

    # ...and the interval resets, so the next new key is throttled again. 10s later,
    # not 1s: if `_last_prune` had NOT been reset by the pass above, the pass would
    # run again here (5040 - 5000 = 40 >= 30) with cutoff 5030.0, reclaiming the row
    # written at 5030.0 and leaving one row instead of two. That difference is what
    # this assertion now measures.
    assert (
        await backend.consume(
            "gate/k4|s",
            maximum=5,
            window=10,
            now=5000.0 + POSTGRES_PRUNE_INTERVAL_SECONDS + 10.0,
        )
        == 0
    )
    assert await _total_rows(db) == 2


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
