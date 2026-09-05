"""
Tests for app.config — configuration loading, URL rewriting, and startup guards.

Covers:
- _to_asyncpg_url:
  - rewrites postgresql:// to postgresql+asyncpg://
  - strips sslmode and channel_binding query params while preserving others
  - idempotent on already converted URLs
  - leaves non-postgres URLs unchanged
- Settings properties:
  - async_database_url and async_database_url_unpooled return asyncpg schemes
- Env-var overrides via monkeypatch (including github_client_id)
- AliasChoices:
  - HAUNTER_MAX_ATTEMPTS and max_attempts aliases
  - HAUNTER_SEED_MAX_FILES and seed_max_files aliases
- Startup security guard for TOKEN_ENCRYPTION_KEY at config.py:172:
  - raises RuntimeError if key is None and pytest is not in sys.modules
  - succeeds if key is set even when pytest is not in sys.modules
"""

import importlib
import os
import sys

import pytest
from pydantic_settings import DotEnvSettingsSource

import app.config
from app.config import Settings, _to_asyncpg_url, settings


# ---------------------------------------------------------------------------
# _to_asyncpg_url
# ---------------------------------------------------------------------------


def test_to_asyncpg_url_scheme_rewrite() -> None:
    raw = "postgresql://user:pass@localhost:5432/testdb"
    expected = "postgresql+asyncpg://user:pass@localhost:5432/testdb"
    assert _to_asyncpg_url(raw) == expected


def test_to_asyncpg_url_strips_sslmode_and_channel_binding() -> None:
    raw = (
        "postgresql://user:pass@ep-xyz.neon.tech/neondb"
        "?sslmode=require&channel_binding=require"
    )
    converted = _to_asyncpg_url(raw)
    assert converted == "postgresql+asyncpg://user:pass@ep-xyz.neon.tech/neondb"
    assert "sslmode" not in converted
    assert "channel_binding" not in converted


def test_to_asyncpg_url_preserves_other_query_params() -> None:
    raw = (
        "postgresql://user:pass@localhost:5432/testdb"
        "?sslmode=require&application_name=haunter&channel_binding=require&search_path=public"
    )
    converted = _to_asyncpg_url(raw)
    assert "application_name=haunter" in converted
    assert "search_path=public" in converted
    assert "sslmode" not in converted
    assert "channel_binding" not in converted


def test_to_asyncpg_url_idempotent() -> None:
    already = "postgresql+asyncpg://user:pass@localhost:5432/testdb"
    assert _to_asyncpg_url(already) == already


def test_to_asyncpg_url_leaves_non_postgres_alone() -> None:
    sqlite_url = "sqlite+aiosqlite:///haunter.db"
    assert _to_asyncpg_url(sqlite_url) == sqlite_url

    http_url = "http://example.com/api"
    assert _to_asyncpg_url(http_url) == http_url


# ---------------------------------------------------------------------------
# Settings async properties
# ---------------------------------------------------------------------------


def test_settings_async_database_urls() -> None:
    assert settings.async_database_url.startswith("postgresql+asyncpg://")
    assert "sslmode" not in settings.async_database_url
    assert "channel_binding" not in settings.async_database_url

    assert settings.async_database_url_unpooled.startswith("postgresql+asyncpg://")
    assert "sslmode" not in settings.async_database_url_unpooled
    assert "channel_binding" not in settings.async_database_url_unpooled


# ---------------------------------------------------------------------------
# Env-var override via monkeypatch
# ---------------------------------------------------------------------------


def test_settings_env_var_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_CLIENT_ID", "test_client_id_override_123")
    s = Settings()
    assert s.github_client_id == "test_client_id_override_123"


# ---------------------------------------------------------------------------
# AliasChoices for max_attempts and seed_max_files
# ---------------------------------------------------------------------------


def test_settings_alias_choices_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    # Setting HAUNTER_MAX_ATTEMPTS in environment
    monkeypatch.delenv("max_attempts", raising=False)
    monkeypatch.setenv("HAUNTER_MAX_ATTEMPTS", "7")
    s1 = Settings()
    assert s1.max_attempts == 7

    # Setting lowercase max_attempts in environment
    monkeypatch.delenv("HAUNTER_MAX_ATTEMPTS", raising=False)
    monkeypatch.setenv("max_attempts", "4")
    s2 = Settings()
    assert s2.max_attempts == 4


def test_settings_alias_choices_seed_max_files(monkeypatch: pytest.MonkeyPatch) -> None:
    # Setting HAUNTER_SEED_MAX_FILES in environment
    monkeypatch.delenv("seed_max_files", raising=False)
    monkeypatch.setenv("HAUNTER_SEED_MAX_FILES", "350")
    s1 = Settings()
    assert s1.seed_max_files == 350

    # Setting lowercase seed_max_files in environment
    monkeypatch.delenv("HAUNTER_SEED_MAX_FILES", raising=False)
    monkeypatch.setenv("seed_max_files", "200")
    s2 = Settings()
    assert s2.seed_max_files == 200


# ---------------------------------------------------------------------------
# TOKEN_ENCRYPTION_KEY startup guard (app/config.py:172)
# ---------------------------------------------------------------------------


def test_token_encryption_key_guard_raises_when_missing_and_no_pytest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When TOKEN_ENCRYPTION_KEY is None and 'pytest' not in sys.modules, reload raises RuntimeError."""
    orig_load = DotEnvSettingsSource._load_env_vars

    def fake_load(self):
        data = dict(orig_load(self))
        data.pop("TOKEN_ENCRYPTION_KEY", None)
        data.pop("token_encryption_key", None)
        return data

    monkeypatch.setattr(DotEnvSettingsSource, "_load_env_vars", fake_load)
    monkeypatch.delenv("TOKEN_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("token_encryption_key", raising=False)

    pytest_module = sys.modules.pop("pytest", None)
    try:
        with pytest.raises(RuntimeError) as exc_info:
            importlib.reload(app.config)
        assert "TOKEN_ENCRYPTION_KEY must be set" in str(exc_info.value)
    finally:
        if pytest_module is not None:
            sys.modules["pytest"] = pytest_module
        # Restore normal config state
        importlib.reload(app.config)


def test_token_encryption_key_guard_succeeds_when_key_set_and_no_pytest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When TOKEN_ENCRYPTION_KEY is set and 'pytest' not in sys.modules, reload succeeds."""
    dummy_key = "8wche2Etq2-FHkJHpJz-MsVV0XFqp_dU_kHCc1FZgG8="
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", dummy_key)

    pytest_module = sys.modules.pop("pytest", None)
    try:
        reloaded = importlib.reload(app.config)
        assert reloaded.settings.token_encryption_key is not None
    finally:
        if pytest_module is not None:
            sys.modules["pytest"] = pytest_module
        importlib.reload(app.config)
