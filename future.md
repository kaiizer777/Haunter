# HAUNTER — Future Roadmap & Architectural Specifications

This document defines the architectural specification and implementation plan for the **Cloud Agentic Live Session** (In-Browser Interactive Pairing Workspace).

---

## Cloud Agentic Live Session (In-Browser Interactive Pairing Workspace)

### 1. Architectural Overview & Topology
Autonomous fire-and-forget fixing is powerful, but developers frequently want to collaborate directly with the agent—guiding architecture decisions, tweaking candidate patches, asking questions against the codebase, and testing changes interactively without checking out branches locally. The Cloud Agentic Live Session provides a real-time, browser-based pairing environment with live code editing, streaming reasoning, and instantaneous sandbox verification.

```
User clicks "Start Agentic Session" on Dashboard or PR
                           │
                           ▼
FastAPI Session Controller (`backend/app/routers/sessions.py`)
  • Initializes session state in Neon Postgres (`agent_sessions`)
  • Enforces max 2 active sessions per user
  • Fetches target repo file tree & symbols via GitHub Git Data API
                           │
                           ▼
Cloudflare Next.js Frontend (`frontend/src/app/sessions/[id]/page.tsx`)
  ┌─────────────────────────┬─────────────────────────┐
  │   Agent Chat Dock       │   Monaco Multi-File IDE │
  │   (SSE Streaming Logs)  │   (Interactive Editor)  │
  └─────────────────────────┴─────────────────────────┘
                           │
User submits prompt: "Refactor user authentication to support passkeys"
                           │
                           ▼
Live Orchestrator Engine (`backend/app/services/session_orchestrator.py`)
  ├── 1. Stream agent thoughts & tool calls via Server-Sent Events (SSE)
  ├── 2. Stream unified diffs & file edits directly into Monaco Editor
  ├── 3. Trigger Sandbox Runner in background against live edits (`POST /sessions/{id}/verify`)
  └── 4. User reviews live diffs -> clicks "Commit & Open PR" (`POST /sessions/{id}/commit`)
                           │
                           ▼
GitHub Git Data API directly commits changes to target repository branch & opens PR
```

---

### 2. Database Schema (`backend/app/models.py`)

```python
class AgentSession(Base):
    __tablename__ = "agent_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="Pairing Session")
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="active")  # active, completed, closed
    branch_name: Mapped[str] = mapped_column(String(255), nullable=False)
    base_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    conversation_history: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    staged_patches: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False, default=dict)  # file_path -> patch
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    user: Mapped["User"] = relationship("User", backref="agent_sessions")
    repo: Mapped["Repo"] = relationship("Repo", backref="agent_sessions")
```

---

## Phased Execution Roadmap

The implementation is partitioned into three sequential, production-grade phases:

### Phase 1: Database Foundation, Session Controller & Git Tree Ingestion
- **Alembic Migration**: `backend/alembic/versions/f6a7b8c9d0e1_add_agent_sessions_table.py` (chained from `e5f6a7b8c9d0`). Server defaults for JSONB (`'[]'::jsonb`, `'{}'::jsonb`) and timestamps (`now()`).
- **SQLAlchemy 2.0 Model**: Add `AgentSession` to `backend/app/models.py` with foreign keys, index decorators, and bidirectional relationships on `User` and `Repo`.
- **Pydantic Schemas**: Define `SessionCreateIn`, `SessionOut`, `SessionListOut`, `SessionUpdateIn` with strict validation.
- **GitHub Git Data API Client**: Extend `backend/app/github_client.py` with `fetch_git_tree` (`GET /repos/{owner}/{repo}/git/trees/{sha}?recursive=1`) and `fetch_branch_sha`.
- **Session API Router**: `backend/app/routers/sessions.py`:
  - `POST /sessions`: Validate repo ownership, resolve branch base SHA, enforce max 2 concurrent active sessions per user, initialize session row.
  - `GET /sessions`: List active/recent sessions for current user with repo metadata.
  - `GET /sessions/{id}`: Detailed session state with staged patches and conversation history (404 on unowned to avoid IDOR leaks).
  - `POST /sessions/{id}/close`: Mark session status as `closed`.
- **Router Registration**: Register router in `backend/main.py`.
- **Test Suite**: `backend/tests/test_sessions_api.py` covering multi-tenant isolation, concurrent session limits, branch resolution, and CRUD lifecycle.

### Phase 2: Real-Time SSE Streamer, LLM Tool-Calling & Sandbox Verification
- **SSE Protocol Specification**: Standardized SSE chunk wire format (`event: thought`, `event: file_diff`, `event: sandbox_status`, `event: error`, `event: done`).
- **SSE Streamer Engine**: `backend/app/services/session_streamer.py` providing async generator for `StreamingResponse`.
- **Live Session Orchestrator**: `backend/app/services/session_orchestrator.py`:
  - Maintains conversation memory in `conversation_history`.
  - Parses LLM tool calls for file reading and patch staging.
  - Updates `staged_patches` atomically in Neon Postgres.
- **Verification Endpoint**: `POST /sessions/{id}/verify` dispatching active staged patches to the isolated sandbox runner and returning test status.
- **Test Suite**: `backend/tests/test_session_orchestrator.py` testing streaming generation, prompt building, and patch state updates.

### Phase 3: Monaco Multi-File IDE Frontend, Live Diff Viewer & 1-Click Commit Publisher
- **Monaco Editor Integration**: `@monaco-editor/react` embedded inside `frontend/src/app/sessions/[id]/page.tsx` with side-by-side diff view and file tab switcher.
- **Live Stream Consumer**: Custom React hook (`useSessionStream`) connecting to SSE endpoint and updating chat timeline and Monaco models with sub-250ms latency.
- **Top Action Bar**: Interactive triggers for "Run Tests in Sandbox" and "Commit & Open PR".
- **GitHub Commit Publisher**: `POST /sessions/{id}/commit` creating Git tree objects and commit blobs via GitHub Git Data API, pushing to branch, and opening PR with author attribution.
- **Sessions Dashboard**: `frontend/src/app/sessions/page.tsx` listing user sessions with tactile obsidian styling, status tags, and resume buttons.
- **Sidebar Integration**: Link in `frontend/src/components/layout/sidebar.tsx`.
- **Frontend Verification**: Clean `tsc --noEmit` and `npm run lint`.
