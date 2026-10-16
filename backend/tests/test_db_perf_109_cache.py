"""db-perf #109 — short-TTL caches for repo_settings + model config, key-case fix.

Hermetic (fast suite): all DB access is mocked, no TEST_DATABASE_URL needed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.adapters.hosting import (
    _cfg_cache,
    _get_provider_config,
    invalidate_provider_cache,
)
from app.llm.config import (
    _model_config_cache,
    get_active_model_config,
    invalidate_model_config_cache,
)
from app.services.repo_settings import (
    _REPO_SETTINGS_CACHE,
    get_repo_settings,
    invalidate_repo_settings_cache,
    update_repo_settings,
)


@pytest.fixture(autouse=True)
def _clear_caches():
    invalidate_provider_cache()
    invalidate_model_config_cache()
    invalidate_repo_settings_cache()
    yield
    invalidate_provider_cache()
    invalidate_model_config_cache()
    invalidate_repo_settings_cache()


def _mock_session_ctx(mock_session):
    ctx = AsyncMock()
    ctx.__aenter__.return_value = mock_session
    ctx.__aexit__.return_value = None
    return ctx


# ---------------------------------------------------------------------------
# 1. hosting key-case split
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_config_key_case_normalized_single_select():
    """HOSTING_PROVIDER and hosting_provider share one cache entry / one SELECT."""
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
        first = await _get_provider_config("HOSTING_PROVIDER", "aws")
        second = await _get_provider_config("hosting_provider", "aws")

    assert first == "aws"
    assert second == "aws"
    assert mock_session.execute.call_count == 1
    assert "HOSTING_PROVIDER" not in _cfg_cache
    assert "hosting_provider" in _cfg_cache


# ---------------------------------------------------------------------------
# 2. repo_settings cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repo_settings_repeated_reads_single_select():
    from app.models import RepoSettings

    repo_id = uuid.uuid4()
    row = RepoSettings(
        id=uuid.uuid4(),
        repo_id=repo_id,
        preset="autonomous",
        settings_version=1,
    )
    mock_db = AsyncMock()
    mock_db.scalar.return_value = row

    first = await get_repo_settings(mock_db, repo_id, auto_create=False)
    second = await get_repo_settings(mock_db, repo_id, auto_create=False)

    # The initial miss returns the live row; the hit must serve a
    # detached snapshot copy, never the session-bound instance (#134-1:
    # caching the live row caused MissingGreenlet/DetachedInstanceError
    # after a later rollback()/close, so `second is row` would re-pin
    # the bug and is intentionally no longer asserted).
    assert first is row
    assert second is not row
    assert second.id == row.id
    assert second.repo_id == repo_id
    assert second.preset == row.preset
    assert mock_db.scalar.call_count == 1


@pytest.mark.asyncio
async def test_repo_settings_auto_create_bypasses_cache():
    """Write path always hits the DB for an attached instance."""
    from app.models import RepoSettings

    repo_id = uuid.uuid4()
    cached_row = RepoSettings(
        id=uuid.uuid4(), repo_id=repo_id, preset="autonomous", settings_version=1
    )
    _REPO_SETTINGS_CACHE[repo_id] = (cached_row, 999999999.0)

    fresh_row = RepoSettings(
        id=uuid.uuid4(), repo_id=repo_id, preset="standard", settings_version=2
    )
    mock_db = AsyncMock()
    mock_db.scalar.return_value = None
    mock_db.add = MagicMock()

    # auto_create=True with no DB row creates + returns a fresh attached row,
    # never the cached read copy.
    created = await get_repo_settings(mock_db, repo_id, auto_create=True)

    assert mock_db.scalar.call_count == 1
    assert mock_db.add.call_count == 1
    assert created is not cached_row


@pytest.mark.asyncio
async def test_repo_settings_update_invalidates_cache():
    from app.models import RepoSettings

    repo_id = uuid.uuid4()
    stale = RepoSettings(
        id=uuid.uuid4(), repo_id=repo_id, preset="autonomous", settings_version=1
    )
    _REPO_SETTINGS_CACHE[repo_id] = (stale, 999999999.0)

    live = RepoSettings(
        id=stale.id, repo_id=repo_id, preset="autonomous", settings_version=1,
        enable_auto_fix=True, allowed_branches=["main"],
    )
    mock_db = AsyncMock()
    mock_db.scalar.return_value = live

    updated = await update_repo_settings(
        mock_db, repo_id, {"max_cost_per_run_cents": 250}
    )
    assert updated.max_cost_per_run_cents == 250
    assert repo_id not in _REPO_SETTINGS_CACHE


# ---------------------------------------------------------------------------
# 3. model-config cache
# ---------------------------------------------------------------------------


def _mock_global_config_session(model_name="cached-model-free"):
    from app.models import ModelConfig

    cfg = ModelConfig(
        provider="opencode_zen",
        model_name=model_name,
        base_url="https://opencode.ai/zen/v1",
        is_active=True,
        scope="global",
        created_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = cfg
    mock_session = AsyncMock()
    mock_session.execute.return_value = mock_result
    return mock_session


@pytest.mark.asyncio
async def test_model_config_repeated_resolve_single_select():
    mock_session = _mock_global_config_session()

    first = await get_active_model_config(db=mock_session)
    second = await get_active_model_config(db=mock_session)

    assert first.model_name == "cached-model-free"
    assert second.model_name == "cached-model-free"
    assert mock_session.execute.call_count == 1


@pytest.mark.asyncio
async def test_model_config_invalidate_forces_reread():
    mock_session = _mock_global_config_session()

    await get_active_model_config(db=mock_session)
    assert mock_session.execute.call_count == 1

    invalidate_model_config_cache()

    await get_active_model_config(db=mock_session)
    assert mock_session.execute.call_count == 2


@pytest.mark.asyncio
async def test_model_config_fallback_not_cached():
    """Empty DB falls back to env defaults without caching the miss."""
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session = AsyncMock()
    mock_session.execute.return_value = mock_result

    await get_active_model_config(db=mock_session)
    await get_active_model_config(db=mock_session)

    assert mock_session.execute.call_count == 2
    assert len(_model_config_cache) == 0


@pytest.mark.asyncio
async def test_llm_complete_twice_single_config_select():
    """Acceptance: repeated complete() calls issue <=1 config SELECT."""
    from app.llm.client import LLMClient

    mock_session = _mock_global_config_session(model_name="seed-free")

    dummy_response = {
        "content": "ok",
        "tool_calls": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "latency_ms": 1,
        "model": "seed-free",
        "finish_reason": "stop",
    }

    with (
        patch(
            "app.llm.client.get_dynamic_free_models",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.llm.client.OpenCodeZenProvider.complete",
            new=AsyncMock(return_value=dict(dummy_response)),
        ),
    ):
        client = LLMClient(timeout=5.0)
        first = await client.complete(
            messages=[{"role": "user", "content": "hi"}], db=mock_session
        )
        second = await client.complete(
            messages=[{"role": "user", "content": "hi"}], db=mock_session
        )

    assert first["content"] == "ok"
    assert second["content"] == "ok"
    assert mock_session.execute.call_count == 1
