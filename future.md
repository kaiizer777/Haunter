# HAUNTER — Future Engineering Roadmap: Level 9 Agent Evolution

Clean, phase-by-phase execution roadmap to evolve the Haunter Live Pairing Agent from basic file reading into a production-grade, Level 9 Autonomous Pairing & CI Diagnostic Engineer.

---

## Phase 1: High-ROI Repo Recon & Navigation
- **Complexity**: `Low`
- **Goal**: Eliminate context-window bloat by allowing the agent to discover files and search strings without loading entire files into memory.

### Deliverables
- **`backend/app/services/session_tools/recon.py`**:
  - `grep_search(query: str, path_prefix?: str, case_sensitive?: bool = False, max_results?: int = 25)`: Fast regex/substring search across repository files.
  - `glob_files(pattern: str, exclude_hidden?: bool = True)`: Pattern-based file path discovery.
  - `read_file_slice(path: str, start_line: int, end_line: int)`: Surgical line-range reader with 1-based indexing.
  - `list_directory(path: str = ".", depth: int = 2)`: Hierarchical folder tree explorer.
- **`backend/app/services/session_orchestrator.py`**:
  - Register Phase 1 tools in `_TOOLS` and dispatch loop.
- **`frontend/src/app/sessions/workspace/SessionWorkspaceClient.tsx`**:
  - Add chip icons and summary counters in `ToolExecutionAccordion` for `grep_search`, `glob_files`, and `read_file_slice`.
- **Tests**:
  - `backend/tests/test_session_recon_tools.py`: Unit tests for regex matching, path validation, slice limits, and depth capping.

### Exit Criteria
- `pytest backend/tests/test_session_recon_tools.py` passes cleanly.
- Agent successfully locates target code using `grep_search` and reads only the relevant 30-line slice.

---

## Phase 2: Surgical Code Editing Engine
- **Complexity**: `Medium`
- **Goal**: Replace brittle unified diff line offsets with exact string replacement, eliminating syntax and patch-rejection errors.

### Deliverables
- **`backend/app/services/session_tools/editor.py`**:
  - `str_replace(path: str, old_str: str, new_str: str)`: Exact search-and-replace tool. Fails closed with descriptive error if `old_str` is not uniquely found.
  - `create_file(path: str, content: str)`: Clean file creation.
  - `delete_file(path: str)`: Staged file deletion.
  - `apply_multi_patch(patches: list[dict])`: Atomic multi-file editing in a single turn.
  - Automatically converts string replacements into unified diffs to update `staged_patches` and Monaco models.
- **`backend/app/services/session_orchestrator.py`**:
  - Integrate editor tools and emit `file_diff` SSE events.
- **`frontend/src/app/sessions/workspace/SessionWorkspaceClient.tsx`**:
  - Support multi-tab file switching when `apply_multi_patch` touches multiple files simultaneously.
- **Tests**:
  - `backend/tests/test_session_editor_tools.py`: Tests for unique matches, non-unique match rejection, empty strings, and multi-file rollback on error.

### Exit Criteria
- `pytest backend/tests/test_session_editor_tools.py` passes.
- Code modifications succeed without line-number calculation errors.

---

## Phase 3: TinyFish Live Web & Docs Intelligence
- **Complexity**: `Low`
- **Goal**: Connect the agent to the live 2026 web ecosystem to verify modern library APIs, breaking changes, and external docs.

### Deliverables
- **`backend/app/services/session_tools/web.py`**:
  - Client integration with **TinyFish Search API** (`api.tinyfish.io/v1/search`) and **Fetch API** (`api.tinyfish.io/v1/fetch`) using `settings.tinyfish_api_key`.
  - `search_web_docs(query: str, domain?: str, max_results?: int = 5)`: Returns token-dense, rank-stable JSON search results.
  - `fetch_web_content(url: str, format?: str = "markdown")`: Scrapes live documentation via stealth Chromium, stripping ads and noise.
  - `fetch_package_metadata(ecosystem: str, package_name: str)`: Fast PyPI/npm version and advisory checker.
- **`backend/app/services/session_orchestrator.py`**:
  - Expose web tools to the agent loop with domain safety allowlists.
- **`frontend/src/app/sessions/workspace/SessionWorkspaceClient.tsx`**:
  - Render web search chips with clickable source URLs in the chat timeline.
- **Tests**:
  - `backend/tests/test_session_web_tools.py`: Tests with mocked TinyFish HTTP responses covering success, rate limits, and fallback paths.

### Exit Criteria
- `pytest backend/tests/test_session_web_tools.py` passes.
- Agent successfully queries TinyFish API for library documentation and cites sources.

---

## Phase 4: AST & Code Intelligence (LSP-Grade)
- **Complexity**: `High`
- **Goal**: Provide the agent with structural code understanding across files without invoking full language servers.

