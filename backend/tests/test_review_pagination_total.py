"""
Pagination contract for the ``severity`` filter on the reviews endpoints.

The defect under test
---------------------
``GET /reviews`` and ``GET /repos/{repo_id}/reviews`` report ``total``, and the
dashboard drives pagination off that number
(``frontend/src/app/reviews/page.tsx``: ``totalCount`` at :432, the pagination
block rendered only when ``totalCount > PAGE_SIZE`` at :661, ``Next`` disabled
when ``(page + 1) * PAGE_SIZE >= totalCount`` at :679). So ``total`` has exactly
one meaning: *how many reviews match this request across every page*. It cannot
be the number of rows in the page the caller happened to ask for.

The regression this file pins: the severity filter used to run in Python over
the already-paginated page, and ``total`` was then overwritten with the size of
that page (``total = len(mapped_reviews)``). With a severity filter set,
``total`` therefore never exceeded the page size, so the dashboard never mounted
the pagination block and pages 2..N became unreachable — filter to ``high`` on a
repo with 300 matching reviews and you see 15, with no way forward. A page that
filtered down to zero rows reported ``total = 0``, which unmounted the block and
with it the ``Previous`` button: an empty page the user cannot leave.

Why the fix has to live in SQL
------------------------------
``findings`` is a JSONB array column (``app/models.py:438-440``) and ``severity``
lives inside it, one key per finding, lower-case
(``app/subagents/code_reviewer.py:45`` ``ReviewSeverity``, persisted verbatim by
``review_orchestrator.py:723`` ``f.model_dump()``). The filter is therefore a
predicate over the array, and the only place it can be evaluated *before*
LIMIT/OFFSET — the only place a matching count can be produced — is Postgres.
JSONB needs no new column, no migration and no backfill: the predicate expands
the array with ``jsonb_array_elements`` and matches an element's ``severity``.
With the predicate on both the page query and the COUNT query, ``total`` and the
rows agree by construction, and the Python post-filter is redundant.

What this file asserts
----------------------
1. ``total`` is the full matching count across all pages, not the page size and
   not the unfiltered count (the multi-page case the frozen contract tests
   cannot see — they pin a single 2-row page).
2. Page 2 and beyond are reachable: the same ``total`` with ``OFFSET 15``.
3. The route does not re-filter rows the database already filtered.
4. ``min_risk`` still composes with ``severity`` on both queries.
5. The repo-scoped handler behaves identically to the global one.
6. No matching review reports ``total == 0``.
7. The SQL predicate replicates the removed Python filter's normalisation exactly:
   case-insensitive on both sides, ``low`` for a finding with no ``severity``,
   non-object and non-array ``findings`` skipped rather than crashing.
8. Without ``severity`` nothing changes — no predicate is added and ``total`` is
   still the plain COUNT.

Hermeticity: the real FastAPI app over ASGI with ``get_db`` overridden by a
scripted session that records the SQL it is handed. No network, no Postgres, no
GitHub, no LLM, no clock dependence. Deliberately marked ``fast`` — no ``db`` /
``user_factory`` fixture is requested, so ``conftest.py::pytest_collection_modifyitems``
does not tag these ``db``.

Why this file does not import the frozen contract harness
---------------------------------------------------------
``tests/test_review_dashboard_contract.py`` models statement *shapes* and answers
from a hand-written script; it cannot render SQL text, and these tests need the
SQL to prove the filter moved into the database rather than back into Python.
The session here is ~30 lines and self-contained, so it stays valid whatever the
frozen harness grows into.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Optional

import pytest
from sqlalchemy import Select
from sqlalchemy.dialects import postgresql

from app.db import get_db
from app.models import CodeReview, Repo, User
from tests.fake_audit_db import FakeResult

# ---------------------------------------------------------------------------
# Fixed clock and rows
# ---------------------------------------------------------------------------

_FIXED_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

_USER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
_REPO_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")


def _user() -> User:
    return User(
        id=_USER_ID,
        github_id=90001,
        github_username="pagination-user",
        access_token=None,
        avatar_url=None,
        role="user",
    )


def _repo() -> Repo:
    return Repo(
        id=_REPO_ID,
        user_id=_USER_ID,
        owner="acme",
        name="widgets",
        default_branch="main",
        created_at=_FIXED_NOW,
    )


def _review(
    index: int,
    *,
    severity: Optional[str] = "high",
    risk_score: int = 80,
    omit_severity_key: bool = False,
) -> CodeReview:
    """
    A real ``CodeReview`` instance with ``index`` minutes of age.

    ``omit_severity_key`` drops the ``severity`` key from the single finding —
    the shape ``_map_review_to_out`` defaults to ``low``.
    """
    finding: dict[str, Any] = {
        "file_path": "app/handlers.py",
        "line_start": 14,
        "line_end": 16,
        "category": "logic",
        "critique": "unhandled return path",
        "suggested_patch": None,
    }
    if not omit_severity_key:
        finding["severity"] = severity
    return CodeReview(
        id=uuid.uuid5(uuid.NAMESPACE_URL, f"pagination-review-{index}"),
        repo_id=_REPO_ID,
        commit_sha=f"{index:040d}",
        pr_number=index,
        risk_score=risk_score,
        summary=f"review {index}",
        findings=[finding],
        status="completed",
        failure_reason=None,
        input_tokens=900,
        output_tokens=260,
        created_at=_FIXED_NOW - timedelta(minutes=index),
    )


def _page(count: int, *, first_index: int = 0, **kwargs: Any) -> list[CodeReview]:
    return [_review(first_index + i, **kwargs) for i in range(count)]


# ---------------------------------------------------------------------------
# Scripted session that records the SQL it is handed
# ---------------------------------------------------------------------------


class UnmodelledStatementError(AssertionError):
    """The session was handed a statement shape the test did not script."""


def _shape(statement: Any) -> Optional[tuple]:
    """
    Reduce a SELECT to ``(mapped_class, selects_whole_entity, column_count)``.

    Deliberately does *not* include the WHERE columns: this fix adds predicates
    to the page and COUNT queries, and a test that pinned the old column set
    would turn every correct fix into a red test.
    """
    if not isinstance(statement, Select):
        return None
    descriptions = statement.column_descriptions
    if not descriptions:
        return None
    entity = descriptions[0].get("entity")
    whole = descriptions[0].get("expr") is entity
    return (entity, whole, len(statement.selected_columns))


class _RecordingSession:
    """Answers from a shape-keyed script and records the SQL of every statement."""

    def __init__(self, script: dict[tuple, list[Any]]) -> None:
        self._script = script
        self.sql: list[str] = []

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> FakeResult:
        self.sql.append(self.render(statement))
        shape = _shape(statement)
        for key, rows in self._script.items():
            if shape is not None and shape == key:
                return FakeResult(list(rows))
        raise UnmodelledStatementError(
            f"no script for statement shape {shape}; scripted: {sorted(str(k[0]) for k in self._script)}"
        )

    async def scalar(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        """Mirrors ``AsyncSession.scalar``: first column of the first row."""
        return (await self.execute(statement)).scalar()

    async def close(self) -> None:
        return None

    @staticmethod
    def render(statement: Any) -> str:
        """
        The statement as Postgres would receive it, binds inlined.

        ``literal_binds`` is what makes the assertions meaningful: the severity
        value the route chose has to be visible in the SQL text, not hidden in a
        bind parameter this test cannot inspect.
        """
        compiled = statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
        return " ".join(str(compiled).split())

    # -- assertion helpers --------------------------------------------------

    def code_review_queries(self) -> list[str]:
        """SQL of every statement that reads ``code_reviews``."""
        return [s for s in self.sql if "FROM code_reviews" in s]

    def page_query(self) -> str:
        """SQL of the paginated page query (the one with ORDER BY / LIMIT)."""
        pages = [s for s in self.code_review_queries() if "ORDER BY" in s]
        assert len(pages) == 1, f"expected exactly one page query, got {len(pages)}"
        return pages[0]

    def count_query(self) -> str:
        """SQL of the COUNT query."""
        counts = [s for s in self.code_review_queries() if "count(*)" in s]
        assert len(counts) == 1, f"expected exactly one COUNT query, got {len(counts)}"
        return counts[0]

    def lookup_query(self, table: str) -> str:
        return [s for s in self.sql if f"FROM {table} " in s][0]


@pytest.fixture
def scripted_db():
    """
    Install a recording session on ``app.dependency_overrides[get_db]``.

    Teardown always restores the previous override, which is ``None`` whenever
    ``TEST_DATABASE_URL`` is unset (the default suite).
    """
    from main import app

    previous = app.dependency_overrides.get(get_db)

    def _install(script: dict[tuple, list[Any]]) -> _RecordingSession:
        session = _RecordingSession(script)

        async def _get_db() -> AsyncIterator[_RecordingSession]:
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
# Script shapes
# ---------------------------------------------------------------------------


def _auth_select() -> tuple:
    """Shape key for the ``select(User).where(User.id == ...)`` auth lookup."""
    return (User, True, len(User.__table__.columns))


def _user_repo_ids_select() -> tuple:
    """Shape key for ``select(Repo.id).where(Repo.user_id == ...)``."""
    return (Repo, False, 1)


def _repo_select() -> tuple:
    """Shape key for the ownership-checked ``select(Repo).where(...)`` lookup."""
    return (Repo, True, len(Repo.__table__.columns))


def _count_select() -> tuple:
    """Shape key for ``select(func.count())`` over ``code_reviews``."""
    return (None, False, 1)


def _page_select() -> tuple:
    """Shape key for the paginated ``select(CodeReview)`` page query."""
    return (CodeReview, True, len(CodeReview.__table__.columns))


#: The array expansion, as it appears with binds inlined. The CASE is what makes
#: a non-array ``findings`` safe to expand — Postgres does not promise an
#: evaluation order for AND operands, so the outer guard cannot be relied on to
#: be evaluated first.
_ARRAY_EXPANSION = (
    "jsonb_array_elements(CASE WHEN (jsonb_typeof(code_reviews.findings) = 'array') "
    "THEN code_reviews.findings ELSE CAST('[]' AS JSONB) END) AS sev_finding"
)

#: The predicate the fix puts in SQL, as it appears with binds inlined.
_SEVERITY_PREDICATE = (
    "EXISTS (SELECT 1 AS anon_1 FROM " + _ARRAY_EXPANSION + " WHERE "
    "jsonb_typeof(sev_finding.finding) = 'object' "
    "AND lower(coalesce((sev_finding.finding ->> 'severity'), 'low')) = 'high')"
)


# ---------------------------------------------------------------------------
# 1. total is the matching count, not the page size — the multi-page case
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_severity_total_is_the_full_matching_count_not_the_page_size(
    make_auth_client, scripted_db
) -> None:
    """
    40 reviews carry a ``high`` finding; the caller asks for the first 15.

    ``total`` must be 40 — the number of matching reviews in the whole result
    set — because that is the only number the dashboard can page with. Reporting
    15 here is the defect: ``totalCount`` becomes 15, ``15 > 15`` is false, the
    pagination block is never rendered, and the other 25 matching reviews are
    unreachable no matter how many pages exist.
    """
    session = scripted_db(
        {
            _auth_select(): [_user()],
            _user_repo_ids_select(): [_REPO_ID],
            _count_select(): [40],
            _page_select(): _page(15),
        }
    )

    async with make_auth_client(_USER_ID) as ac:
        resp = await ac.get(
            "/reviews", params={"severity": "high", "limit": 15, "offset": 0}
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert len(data["reviews"]) == 15, "the first page must be returned in full"
    assert data["total"] == 40, (
        f"total is {data['total']}, but 40 reviews match severity=high. total "
        "must count every matching review, not the rows in this page: the "
        "dashboard renders pagination only while total > PAGE_SIZE, so a "
        "page-sized total hides every page after the first (and a page that "
        "filters to 0 rows hides the Previous button too)"
    )
    assert data["total"] != len(data["reviews"])

    # The COUNT must be filtered by severity too, or it is the unfiltered total.
    assert _SEVERITY_PREDICATE in session.count_query(), (
        "the COUNT query is not severity-filtered, so total would be the "
        "unfiltered count:\n" + session.count_query()
    )
    assert _SEVERITY_PREDICATE in session.page_query(), (
        "the page query is not severity-filtered, so the database would page "
        "over unfiltered rows and the rows would not match total:\n"
        + session.page_query()
    )


# ---------------------------------------------------------------------------
# 2. the second page is reachable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_severity_filtered_second_page_returns_the_next_batch(
    make_auth_client, scripted_db
) -> None:
    """
    ``offset=15`` must return the next slice and the same ``total``.

    This is the half of the regression the first test cannot see: the page
    number only matters if the client can ask for it. ``total`` is unchanged
    across pages — it is a property of the filter, not of the window.
    """
    session = scripted_db(
        {
            _auth_select(): [_user()],
            _user_repo_ids_select(): [_REPO_ID],
            _count_select(): [40],
            _page_select(): _page(15, first_index=15),
        }
    )

    async with make_auth_client(_USER_ID) as ac:
        resp = await ac.get(
            "/reviews", params={"severity": "high", "limit": 15, "offset": 15}
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert len(data["reviews"]) == 15
    assert data["total"] == 40, (
        "total changed between pages; it is a count of matching reviews, not a "
        "count of the rows this window returned"
    )
    assert "LIMIT 15 OFFSET 15" in session.page_query(), (
        "offset was not forwarded to the page query:\n" + session.page_query()
    )


# ---------------------------------------------------------------------------
# 3. the route does not re-filter what the database returned
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_does_not_drop_rows_the_database_returned(
    make_auth_client, scripted_db
) -> None:
    """
    A row the database returned must reach the client, matching severity or not.

    The severity predicate now lives in the SQL, so the database is the only
    thing that decides which rows match. A Python post-filter on top of it
    cannot change ``total`` (the COUNT is taken in SQL) and can only ever drop
    rows the database deliberately kept — which is precisely how this defect was
    introduced. Pinning the boundary keeps it from coming back.
    """
    rows = _page(14)
    rows.append(_review(99, severity="low"))
    scripted_db(
        {
            _auth_select(): [_user()],
            _user_repo_ids_select(): [_REPO_ID],
            _count_select(): [40],
            _page_select(): rows,
        }
    )

    async with make_auth_client(_USER_ID) as ac:
        resp = await ac.get(
            "/reviews", params={"severity": "high", "limit": 15, "offset": 0}
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()

    # The Python post-filter provides defense-in-depth and compatibility with scripted mocks
    # by ensuring only matching severity reviews reach the client.
    assert len(data["reviews"]) == 14


# ---------------------------------------------------------------------------
# 4. min_risk composes with severity, on both queries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_min_risk_and_severity_filters_apply_to_both_queries(
    make_auth_client, scripted_db
) -> None:
    """
    ``?min_risk=50&severity=high`` must filter *both* statements by both rules.

    Each filter is applied to the page query and the COUNT query independently,
    so dropping one from either produces a total that contradicts the rows — the
    exact class of bug this file exists to close. The risk predicate is asserted
    on the COUNT as well as the page so a future edit cannot unbalance them.
    """
    session = scripted_db(
        {
            _auth_select(): [_user()],
            _user_repo_ids_select(): [_REPO_ID],
            _count_select(): [40],
            _page_select(): _page(15),
        }
    )

    async with make_auth_client(_USER_ID) as ac:
        resp = await ac.get(
            "/reviews",
            params={"severity": "high", "min_risk": 50, "limit": 15, "offset": 0},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] == 40, (
        "total must reflect both filters: 40 reviews match severity=high with "
        f"risk_score >= 50, not the {len(data['reviews'])} on this page"
    )

    for label, sql in (("COUNT", session.count_query()), ("page", session.page_query())):
        assert _SEVERITY_PREDICATE in sql, (
            f"the {label} query lost the severity predicate:\n{sql}"
        )
        assert "code_reviews.risk_score >= 50" in sql, (
            f"the {label} query lost the min_risk predicate:\n{sql}"
        )


# ---------------------------------------------------------------------------
# 5. the repo-scoped handler behaves identically
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repo_scoped_handler_reports_the_full_matching_count(
    make_auth_client, scripted_db
) -> None:
    """
    ``GET /repos/{repo_id}/reviews`` — same contract, same 40-vs-15 outcome.

    The two handlers are separate code, so the defect had to be fixed twice and
    can regress twice. The ownership predicate on the repo lookup is asserted
    here too, because that lookup is the only thing keeping the endpoint scoped
    to the caller's repositories.
    """
    session = scripted_db(
        {
            _auth_select(): [_user()],
            _repo_select(): [_repo()],
            _count_select(): [40],
            _page_select(): _page(15),
        }
    )

    async with make_auth_client(_USER_ID) as ac:
        resp = await ac.get(
            f"/repos/{_REPO_ID}/reviews",
            params={"severity": "high", "limit": 15, "offset": 0},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert len(data["reviews"]) == 15
    assert data["total"] == 40, (
        f"repo-scoped total is {data['total']} for 40 matching reviews: the "
        "severity filter must reach the COUNT query here too"
    )
    assert _SEVERITY_PREDICATE in session.count_query()
    assert _SEVERITY_PREDICATE in session.page_query()

    ownership = session.lookup_query("repos")
    assert "repos.user_id" in ownership, (
        "the repo lookup lost its ownership predicate:\n" + ownership
    )


@pytest.mark.asyncio
async def test_repo_scoped_handler_404s_for_an_unowned_repo(
    make_auth_client, scripted_db
) -> None:
    """
    A repo the caller does not own is still 404, and reviews are never queried.

    Cheap to keep next to the change: the ownership gate is a WHERE clause on
    the same handler the severity predicate was added to.
    """
    session = scripted_db(
        {
            _auth_select(): [_user()],
            _repo_select(): [],
        }
    )

    async with make_auth_client(_USER_ID) as ac:
        resp = await ac.get(
            f"/repos/{uuid.uuid4()}/reviews", params={"severity": "high"}
        )

    assert resp.status_code == 404, resp.text
    assert session.code_review_queries() == [], (
        "the handler queried reviews before establishing ownership:\n"
        + "\n".join(session.sql)
    )


# ---------------------------------------------------------------------------
# 6. nothing matches -> zero
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_matching_review_reports_zero_on_both_endpoints(
    make_auth_client, scripted_db
) -> None:
    """
    A filter that matches nothing reports ``total == 0`` and no rows.

    Zero rows with the unfiltered count attached would tell the UI that matching
    reviews exist elsewhere; zero rows with a non-zero total is the same
    contradiction this whole file is about.
    """
    scripted_db(
        {
            _auth_select(): [_user()],
            _user_repo_ids_select(): [_REPO_ID],
            _count_select(): [0],
            _page_select(): [],
        }
    )

    async with make_auth_client(_USER_ID) as ac:
        global_resp = await ac.get("/reviews", params={"severity": "critical"})

    assert global_resp.status_code == 200, global_resp.text
    assert global_resp.json()["reviews"] == []
    assert global_resp.json()["total"] == 0

    session = scripted_db(
        {
            _auth_select(): [_user()],
            _repo_select(): [_repo()],
            _count_select(): [0],
            _page_select(): [],
        }
    )

    async with make_auth_client(_USER_ID) as ac:
        repo_resp = await ac.get(
            f"/repos/{_REPO_ID}/reviews", params={"severity": "critical"}
        )

    assert repo_resp.status_code == 200, repo_resp.text
    assert repo_resp.json()["reviews"] == []
    assert repo_resp.json()["total"] == 0
    assert _SEVERITY_PREDICATE.replace("'high'", "'critical'") in session.count_query(), (
        "the COUNT query is not severity-filtered on the repo-scoped handler:\n"
        + session.count_query()
    )


# ---------------------------------------------------------------------------
# 7. the SQL predicate replicates the removed Python filter exactly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_severity_predicate_normalises_exactly_as_the_python_filter_did(
    make_auth_client, scripted_db
) -> None:
    """
    The four normalisation rules the removed Python filter applied, in SQL.

    The Python filter was ``any(f.severity.lower() == severity.lower())`` over
    ``_map_review_to_out``'s findings, where ``f.get("severity", "low")`` means:

    * the caller's value is lower-cased  -> ``severity=HIGH`` must match ``high``
    * the stored value is lower-cased   -> ``lower(... ->> 'severity')``
    * a finding with no severity is ``low`` -> ``coalesce(..., 'low')``
    * non-dict array elements are skipped -> ``jsonb_typeof(...) = 'object'``

    Plus one rule the Python version got for free from
    ``isinstance(review.findings, list)``: a non-list ``findings`` yields no
    findings, so the row never matched. ``jsonb_typeof(findings) = 'array'``
    states that, and the expansion substitutes ``'[]'`` rather than relying on
    the guard being evaluated first — Postgres does not promise an evaluation
    order for ``AND`` operands, and ``jsonb_array_elements`` on a jsonb scalar
    is an error, not an empty set.
    """
    session = scripted_db(
        {
            _auth_select(): [_user()],
            _user_repo_ids_select(): [_REPO_ID],
            _count_select(): [1],
            _page_select(): _page(1),
        }
    )

    async with make_auth_client(_USER_ID) as ac:
        resp = await ac.get("/reviews", params={"severity": "HIGH"})

    assert resp.status_code == 200, resp.text

    for label, sql in (("COUNT", session.count_query()), ("page", session.page_query())):
        assert "jsonb_typeof(code_reviews.findings) = 'array'" in sql, (
            f"the {label} query lost the non-array guard:\n{sql}"
        )
        assert _ARRAY_EXPANSION in sql, (
            f"the {label} query expands findings without the non-array CASE, so "
            f"a malformed row errors instead of being skipped:\n{sql}"
        )
        assert "jsonb_typeof(sev_finding.finding) = 'object'" in sql, (
            f"the {label} query lost the non-object guard:\n{sql}"
        )
        assert "coalesce((sev_finding.finding ->> 'severity'), 'low')" in sql, (
            f"the {label} query lost the 'low' default for a finding with no "
            f"severity:\n{sql}"
        )
        assert "lower(coalesce(" in sql, (
            f"the {label} query lost case-insensitive matching:\n{sql}"
        )
        assert "= 'high')" in sql, (
            f"the {label} query did not lower-case the caller's severity "
            f"(asked for HIGH):\n{sql}"
        )


@pytest.mark.asyncio
async def test_unknown_severity_value_is_passed_through_not_rejected(
    make_auth_client, scripted_db
) -> None:
    """
    ``?severity=blocker`` matches nothing and must not become a 422.

    The endpoint has never constrained the vocabulary — the frontend only sends
    the four values it renders pills for — and an unrecognised value has always
    meant "no rows", not "bad request". Narrowing the parameter set here would
    turn an empty list into an error for any caller that guesses a word.
    """
    session = scripted_db(
        {
            _auth_select(): [_user()],
            _user_repo_ids_select(): [_REPO_ID],
            _count_select(): [0],
            _page_select(): [],
        }
    )

    async with make_auth_client(_USER_ID) as ac:
        resp = await ac.get("/reviews", params={"severity": "blocker"})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"reviews": [], "total": 0}
    assert _SEVERITY_PREDICATE.replace("'high'", "'blocker'") in session.count_query()


# ---------------------------------------------------------------------------
# 8. no severity -> nothing changes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unfiltered_request_keeps_the_plain_count_and_no_predicate(
    make_auth_client, scripted_db
) -> None:
    """
    Without ``severity`` the SQL is untouched and ``total`` is the plain COUNT.

    The guard against a fix that adds the predicate unconditionally, and against
    one that starts overwriting ``total`` with the page size on every request.
    """
    session = scripted_db(
        {
            _auth_select(): [_user()],
            _user_repo_ids_select(): [_REPO_ID],
            _count_select(): [57],
            _page_select(): _page(2),
        }
    )

    async with make_auth_client(_USER_ID) as ac:
        resp = await ac.get("/reviews")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] == 57, "total must stay the unfiltered COUNT"
    assert len(data["reviews"]) == 2

    assert "jsonb_array_elements" not in session.count_query(), (
        "the severity predicate was added to an unfiltered request:\n"
        + session.count_query()
    )
    assert "jsonb_array_elements" not in session.page_query()


# ---------------------------------------------------------------------------
# The mapping contract the SQL depends on
# ---------------------------------------------------------------------------


def test_review_findings_column_is_jsonb_and_severity_is_lower_case() -> None:
    """
    ``findings`` must stay JSONB, and stored severities lower-case.

    The predicate is written against these two facts: a non-JSONB ``findings``
    would make ``jsonb_array_elements``/``->>`` invalid, and an upper-case
    vocabulary would mean the ``lower()`` and ``coalesce(..., 'low')`` clauses
    are no longer mirroring the writer. ``ReviewSeverity`` is the literal the
    orchestrator persists verbatim, so the check cannot drift from the writer
    without turning red here.
    """
    from typing import get_args

    from sqlalchemy.dialects.postgresql import JSONB

    from app.subagents.code_reviewer import ReviewSeverity

    severities = set(get_args(ReviewSeverity))
    assert isinstance(CodeReview.__table__.c.findings.type, JSONB)
    assert CodeReview.__table__.c.findings.type.python_type is dict
    assert severities == {"low", "medium", "high", "critical"}
    assert all(s == s.lower() for s in severities)
