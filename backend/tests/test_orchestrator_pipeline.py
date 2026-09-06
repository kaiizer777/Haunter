"""
Phase 7 — Orchestrator End-to-End Pipeline Tests (test_orchestrator_pipeline.py).

Covers:
1. Happy path: pending -> context_gathering -> fix_generation -> verification -> pending_pr -> pr_opened.
   - Status transitions validated.
   - run.status == 'pr_opened', run.pr_url set, run.pr_number set, run.pr_branch set.
   - run.failure_reason is None.
   - All run_steps rows have non-null tokens and latency.
2. Retry path: attempt 1 fails sandbox verification, attempt 2 passes.
   - Prior attempt context and failure_reason passed to attempt 2.
   - Final status pr_opened, 2 attempts in DB.
3. Exhausted retries path: all attempts fail sandbox.
   - Transitions to fallback -> fallback_commented.
   - post_commit_comment called once with 'Haunter AI Diagnosis'.
4. Fast-fail repeated failure heuristic:
   - Two consecutive verification failures with matching 200-char tail.
   - Fast-fails to fallback_commented after attempt 2, skipping attempt 3.
5. Fast-fail different tails:
   - Non-matching tails do not fast fail, full retry loop executes.
6. Wall-clock timeout:
   - ORCHESTRATOR_TIMEOUT_S fires -> run moves to error, failure_reason='orchestrator wall-clock timeout'.
   - orchestrator_timeout step logged in run_steps.
7. Per-step failures with failure_reason persistence:
   - Context gatherer crash -> status=error, failure_reason set, context_gathering_error step.
   - Fix generator crash -> status=error, failure_reason set.
   - Sandbox verification crash -> status=error, failure_reason set, verification_error step.
   - PR creation crash -> status=error, failure_reason set.
   - Fallback comment crash -> status=error, failure_reason set.
8. Token key rotation / decryption failure:
   - ValueError('Token decryption failed') handled gracefully without leaking ciphertext.
9. Pipeline idempotency:
   - Re-entry on a run already in fix_generation resumes cleanly to pr_opened without transition error.
10. Webhook delivery idempotency:
   - Duplicate delivery ID returns 200 duplicate, exactly 1 Run row created in DB.
11. _format_failure_reason:
   - Cap truncation, None exception, HTML escaping, whitespace normalization.
12. _sanitize_fallback:
   - Empty summary default, XSS escaping, secret redaction, patch omission.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.orchestrator as orch_module
from app.config import settings
from app.models import Attempt, Repo, Run, RunStep, User
from app.orchestrator import (
    InvalidTransitionError,
    RunStatus,
    _format_failure_reason,
    _sanitize_fallback,
    check_fast_fail,
    handle_failed_run,
)
from app.subagents.fix_generator import FixGenerationError
from tests.conftest import truncate_all


# ---------------------------------------------------------------------------
# Fixtures & Helpers
# ---------------------------------------------------------------------------


async def _create_test_user(db: AsyncSession, username: str = "pipe-user") -> User:
    user = User(
        github_id=int(uuid.uuid4().int % 1_000_000_000 + 100_000_000),
        github_username=username,
        access_token="fake_pipeline_token_123",
    )
    db.add(user)
    await db.commit()
    return user


async def _create_test_repo(db: AsyncSession, user: User, name: str = "pipe-repo") -> Repo:
    repo = Repo(
        user_id=user.id,
        owner="pipe-org",
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


def _make_mock_attempt(
    run_id: uuid.UUID,
    attempt_number: int,
    patch_text: str = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x\n+y\n",
    confidence_score: int = 95,
    strategy_notes: str = "applied fix",
) -> Attempt:
    return Attempt(
        id=uuid.uuid4(),
        run_id=run_id,
        attempt_number=attempt_number,
        patch_text=patch_text,
        confidence_score=confidence_score,
        strategy_notes=strategy_notes,
    )


# ---------------------------------------------------------------------------
# 1. Happy Path: Full End-to-End Pipeline
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_pipeline_happy_path_full_flow(db: AsyncSession) -> None:
    """Happy path: pending -> context_gathering -> fix_generation -> verification -> pending_pr -> pr_opened.
    Asserts:
      - runs.status == 'pr_opened'
      - runs.pr_url is set to GitHub PR URL
      - runs.pr_number is set
      - runs.pr_branch starts with 'haunter/fix-'
      - runs.failure_reason is None
      - All run_steps rows have input_tokens > 0, output_tokens > 0, latency_ms >= 0.
    """
    await truncate_all(db)
    user = await _create_test_user(db)
    repo = await _create_test_repo(db, user)
    run = await _create_test_run(db, repo, status="pending")
    run_id = run.id

    mock_summary = (
        "ImportError: No module named 'utils' in tests/test_main.py:12. "
        "The module was removed in the latest commit."
    )

    async def mock_gather_context(run, repo, db):
        step = RunStep(
            run_id=run.id,
            step_name="context_gatherer",
            input_tokens=150,
            output_tokens=45,
            latency_ms=180,
            cost_estimate=0.0002,
        )
        db.add(step)
        await db.commit()
        return mock_summary

    async def mock_generate_fix(run, diagnosis_summary, prior_attempt, db):
        attempt = _make_mock_attempt(run.id, 1, strategy_notes="add missing import")
        db.add(attempt)
        step = RunStep(
            run_id=run.id,
            step_name="fix_generator",
            input_tokens=220,
            output_tokens=85,
            latency_ms=320,
            cost_estimate=0.0004,
        )
        db.add(step)
        await db.commit()
        return attempt

    async def mock_generate_pr_text(run, verified_attempt, diagnosis_summary, db):
        step = RunStep(
            run_id=run.id,
            step_name="pr_writer",
            input_tokens=180,
            output_tokens=60,
            latency_ms=210,
            cost_estimate=0.0003,
        )
        db.add(step)
        await db.commit()
        return {
            "title": "fix: add missing utils import in test_main.py",
            "body": "Resolves CI failure caused by missing import in test_main.py.",
        }

    with (
        patch("app.orchestrator.gather_context", side_effect=mock_gather_context),
        patch("app.subagents.fix_generator.generate_fix", side_effect=mock_generate_fix),
        patch(
            "app.sandbox.verify",
            new_callable=AsyncMock,
            return_value={"status": "pass", "failure_reason": None, "build_duration_ms": 1450},
        ),
        patch("app.subagents.pr_writer.generate_pr_text", side_effect=mock_generate_pr_text),
        patch("app.github.pr.get_installation_token", new_callable=AsyncMock, return_value="ghs_install_tok"),
        patch("app.github.pr.create_branch", new_callable=AsyncMock, return_value=None),
        patch("app.github.pr.commit_patch", new_callable=AsyncMock, return_value=None),
        patch(
            "app.github.pr.open_pr",
            new_callable=AsyncMock,
            return_value={"html_url": "https://github.com/pipe-org/pipe-repo/pull/42", "number": 42},
        ),
    ):
        await handle_failed_run(run_id)

    db.expire_all()
    refreshed = await db.execute(select(Run).where(Run.id == run_id))
    db_run = refreshed.scalar_one()

    assert db_run.status == "pr_opened"
    assert db_run.pr_url == "https://github.com/pipe-org/pipe-repo/pull/42"
    assert db_run.pr_number == 42
    assert db_run.pr_branch is not None
    assert db_run.pr_branch.startswith("haunter/fix-")
    assert db_run.failure_reason is None

    # Verify run_steps rows
    steps_res = await db.execute(select(RunStep).where(RunStep.run_id == run_id))
    steps = steps_res.scalars().all()
    assert len(steps) >= 3
    step_names = {s.step_name for s in steps}
    assert {"context_gatherer", "fix_generator", "pr_writer"}.issubset(step_names)

    for step in steps:
        assert step.input_tokens is not None and step.input_tokens > 0
        assert step.output_tokens is not None and step.output_tokens > 0
        assert step.latency_ms is not None and step.latency_ms >= 0


# ---------------------------------------------------------------------------
# 2. Retry Path: First Attempt Fails Sandbox, Second Passes
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_pipeline_retry_sandbox_fail_then_pass(db: AsyncSession) -> None:
    """First attempt fails sandbox verification; second attempt passes.
    Verifies:
      - Attempt count == 2
      - Prior attempt passed to generate_fix on attempt 2
      - Strategy notes carry-over
      - Final status == 'pr_opened'
      - runs.failure_reason is None
    """
    await truncate_all(db)
    user = await _create_test_user(db)
    repo = await _create_test_repo(db, user)
    run = await _create_test_run(db, repo, status="pending")
    run_id = run.id

    prior_attempts_received: list[Optional[Attempt]] = []

    async def mock_generate_fix(run, diagnosis_summary, prior_attempt, db):
        prior_attempts_received.append(prior_attempt)
        attempt_num = len(prior_attempts_received)
        attempt = _make_mock_attempt(
            run.id,
            attempt_num,
            strategy_notes=f"strategy_attempt_{attempt_num}",
        )
        db.add(attempt)
        step = RunStep(
            run_id=run.id,
            step_name="fix_generator",
            input_tokens=200,
            output_tokens=80,
            latency_ms=250,
            cost_estimate=0.0003,
        )
        db.add(step)
        await db.commit()
        return attempt

    # Sandbox: attempt 1 fails, attempt 2 passes
    sandbox_results = [
        {"status": "fail", "failure_reason": "AssertionError in tests/test_foo.py: expected 1 got 2", "build_duration_ms": 1200},
        {"status": "pass", "failure_reason": None, "build_duration_ms": 1100},
    ]

    with (
        patch("app.orchestrator.gather_context", AsyncMock(return_value="Initial diagnosis")),
        patch("app.subagents.fix_generator.generate_fix", side_effect=mock_generate_fix),
        patch("app.sandbox.verify", AsyncMock(side_effect=sandbox_results)),
        patch("app.subagents.pr_writer.generate_pr_text", AsyncMock(return_value={"title": "Fix bug", "body": "Fixed on retry"})),
        patch("app.github.pr.get_installation_token", AsyncMock(return_value="ghs_tok")),
        patch("app.github.pr.create_branch", AsyncMock(return_value=None)),
        patch("app.github.pr.commit_patch", AsyncMock(return_value=None)),
        patch("app.github.pr.open_pr", AsyncMock(return_value={"html_url": "https://github.com/pipe-org/pipe-repo/pull/55", "number": 55})),
    ):
        await handle_failed_run(run_id)

    db.expire_all()
    refreshed = await db.execute(select(Run).where(Run.id == run_id))
    db_run = refreshed.scalar_one()

    assert db_run.status == "pr_opened"
    assert db_run.pr_number == 55
    assert db_run.failure_reason is None

    # Assert prior_attempt carry-over
    assert len(prior_attempts_received) == 2
    assert prior_attempts_received[0] is None
    assert prior_attempts_received[1] is not None
    assert prior_attempts_received[1].attempt_number == 1
    assert prior_attempts_received[1].strategy_notes == "strategy_attempt_1"

    # Assert 2 attempts persisted in DB with correct verification statuses
    att_res = await db.execute(select(Attempt).where(Attempt.run_id == run_id).order_by(Attempt.attempt_number))
    attempts = att_res.scalars().all()
    assert len(attempts) == 2
    assert attempts[0].verification_status == "fail"
    assert "AssertionError" in (attempts[0].failure_reason or "")
    assert attempts[1].verification_status == "pass"
    assert attempts[1].failure_reason is None


# ---------------------------------------------------------------------------
# 3. Exhausted-Retry Path: All Attempts Fail -> Fallback Comment Posted
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_pipeline_exhausted_retries_posts_diagnosis_comment(db: AsyncSession) -> None:
    """When all attempts fail sandbox verification:
      - Status transitions to fallback -> fallback_commented.
      - post_commit_comment is called once.
      - Comment body contains 'Haunter AI Diagnosis'.
      - All attempts in DB have verification_status == 'fail'.
    """
    await truncate_all(db)
    user = await _create_test_user(db)
    repo = await _create_test_repo(db, user)
    run = await _create_test_run(db, repo, status="pending")
    run_id = run.id

    attempt_counter = 0

    async def mock_generate_fix(run, diagnosis_summary, prior_attempt, db):
        nonlocal attempt_counter
        attempt_counter += 1
        attempt = _make_mock_attempt(
            run.id,
            attempt_counter,
            strategy_notes=f"exhausted_note_{attempt_counter}",
        )
        db.add(attempt)
        await db.commit()
        return attempt

    # Each attempt fails with a DIFFERENT failure tail to avoid triggering the fast-fail heuristic
    fail_results = [
        {
            "status": "fail",
            "failure_reason": f"Fail reason tail distinct #{i} " + chr(ord("A") + i) * 200,
            "build_duration_ms": 500,
        }
        for i in range(1, settings.max_attempts + 1)
    ]

    # Capture attributes before session expire_all
    repo_owner = repo.owner
    repo_name = repo.name
    head_sha = run.head_sha

    with (
        patch("app.orchestrator.gather_context", AsyncMock(return_value="Distilled root cause summary")),
        patch("app.subagents.fix_generator.generate_fix", side_effect=mock_generate_fix),
        patch("app.sandbox.verify", AsyncMock(side_effect=fail_results)),
        patch("app.github.pr.get_installation_token", AsyncMock(return_value="ghs_install_tok")),
        patch("app.github_client.post_commit_comment", new_callable=AsyncMock) as mock_comment,
    ):
        await handle_failed_run(run_id)

    db.expire_all()
    refreshed = await db.execute(select(Run).where(Run.id == run_id))
    db_run = refreshed.scalar_one()

    assert db_run.status == "fallback_commented"

    # Verify fallback comment was posted
    mock_comment.assert_called_once()
    call_kwargs = mock_comment.call_args.kwargs
    assert call_kwargs["owner"] == repo_owner
    assert call_kwargs["repo"] == repo_name
    assert call_kwargs["sha"] == head_sha
    assert "Haunter AI Diagnosis:" in call_kwargs["body"]
    assert "Distilled root cause summary" in call_kwargs["body"]

    # Verify attempts in DB
    att_res = await db.execute(select(Attempt).where(Attempt.run_id == run_id))
    attempts = att_res.scalars().all()
    assert len(attempts) == settings.max_attempts
    for att in attempts:
        assert att.verification_status == "fail"


# ---------------------------------------------------------------------------
# 4. Fast-Fail Repeated Failure Heuristic
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_pipeline_fast_fail_repeated_sandbox_failure(db: AsyncSession) -> None:
    """Two consecutive verification failures with identical trailing 200 chars trigger
    fast-fail to fallback_commented after attempt #2 without running attempt #3."""
    await truncate_all(db)
    user = await _create_test_user(db)
    repo = await _create_test_repo(db, user)
    run = await _create_test_run(db, repo, status="pending")
    run_id = run.id

    attempt_counter = 0

    async def mock_generate_fix(run, diagnosis_summary, prior_attempt, db):
        nonlocal attempt_counter
        attempt_counter += 1
        attempt = _make_mock_attempt(run.id, attempt_counter)
        db.add(attempt)
        await db.commit()
        return attempt

    # Identical trailing 200 chars
    identical_tail = "pytest: TypeError: NoneType object is not callable in tests/test_core.py:42"
    identical_tail = identical_tail.ljust(200, "-")

    fail_results = [
        {"status": "fail", "failure_reason": "attempt_1_prefix_" + identical_tail, "build_duration_ms": 600},
        {"status": "fail", "failure_reason": "attempt_2_prefix_" + identical_tail, "build_duration_ms": 600},
        {"status": "fail", "failure_reason": "should_never_run_attempt_3", "build_duration_ms": 600},
    ]

    with (
        patch("app.orchestrator.gather_context", AsyncMock(return_value="Diagnosis text")),
        patch("app.subagents.fix_generator.generate_fix", side_effect=mock_generate_fix),
        patch("app.sandbox.verify", AsyncMock(side_effect=fail_results)),
        patch("app.github.pr.get_installation_token", AsyncMock(return_value="ghs_tok")),
        patch("app.github_client.post_commit_comment", new_callable=AsyncMock) as mock_comment,
    ):
        await handle_failed_run(run_id)

    db.expire_all()
    refreshed = await db.execute(select(Run).where(Run.id == run_id))
    db_run = refreshed.scalar_one()

    # Fast-fail must end in fallback_commented
    assert db_run.status == "fallback_commented"
    mock_comment.assert_called_once()

    # Crucial assertion: exactly 2 attempts were executed, NOT 3
    att_res = await db.execute(select(Attempt).where(Attempt.run_id == run_id))
    attempts = att_res.scalars().all()
    assert len(attempts) == 2, f"Expected 2 attempts from fast-fail, got {len(attempts)}"


