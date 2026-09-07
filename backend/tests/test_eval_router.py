"""
Tests for eval router (backend/app/routers/eval.py).

Covers:
- GET /eval-results:
  - Returns 403 for non-admin (admin_user_id configured to a different UUID).
  - Returns 403 when admin_user_id is unset (fail-closed).
  - Returns 200 and list of EvalResultOut for admin.
- GET /eval-results/{eval_id}:
  - Returns 403 for non-admin.
  - Returns 404 for nonexistent eval ID for admin.
  - Returns 200 and EvalResultOut for existing eval ID.
- POST /eval/run:
  - Returns 403 for non-admin.
  - Returns 201 (or 200) + EvalResultOut in dry-run mode with valid fixture ID.
  - Returns 422 with bogus fixture ID (fails allowlist validation).
- Rate limit on POST /eval/run:
  - Lowering limit to 2/minute via route limits monkeypatch triggers 429
    RateLimitExceeded on rapid requests from the same IP.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
import limits
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.limiter import limiter
from app.models import EvalResult, ModelConfig, User
from tests.conftest import truncate_all


@pytest.fixture(autouse=True)
def _reset_eval_limiter():
    limiter.reset()
    yield
    limiter.reset()


@pytest.mark.asyncio
async def test_get_eval_results_non_admin_returns_403(
    db: AsyncSession, user_factory, make_auth_client, monkeypatch: pytest.MonkeyPatch
):
    """GET /eval-results returns 403 when caller is not the configured admin."""
    await truncate_all(db)
    user = await user_factory()
    client = make_auth_client(user.id)

    monkeypatch.setattr(settings, "admin_user_id", str(uuid.uuid4()))

    async with client:
        resp = await client.get("/eval-results")
    assert resp.status_code == 403
    assert resp.json() == {"detail": "Admin access required"}


@pytest.mark.asyncio
async def test_get_eval_results_admin_user_id_unset_fail_closed_403(
    db: AsyncSession, user_factory, make_auth_client, monkeypatch: pytest.MonkeyPatch
):
    """GET /eval-results returns 403 when admin_user_id is not set (fail-closed)."""
    await truncate_all(db)
    user = await user_factory()
    client = make_auth_client(user.id)

    monkeypatch.setattr(settings, "admin_user_id", None)

    async with client:
        resp = await client.get("/eval-results")
    assert resp.status_code == 403
    assert resp.json() == {"detail": "Admin access required"}


@pytest.mark.asyncio
async def test_get_eval_results_admin_returns_list(
    db: AsyncSession, user_factory, make_auth_client, monkeypatch: pytest.MonkeyPatch
):
    """GET /eval-results returns 200 and list of results for configured admin."""
    await truncate_all(db)
    admin_user = await user_factory()
    client = make_auth_client(admin_user.id)

    monkeypatch.setattr(settings, "admin_user_id", str(admin_user.id))

    # Insert a sample ModelConfig and EvalResult
    mc = ModelConfig(
        provider="opencode_zen",
        model_name="nemotron-3.5-lightning-free",
        base_url="https://opencode.ai/zen/v1",
        is_active=True,
    )
    db.add(mc)
    await db.flush()

    er = EvalResult(
        overall_accuracy=0.85,
        model_config_id=mc.id,
        per_subagent_scores={
            "overall": {"pass_rate": 0.90, "total_fixtures": 10, "passed": 9, "failed": 1},
            "context_gatherer": {"average_score": 0.88},
            "fix_generator": {"average_score": 0.82},
            "mode": "DRY-RUN",
        },
    )
    db.add(er)
    await db.commit()

    async with client:
        resp = await client.get("/eval-results")

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["id"] == str(er.id)
    assert data[0]["overall_accuracy"] == 0.85
    assert data[0]["provider"] == "opencode_zen"
    assert data[0]["model_name"] == "nemotron-3.5-lightning-free"


@pytest.mark.asyncio
async def test_get_eval_result_by_id(
    db: AsyncSession, user_factory, make_auth_client, monkeypatch: pytest.MonkeyPatch
):
    """GET /eval-results/{eval_id} returns 200 for existing result, 404 for missing."""
    await truncate_all(db)
    admin_user = await user_factory()
    client = make_auth_client(admin_user.id)

    monkeypatch.setattr(settings, "admin_user_id", str(admin_user.id))

    er = EvalResult(overall_accuracy=0.95, per_subagent_scores={})
    db.add(er)
    await db.commit()

    async with client:
        # Existing eval_id
        resp = await client.get(f"/eval-results/{er.id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == str(er.id)

        # Nonexistent eval_id
        missing_id = uuid.uuid4()
        missing_resp = await client.get(f"/eval-results/{missing_id}")
        assert missing_resp.status_code == 404
        assert missing_resp.json() == {"detail": "Eval result not found"}


@pytest.mark.asyncio
async def test_post_eval_run_non_admin_returns_403(
    db: AsyncSession, user_factory, make_auth_client, monkeypatch: pytest.MonkeyPatch
):
    """POST /eval/run returns 403 when caller is not the configured admin."""
    await truncate_all(db)
    user = await user_factory()
    client = make_auth_client(user.id)

    monkeypatch.setattr(settings, "admin_user_id", str(uuid.uuid4()))

    payload = {"fixture_ids": ["fixture-001"], "dry_run": True}
    async with client:
        resp = await client.post("/eval/run", json=payload)
    assert resp.status_code == 403
    assert resp.json() == {"detail": "Admin access required"}


@pytest.mark.asyncio
async def test_post_eval_run_bogus_fixture_id_returns_422(
    db: AsyncSession, user_factory, make_auth_client, monkeypatch: pytest.MonkeyPatch
):
    """POST /eval/run with fixture ID not in allowlist returns 422."""
    await truncate_all(db)
    admin_user = await user_factory()
    client = make_auth_client(admin_user.id)

    monkeypatch.setattr(settings, "admin_user_id", str(admin_user.id))

    payload = {"fixture_ids": ["bogus-fixture-id-999"], "dry_run": True}
    async with client:
        resp = await client.post("/eval/run", json=payload)
    assert resp.status_code == 422
    assert "Unknown fixture IDs" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_post_eval_run_dry_run_valid_fixture_success(
    db: AsyncSession, user_factory, make_auth_client, monkeypatch: pytest.MonkeyPatch
):
    """
    POST /eval/run as admin with valid fixture ID in dry-run mode returns
    201 Created and persists an EvalResult row.
    """
    await truncate_all(db)
    admin_user = await user_factory()
    client = make_auth_client(admin_user.id)

    monkeypatch.setattr(settings, "admin_user_id", str(admin_user.id))

    payload = {"fixture_ids": ["fixture-001"], "dry_run": True}
    async with client:
        resp = await client.post("/eval/run", json=payload)

    assert resp.status_code in (200, 201)
    data = resp.json()
    assert "id" in data
    assert data["mode"] == "DRY-RUN"
    assert data["overall_accuracy"] is not None


@pytest.mark.asyncio
async def test_post_eval_run_rate_limiting(
    db: AsyncSession, user_factory, make_auth_client, monkeypatch: pytest.MonkeyPatch
):
    """
    POST /eval/run is rate limited. Lowering limit to 2/minute causes the 3rd
    and subsequent rapid requests to return 429 Too Many Requests.
    """
    await truncate_all(db)
    admin_user = await user_factory()
    client = make_auth_client(admin_user.id)

    monkeypatch.setattr(settings, "admin_user_id", str(admin_user.id))

    limiter.reset()

    # Find the route limits for trigger_eval_run
    route_limits = None
    for k, rls in limiter._route_limits.items():
        if "trigger_eval_run" in k:
            route_limits = rls
            break

    assert route_limits is not None and len(route_limits) > 0
    orig_limit = route_limits[0].limit
    route_limits[0].limit = limits.parse("2/minute")

    payload = {"fixture_ids": ["fixture-001"], "dry_run": True}

    try:
        responses = []
        async with client:
            for _ in range(6):
                resp = await client.post("/eval/run", json=payload)
                responses.append(resp)

        status_codes = [r.status_code for r in responses]

        # First 2 should succeed (200/201)
        assert status_codes[0] in (200, 201)
        assert status_codes[1] in (200, 201)

        # Requests 3 through 6 should be rate-limited (429)
        assert status_codes[2:] == [429, 429, 429, 429]
        assert "rate limit exceeded" in responses[2].text.lower()
    finally:
        route_limits[0].limit = orig_limit
        limiter.reset()


@pytest.mark.asyncio
async def test_get_eval_results_admin_role_succeeds(
    db: AsyncSession, user_factory, make_auth_client, monkeypatch: pytest.MonkeyPatch
):
    """GET /eval-results succeeds for user with role='admin' even when admin_user_id is None."""
    await truncate_all(db)
    admin_user = await user_factory(role="admin")
    client = make_auth_client(admin_user.id)

    monkeypatch.setattr(settings, "admin_user_id", None)

    async with client:
        resp = await client.get("/eval-results")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
