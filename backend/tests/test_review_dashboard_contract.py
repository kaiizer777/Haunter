"""
Stage 1 (Repro Agent C) — backend-side API contract defects that break the dashboard.

Three defects, all of them the *backend half* of a dashboard feature that silently
does not work in production:

  C1  GET /runs/{run_id}/trace never returns the generated patch.
      ``attempts.patch_text`` is a NOT NULL column (app/models.py:298) written by
      every fix attempt, and the dashboard renders it (DiffViewer / "Copy Patch")
      from ``attempts[].patch_text``. ``AttemptOut`` (app/routers/traces.py:99-107)
      does not declare the field, so it never leaves the process. The frontend
      type marks it optional (``patch_text?: string``, src/lib/api.ts:92) and its
      own unit test feeds the component a mock that *does* include it
      (src/components/trace/trace-timeline.test.tsx:67) — so the frontend suite
      is green while the real product shows no patch at all.
      Scope note: tests/test_eval_harness.py::test_eval_run_response_no_patch_text
      pins patch text OUT of the ``POST /eval/run`` response. That is a different
      endpoint and a different consumer (a benchmark payload, deliberately slim);
      the trace endpoint is what feeds the diff viewer. These two expectations do
      not contradict each other and this file does not touch the eval path.

  C2  GET /runs?status=... rejects statuses the pipeline actually writes.
      ``RunStatusLiteral`` (app/routers/traces.py:72-81) allows 8 values. The
      orchestrator's own state machine, ``RunStatus`` (app/orchestrator.py:103-118),
      defines 13, and the PR Writer / fallback-comment / flaky-quarantine paths
      write ``pr_opened``, ``fallback_commented`` and ``flaky_detected`` — none of
      which the filter accepts. Validation runs at traces.py:544-556 and answers
      HTTP 422, so selecting "Flaky Test" in the runs table yields an error, not an
      empty list. These tests iterate the real ``RunStatus`` enum rather than a
      hand-written list, so the reproduction cannot drift from the writer.

  C3  ``GET /reviews`` and ``GET /repos/{id}/reviews`` report a ``total`` that
      contradicts their own rows when ``severity`` is set. Both endpoints run the
      COUNT before the severity predicate, which is a Python post-filter over the
      already-paginated page (reviews.py:171-177 and reviews.py:233-239). A page
      showing 1 review beside "2 total" is a count the UI cannot reconcile.
      The rest of C3 (the TS type dropping ``failure_reason``, and the 10px grey
      status word as the only failure signal) is frontend-only and is deliberately
      NOT tested here — see the module docstring of the frontend suite instead.

Hermeticity: every test drives the real FastAPI app over ASGI with
``get_db`` overridden by a scripted session. No network, no Postgres, no GitHub,
no LLM, no clock dependence. The scripted session dispatches on the *shape* of
each SQLAlchemy statement the route emits and raises on anything it does not
recognise, so a route that changes its query surface breaks these tests loudly
instead of silently answering from a stale script.

Deliberately marked ``fast``: none of these tests request the ``db`` /
``user_factory`` / ``repo_factory`` / ``fake_db`` fixtures, so
``tests/conftest.py::pytest_collection_modifyitems`` does not tag them ``db``
and they run in the default suite.

Every test in this file is EXPECTED TO FAIL against the current code. Each
failure is the reproduction.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Optional

import pytest
from sqlalchemy import Column, Select
from sqlalchemy.sql.selectable import SelectStatementGrouping

from app.db import get_db
from app.models import Attempt, CodeReview, Repo, Run, RunStep, User
from tests.fake_audit_db import FakeResult

# ---------------------------------------------------------------------------
# Fixed clock
#
# Every timestamp is derived from this one constant instead of datetime.now(), so
# two runs of this file produce byte-identical request and response payloads.
# ---------------------------------------------------------------------------

_FIXED_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

# A recognisable unified diff: the exact string that must survive serialisation.
_PATCH_TEXT = (
    "--- a/app/handlers.py\n"
    "+++ b/app/handlers.py\n"
    "@@ -14,7 +14,7 @@ def handler():\n"
    "-    return None\n"
    '+    return {"ok": True}\n'
)


# ---------------------------------------------------------------------------
# Scripted session
# ---------------------------------------------------------------------------


class UnsupportedStatementError(AssertionError):
    """The scripted session was handed a statement shape it does not model."""


#: Wildcard for the WHERE-clause component of a shape key: match any predicate.
_ANY_WHERE = object()


def _n_columns(model: type) -> int:
    """How many columns ``select(Model)`` expands to for this mapping."""
    return len(model.__table__.columns)


def _whole_entity(statement: Select) -> bool:
    """
    True when the statement selects a mapped class rather than a scalar/aggregate.

    For ``select(Run)`` the first column description's ``expr`` *is* the class
    itself; for ``select(Repo.id)``, ``select(func.count(...))`` or
    ``select(Run.id, func.left(...))`` it is a column or function element.
    """
    descriptions = statement.column_descriptions
    if not descriptions:
        return False
    entity = descriptions[0].get("entity")
    return entity is not None and descriptions[0].get("expr") is entity


#: Node types that carry a nested SELECT. Their columns are not predicates on the
#: enclosing statement, so the walk must not descend into them.
_SUBQUERY_NODES = (Select, SelectStatementGrouping)


def _where_keys(statement: Select) -> frozenset[str]:
    """
    Column names the top-level WHERE references, as ``"<table>.<column>"``.

    Sub-selects (``Run.repo_id.in_(select(Repo.id).where(...))``) are a hard stop:
    their columns are not predicates on this statement, and letting them leak in
    would make the shape key depend on unrelated tables.
    """
    keys: set[str] = set()
    clause = statement.whereclause
    if clause is None:
        return frozenset()

    def walk(node: Any) -> None:
        if isinstance(node, _SUBQUERY_NODES):
            return
        if isinstance(node, Column):
            keys.add(f"{node.table.name}.{node.key}")
            return
        for child in node.get_children():
            walk(child)

    walk(clause)
    return frozenset(keys)


def _shape(statement: Any) -> Optional[tuple]:
    """
    Reduce a SELECT to the tuple the scripted session dispatches on.

    Returns ``(mapped_class, selects_whole_entity, column_count, where_keys)``,
    or ``None`` for anything that is not a SELECT.
    """
    if not isinstance(statement, Select):
        return None
    descriptions = statement.column_descriptions
    entity = descriptions[0].get("entity") if descriptions else None
    return (
        entity,
        _whole_entity(statement),
        len(statement.selected_columns),
        _where_keys(statement),
    )


def _matches(shape: tuple, key: tuple) -> bool:
    entity, whole, count, where = key
    if where is _ANY_WHERE:
        return shape[:3] == (entity, whole, count)
    return shape == (entity, whole, count, where)


class _ScriptedSession:
    """
    AsyncSession stand-in that answers from a shape-keyed script.

    Every key is ``(mapped_class, selects_whole_entity, column_count, where_keys)``
    — derived from the real statements the routes emit — and the value is the exact
    row list the route should see. A statement whose shape is not in the script
    raises :class:`UnsupportedStatementError` naming both shapes, so a route that
    starts (or stops) issuing a query cannot pass by accident.
    """

    def __init__(self, script: dict[tuple, list[Any]]) -> None:
        self._script = script
        self.seen: list[tuple] = []

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> FakeResult:
        shape = _shape(statement)
        for key, rows in self._script.items():
            if shape is not None and _matches(shape, key):
                self.seen.append(shape)
                return FakeResult(list(rows))
        raise UnsupportedStatementError(
            "scripted session received a statement shape it does not model.\n"
            f"  actual:   {shape}\n"
            f"  scripted: {sorted((str(k[0]), k[1], k[2]) for k in self._script)}"
        )

    async def scalar(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        """Mirrors ``AsyncSession.scalar``: the first column of the first row."""
        return (await self.execute(statement)).scalar()

    async def close(self) -> None:
        return None


@pytest.fixture
def use_scripted_db():
    """
    Install a scripted session on ``app.dependency_overrides[get_db]``.

    Yields a setter; teardown always restores the previous override (which is
    ``None`` whenever ``TEST_DATABASE_URL`` is unset, as in the default suite).
    """
    from main import app

    previous = app.dependency_overrides.get(get_db)

    def _install(script: dict[tuple, list[Any]]) -> _ScriptedSession:
        session = _ScriptedSession(script)

        async def _get_db() -> AsyncIterator[_ScriptedSession]:
            try:
                yield session
            finally:
                await session.close()

        app.dependency_overrides[get_db] = _get_db
        return session

    try:
        yield _install
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous


# ---------------------------------------------------------------------------
# Row builders — real ORM instances, real mapped column values
# ---------------------------------------------------------------------------


def _user(username: str = "contract-user") -> User:
    return User(
        id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
        github_id=90001,
        github_username=username,
        access_token=None,
        avatar_url=None,
        role="user",
    )


def _repo(user: User, owner: str = "acme", name: str = "widgets") -> Repo:
    return Repo(
        id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
        user_id=user.id,
        owner=owner,
        name=name,
        default_branch="main",
        created_at=_FIXED_NOW,
    )


def _run(repo: Repo, status: str = "error", **overrides: Any) -> Run:
    values: dict[str, Any] = {
        "id": uuid.UUID("33333333-3333-4333-8333-333333333333"),
        "repo_id": repo.id,
        "github_run_id": 90001,
        "github_delivery_id": "delivery-contract-1",
        "head_sha": "a" * 40,
        "head_branch": "main",
        "status": status,
        "conclusion": "failure",
        "diagnosis_summary": "Null deref in request handler",
        "created_at": _FIXED_NOW,
        "updated_at": _FIXED_NOW,
    }
    values.update(overrides)
    return Run(**values)


def _attempt(
    run: Run, patch_text: str = _PATCH_TEXT, attempt_number: int = 1
) -> Attempt:
    return Attempt(
        id=uuid.uuid4(),
        run_id=run.id,
        attempt_number=attempt_number,
        patch_text=patch_text,
        confidence_score=91,
        strategy_notes="Null-guard the handler return",
        verification_status="pass",
        failure_reason=None,
        build_duration_ms=3100,
        created_at=_FIXED_NOW + timedelta(seconds=2),
    )


def _code_review(
    repo: Repo,
    *,
    severity: str,
    risk_score: int,
    status: str = "completed",
    failure_reason: Optional[str] = None,
    offset_hours: int = 0,
) -> CodeReview:
    return CodeReview(
        id=uuid.uuid4(),
        repo_id=repo.id,
        commit_sha=f"{severity[0]}" * 40,
        pr_number=42,
        risk_score=risk_score,
        summary=f"Review with a {severity} finding",
        findings=[
            {
                "file_path": "app/handlers.py",
                "line_start": 14,
                "line_end": 16,
                "category": "logic",
                "severity": severity,
                "critique": f"{severity}: unhandled return path",
                "suggested_patch": None,
            }
        ],
        status=status,
        failure_reason=failure_reason,
        input_tokens=900,
        output_tokens=260,
        created_at=_FIXED_NOW - timedelta(hours=offset_hours),
    )


def _auth_select() -> tuple:
    """Shape key for the ``select(User).where(User.id == ...)`` auth lookup."""
    return (User, True, _n_columns(User), frozenset({"users.id"}))


# ---------------------------------------------------------------------------
# C1 — the generated patch never leaves the trace endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_trace_attempt_payload_carries_the_generated_patch(
    make_auth_client, use_scripted_db
) -> None:
    """
    C1: ``GET /runs/{run_id}/trace`` must return each attempt's patch text.

    Plain terms: a run has a fix attempt; the diff the Fix Generator produced is
    stored on that attempt; asking the trace endpoint for the run must hand that
    diff back inside the attempt, because the dashboard has no other way to show
    the user what was changed.

    Drive the real route (traces.py:206-290) and the real response model
    (``TraceOut.attempts`` -> ``AttemptOut``) and assert on the serialised JSON,
    so the assertion is about the wire contract and not about the schema object.
    """
    user = _user()
    repo = _repo(user)
    run = _run(repo, status="pr_opened")
    attempt = _attempt(run)

    use_scripted_db(
        {
            # get_current_user -> auth.py:339
            _auth_select(): [user],
            # traces.py:219-228 — run lookup, ownership-joined on repos
            (
                Run,
                True,
                _n_columns(Run),
                frozenset({"runs.id", "repos.user_id"}),
            ): [run],
            # traces.py:231-236 — timeline steps
            (RunStep, True, _n_columns(RunStep), frozenset({"run_steps.run_id"})): [],
            # traces.py:239-244 — the attempts, ASC by attempt_number
            (Attempt, True, _n_columns(Attempt), frozenset({"attempts.run_id"})): [
                attempt
            ],
            # traces.py:267-279 — retry/refinement children of this run
            (
                Run,
                True,
                _n_columns(Run),
                frozenset({"runs.parent_run_id", "repos.user_id"}),
            ): [],
        }
    )

    async with make_auth_client(user.id) as ac:
        resp = await ac.get(f"/runs/{run.id}/trace")

    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert len(data["attempts"]) == 1, "expected exactly the one attempt the run has"
    payload = data["attempts"][0]

    # The attempt is identified correctly, so a later failure can only be about
    # the missing field rather than about the wrong row being returned.
    assert payload["attempt_number"] == attempt.attempt_number

    assert "patch_text" in payload, (
        "GET /runs/{id}/trace dropped the generated patch: the attempt payload has "
        f"no 'patch_text' key. Keys present: {sorted(payload)}. The column is "
        "NOT NULL on attempts (app/models.py:298) and the dashboard renders "
        "attempts[].patch_text in the diff viewer."
    )
    assert payload["patch_text"] == _PATCH_TEXT, (
        "the patch text was returned but does not match what was stored on the attempt"
    )


# ---------------------------------------------------------------------------
# C2 — the status filter rejects statuses the orchestrator writes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_status_filter_accepts_every_status_the_orchestrator_writes(
    make_auth_client, use_scripted_db
) -> None:
    """
    C2: every value in the orchestrator's own ``RunStatus`` must be accepted by
    ``GET /runs?status=``.

    Plain terms: the filter dropdown may only offer states the pipeline can
    actually reach. If the pipeline writes a state the filter rejects, the user
    picks a perfectly valid option and gets an HTTP 422 error instead of a list.

    The vocabulary is read from ``app.orchestrator.RunStatus`` at run time rather
    than hardcoded, so this reproduces the *drift* itself: adding a status to the
    state machine without widening the allowlist turns this red on its own.
    """
    from app.orchestrator import RunStatus

    user = _user()

    # Empty result set: the assertion is about acceptance, not about rows. The
    # COUNT, the failure-signature cluster scan and the paginated page are all
    # answered with "nothing matched", which is what list_runs expects at
    # total == 0 (traces.py:598-612).
    use_scripted_db(
        {
            _auth_select(): [user],
            # The predicate set legitimately grows with the query params (a
            # supplied `status` adds `runs.status`, `repo_id`/`from`/`to` add
            # more), so these three are matched on (entity, whole-entity,
            # column-count) alone — pinning the WHERE here would make the
            # reproduction depend on which filters the caller sent.
            # traces.py:581-583 — SELECT count(runs.id)
            (Run, False, 1, _ANY_WHERE): [0],
            # traces.py:599-608 — signature cluster scan
            (Run, False, 2, _ANY_WHERE): [],
            # traces.py:620-630 — the paginated page (run + cost + tokens)
            (Run, True, _n_columns(Run) + 2, _ANY_WHERE): [],
        }
    )

    rejected: list[str] = []
    async with make_auth_client(user.id) as ac:
        for status in RunStatus:
            resp = await ac.get("/runs", params={"status": status.value})
            if resp.status_code != 200:
                rejected.append(f"{status.value} -> HTTP {resp.status_code}")

    assert rejected == [], (
        "GET /runs?status=<x> answers 422 for statuses the pipeline itself writes, "
        "so a valid filter option surfaces as an error instead of a list. "
        "Rejected: " + ", ".join(rejected)
    )


# ---------------------------------------------------------------------------
# C3 — `total` must agree with the rows a severity filter leaves behind
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reviews_total_counts_only_severity_matched_reviews(
    make_auth_client, use_scripted_db
) -> None:
    """
    C3a: on ``GET /reviews``, ``total`` must reflect the ``severity`` filter.

    Plain terms: two reviews exist, exactly one has a `high` finding. Asking for
    `?severity=high` must report one review — both in the returned list and in
    the "total" number the UI prints next to it. Reporting 2 there tells the user
    there is another high-severity review on the page that does not exist.

    Both reviews fit in one page (default limit 20), so `total` and the row count
    must be equal here.
    """
    user = _user()
    repo = _repo(user)
    high = _code_review(repo, severity="high", risk_score=80)
    low = _code_review(repo, severity="low", risk_score=10, offset_hours=1)

    use_scripted_db(
        {
            _auth_select(): [user],
            # reviews.py:196-198 — the caller's repo ids
            (Repo, False, 1, frozenset({"repos.user_id"})): [repo.id],
            # reviews.py:204-220 — SELECT count(*) over code_reviews
            (None, False, 1, _ANY_WHERE): [2],
            # reviews.py:222-229 — the page of rows
            (CodeReview, True, _n_columns(CodeReview), _ANY_WHERE): [high, low],
        }
    )

    async with make_auth_client(user.id) as ac:
        resp = await ac.get("/reviews", params={"severity": "high"})

    assert resp.status_code == 200, resp.text
    data = resp.json()

    # Guard against the cheap fix (return no rows and a matching zero): the
    # filtered review must still be there.
    assert [r["id"] for r in data["reviews"]] == [str(high.id)], (
        "severity=high must return the one review that has a high finding"
    )

    assert data["total"] == len(data["reviews"]) == 1, (
        "total contradicts the rows: the severity filter is applied in Python "
        "after the COUNT (reviews.py:233-239), so a severity-filtered page "
        f"reported total={data['total']} while returning {len(data['reviews'])} "
        "row(s)"
    )


@pytest.mark.asyncio
async def test_repo_reviews_total_counts_only_severity_matched_reviews(
    make_auth_client, use_scripted_db
) -> None:
    """
    C3b: same defect on ``GET /repos/{repo_id}/reviews`` (reviews.py:158-177).

    Plain terms: identical to C3a but scoped to one repository, which is the
    endpoint the repos page uses. It shares the count-before-filter ordering, so
    it needs its own regression test — a fix applied only to ``GET /reviews``
    leaves this one broken.
    """
    user = _user()
    repo = _repo(user)
    critical = _code_review(repo, severity="critical", risk_score=95)
    medium = _code_review(repo, severity="medium", risk_score=55, offset_hours=1)

    use_scripted_db(
        {
            _auth_select(): [user],
            # reviews.py:139-141 — ownership-checked repo lookup
            (Repo, True, _n_columns(Repo), frozenset({"repos.id", "repos.user_id"})): [
                repo
            ],
            # reviews.py:148-158 — SELECT count(*) for this repo
            (None, False, 1, _ANY_WHERE): [2],
            # reviews.py:160-167 — the page of rows
            (CodeReview, True, _n_columns(CodeReview), _ANY_WHERE): [critical, medium],
        }
    )

    async with make_auth_client(user.id) as ac:
        resp = await ac.get(
            f"/repos/{repo.id}/reviews", params={"severity": "critical"}
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert [r["id"] for r in data["reviews"]] == [str(critical.id)], (
        "severity=critical must return the one review that has a critical finding"
    )

    assert data["total"] == len(data["reviews"]) == 1, (
        "total contradicts the rows: the severity filter is applied in Python "
        "after the COUNT (reviews.py:171-177), so a severity-filtered page "
        f"reported total={data['total']} while returning {len(data['reviews'])} "
        "row(s)"
    )
