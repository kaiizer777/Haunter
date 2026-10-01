"""
Conversational follow-up webhook routing tests
(`app.webhooks.github_webhook`, Branch B).

Covers:
  1. `@haunter fix` / `address` on an `issue_comment` create a child Run
     threaded to the originating run (`parent_run_id`) with conclusion
     `feedback`.
  2. `@haunter test-fix` — mixed case, extra whitespace, trailing prose —
     creates the same lineage with conclusion `test-fix`.
  3. The same grammar on `pull_request_review_comment`.
  4. Lineage is threaded to the ROOT run, not to the intermediate refinement.
  5. A mention addressed to the auditor (`@haunter audit`) is a strict no-op
     for the fix pipeline.
  6. Signature verification: a missing, forged, or body-swapped
     `X-Hub-Signature-256` is rejected with 401 before any row is created.
  7. The mention gate, the collaborator-authority gate and the branch guard
     still hold.

Everything runs against the in-process store from `tests/fake_audit_db.py`, so
no network, no PostgreSQL, no `TEST_DATABASE_URL`. Only the hosting adapter
(which would otherwise invoke AWS Lambda) is mocked.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Generator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Repo, Run
from app.services.followup_commands import FEEDBACK_CONCLUSION, TEST_FIX_CONCLUSION

# `audit_store`, `fake_audit_db` and `fake_audit_user_factory` are pytest
# fixtures; importing them here is what registers them for this module.
from tests.fake_audit_db import (  # noqa: F401
    FakeAsyncSession,
    FakeStore,
    audit_store,
    fake_audit_db,
    fake_audit_user_factory,
)

TEST_SECRET = settings.github_webhook_secret or "test_webhook_secret_key_12345"

OWNER = "conv-org"
REPO = "conv-repo"
PR_BRANCH = "haunter/fix-11223344-1"
TRIGGER_COMMENT_ID = 5550001


@pytest.fixture(autouse=True)
def deny_external_http(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """No test in this module may reach the real GitHub API."""
    original_send = httpx.AsyncClient.send

    async def guarded_send(self, request, **kwargs):
        if isinstance(self._transport, httpx.ASGITransport):
            return await original_send(self, request, **kwargs)
        raise AssertionError(f"external HTTP blocked: {request.url.host}")

    monkeypatch.setattr(httpx.AsyncClient, "send", guarded_send)
    yield


def signed(secret: str, payload: dict[str, Any]) -> str:
    """Signature over the exact bytes the request will carry."""
    digest = hmac.new(
        secret.encode("utf-8"), json.dumps(payload).encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"sha256={digest}"


def issue_comment_payload(
    *,
    body: str = "@haunter fix the None deref",
    comment_id: int = TRIGGER_COMMENT_ID,
    author_association: str = "MEMBER",
    pr_number: int = 42,
    owner: str = OWNER,
    repo: str = REPO,
) -> dict[str, Any]:
    return {
        "action": "created",
        "issue": {
            "number": pr_number,
            "pull_request": {
                "url": f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
                "html_url": f"https://github.com/{owner}/{repo}/pull/{pr_number}",
            },
        },
        "comment": {
            "id": comment_id,
            "body": body,
            "author_association": author_association,
            "user": {"login": "senior-reviewer"},
        },
        "repository": {
            "name": repo,
            "full_name": f"{owner}/{repo}",
            "owner": {"login": owner},
        },
    }


def review_comment_payload(
    *,
    body: str = "@haunter fix this line",
    comment_id: int = 6660002,
    author_association: str = "OWNER",
    pr_number: int = 42,
    head_ref: str = PR_BRANCH,
    owner: str = OWNER,
    repo: str = REPO,
) -> dict[str, Any]:
    return {
        "action": "created",
        "pull_request": {
            "number": pr_number,
            "head": {"ref": head_ref, "sha": "1" * 40},
            "base": {"ref": "main", "sha": "2" * 40},
        },
        "comment": {
            "id": comment_id,
            "body": body,
            "author_association": author_association,
            "user": {"login": "staff-engineer"},
        },
        "repository": {
            "name": repo,
            "full_name": f"{owner}/{repo}",
            "owner": {"login": owner},
        },
    }


async def seed(
    session: FakeAsyncSession,
    user_factory,
    *,
    owner: str = OWNER,
    repo_name: str = REPO,
    pr_number: int = 42,
    branch: str = PR_BRANCH,
) -> tuple[Repo, Run]:
    """Register a repo that already has a root PR run awaiting refinement."""
    user = await user_factory(
        github_id=int(uuid.uuid4().int % 1_000_000_000 + 300_000_000),
        username=f"conv-user-{uuid.uuid4().hex[:6]}",
    )
    repo = Repo(user_id=user.id, owner=owner, name=repo_name)
    session.add(repo)
    await session.commit()

    root_run = Run(
        repo_id=repo.id,
        github_run_id=99887766,
        github_delivery_id=str(uuid.uuid4()),
        head_sha="a" * 40,
        head_branch=branch,
        pr_number=pr_number,
        pr_branch=branch,
        pr_url=f"https://github.com/{owner}/{repo_name}/pull/{pr_number}",
        status="pr_opened",
        conclusion="success",
    )
    session.add(root_run)
    await session.commit()
    return repo, root_run


async def post_comment(
    client: httpx.AsyncClient,
    event: str,
    payload: dict[str, Any],
    *,
    signature: str | None = None,
) -> httpx.Response:
    """POST one signed webhook delivery with the Lambda dispatch stubbed."""
    raw_body = json.dumps(payload).encode("utf-8")
    headers = {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": str(uuid.uuid4()),
    }
    if signature is not None:
        headers["X-Hub-Signature-256"] = signature
    with patch(
        "app.adapters.hosting.get_hosting_adapter", new_callable=AsyncMock
    ) as mock_get:
        adapter = MagicMock()
        adapter.schedule_pipeline = AsyncMock()
        adapter.schedule_review = AsyncMock()
        mock_get.return_value = adapter
        return await client.post("/webhooks/github", content=raw_body, headers=headers)


def child_runs(store: FakeStore, root_run: Run) -> list[Run]:
    return [
        row
        for row in store.rows(Run.__table__)
        if row.parent_run_id == root_run.id
    ]


# ---------------------------------------------------------------------------
# 1/2/3. Command grammar -> queued follow-up run
# ---------------------------------------------------------------------------

FIX_CASES = [
    ("@haunter fix", "fix"),
    ("@haunter address the flake", "address"),
    ("@Haunter FIX: retry with backoff", "fix"),
    ("@haunter   address", "address"),
    ("hey @haunter, please fix the null deref", "fix"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("body,command", FIX_CASES)
async def test_fix_commands_queue_a_feedback_run(
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
    audit_store: FakeStore,
    body: str,
    command: str,
):
    repo, root_run = await seed(fake_audit_db, fake_audit_user_factory)
    payload = issue_comment_payload(body=body)

    resp = await post_comment(
        client, "issue_comment", payload, signature=signed(TEST_SECRET, payload)
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "queued"
    assert data["command"] == command
    assert data["parent_run_id"] == str(root_run.id)

    children = child_runs(audit_store, root_run)
    assert len(children) == 1
    assert children[0].conclusion == FEEDBACK_CONCLUSION
    assert children[0].status == "pending"
    assert children[0].pr_number == 42
    assert children[0].repo_id == repo.id
    # The triggering comment id is carried on its own column so the pipeline
    # can answer in the same thread, and `github_run_id` stays reserved for
    # real GitHub Actions workflow runs (a follow-up has none).
    assert children[0].trigger_comment_id == TRIGGER_COMMENT_ID
    assert children[0].github_run_id is None


TEST_FIX_CASES = [
    "@haunter test-fix",
    "@haunter test_fix",
    "@haunter TEST-FIX just verify it",
    "@Haunter\tTest-Fix",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", TEST_FIX_CASES)
async def test_test_fix_queues_a_verify_only_run(
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
    audit_store: FakeStore,
    body: str,
):
    _, root_run = await seed(fake_audit_db, fake_audit_user_factory)
    payload = issue_comment_payload(body=body)

    resp = await post_comment(
        client, "issue_comment", payload, signature=signed(TEST_SECRET, payload)
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "queued"
    assert data["command"] == "test-fix"

    children = child_runs(audit_store, root_run)
    assert len(children) == 1
    assert children[0].conclusion == TEST_FIX_CONCLUSION


@pytest.mark.asyncio
async def test_review_comment_uses_the_same_grammar(
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
    audit_store: FakeStore,
):
    _, root_run = await seed(fake_audit_db, fake_audit_user_factory)
    payload = review_comment_payload(body="@haunter test-fix verify only")

    resp = await post_comment(
        client,
        "pull_request_review_comment",
        payload,
        signature=signed(TEST_SECRET, payload),
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "queued"
    assert data["command"] == "test-fix"
    assert data["parent_run_id"] == str(root_run.id)

    children = child_runs(audit_store, root_run)
    assert len(children) == 1
    assert children[0].conclusion == TEST_FIX_CONCLUSION
    assert children[0].trigger_comment_id == 6660002


# ---------------------------------------------------------------------------
# 4. Lineage is threaded to the ROOT run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_followup_threads_to_the_root_run_not_the_intermediate(
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
    audit_store: FakeStore,
):
    """A second-round comment must hang off the originating run.

    Without root traversal each generation would nest one level deeper, and
    the 5-iteration refinement budget would reset on every round.
    """
    _, root_run = await seed(fake_audit_db, fake_audit_user_factory)
    first_child = Run(
        repo_id=root_run.repo_id,
        parent_run_id=root_run.id,
        github_run_id=99887767,
        github_delivery_id=str(uuid.uuid4()),
        head_sha=root_run.head_sha,
        head_branch=PR_BRANCH,
        pr_number=42,
        pr_branch=PR_BRANCH,
        status="pr_opened",
        conclusion=FEEDBACK_CONCLUSION,
    )
    fake_audit_db.add(first_child)
    await fake_audit_db.commit()

    payload = issue_comment_payload(body="@haunter fix again please")
    resp = await post_comment(
        client, "issue_comment", payload, signature=signed(TEST_SECRET, payload)
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "queued"
    assert data["parent_run_id"] == str(root_run.id)
    assert data["run_id"] != str(first_child.id)

    rows = child_runs(audit_store, root_run)
    assert len(rows) == 2
    grandchild = next(r for r in rows if r.id != first_child.id)
    assert grandchild.parent_run_id == root_run.id


# ---------------------------------------------------------------------------
# 5. Non-fix commands are a strict no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    ["@haunter audit", "@haunter audit this PR for security issues"],
)
async def test_audit_command_never_reaches_the_fix_pipeline(
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
    audit_store: FakeStore,
    body: str,
):
    _, root_run = await seed(fake_audit_db, fake_audit_user_factory)
    payload = issue_comment_payload(body=body)

    resp = await post_comment(
        client, "issue_comment", payload, signature=signed(TEST_SECRET, payload)
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ignored"
    assert data["reason"] == "no @haunter fix command"
    assert child_runs(audit_store, root_run) == []


# ---------------------------------------------------------------------------
# 6. Signature verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_signature_is_rejected_before_any_run(
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
    audit_store: FakeStore,
):
    _, root_run = await seed(fake_audit_db, fake_audit_user_factory)
    payload = issue_comment_payload()

    resp = await post_comment(client, "issue_comment", payload)

    assert resp.status_code == 401
    assert resp.json() == {"detail": "Invalid or missing signature"}
    assert child_runs(audit_store, root_run) == []


@pytest.mark.asyncio
async def test_forged_signature_is_rejected(
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
    audit_store: FakeStore,
):
    _, root_run = await seed(fake_audit_db, fake_audit_user_factory)
    payload = issue_comment_payload()

    resp = await post_comment(
        client, "issue_comment", payload, signature=signed("not-the-secret", payload)
    )

    assert resp.status_code == 401
    assert child_runs(audit_store, root_run) == []


@pytest.mark.asyncio
async def test_signature_of_a_different_body_is_rejected(
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
    audit_store: FakeStore,
):
    """HMAC covers the raw bytes, so a swapped body cannot pass."""
    _, root_run = await seed(fake_audit_db, fake_audit_user_factory)
    original = issue_comment_payload(body="@haunter fix")
    tampered = issue_comment_payload(body="@haunter test-fix")

    resp = await post_comment(
        client, "issue_comment", tampered, signature=signed(TEST_SECRET, original)
    )

    assert resp.status_code == 401
    assert child_runs(audit_store, root_run) == []


# ---------------------------------------------------------------------------
# 7. Pre-existing guards still hold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_comment_without_a_mention_is_ignored(
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
    audit_store: FakeStore,
):
    _, root_run = await seed(fake_audit_db, fake_audit_user_factory)
    payload = issue_comment_payload(body="LGTM! Merging soon.")

    resp = await post_comment(
        client, "issue_comment", payload, signature=signed(TEST_SECRET, payload)
    )

    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "no @haunter mention"}
    assert child_runs(audit_store, root_run) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "association", ["NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR"]
)
async def test_untrusted_commenters_cannot_drive_a_run(
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
    audit_store: FakeStore,
    association: str,
):
    _, root_run = await seed(fake_audit_db, fake_audit_user_factory)
    payload = issue_comment_payload(author_association=association)

    resp = await post_comment(
        client, "issue_comment", payload, signature=signed(TEST_SECRET, payload)
    )

    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "unauthorized commenter"}
    assert child_runs(audit_store, root_run) == []


@pytest.mark.asyncio
async def test_non_haunter_branch_is_ignored(
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
    audit_store: FakeStore,
):
    """A fix request on someone else's branch must not touch it."""
    _, root_run = await seed(fake_audit_db, fake_audit_user_factory)
    payload = review_comment_payload(head_ref="feature/someone-elses-work")

    resp = await post_comment(
        client,
        "pull_request_review_comment",
        payload,
        signature=signed(TEST_SECRET, payload),
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    assert child_runs(audit_store, root_run) == []


@pytest.mark.asyncio
async def test_unregistered_repository_is_ignored(
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    audit_store: FakeStore,
):
    payload = issue_comment_payload(owner="never-registered", repo="nope")

    resp = await post_comment(
        client, "issue_comment", payload, signature=signed(TEST_SECRET, payload)
    )

    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "unregistered repository"}
    assert audit_store.rows(Run.__table__) == []


@pytest.mark.asyncio
async def test_followup_run_is_queryable_by_lineage(
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    """The follow-up Run is a real, queryable row linked by parent_run_id."""
    _, root_run = await seed(fake_audit_db, fake_audit_user_factory)
    payload = issue_comment_payload(body="@haunter address this")
    resp = await post_comment(
        client, "issue_comment", payload, signature=signed(TEST_SECRET, payload)
    )
    run_id = resp.json()["run_id"]

    session = cast(AsyncSession, fake_audit_db)
    stored = (await session.execute(select(Run).where(Run.id == uuid.UUID(run_id)))).scalar_one()
    assert stored.parent_run_id == root_run.id
    assert stored.conclusion == FEEDBACK_CONCLUSION
    assert stored.head_branch == PR_BRANCH
