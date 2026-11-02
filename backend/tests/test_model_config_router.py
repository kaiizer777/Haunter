"""
Tests for model configuration router (backend/app/routers/model_config.py).

Covers:
- GET /config/model/available returns both default provider buckets:
  - opencode_go (dynamic gateway models, bootstrap fallback without API key)
  - groq (approved production models).
- PUT /config/model as admin:
  - Creates a new ModelConfig row with is_active=True.
  - Deactivates previous active global ModelConfig rows (single-active invariant).
- PUT /config/model (global) is admin-only (403 for non-admins).
- PUT /config/model with invalid provider (e.g. "gcp") returns 422.
- PUT /config/model with invalid model returns 422 (incl. retired providers).
- Unauthenticated requests return 401.
- GET /config/model (global, no repo_id) is admin-only; returns active global config or env default.
- Global model config only (no per-repo overrides).
"""

from __future__ import annotations

import uuid

import httpx
import pytest
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
async def test_get_available_models_returns_only_default_buckets(
    db: AsyncSession, user_factory, make_auth_client
):
    """
    GET /config/model/available returns exactly the opencode_go + groq buckets.
    Without an opencode_go API key the router serves BOOTSTRAP_GO_MODELS.
    """
    await truncate_all(db)
    user = await user_factory(role="admin")
    client = make_auth_client(user.id)

    async with client:
        resp = await client.get("/config/model/available")

    assert resp.status_code == 200
    data = resp.json()

    # Only the two default providers are exposed.
    assert set(data.keys()) == {"opencode_go", "groq"}

    go_ids = [m["id"] for m in data["opencode_go"]]
    for mid in ("deepseek-v4.1-flash", "glm-5.2", "mimo-v2.6-flash"):
        assert mid in go_ids

    groq_ids = [m["id"] for m in data["groq"]]
    assert groq_ids == [
        "openai/gpt-oss-120b",
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "deepseek-r1-distill-llama-70b",
    ]

    gpt_oss = next(m for m in data["groq"] if m["id"] == "openai/gpt-oss-120b")
    assert gpt_oss["context_window"] == 131072


# ---------------------------------------------------------------------------
# PUT /config/model (Global Model Config Switcher)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_model_config_unauthenticated(client: httpx.AsyncClient):
    """PUT /config/model without session cookie returns 401."""
    payload = {"provider": "opencode_go", "model_name": "deepseek-v4.1-flash"}
    resp = await client.put("/config/model", json=payload)
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Not authenticated"}


@pytest.mark.asyncio
async def test_put_global_model_config_forbidden_for_non_admin(
    db: AsyncSession, user_factory, make_auth_client, monkeypatch: pytest.MonkeyPatch
):
    """PUT /config/model (global) without admin privileges returns 403 Forbidden."""
    await truncate_all(db)
    non_admin_user = await user_factory(role="user")
    client = make_auth_client(non_admin_user.id)

    # Set ADMIN_USER_ID to a different user ID
    monkeypatch.setattr(settings, "admin_user_id", str(uuid.uuid4()))

    payload = {"provider": "opencode_go", "model_name": "deepseek-v4.1-flash"}
    async with client:
        resp = await client.put("/config/model", json=payload)

    assert resp.status_code == 403
    assert "Admin permissions required" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_put_model_config_gated_before_validation(
    db: AsyncSession, user_factory, make_auth_client, monkeypatch: pytest.MonkeyPatch
):
    """Non-admin invalid-provider PUT returns 403, not 422 (no allowlist oracle)."""
    await truncate_all(db)
    non_admin_user = await user_factory(role="user")
    client = make_auth_client(non_admin_user.id)

    monkeypatch.setattr(settings, "admin_user_id", str(uuid.uuid4()))

    payload = {"provider": "gcp", "model_name": "gemini-pro"}
    async with client:
        resp = await client.put("/config/model", json=payload)

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_put_global_model_config_allowed_for_admin(
    db: AsyncSession, user_factory, make_auth_client, monkeypatch: pytest.MonkeyPatch
):
    """PUT /config/model (global) succeeds for admin user."""
    await truncate_all(db)
    admin_user = await user_factory(role="admin")
    client = make_auth_client(admin_user.id)

    payload = {"provider": "opencode_go", "model_name": "deepseek-v4.1-flash"}
    async with client:
        resp = await client.put("/config/model", json=payload)

    assert resp.status_code == 200
    data = resp.json()
    assert data["provider"] == "opencode_go"
    assert data["model_name"] == "deepseek-v4.1-flash"
    assert data["is_active"] is True


