import logging
import warnings
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


def _to_asyncpg_url(url: str) -> str:
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    parsed = urlparse(url)
    if parsed.query:
        qs = parse_qsl(parsed.query, keep_blank_values=True)
        filtered = [(k, v) for k, v in qs if k not in ("sslmode", "channel_binding")]
        parsed = parsed._replace(query=urlencode(filtered))
        url = urlunparse(parsed)
    return url


class Settings(BaseSettings):
    database_url: str
    database_url_unpooled: str

    # GitHub OAuth App credentials (login only — read:user scope).
    # SEPARATE from the GitHub App used for repo/webhook installation (Phase 3+).
    # Reason: Phase 3 needs repo-admin scope; login must never request it — least privilege.
    github_client_id: str
    github_client_secret: str

    # Hardcoded redirect_uri for GitHub OAuth callback.
    # Never derived from request Host header or any client-supplied parameter.
    # Prevents open-redirect / OAuth token theft via manipulated redirect_uri.
    callback_url: str

    # Secret used to sign the httpOnly session cookie (itsdangerous TimestampSigner).
    session_secret_key: str

    # Optional previous signing key for zero-downtime key rotation.
    # get_current_user tries current key first, falls back to this on verify failure.
    session_secret_key_previous: Optional[str] = None

    # Fernet key for encrypting users.access_token at rest.
    # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # If not set, access_token is stored plaintext — pre-prod security blocker.
    token_encryption_key: Optional[str] = None

    # Where to redirect after a successful OAuth callback.
    frontend_url: str

    # Per-IP rate limit for auth endpoints (requests per minute).
    rate_limit_per_minute: int = 20

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def async_database_url(self) -> str:
        return _to_asyncpg_url(self.database_url)

    @property
    def async_database_url_unpooled(self) -> str:
        return _to_asyncpg_url(self.database_url_unpooled)


settings = Settings()

# Startup warning if token encryption is not configured — pre-prod blocker.
if settings.token_encryption_key is None:
    warnings.warn(
        "TOKEN_ENCRYPTION_KEY is not set. users.access_token will be stored as plaintext. "
        "This is a pre-prod security blocker — encrypt before deploying to production.",
        stacklevel=1,
    )
