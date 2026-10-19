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
    # PostgreSQL parses ``array(...)`` as a function call (not a 1-element
    # array literal) and rejects ``jsonb_set(col, array(...), ...)``. The
    # path argument must render as the ``ARRAY[...]`` constructor.
    assert "ARRAY[" in set_sql
    assert " array(" not in set_sql.lower().replace("\n", " ")

    merge_sql = _pg_sql(ta.merge_patches_stmt(sid, {"a.py": "diff"}))
    assert "||" in merge_sql
    assert "jsonb_set" not in merge_sql

    del_sql = _pg_sql(ta.delete_patch_keys_stmt(sid, ["gone.py"]))
    assert " - " in del_sql


def test_coerce_tokens_catches_overflow() -> None:
    """``int(float('inf'))`` raises OverflowError; it must fall through to 0."""
    assert ta._coerce_tokens(float("inf")) == 0
    assert ta._coerce_tokens(-float("inf")) == 0
    # Sanity: a real provider value still round-trips.
    assert ta._coerce_tokens(1234) == 1234
    # And nothing else broke:
    assert ta._coerce_tokens(None) == 0
    assert ta._coerce_tokens("oops") == 0


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


async def test_persist_after_restore_discards_emit_removal() -> None:
    """Checkpoint_restore flushed ``a.py``; a later discard must remove it.

    Reproduces greptile#4297093156 / cubic#4297090237: the bug was that
    ``patches_before`` stayed at the pre-turn snapshot, so a discard
    back to the pre-turn value computed an empty delta and silently kept
    the restored patch saved. The fix resets ``patches_before`` to the
    post-restore snapshot; here we feed the post-reset delta directly
    to ``_persist`` and assert the removal UPDATE is emitted.
    """
    db = AsyncMock()
    orch = _make_orchestrator(db)
    session = _namespace_session()
    base = list(session.conversation_history)

    # End-of-turn state after: checkpoint_restore() flushed {a.py},
    # then discard_patch() removed it back to {}.
    await orch._persist(
        session,
        base,
        {},
        base_history=base,
        new_entries=[],
        patch_upserts={},
        patch_removed=["a.py"],
    )

    assert db.execute.await_count == 1
    sql = _pg_sql(db.execute.await_args_list[0].args[0])
    # Single-key removal uses ``col - 'k'`` (no ARRAY literal needed).
    assert " - " in sql
    assert "ARRAY[" not in sql  # single-key fast path, not the multi-key array op
    assert "staged_patches" in sql


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


# ---------------------------------------------------------------------------
# Canonical-usage retry normalization (cubic#4297090208, cubic#4297090217,
# cubic#4297090228, cubic#4297090249)
# ---------------------------------------------------------------------------


async def test_fix_generator_format_retry_accumulates_usage() -> None:
    """First-call tokens must not be dropped on a format retry (cubic#4297090208).

    Previously the retry path overwrote ``response`` with the second call's
    dict, so only the last call's usage was persisted to RunStep. After the
    fix the two calls' usages are normalized and summed.
    """
    from unittest.mock import patch as _patch

    from app.subagents import fix_generator as fg

    valid_format_json = (
        '{"patch":"--- a/x\\n+++ b/x\\n@@ -1 +1 @@\\n-old\\n+new",'
        '"confidence":80,"strategy_notes":""}'
    )
    invalid_format_json = (
        '{"patch":"plain text no diff markers",'
        '"confidence":80,"strategy_notes":""}'
    )
    valid_fix = SimpleNamespace(patch="--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new", confidence=80, strategy_notes="")
    rejected_fix = SimpleNamespace(patch="plain text no diff markers", confidence=80, strategy_notes="")

    call_log: list[dict[str, Any]] = []

    async def fake_call_and_parse(**_kwargs: Any):
        if not call_log:
            call_log.append({"usage": {"input_tokens": 100, "output_tokens": 50}})
            return rejected_fix, {"usage": {"input_tokens": 100, "output_tokens": 50}}
        call_log.append({"usage": {"input_tokens": 30, "output_tokens": 20}})
        return valid_fix, {"usage": {"input_tokens": 30, "output_tokens": 20}}

    db = AsyncMock()
    with _patch.object(fg, "_call_and_parse", side_effect=fake_call_and_parse):
        _, response = await fg._call_with_format_retry(
            messages=[],
            diagnosis_summary="d",
            prior_attempt=None,
            db=db,
            run_id=uuid.uuid4(),
            repo_id=None,
            review_feedback=None,
        )

    in_tokens, out_tokens = ta.normalize_usage(response.get("usage"))
    assert in_tokens == 130
    assert out_tokens == 70


async def test_fix_generator_retry_normalizes_messy_usage() -> None:
    """Retry-path ``+`` on raw dict.get crashed on ``None`` (cubic#4297090249)."""
    from unittest.mock import patch as _patch

    from app.subagents import fix_generator as fg

    valid_fix_json = (
        '{"patch":"--- a/x\\n+++ b/x\\n@@ -1 +1 @@\\n-old\\n+new",'
        '"confidence":80,"strategy_notes":""}'
    )

    async def fake_llm_complete(**_kwargs: Any):
        if not getattr(fake_llm_complete, "_count", None):
            fake_llm_complete._count = 1
            return {
                "content": "garbage no diff markers",
                "usage": {"input_tokens": None, "output_tokens": 5},
            }
        return {
            "content": valid_fix_json,
            "usage": {"input_tokens": "7", "output_tokens": "3"},
        }

    db = AsyncMock()
    db_path = "app.subagents.fix_generator.LLMClient"  # not used directly
    with _patch.object(fg, "LLMClient") as _client_cls:
        _client_cls.return_value.complete = AsyncMock(side_effect=fake_llm_complete)
        _, response = await fg._call_and_parse(
            messages=[],
            diagnosis_summary="d",
            prior_attempt=None,
            db=db,
            run_id=uuid.uuid4(),
            repo_id=None,
        )

    in_tokens, out_tokens = ta.normalize_usage(response.get("usage"))
    # None -> 0; "7" -> 7; "3" -> 3. The first call's bad patch was discarded
    # but its usage is still summed in (via the retry-path normalize).
    assert in_tokens == 7
    assert out_tokens == 8


async def test_pr_writer_retry_normalizes_messy_usage() -> None:
    """pr_writer retry ``+`` must be replaced with normalized sum (cubic#4297090217)."""
    from unittest.mock import patch as _patch

    from app.subagents import pr_writer as pw

    valid_pr_json = (
        '{"title":"valid title here",'
        '"body":"' + ("a" * 30) + '"}'
    )

    async def fake_llm_complete(**_kwargs: Any):
        if not getattr(fake_llm_complete, "_count", None):
            fake_llm_complete._count = 1
            return {
                "content": "not json",
                "usage": {"input_tokens": None, "output_tokens": 5},
            }
        return {
            "content": valid_pr_json,
            "usage": {"input_tokens": "11", "output_tokens": "4"},
        }

    db = AsyncMock()
    with _patch.object(pw, "LLMClient") as _client_cls:
        _client_cls.return_value.complete = AsyncMock(side_effect=fake_llm_complete)
        _, response = await pw._call_and_parse_pr(
            diagnosis_summary="d",
            patch_text="diff",
            db=db,
            run_id=uuid.uuid4(),
            repo_id=None,
        )

    in_tokens, out_tokens = ta.normalize_usage(response.get("usage"))
    assert in_tokens == 11
    assert out_tokens == 9
