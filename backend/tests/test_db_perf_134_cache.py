"""db-perf #134 — follow-up hardening for the short-TTL config caches (#109).

Covers the 4 defects from issue #134 with hermetic (mocked-DB) tests:
  1. cached repo settings are session-independent snapshots (no
     MissingGreenlet / DetachedInstanceError after rollback + close);
  2. an in-flight read never restores stale data after an invalidation
     (generation guard on cache write-back);
  3. invalidation happens right after commit, before refresh;
  4. both caches are bounded (expired entries pruned, size capped).
"""

from __future__ import annotations

import time
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.orm import object_session

import app.services.repo_settings as rs_mod
from app.llm import config as mc_mod
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

REPO_MAXSIZE = getattr(rs_mod, "_REPO_SETTINGS_MAXSIZE", 64)
MODEL_MAXSIZE = getattr(mc_mod, "_MODEL_CONFIG_MAXSIZE", 64)


@pytest.fixture(autouse=True)
def _clear_caches():
    invalidate_model_config_cache()
    invalidate_repo_settings_cache()
    yield
    invalidate_model_config_cache()
    invalidate_repo_settings_cache()


def _settings_row(repo_id, **overrides):
    from app.models import RepoSettings

    kwargs = {
        "id": uuid.uuid4(),
        "repo_id": repo_id,
        "preset": "autonomous",
        "settings_version": 1,
    }
    kwargs.update(overrides)
    return RepoSettings(**kwargs)


# ---------------------------------------------------------------------------
# #134-1: cached rows are session-independent snapshots
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repo_settings_cache_hit_is_detached_snapshot():
    """A cache hit must not hand out the session-bound ORM instance.

    Old behavior cached the live row, so a later ``rollback()`` (duplicate
    webhook delivery) expired its attributes and the next cache hit raised
    ``MissingGreenlet``/``DetachedInstanceError``. The cached copy must be
    usable with no session attached and serializable via the API schema.
    """
    from app.schemas import RepoSettingsOut
    from app.services.repo_settings import create_default_repo_settings

    repo_id = uuid.uuid4()
    # A fully populated row, like the DB returns in production (every
    # NOT NULL column set), so schema serialization is also covered.
    row = create_default_repo_settings(repo_id)
    row.allowed_branches = ["main"]
    mock_db = AsyncMock()
    mock_db.scalar.return_value = row

    # The initial miss returns the live row for this caller's own session.
    first = await get_repo_settings(mock_db, repo_id, auto_create=False)
    assert mock_db.scalar.call_count == 1
    assert first is row

    # A later hit must serve a detached snapshot — never the cached
    # session-bound instance — surviving rollback + session close.
    mock_db.rollback = AsyncMock()
    await mock_db.rollback()
    second = await get_repo_settings(mock_db, repo_id, auto_create=False)
    assert mock_db.scalar.call_count == 1
    assert second is not row
    assert second is not first
    assert object_session(second) is None
    assert second.preset == "autonomous"
    assert second.settings_version == 1
    assert second.allowed_branches == ["main"]
    RepoSettingsOut.model_validate(second)


@pytest.mark.asyncio
async def test_repo_settings_caller_mutation_does_not_poison_cache():
    """Mutating a returned snapshot must not corrupt the cached entry."""
    repo_id = uuid.uuid4()
    row = _settings_row(repo_id, allowed_branches=["main"])
    mock_db = AsyncMock()
    mock_db.scalar.return_value = row

    first = await get_repo_settings(mock_db, repo_id, auto_create=False)
    first.preset = "audit_only"
    first.allowed_branches.append("evil")

    second = await get_repo_settings(mock_db, repo_id, auto_create=False)
    assert mock_db.scalar.call_count == 1
    assert second.preset == "autonomous"
    assert second.allowed_branches == ["main"]


