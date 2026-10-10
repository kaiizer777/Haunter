"""
Integration tests for Cloud Agentic Live Session API (Phase 1).

Tests:
1. test_create_session_success          — Creates session, mocks SHA resolve, verifies DB row.
2. test_create_session_concurrency_limit — Rejects 3rd session when 2 active exist (HTTP 429).
3. test_create_session_unowned_repo_404  — 404 when user tries to create session on unowned repo.
4. test_get_session_idor_prevention      — User B gets 404 when fetching User A's session.
5. test_get_session_tree                 — Returns filtered repo tree (no node_modules/blobs).
6. test_close_session                    — Closes session; verifies status transition.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import _sign_user_id
from app.models import AgentSession, Repo, User


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _auth_cookies(user: User) -> dict[str, str]:
    return {"haunter_session": _sign_user_id(user.id)}


def _auth_client(make_auth_client, user: User) -> httpx.AsyncClient:
    return make_auth_client(user.id)


async def _seed_user_and_repo(
    db: AsyncSession, *, github_id: int = 11111
) -> tuple[User, Repo]:
    user = User(
        github_id=github_id,
        github_username=f"user-{github_id}",
        role="user",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    repo = Repo(
        user_id=user.id,
        owner="test-org",
        name=f"test-repo-{github_id}",
        default_branch="main",
        github_install_id=999,
    )
    db.add(repo)
    await db.commit()
    await db.refresh(repo)

    return user, repo


async def _seed_active_session(
    db: AsyncSession,
    user: User,
    repo: Repo,
    *,
    title: str = "Pairing Session",
    sha: str = "a" * 40,
) -> AgentSession:
    session = AgentSession(
        user_id=user.id,
        repo_id=repo.id,
        title=title,
        status="active",
        branch_name="main",
        base_sha=sha,
        conversation_history=[],
        staged_patches={},
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


# ---------------------------------------------------------------------------
# Test 1: create_session_success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_session_success(
    db: AsyncSession,
    make_auth_client,
) -> None:
    """POST /sessions creates a session, resolves SHA via mocked GitHub, persists to DB."""
    user, repo = await _seed_user_and_repo(db, github_id=10001)
    fake_sha = "b" * 40

    with (
        patch(
            "app.routers.sessions.get_installation_token",
            new_callable=AsyncMock,
            return_value="mock-gh-token",
        ),
        patch(
            "app.routers.sessions.fetch_branch_sha",
            new_callable=AsyncMock,
            return_value=fake_sha,
        ),
    ):
        async with _auth_client(make_auth_client, user) as ac:
            resp = await ac.post(
                "/sessions",
                json={
                    "repo_id": str(repo.id),
                    "branch_name": "main",
                    "title": "My Session",
                },
            )

    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "active"
    assert data["base_sha"] == fake_sha
    assert data["branch_name"] == "main"
    assert data["title"] == "My Session"
    assert data["repo_owner"] == repo.owner
    assert data["repo_name"] == repo.name
    assert data["user_id"] == str(user.id)
    assert data["repo_id"] == str(repo.id)

    # Verify DB persistence.
    session_id = uuid.UUID(data["id"])
    stmt = select(AgentSession).where(AgentSession.id == session_id)
    result = await db.execute(stmt)
    db_session = result.scalars().first()
    assert db_session is not None
    assert db_session.base_sha == fake_sha
    assert db_session.status == "active"


# ---------------------------------------------------------------------------
# Test 2: concurrency_limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_session_concurrency_limit(
    db: AsyncSession,
    make_auth_client,
) -> None:
    """Third session creation for user with 2 active sessions returns HTTP 429."""
    user, repo = await _seed_user_and_repo(db, github_id=10002)

    # Seed 2 active sessions directly in DB.
    await _seed_active_session(db, user, repo, title="Session 1", sha="c" * 40)
    await _seed_active_session(db, user, repo, title="Session 2", sha="d" * 40)

    with (
        patch(
            "app.routers.sessions.get_installation_token",
            new_callable=AsyncMock,
            return_value="tok",
        ),
        patch(
            "app.routers.sessions.fetch_branch_sha",
            new_callable=AsyncMock,
            return_value="e" * 40,
        ),
    ):
        async with _auth_client(make_auth_client, user) as ac:
            resp = await ac.post(
                "/sessions",
                json={"repo_id": str(repo.id)},
            )

    assert resp.status_code == 429, resp.text
    assert "maximum 2 active sessions" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Test 3: unowned repo → 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_session_unowned_repo_404(
    db: AsyncSession,
    make_auth_client,
) -> None:
    """POST /sessions with another user's repo_id returns 404 (IDOR prevention)."""
    owner_user, repo = await _seed_user_and_repo(db, github_id=10003)

    # Create a different user who will attempt to open a session on owner_user's repo.
    attacker = User(github_id=10099, github_username="attacker", role="user")
    db.add(attacker)
    await db.commit()
    await db.refresh(attacker)

    async with _auth_client(make_auth_client, attacker) as ac:
        resp = await ac.post(
            "/sessions",
            json={"repo_id": str(repo.id)},
        )

    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Test 4: IDOR prevention on GET /sessions/{session_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_session_idor_prevention(
    db: AsyncSession,
    make_auth_client,
) -> None:
    """User B receives 404 when accessing User A's session_id."""
    user_a, repo_a = await _seed_user_and_repo(db, github_id=10004)
    session_a = await _seed_active_session(db, user_a, repo_a, sha="f" * 40)

    user_b = User(github_id=10005, github_username="user-b", role="user")
    db.add(user_b)
    await db.commit()
    await db.refresh(user_b)

    async with _auth_client(make_auth_client, user_b) as ac:
        resp = await ac.get(f"/sessions/{session_a.id}")

    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Test 5: get session tree — filtered
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_session_tree(
    db: AsyncSession,
    make_auth_client,
) -> None:
    """GET /sessions/{id}/tree returns filtered paths (no node_modules, no non-blobs)."""
    user, repo = await _seed_user_and_repo(db, github_id=10006)
    session = await _seed_active_session(db, user, repo, sha="a1b2c3d4e5" * 4)

    mock_tree_response: dict[str, Any] = {
        "sha": session.base_sha,
        "tree": [
            {"type": "tree", "path": "src", "sha": "tree1"},  # non-blob — excluded
            {"type": "blob", "path": "src/main.py", "sha": "blob1"},  # included
            {
                "type": "blob",
                "path": "node_modules/lodash/index.js",
                "sha": "b2",
            },  # excluded
            {"type": "blob", "path": "README.md", "sha": "blob3"},  # included
            {
                "type": "blob",
                "path": "__pycache__/app.cpython-311.pyc",
                "sha": "b4",
            },  # excluded
            {"type": "blob", "path": ".git/config", "sha": "b5"},  # excluded
            {"type": "blob", "path": "backend/app/models.py", "sha": "b6"},  # included
        ],
    }

    with (
        patch(
            "app.routers.sessions.get_installation_token",
            new_callable=AsyncMock,
            return_value="tok",
        ),
        patch(
            "app.routers.sessions.fetch_git_tree",
            new_callable=AsyncMock,
            return_value=mock_tree_response,
        ),
    ):
        async with _auth_client(make_auth_client, user) as ac:
            resp = await ac.get(f"/sessions/{session.id}/tree")

    assert resp.status_code == 200, resp.text
    paths: list[str] = resp.json()
    assert "src/main.py" in paths
    assert "README.md" in paths
    assert "backend/app/models.py" in paths

    # Excluded paths must not appear.
    assert not any("node_modules" in p for p in paths)
    assert not any("__pycache__" in p for p in paths)
    assert not any(".git" in p for p in paths)
    # Non-blob "src" tree entry must not appear as a path.
    assert "src" not in paths


# ---------------------------------------------------------------------------
# Test 6: close session — status transition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_session(
    db: AsyncSession,
    make_auth_client,
) -> None:
    """POST /sessions/{id}/close transitions status to closed and unblocks new session creation."""
    user, repo = await _seed_user_and_repo(db, github_id=10007)

    # Seed 2 active sessions — user is at limit.
    s1 = await _seed_active_session(db, user, repo, title="Session 1", sha="1" * 40)
    s2 = await _seed_active_session(db, user, repo, title="Session 2", sha="2" * 40)

    # Close one.
    async with _auth_client(make_auth_client, user) as ac:
        close_resp = await ac.post(f"/sessions/{s1.id}/close", json={})

    assert close_resp.status_code == 200, close_resp.text
    assert close_resp.json()["status"] == "closed"

    # Verify DB state.
    await db.refresh(s1)
    assert s1.status == "closed"

    # Now user should be able to create a new session (only 1 active remains).
    fake_sha = "3" * 40
    with (
        patch(
            "app.routers.sessions.get_installation_token",
            new_callable=AsyncMock,
            return_value="tok",
        ),
        patch(
            "app.routers.sessions.fetch_branch_sha",
            new_callable=AsyncMock,
            return_value=fake_sha,
        ),
    ):
        async with _auth_client(make_auth_client, user) as ac:
            create_resp = await ac.post(
                "/sessions",
                json={"repo_id": str(repo.id), "title": "Session 3"},
            )

    assert create_resp.status_code == 201, create_resp.text
    assert create_resp.json()["base_sha"] == fake_sha


