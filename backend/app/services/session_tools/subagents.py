"""
Subagent Core — Specialized Subagent Delegation Phase 1 (future.md §1.3).

Provides:
  - RoleConfig: frozen dataclass with tool allowlist, iteration cap, prompt suffix.
  - VALID_ROLES / ROLE_CONFIGS: registry for the 5 specialized roles.
  - SubagentError: typed failure with role + message.
  - SubagentRunner: sequential, blocking subagent executor sharing the
    parent's staged_patches dict by reference.

Invariants (§1.2):
  - staged_patches shared by reference (never copied).
  - no nested invoke_subagent (blocked at dispatch layer).
  - hard MAX_ITERATIONS per role (RoleConfig.max_iterations).
  - path traversal guards + patch size limits apply identically
    (delegation to the same session_tools/* implementations as the parent).
  - sequential blocking run().
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from app.github_client import GitHubClientError, fetch_file_content
from app.llm.client import LLMClient
from app.llm.exceptions import LLMError
from app.models import AgentSession
from app.services.session_streamer import SseQueue
from app.services.session_tools.audit import tool_run_audit_scan
from app.services.session_tools.checkpoints import tool_scan_security_vulnerabilities
from app.services.session_tools.editor import (
    tool_apply_multi_patch,
    tool_create_file,
    tool_delete_file,
    tool_str_replace,
)
from app.services.session_tools.git import (
    tool_git_blame,
    tool_git_diff,
    tool_git_log,
    tool_git_show,
)
from app.services.session_tools.recon import (
    _validate_file_path,
    tool_glob_files,
    tool_grep_search,
    tool_list_directory,
    tool_read_file_slice,
)
from app.services.session_tools.sandbox import (
    tool_run_linter,
    tool_run_targeted_tests,
    tool_run_terminal_command,
)
from app.services.session_tools.symbols import (
    tool_find_references,
    tool_find_symbol,
    tool_get_file_outline,
)
from app.services.session_tools.web import (
    tool_fetch_package_metadata,
    tool_fetch_web_content,
    tool_search_web_docs,
)

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# RoleConfig registry
# ------------------------------------------------------------------


@dataclass(frozen=True)
class RoleConfig:
    role: str
    allowed_tools: frozenset[str]
    max_iterations: int
    system_prompt_suffix: str


_VALID_ROLES: frozenset[str] = frozenset(
    {
        "repo_navigator",
        "feature_architect",
        "bug_hunter",
        "sandbox_verifier",
        "code_guardian",
    }
)

VALID_ROLES: frozenset[str] = _VALID_ROLES

# Shared read-only recon surface for roles that need codebase exploration.
_NAVIGATOR_TOOLS: frozenset[str] = frozenset(
    {
        "grep_search",
        "glob_files",
        "read_file_slice",
        "list_directory",
        "read_file",
        "get_file_outline",
        "find_symbol",
        "find_references",
        "search_web_docs",
        "fetch_web_content",
        "fetch_package_metadata",
    }
)

ROLE_CONFIGS: dict[str, RoleConfig] = {
    "repo_navigator": RoleConfig(
        role="repo_navigator",
        allowed_tools=_NAVIGATOR_TOOLS,
        max_iterations=12,
        system_prompt_suffix=(
            "You are RepoNavigator, a read-only codebase reconnaissance specialist. "
            "Explore the repository with grep_search, glob_files, read_file_slice, "
            "list_directory, get_file_outline, find_symbol, and find_references. "
            "Use web tools only for external API references. "
            "Never modify code — return a structured summary of file paths, "
            "symbols, and cross-file relationships relevant to the task."
        ),
    ),
    "feature_architect": RoleConfig(
        role="feature_architect",
        allowed_tools=_NAVIGATOR_TOOLS
        | frozenset(
            {
                "str_replace",
                "create_file",
                "delete_file",
                "apply_multi_patch",
                "run_linter",
                "scan_security_vulnerabilities",
            }
        ),
        max_iterations=20,
        system_prompt_suffix=(
            "You are FeatureArchitect, a multi-file feature implementation specialist. "
            "Explore with navigator tools, then implement with str_replace, "
            "create_file, delete_file, or apply_multi_patch. "
            "Run run_linter on edited files and scan_security_vulnerabilities "
            "before finishing. Prefer str_replace over raw diffs."
        ),
    ),
    "bug_hunter": RoleConfig(
        role="bug_hunter",
        allowed_tools=_NAVIGATOR_TOOLS
        | frozenset(
            {
                "git_log",
                "git_blame",
                "git_show",
                "git_diff",
                "str_replace",
                "run_targeted_tests",
                "run_linter",
                "scan_security_vulnerabilities",
            }
        ),
        max_iterations=16,
        system_prompt_suffix=(
            "You are BugHunter, a root-cause diagnosis and surgical-fix specialist. "
            "Use git_log, git_blame, git_show, and git_diff for provenance, "
            "navigator tools for context, then fix with str_replace. "
            "Verify with run_targeted_tests and run_linter, and scan for "
            "secrets before finishing."
        ),
    ),
    "sandbox_verifier": RoleConfig(
        role="sandbox_verifier",
        allowed_tools=frozenset(
            {
                "run_targeted_tests",
                "run_linter",
                "run_terminal_command",
                "glob_files",
                "read_file_slice",
            }
        ),
        max_iterations=8,
        system_prompt_suffix=(
            "You are SandboxVerifier, a test-execution and validation specialist. "
            "Run run_targeted_tests, run_linter, and run_terminal_command to "
            "validate correctness. Use glob_files and read_file_slice only to "
            "locate targets. You are read-only — never write or modify code."
        ),
    ),
    "code_guardian": RoleConfig(
        role="code_guardian",
        allowed_tools=_NAVIGATOR_TOOLS
        | frozenset(
            {
                "git_diff",
                "git_log",
                "scan_security_vulnerabilities",
                "run_audit_scan",
            }
        ),
        max_iterations=10,
        system_prompt_suffix=(
            "You are CodeGuardian, a security, performance, and API-compatibility "
            "reviewer. You are strictly read-only by design — no editor tools. "
            "Review staged diffs with git_diff and git_log, run in-depth audits with "
            "run_audit_scan or scan_security_vulnerabilities, and return structured findings only."
        ),
    ),
}


# ------------------------------------------------------------------
# SubagentError
# ------------------------------------------------------------------


class SubagentError(Exception):
    """Typed subagent failure carrying the role and a safe message."""

    def __init__(self, role: str, message: str) -> None:
        super().__init__(f"Subagent '{role}' failed: {message}")
        self.role = role
        self.message = message


# ------------------------------------------------------------------
# SubagentRunner
# ------------------------------------------------------------------


class SubagentRunner:
    """
    Sequential, blocking subagent executor.

    Shares the parent's staged_patches dict by reference — mutations are
    immediately visible to the parent orchestrator.
    """

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
    ) -> None:
        self.role = role
        self.task = task
        self.target_files = list(target_files) if target_files else []
        self.repo_owner = repo_owner
        self.repo_name = repo_name
        self.base_sha = base_sha
        # Shared reference — never copy. Mutations propagate to the parent.
        self.staged_patches = staged_patches
        self.session = session
        self.queue = queue
        self.llm = llm
        self.gh_token = gh_token
        self.model = model
        self.provider = provider

    # ------------------------------------------------------------------
    # Public entrypoint
    # ------------------------------------------------------------------

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
        config = ROLE_CONFIGS.get(self.role)
        if config is None:
            raise SubagentError(self.role, f"unknown role {self.role!r}.")
        if config.max_iterations <= 0:
            raise SubagentError(self.role, "max_iterations must be positive.")

        start_time = time.monotonic()
        _llm_responses: list[dict[str, Any]] = []

        await self.queue.put_subagent_start(role=self.role, task=self.task)

        initial_snapshot: dict[str, str] = dict(self.staged_patches)

        system_content = self._build_role_prompt(config)
        task_hint = self.task
        if self.target_files:
            task_hint += "\n\nFocus files:\n" + "\n".join(
                f"  - {p}" for p in self.target_files
            )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": task_hint},
        ]

        tools = self._filtered_tools(config)
        completed_iterations = 0
        tool_outputs: list[str] = []
        final_content: str | None = None
        truncated = False

        for _iteration in range(config.max_iterations):
            llm_kwargs: dict[str, Any] = {"tool_choice": "auto"}
            if self.provider is not None:
                llm_kwargs["provider"] = self.provider
            if self.model is not None:
                llm_kwargs["model"] = self.model
            try:
                response = await self.llm.complete(
                    messages=messages,
                    tools=tools,
                    **llm_kwargs,
                )
            except LLMError as exc:
                logger.error("subagent_runner: LLM error role=%s: %s", self.role, exc)
                raise SubagentError(self.role, f"LLM unavailable: {exc}") from exc

            _llm_responses.append(response)
            completed_iterations += 1
            content: str | None = response.get("content")
            tool_calls: list[dict[str, Any]] | None = response.get("tool_calls")

            assistant_entry: dict[str, Any] = {
                "role": "assistant",
                "content": content or "",
            }
            if tool_calls:
                assistant_entry["tool_calls"] = tool_calls
            messages.append(assistant_entry)

            if content:
                final_content = content

            if not tool_calls:
                break

            for tc in tool_calls:
                tool_name: str = tc.get("function", {}).get("name", "")
                raw_args: str = tc.get("function", {}).get("arguments", "{}")
                tc_id: str = tc.get("id", "")
                try:
                    args: dict[str, Any] = json.loads(raw_args)
                except (json.JSONDecodeError, TypeError):
                    args = {}
                if not isinstance(args, dict):
                    args = {}

                tool_result = await self._dispatch_subagent_tool(tool_name, args)
                tool_outputs.append(tool_result)

                try:
                    await self.queue.put_tool_call(tool_name, args)
                except Exception as exc:
                    logger.warning(
                        "subagent_runner: failed to emit tool_call role=%s tool=%s: %s",
                        self.role,
                        tool_name,
                        exc,
                    )

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": tool_result,
                    }
                )
        else:
            # Loop exhausted without LLM signalling done.
            truncated = True

        if completed_iterations == 0:
            raise SubagentError(self.role, "zero iterations completed.")

        if final_content:
            summary = final_content
        elif tool_outputs:
            summary = tool_outputs[-1]
        else:
            summary = f"Subagent '{self.role}' completed with no output."

        if truncated:
            summary = (
                f"{summary}\n\n[Note: subagent '{self.role}' reached "
                f"max_iterations ({config.max_iterations}) — output truncated.]"
            )

        patches_modified = sorted(
            k
            for k in self.staged_patches
            if k not in initial_snapshot
            or initial_snapshot[k] != self.staged_patches[k]
        )

        try:
            await self.queue.put_subagent_done(
                role=self.role,
                summary=summary,
                patches_modified=patches_modified,
            )
        except Exception as exc:
            logger.warning(
                "subagent_runner: failed to emit subagent_done role=%s: %s",
                self.role,
                exc,
            )

        latency_ms = int((time.monotonic() - start_time) * 1000)
        total_input_tokens = 0
        total_output_tokens = 0
        for r in _llm_responses:
            usage = (r.get("usage") or {}) if isinstance(r, dict) else {}
            if not isinstance(usage, dict):
                continue
            try:
                total_input_tokens += int(
                    usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
                )
            except (TypeError, ValueError):
                pass
            try:
                total_output_tokens += int(
                    usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
                )
            except (TypeError, ValueError):
                pass

        logger.info(
            "subagent_telemetry role=%s session=%s input_tokens=%d output_tokens=%d "
            "latency_ms=%d iterations=%d",
            self.role,
            str(getattr(self.session, "id", "unknown")),
            total_input_tokens,
            total_output_tokens,
            latency_ms,
            completed_iterations,
        )

        return summary

    # ------------------------------------------------------------------
    # Prompt + tool filtering
    # ------------------------------------------------------------------

    def _build_role_prompt(self, config: RoleConfig) -> str:
        base = (
            f"You are a specialized subagent ({config.role}) working on repository "
            f"`{self.repo_owner}/{self.repo_name}` at base SHA `{self.base_sha[:8]}`. "
            f"Parent task briefing is provided as the user message."
        )
        return f"{base}\n\n{config.system_prompt_suffix}"

    def _filtered_tools(self, config: RoleConfig) -> list[dict[str, Any]] | None:
        """Return the parent _TOOLS subset restricted to the role allowlist."""
        try:
            from app.services.session_orchestrator import _TOOLS as _PARENT_TOOLS
        except Exception as exc:
            logger.warning(
                "subagent_runner: could not load parent _TOOLS for role=%s: %s",
                self.role,
                exc,
            )
            return None
        return [
            t
            for t in _PARENT_TOOLS
            if t.get("function", {}).get("name") in config.allowed_tools
        ]

    # ------------------------------------------------------------------
    # Role-filtered dispatch
    # ------------------------------------------------------------------

    async def _dispatch_subagent_tool(
        self, tool_name: str, args: dict[str, Any]
    ) -> str:
        # Block recursive invocation
        if tool_name == "invoke_subagent":
            return "Error: invoke_subagent is not available inside a subagent context."

        # Enforce role-level tool allowlist
        config = ROLE_CONFIGS.get(self.role)
        if config is None:
            return f"Error: unknown role {self.role!r}."
        if tool_name not in config.allowed_tools:
            return (
                f"Error: tool '{tool_name}' is not permitted for role '{self.role}'. "
                f"Allowed: {sorted(config.allowed_tools)}."
            )

        # Call the same session_tools/* implementations used by the parent orchestrator
        # (imported directly — no duplication).
        if tool_name == "read_file":
            return await self._exec_read_file(args)
        elif tool_name == "grep_search":
            return await self._exec_grep_search(args)
        elif tool_name == "glob_files":
            return await self._exec_glob_files(args)
        elif tool_name == "read_file_slice":
            return await self._exec_read_file_slice(args)
        elif tool_name == "list_directory":
            return await self._exec_list_directory(args)
        elif tool_name == "str_replace":
            return await tool_str_replace(
                path=str(args.get("path", "")),
                old_str=str(args.get("old_str", "")),
                new_str=str(args.get("new_str", "")),
                repo_owner=self.repo_owner,
                repo_name=self.repo_name,
                base_sha=self.base_sha,
                staged_patches=self.staged_patches,
                queue=self.queue,
                gh_token=self.gh_token,
            )
        elif tool_name == "create_file":
            return await tool_create_file(
                path=str(args.get("path", "")),
                content=str(args.get("content", "")),
                staged_patches=self.staged_patches,
                queue=self.queue,
            )
        elif tool_name == "delete_file":
            return await tool_delete_file(
                path=str(args.get("path", "")),
                repo_owner=self.repo_owner,
                repo_name=self.repo_name,
                base_sha=self.base_sha,
                staged_patches=self.staged_patches,
                queue=self.queue,
                gh_token=self.gh_token,
            )
        elif tool_name == "apply_multi_patch":
            patches = args.get("patches", [])
            if not isinstance(patches, list):
                return "Error: 'patches' must be a list."
            return await tool_apply_multi_patch(
                patches=patches,
                repo_owner=self.repo_owner,
                repo_name=self.repo_name,
                base_sha=self.base_sha,
                staged_patches=self.staged_patches,
                queue=self.queue,
                gh_token=self.gh_token,
            )
        elif tool_name == "get_file_outline":
            return await self._exec_get_file_outline(args)
        elif tool_name == "find_symbol":
            return await self._exec_find_symbol(args)
        elif tool_name == "find_references":
            return await self._exec_find_references(args)
        elif tool_name == "run_terminal_command":
            return await self._exec_run_terminal_command(args)
        elif tool_name == "run_linter":
            return await self._exec_run_linter(args)
        elif tool_name == "run_targeted_tests":
            return await self._exec_run_targeted_tests(args)
        elif tool_name == "search_web_docs":
            return await self._exec_search_web_docs(args)
        elif tool_name == "fetch_web_content":
            return await self._exec_fetch_web_content(args)
        elif tool_name == "fetch_package_metadata":
            return await self._exec_fetch_package_metadata(args)
        elif tool_name == "git_log":
            return await self._exec_git_log(args)
        elif tool_name == "git_blame":
            return await self._exec_git_blame(args)
        elif tool_name == "git_show":
            return await self._exec_git_show(args)
        elif tool_name == "git_diff":
            return await self._exec_git_diff(args)
        elif tool_name == "scan_security_vulnerabilities":
            return self._exec_scan_security(args)
        elif tool_name == "run_audit_scan":
            return await self._exec_run_audit_scan(args)
        else:
            logger.warning(
                "subagent_runner: unknown tool_name=%r role=%s",
                tool_name,
                self.role,
            )
            return f"Unknown tool: {tool_name!r}"

    # ------------------------------------------------------------------
    # Per-tool executors (mirror SessionOrchestrator._tool_* semantics)
    # ------------------------------------------------------------------

    async def _exec_read_file(self, args: dict[str, Any]) -> str:
        path: str = str(args.get("path", ""))
        try:
            path = _validate_file_path(path)
        except ValueError as exc:
            return f"Error: {exc}"
        try:
            content = await fetch_file_content(
                owner=self.repo_owner,
                repo=self.repo_name,
                path=path,
                sha=self.base_sha,
                token=self.gh_token,
            )
        except GitHubClientError as exc:
            logger.warning(
                "subagent_runner: read_file GitHub error path=%s: %s", path, exc
            )
            return f"Error reading file: {exc}"
        if content is None:
            return f"File not found: {path!r}"
        max_chars = 50_000
        if len(content) > max_chars:
            content = content[:max_chars] + f"\n\n[...truncated at {max_chars} chars]"
        return content

    async def _exec_grep_search(self, args: dict[str, Any]) -> str:
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
                owner=self.repo_owner,
                repo=self.repo_name,
                base_sha=self.base_sha,
                token=self.gh_token,
            )
        except (ValueError, GitHubClientError) as exc:
            return f"Error: {exc}"

    async def _exec_glob_files(self, args: dict[str, Any]) -> str:
        pattern: str = str(args.get("pattern", ""))
        exclude_hidden: bool = bool(args.get("exclude_hidden", True))
        try:
            matching = await tool_glob_files(
                pattern=pattern,
                exclude_hidden=exclude_hidden,
                owner=self.repo_owner,
                repo=self.repo_name,
                base_sha=self.base_sha,
                token=self.gh_token,
            )
            return (
                "\n".join(matching)
                if matching
                else f"No files matched pattern: {pattern!r}"
            )
        except (ValueError, GitHubClientError) as exc:
            return f"Error: {exc}"

    async def _exec_read_file_slice(self, args: dict[str, Any]) -> str:
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
                owner=self.repo_owner,
                repo=self.repo_name,
                base_sha=self.base_sha,
                token=self.gh_token,
            )
        except (ValueError, GitHubClientError) as exc:
            return f"Error: {exc}"

    async def _exec_list_directory(self, args: dict[str, Any]) -> str:
        path: str = str(args.get("path", "."))
        try:
            depth: int = int(args.get("depth", 2))
        except (TypeError, ValueError):
            depth = 2
        try:
            entries = await tool_list_directory(
                path=path,
                depth=depth,
                owner=self.repo_owner,
                repo=self.repo_name,
                base_sha=self.base_sha,
                token=self.gh_token,
            )
            return (
                "\n".join(entries)
                if entries
                else f"Directory empty or not found: {path!r}"
            )
        except (ValueError, GitHubClientError) as exc:
            return f"Error: {exc}"

    async def _exec_get_file_outline(self, args: dict[str, Any]) -> str:
        path: str = str(args.get("path", ""))
        try:
            return await tool_get_file_outline(
                path=path,
                repo_owner=self.repo_owner,
                repo_name=self.repo_name,
                base_sha=self.base_sha,
                staged_patches=self.staged_patches,
                gh_token=self.gh_token,
            )
        except (ValueError, GitHubClientError) as exc:
            return f"Error: {exc}"

    async def _exec_find_symbol(self, args: dict[str, Any]) -> str:
        name: str = str(args.get("name", ""))
        kind: str | None = args.get("kind") or None
        try:
            return await tool_find_symbol(
                name=name,
                kind=kind,
                repo_owner=self.repo_owner,
                repo_name=self.repo_name,
                base_sha=self.base_sha,
                staged_patches=self.staged_patches,
                gh_token=self.gh_token,
            )
        except (ValueError, GitHubClientError) as exc:
            return f"Error: {exc}"

    async def _exec_find_references(self, args: dict[str, Any]) -> str:
        symbol: str = str(args.get("symbol", ""))
        path: str | None = args.get("path") or None
        try:
            return await tool_find_references(
                symbol=symbol,
                path=path,
                repo_owner=self.repo_owner,
                repo_name=self.repo_name,
                base_sha=self.base_sha,
                staged_patches=self.staged_patches,
                gh_token=self.gh_token,
            )
        except (ValueError, GitHubClientError) as exc:
            return f"Error: {exc}"

    async def _exec_run_terminal_command(self, args: dict[str, Any]) -> str:
        """Subagent tool runner for terminal commands scoped to the session repository."""
        command: str = str(args.get("command", ""))
        cwd = args.get("cwd")
        cwd_str: str | None = str(cwd) if cwd is not None else None
        try:
            timeout_sec: int = int(args.get("timeout_sec", 60))
        except (TypeError, ValueError):
            timeout_sec = 60
        return await tool_run_terminal_command(
            command=command,
            timeout_sec=timeout_sec,
            queue=self.queue,
            cwd=cwd_str,
            repo_owner=self.repo_owner,
            repo_name=self.repo_name,
        )

    async def _exec_run_linter(self, args: dict[str, Any]) -> str:
        """Subagent tool runner for static code analysis scoped to the session repository."""
        raw_paths = args.get("paths", [])
        paths: list[str] = (
            [str(p) for p in raw_paths] if isinstance(raw_paths, list) else []
        )
        linter: str = str(args.get("linter", "auto"))
        cwd = args.get("cwd")
        cwd_str: str | None = str(cwd) if cwd is not None else None
        try:
            timeout_sec: int = int(args.get("timeout_sec", 60))
        except (TypeError, ValueError):
            timeout_sec = 60
        return await tool_run_linter(
            paths=paths,
            linter=linter,
            timeout_sec=timeout_sec,
            queue=self.queue,
            cwd=cwd_str,
            repo_owner=self.repo_owner,
            repo_name=self.repo_name,
        )

    async def _exec_run_targeted_tests(self, args: dict[str, Any]) -> str:
        """Subagent tool runner for test execution scoped to the session repository."""
        raw_targets = args.get("test_targets", [])
        test_targets: list[str] = (
            [str(t) for t in raw_targets] if isinstance(raw_targets, list) else []
        )
        cwd = args.get("cwd")
        cwd_str: str | None = str(cwd) if cwd is not None else None
        try:
            timeout_sec: int = int(args.get("timeout_sec", 120))
        except (TypeError, ValueError):
            timeout_sec = 120
        return await tool_run_targeted_tests(
            test_targets=test_targets,
            timeout_sec=timeout_sec,
            queue=self.queue,
            cwd=cwd_str,
            repo_owner=self.repo_owner,
            repo_name=self.repo_name,
        )

    async def _exec_search_web_docs(self, args: dict[str, Any]) -> str:
        query: str = str(args.get("query", ""))
        domain = args.get("domain") or None
        try:
            max_results: int = int(args.get("max_results", 5))
        except (TypeError, ValueError):
            max_results = 5
        return await tool_search_web_docs(
            query=query,
            domain=domain,
            max_results=max_results,
        )

    async def _exec_fetch_web_content(self, args: dict[str, Any]) -> str:
        url: str = str(args.get("url", ""))
        fmt: str = str(args.get("format", "markdown"))
        return await tool_fetch_web_content(url=url, format=fmt)

    async def _exec_fetch_package_metadata(self, args: dict[str, Any]) -> str:
        ecosystem: str = str(args.get("ecosystem", ""))
        package_name: str = str(args.get("package_name", ""))
        return await tool_fetch_package_metadata(
            ecosystem=ecosystem,
            package_name=package_name,
        )

    async def _exec_git_log(self, args: dict[str, Any]) -> str:
        path: str = str(args.get("path", ""))
        try:
            limit: int = int(args.get("limit", 20))
        except (TypeError, ValueError):
            limit = 20
        return await tool_git_log(
            branch=self.base_sha,
            path=path,
            limit=limit,
            owner=self.repo_owner,
            repo=self.repo_name,
            token=self.gh_token,
        )

    async def _exec_git_blame(self, args: dict[str, Any]) -> str:
        path: str = str(args.get("path", ""))
        if not path:
            return "Error: 'path' is required for git_blame."
        return await tool_git_blame(
            path=path,
            ref=self.base_sha,
            owner=self.repo_owner,
            repo=self.repo_name,
            token=self.gh_token,
        )

    async def _exec_git_show(self, args: dict[str, Any]) -> str:
        commit_sha: str = str(args.get("commit_sha", ""))
        if not commit_sha:
            return "Error: 'commit_sha' is required for git_show."
        return await tool_git_show(
            commit_sha=commit_sha,
            owner=self.repo_owner,
            repo=self.repo_name,
            token=self.gh_token,
        )

    async def _exec_git_diff(self, args: dict[str, Any]) -> str:
        base: str = str(args.get("base", ""))
        head: str = str(args.get("head", ""))
        if not base or not head:
            return "Error: 'base' and 'head' are both required for git_diff."
        return await tool_git_diff(
            base=base,
            head=head,
            owner=self.repo_owner,
            repo=self.repo_name,
            token=self.gh_token,
        )

    def _exec_scan_security(self, args: dict[str, Any]) -> str:
        raw_paths = args.get("paths", [])
        paths: list[str] = (
            [str(p) for p in raw_paths] if isinstance(raw_paths, list) else []
        )
        # Sync turn-local edits to session — scanner reads session.staged_patches.
        self.session.staged_patches = dict(self.staged_patches)
        return tool_scan_security_vulnerabilities(
            paths=paths,
            session=self.session,
            repo_owner=self.repo_owner,
            repo_name=self.repo_name,
            base_sha=self.base_sha,
            gh_token=self.gh_token,
        )

    async def _exec_run_audit_scan(self, args: dict[str, Any]) -> str:
        # Sync turn-local edits to session
        self.session.staged_patches = dict(self.staged_patches)
        return await tool_run_audit_scan(
            args=args,
            session=self.session,
            repo_owner=self.repo_owner,
            repo_name=self.repo_name,
            base_sha=self.base_sha,
            staged_patches=self.staged_patches,
            queue=self.queue,
            llm=self.llm,
            gh_token=self.gh_token,
            db=None,
        )
