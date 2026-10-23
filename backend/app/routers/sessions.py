"""
Agent Session API endpoints — Cloud Agentic Live Session Phases 1 & 2.

Exposes:
  POST /sessions                        — Create a new pairing session.
  GET  /sessions                        — List sessions for authenticated user.
  GET  /sessions/{session_id}           — Fetch a single session.
  GET  /sessions/{session_id}/tree      — Fetch filtered repository file tree.
  POST /sessions/{session_id}/close     — Close an active session.
  POST /sessions/{session_id}/chat      — Stream LLM-driven agent response (SSE).
  POST /sessions/{session_id}/verify    — Dispatch staged patches to sandbox runner.

Security invariants:
- Requires authentication via get_current_user on all endpoints.
- Enforces multi-tenant isolation: every query is scoped to current_user.id.
- Returns 404 for non-existent or unowned sessions/repos (no existence oracle leaks).
- Concurrency guard: rejects POST /sessions when user already has >= 2 active sessions.
- /chat verifies session.user_id == current_user.id before streaming.
- /verify verifies session.user_id == current_user.id and session.status == active.
"""

from __future__ import annotations

import asyncio as _asyncio
import logging
import random as _random
import time
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, AsyncGenerator, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import get_current_user
from app.db import get_db
from app.github.pr import get_installation_token
from app.github_client import (
    GitHubClientError,
    GitHubResourceNotFoundError,
    fetch_branch_sha,
    fetch_git_tree,
)
from app.lambda_runtime import resolve_lambda_timeout_s
from app.models import AgentSession, Repo, User
from app.schemas import (
    ClarificationIn,
    SandboxVerificationOut,
    SessionChatIn,
    SessionCloseIn,
    SessionCommitIn,
    SessionCommitOut,
    SessionCreateIn,
    SessionListOut,
    SessionOut,
)
from app.services.session_orchestrator import (
    SessionOrchestrator,
    _is_lock_conflict as _is_row_lock_conflict,
)
from app.services.session_streamer import SseQueue

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sessions"])

# Retain strong references to running background tasks to prevent garbage collection.
_BACKGROUND_TASKS: set[_asyncio.Task[Any]] = set()

# Live SSE turns by session id: (queue, background task). Lets a client that
# disconnected mid-stream reattach to the in-flight turn via Last-Event-ID
# instead of starting a duplicate turn.
_ACTIVE_TURNS: dict[str, tuple[SseQueue, _asyncio.Task[Any]]] = {}

# Seconds a completed turn's replay buffer stays available for reconnects
# that missed the terminal event.
_COMPLETED_TURN_TTL_S = 300.0

# Monotonic timestamps (time.monotonic()) marking when a registered turn
# completed. A completed entry is kept (not popped) so a reconnect that
# missed `done` replays the terminal outcome instead of getting 410 and
# resubmitting an already-executed turn as a duplicate.
_TURN_COMPLETED_AT: dict[str, float] = {}

# Statuses allowed on the AgentSession model.
_VALID_STATUSES = {"active", "completed", "closed", "awaiting_clarification"}

# Directories and prefixes to strip from the tree response.
# These are never useful to the pairing agent and can be large.
_TREE_IGNORE_PREFIXES = (
    ".git/",
    "node_modules/",
    "__pycache__/",
    ".venv/",
    "venv/",
    "dist/",
    "build/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
)

# Maximum concurrent active sessions per user.
_MAX_ACTIVE_SESSIONS = 2

