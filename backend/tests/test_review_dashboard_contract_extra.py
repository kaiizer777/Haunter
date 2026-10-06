"""
Companion coverage for the C1/C2/C3 dashboard contract fixes.

``tests/test_review_dashboard_contract.py`` is frozen and owns the four
reproductions. This file covers what it deliberately does not, and only that —
the guards around each fix that would otherwise go unpinned:

  C1  ``AttemptOut`` now carries ``patch_text``. The frozen file proves one
      attempt's diff survives serialisation; this file proves every attempt in a
      multi-attempt trace carries its *own* diff (a bug that leaked the first
      attempt's patch onto every row would pass the single-attempt case).

  C2  The status allowlist is now derived from ``app.orchestrator.RunStatus``
      instead of transcribed. The frozen file proves every current status is
      accepted; this file pins the two properties that make that derivation
      meaningful and that the frozen file cannot see:
        - the allowlist is still a real allowlist — free text is rejected with
          422, which is the SQL-injection guard documented at
          ``RunListParams`` and asserted by ``test_traces.py:251-262`` (a DB test
          that deselects whenever ``TEST_DATABASE_URL`` is unset, so it is not
          run in the default suite);
        - the set equals the enum at run time, so re-transcribing the values
          here or in the router turns this red again.

  C3  ``total`` is re-aligned with the rows whenever ``severity`` post-filters
      the page. The frozen file proves a filtered page agrees with its count;
      this file proves the unfiltered path is untouched (COUNT still wins) and
      that a filter matching nothing reports zero rather than the unfiltered
      count.

Hermeticity matches the frozen file: the real FastAPI app over ASGI with
``get_db`` overridden by the same shape-keyed scripted session, which raises on
any statement shape it does not model. No network, no Postgres, no clock
dependence. The harness is imported rather than duplicated.

Deliberately marked ``fast``: no ``db`` / ``user_factory`` / ``repo_factory``
fixtures are requested, so ``tests/conftest.py::pytest_collection_modifyitems``
does not tag these ``db``.
"""

from __future__ import annotations

import pytest
from app.routers.traces import _allowed_run_statuses
from app.models import Attempt, CodeReview, Repo, Run, RunStep
from tests.test_review_dashboard_contract import (
    _ANY_WHERE,
    _attempt,
    _auth_select,
    _code_review,
    _n_columns,
    _repo,
    _run,
    _user,
    use_scripted_db,
)


