"""
Shared fixtures and test configuration for Haunter backend tests.

⚠️  SAFETY: truncate_all() hard-blocks against the production Neon URL.
    Set TEST_DATABASE_URL in env to a Neon branch / local Postgres.
    CI must export TEST_DATABASE_URL — running without it skips all DB-mutating tests.

Hermetic isolation strategy (read this before editing):
  1. The `db` fixture function-scoped. It TRUNCATEs every known table in FK
     order AT SETUP, runs the test on a fresh session, then TRUNCATEs again
     AT TEARDOWN. Either side failing is logged but does not mask test errors.
  2. The autouse `_isolate_settings` fixture snapshots every public Settings
     attribute BEFORE each test and restores it AFTER each test. This guards
     against tests that touch `settings.X = Y` directly (without monkeypatch)
     and would otherwise leak into the next test. Pydantic settings allow
     arbitrary attribute assignment; we copy both scalars and Optional[str]
     and skip property descriptors (async_database_url etc.).
  3. The autouse `_restore_settings_singleton` fixture rebinds
     `app.config.settings` (and any module-level cached `settings` alias) back
     to the conftest-import-time instance after each test. This handles
     `importlib.reload` of `app.config` mid-test.
  4. The test-time FastAPI dependency override (`_test_get_db`) is wired at
     conftest import so any client fixture routes through the test engine.
"""

import copy
import os
import uuid
from typing import AsyncGenerator

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from dotenv import load_dotenv

load_dotenv()

