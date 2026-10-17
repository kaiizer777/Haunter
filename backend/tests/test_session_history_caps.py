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


# ------------------------------------------------------------------
# Issue #105 follow-up coverage — Commit 4 (cubic#4296866350, cubic#4296866356,
# cubic#4296866384).
# ------------------------------------------------------------------


def test_window_truncates_to_newest_200_when_exceeded() -> None:
    """MAX_HISTORY_ENTRIES sliding window actually truncates past 200.

    cubic#4296866384 (P3): the prior 50-attempt run produced 150 entries,
    so the 200-entry window was never exercised. Run 75 attempts × 3
    entries (225 total) and assert the oldest turns are dropped while
    the newest survive in order — bounded by MAX_HISTORY_ENTRIES.
    """
    history: list[dict[str, Any]] = []
    for attempt in range(75):
        history = _compact_history_for_storage(
            history + _make_attempt_entries(attempt, output_size=500)
        )
    # Window trimmed past MAX_HISTORY_ENTRIES (turn-aligned: 198 = 66 turns).
    assert len(history) <= MAX_HISTORY_ENTRIES
    # The newest attempt (74) survived intact.
    assert history[-1]["tool_call_id"] == "c74"
    assert any(e.get("content") == "fix attempt 74" for e in history)
    # The very first attempt (0) and earliest survivors (turns 0-8) were dropped.
    assert all(
        e.get("content") != "fix attempt 0" and e.get("tool_call_id") != "c0"
        for e in history
    )
    assert all(
        e.get("content") != "fix attempt 8" and e.get("tool_call_id") != "c8"
        for e in history
    )
    # The first surviving turn is attempt 9 (entry 27 onwards).
    assert history[0].get("role") == "user"
    assert history[0].get("content") == "fix attempt 9"


def test_staged_capacity_rejects_when_total_bytes_exceeded() -> None:
    """MAX_STAGED_TOTAL_BYTES rejection branch is exercised.

    cubic#4296866356 (P2): the prior loop used 6 KB diffs, so the file cap
    tripped at roughly 600 KB and the byte-cap branch was never run. Stage
    a few large diffs (well under MAX_STAGED_FILES but each close to 1 MB)
    and assert the next upsert is rejected with the explicit byte-cap error.
    """
    # Each diff is ~1.4 MB of body content (well under MAX_STAGED_FILES but
    # large enough that two of them exceed MAX_STAGED_TOTAL_BYTES once
    # JSON-escaped). The candidate-dict serialization in _check_staged_capacity
    # adds ~50 bytes of key/quoting overhead per entry.
    big_diff = "+x\n" * 350_000  # ~1.4 MB of body
    big_diff_path = "big.py"
    assert _check_staged_capacity({}, big_diff_path, big_diff) is None

    patches: dict[str, str] = {big_diff_path: big_diff}
    # Adding a second equally-large diff must trip MAX_STAGED_TOTAL_BYTES.
    overflow_err = _check_staged_capacity(patches, "second_big.py", big_diff)
    assert overflow_err is not None
    assert "exceed" in overflow_err.lower()
    assert "KB" in overflow_err or "MB" in overflow_err
    # The original patch is preserved (rejection is non-destructive).
    assert big_diff_path in patches


async def test_persist_fallback_path_compacts_history() -> None:
    """Fallback persist (after self.db.commit raises) still writes compacted history.

    cubic#4296866350 (P2): _persist has a fallback path through rollback()
    + a second async_session_maker() session. The prior test only covered
    the successful commit. Force self.db.commit() to raise, capture the
    row assigned on the fallback session, and assert it received the
    compacted body — not the unbounded input.
    """
    from contextlib import asynccontextmanager

    # Patch the fallback session factory so _persist's `async with
    # async_session_maker() as fallback_db` resolves to a MagicMock we can
    # inspect. The patch is scoped to this test only via monkeypatch.
    import app.db as app_db_mod

    captured: dict[str, Any] = {}

    fallback_row = MagicMock()
    fallback_row.id = uuid.uuid4()
    fallback_row.conversation_history = None
    fallback_row.staged_patches = None

    fallback_session = MagicMock()
    fallback_session.execute = AsyncMock(
        return_value=MagicMock(
            scalars=MagicMock(
                return_value=MagicMock(first=MagicMock(return_value=fallback_row))
            )
        )
    )

    async def _fallback_commit():
        captured["fallback_committed"] = True

    fallback_session.commit = _fallback_commit

    @asynccontextmanager
    async def _fake_session_maker():
        yield fallback_session

    original_factory = app_db_mod.async_session_maker
    app_db_mod.async_session_maker = _fake_session_maker
    try:
        mock_db = AsyncMock()
        mock_db.is_active = True
        # Force the primary commit to raise so the fallback path runs.
        mock_db.commit = AsyncMock(side_effect=RuntimeError("primary commit failed"))
        mock_db.rollback = AsyncMock()
        mock_db.refresh = AsyncMock()

        orch = SessionOrchestrator(session_id=uuid.uuid4(), db=mock_db, gh_token=None)
        orch._llm = AsyncMock()

        session = MagicMock()
        session.id = orch.session_id
        session.status = "active"
        session.checkpoints = []
        session.plan = []
        session.waiting_input = None

        # Build a history that exceeds MAX_HISTORY_BYTES.
        big_history: list[dict[str, Any]] = []
        for attempt in range(80):
            big_history = big_history + _make_attempt_entries(attempt)
        assert _history_size_bytes(big_history) > MAX_HISTORY_BYTES

        await orch._persist(session, big_history, {"a.py": "diff"})

        # The primary commit raised (we asserted that via mock_db.commit).
        mock_db.commit.assert_awaited()
        # Rollback ran before the fallback.
        mock_db.rollback.assert_awaited()
        # Fallback session wrote the compacted history.
        assert captured.get("fallback_committed") is True
        stored_fallback = fallback_row.conversation_history
        assert stored_fallback is not None
        assert _history_size_bytes(stored_fallback) < MAX_HISTORY_BYTES
        assert len(stored_fallback) <= MAX_HISTORY_ENTRIES
        # The fallback body must start with a user message (no orphan
        # tool results from turn-anchored eviction).
        assert stored_fallback[0].get("role") == "user"
    finally:
        app_db_mod.async_session_maker = original_factory
