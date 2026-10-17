"""
Issue #116 — dashboard read fan-out regression pins (hermetic, ``fast``).

Three sub-problems, one file:

  P1  Reviews page per-row associated-PR fan-out: N rows without ``pr_number``
      fire N ``GET /reviews/{id}/associated-pr`` (N HTTP + N DB round-trips).
      The fix adds ``POST /reviews/associated-pr/batch`` — one HTTP call, one
      ownership-scoped ``SELECT ... WHERE id IN (...)``, concurrent GitHub
      resolution only for rows the DB cannot answer.
  P2  Severity double-filter: the SQL JSONB predicate plus a Python post-filter
      (reviews.py list endpoints). Pinned by query-count (exactly two
      ``code_reviews`` statements per list render) and by
      ``test_review_pagination_total.py`` (single source of truth in SQL).
  P3  Eval user-metrics fan-out: ``GET /eval/user-metrics`` fired up to 8
      sequential round-trips (total / healed / conditional avg / cost / 4
      buckets). The fix answers everything with ONE aggregated statement.

Hermeticity: the real FastAPI app over ASGI with ``get_db`` overridden by a
scripted session. No network (GitHub mocked at the httpx seam via ``respx``),
no Postgres. Deliberately ``fast``: no ``db`` / ``user_factory`` fixtures.

The eval/batch tests below FAIL against the pre-fix code (fan-out raises
``UnsupportedStatementError`` / the batch route answers 405) and pass after.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

import pytest
import respx
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.selectable import Select

from app.config import settings
from app.db import get_db
from app.models import CodeReview, Repo, User
from tests.fake_audit_db import FakeResult

_FIXED_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_USER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
_REPO_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")

_BATCH_URL = "/reviews/associated-pr/batch"
_SHA_A = "a" * 40
_SHA_B = "b" * 40
_SHA_C = "c" * 40


def _user() -> User:
    return User(
        id=_USER_ID,
        github_id=90001,
        github_username="fanout-user",
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
    sha: str,
    pr_number: Optional[int] = None,
    severity: str = "high",
) -> CodeReview:
    return CodeReview(
        id=uuid.uuid5(uuid.NAMESPACE_URL, f"fanout-116-review-{index}"),
        repo_id=_REPO_ID,
        commit_sha=sha,
        pr_number=pr_number,
        risk_score=80,
        summary=f"review {index}",
        findings=[
            {
                "file_path": "app/handlers.py",
                "line_start": 14,
                "line_end": 16,
                "category": "logic",
                "severity": severity,
                "critique": "unhandled return path",
                "suggested_patch": None,
            }
        ],
        status="completed",
        failure_reason=None,
        input_tokens=900,
        output_tokens=260,
        created_at=_FIXED_NOW,
    )


class UnsupportedStatementError(AssertionError):
    """The scripted session was handed a statement shape it does not model."""


class _FanoutSession:
    """Dispatch auth by entity; fail loud on any unmodelled round-trip.

    ``aggregate_row`` answers the single post-fix user-metrics statement (a
    12-column aggregate). Any narrower aggregate statement is a pre-fix
    per-metric round-trip and raises, which is exactly the fan-out repro.
    """

    def __init__(
        self,
        user: User,
        *,
        reviews: list[CodeReview] | None = None,
        aggregate_row: tuple[Any, ...] | None = None,
    ) -> None:
        self._user = user
        self._reviews = list(reviews or [])
        self._aggregate_row = aggregate_row
        self.statements: list[Any] = []

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> FakeResult:
        self.statements.append(statement)
        entity = None
        if isinstance(statement, Select):
            descriptions = statement.column_descriptions
            entity = descriptions[0].get("entity") if descriptions else None
        if entity is User:
            return FakeResult([self._user])
        if entity is CodeReview:
            return FakeResult(list(self._reviews))
        if isinstance(statement, Select) and self._aggregate_row is not None:
            if len(statement.selected_columns) >= 10:
                return FakeResult([self._aggregate_row])
        raise UnsupportedStatementError(
            f"unmodelled statement: {type(statement).__name__} "
            f"entity={entity!r} columns="
            f"{len(statement.selected_columns) if isinstance(statement, Select) else '?'}"
        )

    async def scalar(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        return (await self.execute(statement)).scalar()

    async def close(self) -> None:
        return None

    def code_review_statements(self) -> list[Any]:
        return [
            s
            for s in self.statements
            if isinstance(s, Select)
            and s.column_descriptions
            and s.column_descriptions[0].get("entity") is CodeReview
        ]


@pytest.fixture
def fanout_db():
    """Install a :class:`_FanoutSession` on the app's ``get_db`` override."""
    from main import app

    previous = app.dependency_overrides.get(get_db)

    def _install(
        user: User,
        *,
        reviews: list[CodeReview] | None = None,
        aggregate_row: tuple[Any, ...] | None = None,
    ) -> _FanoutSession:
        session = _FanoutSession(user, reviews=reviews, aggregate_row=aggregate_row)

        async def _get_db() -> AsyncIterator[_FanoutSession]:
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