# Cross-environment turn-discovery retry budget (issue #117). A reconnect that
# lands on a worker where the turn is still starting up re-reads the latest
# turn_id; bound that to a few backoff+jitter retries under a total deadline
# instead of a 25x fixed-sleep stampede, then surface 409 to the caller.
#
# Budget math: with `_TURN_DISCOVERY_BASE_DELAY_S = 0.1` and full-jitter
# caps at `BASE * 2^(n-1)` clamped at `_TURN_DISCOVERY_MAX_DELAY_S`, the
# worst-case ceiling across N attempts is sum of caps. Choosing
# N = 5 gives caps [0.1, 0.2, 0.4, 0.8, 1.0] (the last clamped by
# `_TURN_DISCOVERY_MAX_DELAY_S = 1.0`), summing to exactly
# `_TURN_DISCOVERY_DEADLINE_S = 2.5s`. The remaining-time clamp below
# prevents a single oversized jitter from blowing past the deadline.
_TURN_DISCOVERY_MAX_ATTEMPTS = 5
_TURN_DISCOVERY_BASE_DELAY_S = 0.1
_TURN_DISCOVERY_MAX_DELAY_S = 1.0
_TURN_DISCOVERY_DEADLINE_S = 2.5


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _map_session_to_out(session: AgentSession, repo: Repo) -> SessionOut:
    """Map ORM + joined Repo to the session DTO. Never reads from ORM lazy attributes."""
    return SessionOut(
        id=session.id,
        user_id=session.user_id,
        repo_id=session.repo_id,
        repo_owner=repo.owner,
        repo_name=repo.name,
        title=session.title,
        status=session.status,
        branch_name=session.branch_name,
        base_sha=session.base_sha,
        conversation_history=session.conversation_history or [],
        staged_patches=session.staged_patches or {},
        plan=session.plan or [],
        waiting_input=session.waiting_input,
        checkpoints=list(session.checkpoints or []),
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/sessions", response_model=SessionOut, status_code=status.HTTP_201_CREATED
)
async def create_session(
    body: SessionCreateIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SessionOut:
    """
    Create a new pairing session for a repo owned by the authenticated user.

    - IDOR prevention: repo_id must belong to current_user.
    - Concurrency cap: at most 2 active sessions per user (HTTP 429 on breach).
    - Resolves base_sha via GitHub App installation token.
    """
    # Object-level authorization: verify repo belongs to caller.
    repo_stmt = select(Repo).where(
        Repo.id == body.repo_id, Repo.user_id == current_user.id
    )
    repo_result = await db.execute(repo_stmt)
    repo: Optional[Repo] = repo_result.scalars().first()
    if not repo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Repository not found"
        )

    # Concurrency guard: count active sessions for this user.
    active_count_stmt = (
        select(func.count())
        .select_from(AgentSession)
        .where(AgentSession.user_id == current_user.id, AgentSession.status == "active")
    )
    active_count: int = await db.scalar(active_count_stmt) or 0
    if active_count >= _MAX_ACTIVE_SESSIONS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                "Concurrent live session limit reached (maximum 2 active sessions). "
                "Please close an existing session."
            ),
        )

    # Resolve target branch:
    # When branch_name is omitted (null), never default to repo.default_branch as a writable target.
    # Mint a dedicated topic branch (e.g. haunter/session-{id.hex[:12]}) and resolve base_sha from repo default_branch.
    session_id: uuid.UUID = uuid.uuid4()
    if body.branch_name:
        branch_name: str = body.branch_name
        ref_to_fetch: str = branch_name
    else:
        from app.services.session_commit import minted_session_branch

        branch_name = minted_session_branch(session_id)
        ref_to_fetch = repo.default_branch or "main"

    # Resolve base SHA via GitHub App installation token.
    # Falls back to settings.github_token for local dev (get_installation_token does this internally).
    try:
        gh_token: str = await get_installation_token(repo)
    except Exception:
        gh_token = None  # fetch_branch_sha will fall back to settings.github_token

    try:
        base_sha: str = await fetch_branch_sha(
            owner=repo.owner,
            repo=repo.name,
            branch=ref_to_fetch,
            token=gh_token,
        )
    except GitHubResourceNotFoundError:
        # If the requested branch does not exist on GitHub (e.g. topic branch to be created),
        # initialize base SHA from repo.default_branch or fallback branches without raising 422.
        fallback_candidates = [repo.default_branch or "main", "main", "master"]
        resolved_base_sha: Optional[str] = None
        for candidate in fallback_candidates:
            if candidate == branch_name:
                continue
            try:
                resolved_base_sha = await fetch_branch_sha(
                    owner=repo.owner,
                    repo=repo.name,
                    branch=candidate,
                    token=gh_token,
                )
                if resolved_base_sha:
                    break
            except GitHubResourceNotFoundError:
                continue
            except GitHubClientError as exc:
                logger.error(
                    "GitHub API error resolving fallback branch SHA for %s/%s branch %s: %s",
                    repo.owner,
                    repo.name,
                    candidate,
                    exc,
                )
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Failed to resolve branch SHA from GitHub. Please try again.",
                )
        if resolved_base_sha:
            base_sha = resolved_base_sha
        else:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Branch '{branch_name}' not found in repository {repo.owner}/{repo.name}",
            )
    except GitHubClientError as exc:
        logger.error(
            "GitHub API error resolving branch SHA for %s/%s branch %s: %s",
            repo.owner,
            repo.name,
            branch_name,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to resolve branch SHA from GitHub. Please try again.",
        )

    session = AgentSession(
        id=session_id,
        user_id=current_user.id,
        repo_id=repo.id,
        title=body.title or "Pairing Session",
        status="active",
        branch_name=branch_name,
        base_sha=base_sha,
        conversation_history=[],
        staged_patches={},
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)

    return _map_session_to_out(session, repo)


