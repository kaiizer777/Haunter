"""
Tests for main FastAPI application entrypoint (backend/main.py).

Covers:
- GET /health -> {"status": "ok"}
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
async def test_health_check(client: httpx.AsyncClient):
    """GET /health returns 200 with {'status': 'ok'}."""
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
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
async def test_sandbox_health_aws_provider(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch):
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
    assert response.headers.get("access-control-allow-origin") != "https://attacker.example.com"


@pytest.mark.asyncio
async def test_rate_limit_middleware_exceeded(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch):
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
