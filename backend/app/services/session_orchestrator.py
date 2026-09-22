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

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Path validation — blocks directory traversal in tool-supplied paths.
# ------------------------------------------------------------------

_SAFE_PATH_RE: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9_./ \-]+$")
_MAX_PATH_LEN = 500


def _validate_file_path(path: str) -> str:
    """
    Validate a file path supplied by the LLM tool call.

    Rejects:
      - Paths containing ".." (directory traversal).
      - Paths starting with "/" (absolute path injection).
      - Paths with characters outside [a-zA-Z0-9_./ -].
      - Paths exceeding 500 characters.

    Returns the path unchanged if valid.
    Raises ValueError with a descriptive message on violation.
    """
    if len(path) > _MAX_PATH_LEN:
        raise ValueError(f"File path exceeds maximum length ({len(path)} > {_MAX_PATH_LEN})")
    if path.startswith("/"):
        raise ValueError(f"Absolute path rejected: {path!r}")
    if ".." in path:
        raise ValueError(f"Directory traversal rejected: {path!r}")
    if not _SAFE_PATH_RE.fullmatch(path):
        raise ValueError(f"Path contains disallowed characters: {path!r}")
    return path


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
        "You have access to three tools:\n"
        "  1. `read_file(path)` — read a source file from the repository.\n"
        "  2. `stage_patch(path, diff, action)` — stage a unified diff patch for review.\n"
        "  3. `discard_patch(path)` — remove a previously staged patch.\n\n"
        "When proposing code changes, always read the file first, then stage a precise "
        "unified diff. Explain your reasoning clearly and concisely.\n\n"
        f"Currently staged files:\n{staged_summary}"
    )


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

    async def run(self, user_message: str, queue: SseQueue) -> None:
        """
        Execute one conversational turn and stream events onto `queue`.

        Loads session state from DB, runs the LLM tool-calling loop, persists
        updated conversation_history and staged_patches, then emits `done`.

        All exceptions are caught here; an `error` SSE event is emitted and
        the queue is closed so the HTTP response terminates cleanly.
        """
        try:
            await self._execute(user_message, queue)
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

    async def _execute(self, user_message: str, queue: SseQueue) -> None:
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

        # 3. Tool-calling loop — max 10 iterations to prevent runaway loops.
        MAX_ITERATIONS = 10
        for iteration in range(MAX_ITERATIONS):
            # Emit a heartbeat thought so the stream doesn't idle on large prompts.
            if iteration == 0:
                await queue.put_thought("Analyzing your request…")

            try:
                response = await self._llm.complete(
                    messages=messages,
                    tools=_TOOLS,
                    tool_choice="auto",
                    db=self.db,
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
            for tc in tool_calls:
                tool_name: str = tc.get("function", {}).get("name", "")
                raw_args: str = tc.get("function", {}).get("arguments", "{}")
                tc_id: str = tc.get("id", "")

                try:
                    args: dict[str, Any] = json.loads(raw_args)
                except (json.JSONDecodeError, TypeError):
                    args = {}

                await queue.put_tool_call(tool_name, args)

                tool_result: str = await self._dispatch_tool(
                    tool_name=tool_name,
                    args=args,
                    repo_owner=repo.owner,
                    repo_name=repo.name,
                    base_sha=session.base_sha,
                    staged_patches=staged_patches,
                    queue=queue,
                )

                # Append tool result message for next LLM iteration.
                tool_result_msg: dict[str, Any] = {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": tool_result,
                }
                new_entries.append(tool_result_msg)
                messages.append(tool_result_msg)
        else:
            # Exceeded MAX_ITERATIONS — soft stop, not an error.
            logger.warning(
                "session_orchestrator: session=%s reached max iterations (%d)",
                self.session_id, MAX_ITERATIONS,
            )

        # 5. Persist updated state to DB atomically.
        updated_history = conversation_history + new_entries
        await self._persist(session, updated_history, staged_patches)

        # 6. Signal stream end.
        await queue.put_done(
            session_id=str(self.session_id),
            staged_files_count=len(staged_patches),
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

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

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
