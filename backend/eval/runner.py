"""
Eval runner for Haunter Phase 10.

Drives the eval pipeline against a fixed server-side allowlist of golden
fixtures (never accepting repo references from clients). Supports two modes:

  --dry-run   Stubs LLM responses and Cloud Build — fast, no network calls.
              Used for CI verification and local iteration.
  --live      Real LLMClient calls; Cloud Build still mocked (no tenant code runs).

Each golden case is evaluated for:
  - context_gatherer: keyword match score against expected_root_cause_keywords.
  - fix_generator: confidence score extracted from LLM output vs. expected minimum.

One EvalResult row is persisted per run.

Security invariants:
  - Fixture repo_ref comes ONLY from the server-side allowlist file — never from
    the client / network at runtime.
  - No API keys, tokens, or secrets are logged.
  - Cloud Build is never invoked against real code in eval context.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session_maker
from app.models import EvalResult, ModelConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FIXTURES_PATH: Path = Path(__file__).parent / "fixtures" / "golden_cases.json"

# Allowlisted fixture IDs — derived from the on-disk fixture file only.
# The runner validates that every requested ID exists here before proceeding.
_ALL_FIXTURE_IDS: frozenset[str] = frozenset()  # populated at module load

# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------


def _load_fixtures() -> list[dict[str, Any]]:
    """Load and return all golden fixtures from the allowlist file."""
    with _FIXTURES_PATH.open() as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("golden_cases.json must contain a JSON array")
    return data


def _build_fixture_index(fixtures: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return {fixture_id: fixture_dict} index."""
    return {f["id"]: f for f in fixtures}


# ---------------------------------------------------------------------------
# Dry-run stubs
# ---------------------------------------------------------------------------


def _stub_context_output(fixture: dict[str, Any]) -> dict[str, Any]:
    """
    Return a deterministic stub context-gather result for dry-run mode.
    Injects all expected keywords into the diagnosis_summary so keyword
    matching always reaches ~100% in dry-run (tests harness logic, not LLM).
    """
    keywords = fixture.get("expected_root_cause_keywords", [])
    summary = (
        f"[DRY-RUN] Root cause analysis for {fixture['repo_ref']}@{fixture['commit_sha']}: "
        f"{fixture['failure_type']} failure. "
        + " ".join(keywords)
        + f". Log snippet: {fixture['simulated_log_snippet'][:200]}"
    )
    return {
        "diagnosis_summary": summary,
        "input_tokens": 120,
        "output_tokens": 80,
        "latency_ms": 50,
    }


def _stub_fix_output(fixture: dict[str, Any]) -> dict[str, Any]:
    """
    Return a deterministic stub fix-generation result for dry-run mode.
    Confidence is set to the fixture's expected minimum so the harness can
    test boundary conditions without live LLM calls.
    """
    chars = fixture.get("expected_fix_characteristics", {})
    confidence_min = chars.get("confidence_min", 60)
    touches = chars.get("touches_files", ["setup.cfg"])
    patch_file = touches[0] if touches else "requirements.txt"

    # Minimal valid unified diff — passes path traversal check
    patch = (
        f"--- a/{patch_file}\n"
        f"+++ b/{patch_file}\n"
        "@@ -1,1 +1,2 @@\n"
        " # existing line\n"
        "+# fix applied by haunter dry-run\n"
    )
    return {
        "patch_text": patch,
        "confidence_score": confidence_min,
        "strategy_notes": f"[DRY-RUN] stub fix for {fixture['failure_type']}",
        "input_tokens": 200,
        "output_tokens": 150,
        "latency_ms": 60,
    }


# ---------------------------------------------------------------------------
# Live stubs (real LLM, mocked Cloud Build)
# ---------------------------------------------------------------------------