@pytest.mark.asyncio
async def test_put_model_config_invalid_provider_returns_422(
    db: AsyncSession, user_factory, make_auth_client, monkeypatch: pytest.MonkeyPatch
):
    """PUT /config/model with invalid provider (e.g. 'gcp') returns 422."""
    await truncate_all(db)
    admin_user = await user_factory(role="admin")
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
    PUT /config/model returns 422 when:
    - the model is outside the provider allowlist (opencode_go / groq), or
    - the provider is retired (opencode_zen, openai, anthropic) or unknown.
    """
    await truncate_all(db)
    admin_user = await user_factory(role="admin")
    client = make_auth_client(admin_user.id)

    monkeypatch.setattr(settings, "admin_user_id", str(admin_user.id))

    async with client:
        # opencode_go model outside OPENCODE_GO_MODELS
        r1 = await client.put(
            "/config/model",
            json={"provider": "opencode_go", "model_name": "openai/gpt-oss-120b"},
        )
        assert r1.status_code == 422

        # groq model outside GROQ_MODELS
        r2 = await client.put(
            "/config/model",
            json={"provider": "groq", "model_name": "deepseek-v4.1-flash"},
        )
        assert r2.status_code == 422

        # retired providers are rejected, even with formerly valid models
        for payload in (
            {"provider": "opencode_zen", "model_name": "nemotron-3.5-lightning-free"},
            {"provider": "openai", "model_name": "gpt-4o"},
            {"provider": "anthropic", "model_name": "claude-sonnet-4-5"},
        ):
            r = await client.put("/config/model", json=payload)
            assert r.status_code == 422


async def test_put_model_config_active_row_invariant(
    db: AsyncSession, user_factory, make_auth_client, monkeypatch: pytest.MonkeyPatch
):
    """
    PUT /config/model as admin:
    1. Creates a ModelConfig row with is_active=True.
    2. Deactivates existing active rows (single active-row invariant).
    """
    await truncate_all(db)
    admin_user = await user_factory(role="admin")
    client = make_auth_client(admin_user.id)

    monkeypatch.setattr(settings, "admin_user_id", str(admin_user.id))

    # Seed an existing active model config
    initial_config = ModelConfig(
        provider="opencode_go",
        model_name="deepseek-v4.1-flash",
        base_url="https://opencode.ai/inference/go/openai/v1",
        is_active=True,
    )
    db.add(initial_config)
    await db.commit()
    initial_id = initial_config.id

    # Switch to groq/openai/gpt-oss-120b
    payload = {"provider": "groq", "model_name": "openai/gpt-oss-120b"}
    async with client:
        resp = await client.put("/config/model", json=payload)

    assert resp.status_code == 200
    data = resp.json()
    assert data["provider"] == "groq"
    assert data["model_name"] == "openai/gpt-oss-120b"
    assert data["is_active"] is True
    new_id = data["id"]

    # Verify active-row invariant in DB
    await db.rollback()
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
    user = await user_factory(role="admin")
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
            provider="groq",
            model_name="llama-3.1-8b-instant",
            base_url="https://api.groq.com/openai/v1",
            is_active=True,
        )
        db.add(mc)
        await db.commit()

        # Should now return the active DB row
        resp2 = await client.get("/config/model")
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["provider"] == "groq"
        assert data2["model_name"] == "llama-3.1-8b-instant"
        assert data2["id"] == str(mc.id)


# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_global_model_config_forbidden_for_non_admin(
    db: AsyncSession, user_factory, make_auth_client
):
    """GET /config/model (global, no repo_id) returns 403 for non-admin users."""
    await truncate_all(db)
    non_admin_user = await user_factory(role="user")
    client = make_auth_client(non_admin_user.id)
    async with client:
        resp = await client.get("/config/model")
    assert resp.status_code == 403
    assert "Admin permissions required" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_put_model_config_persisted_role_succeeds(
    db: AsyncSession, user_factory, make_auth_client
):
    """PUT /config/model succeeds when user has role='admin' in DB without needing admin_user_id env."""
    await truncate_all(db)
    admin_user = await user_factory(role="admin")
    client = make_auth_client(admin_user.id)

    payload = {"provider": "opencode_go", "model_name": "deepseek-v4.1-flash"}
    async with client:
        resp = await client.put("/config/model", json=payload)

    assert resp.status_code == 200
    assert resp.json()["model_name"] == "deepseek-v4.1-flash"
    assert resp.json()["is_active"] is True


@pytest.mark.asyncio
async def test_put_global_model_config_allowed_for_admin_github_username(
    db: AsyncSession, user_factory, make_auth_client, monkeypatch: pytest.MonkeyPatch
):
    """PUT /config/model (global) succeeds when caller's username is in admin_github_usernames."""
    await truncate_all(db)
    owner_user = await user_factory(username="kaiizer777", role="user")
    client = make_auth_client(owner_user.id)
    monkeypatch.setattr(settings, "admin_user_id", str(uuid.uuid4()))
    monkeypatch.setattr(settings, "admin_github_usernames", ["kaiizer777", "kaiizer77"])

    payload = {"provider": "opencode_go", "model_name": "deepseek-v4.1-flash"}
    async with client:
        resp = await client.put("/config/model", json=payload)

    assert resp.status_code == 200
    assert resp.json()["model_name"] == "deepseek-v4.1-flash"