from app.auth import _sign_state, _sign_user_id
from app.config import Settings, settings
from app.db import async_session_maker as _prod_session_maker
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
    _clean_params = [
        (k, v)
        for k, v in parse_qsl(_parsed.query)
        if k not in ("sslmode", "channel_binding")
    ]
    _test_url = urlunparse(_parsed._replace(query=urlencode(_clean_params)))
    _test_engine = create_async_engine(
        _test_url, poolclass=NullPool, echo=False, connect_args={"ssl": True}
    )

    class TestAsyncSession(AsyncSession):
        def expire_all(self) -> None:
            """Expire non-primary-key attributes so reloads work without MissingGreenlet on PK access.

            The default `expire_all` expires every attribute including the
            primary key, which forces a lazy roundtrip on the very next
            `model.id` access — and under async the roundtrip happens from
            a sync descriptor, raising MissingGreenlet. Expiring only the
            non-PK attributes keeps `id` accessible without an extra SELECT.
            """
            for state in list(self.sync_session.identity_map.all_states()):
                obj = state.obj()
                if obj is not None and getattr(state, "mapper", None) is not None:
                    pk_keys = {c.key for c in state.mapper.primary_key}
                    attrs = [
                        k for k in state.mapper.column_attrs.keys() if k not in pk_keys
                    ]
                    self.sync_session.expire(obj, attribute_names=attrs)

    async_session_maker = async_sessionmaker(
        bind=_test_engine,
        class_=TestAsyncSession,
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


# ---------------------------------------------------------------------------
# Truncate / hermetic DB state.
# ---------------------------------------------------------------------------
# Order MUST respect FK dependencies: rows that reference other tables are
# deleted BEFORE the rows they reference. CASCADE makes the order forgiving
# for any future FK we forget to list here, but we still list the children
# explicitly for clarity.
_TRUNCATE_ORDER: tuple[str, ...] = (
    "system_configs",
    "repo_settings",
    "audit_jobs",
    "code_reviews",
    "agent_sessions",
    "eval_results",
    "attempts",
    "run_steps",
    "runs",
    "repos",
    "model_configs",
    "users",
)


async def _truncate_all(db: AsyncSession) -> None:
    """Idempotent hard reset of every test table.

    Uses TRUNCATE ... RESTART IDENTITY CASCADE so:
      - sequences reset to 1 (predictable IDs across runs)
      - FK references are not enforced against the truncate (CASCADE)
      - faster than DELETE for the same volume

    Safety: raises RuntimeError if called against a production Neon URL.
    """
    engine_url = str(db.get_bind().url)  # type: ignore[union-attr]
    if _is_prod_url(engine_url):
        raise RuntimeError(
            "truncate_all() blocked: session is connected to production database. "
            "Set TEST_DATABASE_URL to a dedicated test/branch database before running pytest."
        )
    table_list = ", ".join(_TRUNCATE_ORDER)
    await db.execute(text(f"TRUNCATE TABLE {table_list} RESTART IDENTITY CASCADE"))
    await db.commit()
    try:
        from app.adapters.hosting import invalidate_provider_cache

        invalidate_provider_cache()
    except Exception:
        # Cache invalidation is best-effort; never fail a truncate on it.
        pass


# Backwards-compat alias for tests that import `truncate_all` directly.
# New code should use the `_truncate_all` helper above; this alias keeps the
# 30+ call sites in test_*.py working without churn.
async def truncate_all(db: AsyncSession) -> None:  # pragma: no cover - thin wrapper
    await _truncate_all(db)


# ---------------------------------------------------------------------------
# db fixture — function-scoped, TRUNCATE before AND after each test.
# ---------------------------------------------------------------------------
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
        await _truncate_all(session)
        try:
            yield session
        finally:
            # Final truncate. Any state leaked by a failing test is wiped here
            # so the next test starts clean. We log-and-swallow because a
            # secondary failure must not mask the test's primary failure.
            try:
                await _truncate_all(session)
            except Exception:
                pass
            await session.rollback()
            await session.close()


@pytest.fixture
async def client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """Provide an unauthenticated httpx.AsyncClient bound to the FastAPI ASGI app."""
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as ac:
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
        role: str = "user",
    ) -> "object":  # forward ref avoids importing User here
        from sqlalchemy import select

        from app.auth import _encrypt_token
        from app.models import User

        if github_id is None:
            github_id = int(uuid.uuid4().int % 1_000_000_000 + 100_000_000)
        user = User(
            github_id=github_id,
            github_username=username,
            access_token=_encrypt_token(access_token) if access_token else None,
            avatar_url=avatar_url,
            role=role,
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


# ---------------------------------------------------------------------------
# Settings isolation.
# ---------------------------------------------------------------------------
# Snapshot every public scalar field of the Settings instance at conftest
# import time. Pydantic v2 BaseSettings allows attribute assignment, so a
# test that does `settings.foo = "bar"` without monkeypatch will mutate the
# shared instance for every subsequent test. We restore on every test exit.
_ORIGINAL_SETTINGS: Settings = settings


def _snapshot_settings(s: Settings) -> dict[str, object]:
    """Copy every public scalar attribute of a Settings instance.

    Skips pydantic-internal slots, properties, and callables; copies
    Optional[str] / str / int / float / bool / None directly. Anything
    exotic (lists, dicts) is deep-copied.
    """
    snap: dict[str, object] = {}
    for name in vars(s):
        if name.startswith("_"):
            continue
        value = getattr(s, name)
        if callable(value):
            continue
        if isinstance(value, (str, int, float, bool, type(None))):
            snap[name] = value
        else:
            snap[name] = copy.deepcopy(value)
    return snap


def _restore_settings(s: Settings, snap: dict[str, object]) -> None:
    for name, original_value in snap.items():
        try:
            current = getattr(s, name)
        except AttributeError:
            continue
        if current != original_value:
            try:
                setattr(s, name, original_value)
            except (AttributeError, ValueError):
                # Frozen field or pydantic validator rejection — leave it.
                pass


_INITIAL_SETTINGS_SNAPSHOT: dict[str, object] = _snapshot_settings(_ORIGINAL_SETTINGS)


@pytest.fixture(autouse=True)
def _isolate_settings():
    """Snapshot/restore every Settings attribute around each test.

    The snapshot is taken BEFORE the test runs (so the test starts from the
    post-import baseline) and restored AFTER the test finishes (so the next
    test sees the same baseline). This handles tests that mutate settings
    without using monkeypatch or unittest.mock.patch — a real source of
    cross-test pollution in this codebase.
    """
    # Defensive re-snapshot: if a previous test's teardown was skipped (e.g.
    # the test crashed during fixture setup), restore from the immutable
    # import-time baseline before yielding control.
    _restore_settings(_ORIGINAL_SETTINGS, _INITIAL_SETTINGS_SNAPSHOT)
    try:
        yield
    finally:
        _restore_settings(_ORIGINAL_SETTINGS, _INITIAL_SETTINGS_SNAPSHOT)


@pytest.fixture(autouse=True)
def _restore_settings_singleton():
    """Rebind `app.config.settings` and any module-level `settings` alias
    back to the conftest-import-time singleton.

    Handles `importlib.reload(app.config)` mid-test (which creates a new
    Settings instance and rebinds `app.config.settings`), and ensures the
    FastAPI app's dependency-injected modules see the original singleton.
    """
    import sys
    import app.config

    yield
    # After the test, force the canonical singleton everywhere.
    app.config.settings = _ORIGINAL_SETTINGS
    _restore_settings(_ORIGINAL_SETTINGS, _INITIAL_SETTINGS_SNAPSHOT)
    for name, mod in list(sys.modules.items()):
        if name is None:
            continue
        if name.startswith(("app", "tests", "main")):
            s = getattr(mod, "settings", None)
            if s is not None and s is not _ORIGINAL_SETTINGS:
                if (
                    s.__class__.__name__ == "Settings"
                    and s.__class__.__module__ == "app.config"
                ):
                    mod.settings = _ORIGINAL_SETTINGS
