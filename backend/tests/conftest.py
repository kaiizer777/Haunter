"""
Shared fixtures and test configuration for Haunter backend tests.

⚠️  SAFETY: truncate_all() hard-blocks against the production Neon URL.
    Set TEST_DATABASE_URL in env to a Neon branch / local Postgres.
    CI must export TEST_DATABASE_URL — running without it skips all DB-mutating tests.
"""

import os
import uuid
from typing import AsyncGenerator, Callable

import httpx
from httpx import ASGITransport
import pytest
import respx
from freezegun import freeze_time
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from dotenv import load_dotenv

load_dotenv()

from app.auth import _encrypt_token, _sign_state, _sign_user_id
from app.config import settings
from app.db import async_session_maker as _prod_session_maker
from app.models import Attempt, EvalResult, ModelConfig, Repo, Run, RunStep, User
from main import app

# ---------------------------------------------------------------------------
# Test database session — MUST point at a non-prod database.
# ---------------------------------------------------------------------------
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", "")


def _is_prod_url(url: str) -> bool:
    """True if url points to the production database host configured in settings."""
    prod_host = urlparse(settings.database_url).netloc.split("@")[-1]
    url_host = urlparse(url).netloc.split("@")[-1]
    return bool(prod_host and prod_host == url_host)


if _TEST_DB_URL:
    _raw_url = _TEST_DB_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
    _parsed = urlparse(_raw_url)
    _clean_params = [(k, v) for k, v in parse_qsl(_parsed.query) if k not in ("sslmode", "channel_binding")]
    _test_url = urlunparse(_parsed._replace(query=urlencode(_clean_params)))
    _test_engine = create_async_engine(
        _test_url, poolclass=NullPool, echo=False, connect_args={"ssl": True}
    )
    async_session_maker = async_sessionmaker(
        bind=_test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    from app import db as db_module
    db_module.engine = _test_engine
    db_module.async_session_maker = async_session_maker

    async def _test_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with async_session_maker() as session:
            try:
                yield session
            finally:
                await session.close()

    app.dependency_overrides[db_module.get_db] = _test_get_db
else:
    async_session_maker = _prod_session_maker


async def truncate_all(db: AsyncSession) -> None:
    """Clean up all tables in FK order.

    Safety: raises RuntimeError if called against a production Neon URL.
    Always set TEST_DATABASE_URL before running pytest.
    """
    engine_url = str(db.get_bind().url)  # type: ignore[union-attr]
    if _is_prod_url(engine_url):
        raise RuntimeError(
            f"truncate_all() blocked: session is connected to production database. "
            "Set TEST_DATABASE_URL to a dedicated test/branch database before running pytest."
        )
    for stmt in (
        "DELETE FROM eval_results",
        "DELETE FROM attempts",
        "DELETE FROM run_steps",
        "DELETE FROM runs",
        "DELETE FROM repos",
        "DELETE FROM model_configs",
        "DELETE FROM users",
    ):
        await db.execute(text(stmt))
    await db.commit()


@pytest.fixture
async def db() -> AsyncGenerator[AsyncSession, None]:
    """Provide an isolated AsyncSession connected to the test database.

    Skips if TEST_DATABASE_URL is unset and prod URL is detected —
    never mutates real data.
    """
    if not _TEST_DB_URL and _is_prod_url(settings.async_database_url):
        pytest.skip(
            "TEST_DATABASE_URL not set. Refusing to run DB-mutating tests against production. "
            "Export TEST_DATABASE_URL pointing at a Neon branch or local Postgres."
        )
    async with async_session_maker() as session:
        try:
            yield session
        finally:
            await session.rollback()
            await session.close()


@pytest.fixture
async def client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """Provide an unauthenticated httpx.AsyncClient bound to the FastAPI ASGI app."""
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@pytest.fixture
def make_auth_client():
    """Factory to create an authenticated httpx.AsyncClient for a specific user UUID."""
    def _make(user_id: uuid.UUID) -> httpx.AsyncClient:
        cookie = _sign_user_id(user_id)
        transport = ASGITransport(app=app)
        return httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            cookies={"haunter_session": cookie},
        )
    return _make


@pytest.fixture
def user_factory(db: AsyncSession):
    """Factory fixture to create and persist a test User."""
    async def _create(
        github_id: int | None = None,
        username: str = "test-user",
        access_token: str | None = "fake_access_token_123",
        avatar_url: str | None = "https://avatars.githubusercontent.com/u/123",
    ) -> User:
        if github_id is None:
            github_id = int(uuid.uuid4().int % 1_000_000_000 + 100_000_000)
        user = User(
            github_id=github_id,
            github_username=username,
            access_token=_encrypt_token(access_token) if access_token else None,
            avatar_url=avatar_url,
        )
        db.add(user)
        await db.commit()
        # expire_on_commit=False on session maker means user attributes remain
        # accessible without a refresh. Store the id explicitly to be safe.
        user_id = user.id
        # Detach from session so the object is safe to pass around without
        # risk of lazy-load errors across transaction boundaries.
        db.expunge(user)
        # Re-attach a fresh copy (needed if caller does db.add on related objs)
        from sqlalchemy import select
        result = await db.execute(select(User).where(User.id == user_id))
        fresh = result.scalar_one()
        return fresh
    return _create


@pytest.fixture
def signed_state_factory():
    """Helper to generate signed OAuth state cookie values."""
    def _sign(raw_state: str) -> str:
        return _sign_state(raw_state)
    return _sign


@pytest.fixture
def signed_session_factory():
    """Helper to generate signed session cookie values."""
    def _sign(user_id: uuid.UUID) -> str:
        return _sign_user_id(user_id)
    return _sign