### Deliverables
- **`backend/app/services/session_tools/symbols.py`**:
  - Lightweight Tree-sitter / AST parser for Python and TypeScript/JavaScript.
  - `find_symbol(name: str, kind?: str)`: Locates functions, classes, interfaces, or types across the codebase.
  - `find_references(symbol: str, path: str)`: Finds all call sites and usages of a symbol across the project.
  - `get_file_outline(path: str)`: Returns signatures and docstrings of a file without the function bodies.
- **`backend/app/services/session_orchestrator.py`**:
  - Wire symbol tools into agent loop.
- **`frontend/src/app/sessions/workspace/SessionWorkspaceClient.tsx`**:
  - Quick outline inspection drawer and symbol reference chips.
- **Tests**:
  - `backend/tests/test_session_symbols.py`: Verification on nested classes, async functions, TypeScript interfaces, and imported usages.

### Exit Criteria
- `pytest backend/tests/test_session_symbols.py` passes.
- Agent correctly identifies callers of a refactored method across multiple files.

---

## Phase 5: Autonomous Sandbox Execution & Test Automation
- **Complexity**: `High`
- **Goal**: Enable the agent to execute shell commands, linters, and tests inside the isolated sandbox runner and self-correct on failure.

### Deliverables
- **`backend/app/services/session_tools/sandbox.py`**:
  - `run_terminal_command(command: str, timeout_sec?: int = 60)`: Dispatches command to isolated GitHub Actions mirror runner / container.
  - `run_linter(paths: list[str])`: Fast static analysis (`ruff`, `eslint`, `tsc --noEmit`).
  - `run_targeted_tests(test_targets: list[str])`: Executes targeted test files (`pytest`, `vitest`).
  - Real-time SSE streaming for terminal stdout/stderr (`event: terminal_output`).
- **`backend/app/services/session_streamer.py`**:
  - Add `put_terminal_output(chunk: str)` to `SseQueue`.
- **`frontend/src/app/sessions/workspace/SessionWorkspaceClient.tsx`**:
  - Integrated Terminal drawer below Monaco editor rendering streaming ANSI logs.
- **Tests**:
  - `backend/tests/test_session_sandbox_tools.py`: Tests for timeout handling, exit code reporting, and command injection sanitization.

### Exit Criteria
- `pytest backend/tests/test_session_sandbox_tools.py` passes.
- Agent runs `pytest` in sandbox, reads failing trace, fixes code via `str_replace`, and re-runs until tests pass.

---

## Phase 6: Interactive Planning & Clarification UI
- **Complexity**: `Medium`
- **Goal**: Make the agent's multi-step execution transparent and give the user one-click interactive controls for decisions.

### Deliverables
- **Database Migration**:
  - Add `plan: JSONB` and `waiting_input: JSONB` columns to `agent_sessions` table.
- **`backend/app/services/session_tools/planning.py`**:
  - `update_plan(tasks: list[dict])`: Updates live task graph (`pending`, `in_progress`, `completed`).
  - `ask_user_clarification(question: str, options: list[str])`: Pauses agent execution and waits for user input.
- **API Endpoints (`backend/app/routers/sessions.py`)**:
  - `POST /sessions/{id}/clarify`: Resumes the agent loop with the user's selected choice.
- **`frontend/src/app/sessions/workspace/SessionWorkspaceClient.tsx`**:
  - Real-time Plan Checklist widget in the sidebar.
  - Interactive chip selector modal in the chat dock when clarification is requested.
- **Tests**:
  - `backend/tests/test_session_planning.py`: Tests for plan state persistence, loop pausing, and resume on input.

### Exit Criteria
- `pytest backend/tests/test_session_planning.py` passes.
- Interactive clarification modal renders in frontend; selecting an option immediately resumes agent reasoning.

---

## Phase 7: Session Time Machine & Pre-Commit Security
- **Complexity**: `High`
- **Goal**: Safety net for instant turn-by-turn rollbacks and automated security scanning prior to opening a PR.

### Deliverables
- **`backend/app/services/session_tools/checkpoints.py`**:
  - Snapshot manager saving `(checkpoint_id, timestamp, staged_patches_snapshot, description)` after each turn.
  - `checkpoint_restore(checkpoint_id: str)`: Rewinds staged files and conversation state.
  - `scan_security_vulnerabilities(paths: list[str])`: Pre-commit scan using TruffleHog regexes (secrets/tokens) + AST SQL injection checks.
- **API Endpoint**:
  - `POST /sessions/{id}/checkpoints/{checkpoint_id}/restore`.
- **`frontend/src/app/sessions/workspace/SessionWorkspaceClient.tsx`**:
  - Visual Time Machine timeline slider with 1-click Undo/Redo in Monaco header.
  - Security Audit Status badge on the "Commit & Open PR" button.
- **Tests**:
  - `backend/tests/test_session_checkpoints.py`: Tests for exact rollback fidelity and secret detection.

### Exit Criteria
- `pytest backend/tests/test_session_checkpoints.py` passes.
- Rolling back restores prior diffs in Monaco instantly; hardcoded fake secret triggers security blocker.
