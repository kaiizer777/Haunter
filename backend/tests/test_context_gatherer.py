"""
Phase 6 Tests — Context Gatherer Subagent.

Covers:
1. _redact_secrets: parametrize all patterns (sk-, ghp_, npg_, ghr_, PEM, DATABASE_URL, Bearer, mixed-case, multiline).
2. _redact_secrets: non-secret text and substring false positives are NOT touched.
3. _truncate_and_redact: under cap unchanged, over cap truncated with suffix, secrets removed.
4. _extract_file_paths_from_diff: diff header path extraction, dedup, /dev/null skip, cap.
5. gather_context happy path: mocked GitHub + LLM completes, persists run_steps (tokens, latency, cost), appends files.
6. gather_context advances runs.status pending → context_gathering → fix_generation and stores summary in runs.diagnosis_summary.
7. gather_context failure paths:
   - LLM timeout raises TimeoutError, sets status error + failure_reason in orchestrator.
   - Empty response retry (attempt 1 empty → attempt 2 succeeds; both empty → ValueError).
   - GitHub 404 absorbed by _safe_fetch; unhandled gatherer failure sets status error + failure_reason.
8. Security contract: assert no sk- pattern reaches the mocked LLM call payload.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.github_client import GitHubResourceNotFoundError
from app.models import Repo, Run, RunStep, User
from app.orchestrator import RunStatus, handle_failed_run
from app.subagents.context_gatherer import (
    CAP_CHARS,
    _extract_file_paths_from_diff,
    _is_empty_summary,
    _redact_secrets,
    _truncate_and_redact,
    gather_context,
)
from tests.conftest import truncate_all


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _create_user(db: AsyncSession) -> User:
    user = User(
        github_id=int(uuid.uuid4().int % 1_000_000_000 + 100_000_000),
        github_username="gather_tester",
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
    *,
    status: str = "pending",
    head_sha: str = "abcdef1234567890abcdef1234567890abcdef12",
    github_run_id: int = 12345678,
) -> Run:
    run = Run(
        repo_id=repo.id,
        github_run_id=github_run_id,
        github_delivery_id=str(uuid.uuid4()),
        head_sha=head_sha,
        head_branch="main",
        status=status,
        conclusion="failure",
    )
    db.add(run)
    await db.commit()
    return run


# ---------------------------------------------------------------------------
# 1. _redact_secrets parametrization
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "secret_input,expected_sub",
    [
        # sk-XXX (OpenAI style, 20+ alphanumeric)
        ("API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456", "[REDACTED]"),
        ("sk-111122223333444455556666", "[REDACTED]"),
        # ghp_XXX (GitHub PAT, 36+ chars)
        ("ghp_123456789012345678901234567890123456", "[REDACTED]"),
        ("Token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", "[REDACTED]"),
        # npg_XXX (Neon / Supabase key, 20+ chars)
        ("NEON_KEY=npg_abcdefghijklmnopqrstuvwxyz", "[REDACTED]"),
        ("npg_12345678901234567890", "[REDACTED]"),
        # ghr_XXX (GitHub runner registration token, 10+ chars) — regression test for confirmed bug
        ("ghr_1234567890abcdef", "[REDACTED]"),
        ("ghr_runner_token_secret_12345", "[REDACTED]"),
        # PEM private keys: RSA, EC, generic — multiline
        (
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0Z3V...\n-----END RSA PRIVATE KEY-----",
            "[REDACTED_PRIVATE_KEY]",
        ),
        (
            "-----BEGIN EC PRIVATE KEY-----\nMHcCAQEEI...\n-----END EC PRIVATE KEY-----",
            "[REDACTED_PRIVATE_KEY]",
        ),
        (
            "-----BEGIN PRIVATE KEY-----\nMIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQC6...\n-----END PRIVATE KEY-----",
            "[REDACTED_PRIVATE_KEY]",
        ),
        # Connection strings
        (
            "postgresql://user:secretpass@ep-cool-fog-123.neon.tech/neondb?sslmode=require",
            "[REDACTED_CONN_STRING]",
        ),
        (
            "postgres://admin:pass123@localhost:5432/mydb",
            "[REDACTED_CONN_STRING]",
        ),
        (
            "mysql://root:secret@127.0.0.1:3306/db",
            "[REDACTED_CONN_STRING]",
        ),
        (
            "mongodb://user:pass@cluster0.mongodb.net/test",
            "[REDACTED_CONN_STRING]",
        ),
        # DATABASE_URL assignments (mixed case, unpooled)
        (
            "DATABASE_URL=postgres://user:pass@host/db",
            "DATABASE_URL=[REDACTED]",
        ),
        (
            "database_url = postgresql://user:pass@host/db",
            "DATABASE_URL=[REDACTED]",
        ),
        (
            "DATABASE_URL_UNPOOLED=postgres://user:pass@host/db",
            "DATABASE_URL=[REDACTED]",
        ),
        # Generic Bearer / authorization headers (case-insensitive)
        (
            "Authorization: mySecretToken1234567890abcdef",
            "[REDACTED_AUTH_HEADER]",
        ),
        (
            "Bearer: mySecretToken1234567890abcdef",
            "[REDACTED_AUTH_HEADER]",
        ),
        (
            "bearer: abcdefghijklmnopqrstuvwxyz12345",
            "[REDACTED_AUTH_HEADER]",
        ),
        (
            "token = abcdefghijklmnopqrstuvwxyz12345",
            "[REDACTED_AUTH_HEADER]",
        ),
    ],
)
def test_redact_secrets_patterns(secret_input: str, expected_sub: str) -> None:
    """_redact_secrets correctly redacts all specified secret patterns."""
    redacted = _redact_secrets(secret_input)
    assert expected_sub in redacted


def test_redact_secrets_multiline_mixed_case() -> None:
    """_redact_secrets works across newlines and mixed case strings."""
    multiline_text = (
        "Log line 1: Build started\n"
        "database_url = postgresql+asyncpg://admin:super_secret@neon.tech/main\n"
        "Log line 3: Connecting with Bearer: ABCDEFGHIJKLMNOPQRSTUVWXYZ123456\n"
        "Log line 4: Found key sk-proj12345678901234567890\n"
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "ABCD1234EFGH5678\n"
        "IJKL9012MNOP3456\n"
        "-----END RSA PRIVATE KEY-----\n"
        "Log line 8: Finished\n"
    )
    redacted = _redact_secrets(multiline_text)
    assert "super_secret" not in redacted
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ123456" not in redacted
    assert "sk-proj12345678901234567890" not in redacted
    assert "ABCD1234EFGH5678" not in redacted
    assert "[REDACTED_PRIVATE_KEY]" in redacted
    assert "Log line 1: Build started" in redacted
    assert "Log line 8: Finished" in redacted


# ---------------------------------------------------------------------------
# 2. False-positive checks: normal text must NOT be redacted
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "safe_text",
    [
        "the gate was at the start of the day",
        "sk-short",  # < 20 chars
        "ghp_short",  # < 36 chars
        "npg_short",  # < 20 chars
        "ghr_short",  # < 10 chars
        "postgres is a great database engine",
        "postgresql syntax error at line 42",
        "database_url is an important setting",
        "bearer of bad news",
        "the token counter was zero",
        "def test_token_expiration(): pass",
        "import requests",
        "AssertionError: Expected 200 got 500",
    ],
)
def test_redact_secrets_preserves_innocent_text(safe_text: str) -> None:
    """Normal text and short prefixes must NOT be corrupted by secret redaction."""
    assert _redact_secrets(safe_text) == safe_text


# ---------------------------------------------------------------------------
# 3. _truncate_and_redact
# ---------------------------------------------------------------------------

def test_truncate_and_redact_under_cap() -> None:
    """Input under cap is unchanged when no secrets are present."""
    text = "Short log message from pytest runner."
    assert _truncate_and_redact(text) == text


def test_truncate_and_redact_over_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Input over cap is truncated to max_chars with suffix appended."""
    monkeypatch.setattr("app.subagents.context_gatherer.CAP_CHARS", 50)
    long_text = "A" * 80
    result = _truncate_and_redact(long_text)
    assert result.startswith("A" * 50)
    assert "\n[...TRUNCATED...]" in result
    assert len(result) < 80


