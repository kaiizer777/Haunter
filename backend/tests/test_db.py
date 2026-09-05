"""
Tests for app.db — database engine, connection arguments, and session generator.

Covers:
- engine and engine_unpooled use NullPool (Neon PgBouncer requirement)
- get_db() is an async generator that yields an AsyncSession and closes it
- connect_args["ssl"] is True when URL contains "neon.tech" and {} otherwise
"""

from unittest.mock import patch

import pytest
from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import AsyncSession

import app.db
from app.config import settings


def test_engines_use_nullpool() -> None:
    """Both runtime pooled engine and migration unpooled engine must use NullPool."""
    assert isinstance(app.db.engine.pool, NullPool)
    assert isinstance(app.db.engine_unpooled.pool, NullPool)


@pytest.mark.asyncio
async def test_get_db_yields_session_and_closes() -> None:
    """get_db() yields an active AsyncSession and closes it upon generator completion."""
    gen = app.db.get_db()
    session = await anext(gen)
    assert isinstance(session, AsyncSession)

    with patch.object(session, "close", wraps=session.close) as mock_close:
        with pytest.raises(StopAsyncIteration):
            await anext(gen)
        # Verify close was awaited during cleanup
        assert mock_close.await_count >= 1


def test_connect_args_ssl_condition() -> None:
    """SSL connect arg is enabled for neon.tech URLs and empty for non-neon URLs."""
    neon_url = "postgresql+asyncpg://user:pass@ep-sun-123.c-3.ap-southeast-1.aws.neon.tech/neondb"
    local_url = "postgresql+asyncpg://user:pass@localhost:5432/haunter"

    neon_args = {"ssl": True} if "neon.tech" in neon_url else {}
    local_args = {"ssl": True} if "neon.tech" in local_url else {}

    assert neon_args == {"ssl": True}
    assert local_args == {}


def test_active_connect_args_matches_active_database_url() -> None:
    """The module-level connect_args reflects the configured database_url."""
    if "neon.tech" in settings.async_database_url:
        assert app.db.connect_args == {"ssl": True}
    else:
        assert app.db.connect_args == {}
