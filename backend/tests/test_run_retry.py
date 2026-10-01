"""
One-click retry — POST /runs/{run_id}/retry (Feature 1).

Covers:
  1.  Happy path: clones a settled run into a pending child linked by
      parent_run_id + is_retry_child, and re-dispatches the orchestrator.
  2.  Clone semantics: failure coordinates copied, pipeline-owned fields reset.
  3.  404 on a non-existent run_id.
  4.  404 on another tenant's run_id (ownership oracle — never 403).
  5.  401 with no session cookie.
  6.  409 when the source run is still in flight (not settled).
  7.  409 when a child of the run is still in flight.
  8.  429 when the run already has 5 children.
  9.  Retry child is NOT an interactive PR-refinement child — it must take the
      normal PR-Writer path, never commit onto the source run's branch
      (Human Merge Gate Invariant, HAUNTER.md §1.1).
 10.  Trace exposes parent/children lineage, tenant-scoped.
 11.  resolve_workflow_run_id walks parent_run_id to recover the original CI
      workflow run id instead of fabricating one.
"""

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Attempt, Repo, Run, User
from app.orchestrator import is_pr_refinement
from app.routers.traces import _MAX_RUN_CHILDREN, build_retry_child
from app.subagents.context_gatherer import resolve_workflow_run_id
from tests.conftest import truncate_all


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _make_run(repo: Repo, *, status: str, **kwargs: object) -> Run:
    return Run(
        repo_id=repo.id,
        github_run_id=kwargs.pop("github_run_id", None),
        github_delivery_id=kwargs.pop("github_delivery_id", None),
        head_sha=kwargs.pop("head_sha", "a" * 40),
        head_branch=kwargs.pop("head_branch", "main"),
        status=status,
        conclusion=kwargs.pop("conclusion", "failure"),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1 & 2 — happy path + clone semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_clones_run_resets_to_pending_and_dispatches(
    db: AsyncSession, make_auth_client
):
    """A settled run is cloned into a pending child and the pipeline re-dispatched."""
    await truncate_all(db)
    user, repo = await _seed_user_repo(db, 9500, "retry_user")

    source = _make_run(
        repo,
        status="fallback_commented",
        github_run_id=555000111,
        github_delivery_id="delivery-source",
        diagnosis_summary="original diagnosis",
        failure_reason="attempts exhausted",
        pr_url="https://github.com/x/y/pull/7",
        pr_number=7,
        pr_branch="haunter/fix-old-1",
        final_summary="<b>old</b>",
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
    data = resp.json()

    # Cloned run exists exactly once, linked to the source.
    child_result = await db.execute(select(Run).where(Run.parent_run_id == source_id))
    children = child_result.scalars().all()
    assert len(children) == 1
    child = children[0]

    # Response body is the child.
    assert data["id"] == str(child.id)
    assert data["status"] == "pending"
    assert data["parent_run_id"] == str(source_id)

    # Lineage flag set so the orchestrator does not treat this as a PR refinement.
    assert child.is_retry_child is True

    # Failure coordinates copied; pipeline-owned fields reset.
    assert child.repo_id == source.repo_id
    assert child.head_sha == source.head_sha
    assert child.head_branch == source.head_branch
    assert child.conclusion == source.conclusion
    assert child.diagnosis_summary is None
    assert child.failure_reason is None
    assert child.pr_url is None
    assert child.pr_number is None
    assert child.pr_branch is None
    assert child.final_summary is None

    # No fabricated GitHub identity — a real workflow_run webhook must not be
    # discarded as a duplicate by the UNIQUE index on runs.github_run_id.
    assert child.github_run_id is None
    assert child.github_delivery_id is None

    # The source run is untouched.
    src = (await db.execute(select(Run).where(Run.id == source_id))).scalar_one()
    assert src.status == "fallback_commented"
    assert src.github_run_id == 555000111

    # Orchestrator re-dispatched with the child's id.
    mock_adapter.schedule_pipeline.assert_awaited_once()
    assert mock_adapter.schedule_pipeline.await_args.args[0] == child.id


@pytest.mark.asyncio
async def test_retry_of_pr_opened_run_clears_pr_coordinates(
    db: AsyncSession, make_auth_client
):
    """Retrying a pr_opened run must not carry the old PR forward."""
    await truncate_all(db)
    user, repo = await _seed_user_repo(db, 9501, "retry_pr_user")

    source = _make_run(
        repo,
        status="pr_opened",
        github_run_id=555000222,
        pr_number=42,
        pr_branch="haunter/fix-abc-1",
        pr_url="https://github.com/x/y/pull/42",
    )
    db.add(source)
    await db.commit()
    await db.refresh(source)

    mock_adapter = AsyncMock()
    async with make_auth_client(user.id) as client:
        with patch(
            "app.adapters.hosting.get_hosting_adapter",
            new=AsyncMock(return_value=mock_adapter),
        ):
            resp = await client.post(f"/runs/{source.id}/retry")

    assert resp.status_code == 201
    child = (
        await db.execute(select(Run).where(Run.parent_run_id == source.id))
    ).scalar_one()
    assert child.pr_number is None
    assert child.pr_branch is None
    assert child.pr_url is None
    # The invariant under test: opening a NEW PR, not committing onto main.
    assert is_pr_refinement(child) is False


@pytest.mark.asyncio
async def test_retry_child_of_retry_child_stays_out_of_refinement_path(
    db: AsyncSession, make_auth_client
):
    """Retrying an already-retried run keeps every descendant off the PR-refinement path.

    The first child must settle before it can itself be retried — the endpoint
    rejects a retry of an in-flight run with 409 (covered separately below).
    """
    await truncate_all(db)
    user, repo = await _seed_user_repo(db, 9502, "retry_chain_user")

    root = _make_run(repo, status="error", github_run_id=555000333)
    db.add(root)
    await db.commit()
    await db.refresh(root)

    mock_adapter = AsyncMock()
    async with make_auth_client(user.id) as client:
        with patch(
            "app.adapters.hosting.get_hosting_adapter",
            new=AsyncMock(return_value=mock_adapter),
        ):
            first = await client.post(f"/runs/{root.id}/retry")
        assert first.status_code == 201
        child_one_id = uuid.UUID(first.json()["id"])

        # Settle the first child the way the orchestrator would.
        child_one = (await db.execute(select(Run).where(Run.id == child_one_id))).scalar_one()
        child_one.status = "fallback_commented"
        await db.commit()

        with patch(
            "app.adapters.hosting.get_hosting_adapter",
            new=AsyncMock(return_value=mock_adapter),
        ):
            second = await client.post(f"/runs/{child_one_id}/retry")

    assert second.status_code == 201
    child_two = (
        await db.execute(select(Run).where(Run.parent_run_id == child_one_id))
    ).scalar_one()
    assert child_two.is_retry_child is True
    assert is_pr_refinement(child_two) is False


# ---------------------------------------------------------------------------
# 3, 4, 5 — error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_nonexistent_run_404(db: AsyncSession, make_auth_client):
    """Unknown run_id → 404, and no pipeline is dispatched."""
    await truncate_all(db)
    user, _repo = await _seed_user_repo(db, 9516, "retry_missing_user")

    mock_adapter = AsyncMock()
    async with make_auth_client(user.id) as client:
        with patch(
            "app.adapters.hosting.get_hosting_adapter",
            new=AsyncMock(return_value=mock_adapter),
        ):
            resp = await client.post(f"/runs/{uuid.uuid4()}/retry")

    assert resp.status_code == 404
    mock_adapter.schedule_pipeline.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_other_users_run_404(db: AsyncSession, make_auth_client):
    """Another tenant's run_id → 404 (no existence oracle, never 403)."""
    await truncate_all(db)
    victim, victim_repo = await _seed_user_repo(db, 9503, "retry_victim")
    attacker, _ = await _seed_user_repo(db, 9504, "retry_attacker")

    victim_run = _make_run(victim_repo, status="error", github_run_id=555000444)
    db.add(victim_run)
    await db.commit()
    await db.refresh(victim_run)

    mock_adapter = AsyncMock()
    async with make_auth_client(attacker.id) as client:
        with patch(
            "app.adapters.hosting.get_hosting_adapter",
            new=AsyncMock(return_value=mock_adapter),
        ):
            resp = await client.post(f"/runs/{victim_run.id}/retry")

    assert resp.status_code == 404
    mock_adapter.schedule_pipeline.assert_not_awaited()
    assert victim.id != attacker.id


@pytest.mark.asyncio
async def test_retry_unauthenticated_401(client):
    """No session cookie → 401."""
    resp = await client.post(f"/runs/{uuid.uuid4()}/retry")
    assert resp.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "in_flight_status",
    ["pending", "context_gathering", "flake_verification", "fix_generation", "verification", "pending_pr", "fallback"],
)
async def test_retry_in_flight_run_409(
    db: AsyncSession, make_auth_client, in_flight_status: str
):
    """Cloning a run the orchestrator is still mutating → 409."""
    await truncate_all(db)
    user, repo = await _seed_user_repo(db, 9505, "retry_inflight_user")

    run = _make_run(repo, status=in_flight_status, github_run_id=555000555)
    db.add(run)
    await db.commit()
    await db.refresh(run)

    mock_adapter = AsyncMock()
    async with make_auth_client(user.id) as client:
        with patch(
            "app.adapters.hosting.get_hosting_adapter",
            new=AsyncMock(return_value=mock_adapter),
        ):
            resp = await client.post(f"/runs/{run.id}/retry")

    assert resp.status_code == 409
    mock_adapter.schedule_pipeline.assert_not_awaited()
    count = await db.scalar(
        select(func.count(Run.id)).where(Run.parent_run_id == run.id)
    )
    assert count == 0


@pytest.mark.asyncio
async def test_retry_while_child_in_flight_409(db: AsyncSession, make_auth_client):
    """A second retry is rejected while an existing child is still running."""
    await truncate_all(db)
    user, repo = await _seed_user_repo(db, 9506, "retry_busy_user")

    source = _make_run(repo, status="error", github_run_id=555000666)
    db.add(source)
    await db.commit()
    await db.refresh(source)

    busy_child = Run(
        repo_id=repo.id,
        parent_run_id=source.id,
        is_retry_child=True,
        head_sha=source.head_sha,
        head_branch=source.head_branch,
        status="fix_generation",
        conclusion="failure",
    )
    db.add(busy_child)
    await db.commit()

    mock_adapter = AsyncMock()
    async with make_auth_client(user.id) as client:
        with patch(
            "app.adapters.hosting.get_hosting_adapter",
            new=AsyncMock(return_value=mock_adapter),
        ):
            resp = await client.post(f"/runs/{source.id}/retry")

    assert resp.status_code == 409
    mock_adapter.schedule_pipeline.assert_not_awaited()
    count = await db.scalar(
        select(func.count(Run.id)).where(Run.parent_run_id == source.id)
    )
    assert count == 1


@pytest.mark.asyncio
async def test_retry_after_child_settles_succeeds(db: AsyncSession, make_auth_client):
    """Once the existing child settles, a new retry is permitted."""
    await truncate_all(db)
    user, repo = await _seed_user_repo(db, 9507, "retry_settled_user")

    source = _make_run(repo, status="error", github_run_id=555000777)
    db.add(source)
    await db.commit()
    await db.refresh(source)

    settled_child = Run(
        repo_id=repo.id,
        parent_run_id=source.id,
        is_retry_child=True,
        head_sha=source.head_sha,
        head_branch=source.head_branch,
        status="fallback_commented",
        conclusion="failure",
    )
    db.add(settled_child)
    await db.commit()

    mock_adapter = AsyncMock()
    async with make_auth_client(user.id) as client:
        with patch(
            "app.adapters.hosting.get_hosting_adapter",
            new=AsyncMock(return_value=mock_adapter),
        ):
            resp = await client.post(f"/runs/{source.id}/retry")

    assert resp.status_code == 201
    mock_adapter.schedule_pipeline.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_retries_of_same_run_produce_one_child(
    db: AsyncSession, make_auth_client
):
    """Two simultaneous retries must not both pass the guards and insert (TOCTOU).

    The source row is locked with SELECT FOR UPDATE, so the second request waits
    for the first to commit, then observes the freshly created in-flight child
    and is rejected. Without the lock both requests read child_count == 0 and
    both insert, yielding two children and two 201s.
    """
    await truncate_all(db)
    user, repo = await _seed_user_repo(db, 9517, "retry_race_user")

    source = _make_run(repo, status="error", github_run_id=555002222)
    db.add(source)
    await db.commit()
    await db.refresh(source)

    async def retry_once(client):
        # schedule_pipeline is a no-op: the child stays `pending`, which is
        # what makes the second request's in-flight guard meaningful.
        adapter = AsyncMock()
        with patch(
            "app.adapters.hosting.get_hosting_adapter",
            new=AsyncMock(return_value=adapter),
        ):
            return await client.post(f"/runs/{source.id}/retry")

    async with make_auth_client(user.id) as c1, make_auth_client(user.id) as c2:
        first, second = await asyncio.gather(retry_once(c1), retry_once(c2))

    assert sorted([first.status_code, second.status_code]) == [201, 409]

    count = await db.scalar(
        select(func.count(Run.id)).where(Run.parent_run_id == source.id)
    )
    assert count == 1


@pytest.mark.asyncio
async def test_retry_child_limit_429(db: AsyncSession, make_auth_client):
    """The 5-children-per-run cap is enforced."""
    await truncate_all(db)
    user, repo = await _seed_user_repo(db, 9508, "retry_cap_user")

    source = _make_run(repo, status="error", github_run_id=555000888)
    db.add(source)
    await db.commit()
    await db.refresh(source)

    for _ in range(_MAX_RUN_CHILDREN):
        db.add(
            Run(
                repo_id=repo.id,
                parent_run_id=source.id,
                is_retry_child=True,
                head_sha=source.head_sha,
                head_branch=source.head_branch,
                status="error",
                conclusion="failure",
            )
        )
    await db.commit()

    mock_adapter = AsyncMock()
    async with make_auth_client(user.id) as client:
        with patch(
            "app.adapters.hosting.get_hosting_adapter",
            new=AsyncMock(return_value=mock_adapter),
        ):
            resp = await client.post(f"/runs/{source.id}/retry")

    assert resp.status_code == 429
    mock_adapter.schedule_pipeline.assert_not_awaited()


# ---------------------------------------------------------------------------
# 10 — trace lineage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_exposes_parent_and_children(db: AsyncSession, make_auth_client):
    """GET /runs/{id}/trace returns the retry thread."""
    await truncate_all(db)
    user, repo = await _seed_user_repo(db, 9509, "lineage_user")

    source = _make_run(repo, status="error", github_run_id=555000999)
    db.add(source)
    await db.commit()
    await db.refresh(source)

    child = build_retry_child(source)
    db.add(child)
    await db.commit()
    await db.refresh(child)

    async with make_auth_client(user.id) as client:
        parent_view = await client.get(f"/runs/{source.id}/trace")
        child_view = await client.get(f"/runs/{child.id}/trace")

    assert parent_view.status_code == 200
    pdata = parent_view.json()
    assert pdata["parent"] is None
    assert [c["id"] for c in pdata["children"]] == [str(child.id)]
    assert pdata["run"]["parent_run_id"] is None

    assert child_view.status_code == 200
    cdata = child_view.json()
    assert cdata["parent"]["id"] == str(source.id)
    assert cdata["children"] == []
    assert cdata["run"]["parent_run_id"] == str(source.id)


@pytest.mark.asyncio
async def test_trace_children_tenant_scoped(db: AsyncSession, make_auth_client):
    """A child whose repo belongs to another tenant is never surfaced."""
    await truncate_all(db)
    user, _repo_a = await _seed_user_repo(db, 9510, "lineage_owner")
    _other, repo_b = await _seed_user_repo(db, 9511, "lineage_other")

    foreign_parent = _make_run(repo_b, status="error", github_run_id=555001000)
    db.add(foreign_parent)
    await db.commit()
    await db.refresh(foreign_parent)

    # A row that points parent_run_id at another tenant's run — exactly the
    # corrupted-data case the SQL-level tenant filter must survive.
    hostile_child = Run(
        repo_id=repo_b.id,
        parent_run_id=foreign_parent.id,
        is_retry_child=True,
        head_sha="b" * 40,
        head_branch="main",
        status="error",
        conclusion="failure",
    )
    db.add(hostile_child)
    await db.commit()

    async with make_auth_client(user.id) as client:
        resp = await client.get(f"/runs/{foreign_parent.id}/trace")

    # The parent itself is not visible to this caller at all.
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 11 — workflow run id resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_workflow_run_id_walks_to_root(db: AsyncSession):
    """A retry child recovers the original CI workflow run id from its ancestry."""
    await truncate_all(db)
    _user, repo = await _seed_user_repo(db, 9512, "resolve_user")

    root = _make_run(repo, status="error", github_run_id=777888999)
    db.add(root)
    await db.commit()
    await db.refresh(root)

    child_one = build_retry_child(root)
    db.add(child_one)
    await db.commit()
    await db.refresh(child_one)

    child_two = build_retry_child(child_one)
    db.add(child_two)
    await db.commit()
    await db.refresh(child_two)

    # Root resolves from its own column.
    assert await resolve_workflow_run_id(root, db) == 777888999
    # One retry deep: walks to the parent.
    assert await resolve_workflow_run_id(child_one, db) == 777888999
    # Retry of a retry: still resolves.
    assert await resolve_workflow_run_id(child_two, db) == 777888999


@pytest.mark.asyncio
async def test_resolve_workflow_run_id_none_without_reachable_ancestor(
    db: AsyncSession,
):
    """Returns None (not a bogus id) when no ancestor carries a workflow run id."""
    await truncate_all(db)
    _user, repo = await _seed_user_repo(db, 9513, "resolve_none_user")

    # A run with no workflow run id and no parent — e.g. a retry whose whole
    # ancestry predates the nullable column.
    orphan = Run(
        repo_id=repo.id,
        github_run_id=None,
        head_sha="c" * 40,
        head_branch="main",
        status="error",
        conclusion="failure",
    )
    db.add(orphan)
    await db.commit()
    await db.refresh(orphan)

    assert await resolve_workflow_run_id(orphan, db) is None


# ---------------------------------------------------------------------------
# Pure constructor
# ---------------------------------------------------------------------------


def test_build_retry_child_is_pure_and_no_io():
    """build_retry_child constructs the clone without touching the DB."""
    source = Run(
        id=uuid.uuid4(),
        repo_id=uuid.uuid4(),
        github_run_id=12345,
        github_delivery_id="delivery-1",
        head_sha="d" * 40,
        head_branch="feature/x",
        status="pr_opened",
        conclusion="failure",
        pr_number=3,
        pr_branch="haunter/fix-a-1",
    )

    child = build_retry_child(source)

    assert child.id is not source.id
    assert child.parent_run_id == source.id
    assert child.is_retry_child is True
    assert child.status == "pending"
    assert child.github_run_id is None
    assert child.github_delivery_id is None
    assert child.pr_number is None
    assert child.pr_branch is None
    # Source run must not be mutated.
    assert source.status == "pr_opened"
    assert source.pr_number == 3


@pytest.mark.asyncio
async def test_retry_preserves_distinct_attempts_per_child(
    db: AsyncSession, make_auth_client
):
    """Retry children get their own Attempt rows — no collision on (run_id, attempt_number)."""
    await truncate_all(db)
    user, repo = await _seed_user_repo(db, 9514, "retry_attempt_user")

    source = _make_run(repo, status="fallback_commented", github_run_id=555002000)
    db.add(source)
    await db.commit()
    await db.refresh(source)

    db.add(
        Attempt(
            run_id=source.id,
            attempt_number=1,
            patch_text="--- a/x\n+++ b/x\n",
            confidence_score=40,
            verification_status="fail",
        )
    )
    await db.commit()

    mock_adapter = AsyncMock()
    async with make_auth_client(user.id) as client:
        with patch(
            "app.adapters.hosting.get_hosting_adapter",
            new=AsyncMock(return_value=mock_adapter),
        ):
            resp = await client.post(f"/runs/{source.id}/retry")

    assert resp.status_code == 201
    child_id = uuid.UUID(resp.json()["id"])

    child_attempt_count = await db.scalar(
        select(func.count(Attempt.id)).where(Attempt.run_id == child_id)
    )
    assert child_attempt_count == 0
    # The source run's attempt history is preserved.
    source_attempt_count = await db.scalar(
        select(func.count(Attempt.id)).where(Attempt.run_id == source.id)
    )
    assert source_attempt_count == 1


@pytest.mark.asyncio
async def test_retry_dispatch_failure_does_not_leave_phantom_run(
    db: AsyncSession, make_auth_client
):
    """If scheduling fails, the child is marked terminal — never a stuck pending row."""
    await truncate_all(db)
    user, repo = await _seed_user_repo(db, 9515, "retry_dispatch_fail_user")

    source = _make_run(repo, status="error", github_run_id=555002111)
    db.add(source)
    await db.commit()
    await db.refresh(source)

    failing_adapter = AsyncMock()
    failing_adapter.schedule_pipeline.side_effect = RuntimeError("lambda unavailable")

    async with make_auth_client(user.id) as client:
        with patch(
            "app.adapters.hosting.get_hosting_adapter",
            new=AsyncMock(return_value=failing_adapter),
        ):
            resp = await client.post(f"/runs/{source.id}/retry")

    assert resp.status_code == 503

    child = (
        await db.execute(select(Run).where(Run.parent_run_id == source.id))
    ).scalar_one()
    # Terminal, so it no longer blocks future retries of the source run.
    assert child.status == "error"
    assert child.failure_reason is not None
    assert "dispatch" in child.failure_reason.lower()

    # And the guard genuinely treats it as settled.
    ok_adapter = AsyncMock()
    async with make_auth_client(user.id) as client:
        with patch(
            "app.adapters.hosting.get_hosting_adapter",
            new=AsyncMock(return_value=ok_adapter),
        ):
            retry_again = await client.post(f"/runs/{source.id}/retry")
    assert retry_again.status_code == 201