@pytest.mark.asyncio
async def test_put_global_model_config_forbidden_when_no_admin_configured(
    db: AsyncSession, user_factory, make_auth_client, monkeypatch: pytest.MonkeyPatch
):
    """PUT /config/model (global) returns 403 for non-admins even when no admin is configured (closed by default)."""
    await truncate_all(db)
    user = await user_factory(username="regular_user", role="user")
    client = make_auth_client(user.id)
    monkeypatch.setattr(settings, "admin_user_id", None)
    monkeypatch.setattr(settings, "admin_github_usernames", [])

    payload = {"provider": "opencode_go", "model_name": "deepseek-v4.1-flash"}
    async with client:
        resp = await client.put("/config/model", json=payload)

    assert resp.status_code == 403
    assert "Admin permissions required" in resp.json()["detail"]



@pytest.mark.asyncio
async def test_global_read_falls_back_when_active_row_uses_retired_provider(
    db: AsyncSession, user_factory, make_auth_client
):
    """GET /config/model serves env defaults when the active row has a retired
    provider (opencode_zen/openai/anthropic) instead of failing or leaking it.

    Migration decision (issue #205): no migration rewrites stored rows; reads
    treat retired-provider rows as absent so legacy data never 422s.
    """
    await truncate_all(db)
    user = await user_factory(role="admin")
    client = make_auth_client(user.id)

    legacy = ModelConfig(
        provider="openai",
        model_name="gpt-4o",
        base_url="https://api.openai.com/v1",
        is_active=True,
    )
    db.add(legacy)
    await db.commit()

    async with client:
        resp = await client.get("/config/model")

    assert resp.status_code == 200
    data = resp.json()
    assert data["provider"] == settings.default_provider
    assert data["model_name"] == settings.default_model
    assert data["provider"] in ("opencode_go", "groq")




def test_default_provider_and_model_are_allowlisted():
    """settings defaults must pass ModelConfigUpdate validation themselves."""
    from app.schemas import ModelConfigUpdate

    cfg = ModelConfigUpdate(
        provider=settings.default_provider, model_name=settings.default_model
    )
    assert cfg.provider in ("opencode_go", "groq")
