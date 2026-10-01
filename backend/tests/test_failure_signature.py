"""Unit tests for failure-signature clustering (GET /runs grouping)."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.failure_signature import (
    MAX_SIGNATURE_CHARS,
    MAX_SIGNATURE_INPUT_CHARS,
    normalize_failure_signature,
)

_VECTORS_PATH = Path(__file__).resolve().parent / "failure_signature_vectors.json"


def _load_vectors() -> dict[str, Any]:
    """Load the golden vector corpus that pins every expected signature."""
    with _VECTORS_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def test_empty_reason_maps_to_unknown() -> None:
    """Absent, empty and whitespace-only reasons carry no signal."""
    assert normalize_failure_signature(None) == "unknown"
    assert normalize_failure_signature("") == "unknown"
    assert normalize_failure_signature("   \n  ") == "unknown"


def test_strips_commit_sha() -> None:
    """The same error at a different 40-char SHA must cluster together."""
    a = "fix_generator: ValueError: bad ref abc123def456789012345678901234567890abcd in main"
    # Same message with a different 40-char SHA must cluster together.
    b = "fix_generator: ValueError: bad ref ffffffffffffffffffffffffffffffffffffffff in main"
    assert normalize_failure_signature(a) == normalize_failure_signature(b)
    assert "<sha>" in normalize_failure_signature(a)


def test_strips_file_paths() -> None:
    """The same error at a different file/line must cluster together."""
    a = "verification: AssertionError in backend/app/orchestrator.py:422 failed"
    b = "verification: AssertionError in backend/app/orchestrator.py:917 failed"
    assert normalize_failure_signature(a) == normalize_failure_signature(b)
    assert "<path>" in normalize_failure_signature(a)


def test_strips_timestamps() -> None:
    """The same failure at a different instant must cluster together.

    Regression test: the ISO-8601 rule required an uppercase ``T`` separator
    while the normalizer lowercased its input first, so it could never match
    and every timestamp fell through to the date/time/number rules.
    """
    a = "sandbox timeout at 2026-03-01T12:34:56Z after 120s"
    b = "sandbox timeout at 2026-03-02T01:02:03Z after 120s"
    assert normalize_failure_signature(a) == normalize_failure_signature(b)
    assert "<ts>" in normalize_failure_signature(a)


def test_distinct_errors_have_distinct_signatures() -> None:
    """Different failures must not be collapsed into one cluster."""
    a = normalize_failure_signature("fix_generator: ValueError: bad patch")
    b = normalize_failure_signature("sandbox: TimeoutError: runner timed out")
    assert a != b
    assert a != "unknown"
    assert b != "unknown"


def test_signature_grouping_counts() -> None:
    """Clustering is by normalized signature, not by raw text."""
    reasons = [
        "ValueError: bad ref aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa at 12:00:01",
        "ValueError: bad ref bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb at 13:00:02",
        "TimeoutError: runner timed out",
    ]
    sigs = [normalize_failure_signature(r) for r in reasons]
    assert sigs[0] == sigs[1]
    assert sigs[0] != sigs[2]


# ---------------------------------------------------------------------------
# Golden corpus - pins the exact output for every interesting input shape.
# ---------------------------------------------------------------------------


def test_golden_vectors_match_expected_signatures() -> None:
    """Every corpus input normalizes to its recorded signature."""
    doc = _load_vectors()
    mismatches = [
        f"{v['id']}: expected {v['expected']!r}, got {normalize_failure_signature(v['input'])!r}"
        for v in doc["vectors"]
        if normalize_failure_signature(v["input"]) != v["expected"]
    ]
    assert not mismatches, "\n".join(mismatches)


def test_golden_vectors_are_idempotent() -> None:
    """normalize(normalize(x)) == normalize(x) for the whole corpus."""
    doc = _load_vectors()
    not_idempotent = [
        v["id"]
        for v in doc["vectors"]
        if normalize_failure_signature(normalize_failure_signature(v["input"]))
        != v["expected"]
    ]
    assert not not_idempotent, f"not idempotent: {not_idempotent}"


def test_golden_vectors_keep_distinct_failures_distinct() -> None:
    """Signatures the corpus declares distinct stay distinct."""
    doc = _load_vectors()
    by_id = {v["id"]: v for v in doc["vectors"]}
    collisions = [
        f"{a} == {b} ({by_id[a]['expected']!r})"
        for a, b in doc["mustDiffer"]
        if by_id[a]["expected"] == by_id[b]["expected"]
    ]
    assert not collisions, "distinct failures collapsed: " + "; ".join(collisions)


def test_signature_length_is_bounded() -> None:
    """No signature exceeds MAX_SIGNATURE_CHARS."""
    doc = _load_vectors()
    for v in doc["vectors"]:
        assert len(v["expected"]) <= MAX_SIGNATURE_CHARS, v["id"]


def test_truncation_counts_code_points_not_utf16_units() -> None:
    """A non-BMP character on the truncation boundary must survive whole."""
    raw = "x" * (MAX_SIGNATURE_CHARS - 1) + "\U0001F680 tail"
    sig = normalize_failure_signature(raw)
    assert len(sig) == MAX_SIGNATURE_CHARS
    assert sig.endswith("\U0001F680")


def test_input_is_clipped_before_normalizing() -> None:
    """A multi-megabyte failure_reason must cost the same as a short one.

    `runs.failure_reason` is an unbounded Text column, so the cost of
    normalizing one run must not scale with the stored CI log.
    """
    prefix = "pr_writer: rollout blocked "
    raw = prefix + ("x" * (MAX_SIGNATURE_INPUT_CHARS + 500_000))
    started = time.perf_counter()
    sig = normalize_failure_signature(raw)
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert sig.startswith(prefix)
    assert len(sig) <= MAX_SIGNATURE_CHARS
    # A regex sweep over half a megabyte cannot plausibly finish this fast.
    assert elapsed_ms < 250, f"normalizing a clipped input took {elapsed_ms:.1f}ms"


def test_clipping_does_not_change_the_signature_of_a_short_first_line() -> None:
    """Only the first line matters, so clipping past it is a no-op."""
    short = "sandbox: TimeoutError: runner timed out"
    with_tail = short + "\n" + ("stack noise " * 50_000)
    assert normalize_failure_signature(with_tail) == normalize_failure_signature(short)


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


@pytest.mark.asyncio
async def test_signature_counts_are_dropped_not_truncated_past_the_cap(
    db, user_factory, make_auth_client, monkeypatch
) -> None:
    """Past SIGNATURE_CLUSTER_MAX_RUNS the scan is skipped, not silently cut off.

    Truncating would report a count lower than the truth; returning 1 makes the
    client fall back to grouping the page it already has.
    """
    from app.models import Repo, Run
    from app.routers import traces
    from tests.conftest import truncate_all

    await truncate_all(db)
    user = await user_factory(github_id=9502, username="sig_cap_user")
    repo = Repo(user_id=user.id, owner="sigcap", name="repo")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)

    runs: list[Run] = []
    for reason in ("ValueError: shared", "ValueError: shared", "TypeError: other"):
        run = Run(
            repo_id=repo.id,
            github_run_id=int(uuid.uuid4().int % 10_000_000_000) + 1,
            github_delivery_id=str(uuid.uuid4()),
            head_sha="b" * 40,
            head_branch="main",
            status="error",
            failure_reason=reason,
        )
        db.add(run)
        runs.append(run)
    await db.commit()
    for run in runs:
        await db.refresh(run)

    monkeypatch.setattr(traces, "SIGNATURE_CLUSTER_MAX_RUNS", 2)
    async with make_auth_client(user.id) as client:
        resp = await client.get("/runs")

    assert resp.status_code == 200
    by_id = {r["id"]: r for r in resp.json()["runs"]}
    shared_sig = normalize_failure_signature("ValueError: shared")
    assert by_id[str(runs[0].id)]["signature"] == shared_sig
    # 3 matching runs > cap of 2 -> no clustering scan, so no partial count.
    assert by_id[str(runs[0].id)]["signature_count"] == 1
    assert by_id[str(runs[1].id)]["signature_count"] == 1
    assert by_id[str(runs[0].id)]["sample_run_id"] == str(runs[0].id)
