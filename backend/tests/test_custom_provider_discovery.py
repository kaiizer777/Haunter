"""Tests for custom-provider discovery and registry (issue #205).

Covers:
1. OpenAI-compat discovery parses {"data": [...]} from GET {base}/models.
2. Anthropic-compat discovery degrades to manual entry (no HTTP call).
3. Discovery failure (500) degrades to [] without raising.
4. SSRF guards reject non-http(s), loopback, private, and metadata targets.
5. Secrets are encrypted at rest and redacted in public serialization.
6. upsert_custom_provider is idempotent on (name, base_url) and validates input.
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx

from app.llm.custom_provider import (
    CustomProvider,
    redact_secret,
    refresh_custom_provider_models,
    to_public_dict,
    upsert_custom_provider,
)
from app.llm.discovery import (
    discover_provider_models,
    validate_discovery_url,
)


def _mock_session(existing=None) -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = existing
    session.execute.return_value = result
    return session


def _provider(**kwargs) -> CustomProvider:
    from app.auth import _encrypt_token

    kwargs.setdefault("compat", "openai")
    kwargs.setdefault("base_url", "https://models.example.com/v1")
    kwargs.setdefault("api_key_encrypted", _encrypt_token("secret-key-12345"))
    kwargs.setdefault("models_json", [])
    p = CustomProvider(name="acme", **kwargs)
    return p


# ---------------------------------------------------------------------------
# Generic discovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_discover_openai_compat_parses_data_models() -> None:
    route = respx.get("https://models.example.com/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "acme-large"}, {"id": "acme-small"}]},
        )
    )
    result = await discover_provider_models(
        "https://models.example.com/v1", "openai", api_key="k"
    )
    assert route.call_count == 1
    assert result == ["acme-large", "acme-small"]


@pytest.mark.asyncio
@respx.mock
async def test_discover_anthropic_compat_no_http_manual_entry() -> None:
    route = respx.get("https://models.example.com/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "x"}]})
    )
    result = await discover_provider_models(
        "https://models.example.com/v1", "anthropic", api_key="k"
    )
    assert route.call_count == 0
    assert result == []


@pytest.mark.asyncio
@respx.mock
async def test_discover_http_500_degrades_to_empty() -> None:
    route = respx.get("https://models.example.com/v1/models").mock(
        return_value=httpx.Response(500, text="boom")
    )
    result = await discover_provider_models(
        "https://models.example.com/v1", "openai", api_key="k"
    )
    assert route.call_count == 1
    assert result == []


@pytest.mark.asyncio
async def test_refresh_anthropic_returns_manual_list() -> None:
    provider = _provider(compat="anthropic", models_json=["manual-model-1"])
    result = await refresh_custom_provider_models(_mock_session(), provider)
    assert result == ["manual-model-1"]


@pytest.mark.asyncio
@respx.mock
async def test_refresh_openai_persists_discovered_models() -> None:
    respx.get("https://models.example.com/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "acme-large"}]})
    )
    provider = _provider(compat="openai")
    session = _mock_session()
    result = await refresh_custom_provider_models(session, provider)
    assert result == ["acme-large"]
    assert provider.models_json == ["acme-large"]


# ---------------------------------------------------------------------------
# SSRF guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ftp://models.example.com/v1",
        "file:///etc/passwd",
        "http://localhost:8000/v1",
        "http://127.0.0.1:8000/v1",
        "http://10.0.0.5/v1",
        "http://192.168.1.1/v1",
        "http://169.254.169.254/latest",
        "http://metadata.google.internal/v1",
        "not-a-url",
        "",
    ],
)
def test_validate_discovery_url_blocks_unsafe(url: str) -> None:
    with pytest.raises(ValueError):
        validate_discovery_url(url)


def test_validate_discovery_url_allows_public_https() -> None:
    assert (
        validate_discovery_url("https://models.example.com/v1/")
        == "https://models.example.com/v1"
    )


# ---------------------------------------------------------------------------
# Secrets: encrypted at rest, redacted in public output
# ---------------------------------------------------------------------------


def test_secret_encrypted_at_rest_and_redacted() -> None:
    provider = _provider()
    assert provider.api_key_encrypted != "secret-key-12345"
    public = to_public_dict(provider)
    assert "secret-key-12345" not in str(public)
    assert public["api_key_ref"].endswith("2345")
    assert public["name"] == "acme"
    assert public["base_url"] == "https://models.example.com/v1"


def test_redact_secret_short_and_empty() -> None:
    assert redact_secret("") == ""
    assert redact_secret("ab") == "****"


# ---------------------------------------------------------------------------
# Registry: validation + idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_validates_compat_and_url() -> None:
    session = _mock_session()
    with pytest.raises(ValueError):
        await upsert_custom_provider(
            session, name="x", compat="bogus",
            base_url="https://models.example.com/v1", api_key="k",
        )
    with pytest.raises(ValueError):
        await upsert_custom_provider(
            session, name="x", compat="openai",
            base_url="http://127.0.0.1/v1", api_key="k",
        )
    with pytest.raises(ValueError):
        await upsert_custom_provider(
            session, name="  ", compat="openai",
            base_url="https://models.example.com/v1", api_key="k",
        )


@pytest.mark.asyncio
async def test_upsert_idempotent_on_name_and_base_url() -> None:
    existing = _provider()
    session = _mock_session(existing=existing)
    provider, created = await upsert_custom_provider(
        session,
        name="acme",
        compat="anthropic",
        base_url="https://models.example.com/v1/",
        api_key="rotated-key",
    )
    assert created is False
    assert provider is existing
    assert provider.compat == "anthropic"
    # No duplicate row staged for insert.
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_upsert_creates_new_provider() -> None:
    session = _mock_session(existing=None)
    provider, created = await upsert_custom_provider(
        session,
        name="acme",
        compat="openai",
        base_url="https://models.example.com/v1",
        api_key="brand-new-key",
    )
    assert created is True
    assert provider.name == "acme"
    assert provider.base_url == "https://models.example.com/v1"
    assert provider.api_key_encrypted != "brand-new-key"
    session.add.assert_called_once_with(provider)
