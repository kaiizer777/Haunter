"""
SSE Streamer Engine — Cloud Agentic Live Session Phase 2.

Provides:
  - format_sse_event(): canonical SSE wire-format serialiser.
  - SseQueue:           asyncio.Queue-backed async generator, decoupling the
                        orchestrator (producer) from the HTTP response (consumer).

Wire format per spec:
    event: <name>\\n
    data: <json_string>\\n
    \\n

Allowed event names: thought | tool_call | file_diff | sandbox_status | sandbox_queued | sandbox_progress | terminal_output | plan_update | clarification_requested | checkpoint_created | checkpoint_restored | subagent_start | subagent_done | error | done

Security:
  - Never emits raw exception objects or internal paths to the stream.
    Callers must sanitise before calling format_sse_event("error", ...).
  - Content-Type for StreamingResponse must be "text/event-stream".

Required StreamingResponse headers (set by the endpoint, not here):
    Cache-Control: no-cache
    Connection: keep-alive
    X-Accel-Buffering: no
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, AsyncGenerator

logger = logging.getLogger(__name__)

# Sentinel object placed on the queue to signal stream end.
_STREAM_DONE = object()

# Allowed SSE event names — enforced at format time to prevent protocol drift.
_ALLOWED_EVENTS: frozenset[str] = frozenset(
    {
        "thought",
        "tool_call",
        "file_diff",
        "sandbox_status",
        "sandbox_queued",
        "sandbox_progress",
        "terminal_output",
        "plan_update",
        "clarification_requested",
        "checkpoint_created",
        "checkpoint_restored",
        "subagent_start",
        "subagent_done",
        "audit_scan_start",
        "audit_progress",
        "audit_report",
        "error",
        "done",
    }
)

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # AWS access key
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    # GitHub PAT / OAuth / fine-grained tokens
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{36,}\b"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b"), "[REDACTED_GITHUB_TOKEN]"),
    # GitHub runner / recovery codes (ghr_)
    (re.compile(r"\bghr_[A-Za-z0-9_]{10,}\b"), "[REDACTED_GITHUB_TOKEN]"),
    # Neon / Supabase keys (npg_)
    (re.compile(r"\bnpg_[A-Za-z0-9_]{16,}\b"), "[REDACTED_API_KEY]"),
    # OpenAI / Anthropic / general API keys (sk-...)
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "[REDACTED_API_KEY]"),
    # Private keys
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
        "[REDACTED_PRIVATE_KEY]",
    ),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[^\n\r]*"), "[REDACTED_PRIVATE_KEY]"),
    # Database connection strings with embedded passwords (PostgreSQL, MySQL, MongoDB)
    (
        re.compile(r"(postgres(?:ql)?(?:\+[a-z0-9]+)?://[^:/\s]+:)([^@/\s]+)(@)"),
        r"\g<1>[REDACTED_PASSWORD]\g<3>",
    ),
    (
        re.compile(r"(mysql(?:\+[a-z0-9]+)?://[^:/\s]+:)([^@/\s]+)(@)"),
        r"\g<1>[REDACTED_PASSWORD]\g<3>",
    ),
    (
        re.compile(r"(mongodb(?:\+srv)?://[^:/\s]+:)([^@/\s]+)(@)"),
        r"\g<1>[REDACTED_PASSWORD]\g<3>",
    ),
    # Authorization: Bearer headers
    (
        re.compile(r"(?i)\bAuthorization:\s*Bearer\s+[A-Za-z0-9\-._~+/]{16,}=*"),
        "Authorization: Bearer [REDACTED]",
    ),
    # Bearer tokens (min length 16+ chars to prevent over-redacting prose)
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-._~+/]{16,}=*"), "Bearer [REDACTED]"),
    # Query parameter tokens (e.g. ?token=... or &token=...)
    (re.compile(r"(?i)\btoken=[A-Za-z0-9\-._~+/]{16,}=*"), "token=[REDACTED]"),
]

# Backwards compatibility alias
_REDACTION_PATTERNS = _SECRET_PATTERNS


def _redact_string(text: str) -> str:
    """Apply secret redaction patterns to a string."""
    result = text
    for pattern, replacement in _SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def _redact_secrets(obj: Any) -> Any:
    """
    Recursively redact sensitive secrets (AWS keys, GitHub tokens, API keys, passwords)
    from dictionaries, lists, strings, and other data structures before SSE emission.
    """
    if isinstance(obj, str):
        return _redact_string(obj)
    elif isinstance(obj, dict):
        return {k: _redact_secrets(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_redact_secrets(item) for item in obj]
    elif isinstance(obj, tuple):
        return tuple(_redact_secrets(item) for item in obj)
    elif isinstance(obj, set):
        return {_redact_secrets(item) for item in obj}
    return obj


def format_sse_event(
    event: str,
    data: dict[str, Any],
    event_id: int | str | None = None,
    retry: int | None = None,
) -> str:
    """
    Serialise a single SSE event to its wire representation.

    Format:
        event: <event_name>\\n
        data: <json_string>\\n
        \\n

    Args:
        event: One of the allowed event names.
        data:  JSON-serialisable payload dict.

    Returns:
        UTF-8 string ready to be yielded by an async generator.

    Raises:
        ValueError: If event is not in the allowed set.
    """
    if event not in _ALLOWED_EVENTS:
        raise ValueError(
            f"format_sse_event: unknown event {event!r}. "
            f"Allowed: {sorted(_ALLOWED_EVENTS)}"
        )
    sanitized_data = _redact_secrets(data)
    try:
        data_str = json.dumps(sanitized_data, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        logger.error(
            "format_sse_event: failed to serialise data for event=%s: %s", event, exc
        )
        raise

    lines: list[str] = [f"event: {event}"]
    if event_id is not None:
        lines.append(f"id: {event_id}")
    if retry is not None:
        lines.append(f"retry: {retry}")
    lines.append(f"data: {data_str}")
    return "\n".join(lines) + "\n\n"


def _extract_event_id(chunk: str) -> int | None:
    """Extract the integer event ID from a formatted SSE chunk, if present."""
    for line in chunk.splitlines():
        if line.startswith("id: "):
            try:
                return int(line[4:].strip())
            except ValueError:
                return None
    return None


class SseQueue:
    """
    Asyncio-Queue-backed SSE producer/consumer bridge.

    The orchestrator calls `put_*()` helpers to enqueue events.
    The `stream()` async generator is passed directly to StreamingResponse.

    Usage pattern:
        queue = SseQueue()
        # producer side (orchestrator):
        await queue.put_thought("thinking...")
        await queue.put_done(session_id="...", staged_files_count=3)
        # consumer side (endpoint):
        return StreamingResponse(queue.stream(), media_type="text/event-stream", ...)
    """

    def __init__(
        self,
        maxsize: int = 512,
        replay_buffer_size: int = 256,
        retry_ms: int = 3000,
    ) -> None:
        self._maxsize = maxsize
        self._q: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
        self._event_counter = 0
        self._replay_buffer: list[tuple[int, str]] = []
        self._replay_buffer_size = replay_buffer_size
        self._retry_ms = retry_ms
        self._consumer_active = False
        self._consumer_disconnected = False

    # ------------------------------------------------------------------
    # Low-level put
    # ------------------------------------------------------------------

    async def put_event(
        self,
        event: str,
        data: dict[str, Any],
        event_id: int | None = None,
        retry: int | None = None,
    ) -> None:
        """
        Enqueue a formatted SSE string without blocking indefinitely.

        Uses drop-oldest overflow strategy when the queue fills up to prevent
        deadlocking background orchestrator turns on client disconnects.
        """
        if event_id is None:
            self._event_counter += 1
            event_id = self._event_counter

        effective_retry = retry if retry is not None else self._retry_ms
        chunk = format_sse_event(event, data, event_id=event_id, retry=effective_retry)

        # Store in bounded replay buffer for potential client reconnection/resumption
        self._replay_buffer.append((event_id, chunk))
        if len(self._replay_buffer) > self._replay_buffer_size:
            self._replay_buffer.pop(0)

        # The asyncio.Queue carries plain SSE strings (plus the _STREAM_DONE
        # sentinel). Event IDs live in the replay buffer and are re-derived
        # from the wire format on consume, so every queue reader observes str.
        if self._consumer_disconnected and not self._consumer_active:
            # Nobody is reading; the replay buffer already holds the chunk
            # for a later resumption.
            return
        if self._consumer_active:
            # A connected (possibly slow) consumer gets bounded backpressure
            # instead of silently losing events to the overflow policy.
            try:
                await asyncio.wait_for(self._q.put(chunk), timeout=5.0)
                return
            except asyncio.TimeoutError:
                logger.warning(
                    "SseQueue: consumer stalled; falling back to drop-oldest"
                )
        # Non-blocking enqueue with drop-oldest policy to prevent producer deadlock
        try:
            self._q.put_nowait(chunk)
        except asyncio.QueueFull:
            try:
                self._q.get_nowait()
            except (asyncio.QueueEmpty, ValueError):
                pass
            try:
                self._q.put_nowait(chunk)
            except asyncio.QueueFull:
                logger.warning("SseQueue: dropped event %s due to full queue", event)

    async def close(self) -> None:
        """Signal the consumer that the stream is finished."""
        try:
            self._q.put_nowait(_STREAM_DONE)
        except asyncio.QueueFull:
            try:
                self._q.get_nowait()
            except (asyncio.QueueEmpty, ValueError):
                pass
            try:
                self._q.put_nowait(_STREAM_DONE)
            except asyncio.QueueFull:
                pass

    def drain(self) -> None:
        """Drain queued items to prevent dangling references."""
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except (asyncio.QueueEmpty, ValueError):
                break

    @property
    def is_disconnected(self) -> bool:
        """Return whether the HTTP consumer has disconnected."""
        return self._consumer_disconnected

    # ------------------------------------------------------------------
    # Typed helpers (avoid magic strings at call sites)
    # ------------------------------------------------------------------

    async def put_thought(self, delta: str) -> None:
        await self.put_event("thought", {"delta": delta})

    async def put_tool_call(self, tool: str, args: dict[str, Any]) -> None:
        await self.put_event("tool_call", {"tool": tool, "args": args})

    async def put_file_diff(
        self,
        path: str,
        diff: str,
        action: str,
    ) -> None:
        """
        Args:
            action: One of "modify" | "create" | "delete".
        """
        if action not in {"modify", "create", "delete"}:
            raise ValueError(f"put_file_diff: invalid action {action!r}")
        await self.put_event(
            "file_diff", {"path": path, "diff": diff, "action": action}
        )

    async def put_sandbox_status(self, status: str, logs: str = "") -> None:
        """
        Args:
            status: One of "queued" | "running" | "passed" | "failed".
        """
        if status not in {"queued", "running", "passed", "failed"}:
            raise ValueError(f"put_sandbox_status: invalid status {status!r}")
        await self.put_event("sandbox_status", {"status": status, "logs": logs})

    async def put_sandbox_queued(self, run_url: str, workflow_name: str) -> None:
        """
        Emit sandbox_queued when a CI sandbox run is dispatched.

        Args:
            run_url: URL of the GitHub Actions run (or "pending" if not yet known).
            workflow_name: Workflow file or auto-detected workflow label.
        """
        await self.put_event(
            "sandbox_queued", {"run_url": run_url, "workflow_name": workflow_name}
        )

    async def put_sandbox_progress(self, step_name: str, status: str) -> None:
        """
        Emit sandbox_progress for a CI sandbox step transition.

        Args:
            step_name: Human-readable step (e.g. "dispatch", "polling", "complete").
            status: One of "in_progress" | "completed".
        """
        if status not in {"in_progress", "completed"}:
            raise ValueError(f"put_sandbox_progress: invalid status {status!r}")
        await self.put_event(
            "sandbox_progress", {"step_name": step_name, "status": status}
        )

    async def put_terminal_output(self, chunk: str, stream: str = "stdout") -> None:
        """
        Stream a chunk of terminal output (stdout or stderr) to the client.

        Args:
            chunk:  Raw text chunk from the subprocess output.
            stream: One of "stdout" | "stderr" (informational for the client).
        """
        await self.put_event("terminal_output", {"chunk": chunk, "stream": stream})

    async def put_plan_update(self, tasks: list[dict[str, Any]]) -> None:
        await self.put_event("plan_update", {"tasks": tasks})

    async def put_clarification_requested(
        self, question: str, options: list[str]
    ) -> None:
        await self.put_event(
            "clarification_requested", {"question": question, "options": options}
        )

    async def put_checkpoint_created(self, checkpoint: dict[str, Any]) -> None:
        """Emit checkpoint_created event with the new checkpoint metadata."""
        await self.put_event("checkpoint_created", checkpoint)

    async def put_checkpoint_restored(
        self, checkpoint_id: str, staged_patches: dict[str, str]
    ) -> None:
        """Emit checkpoint_restored event to sync Monaco editor buffers."""
        await self.put_event(
            "checkpoint_restored",
            {"checkpoint_id": checkpoint_id, "staged_patches": staged_patches},
        )

    async def put_subagent_start(self, role: str, task: str) -> None:
        """Emit subagent_start event when a subagent begins execution."""
        await self.put_event("subagent_start", {"role": role, "task": task})

    async def put_subagent_done(
        self,
        role: str,
        summary: str,
        patches_modified: list[str],
    ) -> None:
        """Emit subagent_done event when a subagent finishes."""
        await self.put_event(
            "subagent_done",
            {
                "role": role,
                "summary": summary,
                "patches_modified": patches_modified,
            },
        )

    async def put_audit_scan_start(
        self,
        scan_type: str,
        total_files_estimated: int = 0,
        target_path: str = "all",
    ) -> None:
        """Emit audit_scan_start event when a repository or security scan starts."""
        await self.put_event(
            "audit_scan_start",
            {
                "scan_type": scan_type,
                "total_files_estimated": total_files_estimated,
                "target_path": target_path,
            },
        )

    async def put_audit_progress(
        self,
        files_scanned: int,
        current_file: str,
        total_files: int = 0,
    ) -> None:
        """Emit audit_progress event during file inspection."""
        await self.put_event(
            "audit_progress",
            {
                "files_scanned": files_scanned,
                "current_file": current_file,
                "total_files": total_files,
            },
        )

    async def put_audit_report(
        self,
        scan_type: str,
        findings: list[dict[str, Any]],
        health_score: int,
        summary: str,
        audit_id: str = "",
        severity_counts: dict[str, int] | None = None,
        report_markdown: str = "",
    ) -> None:
        """Emit audit_report event with synthesized findings and health score."""
        payload: dict[str, Any] = {
            "scan_type": scan_type,
            "health_score": health_score,
            "summary": summary,
            "findings": findings,
        }
        if audit_id:
            payload["audit_id"] = audit_id
        if severity_counts is not None:
            payload["severity_counts"] = severity_counts
        if report_markdown:
            payload["report_markdown"] = report_markdown
        await self.put_event("audit_report", payload)

    async def put_error(self, error: str, code: str) -> None:
        await self.put_event("error", {"error": error, "code": code})

    async def put_done(
        self, session_id: str, staged_files_count: int, model_used: str = ""
    ) -> None:
        await self.put_event(
            "done",
            {
                "session_id": session_id,
                "staged_files_count": staged_files_count,
                "model_used": model_used,
            },
        )
        await self.close()

    # ------------------------------------------------------------------
    # Async generator for StreamingResponse
    # ------------------------------------------------------------------

    async def stream(
        self, last_event_id: int | None = None
    ) -> AsyncGenerator[str, None]:
        """
        Async generator consumed by FastAPI's StreamingResponse.

        Yields SSE chunks until the sentinel is received or client disconnects.
        If last_event_id is specified, replays buffered events with id > last_event_id first.
        """
        self._consumer_active = True
        self._consumer_disconnected = False
        max_yielded_id = last_event_id if last_event_id is not None else 0
        try:
            # Replay any missed events if client is reconnecting/resuming
            if last_event_id is not None:
                for eid, chunk in list(self._replay_buffer):
                    if eid > last_event_id:
                        yield chunk
                        if eid > max_yielded_id:
                            max_yielded_id = eid

            while True:
                item = await self._q.get()
                if item is _STREAM_DONE:
                    break
                eid = _extract_event_id(item) if isinstance(item, str) else None
                if eid is None or eid > max_yielded_id or max_yielded_id == 0:
                    yield item
                    if eid is not None and eid > max_yielded_id:
                        max_yielded_id = eid
        except (GeneratorExit, asyncio.CancelledError):
            logger.info("SseQueue: consumer disconnected mid-stream")
            raise
        finally:
            self._consumer_active = False
            self._consumer_disconnected = True
            self.drain()
