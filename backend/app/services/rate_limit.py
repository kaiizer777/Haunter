"""
Sliding-window rate-limit store for webhook ingress (issue #161).

The webhook throttler in :mod:`app.webhooks` needs a sliding-window budget keyed
``repo-full-name|sender-login``. Where those buckets live decides how strong the
control actually is, so the store is a seam rather than a module-level dict: the
budget is no longer a detail of the router, and a different implementation (one
whose state outlives the process) can be dropped in without touching the
throttling arithmetic that the wire contract depends on.

* :class:`InMemoryRateLimitBackend` — process-local buckets. Correct for local
  dev and for the hermetic test suite, and the previous behaviour verbatim.
* :class:`PostgresRateLimitBackend` — rows in ``webhook_rate_limit_hits``, so
  every process that can see the database spends from ONE budget.

Postgres rather than DynamoDB, deliberately. It needs no new IAM permissions:
the orchestrator role's inline policy grants only self-invoke plus one SSM read
and explicitly denies every other service (infra/aws/lambda.tf), and reaching
Postgres adds none — a DynamoDB table would mean widening a least-privilege
role. It also reuses the cross-Lambda shared-state pattern already established
by ``app.services.audit_pipeline.claim_next_queued_job``.

Nothing here is constructed at import time and nothing here opens a connection
on import: this module is pulled in by the webhook request path, and building a
store must not be able to reach a database while the module graph is still
loading.

Why the seam exists at all: the orchestrator runs on Lambda behind a Function URL
with ``reserved_concurrent_executions = 10`` (infra/aws/variables.tf), and module
state in Lambda is per execution environment. An in-process limiter therefore
enforced ``10 x webhook_rate_limit_max_deliveries`` per window against a
documented ceiling of one, and every cold start reset all buckets.

Neither store decides whether a delivery is legitimate: the throttler runs
strictly AFTER HMAC verification, so only a holder of
``GITHUB_WEBHOOK_SECRET`` can spend budget at all.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Protocol, runtime_checkable

from sqlalchemy import delete, func, select, text

from app.models import WebhookRateLimitHit

logger = logging.getLogger(__name__)

#: Hard cap on distinct buckets held in memory. Attacker-chosen keys (a sender
#: that signs deliveries with a fresh repo/sender pair per request) must not be
#: able to grow process memory without bound, so the oldest-inserted bucket is
#: evicted once the cap is reached.
RATE_LIMIT_MAX_BUCKETS = 10_000

#: Minimum spacing between two global prune passes, per backend instance. The
#: prune reclaims rows for buckets that will never post again — housekeeping,
#: never part of the correctness argument for a single delivery — so it must not
#: become part of the hot path.
POSTGRES_PRUNE_INTERVAL_SECONDS = 30.0

#: Rows deleted per global prune pass. Keeps the DELETE's row-lock set, and the
#: WAL it generates, bounded no matter how large the backlog is.
POSTGRES_PRUNE_BATCH = 5_000

#: Why the shared store cannot leak rows forever, and why it has no hard row cap
#: like the in-memory one. Two mechanisms, by when they act:
#:
#: 1. Per-bucket pruning runs on EVERY consume. Rows with
#:    ``hit_at <= now - window`` are deleted before the count, and the count gate
#:    only inserts when the live count is below ``maximum`` — so a live bucket
#:    holds at most ``maximum`` rows at any instant.
#: 2. A batched, interval-throttled global prune reclaims rows belonging to
#:    buckets that are never touched again (the common case: a one-shot sender).
#:    Without it those rows would sit forever, because per-bucket pruning only
#:    runs when that exact key posts again.
#:
#: What this does NOT bound is the row COUNT: it is
#: ``active_keys_within_the_window * maximum``, and a webhook-secret holder can
#: mint unbounded distinct keys. That is a deliberate trade against the
#: alternative. A hard cap means evicting buckets, and evicting a LIVE bucket
#: hands an attacker the ability to reset a victim's budget on demand — a
#: denial-of-throttling, which is a cost-amplification primitive and strictly
#: worse than the bounded table it would prevent. Time-based expiry is the
#: honest bound: no row outlives one window past its last write, and the global
#: prune is batched so a large backlog drains over successive passes rather than
#: behind one long lock.

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@runtime_checkable
class RateLimitBackend(Protocol):
    """Sliding-window budget store keyed by an opaque string."""

    async def consume(
        self, key: str, *, maximum: int, window: int, now: Optional[float] = None
    ) -> int:
        """Record one delivery against ``key`` and return retry-after seconds.

        Returns 0 when the delivery is allowed. Otherwise returns
        ``max(1, int(oldest + window - now) + 1)`` — the seconds until the
        oldest in-window entry leaves the window, which is the caller's
        ``Retry-After``.

        ``now`` is a clock override for deterministic tests only; ``None`` means
        "use the backend's own clock". An implementation whose state is shared
        between processes must order that state on a wall clock.

        Callers own the "disabled" check (``maximum <= 0 or window <= 0``
        disables throttling, failing open on misconfiguration); implementations
        assume a positive budget and window.
        """

    def reset(self) -> None:
        """Drop process-local limiter state. Test hook only.

        MUST NOT touch a database: it is called from synchronous test fixtures,
        and the hermetic suite must never open a connection.
        """


class InMemoryRateLimitBackend:
    """Process-local sliding-window buckets.

    Extracted verbatim from the module-level implementation in
    ``app.webhooks`` that this replaced — same eviction order, same cap, same
    arithmetic. Two properties are load-bearing and must not be "improved":

    * the oldest-inserted bucket is evicted first (``next(iter(dict))``), which
      caps memory against attacker-chosen keys;
    * eviction from a bucket drops entries with ``hit_at <= now - window``
      (inclusive), which is what makes the retry-after arithmetic line up with
      the window the caller was promised.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    async def consume(
        self, key: str, *, maximum: int, window: int, now: Optional[float] = None
    ) -> int:
        current = now if now is not None else time.monotonic()
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= RATE_LIMIT_MAX_BUCKETS:
                    self._buckets.pop(next(iter(self._buckets)))
                bucket = self._buckets[key] = deque()
            cutoff = current - window
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= maximum:
                retry_after = int(bucket[0] + window - current) + 1
                return max(1, retry_after)
            bucket.append(current)
            return 0

    def reset(self) -> None:
        self._buckets.clear()