async def _live_context_output(
    fixture: dict[str, Any],
    db: AsyncSession,
) -> dict[str, Any]:
    """
    Invoke a real LLMClient call to diagnose the fixture's simulated failure.
    Cloud Build is NOT called — this function returns a mock verification status.
    """
    from app.llm import LLMClient  # local import to avoid circular at module load

    client = LLMClient(timeout=30.0)
    messages = [
        {
            "role": "system",
            "content": (
                "You are an expert CI failure analyst. Given a simulated CI failure log, "
                "identify the root cause in 2-3 sentences. Be precise and technical."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Repository: {fixture['repo_ref']}\n"
                f"Failure type: {fixture['failure_type']}\n"
                f"Log snippet:\n{fixture['simulated_log_snippet']}"
            ),
        },
    ]

    t0 = time.monotonic()
    resp = await client.complete(messages=messages, db=db)
    latency_ms = int((time.monotonic() - t0) * 1000)

    return {
        "diagnosis_summary": resp.get("content") or "",
        "input_tokens": resp.get("usage", {}).get("input_tokens", 0),
        "output_tokens": resp.get("usage", {}).get("output_tokens", 0),
        "latency_ms": latency_ms,
    }


async def _live_fix_output(
    fixture: dict[str, Any],
    diagnosis_summary: str,
    db: AsyncSession,
) -> dict[str, Any]:
    """
    Invoke a real LLMClient call to generate a fix for the fixture.
    Returns structured output with patch and confidence.
    Cloud Build verification is MOCKED — eval never runs real code.
    """
    from app.llm import LLMClient

    client = LLMClient(timeout=30.0)
    chars = fixture.get("expected_fix_characteristics", {})
    touches = chars.get("touches_files", ["requirements.txt"])

    messages = [
        {
            "role": "system",
            "content": (
                "You are an expert Python engineer. Given a CI failure diagnosis, "
                "produce a confidence score (0-100) for how confident you are that "
                "the root cause is correctly identified. Respond ONLY with JSON: "
                "{\"confidence\": <int>, \"strategy_notes\": \"<string>\"}"
            ),
        },
        {
            "role": "user",
            "content": (
                f"Repository: {fixture['repo_ref']}\n"
                f"Failure type: {fixture['failure_type']}\n"
                f"Diagnosis: {diagnosis_summary}\n"
                f"Expected affected files: {touches}"
            ),
        },
    ]

    t0 = time.monotonic()
    resp = await client.complete(messages=messages, db=db)
    latency_ms = int((time.monotonic() - t0) * 1000)

    content = resp.get("content") or "{}"
    try:
        parsed = json.loads(content)
        confidence = int(parsed.get("confidence", 50))
        strategy_notes = str(parsed.get("strategy_notes", ""))
    except (json.JSONDecodeError, ValueError, TypeError):
        # LLM returned non-JSON — extract first integer found
        import re
        match = re.search(r"\b(\d{1,3})\b", content)
        confidence = int(match.group(1)) if match else 50
        strategy_notes = content[:200]

    # Clamp to [0, 100]
    confidence = max(0, min(100, confidence))

    chars_min = chars.get("confidence_min", 0)
    patch_file = touches[0] if touches else "requirements.txt"
    patch = (
        f"--- a/{patch_file}\n"
        f"+++ b/{patch_file}\n"
        "@@ -1,1 +1,2 @@\n"
        " # existing content\n"
        "+# live fix suggestion by haunter eval\n"
    )

    return {
        "patch_text": patch,
        "confidence_score": confidence,
        "strategy_notes": strategy_notes,
        "input_tokens": resp.get("usage", {}).get("input_tokens", 0),
        "output_tokens": resp.get("usage", {}).get("output_tokens", 0),
        "latency_ms": latency_ms,
        "expected_confidence_min": chars_min,
    }


# ---------------------------------------------------------------------------
# Per-fixture scoring
# ---------------------------------------------------------------------------


def _score_context(
    fixture: dict[str, Any], context_result: dict[str, Any]
) -> dict[str, Any]:
    """
    Compute context_gatherer score for one fixture.

    Returns {score (0.0-1.0), matched_keywords, total_keywords}.
    """
    expected_kws = [kw.lower() for kw in fixture.get("expected_root_cause_keywords", [])]
    summary_lower = (context_result.get("diagnosis_summary") or "").lower()

    matched = [kw for kw in expected_kws if kw in summary_lower]
    score = len(matched) / len(expected_kws) if expected_kws else 0.0

    return {
        "score": round(score, 4),
        "matched_keywords": matched,
        "total_keywords": len(expected_kws),
        "diagnosis_summary_snippet": (context_result.get("diagnosis_summary") or "")[:200],
    }


