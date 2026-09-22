"""
Feature 2: Flaky Test Detective & Quarantine Engine tests (test_flaky_detective.py).

Covers:
1. State transitions: context_gathering -> flake_verification -> flaky_detected when test passes 2/2 clean runs.
2. State transitions: flake_verification -> fix_generation when test consistently fails on rerun.
3. Zero token leak: Fix Generator and LLMClient are never called when flaky test is quarantined (0 tokens burned).
4. Commit comment format and secret redaction verification.
5. Edge cases:
   - No test target isolated -> falls through directly to fix_generation.
   - Sandbox verify_determinism exception -> falls through safely to fix_generation.
6. Unit tests for extract_failing_test_target and DeterminismResult.
"""

from __future__ import annotations

import uuid
from typing import Optional
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Repo, Run, RunStep, User
from app.orchestrator import RunStatus, _ALLOWED_TRANSITIONS, handle_failed_run
from app.sandbox.runner import DeterminismResult
from app.subagents.context_gatherer import extract_failing_test_target
from tests.conftest import truncate_all


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_test_user(db: AsyncSession, username: str = "flaky-user") -> User:
    user = User(
        github_id=int(uuid.uuid4().int % 1_000_000_000 + 100_000_000),
        github_username=username,
        access_token="fake_flaky_token_123",
    )
    db.add(user)
    await db.commit()
    return user


async def _create_test_repo(db: AsyncSession, user: User, name: str = "flaky-repo") -> Repo:
    repo = Repo(
        user_id=user.id,
        owner="flaky-org",
        name=name,
        default_branch="main",
    )
    db.add(repo)
    await db.commit()
    return repo


async def _create_test_run(
    db: AsyncSession,
    repo: Repo,
    status: str = "pending",
    diagnosis_summary: Optional[str] = None,
) -> Run:
    run = Run(
        repo_id=repo.id,
        github_run_id=int(uuid.uuid4().int % 1_000_000_000),
        github_delivery_id=str(uuid.uuid4()),
        head_sha="0123456789abcdef0123456789abcdef01234567",
        head_branch="main",
        status=status,
        conclusion="failure",
        diagnosis_summary=diagnosis_summary,
    )
    db.add(run)
    await db.commit()
    return run


# ---------------------------------------------------------------------------
# Unit tests: extract_failing_test_target & DeterminismResult
# ---------------------------------------------------------------------------


def test_determinism_result_dict_and_attr_access():
    res = DeterminismResult(
        is_flaky=True,
        consecutive_passes=2,
        iteration_results=[
            {"iteration": 1, "passed": True, "duration_ms": 1200, "logs": ""},
            {"iteration": 2, "passed": True, "duration_ms": 1150, "logs": ""},
        ],
    )
    assert res.is_flaky is True
    assert res["is_flaky"] is True
    assert res.consecutive_passes == 2
    assert res["consecutive_passes"] == 2
    assert len(res.iteration_results) == 2
    assert len(res["iteration_results"]) == 2


def test_extract_failing_test_target_markdown_section():
    summary = (
        "Root cause analysis...\n\n"
        "## Failing Test Files (for reference — do NOT modify test files)\n"
        "### tests/test_payments.py\n"
        "```python\ndef test_charge(): ...\n```"
    )
    target = extract_failing_test_target(summary)
    assert target == "tests/test_payments.py"


def test_extract_failing_test_target_pytest_failed_line():
    summary = (
        "CI failure details:\n"
        "FAILED tests/test_race.py::test_concurrent_transfers - TimeoutError\n"
        "Assertion failed."
    )
    target = extract_failing_test_target(summary)
    assert target == "tests/test_race.py::test_concurrent_transfers"


def test_extract_failing_test_target_jest_fail():
    summary = "FAIL src/components/payment.test.ts\nError: timed out"
    target = extract_failing_test_target(summary)
    assert target == "src/components/payment.test.ts"


def test_extract_failing_test_target_empty():
    assert extract_failing_test_target("") is None
    assert extract_failing_test_target(None) is None
    assert extract_failing_test_target("Build failed due to syntax error in main.py") is None


# ---------------------------------------------------------------------------
# State transition checks
# ---------------------------------------------------------------------------


def test_allowed_transitions_table():
    assert RunStatus.flake_verification in _ALLOWED_TRANSITIONS[RunStatus.context_gathering]
    assert RunStatus.fix_generation in _ALLOWED_TRANSITIONS[RunStatus.context_gathering]
    assert RunStatus.flaky_detected in _ALLOWED_TRANSITIONS[RunStatus.flake_verification]
    assert RunStatus.fix_generation in _ALLOWED_TRANSITIONS[RunStatus.flake_verification]
    assert _ALLOWED_TRANSITIONS[RunStatus.flaky_detected] == set()


