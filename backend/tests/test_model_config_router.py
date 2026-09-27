"""
Tests for model configuration router (backend/app/routers/model_config.py).

Covers:
- GET /config/model/available returns all three provider buckets:
  - opencode_zen (free tier models)
  - openai (approved production models)
  - anthropic (approved production models)
  - HTTP calls to discovery endpoint mocked via respx.
- PUT /config/model as admin:
  - Creates a new ModelConfig row with is_active=True.
  - Deactivates previous active global ModelConfig rows (single-active invariant).
- PUT /config/model is open to any authenticated user (Phase 3.2 Issue 2: no 403).
- PUT /config/model with invalid provider (e.g. "gcp") returns 422.
- PUT /config/model with invalid model (e.g. provider="openai", model="invalid") returns 422.
- Unauthenticated requests return 401.
- GET /config/model returns the currently active global config or env default.
- Per-repo model config endpoints (GET/PUT /config/model/{repo_id}) enforce tenant ownership (404 on unowned repo).
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.llm.discovery import clear_model_cache
from app.models import ModelConfig, Repo, User
from tests.conftest import truncate_all


@pytest.fixture(autouse=True)
def _reset_discovery():
    clear_model_cache()
    yield
    clear_model_cache()


# ---------------------------------------------------------------------------
# GET /config/model/available
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_available_models_unauthenticated(client: httpx.AsyncClient):
    """GET /config/model/available without session cookie returns 401."""
    resp = await client.get("/config/model/available")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Not authenticated"}


@pytest.mark.asyncio
async def test_get_available_models_returns_all_three_buckets(
    db: AsyncSession, user_factory, make_auth_client
):
    """
    GET /config/model/available returns opencode_zen, openai, and anthropic buckets.
    Network calls to OpenCode Zen models endpoint are mocked with respx.
    """
    await truncate_all(db)
    user = await user_factory()
    client = make_auth_client(user.id)

    zen_discovery_payload = {
        "data": [
            {"id": "nemotron-3.5-lightning-free"},
            {"id": "deepseek-r1-distill-free"},
            {"id": "gpt-4o-paid"},  # non-free model should be filtered out
        ]
    }

    with respx.mock(base_url="https://opencode.ai/zen/v1") as rx:
        rx.get("/models").mock(
            return_value=httpx.Response(200, json=zen_discovery_payload)
        )
        async with client:
            resp = await client.get("/config/model/available")

    assert resp.status_code == 200
    data = resp.json()

    # Verify all three buckets exist and are non-empty
    assert "opencode_zen" in data
    assert "openai" in data
    assert "anthropic" in data

    zen_ids = [m["id"] for m in data["opencode_zen"]]
    assert "nemotron-3.5-lightning-free" in zen_ids
    assert "gpt-4o-paid" not in zen_ids

    openai_ids = [m["id"] for m in data["openai"]]
    assert "gpt-4o" in openai_ids
    assert "gpt-4o-mini" in openai_ids

    anthropic_ids = [m["id"] for m in data["anthropic"]]
    assert "claude-sonnet-4-5" in anthropic_ids
    assert "claude-haiku-3-5" in anthropic_ids

    # Verify context_window presence and correctness
    gpt4o_item = next(m for m in data["openai"] if m["id"] == "gpt-4o")
    assert gpt4o_item["context_window"] == 128000

    claude_item = next(m for m in data["anthropic"] if m["id"] == "claude-sonnet-4-5")
    assert claude_item["context_window"] == 200000

    zen_item = next(m for m in data["opencode_zen"] if m["id"] == "nemotron-3.5-lightning-free")
    assert zen_item["context_window"] == 131072



# ---------------------------------------------------------------------------
# PUT /config/model (Global Model Config Switcher)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_model_config_unauthenticated(client: httpx.AsyncClient):
    """PUT /config/model without session cookie returns 401."""
    payload = {"provider": "opencode_zen", "model_name": "nemotron-3.5-lightning-free"}
    resp = await client.put("/config/model", json=payload)
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Not authenticated"}


@pytest.mark.asyncio
async def test_put_model_config_open_access_allows_any_authenticated_user(
    db: AsyncSession, user_factory, make_auth_client, monkeypatch: pytest.MonkeyPatch
):
    """PUT /config/model is open to any authenticated user (future.md §3.2 Issue 2).

    Regression: the old admin gate returned 403 for normal users. Any
    authenticated user can now switch the global model freely.
    """
    await truncate_all(db)
    user = await user_factory()
    client = make_auth_client(user.id)

    # Even with ADMIN_USER_ID pinned to somebody else, a normal user succeeds.
    configured_admin_id = str(uuid.uuid4())
    monkeypatch.setattr(settings, "admin_user_id", configured_admin_id)

    payload = {"provider": "opencode_zen", "model_name": "nemotron-3.5-lightning-free"}
    async with client:
        resp = await client.put("/config/model", json=payload)

    assert resp.status_code == 200
    data = resp.json()
    assert data["provider"] == "opencode_zen"
    assert data["model_name"] == "nemotron-3.5-lightning-free"
    assert data["is_active"] is True


@pytest.mark.asyncio
async def test_put_model_config_invalid_provider_returns_422(
    db: AsyncSession, user_factory, make_auth_client, monkeypatch: pytest.MonkeyPatch
):
    """PUT /config/model with invalid provider (e.g. 'gcp') returns 422."""
    await truncate_all(db)
    admin_user = await user_factory()
    client = make_auth_client(admin_user.id)

    monkeypatch.setattr(settings, "admin_user_id", str(admin_user.id))

    payload = {"provider": "gcp", "model_name": "gemini-pro"}
    async with client:
        resp = await client.put("/config/model", json=payload)

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_put_model_config_invalid_model_returns_422(
    db: AsyncSession, user_factory, make_auth_client, monkeypatch: pytest.MonkeyPatch
):
    """
    PUT /config/model with invalid model for provider returns 422:
    - opencode_zen must end with '-free'
    - openai must be in OPENAI_MODELS
    - anthropic must be in ANTHROPIC_MODELS
    """
    await truncate_all(db)
    admin_user = await user_factory()
    client = make_auth_client(admin_user.id)

    monkeypatch.setattr(settings, "admin_user_id", str(admin_user.id))

    async with client:
        # opencode_zen without -free
        r1 = await client.put(
            "/config/model",
            json={"provider": "opencode_zen", "model_name": "nemotron-paid"},
        )
        assert r1.status_code == 422

        # openai invalid model
        r2 = await client.put(
            "/config/model",
            json={"provider": "openai", "model_name": "gpt-3.5-turbo"},
        )
        assert r2.status_code == 422

        # anthropic invalid model
        r3 = await client.put(
            "/config/model",
            json={"provider": "anthropic", "model_name": "claude-2.0"},
        )
        assert r3.status_code == 422


@pytest.mark.asyncio
async def test_put_model_config_active_row_invariant(
    db: AsyncSession, user_factory, make_auth_client, monkeypatch: pytest.MonkeyPatch
):
    """
    PUT /config/model as admin:
    1. Creates a ModelConfig row with is_active=True.
    2. Deactivates existing active rows (single active-row invariant).
    """
    await truncate_all(db)
    admin_user = await user_factory()
    client = make_auth_client(admin_user.id)

    monkeypatch.setattr(settings, "admin_user_id", str(admin_user.id))

    # Seed an existing active model config
    initial_config = ModelConfig(
        provider="opencode_zen",
        model_name="nemotron-3.5-lightning-free",
        base_url="https://opencode.ai/zen/v1",
        is_active=True,
    )
    db.add(initial_config)
    await db.commit()
    initial_id = initial_config.id

    # Switch to openai/gpt-4o
    payload = {"provider": "openai", "model_name": "gpt-4o"}
    async with client:
        resp = await client.put("/config/model", json=payload)

    assert resp.status_code == 200
    data = resp.json()
    assert data["provider"] == "openai"
    assert data["model_name"] == "gpt-4o"
    assert data["is_active"] is True
    new_id = data["id"]

    # Verify active-row invariant in DB
    db.expire_all()
    result = await db.execute(select(ModelConfig))
    all_configs = result.scalars().all()
    assert len(all_configs) == 2

    config_map = {str(c.id): c for c in all_configs}
    assert config_map[str(initial_id)].is_active is False
    assert config_map[str(new_id)].is_active is True

    # Active count must be exactly 1
    active_count = sum(1 for c in all_configs if c.is_active)
    assert active_count == 1


# ---------------------------------------------------------------------------
# GET /config/model
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_model_config_global_fallback_and_active(
    db: AsyncSession, user_factory, make_auth_client
):
    """
    GET /config/model returns fallback defaults when no DB row exists,
    and returns active DB row once populated.
    """
    await truncate_all(db)
    user = await user_factory()
    client = make_auth_client(user.id)

    async with client:
        # Fallback to env default
        resp = await client.get("/config/model")
        assert resp.status_code == 200
        data = resp.json()
        assert data["provider"] == settings.default_provider
        assert data["model_name"] == settings.default_model

        # Insert active row in DB
        mc = ModelConfig(
            provider="anthropic",
            model_name="claude-sonnet-4-5",
            base_url="https://api.anthropic.com/v1",
            is_active=True,
        )
        db.add(mc)
        await db.commit()

        # Should now return the active DB row
        resp2 = await client.get("/config/model")
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["provider"] == "anthropic"
        assert data2["model_name"] == "claude-sonnet-4-5"
        assert data2["id"] == str(mc.id)


# ---------------------------------------------------------------------------
# Per-repo model config endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repo_model_config_ownership_enforced(
    db: AsyncSession, user_factory, make_auth_client
):
    """
    GET and PUT /config/model/{repo_id} return 404 if the repo does not belong
    to the current authenticated user (prevents existence oracle leakage).
    """
    await truncate_all(db)
    user_a = await user_factory(github_id=920, username="user_a")
    user_b = await user_factory(github_id=921, username="user_b")

    # Repo owned by user_a
    repo_a = Repo(
        user_id=user_a.id,
        owner="owner-a",
        name="repo-a",
        default_branch="main",
    )
    db.add(repo_a)
    await db.commit()

    # User B tries to read and update user A's repo config
    client_b = make_auth_client(user_b.id)
    async with client_b:
        get_resp = await client_b.get(f"/config/model/{repo_a.id}")
        assert get_resp.status_code == 404
        assert get_resp.json() == {"detail": "Repo not found"}

        put_resp = await client_b.put(
            f"/config/model/{repo_a.id}",
            json={"provider": "openai", "model_name": "gpt-4o"},
        )
        assert put_resp.status_code == 404
        assert put_resp.json() == {"detail": "Repo not found"}


@pytest.mark.asyncio
async def test_put_model_config_persisted_role_succeeds(
    db: AsyncSession, user_factory, make_auth_client
):
    """PUT /config/model succeeds when user has role='admin' in DB without needing admin_user_id env."""
    await truncate_all(db)
    admin_user = await user_factory(role="admin")
    client = make_auth_client(admin_user.id)

    payload = {"provider": "opencode_zen", "model_name": "nemotron-3.5-lightning-free"}
    async with client:
        resp = await client.put("/config/model", json=payload)

    assert resp.status_code == 200
    assert resp.json()["model_name"] == "nemotron-3.5-lightning-free"
    assert resp.json()["is_active"] is True


# ---------------------------------------------------------------------------
# Phase 3.1 — Per-Repo vs Global isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_global_preserves_repo_overrides(
    db: AsyncSession, user_factory, make_auth_client, monkeypatch: pytest.MonkeyPatch
):
    """
    PUT /config/model (global) must deactivate ONLY scope='global' rows.

    Regression for future.md §3.2 Issue 1: the old
    UPDATE ... WHERE is_active=true wiped repo-specific active configs.
    """
    await truncate_all(db)
    admin_user = await user_factory(role="admin")
    client = make_auth_client(admin_user.id)
    monkeypatch.setattr(settings, "admin_user_id", str(admin_user.id))

    # Seed one active global config.
    global_cfg = ModelConfig(
        provider="opencode_zen",
        model_name="nemotron-3.5-lightning-free",
        base_url="https://opencode.ai/zen/v1",
        is_active=True,
        scope="global",
    )
    db.add(global_cfg)
    await db.commit()

    # Seed one repo with its own active repo-scoped override.
    repo = Repo(
        user_id=admin_user.id,
        owner="acme",
        name="service",
    )
    db.add(repo)
    await db.commit()
    await db.refresh(repo)
    repo_id = repo.id

    repo_cfg = ModelConfig(
        provider="openai",
        model_name="gpt-4o",
        base_url="https://api.openai.com/v1",
        is_active=True,
        scope="repo",
        repo_id=repo_id,
        user_id=admin_user.id,
    )
    db.add(repo_cfg)
    await db.commit()
    await db.refresh(repo_cfg)
    repo.active_model_config_id = repo_cfg.id
    await db.commit()

    # Global switch — must not touch the repo override.
    async with client:
        resp = await client.put(
            "/config/model", json={"provider": "openai", "model_name": "gpt-4o-mini"}
        )

    assert resp.status_code == 200
    assert resp.json()["scope"] == "global"

    db.expire_all()
    result = await db.execute(select(ModelConfig))
    rows = {str(c.id): c for c in result.scalars().all()}

    # Old global deactivated, new global active.
    assert rows[str(global_cfg.id)].is_active is False
    assert rows[str(global_cfg.id)].scope == "global"
    assert rows[resp.json()["id"]].is_active is True
    assert rows[resp.json()["id"]].scope == "global"

    # Repo override untouched.
    assert rows[str(repo_cfg.id)].is_active is True
    assert rows[str(repo_cfg.id)].scope == "repo"
    assert rows[str(repo_cfg.id)].model_name == "gpt-4o"

    # Repo GET still resolves the override, global GET resolves the new default.
    client2 = make_auth_client(admin_user.id)
    async with client2:
        repo_resp = await client2.get(f"/config/model?repo_id={repo_id}")
        assert repo_resp.status_code == 200
        assert repo_resp.json()["model_name"] == "gpt-4o"
        assert repo_resp.json()["scope"] == "repo"

        global_resp = await client2.get("/config/model")
        assert global_resp.status_code == 200
        assert global_resp.json()["model_name"] == "gpt-4o-mini"
        assert global_resp.json()["scope"] == "global"


@pytest.mark.asyncio
async def test_put_repo_stamps_repo_scope(
    db: AsyncSession, user_factory, make_auth_client
):
    """PUT /config/model/{repo_id} stamps scope='repo' + repo_id/user_id pins."""
    await truncate_all(db)
    user = await user_factory()
    repo = Repo(user_id=user.id, owner="acme", name="api")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)

    client = make_auth_client(user.id)
    async with client:
        resp = await client.put(
            f"/config/model/{repo.id}",
            json={"provider": "anthropic", "model_name": "claude-sonnet-4-5"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["scope"] == "repo"
    assert data["repo_id"] == str(repo.id)
    assert data["user_id"] == str(user.id)

    db.expire_all()
    row = (
        await db.execute(select(ModelConfig).where(ModelConfig.id == data["id"]))
    ).scalar_one()
    assert row.scope == "repo"
    assert row.repo_id == repo.id
    assert row.is_active is True