def _render(statement: Any) -> str:
    compiled = statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    )
    return " ".join(str(compiled).split())


# ---------------------------------------------------------------------------
# P3 — eval user-metrics: one round-trip, identical numbers
# ---------------------------------------------------------------------------

#: (total, healed, avg_secs, cost, b0_total, b0_passed, ..., b3_total, b3_passed)
_AGGREGATE_ROW: tuple[Any, ...] = (10, 4, 90.0, 2.5, 5, 3, 4, 4, 0, 0, 7, 2)


@pytest.mark.asyncio
async def test_user_metrics_single_round_trip_with_matching_numbers(
    make_auth_client, fanout_db
) -> None:
    """ONE non-auth statement answers the whole dashboard rollup."""
    session = fanout_db(_user(), aggregate_row=_AGGREGATE_ROW)

    async with make_auth_client(_USER_ID) as ac:
        resp = await ac.get("/eval/user-metrics")

    assert resp.status_code == 200, resp.text
    non_auth = [s for s in session.statements if not _is_user_lookup(s)]
    assert len(non_auth) == 1, (
        f"expected exactly 1 metrics round-trip, got {len(non_auth)} — "
        "the endpoint is fanning out per metric again"
    )
    data = resp.json()
    assert data["total_runs"] == 10
    assert data["healed_runs"] == 4
    assert data["success_rate_pct"] == 40.0
    assert data["avg_duration_seconds"] == 90.0
    assert data["total_cost"] == 2.5
    assert data["avg_cost_per_run"] == 0.25
    assert [b["bucket"] for b in data["confidence_calibration"]] == [
        "0-50%",
        "50-75%",
        "75-90%",
        "90-100%",
    ]
    assert [(b["total_attempts"], b["passed_attempts"]) for b in data["confidence_calibration"]] == [
        (5, 3),
        (4, 4),
        (0, 0),
        (7, 2),
    ]
    assert [b["accuracy_pct"] for b in data["confidence_calibration"]] == [
        60.0,
        100.0,
        0.0,
        28.6,
    ]


@pytest.mark.asyncio
async def test_user_metrics_single_statement_covers_every_metric(
    make_auth_client, fanout_db
) -> None:
    """The one statement must scope by tenant and cover every metric family."""
    session = fanout_db(_user(), aggregate_row=_AGGREGATE_ROW)

    async with make_auth_client(_USER_ID) as ac:
        resp = await ac.get("/eval/user-metrics")

    assert resp.status_code == 200, resp.text
    non_auth = [s for s in session.statements if not _is_user_lookup(s)]
    assert len(non_auth) == 1
    sql = _render(non_auth[0])
    for needle in (
        "repos.user_id",  # tenant scoping on every aggregate
        "pr_opened",
        "completed",  # healed definition
        "cost_estimate",  # cost family
        "confidence_score",  # bucket family
        "verification_status",  # pass counting
    ):
        assert needle in sql, f"single metrics query lost {needle!r}:\n{sql}"