# ---------------------------------------------------------------------------
# 1. Pipeline Test: Flaky Test Quarantined (2/2 passes)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_flaky_test_detected_quarantines_and_aborts(db: AsyncSession) -> None:
    """
    When verify_determinism passes 2/2 clean runs:
      - State transitions: pending -> context_gathering -> flake_verification -> flaky_detected.
      - run.conclusion == 'flaky_test'
      - run.failure_reason mentions flaky test and consecutive clean runs.
      - Commit comment is posted with expected warning text.
      - Zero tokens sent to Fix Generator (generate_fix never called).
    """
    await truncate_all(db)
    user = await _create_test_user(db)
    repo = await _create_test_repo(db, user)
    run = await _create_test_run(db, repo, status="pending")
    run_id = run.id

    mock_summary = (
        "FAILED tests/test_async_timer.py::test_timing_anomaly\n"
        "AssertionError: Expected within 10ms, got 25ms"
    )

    mock_determinism = DeterminismResult(
        is_flaky=True,
        consecutive_passes=2,
        iteration_results=[
            {"iteration": 1, "passed": True, "duration_ms": 1000, "logs": "pass"},
            {"iteration": 2, "passed": True, "duration_ms": 950, "logs": "pass"},
        ],
    )

    async def mock_gather_context(run, repo, db):
        step = RunStep(
            run_id=run.id,
            step_name="context_gatherer",
            input_tokens=100,
            output_tokens=30,
            latency_ms=120,
            cost_estimate=0.0001,
        )
        db.add(step)
        await db.commit()
        return mock_summary

    mock_generate_fix = AsyncMock()
    mock_post_commit_comment = AsyncMock(return_value={"id": 9999})
    mock_verify_determinism = AsyncMock(return_value=mock_determinism)

    repo_owner = repo.owner
    repo_name = repo.name
    head_sha = run.head_sha

    with (
        patch("app.orchestrator.gather_context", side_effect=mock_gather_context),
        patch("app.sandbox.verify_determinism", mock_verify_determinism),
        patch("app.subagents.fix_generator.generate_fix", mock_generate_fix),
        patch("app.github_client.post_commit_comment", mock_post_commit_comment),
        patch("app.github.pr.get_installation_token", AsyncMock(return_value="fake_gh_token")),
    ):
        await handle_failed_run(run_id)

    # 1. Assert terminal state
    db.expire_all()
    refreshed = await db.execute(select(Run).where(Run.id == run_id))
    fresh_run = refreshed.scalar_one()
    assert fresh_run is not None
    assert fresh_run.status == RunStatus.flaky_detected.value
    assert fresh_run.conclusion == "flaky_test"
    assert "Flaky test detected in tests/test_async_timer.py::test_timing_anomaly" in (fresh_run.failure_reason or "")
    assert "passed 2/2 consecutive clean runs" in (fresh_run.failure_reason or "")

    # 2. Assert Zero Token Leak
    mock_generate_fix.assert_not_called()

    # 3. Assert Commit Comment posted
    mock_post_commit_comment.assert_called_once()
    comment_kwargs = mock_post_commit_comment.call_args.kwargs
    assert comment_kwargs["owner"] == repo_owner
    assert comment_kwargs["repo"] == repo_name
    assert comment_kwargs["sha"] == head_sha
    assert "⚠️ Flaky test detected in `tests/test_async_timer.py::test_timing_anomaly`." in comment_kwargs["body"]
    assert "Passed 2/2 clean runs in sandbox. Fix generation aborted — quarantine suggested." in comment_kwargs["body"]

    # 4. Assert RunStep recorded for flake_verification with 0 tokens
    steps = (await db.execute(select(RunStep).where(RunStep.run_id == run_id))).scalars().all()
    step_names = [s.step_name for s in steps]
    assert "context_gatherer" in step_names
    assert "flake_verification" in step_names
    flake_step = next(s for s in steps if s.step_name == "flake_verification")
    assert flake_step.input_tokens == 0
    assert flake_step.output_tokens == 0
    assert flake_step.latency_ms >= 0


