# FUTURE02.md — Autonomous Auditor Bot, Per-Repo Settings & Live Session Audits

This document specifies the technical architecture, database schemas, API contracts, webhook routing, slash commands, and frontend design systems for **Haunter v2 Feature Extensions**:
1. **Autonomous Read-Only Reviewer & Auditor Bot ("Auditor Mode")**
2. **Granular Per-Repository Feature Toggles & Settings Dashboard (`/settings`)**
3. **Live Session Slash Commands: Whole-Repo Security Scan & Code Audit (`/security-scan` & `/repo-audit`)**

---

## Table of Contents

- [Roadmap Overview & Status Matrix](#roadmap-overview--status-matrix)
- [1. Autonomous Read-Only Reviewer & Auditor Bot ("Auditor Mode")](#1-autonomous-read-only-reviewer--auditor-bot-auditor-mode)
  - [1.1 Motivation & Core Philosophy](#11-motivation--core-philosophy)
  - [1.2 Webhook Triggers & Activation Matrix](#12-webhook-triggers--activation-matrix)
  - [1.3 Audit Pipeline & Subagent Architecture](#13-audit-pipeline--subagent-architecture)
  - [1.4 GitHub PR & Commit Comment Report Format](#14-github-pr--commit-comment-report-format)
  - [1.5 Phase 1 — Webhook Routing & Trigger Filter Engine](#15-phase-1--webhook-routing--trigger-filter-engine)
  - [1.6 Phase 2 — Multi-Perspective Audit Core & Report Formatter](#16-phase-2--multi-perspective-audit-core--report-formatter)
  - [1.7 Phase 3 — GitHub Comment & Review Annotation Publisher](#17-phase-3--github-comment--review-annotation-publisher)
  - [1.8 Exit Criteria & Invariants](#18-exit-criteria--invariants)
- [2. Granular Per-Repository Feature Toggles & Settings Dashboard (`/settings`)](#2-granular-per-repository-feature-toggles--settings-dashboard-settings)
  - [2.1 Overview & Architecture](#21-overview--architecture)
  - [2.2 Database Schema (`repo_settings` & `repo_features`)](#22-database-schema-repo_settings--repo_features)
  - [2.3 Backend API Contracts](#23-backend-api-contracts)
  - [2.4 Feature Flags & Presets Matrix](#24-feature-flags--presets-matrix)
  - [2.5 Frontend: Studio-Grade `/settings` Surface](#25-frontend-studio-grade-settings-surface)
  - [2.6 Phased Implementation Plan](#26-phased-implementation-plan)
  - [2.7 Exit Criteria & Verification](#27-exit-criteria--verification)
- [3. Live Session Slash Commands: Whole-Repo Security Scan & Code Audit](#3-live-session-slash-commands-whole-repo-security-scan--code-audit)
  - [3.1 Motivation & Session Interactivity](#31-motivation--session-interactivity)
  - [3.2 Slash Command Syntax & Capabilities](#32-slash-command-syntax--capabilities)
  - [3.3 Pipeline Execution & Subagent Delegation Model](#33-pipeline-execution--subagent-delegation-model)
  - [3.4 SSE Stream Protocol & Live Progress Events](#34-sse-stream-protocol--live-progress-events)
  - [3.5 Frontend: Interactive Audit Report Card & 1-Click Fix](#35-frontend-interactive-audit-report-card--1-click-fix)
  - [3.6 Phased Implementation Plan](#36-phased-implementation-plan)
  - [3.7 Exit Criteria & Verification](#37-exit-criteria--verification)
- [4. Global Non-Goals & Boundaries](#4-global-non-goals--boundaries)

---

## Roadmap Overview & Status Matrix

| # | Feature / Initiative | Target Scope | Runtime / Dependencies | Status |
|---|---|---|---|---|
| **1** | **Autonomous Read-Only Auditor Bot** | Backend (`app/routers/webhooks.py`, `app/subagents/auditor.py`, `app/github_client.py`) | GitHub Webhooks (`pull_request`, `workflow_run`), Neon DB, Multi-Perspective LLM Review | `Planned` |
| **2** | **Per-Repo Settings & Feature Dashboard** | Backend (`app/routers/settings.py`, `app/models.py`, `app/db.py`) + Frontend (`/settings`, `useRepoSettings.ts`) | Next.js App Router, Tailwind tokens, Neon Postgres Async SQLAlchemy, GitHub App OAuth | `Planned` |
| **3** | **Live Session `/security-scan` & `/repo-audit`** | Backend (`session_orchestrator.py`, `session_tools/audit.py`, `session_streamer.py`) + Frontend (`SessionWorkspaceClient.tsx`, `AuditReportCard.tsx`) | SSE stream queue, AST analysis, security ruleset, interactive 1-click surgical fix | `Planned` |

---

## 1. Autonomous Read-Only Reviewer & Auditor Bot ("Auditor Mode")

| Attribute | Details |
|---|---|
| **Feature** | Autonomous Read-Only Reviewer & Security Auditor Bot for GitHub Repositories |
| **Scope** | Backend (`webhooks.py`, `subagents/auditor.py`, `services/audit_pipeline.py`, `github_client.py`) |
| **Author** | MD Sufiyan Bari |
| **Status** | `Planned` |

### 1.1 Motivation & Core Philosophy

Many engineering teams want Haunter's deep reasoning and diagnostic capabilities without granting write access to open pull requests or automatically push code commits. 

**Auditor Mode** turns Haunter into a dedicated, read-only AI Senior Staff Engineer & Security Reviewer:
- **Zero Automated Code Mutations**: The bot never creates branches or opens unsolicited PRs.
- **Deep Multi-Vector Analysis**: Audits diffs for security vulnerabilities (OWASP, secret leaks, SSRF, injection), race conditions, edge-case regressions, and architectural anti-patterns.
- **Self-Healing Enablement**: Emits structured, high-confidence reports with exact code snippets and unified diffs so developers (or their local coding agents like Cursor/Aider/Claude) can implement fixes immediately.

---

### 1.2 Webhook Triggers & Activation Matrix

Users configure when Haunter wakes up per repository via granular event flags:

| Trigger Event | GitHub Webhook Type | Audit Target | Typical Use Case |
|---|---|---|---|
| **`on_pull_request`** | `pull_request.opened`, `pull_request.synchronize` | PR unified diff + base branch context | Comprehensive code review & PR gating |
| **`on_ci_failure`** | `workflow_run.completed` (status: `failure`) | CI step logs + failing commit diff | Root-cause diagnosis with zero automated PR |
| **`on_ci_success`** | `workflow_run.completed` (status: `success`) | Merged build diff + performance audit | Post-build sanity & regression check |
| **`on_manual_mention`** | `issue_comment.created` (`@haunter audit`) | Targeted PR thread / issue context | On-demand ad-hoc review requested by devs |

---

### 1.3 Audit Pipeline & Subagent Architecture

```
GitHub Webhook Event (PR Opened / Synced or CI Workflow Run)
    │
    ▼
FastAPI Router (POST /api/webhook)
    │
    ├──► 1. Verify GitHub Webhook HMAC SHA-256 Signature
    ├──► 2. Load Repo Feature Settings (Check if Auditor Mode is enabled for this event)
    │
    ▼
Audit Pipeline Dispatcher (app/services/audit_pipeline.py)
    │
    ├─── Step 1: Context Gathering (GitHub API)
    │       ├─► Fetch unified PR diff or commit range diff
    │       ├─► Fetch changed file contents + ast outline
    │       └─► Fetch relevant CI error logs (if CI failure trigger)
    │
    ├─── Step 2: Multi-Perspective Parallel Subagent Review
    │       ├─► [Security Subagent]     → CWE/OWASP, SSRF, Authz, Secrets, Injection
    │       ├─► [Correctness Subagent]  → Logic bugs, null pointers, off-by-one, race conditions
    │       ├─► [Performance Subagent]  → N+1 queries, unindexed filters, blocking I/O in async
    │       └─► [Architecture Subagent] → API contracts, backward compatibility, type safety
    │
    ├─── Step 3: Synthesis & Confidence Scoring
    │       └─► Filter false positives, aggregate severity ([BLOCKER], [WARNING], [NOTE])
    │
    └─── Step 4: GitHub API Publisher
            ├─► Post summary review comment with expandable sections
            └─► Post inline review annotations on affected diff lines
```

---

### 1.4 GitHub PR & Commit Comment Report Format

When an audit completes, Haunter formats its findings into an actionable GitHub Markdown review:

````markdown
## 🛡️ Haunter Autonomous Audit Report

**Status:** ⚠️ Action Recommended (2 Warnings, 0 Blockers)  
**Confidence Score:** `94%`  
**Audit Target:** Commit `a8f3b21` / PR `#42`  
**Engine:** `nemotron-3.5-lightning`

---

### 🔍 Executive Summary
This PR introduces the new JWT refresh token rotation handler. While the overall async flow is correct, two potential security and performance issues were detected in `backend/app/auth.py`.

---

### 🚨 Findings & Recommendations

#### 1. ⚠️ [SECURITY] Non-Constant-Time Token Comparison
- **File:** `backend/app/auth.py#L84`
- **Category:** Timing Attack / Insecure Crypto
- **Impact:** Comparing refresh tokens with standard `==` leaks timing information byte-by-byte.
- **Suggested Fix:**
```python
# Before
if token != stored_token:
    raise Unauthorized()

# After
import hmac
if not hmac.compare_digest(token, stored_token):
    raise Unauthorized()
```

#### 2. ⚠️ [PERFORMANCE] Unindexed Tenant Query Filter
- **File:** `backend/app/routers/tokens.py#L32`
- **Category:** Database Query Efficiency
- **Impact:** Filtering `WHERE tenant_id = :id AND expires_at > now()` without a composite index forces a sequential table scan on large sessions tables.

---

### 🛠️ Remediation Unified Diff
```diff
--- a/backend/app/auth.py
+++ b/backend/app/auth.py
@@ -84,2 +84,3 @@
-if token != stored_token:
+import hmac
+if not hmac.compare_digest(token, stored_token):
     raise Unauthorized()
```
*Generated autonomously by Haunter Guardian Mode. Zero changes were committed to your branch.*
````

---

### 1.5 Phase 1 — Webhook Routing & Trigger Filter Engine

**Goal:** Intercept incoming GitHub webhooks (`pull_request`, `workflow_run`, `issue_comment`), verify cryptographic signatures, query `repo_settings` to evaluate whether Auditor Mode is activated for this specific event type, and dispatch background tasks.

**Files to create/modify:**
- `backend/app/routers/webhooks.py` ← extend event handlers
- `backend/app/services/audit_pipeline.py` ← **new file**
- `backend/tests/test_audit_webhook_routing.py` ← **new test file**

---

### 1.6 Phase 2 — Multi-Perspective Audit Core & Report Formatter

**Goal:** Implement the parallel subagent analysis engine that inspects AST diffs, checks rules against the codebase skills checklist (`backend/skills.md`, `frontend/skills.md`), and synthesizes structured findings with confidence metrics.

**Files to create/modify:**
- `backend/app/subagents/auditor.py` ← **new file**
- `backend/app/llm/prompts/audit_prompts.py` ← **new file**
- `backend/tests/test_auditor_core.py` ← **new test file**

---

### 1.7 Phase 3 — GitHub Comment & Review Annotation Publisher

**Goal:** Publish formatted audit reports and line-specific review comments to the GitHub Pull Request Review API (`POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews`) or Commit Comments API.

**Files to create/modify:**
- `backend/app/github_client.py` ← add `create_pr_review`, `create_commit_comment`
- `backend/tests/test_github_audit_publisher.py` ← **new test file**

---

### 1.8 Exit Criteria & Invariants

- [ ] Webhook signature verification uses `hmac.compare_digest` with raw payload bytes.
- [ ] Auditor mode **never** attempts `git push`, branch creation, or PR creation.
- [ ] If confidence score falls below 75%, comments are suppressed or tagged as informational notes only.
- [ ] All unit tests in `test_audit_*.py` pass 100% green with mocked GitHub/LLM calls.

---

## 2. Granular Per-Repository Feature Toggles & Settings Dashboard (`/settings`)

| Attribute | Details |
|---|---|
| **Feature** | Dedicated `/settings` Control Center for Granular Per-Repo Feature Enablement |
| **Scope** | Backend (`routers/settings.py`, `models.py`, `schemas/settings.py`) + Frontend (`/settings`, `useRepoSettings.ts`, `SettingsWorkspace.tsx`) |
| **Author** | MD Sufiyan Bari |
| **Status** | `Planned` |

### 2.1 Overview & Architecture

Every organization and repository has different needs:
- An open-source library may only want **Read-Only Auditor Mode** on PRs.
- An internal backend service may want **Full Autonomous CI Healing & Auto-PRs**.
- A frontend web app may want **WebContainer Live Dev Preview** enabled for live studio sessions.

The `/settings` dashboard provides a centralized management matrix where repository owners toggle features on/off with 1-click preset profiles and granular override controls.

```
Frontend (/settings)
    │
    ├──► Repo Selector Dropdown (Lists all synced GitHub repos)
    ├──► Preset Profile Switcher (Autonomous DevOps | Auditor Only | Live Studio | Custom)
    ├──► Granular Feature Switches (Auto-Fixer, Sandbox, Auditor, Live Preview, Subagents)
    └──► Trigger Configuration (On PR, On CI Failure, Branch Allowlist)
    │
    ▼ [REST API: PATCH /api/settings/repos/{repo_id}]
FastAPI Settings Router (backend/app/routers/settings.py)
    │
    ▼ [SQLAlchemy Async Engine]
Neon PostgreSQL DB (repo_settings & repo_features tables)
```

---

### 2.2 Database Schema (`repo_settings` & `repo_features`)

Create an Alembic migration for the dedicated `repo_settings` table:

```python
# backend/app/models.py
from sqlalchemy import Boolean, Column, ForeignKey, Integer, String, JSON, DateTime, func
from sqlalchemy.orm import relationship
from app.db import Base

class RepoSettings(Base):
    __tablename__ = "repo_settings"

    id = Column(Integer, primary_key=True, index=True)
    repo_id = Column(Integer, ForeignKey("repos.id", ondelete="CASCADE"), unique=True, nullable=False, index=True)
    
    # Preset Profile: "full_autonomous" | "auditor_only" | "live_studio_only" | "custom"
    preset_profile = Column(String(50), nullable=False, default="full_autonomous")
    
    # Feature Toggles
    enable_auto_fixer = Column(Boolean, nullable=False, default=True)
    enable_auditor_mode = Column(Boolean, nullable=False, default=False)
    enable_live_sessions = Column(Boolean, nullable=False, default=True)
    enable_webcontainer_preview = Column(Boolean, nullable=False, default=True)
    enable_ci_sandbox = Column(Boolean, nullable=False, default=True)
    enable_subagents = Column(Boolean, nullable=False, default=True)
    
    # Auditor Activation Triggers
    audit_trigger_on_pr = Column(Boolean, nullable=False, default=True)
    audit_trigger_on_ci_failure = Column(Boolean, nullable=False, default=True)
    audit_trigger_on_ci_success = Column(Boolean, nullable=False, default=False)
    
    # Filter Configurations
    monitored_branches = Column(JSON, nullable=False, default=lambda: ["main", "master"])
    ignore_draft_prs = Column(Boolean, nullable=False, default=True)
    min_confidence_threshold = Column(Integer, nullable=False, default=80)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    repo = relationship("Repo", back_populates="settings")
```

---

### 2.3 Backend API Contracts

#### 1. `GET /api/settings/repos`
Returns all linked repositories with their active settings and feature toggles:
```json
[
  {
    "repo_id": 101,
    "repo_full_name": "kaiizer777/Haunter",
    "preset_profile": "full_autonomous",
    "features": {
      "auto_fixer": true,
      "auditor_mode": false,
      "live_sessions": true,
      "webcontainer_preview": true,
      "ci_sandbox": true,
      "subagents": true
    },
    "audit_triggers": {
      "on_pr": true,
      "on_ci_failure": true,
      "on_ci_success": false
    },
    "monitored_branches": ["main"]
  }
]
```

#### 2. `PATCH /api/settings/repos/{repo_id}`
Updates granular feature switches or changes preset profile:
```json
// Request Body
{
  "preset_profile": "auditor_only",
  "features": {
    "auto_fixer": false,
    "auditor_mode": true
  },
  "audit_triggers": {
    "on_pr": true,
    "on_ci_failure": false
  }
}
```

#### 3. `POST /api/settings/repos/{repo_id}/preset/{preset_name}`
1-click shortcut to apply standard configuration profiles.

---

### 2.4 Feature Flags & Presets Matrix

| Preset Name | Auto-Fixer | Auditor Mode | CI Sandbox | Live Dev Studio | WebContainer | Target Persona |
|---|---|---|---|---|---|---|
| **`full_autonomous`** (Default) | ✅ ON | ❌ OFF | ✅ ON | ✅ ON | ✅ ON | Solo devs / fast teams seeking autonomous PR healing |
| **`auditor_only`** | ❌ OFF | ✅ ON | ❌ OFF | ❌ OFF | ❌ OFF | Large teams wanting non-invasive code reviews & security audits |
| **`live_studio_only`** | ❌ OFF | ❌ OFF | ✅ ON | ✅ ON | ✅ ON | Devs using Haunter exclusively as a browser IDE |
| **`custom`** | ⚙️ Configurable | ⚙️ Configurable | ⚙️ Configurable | ⚙️ Configurable | ⚙️ Configurable | Advanced custom workflows |

---

### 2.5 Frontend: Studio-Grade `/settings` Surface

**Target Route:** `frontend/src/app/settings/page.tsx`

#### UI Layout & Component Hierarchy
```
┌─────────────────────────────────────────────────────────────────────────────┐
│  SETTINGS CONTROL CENTER                                                    │
│  Manage feature toggles and autonomous agents across your repositories      │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  [ Select Repository: ▼ kaiizer777/Haunter                       ]          │
│                                                                             │
│  ┌─ PRESET PROFILES ──────────────────────────────────────────────────────┐ │
│  │ [ 🚀 Full Autonomous ]  [ 🛡️ Auditor Only ]  [ 💻 Studio Only ]  [ ⚙️ ] │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
│                                                                             │
│  ┌─ CORE CAPABILITIES ───────────────────────────────────────────────────┐ │
│  │ ⚡ Autonomous Fixer & Auto-PR Generator                     [ TOGGLE ON ]│ │
│  │    Automatically diagnoses CI failures and opens surgical PRs.         │ │
│  │                                                                        │ │
│  │ 🛡️ Read-Only Guardian Reviewer Bot                         [ TOGGLE OFF]│ │
│  │    Posts in-depth audit reports on PRs/commits without modifying code.  │ │
│  │                                                                        │ │
│  │ 🧪 Isolated CI Sandbox Verification                        [ TOGGLE ON ]│ │
│  │    Executes tests inside ephemeral GitHub Actions mirror runners.      │ │
│  │                                                                        │ │
│  │ 🌐 WebContainer In-Browser Dev Server                      [ TOGGLE ON ]│ │
│  │    Boots live Node.js dev server with real-time HMR inside browser.     │ │
│  │                                                                        │ │
│  │ 🤖 Subagent Delegation Engine                              [ TOGGLE ON ]│ │
│  │    Enables multi-role specialized subagents in live cloud sessions.     │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
│                                                                             │
│  ┌─ AUDITOR BOT TRIGGERS (Active when Auditor Mode is Enabled) ───────────┐ │
│  │ ☑️ Trigger on Pull Requests (Opened / Synchronized)                     │ │
│  │ ☑️ Trigger on CI Workflow Failures                                      │ │
│  │ ☐ Trigger on Successful CI Runs                                         │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
│                                                                             │
│  [ Save Settings ]                               [ ↺ Reset to Default ]    │
└─────────────────────────────────────────────────────────────────────────────┘
```

#### Styling & Component Discipline
- **Palette**: `bg-[#09090b]`, `zinc-900` cards, `zinc-800` subtle border strokes.
- **Interactive 3D-ish Toggles**: Switch pills use painted-light highlights (`inset 0 1px 0 rgba(255,255,255,0.2)` + soft lift shadow).
- **Responsive**: Container queries for clean multi-column layouts across desktop and mobile.
- **Feedback**: Optimistic toggle updates with rollback on API errors, accompanied by non-intrusive toast notifications.

---

### 2.6 Phased Implementation Plan

- **Phase 1: Database Model & Alembic Migration**
  - Create `RepoSettings` model in `backend/app/models.py`.
  - Generate Alembic revision `add_repo_settings_table.py` using direct DB URL.
  - Run migration on Neon PostgreSQL.

- **Phase 2: FastAPI Settings Endpoints & Service Layer**
  - Implement `backend/app/routers/settings.py` with Pydantic v2 schemas.
  - Add helper `get_repo_settings(repo_id)` in `backend/app/services/settings_service.py` with in-memory caching.
  - Write test suite `backend/tests/test_settings_api.py`.

- **Phase 3: Webhook & Feature Enforcement Interceptors**
  - Update `webhooks.py`, `session_orchestrator.py`, and `orchestrator.py` to check `repo_settings` before executing features.
  - If a feature is toggled OFF for a repo, the backend exits early with clear logs.

- **Phase 4: Frontend Settings Page & Navigation**
  - Create `frontend/src/app/settings/page.tsx` and `frontend/src/components/settings/RepoSettingsCard.tsx`.
  - Add `/settings` route link to the sidebar navigation (`frontend/src/components/layout/sidebar.tsx`).
  - Verify zero TypeScript errors (`tsc --noEmit`).

---

### 2.7 Exit Criteria & Verification

- [ ] `pytest backend/tests/test_settings_api.py` passes 100% green.
- [ ] Changing a repo's preset to `auditor_only` disables automatic PR creation on CI failures and enables review comments on PR webhooks.
- [ ] Setting switches in `/settings` persists cleanly to Neon DB without page reload.
- [ ] `tsc --noEmit` on `frontend/` succeeds with zero errors.

---

## 3. Live Session Slash Commands: Whole-Repo Security Scan & Code Audit

| Attribute | Details |
|---|---|
| **Feature** | Interactive `/security-scan` & `/repo-audit` Slash Commands for Live Cloud Sessions (`/session`) |
| **Scope** | Backend (`session_orchestrator.py`, `session_tools/audit.py`, `session_streamer.py`) + Frontend (`SessionWorkspaceClient.tsx`, `AuditReportCard.tsx`) |
| **Author** | MD Sufiyan Bari |
| **Status** | `Planned` |

### 3.1 Motivation & Session Interactivity

When developing inside a live cloud session (`/sessions`), users frequently want to sanity-check the entire codebase before pushing commits or creating PRs. Instead of switching out of the session into external CLI scanners, Haunter provides direct chat-driven slash commands:
- **`/security-scan`**: Initiates a full repository security audit (hardcoded secrets, OWASP Top 10 vulnerabilities, unvalidated trust boundaries, insecure crypto, SSRF vectors, dangerous dependencies).
- **`/repo-audit`**: Initiates a deep codebase architecture and health audit (N+1 queries, unindexed filters, blocking sync calls inside `async def`, schema validation gaps, dead code, missing error boundaries).

---

### 3.2 Slash Command Syntax & Capabilities

Users can type commands directly into the session prompt:

```
/security-scan                  → Scans entire repository with default high-sensitivity ruleset
/security-scan --path=backend/  → Scopes security scan strictly to backend directory
/repo-audit                     → Full architecture, performance, and best-practice audit
/repo-audit --strict            → Audits against elite backend & frontend production checklists
```

---

### 3.3 Pipeline Execution & Subagent Delegation Model

When a slash command is submitted, `SessionOrchestrator` intercepts it before calling the standard LLM turn and delegates to the specialized subagents:

```
User enters `/security-scan` or `/repo-audit` in Session Chat
       │
       ▼
SessionOrchestrator Command Router
       │
       ├─► Intercepts slash command syntax
       ├─► Emits SSE `audit_scan_start` { scan_type: "security" | "architecture", target_path }
       │
       ▼
Subagent Runner Dispatch (`code_guardian` / `repo_navigator`)
       │
       ├──► 1. File Discovery & AST Traversal (`glob_files`, `get_file_outline`)
       ├──► 2. Rule-Engine Evaluation:
       │       • Regex Secret Scanners (Stripe keys, AWS secrets, GitHub tokens, JWTs)
       │       • Crypto Scanners (`timingSafeEqual` / `hmac.compare_digest` vs `==`)
       │       • Async Scanners (`time.sleep` / `requests.get` inside `async def`)
       │       • Query Scanners (Unindexed foreign keys, missing `selectinload`)
       │       • Security Boundary Scanners (Unparsed raw webhook bodies, missing Zod `.strict()`)
       │
       ├──► 3. LLM Deep Synthesis & Severity Rating:
       │       • Categorizes findings: `CRITICAL` 🔴 | `HIGH` 🟠 | `MEDIUM` 🟡 | `LOW` 🔵
       │       • Produces exact file paths, line ranges, and reproduction context
       │
       ▼
SSE Stream Queue Emission (`session_streamer.py`):
       ├── `audit_progress` → { files_scanned: 42, total_files: 120, current_file: "auth.py" }
       └── `audit_report`   → { scan_type, findings: [...], score: 92, summary }
       │
       ▼
Frontend `AuditReportCard` Component Renders in Chat Feed
```

---

### 3.4 SSE Stream Protocol & Live Progress Events

Add to `session_streamer.py` `_ALLOWED_EVENTS`:

```python
"audit_scan_start",     # { scan_type: str, total_files_estimated: int }
"audit_progress",       # { files_scanned: int, current_file: str }
"audit_report",         # { scan_type: str, findings: list[dict], health_score: int }
```

#### Event Data Schema for `audit_report`:
```json
{
  "scan_type": "security_scan",
  "health_score": 88,
  "summary": "Scanned 64 files. Found 1 Critical vulnerability and 2 Warnings.",
  "findings": [
    {
      "id": "SEC-001",
      "severity": "CRITICAL",
      "category": "Insecure Direct Object Reference",
      "file_path": "backend/app/routers/sessions.py",
      "line_start": 42,
      "line_end": 45,
      "title": "Missing Tenant Boundary Check on Session Lookup",
      "description": "Query fetches session by primary key without scoping by authenticated user's tenant_id.",
      "suggested_fix": "WHERE id = :session_id AND tenant_id = :current_tenant_id",
      "can_auto_fix": true
    }
  ]
}
```

---

### 3.5 Frontend: Interactive Audit Report Card & 1-Click Fix

When the frontend receives `audit_report`, it renders an interactive `AuditReportCard` inside `SessionWorkspaceClient`:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ 🛡️ REPOSITORY SECURITY SCAN REPORT                         Score: 88/100   │
│ 1 Critical  •  2 Warnings  •  0 Info                      [ Filter: All ▼ ] │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│ 🔴 [CRITICAL] Missing Tenant Boundary Check on Session Lookup               │
│    File: backend/app/routers/sessions.py:42-45                              │
│    Query fetches session by primary key without scoping by current tenant.  │
│                                                                             │
│    Suggested Patch:                                                         │
│    ┌──────────────────────────────────────────────────────────────────────┐ │
│    │ - query = select(Session).where(Session.id == session_id)            │ │
│    │ + query = select(Session).where(Session.id == session_id,            │ │
│    │ +                               Session.tenant_id == current_tenant) │ │
│    └──────────────────────────────────────────────────────────────────────┘ │
│                                                                             │
│    [ 🤖 Stage Surgical Fix with Agent ]          [ ↗ Open File in Editor ] │
│                                                                             │
├─────────────────────────────────────────────────────────────────────────────┤
│ 🟡 [WARNING] Non-Constant-Time Refresh Token Comparison                     │
│    File: backend/app/auth.py:84                                             │
│    [ 🤖 Stage Surgical Fix with Agent ]          [ ↗ Open File in Editor ] │
└─────────────────────────────────────────────────────────────────────────────┘
```

#### 1-Click "Stage Surgical Fix with Agent" Flow:
When the user clicks the fix button on any finding card:
1. The client automatically sends an internal prompt to the session:  
   `"Apply surgical fix for finding SEC-001 in backend/app/routers/sessions.py:42-45"`.
2. The orchestrator invokes `feature_architect` or `bug_hunter` to patch the file via `str_replace`.
3. The staged patch immediately appears in the Monaco Diff Editor for user review and commit.

---

### 3.6 Phased Implementation Plan

- **Phase 1: Backend Slash Command Interceptor & Audit Tools**
  - Create `backend/app/services/session_tools/audit.py` containing rule checkers (regex, AST traversal, async checks, token comparisons).
  - Add slash command parsing in `session_orchestrator.py` for `/security-scan` and `/repo-audit`.
  - Register SSE events `audit_scan_start`, `audit_progress`, `audit_report` in `session_streamer.py`.
  - Write test suite `backend/tests/test_session_slash_audits.py`.

- **Phase 2: Frontend Interactive Report Cards & Slash Command Auto-Complete**
  - Add slash command autocomplete popup when the user types `/` into the session chat input.
  - Implement `frontend/src/components/workspace/AuditReportCard.tsx` with severity filters and diff highlights.
  - Connect the "Stage Surgical Fix" button to dispatch prompt actions into `useSessionStream`.

- **Phase 3: Integration & Performance Validation**
  - Verify full repo scan completes within <15 seconds on standard repositories by utilizing parallel chunked file inspection.
  - Ensure zero false positives on known security patterns.

---

### 3.7 Exit Criteria & Verification

- [ ] Typing `/security-scan` in live session triggers SSE stream and renders `AuditReportCard`.
- [ ] Typing `/repo-audit` executes architectural analysis against `backend/skills.md` & `frontend/skills.md`.
- [ ] Clicking "Stage Surgical Fix" on a finding generates a valid `file_diff` patch staged in the Monaco editor.
- [ ] `pytest backend/tests/test_session_slash_audits.py -v` passes 100% green.
- [ ] `tsc --noEmit` in `frontend/` succeeds with zero errors.

---

## 4. Global Non-Goals & Boundaries

- **No Multi-Org Permission Escalation**: Only repository admins and authenticated repository collaborators can modify repository feature settings.
- **No Direct Code Mutations in Auditor Mode**: Guardian/Auditor mode is strictly read-only; write tokens are never used to push commits or create branches under this mode.
- **No Unbounded Webhook Execution**: Webhooks for repositories that have disabled all features exit immediately after HMAC verification (latency < 50ms).
- **No Blocking Full-Repo Scans in Request Thread**: Slash command audit scans execute as non-blocking async background tasks, streaming progress events via SSE without freezing the session event loop.
