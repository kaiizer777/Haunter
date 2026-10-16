"""
Issue #106 — JSONB whole-row rewrites + token accounting mismatch.

Hermetic (no DB): statement builders are asserted via PostgreSQL-dialect
compilation, and the orchestrator fast path is asserted via an AsyncMock
session spy. Auto-marked ``fast`` by conftest (no db/slow/eval marker).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql

from app.services import token_accounting as ta


def _pg_sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


# ---------------------------------------------------------------------------
# Canonical pricing — one formula, one place
# ---------------------------------------------------------------------------


def test_estimate_cost_matches_legacy_formula() -> None:
    assert ta.estimate_cost(120, 80) == pytest.approx(
        round(120 * 0.001 / 1_000 + 80 * 0.002 / 1_000, 8)
    )
    assert ta.estimate_cost(0, 0) == 0.0
    assert ta.COST_PER_INPUT_TOKEN == 0.001 / 1_000
    assert ta.COST_PER_OUTPUT_TOKEN == 0.002 / 1_000


def test_run_step_round_trip_matches_recomputation() -> None:
    """Stored columns must equal recomputation — the dashboard invariant."""
    step = ta.make_run_step(
        run_id=uuid.uuid4(), step_name="probe", input_tokens=200, output_tokens=150
    )
    assert ta.cost_matches_stored(
        step.input_tokens, step.output_tokens, step.cost_estimate
    )
    # Coercion keeps rows self-consistent for messy provider payloads.
    messy = ta.make_run_step(
        run_id=uuid.uuid4(),
        step_name="probe",
        usage={"prompt_tokens": "200", "completion_tokens": None},
        latency_ms="12",
    )
    assert (messy.input_tokens, messy.output_tokens) == (200, 0)
    assert ta.cost_matches_stored(
        messy.input_tokens, messy.output_tokens, messy.cost_estimate
    )
    assert not ta.cost_matches_stored(200, 150, 999.0)


def test_pricing_has_single_source_of_truth() -> None:
    """No subagent may carry its own COST_PER_* copy (the #106 drift)."""
    import app.subagents.context_gatherer as cg
    import app.subagents.fix_generator as fg
    import app.subagents.pr_writer as pw

    for module in (cg, fg, pw):
        assert "COST_PER_INPUT_TOKEN" not in vars(module), module.__name__
        assert "COST_PER_OUTPUT_TOKEN" not in vars(module), module.__name__


# ---------------------------------------------------------------------------
# Usage normalization
# ---------------------------------------------------------------------------


def test_normalize_usage_spellings_and_mess() -> None:
    assert ta.normalize_usage({"input_tokens": 10, "output_tokens": 5}) == (10, 5)
    # Wire-level spelling fallback.
    assert ta.normalize_usage({"prompt_tokens": 10, "completion_tokens": 5}) == (10, 5)
    # Canonical spelling wins when both present.
    assert ta.normalize_usage(
        {
            "input_tokens": 7,
            "prompt_tokens": 70,
            "output_tokens": 3,
            "completion_tokens": 30,
        }
    ) == (7, 3)
    # Messy payloads coerce instead of raising (old code: int(None) → TypeError).
    assert ta.normalize_usage({"input_tokens": None, "output_tokens": "42"}) == (0, 42)
    assert ta.normalize_usage(None) == (0, 0)
    assert ta.normalize_usage("nope") == (0, 0)  # type: ignore[arg-type]
    assert ta.normalize_usage({"input_tokens": -5, "output_tokens": True}) == (0, 0)


# ---------------------------------------------------------------------------
# Pure delta helpers
# ---------------------------------------------------------------------------


def test_history_suffix_detects_appends_and_rewrites() -> None:
    base = [{"role": "user", "content": "a"}]
    new_entries = [{"role": "assistant", "content": "b"}]
    assert ta.history_suffix(base, base + new_entries) == new_entries
    assert ta.history_suffix(base, list(base)) == []
    # Reorder / edit / truncate are NOT appends → full-write fallback.
    assert ta.history_suffix(base, [{"role": "user", "content": "CHANGED"}]) is None
    assert ta.history_suffix(base + new_entries, base) is None


def test_patch_delta_splits_upserts_and_removals() -> None:
    before = {"a.py": "diff-a", "b.py": "diff-b"}
    after = {"a.py": "diff-a", "b.py": "diff-b2", "c.py": "diff-c"}
    upserts, removed = ta.patch_delta(before, after)
    assert upserts == {"b.py": "diff-b2", "c.py": "diff-c"}
    assert removed == []
    upserts2, removed2 = ta.patch_delta(after, before)
    assert upserts2 == {"b.py": "diff-b"}
    assert removed2 == ["c.py"]
    assert ta.patch_delta(before, dict(before)) == ({}, [])


# ---------------------------------------------------------------------------
# Partial-update statements emit server-side deltas, not full documents
# ---------------------------------------------------------------------------


def test_statements_emit_partial_operators() -> None:
    sid = uuid.uuid4()
    assert ta.append_history_stmt(sid, []) is None
    assert ta.merge_patches_stmt(sid, {}) is None
    assert ta.delete_patch_keys_stmt(sid, []) is None

    history_sql = _pg_sql(ta.append_history_stmt(sid, [{"role": "user"}]))
    assert "||" in history_sql
    assert "conversation_history" in history_sql

    set_sql = _pg_sql(ta.set_patch_key_stmt(sid, "a.py", "diff"))
    assert "jsonb_set" in set_sql
    assert "to_jsonb" in set_sql

    merge_sql = _pg_sql(ta.merge_patches_stmt(sid, {"a.py": "diff"}))
    assert "||" in merge_sql
    assert "jsonb_set" not in merge_sql

    del_sql = _pg_sql(ta.delete_patch_keys_stmt(sid, ["gone.py"]))
    assert " - " in del_sql


# ---------------------------------------------------------------------------
# Orchestrator fast path: spy on the session, not the database
# ---------------------------------------------------------------------------


def _make_orchestrator(db: Any):
    from app.services.session_orchestrator import SessionOrchestrator

    return SessionOrchestrator(session_id=uuid.uuid4(), db=db)


def _namespace_session() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        status="active",
        conversation_history=[{"role": "user", "content": "hi"}],
        staged_patches={},
        checkpoints=[],
        plan=[],
        waiting_input=None,
        active_turn_id=None,
    )


