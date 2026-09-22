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

Allowed event names: thought | tool_call | file_diff | sandbox_status | error | done

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
from typing import Any, AsyncGenerator

logger = logging.getLogger(__name__)

# Sentinel object placed on the queue to signal stream end.
_STREAM_DONE = object()

# Allowed SSE event names — enforced at format time to prevent protocol drift.
_ALLOWED_EVENTS: frozenset[str] = frozenset(
    {"thought", "tool_call", "file_diff", "sandbox_status", "error", "done"}
)


def format_sse_event(event: str, data: dict[str, Any]) -> str:
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
    try:
        data_str = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        logger.error("format_sse_event: failed to serialise data for event=%s: %s", event, exc)
        raise

    return f"event: {event}\ndata: {data_str}\n\n"


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

    def __init__(self, maxsize: int = 512) -> None:
        self._q: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)

    # ------------------------------------------------------------------
    # Low-level put
    # ------------------------------------------------------------------

    async def put_event(self, event: str, data: dict[str, Any]) -> None:
        """Enqueue a formatted SSE string. Raises ValueError on unknown event."""
        chunk = format_sse_event(event, data)
        await self._q.put(chunk)

    async def close(self) -> None:
        """Signal the consumer that the stream is finished."""
        await self._q.put(_STREAM_DONE)

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
        await self.put_event("file_diff", {"path": path, "diff": diff, "action": action})

    async def put_sandbox_status(self, status: str, logs: str = "") -> None:
        """
        Args:
            status: One of "queued" | "running" | "passed" | "failed".
        """
        if status not in {"queued", "running", "passed", "failed"}:
            raise ValueError(f"put_sandbox_status: invalid status {status!r}")
        await self.put_event("sandbox_status", {"status": status, "logs": logs})

    async def put_error(self, error: str, code: str) -> None:
        await self.put_event("error", {"error": error, "code": code})

    async def put_done(self, session_id: str, staged_files_count: int) -> None:
        await self.put_event("done", {"session_id": session_id, "staged_files_count": staged_files_count})
        await self.close()

    # ------------------------------------------------------------------
    # Async generator for StreamingResponse
    # ------------------------------------------------------------------

    async def stream(self) -> AsyncGenerator[str, None]:
        """
        Async generator consumed by FastAPI's StreamingResponse.

        Yields SSE chunks until the sentinel is received.
        """
        while True:
            item = await self._q.get()
            if item is _STREAM_DONE:
                break
            yield item
