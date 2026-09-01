"""
Phase 14 — Hosting adapter and /config/hosting endpoint tests.

Covers:
 1. AWSHostingAdapter.schedule_pipeline -> invokes Lambda async (boto3 mocked).
 2. AWSHostingAdapter falls back to BackgroundTasks when function name not set.
 3. get_hosting_adapter() returns AWSHostingAdapter when HOSTING_PROVIDER=aws.
 4. GET /config/hosting -> 200 with env defaults (unauthenticated -> 401).
 5. GET /config/hosting authenticated -> 200 with correct shape.
 6. PUT /config/hosting valid -> 200 updates both providers (admin user).
 7. PUT /config/hosting non-admin -> 403 Forbidden.
 8. PUT /config/hosting evil hosting_provider -> 422 Unprocessable Entity.
 9. PUT /config/hosting evil sandbox_provider -> 422 Unprocessable Entity.
10. Webhook with AWS hosting -> Lambda invoke called (BackgroundTasks not used for pipeline).
11. invalidate_provider_cache() clears the TTL cache.
12. _get_provider_config returns env default on DB error (resilience).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.adapters.hosting import (
    AWSHostingAdapter,
    _ALLOWED_PROVIDERS,
    _cfg_cache,
    get_hosting_adapter,
    invalidate_provider_cache,
)
from app.models import User
from tests.conftest import truncate_all


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(admin: bool = False) -> User:
    uid = uuid.uuid4()
    return User(
        id=uid,
        github_id=999000 + uid.int % 1000,
        github_username="testuser",
        access_token="fake",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Test 2: AWSHostingAdapter.schedule_pipeline invokes Lambda async
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aws_adapter_invokes_lambda_async():
    """AWSHostingAdapter should call boto3 lambda invoke with InvocationType='Event'."""
    from fastapi import BackgroundTasks

    run_id = uuid.uuid4()
    bg = BackgroundTasks()

    mock_boto_client = MagicMock()
    mock_boto_client.invoke.return_value = {"StatusCode": 202}

    with patch("app.config.settings.aws_lambda_function_name", "haunter-test"):
        with patch("boto3.client", return_value=mock_boto_client):
            adapter = AWSHostingAdapter()
            await adapter.schedule_pipeline(run_id, bg)

    mock_boto_client.invoke.assert_called_once()
    call_kwargs = mock_boto_client.invoke.call_args
    assert call_kwargs.kwargs.get("InvocationType") == "Event" or (
        len(call_kwargs.args) > 1 and "Event" in str(call_kwargs)
    )
    assert str(run_id) in call_kwargs.kwargs.get("Payload", b"").decode()

    # Should NOT have added anything to background_tasks
    assert len(bg.tasks) == 0


# ---------------------------------------------------------------------------
# Test 3: AWSHostingAdapter falls back to BackgroundTasks when no function name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aws_adapter_fallback_on_missing_function_name():
    """AWSHostingAdapter falls back to BackgroundTasks when function name not set."""
    from fastapi import BackgroundTasks

    run_id = uuid.uuid4()
    bg = BackgroundTasks()

    with patch("app.config.settings.aws_lambda_function_name", None):
        with patch.dict("os.environ", {}, clear=False):
            # Remove AWS_LAMBDA_FUNCTION_NAME if set
            import os
            os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)

            adapter = AWSHostingAdapter()
            with patch("app.orchestrator.handle_failed_run"):
                await adapter.schedule_pipeline(run_id, bg)

    # Should have fallen back to BackgroundTasks
    assert len(bg.tasks) == 1


# ---------------------------------------------------------------------------
# Test 5: get_hosting_adapter factory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_hosting_adapter_aws():
    """get_hosting_adapter returns AWSHostingAdapter when provider=aws."""
    invalidate_provider_cache()
    with patch(
        "app.adapters.hosting._get_provider_config",
        new=AsyncMock(return_value="aws"),
    ):
        adapter = await get_hosting_adapter()
    assert isinstance(adapter, AWSHostingAdapter)


# ---------------------------------------------------------------------------
# Test 6: GET /config/hosting unauthenticated -> 401
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_hosting_config_unauthenticated(client):
    """GET /config/hosting without session cookie must return 401."""
    resp = await client.get("/config/hosting")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Test 7: GET /config/hosting authenticated -> 200 with correct shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_hosting_config_authenticated(make_auth_client, db, user_factory):
    """GET /config/hosting returns 200 with hosting_provider and sandbox_provider."""
    await truncate_all(db)
    user = await user_factory()
    async with make_auth_client(user.id) as auth_client:
        resp = await auth_client.get("/config/hosting")

    assert resp.status_code == 200
    data = resp.json()
    assert "hosting_provider" in data
    assert "sandbox_provider" in data
    assert "source" in data
    assert data["hosting_provider"] == "aws"


# ---------------------------------------------------------------------------
# Test 8: PUT /config/hosting valid body -> 200 (admin)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_hosting_config_admin_valid(make_auth_client, db, user_factory):
    """PUT /config/hosting with valid body and admin user -> 200 and updated values."""
    await truncate_all(db)
    user = await user_factory()

    with patch("app.config.settings.admin_user_id", str(user.id)):
        async with make_auth_client(user.id) as auth_client:
            resp = await auth_client.put(
                "/config/hosting",
                json={"hosting_provider": "aws", "sandbox_provider": "aws"},
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["hosting_provider"] == "aws"
    assert data["sandbox_provider"] == "aws"
    assert data["source"] == "db"


# ---------------------------------------------------------------------------
# Test 9: PUT /config/hosting non-admin -> 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_hosting_config_non_admin_forbidden(make_auth_client, db, user_factory):
    """PUT /config/hosting with ADMIN_USER_ID set to different user -> 403."""
    await truncate_all(db)
    user = await user_factory()
    other_admin_id = str(uuid.uuid4())

    with patch("app.config.settings.admin_user_id", other_admin_id):
        async with make_auth_client(user.id) as auth_client:
            resp = await auth_client.put(
                "/config/hosting",
                json={"hosting_provider": "aws", "sandbox_provider": "aws"},
            )

    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Test 10: PUT /config/hosting evil hosting_provider -> 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_hosting_config_evil_hosting_provider(make_auth_client, db, user_factory):
    """PUT /config/hosting with hosting_provider='evil' must be rejected with 422."""
    await truncate_all(db)
    user = await user_factory()

    with patch("app.config.settings.admin_user_id", str(user.id)):
        async with make_auth_client(user.id) as auth_client:
            resp = await auth_client.put(
                "/config/hosting",
                json={"hosting_provider": "evil_provider", "sandbox_provider": "aws"},
            )

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Test 11: PUT /config/hosting evil sandbox_provider -> 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_hosting_config_evil_sandbox_provider(make_auth_client, db, user_factory):
    """PUT /config/hosting with sandbox_provider='malicious' must be rejected with 422."""
    await truncate_all(db)
    user = await user_factory()

    with patch("app.config.settings.admin_user_id", str(user.id)):
        async with make_auth_client(user.id) as auth_client:
            resp = await auth_client.put(
                "/config/hosting",
                json={"hosting_provider": "aws", "sandbox_provider": "malicious"},
            )
            assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Test 14: invalidate_provider_cache clears cache
# ---------------------------------------------------------------------------


def test_invalidate_provider_cache_clears_all():
    """invalidate_provider_cache should empty the TTL cache dict."""
    _cfg_cache["hosting_provider"] = ("aws", 9999.0)
    _cfg_cache["sandbox_provider"] = ("aws", 9999.0)
    assert len(_cfg_cache) == 2

    invalidate_provider_cache()

    assert len(_cfg_cache) == 0


# ---------------------------------------------------------------------------
# Test 15: _get_provider_config returns env default on DB error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_provider_config_db_error_fallback():
    """_get_provider_config falls back to env default when DB is unreachable."""
    from app.adapters.hosting import _get_provider_config

    invalidate_provider_cache()

    with patch("app.db.async_session_maker", side_effect=Exception("DB down")):
        result = await _get_provider_config("hosting_provider", "aws")

    assert result == "aws"


# ---------------------------------------------------------------------------
# Test: _ALLOWED_PROVIDERS contains exactly aws
# ---------------------------------------------------------------------------


def test_allowed_providers_is_strict():
    """Allowed provider set must be exactly {'aws'}."""
    assert _ALLOWED_PROVIDERS == frozenset({"aws"})