def bucket_lock_id(key: str) -> int:
    """Map a bucket key to a 64-bit advisory-lock id.

    Derived in Python with BLAKE2b rather than in SQL (``hashtextextended``) so
    the same key produces the same lock in every process and every Postgres
    version, with no dependency on a server function's availability.
    ``signed=True`` maps the digest into the signed 64-bit range that
    ``pg_advisory_xact_lock(bigint)`` accepts.

    A collision is harmless: it over-serialises two unrelated buckets for the
    lifetime of one transaction. It does not merge their budgets, which is the
    only thing that would be a correctness bug.
    """
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def _to_utc_datetime(epoch_seconds: float) -> datetime:
    """Convert epoch seconds to a whole-microsecond UTC datetime.

    Rounding to microseconds up front is what keeps the retry-after arithmetic
    exact. Postgres TIMESTAMP stores microseconds, so a value written at
    t=1000.0 comes back as exactly t=1000.000000. Recomputing
    ``oldest + window - current`` in float seconds instead would round at the
    ~1e-7 s level, and ``int()`` could truncate 8.9999999 to 8 where the
    in-memory backend returns 9. Whole microseconds make every intermediate an
    exact integer count of microseconds, so both backends agree on the number
    the wire contract promises.
    """
    return _EPOCH + timedelta(microseconds=round(epoch_seconds * 1_000_000))


