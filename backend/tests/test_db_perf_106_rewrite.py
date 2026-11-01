"""
Issue #106 — JSONB whole-row rewrites + token accounting mismatch.

Hermetic (no DB): token accounting and run-step SQL behaviour.
Auto-marked ``fast`` by conftest (no db/slow/eval marker).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.services import token_accounting as ta


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
        )

    in_tokens, out_tokens = ta.normalize_usage(response.get("usage"))
    assert in_tokens == 11
    assert out_tokens == 9