@router.get("/sessions", response_model=SessionListOut)
async def list_sessions(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    session_status: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> SessionListOut:
    """
    List pairing sessions owned by the authenticated user.

    Supports optional ?status= filter and pagination (?limit=&offset=).
    """
    if session_status is not None and session_status not in _VALID_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid status filter. Allowed: {sorted(_VALID_STATUSES)}",
        )

    base_where = [AgentSession.user_id == current_user.id]
    if session_status is not None:
        base_where.append(AgentSession.status == session_status)

    count_stmt = select(func.count()).select_from(AgentSession).where(*base_where)
    total: int = await db.scalar(count_stmt) or 0

    stmt = (
        select(AgentSession)
        .options(selectinload(AgentSession.repo))
        .where(*base_where)
        .order_by(AgentSession.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(stmt)
    sessions: list[AgentSession] = list(result.scalars().all())

    out = [_map_session_to_out(s, s.repo) for s in sessions]
    return SessionListOut(sessions=out, total=total)


async def _is_turn_active_elsewhere(
    db: AsyncSession | AsyncConnection, session_id: uuid.UUID
) -> bool:
    """
    Probe whether another backend worker currently holds this session's row lock.

    A live turn holds the session row lock from _load_session until its final
    _persist commit. _ACTIVE_TURNS only sees turns in this process, so on Lambda
    (Function URL fans out across execution environments) a reconnect can miss
    a turn that is alive elsewhere. A NOWAIT lock conflict here means exactly
    that: some other worker owns the turn. Any other outcome reports False.
    """
    from sqlalchemy.exc import DBAPIError, OperationalError

    try:
        probe_stmt = (
            select(AgentSession.id)
            .where(AgentSession.id == session_id)
            .with_for_update(nowait=True)
        )
        await db.execute(probe_stmt)
        try:
            await db.rollback()
        except Exception:
            pass
        return False
    except (OperationalError, DBAPIError) as exc:
        try:
            await db.rollback()
        except Exception:
            pass
        return _is_row_lock_conflict(exc)


def _register_turn(
    session_id: uuid.UUID, queue: SseQueue, task: _asyncio.Task[Any]
) -> bool:
    """
    Register a new turn unless a live one already owns this session.

    Returns True when the new turn was registered. Returns False (leaving the
    live entry untouched) when an overlapping POST won the race past the
    pre-creation live-turn check in chat_session: the new turn runs
    unregistered, so neither this registration nor its cleanup can orphan the
    live turn and break resumption with a spurious 410.
    """
    live_entry = _live_turn(session_id)
    if live_entry is None:
        _ACTIVE_TURNS[str(session_id)] = (queue, task)
        _TURN_COMPLETED_AT.pop(str(session_id), None)
        return True
    logger.warning(
        "chat_session: overlapping POST for session %s while a turn is live; "
        "preserving the live registry entry",
        session_id,
    )
    return False


def _live_turn(
    session_id: uuid.UUID,
) -> tuple[SseQueue, _asyncio.Task[Any]] | None:
    """Return the live (unfinished) turn entry for session_id, if any."""
    entry = _ACTIVE_TURNS.get(str(session_id))
    if entry is not None and not entry[1].done():
        return entry
    return None


def _forget_turn(session_id: str) -> None:
    """Drop any registry entry and completion stamp for session_id."""
    _ACTIVE_TURNS.pop(session_id, None)
    _TURN_COMPLETED_AT.pop(session_id, None)


def _completed_turn_expired(session_id: str) -> bool:
    """True when no retained completed turn exists or it is past its TTL."""
    completed_at = _TURN_COMPLETED_AT.get(session_id)
    if completed_at is None:
        return True
    return (time.monotonic() - completed_at) > _COMPLETED_TURN_TTL_S


def _sweep_expired_turns() -> None:
    """Drop retained completed turns past their replay TTL (bounded memory)."""
    for key, completed_at in list(_TURN_COMPLETED_AT.items()):
        if (time.monotonic() - completed_at) > _COMPLETED_TURN_TTL_S:
            _forget_turn(key)


@router.get("/sessions/{session_id}", response_model=SessionOut)
async def get_session(
    session_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SessionOut:
    """
    Fetch a single session. Scoped strictly to current_user.id.

    Returns 404 for non-existent AND unowned sessions (no existence oracle leak).
    """
    stmt = (
        select(AgentSession)
        .options(selectinload(AgentSession.repo))
        .where(AgentSession.id == session_id, AgentSession.user_id == current_user.id)
    )
    result = await db.execute(stmt)
    session: Optional[AgentSession] = result.scalars().first()
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
        )

    return _map_session_to_out(session, session.repo)


@router.get("/sessions/{session_id}/tree", response_model=list[str])
async def get_session_tree(
    session_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[str]:
    """
    Fetch filtered repository file tree paths for a session's base SHA.

    Strips: .git, node_modules, __pycache__, .venv, dist, build, and similar noise.
    Strips: non-blob entries (trees/submodules).
    Returns: list of file path strings for the frontend tree view.
    """
    stmt = (
        select(AgentSession)
        .options(selectinload(AgentSession.repo))
        .where(AgentSession.id == session_id, AgentSession.user_id == current_user.id)
    )
    result = await db.execute(stmt)
    session: Optional[AgentSession] = result.scalars().first()
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
        )

    repo = session.repo

    try:
        gh_token: str = await get_installation_token(repo)
    except Exception:
        gh_token = None

    try:
        tree_data: dict[str, Any] = await fetch_git_tree(
            owner=repo.owner,
            repo=repo.name,
            tree_sha=session.base_sha,
            recursive=True,
            token=gh_token,
        )
    except GitHubResourceNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository tree not found for this session's base SHA",
        )
    except GitHubClientError as exc:
        logger.error(
            "GitHub API error fetching tree for session %s (%s/%s @ %s): %s",
            session_id,
            repo.owner,
            repo.name,
            session.base_sha,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to fetch repository tree from GitHub. Please try again.",
        )

    paths: list[str] = []
    for item in tree_data.get("tree", []):
        # Only include blob entries (exclude trees, commits/submodules).
        if item.get("type") != "blob":
            continue
        p: str = item.get("path", "")
        if not p:
            continue
        # Filter common noise directories.
        if any(
            p.startswith(prefix) or f"/{prefix}" in f"/{p}"
            for prefix in _TREE_IGNORE_PREFIXES
        ):
            continue
        paths.append(p)

    return paths


@router.post("/sessions/{session_id}/close", response_model=SessionOut)
async def close_session(
    session_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    body: Optional[SessionCloseIn] = None,
) -> SessionOut:
    """
    Close an active pairing session.

    Sets status="closed" and updates updated_at.
    Returns 404 for non-existent or unowned sessions.
    Closing an already-closed session is idempotent (returns current state).
    """
    stmt = (
        select(AgentSession)
        .options(selectinload(AgentSession.repo))
        .where(AgentSession.id == session_id, AgentSession.user_id == current_user.id)
    )
    result = await db.execute(stmt)
    session: Optional[AgentSession] = result.scalars().first()
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
        )

    repo = session.repo

    if session.status != "closed":
        session.status = "closed"
        session.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(session)

    return _map_session_to_out(session, repo)


# ---------------------------------------------------------------------------
# Phase 2 — Chat (SSE Streaming) & Verify endpoints
# ---------------------------------------------------------------------------


def _format_chunk(event_id: int, event: str, chunk: str) -> str:
    """Format an SSE wire chunk for StreamingResponse."""
    if chunk.startswith("event:") or "\ndata:" in chunk:
        return chunk if chunk.endswith("\n\n") else (chunk + "\n\n")
    return f"id: {event_id}\nevent: {event}\ndata: {chunk}\n\n"


#: Seconds between DB polls while a turn runs on another worker. Uses 1.0s ticks
#: to bound polling round trips across Lambda execution environments, trading up
#: to 1s of resumed-stream latency for reduced database query load.
_SSE_POLL_INTERVAL_S = 1.0

#: Seconds between liveliness probes of the session row lock while a turn runs
#: elsewhere. Wall-clock cadence, not ticks: the poll sleep below is capped so
#: the loop always wakes when a probe is due, holding ~5s even under full idle
#: backoff (tick-counted before #148, stretching to ~75s). The NOWAIT probe
#: opens a lock attempt plus a rollback, so it still runs far less often than once per tick while events flow.
_SSE_LIVENESS_PROBE_INTERVAL_S = 5.0

