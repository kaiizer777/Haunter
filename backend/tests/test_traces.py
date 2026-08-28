"""
Phase 9 Observability Tests — traces.py

Covers:
 1.  GET /runs/{run_id}/trace own run → 200 + chronological timeline
 2.  GET /runs/{run_id}/trace second user's run_id → 404 (ownership oracle)
 3.  GET /runs own runs → correct list + total count
 4.  GET /runs second user's cookie → does NOT include victim's runs
 5.  GET /runs?status=completed filter works
 6.  GET /runs?limit=1000 → 422 (bounded param)
 7.  GET /runs?status=evil_status → 422 (allowlist)
 8.  GET /repos/{repo_id}/stats own repo → correct aggregates
 9.  GET /repos/{victim_repo_id}/stats → 404 (ownership)
10.  failure_classification: wrong_diagnosis (no attempts)
11.  failure_classification: wrong_fix (attempts, no verification_status)
12.  failure_classification: tests_still_failing (all verification_status=fail)
13.  failure_classification: sandbox_error (TIMEOUT in failure_reason)
14.  run_steps content: no raw secrets (sk-/npg_/BEGIN PRIVATE)
"""

import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Attempt, Repo, Run, RunStep, User
from tests.conftest import truncate_all


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run(
    repo_id: uuid.UUID,
    *,
    status: str = "completed",
    github_run_id: int | None = None,
) -> Run:
    return Run(
        repo_id=repo_id,
        github_run_id=github_run_id or (int(uuid.uuid4().int % 10_000_000_000) + 1),
        github_delivery_id=str(uuid.uuid4()),
        head_sha="a" * 40,
        head_branch="main",
        status=status,
        diagnosis_summary="Test diagnosis summary",
    )


def _make_step(run_id: uuid.UUID, *, step_name: str = "context_gatherer") -> RunStep:
    return RunStep(
        run_id=run_id,
        step_name=step_name,
        input_tokens=100,
        output_tokens=50,
        latency_ms=500,
        cost_estimate=0.0002,
    )


def _make_attempt(
    run_id: uuid.UUID,
    *,
    attempt_number: int = 1,
    verification_status: str | None = None,
    failure_reason: str | None = None,
) -> Attempt:
    return Attempt(
        run_id=run_id,
        attempt_number=attempt_number,
        patch_text="--- a/fix.py\n+++ b/fix.py\n@@ -1 +1 @@\n-bug\n+fix",
        confidence_score=80,
        verification_status=verification_status,
        failure_reason=failure_reason,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_own_run_200(
    db: AsyncSession, user_factory, make_auth_client
):
    """GET /runs/{run_id}/trace for own run returns 200 with timeline."""
    await truncate_all(db)

    user = await user_factory(github_id=9001, username="trace_owner")
    repo = Repo(user_id=user.id, owner="org", name="repo1")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)

    run = _make_run(repo.id, status="completed")
    db.add(run)
    await db.commit()
    await db.refresh(run)

    step = _make_step(run.id)
    db.add(step)
    attempt = _make_attempt(run.id, verification_status="pass")
    db.add(attempt)
    await db.commit()

    async with make_auth_client(user.id) as client:
        resp = await client.get(f"/runs/{run.id}/trace")

    assert resp.status_code == 200
    data = resp.json()
    assert data["run"]["id"] == str(run.id)
    assert data["run"]["status"] == "completed"
    assert len(data["steps"]) == 1
    assert data["steps"][0]["step_name"] == "context_gatherer"
    assert data["steps"][0]["input_tokens"] == 100
    assert len(data["attempts"]) == 1
    assert data["attempts"][0]["verification_status"] == "pass"
    assert data["total_cost"] == pytest.approx(0.0002, abs=1e-9)
    assert data["total_latency_ms"] == 500
    assert data["failure_classification"] is None  # completed runs → None


@pytest.mark.asyncio
async def test_trace_other_user_run_404(
    db: AsyncSession, user_factory, make_auth_client
):
    """GET /runs/{victim_run_id}/trace by attacker → 404 (no existence oracle)."""
    await truncate_all(db)

    victim = await user_factory(github_id=9002, username="victim")
    attacker = await user_factory(github_id=9003, username="attacker")

    victim_repo = Repo(user_id=victim.id, owner="vic", name="repo")
    db.add(victim_repo)
    await db.commit()
    await db.refresh(victim_repo)

    victim_run = _make_run(victim_repo.id)
    db.add(victim_run)
    await db.commit()
    await db.refresh(victim_run)

    # Attacker tries to read victim's trace.
    async with make_auth_client(attacker.id) as client:
        resp = await client.get(f"/runs/{victim_run.id}/trace")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_runs_own(
    db: AsyncSession, user_factory, make_auth_client
):
    """GET /runs returns only the caller's runs with correct total."""
    await truncate_all(db)

    user_a = await user_factory(github_id=9010, username="user_a")
    user_b = await user_factory(github_id=9011, username="user_b")

    repo_a = Repo(user_id=user_a.id, owner="a", name="repo")
    repo_b = Repo(user_id=user_b.id, owner="b", name="repo")
    db.add(repo_a)
    db.add(repo_b)
    await db.commit()
    await db.refresh(repo_a)
    await db.refresh(repo_b)

    # 2 runs for user A, 1 for user B.
    for _ in range(2):
        db.add(_make_run(repo_a.id))
    db.add(_make_run(repo_b.id))
    await db.commit()

    async with make_auth_client(user_a.id) as client:
        resp = await client.get("/runs")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["runs"]) == 2
    # All returned runs must belong to repos owned by user_a.
    assert all(r["repo_id"] == str(repo_a.id) for r in data["runs"])


