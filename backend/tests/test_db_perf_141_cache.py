"""provider-config cache #141 — mid-read invalidation must not be undone.

Ports the #134-2 generation-guard guarantee to the hosting provider-config
cache (``_cfg_cache``): ``_get_provider_config`` captures the invalidation
generation before the DB await and skips the write-back when it changed, so
an ``invalidate_provider_cache()`` landing mid-read (e.g. PUT
/config/hosting committing a new value) stays effective instead of being
silently undone by the stale in-flight read repopulating the cache for a
fresh 60s TTL.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.adapters.hosting import (
    _cfg_cache,
    _get_provider_config,
    invalidate_provider_cache,
)


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
# #141: in-flight reads never restore stale data after invalidation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_config_stale_inflight_read_not_cached():
    """A read racing a mid-flight invalidation must not repopulate the cache."""
    invalidate_provider_cache()
    mock_row = MagicMock()
    mock_row.value = "aws"
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_row
    mock_session = AsyncMock()

    async def _slow_execute(*args, **kwargs):
        # PUT /config/hosting commits a new value and invalidates while
        # this read's DB round-trip is in flight.
        invalidate_provider_cache()
        return mock_result

    mock_session.execute.side_effect = _slow_execute

    with patch(
        "app.db.async_session_maker",
        return_value=_mock_session_ctx(mock_session),
    ):
        result = await _get_provider_config("hosting_provider", "aws")

    # The stale value is still served to this caller...
    assert result == "aws"
    # ...but never written back: the mid-read invalidation stays effective.
    assert "hosting_provider" not in _cfg_cache


@pytest.mark.asyncio
async def test_provider_config_sequential_read_still_cached():
    """Without a concurrent invalidation the read path still populates."""
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
        await _get_provider_config("hosting_provider", "aws")
        await _get_provider_config("hosting_provider", "aws")

    assert mock_session.execute.call_count == 1
    assert "hosting_provider" in _cfg_cache
