"""
Live Session Orchestrator — Cloud Agentic Live Session Phase 2.

Drives the interactive LLM agent loop for a pairing session:
  1. Loads conversation_history and staged_patches from AgentSession.
  2. Constructs LLM messages with system context (repo, branch, staged diffs).
  3. Calls LLMClient.complete() with tool definitions.
  4. Dispatches tool calls: read_file | stage_patch | discard_patch.
  5. Persists conversation_history and staged_patches atomically to Neon Postgres.
  6. Streams thought, tool_call, and file_diff events via SseQueue.

Concurrency guard:
  - Checks AgentSession.status == "active" inside an atomic SELECT FOR UPDATE-like
    read before starting the loop. Concurrent prompts on the same session are rejected
    with a 409 ConflictError (caller converts to HTTP 409).

Security:
  - Object-level authz (session.user_id == caller) is enforced at the endpoint layer.
  - File paths supplied via tool calls are validated to prevent path traversal
    before any GitHub API request is made.
  - No secrets or connection strings are emitted into the SSE stream.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.github_client import (
    GitHubClientError,
    fetch_file_content,
)
from app.llm.client import LLMClient
from app.llm.exceptions import LLMError
from app.models import AgentSession
from app.services.session_streamer import SseQueue
from app.services.session_tools.recon import (
    _validate_file_path,
    tool_glob_files,
    tool_grep_search,
    tool_list_directory,
    tool_read_file_slice,
)
from app.services.session_tools.editor import (
    tool_apply_multi_patch,
    tool_create_file,
    tool_delete_file,
    tool_str_replace,
)
from app.services.session_tools.symbols import (
    tool_find_references,
    tool_find_symbol,
    tool_get_file_outline,
)
from app.services.session_tools.sandbox import (
    tool_run_terminal_command,
    tool_run_linter,
    tool_run_targeted_tests,
)
from app.services.session_tools.web import (
    tool_search_web_docs,
    tool_fetch_web_content,
    tool_fetch_package_metadata,
)
from app.services.session_tools.planning import (
    tool_ask_user_clarification,
    tool_update_plan,
)
from app.services.session_tools.checkpoints import (
    create_checkpoint,
    tool_checkpoint_restore,
    tool_scan_security_vulnerabilities,
)

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# LLM tool schema definitions
# ------------------------------------------------------------------

_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read the content of a source file from the repository at the session branch. "
                "Use this to inspect code before proposing a patch."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative file path within the repository (e.g. 'src/main.py').",
                    }
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stage_patch",
            "description": (
                "Stage a unified diff patch for a file. The diff will be surfaced to the user "
                "in the Monaco editor and can later be committed via POST /sessions/{id}/commit."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative file path the diff targets.",
                    },
                    "diff": {
                        "type": "string",
                        "description": "Unified diff string (--- a/..., +++ b/..., @@ ... @@).",
                    },
                    "action": {
                        "type": "string",
                        "enum": ["modify", "create", "delete"],
                        "description": "Whether this diff modifies an existing file, creates a new one, or deletes it.",
                    },
                },
                "required": ["path", "diff", "action"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "discard_patch",
            "description": "Remove a previously staged patch for a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative file path whose staged patch should be discarded.",
                    }
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_search",
            "description": (
                "Search for a regex or substring query across repository source files at the session commit. "
                "Returns formatted matches: 'file_path:line_number: content' capped at max_results. "
                "Use this to locate function definitions, variable usages, or error strings."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Regex pattern or substring to search for.",
                    },
                    "path_prefix": {
                        "type": "string",
                        "description": "Optional directory or path prefix to narrow search (e.g. 'src/' or 'backend/app').",
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "description": "Whether the search is case-sensitive (defaults to false).",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of matching lines to return (default 25).",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob_files",
            "description": (
                "Find file paths in the repository matching a wildcard pattern (e.g. '**/*auth*.py', 'src/components/**/*.tsx'). "
                "Use this to discover file locations before reading or editing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern to match relative file paths.",
                    },
                    "exclude_hidden": {
                        "type": "boolean",
                        "description": "Whether to exclude hidden files and folders starting with '.' (default true).",
                    },
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file_slice",
            "description": (
                "Read a specific line range from a file (1-based, inclusive). "
                "Returns line-numbered lines (e.g. '42: def foo():'). "
                "Use this instead of read_file for large files to avoid blowing the context window."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative file path in the repository.",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "Starting line number (1-based, inclusive).",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Ending line number (1-based, inclusive).",
                    },
                },
                "required": ["path", "start_line", "end_line"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": (
                "List contents of a directory in the repository up to a given depth. "
                "Returns directories and files relative to the specified path."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative directory path to explore (default is '.' for repo root).",
                    },
                    "depth": {
                        "type": "integer",
                        "description": "Maximum directory traversal depth (default 2).",
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "str_replace",
            "description": (
                "Perform an exact, unique string replacement in a file. "
                "Fails with a descriptive error if old_str is not found or is ambiguous (found more than once). "
                "Always include enough surrounding context in old_str to make it unique. "
                "Prefer this over stage_patch for code modifications."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative file path to edit.",
                    },
                    "old_str": {
                        "type": "string",
                        "description": "The exact string to replace. Must match uniquely — include surrounding lines if needed.",
                    },
                    "new_str": {
                        "type": "string",
                        "description": "The replacement string. May be empty to delete the matched block.",
                    },
                },
                "required": ["path", "old_str", "new_str"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": (
                "Stage a new file with the given content. "
                "Generates a unified diff from /dev/null to the new content and updates staged_patches."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path for the new file.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full content of the new file.",
                    },
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": (
                "Stage deletion of an existing file. "
                "Generates a unified diff from the current content to /dev/null."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path of the file to delete.",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_multi_patch",
            "description": (
                "Atomically apply a batch of file edits in a single turn. "
                "All operations are validated first — if any fails, nothing is staged. "
                "Use this to make coordinated changes across multiple files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "patches": {
                        "type": "array",
                        "description": (
                            "List of edit operations. Each entry must have a 'type' field: "
                            "'str_replace' (requires path, old_str, new_str), "
                            "'create_file' (requires path, content), or "
                            "'delete_file' (requires path)."
                        ),
                        "items": {"type": "object"},
                    },
                },
                "required": ["patches"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_file_outline",
            "description": (
                "Return a compact structural outline of a source file — classes, methods, "
                "function signatures, and docstrings — without loading implementation bodies. "
                "Use this to understand a file's structure before reading or editing it. "
                "Supported extensions: .py, .ts, .tsx, .js, .jsx."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative file path within the repository.",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_symbol",
            "description": (
                "Locate definitions of functions, classes, interfaces, or types across "
                "repository source files. Returns path:line: signature for each match. "
                "Use this to find where a symbol is defined before reading or editing it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Symbol identifier to search for (e.g. 'UserService', 'get_user').",
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["function", "class", "interface", "type"],
                        "description": "Optional kind filter to narrow results.",
                    },
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_references",
            "description": (
                "Find all call sites and usages of a symbol across repository source files "
                "using word-boundary matching (so 'user' matches 'user.id' but NOT 'username'). "
                "Use this before renaming or refactoring a symbol to inspect all callers. "
                "Results capped at 50 to avoid context bloat."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "Symbol name to search for (e.g. 'get_user', 'UserService').",
                    },
                    "path": {
                        "type": "string",
                        "description": "Optional path prefix to restrict the search scope.",
                    },
                },
                "required": ["symbol"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_terminal_command",
            "description": (
                "Run a shell command in the sandbox environment. "
                "Returns stdout, stderr, and exit code. "
                "Use this to verify fixes, check build output, or inspect the runtime environment. "
                "Output streams live to the terminal drawer via SSE."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute (e.g. 'pytest tests/test_auth.py -v').",
                    },
                    "timeout_sec": {
                        "type": "integer",
                        "description": "Maximum execution time in seconds (default 60, max 300).",
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_linter",
            "description": (
                "Run static analysis on specified file paths. "
                "Auto-detects linter: .py files → ruff check; .ts/.tsx/.js → eslint. "
                "Returns structured diagnostics for the LLM to act on."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of relative file paths to lint.",
                    },
                    "linter": {
                        "type": "string",
                        "description": "Linter override ('auto' by default — auto-detected from extensions).",
                    },
                },
                "required": ["paths"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_targeted_tests",
            "description": (
                "Execute targeted test files via the appropriate test runner. "
                "Auto-detects: .py → pytest -v; .ts/.tsx → npx vitest run. "
                "After proposing changes with str_replace, run targeted tests to verify the fix "
                "before declaring completion. Captures failing tracebacks for self-correction."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "test_targets": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of test file paths or pytest node IDs (e.g. 'tests/test_auth.py::test_login').",
                    },
                    "timeout_sec": {
                        "type": "integer",
                        "description": "Maximum execution time in seconds (default 120, max 300).",
                    },
                },
                "required": ["test_targets"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_web_docs",
            "description": (
                "Search the live web and developer documentation via TinyFish Search API. "
                "Use this to look up current library APIs, breaking changes, migration guides, "
                "and external references that may not be in the repository. "
                "Returns Markdown-formatted results with titles, URLs, and snippets."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (e.g. 'FastAPI lifespan migration', 'httpx AsyncClient timeout').",
                    },
                    "domain": {
                        "type": "string",
                        "description": "Optional domain to restrict results to (e.g. 'docs.python.org', 'react.dev').",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results to return (default 5, max 20).",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_web_content",
            "description": (
                "Fetch and render a web page, GitHub issue, or documentation page as clean Markdown "
                "via TinyFish Fetch API (stealth Chromium rendering, ad/noise stripped). "
                "Use this after search_web_docs to read the full content of a specific URL. "
                "Only http:// and https:// URLs pointing to public hosts are permitted."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Full URL to fetch (e.g. 'https://docs.python.org/3/library/asyncio.html').",
                    },
                    "format": {
                        "type": "string",
                        "description": "Output format — 'markdown' (default) or 'text'.",
                    },
                },
                "required": ["url"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_package_metadata",
            "description": (
                "Check official package metadata (latest version, license, summary, dependencies) "
                "directly from PyPI or the npm registry. "
                "Use this to verify whether a dependency is up-to-date, identify breaking version jumps, "
                "or confirm the correct package name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ecosystem": {
                        "type": "string",
                        "enum": ["pypi", "npm"],
                        "description": "Package registry: 'pypi' for Python packages, 'npm' for JavaScript/TypeScript packages.",
                    },
                    "package_name": {
                        "type": "string",
                        "description": "Exact package name (e.g. 'httpx', 'react', '@types/node').",
                    },
                },
                "required": ["ecosystem", "package_name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_plan",
            "description": (
                "Update and render the multi-step execution plan checklist for the user. "
                "For multi-step requests, start by calling update_plan to outline your steps. "
                "Update task statuses ('pending', 'in_progress', 'completed', 'failed') as you progress."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "description": "List of task objects in the execution plan.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {
                                    "type": "string",
                                    "description": "Unique identifier for the task step (e.g. '1', 'explore_code').",
                                },
                                "title": {
                                    "type": "string",
                                    "description": "Short, human-readable description of the step.",
                                },
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed", "failed"],
                                    "description": "Current status of the task.",
                                },
                            },
                            "required": ["id", "title", "status"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["tasks"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user_clarification",
            "description": (
                "Ask the user for clarification when you encounter ambiguous architectural trade-offs, "
                "multiple valid implementation choices, or design decisions that require human input. "
                "This pauses agent execution and presents clickable choice pills in the frontend."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The specific question or decision for the user to answer.",
                    },
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of 2 to 5 actionable options or choices for the user to pick from.",
                    },
                },
                "required": ["question", "options"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "checkpoint_restore",
            "description": (
                "Restore the session state to a prior checkpoint by its checkpoint_id. "
                "Reverts staged patches and conversation history to the snapshotted state. "
                "Use this when a change direction was wrong and needs to be rolled back."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "checkpoint_id": {
                        "type": "string",
                        "description": "The checkpoint ID to restore to (e.g. 'cp_a1b2c3d4').",
                    },
                },
                "required": ["checkpoint_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scan_security_vulnerabilities",
            "description": (
                "Scan staged file paths for secrets (AWS keys, GitHub PATs, API keys) and "
                "code injection flaws (SQL f-string injection, shell injection) before committing. "
                "Call this before completing any task that modifies files to ensure no credentials "
                "or injection vulnerabilities were accidentally introduced."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of relative file paths to scan.",
                    },
                },
                "required": ["paths"],
                "additionalProperties": False,
            },
        },
    },
]


# ------------------------------------------------------------------
# System prompt builder
# ------------------------------------------------------------------

def _build_system_prompt(
    repo_owner: str,
    repo_name: str,
    branch_name: str,
    base_sha: str,
    staged_patches: dict[str, str],
) -> str:
    """Construct the system message injected at the start of every LLM call."""
    staged_summary = (
        "\n".join(f"  - {path}" for path in staged_patches)
        if staged_patches
        else "  (none)"
    )
    return (
        f"You are an expert pair-programming agent working on the repository "
        f"`{repo_owner}/{repo_name}` (branch: `{branch_name}`, base SHA: `{base_sha[:8]}`).\n\n"
        "Your job is to help the user understand, modify, and improve the codebase. "
        "You have access to the following tools:\n"
        "  1. `grep_search(query, path_prefix, case_sensitive, max_results)` — fast regex/substring search across repository files.\n"
        "  2. `glob_files(pattern, exclude_hidden)` — find files matching a glob pattern (e.g. `**/*auth*.py`).\n"
        "  3. `read_file_slice(path, start_line, end_line)` — surgical line-range reader with 1-based indexing.\n"
        "  4. `list_directory(path, depth)` — explore directory tree structure up to specified depth.\n"
        "  5. `read_file(path)` — read an entire source file from the repository.\n"
        "  6. `str_replace(path, old_str, new_str)` — exact string replacement (preferred for code edits; fails if old_str is not uniquely found).\n"
        "  7. `create_file(path, content)` — stage a new file.\n"
        "  8. `delete_file(path)` — stage deletion of a file.\n"
        "  9. `apply_multi_patch(patches)` — atomically apply edits across multiple files in one turn.\n"
        " 10. `stage_patch(path, diff, action)` — stage a raw unified diff patch for review (use str_replace instead when possible).\n"
        " 11. `discard_patch(path)` — remove a previously staged patch.\n"
        " 12. `get_file_outline(path)` — return signatures, classes, and docstrings of a file without implementation bodies (saves context tokens).\n"
        " 13. `find_symbol(name, kind)` — locate definitions of functions, classes, interfaces, or types across the codebase.\n"
        " 14. `find_references(symbol, path)` — find all call sites and usages of a symbol (word-boundary matched, capped at 50).\n"
        " 15. `run_terminal_command(command, timeout_sec)` — run a shell command and return stdout/stderr/exit code. Output streams live to the terminal drawer.\n"
        " 16. `run_linter(paths, linter)` — run ruff/eslint on the specified files and get diagnostics.\n"
        " 17. `run_targeted_tests(test_targets, timeout_sec)` — run pytest or vitest on specific test files and capture tracebacks.\n"
        " 18. `search_web_docs(query, domain, max_results)` — search live web/docs via TinyFish for up-to-date library APIs, breaking changes, and migration guides.\n"
        " 19. `fetch_web_content(url, format)` — fetch and render a public documentation page or GitHub issue as clean Markdown via TinyFish.\n"
        " 20. `fetch_package_metadata(ecosystem, package_name)` — check official latest version, license, and dependencies from PyPI or npm.\n"
        " 21. `update_plan(tasks)` — update and render a live multi-step task checklist (statuses: pending, in_progress, completed, failed).\n"
        " 22. `ask_user_clarification(question, options)` — pause execution and ask the user to pick between trade-offs or design decisions.\n\n"
        "For multi-step requests, start by calling update_plan to outline your steps. "
        "Update task statuses as you progress. If you encounter ambiguous architectural trade-offs, "
        "call ask_user_clarification to let the user decide.\n"
        "You have access to live web tools. Use `search_web_docs` and `fetch_web_content` via TinyFish "
        "to look up documentation and breaking API changes. "
        "Use `fetch_package_metadata` to check official package versions before suggesting upgrades.\n"
        "For code modifications, prefer `str_replace` over `stage_patch`. "
        "Always provide enough surrounding lines in `old_str` so it matches uniquely — "
        "the tool will reject the edit if `old_str` is ambiguous or missing.\n"
        "After proposing changes with `str_replace` or `create_file`, always run `run_targeted_tests` "
        "on the affected test files to verify your fix before declaring completion. "
        "If tests fail, read the traceback, correct the code with `str_replace`, and re-run until they pass.\n"
        "To prevent context-window bloat, prefer `get_file_outline` over reading entire files, "
        "and use `grep_search`, `glob_files`, `read_file_slice` for targeted exploration.\n"
        "Use `find_references` before renaming or refactoring a function to inspect all callers.\n"
        "Explain your reasoning clearly and concisely.\n"
        " 23. `checkpoint_restore(checkpoint_id)` — restore session state to a prior checkpoint, reverting staged patches and conversation history.\n"
        " 24. `scan_security_vulnerabilities(paths)` — scan staged files for secrets (AWS keys, GitHub PATs, API keys) and injection flaws before committing.\n\n"
        "Security requirement: Before completing any task that modifies files, call `scan_security_vulnerabilities` "
        "on the modified file paths to verify that no secrets or SQL injection vulnerabilities were accidentally introduced. "
        "Do not declare the task complete if violations are found — fix them first.\n\n"
        f"Currently staged files:\n{staged_summary}"
    )


def _infer_provider(model: str) -> str | None:
    """
    Infer the LLM provider from a model identifier string.

    Rules (applied in order):
    - Contains "/" (e.g. "openai/gpt-oss-120b", "meta-llama/…") → "groq"
    - Contains "llama", "mixtral", or "groq" (case-insensitive) → "groq"
    - Ends with "-free" or contains "nemotron" → "opencode_zen"
    - Otherwise: None (LLMClient falls back to DB-configured active provider)
    """
    m = model.strip().lower()
    if "/" in m:
        return "groq"
    if any(kw in m for kw in ("llama", "mixtral", "groq")):
        return "groq"
    if m.endswith("-free") or "nemotron" in m:
        return "opencode_zen"
    return None


# ------------------------------------------------------------------
# Concurrent-session guard error
# ------------------------------------------------------------------

class SessionBusyError(Exception):
    """Raised when a concurrent prompt is already in flight for this session."""


# ------------------------------------------------------------------
# Main orchestrator
# ------------------------------------------------------------------

class SessionOrchestrator:
    """
    Drives the LLM tool-calling loop for a single pairing session turn.

    One instance is created per HTTP request; it is NOT shared across requests.
    """

    def __init__(
        self,
        session_id: uuid.UUID,
        db: AsyncSession,
        gh_token: str | None = None,
    ) -> None:
        self.session_id = session_id
        self.db = db
        self.gh_token = gh_token
        self._llm = LLMClient(timeout=120.0)

    async def run(
        self,
        user_message: str,
        queue: SseQueue,
        model: str | None = None,
        provider: str | None = None,
    ) -> None:
        """
        Execute one conversational turn and stream events onto `queue`.

        Loads session state from DB, runs the LLM tool-calling loop, persists
        updated conversation_history and staged_patches, then emits `done`.

        Args:
            user_message: The user's prompt text.
            queue: SSE event queue for streaming back to the client.
            model: Optional model override (e.g. "llama-3.3-70b-versatile").
            provider: Optional provider override ("groq" | "opencode_zen"). If
                omitted and model is given, provider is inferred from the model
                name: Groq-style names (contains "llama", "groq", or "/" prefix)
                → "groq"; "-free" suffix or "nemotron" → "opencode_zen".

        All exceptions are caught here; an `error` SSE event is emitted and
        the queue is closed so the HTTP response terminates cleanly.
        """
        try:
            await self._execute(user_message, queue, model=model, provider=provider)
        except SessionBusyError:
            await queue.put_error(
                "Another prompt is already in progress for this session. Please wait.",
                "SESSION_BUSY",
            )
            await queue.close()
        except Exception as exc:
            logger.exception(
                "session_orchestrator: unhandled error in session=%s: %s", self.session_id, exc
            )
            await queue.put_error(
                "An internal error occurred while processing your request.",
                "INTERNAL_ERROR",
            )
            await queue.close()

    async def _execute(
        self,
        user_message: str,
        queue: SseQueue,
        model: str | None = None,
        provider: str | None = None,
    ) -> None:
        # Resolve effective provider — explicit > inferred from model name > DB config (LLMClient default).
        effective_provider: str | None = provider
        if effective_provider is None and model is not None:
            effective_provider = _infer_provider(model)

        # 1. Load session from DB — re-validate ownership at the data layer.
        session = await self._load_session()
        if session is None:
            await queue.put_error("Session not found or already closed.", "SESSION_NOT_FOUND")
            await queue.close()
            return

        repo = session.repo  # selectinloaded by _load_session

        # 2. Build messages: system prompt + history + new user message.
        conversation_history: list[dict[str, Any]] = list(session.conversation_history or [])
        staged_patches: dict[str, str] = dict(session.staged_patches or {})

        system_msg: dict[str, Any] = {
            "role": "system",
            "content": _build_system_prompt(
                repo_owner=repo.owner,
                repo_name=repo.name,
                branch_name=session.branch_name,
                base_sha=session.base_sha,
                staged_patches=staged_patches,
            ),
        }
        user_msg: dict[str, Any] = {"role": "user", "content": user_message}
        messages: list[dict[str, Any]] = [system_msg] + conversation_history + [user_msg]

        # Track assistant turns and tool results to append to history.
        new_entries: list[dict[str, Any]] = [user_msg]
        # Track the actual model that produced the final response (updated each LLM call).
        actual_model_used: str = model or ""

        # 3. Tool-calling loop — max 10 iterations to prevent runaway loops.
        MAX_ITERATIONS = 10
        for iteration in range(MAX_ITERATIONS):
            # Emit a heartbeat thought so the stream doesn't idle on large prompts.
            if iteration == 0:
                await queue.put_thought("Analyzing your request…")

            try:
                llm_kwargs: dict[str, Any] = {
                    "tool_choice": "auto",
                    "db": self.db,
                }
                if effective_provider is not None:
                    llm_kwargs["provider"] = effective_provider
                if model is not None:
                    llm_kwargs["model"] = model
                response = await self._llm.complete(
                    messages=messages,
                    tools=_TOOLS,
                    **llm_kwargs,
                )
            except LLMError as exc:
                logger.error(
                    "session_orchestrator: LLM error in session=%s iteration=%d: %s",
                    self.session_id, iteration, exc,
                )
                await queue.put_error("LLM request failed. Please try again.", "LLM_ERROR")
                await queue.close()
                return

            content: str | None = response.get("content")
            tool_calls: list[dict[str, Any]] | None = response.get("tool_calls")
            # Capture the actual model that responded (may differ from requested due to fallback).
            actual_model_used = response.get("model") or actual_model_used

            # Emit any assistant text as thought deltas.
            if content:
                await queue.put_thought(content)

            assistant_entry: dict[str, Any] = {"role": "assistant", "content": content or ""}
            if tool_calls:
                assistant_entry["tool_calls"] = tool_calls

            new_entries.append(assistant_entry)
            messages.append(assistant_entry)

            if not tool_calls:
                # No more tool calls — LLM is done.
                break

            # 4. Dispatch each tool call.
            paused_for_clarification = False
            for tc in tool_calls:
                tool_name: str = tc.get("function", {}).get("name", "")
                raw_args: str = tc.get("function", {}).get("arguments", "{}")
                tc_id: str = tc.get("id", "")

                try:
                    args: dict[str, Any] = json.loads(raw_args)
                except (json.JSONDecodeError, TypeError):
                    args = {}

                tool_result: str = await self._dispatch_tool(
                    tool_name=tool_name,
                    args=args,
                    repo_owner=repo.owner,
                    repo_name=repo.name,
                    base_sha=session.base_sha,
                    staged_patches=staged_patches,
                    queue=queue,
                    session=session,
                )

                if tool_name == "ask_user_clarification":
                    paused_for_clarification = True

                # Populate match_count / file_count / symbol_count on args for frontend UI chips.
                if tool_name == "grep_search":
                    if not tool_result.startswith("Error") and not tool_result.startswith("No matches"):
                        args["match_count"] = len([line for line in tool_result.splitlines() if line.strip()])
                    else:
                        args["match_count"] = 0
                elif tool_name == "glob_files":
                    if not tool_result.startswith("Error") and not tool_result.startswith("No files"):
                        args["file_count"] = len([line for line in tool_result.splitlines() if line.strip()])
                    else:
                        args["file_count"] = 0
                elif tool_name == "find_symbol":
                    if not tool_result.startswith("Error") and not tool_result.startswith("No definitions"):
                        # First line is the header "Found N definition(s)…"
                        data_lines = [line for line in tool_result.splitlines()[1:] if line.strip()]
                        args["symbol_count"] = len(data_lines)
                    else:
                        args["symbol_count"] = 0
                elif tool_name == "find_references":
                    if not tool_result.startswith("Error") and not tool_result.startswith("No references"):
                        data_lines = [line for line in tool_result.splitlines()[1:] if line.strip()]
                        args["match_count"] = len(data_lines)
                    else:
                        args["match_count"] = 0
                elif tool_name == "update_plan":
                    tasks = args.get("tasks", [])
                    if isinstance(tasks, list):
                        completed = sum(1 for t in tasks if isinstance(t, dict) and t.get("status") == "completed")
                        args["completed_count"] = completed
                        args["total_count"] = len(tasks)

                await queue.put_tool_call(tool_name, args)

                # Append tool result message for next LLM iteration.
                tool_result_msg: dict[str, Any] = {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": tool_result,
                }
                new_entries.append(tool_result_msg)
                messages.append(tool_result_msg)

                if paused_for_clarification:
                    break

            if paused_for_clarification:
                logger.info(
                    "session_orchestrator: session=%s paused for user clarification",
                    self.session_id,
                )
                break
        else:
            # Exceeded MAX_ITERATIONS — soft stop, not an error.
            logger.warning(
                "session_orchestrator: session=%s reached max iterations (%d)",
                self.session_id, MAX_ITERATIONS,
            )

        # 5. Persist updated state to DB atomically.
        updated_history = conversation_history + new_entries
        session.conversation_history = updated_history
        session.staged_patches = staged_patches

        # Auto-checkpoint after each turn if staged_patches were modified.
        turn_number = len(updated_history)
        if staged_patches:
            cp = create_checkpoint(
                session=session,
                description=f"Turn {turn_number}: {user_message[:80]}",
                turn=turn_number,
            )
            try:
                await queue.put_checkpoint_created(cp)
            except Exception as cp_exc:
                logger.warning(
                    "session_orchestrator: failed to emit checkpoint_created event: %s", cp_exc
                )

        await self._persist(session, updated_history, staged_patches)

        # 6. Signal stream end.
        await queue.put_done(
            session_id=str(self.session_id),
            staged_files_count=len(staged_patches),
            model_used=actual_model_used,
        )

    # ------------------------------------------------------------------
    # Tool dispatch
    # ------------------------------------------------------------------

    async def _dispatch_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        repo_owner: str,
        repo_name: str,
        base_sha: str,
        staged_patches: dict[str, str],
        queue: SseQueue,
        session: AgentSession | None = None,
    ) -> str:
        """
        Execute a single tool call and return a string result for the LLM.

        Mutates `staged_patches` in place for stage_patch / discard_patch.
        """
        if tool_name == "read_file":
            return await self._tool_read_file(
                args=args,
                repo_owner=repo_owner,
                repo_name=repo_name,
                base_sha=base_sha,
            )
        elif tool_name == "stage_patch":
            return await self._tool_stage_patch(
                args=args,
                staged_patches=staged_patches,
                queue=queue,
            )
        elif tool_name == "discard_patch":
            return self._tool_discard_patch(args=args, staged_patches=staged_patches)
        elif tool_name == "grep_search":
            return await self._tool_grep_search(
                args=args,
                repo_owner=repo_owner,
                repo_name=repo_name,
                base_sha=base_sha,
            )
        elif tool_name == "glob_files":
            return await self._tool_glob_files(
                args=args,
                repo_owner=repo_owner,
                repo_name=repo_name,
                base_sha=base_sha,
            )
        elif tool_name == "read_file_slice":
            return await self._tool_read_file_slice(
                args=args,
                repo_owner=repo_owner,
                repo_name=repo_name,
                base_sha=base_sha,
            )
        elif tool_name == "list_directory":
            return await self._tool_list_directory(
                args=args,
                repo_owner=repo_owner,
                repo_name=repo_name,
                base_sha=base_sha,
            )
        elif tool_name == "str_replace":
            return await self._tool_str_replace(
                args=args,
                repo_owner=repo_owner,
                repo_name=repo_name,
                base_sha=base_sha,
                staged_patches=staged_patches,
                queue=queue,
            )
        elif tool_name == "create_file":
            return await self._tool_create_file(
                args=args,
                staged_patches=staged_patches,
                queue=queue,
            )
        elif tool_name == "delete_file":
            return await self._tool_delete_file(
                args=args,
                repo_owner=repo_owner,
                repo_name=repo_name,
                base_sha=base_sha,
                staged_patches=staged_patches,
                queue=queue,
            )
        elif tool_name == "apply_multi_patch":
            return await self._tool_apply_multi_patch(
                args=args,
                repo_owner=repo_owner,
                repo_name=repo_name,
                base_sha=base_sha,
                staged_patches=staged_patches,
                queue=queue,
            )
        elif tool_name == "get_file_outline":
            return await self._tool_get_file_outline(
                args=args,
                repo_owner=repo_owner,
                repo_name=repo_name,
                base_sha=base_sha,
                staged_patches=staged_patches,
            )
        elif tool_name == "find_symbol":
            return await self._tool_find_symbol(
                args=args,
                repo_owner=repo_owner,
                repo_name=repo_name,
                base_sha=base_sha,
                staged_patches=staged_patches,
            )
        elif tool_name == "find_references":
            return await self._tool_find_references(
                args=args,
                repo_owner=repo_owner,
                repo_name=repo_name,
                base_sha=base_sha,
                staged_patches=staged_patches,
            )
        elif tool_name == "run_terminal_command":
            return await self._tool_run_terminal_command(
                args=args,
                queue=queue,
            )
        elif tool_name == "run_linter":
            return await self._tool_run_linter(
                args=args,
                queue=queue,
            )
        elif tool_name == "run_targeted_tests":
            return await self._tool_run_targeted_tests(
                args=args,
                queue=queue,
            )
        elif tool_name == "search_web_docs":
            return await self._tool_search_web_docs(args=args)
        elif tool_name == "fetch_web_content":
            return await self._tool_fetch_web_content(args=args)
        elif tool_name == "fetch_package_metadata":
            return await self._tool_fetch_package_metadata(args=args)
        elif tool_name == "update_plan":
            if session is None:
                return "Error: Session context is required to update plan."
            return await self._tool_update_plan(args=args, session=session, queue=queue)
        elif tool_name == "ask_user_clarification":
            if session is None:
                return "Error: Session context is required to request clarification."
            return await self._tool_ask_user_clarification(args=args, session=session, queue=queue)
        elif tool_name == "checkpoint_restore":
            if session is None:
                return "Error: Session context is required for checkpoint_restore."
            return await self._tool_checkpoint_restore(args=args, session=session, queue=queue)
        elif tool_name == "scan_security_vulnerabilities":
            if session is None:
                return "Error: Session context is required for scan_security_vulnerabilities."
            return self._tool_scan_security_vulnerabilities(
                args=args,
                session=session,
                repo_owner=repo_owner,
                repo_name=repo_name,
                base_sha=base_sha,
            )
        else:
            logger.warning(
                "session_orchestrator: unknown tool_name=%r in session=%s",
                tool_name, self.session_id,
            )
            return f"Unknown tool: {tool_name!r}"

    async def _tool_read_file(
        self,
        args: dict[str, Any],
        repo_owner: str,
        repo_name: str,
        base_sha: str,
    ) -> str:
        path: str = args.get("path", "")
        try:
            path = _validate_file_path(path)
        except ValueError as exc:
            return f"Error: {exc}"

        try:
            content = await fetch_file_content(
                owner=repo_owner,
                repo=repo_name,
                path=path,
                sha=base_sha,
                token=self.gh_token,
            )
        except GitHubClientError as exc:
            logger.warning(
                "session_orchestrator: read_file GitHub error for path=%s: %s", path, exc
            )
            return f"Error reading file: {exc}"

        if content is None:
            return f"File not found: {path!r}"

        # Truncate very large files to avoid bloating the context window.
        MAX_CONTENT_CHARS = 50_000
        if len(content) > MAX_CONTENT_CHARS:
            content = content[:MAX_CONTENT_CHARS] + f"\n\n[...truncated at {MAX_CONTENT_CHARS} chars]"

        return content

    async def _tool_stage_patch(
        self,
        args: dict[str, Any],
        staged_patches: dict[str, str],
        queue: SseQueue,
    ) -> str:
        path: str = args.get("path", "")
        diff: str = args.get("diff", "")
        action: str = args.get("action", "modify")

        # Validate path.
        try:
            path = _validate_file_path(path)
        except ValueError as exc:
            return f"Error: {exc}"

        # Validate diff — must be non-empty.
        if not diff.strip():
            return "Error: diff must not be empty."

        # Validate action.
        if action not in {"modify", "create", "delete"}:
            return f"Error: invalid action {action!r}. Must be 'modify', 'create', or 'delete'."

        # Validate patch size — mirror the SandboxInput 512 KB limit.
        MAX_PATCH_BYTES = 512 * 1024
        if len(diff.encode("utf-8")) > MAX_PATCH_BYTES:
            return "Error: diff exceeds maximum size (512 KB)."

        # Stage the patch (upsert).
        staged_patches[path] = diff

        # Emit a file_diff SSE event so the Monaco editor updates live.
        try:
            await queue.put_file_diff(path=path, diff=diff, action=action)
        except Exception as exc:
            logger.error("session_orchestrator: failed to emit file_diff for path=%s: %s", path, exc)

        return f"Patch staged for {path!r} (action={action})."

    def _tool_discard_patch(
        self,
        args: dict[str, Any],
        staged_patches: dict[str, str],
    ) -> str:
        path: str = args.get("path", "")
        try:
            path = _validate_file_path(path)
        except ValueError as exc:
            return f"Error: {exc}"

        if path in staged_patches:
            del staged_patches[path]
            return f"Patch for {path!r} discarded."
        return f"No staged patch found for {path!r}."

    async def _tool_grep_search(
        self,
        args: dict[str, Any],
        repo_owner: str,
        repo_name: str,
        base_sha: str,
    ) -> str:
        query: str = str(args.get("query", ""))
        path_prefix: str = str(args.get("path_prefix", ""))
        case_sensitive: bool = bool(args.get("case_sensitive", False))
        try:
            max_results: int = int(args.get("max_results", 25))
        except (TypeError, ValueError):
            max_results = 25

        try:
            return await tool_grep_search(
                query=query,
                path_prefix=path_prefix,
                case_sensitive=case_sensitive,
                max_results=max_results,
                owner=repo_owner,
                repo=repo_name,
                base_sha=base_sha,
                token=self.gh_token,
            )
        except (ValueError, GitHubClientError) as exc:
            return f"Error: {exc}"

    async def _tool_glob_files(
        self,
        args: dict[str, Any],
        repo_owner: str,
        repo_name: str,
        base_sha: str,
    ) -> str:
        pattern: str = str(args.get("pattern", ""))
        exclude_hidden: bool = bool(args.get("exclude_hidden", True))
        try:
            matching = await tool_glob_files(
                pattern=pattern,
                exclude_hidden=exclude_hidden,
                owner=repo_owner,
                repo=repo_name,
                base_sha=base_sha,
                token=self.gh_token,
            )
            return "\n".join(matching) if matching else f"No files matched pattern: {pattern!r}"
        except (ValueError, GitHubClientError) as exc:
            return f"Error: {exc}"

    async def _tool_read_file_slice(
        self,
        args: dict[str, Any],
        repo_owner: str,
        repo_name: str,
        base_sha: str,
    ) -> str:
        path: str = str(args.get("path", ""))
        try:
            start_line: int = int(args.get("start_line", 1))
            end_line: int = int(args.get("end_line", 1))
        except (TypeError, ValueError):
            return "Error: start_line and end_line must be valid integers."

        try:
            return await tool_read_file_slice(
                path=path,
                start_line=start_line,
                end_line=end_line,
                owner=repo_owner,
                repo=repo_name,
                base_sha=base_sha,
                token=self.gh_token,
            )
        except (ValueError, GitHubClientError) as exc:
            return f"Error: {exc}"

    async def _tool_list_directory(
        self,
        args: dict[str, Any],
        repo_owner: str,
        repo_name: str,
        base_sha: str,
    ) -> str:
        path: str = str(args.get("path", "."))
        try:
            depth: int = int(args.get("depth", 2))
        except (TypeError, ValueError):
            depth = 2

        try:
            entries = await tool_list_directory(
                path=path,
                depth=depth,
                owner=repo_owner,
                repo=repo_name,
                base_sha=base_sha,
                token=self.gh_token,
            )
            return "\n".join(entries) if entries else f"Directory empty or not found: {path!r}"
        except (ValueError, GitHubClientError) as exc:
            return f"Error: {exc}"

    async def _tool_str_replace(
        self,
        args: dict[str, Any],
        repo_owner: str,
        repo_name: str,
        base_sha: str,
        staged_patches: dict[str, str],
        queue: SseQueue,
    ) -> str:
        path: str = str(args.get("path", ""))
        old_str: str = str(args.get("old_str", ""))
        new_str: str = str(args.get("new_str", ""))
        return await tool_str_replace(
            path=path,
            old_str=old_str,
            new_str=new_str,
            repo_owner=repo_owner,
            repo_name=repo_name,
            base_sha=base_sha,
            staged_patches=staged_patches,
            queue=queue,
            gh_token=self.gh_token,
        )

    async def _tool_create_file(
        self,
        args: dict[str, Any],
        staged_patches: dict[str, str],
        queue: SseQueue,
    ) -> str:
        path: str = str(args.get("path", ""))
        content: str = str(args.get("content", ""))
        return await tool_create_file(
            path=path,
            content=content,
            staged_patches=staged_patches,
            queue=queue,
        )

    async def _tool_delete_file(
        self,
        args: dict[str, Any],
        repo_owner: str,
        repo_name: str,
        base_sha: str,
        staged_patches: dict[str, str],
        queue: SseQueue,
    ) -> str:
        path: str = str(args.get("path", ""))
        return await tool_delete_file(
            path=path,
            repo_owner=repo_owner,
            repo_name=repo_name,
            base_sha=base_sha,
            staged_patches=staged_patches,
            queue=queue,
            gh_token=self.gh_token,
        )

    async def _tool_apply_multi_patch(
        self,
        args: dict[str, Any],
        repo_owner: str,
        repo_name: str,
        base_sha: str,
        staged_patches: dict[str, str],
        queue: SseQueue,
    ) -> str:
        patches: list[dict[str, Any]] = args.get("patches", [])
        if not isinstance(patches, list):
            return "Error: 'patches' must be a list."
        return await tool_apply_multi_patch(
            patches=patches,
            repo_owner=repo_owner,
            repo_name=repo_name,
            base_sha=base_sha,
            staged_patches=staged_patches,
            queue=queue,
            gh_token=self.gh_token,
        )

    async def _tool_get_file_outline(
        self,
        args: dict[str, Any],
        repo_owner: str,
        repo_name: str,
        base_sha: str,
        staged_patches: dict[str, str],
    ) -> str:
        path: str = str(args.get("path", ""))
        try:
            return await tool_get_file_outline(
                path=path,
                repo_owner=repo_owner,
                repo_name=repo_name,
                base_sha=base_sha,
                staged_patches=staged_patches,
                gh_token=self.gh_token,
            )
        except (ValueError, GitHubClientError) as exc:
            return f"Error: {exc}"

    async def _tool_find_symbol(
        self,
        args: dict[str, Any],
        repo_owner: str,
        repo_name: str,
        base_sha: str,
        staged_patches: dict[str, str],
    ) -> str:
        name: str = str(args.get("name", ""))
        kind: str | None = args.get("kind") or None
        try:
            return await tool_find_symbol(
                name=name,
                kind=kind,
                repo_owner=repo_owner,
                repo_name=repo_name,
                base_sha=base_sha,
                staged_patches=staged_patches,
                gh_token=self.gh_token,
            )
        except (ValueError, GitHubClientError) as exc:
            return f"Error: {exc}"

    async def _tool_find_references(
        self,
        args: dict[str, Any],
        repo_owner: str,
        repo_name: str,
        base_sha: str,
        staged_patches: dict[str, str],
    ) -> str:
        symbol: str = str(args.get("symbol", ""))
        path: str | None = args.get("path") or None
        try:
            return await tool_find_references(
                symbol=symbol,
                path=path,
                repo_owner=repo_owner,
                repo_name=repo_name,
                base_sha=base_sha,
                staged_patches=staged_patches,
                gh_token=self.gh_token,
            )
        except (ValueError, GitHubClientError) as exc:
            return f"Error: {exc}"

    # ------------------------------------------------------------------
    # Web intelligence tool handlers (Phase 3)
    # ------------------------------------------------------------------

    async def _tool_search_web_docs(self, args: dict[str, Any]) -> str:
        query: str = str(args.get("query", ""))
        domain: str | None = args.get("domain") or None
        try:
            max_results: int = int(args.get("max_results", 5))
        except (TypeError, ValueError):
            max_results = 5
        return await tool_search_web_docs(
            query=query,
            domain=domain,
            max_results=max_results,
        )

    async def _tool_fetch_web_content(self, args: dict[str, Any]) -> str:
        url: str = str(args.get("url", ""))
        format: str = str(args.get("format", "markdown"))
        return await tool_fetch_web_content(url=url, format=format)

    async def _tool_fetch_package_metadata(self, args: dict[str, Any]) -> str:
        ecosystem: str = str(args.get("ecosystem", ""))
        package_name: str = str(args.get("package_name", ""))
        return await tool_fetch_package_metadata(
            ecosystem=ecosystem,
            package_name=package_name,
        )

    async def _tool_update_plan(
        self,
        args: dict[str, Any],
        session: AgentSession,
        queue: SseQueue,
    ) -> str:
        tasks = args.get("tasks", [])
        return await tool_update_plan(tasks=tasks, session=session, queue=queue, db=self.db)

    async def _tool_ask_user_clarification(
        self,
        args: dict[str, Any],
        session: AgentSession,
        queue: SseQueue,
    ) -> str:
        question = str(args.get("question", ""))
        raw_options = args.get("options", [])
        options = [str(opt) for opt in raw_options] if isinstance(raw_options, list) else []
        return await tool_ask_user_clarification(
            question=question,
            options=options,
            session=session,
            queue=queue,
            db=self.db,
        )

    async def _tool_checkpoint_restore(
        self,
        args: dict[str, Any],
        session: AgentSession,
        queue: SseQueue,
    ) -> str:
        checkpoint_id: str = str(args.get("checkpoint_id", "")).strip()
        if not checkpoint_id:
            return "Error: checkpoint_id is required."
        return await tool_checkpoint_restore(
            checkpoint_id=checkpoint_id,
            session=session,
            queue=queue,
            db=self.db,
        )

    def _tool_scan_security_vulnerabilities(
        self,
        args: dict[str, Any],
        session: AgentSession,
        repo_owner: str,
        repo_name: str,
        base_sha: str,
    ) -> str:
        raw_paths = args.get("paths", [])
        paths: list[str] = [str(p) for p in raw_paths] if isinstance(raw_paths, list) else []
        return tool_scan_security_vulnerabilities(
            paths=paths,
            session=session,
            repo_owner=repo_owner,
            repo_name=repo_name,
            base_sha=base_sha,
            gh_token=self.gh_token,
        )

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    async def _tool_run_terminal_command(
        self,
        args: dict[str, Any],
        queue: SseQueue,
    ) -> str:
        command: str = str(args.get("command", ""))
        try:
            timeout_sec: int = int(args.get("timeout_sec", 60))
        except (TypeError, ValueError):
            timeout_sec = 60
        result = await tool_run_terminal_command(
            command=command,
            timeout_sec=timeout_sec,
            queue=queue,
        )
        # Populate exit_code on args for frontend chip counters.
        first_line = result.splitlines()[0] if result else ""
        # Format: "Exit code: N (Duration: x.xxs)"
        m = re.match(r"Exit code:\s*(-?\d+)", first_line)
        if m:
            args["exit_code"] = int(m.group(1))
        return result

    async def _tool_run_linter(
        self,
        args: dict[str, Any],
        queue: SseQueue,
    ) -> str:
        raw_paths = args.get("paths", [])
        paths: list[str] = [str(p) for p in raw_paths] if isinstance(raw_paths, list) else []
        linter: str = str(args.get("linter", "auto"))
        try:
            timeout_sec: int = int(args.get("timeout_sec", 60))
        except (TypeError, ValueError):
            timeout_sec = 60
        result = await tool_run_linter(
            paths=paths,
            linter=linter,
            timeout_sec=timeout_sec,
            queue=queue,
        )
        # Populate file_count for frontend chip.
        args["file_count"] = len(paths)
        return result

    async def _tool_run_targeted_tests(
        self,
        args: dict[str, Any],
        queue: SseQueue,
    ) -> str:
        raw_targets = args.get("test_targets", [])
        test_targets: list[str] = (
            [str(t) for t in raw_targets] if isinstance(raw_targets, list) else []
        )
        try:
            timeout_sec: int = int(args.get("timeout_sec", 120))
        except (TypeError, ValueError):
            timeout_sec = 120
        result = await tool_run_targeted_tests(
            test_targets=test_targets,
            timeout_sec=timeout_sec,
            queue=queue,
        )
        # Populate target_count for frontend chip.
        args["target_count"] = len(test_targets)
        return result

    async def _load_session(self) -> AgentSession | None:
        """
        Load the AgentSession with its repo relationship.

        Returns None if the session does not exist or is not active.
        Note: object-level auth (user_id check) is enforced at the endpoint;
        this layer only checks existence and status.
        """
        from sqlalchemy.orm import selectinload

        stmt = (
            select(AgentSession)
            .options(selectinload(AgentSession.repo))
            .where(
                AgentSession.id == self.session_id,
                AgentSession.status == "active",
            )
        )
        result = await self.db.execute(stmt)
        return result.scalars().first()

    async def _persist(
        self,
        session: AgentSession,
        updated_history: list[dict[str, Any]],
        updated_patches: dict[str, str],
    ) -> None:
        """Atomically persist conversation_history and staged_patches."""
        session.conversation_history = updated_history
        session.staged_patches = updated_patches
        session.updated_at = datetime.now(timezone.utc)
        await self.db.commit()
        await self.db.refresh(session)
