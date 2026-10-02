"""
Issue #37 — a live delivery may not be attributed to an arbitrary tenant.

`repos` is unique per (user_id, owner, name) — models.Repo / add_repo — so two
tenants MAY register the same owner/name, and that is a supported configuration.
GitHub's payload carries no tenant, so `select(Repo).where(owner, name)` could
match N>1 rows and the unordered `.scalars().first()` used to take whichever one
Postgres happened to return. That row selects repo settings, reads the per-repo
auditor kill switch, queues the run / review / audit, and owns the persisted
delivery row: a coin flip that routes one tenant's delivery through another
tenant's pipeline and leaves the loser with nothing and no error.

The fix refuses instead of guessing: `_resolve_repo_for_delivery` resolves only
when EXACTLY ONE registration matches, records an unattributed diagnostic row
for the ambiguous case, and dispatches nothing.

Fast (hermetic, no DB — a stub session answers the owner/name lookup and nothing
else, which is all the rejected path touches):
- exactly one match resolves, zero matches keeps the pre-existing unregistered
  behaviour, two matches refuses and records
- the refusal names no tenant: no id, no name, no match count
- determinism: identical outcome for both insert orders, repeated N times
- the replay pin filters on repos.id, so it can never be ambiguous
- end-to-end through POST /webhooks/github for all FOUR live call sites
  (workflow_run, pull_request, push, issue_comment) — a fix applied to one call
  site and not the other three would fail here
"""

import hashlib
import hmac
import json
import uuid
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.db import get_db
from app.models import Repo, WebhookDelivery
from app.webhooks import (
    _REASON_AMBIGUOUS_REGISTRATION,
    _REASON_UNREGISTERED,
    _repo_lookup_stmt,
    _resolve_repo_for_delivery,
)

TEST_SECRET = settings.github_webhook_secret or "test_webhook_secret_key_12345"

OWNER = "amb-org"
REPO_NAME = "amb-repo"


# ---------------------------------------------------------------------------
# Stub session — answers the owner/name lookup with a scripted result set.
# ---------------------------------------------------------------------------