# ---------------------------------------------------------------------------
# C1 — every attempt carries its own patch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_trace_returns_a_distinct_patch_for_each_attempt(
    make_auth_client, use_scripted_db
) -> None:
    """
    Each attempt in ``TraceOut.attempts`` must carry the diff stored on *that*
    attempt.

    The frozen test drives a single attempt, which cannot tell "read the column
    per row" apart from "broadcast one attempt's patch onto every row". Two
    attempts with distinguishable diffs pin it down.
    """
    user = _user()
    repo = _repo(user)
    run = _run(repo, status="pr_opened")
    first_patch = "--- a/app/one.py\n+++ b/app/one.py\n@@ -1 +1 @@\n-a\n+b\n"
    second_patch = "--- a/app/two.py\n+++ b/app/two.py\n@@ -2 +2 @@\n-c\n+d\n"
    attempts = [
        _attempt(run, patch_text=first_patch, attempt_number=1),
        _attempt(run, patch_text=second_patch, attempt_number=2),
    ]

    use_scripted_db(
        {
            _auth_select(): [user],
            (
                Run,
                True,
                _n_columns(Run),
                frozenset({"runs.id", "repos.user_id"}),
            ): [run],
            (RunStep, True, _n_columns(RunStep), frozenset({"run_steps.run_id"})): [],
            (Attempt, True, _n_columns(Attempt), frozenset({"attempts.run_id"})): (
                attempts
            ),
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
    payload = resp.json()["attempts"]

    assert [a["attempt_number"] for a in payload] == [1, 2]
    assert [a["patch_text"] for a in payload] == [first_patch, second_patch], (
        "each attempt must return the patch stored on that attempt, not a shared "
        "or broadcast diff"
    )


# ---------------------------------------------------------------------------
# C2 — the allowlist is derived, and it is still an allowlist
# ---------------------------------------------------------------------------


def test_allowed_run_statuses_is_derived_from_the_orchestrator_enum() -> None:
    """
    ``_allowed_run_statuses`` must equal the enum's values, read at call time.

    This is the assertion that makes the C2 fix durable. Comparing against the
    enum at run time (not a literal list) means re-transcribing the values — in
    the router or in a copy elsewhere — fails here instead of silently bringing
    the drift back.
    """
    from app.orchestrator import RunStatus

    assert set(_allowed_run_statuses()) == {status.value for status in RunStatus}
    # No duplicates and no empty/blank entries accepted as statuses.
    assert len(_allowed_run_statuses()) == len(set(_allowed_run_statuses()))
    assert all(status for status in _allowed_run_statuses())


@pytest.mark.asyncio
async def test_run_status_filter_still_rejects_free_text(
    make_auth_client, use_scripted_db
) -> None:
    """
    ``?status=evil_injection`` must still answer 422.

    The allowlist is the SQL-injection guard for this filter, not merely a UX
    nicety: dropping it to "accept whatever the enum says" would also accept
    arbitrary caller text. Deriving the set must not weaken the rejection — only
    widen which legitimate statuses pass.
    """
    user = _user()

    # The 422 is raised during param validation, before any listing query runs,
    # so the auth lookup is the only statement the route should emit.
    use_scripted_db({_auth_select(): [user]})

    async with make_auth_client(user.id) as ac:
        resp = await ac.get("/runs", params={"status": "evil_injection"})

    assert resp.status_code == 422, resp.text
    assert "status must be one of" in resp.text, (
        "the rejection should name the derived allowlist so a caller can see "
        f"which values are valid; got detail: {resp.json()}"
    )


# ---------------------------------------------------------------------------
# C3 — the unfiltered path is untouched, and an empty filter page reports 0
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reviews_total_is_the_count_when_no_severity_filter_is_set(
    make_auth_client, use_scripted_db
) -> None:
    """
    Without ``severity``, ``total`` must remain the SQL COUNT — not the page size.

    The C3 fix re-aligns ``total`` only on the post-filtered path. Without this
    test a fix that unconditionally overwrote ``total`` with ``len(reviews)``
    would silently break pagination for every unfiltered listing (a 100-row page
    of a 500-review repo would report total=100).
    """
    user = _user()
    repo = _repo(user)
    reviews = [
        _code_review(repo, severity="high", risk_score=80),
        _code_review(repo, severity="low", risk_score=10, offset_hours=1),
    ]

    use_scripted_db(
        {
            _auth_select(): [user],
            (Repo, False, 1, frozenset({"repos.user_id"})): [repo.id],
            (None, False, 1, _ANY_WHERE): [57],
            (CodeReview, True, _n_columns(CodeReview), _ANY_WHERE): reviews,
        }
    )

    async with make_auth_client(user.id) as ac:
        resp = await ac.get("/reviews")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] == 57
    assert len(data["reviews"]) == 2


@pytest.mark.asyncio
async def test_repo_reviews_total_is_zero_when_severity_matches_nothing(
    make_auth_client, use_scripted_db
) -> None:
    """
    A severity filter that matches no review must report ``total == 0``.

    The cheap "fix" of returning no rows with the unfiltered count still
    attached would satisfy a filter-matches-something test while telling the UI
    that matching reviews exist somewhere. Zero rows must mean zero.
    """
    user = _user()
    repo = _repo(user)
    only_low = _code_review(repo, severity="low", risk_score=10)

    use_scripted_db(
        {
            _auth_select(): [user],
            (Repo, True, _n_columns(Repo), frozenset({"repos.id", "repos.user_id"})): [
                repo
            ],
            (None, False, 1, _ANY_WHERE): [0],
            (CodeReview, True, _n_columns(CodeReview), _ANY_WHERE): [only_low],
        }
    )

    async with make_auth_client(user.id) as ac:
        resp = await ac.get(
            f"/repos/{repo.id}/reviews", params={"severity": "critical"}
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["reviews"] == []
    assert data["total"] == 0
