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

import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
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
from app.services.session_orchestrator import SessionOrchestrator
from app.services.session_streamer import SseQueue

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sessions"])

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


@router.post("/sessions", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
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
    repo_stmt = select(Repo).where(Repo.id == body.repo_id, Repo.user_id == current_user.id)
    repo_result = await db.execute(repo_stmt)
    repo: Optional[Repo] = repo_result.scalars().first()
    if not repo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repository not found")

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

    # Resolve target branch.
    branch_name: str = body.branch_name or repo.default_branch or "main"

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
            branch=branch_name,
            token=gh_token,
        )
    except GitHubResourceNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Branch '{branch_name}' not found in repository {repo.owner}/{repo.name}",
        )
    except GitHubClientError as exc:
        logger.error(
            "GitHub API error resolving branch SHA for %s/%s branch %s: %s",
            repo.owner, repo.name, branch_name, exc,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to resolve branch SHA from GitHub. Please try again.",
        )

    session = AgentSession(
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

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
            session_id, repo.owner, repo.name, session.base_sha, exc,
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
        if any(p.startswith(prefix) or f"/{prefix}" in f"/{p}" for prefix in _TREE_IGNORE_PREFIXES):
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    if session.status != "closed":
        session.status = "closed"
        session.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(session)

    return _map_session_to_out(session, session.repo)


# ---------------------------------------------------------------------------
# Phase 2 — Chat (SSE Streaming) & Verify endpoints
# ---------------------------------------------------------------------------


@router.post("/sessions/{session_id}/chat")
async def chat_session(
    session_id: uuid.UUID,
    body: SessionChatIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
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
    # Object-level authorization: session must be active and owned by caller.
    stmt = (
        select(AgentSession)
        .where(
            AgentSession.id == session_id,
            AgentSession.user_id == current_user.id,
        )
    )
    result = await db.execute(stmt)
    session: Optional[AgentSession] = result.scalars().first()

    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    if session.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Session is not active (status={session.status!r}). Cannot submit prompt.",
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
    )

    import asyncio as _asyncio

    # Launch the orchestrator in a background task so the StreamingResponse
    # generator can start yielding immediately while the LLM runs.
    _asyncio.ensure_future(
        orchestrator.run(
            user_message=body.message,
            queue=queue,
            model=body.model,
            provider=body.provider,
        )
    )

    return StreamingResponse(
        queue.stream(),
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
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

    Commit pipeline:
    1. Fetch base file content at session.base_sha for each staged file.
    2. Apply unified diff via patch_applier.apply_unified_diff.
    3. Create Git blob for each patched file.
    4. Create a Git tree rooted at session.base_sha.
    5. Create a Git commit.
    6. Update the session branch ref.
    7. Open a PR against the repo default branch.
    8. Mark session status="completed", record PR metadata in conversation_history.
    """
    from datetime import datetime, timezone

    from app.github_client import (
        GitHubClientError as _GHErr,
        create_blob as _create_blob,
        create_git_commit as _create_commit,
        create_git_tree as _create_tree,
        create_pull_request as _create_pr,
        fetch_file_content as _fetch_file,
        update_branch_ref as _update_ref,
    )
    from app.services.patch_applier import apply_unified_diff

    # ------------------------------------------------------------------
    # 1. Load session with repo join -- object-level auth.
    # ------------------------------------------------------------------
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    # ------------------------------------------------------------------
    # 2. State guards.
    # ------------------------------------------------------------------
    if session.status != "active":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Cannot commit a session with status={session.status!r}. "
                "Only active sessions can be committed."
            ),
        )

    staged_patches: dict[str, str] = session.staged_patches or {}
    if not staged_patches:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No staged patches to commit. Stage at least one file patch via the chat agent.",
        )

    repo = session.repo

    # ------------------------------------------------------------------
    # 3. Resolve GitHub installation token.
    # ------------------------------------------------------------------
    try:
        gh_token: Optional[str] = await get_installation_token(repo)
    except Exception:
        gh_token = None

    # ------------------------------------------------------------------
    # 4. Apply patches: fetch base content, apply diff, create blobs.
    # ------------------------------------------------------------------
    tree_entries: list[dict] = []

    for file_path, patch_text in staged_patches.items():
        base_content: Optional[str] = await _fetch_file(
            owner=repo.owner,
            repo=repo.name,
            path=file_path,
            sha=session.base_sha,
            token=gh_token,
        )
        original = base_content if base_content is not None else ""

        try:
            patched_content = apply_unified_diff(original, patch_text)
        except ValueError as exc:
            logger.error(
                "sessions/commit: patch application failed for %s in session %s: %s",
                file_path, session_id, exc,
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Patch for '{file_path}' could not be applied cleanly: {exc}",
            )

        try:
            blob_sha = await _create_blob(
                owner=repo.owner,
                repo=repo.name,
                content=patched_content,
                encoding="utf-8",
                installation_token=gh_token,
            )
        except _GHErr as exc:
            logger.error(
                "sessions/commit: create_blob failed for %s in session %s: %s",
                file_path, session_id, exc,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to create Git blob for '{file_path}' via GitHub API: {exc}",
            )

        tree_entries.append({
            "path": file_path,
            "mode": "100644",
            "type": "blob",
            "sha": blob_sha,
        })

    # ------------------------------------------------------------------
    # 5. Create Git tree (rooted at base_sha as base_tree).
    # ------------------------------------------------------------------
    try:
        tree_sha = await _create_tree(
            owner=repo.owner,
            repo=repo.name,
            tree=tree_entries,
            base_tree=session.base_sha,
            installation_token=gh_token,
        )
    except _GHErr as exc:
        logger.error("sessions/commit: create_git_tree failed for session %s: %s", session_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to create Git tree via GitHub API: {exc}",
        )

    # ------------------------------------------------------------------
    # 6. Create Git commit.
    # ------------------------------------------------------------------
    commit_message = body.title
    if body.body:
        commit_message = f"{body.title}\n\n{body.body}"

    try:
        commit_sha = await _create_commit(
            owner=repo.owner,
            repo=repo.name,
            message=commit_message,
            tree_sha=tree_sha,
            parents=[session.base_sha],
            installation_token=gh_token,
        )
    except _GHErr as exc:
        logger.error("sessions/commit: create_git_commit failed for session %s: %s", session_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to create Git commit via GitHub API: {exc}",
        )

    # ------------------------------------------------------------------
    # 7. Update branch ref.
    # ------------------------------------------------------------------
    try:
        await _update_ref(
            owner=repo.owner,
            repo=repo.name,
            branch=session.branch_name,
            commit_sha=commit_sha,
            force=False,
            installation_token=gh_token,
        )
    except _GHErr as exc:
        logger.error(
            "sessions/commit: update_branch_ref failed for session %s branch %s: %s",
            session_id, session.branch_name, exc,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to update branch ref via GitHub API: {exc}",
        )

    # ------------------------------------------------------------------
    # 8. Open pull request against repo default branch.
    # ------------------------------------------------------------------
    pr_base = repo.default_branch or "main"
    try:
        pr_data: dict = await _create_pr(
            owner=repo.owner,
            repo=repo.name,
            title=body.title,
            head=session.branch_name,
            base=pr_base,
            body=body.body,
            installation_token=gh_token,
        )
    except _GHErr as exc:
        logger.error("sessions/commit: create_pull_request failed for session %s: %s", session_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to open pull request via GitHub API: {exc}",
        )

    pr_url: str = pr_data.get("html_url", "")
    pr_number: int = pr_data.get("number", 0)

    # ------------------------------------------------------------------
    # 9. Persist: mark session completed, record PR metadata.
    # ------------------------------------------------------------------
    session.status = "completed"
    session.updated_at = datetime.now(timezone.utc)

    history: list = list(session.conversation_history or [])
    history.append({
        "role": "system",
        "content": (
            f"Commit published. PR #{pr_number} opened: {pr_url}\n"
            f"Commit SHA: {commit_sha}"
        ),
        "pr_url": pr_url,
        "pr_number": pr_number,
        "commit_sha": commit_sha,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    session.conversation_history = history

    await db.commit()

    logger.info(
        "sessions/commit: session %s committed -- PR #%s %s commit=%s",
        session_id, pr_number, pr_url, commit_sha,
    )

    return SessionCommitOut(
        pr_url=pr_url,
        pr_number=pr_number,
        commit_sha=commit_sha,
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

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
    history.append({
        "role": "user",
        "content": f"[User Clarification Response]: {body.response}",
    })
    session.conversation_history = history

    # Reset session status and clear waiting_input.
    session.status = "active"
    session.waiting_input = None
    session.updated_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(session)

    return _map_session_to_out(session, session.repo)


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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    # Verify checkpoint exists before calling restore.
    checkpoints: list[dict[str, Any]] = list(session.checkpoints or [])
    cp = next((c for c in checkpoints if c.get("checkpoint_id") == checkpoint_id), None)
    if cp is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Checkpoint '{checkpoint_id}' not found in session.",
        )

    # Restore state directly (no SSE queue needed for the REST endpoint path).
    session.staged_patches = dict(cp["staged_patches"])
    history_length: int = cp["history_length"]
    session.conversation_history = list((session.conversation_history or [])[:history_length])
    session.updated_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(session)

    logger.info(
        "sessions/restore_checkpoint: session %s restored to checkpoint %s",
        session_id, checkpoint_id,
    )

    return _map_session_to_out(session, session.repo)