#: Upper bound on the idle backoff applied to the poll sleep. Consecutive empty
#: ticks back off exponentially from _SSE_POLL_INTERVAL_S (1s, 2s, 4s, 8s, ...)
#: up to this cap, mirroring the audit dispatch worker's idle backoff, so an idle
#: open tab issues ~0.07 queries/sec instead of 1/sec. Every tick reuses the
#: one held stream connection (#165: no fresh session per tick under NullPool),
#: so the tick rate is only the query rate, not the connection-churn rate. The
#: cost is bounded event latency: an event committed while the stream is fully
#: backed off can wait up to this cap before it is observed.
_SSE_IDLE_BACKOFF_MAX_S: float = 15.0

#: Bounded terminal-event drain attempts after the turn lock releases.
_SSE_DRAIN_ATTEMPTS = 3
_SSE_DRAIN_INTERVAL_S = 0.5


def _sse_idle_delay_s(empty_streak: int) -> float:
    """Exponential idle backoff: 1s, 2s, 4s, ... capped at
    ``_SSE_IDLE_BACKOFF_MAX_S``.

    Mirrors ``_audit_dispatch_idle_delay_s`` (audit_pipeline) so both idle
    pollers share one backoff shape.
    """
    if empty_streak <= 0:
        return _SSE_POLL_INTERVAL_S
    try:
        delay = float(_SSE_POLL_INTERVAL_S) * (2.0 ** (empty_streak - 1))
    except OverflowError:
        return float(_SSE_IDLE_BACKOFF_MAX_S)
    return min(delay, float(_SSE_IDLE_BACKOFF_MAX_S))


async def _stream_from_db(
    session_id: uuid.UUID,
    turn_id: str,
    last_event_id: int,
    request: Optional[Request] = None,
    timeout_s: Optional[float] = None,
) -> AsyncGenerator[str, None]:
    """
    Async generator that reads and streams SSE events from PostgreSQL.

    Serves cross-environment resumption on AWS Lambda:
    1. Fetches and yields any events already persisted with event_id > last_event_id.
    2. If a terminal event ('done' or 'error') is yielded, returns immediately.
    3. If the turn is still running in another environment, polls with an
       _SSE_POLL_INTERVAL_S sleep — exponentially backed off up to
       _SSE_IDLE_BACKOFF_MAX_S across consecutive empty ticks, and reset to
       _SSE_POLL_INTERVAL_S as soon as events arrive — until a terminal event
       is persisted, the turn worker drops its row lock, the client
       disconnects, or the Lambda-anchored timeout below is reached
       (explicit ``timeout_s`` wins; otherwise the invoke's remaining
       time minus a safety margin, 900s fallback outside Lambda).

    The whole stream holds a single shared engine connection (issue #165).
    Under ``NullPool`` every fresh session checkout is a brand-new physical
    connection, so the old session-per-tick pattern churned one TCP/TLS
    handshake per poll. One held connection keeps the steady-state count flat
    at 1; a rollback after every read batch keeps it out of
    idle-in-transaction across sleeps and client yields, while READ COMMITTED
    still observes rows committed after the stream started.
    """
    from app.db import engine as _poll_engine
    from app.models import SessionStreamEvent

    def _pending_stmt(after_id: int):
        return (
            select(SessionStreamEvent)
            .where(
                SessionStreamEvent.session_id == session_id,
                SessionStreamEvent.turn_id == turn_id,
                SessionStreamEvent.event_id > after_id,
            )
            .order_by(SessionStreamEvent.event_id.asc())
        )

    max_yielded_id = last_event_id

    # 1. Yield any events already persisted. The connection below is the one
    # held for the whole stream: every poll, probe, and drain further down
    # reuses it instead of opening a fresh session per tick (issue #165).
    async with _poll_engine.connect() as poll_db:
        res = await poll_db.execute(_pending_stmt(max_yielded_id))
        initial_rows = res.scalars().all()
        await poll_db.rollback()

        for row in initial_rows:
            if row.event_id <= max_yielded_id or row.event == "turn_start":
                continue
            yield _format_chunk(row.event_id, row.event, row.chunk)
            max_yielded_id = row.event_id
            if row.event in ("done", "error"):
                return

        # 2. Poll for emerging events if the turn is in-flight elsewhere.
        if timeout_s is None:
            # Anchor to the real Lambda remaining budget (900s fallback outside
            # Lambda) so the guard below fires before the hard kill. The deadline
            # is measured from here, but remaining time already excludes the
            # pre-stream work above plus a safety margin.
            timeout_s = resolve_lambda_timeout_s()
        start_time = time.monotonic()
        # Wall-clock liveliness probe (#148): `idle_streak` drives the poll backoff
        # and resets only on real rows; `last_probe_at` drives the probe and resets
        # on rows and on every probe, so backoff can never stretch the probe
        # cadence past _SSE_LIVENESS_PROBE_INTERVAL_S.
        idle_streak = 0
        last_probe_at = start_time

        while True:
            if time.monotonic() - start_time > timeout_s:
                logger.warning(
                    "_stream_from_db: reached timeout (%.1fs) for session %s turn %s",
                    timeout_s,
                    session_id,
                    turn_id,
                )
                break

            if request is not None:
                try:
                    if await request.is_disconnected():
                        logger.info(
                            "_stream_from_db: client disconnected for session %s",
                            session_id,
                        )
                        break
                except Exception:
                    pass

            # Cap the backoff sleep so the loop always wakes when a probe is due;
            # without this the time-based gate below could only fire once per
            # (up to 15s) tick and the cadence would still stretch under backoff.
            probe_due_in = _SSE_LIVENESS_PROBE_INTERVAL_S - (
                time.monotonic() - last_probe_at
            )
            await _asyncio.sleep(
                min(_sse_idle_delay_s(idle_streak), max(0.0, probe_due_in))
            )

            res = await poll_db.execute(_pending_stmt(max_yielded_id))
            rows = res.scalars().all()
            await poll_db.rollback()

            if rows:
                idle_streak = 0
                last_probe_at = time.monotonic()
                for row in rows:
                    if row.event_id <= max_yielded_id or row.event == "turn_start":
                        continue
                    yield _format_chunk(row.event_id, row.event, row.chunk)
                    max_yielded_id = row.event_id
                    if row.event in ("done", "error"):
                        return
                continue

            idle_streak += 1
            now = time.monotonic()
            if now - last_probe_at < _SSE_LIVENESS_PROBE_INTERVAL_S:
                continue
            last_probe_at = now

            is_active = await _is_turn_active_elsewhere(poll_db, session_id)
            if is_active:
                continue

            # Turn lock released: drain any final event committed right before
            # lock release, then close. Bounded attempts.
            for _ in range(_SSE_DRAIN_ATTEMPTS):
                final_res = await poll_db.execute(_pending_stmt(max_yielded_id))
                drain_rows = final_res.scalars().all()
                await poll_db.rollback()

                for row in drain_rows:
                    if row.event_id <= max_yielded_id or row.event == "turn_start":
                        continue
                    yield _format_chunk(row.event_id, row.event, row.chunk)
                    max_yielded_id = row.event_id
                    if row.event in ("done", "error"):
                        return
                await _asyncio.sleep(_SSE_DRAIN_INTERVAL_S)
            break


