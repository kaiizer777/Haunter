"""
Phase 6 Fix Generator tests.

Covers:
1. Happy path: valid LLM JSON → Attempt inserted, correct fields, attempt_number=1.
2. End-to-end orchestrator: gather + generate → runs.status=="verification", attempt row exists.
3. Path traversal rejected: '../../etc/passwd' → PatchRejected, no Attempt inserted.
4. GitHub workflows path rejected: '.github/workflows/ci.yml' → PatchRejected.
5. Confidence=150 → ValidationError, retry, then FixGenerationError (both tries invalid).
6. Confidence as string (strict mode) → ValidationError without coercion.
7. Attempt cap: 3 pre-existing → AttemptCapExceeded, no LLM call.
8. Attempt number increments: 2 pre-existing → new attempt gets attempt_number=3.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Attempt, Repo, Run, RunStep, User
from app.subagents.fix_generator import (
    AttemptCapExceeded,
    FixGenerationError,
    FixOutput,
    LowConfidenceSkip,
    LOW_CONFIDENCE_THRESHOLD,
    PatchRejected,
    _validate_patch,
    generate_fix,
)
from tests.conftest import truncate_all


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_PATCH = (
    "--- a/app/models.py\n"
    "+++ b/app/models.py\n"
    "@@ -10,1 +10,1 @@\n"
    "-import foo\n"
    "+import bar\n"
)

_VALID_LLM_RESPONSE = {
    "content": json.dumps(
        {
            "patch": _VALID_PATCH,
            "confidence": 78,
            "strategy_notes": "replaced bad import",
        }
    ),
    "usage": {"input_tokens": 200, "output_tokens": 80},
    "latency_ms": 350,
    "model": "nemotron-3.5-lightning-free",
}


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


async def _create_run(db: AsyncSession, repo: Repo, status: str = "fix_generation") -> Run:
    run = Run(
        repo_id=repo.id,
        github_run_id=int(uuid.uuid4().int % 1_000_000_000),
        github_delivery_id=str(uuid.uuid4()),
        head_sha="abcdef1234567890abcdef1234567890abcdef12",
        head_branch="main",
        status=status,
        conclusion="failure",
        diagnosis_summary="ImportError: cannot import foo in app/models.py:10",
    )
    db.add(run)
    await db.commit()
    return run


async def _insert_attempt(
    db: AsyncSession,
    run: Run,
    number: int,
    patch_text: str = _VALID_PATCH,
    verification_status: str = "failed",
    failure_reason: str | None = "build error",
) -> Attempt:
    attempt = Attempt(
        run_id=run.id,
        attempt_number=number,
        patch_text=patch_text,
        confidence_score=60,
        verification_status=verification_status,
        failure_reason=failure_reason,
    )
    db.add(attempt)
    await db.commit()
    return attempt


# ---------------------------------------------------------------------------
# Test 1: Happy path — valid response → Attempt row with correct fields
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_generate_fix_happy_path(db: AsyncSession) -> None:
    """Valid LLM JSON → Attempt inserted with correct fields, attempt_number=1."""
    await truncate_all(db)
    user = await _create_user(db)
    repo = await _create_repo(db, user)
    run = await _create_run(db, repo)

    with patch(
        "app.subagents.fix_generator.LLMClient.complete",
        new_callable=AsyncMock,
        return_value=_VALID_LLM_RESPONSE,
    ):
        attempt = await generate_fix(
            run=run,
            diagnosis_summary="ImportError: cannot import foo in app/models.py:10",
            prior_attempt=None,
            db=db,
        )

    assert attempt.attempt_number == 1
    assert attempt.confidence_score == 78
    assert attempt.verification_status == "pending"
    assert attempt.strategy_notes == "replaced bad import"
    assert "@@" in attempt.patch_text

    # Confirm DB row exists
    result = await db.execute(select(Attempt).where(Attempt.run_id == run.id))
    all_attempts = result.scalars().all()
    assert len(all_attempts) == 1
    assert all_attempts[0].id == attempt.id

    # Confirm RunStep trace row was inserted
    steps_result = await db.execute(select(RunStep).where(RunStep.run_id == run.id))
    steps = steps_result.scalars().all()
    assert any(s.step_name == "fix_generator" for s in steps)


# ---------------------------------------------------------------------------
# Test 2: End-to-end orchestrator wiring
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_orchestrator_advances_to_pending_pr(db: AsyncSession) -> None:
    """handle_failed_run with mocked gather + generate + verify (pass) → runs.status=='pending_pr'."""
    await truncate_all(db)
    user = await _create_user(db)
    repo = await _create_repo(db, user)
    run = await _create_run(db, repo, status="pending")
    run_id = run.id

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
            return_value={
                "content": "ImportError: cannot import foo in app/models.py:10",
                "usage": {"input_tokens": 100, "output_tokens": 30},
                "latency_ms": 200,
                "model": "nemotron-3.5-lightning-free",
            },
        ),
        patch(
            "app.subagents.fix_generator.LLMClient.complete",
            new_callable=AsyncMock,
            return_value=_VALID_LLM_RESPONSE,
        ),
        patch(
            "app.sandbox.verifier.verify_patch",
            new_callable=AsyncMock,
            return_value={"status": "pass", "failure_reason": None, "build_duration_ms": 1234},
        ),
        # Phase 8 additions: mock PR opening flow
        patch("app.subagents.pr_writer.generate_pr_text", new_callable=AsyncMock, return_value={"title": "t", "body": "b"}),
        patch("app.github.pr.get_installation_token", new_callable=AsyncMock, return_value="token"),
        patch("app.github.pr.create_branch", new_callable=AsyncMock),
        patch("app.github.pr.commit_patch", new_callable=AsyncMock),
        patch("app.github.pr.open_pr", new_callable=AsyncMock, return_value={"html_url": "url", "number": 1}),
    ):
        from app.orchestrator import handle_failed_run

        await handle_failed_run(run_id)

    db.expire_all()

    result = await db.execute(select(Run).where(Run.id == run_id))
    updated_run = result.scalar_one()
    # Phase 8: advances through pending_pr and creates PR, ending in pr_opened
    assert updated_run.status == "pr_opened"

    attempts_result = await db.execute(select(Attempt).where(Attempt.run_id == run_id))
    attempts = attempts_result.scalars().all()
    assert len(attempts) == 1
    assert attempts[0].attempt_number == 1
    assert attempts[0].verification_status == "pass"
    assert attempts[0].build_duration_ms == 1234


# ---------------------------------------------------------------------------
# Test 3: Path traversal rejected — '../../etc/passwd'
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_traversal_patch_rejected(db: AsyncSession) -> None:
    """Patch targeting '../../etc/passwd' → PatchRejected, no Attempt inserted."""
    await truncate_all(db)
    user = await _create_user(db)
    repo = await _create_repo(db, user)
    run = await _create_run(db, repo)

    traversal_patch = (
        "--- a/../../etc/passwd\n"
        "+++ b/../../etc/passwd\n"
        "@@ -1,1 +1,1 @@\n"
        "-root:x:0:0\n"
        "+haxx0r:x:0:0\n"
    )

    bad_response = {
        **_VALID_LLM_RESPONSE,
        "content": json.dumps(
            {"patch": traversal_patch, "confidence": 70, "strategy_notes": "evil"}
        ),
    }

    with (
        patch(
            "app.subagents.fix_generator.LLMClient.complete",
            new_callable=AsyncMock,
            return_value=bad_response,
        ),
        pytest.raises(PatchRejected),
    ):
        await generate_fix(
            run=run,
            diagnosis_summary="test",
            prior_attempt=None,
            db=db,
        )

    # No Attempt inserted
    result = await db.execute(select(Attempt).where(Attempt.run_id == run.id))
    assert result.scalars().all() == []


# ---------------------------------------------------------------------------
# Test 4: .github/workflows path rejected
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_github_workflows_patch_rejected(db: AsyncSession) -> None:
    """Patch targeting '.github/workflows/ci.yml' → PatchRejected, no Attempt inserted."""
    await truncate_all(db)
    user = await _create_user(db)
    repo = await _create_repo(db, user)
    run = await _create_run(db, repo)

    workflow_patch = (
        "--- a/.github/workflows/ci.yml\n"
        "+++ b/.github/workflows/ci.yml\n"
        "@@ -1,1 +1,1 @@\n"
        "-python-version: '3.11'\n"
        "+python-version: '2.7'\n"
    )

    bad_response = {
        **_VALID_LLM_RESPONSE,
        "content": json.dumps(
            {"patch": workflow_patch, "confidence": 55, "strategy_notes": None}
        ),
    }

    with (
        patch(
            "app.subagents.fix_generator.LLMClient.complete",
            new_callable=AsyncMock,
            return_value=bad_response,
        ),
        pytest.raises(PatchRejected),
    ):
        await generate_fix(
            run=run,
            diagnosis_summary="test",
            prior_attempt=None,
            db=db,
        )

    result = await db.execute(select(Attempt).where(Attempt.run_id == run.id))
    assert result.scalars().all() == []


# ---------------------------------------------------------------------------
# Test 5: confidence=150 → ValidationError on both tries → FixGenerationError
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_confidence_out_of_range_raises_fix_generation_error(db: AsyncSession) -> None:
    """LLM returns confidence=150 → rejected via Pydantic, retried, still invalid → FixGenerationError."""
    await truncate_all(db)
    user = await _create_user(db)
    repo = await _create_repo(db, user)
    run = await _create_run(db, repo)

    bad_content = json.dumps({"patch": _VALID_PATCH, "confidence": 150, "strategy_notes": None})
    bad_response = {**_VALID_LLM_RESPONSE, "content": bad_content}

    # Both initial and retry return the same bad content
    with (
        patch(
            "app.subagents.fix_generator.LLMClient.complete",
            new_callable=AsyncMock,
            return_value=bad_response,
        ),
        pytest.raises(FixGenerationError),
    ):
        await generate_fix(
            run=run,
            diagnosis_summary="test",
            prior_attempt=None,
            db=db,
        )

    # LLM called twice (initial + retry)
    # No Attempt inserted
    result = await db.execute(select(Attempt).where(Attempt.run_id == run.id))
    assert result.scalars().all() == []


# ---------------------------------------------------------------------------
# Test 6: confidence as string → ValidationError (strict=True, no coercion)
# ---------------------------------------------------------------------------


def test_fix_output_strict_rejects_string_confidence() -> None:
    """FixOutput with strict=True must reject confidence='78' (no int coercion)."""
    with pytest.raises(ValidationError) as exc_info:
        FixOutput.model_validate_json(
            json.dumps({"patch": _VALID_PATCH, "confidence": "78", "strategy_notes": None})
        )
    errors = exc_info.value.errors()
    assert any(e["loc"] == ("confidence",) for e in errors)


def test_fix_output_strict_rejects_float_confidence() -> None:
    """FixOutput with strict=True must reject confidence=78.5 (no float→int coercion)."""
    with pytest.raises(ValidationError) as exc_info:
        FixOutput.model_validate_json(
            json.dumps({"patch": _VALID_PATCH, "confidence": 78.5, "strategy_notes": None})
        )
    errors = exc_info.value.errors()
    assert any(e["loc"] == ("confidence",) for e in errors)


# ---------------------------------------------------------------------------
# Test 7: Attempt cap — 3 pre-existing → AttemptCapExceeded, no LLM call
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_attempt_cap_enforced(db: AsyncSession) -> None:
    """3 pre-existing attempts → generate_fix raises AttemptCapExceeded without calling LLM."""
    await truncate_all(db)
    user = await _create_user(db)
    repo = await _create_repo(db, user)
    run = await _create_run(db, repo)

    for n in range(1, 4):
        await _insert_attempt(db, run, number=n)

    mock_llm = AsyncMock()

    with (
        patch("app.subagents.fix_generator.LLMClient.complete", mock_llm),
        pytest.raises(AttemptCapExceeded),
    ):
        await generate_fix(
            run=run,
            diagnosis_summary="test",
            prior_attempt=None,
            db=db,
        )

    mock_llm.assert_not_called()


# ---------------------------------------------------------------------------
# Test 8: Attempt number increments correctly
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_attempt_number_increments(db: AsyncSession) -> None:
    """2 pre-existing attempts → new attempt gets attempt_number=3."""
    await truncate_all(db)
    user = await _create_user(db)
    repo = await _create_repo(db, user)
    run = await _create_run(db, repo)

    await _insert_attempt(db, run, number=1)
    await _insert_attempt(db, run, number=2)

    with patch(
        "app.subagents.fix_generator.LLMClient.complete",
        new_callable=AsyncMock,
        return_value=_VALID_LLM_RESPONSE,
    ):
        attempt = await generate_fix(
            run=run,
            diagnosis_summary="test retry scenario",
            prior_attempt=None,
            db=db,
        )

    assert attempt.attempt_number == 3
    assert attempt.verification_status == "pending"

    result = await db.execute(select(Attempt).where(Attempt.run_id == run.id))
    all_attempts = result.scalars().all()
    assert len(all_attempts) == 3
    numbers = sorted(a.attempt_number for a in all_attempts)
    assert numbers == [1, 2, 3]


# ---------------------------------------------------------------------------
# Test 9: .github/workflows/ patch STILL raises PatchRejected after refactor
#         (security invariant must survive the LowConfidenceSkip addition)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_workflow_patch_still_hard_rejected(db: AsyncSession) -> None:
    """
    Security invariant: even after the LowConfidenceSkip refactor, a patch that
    targets .github/workflows/ with confidence >= LOW_CONFIDENCE_THRESHOLD must
    still raise PatchRejected (not LowConfidenceSkip).  No Attempt inserted.
    """
    await truncate_all(db)
    user = await _create_user(db)
    repo = await _create_repo(db, user)
    run = await _create_run(db, repo)

    workflow_patch = (
        "--- a/.github/workflows/ci.yml\n"
        "+++ b/.github/workflows/ci.yml\n"
        "@@ -1,1 +1,1 @@\n"
        "-python-version: '3.11'\n"
        "+python-version: '2.7'\n"
    )
    # confidence=55 is above LOW_CONFIDENCE_THRESHOLD (30), so LowConfidenceSkip
    # must NOT fire — PatchRejected must be raised by _validate_patch instead.
    bad_response = {
        **_VALID_LLM_RESPONSE,
        "content": json.dumps(
            {"patch": workflow_patch, "confidence": 55, "strategy_notes": None}
        ),
    }

    with (
        patch(
            "app.subagents.fix_generator.LLMClient.complete",
            new_callable=AsyncMock,
            return_value=bad_response,
        ),
        pytest.raises(PatchRejected) as exc_info,
    ):
        await generate_fix(
            run=run,
            diagnosis_summary="test",
            prior_attempt=None,
            db=db,
        )

    # Confirm the message identifies the blocked prefix, not a confidence issue.
    assert "blocked prefix" in str(exc_info.value).lower()

    # No Attempt row inserted.
    result = await db.execute(select(Attempt).where(Attempt.run_id == run.id))
    assert result.scalars().all() == []


# ---------------------------------------------------------------------------
# Test 10: Zero-confidence empty patch → LowConfidenceSkip, no Attempt inserted
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_zero_confidence_noop_triggers_low_confidence_skip(db: AsyncSession) -> None:
    """
    LLM returns {"patch": "", "confidence": 0, "strategy_notes": "insufficient data"}
    → LowConfidenceSkip raised (not PatchRejected, not FixGenerationError).
    No Attempt row inserted.
    """
    await truncate_all(db)
    user = await _create_user(db)
    repo = await _create_repo(db, user)
    run = await _create_run(db, repo)

    noop_response = {
        **_VALID_LLM_RESPONSE,
        "content": json.dumps(
            {
                "patch": "",
                "confidence": 0,
                "strategy_notes": "insufficient data to determine cause",
            }
        ),
    }

    with (
        patch(
            "app.subagents.fix_generator.LLMClient.complete",
            new_callable=AsyncMock,
            return_value=noop_response,
        ),
        pytest.raises(LowConfidenceSkip) as exc_info,
    ):
        await generate_fix(
            run=run,
            diagnosis_summary="Error type: Not discernible; logs terminate after checkout.",
            prior_attempt=None,
            db=db,
        )

    # strategy_notes should be surfaced in the exception message.
    assert "insufficient data" in str(exc_info.value)

    # No Attempt row inserted — this is the key invariant.
    result = await db.execute(select(Attempt).where(Attempt.run_id == run.id))
    assert result.scalars().all() == []


# ---------------------------------------------------------------------------
# Test 11: confidence < LOW_CONFIDENCE_THRESHOLD → LowConfidenceSkip, no Attempt
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_low_confidence_patch_triggers_low_confidence_skip(db: AsyncSession) -> None:
    """
    LLM returns a syntactically valid patch with confidence < LOW_CONFIDENCE_THRESHOLD.
    → LowConfidenceSkip raised. No Attempt row inserted.
    The patch itself is valid (passes _validate_patch) — the gate fires purely
    on confidence, not on patch content.
    """
    await truncate_all(db)
    user = await _create_user(db)
    repo = await _create_repo(db, user)
    run = await _create_run(db, repo)

    # Confidence is below threshold; patch is otherwise valid.
    low_conf_confidence = LOW_CONFIDENCE_THRESHOLD - 1  # e.g. 29
    low_conf_response = {
        **_VALID_LLM_RESPONSE,
        "content": json.dumps(
            {
                "patch": _VALID_PATCH,
                "confidence": low_conf_confidence,
                "strategy_notes": "guessing based on no evidence",
            }
        ),
    }

    with (
        patch(
            "app.subagents.fix_generator.LLMClient.complete",
            new_callable=AsyncMock,
            return_value=low_conf_response,
        ),
        pytest.raises(LowConfidenceSkip) as exc_info,
    ):
        await generate_fix(
            run=run,
            diagnosis_summary="Some vague diagnosis with no file/line info.",
            prior_attempt=None,
            db=db,
        )

    # Exception message should mention the confidence value.
    assert str(low_conf_confidence) in str(exc_info.value)

    # No Attempt row inserted.
    result = await db.execute(select(Attempt).where(Attempt.run_id == run.id))
    assert result.scalars().all() == []
