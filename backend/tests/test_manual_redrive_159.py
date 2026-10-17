"""
Manual replay for failed runs — acceptance criteria for issue #159.

Issue #159 ("No redrive: add manual replay for failed runs"): once a run
fails or exhausts its fix attempts, operators must be able to re-queue it
through the pipeline from the dashboard without pushing a dummy commit.

Delivery: POST /runs/{run_id}/retry (Feature 1, one-click retry) clones a
settled run into a fresh pending child linked by parent_run_id +
is_retry_child and re-dispatches the orchestrator pipeline. The dashboard
surfaces the action per row (runs list) and on the run detail page, and
lands on the dispatched child via the returned id.

This file pins the issue's acceptance scenario end to end — a source run
carrying real exhaust history (failed attempts, fallback comment state,
tracking issue) replays into a clean pending child — rather than
re-covering the retry endpoint's guard matrix (409/429/tenant isolation),
which lives in tests/test_run_retry.py.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Attempt, Repo, Run, User
from tests.conftest import truncate_all


async def _seed_user_repo(db: AsyncSession, github_id: int, username: str) -> tuple[User, Repo]:
    user = User(
        id=uuid.uuid4(),
        github_id=github_id,
        github_username=username,
        access_token=None,
        role="user",
    )
    db.add(user)
    await db.commit()

    repo = Repo(user_id=user.id, owner=f"{username}-org", name="app")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)
    return user, repo


def _exhausted_source_run(repo: Repo) -> Run:
    """A run that failed and exhausted its fix attempts, as in issue #159."""
    return Run(
        repo_id=repo.id,
        github_run_id=159000111,
        github_delivery_id="delivery-159-exhausted",
        head_sha="a" * 40,
        head_branch="main",
        status="fallback_commented",
        conclusion="failure",
        diagnosis_summary="root cause: missing await on pool acquire",
        failure_reason="attempts exhausted",
        fallback_issue_url="https://github.com/o/r/issues/9",
        fallback_issue_number=9,
    )
