"""Issue #157 — orphan pending runs: pipeline-outbox sweeper (hermetic).

Repro: a run accepted (row committed as ``pending``) but never executed
because the async self-invoke failed/crashed stays ``pending`` forever —
nothing ever scans for accepted-but-never-started runs.

Expected: a periodic sweeper redrives stale ``pending`` runs past a timeout
and marks ancient ones exhausted (``error`` + ``failure_reason``), so no run
stays ``pending`` past the timeout without executing or being failed.

Zero I/O: fake session + fake hosting adapter, no database, no network.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.services.pipeline_outbox import sweep_stale_pending_runs

pytestmark = pytest.mark.fast


# ---------------------------------------------------------------------------
# Fakes (zero I/O)
# ---------------------------------------------------------------------------


class FakeResult:
    def __init__(self, rows=(), rowcount=0):
        self._rows = list(rows)
        self.rowcount = rowcount

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class FakeSession:
    """Scripted stand-in for AsyncSession: one canned result per execute()."""

    def __init__(self, script):
        self._script = list(script)
        self.executed = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, stmt):
        self.executed.append(stmt)
        assert self._script, "FakeSession: more execute() calls than scripted"
        return self._script.pop(0)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass

    def add(self, obj):
        pass


class FakeAdapter:
    """Stand-in for AWSHostingAdapter: records redrives, optionally raises."""

    def __init__(self, fail_with=None):
        self.scheduled = []
        self.fail_with = fail_with

    async def schedule_pipeline(self, run_id, background_tasks=None):
        if self.fail_with is not None:
            raise self.fail_with
        self.scheduled.append(run_id)


def _make_run(status="pending", age_seconds=0):
    from app.models import Run

    now = datetime.now(timezone.utc)
    uid = uuid.uuid4()
    return Run(
        id=uid,
        repo_id=uuid.uuid4(),
        github_run_id=None,
        head_sha="a" * 40,
        head_branch="main",
        status=status,
        conclusion="failure",
        created_at=now - timedelta(seconds=age_seconds),
        updated_at=now - timedelta(seconds=age_seconds),
    )


def _session_factory(session):
    def _factory():
        return session

    return _factory


async def _adapter_factory(adapter):
    async def _factory():
        return adapter

    return _factory


# ---------------------------------------------------------------------------
# Repro + behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_pending_run_is_redriven():
    """Core #157 repro: pending run older than the timeout gets rescheduled."""
    run = _make_run(status="pending", age_seconds=3600)
    session = FakeSession(
        [
            FakeResult(rows=[run]),  # scan
            FakeResult(rowcount=1),  # CAS claim (still pending)
        ]
    )
    adapter = FakeAdapter()

    summary = await sweep_stale_pending_runs(
        session_factory=_session_factory(session),
        adapter_factory=_adapter_factory(adapter),
        stale_after_s=300,
        exhaust_after_s=7200,
        now=datetime.now(timezone.utc),
    )

    assert adapter.scheduled == [run.id]
    assert summary.redriven == [str(run.id)]
    assert summary.exhausted == []


@pytest.mark.asyncio
async def test_fresh_pending_run_is_untouched():
    """A recently accepted run (child may still start) is never redriven."""
    run = _make_run(status="pending", age_seconds=10)
    session = FakeSession(
        [
            FakeResult(rows=[]),  # scan finds nothing stale
        ]
    )
    adapter = FakeAdapter()

    summary = await sweep_stale_pending_runs(
        session_factory=_session_factory(session),
        adapter_factory=_adapter_factory(adapter),
        stale_after_s=300,
        exhaust_after_s=7200,
        now=datetime.now(timezone.utc),
    )

    assert adapter.scheduled == []
    assert summary.redriven == []
    assert summary.exhausted == []
    assert summary.scanned == 0


@pytest.mark.asyncio
async def test_ancient_pending_run_is_marked_exhausted():
    """A run pending past the exhaust horizon is failed, never redriven."""
    run = _make_run(status="pending", age_seconds=99999)
    session = FakeSession(
        [
            FakeResult(rows=[run]),  # scan
            FakeResult(rowcount=1),  # CAS pending -> error
        ]
    )
    adapter = FakeAdapter()

    summary = await sweep_stale_pending_runs(
        session_factory=_session_factory(session),
        adapter_factory=_adapter_factory(adapter),
        stale_after_s=300,
        exhaust_after_s=7200,
        now=datetime.now(timezone.utc),
    )

    assert adapter.scheduled == []
    assert summary.exhausted == [str(run.id)]
    assert summary.redriven == []


@pytest.mark.asyncio
async def test_failed_redrive_marks_run_error():
    """A stale run whose reschedule raises is failed, not left pending."""
    run = _make_run(status="pending", age_seconds=3600)
    session = FakeSession(
        [
            FakeResult(rows=[run]),  # scan
            FakeResult(rowcount=1),  # CAS claim (still pending)
            FakeResult(rowcount=1),  # CAS pending -> error after raise
        ]
    )
    adapter = FakeAdapter(fail_with=RuntimeError("Lambda invoke refused"))

    summary = await sweep_stale_pending_runs(
        session_factory=_session_factory(session),
        adapter_factory=_adapter_factory(adapter),
        stale_after_s=300,
        exhaust_after_s=7200,
        now=datetime.now(timezone.utc),
    )

    assert adapter.scheduled == []
    assert summary.exhausted == [str(run.id)]
    assert summary.redriven == []


@pytest.mark.asyncio
async def test_race_with_starting_child_is_skipped():
    """If the child claimed the run first (CAS rowcount 0), skip quietly."""
    run = _make_run(status="pending", age_seconds=3600)
    session = FakeSession(
        [
            FakeResult(rows=[run]),  # scan
            FakeResult(rowcount=0),  # lost the race — child started
        ]
    )
    adapter = FakeAdapter()

    summary = await sweep_stale_pending_runs(
        session_factory=_session_factory(session),
        adapter_factory=_adapter_factory(adapter),
        stale_after_s=300,
        exhaust_after_s=7200,
        now=datetime.now(timezone.utc),
    )

    assert adapter.scheduled == []
    assert summary.redriven == []
    assert summary.exhausted == []
    assert summary.skipped == 1
