# HAUNTER — Future Roadmap & Architectural Specifications

This document defines the high-priority engineering roadmap for Haunter. It outlines technical specifications, system topologies, database schemas, target touchpoints, and acceptance criteria for upcoming core features.

---

## 1. Autonomous Push-Level Code Review & Actionable Remediation Sentinel

### 1.1 Problem Statement
While Haunter excels at repairing failing CI pipelines post-mortem, many regressions (security vulnerabilities, unhandled edge cases, N+1 query patterns, and subtle logic bugs) pass unit test suites and get merged into production unnoticed. Traditional AI review bots create review fatigue by spamming cosmetic nitpicks without actionable solutions. Haunter's review sentinel provides deep, AST-grounded reviews on every `push` and `pull_request` event, backed by pre-verified candidate patch suggestions.

### 1.2 System Architecture & Flow
```
Developer pushes code (git push / PR opened)
                 │
                 ▼
GitHub Webhook (`push` / `pull_request.synchronize`)
                 │
                 ▼
FastAPI Webhook Gateway (`backend/app/api/webhooks.py`)
                 │ (Verifies HMAC, filters bot commits & WIP drafts)
                 ▼
Async Self-Invocation (`lambda_handler.py` -> Review Pipeline)
                 │
                 ├── 1. Context Gatherer: Fetches full commit diff, touched AST symbols, and related PR context
                 ├── 2. Review Subagent: Analyzes code against 4 core dimensions:
                 │      • Security & Vulnerability (OWASP, SQLi, SSRF, secret leaks)
                 │      • Logic & Edge Cases (null dereferences, race conditions, off-by-one errors)
                 │      • Performance & Resources (N+1 queries, unclosed handles, unbounded arrays)
                 │      • Backward Compatibility (breaking changes to exported public signatures)
                 │
                 ├── 3. Sandbox Verifier (Optional for actionable patches):
                 │      Verifies suggested code patches inside isolated GitHub Actions test mirror
                 │
                 └── 4. GitHub Review Publisher:
                        Posts structured review with Risk Score (0-100), inline GitHub suggestion diffs,
                        and 1-click commit action hooks.
```

### 1.3 Database Schema Changes (`backend/app/models.py`)
```python
class CodeReview(Base):
    __tablename__ = "code_reviews"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"), index=True)
    commit_sha: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    pr_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    risk_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0) # 0 (safe) to 100 (critical risk)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    findings: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="completed")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    repo: Mapped["Repo"] = relationship("Repo", backref="code_reviews")
```

### 1.4 Target Touchpoints
- **Webhook Ingestion:** [`backend/app/webhooks.py`](file:///C:/Users/bari2/Desktop/Haunter/backend/app/webhooks.py)
  - Handle `pull_request` (`opened`, `synchronize`) and `push` events.
  - Filter out automated bot commits, merge commits, and draft PRs.
- **Review Subagent:** `backend/app/subagents/code_reviewer.py`
  - Strict Pydantic output schema (`ReviewOutput(risk_score: int, summary: str, comments: list[ReviewComment])`).
  - Generate GitHub-compatible markdown suggestion blocks: ```` ```suggestion ````.
- **GitHub Client:** [`backend/app/github_client.py`](file:///C:/Users/bari2/Desktop/Haunter/backend/app/github_client.py)
  - Submit batch reviews via `POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews`.
- **Frontend Dashboard:** `frontend/src/app/reviews/page.tsx`
  - Visual review history with risk score gauges, filterable severity tags, and inline diff inspector.

### 1.5 Acceptance Criteria
- Full reviews posted to GitHub PRs within 30 seconds of push.
- Zero generic boilerplate nitpicks; every finding must cite specific lines and failure mechanisms.
- All code suggestions formatted as valid, syntax-correct GitHub suggestion blocks.

---

## 2. Cloud Agentic Live Session (In-Browser Interactive Pairing Workspace)

### 2.1 Problem Statement
Autonomous fire-and-forget fixing is powerful, but developers frequently want to collaborate directly with the agent—guiding architecture decisions, tweaking candidate patches, asking questions against the codebase, and testing changes interactively without checking out branches locally. The Cloud Agentic Live Session provides a real-time, browser-based pairing environment with live code editing, streaming reasoning, and instantaneous sandbox verification.

### 2.2 System Architecture & Flow
```
User clicks "Start Agentic Session" on Dashboard or PR
                           │
                           ▼
FastAPI Session Controller (`backend/app/routers/sessions.py`)
  • Initializes session state in Neon Postgres
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
  ├── 3. Trigger Sandbox Runner in background against live edits
  └── 4. User reviews live diffs -> clicks "Commit & Open PR"
                           │
                           ▼
GitHub Git Data API directly commits changes to target repository branch
```

### 2.3 Database Schema Changes (`backend/app/models.py`)
```python
class AgentSession(Base):
    __tablename__ = "agent_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="Pairing Session")
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="active") # active, completed, closed
    branch_name: Mapped[str] = mapped_column(String(255), nullable=False)
    base_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    conversation_history: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    staged_patches: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False, default=dict) # file_path -> patch
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    user: Mapped["User"] = relationship("User", backref="agent_sessions")
    repo: Mapped["Repo"] = relationship("Repo", backref="agent_sessions")
```

### 2.4 Target Touchpoints
- **Session API Router:** `backend/app/routers/sessions.py`
  - `POST /sessions`: Create new session, resolve branch & base commit.
  - `POST /sessions/{id}/chat`: Stream prompt to LLM and return SSE token chunks.
  - `POST /sessions/{id}/verify`: Dispatch active staged patches to sandbox runner.
  - `POST /sessions/{id}/commit`: Push staged changes to GitHub branch via Git Data API.
- **SSE Event Streamer:** `backend/app/services/session_streamer.py`
  - Standardized SSE protocol: `event: thought`, `event: file_diff`, `event: sandbox_status`, `event: done`.
- **Frontend Workspace:** `frontend/src/app/sessions/[id]/page.tsx`
  - Integrated `@monaco-editor/react` with syntax highlighting and side-by-side diff mode.
  - Interactive chat panel with token usage and real-time execution status timeline.
  - Top action bar with "Run Tests in Sandbox" and "Commit to GitHub" buttons.

### 2.5 Security & Rate Controls
- Session isolation: Every session is scoped strictly to authenticated user's repository access permissions.
- Concurrent session limit: Maximum of 2 active live pairing sessions per user to prevent compute/memory exhaustion.
- Ephemeral state: Staged patches stored in Neon Postgres with 24-hour expiration for inactive sessions.

### 2.6 Acceptance Criteria
- Token and diff streaming latency under 250ms from LLM generation to Monaco editor display.
- Ability to edit multiple files in a single session and verify them in the sandbox runner without local git installation.
- 1-click commit pushes directly to user's repository with verified author attribution.