@router.post("/sessions/{session_id}/chat")
async def chat_session(
    session_id: uuid.UUID,
    body: SessionChatIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    request: Request = None,  # type: ignore[assignment]
    last_event_id_header: Annotated[Optional[str], Header(alias="Last-Event-ID")] = None,
    last_event_id_query: Annotated[Optional[int], Query(alias="last_event_id")] = None,
) -> StreamingResponse:
    """
    Submit a user message to the live pairing agent and stream the response via SSE.

    Security:
    - Session must belong to current_user.id (IDOR prevention via DB scoped query).
    - Session must have status == "active".
    - Returns 404 for unowned or non-existent sessions (no existence oracle leak).
    - Returns 409 if a concurrent prompt is already in flight.

    SSE event stream uses:
        event: thought         — streaming agent reasoning tokens
        event: tool_call       — agent tool invocation notification
        event: file_diff       — staged file patch
        event: error           — error payload
        event: done            — stream termination marker

    Response headers:
        Content-Type: text/event-stream
        Cache-Control: no-cache
        Connection: keep-alive
        X-Accel-Buffering: no
    """
    # Drop retained completed turns past their replay TTL (bounded memory).
    _sweep_expired_turns()

    # Object-level authorization: session must be active and owned by caller.
    stmt = select(AgentSession).where(
        AgentSession.id == session_id,
        AgentSession.user_id == current_user.id,
    )
    result = await db.execute(stmt)
    session: Optional[AgentSession] = result.scalars().first()

    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
        )
    if session.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Session is not active (status={session.status!r}). Cannot submit prompt.",
        )

    # Parse resumption event ID if provided
    parsed_last_event_id: Optional[int] = None
    if last_event_id_header and last_event_id_header.isdigit():
        parsed_last_event_id = int(last_event_id_header)
    elif last_event_id_query is not None:
        parsed_last_event_id = last_event_id_query

    # A resumption request reattaches to the live turn for this session
    # instead of starting a duplicate orchestrator. The replay buffer on the
    # existing queue supplies every event after the resumption ID.
    if parsed_last_event_id is not None:
        existing = _ACTIVE_TURNS.get(str(session_id))
        if existing is not None:
            existing_queue, existing_task = existing
            if not existing_task.done():
                # The original turn already owns this prompt; this request's
                # message/model/provider are intentionally ignored.
                if existing_queue.resume_gap(parsed_last_event_id):
                    raise HTTPException(
                        status_code=status.HTTP_410_GONE,
                        detail=(
                            "RESUME_GAP: Last-Event-ID predates the retained replay "
                            "window; some events were evicted before this reconnect. "
                            "Reload the session state and send your message again "
                            "as a new request."
                        ),
                    )
                return StreamingResponse(
                    existing_queue.stream(last_event_id=parsed_last_event_id),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                    },
                )
            if not _completed_turn_expired(str(session_id)):
                # Completed but retained: replay the terminal outcome so the
                # client does not resubmit an already-executed turn. Served
                # from the retained buffer only — attaching a live stream to
                # a dead turn could park on _q.get() forever.
                if existing_queue.resume_gap(parsed_last_event_id):
                    raise HTTPException(
                        status_code=status.HTTP_410_GONE,
                        detail=(
                            "RESUME_GAP: Last-Event-ID predates the retained replay "
                            "window; some events were evicted before this reconnect. "
                            "Reload the session state and send your message again "
                            "as a new request."
                        ),
                    )
                replay = existing_queue.replay_after(parsed_last_event_id)

                async def _replay_only() -> AsyncGenerator[str, None]:
                    for chunk in replay:
                        yield chunk

                return StreamingResponse(
                    _replay_only(),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                    },
                )
            _forget_turn(str(session_id))

        # No live turn in this local execution environment.
        # Query DB for latest turn_id from stream events for cross-environment Lambda replay.
        from app.models import SessionStreamEvent

        latest_turn_stmt = (
            select(SessionStreamEvent.turn_id)
            .where(SessionStreamEvent.session_id == session_id)
            .order_by(SessionStreamEvent.id.desc())
            .limit(1)
        )
        latest_turn_res = await db.execute(latest_turn_stmt)
        active_turn_id = latest_turn_res.scalars().first()

        # If turn is active elsewhere, the orchestrator might be in the middle of
        # turn startup (clearing old events and publishing turn_start). Retry turn
        # discovery with bounded exponential backoff + full jitter under a total
        # deadline until events for the new turn appear. Resume is read-only
        # (SELECTs only — no turn effects are applied), so a retry can never
        # double-apply; on exhaustion surface 409 instead of looping silently.
        if await _is_turn_active_elsewhere(db, session_id):
            if active_turn_id is None:
                deadline = time.monotonic() + _TURN_DISCOVERY_DEADLINE_S
                for attempt in range(1, _TURN_DISCOVERY_MAX_ATTEMPTS + 1):
                    # Cap the *planned* sleep by the remaining deadline budget so
                    # one oversized jitter cannot push a single attempt past
                    # `_TURN_DISCOVERY_DEADLINE_S`. A negative/zero remaining
                    # budget means we are already past the deadline — exit
                    # without sleeping so the caller sees 409 promptly.
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    cap = min(
                        _TURN_DISCOVERY_BASE_DELAY_S * (2 ** (attempt - 1)),
                        _TURN_DISCOVERY_MAX_DELAY_S,
                    )
                    delay = _random.uniform(0, min(cap, remaining))
                    await _asyncio.sleep(delay)
                    retry_res = await db.execute(latest_turn_stmt)
                    active_turn_id = retry_res.scalars().first()
                    if active_turn_id is not None:
                        break
                    logger.warning(
                        "chat resume: turn discovery attempt %d/%d found no turn "
                        "for session %s; retrying",
                        attempt,
                        _TURN_DISCOVERY_MAX_ATTEMPTS,
                        session_id,
                    )
                    # Re-check after the DB read too — a slow read may have
                    # consumed the rest of the budget, in which case we exit
                    # instead of returning a stale value on the next iteration.
                    if time.monotonic() >= deadline:
                        break
                if active_turn_id is None:
                    logger.error(
                        "chat resume: turn discovery exhausted %d attempts for "
                        "session %s; turn still active elsewhere",
                        _TURN_DISCOVERY_MAX_ATTEMPTS,
                        session_id,
                    )

            # If still None or has no events after discovery retry
            if active_turn_id is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "TURN_ACTIVE_ELSEWHERE: a turn for this session is still running "
                        "on another worker. Retry this resumption shortly; keep the "
                        "same Last-Event-ID."
                    ),
                )
        elif active_turn_id is None:
            active_turn_id = session.active_turn_id

        if active_turn_id is None:
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="No resumable turn for this session. Please send your message again as a new request.",
            )

        event_bounds_stmt = (
            select(
                func.min(SessionStreamEvent.event_id),
                func.max(SessionStreamEvent.event_id),
            )
            .where(
                SessionStreamEvent.session_id == session_id,
                SessionStreamEvent.turn_id == active_turn_id,
            )
        )
        bounds_res = await db.execute(event_bounds_stmt)
        min_event_id, max_event_id = bounds_res.first() or (None, None)

        if min_event_id is None:
            if await _is_turn_active_elsewhere(db, session_id):
                return StreamingResponse(
                    _stream_from_db(
                        session_id, active_turn_id, parsed_last_event_id, request=request
                    ),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                    },
                )
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="No resumable turn for this session. Please send your message again as a new request.",
            )

        if parsed_last_event_id < min_event_id - 1 or parsed_last_event_id < 0:
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail=(
                    "RESUME_GAP: Last-Event-ID predates the retained replay "
                    "window; some events were evicted before this reconnect. "
                    "Reload the session state and send your message again "
                    "as a new request."
                ),
            )

        return StreamingResponse(
            _stream_from_db(
                session_id, active_turn_id, parsed_last_event_id, request=request
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # Overlapping non-resumption POST while a turn owns this session: reject
    # before starting a second orchestrator. The live entry stays untouched,
    # and the caller gets an explicit busy signal instead of a turn it could
    # never resume (the row lock would yield SESSION_BUSY anyway). Resumption
    # POSTs (with Last-Event-ID) took the reattach path above and never reach
    # here. _register_turn remains as the race guard for simultaneous POSTs.
    if _live_turn(session_id) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "SESSION_BUSY: another prompt is already in progress for this "
                "session. Please wait for it to finish and send your message again."
            ),
        )

    # Resolve GitHub installation token for file reads — best-effort.
    try:
        from sqlalchemy.orm import selectinload as _sel

        repo_stmt = (
            select(AgentSession)
            .options(_sel(AgentSession.repo))
            .where(AgentSession.id == session_id)
        )
        repo_result = await db.execute(repo_stmt)
        full_session: Optional[AgentSession] = repo_result.scalars().first()
        repo = full_session.repo if full_session else None
        gh_token: Optional[str] = await get_installation_token(repo) if repo else None
    except Exception:
        gh_token = None

    # Build the SSE queue and orchestrator.
    queue = SseQueue()
    orchestrator = SessionOrchestrator(
        session_id=session_id,
        db=db,
        gh_token=gh_token,
        current_user=current_user,
    )

    # Launch the orchestrator in a background task so the StreamingResponse
    # generator can start yielding immediately while the LLM runs.
    task = _asyncio.create_task(
        orchestrator.run(
            user_message=body.message,
            queue=queue,
            model=body.model,
            provider=body.provider,
        )
    )
    _BACKGROUND_TASKS.add(task)
    _register_turn(session_id, queue, task)

    def _on_orchestrator_done(t: _asyncio.Task) -> None:
        _BACKGROUND_TASKS.discard(t)
        entry = _ACTIVE_TURNS.get(str(session_id))
        if entry is not None and entry[1] is t:
            if t.cancelled():
                # No terminal event was emitted; retaining the queue could
                # hang a reattaching consumer on _q.get() forever.
                _forget_turn(str(session_id))
            else:
                # Retain the completed turn for a bounded replay period so a
                # client that missed the terminal event replays the outcome
                # instead of getting 410 and resubmitting a duplicate turn.
                _TURN_COMPLETED_AT[str(session_id)] = time.monotonic()
        if not t.cancelled():
            exc = t.exception()
            if exc:
                logger.error(
                    "session_orchestrator background task failed for session %s: %s",
                    session_id,
                    exc,
                    exc_info=exc,
                )

    task.add_done_callback(_on_orchestrator_done)

    return StreamingResponse(
        queue.stream(last_event_id=parsed_last_event_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/sessions/{session_id}/verify",
    response_model=SandboxVerificationOut,
)
async def verify_session(
    session_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SandboxVerificationOut:
    """
    Dispatch all currently staged patches to the isolated sandbox runner.

    Requires:
    - Session must belong to current_user.id.
    - Session must be active.
    - At least one staged patch must exist.

    Calls the sandbox verifier subagent (`backend/app/subagents/sandbox_verifier.py`)
    which triggers a GitHub Actions CI workflow on the isolated mirror repo.

    Returns SandboxVerificationOut with status, passed, run_url, and logs.
    """
    # Object-level authorization + active guard.
    stmt = (
        select(AgentSession)
        .options(selectinload(AgentSession.repo))
        .where(
            AgentSession.id == session_id,
            AgentSession.user_id == current_user.id,
        )
    )
    result = await db.execute(stmt)
    session: Optional[AgentSession] = result.scalars().first()

    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
        )
    if session.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Session is not active (status={session.status!r}). Cannot verify.",
        )

    staged_patches: dict[str, str] = session.staged_patches or {}
    if not staged_patches:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No staged patches to verify. Stage at least one patch via the chat agent first.",
        )

    repo = session.repo

    # Resolve GitHub installation token.
    try:
        gh_token: Optional[str] = await get_installation_token(repo)
    except Exception:
        gh_token = None

    # Dispatch to sandbox verifier.
    from app.subagents.sandbox_verifier import verify_session_patches

    try:
        verification_result = await verify_session_patches(
            session=session,
            repo=repo,
            staged_patches=staged_patches,
            gh_token=gh_token,
        )
    except Exception as exc:
        logger.error(
            "sessions: sandbox verification failed for session=%s: %s", session_id, exc
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Sandbox verification failed. Please try again.",
        )

    return SandboxVerificationOut(
        status=verification_result.get("status", "failed"),
        passed=verification_result.get("passed", False),
        run_url=verification_result.get("run_url"),
        logs=verification_result.get("logs"),
    )


