"""
Shared fixtures and test configuration for Haunter backend tests.
"""

import uuid
from typing import AsyncGenerator, Callable

import httpx
from httpx import ASGITransport
import pytest
import respx
from freezegun import freeze_time
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import _sign_state, _sign_user_id
from app.config import settings
from app.db import async_session_maker
from app.models import Attempt, EvalResult, ModelConfig, Repo, Run, RunStep, User
from main import app


async def truncate_all(db: AsyncSession) -> None:
    """Clean up all tables in FK order. Each statement is a separate execute()
    because asyncpg's prepared-statement protocol rejects multi-command strings."""
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
    """Provide an isolated AsyncSession connected to the database."""
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
            access_token=access_token,
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
