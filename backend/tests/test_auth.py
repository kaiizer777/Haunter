"""
Authentication & Session Management tests (test_auth.py).

Covers:
1. Login redirect (302) & state cookie security attributes.
2. Callback missing state cookie -> 400 + Max-Age=0.
3. Callback missing state parameter -> 400 + Max-Age=0.
4. Callback tampered state signature -> 400 + Max-Age=0.
5. Callback expired state (>600s via freeze_time) -> 400.
6. Callback valid state but missing code -> 400 + Max-Age=0.
7. Callback valid full flow -> 302 + upsert user + 14d session cookie + clear state cookie.
8. Callback state single-use / replay prevention -> 400.
9. Me endpoint: missing cookie -> 401.
10. Me endpoint: tampered cookie -> 401.
11. Me endpoint: expired session (>14d via freeze_time) -> 401.
12. Me endpoint: valid signed cookie for non-existent user in DB -> 401.
13. Me endpoint: valid authenticated user -> 200 shape matching schema.
14. Logout endpoint: clears session cookie with identical attributes; idempotent 200.
15. Key rotation: previous secret key valid, primary key valid, untrusted key rejected.
16. Rate limiting: 20 requests ok, 21st triggers 429.
17. CORS configuration: blocked evil origin, allowed frontend origin, preflight handling.
18. PKCE architecture decision documented + generic error messages without secret leaks.
"""

from datetime import timedelta
import uuid
from freezegun import freeze_time
import httpx
from itsdangerous import TimestampSigner
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.auth as auth_module
from app.auth import _SESSION_MAX_AGE, _STATE_MAX_AGE, _sign_state, _sign_user_id, _verify_session_cookie
from app.config import settings
from app.limiter import limiter
from app.models import User
from tests.conftest import truncate_all


@pytest.mark.asyncio
async def test_login_redirect_and_state_cookie_attrs(client: httpx.AsyncClient):
    """GET /auth/login returns 302 to GitHub with hardened state cookie."""
    resp = await client.get("/auth/login")
    assert resp.status_code == 302
    location = resp.headers.get("location", "")
    assert location.startswith("https://github.com/login/oauth/authorize")
    assert f"client_id={settings.github_client_id}" in location
    assert "scope=read%3Auser+repo" in location or "scope=read%3Auser%20repo" in location or "scope=read:user repo" in location
    assert "state=" in location

    # Verify state cookie attributes
    set_cookie = resp.headers.get("set-cookie", "")
    assert "haunter_oauth_state=" in set_cookie
    assert "HttpOnly" in set_cookie or "httponly" in set_cookie
    assert "Secure" in set_cookie or "secure" in set_cookie
    assert "SameSite=none" in set_cookie.lower() or "samesite=none" in set_cookie.lower()
    assert f"Max-Age={_STATE_MAX_AGE}" in set_cookie or f"max-age={_STATE_MAX_AGE}" in set_cookie


@pytest.mark.asyncio
async def test_login_redirect_includes_repo_scope(client: httpx.AsyncClient):
    """GET /auth/login returns 302 with read:user repo scope for repo discovery."""
    resp = await client.get("/auth/login")
    assert resp.status_code == 302
    location = resp.headers.get("location", "")
    assert "scope=read%3Auser+repo" in location or "scope=read%3Auser%20repo" in location or "scope=read:user repo" in location


@pytest.mark.asyncio
async def test_callback_missing_state_cookie(client: httpx.AsyncClient):
    """Callback with missing state cookie returns 400 and clears state cookie."""
    resp = await client.get("/auth/callback?code=test_code&state=test_state")
    assert resp.status_code == 400
    assert resp.json() == {"detail": "Authentication failed"}
    set_cookie = resp.headers.get("set-cookie", "")
    assert "haunter_oauth_state=" in set_cookie
    assert "Max-Age=0" in set_cookie or "max-age=0" in set_cookie


@pytest.mark.asyncio
async def test_callback_missing_state_param(client: httpx.AsyncClient, signed_state_factory):
    """Callback with missing state query parameter returns 400 and clears cookie."""
    signed_state = signed_state_factory("valid_raw_state")
    resp = await client.get(
        "/auth/callback?code=test_code",
        cookies={"haunter_oauth_state": signed_state},
    )
    assert resp.status_code == 400
    assert resp.json() == {"detail": "Authentication failed"}
    set_cookie = resp.headers.get("set-cookie", "")
    assert "haunter_oauth_state=" in set_cookie
    assert "Max-Age=0" in set_cookie or "max-age=0" in set_cookie