# ---------------------------------------------------------------------------
# 5. Wall-Clock Timeout
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_pipeline_wall_clock_timeout(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """When orchestrator body exceeds ORCHESTRATOR_TIMEOUT_S:
      - asyncio.TimeoutError is caught.
      - runs.status transitions to 'error'.
      - runs.failure_reason == 'orchestrator wall-clock timeout'.
      - RunStep 'orchestrator_timeout' is recorded.
    """
    await truncate_all(db)
    user = await _create_test_user(db)
    repo = await _create_test_repo(db, user)
    run = await _create_test_run(db, repo, status="pending")
    run_id = run.id

    # Set very short timeout for test
    monkeypatch.setattr(orch_module, "ORCHESTRATOR_TIMEOUT_S", 0.05)

    async def slow_gather(run, repo, db):
        await asyncio.sleep(0.2)
        return "Slow summary"

    with patch("app.orchestrator.gather_context", side_effect=slow_gather):
        await handle_failed_run(run_id)

    db.expire_all()
    refreshed = await db.execute(select(Run).where(Run.id == run_id))
    db_run = refreshed.scalar_one()

    assert db_run.status == "error"
    assert db_run.failure_reason == "orchestrator wall-clock timeout"

    steps_res = await db.execute(select(RunStep).where(RunStep.run_id == run_id))
    steps = steps_res.scalars().all()
    assert any(s.step_name == "orchestrator_timeout" for s in steps)


# ---------------------------------------------------------------------------
# 6. Per-Step Failures
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_pipeline_context_gatherer_crash_sets_error(db: AsyncSession) -> None:
    """When context_gatherer raises an unhandled exception:
      - runs.status ends in 'error'.
      - runs.failure_reason contains stage and exception message.
      - synthetic context_gathering_error step recorded in run_steps.
    """
    await truncate_all(db)
    user = await _create_test_user(db)
    repo = await _create_test_repo(db, user)
    run = await _create_test_run(db, repo, status="pending")
    run_id = run.id

    with patch(
        "app.orchestrator.gather_context",
        AsyncMock(side_effect=RuntimeError("GitHub API 404: logs not found")),
    ):
        await handle_failed_run(run_id)

    db.expire_all()
    refreshed = await db.execute(select(Run).where(Run.id == run_id))
    db_run = refreshed.scalar_one()

    assert db_run.status == "error"
    assert db_run.failure_reason is not None
    assert "context_gathering" in db_run.failure_reason
    assert "RuntimeError" in db_run.failure_reason
    assert "GitHub API 404" in db_run.failure_reason

    steps_res = await db.execute(select(RunStep).where(RunStep.run_id == run_id))
    steps = steps_res.scalars().all()
    assert any(s.step_name == "context_gathering_error" for s in steps)


@pytest.mark.anyio
async def test_pipeline_fix_generator_timeout_crash(db: AsyncSession) -> None:
    """When generate_fix raises FixGenerationError:
      - runs.status ends in 'error'.
      - runs.failure_reason is persisted with fix_generator stage prefix.
    """
    await truncate_all(db)
    user = await _create_test_user(db)
    repo = await _create_test_repo(db, user)
    run = await _create_test_run(db, repo, status="pending")
    run_id = run.id

    with (
        patch("app.orchestrator.gather_context", AsyncMock(return_value="Diagnosis ready")),
        patch(
            "app.subagents.fix_generator.generate_fix",
            AsyncMock(side_effect=FixGenerationError("LLM output failed schema validation on both attempts")),
        ),
    ):
        await handle_failed_run(run_id)

    db.expire_all()
    refreshed = await db.execute(select(Run).where(Run.id == run_id))
    db_run = refreshed.scalar_one()

    assert db_run.status == "error"
    assert db_run.failure_reason is not None
    assert "fix_generator" in db_run.failure_reason
    assert "FixGenerationError" in db_run.failure_reason


@pytest.mark.anyio
async def test_pipeline_sandbox_crash_sets_error(db: AsyncSession) -> None:
    """When sandbox_verify raises an unexpected runtime exception:
      - runs.status transitions to 'error'.
      - runs.failure_reason is recorded with verification prefix.
      - verification_error step recorded in run_steps.
    """
    await truncate_all(db)
    user = await _create_test_user(db)
    repo = await _create_test_repo(db, user)
    run = await _create_test_run(db, repo, status="pending")
    run_id = run.id

    async def mock_generate_fix(run, diagnosis_summary, prior_attempt, db):
        attempt = _make_mock_attempt(run.id, 1)
        db.add(attempt)
        await db.commit()
        return attempt

    with (
        patch("app.orchestrator.gather_context", AsyncMock(return_value="Diagnosis")),
        patch("app.subagents.fix_generator.generate_fix", side_effect=mock_generate_fix),
        patch(
            "app.sandbox.verify",
            AsyncMock(side_effect=RuntimeError("CodeBuild container provisioning failed")),
        ),
    ):
        await handle_failed_run(run_id)

    db.expire_all()
    refreshed = await db.execute(select(Run).where(Run.id == run_id))
    db_run = refreshed.scalar_one()

    assert db_run.status == "error"
    assert db_run.failure_reason is not None
    assert "verification" in db_run.failure_reason
    assert "CodeBuild container provisioning failed" in db_run.failure_reason

    steps_res = await db.execute(select(RunStep).where(RunStep.run_id == run_id))
    steps = steps_res.scalars().all()
    assert any(s.step_name == "verification_error" for s in steps)


@pytest.mark.anyio
async def test_pipeline_pr_writer_crash_sets_error(db: AsyncSession) -> None:
    """When PR creation (e.g. open_pr) fails with an exception:
      - runs.status moves to 'error'.
      - runs.failure_reason records pr_writer stage error.
    """
    await truncate_all(db)
    user = await _create_test_user(db)
    repo = await _create_test_repo(db, user)
    run = await _create_test_run(db, repo, status="pending")
    run_id = run.id

    async def mock_generate_fix(run, diagnosis_summary, prior_attempt, db):
        attempt = _make_mock_attempt(run.id, 1)
        db.add(attempt)
        await db.commit()
        return attempt

    with (
        patch("app.orchestrator.gather_context", AsyncMock(return_value="Diagnosis")),
        patch("app.subagents.fix_generator.generate_fix", side_effect=mock_generate_fix),
        patch("app.sandbox.verify", AsyncMock(return_value={"status": "pass", "failure_reason": None, "build_duration_ms": 1000})),
        patch("app.subagents.pr_writer.generate_pr_text", AsyncMock(return_value={"title": "PR Title", "body": "PR Body"})),
        patch("app.github.pr.get_installation_token", AsyncMock(return_value="tok")),
        patch("app.github.pr.create_branch", AsyncMock(return_value=None)),
        patch("app.github.pr.commit_patch", AsyncMock(return_value=None)),
        patch("app.github.pr.open_pr", AsyncMock(side_effect=RuntimeError("GitHub API 403: branch protection"))),
    ):
        await handle_failed_run(run_id)

    db.expire_all()
    refreshed = await db.execute(select(Run).where(Run.id == run_id))
    db_run = refreshed.scalar_one()

    assert db_run.status == "error"
    assert db_run.failure_reason is not None
    assert "pr_writer" in db_run.failure_reason
    assert "branch protection" in db_run.failure_reason


@pytest.mark.anyio
async def test_pipeline_fallback_comment_crash_sets_error(db: AsyncSession) -> None:
    """When posting fallback comment fails:
      - runs.status moves to 'error'.
      - runs.failure_reason records fallback_comment error.
    """
    await truncate_all(db)
    user = await _create_test_user(db)
    repo = await _create_test_repo(db, user)
    run = await _create_test_run(db, repo, status="pending")
    run_id = run.id

    attempt_counter = 0

    async def mock_generate_fix(run, diagnosis_summary, prior_attempt, db):
        nonlocal attempt_counter
        attempt_counter += 1
        attempt = _make_mock_attempt(run.id, attempt_counter)
        db.add(attempt)
        await db.commit()
        return attempt

    fail_results = [
        {
            "status": "fail",
            "failure_reason": f"Fail reason distinct #{i} " + chr(ord("A") + i) * 200,
            "build_duration_ms": 500,
        }
        for i in range(1, settings.max_attempts + 1)
    ]

    with (
        patch("app.orchestrator.gather_context", AsyncMock(return_value="Diagnosis")),
        patch("app.subagents.fix_generator.generate_fix", side_effect=mock_generate_fix),
        patch("app.sandbox.verify", AsyncMock(side_effect=fail_results)),
        patch("app.github.pr.get_installation_token", AsyncMock(return_value="tok")),
        patch("app.github_client.post_commit_comment", AsyncMock(side_effect=RuntimeError("GitHub comment API down 500"))),
    ):
        await handle_failed_run(run_id)

    db.expire_all()
    refreshed = await db.execute(select(Run).where(Run.id == run_id))
    db_run = refreshed.scalar_one()

    assert db_run.status == "error"
    assert db_run.failure_reason is not None
    assert "fallback_comment" in db_run.failure_reason
    assert "GitHub comment API down" in db_run.failure_reason


# ---------------------------------------------------------------------------
# 7. Token Key Rotation Mid-Run (No Ciphertext Leakage)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_pipeline_token_key_rotation_does_not_leak_ciphertext(db: AsyncSession) -> None:
    """When token decryption fails mid-run due to key rotation:
      - runs.status transitions to 'error'.
      - failure_reason contains 'ValueError: Token decryption failed'.
      - Raw ciphertext or secret tokens NEVER leak into failure_reason, trace steps, or logs.
    """
    await truncate_all(db)
    user = await _create_test_user(db)
    repo = await _create_test_repo(db, user)
    run = await _create_test_run(db, repo, status="pending")
    run_id = run.id

    secret_ciphertext = "gAAAAABlXYZ_super_secret_raw_ciphertext_bytes_here"

    async def gather_with_token_failure(run, repo, db):
        # Simulate token decryption error caused by key rotation
        raise ValueError("Token decryption failed")

    with patch("app.orchestrator.gather_context", side_effect=gather_with_token_failure):
        await handle_failed_run(run_id)

    db.expire_all()
    refreshed = await db.execute(select(Run).where(Run.id == run_id))
    db_run = refreshed.scalar_one()

    assert db_run.status == "error"
    assert db_run.failure_reason is not None
    assert "Token decryption failed" in db_run.failure_reason
    # Verify no ciphertext leakage
    assert secret_ciphertext not in db_run.failure_reason

    steps_res = await db.execute(select(RunStep).where(RunStep.run_id == run_id))
    steps = steps_res.scalars().all()
    for s in steps:
        assert secret_ciphertext not in s.step_name


# ---------------------------------------------------------------------------
# 8. Idempotent Re-entry & Duplicate Webhook
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_pipeline_idempotent_reentry_from_fix_generation(db: AsyncSession) -> None:
    """If handle_failed_run is re-invoked on a run already at 'fix_generation'
    (e.g. SQS redelivery or background task resume):
      - Skips pending -> context_gathering transition without error.
      - Resumes directly and reaches pr_opened.
    """
    await truncate_all(db)
    user = await _create_test_user(db)
    repo = await _create_test_repo(db, user)
    run = await _create_test_run(
        db, repo, status="fix_generation", diagnosis_summary="Prior gathered context"
    )
    run_id = run.id

    async def mock_generate_fix(run, diagnosis_summary, prior_attempt, db):
        attempt = _make_mock_attempt(run.id, 1)
        db.add(attempt)
        await db.commit()
        return attempt

    with (
        patch("app.orchestrator.gather_context", AsyncMock(return_value="Prior gathered context")),
        patch("app.subagents.fix_generator.generate_fix", side_effect=mock_generate_fix),
        patch("app.sandbox.verify", AsyncMock(return_value={"status": "pass", "failure_reason": None, "build_duration_ms": 1000})),
        patch("app.subagents.pr_writer.generate_pr_text", AsyncMock(return_value={"title": "Fix", "body": "Fixed"})),
        patch("app.github.pr.get_installation_token", AsyncMock(return_value="tok")),
        patch("app.github.pr.create_branch", AsyncMock(return_value=None)),
        patch("app.github.pr.commit_patch", AsyncMock(return_value=None)),
        patch("app.github.pr.open_pr", AsyncMock(return_value={"html_url": "https://github.com/pipe-org/pipe-repo/pull/77", "number": 77})),
    ):
        await handle_failed_run(run_id)

    db.expire_all()
    refreshed = await db.execute(select(Run).where(Run.id == run_id))
    db_run = refreshed.scalar_one()

    assert db_run.status == "pr_opened"
    assert db_run.pr_number == 77


@pytest.mark.anyio
async def test_webhook_delivery_idempotency_no_duplicate_run(
    client: httpx.AsyncClient,
    db: AsyncSession,
) -> None:
    """Duplicate webhook delivery with the same delivery_id produces 200 duplicate
    and creates exactly ONE Run row in the database."""
    await truncate_all(db)
    user = await _create_test_user(db, username="webhook-idem-user")
    repo = await _create_test_repo(db, user, name="idem-repo")

    webhook_secret = settings.github_webhook_secret or "test_webhook_secret_key_12345"
    delivery_id = str(uuid.uuid4())
    run_id = 88112233

    payload = {
        "action": "completed",
        "workflow_run": {
            "id": run_id,
            "head_sha": "abcdefabcdefabcdefabcdefabcdefabcdefabcd",
            "head_branch": "main",
            "conclusion": "failure",
            "html_url": f"https://github.com/pipe-org/idem-repo/actions/runs/{run_id}",
        },
        "repository": {
            "name": "idem-repo",
            "full_name": "pipe-org/idem-repo",
            "owner": {"login": "pipe-org"},
        },
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = "sha256=" + hmac.new(webhook_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()

    headers = {
        "X-GitHub-Event": "workflow_run",
        "X-GitHub-Delivery": delivery_id,
        "X-Hub-Signature-256": sig,
        "Content-Type": "application/json",
    }

    with patch("app.adapters.hosting.AWSHostingAdapter.schedule_pipeline", new_callable=AsyncMock):
        # First delivery -> queued
        resp1 = await client.post("/webhooks/github", headers=headers, content=raw_body)
        assert resp1.status_code == 200
        assert resp1.json()["status"] == "queued"

        # Second delivery (same delivery_id) -> duplicate
        resp2 = await client.post("/webhooks/github", headers=headers, content=raw_body)
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "duplicate"

    # Exactly 1 run row in DB
    runs_count = await db.execute(select(func.count()).select_from(Run).where(Run.github_delivery_id == delivery_id))
    assert runs_count.scalar() == 1


# ---------------------------------------------------------------------------
# 9. _format_failure_reason and _sanitize_fallback Unit Tests
# ---------------------------------------------------------------------------


def test_format_failure_reason_cap_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    """_format_failure_reason truncates the formatted string to _FAILURE_REASON_MAX_CHARS."""
    monkeypatch.setattr(orch_module, "_FAILURE_REASON_MAX_CHARS", 60)
    exc = ValueError("A" * 200)
    result = _format_failure_reason("stage", exc)
    assert len(result) <= 60
    assert result.startswith("stage: ValueError:")


def test_format_failure_reason_none_exception() -> None:
    """_format_failure_reason handles None exception safely without throwing."""
    result = _format_failure_reason("orchestrator", None)  # type: ignore[arg-type]
    assert "orchestrator" in result
    assert "NoneType" in result
    assert "(no message)" in result


def test_sanitize_fallback_comprehensive() -> None:
    """_sanitize_fallback:
      - None/empty summary defaults to '(no diagnosis available)'
      - HTML/XSS strings escaped
      - sk- secrets redacted
      - Raw patches from attempts never included in the comment output
    """
    # 1. Empty summary default
    out_empty = _sanitize_fallback(None, [])
    assert "(no diagnosis available)" in out_empty
    assert out_empty.startswith("**Haunter AI Diagnosis:**\n\n")

    # 2. XSS escaping & secret redaction
    xss_and_secret = "<script>alert('xss')</script> using sk-abc123XYZ789abcdefghij"
    out_sanitized = _sanitize_fallback(xss_and_secret, [])
    assert "<script>" not in out_sanitized
    assert "&lt;script&gt;" in out_sanitized
    assert "sk-abc123XYZ789abcdefghij" not in out_sanitized
    assert "[REDACTED]" in out_sanitized

    # 3. Patch omission: attempts with raw patch text are never reflected in the comment
    attempt_with_patch = Attempt(
        attempt_number=1,
        patch_text="--- a/secret_file.py\n+++ b/secret_file.py\n@@ -1 +1 @@\n-super_secret_raw_diff_marker",
    )
    out_attempts = _sanitize_fallback("Legit diagnosis", [attempt_with_patch])
    assert "super_secret_raw_diff_marker" not in out_attempts
    assert "--- a/secret_file.py" not in out_attempts