# ---------------------------------------------------------------------------
# Chat resumption helpers (hermetic — no DB).
# ---------------------------------------------------------------------------


def _lock_conflict_error() -> Exception:
    """Build an OperationalError shaped like a Postgres NOWAIT lock conflict."""
    from sqlalchemy.exc import OperationalError

    return OperationalError(
        "SELECT agent_sessions.id FROM agent_sessions WHERE ... FOR UPDATE NOWAIT",
        {},
        Exception('could not obtain lock on relation "agent_sessions"'),
    )


@pytest.mark.asyncio
async def test_is_turn_active_elsewhere_detects_row_lock() -> None:
    """A NOWAIT lock conflict means a live turn runs on another worker."""
    from app.routers.sessions import _is_turn_active_elsewhere

    class _LockedDb:
        rolled_back = False

        async def execute(self, _stmt):
            raise _lock_conflict_error()

        async def rollback(self) -> None:
            self.rolled_back = True

    db = _LockedDb()
    assert await _is_turn_active_elsewhere(db, uuid.uuid4()) is True  # type: ignore[arg-type]
    assert db.rolled_back is True


@pytest.mark.asyncio
async def test_is_turn_active_elsewhere_free_row_and_other_errors() -> None:
    """A free row (or a non-lock DB error) reports no remote turn."""
    from app.routers.sessions import _is_turn_active_elsewhere

    class _FreeDb:
        async def execute(self, _stmt):
            return object()

        async def rollback(self) -> None:
            raise AssertionError("rollback must not run on the success path")

    assert await _is_turn_active_elsewhere(_FreeDb(), uuid.uuid4()) is False  # type: ignore[arg-type]

    class _OtherDbError:
        rolled_back = False

        async def execute(self, _stmt):
            from sqlalchemy.exc import OperationalError

            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

        async def rollback(self) -> None:
            self.rolled_back = True

    other_db = _OtherDbError()
    assert await _is_turn_active_elsewhere(other_db, uuid.uuid4()) is False  # type: ignore[arg-type]
    assert other_db.rolled_back is True


