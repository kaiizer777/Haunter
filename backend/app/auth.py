"""
GitHub OAuth authentication for Haunter (FastAPI-native, no Better Auth).

Design decisions documented here:

AUTH ARCHITECTURE
-----------------
- We use a SEPARATE GitHub OAuth App (read:user scope only) purely for login.
  The GitHub App used for repo installation / webhook reception (Phase 3+) is a
  distinct credential with repo-admin scope. Login must never request destructive
  scopes — least-privilege principle.
- Session is a signed, httpOnly, Secure, SameSite=Lax cookie containing only the
  user UUID (itsdangerous TimestampSigner, 14-day max-age). No JWT, no JWKS.
- Cookie verification is done entirely inside FastAPI via get_current_user; the
  Next.js frontend never sees or stores auth state beyond calling /auth/me.

CSRF / STATE PROTECTION
-----------------------
- GET /auth/login generates a cryptographically random state value, signs it with
  TimestampSigner, and stores it in a short-lived (10min) httpOnly cookie
  (haunter_oauth_state). The raw (unsigned) state is sent to GitHub as the state
  parameter. On callback, the signed cookie is verified and constant-time compared
  before any code exchange. The state cookie is deleted immediately after check
  (single-use). Mismatch/missing/expired → generic 400 before touching the code.

REDIRECT_URI
------------
- Both login and callback use settings.callback_url — a hardcoded exact-match value
  from server config. Never derived from request Host header, X-Forwarded-Host, or
  any client-supplied parameter. Prevents open-redirect / OAuth token theft via
  manipulated redirect_uri.

PKCE DECISION
-------------
- GitHub OAuth Apps do not mandate PKCE (as of 2024). authlib's AsyncOAuth2Client
  does not natively chain S256 code_verifier into GitHub's OAuth App flow without
  manual header injection. Compensating controls in place:
    1. Single-use signed state cookie with 10min TTL (CSRF protection).
    2. Code-for-token exchange is server-side with client_secret (confidential client).
    3. redirect_uri is hardcoded from server config — no client influence.
  Accepted risk is explicitly documented. Revisit if GitHub mandates PKCE for OAuth Apps.

COOKIE PREFIX (__Host-)
-----------------------
- NOT used. Reason: API (Cloud Run / localhost:8000) and frontend (localhost:3000 /
  separate domain) are on different origins. __Host- requires Secure, no Domain
  attribute, and Path=/. When the API and frontend are on separate origins, the browser
  sends the API cookie to the API origin automatically without a Domain attribute —
  __Host- would be compatible here BUT requires the cookie to only be sent over HTTPS.
  During local dev (http://localhost) Secure cookies are rejected by browsers, breaking
  the flow. For the Cloud Run + separate-frontend topology this flag is revisitable post-
  deploy when both endpoints are on HTTPS. Standard haunter_session (no prefix) with
  Secure;HttpOnly;SameSite=Lax;Path=/ is correct for this topology.

KEY ROTATION
------------
- SESSION_SECRET_KEY_PREVIOUS env var enables zero-downtime key rotation.
  get_current_user tries the current key first, falls back to the previous key on
  verify failure. Once all old cookies have expired (14d), the previous key can be
  dropped from config.

TOKEN AT REST
-------------
- users.access_token is encrypted with Fernet (TOKEN_ENCRYPTION_KEY env var) before
  INSERT/UPDATE and decrypted at point of use.
  TODO(security): encrypt access_token before prod — pre-prod security blocker.
  If TOKEN_ENCRYPTION_KEY is not set, the token is stored plaintext and a startup
  WARNING is emitted by config.py. Never log the token value.

ERROR POLICY
------------
- ALL auth error paths return a generic {"detail": "..."} to the client.
  Full detail (minus secrets) goes to server-side logs only.
  code, access_token, and SESSION_SECRET_KEY values are NEVER logged.
"""

import hmac
import logging
import secrets
import uuid
from datetime import timedelta
from typing import Annotated

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.limiter import limiter
from app.models import User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SESSION_COOKIE_NAME = "haunter_session"
_STATE_COOKIE_NAME = "haunter_oauth_state"
_SESSION_MAX_AGE = int(timedelta(days=14).total_seconds())   # 14 days — enforced on every read
_STATE_MAX_AGE = 600                                          # 10 minutes — OAuth state is short-lived

_GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
_GITHUB_USER_URL = "https://api.github.com/user"

# Rate limit string evaluated at import time from settings.
# slowapi accepts a string like "20/minute" — no lambda needed.
_RATE_LIMIT = f"{settings.rate_limit_per_minute}/minute"


# ---------------------------------------------------------------------------
# Fernet token encryption (at-rest protection for users.access_token)
# TODO(security): encrypt access_token before prod — pre-prod security blocker.
# ---------------------------------------------------------------------------

def _get_fernet():
    """Return a Fernet instance if TOKEN_ENCRYPTION_KEY is configured, else None."""
    if settings.token_encryption_key is None:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(settings.token_encryption_key.encode())
    except Exception:
        logger.error("TOKEN_ENCRYPTION_KEY is set but invalid — cannot initialize Fernet.")
        raise


def _encrypt_token(token: str) -> str:
    """
    Encrypt the access token before storing. If TOKEN_ENCRYPTION_KEY is not set,
    returns plaintext — this is the pre-prod blocker path (warned at startup).
    NEVER log the token value or the encrypted bytes.
    """
    fernet = _get_fernet()
    if fernet is None:
        # TODO(security): encrypt access_token before prod — TOKEN_ENCRYPTION_KEY not set.
        return token
    return fernet.encrypt(token.encode()).decode()


def _decrypt_token(stored: str) -> str:
    """
    Decrypt access token for use in GitHub API calls.
    If TOKEN_ENCRYPTION_KEY is not set, returns stored value as-is (plaintext path).
    Raises ValueError on decryption failure — caller must handle and not leak detail.
    NEVER log the return value.
    """
    fernet = _get_fernet()
    if fernet is None:
        return stored
    try:
        return fernet.decrypt(stored.encode()).decode()
    except Exception as exc:
        raise ValueError("Token decryption failed") from exc


# ---------------------------------------------------------------------------
# Signer helpers — key rotation support
# ---------------------------------------------------------------------------

def _signers() -> list[TimestampSigner]:
    """
    Returns ordered list of TimestampSigners: current key first, previous key second.
    Verification tries each in order so rotating SESSION_SECRET_KEY doesn't force-logout
    all users — old sessions signed with the previous key remain valid for their max_age.
    Once all old cookies expire (14d), SESSION_SECRET_KEY_PREVIOUS can be removed.
    """
    pool = [TimestampSigner(settings.session_secret_key)]
    if settings.session_secret_key_previous:
        pool.append(TimestampSigner(settings.session_secret_key_previous))
    return pool


def _sign_user_id(user_id: uuid.UUID) -> str:
    """Sign user_id with the current (primary) key."""
    return TimestampSigner(settings.session_secret_key).sign(str(user_id)).decode()


def _verify_session_cookie(raw: str) -> uuid.UUID:
    """
    Try each signer in rotation order. Returns UUID on success.
    Raises HTTPException(401) on any invalid/expired/tampered cookie.
    Never logs the raw cookie value.
    """
    for signer in _signers():
        try:
            value = signer.unsign(raw, max_age=_SESSION_MAX_AGE).decode()
            return uuid.UUID(value)
        except (SignatureExpired, BadSignature, ValueError):
            continue
    raise HTTPException(status_code=401, detail="Invalid or expired session")


def _sign_state(raw_state: str) -> str:
    """Sign the raw OAuth state value with current key for storage in cookie."""
    return TimestampSigner(settings.session_secret_key).sign(raw_state).decode()


def _verify_state_cookie(signed_cookie: str, request_state: str) -> None:
    """
    Validates the OAuth state parameter against the signed cookie.
    Tries the full _signers() pool (current + previous key) so state cookies
    issued before a key rotation remain valid for their 10min TTL.
    Performs constant-time comparison after signature verification.
    Raises HTTPException(400) generic on ANY failure — before code exchange.
    Never logs signed_cookie or request_state values.
    """
    recovered_state: str | None = None
    for signer in _signers():
        try:
            recovered_state = signer.unsign(signed_cookie, max_age=_STATE_MAX_AGE).decode()
            break  # verified with this key — stop trying
        except (SignatureExpired, BadSignature, ValueError):
            continue

    if recovered_state is None:
        logger.warning("OAuth state cookie signature verification failed")
        raise HTTPException(status_code=400, detail="Authentication failed")

    # Constant-time compare the raw state from GitHub against the one we generated.
    if not hmac.compare_digest(recovered_state.encode(), request_state.encode()):
        logger.warning("OAuth state mismatch — possible CSRF attempt")
        raise HTTPException(status_code=400, detail="Authentication failed")


