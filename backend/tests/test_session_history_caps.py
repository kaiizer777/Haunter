"""Issue #105 — cap conversation_history/staged_patches JSONB growth.

Stored history/patches must stay bounded by constant regardless of attempt
count. These tests are hermetic (no DB, no network).
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from app.services.session_orchestrator import (
    MAX_HISTORY_BYTES,
    MAX_HISTORY_ENTRIES,
    MAX_STAGED_FILES,
    MAX_STAGED_TOTAL_BYTES,
    SessionOrchestrator,
    _check_staged_capacity,
    _compact_history_for_storage,
    _compact_text,
    _history_size_bytes,
    _staged_patches_size_bytes,
)


def _make_attempt_entries(attempt: int, output_size: int = 50_000) -> list[dict[str, Any]]:
    """One simulated turn: user + assistant + large verbatim tool output."""
    big_output = f"[attempt {attempt}]\n" + ("x" * (output_size - 64)) + f"\n[/attempt {attempt}]"
    return [
        {"role": "user", "content": f"fix attempt {attempt}"},
        {
            "role": "assistant",
            "content": f"trying fix {attempt}",
            "tool_calls": [{"id": f"c{attempt}", "function": {"name": "read_file"}}],
        },
        {"role": "tool", "tool_call_id": f"c{attempt}", "content": big_output},
    ]


def test_compact_text_keeps_head_and_tail() -> None:
    text = "HEAD-" + "m" * 20_000 + "-TAIL"
    out = _compact_text(text, 1_000)
    assert len(out) < len(text)
    assert out.startswith("HEAD-")
    assert out.endswith("-TAIL")
    assert "compacted" in out
    # Short text unchanged.
    assert _compact_text("small", 1_000) == "small"
    # Idempotence is now owned by the entry-level `_compacted` flag inside
    # _compact_history_for_storage (see greptile#4296862877 / cubic#4296866375
    # — pattern-matching the literal "[...compacted " in user/tool content
    # would disable the storage cap for any legitimate tool result that
    # happens to contain that string). _compact_text itself does NOT
    # special-case the marker; re-compacting an already-compacted body
    # produces a larger head+tail that preserves head/tail boundaries.


def test_50_attempt_run_history_under_cap() -> None:
    """Acceptance: 50-attempt run stores < cap bytes in conversation_history."""
    history: list[dict[str, Any]] = []
    for attempt in range(50):
        # Mirror the persist path: accumulate then compact each turn.
        history = _compact_history_for_storage(history + _make_attempt_entries(attempt))
    assert len(history) <= MAX_HISTORY_ENTRIES
    assert _history_size_bytes(history) < MAX_HISTORY_BYTES
    # Newest turn survives windowing.
    assert history[-1]["tool_call_id"] == "c49"
    assert "[attempt 49]" in history[-1]["content"]
    # Run-detail UI renders from windowed data: every entry keeps role/content.
    for entry in history:
        assert entry.get("role") in {"user", "assistant", "tool", "system"}
        assert isinstance(entry.get("content"), str)


def test_compact_history_never_mutates_input() -> None:
    entries = _make_attempt_entries(0)
    snapshot = json.dumps(entries)
    _compact_history_for_storage(entries)
    assert json.dumps(entries) == snapshot


def test_staged_capacity_bounds_total_bytes() -> None:
    """Simulated multi-attempt staging stays within the total byte cap."""
    patches: dict[str, str] = {}
    attempt = 0
    while True:
        path = f"file_{attempt}.py"
        diff = f"--- a/{path}\n+++ b/{path}\n" + ("+x\n" * 2_000)
        err = _check_staged_capacity(patches, path, diff)
        if err is not None:
            break
        patches[path] = diff
        attempt += 1
        assert attempt < 10_000  # guard against an unbounded test
    assert _staged_patches_size_bytes(patches) <= MAX_STAGED_TOTAL_BYTES
    assert len(patches) <= MAX_STAGED_FILES
    assert attempt > 0  # at least one patch stageable
    # Replacing an existing path never counts as a new file.
    assert _check_staged_capacity(patches, "file_0.py", patches["file_0.py"]) is None


async def test_tool_stage_patch_rejects_when_session_full() -> None:
    """stage_patch returns an explicit error instead of growing unbounded."""
    orch = SessionOrchestrator(session_id=uuid.uuid4(), db=MagicMock(), gh_token=None)
    orch._llm = AsyncMock()
    patches = {f"f{i}.py": "d" * 10_000 for i in range(MAX_STAGED_FILES)}
    result = await orch._tool_stage_patch(
        args={"path": "overflow.py", "diff": "new-diff", "action": "modify"},
        staged_patches=patches,
        queue=AsyncMock(),
        repo_name=None,
    )
    assert result.startswith("Error")
    assert "overflow.py" not in patches


async def test_persist_compacts_history_before_commit() -> None:
    """_persist (the DB choke point) writes bounded history on every path."""
    mock_db = AsyncMock()
    mock_db.is_active = True
    orch = SessionOrchestrator(session_id=uuid.uuid4(), db=mock_db, gh_token=None)
    orch._llm = AsyncMock()

    session = MagicMock()
    session.id = orch.session_id
    session.status = "active"
    session.checkpoints = []
    session.plan = []
    session.waiting_input = None

    big_history: list[dict[str, Any]] = []
    for attempt in range(50):
        big_history = big_history + _make_attempt_entries(attempt)
    assert _history_size_bytes(big_history) > MAX_HISTORY_BYTES  # precondition: unbounded input

    await orch._persist(session, big_history, {"a.py": "diff"})
    stored = session.conversation_history
    assert _history_size_bytes(stored) < MAX_HISTORY_BYTES
    assert len(stored) <= MAX_HISTORY_ENTRIES
    mock_db.commit.assert_awaited()