@pytest.mark.asyncio
async def test_user_metrics_zero_rows_answers_zeros(
    make_auth_client, fanout_db
) -> None:
    """Empty tenant: no division by zero, four empty buckets, all zeros."""
    session = fanout_db(
        _user(), aggregate_row=(0, 0, None, 0.0, 0, 0, 0, 0, 0, 0, 0, 0)
    )

    async with make_auth_client(_USER_ID) as ac:
        resp = await ac.get("/eval/user-metrics")

    assert resp.status_code == 200, resp.text
    non_auth = [s for s in session.statements if not _is_user_lookup(s)]
    assert len(non_auth) == 1
    data = resp.json()
    assert data["total_runs"] == 0
    assert data["healed_runs"] == 0
    assert data["success_rate_pct"] == 0.0
    assert data["avg_duration_seconds"] == 0.0
    assert data["total_cost"] == 0.0
    assert data["avg_cost_per_run"] == 0.0
    assert len(data["confidence_calibration"]) == 4
    assert all(b["total_attempts"] == 0 for b in data["confidence_calibration"])


def _is_user_lookup(statement: Any) -> bool:
    return (
        isinstance(statement, Select)
        and bool(statement.column_descriptions)
        and statement.column_descriptions[0].get("entity") is User
    )


# ---------------------------------------------------------------------------
# P2 — reviews list: O(1) code_reviews statements per render
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reviews_list_renders_page_in_constant_queries(
    make_auth_client, fanout_db
) -> None:
    """A 100-row page still costs exactly 2 code_reviews statements (COUNT+page).

    Guards the O(1)-per-render contract: row count must never multiply the
    number of database round-trips.
    """
    rows = [
        _review(i, sha=f"{i:040d}", pr_number=i, severity="high") for i in range(100)
    ]
    session = fanout_db(_user(), reviews=rows)

    async with make_auth_client(_USER_ID) as ac:
        resp = await ac.get("/reviews", params={"severity": "high", "limit": 100})

    assert resp.status_code == 200, resp.text
    assert len(session.code_review_statements()) == 2, (
        f"expected COUNT + page only, got {len(session.code_review_statements())} "
        "code_reviews statements"
    )


# ---------------------------------------------------------------------------
# P1 — batch associated-PR lookup
# ---------------------------------------------------------------------------


def _pulls_url(sha: str) -> str:
    return f"https://api.github.com/repos/acme/widgets/commits/{sha}/pulls"


def _pr(number: int, *, state: str = "open") -> dict[str, Any]:
    return {
        "number": number,
        "state": state,
        "title": f"Change #{number}",
        "html_url": f"https://github.com/acme/widgets/pull/{number}",
    }


