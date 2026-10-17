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


async def _seed_user_repo(
    db: AsyncSession, github_id: int, username: str
) -> tuple[User, Repo]:
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


@pytest.mark.asyncio
async def test_exhausted_run_replays_into_clean_pending_child(
    db: AsyncSession, make_auth_client
):
    """Issue #159: a run that exhausted its fix attempts can be replayed.

    The source carries three failed attempts, a fallback verdict and a
    tracking issue. POST /runs/{id}/retry must answer 201 with a pending
    child linked to the source and dispatch it through the hosting adapter
    — the operator re-queues the failure without pushing a dummy commit.
    """
    await truncate_all(db)
    user, repo = await _seed_user_repo(db, 15901, "redrive_user")

    source = _exhausted_source_run(repo)
    db.add(source)
    await db.commit()
    await db.refresh(source)
    source_id = source.id

    for n in (1, 2, 3):
        db.add(
            Attempt(
                run_id=source_id,
                attempt_number=n,
                patch_text=f"diff --git attempt {n}",
                verification_status="failed",
                failure_reason=f"attempt {n} failed sandbox CI",
            )
        )
    await db.commit()

    mock_adapter = AsyncMock()
    async with make_auth_client(user.id) as client:
        with patch(
            "app.adapters.hosting.get_hosting_adapter",
            new=AsyncMock(return_value=mock_adapter),
        ):
            resp = await client.post(f"/runs/{source_id}/retry")

    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "pending"
    assert data["parent_run_id"] == str(source_id)

    child_id = uuid.UUID(data["id"])
    child = (await db.execute(select(Run).where(Run.id == child_id))).scalar_one()
    assert child.parent_run_id == source_id
    assert child.is_retry_child is True
    assert child.status == "pending"

    mock_adapter.schedule_pipeline.assert_awaited_once()
    assert mock_adapter.schedule_pipeline.await_args.args[0] == child_id

    # The child starts clean: no verdict, artefact, PR coordinate, tracking
    # issue, or fabricated GitHub identity is carried from the exhaust path.
    for reset in (
        "diagnosis_summary",
        "failure_reason",
        "pr_url",
        "pr_number",
        "pr_branch",
        "final_summary",
        "fallback_issue_url",
        "fallback_issue_number",
        "github_run_id",
        "github_delivery_id",
    ):
        assert getattr(child, reset) is None, f"{reset} leaked into replay child"
    child_attempts = await db.scalar(
        select(func.count(Attempt.id)).where(Attempt.run_id == child_id)
    )
    assert child_attempts == 0

    # The failed source run is untouched: still settled, still carrying its
    # own three attempts and its GitHub identity.
    src = (await db.execute(select(Run).where(Run.id == source_id))).scalar_one()
    assert src.status == "fallback_commented"
    assert src.github_run_id == 159000111
    src_attempts = await db.scalar(
        select(func.count(Attempt.id)).where(Attempt.run_id == source_id)
    )
    assert src_attempts == 3


@pytest.mark.asyncio
async def test_transient_error_run_replays_and_child_trace_resolves(
    db: AsyncSession, make_auth_client
):
    """Issue #159: a transiently failed run replays and the child is traceable.

    The dashboard lands on the dispatched child via the POST response id, so
    the child trace must resolve with its parent linkage intact.
    """
    await truncate_all(db)
    user, repo = await _seed_user_repo(db, 15902, "redrive_transient_user")

    source = Run(
        repo_id=repo.id,
        github_run_id=159000222,
        github_delivery_id="delivery-159-transient",
        head_sha="b" * 40,
        head_branch="main",
        status="error",
        conclusion="failure",
        failure_reason="sandbox verification timed out",
    )
    db.add(source)
    await db.commit()
    await db.refresh(source)
    source_id = source.id

    mock_adapter = AsyncMock()
    async with make_auth_client(user.id) as client:
        with patch(
            "app.adapters.hosting.get_hosting_adapter",
            new=AsyncMock(return_value=mock_adapter),
        ):
            resp = await client.post(f"/runs/{source_id}/retry")
        assert resp.status_code == 201
        child_id = resp.json()["id"]

        trace = await client.get(f"/runs/{child_id}/trace")

    assert trace.status_code == 200
    body = trace.json()
    assert body["run"]["status"] == "pending"
    assert body["run"]["parent_run_id"] == str(source_id)
    assert body["parent"]["id"] == str(source_id)
    mock_adapter.schedule_pipeline.assert_awaited_once()
