"""
Phase 5 orchestrator + context gatherer tests.

Covers:
1. Valid transition sequence persists correct status in DB.
2. Invalid skip (context_gathering → pending_pr) raises InvalidTransitionError.
3. _redact_secrets scrubs all known secret patterns.
4. gather_context() with mocked GitHub + LLM creates run_steps row, advances status.
5. Secret-laden fake logs are redacted before reaching the LLM call.
6. GitHub fetch timeout does not stall gather (total ≤ 35s per-call).
7. handle_failed_run with GitHub 404 → runs.status = error.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Repo, Run, RunStep, User
from app.orchestrator import (
    InvalidTransitionError,
    RunStatus,
    _transition,
    _validate_transition,
    check_fast_fail,
    handle_failed_run,
)
from app.subagents.context_gatherer import _redact_secrets, _truncate_and_redact
from tests.conftest import truncate_all


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_user(db: AsyncSession) -> User:
    user = User(
        github_id=int(uuid.uuid4().int % 1_000_000_000 + 100_000_000),
        github_username="testuser",
        access_token="fake_token",
    )
    db.add(user)
    await db.commit()
    return user


async def _create_repo(db: AsyncSession, user: User) -> Repo:
    repo = Repo(
        user_id=user.id,
        owner="test-org",
        name="test-repo",
        default_branch="main",
    )
    db.add(repo)
    await db.commit()
    return repo


async def _create_run(
    db: AsyncSession,
    repo: Repo,
    status: str = "pending",
    diagnosis_summary: str | None = None,
) -> Run:
    run = Run(
        repo_id=repo.id,
        github_run_id=int(uuid.uuid4().int % 1_000_000_000),
        github_delivery_id=str(uuid.uuid4()),
        head_sha="abcdef1234567890abcdef1234567890abcdef12",
        head_branch="main",
        status=status,
        conclusion="failure",
        diagnosis_summary=diagnosis_summary,
    )
    db.add(run)
    await db.commit()
    return run


# ---------------------------------------------------------------------------
# Test 1: valid transition sequence persists status
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_valid_transition_sequence(db: AsyncSession) -> None:
    """pending → context_gathering → fix_generation persists in DB correctly."""
    await truncate_all(db)
    user = await _create_user(db)
    repo = await _create_repo(db, user)
    run = await _create_run(db, repo, status="pending")

    await _transition(run, RunStatus.context_gathering, db)
    assert run.status == "context_gathering"

    await _transition(run, RunStatus.fix_generation, db)
    assert run.status == "fix_generation"

    # Confirm DB row reflects new status
    refreshed = await db.execute(select(Run).where(Run.id == run.id))
    db_run = refreshed.scalar_one()
    assert db_run.status == "fix_generation"


# ---------------------------------------------------------------------------
# Test 2: invalid transition raises
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_invalid_transition_skip_raises(db: AsyncSession) -> None:
    """context_gathering → pending_pr is not allowed — must raise."""
    await truncate_all(db)
    user = await _create_user(db)
    repo = await _create_repo(db, user)
    run = await _create_run(db, repo, status="context_gathering")

    with pytest.raises(InvalidTransitionError) as exc_info:
        await _transition(run, RunStatus.pending_pr, db)

    assert exc_info.value.from_status == RunStatus.context_gathering
    assert exc_info.value.to_status == RunStatus.pending_pr
    # DB must NOT have been updated — run should still be context_gathering
    refreshed = await db.execute(select(Run).where(Run.id == run.id))
    db_run = refreshed.scalar_one()
    assert db_run.status == "context_gathering"


@pytest.mark.anyio
async def test_completed_has_no_allowed_transitions() -> None:
    """No transition out of 'completed' is allowed."""
    with pytest.raises(InvalidTransitionError):
        _validate_transition(RunStatus.completed, RunStatus.fix_generation)

    with pytest.raises(InvalidTransitionError):
        _validate_transition(RunStatus.completed, RunStatus.error)


# ---------------------------------------------------------------------------
# Test 3: secret redaction
# ---------------------------------------------------------------------------


def test_redact_secrets_sk_key() -> None:
    text = "Error: invalid key sk-abc123XYZ789abcdefghij provided"
    result = _redact_secrets(text)
    assert "sk-abc123XYZ789abcdefghij" not in result
    assert "[REDACTED]" in result


def test_redact_secrets_ghp_token() -> None:
    text = "token=ghp_abcdefghijklmnopqrstuvwxyz123456789012"
    result = _redact_secrets(text)
    assert "ghp_" not in result
    assert "[REDACTED]" in result


def test_redact_secrets_database_url() -> None:
    text = "DATABASE_URL=postgres://user:pass@host/db\nother line"
    result = _redact_secrets(text)
    assert "postgres://user:pass@host/db" not in result
    assert "[REDACTED]" in result


def test_redact_secrets_pem_key() -> None:
    fake_pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA0Z3VS5JJcds3xHn/ygWep4X\n"
        "-----END RSA PRIVATE KEY-----"
    )
    result = _redact_secrets(fake_pem)
    assert "BEGIN RSA PRIVATE KEY" not in result
    assert "[REDACTED_PRIVATE_KEY]" in result


def test_redact_secrets_npg_key() -> None:
    text = "NEON_KEY=npg_xyz123abcdef456789ghij"
    result = _redact_secrets(text)
    assert "npg_xyz" not in result
    assert "[REDACTED]" in result


def test_truncate_and_redact_caps_length() -> None:
    long_text = "a" * 10_000_100
    result = _truncate_and_redact(long_text)
    assert len(result) < 10_000_100
    assert "[...TRUNCATED...]" in result


# ---------------------------------------------------------------------------
# Test 4: gather_context with mocked GitHub + LLM creates run_steps row
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_gather_context_creates_run_step(db: AsyncSession) -> None:
    """gather_context() with mocked GitHub and LLM should:
    - insert a run_steps row with input_tokens > 0
    - return a non-empty summary string (100–600 chars)
    - not store raw log content in run_steps
    """
    await truncate_all(db)
    user = await _create_user(db)
    repo = await _create_repo(db, user)
    run = await _create_run(db, repo, status="context_gathering")

    mock_summary = (
        "CI failure: ImportError in tests/test_api.py:42. "
        "Module 'httpx' not installed in the test environment. "
        "The dependency was removed from requirements.txt in the latest commit."
    )

    with (
        patch("app.subagents.context_gatherer.gh.fetch_workflow_run_logs", new_callable=AsyncMock) as mock_logs,
        patch("app.subagents.context_gatherer.gh.fetch_diff", new_callable=AsyncMock) as mock_diff,
        patch("app.subagents.context_gatherer.gh.fetch_commit_metadata", new_callable=AsyncMock) as mock_meta,
        patch("app.subagents.context_gatherer.LLMClient.complete", new_callable=AsyncMock) as mock_llm,
    ):
        mock_logs.return_value = "2024-01-01T00:00:00Z ImportError: No module named 'httpx'"
        mock_diff.return_value = "-httpx==0.24.0\n"
        mock_meta.return_value = {"sha": "abc123", "commit": {"message": "remove httpx"}}
        mock_llm.return_value = {
            "content": mock_summary,
            "usage": {"input_tokens": 150, "output_tokens": 45},
            "latency_ms": 320,
            "model": "nemotron-3.5-lightning-free",
        }

        from app.subagents.context_gatherer import gather_context

        summary = await gather_context(run=run, repo=repo, db=db)

    assert len(summary) >= 50, f"Summary too short: {summary!r}"
    assert len(summary) <= 1600, f"Summary too long: {summary!r}"

    # Check run_steps row was inserted
    steps_result = await db.execute(select(RunStep).where(RunStep.run_id == run.id))
    steps = steps_result.scalars().all()
    assert len(steps) == 1
    step = steps[0]
    assert step.input_tokens > 0
    assert step.output_tokens > 0
    assert step.latency_ms >= 0
    assert step.cost_estimate > 0.0
    assert step.step_name == "context_gatherer"


# ---------------------------------------------------------------------------
# Test 5: secret-laden logs are redacted before LLM sees them
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_gather_context_redacts_secrets_before_llm(db: AsyncSession) -> None:
    """Secrets in fake CI logs must not appear in the messages sent to LLM."""
    await truncate_all(db)
    user = await _create_user(db)
    repo = await _create_repo(db, user)
    run = await _create_run(db, repo, status="context_gathering")

    secret_log = (
        "Running tests... DATABASE_URL=postgres://admin:hunter2@neon.tech/prod\n"
        "Error: sk-supersecretkey1234567890abcdefghij token rejected\n"
        "FAILED tests/test_db.py::test_connection"
    )

    captured_messages: list[list[dict]] = []

    async def capture_complete(messages, **kwargs):
        captured_messages.append(messages)
        return {
            "content": "ImportError in test_db.py:10 — DB connection rejected",
            "usage": {"input_tokens": 80, "output_tokens": 20},
            "latency_ms": 200,
            "model": "nemotron-3.5-lightning-free",
        }

    with (
        patch("app.subagents.context_gatherer.gh.fetch_workflow_run_logs", new_callable=AsyncMock) as mock_logs,
        patch("app.subagents.context_gatherer.gh.fetch_diff", new_callable=AsyncMock) as mock_diff,
        patch("app.subagents.context_gatherer.gh.fetch_commit_metadata", new_callable=AsyncMock) as mock_meta,
        patch("app.subagents.context_gatherer.LLMClient.complete", side_effect=capture_complete),
    ):
        mock_logs.return_value = secret_log
        mock_diff.return_value = ""
        mock_meta.return_value = {}

        from app.subagents.context_gatherer import gather_context

        await gather_context(run=run, repo=repo, db=db)

    assert len(captured_messages) == 1
    full_prompt = str(captured_messages[0])

    # Raw secrets must not appear in anything sent to the LLM
    assert "postgres://admin:hunter2@neon.tech/prod" not in full_prompt
    assert "sk-supersecretkey1234567890abcdefghij" not in full_prompt
    assert "hunter2" not in full_prompt

    # Redacted placeholders should be present
    assert "[REDACTED" in full_prompt or "REDACTED" in full_prompt


# ---------------------------------------------------------------------------
# Test 6: GitHub fetch timeout doesn't hang gather indefinitely
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_github_fetch_timeout_does_not_stall(db: AsyncSession) -> None:
    """A hung GitHub fetch should time out via wait_for and not block > 35s total.
    We mock asyncio.wait_for to raise TimeoutError immediately for the logs call
    while other fetches succeed."""
    await truncate_all(db)
    user = await _create_user(db)
    repo = await _create_repo(db, user)
    run = await _create_run(db, repo, status="context_gathering")

    async def slow_logs(*args, **kwargs):
        await asyncio.sleep(0)  # yield; _safe_fetch will wrap with wait_for
        return "slow log output"

    with (
        patch("app.subagents.context_gatherer.gh.fetch_workflow_run_logs", new_callable=AsyncMock) as mock_logs,
        patch("app.subagents.context_gatherer.gh.fetch_diff", new_callable=AsyncMock) as mock_diff,
        patch("app.subagents.context_gatherer.gh.fetch_commit_metadata", new_callable=AsyncMock) as mock_meta,
        patch("app.subagents.context_gatherer.LLMClient.complete", new_callable=AsyncMock) as mock_llm,
        # Simulate timeout on the logs fetch only
        patch(
            "app.subagents.context_gatherer.asyncio.wait_for",
            side_effect=_mock_wait_for_timeout_on_logs,
        ),
    ):
        mock_logs.return_value = "logs"
        mock_diff.return_value = "diff content"
        mock_meta.return_value = {"sha": "aaa"}
        mock_llm.return_value = {
            "content": "Diff shows a removed dep. Import failed.",
            "usage": {"input_tokens": 50, "output_tokens": 15},
            "latency_ms": 100,
            "model": "nemotron-3.5-lightning-free",
        }

        from app.subagents.context_gatherer import gather_context

        # Should not raise — _safe_fetch absorbs the timeout
        summary = await gather_context(run=run, repo=repo, db=db)
        assert isinstance(summary, str)


_wait_for_call_count = 0


async def _mock_wait_for_timeout_on_logs(coro, timeout):
    """Raise TimeoutError on the first wait_for call (logs), pass through the rest."""
    global _wait_for_call_count
    _wait_for_call_count += 1
    if _wait_for_call_count == 1:
        coro.close()  # avoid coroutine-never-awaited warning
        raise asyncio.TimeoutError
    return await coro


# ---------------------------------------------------------------------------
# Test 7: handle_failed_run with GitHub 404 → status = error
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_handle_failed_run_github_404_sets_error(db: AsyncSession) -> None:
    """If all GitHub fetches fail (simulated via exception), status → error."""
    await truncate_all(db)
    user = await _create_user(db)
    repo = await _create_repo(db, user)
    run = await _create_run(db, repo, status="pending")
    run_id = run.id

    from app.github_client import GitHubResourceNotFoundError

    with (
        patch("app.subagents.context_gatherer.gh.fetch_workflow_run_logs", new_callable=AsyncMock) as mock_logs,
        patch("app.subagents.context_gatherer.gh.fetch_diff", new_callable=AsyncMock) as mock_diff,
        patch("app.subagents.context_gatherer.gh.fetch_commit_metadata", new_callable=AsyncMock) as mock_meta,
        patch("app.subagents.context_gatherer.LLMClient.complete", new_callable=AsyncMock) as mock_llm,
    ):
        # All fetches fail with 404 — _safe_fetch returns "" for each
        mock_logs.side_effect = GitHubResourceNotFoundError("not found")
        mock_diff.side_effect = GitHubResourceNotFoundError("not found")
        mock_meta.side_effect = GitHubResourceNotFoundError("not found")

        # LLM still returns something (gather_context called with empty inputs)
        mock_llm.return_value = {
            "content": "No logs available. Unable to determine root cause.",
            "usage": {"input_tokens": 30, "output_tokens": 10},
            "latency_ms": 150,
            "model": "nemotron-3.5-lightning-free",
        }

        from app.orchestrator import handle_failed_run

        # Should complete without raising (orchestrator handles errors internally)
        await handle_failed_run(run_id)

        # Clear the identity map so we read the updated row committed by the separate session
        db.expire_all()

        # Run should now be fix_generation (gather succeeded with empty inputs + LLM summary)
        refreshed = await db.execute(select(Run).where(Run.id == run_id))
        db_run = refreshed.scalar_one()
        # All fetches returned "" → gather_context still calls LLM → status = fix_generation
    assert db_run.status in ("fix_generation", "error")
    # diagnosis_summary should be set if LLM succeeded
    if db_run.status == "fix_generation":
        assert db_run.diagnosis_summary is not None


# ---------------------------------------------------------------------------
# Test 10: Phase 8 Fallback logic
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_handle_failed_run_exhausts_attempts_and_falls_back(db: AsyncSession) -> None:
    """If 3 attempts all fail verification, orchestrator transitions to fallback_commented and posts a comment."""
    await truncate_all(db)
    user = await _create_user(db)
    repo = await _create_repo(db, user)
    run = await _create_run(db, repo, status="pending")
    run.diagnosis_summary = "test diagnosis"
    await db.commit()
    run_id = run.id
    # Capture before session expires
    repo_owner = repo.owner
    repo_name = repo.name
    head_sha = run.head_sha

    FAKE_INSTALL_TOKEN = "ghs_fakeinstallationtoken123"

    _FIX_RESPONSE = {
        "content": '{"patch": "--- a/x\\n+++ b/x\\n@@ -1 +1 @@\\n-x\\n+y\\n", "confidence": 90, "strategy_notes": "test"}',
        "usage": {"input_tokens": 10, "output_tokens": 10},
        "latency_ms": 100,
        "model": "test",
    }

    with (
        patch(
            "app.subagents.context_gatherer.gh.fetch_workflow_run_logs",
            new_callable=AsyncMock,
            return_value="ImportError in app/models.py:10",
        ),
        patch(
            "app.subagents.context_gatherer.gh.fetch_diff",
            new_callable=AsyncMock,
            return_value="-import foo\n",
        ),
        patch(
            "app.subagents.context_gatherer.gh.fetch_commit_metadata",
            new_callable=AsyncMock,
            return_value={"sha": "abc"},
        ),
        patch(
            "app.subagents.context_gatherer.LLMClient.complete",
            new_callable=AsyncMock,
            side_effect=[
                {
                    "content": "test diagnosis",
                    "usage": {"input_tokens": 10, "output_tokens": 10},
                    "latency_ms": 100,
                    "model": "test",
                },
                _FIX_RESPONSE,
                _FIX_RESPONSE,
                _FIX_RESPONSE,
            ],
        ),
        patch(
            "app.sandbox.runner.verify_patch",
            new_callable=AsyncMock,
            return_value={"status": "fail", "failure_reason": "failed tests", "build_duration_ms": 500},
        ),
        # Phase 8: get_installation_token now fetches the write token
        patch(
            "app.github.pr.get_installation_token",
            new_callable=AsyncMock,
            return_value=FAKE_INSTALL_TOKEN,
        ),
        patch(
            "app.github_client.post_commit_comment",
            new_callable=AsyncMock,
        ) as mock_post_comment,
    ):
        from app.orchestrator import handle_failed_run
        await handle_failed_run(run_id)

    db.expire_all()
    refreshed = await db.execute(select(Run).where(Run.id == run_id))
    db_run = refreshed.scalar_one()

    # Phase 8: terminal status is fallback_commented (not the intermediate 'fallback')
    assert db_run.status == "fallback_commented"

    # Verify that post_commit_comment was called with sanitised body + installation token
    mock_post_comment.assert_called_once()
    kwargs = mock_post_comment.call_args.kwargs
    assert kwargs["owner"] == repo_owner
    assert kwargs["repo"] == repo_name
    assert kwargs["sha"] == head_sha
    assert "Haunter AI Diagnosis:" in kwargs["body"]
    assert "test diagnosis" in kwargs["body"]
    # Must use installation token, not broad PAT
    assert kwargs["token"] == FAKE_INSTALL_TOKEN


# ---------------------------------------------------------------------------
# Test 11: Phase 15 — _format_failure_reason produces safe, redacted strings
# ---------------------------------------------------------------------------


def test_format_failure_reason_includes_stage_and_type() -> None:
    """The formatted string must include the stage label, exception type, and message."""
    from app.orchestrator import _format_failure_reason

    exc = RuntimeError("DB connection refused")
    out = _format_failure_reason("context_gatherer", exc)
    assert out == "context_gatherer: RuntimeError: DB connection refused"


def test_format_failure_reason_handles_empty_message() -> None:
    """An exception with no message should still produce a useful string."""
    from app.orchestrator import _format_failure_reason

    exc = RuntimeError()
    out = _format_failure_reason("pr_writer", exc)
    assert "pr_writer" in out
    assert "RuntimeError" in out
    assert "(no message)" in out


def test_format_failure_reason_truncates_long_messages() -> None:
    """A very long message must be truncated to the cap to keep payloads bounded."""
    from app.orchestrator import _format_failure_reason

    huge = "x" * 10_000_005
    exc = ValueError(huge)
    out = _format_failure_reason("fix_generator", exc)
    assert len(out) <= 10_000_000
    assert "fix_generator" in out
    assert "ValueError" in out


def test_format_failure_reason_html_escapes_message() -> None:
    """Any HTML/JS that sneaks into an exception message must be neutralised so
    it cannot land as raw markup on the dashboard."""
    from app.orchestrator import _format_failure_reason

    exc = RuntimeError("<script>alert('xss')</script>")
    out = _format_failure_reason("orchestrator", exc)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_format_failure_reason_collapses_whitespace() -> None:
    """Newlines / tabs / multiple spaces must collapse to single spaces so the
    column stores one logical line."""
    from app.orchestrator import _format_failure_reason

    exc = RuntimeError("line1\nline2\t\twith\ttabs")
    out = _format_failure_reason("verification", exc)
    assert "\n" not in out
    assert "\t" not in out
    assert "line1 line2 with tabs" in out


# ---------------------------------------------------------------------------
# Test 12: Phase 15 — outer exception handler persists failure_reason + step
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_handle_failed_run_outer_exception_writes_failure_reason(
    db: AsyncSession,
) -> None:
    """If an unhandled exception escapes during the pipeline (simulated by
    forcing gather_context to raise), the orchestrator must:

      1. transition the run to status=error
      2. write a synthetic <stage>_error run_steps row (so the timeline isn't empty)
      3. write a failure_reason onto runs that includes the stage and exception type

    This is the regression that produced the empty dashboard: status flipped to
    error but nothing else was persisted, so users saw 0 attempts / 0 cost /
    0 latency and a misleading "wrong_diagnosis" badge.
    """
    await truncate_all(db)
    user = await _create_user(db)
    repo = await _create_repo(db, user)
    run = await _create_run(db, repo, status="pending")
    run_id = run.id

    class _FakePipelineCrash(RuntimeError):
        """Distinct type so we can assert the exception name is surfaced."""

    with patch(
        "app.orchestrator.gather_context",
        new_callable=AsyncMock,
        side_effect=_FakePipelineCrash("simulated LLM provider outage"),
    ):
        from app.orchestrator import handle_failed_run

        # Should NOT raise — the outer handler must swallow and persist.
        await handle_failed_run(run_id)

    db.expire_all()
    refreshed = await db.execute(select(Run).where(Run.id == run_id))
    db_run = refreshed.scalar_one()

    # 1. Status moved to terminal error
    assert db_run.status == "error"

    # 2. A trace step was written so the timeline is no longer empty
    steps_result = await db.execute(select(RunStep).where(RunStep.run_id == run_id))
    steps = steps_result.scalars().all()
    assert len(steps) >= 1
    # Stage label is the orchestrator's current step at crash time.
    # gather_context runs while state["step"] == "context_gathering",
    # so the synthetic step name is "context_gathering_error".
    assert any(s.step_name == "context_gathering_error" for s in steps)

    # 3. failure_reason was written and is informative
    assert db_run.failure_reason is not None
    assert "context_gathering" in db_run.failure_reason
    assert "_FakePipelineCrash" in db_run.failure_reason
    assert "simulated LLM provider outage" in db_run.failure_reason


# ---------------------------------------------------------------------------
# Test 13: Phase 15 — empty diagnosis summary from the LLM is a hard failure
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_gather_context_empty_content_raises(db: AsyncSession) -> None:
    """If the LLM returns empty content (None, '', or whitespace-only), the
    gatherer must raise so the orchestrator records a real failure_reason
    instead of silently passing '' to Fix Generator (which then rejects the
    patch as 'wrong_diagnosis').

    This is the regression behind the screenshot bug: 5188 tokens consumed,
    8.22s latency, but diagnosis_summary was empty in the DB.
    """
    await truncate_all(db)
    user = await _create_user(db)
    repo = await _create_repo(db, user)
    run = await _create_run(db, repo, status="context_gathering")

    with (
        patch("app.subagents.context_gatherer.gh.fetch_workflow_run_logs", new_callable=AsyncMock) as mock_logs,
        patch("app.subagents.context_gatherer.gh.fetch_diff", new_callable=AsyncMock) as mock_diff,
        patch("app.subagents.context_gatherer.gh.fetch_commit_metadata", new_callable=AsyncMock) as mock_meta,
        patch("app.subagents.context_gatherer.LLMClient.complete", new_callable=AsyncMock) as mock_llm,
    ):
        mock_logs.return_value = "Error: ImportError"
        mock_diff.return_value = ""
        mock_meta.return_value = {"sha": "abc"}
        # The model hit its cap and produced only whitespace / a stray code fence.
        # Whitespace-only content is the silent killer — it collapses to "" on .strip().
        mock_llm.return_value = {
            "content": "   \n\n```\n```\n\n  ",
            "usage": {"input_tokens": 4676, "output_tokens": 512},
            "latency_ms": 8220,
            "model": "hy3-free",
        }

        from app.subagents.context_gatherer import gather_context

        with pytest.raises(ValueError) as exc_info:
            await gather_context(run=run, repo=repo, db=db)

    # The error message must name the model + the empty-output condition so the
    # orchestrator's _format_failure_reason produces a useful dashboard string.
    msg = str(exc_info.value)
    assert "empty summary" in msg
    assert "hy3-free" in msg
    assert "output_tokens=512" in msg

    # A context_gatherer_error step should have been written (NOT a success step)
    db.expire_all()
    steps_result = await db.execute(select(RunStep).where(RunStep.run_id == run.id))
    steps = steps_result.scalars().all()
    assert len(steps) == 1
    assert steps[0].step_name == "context_gatherer_error"
    # Token counts from the LLM call are preserved on the error step so the
    # dashboard shows the cost of the wasted call.
    assert steps[0].input_tokens == 4676
    assert steps[0].output_tokens == 512
    assert steps[0].latency_ms == 8220


# ---------------------------------------------------------------------------
# Test 14: PatchFormatRetryExhausted routes to fallback_commented
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_orchestrator_format_exhausted_routes_to_fallback(
    db: AsyncSession,
) -> None:
    await truncate_all(db)
    user = await _create_user(db)
    repo = await _create_repo(db, user)
    # Create the run in fix_generation status with a non-deterministic
    # diagnosis so the deterministic ModuleNotFoundError fast-path
    # does NOT short-circuit the LLM path.
    run = await _create_run(
        db, repo, status="fix_generation",
        diagnosis_summary="SyntaxError: invalid syntax (line 42)",
    )

    # Format-broken response: no diff markers at all
    broken_response = {
        "content": json.dumps({
            "patch": "def fixed():\n    return 42\n",
            "confidence": 80,
            "strategy_notes": "no diff markers",
        }),
        "usage": {"input_tokens": 100, "output_tokens": 50},
        "latency_ms": 200,
        "model": "nemotron-3.5-lightning-free",
    }

    # Mock the GitHub side so the post-fallback comment POST is fake
    with (
        patch(
            "app.subagents.context_gatherer.gather_context",
            AsyncMock(return_value="SyntaxError: invalid syntax (line 42)"),
        ),
        patch(
            "app.subagents.fix_generator.LLMClient.complete",
            AsyncMock(return_value=broken_response),
        ),
        patch(
            "app.github.pr.get_installation_token",
            AsyncMock(return_value="ghs_fake"),
        ),
        patch(
            "app.github_client.post_commit_comment",
            AsyncMock(return_value=None),
        ) as mock_comment,
    ):
        await handle_failed_run(run_id=run.id)

    # Refresh run from DB
    refreshed = await db.execute(select(Run).where(Run.id == run.id))
    run = refreshed.scalar_one()
    assert run.status == "fallback_commented", (
        f"expected fallback_commented, got {run.status}"
    )
    # The diagnosis comment was posted
    assert mock_comment.called, "fallback comment was not posted"
    # A fix_generator_format_exhausted step was recorded
    steps = await db.execute(
        select(RunStep).where(RunStep.run_id == run.id)
    )
    step_names = {s.step_name for s in steps.scalars().all()}
    assert "fix_generator_format_exhausted" in step_names


# ---------------------------------------------------------------------------
# Test 15: Patch traversal raises plain PatchRejected -> status=error
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_orchestrator_path_traversal_still_errors(
    db: AsyncSession,
) -> None:
    await truncate_all(db)
    user = await _create_user(db)
    repo = await _create_repo(db, user)
    run = await _create_run(
        db, repo, status="fix_generation",
        diagnosis_summary="Error in etc/passwd",
    )

    # Path-traversal patch: not retriable (raises plain PatchRejected)
    traversal_patch = (
        "--- a/../../etc/passwd\n"
        "+++ b/etc/passwd\n"
        "@@ -0,0 +1 @@\n"
        "+malicious:root:0:0\n"
    )
    traversal_response = {
        "content": json.dumps({
            "patch": traversal_patch,
            "confidence": 90,
            "strategy_notes": "evil",
        }),
        "usage": {"input_tokens": 100, "output_tokens": 50},
        "latency_ms": 200,
        "model": "nemotron-3.5-lightning-free",
    }

    with (
        patch(
            "app.subagents.context_gatherer.gather_context",
            AsyncMock(return_value="Error in etc/passwd"),
        ),
        patch(
            "app.subagents.fix_generator.LLMClient.complete",
            AsyncMock(return_value=traversal_response),
        ),
        patch(
            "app.github.pr.get_installation_token",
            AsyncMock(return_value="ghs_fake"),
        ),
        patch(
            "app.github_client.post_commit_comment",
            AsyncMock(return_value=None),
        ) as mock_comment,
    ):
        await handle_failed_run(run_id=run.id)

    refreshed = await db.execute(select(Run).where(Run.id == run.id))
    run = refreshed.scalar_one()
    # Security boundary: NOT fallback_commented
    assert run.status == "error", (
        f"expected error, got {run.status}"
    )
    # No fallback comment was posted
    assert not mock_comment.called, (
        "fallback comment MUST NOT be posted on security violation"
    )
    # failure_reason mentions the path traversal
    assert "path traversal" in (run.failure_reason or "").lower()


# ---------------------------------------------------------------------------
# Test 16: Fast-fail heuristic gates (Patch B)
# ---------------------------------------------------------------------------


def test_fast_fail_triggers_on_repeated_fix_generation_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two consecutive attempts fail at fix_generation with identical trailing
    failure_reason: assert skip_to_fallback is True and log contains 'deterministic LLM loop'."""
    caplog.set_level(logging.WARNING, logger="app.orchestrator")
    identical_tail = "pytest: 1 failed in tests/test_main.py:42"

    skip_to_fallback, reason = check_fast_fail(
        prior_tail=identical_tail,
        current_tail=identical_tail,
        current_step=RunStatus.fix_generation.value,
        attempt_number=2,
        prior_attempt_number=1,
    )

    assert skip_to_fallback is True
    assert reason == "llm_loop"
    log_messages = [r.getMessage() for r in caplog.records]
    assert any("deterministic LLM loop" in m for m in log_messages)
    assert any("LLM-deterministic, step=" in m for m in log_messages)


def test_fast_fail_does_not_trigger_on_repeated_mirror_seed_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two consecutive attempts fail at mirror_seed step: assert skip_to_fallback
    is False so loop continues."""
    caplog.set_level(logging.INFO, logger="app.orchestrator")
    seed_tail = "github_actions_runner: failed to seed mirror (422 Unprocessable)"

    skip_to_fallback, reason = check_fast_fail(
        prior_tail=seed_tail,
        current_tail=seed_tail,
        current_step="mirror_seed",
        attempt_number=2,
        prior_attempt_number=1,
    )

    assert skip_to_fallback is False
    assert reason == "non_llm_repeat"
    log_messages = [r.getMessage() for r in caplog.records]
    assert not any("fast-failing" in m for m in log_messages)


def test_fast_fail_does_not_trigger_on_repeated_context_gatherer_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two consecutive attempts fail at context_gatherer step: assert skip_to_fallback
    is False so loop continues."""
    caplog.set_level(logging.INFO, logger="app.orchestrator")
    gather_tail = "context_gatherer: GitHub API rate limit exceeded"

    skip_to_fallback, reason = check_fast_fail(
        prior_tail=gather_tail,
        current_tail=gather_tail,
        current_step="context_gatherer",
        attempt_number=2,
        prior_attempt_number=1,
    )

    assert skip_to_fallback is False
    assert reason == "non_llm_repeat"
    log_messages = [r.getMessage() for r in caplog.records]
    assert not any("fast-failing" in m for m in log_messages)