@pytest.mark.asyncio
@respx.mock
async def test_batch_resolves_mixed_page_in_one_db_round_trip(
    make_auth_client, fanout_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DB-known + GitHub-resolved + missing PRs answered by a single POST."""
    monkeypatch.setattr(settings, "github_token", "test-token")
    direct = _review(0, sha=_SHA_A, pr_number=77)
    direct.repo = _repo()
    via_github = _review(1, sha=_SHA_B, pr_number=None)
    via_github.repo = _repo()
    missing = _review(2, sha=_SHA_C, pr_number=None)
    missing.repo = _repo()
    session = fanout_db(_user(), reviews=[direct, via_github, missing])

    pulls_b = respx.get(_pulls_url(_SHA_B)).respond(status_code=200, json=[_pr(9)])
    pulls_c = respx.get(_pulls_url(_SHA_C)).respond(status_code=200, json=[])

    async with make_auth_client(_USER_ID) as ac:
        resp = await ac.post(
            _BATCH_URL,
            json={"review_ids": [str(direct.id), str(via_github.id), str(missing.id)]},
        )

    assert resp.status_code == 200, resp.text
    assert len(session.code_review_statements()) == 1, (
        "batch lookup must issue exactly one ownership-scoped SELECT, "
        f"got {len(session.code_review_statements())}"
    )
    assert pulls_b.called, "GitHub must be consulted for the unknown row"
    assert pulls_c.called
    data = resp.json()["associated_prs"]
    assert data[str(direct.id)] == {
        "number": 77,
        "url": "https://github.com/acme/widgets/pull/77",
        "title": None,
    }
    assert data[str(via_github.id)]["number"] == 9
    assert data[str(missing.id)] is None


@pytest.mark.asyncio
@respx.mock
async def test_batch_statement_filters_by_ids_and_owner(
    make_auth_client, fanout_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single SELECT must name the requested ids and the caller's user id."""
    monkeypatch.setattr(settings, "github_token", "test-token")
    row = _review(0, sha=_SHA_A, pr_number=77)
    row.repo = _repo()
    session = fanout_db(_user(), reviews=[row])

    async with make_auth_client(_USER_ID) as ac:
        resp = await ac.post(_BATCH_URL, json={"review_ids": [str(row.id)]})

    assert resp.status_code == 200, resp.text
    assert len(session.code_review_statements()) == 1
    compiled = _render(session.code_review_statements()[0]).replace("-", "")
    assert str(row.id).replace("-", "") in compiled
    assert str(_USER_ID).replace("-", "") in compiled


@pytest.mark.asyncio
async def test_batch_empty_ids_answers_empty_map(
    make_auth_client, fanout_db
) -> None:
    """Degenerate input is a 200 with an empty map, not an error."""
    fanout_db(_user(), reviews=[])

    async with make_auth_client(_USER_ID) as ac:
        resp = await ac.post(_BATCH_URL, json={"review_ids": []})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"associated_prs": {}}


@pytest.mark.asyncio
async def test_batch_unowned_id_answers_null_not_404(
    make_auth_client, fanout_db
) -> None:
    """Unknown / other-tenant ids resolve to null so the page keeps rendering."""
    fanout_db(_user(), reviews=[])

    async with make_auth_client(_USER_ID) as ac:
        resp = await ac.post(_BATCH_URL, json={"review_ids": [str(uuid.uuid4())]})

    assert resp.status_code == 200, resp.text
    assert list(resp.json()["associated_prs"].values()) == [None]


@pytest.mark.asyncio
async def test_batch_rejects_oversized_pages_with_422(
    make_auth_client, fanout_db
) -> None:
    """More ids than any page can hold (100) is a validation error."""
    fanout_db(_user(), reviews=[])

    async with make_auth_client(_USER_ID) as ac:
        resp = await ac.post(
            _BATCH_URL, json={"review_ids": [str(uuid.uuid4()) for _ in range(101)]}
        )

    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
@respx.mock
async def test_batch_missing_token_resolves_unknown_to_null(
    make_auth_client, fanout_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No server token: DB-known rows still answer, unknown rows are null."""
    monkeypatch.setattr(settings, "github_token", None)
    direct = _review(0, sha=_SHA_A, pr_number=77)
    direct.repo = _repo()
    unknown = _review(1, sha=_SHA_B, pr_number=None)
    unknown.repo = _repo()
    fanout_db(_user(), reviews=[direct, unknown])

    pulls = respx.get(_pulls_url(_SHA_B)).respond(status_code=200, json=[_pr(9)])
    async with make_auth_client(_USER_ID) as ac:
        resp = await ac.post(
            _BATCH_URL, json={"review_ids": [str(direct.id), str(unknown.id)]}
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()["associated_prs"]
    assert data[str(direct.id)]["number"] == 77
    assert data[str(unknown.id)] is None
    assert pulls.called is False