# ---------------------------------------------------------------------------
# Phase 3 -- Commit Publisher: POST /sessions/{session_id}/commit
# ---------------------------------------------------------------------------


@router.post(
    "/sessions/{session_id}/commit",
    response_model=SessionCommitOut,
)
async def commit_session(
    session_id: uuid.UUID,
    body: SessionCommitIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SessionCommitOut:
    """
    Apply staged patches, push a commit to the session branch, and open a PR.

    Security:
    - Session must belong to current_user.id (IDOR prevention).
    - Session must be active (400 if closed/completed).
    - staged_patches must be non-empty (422 if empty).
    - Returns 404 for unowned/non-existent sessions.

    Commit pipeline: delegates to `publish_session_patches` (see its docstring
    for the branch/path/write-access/mint/patch/commit/ref/PR/rollback steps).
    This endpoint only owns auth, the row lock, fast guards, the
    session-namespace branch check, and error mapping.
    """
    from app.services.session_commit import (
        SessionCommitError,
        minted_session_branch,
        publish_session_patches,
    )

    # ------------------------------------------------------------------
    # 1. Load session with repo join -- object-level auth + concurrency lock.
    # ------------------------------------------------------------------
    from sqlalchemy.exc import DBAPIError, OperationalError

    try:
        stmt = (
            select(AgentSession)
            .options(selectinload(AgentSession.repo))
            .where(
                AgentSession.id == session_id,
                AgentSession.user_id == current_user.id,
            )
            .with_for_update(nowait=True)
        )
        result = await db.execute(stmt)
        session: Optional[AgentSession] = result.scalars().first()
    except (OperationalError, DBAPIError) as exc:
        # Only NOWAIT lock contention (Postgres 55P03) is retryable. Any
        # other DB failure must not masquerade as a lock conflict, or
        # clients would retry a non-transient error forever.
        if not _is_row_lock_conflict(exc):
            logger.error(
                "sessions/commit: session %s DB error (not a lock conflict): %s",
                session_id,
                exc,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to access the session. Please try again.",
            ) from exc
        logger.warning("sessions/commit: session %s lock conflict: %s", session_id, exc)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Another operation is currently locking this session. Please retry.",
        ) from exc

    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
        )

    # ------------------------------------------------------------------
    # 2. Fast state guards (the shared publisher re-checks all of them).
    # ------------------------------------------------------------------
    if session.status != "active":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Cannot commit a session with status={session.status!r}. "
                "Only active sessions can be committed."
            ),
        )

    if not (session.staged_patches or {}):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No staged patches to commit. Stage at least one file patch via the chat agent.",
        )

    repo = session.repo

    # Dashboard branch override must stay inside this session's namespace.
    # The agent tool path always mints haunter/session-<id hex[:12]>; an arbitrary
    # override (e.g. a teammate's feat/ branch) would publish onto a foreign
    # ref, so only the minted name (or the already-minted session value)
    # is accepted here.
    if body.branch_name is not None:
        minted_branch = minted_session_branch(session.id)
        namespaced_current = (
            isinstance(session.branch_name, str)
            and session.branch_name.startswith("haunter/session-")
        )
        if body.branch_name != minted_branch and not (
            namespaced_current and body.branch_name == session.branch_name
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Invalid branch_name {body.branch_name!r}: the commit "
                    "endpoint only publishes to this session's branch "
                    f"({minted_branch})."
                ),
            )

    # ------------------------------------------------------------------
    # 3-8. Shared publisher: branch safety, path revalidation, write access,
    #      token minting, patch application, commit, ref update, PR, and
    #      persistence all live in one place so the session agent and this
    #      endpoint cannot drift apart. The publisher mints the installation
    #      token only after every guard passes.
    # ------------------------------------------------------------------
    try:
        result = await publish_session_patches(
            session=session,
            repo=repo,
            title=body.title,
            body=body.body,
            current_user=current_user,
            db=db,
            target_branch=body.branch_name,
        )
    except SessionCommitError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=str(exc),
        ) from exc

    return SessionCommitOut(
        pr_url=result.pr_url,
        pr_number=result.pr_number,
        commit_sha=result.commit_sha,
    )