class PostgresRateLimitBackend:
    """Sliding-window buckets shared by every process that can see the DB.

    One accepted delivery is one row in ``webhook_rate_limit_hits``; a bucket's
    in-window count is the number of its rows newer than ``now - window``. This
    is the store that makes ``WEBHOOK_RATE_LIMIT_MAX_DELIVERIES`` mean what it
    says under Lambda, where ten execution environments would otherwise each get
    their own budget.

    Uses a wall clock (:func:`time.time`) rather than :func:`time.monotonic`,
    because these rows are shared between processes and must be ordered against
    a clock they all agree on. Clock skew between machines therefore shows up as
    a small error in the window, which is the right trade for a throttle.
    """

    def __init__(
        self, *, prune_interval_seconds: float = POSTGRES_PRUNE_INTERVAL_SECONDS
    ) -> None:
        self._prune_interval = prune_interval_seconds
        # Insertion-ordered so the oldest-first eviction mirrors the in-memory
        # store's dict ordering. Bounded, or it would trade a leak in the table
        # for a leak in process memory.
        self._seen_keys: OrderedDict[str, None] = OrderedDict()
        self._last_prune: Optional[float] = None

    async def consume(
        self, key: str, *, maximum: int, window: int, now: Optional[float] = None
    ) -> int:
        current = now if now is not None else time.time()
        current_at = _to_utc_datetime(current)

        # Its OWN session, never the request-scoped `db`. That session can still
        # be rolled back after this returns — _record_webhook_delivery and the
        # duplicate-delivery handler both roll back on a path that continues past
        # the throttler — which would erase the delivery we just counted. Same
        # reason app.services.audit_pipeline.claim_next_queued_job opens one.
        from app.db import async_session_maker

        async with async_session_maker() as db:
            try:
                await self._prune_expired_if_due(db, key=key, now=current, window=window)
                retry_after = await self._consume_locked(
                    db, key=key, maximum=maximum, window=window, current_at=current_at
                )
                await db.commit()
                return retry_after
            except Exception:
                await db.rollback()
                raise

    async def _consume_locked(
        self,
        db: Any,
        *,
        key: str,
        maximum: int,
        window: int,
        current_at: datetime,
    ) -> int:
        """Prune, count, and conditionally insert — serialised per bucket.

        WHY AN ADVISORY LOCK AND NOT A SINGLE ATOMIC STATEMENT: the decision is
        read-count-then-write ("is this bucket over budget? if not, record the
        delivery"), and under PostgreSQL's default READ COMMITTED isolation
        wrapping that in a transaction is NOT enough — two execution environments
        can both read a count of ``maximum - 1`` and both insert, overshooting the
        budget by one per colliding pair. Environments colliding on a
        pre-increment count is the exact failure this store exists to prevent, so
        the read and the write are serialised, not merely wrapped.

        ``pg_advisory_xact_lock`` is used rather than ``SELECT ... FOR UPDATE``
        because FOR UPDATE cannot lock a row that does not exist yet, and the
        FIRST delivery for a new bucket key is precisely the case with nothing to
        lock. Keyed by a hash of the bucket key it is always available and scoped
        to one bucket: two different senders never block each other, and a
        transaction takes exactly one lock, so there is no lock-ordering cycle and
        therefore no deadlock.

        The *xact* form is deliberate. It is released at COMMIT/ROLLBACK on the
        connection that took it, whereas the session-level ``pg_advisory_lock``
        would be unsafe here: this app reaches Postgres through Neon's PgBouncer
        in transaction-pooling mode, where the server session behind a client
        connection can change between transactions, and a session-level lock
        could be stranded on a backend no client holds any more.

        The statements are separate on purpose. A data-modifying CTE cannot be
        folded into the count that follows it: every statement in a CTE sees the
        same snapshot, so a prune and a count in one statement would still count
        the rows the prune deleted.

        Deadlock freedom: the advisory lock is taken FIRST and only ever waits on
        row locks from a concurrent global prune, which in turn never waits on an
        advisory lock. One lock per transaction, always in the same position, so
        there is no cycle to form.
        """
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": bucket_lock_id(key)},
        )

        cutoff_at = current_at - timedelta(seconds=window)
        await db.execute(
            delete(WebhookRateLimitHit)
            .where(
                WebhookRateLimitHit.bucket_key == key,
                WebhookRateLimitHit.hit_at <= cutoff_at,
            )
            .execution_options(synchronize_session=False)
        )

        total, oldest_at = (
            await db.execute(
                select(
                    func.count(WebhookRateLimitHit.id),
                    func.min(WebhookRateLimitHit.hit_at),
                ).where(WebhookRateLimitHit.bucket_key == key)
            )
        ).one()

        if int(total) >= maximum:
            # total >= maximum >= 1, so the bucket has a row and min() is not NULL.
            retry_after = int(
                ((oldest_at + timedelta(seconds=window)) - current_at).total_seconds()
            ) + 1
            return max(1, retry_after)

        db.add(WebhookRateLimitHit(bucket_key=key, hit_at=current_at))
        return 0

    async def _prune_expired_if_due(
        self, db: Any, *, key: str, now: float, window: int
    ) -> None:
        """Reclaim rows for buckets that will never post again.

        Gated twice so it can never become the hot path: only when this process
        has not seen the key before, and only if ``prune_interval_seconds`` has
        elapsed since the last pass. Across ten execution environments the whole
        fleet prunes at most ten times per interval, and only on keys it had not
        already seen.

        Runs in its own transaction, BEFORE the bucket lock is taken: a global
        DELETE takes row locks on OTHER buckets, and holding it inside the
        per-bucket transaction would make unrelated senders wait on this
        housekeeping. Its own transaction also means the commit below cannot be
        half-applied by the bucket transaction's rollback.

        Failure is logged and swallowed. It is hygiene, not part of any
        delivery's decision, and must not become a 5xx on the webhook path.
        """
        if key in self._seen_keys:
            return
        if self._last_prune is not None and now - self._last_prune < self._prune_interval:
            return
        if len(self._seen_keys) >= RATE_LIMIT_MAX_BUCKETS:
            self._seen_keys.popitem(last=False)
        self._seen_keys[key] = None
        # Claim the slot before running, so a prune that keeps failing cannot be
        # retried into a hot loop by every subsequent new key.
        self._last_prune = now
        try:
            # Batched through an id subquery rather than a bare DELETE ... LIMIT:
            # the bound is what keeps this pass's row-lock set and its WAL small
            # regardless of backlog size, and the subquery is driven by
            # ix_webhook_rate_limit_hits_hit_at, so the pass stays an index scan
            # instead of scanning the whole table to satisfy an ordering it does
            # not have.
            await db.execute(
                delete(WebhookRateLimitHit).where(
                    WebhookRateLimitHit.id.in_(
                        select(WebhookRateLimitHit.id)
                        .where(
                            WebhookRateLimitHit.hit_at <= _to_utc_datetime(now - window)
                        )
                        .limit(POSTGRES_PRUNE_BATCH)
                    )
                )
            )
            await db.commit()
        except Exception as exc:
            await db.rollback()
            logger.warning(
                "Webhook rate limit prune failed (%s); expired rows will be "
                "reclaimed on a later pass",
                type(exc).__name__,
            )

    def reset(self) -> None:
        """Clear the process-local prune bookkeeping only.

        Deliberately does not touch ``webhook_rate_limit_hits``: this is called
        from synchronous fixtures, and the hermetic suite must never open a
        connection. Tests that need the table emptied call ``truncate_all``.
        """
        self._seen_keys.clear()
        self._last_prune = None


