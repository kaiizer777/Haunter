"""
On-demand associated-PR lookup: ``GET /reviews/{review_id}/associated-pr``.

Covers the read-only endpoint the push cards use to swap ``Push @ sha`` for
``PR #n``: a ``pr_number``-set row answers directly without touching GitHub;
otherwise the route calls
``GET /repos/{owner}/{repo}/commits/{sha}/pulls`` with the server token,
preferring an open PR and falling back to the highest PR number. Any failure
(no token, non-200, network error, empty list) answers HTTP 200 with
``{"associated_pr": null}`` so the row keeps rendering as a plain push.

Hermeticity: the real FastAPI app over ASGI with ``get_db`` overridden by a
minimal scripted session (dispatches the auth ``select(User)`` and the
ownership-scoped ``select(CodeReview)`` from in-process ORM rows; anything
else raises). GitHub is mocked at the httpx transport seam via ``respx``
— the same seam ``tests/test_github_client.py`` uses — so no network, no
Postgres, no token. Marked ``fast``: no ``db`` / ``user_factory`` /
``repo_factory`` fixtures, so the default suite runs these.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

import httpx
import pytest
import respx

from app.config import settings
from app.db import get_db
from app.models import CodeReview, Repo, User
from tests.fake_audit_db import FakeResult

_FIXED_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_SHA = "b" * 40
_PULLS_URL = f"https://api.github.com/repos/acme/widgets/commits/{_SHA}/pulls"


# ---------------------------------------------------------------------------
# Scripted session + row builders
# ---------------------------------------------------------------------------


class _ScriptedSession:
    """AsyncSession stand-in dispatching on the selected entity.

    The associated-pr route emits exactly two SELECT shapes: the auth lookup
    (``select(User)`` via ``get_current_user``) and the ownership-scoped
    detail lookup (``select(CodeReview)`` joined to repos). Anything else
    raises loudly instead of answering from a stale script.
    """

    def __init__(self, user: User, review: Optional[CodeReview]) -> None:
        self._user = user
        self._review = review

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> FakeResult:
        entity = statement.column_descriptions[0].get("entity")
        if entity is User:
            return FakeResult([self._user])
        if entity is CodeReview:
            return FakeResult([self._review] if self._review is not None else [])
        raise AssertionError(
            f"scripted session received a statement selecting {entity!r}"
        )

    async def scalar(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        return (await self.execute(statement)).scalar()

    async def close(self) -> None:
        return None


@pytest.fixture
def install_db():
    """Install a scripted session on ``app.dependency_overrides[get_db]``."""
    from main import app

    previous = app.dependency_overrides.get(get_db)

    def _install(user: User, review: Optional[CodeReview]) -> _ScriptedSession:
        session = _ScriptedSession(user, review)

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


def _user() -> User:
    return User(
        id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
        github_id=90001,
        github_username="assoc-pr-user",
        access_token=None,
        avatar_url=None,
        role="user",
    )


def _repo(user: User) -> Repo:
    return Repo(
        id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
        user_id=user.id,
        owner="acme",
        name="widgets",
        default_branch="main",
        created_at=_FIXED_NOW,
    )


def _review(repo: Repo, *, pr_number: Optional[int] = None) -> CodeReview:
    row = CodeReview(
        id=uuid.uuid4(),
        repo_id=repo.id,
        commit_sha=_SHA,
        pr_number=pr_number,
        risk_score=10,
        summary="Push-level review",
        findings=[],
        status="completed",
        failure_reason=None,
        input_tokens=100,
        output_tokens=50,
        created_at=_FIXED_NOW,
    )
    row.repo = repo
    return row


def _pr(number: int, *, state: str = "open") -> dict[str, Any]:
    return {
        "number": number,
        "state": state,
        "title": f"Change #{number}",
        "html_url": f"https://github.com/acme/widgets/pull/{number}",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_pr_number_set_row_answers_directly_without_github_call(
    make_auth_client, install_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row with ``pr_number`` set returns it directly; GitHub is not called."""
    monkeypatch.setattr(settings, "github_token", "test-token")
    user = _user()
    repo = _repo(user)
    review = _review(repo, pr_number=77)
    install_db(user, review)

    pulls = respx.get(_PULLS_URL).respond(status_code=200, json=[])
    async with make_auth_client(user.id) as ac:
        resp = await ac.get(f"/reviews/{review.id}/associated-pr")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "associated_pr": {
            "number": 77,
            "url": "https://github.com/acme/widgets/pull/77",
            "title": None,
        }
    }
    assert pulls.called is False, "pr_number-set row must not call GitHub"


