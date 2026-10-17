"""
Tests for main FastAPI application entrypoint (backend/main.py).

Covers:
- GET /health -> {"status": "ok", "runs": {...stranded-run metrics...}}
- GET /health/sandbox -> default github_actions shape and non-github_actions shape
- CORS preflight OPTIONS /repos with allowed Origin vs unauthorized origin
- Rate-limit middleware on /auth/login: lowering limit via monkeypatch to 3,
  sending 11 rapid requests, and verifying RateLimitExceeded triggers 429.
"""

from __future__ import annotations

import httpx
import limits
import pytest

from app.config import settings
from app.limiter import limiter


@pytest.mark.asyncio
async def test_health_check(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """GET /health returns 200 with status ok plus empty runs metrics."""
    async def _no_active_runs() -> list:
        return []

    monkeypatch.setattr("main._query_active_run_ages", _no_active_runs)
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "runs": {
            "active_count": 0,
            "oldest_active_age_seconds": None,
            "stuck_count": 0,
            "stuck_threshold_seconds": 1800,
            "degraded": False,
        },
    }


@pytest.mark.asyncio
async def test_health_exposes_stranded_run_metrics(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """#158 repro: /health must expose pending-run age/count for alerts."""
    async def _fake_ages() -> list:
        return [60.0, 5 * 3600.0]

    monkeypatch.setattr("main._query_active_run_ages", _fake_ages, raising=False)
    response = await client.get("/health")
    assert response.status_code == 200
    runs = response.json()["runs"]
    assert runs["active_count"] == 2
    assert runs["oldest_active_age_seconds"] == 5 * 3600.0
    assert runs["stuck_count"] == 1
    assert runs["stuck_threshold_seconds"] == 1800
    assert runs["degraded"] is True


@pytest.mark.asyncio
async def test_health_no_stuck_runs_not_degraded(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """Fresh active runs report degraded=False so alerts stay quiet."""
    import main

    async def _fresh_ages() -> list:
        return [10.0, 120.0]

    monkeypatch.setattr(main, "_query_active_run_ages", _fresh_ages)
    response = await client.get("/health")
    assert response.status_code == 200
    runs = response.json()["runs"]
    assert runs["active_count"] == 2
    assert runs["oldest_active_age_seconds"] == 120.0
    assert runs["stuck_count"] == 0
    assert runs["degraded"] is False


@pytest.mark.asyncio
async def test_health_db_unavailable_still_ok(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """DB outage keeps liveness 200 with runs=null instead of flapping."""
    import main

    async def _db_down() -> None:
        return None

    monkeypatch.setattr(main, "_query_active_run_ages", _db_down)
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "runs": None}


@pytest.mark.asyncio
async def test_query_active_run_ages_never_raises(
    monkeypatch: pytest.MonkeyPatch,
):
    """The DB helper converts any failure into None without raising."""
    import main

    def _raising_factory(*args: object, **kwargs: object) -> object:
        raise TimeoutError("database unreachable")

    monkeypatch.setattr("app.db.async_session_maker", _raising_factory)
    assert await main._query_active_run_ages() is None


def test_active_run_statuses_match_orchestrator_non_terminal():
    """_ACTIVE_RUN_STATUSES tracks RunStatus minus terminal states (no drift)."""
    import main
    from app.orchestrator import RunStatus

    terminal = {
        "pr_opened",
        "fallback_commented",
        "flaky_detected",
        "completed",
        "error",
    }
    expected = {s.value for s in RunStatus} - terminal
    assert main._ACTIVE_RUN_STATUSES == expected


@pytest.mark.asyncio
async def test_health_reports_real_stranded_run(
    db: AsyncSession, client: httpx.AsyncClient
):
    """Integration (#158): a stale active run surfaces as stuck from real rows.

    Skips without TEST_DATABASE_URL like every other DB-mutating test.
    """
    import uuid
    from datetime import datetime, timedelta, timezone

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import Repo, Run, User

    user = User(
        id=uuid.uuid4(),
        github_id=987654321,
        github_username="health_158_user",
        access_token=None,
        role="user",
    )
    db.add(user)
    await db.commit()
    repo = Repo(user_id=user.id, owner="health-158-org", name="app")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)

    now = datetime.now(timezone.utc)
    db.add_all(
        [
            Run(
                repo_id=repo.id,
                head_sha="a" * 40,
                head_branch="main",
                status="pending",
                created_at=now - timedelta(hours=5),
            ),
            Run(
                repo_id=repo.id,
                head_sha="b" * 40,
                head_branch="main",
                status="fix_generation",
                created_at=now - timedelta(minutes=2),
            ),
            Run(
                repo_id=repo.id,
                head_sha="c" * 40,
                head_branch="main",
                status="pr_opened",
                created_at=now - timedelta(days=9),
            ),
        ]
    )
    await db.commit()

    response = await client.get("/health")
    assert response.status_code == 200
    runs = response.json()["runs"]
    assert runs["active_count"] == 2
    assert runs["stuck_count"] == 1
    assert runs["degraded"] is True
    assert runs["oldest_active_age_seconds"] >= 5 * 3600


async def test_sandbox_health_default_github_actions(client: httpx.AsyncClient):
    """GET /health/sandbox returns 200 with github_actions provider details."""
    response = await client.get("/health/sandbox")
    assert response.status_code == 200
    data = response.json()
    assert data["provider"] == "github_actions"
    assert data["ok"] is True
    assert "org" in data["detail"]
    assert "app_id" in data["detail"]
    assert "installation_id" in data["detail"]


@pytest.mark.asyncio
async def test_sandbox_health_aws_provider(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """GET /health/sandbox with provider=aws returns empty detail dict."""
    monkeypatch.setattr(settings, "sandbox_provider", "aws")
    response = await client.get("/health/sandbox")
    assert response.status_code == 200
    data = response.json()
    assert data["provider"] == "aws"
    assert data["ok"] is True
    assert data["detail"] == {}


@pytest.mark.asyncio
async def test_cors_preflight_allowed_origin(client: httpx.AsyncClient):
    """CORS preflight OPTIONS /repos with Origin: {settings.frontend_url} returns allow headers."""
    headers = {
        "Origin": settings.frontend_url,
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "Content-Type",
    }
    response = await client.options("/repos", headers=headers)
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == settings.frontend_url
    assert response.headers.get("access-control-allow-credentials") == "true"


@pytest.mark.asyncio
async def test_cors_disallowed_origin(client: httpx.AsyncClient):
    """CORS request from untrusted origin does not receive access-control-allow-origin."""
    headers = {
        "Origin": "https://attacker.example.com",
        "Access-Control-Request-Method": "GET",
    }
    response = await client.options("/repos", headers=headers)
    assert (
        response.headers.get("access-control-allow-origin")
        != "https://attacker.example.com"
    )


@pytest.mark.asyncio
async def test_rate_limit_middleware_exceeded(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """11 rapid requests to /auth/login with cap lowered to 3 triggers RateLimitExceeded (429)."""
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "3")
    monkeypatch.setattr(settings, "rate_limit_per_minute", 3)
    limiter.reset()

    # Route limits in slowapi are stored in _route_limits
    route_limits = limiter._route_limits.get("app.auth.login")
    assert route_limits is not None and len(route_limits) > 0

    orig_limit = route_limits[0].limit
    route_limits[0].limit = limits.parse(f"{settings.rate_limit_per_minute}/minute")

    try:
        responses = []
        for _ in range(11):
            resp = await client.get("/auth/login", follow_redirects=False)
            responses.append(resp)

        status_codes = [r.status_code for r in responses]

        # First 3 requests succeed (302 redirect to GitHub OAuth)
        assert status_codes[:3] == [302, 302, 302]

        # Requests 4 through 11 exceed rate limit -> 429 Too Many Requests
        assert status_codes[3:] == [429] * 8
        assert "rate limit exceeded" in responses[3].text.lower()
    finally:
        route_limits[0].limit = orig_limit
        limiter.reset()
