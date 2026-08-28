"""
Eval regression comparator — Phase 10.

Compares two EvalResult rows by ID, diffs per-subagent scores, and flags
regressions when any metric drops by more than REGRESSION_THRESHOLD (5%).

CLI usage:
    uv run python -m eval.compare <eval_id_a> <eval_id_b>

Python usage:
    from eval.compare import compare_eval
    result = asyncio.run(compare_eval(uuid_a, uuid_b))

Security: reads only from eval_results; no external I/O.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session_maker
from app.models import EvalResult

logger = logging.getLogger(__name__)

# A drop of more than this fraction triggers a regression flag.
REGRESSION_THRESHOLD: float = 0.05


# ---------------------------------------------------------------------------
# Core comparison logic
# ---------------------------------------------------------------------------


def _diff_scores(
    label: str,
    score_a: float | None,
    score_b: float | None,
) -> dict[str, Any]:
    """
    Produce a structured diff for a single metric.

    Returns:
        {label, score_a, score_b, delta, regressed}
    """
    a = score_a if score_a is not None else 0.0
    b = score_b if score_b is not None else 0.0
    delta = round(b - a, 6)
    regressed = delta < -REGRESSION_THRESHOLD

    return {
        "label": label,
        "eval_a": round(a, 6),
        "eval_b": round(b, 6),
        "delta": delta,
        "regressed": regressed,
    }


def _extract_subagent_avg(
    eval_result: EvalResult, subagent: str
) -> float | None:
    """Extract average_score for a given subagent from per_subagent_scores JSONB."""
    scores = eval_result.per_subagent_scores or {}
    sub = scores.get(subagent, {})
    return sub.get("average_score")


def compare_eval_objects(
    eval_a: EvalResult,
    eval_b: EvalResult,
) -> dict[str, Any]:
    """
    Diff two EvalResult objects in-memory.

    Returns a structured comparison report.
    """
    diffs: list[dict[str, Any]] = []

    # 1. Overall accuracy diff
    diffs.append(
        _diff_scores(
            "overall_accuracy",
            eval_a.overall_accuracy,
            eval_b.overall_accuracy,
        )
    )

    # 2. Per-subagent diffs
    for subagent in ("context_gatherer", "fix_generator"):
        diffs.append(
            _diff_scores(
                f"{subagent}.average_score",
                _extract_subagent_avg(eval_a, subagent),
                _extract_subagent_avg(eval_b, subagent),
            )
        )

    # 3. Pass rate diff (overall.pass_rate)
    def _pass_rate(er: EvalResult) -> float | None:
        overall = (er.per_subagent_scores or {}).get("overall", {})
        return overall.get("pass_rate")

    diffs.append(
        _diff_scores(
            "overall.pass_rate",
            _pass_rate(eval_a),
            _pass_rate(eval_b),
        )
    )

    regressions = [d for d in diffs if d["regressed"]]
    any_regression = len(regressions) > 0

    return {
        "eval_a": {
            "id": str(eval_a.id),
            "created_at": eval_a.created_at.isoformat(),
            "overall_accuracy": eval_a.overall_accuracy,
            "mode": (eval_a.per_subagent_scores or {}).get("mode", "UNKNOWN"),
        },
        "eval_b": {
            "id": str(eval_b.id),
            "created_at": eval_b.created_at.isoformat(),
            "overall_accuracy": eval_b.overall_accuracy,
            "mode": (eval_b.per_subagent_scores or {}).get("mode", "UNKNOWN"),
        },
        "diffs": diffs,
        "regression_threshold": REGRESSION_THRESHOLD,
        "regressions": regressions,
        "any_regression": any_regression,
        "verdict": "REGRESSION" if any_regression else "OK",
    }


async def compare_eval(
    eval_id_a: uuid.UUID,
    eval_id_b: uuid.UUID,
) -> dict[str, Any]:
    """
    Load two EvalResult rows and produce a regression diff report.

    Args:
        eval_id_a: ID of the baseline eval.
        eval_id_b: ID of the candidate eval.

    Returns:
        Structured comparison report dict.

    Raises:
        ValueError: If either eval_id is not found in DB.
    """
    async with async_session_maker() as db:
        a_result = await db.execute(select(EvalResult).where(EvalResult.id == eval_id_a))
        eval_a = a_result.scalar_one_or_none()
        if eval_a is None:
            raise ValueError(f"EvalResult {eval_id_a} not found")

        b_result = await db.execute(select(EvalResult).where(EvalResult.id == eval_id_b))
        eval_b = b_result.scalar_one_or_none()
        if eval_b is None:
            raise ValueError(f"EvalResult {eval_id_b} not found")

    report = compare_eval_objects(eval_a, eval_b)

    if report["any_regression"]:
        logger.warning(
            "Regression detected: eval_a=%s eval_b=%s regressions=%s",
            eval_id_a, eval_id_b,
            [r["label"] for r in report["regressions"]],
        )
    else:
        logger.info(
            "No regression: eval_a=%s eval_b=%s verdict=OK", eval_id_a, eval_id_b
        )

    return report


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Compare two Haunter EvalResult rows for regression.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  uv run python -m eval.compare <uuid_a> <uuid_b>\n"
        ),
    )
    p.add_argument("eval_id_a", type=uuid.UUID, help="Baseline EvalResult ID")
    p.add_argument("eval_id_b", type=uuid.UUID, help="Candidate EvalResult ID")
    return p


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = _build_parser()
    args = parser.parse_args()

    report = await compare_eval(args.eval_id_a, args.eval_id_b)
    print(json.dumps(report, indent=2))

    # Exit code 1 if regression detected — useful in CI pipelines
    import sys
    sys.exit(1 if report["any_regression"] else 0)


if __name__ == "__main__":
    asyncio.run(_main())
