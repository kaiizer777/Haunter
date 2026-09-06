"""
Tests for LLM dynamic model config resolution (backend/app/llm/config.py).

Covers:
1. DB empty and no per-repo override -> returns environment defaults.
2. Multiple is_active=True rows in DB -> picks latest by created_at.desc().
3. Per-repo override present and active -> repo config overrides global config.
4. Per-repo override inactive (is_active=False) -> falls through to global active config.
5. Repo ID not found or repo has active_model_config_id=None -> falls through to global active.
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
    assert resolved.base_url == settings.opencode_zen_base_url


@pytest.mark.asyncio
async def test_get_active_model_config_multiple_active_picks_latest(db: AsyncSession) -> None:
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
async def test_get_active_model_config_repo_override_wins(
    db: AsyncSession,
    user_factory,
) -> None:
    await truncate_all(db)

    global_cfg = ModelConfig(
        provider="opencode_zen",
        model_name="global-active-free",
        base_url="https://global.endpoint/v1",
        is_active=True,
    )
    repo_cfg = ModelConfig(
        provider="openai",
        model_name="gpt-4o",
        base_url="https://api.openai.com/v1",
        is_active=True,
    )
    db.add_all([global_cfg, repo_cfg])
    await db.commit()

    user = await user_factory(github_id=9871, username="repo_cfg_user")
    repo = Repo(
        user_id=user.id,
        owner="test-owner",
        name="override-repo",
        active_model_config_id=repo_cfg.id,
    )
    db.add(repo)
    await db.commit()

    # When repo_id is provided, repo override takes precedence
    resolved_repo = await get_active_model_config(db=db, repo_id=repo.id)
    assert resolved_repo.provider == "openai"
    assert resolved_repo.model_name == "gpt-4o"
    assert resolved_repo.base_url == "https://api.openai.com/v1"

    # When repo_id is None, global active config is used
    resolved_global = await get_active_model_config(db=db, repo_id=None)
    assert resolved_global.model_name == "global-active-free"


@pytest.mark.asyncio
async def test_get_active_model_config_repo_override_inactive_fallthrough(
    db: AsyncSession,
    user_factory,
) -> None:
    await truncate_all(db)

    global_cfg = ModelConfig(
        provider="opencode_zen",
        model_name="global-active-free",
        base_url="https://global.endpoint/v1",
        is_active=True,
    )
    inactive_repo_cfg = ModelConfig(
        provider="anthropic",
        model_name="claude-sonnet-4-5",
        base_url="https://api.anthropic.com",
        is_active=False,  # Inactive override
    )
    db.add_all([global_cfg, inactive_repo_cfg])
    await db.commit()

    user = await user_factory(github_id=9872, username="inactive_cfg_user")
    repo = Repo(
        user_id=user.id,
        owner="test-owner",
        name="inactive-repo",
        active_model_config_id=inactive_repo_cfg.id,
    )
    db.add(repo)
    await db.commit()

    # Inactive repo config falls through to global active config
    resolved = await get_active_model_config(db=db, repo_id=repo.id)
    assert resolved.model_name == "global-active-free"


@pytest.mark.asyncio
async def test_get_active_model_config_repo_not_found_or_no_config(
    db: AsyncSession,
    user_factory,
) -> None:
    await truncate_all(db)

    global_cfg = ModelConfig(
        provider="opencode_zen",
        model_name="global-active-free",
        base_url="https://global.endpoint/v1",
        is_active=True,
    )
    db.add(global_cfg)
    await db.commit()

    user = await user_factory(github_id=9873, username="no_cfg_user")
    repo_no_cfg = Repo(
        user_id=user.id,
        owner="test-owner",
        name="no-cfg-repo",
        active_model_config_id=None,
    )
    db.add(repo_no_cfg)
    await db.commit()

    # Non-existent repo UUID falls through to global config
    random_repo_id = uuid.uuid4()
    resolved_random = await get_active_model_config(db=db, repo_id=random_repo_id)
    assert resolved_random.model_name == "global-active-free"

    # Repo with active_model_config_id=None falls through to global config
    resolved_none = await get_active_model_config(db=db, repo_id=repo_no_cfg.id)
    assert resolved_none.model_name == "global-active-free"


@pytest.mark.asyncio
async def test_get_active_model_config_db_exception_fallback() -> None:
    failing_db = AsyncMock(spec=AsyncSession)
    failing_db.execute.side_effect = RuntimeError("Database connection died")

    resolved = await get_active_model_config(db=failing_db)

    assert resolved.provider == settings.default_provider
    assert resolved.model_name == settings.default_model
    assert resolved.base_url == settings.opencode_zen_base_url


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