@pytest.mark.asyncio
@respx.mock
async def test_associated_found_prefers_open_pr(
    make_auth_client, install_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multiple PRs for the commit: the first open one wins."""
    monkeypatch.setattr(settings, "github_token", "test-token")
    user = _user()
    repo = _repo(user)
    review = _review(repo, pr_number=None)
    install_db(user, review)

    respx.get(_PULLS_URL).respond(
        status_code=200, json=[_pr(5, state="closed"), _pr(9), _pr(7)]
    )
    async with make_auth_client(user.id) as ac:
        resp = await ac.get(f"/reviews/{review.id}/associated-pr")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "associated_pr": {
            "number": 9,
            "url": "https://github.com/acme/widgets/pull/9",
            "title": "Change #9",
        }
    }


@pytest.mark.asyncio
@respx.mock
async def test_associated_found_falls_back_to_highest_number(
    make_auth_client, install_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No open PR: the most recent (highest number) is returned."""
    monkeypatch.setattr(settings, "github_token", "test-token")
    user = _user()
    repo = _repo(user)
    review = _review(repo, pr_number=None)
    install_db(user, review)

    respx.get(_PULLS_URL).respond(
        status_code=200, json=[_pr(3, state="closed"), _pr(11, state="merged")]
    )
    async with make_auth_client(user.id) as ac:
        resp = await ac.get(f"/reviews/{review.id}/associated-pr")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "associated_pr": {
            "number": 11,
            "url": "https://github.com/acme/widgets/pull/11",
            "title": "Change #11",
        }
    }


@pytest.mark.asyncio
@respx.mock
async def test_no_associated_pr_returns_null(
    make_auth_client, install_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty pulls list (direct-to-main push) answers null with HTTP 200."""
    monkeypatch.setattr(settings, "github_token", "test-token")
    user = _user()
    repo = _repo(user)
    review = _review(repo, pr_number=None)
    install_db(user, review)

    respx.get(_PULLS_URL).respond(status_code=200, json=[])
    async with make_auth_client(user.id) as ac:
        resp = await ac.get(f"/reviews/{review.id}/associated-pr")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"associated_pr": None}


@pytest.mark.asyncio
@respx.mock
async def test_missing_token_returns_null_without_github_call(
    make_auth_client, install_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No server token configured: null, and GitHub is never called."""
    monkeypatch.setattr(settings, "github_token", None)
    user = _user()
    repo = _repo(user)
    review = _review(repo, pr_number=None)
    install_db(user, review)

    pulls = respx.get(_PULLS_URL).respond(status_code=200, json=[_pr(9)])
    async with make_auth_client(user.id) as ac:
        resp = await ac.get(f"/reviews/{review.id}/associated-pr")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"associated_pr": None}
    assert pulls.called is False, "missing token must short-circuit before GitHub"


@pytest.mark.asyncio
@respx.mock
async def test_github_error_status_returns_null(
    make_auth_client, install_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GitHub 403 (rate limit) never fails the page: null with HTTP 200."""
    monkeypatch.setattr(settings, "github_token", "test-token")
    user = _user()
    repo = _repo(user)
    review = _review(repo, pr_number=None)
    install_db(user, review)

    respx.get(_PULLS_URL).respond(status_code=403, json={"message": "rate limited"})
    async with make_auth_client(user.id) as ac:
        resp = await ac.get(f"/reviews/{review.id}/associated-pr")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"associated_pr": None}


@pytest.mark.asyncio
@respx.mock
async def test_github_network_error_returns_null(
    make_auth_client, install_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GitHub timeout/unreachable never fails the page: null with HTTP 200."""
    monkeypatch.setattr(settings, "github_token", "test-token")
    user = _user()
    repo = _repo(user)
    review = _review(repo, pr_number=None)
    install_db(user, review)

    respx.get(_PULLS_URL).mock(side_effect=httpx.ConnectError("unreachable"))
    async with make_auth_client(user.id) as ac:
        resp = await ac.get(f"/reviews/{review.id}/associated-pr")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"associated_pr": None}


@pytest.mark.asyncio
@respx.mock
async def test_missing_or_unowned_review_returns_404(
    make_auth_client, install_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown id (or a row in another tenant's repo) answers 404, like detail."""
    monkeypatch.setattr(settings, "github_token", "test-token")
    user = _user()
    install_db(user, None)

    async with make_auth_client(user.id) as ac:
        resp = await ac.get(f"/reviews/{uuid.uuid4()}/associated-pr")

    assert resp.status_code == 404, resp.text
