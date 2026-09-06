"""
Pure unit tests for eval comparator (backend/eval/compare.py).

Covers:
- _diff_scores:
  - equal scores -> regressed=False, delta=0.
  - score B > A by 1pp -> regressed=False (under 5%).
  - score B < A by 6pp -> regressed=True (delta < -0.05).
  - None scores -> coerced to 0 with regressed evaluated on resulting delta.
- compare_eval_objects:
  - with two EvalResult instances where eval_b.overall_accuracy dropped 10pp:
    any_regression=True, verdict="REGRESSION".
  - with no drop: any_regression=False, verdict="OK".
  - per-subagent context_gatherer.average_score and fix_generator.average_score diffed.
  - pass_rate diffed under overall.pass_rate label.
- compare_eval(uuid, uuid):
  - missing eval_id -> raises ValueError.
  - both found -> returns structured in-memory report.
- CLI parser verification (_build_parser).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import EvalResult
from eval.compare import (
    REGRESSION_THRESHOLD,
    _build_parser,
    _diff_scores,
    _extract_subagent_avg,
    compare_eval,
    compare_eval_objects,
)


# ---------------------------------------------------------------------------
# _diff_scores unit tests
# ---------------------------------------------------------------------------


def test_diff_scores_equal():
    """Equal scores produce delta=0 and regressed=False."""
    result = _diff_scores("overall_accuracy", 0.85, 0.85)
    assert result["label"] == "overall_accuracy"
    assert result["eval_a"] == 0.85
    assert result["eval_b"] == 0.85
    assert result["delta"] == 0.0
    assert result["regressed"] is False


def test_diff_scores_b_greater_than_a_by_1pp():
    """Score B higher than A by 1pp (0.01) is not regressed (under 5% threshold)."""
    result = _diff_scores("overall_accuracy", 0.80, 0.81)
    assert result["delta"] == 0.01
    assert result["regressed"] is False


def test_diff_scores_b_less_than_a_by_6pp():
    """Score B lower than A by 6pp (-0.06) triggers regression flag (delta < -0.05)."""
    result = _diff_scores("overall_accuracy", 0.80, 0.74)
    assert result["delta"] == -0.06
    assert result["regressed"] is True


def test_diff_scores_none_scores_coerced_to_zero():
    """None scores are coerced to 0.0 and regressed evaluated on resulting delta."""
    # None -> 0.80: delta = +0.80 (not regressed)
    r1 = _diff_scores("metric", None, 0.80)
    assert r1["eval_a"] == 0.0
    assert r1["eval_b"] == 0.80
    assert r1["delta"] == 0.80
    assert r1["regressed"] is False

    # 0.80 -> None: delta = -0.80 (regressed)
    r2 = _diff_scores("metric", 0.80, None)
    assert r2["eval_a"] == 0.80
    assert r2["eval_b"] == 0.0
    assert r2["delta"] == -0.80
    assert r2["regressed"] is True

    # None -> None: delta = 0.0 (not regressed)
    r3 = _diff_scores("metric", None, None)
    assert r3["eval_a"] == 0.0
    assert r3["eval_b"] == 0.0
    assert r3["delta"] == 0.0
    assert r3["regressed"] is False


def test_extract_subagent_avg():
    """_extract_subagent_avg extracts average_score from per_subagent_scores or returns None."""
    er_empty = EvalResult(per_subagent_scores=None)
    assert _extract_subagent_avg(er_empty, "context_gatherer") is None

    er_populated = EvalResult(
        per_subagent_scores={
            "context_gatherer": {"average_score": 0.92},
            "fix_generator": {"average_score": 0.88},
        }
    )
    assert _extract_subagent_avg(er_populated, "context_gatherer") == 0.92
    assert _extract_subagent_avg(er_populated, "fix_generator") == 0.88
    assert _extract_subagent_avg(er_populated, "nonexistent") is None


# ---------------------------------------------------------------------------
# compare_eval_objects unit tests
# ---------------------------------------------------------------------------


def _make_eval_result(
    overall_accuracy: float | None = 0.85,
    cg_score: float | None = 0.85,
    fg_score: float | None = 0.85,
    pass_rate: float | None = 0.90,
    mode: str = "DRY-RUN",
) -> EvalResult:
    return EvalResult(
        id=uuid.uuid4(),
        overall_accuracy=overall_accuracy,
        created_at=datetime.now(timezone.utc),
        per_subagent_scores={
            "context_gatherer": {"average_score": cg_score},
            "fix_generator": {"average_score": fg_score},
            "overall": {"pass_rate": pass_rate},
            "mode": mode,
        },
    )


def test_compare_eval_objects_regression_detected():
    """When eval_b.overall_accuracy drops 10pp, verdict is REGRESSION and any_regression=True."""
    eval_a = _make_eval_result(overall_accuracy=0.85)
    eval_b = _make_eval_result(overall_accuracy=0.75)  # 10pp drop

    report = compare_eval_objects(eval_a, eval_b)

    assert report["any_regression"] is True
    assert report["verdict"] == "REGRESSION"
    assert len(report["regressions"]) >= 1

    labels = [r["label"] for r in report["regressions"]]
    assert "overall_accuracy" in labels


def test_compare_eval_objects_no_regression_ok():
    """When scores are equal or improve, verdict is OK and any_regression=False."""
    eval_a = _make_eval_result(overall_accuracy=0.80, cg_score=0.80, fg_score=0.80, pass_rate=0.80)
    eval_b = _make_eval_result(overall_accuracy=0.82, cg_score=0.85, fg_score=0.81, pass_rate=0.85)

    report = compare_eval_objects(eval_a, eval_b)

    assert report["any_regression"] is False
    assert report["verdict"] == "OK"
    assert len(report["regressions"]) == 0


def test_compare_eval_objects_diffs_all_required_metrics():
    """
    Assert that diffs includes overall_accuracy, context_gatherer.average_score,
    fix_generator.average_score, and overall.pass_rate.
    """
    eval_a = _make_eval_result(
        overall_accuracy=0.90, cg_score=0.85, fg_score=0.80, pass_rate=0.95
    )
    eval_b = _make_eval_result(
        overall_accuracy=0.90, cg_score=0.85, fg_score=0.80, pass_rate=0.95
    )

    report = compare_eval_objects(eval_a, eval_b)
    diff_labels = [d["label"] for d in report["diffs"]]

    assert "overall_accuracy" in diff_labels
    assert "context_gatherer.average_score" in diff_labels
    assert "fix_generator.average_score" in diff_labels
    assert "overall.pass_rate" in diff_labels


# ---------------------------------------------------------------------------
# compare_eval(uuid, uuid) async unit tests (mocked session, no DB)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compare_eval_missing_eval_a_raises_value_error():
    """compare_eval raises ValueError when eval_id_a is not found."""
    id_a = uuid.uuid4()
    id_b = uuid.uuid4()

    mock_db = AsyncMock()
    mock_db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

    mock_session_maker = MagicMock()
    mock_session_maker.return_value.__aenter__.return_value = mock_db

    with patch("eval.compare.async_session_maker", mock_session_maker):
        with pytest.raises(ValueError, match=f"EvalResult {id_a} not found"):
            await compare_eval(id_a, id_b)


@pytest.mark.asyncio
async def test_compare_eval_missing_eval_b_raises_value_error():
    """compare_eval raises ValueError when eval_id_b is not found."""
    id_a = uuid.uuid4()
    id_b = uuid.uuid4()

    eval_a = _make_eval_result()
    eval_a.id = id_a

    # First call finds eval_a, second call returns None for eval_b
    res_a = MagicMock(scalar_one_or_none=MagicMock(return_value=eval_a))
    res_b = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

    mock_db = AsyncMock()
    mock_db.execute.side_effect = [res_a, res_b]

    mock_session_maker = MagicMock()
    mock_session_maker.return_value.__aenter__.return_value = mock_db

    with patch("eval.compare.async_session_maker", mock_session_maker):
        with pytest.raises(ValueError, match=f"EvalResult {id_b} not found"):
            await compare_eval(id_a, id_b)


@pytest.mark.asyncio
async def test_compare_eval_both_found_returns_report():
    """compare_eval returns report dict when both eval IDs exist."""
    id_a = uuid.uuid4()
    id_b = uuid.uuid4()

    eval_a = _make_eval_result(overall_accuracy=0.85)
    eval_a.id = id_a
    eval_b = _make_eval_result(overall_accuracy=0.85)
    eval_b.id = id_b

    res_a = MagicMock(scalar_one_or_none=MagicMock(return_value=eval_a))
    res_b = MagicMock(scalar_one_or_none=MagicMock(return_value=eval_b))

    mock_db = AsyncMock()
    mock_db.execute.side_effect = [res_a, res_b]

    mock_session_maker = MagicMock()
    mock_session_maker.return_value.__aenter__.return_value = mock_db

    with patch("eval.compare.async_session_maker", mock_session_maker):
        report = await compare_eval(id_a, id_b)

    assert isinstance(report, dict)
    assert report["verdict"] == "OK"
    assert report["eval_a"]["id"] == str(id_a)
    assert report["eval_b"]["id"] == str(id_b)


def test_build_parser():
    """_build_parser parses eval_id_a and eval_id_b positional UUIDs."""
    parser = _build_parser()
    id_a = str(uuid.uuid4())
    id_b = str(uuid.uuid4())
    args = parser.parse_args([id_a, id_b])
    assert args.eval_id_a == uuid.UUID(id_a)
    assert args.eval_id_b == uuid.UUID(id_b)