# ---------------------------------------------------------------------------
# Phase 6 — Clarification: POST /sessions/{session_id}/clarify
# ---------------------------------------------------------------------------


@router.post(
    "/sessions/{session_id}/clarify",
    response_model=SessionOut,
)
async def clarify_session(
    session_id: uuid.UUID,
    body: ClarificationIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SessionOut:
    """
    Submit user clarification response to resume an agent pairing session.

    Security & invariants:
    - Enforces object-level ownership (session.user_id == current_user.id).
    - Returns 404 for unowned or non-existent sessions (no IDOR existence leak).
    - Rejects with 400 Bad Request if session.status != "awaiting_clarification".
    - Appends chosen option to session.conversation_history:
      {"role": "user", "content": f"[User Clarification Response]: {body.response}"}
    - Resets session.status = "active" and session.waiting_input = None.
    - Commits changes and returns SessionOut.
    """
    stmt = (
        select(AgentSession)
        .options(selectinload(AgentSession.repo))
        .where(
            AgentSession.id == session_id,
            AgentSession.user_id == current_user.id,
        )
    )
    result = await db.execute(stmt)
    session: Optional[AgentSession] = result.scalars().first()

    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
        )

    if session.status != "awaiting_clarification":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Session is not awaiting clarification (current status: {session.status!r}). "
                "Cannot submit clarification."
            ),
        )

    # Append response to conversation history.
    history: list[dict[str, Any]] = list(session.conversation_history or [])
    history.append(
        {
            "role": "user",
            "content": f"[User Clarification Response]: {body.response}",
        }
    )
    session.conversation_history = history

    # Reset session status and clear waiting_input.
    session.status = "active"
    session.waiting_input = None
    repo = session.repo
    await db.commit()
    await db.refresh(session)

    return _map_session_to_out(session, repo)


