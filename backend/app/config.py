import logging
import sys
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from pydantic import AliasChoices, Field, model_validator
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

    github_auditor_app_id: Optional[str] = None
    github_auditor_app_private_key: Optional[str] = None
    audit_self_invoke_secret: Optional[str] = None

    # Where to redirect after a successful OAuth callback.
    frontend_url: str

    # Per-IP rate limit for auth endpoints (requests per minute).
    # Per-IP rate limit for sensitive auth endpoints. Bumped to a near-unlimited
    # value while the pipeline is being validated end-to-end; tighten to ~30 once
    # the dashboard is in a steady state.
    rate_limit_per_minute: int = 1000

    # LLM Provider Configuration (Phase 4)
    # OpenCode Zen API key (Secret Manager / .env only). Injected at request time, never logged.
    opencode_zen_api_key: Optional[str] = None
    opencode_zen_base_url: str = "https://opencode.ai/zen/v1"
    default_provider: str = "opencode_zen"
    default_model: str = "nemotron-3.5-lightning-free"

    # Hard ceiling for `max_tokens` on any outgoing OpenCode Zen request.
    # Subagents currently pass `max_tokens=10_000_000` for "test mode" with the
    # free API, but the free endpoints treat `max_tokens` as part of the model's
    # context budget and reject requests that exceed it (HTTP 400). The provider
    # adapter clamps whatever the subagent passes down to this value before
    # serialising the payload, so the test-mode source stays intact while
    # outgoing requests stay inside the model's accepted range.
    # 250k leaves room for ~750k input tokens on the 1M-context free-tier
    # endpoints while giving the model enough output budget for long responses
    # (full diffs, full PR bodies). Bump this when moving to a paid tier with
    # a larger context window.
    opencode_zen_max_output_tokens: int = 250_000

    # Groq LLM Provider Configuration
    # GROQ_API_KEY and GROQ_MODEL_NAME are loaded via environment variables or .env.
    # Injected per request, never logged or hardcoded in source code.
    groq_api_key: Optional[str] = None
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_model_name: Optional[str] = Field(
        default="openai/gpt-oss-120b",
        description="Default Groq model name from env",
    )

    # OpenAI Provider Configuration (Phase 3.2 Issue 3).
    # OPENAI_API_KEY / OPENAI_BASE_URL are loaded via environment variables or .env.
    # Injected per request, never logged or hardcoded in source code.
    openai_api_key: Optional[str] = None
    openai_base_url: str = "https://api.openai.com/v1"

    # Anthropic Provider Configuration (Phase 3.2 Issue 3).
    # ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL are loaded via environment variables or .env.
    # Injected per request, never logged or hardcoded in source code.
    anthropic_api_key: Optional[str] = None
    anthropic_base_url: str = "https://api.anthropic.com/v1"

    # Optional admin user UUID string for global model config switcher authorization
    admin_user_id: Optional[str] = None

    # TinyFish API Configuration (Web Search, Stealth Fetch & Agent API)
    tinyfish_api_key: Optional[str] = None
    tinyfish_base_url: str = "https://api.tinyfish.io/v1"
    tinyfish_search_url: str = "https://api.search.tinyfish.ai"
    tinyfish_fetch_url: str = "https://api.fetch.tinyfish.ai"

    # GitHub App credentials for repo-write operations (Phase 8).
    # App permissions required: contents:write, pull_requests:write — NO administration.
    # github_app_private_key is a PEM string — load from SSM / Secret Manager in prod.
    # NEVER commit the PEM or log it. If not set, installation token auth is unavailable
    # and get_installation_token() falls back to settings.github_token (dev only).
    github_app_id: Optional[str] = None
    github_app_private_key: Optional[str] = None  # full PEM, newlines preserved

    # SSM Parameter Store path (SecureString) holding the PEM for the
    # repo-write GitHub App. The Lambda path (see infra/aws/lambda.tf):
    # the ~1.6KB PEM is read at runtime via boto3 instead of being injected
    # as an env var, keeping the function under the Lambda
    # environment-variable size limit. Local dev sets the PEM directly via
    # GITHUB_APP_PRIVATE_KEY instead.
    github_app_private_key_ssm_path: str = "/haunter/GITHUB_APP_PRIVATE_KEY"

    # Phase 13 — Sandbox provider selection.
    # "github_actions" uses a Haunter-org test mirror + GitHub Actions polling
    # (see github.md). Active sandbox provider.
    sandbox_provider: str = "github_actions"

    # AWS region (used by AWSHostingAdapter for Lambda invocations).
    aws_region: str = "us-east-1"

    # GitHub Actions sandbox adapter (required when sandbox_provider="github_actions").
    # The App lives in a Haunter-owned org and writes to per-user test-mirror
    # repos there. Polling, not webhook — the test repo has no inbound webhook.
    # PEM is loaded at runtime from SSM SecureString (see github.md Phase 1.3)
    # so the private key never enters Terraform state, .env, or lambda.zip.
    github_sandbox_org: str = "haunter-sandboxes"
    github_sandbox_repo: str = "haunter-sandbox-runner"
    github_sandbox_app_id: Optional[str] = None
    github_sandbox_installation_id: Optional[str] = None
    github_sandbox_app_private_key_ssm_path: str = (
        "/haunter/GITHUB_SANDBOX_APP_PRIVATE_KEY"
    )
    github_sandbox_poll_interval_seconds: float = 10.0
    github_sandbox_poll_timeout_seconds: float = 120.0
    github_sandbox_workflow_filename_py: str = "haunter-test-py.yml"
    github_sandbox_workflow_filename_ts: str = "haunter-test-ts.yml"
    # Language packs. One template per detect_language() key; the key set is
    # app.sandbox.mirror.SUPPORTED_LANGUAGES and every value names a file in
    # app/sandbox/workflow_templates/ (asserted by tests/test_sandbox_lang_packs.py).
    github_sandbox_workflow_filename_go: str = "haunter-test-go.yml"
    github_sandbox_workflow_filename_rust: str = "haunter-test-rust.yml"
    github_sandbox_workflow_filename_java: str = "haunter-test-java.yml"
    github_sandbox_workflow_filename_docker: str = "haunter-test-docker.yml"

    # Phase 14 — Hosting provider selection.
    # "aws" uses Lambda + Function URL (always-free 1M req + 400k GB-s/mo).
    # Hot-switchable via DB (system_configs key="hosting_provider") with 60s TTL cache.
    # Allowlisted: "aws" only — never free-text, never from request headers.
    hosting_provider: str = "aws"

    # Name/ARN of the Lambda function to self-invoke for async pipeline execution.
    # Defaults to AWS_LAMBDA_FUNCTION_NAME, which the Lambda runtime injects
    # automatically and which therefore needs no Terraform support.
    #
    # AWS_LAMBDA_FUNCTION_NAME is an AWS-RESERVED key: Lambda owns it and sets it
    # itself, so Terraform must never pass it in an environment.variables block.
    # CreateFunction rejects it outright —
    #   InvalidParameterValueException: ...contains reserved keys that are
    #   currently not supported for modification. Reserved keys used in this
    #   request: AWS_LAMBDA_FUNCTION_NAME
    # HAUNTER_LAMBDA_FUNCTION_NAME is the supported, non-reserved override, for a
    # function that must target a *different* Lambda than the one it runs in (the
    # audit dispatcher schedules its children on the main function).
    #
    # Alias order is load-bearing and must stay as written. Lambda ALWAYS sets
    # AWS_LAMBDA_FUNCTION_NAME in its own runtime, so that alias is always present
    # on the dispatcher; listing it first would let the dispatcher's own name win
    # and every audit child would self-invoke back into the dispatcher.
    # Required when hosting_provider="aws". Never commit a hardcoded ARN.
    aws_lambda_function_name: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "HAUNTER_LAMBDA_FUNCTION_NAME", "aws_lambda_function_name"
        ),
    )

    # Maximum number of fix-generation attempts per Run before falling back to
    # a diagnosis-only comment. Phase 1 (BLOCKER-1 / NICE-1 / O-07): single
    # source of truth, default lowered from 10 to 3. Tighter cap means a stuck
    # LLM loop cannot burn the full Lambda 900s budget before the orchestrator
    # wall-clock timeout fires. Override per environment via HAUNTER_MAX_ATTEMPTS
    # (e.g. HAUNTER_MAX_ATTEMPTS=2 for very strict demo runs).
    max_attempts: int = Field(
        default=3,
        validation_alias=AliasChoices("max_attempts", "HAUNTER_MAX_ATTEMPTS"),
    )

    # NICE-3: cap on the number of files Haunter seeds from the user's failing
    # commit into the test mirror. Raised from 50 → 500 so large repos (e.g.
    # UpGrade at 343 files) don't have their pytest configs and test directories
    # dropped by the alphabetical-sort truncation (Fix 1). The seeder now uses
    # priority-based ordering (config/test files first) so the effective number
    # of *useful* files is much lower than the raw cap.
    # Values >200 may approach GitHub Actions runner time limits for large suites;
    # revisit if average run time exceeds 3 min.
    seed_max_files: int = Field(
        default=500,
        validation_alias=AliasChoices("seed_max_files", "HAUNTER_SEED_MAX_FILES"),
    )

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Feature 8 — retention windows for `webhook_deliveries`.
    #
    # ROW retention and PAYLOAD retention are separate knobs on purpose. The row
    # is the audit record the health tab queries (`event` / `status` / `reason` /
    # `delivery_id` / `repo_id`); the payload is the replay buffer and is ~99% of
    # the bytes a delivery costs. A single window would force a choice between
    # dropping the audit trail early and paying for the bodies forever, so the
    # sweeper (app.services.webhook_retention) NULLs the payload on the short
    # window and DELETEs the row on the long one.
    #
    # Default payload window matches the 90-day range cap the traces API already
    # enforces (app/routers/traces.py RunListParams), so "how far back can a user
    # look" stays one mental model. The row window is a full year: the row is
    # ~100 bytes, so a year of history costs nothing measurable while still
    # bounding the table (see the growth model in issue #36).
    webhook_payload_retention_days: int = Field(
        default=90,
        gt=0,
        validation_alias=AliasChoices(
            "webhook_payload_retention_days", "HAUNTER_WEBHOOK_PAYLOAD_RETENTION_DAYS"
        ),
    )
    webhook_row_retention_days: int = Field(
        default=365,
        gt=0,
        validation_alias=AliasChoices(
            "webhook_row_retention_days", "HAUNTER_WEBHOOK_ROW_RETENTION_DAYS"
        ),
    )
    # Each sweep runs bounded batches instead of one unbounded statement: a
    # single UPDATE/DELETE over the whole table would take a long-lived row lock
    # and produce a large amount of dead tuples in one transaction, stalling
    # concurrent inserts from webhook ingestion. Batches are index-friendly on
    # ix_webhook_deliveries_created_at and each one commits on its own.
    webhook_retention_sweep_batch_size: int = Field(
        default=1000,
        gt=0,
        validation_alias=AliasChoices(
            "webhook_retention_sweep_batch_size",
            "HAUNTER_WEBHOOK_RETENTION_SWEEP_BATCH_SIZE",
        ),
    )
    # Hard ceiling on batches per sweep, so one invocation can drain at most
    # batch_size * max_batches rows and cannot run unbounded if a predicate
    # ever matches more than expected. A daily cron at 1000 x 20 clears 20k
    # rows/day, three orders of magnitude above the observed write rate.
    webhook_retention_sweep_max_batches: int = Field(
        default=20,
        gt=0,
        validation_alias=AliasChoices(
            "webhook_retention_sweep_max_batches",
            "HAUNTER_WEBHOOK_RETENTION_SWEEP_MAX_BATCHES",
        ),
    )

    @model_validator(mode="after")
    def _validate_webhook_retention_windows(self) -> "Settings":
        """Reject a row window shorter than the payload window.

        Nothing breaks if it is — the delete predicate is a superset of the
        NULL-out predicate, so the rows would simply vanish instead of being
        kept — but it silently discards the audit history the payload window was
        deliberately separate to preserve. Fail at boot instead of at sweep time.
        """
        if self.webhook_row_retention_days < self.webhook_payload_retention_days:
            raise ValueError(
                "webhook_row_retention_days must be >= webhook_payload_retention_days: "
                "the row is the audit record and must outlive the payload it carries"
            )
        return self

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
if settings.token_encryption_key is None and "pytest" not in sys.modules:
    raise RuntimeError(
        "TOKEN_ENCRYPTION_KEY must be set — users.access_token would be stored as "
        "plaintext at rest in Neon Postgres (backend/app/auth.py:148). "
        "Generate with: "
        'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
    )


