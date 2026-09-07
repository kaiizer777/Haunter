"""
Tests for hosting and sandbox provider configuration router (backend/app/routers/hosting_config.py).

Covers:
- GET /config/hosting as authenticated user returns {hosting_provider, sandbox_provider, source}:
  - source="env" when system_configs has no rows.
  - source="db" when system_configs has rows.
- PUT /config/hosting as non-admin (admin_user_id set to a different UUID) returns 403.
- PUT /config/hosting as admin returns 200 and persists values (verified via GET).
- Invalid provider in payload (e.g. "gcp") returns 422 via Pydantic allowlist validation.
- Cache invalidation: after successful PUT, the in-process TTL cache is invalidated
  and subsequent calls read the new DB value.
- Unauthenticated requests to GET and PUT return 401.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.hosting import (
    get_active_hosting_provider,
    get_active_sandbox_provider,
    invalidate_provider_cache,
)
from app.config import settings
from app.models import SystemConfig, User
from tests.conftest import truncate_all


@pytest.mark.asyncio
async def test_get_hosting_config_unauthenticated(client: httpx.AsyncClient):
    """GET /config/hosting without a session cookie returns 401 Unauthorized."""
    resp = await client.get("/config/hosting")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Not authenticated"}


@pytest.mark.asyncio
async def test_put_hosting_config_unauthenticated(client: httpx.AsyncClient):
    """PUT /config/hosting without a session cookie returns 401 Unauthorized."""
    payload = {"hosting_provider": "aws", "sandbox_provider": "aws"}
    resp = await client.put("/config/hosting", json=payload)
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Not authenticated"}


@pytest.mark.asyncio
async def test_get_hosting_config_source_env(
    db: AsyncSession, user_factory, make_auth_client
):
    """
    GET /config/hosting when system_configs table has no rows returns
    values from settings (env defaults) with source="env".
    """
    await truncate_all(db)
    user = await user_factory()
    client = make_auth_client(user.id)

    async with client:
        resp = await client.get("/config/hosting")

    assert resp.status_code == 200
    data = resp.json()
    assert data["hosting_provider"] == settings.hosting_provider
    assert data["sandbox_provider"] == settings.sandbox_provider
    assert data["source"] == "env"


@pytest.mark.asyncio
async def test_get_hosting_config_source_db(
    db: AsyncSession, user_factory, make_auth_client
):
    """
    GET /config/hosting when system_configs table has rows returns
    persisted values with source="db".
    """
    await truncate_all(db)
    user = await user_factory()
    client = make_auth_client(user.id)

    # Insert DB overrides
    now = datetime.now(timezone.utc)
    db.add_all([
        SystemConfig(key="hosting_provider", value="aws", updated_at=now),
        SystemConfig(key="sandbox_provider", value="aws", updated_at=now),
    ])
    await db.commit()

    async with client:
        resp = await client.get("/config/hosting")

    assert resp.status_code == 200
    data = resp.json()
    assert data["hosting_provider"] == "aws"
    assert data["sandbox_provider"] == "aws"
    assert data["source"] == "db"


@pytest.mark.asyncio
async def test_put_hosting_config_non_admin_returns_403(
    db: AsyncSession, user_factory, make_auth_client, monkeypatch: pytest.MonkeyPatch
):
    """
    PUT /config/hosting as a non-admin user (admin_user_id set to a different UUID)
    returns 403 Forbidden.
    """
    await truncate_all(db)
    user = await user_factory()
    client = make_auth_client(user.id)

    configured_admin_id = str(uuid.uuid4())
    monkeypatch.setattr(settings, "admin_user_id", configured_admin_id)

    payload = {"hosting_provider": "aws", "sandbox_provider": "aws"}
    async with client:
        resp = await client.put("/config/hosting", json=payload)

    assert resp.status_code == 403
    assert resp.json() == {
        "detail": "Admin permissions required to update hosting/sandbox provider"
    }


@pytest.mark.asyncio
async def test_put_hosting_config_admin_success_and_persisted(
    db: AsyncSession, user_factory, make_auth_client, monkeypatch: pytest.MonkeyPatch
):
    """
    PUT /config/hosting as configured admin returns 200, updates DB system_configs,
    and returns source="db". Subsequent GET /config/hosting reflects the new state.
    """
    await truncate_all(db)
    admin_user = await user_factory()
    client = make_auth_client(admin_user.id)

    monkeypatch.setattr(settings, "admin_user_id", str(admin_user.id))

    payload = {"hosting_provider": "aws", "sandbox_provider": "aws"}
    async with client:
        resp = await client.put("/config/hosting", json=payload)

        assert resp.status_code == 200
        data = resp.json()
        assert data["hosting_provider"] == "aws"
        assert data["sandbox_provider"] == "aws"
        assert data["source"] == "db"

        # Verify persisted via GET
        get_resp = await client.get("/config/hosting")
        assert get_resp.status_code == 200
        get_data = get_resp.json()
        assert get_data["hosting_provider"] == "aws"
        assert get_data["sandbox_provider"] == "aws"
        assert get_data["source"] == "db"

    # Verify directly in DB
    result = await db.execute(
        select(SystemConfig).where(
            SystemConfig.key.in_(["hosting_provider", "sandbox_provider"])
        )
    )
    rows = {r.key: r.value for r in result.scalars().all()}
    assert rows.get("hosting_provider") == "aws"
    assert rows.get("sandbox_provider") == "aws"


@pytest.mark.asyncio
async def test_put_hosting_config_invalid_provider_returns_422(
    db: AsyncSession, user_factory, make_auth_client, monkeypatch: pytest.MonkeyPatch
):
    """
    PUT /config/hosting with an invalid provider (e.g. "gcp") fails Pydantic schema
    validation and returns 422 Unprocessable Entity.
    """
    await truncate_all(db)
    admin_user = await user_factory()
    client = make_auth_client(admin_user.id)

    monkeypatch.setattr(settings, "admin_user_id", str(admin_user.id))

    invalid_hosting_payload = {"hosting_provider": "gcp", "sandbox_provider": "aws"}
    invalid_sandbox_payload = {"hosting_provider": "aws", "sandbox_provider": "gcp"}
    async with client:
        resp = await client.put("/config/hosting", json=invalid_hosting_payload)
        assert resp.status_code == 422

        resp2 = await client.put("/config/hosting", json=invalid_sandbox_payload)
        assert resp2.status_code == 422


@pytest.mark.asyncio
async def test_put_hosting_config_invalidates_in_process_cache(
    db: AsyncSession, user_factory, make_auth_client, monkeypatch: pytest.MonkeyPatch
):
    """
    After a successful PUT /config/hosting, invalidate_provider_cache() is triggered,
    ensuring subsequent adapter reads immediately query DB rather than returning stale cache.
    """
    await truncate_all(db)
    admin_user = await user_factory()
    client = make_auth_client(admin_user.id)

    monkeypatch.setattr(settings, "admin_user_id", str(admin_user.id))

    # Prime adapter cache with env defaults
    cached_val = await get_active_hosting_provider()
    assert cached_val == settings.hosting_provider

    payload = {"hosting_provider": "aws", "sandbox_provider": "aws"}
    async with client:
        put_resp = await client.put("/config/hosting", json=payload)
        assert put_resp.status_code == 200

    # Cache should have been invalidated by PUT handler
    new_provider = await get_active_hosting_provider()
    assert new_provider == "aws"

    new_sandbox = await get_active_sandbox_provider()
    assert new_sandbox == "aws"


@pytest.mark.asyncio
async def test_put_hosting_config_admin_role_succeeds(
    db: AsyncSession, user_factory, make_auth_client
):
    """PUT /config/hosting succeeds for user with role='admin' in DB without admin_user_id env."""
    await truncate_all(db)
    admin_user = await user_factory(role="admin")
    client = make_auth_client(admin_user.id)

    payload = {"hosting_provider": "aws", "sandbox_provider": "aws"}
    async with client:
        resp = await client.put("/config/hosting", json=payload)

    assert resp.status_code == 200
    assert resp.json()["hosting_provider"] == "aws"
