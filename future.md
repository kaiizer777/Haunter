# Haunter — Future Roadmap & Technical Specifications

This document contains the end-to-end technical specifications, architectural designs, and phased execution plans for upcoming features in **Haunter**.

---

## Table of Contents

- [Roadmap Overview & Status Matrix](#roadmap-overview--status-matrix)
- [1. Specialized Subagent Delegation (`invoke_subagent`)](#1-specialized-subagent-delegation-invoke_subagent)
  - [1.1 Overview & Role Matrix](#11-overview--role-matrix)
  - [1.2 Architecture & Execution Model](#12-architecture--execution-model)
  - [1.3 Phase 1 — Subagent Core: Runner, Role Configs & SSE Events](#13-phase-1--subagent-core-runner-role-configs--sse-events)
  - [1.4 Phase 2 — Orchestrator Integration & Lead Architect Wiring](#14-phase-2--orchestrator-integration--lead-architect-wiring)
  - [1.5 Phase 3 — Frontend: Subagent Progress Cards](#15-phase-3--frontend-subagent-progress-cards)
  - [1.6 Phase 4 — Observability & Telemetry](#16-phase-4--observability--telemetry)
  - [1.7 Phase Sequencing & Dependency Graph](#17-phase-sequencing--dependency-graph)
  - [1.8 Non-Goals & Boundaries](#18-non-goals--boundaries)
- [2. WebContainer Live Dev Preview — Haunter Studio](#2-webcontainer-live-dev-preview--haunter-studio)
  - [2.1 Overview & Runtime Scope](#21-overview--runtime-scope)
  - [2.2 Architecture & Cross-Origin Isolation (COOP/COEP)](#22-architecture--cross-origin-isolation-coopcoep)
  - [2.3 Phase 1 — COOP/COEP Headers & `useWebContainer` Hook](#23-phase-1--coopcoep-headers--usewebcontainer-hook)
  - [2.4 Phase 2 — Preview Panel UI & `.env` Manager](#24-phase-2--preview-panel-ui--env-manager)
  - [2.5 Phase 3 — Agent File-Sync Loop (Live HMR via SSE `file_diff`)](#25-phase-3--agent-file-sync-loop-live-hmr-via-sse-file_diff)
  - [2.6 Phase Sequencing](#26-phase-sequencing)
  - [2.7 Non-Goals (WebContainer v1)](#27-non-goals-webcontainer-v1)
- [3. Model Configuration Architecture Overhaul & Fixes](#3-model-configuration-architecture-overhaul--fixes)
  - [3.1 Summary & Metadata](#31-summary--metadata)
  - [3.2 Identified Issues & Planned Fixes](#32-identified-issues--planned-fixes)
- [4. Live Session CI Sandbox Verification Engine](#4-live-session-ci-sandbox-verification-engine)
  - [4.1 Overview & Motivation](#41-overview--motivation)
  - [4.2 Architecture & Autonomous CI Self-Healing Loop](#42-architecture--autonomous-ci-self-healing-loop)
  - [4.3 Detailed Component Changes](#43-detailed-component-changes)
  - [4.4 Phased Implementation Plan](#44-phased-implementation-plan)
  - [4.5 Non-Goals](#45-non-goals)

---

## Roadmap Overview & Status Matrix

| # | Feature / Initiative | Target Scope | Runtime / Dependencies | Status |
|---|---|---|---|---|
| **1** | **Specialized Subagent Delegation** | Backend (`session_orchestrator.py`, `session_tools/subagents.py`, `session_streamer.py`) + Frontend (`useSessionStream.ts`, `SubagentCard`) | Async Python coroutines, SSE wire protocol, existing LLM client | `Planned` |
| **2** | **WebContainer Live Dev Preview** | Frontend-only v1 (`useWebContainer.ts`, `WebPreviewPanel.tsx`, `SessionWorkspaceClient.tsx`) | `@webcontainer/api` (WASM / Service Worker in-browser), COOP/COEP | `Planned (v1)` |
| **3** | **Model Config Overhaul & Fixes** | Backend (`model_config.py`, `llm/client.py`, `app/config.py`) + Frontend (`config/page.tsx`, `lib/api.ts`) | Neon DB `model_configs` table, OpenAI / Anthropic / Groq adapters | `Planned` |
| **4** | **Live Session CI Sandbox Engine** | Backend (`session_tools/sandbox.py`, `subagents/sandbox_verifier.py`) + Frontend (`SessionWorkspaceClient.tsx`, `TerminalDrawer`) | GitHub Actions Mirror Repos, Octokit / REST API, Live SSE logs | `Planned` |

---

## 1. Specialized Subagent Delegation (`invoke_subagent`)

| Attribute | Details |
|---|---|
| **Feature** | `invoke_subagent` — Orchestrator-dispatched specialized subagent delegation for live `/session` surface |
| **Scope** | Backend (`session_orchestrator.py`, new `session_tools/subagents.py`, `session_streamer.py`) + Frontend (`useSessionStream.ts`, SubagentCard component) |
| **Author** | MD Sufiyan Bari |
| **Status** | `Planned` |

### 1.1 Overview & Role Matrix

Currently, the `SessionOrchestrator` is a generalist agent — it handles every task itself: code exploration, feature writing, bug fixing, code review, and sandbox execution. For simple tasks this is fine. For complex multi-file features, deep bug investigations, or thorough security reviews, it burns tokens on exploration work that dilutes its focus.

This feature adds a single new LLM tool — `invoke_subagent` — to the session agent's tool surface. When the orchestrator (acting as **Lead Architect**) determines that a task warrants delegation, it calls `invoke_subagent` with a `role` and a self-contained `task` briefing. The backend spins up a focused, permission-restricted subagent worker for that role, runs it as a nested async coroutine, and streams progress events back through the existing `SseQueue` wire protocol.

#### The 5 Specialized Roles

| Role Key | Title | Primary Responsibility |
|---|---|---|
| `repo_navigator` | RepoNavigator | Codebase recon, symbol graphs, cross-file context gathering |
| `feature_architect` | FeatureArchitect | Multi-file feature implementation and scaffolding |
| `bug_hunter` | BugHunter | Root cause diagnosis, surgical fix, regression test |
| `sandbox_verifier` | SandboxVerifier | Test execution, linting, CI environment validation |
| `code_guardian` | CodeGuardian | Security review, performance audit, API compatibility check |

---

### 1.2 Architecture & Execution Model

```
SessionOrchestrator (Lead Architect / Orchestrator LLM)
    │
    │  invoke_subagent("role", "task", optional: target_files)
    ▼
SubagentRunner (new: backend/app/services/session_tools/subagents.py)
    │
    ├─── RoleConfig lookup  (tool permissions per role, capped MAX_ITERATIONS)
    │
    ├─── Nested LLM loop   (same LLMClient, same GitHub token, role-specific _TOOLS subset)
    │
    ├─── SseQueue events:
    │       subagent_start  → {role, task}
    │       tool_call       → existing wire event (reused, subagent-scoped)
    │       subagent_done   → {role, summary, patches_modified: []}
    │       error           → {code: "SUBAGENT_ERROR", role}
    │
    └─── Returns: str result summary injected as tool_result into parent LLM
```

The parent orchestrator's LLM loop receives the subagent's structured summary as a `tool` message and continues its turn with full context of what the subagent found/built.

#### Key Invariants
- Subagents share the same `staged_patches` dict (by reference) — changes made by a `feature_architect` subagent are immediately visible to the parent.
- Subagents do **not** call `invoke_subagent` themselves. Nested invocation is blocked at the dispatch layer to prevent infinite recursion.
- Each subagent has a hard `MAX_ITERATIONS` cap independent of the parent's cap.
- All existing path traversal guards, patch size limits, and `scan_security_vulnerabilities` invariants apply inside subagent tool calls identically to the parent.
- Subagents are sequential — one at a time, blocking the parent loop until `run()` returns.

---

### 1.3 Phase 1 — Subagent Core: Runner, Role Configs & SSE Events

**Goal:** Build the subagent execution engine and wire it into the existing SSE infrastructure. No new LLM tool yet — just the internal machinery that Phase 2 will plug into.

**Files to create/modify:**
- `backend/app/services/session_tools/subagents.py` ← **new file**
- `backend/app/services/session_streamer.py` ← add 2 new allowed event names + helpers
- `backend/tests/test_session_subagents.py` ← **new test file**

#### 1.3.1 `session_streamer.py` — New SSE Event Names

Add to `_ALLOWED_EVENTS` frozenset:
```python
"subagent_start",   # emitted when a subagent begins execution
"subagent_done",    # emitted when a subagent finishes
```

Add two helper methods to `SseQueue` (matching the pattern of existing `put_*` helpers):
```python
async def put_subagent_start(self, role: str, task: str) -> None:
    await self._put("subagent_start", {"role": role, "task": task})

async def put_subagent_done(
    self,
    role: str,
    summary: str,
    patches_modified: list[str],
) -> None:
    await self._put("subagent_done", {
        "role": role,
        "summary": summary,
        "patches_modified": patches_modified,
    })
```

#### 1.3.2 `session_tools/subagents.py` — RoleConfig Registry & SubagentRunner

##### Exports
```python
VALID_ROLES: frozenset[str]   # the 5 role key strings
ROLE_CONFIGS: dict[str, RoleConfig]

class SubagentError(Exception):
    role: str
    message: str

class SubagentRunner: ...
```

##### RoleConfig (frozen dataclass)
```python
@dataclass(frozen=True)
class RoleConfig:
    role: str
    allowed_tools: frozenset[str]
    max_iterations: int
    system_prompt_suffix: str
```

##### Role Permission Table
| Role | Allowed Tools | Max Iterations |
|---|---|---|
| `repo_navigator` | `grep_search`, `glob_files`, `read_file_slice`, `list_directory`, `read_file`, `get_file_outline`, `find_symbol`, `find_references`, `search_web_docs`, `fetch_web_content`, `fetch_package_metadata` | 12 |
| `feature_architect` | All `repo_navigator` tools + `str_replace`, `create_file`, `delete_file`, `apply_multi_patch`, `run_linter`, `scan_security_vulnerabilities` | 20 |
| `bug_hunter` | All `repo_navigator` tools + `git_log`, `git_blame`, `git_show`, `git_diff`, `str_replace`, `run_targeted_tests`, `run_linter`, `scan_security_vulnerabilities` | 16 |
| `sandbox_verifier` | `run_targeted_tests`, `run_linter`, `run_terminal_command`, `glob_files`, `read_file_slice` | 8 |
| `code_guardian` | All `repo_navigator` tools + `git_diff`, `git_log`, `scan_security_vulnerabilities` | 10 |

> [!NOTE]
> `code_guardian` intentionally has **no editor tools** — it is read-only by design.

##### SubagentRunner Signature
```python
class SubagentRunner:
    def __init__(
        self,
        role: str,
        task: str,
        target_files: list[str],
        repo_owner: str,
        repo_name: str,
        base_sha: str,
        staged_patches: dict[str, str],  # shared reference with parent
        session: AgentSession,
        queue: SseQueue,
        llm: LLMClient,
        gh_token: str | None,
        model: str | None,
        provider: str | None,
    ) -> None: ...

    async def run(self) -> str:
        """
        Run the subagent tool-calling loop.

        Flow:
          1. Emit subagent_start SSE event.
          2. Build role-scoped system prompt (session prompt + role suffix + target_files hint).
          3. Run LLM loop up to RoleConfig.max_iterations.
             - Dispatch tool calls through role-filtered _dispatch_subagent_tool().
             - Emit tool_call SSE events via queue (reuses existing wire format).
          4. Emit subagent_done SSE event with compact summary.
          5. Return summary string for injection as tool_result into parent LLM.

        Raises SubagentError if LLM is unavailable or zero iterations complete.
        """
```

##### Tool Dispatch Inside the Runner
```python
async def _dispatch_subagent_tool(self, tool_name: str, args: dict[str, Any]) -> str:
    # Block recursive invocation
    if tool_name == "invoke_subagent":
        return "Error: invoke_subagent is not available inside a subagent context."

    # Enforce role-level tool allowlist
    config = ROLE_CONFIGS[self.role]
    if tool_name not in config.allowed_tools:
        return (
            f"Error: tool '{tool_name}' is not permitted for role '{self.role}'. "
            f"Allowed: {sorted(config.allowed_tools)}."
        )

    # Call the same session_tools/* implementations used by the parent orchestrator
    # (imported directly — no duplication).
    ...
```

#### 1.3.3 Tests (`test_session_subagents.py`)

All tests are unit-level. Patch `LLMClient.complete` and `SseQueue` — no real GitHub calls, no real LLM calls.

| Test | What it verifies |
|---|---|
| `test_role_config_tool_allowlist` | Every role's `allowed_tools` is a proper subset of the known full `_TOOLS` name list; no typos in any allowlist. |
| `test_repo_navigator_blocks_editor_tools` | Calling `str_replace` from `repo_navigator` subagent returns the permission error string, not a file edit. |
| `test_subagent_runner_emits_sse_events` | LLM returns a single no-tool-call response; `subagent_start` and `subagent_done` are emitted on `SseQueue`. |
| `test_subagent_runner_blocks_recursive_invoke` | LLM returns `invoke_subagent` tool call; runner returns recursive invocation error string, no crash. |
| `test_subagent_runner_respects_max_iterations` | LLM always returns a tool call; runner exits cleanly after `max_iterations` with truncation notice in summary. |
| `test_staged_patches_shared_ref` | `feature_architect` subagent calls `str_replace`; parent's `staged_patches` dict is mutated (shared reference confirmed). |

#### Exit Criteria Phase 1
- `pytest backend/tests/test_session_subagents.py -v` → all 6 tests green.
- `format_sse_event("subagent_start", {...})` and `format_sse_event("subagent_done", {...})` do not raise.
- `SubagentRunner` instantiates and `run()` executes independently of `SessionOrchestrator`.

---

### 1.4 Phase 2 — Orchestrator Integration & Lead Architect Wiring

**Goal:** Add the `invoke_subagent` LLM tool definition to `_TOOLS`, update `_build_system_prompt`, and route it in `_dispatch_tool`. The orchestrator becomes the Lead Architect.

**Files to modify:**
- `backend/app/services/session_orchestrator.py`
- `backend/tests/test_session_orchestrator.py` (extend — no regressions to existing tests)

#### 1.4.1 Tool Schema — Append to `_TOOLS`

Insert after the `git_diff` tool definition (last entry in `_TOOLS`, before the closing `]`):

```python
{
    "type": "function",
    "function": {
        "name": "invoke_subagent",
        "description": (
            "Delegate a complex, focused sub-task to a specialized subagent. "
            "Use this when a request benefits from a dedicated expert rather than handling everything directly.\n"
            "Available roles:\n"
            "  - 'repo_navigator': deep codebase exploration, symbol graphs, cross-file context.\n"
            "    Use BEFORE feature_architect or bug_hunter on large or unfamiliar codebases.\n"
            "  - 'feature_architect': implements multi-file features, endpoints, models, UI components.\n"
            "    Writes and stages code — use for any significant implementation work.\n"
            "  - 'bug_hunter': diagnoses root causes from tracebacks, writes surgical fix patches\n"
            "    and regression tests. Use when the user reports a bug or unexpected behavior.\n"
            "  - 'sandbox_verifier': runs tests, linting, terminal commands to validate correctness.\n"
            "    Read-only — does NOT write or modify code.\n"
            "  - 'code_guardian': performs security, performance, and API compatibility review\n"
            "    on staged diffs. Read-only — returns structured findings only.\n"
            "The subagent runs to completion and returns a structured summary. "
            "Staged patches produced by the subagent are automatically visible to you."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "enum": [
                        "repo_navigator",
                        "feature_architect",
                        "bug_hunter",
                        "sandbox_verifier",
                        "code_guardian",
                    ],
                    "description": "The specialized subagent role to dispatch.",
                },
                "task": {
                    "type": "string",
                    "description": (
                        "Detailed, self-contained task briefing. Include: what to do, which files "
                        "are likely involved (if known), expected output, and any constraints."
                    ),
                },
                "target_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional list of relative file paths the subagent should focus on."
                    ),
                },
            },
            "required": ["role", "task"],
            "additionalProperties": False,
        },
    },
},
```

#### 1.4.2 `_build_system_prompt` — Orchestrator Identity & Tool Listing Update

In the tool listing section, after tool 28 (`git_diff`), append:
```
" 29. `invoke_subagent(role, task, target_files?)` — delegate a focused sub-task to a specialized expert subagent.\n"
"     Roles: 'repo_navigator' | 'feature_architect' | 'bug_hunter' | 'sandbox_verifier' | 'code_guardian'.\n"
```

In the behavioral rules block, append before the `staged_summary` line:
```
"You are the Lead Architect of this session. For complex tasks, delegate via invoke_subagent rather than doing everything yourself. "
"Standard implementation chain: invoke repo_navigator first on large codebases → then feature_architect → then sandbox_verifier. "
"Always validate patches with sandbox_verifier or run_targeted_tests after any feature_architect or bug_hunter run.\n"
```

#### 1.4.3 `_dispatch_tool` — Add Route

Add immediately before the `else: unknown tool` branch:

```python
elif tool_name == "invoke_subagent":
    if session is None:
        return "Error: Session context required for invoke_subagent."
    return await self._tool_invoke_subagent(
        args=args,
        repo_owner=repo_owner,
        repo_name=repo_name,
        base_sha=base_sha,
        staged_patches=staged_patches,
        session=session,
        queue=queue,
    )
```

#### 1.4.4 `_tool_invoke_subagent` Method Implementation

Add to `SessionOrchestrator`:

```python
async def _tool_invoke_subagent(
    self,
    args: dict[str, Any],
    repo_owner: str,
    repo_name: str,
    base_sha: str,
    staged_patches: dict[str, str],
    session: AgentSession,
    queue: SseQueue,
) -> str:
    from app.services.session_tools.subagents import (
        SubagentRunner,
        SubagentError,
        VALID_ROLES,
    )

    role: str = args.get("role", "")
    task: str = args.get("task", "")
    target_files: list[str] = args.get("target_files") or []

    if role not in VALID_ROLES:
        return f"Error: unknown role {role!r}. Valid roles: {sorted(VALID_ROLES)}."
    if not task.strip():
        return "Error: task must not be empty."

    runner = SubagentRunner(
        role=role,
        task=task,
        target_files=target_files,
        repo_owner=repo_owner,
        repo_name=repo_name,
        base_sha=base_sha,
        staged_patches=staged_patches,  # shared reference
        session=session,
        queue=queue,
        llm=self._llm,
        gh_token=self.gh_token,
        model=None,
        provider=None,
    )

    try:
        return await runner.run()
    except SubagentError as exc:
        logger.error(
            "session_orchestrator: subagent role=%s failed in session=%s: %s",
            role, self.session_id, exc.message,
        )
        return (
            f"Subagent '{role}' failed: {exc.message}. "
            "You may retry the delegation or proceed without it."
        )
```

#### 1.4.5 Tests — Extend `test_session_orchestrator.py`

| Test | What it verifies |
|---|---|
| `test_invoke_subagent_dispatches_runner` | `SubagentRunner.run` mocked to return a fixed summary; orchestrator tool_result contains that exact summary. |
| `test_invoke_subagent_unknown_role` | `role="totally_made_up"` → error string returned, no exception raised. |
| `test_invoke_subagent_empty_task` | Empty `task` → error string returned. |
| `test_invoke_subagent_subagent_error_is_soft` | `SubagentRunner.run` raises `SubagentError` → orchestrator returns graceful error string, parent loop continues. |

#### Exit Criteria Phase 2
- `pytest backend/tests/test_session_orchestrator.py -v` → all tests (new + existing) green, zero regressions.
- `invoke_subagent` present in `_TOOLS` with the exact schema above.
- `_build_system_prompt` output contains the Lead Architect identity lines.
- `SubagentError` is soft — the parent tool-calling loop does not terminate on subagent failure.

---

### 1.5 Phase 3 — Frontend: Subagent Progress Cards

**Goal:** Render `subagent_start` and `subagent_done` SSE events as live progress cards in the session workspace UI.

**Files to modify:**
- `frontend/src/hooks/useSessionStream.ts` — extend with two new event cases
- Session workspace component (wherever `tool_call` events render) — add `SubagentCard` inline

> [!NOTE]
> Existing `tool_call` SSE events emitted *inside* the subagent already flow through the current event handler and render in the tool call feed unchanged. Only the outer start/done wrapper cards are new.

#### 1.5.1 `useSessionStream.ts` — Event Types & Handlers

Add to the event dispatch switch/map:
```typescript
case "subagent_start": {
    const { role, task } = data as { role: string; task: string };
    onSubagentStart?.({ role, task, startedAt: Date.now() });
    break;
}
case "subagent_done": {
    const { role, summary, patches_modified } = data as {
        role: string;
        summary: string;
        patches_modified: string[];
    };
    onSubagentDone?.({ role, summary, patchesModified: patches_modified });
    break;
}
```

Extend the hook options interface:
```typescript
interface UseSessionStreamOptions {
    // ...existing fields...
    onSubagentStart?: (event: SubagentStartEvent) => void;
    onSubagentDone?: (event: SubagentDoneEvent) => void;
}

interface SubagentStartEvent {
    role: string;
    task: string;
    startedAt: number;  // Date.now() at receipt
}

interface SubagentDoneEvent {
    role: string;
    summary: string;
    patchesModified: string[];
}
```

#### 1.5.2 `SubagentCard` Component

Inline component collocated with the workspace component (separate file only if >80 lines).

**Role emoji map:**
```typescript
const ROLE_EMOJI: Record<string, string> = {
    repo_navigator: "🧭",
    feature_architect: "⚡",
    bug_hunter: "🔍",
    sandbox_verifier: "🧪",
    code_guardian: "🛡️",
};
```

**Running state** (start received, done not yet received):
- Role emoji + title chip
- First 120 chars of task text in muted color
- Animated pulse indicator on the right

**Done state** (done received):
- Green checkmark + role emoji + title chip (no pulse)
- Summary text
- File pills for each path in `patchesModified` (if non-empty), styled like existing file chips in tool_call cards

No new npm dependencies. Tailwind classes only.

#### Exit Criteria Phase 3
- `tsc --noEmit` (or `npm run build`) → zero new TypeScript errors.
- Manual session test: trigger `invoke_subagent` → SubagentCard renders start state → transitions to done state.
- No changes to existing test files.

---

### 1.6 Phase 4 — Observability & Telemetry

**Goal:** Log subagent invocations with per-role token consumption and latency for production traceability.

**Files to modify:**
- `backend/app/services/session_tools/subagents.py` — structured telemetry log after each run
- `backend/tests/test_session_subagents.py` — add one test

> [!IMPORTANT]
> Check `backend/app/models.py` before implementing: verify whether `AgentSession` has a `run_id` FK to `runs`. If present, a `run_steps` DB write can be added. If not, telemetry is structured log only — **no migration without explicit approval**.

#### 1.6.1 Telemetry in `SubagentRunner.run()`
```python
start_time = time.monotonic()

# ... subagent loop ...

latency_ms = int((time.monotonic() - start_time) * 1000)
total_input_tokens = sum(r.get("usage", {}).get("prompt_tokens", 0) for r in _llm_responses)
total_output_tokens = sum(r.get("usage", {}).get("completion_tokens", 0) for r in _llm_responses)

logger.info(
    "subagent_telemetry role=%s session=%s input_tokens=%d output_tokens=%d "
    "latency_ms=%d iterations=%d",
    self.role,
    str(self.session.id),
    total_input_tokens,
    total_output_tokens,
    latency_ms,
    completed_iterations,
)
```

#### 1.6.2 New Test
- `test_subagent_telemetry_logged` — patch `logger.info`; verify it is called with the `subagent_telemetry` prefix and correct role after `run()` completes.

#### Exit Criteria Phase 4
- `pytest backend/tests/test_session_subagents.py -v` → all tests green.
- Every subagent invocation produces a structured log line with `subagent_telemetry` prefix, role, token counts, latency_ms.
- Zero Alembic migrations introduced.

---

### 1.7 Phase Sequencing & Dependency Graph

```
Phase 1 (Subagent Core) ──► Phase 2 (Orchestrator Wiring) ──┬──► Phase 3 (Frontend Cards)
                                                            └──► Phase 4 (Telemetry)
```

Phases 3 and 4 are fully independent of each other and can be executed in either order after Phase 2 is complete.

---

### 1.8 Non-Goals & Boundaries

- **Parallel concurrent subagents** — sequential only, one at a time.
- **Subagents spawning subagents** — blocked at dispatch layer.
- **Per-subagent model/provider override** — subagents inherit the session's active model.
- **Persistent subagent memory across turns** — fresh context per `invoke_subagent` call; only `staged_patches` are shared.
- **Any changes to the CI pipeline** (`app/orchestrator.py`, `app/subagents/`) — separate system entirely.
- **Alembic migrations** without explicit confirmation that `AgentSession.run_id` exists in schema.

---

## 2. WebContainer Live Dev Preview — Haunter Studio

| Attribute | Details |
|---|---|
| **Feature** | In-browser live dev server + real-time preview for cloud agentic sessions |
| **Scope** | Frontend-only (v1). Zero new backend endpoints. Zero infrastructure cost at idle. |
| **Author** | MD Sufiyan Bari |
| **Status** | `Planned (v1)` |
| **Runtime** | StackBlitz WebContainer API — full Node.js runtime inside the user's browser via WebAssembly + Service Workers. |

### 2.1 Overview & Runtime Scope

When a user opens a Haunter cloud agentic session, they currently see: agent chat, Monaco diff editor, and a terminal drawer. They have no way to see the actual running app — they read diffs and trust the agent.

This feature adds a **"Preview"** tab to the workspace topbar. When activated:
1. A WebContainer boots in the browser (WASM Node.js runtime, ~2–4s cold start).
2. The repo's file tree is mounted into the container's virtual filesystem.
3. The container runs `npm install` → `npm run dev` and streams all output into the existing `TerminalDrawer`.
4. When the dev server is ready, a live preview `<iframe>` appears showing the running app.
5. Every time the agent stages a patch (SSE `file_diff` event), the updated file is written directly to the WebContainer filesystem — the dev server reloads via HMR in under 200ms.
6. Users can manage `.env` variables via a slide-out drawer. The container writes them to `/.env` and restarts the dev server.

**Cost at 0 users: $0.00.** All compute runs in the visitor's browser tab. The container lifecycle is tied entirely to the browser tab — no cloud processes, no billing.

**v1 scope:** Node.js / Next.js / Vite / Express apps only. Python (FastAPI, Django) and Docker-based projects are explicitly out of scope — see v2 note.

---

### 2.2 Architecture & Cross-Origin Isolation (COOP/COEP)

```
SessionWorkspaceClient (existing)
    │
    │  SSE file_diff event (existing wire)
    ▼
useWebContainer (new hook)
    │
    ├─► WebContainer.boot()           (singleton per tab, boots once)
    │       │
    │       ├─► webcontainer.mount(fileTree)     (project files → virtual fs)
    │       ├─► webcontainer.spawn("npm", ["install"])  → pipe to TerminalDrawer
    │       ├─► webcontainer.spawn("npm", ["run", "dev"]) → pipe to TerminalDrawer
    │       └─► webcontainer.on("server-ready", (port, url) → setPreviewUrl(url))
    │
    ├─► webcontainer.fs.writeFile(path, content)   (on every file_diff SSE)
    │       └─► Dev server HMR fires → iframe reloads in < 200ms
    │
    └─► webcontainer.fs.writeFile("/.env", envContent)  (on user .env save)
            └─► Process restart → iframe reloads
```

#### Browser Prerequisites (Cross-Origin Isolation)

WebContainers require `SharedArrayBuffer`, which browsers gate behind two HTTP response headers:
- `Cross-Origin-Embedder-Policy: require-corp`
- `Cross-Origin-Opener-Policy: same-origin`

These must be set in:
1. `next.config.ts` → `headers()` for local `npm run dev`.
2. `frontend/public/_headers` → for Cloudflare Pages production static hosting.

> [!IMPORTANT]
> COOP/COEP break embedded third-party iframes that are not CORP-compliant (e.g. Google Maps, Stripe.js, YouTube embeds). Haunter Studio has no such embeds — this is safe. Verify before adding any third-party embed in future.

---

### 2.3 Phase 1 — COOP/COEP Headers & `useWebContainer` Hook

**Goal:** Wire the security headers required for `SharedArrayBuffer`, install `@webcontainer/api`, and build the core `useWebContainer` hook that manages the full WebContainer lifecycle (boot → mount → install → dev server → server-ready URL). No UI changes yet — the hook is inert until Phase 2 plugs it in.

**Files to create/modify:**
- `frontend/next.config.ts` ← add `headers()` config
- `frontend/public/_headers` ← **new file** (Cloudflare Pages static header rules)
- `frontend/src/hooks/useWebContainer.ts` ← **new file**
- `frontend/package.json` ← add `@webcontainer/api`

#### 2.3.1 `next.config.ts` — COOP/COEP Headers

Remove the `output: "export"` mode **only for local dev** — static export does not support `headers()`. The static export config stays as-is for production Cloudflare Pages builds; headers for production are handled by `public/_headers`.

Add to `next.config.ts`:

```ts
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "export",
  images: { unoptimized: true },
  devIndicators: false,
  env: {
    NEXT_PUBLIC_API_URL:
      process.env.NEXT_PUBLIC_API_URL ||
      "https://gjdbtzw5h36jhniqgdcxvhmjxu0tcjqr.lambda-url.us-east-1.on.aws",
  },
  // COOP/COEP required by @webcontainer/api (SharedArrayBuffer).
  // Only applied in `next dev` — static export ignores headers().
  // Production headers are set via public/_headers (Cloudflare Pages).
  async headers() {
    return [
      {
        source: "/(.*)",
        headers: [
          { key: "Cross-Origin-Embedder-Policy", value: "require-corp" },
          { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
        ],
      },
    ];
  },
};

export default nextConfig;
```

#### 2.3.2 `frontend/public/_headers` — Cloudflare Pages Production Headers

```
/*
  Cross-Origin-Embedder-Policy: require-corp
  Cross-Origin-Opener-Policy: same-origin
```

This is the standard Cloudflare Pages `_headers` format. Cloudflare applies these on every response from the Pages CDN edge.

#### 2.3.3 Install `@webcontainer/api`

```bash
npm install @webcontainer/api --save
```

Add to `package.json` `dependencies` (not devDependencies — required at runtime):
```json
"@webcontainer/api": "^1.5.0"
```

Pin to minor — the API surface changes frequently between majors.

#### 2.3.4 `useWebContainer.ts` — Hook Interface & Contract

```typescript
// frontend/src/hooks/useWebContainer.ts
"use client";

export type WebContainerStatus =
  | "idle"           // not started
  | "booting"        // WebContainer.boot() in progress
  | "mounting"       // writing files into virtual fs
  | "installing"     // npm install running
  | "starting"       // npm run dev in progress
  | "ready"          // dev server live, previewUrl populated
  | "error";         // unrecoverable failure

export interface UseWebContainerReturn {
  status: WebContainerStatus;
  previewUrl: string | null;
  terminalOutput: string[];           // streamed to existing TerminalDrawer
  boot: (fileTree: Record<string, FileSystemNode>) => Promise<void>;
  writeFile: (path: string, content: string) => Promise<void>;
  writeEnvFile: (vars: Record<string, string>) => Promise<void>;
  restartDevServer: () => Promise<void>;
  teardown: () => void;
  error: string | null;
}
```

##### Internal Implementation Rules
- `WebContainer.boot()` is called **once per hook instance**. Guard with a ref to prevent double-boot on React StrictMode double-invoke.
- `npm install` is run once at mount time. `npm run dev` is run after install resolves.
- `stdout` and `stderr` from both spawned processes are piped via `ReadableStream` → `setTerminalOutput((prev) => [...prev, chunk])`. These chunks flow directly into the existing `TerminalDrawer` in `SessionWorkspaceClient`.
- `writeFile(path, content)` calls `webcontainerInstance.fs.writeFile(path, content)`. This is the hot path called on every `file_diff` SSE event — it must be synchronous-feeling (no dev server restart, HMR handles it).
- `writeEnvFile(vars)` serializes `vars` into dotenv format, writes to `/.env`, then kills and re-spawns the dev server process.
- `teardown()` kills all spawned processes and nulls the WebContainer ref. Called on component unmount.
- The hook is `"use client"` — never imported from Server Components.
- `WebContainer` import is wrapped in a dynamic `import()` inside `boot()` to prevent SSR import errors (WebContainer uses browser-only APIs).

**`FileSystemNode` type** (re-exported from `@webcontainer/api`):
```typescript
import type { FileSystemTree } from "@webcontainer/api";
export type { FileSystemTree };
```

#### Exit Criteria Phase 1
- `npm run dev` in `frontend/` serves the app with both COOP and COEP headers (verify in browser DevTools → Network → response headers on any page).
- `@webcontainer/api` is in `package.json` dependencies, installs cleanly.
- `useWebContainer.ts` compiles with `tsc --noEmit` — zero TypeScript errors.
- `public/_headers` present with correct syntax — verify by deploying to a Cloudflare Pages preview branch and checking response headers.
- `WebContainer.boot()` does **not** throw a `SharedArrayBuffer is not defined` error in the browser console with both headers present.

---

### 2.4 Phase 2 — Preview Panel UI & `.env` Manager

**Goal:** Build the `WebPreviewPanel` component and integrate it into `SessionWorkspaceClient` as a new **"Preview"** view mode alongside the existing Chat / Diffs / Split tabs. Add the `.env` slide-out drawer for environment variable management.

**Files to create/modify:**
- `frontend/src/components/workspace/WebPreviewPanel.tsx` ← **new file**
- `frontend/src/app/sessions/workspace/SessionWorkspaceClient.tsx` ← extend view mode switcher + wire hook

#### 2.4.1 `WebPreviewPanel` Component

Prop surface:
```typescript
interface WebPreviewPanelProps {
  status: WebContainerStatus;
  previewUrl: string | null;
  onBoot: () => void;          // called when user clicks "Launch Preview"
  onRefresh: () => void;       // reloads the iframe src
  onRestartServer: () => void; // kills + restarts npm run dev
  className?: string;
}
```

**Anatomy (top to bottom):**
```
┌──────────────────────────────────────────────────────────┐
│  PREVIEW BROWSER BAR                                     │
│  [⟳ Refresh] [url: localhost:3000]  [●Live] [⊞ Viewport]│
├──────────────────────────────────────────────────────────┤
│                                                          │
│                   <iframe src={previewUrl}>              │
│              (full remaining height, no border)          │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

**States the panel must handle:**
| Status | What renders |
|---|---|
| `idle` | Dark placeholder card with "Launch Preview" CTA button (3D-ish primary, per frontend skill §4) |
| `booting` / `mounting` | Spinner + status label (e.g. "Booting WebContainer…", "Mounting files…") |
| `installing` | Progress label "Installing dependencies…" + `TerminalDrawer` output visible below |
| `starting` | "Starting dev server…" label |
| `ready` | Browser bar + live `<iframe>`. The iframe `src` is set to `previewUrl`. |
| `error` | Error card with message + "Retry" button that calls `onBoot()` |

- **Viewport switcher:** Three icon buttons — Desktop (full width), Tablet (768px max-width centered), Mobile (390px max-width centered). Clicking constrains the `<iframe>` container width and adds a device-frame-like visual border. No new deps — pure CSS.
- **`.env` drawer trigger:** A `⚙ Env` button in the browser bar opens `EnvDrawer` (see §2.4.2).
- **Design language:** Match `SessionWorkspaceClient.tsx` exactly — `bg-[#09090b]`, `zinc-*` token palette, same border/shadow style as existing topbar segments. No new Tailwind plugins, no new npm deps, no glassmorphism for its own sake.

#### 2.4.2 `EnvDrawer` Component (collocated in `WebPreviewPanel.tsx`)

A right-side slide-out panel (fixed, `z-50`, `w-80`) that lists the current `.env` key-value pairs the user has set for this session.

```typescript
interface EnvDrawerProps {
  vars: Record<string, string>;
  onSave: (vars: Record<string, string>) => void;  // calls writeEnvFile + restarts server
  onClose: () => void;
}
```

**Behavior:**
- Opens pre-populated with any vars already stored in component state.
- Rows: key input + value input (type `password` for values matching `*_KEY`, `*_SECRET`, `*_TOKEN` patterns — toggleable to plaintext via eye icon).
- "+ Add Variable" button appends a new empty row.
- "Save & Restart" button serializes vars, calls `onSave`, closes drawer, shows a brief "Restarting…" badge in the browser bar.
- Values are **never persisted to the backend or localStorage** — they live only in the WebContainer virtual filesystem for the duration of the browser tab session. This is stated explicitly in a one-line footnote in the UI.

#### 2.4.3 `SessionWorkspaceClient.tsx` — View Mode Extension

**Add "Preview" to the view mode union:**
```typescript
const [viewMode, setViewMode] = useState<"chat" | "diffs" | "split" | "preview">("chat");
```

**Add "Preview" tab button** in the existing view switcher segment (after the "Split" button), with a `Globe` icon from lucide-react (already imported at line 59).

**Wire `useWebContainer` hook:**
```typescript
const {
  status: wcStatus,
  previewUrl,
  terminalOutput: wcTerminalOutput,
  boot: bootWebContainer,
  writeFile: wcWriteFile,
  writeEnvFile,
  restartDevServer,
  teardown: wcTeardown,
  error: wcError,
} = useWebContainer();
```

**Merge WebContainer terminal output into existing `terminalLogs`:**
```typescript
// wcTerminalOutput chunks are appended to the existing terminalLogs state
// so they flow into the existing <TerminalDrawer logs={terminalLogs} />.
useEffect(() => {
  if (wcTerminalOutput.length > 0) {
    setTerminalLogs((prev) => [...prev, ...wcTerminalOutput.slice(prev.length - terminalLogs.length)]);
  }
}, [wcTerminalOutput]);
```

> [!NOTE]
> Do not duplicate `TerminalDrawer` — merge WebContainer output into the existing `terminalLogs` state that `TerminalDrawer` already reads. One terminal, all output sources.

**Teardown on unmount:**
```typescript
useEffect(() => () => wcTeardown(), []);
```

**Render the Preview panel** inside the workspace body flex container:
```tsx
{viewMode === "preview" && (
  <WebPreviewPanel
    status={wcStatus}
    previewUrl={previewUrl}
    onBoot={handleBootPreview}
    onRefresh={() => { /* increment an iframeKey state to force remount */ }}
    onRestartServer={restartDevServer}
    className="flex-1 h-full"
  />
)}
```

**`handleBootPreview`**: Calls `wcBoot(fileTree)` where `fileTree` is built from `stagedPatches` merged into a base empty project scaffold. In v1 the file tree is synthetic — the WebContainer boots with only the files the agent has staged. Full repo clone (via GitHub API file tree fetch) is a v2 enhancement.

#### Exit Criteria Phase 2
- `tsc --noEmit` → zero new TypeScript errors.
- `viewMode` union updated to include `"preview"` without breaking existing Chat / Diffs / Split rendering.
- "Preview" tab renders in the view switcher; clicking it mounts `WebPreviewPanel`.
- `WebPreviewPanel` renders all six states (idle, booting, mounting, installing, starting, ready, error) — manually verify each by reading the component JSX. No automated UI test required per project rules.
- `EnvDrawer` opens/closes, adds rows, serializes vars correctly — manual verify.
- No regressions on existing tests: `npm test` in `frontend/` still passes.

---

### 2.5 Phase 3 — Agent File-Sync Loop (Live HMR via SSE `file_diff`)

**Goal:** Close the live-edit loop. Every `file_diff` SSE event the agent emits during an active streaming turn is immediately written to the WebContainer virtual filesystem, triggering HMR in the iframe in real time. The user watches their app update as the agent patches files — no manual refresh needed.

**Files to modify:**
- `frontend/src/app/sessions/workspace/SessionWorkspaceClient.tsx` ← add file-sync effect
- `frontend/src/hooks/useWebContainer.ts` ← verify `writeFile` path normalization

#### 2.5.1 File-Sync Effect in `SessionWorkspaceClient`

The existing SSE pipeline already populates `stagedPatches` (a `Record<string, string>` of `filePath → unified diff`) on every `file_diff` event. The file-sync effect watches `stagedPatches` and writes updated content to the WebContainer:

```typescript
// Sync agent-staged patches into the WebContainer filesystem on every change.
// Only fires when the preview panel is active AND the container is ready.
useEffect(() => {
  if (wcStatus !== "ready") return;
  if (viewMode !== "preview" && viewMode !== "split") return;

  const entries = Object.entries(stagedPatches);
  if (entries.length === 0) return;

  // Write the latest version of each staged file to the container.
  // parseDiffForMonaco is already available — use its `modified` output as
  // the complete post-patch file content to write.
  entries.forEach(([filePath, diff]) => {
    const { modified } = parseDiffForMonaco(diff);
    if (modified) {
      wcWriteFile(filePath, modified).catch((err) => {
        console.error("[WebContainer] writeFile failed:", filePath, err);
      });
    }
  });
}, [stagedPatches, wcStatus, viewMode]);
```

**Key Invariants:**
- `wcWriteFile` is called only when `wcStatus === "ready"` — the container must be fully booted before any file writes.
- If `viewMode` is not `"preview"` or `"split"`, the sync effect is a no-op (don't boot the container in the background if the user hasn't opened Preview).
- `parseDiffForMonaco` is already defined at the module level in `SessionWorkspaceClient.tsx` (line ~1271) — no new import needed.
- Each `wcWriteFile` call is independent. A failure on one file logs to console and does not block others.

#### 2.5.2 `useWebContainer.ts` — Path Normalization in `writeFile`

WebContainer virtual filesystem paths must be absolute from the project root (e.g. `/src/app/page.tsx`). Agent `file_diff` paths may be relative (e.g. `src/app/page.tsx`). Normalize on write:

```typescript
async writeFile(path: string, content: string): Promise<void> {
  if (!containerRef.current) return;
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  await containerRef.current.fs.writeFile(normalizedPath, content);
}
```

#### 2.5.3 Split View — Chat + Preview Side-by-Side

In the existing "Split" view mode, the right panel currently shows the Monaco Diff Editor. Add the ability for the right panel to show either the Diff editor or the Preview iframe, toggled by a small segmented control inside the right panel header.

Mechanism: Add `splitRightPanel: "diffs" | "preview"` state, defaulting to `"diffs"`. The segmented control switches between them. This is a pure UI state change — no new data flows needed.

#### Exit Criteria Phase 3
- With the Preview tab open and a WebContainer in `ready` state, calling `setStagedPatches` with a new file entry causes `wcWriteFile` to be called with the correct normalized path and the full post-patch file content (verify via `console.log` before removing, or by watching the TerminalDrawer for HMR output from the dev server).
- HMR fires in the iframe within ~200ms of the file write — observable as a visual reload in the preview without a full page refresh.
- Relative paths (e.g. `src/app/page.tsx`) are correctly normalized to `/src/app/page.tsx` before the `fs.writeFile` call.
- No new TypeScript errors (`tsc --noEmit`).
- No regressions on existing tests.

---

### 2.6 Phase Sequencing

```
Phase 1 (Headers + Hook)
    └──► Phase 2 (Preview Panel UI + .env Drawer)
              └──► Phase 3 (Agent File-Sync → Live HMR)
```

All phases are strictly sequential. Phase 2 requires Phase 1's hook. Phase 3 requires Phase 2's sync point (`wcStatus === "ready"` and `viewMode` awareness).

---

### 2.7 Non-Goals (WebContainer v1)

- **Python / FastAPI / Django / Go / Rust runtimes** — WebContainers is Node.js/WASM only. These require E2B Sandboxes (v2 consideration).
- **Full repo clone into WebContainer** — v1 boots with only agent-staged files + a minimal scaffold. Full `git clone` via GitHub API file tree fetch is v2.
- **Persistent WebContainer state across page refreshes** — container lifecycle is bound to the browser tab. No OPFS persistence in v1.
- **WebContainer-to-Lambda backend proxy** — the preview app runs fully in the browser. No backend port-forwarding tunnel.
- **Multi-user shared preview sessions** — one WebContainer per browser tab, fully isolated.
- **Any new FastAPI endpoints** — zero backend changes in all three phases.
- **New npm dependencies** beyond `@webcontainer/api` — all UI components use existing Tailwind, lucide-react, and React.

---

## 3. Model Configuration Architecture Overhaul & Fixes

| Attribute | Details |
|---|---|
| **Feature** | Unified, reliable Model & Provider Hot-Swapping across Backend, Subagents, and Frontend |
| **Scope** | Backend (`app/routers/model_config.py`, `app/llm/config.py`, `app/llm/client.py`, `app/config.py`, `app/models.py`) + Frontend (`config/page.tsx`, `lib/api.ts`) |
| **Status** | `Planned` |

### 3.1 Summary & Metadata

This initiative streamlines multi-provider configuration (OpenCode Zen, OpenAI, Anthropic, Groq) and ensures repo-level and global-level LLM overrides coexist without race conditions or unintentional mass-deactivations.

---

### 3.2 Identified Issues & Planned Fixes

#### 1. Per-Repo vs Global Model Config Isolation (Fix Mass Deactivation Bug)
- **Problem:** Updating the global model via `PUT /config/model` executes `UPDATE model_configs SET is_active = false WHERE is_active = true`, which wipes out all repo-specific active configurations. Additionally, the `model_configs` table lacks a discriminator (`scope` / `repo_id`), causing global queries to potentially pick up repo overrides.
- **Planned Fix:**
  - Add `scope` (`global` | `repo`) and optional `repo_id` / `user_id` to `model_configs` table.
  - Scope global deactivation queries strictly to `WHERE is_active = true AND scope = 'global'`, preserving all repo custom configurations.
  - In `_resolve_from_db`, cleanly differentiate global platform configs from repo-scoped overrides.

#### 2. Remove Admin Functionality & Restrictions from Model Switcher
- **Problem:** `PUT /config/model` and the frontend UI gate global model switches behind `is_admin` / `ADMIN_USER_ID`, locking out normal users and causing 403 Forbidden errors and locked UI banners.
- **Planned Fix:**
  - Strip the `is_admin` requirement completely from `PUT /config/model` — allow any authenticated user to switch the active model freely.
  - Remove admin locks, warning banners, and disabled states from `frontend/src/app/config/page.tsx`.

#### 3. OpenAI & Anthropic Provider Realism & Proper Adapters
- **Problem:** OpenAI and Anthropic are offered in the UI and router base URL map, but `Settings` lacks API keys (`openai_api_key`, `anthropic_api_key`), and `LLMClient` routes them into `OpenCodeZenProvider` with invalid authentication headers and payload schemas.
- **Planned Fix:**
  - Introduce `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` in `Settings` (`app/config.py`).
  - Implement dedicated adapters for OpenAI (`/v1/chat/completions`) and Anthropic (`/v1/messages` with `x-api-key` and Anthropic-native payload format).
  - Ensure `LLMClient.complete()` routes each provider to its respective adapter with correct credentials.

#### 4. Wire Groq into Frontend Model Switcher
- **Problem:** Groq is supported on the backend (`GroqProvider`, router available endpoints), but missing from `PROVIDER_OPTIONS`, `PROVIDER_METADATA`, and default options in `frontend/src/app/config/page.tsx`.
- **Planned Fix:**
  - Add `groq` to `PROVIDER_OPTIONS` and `PROVIDER_METADATA` in `frontend/src/app/config/page.tsx` with models (`openai/gpt-oss-120b`, `llama-3.3-70b-versatile`, `llama-3.1-8b-instant`).
  - Ensure the UI allows seamless 1-click hot-swapping to Groq engines.

#### 5. Dynamic Fallback Base URL Resolution
- **Problem:** `backend/app/llm/config.py` and `backend/app/routers/model_config.py` hardcode `settings.opencode_zen_base_url` as the fallback `base_url` regardless of `settings.default_provider`.
- **Planned Fix:**
  - Dynamically derive fallback `base_url` using a provider-to-base-URL mapping based on `settings.default_provider` (e.g., resolving `groq_base_url` when `default_provider="groq"`).

#### 6. Clean Model Config Page UX & Scope Defaulting
- **Problem:** `selectedScope` default initialized to `"global"` with admin lock friction, confusing users with disabled controls.
- **Planned Fix:**
  - With admin restrictions removed, both Global and Repo scopes are fully interactive for all users without lock overlays.
  - Show explicit badges indicating whether a repo is currently using its own custom override or inheriting the platform default, with a 1-click "Reset to Global Default" action.

---

## 4. Live Session CI Sandbox Verification Engine

| Attribute | Details |
|---|---|
| **Feature** | Autonomous GitHub Actions Mirror CI Verification for Live Cloud Sessions (`/sessions`) |
| **Scope** | Backend (`session_orchestrator.py`, `session_tools/sandbox.py`, `subagents/sandbox_verifier.py`, `session_streamer.py`) + Frontend (`useSessionStream.ts`, `SessionWorkspaceClient.tsx`, `TerminalDrawer`) |
| **Author** | MD Sufiyan Bari |
| **Status** | `Planned` |

### 4.1 Overview & Motivation

Currently in `/sessions`, testing is divided across two isolated silos:
1. **Local Terminal Subprocesses (`session_tools/sandbox.py`)**: The AI agent loop calls `run_targeted_tests` or `run_terminal_command`, which executes `subprocess.Popen` on the local backend host.
   - *Failure Mode in Production*: When deployed to AWS Lambda or containerized serverless runtimes, local subprocesses fail because Lambda has a read-only filesystem (`/tmp` only), no Docker daemon, and cannot provision custom OS dependencies, multi-language runtimes, or database containers required by arbitrary user repositories.
2. **Top-Bar Manual UI Button (`api.verifySession`)**: Triggers `verify_session_patches` in `app/subagents/sandbox_verifier.py`, creating an isolated GitHub Actions mirror repo (`haunter-sandbox-mirror-{repo}`), pushing staged diffs, running real CI workflows in GitHub Actions, and returning status + logs.
   - *Failure Mode*: The AI agent **cannot invoke this runner itself** during its reasoning loop. The agent remains blind to the real CI environment and cannot autonomously iterate until tests pass in CI.

This feature bridges both worlds: it equips the AI agent with a first-class `verify_in_ci_sandbox` tool, enabling autonomous, cloud-native verification loops backed by real GitHub Actions runners.

---

### 4.2 Architecture & Autonomous CI Self-Healing Loop

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                       AUTONOMOUS CI SELF-HEALING LOOP                       │
└─────────────────────────────────────────────────────────────────────────────┘

User Prompt: "Fix the authentication token expiry bug"
       │
       ▼
SessionOrchestrator LLM
       │  (1) Proposes code changes via `str_replace` or `create_file`
       ▼
Staged Patches updated in AgentSession
       │
       │  (2) Agent calls `verify_in_ci_sandbox(test_suite_filter="auth")`
       ▼
Subagent / Sandbox Bridge (`app/subagents/sandbox_verifier.py`)
       │
       ├──► Concatenates staged patches into unified diff
       ├──► Pushes ephemeral verification branch to isolated mirror repo
       ├──► Dispatches GitHub Actions workflow via Octokit / REST API
       │
       ▼
GitHub Actions Cloud Runner
       │  (Runs actual repository CI workflow, linters, matrix tests)
       │  (Streams step logs & progress via GitHub API)
       │
       ▼
SseQueue Event Emission (`session_streamer.py`):
       ├── `sandbox_start` → { run_url, branch, status: "queued" | "in_progress" }
       ├── `terminal_output` → Real-time GitHub Actions step log lines
       └── `sandbox_result` → { passed: bool, exit_code: int, summary: str }
       │
       ▼
Returned to Agent LLM Context:
       ┌───────────────────────────────┴───────────────────────────────┐
       ▼                                                               ▼
   [ ❌ Tests Failed ]                                             [ ✅ Tests Passed ]
   Agent inspects raw CI traceback & step errors                   Agent concludes turn,
   Agent applies targeted fix via `str_replace`                    marks task complete,
   Agent re-runs `verify_in_ci_sandbox`                            presents 1-click "Commit PR"
```

---

### 4.3 Detailed Component Changes

#### 4.3.1 Backend: New Agent Tool `verify_in_ci_sandbox`

**Files:** `backend/app/services/session_tools/sandbox.py` & `session_orchestrator.py`

1. **Tool Definition:**
   ```python
   {
       "name": "verify_in_ci_sandbox",
       "description": (
           "Dispatch all currently staged patches to the isolated GitHub Actions CI sandbox mirror repo. "
           "Runs the real repository test suite in GitHub Actions, streams CI logs live, and returns "
           "pass/fail status with full compiler/test error tracebacks for self-healing."
       ),
       "parameters": {
           "type": "object",
           "properties": {
               "workflow_file": {
                   "type": "string",
                   "description": "Optional specific workflow file to trigger (e.g. 'ci.yml' or 'test.yml'). Default auto-detects.",
               },
               "timeout_sec": {
                   "type": "integer",
                   "description": "Maximum seconds to wait for GitHub Actions CI run completion (default 180, max 600).",
               },
           },
       },
   }
   ```

2. **Dispatcher Handler in Orchestrator (`session_orchestrator.py`):**
   - Reads `session.staged_patches` and `session.repo`.
   - Calls `verify_session_patches(session=session, repo=repo, staged_patches=session.staged_patches, gh_token=gh_token)`.
   - Streams `sandbox_start` and `terminal_output` log events to the frontend via `SseQueue`.
   - Formats the execution result (passed/failed, exit code, duration, raw failing logs) as the tool response for the LLM.

3. **Hybrid Engine (`SANDBOX_PROVIDER` aware):**
   - When `settings.sandbox_provider == "local"` (local developer workstation): delegates to fast local `tool_run_targeted_tests`.
   - When `settings.sandbox_provider == "github_actions"` (production AWS Lambda / cloud): delegates to `verify_session_patches`.

#### 4.3.2 SSE Streaming Events (`session_streamer.py`)

Add to `_ALLOWED_EVENTS`:
- `sandbox_queued`: `{ run_url: str, workflow_name: str }`
- `sandbox_progress`: `{ step_name: str, status: "in_progress" | "completed" }`

#### 4.3.3 Frontend Enhancements (`SessionWorkspaceClient.tsx` & Terminal Drawer)

1. **Live CI Status Chip:**
   - When `verify_in_ci_sandbox` or the top-bar "Run Tests" button is active, render an interactive chip showing GitHub Actions run status (`queued` → `running` → `passed`/`failed`) with an external link icon directly to GitHub Actions.
2. **Terminal Drawer Log Streaming:**
   - Direct the raw streamed lines from the GitHub Actions runner into the existing terminal drawer with colorized ANSI codes.
3. **Agent Action Badge:**
   - Display a distinct `<ShieldCheck />` or `<TestTube2 />` badge in the chat message stream indicating: *"Agent verified changes in GitHub Actions CI Sandbox (Run #1042 — Passed in 42s)"*.

---

### 4.4 Phased Implementation Plan

- **Phase 1: Backend Tool & Bridge Wiring**
  - Export `tool_verify_ci_sandbox` from `session_tools/sandbox.py`.
  - Register tool in `session_orchestrator.py` system prompts and tool dispatch dictionary.
  - Wire `verify_session_patches` to stream intermediate polling logs to `queue.put_terminal_output()`.
  - Write unit tests in `backend/tests/test_session_ci_sandbox.py`.

- **Phase 2: Frontend Streaming & Visual Polish**
  - Add `sandbox_queued` and `sandbox_progress` handlers in `useSessionStream.ts`.
  - Render GitHub Actions runner status & external workflow link in `SessionWorkspaceClient.tsx`.
  - Ensure dark-mode theme fidelity and seamless split-view transitions during CI runs.

- **Phase 3: Autonomous Self-Correction Eval Harness**
  - Test with golden broken repo cases where the agent must iteratively fix syntax and test failures across 2–3 CI runs autonomously before opening a PR.

---

### 4.5 Non-Goals

- Running arbitrary untrusted user code directly inside the FastAPI Lambda execution environment.
- Modifying the user's primary default branch directly during verification (all testing remains strictly isolated to the sandbox mirror repo).