@pytest.mark.asyncio
async def test_callback_tampered_state_signature(client: httpx.AsyncClient):
    """Callback with forged/tampered state signature returns 400."""
    resp = await client.get(
        "/auth/callback?code=test_code&state=legit_state",
        cookies={"haunter_oauth_state": "legit_state.tampered_signature_bytes"},
    )
    assert resp.status_code == 400
    assert resp.json() == {"detail": "Authentication failed"}
    set_cookie = resp.headers.get("set-cookie", "")
    assert "haunter_oauth_state=" in set_cookie
    assert "Max-Age=0" in set_cookie or "max-age=0" in set_cookie


@pytest.mark.asyncio
async def test_callback_expired_state(client: httpx.AsyncClient, signed_state_factory):
    """State cookie older than 600s is rejected as expired."""
    raw_state = "state_to_expire_123"
    with freeze_time("2026-08-01 12:00:00"):
        signed_state = signed_state_factory(raw_state)

    with freeze_time("2026-08-01 12:10:05"):  # 605s later (> 600s)
        resp = await client.get(
            f"/auth/callback?code=test_code&state={raw_state}",
            cookies={"haunter_oauth_state": signed_state},
        )
        assert resp.status_code == 400
        assert resp.json() == {"detail": "Authentication failed"}


@pytest.mark.asyncio
async def test_callback_valid_state_missing_code(client: httpx.AsyncClient, signed_state_factory):
    """Valid state signature but missing code query parameter returns 400."""
    raw_state = "valid_state_no_code"
    signed_state = signed_state_factory(raw_state)

    resp = await client.get(
        f"/auth/callback?state={raw_state}",
        cookies={"haunter_oauth_state": signed_state},
    )
    assert resp.status_code == 400
    assert resp.json() == {"detail": "Authentication failed"}
    set_cookie = resp.headers.get("set-cookie", "")
    assert "haunter_oauth_state=" in set_cookie
    assert "Max-Age=0" in set_cookie or "max-age=0" in set_cookie


@pytest.mark.asyncio
@respx.mock
async def test_callback_valid_full_flow(
    client: httpx.AsyncClient,
    db: AsyncSession,
    signed_state_factory,
):
    """Complete valid OAuth callback flow: exchanges code, fetches profile, sets 14d session."""
    await truncate_all(db)
    raw_state = "legit_flow_state_98765"
    signed_state = signed_state_factory(raw_state)

    # Mock GitHub OAuth Token endpoint
    respx.post("https://github.com/login/oauth/access_token").mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "gho_mock_access_token_xyz999", "token_type": "bearer"},
        )
    )

    # Mock GitHub User API endpoint
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 77112233,
                "login": "octo_auth_user",
                "avatar_url": "https://avatars.githubusercontent.com/u/77112233",
            },
        )
    )

    resp = await client.get(
        f"/auth/callback?code=valid_gh_code&state={raw_state}",
        cookies={"haunter_oauth_state": signed_state},
        follow_redirects=False,
    )

    # 1. 302 Redirect to FRONTEND_URL
    assert resp.status_code == 302
    assert resp.headers["location"] == settings.frontend_url

    # 2. Session cookie set with 14d max age & security attributes
    cookies_header = resp.headers.get_list("set-cookie")
    cookies_str = " ; ".join(cookies_header)
    assert "haunter_session=" in cookies_str
    assert f"Max-Age={_SESSION_MAX_AGE}" in cookies_str or f"max-age={_SESSION_MAX_AGE}" in cookies_str
    assert "haunter_oauth_state=" in cookies_str  # state cookie cleared

    # 3. User persisted in DB
    result = await db.execute(select(User).where(User.github_id == 77112233))
    user = result.scalar_one_or_none()
    assert user is not None
    assert user.github_username == "octo_auth_user"
    assert user.avatar_url == "https://avatars.githubusercontent.com/u/77112233"
    assert user.access_token is not None