def test_register_turn_preserves_live_entry() -> None:
    """An overlapping POST never replaces (or orphans) the live turn entry."""
    from unittest.mock import MagicMock

    from app.routers.sessions import _ACTIVE_TURNS, _register_turn
    from app.services.session_streamer import SseQueue

    session_id = uuid.uuid4()
    key = str(session_id)
    _ACTIVE_TURNS.pop(key, None)
    try:
        live_queue, live_task = SseQueue(), MagicMock()
        live_task.done.return_value = False
        assert _register_turn(session_id, live_queue, live_task) is True
        assert _ACTIVE_TURNS[key][1] is live_task

        # Overlapping turn runs unregistered; the live entry survives.
        other_queue, other_task = SseQueue(), MagicMock()
        other_task.done.return_value = False
        assert _register_turn(session_id, other_queue, other_task) is False
        assert _ACTIVE_TURNS[key] == (live_queue, live_task)

        # Once the live turn finishes, a new turn registers normally.
        live_task.done.return_value = True
        assert _register_turn(session_id, other_queue, other_task) is True
        assert _ACTIVE_TURNS[key] == (other_queue, other_task)
    finally:
        _ACTIVE_TURNS.pop(key, None)


def test_live_turn_reports_unfinished_entry_only() -> None:
    """_live_turn returns the entry while its task runs, else None."""
    from unittest.mock import MagicMock

    from app.routers.sessions import _ACTIVE_TURNS, _live_turn
    from app.services.session_streamer import SseQueue

    session_id = uuid.uuid4()
    key = str(session_id)
    _ACTIVE_TURNS.pop(key, None)
    try:
        assert _live_turn(session_id) is None

        queue, task = SseQueue(), MagicMock()
        task.done.return_value = False
        _ACTIVE_TURNS[key] = (queue, task)
        assert _live_turn(session_id) == (queue, task)

        task.done.return_value = True
        assert _live_turn(session_id) is None
    finally:
        _ACTIVE_TURNS.pop(key, None)


