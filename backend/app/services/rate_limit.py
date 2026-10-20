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
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Protocol, runtime_checkable

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

try:
    import asyncpg
    from asyncpg.exceptions import PostgresError, UndefinedTableError

    _STORE_EXCEPTIONS: tuple[type[BaseException], ...] = (
        SQLAlchemyError,
        ProgrammingError,
        OSError,
        TimeoutError,
        asyncio.TimeoutError,
        PostgresError,
        UndefinedTableError,
        asyncpg.InterfaceError,
    )
except ImportError:  # pragma: no cover
    UndefinedTableError = None  # type: ignore[assignment, misc]
    _STORE_EXCEPTIONS = (
        SQLAlchemyError,
        ProgrammingError,
        OSError,
        TimeoutError,
        asyncio.TimeoutError,
    )

from app.models import (
    BUCKET_KEY_MAX_LENGTH,
    MAX_BUCKET_KEY_LENGTH,
    WebhookRateLimitHit,
)

#: Authoritative ceiling on bucket key length (re-exported from app.models).
MAX_BUCKET_KEY_LENGTH: int = MAX_BUCKET_KEY_LENGTH
BUCKET_KEY_MAX_LENGTH: int = BUCKET_KEY_MAX_LENGTH


def normalize_bucket_key(key: str) -> str:
    """Ensure a bucket key does not exceed MAX_BUCKET_KEY_LENGTH characters.

    If len(key) <= MAX_BUCKET_KEY_LENGTH, return key unchanged.
    Otherwise, deterministically bound the key using a truncated prefix and
    a SHA-256 digest so that:
    1. The result is strictly <= MAX_BUCKET_KEY_LENGTH characters.
    2. Insertion into WebhookRateLimitHit.bucket_key never raises Postgres DataError.
    3. Distinct oversized keys that share a prefix do not collide into the same bucket.
    4. Strings with lone surrogates are encoded safely with errors="replace".
    """
    if len(key) <= MAX_BUCKET_KEY_LENGTH:
        return key

    digest = hashlib.sha256(key.encode("utf-8", errors="replace")).hexdigest()
    prefix_len = MAX_BUCKET_KEY_LENGTH - len(digest) - 1
    prefix = key[:prefix_len] if prefix_len > 0 else ""
    if prefix:
        return f"{prefix}:{digest}"
    return digest[:MAX_BUCKET_KEY_LENGTH]