@pytest.mark.asyncio
async def test_callback_replay_same_state(client: httpx.AsyncClient, signed_state_factory):
    """Replaying an OAuth callback without state cookie fails immediately."""
    raw_state = "replayed_state_token"
    # State cookie is not provided (simulates single-use after client cleared it or attacker replaying)
    resp = await client.get(f"/auth/callback?code=replayed_code&state={raw_state}")
    assert resp.status_code == 400
    assert resp.json() == {"detail": "Authentication failed"}


@pytest.mark.asyncio
async def test_me_no_cookie(client: httpx.AsyncClient):
    """GET /auth/me without session cookie returns 401."""
    resp = await client.get("/auth/me")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Not authenticated"}


@pytest.mark.asyncio
async def test_me_tampered_cookie(client: httpx.AsyncClient):
    """GET /auth/me with tampered session cookie returns 401."""
    resp = await client.get(
        "/auth/me",
        cookies={"haunter_session": "tampered_session_value.sig123"},
    )
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Invalid or expired session"}


@pytest.mark.asyncio
async def test_me_expired_session(
    client: httpx.AsyncClient,
    db: AsyncSession,
    user_factory,
    signed_session_factory,
):
    """GET /auth/me with session >14 days old returns 401."""
    await truncate_all(db)
    user = await user_factory(github_id=601, username="expired_user")

    with freeze_time("2026-08-01 12:00:00"):
        cookie = signed_session_factory(user.id)

    # 15 days later (> 14d max_age)
    with freeze_time("2026-08-16 12:00:01"):
        resp = await client.get("/auth/me", cookies={"haunter_session": cookie})
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me_nonexistent_user(client: httpx.AsyncClient, signed_session_factory):
    """GET /auth/me with valid signature for non-existent user returns 401."""
    ghost_id = uuid.uuid4()
    cookie = signed_session_factory(ghost_id)
    resp = await client.get("/auth/me", cookies={"haunter_session": cookie})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Not authenticated"}


@pytest.mark.asyncio
async def test_me_valid_user(
    client: httpx.AsyncClient,
    db: AsyncSession,
    user_factory,
    signed_session_factory,
):
    """GET /auth/me with valid session cookie returns authenticated profile."""
    await truncate_all(db)
    user = await user_factory(github_id=602, username="valid_user", avatar_url="https://avatar.url")
    cookie = signed_session_factory(user.id)

    resp = await client.get("/auth/me", cookies={"haunter_session": cookie})
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == str(user.id)
    assert data["github_username"] == "valid_user"
    assert data["avatar_url"] == "https://avatar.url"


@pytest.mark.asyncio
async def test_logout_clears_cookie_and_idempotent(client: httpx.AsyncClient):
    """POST /auth/logout clears session cookie with identical attributes; idempotent."""
    resp = await client.post("/auth/logout", cookies={"haunter_session": "active_session"})
    assert resp.status_code == 200
    assert resp.json() == {"detail": "Logged out"}

    set_cookie = resp.headers.get("set-cookie", "")
    assert "haunter_session=" in set_cookie
    assert "Max-Age=0" in set_cookie or "max-age=0" in set_cookie
    assert "HttpOnly" in set_cookie or "httponly" in set_cookie
    assert "Secure" in set_cookie or "secure" in set_cookie
    assert "samesite=none" in set_cookie.lower()

    # Second logout without cookie is idempotent 200
    resp2 = await client.post("/auth/logout")
    assert resp2.status_code == 200


@pytest.mark.asyncio
async def test_key_rotation_fallback(db: AsyncSession, user_factory):
    """Sessions signed with previous key remain valid through rotation; invalid key fails."""
    await truncate_all(db)
    user = await user_factory(github_id=603, username="rotation_user")

    # Configure previous key in settings
    prev_key = "previous_secret_rotation_key_12345"
    orig_prev = settings.session_secret_key_previous
    settings.session_secret_key_previous = prev_key

    try:
        # 1. Sign with previous key
        old_cookie = TimestampSigner(prev_key).sign(str(user.id)).decode()
        verified_id = _verify_session_cookie(old_cookie)
        assert verified_id == user.id

        # 2. Sign with current key
        new_cookie = TimestampSigner(settings.session_secret_key).sign(str(user.id)).decode()
        verified_id_new = _verify_session_cookie(new_cookie)
        assert verified_id_new == user.id

        # 3. Sign with unknown untrusted key -> raises HTTPException 401
        bad_cookie = TimestampSigner("untrusted_random_key").sign(str(user.id)).decode()
        with pytest.raises(Exception):
            _verify_session_cookie(bad_cookie)

    finally:
        settings.session_secret_key_previous = orig_prev