class _StubResult:
    """Result of a SELECT, exposing the ScalarResult surface the handler uses."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> "_StubResult":
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def first(self) -> Optional[Any]:
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self) -> Optional[Any]:
        return self._rows[0] if self._rows else None


class _StubSession:
    """AsyncSession stand-in that replays a scripted owner/name result set.

    The row ORDER is part of the contract: it is what the database returns for an
    unordered `select`, which is exactly the non-determinism under test. A real
    AsyncSession would also answer every later query, but the refused path never
    issues one — `add`/`commit`/`rollback` cover the health-log insert.
    """

    def __init__(self, rows: list[Any]) -> None:
        self.rows = list(rows)
        self.added: list[Any] = []
        self.commits = 0

    async def execute(self, stmt: Any) -> _StubResult:
        return _StubResult(self.rows)

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        pass


def _repo(user_id: Optional[uuid.UUID] = None, suffix: str = "a") -> Repo:
    return Repo(
        id=uuid.uuid4(),
        user_id=user_id or uuid.uuid4(),
        owner=OWNER,
        name=REPO_NAME,
        default_branch="main" if suffix == "a" else None,
    )


async def _resolve(db: _StubSession, replay_repo_id: Optional[uuid.UUID] = None):
    return await _resolve_repo_for_delivery(
        db,  # type: ignore[arg-type]
        event="workflow_run",
        delivery_id="del-ambiguous-1",
        owner=OWNER,
        name=REPO_NAME,
        replay_repo_id=replay_repo_id,
    )


# ---------------------------------------------------------------------------
# Resolution outcomes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exactly_one_match_resolves_and_records_nothing():
    repo = _repo()
    db = _StubSession([repo])

    resolved, reason = await _resolve(db)

    assert resolved is repo
    assert reason is None
    # A resolution is not a rejection: no diagnostic row is owed.
    assert db.added == []
    assert db.commits == 0


@pytest.mark.asyncio
async def test_zero_matches_keeps_the_pre_existing_unregistered_behaviour():
    db = _StubSession([])

    resolved, reason = await _resolve(db)

    assert resolved is None
    assert reason == _REASON_UNREGISTERED
    # Unchanged: log-only. There is no repo to attribute a row to, so writing one
    # would grow the health table with rows no tenant can read.
    assert db.added == []
    assert db.commits == 0


@pytest.mark.asyncio
async def test_two_matches_refuse_and_record_one_unattributed_diagnostic_row():
    first, second = _repo(suffix="a"), _repo(suffix="b")
    db = _StubSession([first, second])

    resolved, reason = await _resolve(db)

    assert resolved is None
    assert reason == _REASON_AMBIGUOUS_REGISTRATION
    # Distinguishable from "nobody registered it" without a schema change.
    assert reason != _REASON_UNREGISTERED

    assert len(db.added) == 1
    row = db.added[0]
    assert isinstance(row, WebhookDelivery)
    assert row.status == "ignored"
    assert row.reason == _REASON_AMBIGUOUS_REGISTRATION
    assert row.event == "workflow_run"
    assert row.delivery_id == "del-ambiguous-1"
    # Attributed to NO repo: `repo_id` is the tenant boundary of both the delivery
    # listing and the replay route, so pinning either match would assert a tenancy
    # this delivery does not have and expose the decision in that tenant's history.
    assert row.repo_id is None
    # No replay buffer either — the row describes work that never ran, and there
    # is no repo to re-drive it onto.
    assert row.payload is None


@pytest.mark.asyncio
async def test_three_matches_are_refused_exactly_like_two():
    db = _StubSession([_repo(suffix=str(i)) for i in range(3)])

    resolved, reason = await _resolve(db)

    assert resolved is None
    assert reason == _REASON_AMBIGUOUS_REGISTRATION
    assert len(db.added) == 1


@pytest.mark.asyncio
async def test_refusal_leaks_no_tenant_identity():
    tenant_a, tenant_b = _repo(suffix="a"), _repo(suffix="b")
    db = _StubSession([tenant_a, tenant_b])

    resolved, reason = await _resolve(db)

    body = {"status": "ignored", "reason": reason}
    row = db.added[0]
    serialized = json.dumps(body, default=str) + json.dumps(
        {
            "status": row.status,
            "reason": row.reason,
            "repo": row.repo,
            "repo_id": str(row.repo_id),
        }
    )
    # No id and no match count — a caller must not be able to tell "one other
    # tenant has this repo" from "several do", or correlate the refusal at all.
    assert str(tenant_a.id) not in serialized
    assert str(tenant_b.id) not in serialized
    assert str(tenant_a.user_id) not in serialized
    assert str(tenant_b.user_id) not in serialized
    assert "2" not in (reason or "")
    assert "3" not in (reason or "")


@pytest.mark.asyncio
async def test_ambiguous_resolution_is_identical_for_every_insert_order():
    """The whole point: no insert order may change the outcome.

    With the pre-fix `.scalars().first()`, order [a, b] resolved tenant a and
    order [b, a] resolved tenant b — two different pipelines for one delivery.
    Reverting the fix makes this fail on the first assertion of order [b, a].
    """
    tenant_a, tenant_b = _repo(suffix="a"), _repo(suffix="b")

    for _ in range(25):
        for rows in ([tenant_a, tenant_b], [tenant_b, tenant_a]):
            db = _StubSession(rows)
            resolved, reason = await _resolve(db)

            assert resolved is None, f"a row was picked from {[r.id for r in rows]}"
            assert reason == _REASON_AMBIGUOUS_REGISTRATION
            assert len(db.added) == 1
            assert db.added[0].repo_id is None


def test_replay_pin_filters_on_id_and_not_owner_name():
    """Replay is pinned to one authorized repos.id, so it cannot be ambiguous."""
    repo_id = uuid.uuid4()

    # `select(Repo)` expands to every column, so the predicate is what matters.
    pinned_where = str(
        _repo_lookup_stmt(OWNER, REPO_NAME, repo_id)
        .whereclause.compile(compile_kwargs={"literal_binds": True})
    )
    live_where = str(
        _repo_lookup_stmt(OWNER, REPO_NAME, None)
        .whereclause.compile(compile_kwargs={"literal_binds": True})
    )

    assert "repos.id = " in pinned_where and repo_id.hex in pinned_where
    assert "repos.owner" not in pinned_where
    assert "repos.name" not in pinned_where
    assert "repos.owner" in live_where and "repos.name" in live_where
    assert "repos.id" not in live_where


# ---------------------------------------------------------------------------
# End to end through POST /webhooks/github — one case per live call site.
# ---------------------------------------------------------------------------


def _signed_headers(event: str, payload: dict[str, Any]) -> tuple[dict, bytes]:
    raw = json.dumps(payload).encode("utf-8")
    digest = hmac.new(TEST_SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return (
        {
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": f"sha256={digest}",
        },
        raw,
    )


def _workflow_run_payload() -> dict[str, Any]:
    return {
        "action": "completed",
        "workflow_run": {
            "id": 778001,
            "head_sha": "0123456789abcdef0123456789abcdef01234567",
            "head_branch": "main",
            "conclusion": "failure",
        },
        "repository": {
            "name": REPO_NAME,
            "full_name": f"{OWNER}/{REPO_NAME}",
            "owner": {"login": OWNER},
        },
    }


def _pull_request_payload() -> dict[str, Any]:
    return {
        "action": "opened",
        "number": 7,
        "pull_request": {
            "number": 7,
            "state": "open",
            "draft": False,
            "head": {
                "ref": "feature/ambiguity",
                "sha": "1111222233334444555566667777888899990000",
            },
            "user": {"login": "dev-user", "type": "User"},
        },
        "repository": {
            "name": REPO_NAME,
            "full_name": f"{OWNER}/{REPO_NAME}",
            "owner": {"login": OWNER},
        },
        "sender": {"login": "dev-user", "type": "User"},
    }


def _push_payload() -> dict[str, Any]:
    return {
        "ref": "refs/heads/main",
        "deleted": False,
        "head_commit": {
            "id": "aaaabbbbccccddddeeeeffff0000111122223333",
            "author": {"name": "Senior Dev", "email": "dev@example.com"},
        },
        "repository": {
            "name": REPO_NAME,
            "full_name": f"{OWNER}/{REPO_NAME}",
            "owner": {"login": OWNER},
        },
        "sender": {"login": "dev-user", "type": "User"},
    }


def _issue_comment_payload() -> dict[str, Any]:
    return {
        "action": "created",
        "issue": {
            "number": 7,
            "pull_request": {
                "url": f"https://api.github.com/repos/{OWNER}/{REPO_NAME}/pulls/7",
                "html_url": f"https://github.com/{OWNER}/{REPO_NAME}/pull/7",
            },
        },
        "comment": {
            "id": 9911,
            "body": "@haunter fix the None deref",
            "author_association": "MEMBER",
            "user": {"login": "senior-reviewer"},
        },
        "repository": {
            "name": REPO_NAME,
            "full_name": f"{OWNER}/{REPO_NAME}",
            "owner": {"login": OWNER},
        },
    }


LIVE_CALL_SITES = [
    ("workflow_run", _workflow_run_payload),
    ("pull_request", _pull_request_payload),
    ("push", _push_payload),
    ("issue_comment", _issue_comment_payload),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event", "build_payload"),
    LIVE_CALL_SITES,
    ids=[event for event, _ in LIVE_CALL_SITES],
)
async def test_every_live_call_site_refuses_an_ambiguous_registration(
    client: httpx.AsyncClient, event: str, build_payload
):
    """All four call sites must go through the non-ambiguous resolution.

    Each payload is otherwise valid enough to reach the registration guard, so a
    call site that still took an unordered first match would queue real work
    against whichever tenant's row the stub returns first and answer "queued".
    """
    from main import app

    tenant_a, tenant_b = _repo(suffix="a"), _repo(suffix="b")
    db = _StubSession([tenant_a, tenant_b])

    async def _fake_db():
        yield db  # type: ignore[misc]

    app.dependency_overrides[get_db] = _fake_db
    adapter = MagicMock()
    adapter.schedule_pipeline = AsyncMock()
    adapter.schedule_review = AsyncMock()
    try:
        with patch(
            "app.adapters.hosting.get_hosting_adapter", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = adapter
            headers, raw = _signed_headers(event, build_payload())
            resp = await client.post("/webhooks/github", headers=headers, content=raw)
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ignored",
        "reason": _REASON_AMBIGUOUS_REGISTRATION,
    }
    # Nothing dispatched on either tenant's behalf.
    assert adapter.schedule_pipeline.await_count == 0
    assert adapter.schedule_review.await_count == 0
    # One diagnostic, recorded against no repo.
    assert len(db.added) == 1
    assert db.added[0].repo_id is None
    assert db.added[0].event == event


@pytest.mark.asyncio
async def test_single_registration_is_not_rejected_by_the_ambiguity_check(
    client: httpx.AsyncClient,
):
    """Exactly one registration must keep flowing, not be refused.

    Driven end to end through the handler: with one match the resolver returns
    the row and the guard passes, so the handler moves on to repo settings and
    the stub — which only answers the owner/name lookup — raises there. Reaching
    that point IS the assertion; an ambiguity check that rejected a unique
    registration would have answered with the refusal before touching anything.
    (The full single-match happy path against a real database is covered by
    tests/test_webhooks.py::test_webhook_valid_failure_creates_run,
    tests/test_code_review_webhook.py::test_push_webhook_success and
    ::test_pr_webhook_opened_success.)
    """
    from main import app

    tenant = _repo(suffix="a")
    db = _StubSession([tenant])

    async def _fake_db():
        yield db  # type: ignore[misc]

    app.dependency_overrides[get_db] = _fake_db
    try:
        headers, raw = _signed_headers("workflow_run", _workflow_run_payload())
        with pytest.raises(AttributeError):
            await client.post("/webhooks/github", headers=headers, content=raw)
    finally:
        app.dependency_overrides.pop(get_db, None)

    # No refusal was recorded, and nothing was queued on the tenant's behalf
    # before the stub stopped the handler.
    assert db.added == []