#: Backend used when nothing selects one. A module constant so a freshly
#: imported process has a working limiter with no configuration and no database
#: in the picture.
DEFAULT_RATE_LIMIT_BACKEND = "memory"

#: Every backend this module can build, keyed by the name a deployment selects
#: it with.
_BACKENDS: dict[str, type[RateLimitBackend]] = {
    "memory": InMemoryRateLimitBackend,
    "postgres": PostgresRateLimitBackend,
}

_unknown_name_logged = False

_resolved: Optional[RateLimitBackend] = None


def build_rate_limit_backend(name: str) -> RateLimitBackend:
    """Instantiate the backend registered under ``name``.

    An unrecognised name logs once at ERROR and falls back to the in-memory
    store rather than raising. Two alternatives were rejected: raising turns a
    config typo into a 5xx on every webhook delivery — an availability incident
    caused by a security control — and returning ``None`` switches the control
    off silently, which is the failure mode this module exists to close. The
    in-memory store is still a real control, and the ERROR log makes the
    misconfiguration loud enough to alert on.
    """
    global _unknown_name_logged

    normalized = (name or "").strip().lower()
    backend_cls = _BACKENDS.get(normalized)
    if backend_cls is None:
        if not _unknown_name_logged:
            _unknown_name_logged = True
            logger.error(
                "Unknown webhook rate limit backend %r; known values are %s. "
                "Falling back to the per-process in-memory limiter, whose budget "
                "is per execution environment and therefore weaker than configured.",
                normalized,
                sorted(_BACKENDS),
            )
        return InMemoryRateLimitBackend()
    return backend_cls()


def get_rate_limit_backend() -> RateLimitBackend:
    """Return the process-wide limiter, building it on first use."""
    global _resolved

    if _resolved is None:
        _resolved = build_rate_limit_backend(DEFAULT_RATE_LIMIT_BACKEND)
    return _resolved


def reset_rate_limit_backend() -> None:
    """Drop the cached backend and its process-local state. Test hook only."""
    global _resolved

    if _resolved is not None:
        _resolved.reset()
    _resolved = None
