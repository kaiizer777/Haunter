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
- PUT /config/model as non-admin returns 403 Forbidden.
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
async def test_put_model_config_non_admin_returns_403(
    db: AsyncSession, user_factory, make_auth_client, monkeypatch: pytest.MonkeyPatch
):
    """PUT /config/model returns 403 when caller is not the configured admin."""
    await truncate_all(db)
    user = await user_factory()
    client = make_auth_client(user.id)

    configured_admin_id = str(uuid.uuid4())
    monkeypatch.setattr(settings, "admin_user_id", configured_admin_id)

    payload = {"provider": "opencode_zen", "model_name": "nemotron-3.5-lightning-free"}
    async with client:
        resp = await client.put("/config/model", json=payload)

    assert resp.status_code == 403
    assert resp.json() == {
        "detail": "Admin permissions required to update global model config"
    }


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
async def test_put_model_config_admin_active_row_invariant(
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