# ---------------------------------------------------------------------------
# Cookie helpers
# ---------------------------------------------------------------------------

def _set_session_cookie(response: Response, user_id: uuid.UUID) -> None:
    """
    Set the session cookie with full security attributes.
    Attributes must be identical to those used in _clear_session_cookie — browsers
    only remove a cookie when the delete request matches the original attributes exactly.
    """
    response.set_cookie(
        key=_SESSION_COOKIE_NAME,
        value=_sign_user_id(user_id),
        max_age=_SESSION_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    """
    Delete the session cookie using IDENTICAL attributes to _set_session_cookie.
    Mismatched attributes (path, samesite, secure) cause browsers to leave a stale
    cookie alive — this must stay in sync with _set_session_cookie.
    """
    response.delete_cookie(
        key=_SESSION_COOKIE_NAME,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def _set_state_cookie(response: Response, signed_state: str) -> None:
    """Short-lived state cookie — httpOnly, 10min TTL, deleted after use."""
    response.set_cookie(
        key=_STATE_COOKIE_NAME,
        value=signed_state,
        max_age=_STATE_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def _clear_state_cookie(response: Response) -> None:
    """Immediately invalidate the state cookie after use (single-use semantics)."""
    response.delete_cookie(
        key=_STATE_COOKIE_NAME,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


# ---------------------------------------------------------------------------
# Dependency: get_current_user
# ---------------------------------------------------------------------------


class UserOut(BaseModel):
    id: uuid.UUID
    github_username: str
    avatar_url: str | None
    is_admin: bool = False

    model_config = {"from_attributes": True}


async def get_current_user(
    db: Annotated[AsyncSession, Depends(get_db)],
    haunter_session: Annotated[str | None, Cookie()] = None,
) -> User:
    """
    FastAPI dependency. Reads the signed session cookie, verifies signature + max_age
    (14d, enforced by itsdangerous), and returns the User ORM object.
    Raises 401 if cookie is missing, invalid, expired, or the user no longer exists.
    Supports key rotation via _signers() pool — old sessions remain valid through rotation.
    """
    if haunter_session is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user_id = _verify_session_cookie(haunter_session)

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    return user


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/login")
@limiter.limit(_RATE_LIMIT)
async def login(request: Request, response: Response) -> RedirectResponse:
    """
    Redirect the browser to GitHub's OAuth authorization page.

    Generates a cryptographically random state, signs it with TimestampSigner,
    stores the signed value in a short-lived (10min) httpOnly cookie, and passes
    the raw state to GitHub. The redirect_uri is hardcoded from settings.callback_url
    — never derived from request Host or any client-supplied value.
    """
    raw_state = secrets.token_urlsafe(32)
    signed_state = _sign_state(raw_state)

    client = AsyncOAuth2Client(
        client_id=settings.github_client_id,
        scope="read:user",
        redirect_uri=settings.callback_url,  # hardcoded from config — never from Host header
        state=raw_state,
    )
    url, _ = client.create_authorization_url(_GITHUB_AUTHORIZE_URL)

    redirect = RedirectResponse(url, status_code=302)
    _set_state_cookie(redirect, signed_state)
    return redirect


@router.get("/callback")
@limiter.limit(_RATE_LIMIT)
async def callback(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    haunter_oauth_state: Annotated[str | None, Cookie()] = None,
) -> Response:
    """
    GitHub redirects here with ?code=&state=.

    Security order is strict:
    1. Validate state (signature + max_age + constant-time compare) — BEFORE code exchange.
    2. Clear state cookie immediately on ALL paths (single-use — no 10-min replay window).
    3. Exchange code for token using hardcoded redirect_uri from settings.callback_url.
    4. Fetch GitHub user profile.
    5. Upsert user, encrypt access_token, set session cookie.
    6. Redirect to FRONTEND_URL.

    All error paths return Response objects (not HTTPException) so that _clear_state_cookie
    can be attached to the actual HTTP response the browser receives — not just the success path.
    code and access_token values are NEVER logged.
    """
    request_state = request.query_params.get("state")
    request_code = request.query_params.get("code")

    def _error(msg: str, status: int = 400) -> Response:
        """
        Build an error response with the state cookie cleared.
        Returning a Response (not raising HTTPException) is the only way to attach
        Set-Cookie headers to error responses from inside a FastAPI route handler.
        """
        logger.warning("OAuth callback error: %s", msg)
        resp = Response(
            content='{"detail":"Authentication failed"}',
            status_code=status,
            media_type="application/json",
        )
        _clear_state_cookie(resp)
        return resp

    # --- Step 1: Validate state BEFORE touching the code ---
    if haunter_oauth_state is None or request_state is None:
        return _error("missing state cookie or state param")

    # _verify_state_cookie raises HTTPException on failure. Catch and convert to
    # a Response so the state cookie clear is included in the error response.
    try:
        _verify_state_cookie(haunter_oauth_state, request_state)
    except HTTPException:
        return _error("state verification failed")

    if not request_code:
        return _error("missing code param")

    # --- Step 3: Exchange code for access token ---
    try:
        async with AsyncOAuth2Client(
            client_id=settings.github_client_id,
            client_secret=settings.github_client_secret,
            redirect_uri=settings.callback_url,  # hardcoded — never from request
        ) as client:
            token_response = await client.fetch_token(
                _GITHUB_TOKEN_URL,
                code=request_code,
                grant_type="authorization_code",
            )
            access_token: str = token_response["access_token"]
            # access_token is NEVER logged — only used for GitHub API call below.
    except Exception:
        return _error("code exchange failed (detail redacted)")

    # --- Step 4: Fetch GitHub user profile ---
    try:
        async with httpx.AsyncClient() as http:
            gh_resp = await http.get(
                _GITHUB_USER_URL,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
            gh_resp.raise_for_status()
            gh_user = gh_resp.json()
    except Exception:
        return _error("GitHub user fetch failed (detail redacted)")

    github_id: int = gh_user["id"]
    github_username: str = gh_user["login"]
    avatar_url: str | None = gh_user.get("avatar_url")

    # --- Step 5: Upsert user with encrypted token ---
    encrypted_token = _encrypt_token(access_token)

    try:
        result = await db.execute(select(User).where(User.github_id == github_id))
        user = result.scalar_one_or_none()

        if user is None:
            user = User(
                github_id=github_id,
                github_username=github_username,
                avatar_url=avatar_url,
                access_token=encrypted_token,
            )
            db.add(user)
        else:
            user.github_username = github_username
            user.avatar_url = avatar_url
            user.access_token = encrypted_token

        await db.commit()
        await db.refresh(user)
    except Exception:
        logger.exception("OAuth callback: DB upsert failed")
        return _error("DB upsert failed")

    # --- Step 6: Set session cookie, clear state cookie (single-use), redirect ---
    redirect = RedirectResponse(settings.frontend_url, status_code=302)
    _set_session_cookie(redirect, user.id)
    _clear_state_cookie(redirect)
    return redirect


@router.post("/logout")
@limiter.limit(_RATE_LIMIT)
async def logout(request: Request, response: Response) -> dict[str, str]:
    """
    Clear the session cookie.
    Uses IDENTICAL attributes to _set_session_cookie so browsers actually remove it.
    """
    _clear_session_cookie(response)
    return {"detail": "Logged out"}


@router.get("/me", response_model=UserOut)
async def me(
    current_user: Annotated[User, Depends(get_current_user)],
) -> UserOut:
    """Return the authenticated user's public profile, or 401 if not logged in."""
    is_admin = bool(settings.admin_user_id and str(current_user.id) == settings.admin_user_id)
    return UserOut(
        id=current_user.id,
        github_username=current_user.github_username,
        avatar_url=current_user.avatar_url,
        is_admin=is_admin,
    )