def test_truncate_and_redact_removes_secrets_in_head_and_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Secrets in head are redacted; secrets in tail are discarded by truncation."""
    monkeypatch.setattr("app.subagents.context_gatherer.CAP_CHARS", 60)
    head_secret = "sk-12345678901234567890"
    tail_secret = "npg_99999999999999999999"
    text = f"Prefix {head_secret} " + ("x" * 50) + f" Tail {tail_secret}"

    result = _truncate_and_redact(text)
    assert head_secret not in result
    assert tail_secret not in result
    assert "[REDACTED]" in result
    assert "\n[...TRUNCATED...]" in result


# ---------------------------------------------------------------------------
# 4. _extract_file_paths_from_diff
# ---------------------------------------------------------------------------

def test_extract_file_paths_from_diff() -> None:
    """Extracts touched file paths, deduplicates, skips /dev/null, and respects cap."""
    diff_text = (
        "--- a/backend/app/main.py\n"
        "+++ b/backend/app/main.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-foo\n+bar\n"
        "--- /dev/null\n"
        "+++ b/backend/app/new_file.py\n"
        "@@ -0,0 +1,1 @@\n"
        "+new\n"
        "--- a/backend/app/main.py\n"
        "+++ b/backend/app/main.py\n"
    )
    paths = _extract_file_paths_from_diff(diff_text)
    assert paths == ["backend/app/main.py", "backend/app/new_file.py"]


def test_extract_file_paths_empty() -> None:
    """Returns empty list for empty diff."""
    assert _extract_file_paths_from_diff("") == []


# ---------------------------------------------------------------------------
# 5. gather_context happy path (unit)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_gather_context_happy_path(db: AsyncSession) -> None:
    """gather_context calls LLMClient.complete, persists run_steps row, appends file list."""
    await truncate_all(db)
    user = await _create_user(db)
    repo = await _create_repo(db, user)
    run = await _create_run(db, repo, status="context_gathering")

    fixture_logs = "FAILED tests/test_db.py::test_connect - ConnectionRefusedError"
    fixture_diff = "--- a/backend/app/db.py\n+++ b/backend/app/db.py\n@@ -10,1 +10,1 @@\n-port = 5432\n+port = 9999\n"
    fixture_meta = {"sha": run.head_sha, "commit": {"message": "change db port"}}

    mock_llm_response = {
        "content": "ConnectionRefusedError in backend/app/db.py:10 — database port changed to 9999.",
        "usage": {"input_tokens": 120, "output_tokens": 40},
        "latency_ms": 280,
        "model": "nemotron-3.5-lightning-free",
    }

    with (
        patch("app.subagents.context_gatherer.gh.fetch_workflow_run_logs", new_callable=AsyncMock, return_value=fixture_logs),
        patch("app.subagents.context_gatherer.gh.fetch_diff", new_callable=AsyncMock, return_value=fixture_diff),
        patch("app.subagents.context_gatherer.gh.fetch_commit_metadata", new_callable=AsyncMock, return_value=fixture_meta),
        patch("app.subagents.context_gatherer.LLMClient.complete", new_callable=AsyncMock, return_value=mock_llm_response) as mock_complete,
    ):
        summary = await gather_context(run=run, repo=repo, db=db)

    # 1. Returned summary contains diagnosis and appended file paths section
    assert "ConnectionRefusedError" in summary
    assert "## Files in the failing commit" in summary
    assert "backend/app/db.py" in summary

    # 2. LLM was invoked with built messages
    assert mock_complete.call_count == 1
    call_kwargs = mock_complete.call_args[1]
    messages = call_kwargs["messages"]
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert "Context Gatherer" in messages[0]["content"]
    assert "FAILED tests/test_db.py" in messages[1]["content"]

    # 3. run_steps row persisted with accurate metrics
    steps = (await db.execute(select(RunStep).where(RunStep.run_id == run.id))).scalars().all()
    assert len(steps) == 1
    step = steps[0]
    assert step.step_name == "context_gatherer"
    assert step.input_tokens == 120
    assert step.output_tokens == 40
    assert step.latency_ms == 280
    assert step.cost_estimate == pytest.approx((120 * 0.001 / 1000) + (40 * 0.002 / 1000), abs=1e-8)


# ---------------------------------------------------------------------------
# 6. Status advancement: pending → context_gathering → fix_generation
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_gather_context_advances_status_and_stores_summary(db: AsyncSession) -> None:
    """Orchestrator advances status pending → context_gathering → fix_generation, stores diagnosis_summary."""
    await truncate_all(db)
    user = await _create_user(db)
    repo = await _create_repo(db, user)
    run = await _create_run(db, repo, status="pending")
    run_id = run.id

    diagnosis_text = "Root cause: SyntaxError in backend/app/main.py:12"
    status_at_fix_gen = None
    summary_at_fix_gen = None

    async def mock_generate_fix(run, diagnosis_summary, *args, **kwargs):
        nonlocal status_at_fix_gen, summary_at_fix_gen
        status_at_fix_gen = run.status
        summary_at_fix_gen = diagnosis_summary
        raise RuntimeError("stop_after_context_gathered")

    with (
        patch("app.subagents.context_gatherer.gh.fetch_workflow_run_logs", new_callable=AsyncMock, return_value="SyntaxError at main.py:12"),
        patch("app.subagents.context_gatherer.gh.fetch_diff", new_callable=AsyncMock, return_value="--- a/backend/app/main.py\n+++ b/backend/app/main.py\n@@ -12,1 +12,1 @@\n"),
        patch("app.subagents.context_gatherer.gh.fetch_commit_metadata", new_callable=AsyncMock, return_value={"sha": run.head_sha}),
        patch(
            "app.subagents.context_gatherer.LLMClient.complete",
            new_callable=AsyncMock,
            return_value={
                "content": diagnosis_text,
                "usage": {"input_tokens": 100, "output_tokens": 30},
                "latency_ms": 200,
                "model": "nemotron-3.5-lightning-free",
            },
        ),
        patch("app.subagents.fix_generator.generate_fix", side_effect=mock_generate_fix),
    ):
        await handle_failed_run(run_id)

    assert status_at_fix_gen == "fix_generation"
    assert summary_at_fix_gen is not None
    assert diagnosis_text in summary_at_fix_gen

    db.expire_all()
    refreshed_run = (await db.execute(select(Run).where(Run.id == run_id))).scalar_one()

    # Diagnosis summary was saved
    assert refreshed_run.diagnosis_summary is not None
    assert diagnosis_text in refreshed_run.diagnosis_summary
    assert "backend/app/main.py" in refreshed_run.diagnosis_summary


# ---------------------------------------------------------------------------
# 7. Failure paths
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_gather_context_llm_timeout_raises_and_sets_error_status(db: AsyncSession) -> None:
    """LLM timeout during gather_context raises TimeoutError and orchestrator transitions to error."""
    await truncate_all(db)
    user = await _create_user(db)
    repo = await _create_repo(db, user)
    run = await _create_run(db, repo, status="pending")
    run_id = run.id

    with (
        patch("app.subagents.context_gatherer.gh.fetch_workflow_run_logs", new_callable=AsyncMock, return_value="logs"),
        patch("app.subagents.context_gatherer.gh.fetch_diff", new_callable=AsyncMock, return_value="diff"),
        patch("app.subagents.context_gatherer.gh.fetch_commit_metadata", new_callable=AsyncMock, return_value={}),
        patch("app.subagents.context_gatherer.LLMClient.complete", new_callable=AsyncMock, side_effect=asyncio.TimeoutError),
    ):
        # Direct call raises TimeoutError
        with pytest.raises(TimeoutError):
            await gather_context(run=run, repo=repo, db=db)

        # Orchestrator handles it: status → error, failure_reason set
        await handle_failed_run(run_id)

    db.expire_all()
    failed_run = (await db.execute(select(Run).where(Run.id == run_id))).scalar_one()
    assert failed_run.status == "error"
    assert failed_run.failure_reason is not None
    assert "orchestrator" in failed_run.failure_reason or "context_gathering" in failed_run.failure_reason or "TimeoutError" in failed_run.failure_reason

    # Confirm error step was logged
    steps = (await db.execute(select(RunStep).where(RunStep.run_id == run_id))).scalars().all()
    assert any(s.step_name in ("orchestrator_timeout", "context_gatherer_error", "context_gathering_error") or "error" in s.step_name for s in steps)


@pytest.mark.anyio
async def test_gather_context_empty_response_retry_flow(db: AsyncSession) -> None:
    """Attempt 1 returns empty → retries with tighter prompt; attempt 2 succeeds."""
    await truncate_all(db)
    user = await _create_user(db)
    repo = await _create_repo(db, user)
    run = await _create_run(db, repo, status="context_gathering")

    empty_response = {
        "content": "```json\n```",
        "usage": {"input_tokens": 50, "output_tokens": 5},
        "latency_ms": 100,
        "model": "nemotron-3.5-lightning-free",
    }
    valid_response = {
        "content": "TypeError: unsupported operand type in calc.py:5",
        "usage": {"input_tokens": 60, "output_tokens": 25},
        "latency_ms": 150,
        "model": "nemotron-3.5-lightning-free",
    }

    mock_complete = AsyncMock(side_effect=[empty_response, valid_response])

    with (
        patch("app.subagents.context_gatherer.gh.fetch_workflow_run_logs", new_callable=AsyncMock, return_value="TypeError"),
        patch("app.subagents.context_gatherer.gh.fetch_diff", new_callable=AsyncMock, return_value=""),
        patch("app.subagents.context_gatherer.gh.fetch_commit_metadata", new_callable=AsyncMock, return_value={}),
        patch("app.subagents.context_gatherer.LLMClient.complete", mock_complete),
    ):
        summary = await gather_context(run=run, repo=repo, db=db)

    assert mock_complete.call_count == 2
    assert "TypeError: unsupported operand" in summary

    # Aggregated token counts persisted
    step = (await db.execute(select(RunStep).where(RunStep.run_id == run.id))).scalar_one()
    assert step.input_tokens == 110  # 50 + 60
    assert step.output_tokens == 30  # 5 + 25


@pytest.mark.anyio
async def test_gather_context_both_attempts_empty_raises_value_error(db: AsyncSession) -> None:
    """Both LLM attempts return empty → persists context_gatherer_error step and raises ValueError."""
    await truncate_all(db)
    user = await _create_user(db)
    repo = await _create_repo(db, user)
    run = await _create_run(db, repo, status="context_gathering")

    empty_response = {
        "content": "   \n\n  ",
        "usage": {"input_tokens": 40, "output_tokens": 2},
        "latency_ms": 80,
        "model": "nemotron-3.5-lightning-free",
    }

    with (
        patch("app.subagents.context_gatherer.gh.fetch_workflow_run_logs", new_callable=AsyncMock, return_value="logs"),
        patch("app.subagents.context_gatherer.gh.fetch_diff", new_callable=AsyncMock, return_value=""),
        patch("app.subagents.context_gatherer.gh.fetch_commit_metadata", new_callable=AsyncMock, return_value={}),
        patch("app.subagents.context_gatherer.LLMClient.complete", new_callable=AsyncMock, return_value=empty_response),
        pytest.raises(ValueError, match="empty summary"),
    ):
        await gather_context(run=run, repo=repo, db=db)

    step = (await db.execute(select(RunStep).where(RunStep.run_id == run.id))).scalar_one()
    assert step.step_name == "context_gatherer_error"
    assert step.input_tokens == 80  # 40 + 40


@pytest.mark.anyio
async def test_gather_context_github_404_handled(db: AsyncSession) -> None:
    """GitHub 404 is caught by _safe_fetch, logs unavailable, gather continues."""
    await truncate_all(db)
    user = await _create_user(db)
    repo = await _create_repo(db, user)
    run = await _create_run(db, repo, status="context_gathering")

    with (
        patch(
            "app.subagents.context_gatherer.gh.fetch_workflow_run_logs",
            new_callable=AsyncMock,
            side_effect=GitHubResourceNotFoundError("Logs not found (404)"),
        ),
        patch("app.subagents.context_gatherer.gh.fetch_diff", new_callable=AsyncMock, return_value=""),
        patch("app.subagents.context_gatherer.gh.fetch_commit_metadata", new_callable=AsyncMock, return_value={}),
        patch(
            "app.subagents.context_gatherer.LLMClient.complete",
            new_callable=AsyncMock,
            return_value={
                "content": "No logs available. Failure could not be diagnosed.",
                "usage": {"input_tokens": 30, "output_tokens": 15},
                "latency_ms": 150,
                "model": "nemotron-3.5-lightning-free",
            },
        ),
    ):
        summary = await gather_context(run=run, repo=repo, db=db)

    assert "No logs available" in summary


# ---------------------------------------------------------------------------
# 8. Security contract: assert no sk- pattern reaches mocked LLM call
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_security_contract_no_secret_reaches_llm_payload(db: AsyncSession) -> None:
    """Secrets in CI logs/diff/meta MUST NOT reach the payload sent to LLMClient.complete."""
    await truncate_all(db)
    user = await _create_user(db)
    repo = await _create_repo(db, user)
    run = await _create_run(db, repo, status="context_gathering")

    leaked_openai = "sk-proj12345678901234567890abcdef"
    leaked_ghp = "ghp_123456789012345678901234567890123456"
    leaked_npg = "npg_supersecretneonkey12345"
    leaked_ghr = "ghr_runnerregistrationtoken123"
    leaked_conn = "postgres://admin:supersecretpass@db.neon.tech/prod"
    leaked_pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0Z3V...\n-----END RSA PRIVATE KEY-----"

    tainted_logs = f"Error: auth failed with {leaked_openai} and {leaked_ghp} and {leaked_ghr}"
    tainted_diff = f"--- a/config.py\n+++ b/config.py\n@@ -1,1 +1,1 @@\n-DATABASE_URL={leaked_conn}\n+{leaked_npg}\n"
    tainted_meta = {"commit": {"message": f"fixed key {leaked_pem}"}}

    captured_payloads: list[list[dict[str, str]]] = []

    async def capture_complete(messages: list[dict[str, str]], **kwargs: Any) -> dict:
        captured_payloads.append(messages)
        return {
            "content": "Root cause: authentication failed due to expired credentials.",
            "usage": {"input_tokens": 100, "output_tokens": 20},
            "latency_ms": 180,
            "model": "nemotron-3.5-lightning-free",
        }

    with (
        patch("app.subagents.context_gatherer.gh.fetch_workflow_run_logs", new_callable=AsyncMock, return_value=tainted_logs),
        patch("app.subagents.context_gatherer.gh.fetch_diff", new_callable=AsyncMock, return_value=tainted_diff),
        patch("app.subagents.context_gatherer.gh.fetch_commit_metadata", new_callable=AsyncMock, return_value=tainted_meta),
        patch("app.subagents.context_gatherer.LLMClient.complete", side_effect=capture_complete),
    ):
        await gather_context(run=run, repo=repo, db=db)

    assert len(captured_payloads) == 1
    full_llm_input = json.dumps(captured_payloads[0])

    # Assert NONE of the raw secrets leaked across the LLM client boundary
    assert leaked_openai not in full_llm_input, "OpenAI sk- token leaked to LLM!"
    assert leaked_ghp not in full_llm_input, "GitHub PAT leaked to LLM!"
    assert leaked_npg not in full_llm_input, "Neon npg_ key leaked to LLM!"
    assert leaked_ghr not in full_llm_input, "GitHub runner ghr_ token leaked to LLM!"
    assert "supersecretpass" not in full_llm_input, "Database password leaked to LLM!"
    assert "BEGIN RSA PRIVATE KEY" not in full_llm_input, "PEM private key leaked to LLM!"

    # Assert redaction placeholders were substituted
    assert "[REDACTED]" in full_llm_input
    assert "[REDACTED_PRIVATE_KEY]" in full_llm_input
