"""
Phase 3 — deterministic ModuleNotFoundError fallback tests (BLOCKER-2 + FG-05).

When the diagnosis names ``ModuleNotFoundError: No module named 'X'`` and ``X``
is importable from the repo root, the deterministic helper
``_module_not_found_path_fix`` returns a unified diff that creates a top-level
``conftest.py`` with the canonical ``sys.path.insert`` shim. The orchestrator
skips the LLM call entirely on the deterministic path; ``generate_fix`` is
verified to do this end-to-end (with the LLMClient patched to assert it is
never invoked).

This file mixes sync pure-function tests with one async integration test that
needs the real DB. The integration test skips cleanly without
``TEST_DATABASE_URL`` — same pattern as the rest of the suite.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Attempt, Repo, Run, RunStep, User
from app.subagents.fix_generator import (
    _module_not_found_path_fix,
    generate_fix,
)
from tests.conftest import truncate_all


# ---------------------------------------------------------------------------
# Pure-function tests (sync, no DB)
# ---------------------------------------------------------------------------


def test_module_not_found_for_app_returns_conftest() -> None:
    """``ModuleNotFoundError: No module named 'app'`` → conftest.py patch with sys.path shim."""
    patch_text = _module_not_found_path_fix(
        "ModuleNotFoundError: No module named 'app' in test_foo.py:3"
    )
    assert patch_text is not None
    assert isinstance(patch_text, str)
    assert patch_text.strip()  # non-empty
    assert "conftest.py" in patch_text
    # The canonical sys.path line — the LLM is trained to match this exact
    # shape via the system-prompt worked example, so it must appear verbatim.
    assert "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))" in patch_text


def test_module_not_found_for_dotted_module_returns_conftest() -> None:
    """``ModuleNotFoundError: No module named 'src.utils'`` → conftest.py patch (top-level package is ``src``)."""
    patch_text = _module_not_found_path_fix(
        "ModuleNotFoundError: No module named 'src.utils'"
    )
    assert patch_text is not None
    assert "conftest.py" in patch_text
    assert "sys.path.insert" in patch_text


def test_no_module_not_found_returns_none() -> None:
    """A non-ModuleNotFoundError diagnosis → None (caller falls through to LLM)."""
    patch_text = _module_not_found_path_fix(
        "AssertionError: 1 != 2 in test_foo.py:10"
    )
    assert patch_text is None


def test_module_not_found_for_stdlib_safety_net() -> None:
    """
    ``ModuleNotFoundError: No module named 'os'`` → ``None``.

    Design decision (documented in ``_module_not_found_path_fix`` docstring):
    the stdlib safety net **rejects** well-known stdlib module names. Without
    this guard, a missing stdlib import (which can never be fixed by a
    conftest shim) would be patched with a useless ``conftest.py`` and waste
    an attempt. Trade-off: a small false-negative rate for missing
    third-party packages that happen to share a name with a stdlib module
    (e.g. an internal project also called ``logging``). Accepted as a known
    limitation; the LLM-driven retry path will correct the wasted attempt.

    The allowlist is intentionally small — see ``_STDLIB_MODULE_HINTS``.
    """
    patch_text = _module_not_found_path_fix(
        "ModuleNotFoundError: No module named 'os'"
    )
    assert patch_text is None


def test_module_not_found_double_quoted_returns_conftest() -> None:
    """Module name may be quoted with single OR double quotes — both must match."""
    patch_text = _module_not_found_path_fix(
        'ModuleNotFoundError: No module named "app"'
    )
    assert patch_text is not None
    assert "conftest.py" in patch_text


# ---------------------------------------------------------------------------
# Integration test — generate_fix must bypass the LLM on the deterministic path
# ---------------------------------------------------------------------------


async def _create_user(db: AsyncSession) -> User:
    user = User(
        github_id=int(uuid.uuid4().int % 1_000_000_000 + 100_000_000),
        github_username="deterministic-test-user",
        access_token="fake_deterministic_token",
    )
    db.add(user)
    await db.commit()
    return user


async def _create_repo(db: AsyncSession, user: User) -> Repo:
    repo = Repo(
        user_id=user.id,
        owner="det-test-org",
        name="det-test-repo",
        default_branch="main",
    )
    db.add(repo)
    await db.commit()
    return repo


async def _create_run(
    db: AsyncSession, repo: Repo, diagnosis_summary: str, status: str = "fix_generation"
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


@pytest.mark.anyio
async def test_generate_fix_uses_deterministic_when_match(db: AsyncSession) -> None:
    """
    End-to-end: a diagnosis containing ``ModuleNotFoundError: No module named 'app'``
    causes ``generate_fix`` to skip the LLM call entirely and insert an Attempt
    with the canonical ``conftest.py`` patch and ``strategy_notes`` starting
    with ``"deterministic"``.

    The LLMClient is patched to raise ``AssertionError`` if invoked — a
    defensive guard so a regression in the bypass logic is caught loudly
    (the test fails) instead of silently falling through to the LLM path.
    """
    await truncate_all(db)
    user = await _create_user(db)
    repo = await _create_repo(db, user)
    diagnosis = (
        "ModuleNotFoundError: No module named 'app' "
        "in tests/test_smoke.py:3\n"
    )
    run = await _create_run(db, repo, diagnosis_summary=diagnosis)

    # Patching LLMClient.complete to raise if called — proves the deterministic
    # fast-path bypasses the LLM entirely. side_effect=AsyncMock(side_effect=...)
    # raises the supplied exception when the coroutine is awaited.
    never_called = AsyncMock(
        side_effect=AssertionError("LLM should not have been called")
    )

    with patch("app.subagents.fix_generator.LLMClient.complete", never_called):
        attempt = await generate_fix(
            run=run,
            diagnosis_summary=diagnosis,
            prior_attempt=None,
            db=db,
        )

    # Attempt row exists and contains the canonical patch.
    assert attempt.attempt_number == 1
    assert "conftest.py" in attempt.patch_text
    assert "sys.path.insert" in attempt.patch_text
    assert attempt.confidence_score == 95
    assert (attempt.strategy_notes or "").startswith("deterministic")

    # No additional Attempt rows created.
    result = await db.execute(select(Attempt).where(Attempt.run_id == run.id))
    all_attempts = result.scalars().all()
    assert len(all_attempts) == 1
    assert all_attempts[0].id == attempt.id

    # RunStep trace is named with the distinct deterministic step label.
    steps_result = await db.execute(select(RunStep).where(RunStep.run_id == run.id))
    steps = steps_result.scalars().all()
    assert any(s.step_name == "fix_generator_deterministic" for s in steps)
    # And the regular "fix_generator" step is NOT present — the LLM path was skipped.
    assert not any(s.step_name == "fix_generator" for s in steps)