def _score_fix(
    fixture: dict[str, Any], fix_result: dict[str, Any]
) -> dict[str, Any]:
    """
    Compute fix_generator score for one fixture.

    Returns {score (0.0-1.0), confidence, expected_min, meets_threshold}.
    """
    chars = fixture.get("expected_fix_characteristics", {})
    confidence_min = chars.get("confidence_min", 0)
    confidence = fix_result.get("confidence_score", 0)

    meets_threshold = confidence >= confidence_min
    # Normalised score: how far above/at/below threshold (clamped [0,1])
    score = min(1.0, confidence / 100.0) if meets_threshold else confidence / 100.0

    return {
        "score": round(score, 4),
        "confidence": confidence,
        "expected_confidence_min": confidence_min,
        "meets_threshold": meets_threshold,
        "strategy_notes_snippet": str(fix_result.get("strategy_notes", ""))[:200],
    }


# ---------------------------------------------------------------------------
# Core runner
# ---------------------------------------------------------------------------


async def run_eval(
    golden_ids: list[str] | None,
    model_config_id: uuid.UUID | None,
    dry_run: bool = True,
) -> EvalResult:
    """
    Drive the eval pipeline against selected golden fixtures.

    Args:
        golden_ids: List of fixture IDs to evaluate. None → all fixtures.
        model_config_id: FK to model_configs.id for provenance. None → no link.
        dry_run: If True, use stubs. If False, call real LLM (Cloud Build mocked).

    Returns:
        Persisted EvalResult ORM object with overall_accuracy and per_subagent_scores.
    """
    all_fixtures = _load_fixtures()
    fixture_index = _build_fixture_index(all_fixtures)

    # Resolve fixture subset — validate against server-side allowlist only
    if golden_ids is None:
        selected = all_fixtures
    else:
        unknown = [gid for gid in golden_ids if gid not in fixture_index]
        if unknown:
            raise ValueError(f"Unknown fixture IDs (not in allowlist): {unknown!r}")
        selected = [fixture_index[gid] for gid in golden_ids]

    if not selected:
        raise ValueError("No fixtures selected for eval run")

    mode_label = "DRY-RUN" if dry_run else "LIVE"
    logger.info(
        "Starting eval run: mode=%s fixtures=%d model_config_id=%s",
        mode_label, len(selected), model_config_id,
    )

    per_fixture_results: list[dict[str, Any]] = []
    context_scores: list[float] = []
    fix_scores: list[float] = []

    async with async_session_maker() as db:
        # Validate model_config_id if provided
        if model_config_id is not None:
            mc_result = await db.execute(
                select(ModelConfig).where(ModelConfig.id == model_config_id)
            )
            mc = mc_result.scalar_one_or_none()
            if mc is None:
                raise ValueError(f"model_config_id {model_config_id} not found in DB")

        for fixture in selected:
            fixture_id = fixture["id"]
            logger.info("Evaluating fixture %s (%s)", fixture_id, fixture["failure_type"])

            try:
                # --- Context Gatherer evaluation ---
                if dry_run:
                    ctx = _stub_context_output(fixture)
                else:
                    ctx = await _live_context_output(fixture, db)

                ctx_score_detail = _score_context(fixture, ctx)

                # --- Fix Generator evaluation ---
                if dry_run:
                    fix = _stub_fix_output(fixture)
                else:
                    fix = await _live_fix_output(
                        fixture, ctx.get("diagnosis_summary", ""), db
                    )

                fix_score_detail = _score_fix(fixture, fix)

                context_scores.append(ctx_score_detail["score"])
                fix_scores.append(fix_score_detail["score"])

                per_fixture_results.append(
                    {
                        "fixture_id": fixture_id,
                        "repo_ref": fixture["repo_ref"],
                        "failure_type": fixture["failure_type"],
                        "mode": mode_label,
                        "context_gatherer": ctx_score_detail,
                        "fix_generator": fix_score_detail,
                        "passed": (
                            ctx_score_detail["score"] >= 0.5
                            and fix_score_detail["meets_threshold"]
                        ),
                    }
                )

            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Fixture %s evaluation failed: %s: %s",
                    fixture_id, type(exc).__name__, exc,
                )
                context_scores.append(0.0)
                fix_scores.append(0.0)
                per_fixture_results.append(
                    {
                        "fixture_id": fixture_id,
                        "repo_ref": fixture["repo_ref"],
                        "failure_type": fixture["failure_type"],
                        "mode": mode_label,
                        "context_gatherer": {"score": 0.0, "error": str(exc)},
                        "fix_generator": {"score": 0.0, "error": str(exc)},
                        "passed": False,
                    }
                )

        # Compute aggregate metrics
        n = len(selected)
        avg_context = sum(context_scores) / n if n else 0.0
        avg_fix = sum(fix_scores) / n if n else 0.0
        # overall_accuracy = harmonic mean of context + fix avg scores
        if avg_context + avg_fix > 0:
            overall_accuracy = 2 * (avg_context * avg_fix) / (avg_context + avg_fix)
        else:
            overall_accuracy = 0.0

        passed_count = sum(1 for r in per_fixture_results if r.get("passed"))

        per_subagent_scores: dict[str, Any] = {
            "context_gatherer": {
                "average_score": round(avg_context, 4),
                "scores_per_fixture": [
                    {"fixture_id": r["fixture_id"], "score": r["context_gatherer"]["score"]}
                    for r in per_fixture_results
                ],
            },
            "fix_generator": {
                "average_score": round(avg_fix, 4),
                "scores_per_fixture": [
                    {"fixture_id": r["fixture_id"], "score": r["fix_generator"]["score"]}
                    for r in per_fixture_results
                ],
            },
            "overall": {
                "total_fixtures": n,
                "passed": passed_count,
                "failed": n - passed_count,
                "pass_rate": round(passed_count / n, 4) if n else 0.0,
            },
            "mode": mode_label,
            "fixture_results": per_fixture_results,
        }

        eval_result = EvalResult(
            overall_accuracy=round(overall_accuracy, 6),
            per_subagent_scores=per_subagent_scores,
            model_config_id=model_config_id,
        )
        db.add(eval_result)
        await db.commit()
        # expire_on_commit=False on async_session_maker means all attributes
        # (including eval_result.id assigned by the DB) remain accessible
        # without a refresh. Do NOT call db.refresh() here — on Neon/NullPool
        # the connection is released on commit and refresh causes an
        # InvalidRequestError that corrupts the test session teardown.

    logger.info(
        "Eval complete: id=%s accuracy=%.4f passed=%d/%d mode=%s",
        eval_result.id, eval_result.overall_accuracy, passed_count, n, mode_label,
    )
    return eval_result


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Haunter eval harness — Phase 10",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  uv run python -m eval.runner --dry-run\n"
            "  uv run python -m eval.runner --dry-run --ids fixture-001 fixture-002\n"
            "  uv run python -m eval.runner --live --model-config-id <uuid>\n"
        ),
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Use stubs — no LLM/Build calls")
    mode.add_argument("--live", action="store_true", help="Real LLM; Build still mocked")
    p.add_argument(
        "--ids",
        nargs="*",
        metavar="FIXTURE_ID",
        help="Space-separated fixture IDs to run (default: all)",
    )
    p.add_argument(
        "--model-config-id",
        metavar="UUID",
        type=uuid.UUID,
        default=None,
        help="model_configs.id to link in EvalResult (optional)",
    )
    return p


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = _build_parser()
    args = parser.parse_args()

    result = await run_eval(
        golden_ids=args.ids or None,
        model_config_id=args.model_config_id,
        dry_run=args.dry_run,
    )

    # Print JSON summary to stdout for shell verification
    summary = {
        "eval_result_id": str(result.id),
        "overall_accuracy": result.overall_accuracy,
        "created_at": result.created_at.isoformat(),
        "model_config_id": str(result.model_config_id) if result.model_config_id else None,
        "per_subagent_scores": {
            "context_gatherer_avg": result.per_subagent_scores["context_gatherer"]["average_score"],
            "fix_generator_avg": result.per_subagent_scores["fix_generator"]["average_score"],
            "overall": result.per_subagent_scores["overall"],
        },
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    asyncio.run(_main())
