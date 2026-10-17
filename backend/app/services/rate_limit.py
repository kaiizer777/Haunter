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
import time
from collections import deque
from typing import Optional, Protocol, runtime_checkable

#: Hard cap on distinct buckets held in memory. Attacker-chosen keys (a sender
#: that signs deliveries with a fresh repo/sender pair per request) must not be
#: able to grow process memory without bound, so the oldest-inserted bucket is
#: evicted once the cap is reached.
RATE_LIMIT_MAX_BUCKETS = 10_000


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


_resolved: Optional[RateLimitBackend] = None


def get_rate_limit_backend() -> RateLimitBackend:
    """Return the process-wide limiter, building it on first use."""
    global _resolved

    if _resolved is None:
        _resolved = InMemoryRateLimitBackend()
    return _resolved


def reset_rate_limit_backend() -> None:
    """Drop the cached backend and its process-local state. Test hook only."""
    global _resolved

    if _resolved is not None:
        _resolved.reset()
    _resolved = None
