"""Custom LLM provider registry for Haunter (issue #205).

Stores user-defined OpenAI- or Anthropic-compatible providers: a display
name, a compat mode, a server-validated base_url, and an encrypted API-key
reference. Secrets are encrypted at rest with the same Fernet helper used
for GitHub OAuth tokens (app.auth) and are redacted in every response —
the plaintext key is only decrypted in-memory for outbound discovery calls
and is never logged.
"""

import logging
import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Optional

from sqlalchemy import JSON, String, Text, UniqueConstraint, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.auth import _decrypt_token, _encrypt_token
from app.llm.discovery import discover_provider_models, validate_discovery_url
from app.models import Base

logger = logging.getLogger(__name__)


class CustomProviderCompat(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"


class CustomProvider(Base):
    """A user-registered third-party model endpoint.

    Idempotency key is (name, base_url): re-registering the same pair updates
    the existing row instead of creating a duplicate.
    """

    __tablename__ = "custom_providers"
    __table_args__ = (
        UniqueConstraint("name", "base_url", name="uq_custom_provider_name_base_url"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    compat: Mapped[str] = mapped_column(String(16), nullable=False)
    base_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    api_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    models_json: Mapped[Any] = mapped_column(JSON, nullable=True)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, default=lambda: datetime.now(timezone.utc)
    )


def redact_secret(value: str) -> str:
    """Return a redacted placeholder; the secret value never leaves the server."""
    if not value:
        return ""
    return "****" + value[-4:] if len(value) > 4 else "****"


def to_public_dict(provider: CustomProvider) -> dict[str, Any]:
    """Serialize a provider for API responses — secret redacted, never plaintext."""
    return {
        "id": str(provider.id),
        "name": provider.name,
        "compat": provider.compat,
        "base_url": provider.base_url,
        "api_key_ref": redact_secret(
            _decrypt_token(provider.api_key_encrypted)
            if provider.api_key_encrypted
            else ""
        ),
        "models": list(provider.models_json or []),
        "created_at": provider.created_at.isoformat()
        if provider.created_at
        else None,
    }


async def upsert_custom_provider(
    session: Any,
    *,
    name: str,
    compat: str,
    base_url: str,
    api_key: str,
    created_by: uuid.UUID | None = None,
) -> tuple[CustomProvider, bool]:
    """Create or update a custom provider. Returns (provider, created).

    Idempotent on (name, base_url): an existing row is updated in place.
    Raises ValueError on invalid compat, bad base_url (SSRF guard), or
    missing/blank fields. The api_key is encrypted before storage.
    """
    from sqlalchemy import select

    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("name must not be blank")
    try:
        mode = CustomProviderCompat(compat)
    except ValueError:
        raise ValueError("compat must be 'openai' or 'anthropic'") from None
    if not (api_key or "").strip():
        raise ValueError("api_key must not be blank")
    normalized_url = validate_discovery_url(base_url)

    existing = (
        await session.execute(
            select(CustomProvider).where(
                CustomProvider.name == clean_name,
                CustomProvider.base_url == normalized_url,
            )
        )
    ).scalar_one_or_none()

    encrypted = _encrypt_token(api_key.strip())
    if existing is not None:
        existing.compat = mode.value
        existing.api_key_encrypted = encrypted
        await session.flush()
        return existing, False

    provider = CustomProvider(
        name=clean_name,
        compat=mode.value,
        base_url=normalized_url,
        api_key_encrypted=encrypted,
        models_json=[],
        created_by=created_by,
    )
    session.add(provider)
    await session.flush()
    return provider, True


async def refresh_custom_provider_models(
    session: Any,
    provider: CustomProvider,
) -> list[str]:
    """Run compat-mode discovery and persist the model list on the provider.

    OpenAI mode queries GET {base_url}/models; Anthropic mode has no list
    endpoint so discovery degrades to the stored manual-entry list ([] when
    nothing was entered). Failures degrade the same way without raising.
    """
    from sqlalchemy import select

    if provider.compat == CustomProviderCompat.ANTHROPIC.value:
        return list(provider.models_json or [])
    try:
        api_key = _decrypt_token(provider.api_key_encrypted)
    except ValueError:
        logger.warning("custom provider refresh: stored secret undecryptable")
        return list(provider.models_json or [])
    discovered = await discover_provider_models(
        provider.base_url, provider.compat, api_key=api_key
    )
    if discovered:
        provider.models_json = discovered
        await session.flush()
        return list(discovered)
    stored = (
        await session.execute(
            select(CustomProvider.models_json).where(
                CustomProvider.id == provider.id
            )
        )
    ).scalar_one_or_none()
    return list(stored or provider.models_json or [])
