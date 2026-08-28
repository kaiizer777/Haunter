import logging
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

    # GitHub Webhook secret for HMAC-SHA256 signature verification (X-Hub-Signature-256).
    # Separate from OAuth App credentials — least privilege.
    github_webhook_secret: Optional[str] = None

    # GitHub Personal Access Token or App Installation Token for REST client calls (Phase 3+).
    # Used by backend/app/github_client.py. Never logged or stored in run rows.
    github_token: Optional[str] = None

    # Where to redirect after a successful OAuth callback.
    frontend_url: str

    # Per-IP rate limit for auth endpoints (requests per minute).
    rate_limit_per_minute: int = 20

    # LLM Provider Configuration (Phase 4)
    # OpenCode Zen API key (Secret Manager / .env only). Injected at request time, never logged.
    opencode_zen_api_key: Optional[str] = None
    opencode_zen_base_url: str = "https://opencode.ai/zen/v1"
    default_provider: str = "opencode_zen"
    default_model: str = "nemotron-3.5-lightning-free"

    # Optional admin user UUID string for global model config switcher authorization
    admin_user_id: Optional[str] = None

    # GCP project ID for Cloud Build sandbox (Phase 7).
    # Required when Cloud Build verification is enabled.
    gcp_project_id: Optional[str] = None

    # GitHub App credentials for repo-write operations (Phase 8).
    # App permissions required: contents:write, pull_requests:write — NO administration.
    # github_app_private_key is a PEM string — load from GCP Secret Manager in prod.
    # NEVER commit the PEM or log it. If not set, installation token auth is unavailable
    # and get_installation_token() falls back to settings.github_token (dev only).
    github_app_id: Optional[str] = None
    github_app_private_key: Optional[str] = None  # full PEM, newlines preserved

    # Phase 13 — Sandbox provider selection.
    # "gcp" uses Cloud Build (default, Phase 7). "aws" uses CodeBuild adapter.
    sandbox_provider: str = "gcp"

    # AWS CodeBuild adapter (required when sandbox_provider="aws").
    # aws_codebuild_project_name: pre-provisioned CodeBuild project (see infra/aws/).
    # aws_region: AWS region the CodeBuild project lives in.
    aws_codebuild_project_name: Optional[str] = None
    aws_region: str = "us-east-1"

    # Phase 14 — Hosting provider selection.
    # "gcp" uses Cloud Run (default, HAUNTER.md:147).
    # "aws" uses Lambda + Function URL (always-free 1M req + 400k GB-s/mo).
    # Hot-switchable via DB (system_configs key="hosting_provider") with 60s TTL cache.
    # Allowlisted: "gcp" | "aws" only — never free-text, never from request headers.
    hosting_provider: str = "gcp"

    # Name/ARN of the Lambda function to self-invoke for async pipeline execution.
    # Defaults to AWS_LAMBDA_FUNCTION_NAME env var (set automatically by Lambda runtime).
    # Required when hosting_provider="aws". Never commit a hardcoded ARN.
    aws_lambda_function_name: Optional[str] = None

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


    @property
    def async_database_url(self) -> str:
        return _to_asyncpg_url(self.database_url)

    @property
    def async_database_url_unpooled(self) -> str:
        return _to_asyncpg_url(self.database_url_unpooled)


settings = Settings()

# Enforce token encryption at startup — fail closed in non-test environments.
# Detects pytest by checking sys.modules (pytest is imported before any conftest/module import),
# which is more reliable than PYTEST_CURRENT_TEST (set after collection starts).
import sys as _sys
if settings.token_encryption_key is None and "pytest" not in _sys.modules:
    raise RuntimeError(
        "TOKEN_ENCRYPTION_KEY must be set — users.access_token would be stored as "
        "plaintext at rest in Neon Postgres (backend/app/auth.py:148). "
        "Generate with: "
        'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
    )
del _sys