# Sentinel values that mean "operator never filled this in". Matched
# case-insensitively so both REPLACE_ME and replace-me are caught.
_PLACEHOLDER_SENTINELS: frozenset[str] = frozenset(
    {"replace_me", "replace-me", "placeholder"}
)

# Fields holding an opaque credential. A sentinel appearing anywhere in the value
# means the .env.example default was never replaced, so substring matching is both
# safe and correct here: a real GitHub client id or Fernet key is a fixed-shape
# token that cannot legitimately contain a sentinel.
# Field name -> operator-facing env var name, for the error message only. Values
# are never interpolated into the exception — a startup error that echoes a
# credential leaks it into logs and crash reporters.
_PLACEHOLDER_CREDENTIAL_FIELDS: dict[str, str] = {
    "github_client_id": "GITHUB_CLIENT_ID",
    "github_client_secret": "GITHUB_CLIENT_SECRET",
    "session_secret_key": "SESSION_SECRET_KEY",
    "token_encryption_key": "TOKEN_ENCRYPTION_KEY",
}

# Fields holding a URL. Only an exact sentinel match counts: a hostname or path
# may legitimately contain "placeholder" (e.g. an internal
# https://placeholder.example.com origin), and substring matching here would stop
# a correctly configured deployment from booting.
_PLACEHOLDER_URL_FIELDS: dict[str, str] = {
    "callback_url": "CALLBACK_URL",
    "frontend_url": "FRONTEND_URL",
}