@pytest.mark.asyncio
async def test_list_runs_isolation(
    db: AsyncSession, user_factory, make_auth_client
):
    """GET /runs with attacker cookie never includes victim's runs."""
    await truncate_all(db)

    victim = await user_factory(github_id=9020, username="vic2")
    attacker = await user_factory(github_id=9021, username="att2")

    vic_repo = Repo(user_id=victim.id, owner="vic2", name="repo")
    db.add(vic_repo)
    await db.commit()
    await db.refresh(vic_repo)

    db.add(_make_run(vic_repo.id))
    await db.commit()

    async with make_auth_client(attacker.id) as client:
        resp = await client.get("/runs")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["runs"] == []


@pytest.mark.asyncio
async def test_list_runs_status_filter(
    db: AsyncSession, user_factory, make_auth_client
):
    """GET /runs?status=completed returns only completed runs."""
    await truncate_all(db)

    user = await user_factory(github_id=9030, username="filter_user")
    repo = Repo(user_id=user.id, owner="f", name="repo")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)

    db.add(_make_run(repo.id, status="completed"))
    db.add(_make_run(repo.id, status="error"))
    await db.commit()

    async with make_auth_client(user.id) as client:
        resp = await client.get("/runs?status=completed")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["runs"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_list_runs_limit_too_large_422(
    db: AsyncSession, user_factory, make_auth_client
):
    """GET /runs?limit=1000 → 422 (bounded param)."""
    await truncate_all(db)
    user = await user_factory(github_id=9040, username="limit_user")

    async with make_auth_client(user.id) as client:
        resp = await client.get("/runs?limit=1000")

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_runs_invalid_status_422(
    db: AsyncSession, user_factory, make_auth_client
):
    """GET /runs?status=evil_injection → 422 (allowlist)."""
    await truncate_all(db)
    user = await user_factory(github_id=9041, username="status_user")

    async with make_auth_client(user.id) as client:
        resp = await client.get("/runs?status=evil_injection")

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_repo_stats_own(
    db: AsyncSession, user_factory, make_auth_client
):
    """GET /repos/{repo_id}/stats → correct aggregates for own repo."""
    await truncate_all(db)

    user = await user_factory(github_id=9050, username="stats_user")
    repo = Repo(user_id=user.id, owner="s", name="repo")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)

    # 2 completed, 1 error — success_rate = 2/3
    for status in ("completed", "completed", "error"):
        run = _make_run(repo.id, status=status)
        db.add(run)
        await db.commit()
        await db.refresh(run)
        step = _make_step(run.id)
        db.add(step)
        await db.commit()

    async with make_auth_client(user.id) as client:
        resp = await client.get(f"/repos/{repo.id}/stats")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_runs"] == 3
    assert data["success_rate"] == pytest.approx(2 / 3, rel=0.01)
    assert data["avg_cost"] == pytest.approx(0.0002, abs=1e-6)
    assert data["avg_latency_ms"] == pytest.approx(500.0, rel=0.01)


@pytest.mark.asyncio
async def test_repo_stats_other_user_404(
    db: AsyncSession, user_factory, make_auth_client
):
    """GET /repos/{victim_repo_id}/stats by attacker → 404."""
    await truncate_all(db)

    victim = await user_factory(github_id=9060, username="vic3")
    attacker = await user_factory(github_id=9061, username="att3")

    vic_repo = Repo(user_id=victim.id, owner="vic3", name="repo")
    db.add(vic_repo)
    await db.commit()
    await db.refresh(vic_repo)

    async with make_auth_client(attacker.id) as client:
        resp = await client.get(f"/repos/{vic_repo.id}/stats")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_classify_wrong_diagnosis_no_attempts(
    db: AsyncSession, user_factory, make_auth_client
):
    """failure_classification = wrong_diagnosis when run failed with no attempts."""
    await truncate_all(db)

    user = await user_factory(github_id=9070, username="cl_user1")
    repo = Repo(user_id=user.id, owner="cl1", name="repo")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)

    run = _make_run(repo.id, status="error")
    db.add(run)
    await db.commit()
    await db.refresh(run)

    async with make_auth_client(user.id) as client:
        resp = await client.get(f"/runs/{run.id}/trace")

    assert resp.status_code == 200
    assert resp.json()["failure_classification"] == "wrong_diagnosis"