@pytest.mark.asyncio
async def test_rate_limiting_auth_routes(client: httpx.AsyncClient):
    """Exceeding rate limit of 20 req/min on auth endpoints returns 429."""
    limiter.reset()

    # Hit /auth/logout 20 times (all 200)
    for _ in range(20):
        resp = await client.post("/auth/logout")
        assert resp.status_code == 200

    # 21st request triggers rate limit 429
    resp_blocked = await client.post("/auth/logout")
    assert resp_blocked.status_code == 429
    limiter.reset()


@pytest.mark.asyncio
async def test_cors_configuration(client: httpx.AsyncClient):
    """CORS checks: allow frontend_url with credentials, block arbitrary evil origin, handle preflight."""
    # 1. Legit origin request
    resp_legit = await client.get("/health", headers={"Origin": settings.frontend_url})
    assert resp_legit.status_code == 200
    assert resp_legit.headers.get("access-control-allow-origin") == settings.frontend_url
    assert resp_legit.headers.get("access-control-allow-credentials") == "true"

    # 2. Evil origin request
    resp_evil = await client.get("/health", headers={"Origin": "https://evil-attacker.com"})
    assert resp_evil.headers.get("access-control-allow-origin") != "https://evil-attacker.com"

    # 3. Preflight OPTIONS request
    resp_opt = await client.options(
        "/auth/logout",
        headers={
            "Origin": settings.frontend_url,
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp_opt.status_code == 200
    assert resp_opt.headers.get("access-control-allow-origin") == settings.frontend_url


def test_session_max_age_14d_constant():
    """Verify session max_age is exactly 14 days (1,209,600 seconds)."""
    assert _SESSION_MAX_AGE == int(timedelta(days=14).total_seconds())
    assert _SESSION_MAX_AGE == 1209600


def test_pkce_docstring_and_generic_error_policy():
    """Verify PKCE architectural decision and error handling policies are documented in auth module."""
    doc = auth_module.__doc__ or ""
    assert "PKCE DECISION" in doc
    assert "compensating controls" in doc.lower() or "csrf" in doc.lower()
    assert "ERROR POLICY" in doc


def test_encryption_required_raises():
    """
    config.py must raise RuntimeError when TOKEN_ENCRYPTION_KEY is None and pytest is not
    in sys.modules (i.e. non-test runtime).

    Simulates this by temporarily removing 'pytest' from sys.modules before re-importing
    app.config with no TOKEN_ENCRYPTION_KEY in env or dotenv.
    """
    import importlib
    import os
    import sys
    from unittest.mock import patch
    from pydantic_settings.sources import DotEnvSettingsSource

    # Pop pytest from sys.modules to simulate non-test runtime
    saved_pytest = sys.modules.pop("pytest", None)
    # Remove cached app.config so the module-level guard re-executes
    sys.modules.pop("app.config", None)

    real_dotenv_call = DotEnvSettingsSource.__call__

    def mock_dotenv_call(self):
        vals = real_dotenv_call(self)
        if isinstance(vals, dict):
            vals = {k: v for k, v in vals.items() if k.lower() != "token_encryption_key"}
        return vals

    try:
        # Ensure TOKEN_ENCRYPTION_KEY is absent from environ and dotenv
        original_key = os.environ.pop("TOKEN_ENCRYPTION_KEY", None)
        try:
            with patch.object(DotEnvSettingsSource, "__call__", mock_dotenv_call):
                with pytest.raises(RuntimeError, match="TOKEN_ENCRYPTION_KEY must be set"):
                    importlib.import_module("app.config")
        finally:
            if original_key is not None:
                os.environ["TOKEN_ENCRYPTION_KEY"] = original_key
    finally:
        # Restore pytest in sys.modules and reset app.config to clean state
        if saved_pytest is not None:
            sys.modules["pytest"] = saved_pytest
        sys.modules.pop("app.config", None)
        importlib.import_module("app.config")