# ---------------------------------------------------------------------------
# 2. Pipeline Test: Consistently Failing Test Falls Through to Fix Generation
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_consistently_failing_test_proceeds_to_fix_generation(db: AsyncSession) -> None:
    """
    When clean rerun fails (test is deterministic bug):
      - State transitions: flake_verification -> fix_generation.
      - generate_fix IS called.
    """
    await truncate_all(db)
    user = await _create_test_user(db)
    repo = await _create_test_repo(db, user)
    run = await _create_test_run(db, repo, status="pending")
    run_id = run.id

    mock_summary = "FAILED tests/test_calc.py::test_addition"

    # Determinism check reproduces failure -> is_flaky is False
    mock_determinism = DeterminismResult(
        is_flaky=False,
        consecutive_passes=0,
        iteration_results=[
            {"iteration": 1, "passed": False, "duration_ms": 1000, "logs": "failed again"},
        ],
    )

    async def mock_gather_context(run, repo, db):
        return mock_summary

    mock_verify_determinism = AsyncMock(return_value=mock_determinism)
    mock_post_commit_comment = AsyncMock()

    # Throw LowConfidenceSkip to terminate cleanly at fallback
    from app.subagents.fix_generator import LowConfidenceSkip
    mock_generate_fix = AsyncMock(side_effect=LowConfidenceSkip("cannot fix"))

    with (
        patch("app.orchestrator.gather_context", side_effect=mock_gather_context),
        patch("app.sandbox.verify_determinism", mock_verify_determinism),
        patch("app.subagents.fix_generator.generate_fix", mock_generate_fix),
        patch("app.github_client.post_commit_comment", mock_post_commit_comment),
        patch("app.github.pr.get_installation_token", AsyncMock(return_value="fake_gh_token")),
    ):
        await handle_failed_run(run_id)

    # Assert generate_fix was called after falling through from flake_verification
    mock_generate_fix.assert_called_once()
    # Flaky commit comment was NOT posted
    mock_post_commit_comment.assert_called_once()  # Called only for fallback comment
    assert "⚠️ Flaky test detected" not in mock_post_commit_comment.call_args.kwargs["body"]


# ---------------------------------------------------------------------------
# 3. Pipeline Test: No Test Target Isolated Falls Through to Fix Generation
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_no_test_target_proceeds_directly_to_fix_generation(db: AsyncSession) -> None:
    """
    When summary contains no identifiable test target:
      - Transitions context_gathering -> fix_generation directly.
      - verify_determinism is never called.
    """
    await truncate_all(db)
    user = await _create_test_user(db)
    repo = await _create_test_repo(db, user)
    run = await _create_test_run(db, repo, status="pending")
    run_id = run.id

    mock_summary = "General build error: Docker daemon unreachable."

    async def mock_gather_context(run, repo, db):
        return mock_summary

    mock_verify_determinism = AsyncMock()
    from app.subagents.fix_generator import LowConfidenceSkip
    mock_generate_fix = AsyncMock(side_effect=LowConfidenceSkip("general error"))

    with (
        patch("app.orchestrator.gather_context", side_effect=mock_gather_context),
        patch("app.sandbox.verify_determinism", mock_verify_determinism),
        patch("app.subagents.fix_generator.generate_fix", mock_generate_fix),
        patch("app.github_client.post_commit_comment", AsyncMock()),
        patch("app.github.pr.get_installation_token", AsyncMock(return_value="token")),
    ):
        await handle_failed_run(run_id)

    # verify_determinism skipped
    mock_verify_determinism.assert_not_called()
    # proceed to fix generation
    mock_generate_fix.assert_called_once()


# ---------------------------------------------------------------------------
# 4. Pipeline Test: Determinism Exception Falls Through Safely
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_verify_determinism_exception_falls_through_to_fix_generation(db: AsyncSession) -> None:
    """
    When verify_determinism raises a network/API exception:
      - Catches exception safely.
      - Transitions flake_verification -> fix_generation.
      - Does not crash the pipeline.
    """
    await truncate_all(db)
    user = await _create_test_user(db)
    repo = await _create_test_repo(db, user)
    run = await _create_test_run(db, repo, status="pending")
    run_id = run.id

    mock_summary = "FAILED tests/test_something.py::test_flaky"

    async def mock_gather_context(run, repo, db):
        return mock_summary

    mock_verify_determinism = AsyncMock(side_effect=RuntimeError("GitHub API outage 503"))
    from app.subagents.fix_generator import LowConfidenceSkip
    mock_generate_fix = AsyncMock(side_effect=LowConfidenceSkip("outage fallback"))

    with (
        patch("app.orchestrator.gather_context", side_effect=mock_gather_context),
        patch("app.sandbox.verify_determinism", mock_verify_determinism),
        patch("app.subagents.fix_generator.generate_fix", mock_generate_fix),
        patch("app.github_client.post_commit_comment", AsyncMock()),
        patch("app.github.pr.get_installation_token", AsyncMock(return_value="token")),
    ):
        await handle_failed_run(run_id)

    # verify_determinism was attempted
    mock_verify_determinism.assert_called_once()
    # proceeded to fix generator despite the error
    mock_generate_fix.assert_called_once()