@pytest.mark.asyncio
async def test_classify_wrong_fix(
    db: AsyncSession, user_factory, make_auth_client
):
    """failure_classification = wrong_fix when attempts exist but none reached sandbox."""
    await truncate_all(db)

    user = await user_factory(github_id=9071, username="cl_user2")
    repo = Repo(user_id=user.id, owner="cl2", name="repo")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)

    run = _make_run(repo.id, status="error")
    db.add(run)
    await db.commit()
    await db.refresh(run)

    # Attempt with no verification_status → patch-sanity rejected
    attempt = _make_attempt(run.id, verification_status=None)
    db.add(attempt)
    await db.commit()

    async with make_auth_client(user.id) as client:
        resp = await client.get(f"/runs/{run.id}/trace")

    assert resp.status_code == 200
    assert resp.json()["failure_classification"] == "wrong_fix"


@pytest.mark.asyncio
async def test_classify_tests_still_failing(
    db: AsyncSession, user_factory, make_auth_client
):
    """failure_classification = tests_still_failing when all attempts fail verification."""
    await truncate_all(db)

    user = await user_factory(github_id=9072, username="cl_user3")
    repo = Repo(user_id=user.id, owner="cl3", name="repo")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)

    run = _make_run(repo.id, status="fallback")
    db.add(run)
    await db.commit()
    await db.refresh(run)

    for i in range(1, 4):
        db.add(_make_attempt(run.id, attempt_number=i, verification_status="fail"))
    await db.commit()

    async with make_auth_client(user.id) as client:
        resp = await client.get(f"/runs/{run.id}/trace")

    assert resp.status_code == 200
    assert resp.json()["failure_classification"] == "tests_still_failing"


@pytest.mark.asyncio
async def test_classify_sandbox_error(
    db: AsyncSession, user_factory, make_auth_client
):
    """failure_classification = sandbox_error when Cloud Build TIMEOUT in failure_reason."""
    await truncate_all(db)

    user = await user_factory(github_id=9073, username="cl_user4")
    repo = Repo(user_id=user.id, owner="cl4", name="repo")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)

    run = _make_run(repo.id, status="fallback")
    db.add(run)
    await db.commit()
    await db.refresh(run)

    db.add(
        _make_attempt(
            run.id,
            verification_status="fail",
            failure_reason="BUILD_STATUS: TIMEOUT\nBuild exceeded time limit.",
        )
    )
    await db.commit()

    async with make_auth_client(user.id) as client:
        resp = await client.get(f"/runs/{run.id}/trace")

    assert resp.status_code == 200
    assert resp.json()["failure_classification"] == "sandbox_error"


@pytest.mark.asyncio
async def test_no_raw_secrets_in_steps(
    db: AsyncSession, user_factory, make_auth_client
):
    """run_steps content must never contain raw secrets (sk-/npg_/BEGIN PRIVATE)."""
    await truncate_all(db)

    user = await user_factory(github_id=9080, username="sec_user")
    repo = Repo(user_id=user.id, owner="sec", name="repo")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)

    run = _make_run(repo.id)
    db.add(run)
    await db.commit()
    await db.refresh(run)

    # Deliberately store a step — step_name should be clean (no raw content stored).
    step = RunStep(
        run_id=run.id,
        step_name="context_gatherer",
        input_tokens=200,
        output_tokens=100,
        latency_ms=300,
        cost_estimate=0.0003,
    )
    db.add(step)
    await db.commit()

    async with make_auth_client(user.id) as client:
        resp = await client.get(f"/runs/{run.id}/trace")

    assert resp.status_code == 200
    response_text = resp.text

    # These patterns must NEVER appear in the trace response.
    assert "sk-" not in response_text
    assert "npg_" not in response_text
    assert "BEGIN PRIVATE" not in response_text


@pytest.mark.asyncio
async def test_trace_unauthenticated_401(client):
    """GET /runs/{id}/trace without cookie → 401."""
    resp = await client.get(f"/runs/{uuid.uuid4()}/trace")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_runs_unauthenticated_401(client):
    """GET /runs without cookie → 401."""
    resp = await client.get("/runs")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_repo_stats_unauthenticated_401(client):
    """GET /repos/{id}/stats without cookie → 401."""
    resp = await client.get(f"/repos/{uuid.uuid4()}/stats")
    assert resp.status_code == 401