# ---------------------------------------------------------------------------
# #134-2: in-flight reads never restore stale data after invalidation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repo_settings_stale_inflight_read_not_cached():
    """A read that started before a write must not repopulate the cache."""
    repo_id = uuid.uuid4()
    stale_row = _settings_row(repo_id, preset="autonomous")

    async def _slow_scalar(*args, **kwargs):
        # A concurrent write commits + invalidates while this read is in flight.
        invalidate_repo_settings_cache(repo_id)
        return stale_row

    mock_db = AsyncMock()
    mock_db.scalar.side_effect = _slow_scalar

    result = await get_repo_settings(mock_db, repo_id, auto_create=False)
    assert result.preset == "autonomous"
    # The stale result was served to this caller but must NOT be cached:
    # the invalidation that landed mid-read stays effective.
    assert repo_id not in _REPO_SETTINGS_CACHE


@pytest.mark.asyncio
async def test_repo_settings_sequential_read_still_cached():
    """Without a concurrent invalidation the read path still populates."""
    repo_id = uuid.uuid4()
    mock_db = AsyncMock()
    mock_db.scalar.return_value = _settings_row(repo_id)

    await get_repo_settings(mock_db, repo_id, auto_create=False)
    await get_repo_settings(mock_db, repo_id, auto_create=False)
    assert mock_db.scalar.call_count == 1