bound_bucket_key = normalize_bucket_key

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
        key = normalize_bucket_key(key)
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

    ``errors="replace"`` ensures strings containing lone surrogate characters
    (e.g., malformed or adversarial inputs) do not raise UnicodeEncodeError
    and produce a deterministic 64-bit signed integer.

    A collision is harmless: it over-serialises two unrelated buckets for the
    lifetime of one transaction. It does not merge their budgets, which is the
    only thing that would be a correctness bug.
    """
    key = normalize_bucket_key(key)
    digest = hashlib.blake2b(
        key.encode("utf-8", errors="replace"), digest_size=8
    ).digest()
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

    Degraded Mode Trade-off:
    When Postgres rate limiting fails or times out, :func:`consume_rate_limit`
    degrades to :class:`InMemoryRateLimitBackend`. This trades off strict global
    rate limiting for availability by enforcing local per-environment budgets
    during store outages. While this prevents dropping or 5xx-ing webhooks, it
    temporarily relaxes the security posture from a global aggregate ceiling
    across all Lambda execution environments back to an isolated per-environment
    ceiling (e.g. 10 Lambdas = up to a 10x multiplier on allowed deliveries).

    Uses a wall clock (:func:`time.time`) rather than :func:`time.monotonic`,
    because these rows are shared between processes and must be ordered against
    a clock they all agree on. Clock skew between machines therefore shows up as
    a small error in the window, which is the right trade for a throttle.
    """

    def __init__(
        self,
        *,
        prune_interval_seconds: float = POSTGRES_PRUNE_INTERVAL_SECONDS,
        _before_lock_hook: Optional[Callable[[], Awaitable[None]]] = None,
        _after_lock_hook: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        self._prune_interval = prune_interval_seconds
        self._before_lock_hook = _before_lock_hook
        self._after_lock_hook = _after_lock_hook
        # Insertion-ordered so the oldest-first eviction mirrors the in-memory
        # store's dict ordering. Bounded, or it would trade a leak in the table
        # for a leak in process memory.
        self._seen_keys: OrderedDict[str, None] = OrderedDict()
        self._last_prune: Optional[float] = None

    async def consume(
        self, key: str, *, maximum: int, window: int, now: Optional[float] = None
    ) -> int:
        key = normalize_bucket_key(key)
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

        Deadlock freedom: the advisory lock is taken FIRST while this transaction
        holds no row locks, so waiting on it can never complete a cycle. The
        remaining edge — the per-bucket DELETE here and the global prune's
        DELETE taking row locks in different orders (bucket_key order vs hit_at
        order) — is closed on the prune side with SELECT ... FOR UPDATE
        SKIP LOCKED, so the global pass never waits on a row lock at all.
        """
        if self._before_lock_hook is not None:
            await self._before_lock_hook()

        await db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": bucket_lock_id(key)},
        )
        if self._after_lock_hook is not None:
            await self._after_lock_hook()

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

        Gated on the interval, not on key novelty: a hot bucket that posts on
        every delivery must not starve the pass forever, or rows for buckets
        that will never post again are never reclaimed on a busy instance.
        Across ten execution environments the whole fleet still prunes at most
        ten times per interval, and the pass stays out of the hot path.

        Runs in its own transaction, BEFORE the bucket lock is taken: a global
        DELETE takes row locks on OTHER buckets, and holding it inside the
        per-bucket transaction would make unrelated senders wait on this
        housekeeping. Its own transaction also means the commit below cannot be
        half-applied by the bucket transaction's rollback.

        Failure is logged and swallowed. It is hygiene, not part of any
        delivery's decision, and must not become a 5xx on the webhook path.
        """
        if key not in self._seen_keys:
            if len(self._seen_keys) >= RATE_LIMIT_MAX_BUCKETS:
                self._seen_keys.popitem(last=False)
            self._seen_keys[key] = None
        if (
            self._last_prune is not None
            and now - self._last_prune < self._prune_interval
        ):
            return
        # Claim the slot before running, so a prune that keeps failing cannot be
        # retried into a hot loop by every subsequent new key.
        self._last_prune = now
        await self._run_global_prune(db, now=now, window=window)

    async def _run_global_prune(self, db: Any, *, now: float, window: int) -> None:
        try:
            # Batched through an id subquery rather than a bare DELETE ... LIMIT:
            # the bound is what keeps this pass's row-lock set and its WAL small
            # regardless of backlog size, and the subquery is driven by
            # ix_webhook_rate_limit_hits_hit_at, so the pass stays an index scan
            # instead of scanning the whole table to satisfy an ordering it does
            # not have. SKIP LOCKED so this pass never waits on a row locked by
            # a concurrent per-bucket prune: the two DELETEs take row locks in
            # different orders (hit_at order here, bucket_key order there), which
            # is a deadlock cycle without it. Skipped rows are simply reclaimed
            # on a later pass.
            candidate_ids = (
                await db.execute(
                    select(WebhookRateLimitHit.id)
                    .where(
                        WebhookRateLimitHit.hit_at <= _to_utc_datetime(now - window)
                    )
                    .limit(POSTGRES_PRUNE_BATCH)
                    .with_for_update(skip_locked=True)
                )
            ).scalars().all()
            if candidate_ids:
                await db.execute(
                    delete(WebhookRateLimitHit).where(
                        WebhookRateLimitHit.id.in_(candidate_ids)
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
#: in the picture; deployments select explicitly via
#: settings.webhook_rate_limit_backend.
DEFAULT_RATE_LIMIT_BACKEND = "memory"

#: Every backend this module can build, keyed by the name a deployment selects
#: it with.
_BACKENDS: dict[str, type[RateLimitBackend]] = {
    "memory": InMemoryRateLimitBackend,
    "postgres": PostgresRateLimitBackend,
}

_unknown_backend_names_logged: set[str] = set()
#: Backwards-compatible alias for log deduplication latch inspection.
_unknown_name_logged: set[str] = _unknown_backend_names_logged

_resolved_name: Optional[str] = None
_resolved: Optional[RateLimitBackend] = None


def build_rate_limit_backend(name: str) -> RateLimitBackend:
    """Instantiate the backend registered under ``name``.

    An unrecognised name logs once per distinct name at ERROR and falls back
    to the in-memory store rather than raising. Two alternatives were rejected:
    raising turns a config typo into a 5xx on every webhook delivery — an
    availability incident caused by a security control — and returning ``None``
    switches the control off silently, which is the failure mode this module
    exists to close. The in-memory store is still a real control, and the
    ERROR log makes the misconfiguration loud enough to alert on.
    """
    global _unknown_backend_names_logged

    normalized = (name or "").strip().lower()
    backend_cls = _BACKENDS.get(normalized)
    if backend_cls is None:
        if normalized not in _unknown_backend_names_logged:
            _unknown_backend_names_logged.add(normalized)
            logger.error(
                "Unknown webhook rate limit backend %r; known values are %s. "
                "Falling back to the per-process in-memory limiter, whose budget "
                "is per execution environment and therefore weaker than configured.",
                normalized,
                sorted(_BACKENDS),
            )
        return InMemoryRateLimitBackend()
    return backend_cls()


def get_rate_limit_backend(name: str) -> RateLimitBackend:
    """Return the process-wide limiter for ``name``, rebuilding on a change.

    Cached per name rather than built once, so the hot path is a string compare
    instead of a construction, and so a deployment (or a test) that changes the
    setting gets the backend it asked for rather than whichever one happened to
    be resolved first.
    """
    global _resolved_name, _resolved

    # Normalised here, not just in build: without this, "POSTGRES" and
    # "postgres" resolve to different cache slots and the backend is rebuilt
    # on every call that spells the name differently.
    normalized = (name or "").strip().lower()
    if _resolved is None or _resolved_name != normalized:
        _resolved = build_rate_limit_backend(normalized)
        _resolved_name = normalized
    return _resolved


#: Store used when the configured one raises. Resolved once and REUSED: handing
#: out a fresh instance per failed call would reset the bucket every time, which
#: is no limit at all — the opposite of what degraded mode is for.
_degraded: Optional[RateLimitBackend] = None

#: Ceiling for one store round trip per delivery. The Postgres backend holds a
#: pooled connection (and transiently a per-bucket advisory lock) for the whole
#: call, so a wedged store must not wedge the webhook path with it: past this
#: the call is abandoned and the delivery is throttled by the degraded store.
STORE_ROUND_TRIP_TIMEOUT_SECONDS = 5.0

#: Consecutive store failures before the breaker opens and deliveries skip the
#: configured store entirely until the cooldown elapses. Without it, every
#: delivery during an outage pays a full timeout before degrading.
_BREAKER_THRESHOLD = 5
_CONSECUTIVE_STORE_FAILURES_THRESHOLD = _BREAKER_THRESHOLD
_BREAKER_COOLDOWN_SECONDS = 30.0
_consecutive_store_failures = 0
_breaker_open_until: Optional[float] = None

#: Minimum spacing between two ERROR logs for the same backend name. During a
#: store outage every delivery fails, and an ERROR per delivery is a CloudWatch
#: cost and signal problem: the first failure is loud, the rest are counted at
#: debug until the interval elapses.
BACKEND_ERROR_LOG_INTERVAL_SECONDS = 60.0
_last_backend_error_log_at: dict[str, float] = {}


async def _consume_degraded(
    key: str, *, maximum: int, window: int, now: Optional[float] = None
) -> int:
    """Consume from the reused process-local limiter. Degraded mode only.

    Degraded mode trades off strict global rate limiting for availability by
    enforcing local per-environment budgets during store outages. While this
    prevents dropping or failing webhooks when the primary store is down, it
    temporarily relaxes the security posture from a global aggregate ceiling
    across all Lambda execution environments back to an isolated per-environment
    ceiling (e.g. 10 Lambdas = up to a 10x delivery multiplier).
    """
    global _degraded

    key = normalize_bucket_key(key)
    if _degraded is None:
        _degraded = InMemoryRateLimitBackend()
    return await _degraded.consume(key, maximum=maximum, window=window, now=now)


def _log_backend_error_throttled(name: str, exc: BaseException) -> None:
    """ERROR on the first failure per interval, debug for the rest."""
    now_mono = time.monotonic()
    last = _last_backend_error_log_at.get(name)
    if last is not None and now_mono - last < BACKEND_ERROR_LOG_INTERVAL_SECONDS:
        logger.debug(
            "Webhook rate limit backend %r still unavailable (%s); throttling "
            "with the in-process limiter (consecutive failures: %d)",
            name,
            type(exc).__name__,
            _consecutive_store_failures,
        )
        return
    _last_backend_error_log_at[name] = now_mono
    logger.error(
        "Webhook rate limit backend %r unavailable (%s); throttling with the "
        "in-process limiter for this delivery (consecutive failures: %d)",
        name,
        type(exc).__name__,
        _consecutive_store_failures,
    )
    logger.warning(
        "Postgres rate limit store unavailable; operating in degraded in-memory mode "
        "with per-instance budget (relaxed global ceiling)"
    )
    orig = getattr(exc, "orig", None)
    is_missing_table = (
        isinstance(exc, ProgrammingError)
        or (UndefinedTableError is not None and isinstance(exc, UndefinedTableError))
        or (UndefinedTableError is not None and isinstance(orig, UndefinedTableError))
        or "does not exist" in str(exc).lower()
        or "undefinedtable" in type(exc).__name__.lower()
        or (orig is not None and "undefinedtable" in type(orig).__name__.lower())
    )
    if is_missing_table:
        logger.warning(
            "Webhook rate limit table 'webhook_rate_limit_hits' does not exist "
            "(migration a2b3c4d5e6f7 pending?); run 'alembic upgrade head'. "
            "Operating in degraded in-memory mode."
        )


async def consume_rate_limit(
    name: str, key: str, *, maximum: int, window: int, now: Optional[float] = None
) -> int:
    """Resolve the backend named by ``name`` and consume one delivery from it.

    Degrades to the process-local store when the configured one fails with a
    store/infrastructure error (``SQLAlchemyError``, ``OSError``, or a store
    round-trip timeout). Degraded mode trades off strict global rate limiting for
    availability by enforcing local per-environment budgets during store outages.
    While this prevents dropping or failing webhooks, it temporarily relaxes the
    security posture from a global aggregate ceiling across all Lambda execution
    environments back to an isolated per-environment ceiling (e.g. 10 Lambdas =
    up to a 10x multiplier).

    Anything else — a programming error in the backend itself — propagates:
    degrading on those would turn every bug into silently weaker throttling, which
    is exactly the failure mode the ERROR log exists to surface. The throttler is
    a guard rail, not the product: a store outage must not turn every webhook
    delivery into a 5xx, but a down limiter is silently weaker than the operator
    believes it is and that has to stay alertable, hence the throttled ERROR and
    relaxed ceiling WARNING rather than silence.

    Past ``_BREAKER_THRESHOLD`` consecutive failures the breaker opens and
    deliveries skip the store until the cooldown elapses, so an outage costs a
    degraded decision instead of a timeout per delivery.

    The caller owns the "disabled" check (``maximum <= 0 or window <= 0``), so
    it costs nothing and cannot be bypassed by whichever store is deployed.
    """
    global _consecutive_store_failures, _breaker_open_until, _degraded

    key = normalize_bucket_key(key)
    backend = get_rate_limit_backend(name)
    if _breaker_open_until is not None and time.monotonic() < _breaker_open_until:
        logger.debug(
            "Webhook rate limit backend %r breaker open; throttling with the "
            "in-process limiter for this delivery",
            name,
        )
        return await _consume_degraded(
            key, maximum=maximum, window=window, now=now
        )
    try:
        result = await asyncio.wait_for(
            backend.consume(key, maximum=maximum, window=window, now=now),
            timeout=STORE_ROUND_TRIP_TIMEOUT_SECONDS,
        )
    except _STORE_EXCEPTIONS as exc:
        _consecutive_store_failures += 1
        threshold = (
            _CONSECUTIVE_STORE_FAILURES_THRESHOLD
            if _CONSECUTIVE_STORE_FAILURES_THRESHOLD != 5
            else _BREAKER_THRESHOLD
        )
        if _consecutive_store_failures >= threshold:
            _breaker_open_until = time.monotonic() + _BREAKER_COOLDOWN_SECONDS
        _log_backend_error_throttled(name, exc)
        return await _consume_degraded(
            key, maximum=maximum, window=window, now=now
        )
    _consecutive_store_failures = 0
    _breaker_open_until = None
    if _degraded is not None:
        _degraded.reset()
        _degraded = None
    return result


def reset_rate_limit_backend() -> None:
    """Drop the cached backend and its process-local state. Test hook only."""
    global _resolved_name, _resolved
    global _consecutive_store_failures, _breaker_open_until, _degraded

    if _resolved is not None:
        _resolved.reset()
    if _degraded is not None:
        _degraded.reset()
    _resolved = None
    _resolved_name = None
    _degraded = None
    _unknown_backend_names_logged.clear()
    _consecutive_store_failures = 0
    _breaker_open_until = None
    _last_backend_error_log_at.clear()