def _is_unusable_secret(value: object, *, exact_only: bool) -> bool:
    """
    True when a configured value is unusable as a credential.

    An absent (None) optional secret is not judged here — TOKEN_ENCRYPTION_KEY has
    its own fail-closed guard above, which must keep ownership of the None case so
    its error message stays specific.

    A blank or whitespace-only value IS unusable: itsdangerous would sign sessions
    with an empty key, and authlib would build an OAuth URL with an empty
    client_id — both failures that surface far from their cause.
    """
    if value is None:
        return False
    if not isinstance(value, str):
        return True
    normalized = value.strip().lower()
    if not normalized:
        return True
    if exact_only:
        return normalized in _PLACEHOLDER_SENTINELS
    return any(sentinel in normalized for sentinel in _PLACEHOLDER_SENTINELS)


def _find_placeholder_credential() -> str | None:
    """
    Return the env var name whose value is still unusable — blank or left at a
    .env.example placeholder — or None when every guarded field is usable.
    """
    for field, env_name in _PLACEHOLDER_CREDENTIAL_FIELDS.items():
        if _is_unusable_secret(getattr(settings, field, None), exact_only=False):
            return env_name
    for field, env_name in _PLACEHOLDER_URL_FIELDS.items():
        if _is_unusable_secret(getattr(settings, field, None), exact_only=True):
            return env_name
    return None


# Fail closed on unusable credentials at non-test startup.
# Without this, a placeholder client_id is passed straight into the OAuth
# authorization URL (app/auth.py), GitHub returns an opaque 404 for the unknown
# client_id, and the operator sees a dead login button with no signal about the
# real cause. A boot-time error naming the offending env var is strictly more
# actionable than a 404 discovered mid-login-flow.
# Uses the same sys.modules pytest probe as the TOKEN_ENCRYPTION_KEY guard above:
# the test suite legitimately runs against placeholder values, and a hard startup
# failure would take out the whole suite at import time rather than one test.
if _placeholder_credential := _find_placeholder_credential():
    if "pytest" not in sys.modules:
        raise RuntimeError(
            f"{_placeholder_credential} is missing, blank, or still set to a "
            "placeholder value, so GitHub OAuth login cannot start. Register a "
            "GitHub OAuth App with read:user and repo scopes, set its "
            "authorization callback URL to exactly match CALLBACK_URL, then set "
            f"the real {_placeholder_credential} in the active environment or "
            "deployment configuration (backend/.env for local development) and "
            "restart."
        )
    logger.warning(
        "%s is missing, blank, or a placeholder value — OAuth login will fail "
        "against GitHub. Test context only.",
        _placeholder_credential,
    )