@pytest.mark.asyncio
async def test_model_config_stale_inflight_read_not_cached():
    """Same generation-guard guarantee for the model-config cache."""
    from datetime import datetime, timezone

    from app.models import ModelConfig

    cfg = ModelConfig(
        provider="opencode_zen",
        model_name="stale-model",
        base_url="https://opencode.ai/zen/v1",
        is_active=True,
        scope="global",
        created_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = cfg
    mock_session = AsyncMock()

    async def _slow_execute(*args, **kwargs):
        invalidate_model_config_cache()
        return mock_result

    mock_session.execute.side_effect = _slow_execute

    resolved = await get_active_model_config(db=mock_session)
    assert resolved.model_name == "stale-model"
    assert len(_model_config_cache) == 0


# ---------------------------------------------------------------------------
# #134-3: invalidate immediately after commit, before refresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_repo_settings_invalidates_before_refresh():
    """Even if refresh() blows up, the post-commit invalidation stands."""
    repo_id = uuid.uuid4()
    _REPO_SETTINGS_CACHE[repo_id] = (_settings_row(repo_id), time.monotonic())

    live = _settings_row(repo_id, enable_auto_fix=True)
    mock_db = AsyncMock()
    mock_db.scalar.return_value = live
    mock_db.refresh.side_effect = RuntimeError("refresh failed")

    with pytest.raises(RuntimeError, match="refresh failed"):
        await update_repo_settings(mock_db, repo_id, {"max_cost_per_run_cents": 250})

    mock_db.commit.assert_awaited_once()
    assert repo_id not in _REPO_SETTINGS_CACHE


@pytest.mark.asyncio
async def test_post_preset_invalidates_before_refresh(monkeypatch):
    """Preset handler: commit ok + refresh boom => cache already dropped."""
    from app.routers import settings as settings_router

    repo_id = uuid.uuid4()
    _REPO_SETTINGS_CACHE[repo_id] = (_settings_row(repo_id), time.monotonic())

    attached = _settings_row(repo_id)

    async def _fake_get_settings(db, rid, *, auto_create=False):
        assert rid == repo_id
        return attached

    async def _fake_repo_or_404(db, rid, user_id):
        return MagicMock(id=rid)

    monkeypatch.setattr(settings_router, "get_repo_settings", _fake_get_settings)
    monkeypatch.setattr(settings_router, "_get_user_repo_or_404", _fake_repo_or_404)

    mock_db = AsyncMock()
    mock_db.refresh.side_effect = RuntimeError("refresh failed")
    body = MagicMock()
    body.preset = "conservative"

    with pytest.raises(RuntimeError, match="refresh failed"):
        await settings_router.post_preset(
            repo_id, body, MagicMock(id=uuid.uuid4()), mock_db
        )

    mock_db.commit.assert_awaited_once()
    assert repo_id not in _REPO_SETTINGS_CACHE


@pytest.mark.asyncio
async def test_post_preset_path_invalidates_before_refresh(monkeypatch):
    """Path-param preset handler: commit ok + refresh boom => dropped."""
    from app.routers import settings as settings_router

    repo_id = uuid.uuid4()
    _REPO_SETTINGS_CACHE[repo_id] = (_settings_row(repo_id), time.monotonic())

    attached = _settings_row(repo_id)

    async def _fake_get_settings(db, rid, *, auto_create=False):
        assert rid == repo_id
        return attached

    async def _fake_repo_or_404(db, rid, user_id):
        return MagicMock(id=rid)

    monkeypatch.setattr(settings_router, "get_repo_settings", _fake_get_settings)
    monkeypatch.setattr(settings_router, "_get_user_repo_or_404", _fake_repo_or_404)

    mock_db = AsyncMock()
    mock_db.refresh.side_effect = RuntimeError("refresh failed")

    with pytest.raises(RuntimeError, match="refresh failed"):
        await settings_router.post_preset_path(
            repo_id, "conservative", MagicMock(id=uuid.uuid4()), mock_db
        )

    mock_db.commit.assert_awaited_once()
    assert repo_id not in _REPO_SETTINGS_CACHE


@pytest.mark.asyncio
async def test_post_preset_commit_failure_keeps_valid_cache(monkeypatch):
    """Preset handler: commit failure must not poison the still-valid cache."""
    from app.routers import settings as settings_router

    repo_id = uuid.uuid4()
    cached = _settings_row(repo_id, preset="autonomous")
    _REPO_SETTINGS_CACHE[repo_id] = (cached, time.monotonic())

    attached = _settings_row(repo_id)

    async def _fake_get_settings(db, rid, *, auto_create=False):
        return attached

    async def _fake_repo_or_404(db, rid, user_id):
        return MagicMock(id=rid)

    monkeypatch.setattr(settings_router, "get_repo_settings", _fake_get_settings)
    monkeypatch.setattr(settings_router, "_get_user_repo_or_404", _fake_repo_or_404)

    mock_db = AsyncMock()
    mock_db.commit.side_effect = RuntimeError("commit failed")
    body = MagicMock()
    body.preset = "conservative"

    with pytest.raises(RuntimeError, match="commit failed"):
        await settings_router.post_preset(
            repo_id, body, MagicMock(id=uuid.uuid4()), mock_db
        )

    # Commit failed => DB unchanged => the cached snapshot is still valid.
    assert repo_id in _REPO_SETTINGS_CACHE


# ---------------------------------------------------------------------------
# #134-4: bounded caches with expiry pruning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repo_settings_cache_is_bounded():
    """Distinct-repo reads must not grow the cache without limit."""
    mock_db = AsyncMock()
    created = []

    async def _scalar(*args, **kwargs):
        row = _settings_row(uuid.uuid4())
        created.append(row)
        return row

    mock_db.scalar.side_effect = _scalar

    for _ in range(REPO_MAXSIZE + 10):
        await get_repo_settings(mock_db, uuid.uuid4(), auto_create=False)

    assert len(_REPO_SETTINGS_CACHE) <= REPO_MAXSIZE


@pytest.mark.asyncio
async def test_repo_settings_expired_entries_pruned_on_store():
    """Expired entries are evicted (not just skipped) on the next store."""
    repo_id = uuid.uuid4()
    stale_key = uuid.uuid4()
    _REPO_SETTINGS_CACHE[stale_key] = (
        _settings_row(stale_key),
        time.monotonic() - 3600.0,
    )

    mock_db = AsyncMock()
    mock_db.scalar.return_value = _settings_row(repo_id)
    await get_repo_settings(mock_db, repo_id, auto_create=False)

    assert stale_key not in _REPO_SETTINGS_CACHE
    assert repo_id in _REPO_SETTINGS_CACHE


@pytest.mark.asyncio
async def test_model_config_cache_is_bounded():
    """Distinct-key resolves must not grow the model-config cache unbounded."""
    from datetime import datetime, timezone

    from app.models import ModelConfig

    cfg = ModelConfig(
        provider="opencode_zen",
        model_name="bounded-model",
        base_url="https://opencode.ai/zen/v1",
        is_active=True,
        scope="global",
        created_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = cfg
    mock_session = AsyncMock()
    mock_session.execute.return_value = mock_result

    for _ in range(MODEL_MAXSIZE + 10):
        await get_active_model_config(db=mock_session, repo_id=uuid.uuid4())

    assert len(_model_config_cache) <= MODEL_MAXSIZE



@pytest.mark.asyncio
async def test_model_config_expired_entries_pruned_on_store():
    """Expired model-config entries are evicted (not just skipped) on store."""
    from datetime import datetime, timezone

    from app.models import ModelConfig

    stale_key = "repo:00000000-0000-0000-0000-000000000000"
    _model_config_cache[stale_key] = (
        mc_mod.ResolvedModelConfig(
            provider="opencode_zen",
            model_name="stale-model",
            base_url="https://opencode.ai/zen/v1",
        ),
        time.monotonic() - 3600.0,
    )

    cfg = ModelConfig(
        provider="opencode_zen",
        model_name="bounded-model",
        base_url="https://opencode.ai/zen/v1",
        is_active=True,
        scope="global",
        created_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = cfg
    mock_session = AsyncMock()
    mock_session.execute.return_value = mock_result

    resolved = await get_active_model_config(db=mock_session)
    assert resolved.model_name == "bounded-model"
    assert stale_key not in _model_config_cache
    assert "global" in _model_config_cache


@pytest.mark.asyncio
async def test_update_model_config_repo_invalidates_before_refresh():
    """Repo PUT: commit ok + refresh boom => cache already dropped."""
    from app.routers import model_config as mc_router

    repo_id = uuid.uuid4()
    cache_key = f"repo:{repo_id}"
    _model_config_cache[cache_key] = (
        mc_mod.ResolvedModelConfig(
            provider="opencode_zen",
            model_name="old-model",
            base_url="https://opencode.ai/zen/v1",
        ),
        time.monotonic(),
    )

    repo = MagicMock()
    repo.id = repo_id
    repo.active_model_config_id = None
    repo_result = MagicMock()
    repo_result.scalar_one_or_none.return_value = repo

    added = []
    mock_db = AsyncMock()
    mock_db.execute.return_value = repo_result
    # db.add is sync in production: keep it a plain mock so no
    # coroutine goes unawaited (same pattern as the #109 auto-create test).
    mock_db.add = MagicMock(side_effect=lambda obj: added.append(obj))

    async def _flush():
        for obj in added:
            obj.id = uuid.uuid4()

    mock_db.flush.side_effect = _flush
    mock_db.refresh.side_effect = RuntimeError("refresh failed")

    body = MagicMock(
        provider="opencode_zen", model_name="probe-free", repo_id=repo_id
    )

    with pytest.raises(RuntimeError, match="refresh failed"):
        await mc_router.update_model_config_endpoint(
            body, MagicMock(id=uuid.uuid4()), mock_db
        )

    mock_db.commit.assert_awaited_once()
    assert cache_key not in _model_config_cache


@pytest.mark.asyncio
async def test_update_model_config_global_invalidates_before_refresh():
    """Global PUT: commit ok + refresh boom => cache already dropped."""
    from app.routers import model_config as mc_router

    _model_config_cache["global"] = (
        mc_mod.ResolvedModelConfig(
            provider="opencode_zen",
            model_name="old-model",
            base_url="https://opencode.ai/zen/v1",
        ),
        time.monotonic(),
    )

    added = []
    mock_db = AsyncMock()
    # db.add is sync in production: keep it a plain mock so no
    # coroutine goes unawaited (same pattern as the #109 auto-create test).
    mock_db.add = MagicMock(side_effect=lambda obj: added.append(obj))

    async def _flush():
        for obj in added:
            obj.id = uuid.uuid4()

    mock_db.flush.side_effect = _flush
    mock_db.refresh.side_effect = RuntimeError("refresh failed")

    admin = MagicMock(id=uuid.uuid4(), is_admin=True)
    body = MagicMock(provider="opencode_zen", model_name="probe-free", repo_id=None)

    with pytest.raises(RuntimeError, match="refresh failed"):
        await mc_router.update_model_config_endpoint(body, admin, mock_db)

    mock_db.commit.assert_awaited_once()
    assert "global" not in _model_config_cache