async def test_persist_emits_partial_writes_for_append() -> None:
    """Small turn → ``||`` UPDATE only; no full-document rebind."""
    db = AsyncMock()
    orch = _make_orchestrator(db)
    session = _namespace_session()
    base = list(session.conversation_history)
    new_entries = [{"role": "assistant", "content": "done"}]

    await orch._persist(
        session,
        base + new_entries,
        {},
        base_history=base,
        new_entries=new_entries,
        patch_upserts={},
        patch_removed=[],
    )

    assert db.execute.await_count == 1  # history append only
    sql = _pg_sql(db.execute.await_args_list[0].args[0])
    assert "||" in sql
    assert "jsonb_set" not in sql
    db.commit.assert_awaited_once()


async def test_persist_single_patch_key_uses_jsonb_set() -> None:
    db = AsyncMock()
    orch = _make_orchestrator(db)
    session = _namespace_session()
    base = list(session.conversation_history)
    new_entries = [{"role": "assistant", "content": "staged"}]

    await orch._persist(
        session,
        base + new_entries,
        {"a.py": "diff"},
        base_history=base,
        new_entries=new_entries,
        patch_upserts={"a.py": "diff"},
        patch_removed=[],
    )

    assert db.execute.await_count == 2
    compiled = [_pg_sql(call.args[0]) for call in db.execute.await_args_list]
    assert any("jsonb_set" in sql for sql in compiled)
    assert any("||" in sql for sql in compiled)


async def test_persist_falls_back_to_full_write_on_rewrite() -> None:
    """Checkpoint-restore-style rewrite → legacy full write, no partial UPDATE."""
    db = AsyncMock()
    orch = _make_orchestrator(db)
    session = _namespace_session()
    rewritten = [{"role": "user", "content": "totally different"}]

    await orch._persist(
        session,
        rewritten,
        {},
        base_history=list(session.conversation_history),
        new_entries=rewritten,
        patch_upserts={},
        patch_removed=[],
    )

    db.execute.assert_not_awaited()
    db.commit.assert_awaited_once()


async def test_persist_without_deltas_takes_legacy_path() -> None:
    """Callers without delta args (slash commands, error fallbacks) are unchanged."""
    db = AsyncMock()
    orch = _make_orchestrator(db)
    session = _namespace_session()

    await orch._persist(session, list(session.conversation_history), {})

    db.execute.assert_not_awaited()
    db.commit.assert_awaited_once()


def test_plan_delta_statements_selects_operators() -> None:
    """Pure planner: single key → jsonb_set; multi-key → merge; removals → `-`."""
    from app.services.session_orchestrator import SessionOrchestrator

    sid = uuid.uuid4()
    session = SimpleNamespace(id=sid)
    base = [{"role": "user", "content": "hi"}]
    new_entries = [{"role": "assistant", "content": "ok"}]

    assert (
        SessionOrchestrator._plan_delta_statements(
            session,
            base_history=None,
            updated_history=base + new_entries,
            new_entries=new_entries,
            patch_upserts={},
            patch_removed=[],
        )
        == []
    )
    assert (
        SessionOrchestrator._plan_delta_statements(
            session,
            base_history=base,
            updated_history=[{"role": "user", "content": "rewritten"}],
            new_entries=new_entries,
            patch_upserts={},
            patch_removed=[],
        )
        == []
    )

    multi = SessionOrchestrator._plan_delta_statements(
        session,
        base_history=base,
        updated_history=base + new_entries,
        new_entries=new_entries,
        patch_upserts={"a.py": "1", "b.py": "2"},
        patch_removed=[],
    )
    multi_sql = [_pg_sql(stmt) for stmt in multi]
    assert any("||" in sql for sql in multi_sql)
    assert not any("jsonb_set" in sql for sql in multi_sql)

    with_removal = SessionOrchestrator._plan_delta_statements(
        session,
        base_history=base,
        updated_history=base + new_entries,
        new_entries=new_entries,
        patch_upserts={"a.py": "1"},
        patch_removed=["gone.py"],
    )
    removal_sql = [_pg_sql(stmt) for stmt in with_removal]
    assert not any("jsonb_set" in sql for sql in removal_sql)
    assert any(" - " in sql for sql in removal_sql)