# ---------------------------------------------------------------------------
# Phase 7 — Checkpoint Restore: POST /sessions/{session_id}/checkpoints/{checkpoint_id}/restore
# ---------------------------------------------------------------------------


@router.post(
    "/sessions/{session_id}/checkpoints/{checkpoint_id}/restore",
    response_model=SessionOut,
)
async def restore_checkpoint(
    session_id: uuid.UUID,
    checkpoint_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SessionOut:
    """
    Restore a session to a prior checkpoint by ID.

    Security invariants:
    - Enforces object-level ownership (session.user_id == current_user.id).
    - Returns 404 for unowned or non-existent sessions (no IDOR existence leak).
    - Returns 404 if checkpoint_id is not found in session.checkpoints.
    - Reverts staged_patches and truncates conversation_history to the checkpoint snapshot.
    - Commits changes and returns updated SessionOut.
    """
    from sqlalchemy.exc import DBAPIError, OperationalError

    try:
        stmt = (
            select(AgentSession)
            .options(selectinload(AgentSession.repo))
            .where(
                AgentSession.id == session_id,
                AgentSession.user_id == current_user.id,
            )
            .with_for_update(nowait=True)
        )
        result = await db.execute(stmt)
        session: Optional[AgentSession] = result.scalars().first()
    except (OperationalError, DBAPIError) as exc:
        # Only NOWAIT lock contention (Postgres 55P03) is retryable. Any
        # other DB failure must not masquerade as a lock conflict, or
        # clients would retry a non-transient error forever.
        if not _is_row_lock_conflict(exc):
            logger.error(
                "sessions/restore_checkpoint: session %s DB error (not a lock conflict): %s",
                session_id,
                exc,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to access the session. Please try again.",
            ) from exc
        logger.warning(
            "sessions/restore_checkpoint: session %s lock conflict: %s",
            session_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Another operation is currently locking this session. Please retry.",
        ) from exc

    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
        )

    # Publishing is irreversible: the publisher marks the session completed and
    # records the PR in conversation_history. Restoring would erase that record,
    # so refuse before touching checkpoints, patches, or history.
    if session.status != "active":
        if session.status == "completed":
            restore_detail = (
                "Cannot restore a checkpoint after the session has been published."
            )
        else:
            restore_detail = (
                f"Cannot restore a checkpoint while the session is {session.status}."
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=restore_detail,
        )

    # Verify checkpoint exists before calling restore.
    checkpoints: list[dict[str, Any]] = list(session.checkpoints or [])
    cp = next((c for c in checkpoints if c.get("checkpoint_id") == checkpoint_id), None)
    if cp is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Checkpoint '{checkpoint_id}' not found in session.",
        )

    # Restore state directly (no SSE queue needed for the REST endpoint path).
    cp_idx = checkpoints.index(cp)
    session.checkpoints = list(checkpoints[: cp_idx + 1])
    session.staged_patches = dict(cp["staged_patches"])
    history_length: int = cp["history_length"]
    session.conversation_history = list(
        (session.conversation_history or [])[:history_length]
    )
    repo = session.repo
    await db.commit()
    await db.refresh(session)

    logger.info(
        "sessions/restore_checkpoint: session %s restored to checkpoint %s",
        session_id,
        checkpoint_id,
    )

    return _map_session_to_out(session, repo)
