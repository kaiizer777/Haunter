"""
Tests for LLM dynamic model config resolution (backend/app/llm/config.py).

Covers:
1. DB empty and no per-repo override -> returns environment defaults.
2. Multiple is_active=True rows in DB -> picks latest by created_at.desc().
3. Global active config resolution from DB.
4. DB empty -> environment defaults.
6. DB session failure -> catches exception, logs warning, and falls back to environment defaults.
7. get_active_model_config(db=None) creates session via async_session_maker.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.llm.config import ResolvedModelConfig, get_active_model_config
from app.models import ModelConfig, Repo
from tests.conftest import truncate_all


@pytest.mark.asyncio
async def test_get_active_model_config_empty_db_env_fallback(db: AsyncSession) -> None:
    await truncate_all(db)

    resolved = await get_active_model_config(db=db)

    assert isinstance(resolved, ResolvedModelConfig)
    assert resolved.provider == settings.default_provider
    assert resolved.model_name == settings.default_model
    assert resolved.base_url == settings.opencode_go_base_url


@pytest.mark.asyncio
async def test_get_active_model_config_multiple_active_picks_latest(
    db: AsyncSession,
) -> None:
    await truncate_all(db)

    older_cfg = ModelConfig(
        provider="opencode_zen",
        model_name="older-model-free",
        base_url="https://opencode.ai/zen/v1",
        is_active=True,
        created_at=datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
    )
    newer_cfg = ModelConfig(
        provider="opencode_zen",
        model_name="newer-model-free",
        base_url="https://opencode.ai/zen/v1",
        is_active=True,
        created_at=datetime(2025, 1, 2, 12, 0, 0, tzinfo=timezone.utc),
    )
    db.add_all([older_cfg, newer_cfg])
    await db.commit()

    resolved = await get_active_model_config(db=db)

    assert resolved.model_name == "newer-model-free"


@pytest.mark.asyncio
async def test_get_active_model_config_db_exception_fallback() -> None:
    failing_db = AsyncMock(spec=AsyncSession)
    failing_db.execute.side_effect = RuntimeError("Database connection died")

    resolved = await get_active_model_config(db=failing_db)

    assert resolved.provider == settings.default_provider
    assert resolved.model_name == settings.default_model
    assert resolved.base_url == settings.opencode_go_base_url


@pytest.mark.asyncio
async def test_get_active_model_config_default_session_context() -> None:
    mock_session = AsyncMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = mock_result

    class MockSessionCtx:
        async def __aenter__(self):
            return mock_session

        async def __aexit__(self, *args):
            pass

    with patch("app.llm.config.async_session_maker", return_value=MockSessionCtx()):
        resolved = await get_active_model_config(db=None)

    assert resolved.provider == settings.default_provider
    assert resolved.model_name == settings.default_model
