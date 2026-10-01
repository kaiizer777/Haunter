"""Unit tests for failure-signature clustering (GET /runs grouping)."""

from __future__ import annotations

import uuid

import pytest

from app.failure_signature import normalize_failure_signature


def test_empty_reason_maps_to_unknown() -> None:
    assert normalize_failure_signature(None) == "unknown"
    assert normalize_failure_signature("") == "unknown"
    assert normalize_failure_signature("   \n  ") == "unknown"


def test_strips_commit_sha() -> None:
    a = "fix_generator: ValueError: bad ref abc123def456789012345678901234567890abcd in main"
    # Same message with a different 40-char SHA must cluster together.
    b = "fix_generator: ValueError: bad ref ffffffffffffffffffffffffffffffffffffffff in main"
    assert normalize_failure_signature(a) == normalize_failure_signature(b)
    assert "<sha>" in normalize_failure_signature(a)


def test_strips_file_paths() -> None:
    a = "verification: AssertionError in backend/app/orchestrator.py:422 failed"
    b = "verification: AssertionError in backend/app/orchestrator.py:917 failed"
    assert normalize_failure_signature(a) == normalize_failure_signature(b)
    assert "<path>" in normalize_failure_signature(a)


def test_strips_timestamps() -> None:
    a = "sandbox timeout at 2026-03-01T12:34:56Z after 120s"
    b = "sandbox timeout at 2026-03-02T01:02:03Z after 120s"
    assert normalize_failure_signature(a) == normalize_failure_signature(b)
    assert "<ts>" in normalize_failure_signature(a)


def test_distinct_errors_have_distinct_signatures() -> None:
    a = normalize_failure_signature("fix_generator: ValueError: bad patch")
    b = normalize_failure_signature("sandbox: TimeoutError: runner timed out")
    assert a != b
    assert a != "unknown"
    assert b != "unknown"


def test_signature_grouping_counts() -> None:
    reasons = [
        "ValueError: bad ref aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa at 12:00:01",
        "ValueError: bad ref bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb at 13:00:02",
        "TimeoutError: runner timed out",
    ]
    sigs = [normalize_failure_signature(r) for r in reasons]
    assert sigs[0] == sigs[1]
    assert sigs[0] != sigs[2]


@pytest.mark.asyncio
async def test_list_runs_returns_signature_fields(
    db, user_factory, make_auth_client
) -> None:
    """GET /runs exposes signature / signature_count / sample_run_id."""
    from app.models import Repo, Run
    from tests.conftest import truncate_all

    await truncate_all(db)
    user = await user_factory(github_id=9501, username="sig_user")
    repo = Repo(user_id=user.id, owner="sig", name="repo")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)

    base_sha = "a" * 40
    shared_a = "ValueError: bad ref aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa in main"
    shared_b = "ValueError: bad ref bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb in main"
    other = "TimeoutError: runner timed out after 120s"
    runs: list[Run] = []
    for reason in (shared_a, shared_b, other):
        run = Run(
            repo_id=repo.id,
            github_run_id=int(uuid.uuid4().int % 10_000_000_000) + 1,
            github_delivery_id=str(uuid.uuid4()),
            head_sha=base_sha,
            head_branch="main",
            status="error",
            failure_reason=reason,
        )
        db.add(run)
        runs.append(run)
    await db.commit()
    for run in runs:
        await db.refresh(run)

    async with make_auth_client(user.id) as client:
        resp = await client.get("/runs")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 3
    by_id = {r["id"]: r for r in data["runs"]}
    first_sig = normalize_failure_signature(shared_a)
    assert by_id[str(runs[0].id)]["signature"] == first_sig
    assert by_id[str(runs[1].id)]["signature"] == first_sig
    assert by_id[str(runs[0].id)]["signature_count"] == 2
    assert by_id[str(runs[1].id)]["signature_count"] == 2
    # Sample is the earliest run sharing the signature (runs[0] created first).
    assert by_id[str(runs[0].id)]["sample_run_id"] == str(runs[0].id)
    assert by_id[str(runs[1].id)]["sample_run_id"] == str(runs[0].id)
    other_sig = normalize_failure_signature(other)
    assert by_id[str(runs[2].id)]["signature"] == other_sig
    assert by_id[str(runs[2].id)]["signature_count"] == 1
    assert by_id[str(runs[2].id)]["sample_run_id"] == str(runs[2].id)
