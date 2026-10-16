"""Concurrent attempt-cap integration test for fix_generator (Issue #114).

The fix_generator attempt-cap check on current main runs WITHOUT a
SELECT FOR UPDATE on the Run row (issue #107 removed the lock because
holding it across LLM inference serializes concurrent workers).
Atomicity is instead fenced by the uq_attempt_run_number unique
constraint on Insert: a concurrent committer wins the (run_id,
attempt_number) tuple, and the loser's INSERT raises IntegrityError,
which fix_generator catches and surfaces as AttemptClaimLost.

This file proves that fence holds under concurrent attempts against
real PostgreSQL by running two `generate_fix` calls against the same
Run and asserting:

  1. Both calls finish.
  2. Exactly one succeeds with an Attempt row.
  3. Exactly one fails with AttemptClaimLost (the unique-constraint
     loser). With zero priors and a cap of 3, neither call should
     see existing_count >= cap, so neither raises AttemptCapExceeded.
  4. The DB ends with exactly one attempt row at attempt_number=1.

@ pytest.mark.db: requires a real test database (TEST_DATABASE_URL).
Hermetic subset — collection only.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Attempt, Run
from app.subagents.fix_generator import AttemptClaimLost, generate_fix

# Patch _max_attempts so the cap is independent of HAUNTER_MAX_ATTEMPTS.
# 3 is the production default. With zero priors seeded, both concurrent
# calls see existing_count=0 (< cap) and proceed to INSERT. Exactly one
# wins the uq_attempt_run_number tuple; the other raises AttemptClaimLost.
_PINNED_CAP = 3


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

_DIAGNOSIS = "ImportError: cannot import foo in app/models.py:10"


def _new_run() -> Run:
    return Run(id=uuid.uuid4(), diagnosis_summary=_DIAGNOSIS)


@patch("app.subagents.fix_generator._max_attempts", return_value=_PINNED_CAP)
async def test_two_concurrent_generate_fix_calls_do_not_exceed_cap(
    _patched,
    db: AsyncSession,
) -> None:
    """Two concurrent generate_fix calls race the (run_id, attempt_number)
    INSERT. One wins the unique tuple; the other raises AttemptClaimLost.

    Auto-marked @pytest.mark.db via the db fixture (see conftest.py).
    Skipped when TEST_DATABASE_URL is unset.
    """
    from tests.conftest import async_session_maker, truncate_all

    # Seed one Run with zero priors, both calls will use this id.
    run = _new_run()
    db.add(run)
    await db.commit()
    await db.refresh(run)
    run_id = run.id

    async def call_generate_fix() -> object:
        # Each concurrent call gets its own session so both can be in
        # flight at the same time and race the INSERT.
        async with async_session_maker() as session:
            try:
                # Re-load the Run in this session so the Insert targets
                # a row this session can see.
                result = await session.execute(select(Run).where(Run.id == run_id))
                local_run = result.scalar_one()

                with patch(
                    "app.subagents.fix_generator.LLMClient.complete",
                    new_callable=AsyncMock,
                    return_value=_VALID_LLM_RESPONSE,
                ):
                    return await generate_fix(
                        run=local_run,
                        diagnosis_summary=_DIAGNOSIS,
                        prior_attempt=None,
                        db=session,
                    )
            finally:
                try:
                    await truncate_all(session)
                except Exception:
                    pass
                await session.rollback()
                await session.close()

    first, second = await asyncio.gather(
        call_generate_fix(),
        call_generate_fix(),
        return_exceptions=True,
    )

    outcomes = [first, second]
    successes = [o for o in outcomes if isinstance(o, Attempt)]
    claim_lost = [o for o in outcomes if isinstance(o, AttemptClaimLost)]
    unexpected = [
        o
        for o in outcomes
        if not isinstance(o, (Attempt, AttemptClaimLost))
    ]
    assert not unexpected, f"unexpected exceptions: {unexpected!r}"
    # With zero priors, neither call sees existing_count >= cap. The
    # race resolves at the unique-constraint fence: one wins, one loses.
    assert len(successes) == 1, (
        f"expected exactly one success, got {len(successes)}: {outcomes!r}"
    )
    assert len(claim_lost) == 1, (
        f"expected exactly one AttemptClaimLost, got {len(claim_lost)}: "
        f"{outcomes!r}"
    )

    # Verify the DB state: exactly one new attempt row, no duplicate
    # attempt_number (uq_attempt_run_number caught the race exactly once).
    async with async_session_maker() as verify_session:
        count_result = await verify_session.execute(
            select(func.count()).select_from(Attempt).where(Attempt.run_id == run_id)
        )
        attempt_count = int(count_result.scalar_one())
        assert attempt_count == 1

        nums_result = await verify_session.execute(
            select(Attempt.attempt_number).where(Attempt.run_id == run_id)
        )
        nums = [row[0] for row in nums_result.all()]
        assert nums == [1], f"expected single attempt_number=1, got {nums}"
