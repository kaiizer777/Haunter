"""db-perf #136 — bounding the hosting provider-config cache (``_cfg_cache``).

Same bounded-TTL shape that #134 applied to the repo-settings and
model-config caches: TTL was checked on read but expired keys were never
evicted, so the only removal primitive was a blanket ``.clear()``. This
file pins the two guarantees added by #136 — expired entries are pruned on
store, and the size is capped — with hermetic (mocked-DB) tests.

Scope note: unlike #134-4, ``_cfg_cache`` is not leaking today. Every
production call site passes a hardcoded literal ("hosting_provider" /
"sandbox_provider"), so the keyspace has cardinality 2. These tests are
defense-in-depth against a future dynamic-key caller; the cap is therefore
exercised by seeding synthetic keys directly, which is the only way to
drive it given real call sites only ever produce 2 entries.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.adapters.hosting as hosting_mod
from app.adapters.hosting import (
    _cfg_cache,
    _get_provider_config,
    invalidate_provider_cache,
)

# getattr, not a bare import: the constant name is an implementation detail
# and the test must keep working if it is renamed (same idiom as #134).
CFG_MAXSIZE = getattr(hosting_mod, "_CFG_CACHE_MAXSIZE", 8)


@pytest.fixture(autouse=True)
def _clear_caches():
    invalidate_provider_cache()
    yield
    invalidate_provider_cache()


def _mock_session_ctx(mock_session):
    ctx = AsyncMock()
    ctx.__aenter__.return_value = mock_session
    ctx.__aexit__.return_value = None
    return ctx


# ---------------------------------------------------------------------------
# #136: bounded caches with expiry pruning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_config_expired_entries_pruned_on_store():
    """Expired entries are evicted (not just skipped) on the next store.

    A TTL miss on read is not eviction — the stale entry survives in the dict
    indefinitely. Seeding a key 60x past TTL and then storing a *different*
    key proves the stale one is actually removed from the cache.
    """
    invalidate_provider_cache()
    # 3600s stale against a 60s TTL: a 60x margin, so this cannot flake.
    _cfg_cache["hosting_provider"] = ("aws", time.monotonic() - 3600.0)

    mock_row = MagicMock()
    mock_row.value = "aws"
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_row
    mock_session = AsyncMock()
    mock_session.execute.return_value = mock_result

    with patch(
        "app.db.async_session_maker",
        return_value=_mock_session_ctx(mock_session),
    ):
        res = await _get_provider_config("sandbox_provider", "aws")

    assert res == "aws"
    assert mock_session.execute.call_count == 1
    # Evicted, not merely bypassed on read.
    assert "hosting_provider" not in _cfg_cache
    assert "sandbox_provider" in _cfg_cache


@pytest.mark.asyncio
async def test_provider_config_cache_is_bounded():
    """The cap holds when far more distinct keys than the cap are stored."""
    invalidate_provider_cache()
    # Fresh (unexpired) synthetic keys, so this exercises the size cap and
    # not the expiry prune above. Real call sites only ever produce 2 keys,
    # so seeding directly is the only way to drive the cap at all.
    now = time.monotonic()
    for i in range(CFG_MAXSIZE + 10):
        _cfg_cache[f"synthetic_key_{i}"] = ("aws", now)

    mock_row = MagicMock()
    mock_row.value = "aws"
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_row
    mock_session = AsyncMock()
    mock_session.execute.return_value = mock_result

    with patch(
        "app.db.async_session_maker",
        return_value=_mock_session_ctx(mock_session),
    ):
        res = await _get_provider_config("hosting_provider", "aws")

    assert res == "aws"
    assert len(_cfg_cache) <= CFG_MAXSIZE
    assert "hosting_provider" in _cfg_cache


@pytest.mark.asyncio
async def test_provider_config_overwrite_keeps_other_live_entries():
    """A fresh cache hit must not disturb other live entries.

    Scope note: this pins the *hit* path only. The seeded "hosting_provider"
    entry is fresh and non-empty, so the call returns at the TTL check and
    never reaches the store — ``_prune_cfg_cache`` is not called here, and
    these assertions hold no matter what the cap logic does. The store-path
    guarantee this docstring previously claimed ("the cap is enforced only when
    the stored key is new") is pinned by
    test_provider_config_overwrite_at_cap_does_not_evict_live_entries, and both
    real keys coexisting by test_cap_allows_both_real_keys_to_coexist.
    """
    invalidate_provider_cache()
    now = time.monotonic()
    _cfg_cache["sandbox_provider"] = ("aws", now)
    _cfg_cache["hosting_provider"] = ("aws", now)

    mock_row = MagicMock()
    mock_row.value = "aws"
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_row
    mock_session = AsyncMock()
    mock_session.execute.return_value = mock_result

    with patch(
        "app.db.async_session_maker",
        return_value=_mock_session_ctx(mock_session),
    ):
        await _get_provider_config("hosting_provider", "aws")

    assert len(_cfg_cache) == 2
    assert "sandbox_provider" in _cfg_cache
    assert "hosting_provider" in _cfg_cache


@pytest.mark.asyncio
async def test_provider_config_overwrite_at_cap_does_not_evict_live_entries():
    """Overwriting a live key while the cache is full must evict nothing.

    ``_prune_cfg_cache`` enforces the cap only when the stored key is new, so
    a routine refresh at a full cache must be a no-op for every other entry.
    The cache is filled to exactly the cap first, otherwise the cap would not
    bite and the assertions below would hold regardless of the guard.

    Reaching the branch needs a store for a key that is already live, which the
    normal "aws" path cannot do: a fresh hit returns early on the TTL check,
    and an expired entry is deleted by the expiry prune before the guard runs,
    which makes the key new again. The way in is a falsy ``env_default`` — an
    empty ``HOSTING_PROVIDER`` plus a non-allowlisted DB row stores ``""``,
    which is falsy and so never satisfies the cache-hit check, forcing the
    store path to re-run. Degenerate, but reachable via configuration, and it
    is the only route to a documented guarantee that would otherwise go
    untested.
    """
    invalidate_provider_cache()
    now = time.monotonic()
    # Fresh (unexpired) filler so this exercises the cap and not the expiry
    # prune; same synthetic-key idiom as test_provider_config_cache_is_bounded.
    # CFG_MAXSIZE - 1 entries leave room for the first store to fill the cache
    # to exactly the cap without evicting anything.
    _cfg_cache["sandbox_provider"] = ("aws", now)
    for i in range(CFG_MAXSIZE - 2):
        _cfg_cache[f"synthetic_key_{i}"] = ("aws", now)

    # Allowlist miss -> value falls back to env_default, which is "" here, so
    # the cached value is falsy.
    mock_row = MagicMock()
    mock_row.value = "not-a-provider"
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_row
    mock_session = AsyncMock()
    mock_session.execute.return_value = mock_result

    with patch(
        "app.db.async_session_maker",
        return_value=_mock_session_ctx(mock_session),
    ):
        # First store: "hosting_provider" is new, so the cap applies and the
        # cache fills to exactly the cap.
        await _get_provider_config("hosting_provider", "")
        assert len(_cfg_cache) == CFG_MAXSIZE

        # Second store: the key is now live, so the cap guard is False and the
        # overwrite must not evict anything.
        await _get_provider_config("hosting_provider", "")

    assert len(_cfg_cache) == CFG_MAXSIZE
    assert "sandbox_provider" in _cfg_cache
    assert all(f"synthetic_key_{i}" in _cfg_cache for i in range(CFG_MAXSIZE - 2))


@pytest.mark.asyncio
async def test_cap_allows_both_real_keys_to_coexist():
    """The cap must never be the reason a live key disappears.

    ``get_active_hosting_provider()`` and ``get_active_sandbox_provider()``
    read their keys independently, so a cap below the 2-key keyspace would
    make each read evict the key the other one just cached — the cache could
    then never hit. Driving both real keys through the store path (rather than
    seeding ``_cfg_cache`` directly) is what makes this a test of the cap: a
    seed-and-assert-length check would pass even with the cap at 1.

    The ``>= 2`` floor is deliberately *not* asserted against the constant
    here: that is a restatement of ``_CFG_CACHE_MAXSIZE``, and it would fail
    the test before the behavioural check below ever ran. The floor is instead
    demonstrated — lower the cap to 1 and the second store evicts the first
    key, failing on ``len(_cfg_cache) == 2``.
    """
    invalidate_provider_cache()
    mock_row = MagicMock()
    mock_row.value = "aws"
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_row
    mock_session = AsyncMock()
    mock_session.execute.return_value = mock_result

    with patch(
        "app.db.async_session_maker",
        return_value=_mock_session_ctx(mock_session),
    ):
        await _get_provider_config("sandbox_provider", "aws")
        await _get_provider_config("hosting_provider", "aws")

    # The second store must not have evicted the first key.
    assert len(_cfg_cache) == 2
    assert "sandbox_provider" in _cfg_cache
    assert "hosting_provider" in _cfg_cache