def test_completed_turn_retention_lifecycle() -> None:
    """Completed turns are retained for replay until TTL, then swept."""
    import time
    from unittest.mock import MagicMock

    from app.routers.sessions import (
        _ACTIVE_TURNS,
        _COMPLETED_TURN_TTL_S,
        _completed_turn_expired,
        _forget_turn,
        _register_turn,
        _sweep_expired_turns,
        _TURN_COMPLETED_AT,
    )
    from app.services.session_streamer import SseQueue

    session_id = uuid.uuid4()
    key = str(session_id)
    _ACTIVE_TURNS.pop(key, None)
    _TURN_COMPLETED_AT.pop(key, None)
    try:
        queue, task = SseQueue(), MagicMock()
        task.done.return_value = True
        _ACTIVE_TURNS[key] = (queue, task)

        # No stamp yet: treated as expired (safe default, matches old pop-on-done).
        assert _completed_turn_expired(key) is True

        # Fresh completion: retained, and a finished task is not "live",
        # so new prompts are NOT rejected with 409.
        _TURN_COMPLETED_AT[key] = time.monotonic()
        assert _completed_turn_expired(key) is False

        from app.routers.sessions import _live_turn

        assert _live_turn(session_id) is None

        # A new turn replaces the retained entry and clears its stamp.
        new_queue, new_task = SseQueue(), MagicMock()
        new_task.done.return_value = False
        assert _register_turn(session_id, new_queue, new_task) is True
        assert _ACTIVE_TURNS[key] == (new_queue, new_task)
        assert key not in _TURN_COMPLETED_AT

        # Stale completions are swept; fresh ones survive the sweep.
        _ACTIVE_TURNS[key] = (queue, task)
        _TURN_COMPLETED_AT[key] = time.monotonic() - _COMPLETED_TURN_TTL_S - 1.0
        _sweep_expired_turns()
        assert key not in _ACTIVE_TURNS
        assert key not in _TURN_COMPLETED_AT

        _ACTIVE_TURNS[key] = (queue, task)
        _TURN_COMPLETED_AT[key] = time.monotonic()
        _sweep_expired_turns()
        assert _ACTIVE_TURNS[key] == (queue, task)

        _forget_turn(key)
        assert key not in _ACTIVE_TURNS
        assert key not in _TURN_COMPLETED_AT
    finally:
        _ACTIVE_TURNS.pop(key, None)
        _TURN_COMPLETED_AT.pop(key, None)
