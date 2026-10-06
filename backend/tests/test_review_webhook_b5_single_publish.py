"""
Reproduction for B5 — two reviews published per ``pull_request`` delivery.

``app.webhooks.github_webhook``'s ``pull_request`` branch used to dispatch BOTH
publishers for one delivery:

* an auditor-mode ``pr_audit`` (``audit_pipeline.dispatch_audit`` ->
  ``execute_audit_job`` -> ``audit_publisher.publish_audit_review`` ->
  ``POST /pulls/{n}/reviews``), and
* a ``CodeReview`` (``_dispatch_review`` -> ``run_code_review_pipeline`` ->
  ``create_pull_request_review`` -> ``POST /pulls/{n}/reviews``).

Two publishers, no coordination, one PR: two competing formal reviews from two
different subagents, and two spends of the ``POST /pulls/{n}/reviews`` budget
GitHub documents as a secondary-rate-limit trigger.

Intended behaviour, as these tests encode it: for a single ``pull_request``
delivery exactly ONE of the two publishers runs, and it publishes exactly once.
The repo's own auditor opt-in (``repo_settings.enable_auditor_mode`` +
``audit_trigger_on_pr``) is the admin override that picks the owner — it is the
only way to turn the automatic PR audit on, so a repo that turned it on gets the
auditor's read-only review and no competing sentinel review. A default repo
(auditor off) keeps the unconditional code-review sentinel.

The auditor's ``workflow_run`` and ``@haunter audit`` trigger paths are pinned
here too: they are out of scope for B5 and must keep dispatching — the manual
mention in particular is the auditor's only remaining route to a PR audit, and
B5 would be a regression without it.

Harness: the real ``POST /webhooks/github`` route over the ``client`` fixture
and the in-process ``tests/fake_audit_db.py`` store, real HMAC over the raw body
via ``settings.github_webhook_secret``, and the patch targets
``tests/test_code_review_webhook.py`` already uses.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import BackgroundTasks
from sqlalchemy import func, select

from app.config import settings
from app.models import AuditJob, CodeReview, Repo, RepoSettings, WebhookDelivery
from app.services import audit_pipeline, review_orchestrator
from app.services.review_orchestrator import run_code_review_pipeline
from tests.fake_audit_db import (  # noqa: F401  (pytest fixtures)
    FakeAsyncSession,
    FakeStore,
    FakeSessionMaker,
    audit_store,
    fake_audit_db,
    fake_audit_user_factory,
)

TEST_SECRET = settings.github_webhook_secret or "test_webhook_secret_key_12345"

#: The dispatcher signs the per-attempt fence with this; unset in CI, so the
#: claim path would raise SelfInvocationError without it.
TEST_SELF_INVOKE_SECRET = "b5-test-self-invoke-secret"

OWNER = "b5-org"
REPO_NAME = "b5-repo"

PR_NUMBER = 42
HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
BASE_SHA = "9999888877776666555544443333222211110000"

_CODE_REVIEW_LLM_RESPONSE = {
    "content": json.dumps(
        {"risk_score": 10, "summary": "Clean change, no issues found.", "findings": []}
    ),
    "usage": {"input_tokens": 100, "output_tokens": 20},
}

#: The auditor runs four LLM passes and parses a different reply shape.
_AUDITOR_LLM_RESPONSE = {
    "content": json.dumps(
        {"summary": "No grounded issue found.", "confidence": 90, "findings": []}
    ),
    "usage": {"input_tokens": 10, "output_tokens": 5},
    "latency_ms": 1,
    "model": "test-engine",
}

_AUDIT_DIFF = (
    "diff --git a/src/app.ts b/src/app.ts\n"
    "--- a/src/app.ts\n"
    "+++ b/src/app.ts\n"
    "@@ -1,0 +1,1 @@\n"
    "+export const value = 1;\n"
)


@pytest.fixture(autouse=True)
def _self_invoke_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "audit_self_invoke_secret", TEST_SELF_INVOKE_SECRET)


# ---------------------------------------------------------------------------
# Payloads / signing
# ---------------------------------------------------------------------------


def _signed_headers(event: str, payload: dict[str, Any]) -> tuple[dict[str, str], bytes]:
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


def _repository() -> dict[str, Any]:
    return {
        "name": REPO_NAME,
        "full_name": f"{OWNER}/{REPO_NAME}",
        "owner": {"login": OWNER},
    }


def _pr_payload(
    *,
    action: str = "opened",
    pr_number: int = PR_NUMBER,
    sha: str = HEAD_SHA,
) -> dict[str, Any]:
    return {
        "action": action,
        "number": pr_number,
        "pull_request": {
            "number": pr_number,
            "state": "open",
            "draft": False,
            "head": {"ref": "feature/b5", "sha": sha},
            "base": {"ref": "main", "sha": BASE_SHA},
            "user": {"login": "dev-user", "type": "User"},
        },
        "repository": _repository(),
        "sender": {"login": "dev-user", "type": "User"},
    }


def _workflow_payload(*, run_id: int = 770001) -> dict[str, Any]:
    return {
        "action": "completed",
        "workflow_run": {
            "id": run_id,
            "head_sha": HEAD_SHA,
            "head_branch": "main",
            "conclusion": "failure",
            "html_url": f"https://github.com/{OWNER}/{REPO_NAME}/actions/runs/{run_id}",
        },
        "repository": _repository(),
    }


def _review_comment_payload(*, comment_id: int, body: str) -> dict[str, Any]:
    return {
        "action": "created",
        "pull_request": {
            "number": PR_NUMBER,
            "head": {"ref": "feature/b5", "sha": HEAD_SHA},
            "base": {"ref": "main", "sha": BASE_SHA},
        },
        "comment": {
            "id": comment_id,
            "body": body,
            "author_association": "MEMBER",
            "user": {"login": "dev-user", "type": "User"},
        },
        "repository": _repository(),
        "sender": {"login": "dev-user", "type": "User"},
    }


# ---------------------------------------------------------------------------
# Fake-store helpers
# ---------------------------------------------------------------------------


def _hosting_adapter() -> MagicMock:
    adapter = MagicMock()
    adapter.schedule_review = AsyncMock()
    adapter.schedule_pipeline = AsyncMock()
    adapter.schedule_audit = AsyncMock()
    return adapter


async def _seed_repo(
    db: FakeAsyncSession,
    user_factory,
    *,
    owner: str = OWNER,
    name: str = REPO_NAME,
) -> Repo:
    user = await user_factory(
        github_id=int(uuid.uuid4().int % 1_000_000_000 + 500_000_000),
        username=f"b5-user-{uuid.uuid4().hex[:6]}",
    )
    repo = Repo(user_id=user.id, owner=owner, name=name, default_branch="main")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)
    return repo


async def _enable_auditor(db: FakeAsyncSession, repo: Repo) -> RepoSettings:
    """The admin opt-in: Auditor Mode on, PR trigger on."""
    row = RepoSettings(
        repo_id=repo.id,
        enable_auditor_mode=True,
        audit_trigger_on_pr=True,
        audit_trigger_on_ci_failure=True,
        audit_trigger_on_ci_success=False,
        audit_trigger_on_manual_mention=True,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def _audit_jobs(db: FakeAsyncSession, repo_id: Any) -> list[AuditJob]:
    return list(
        (
            await db.scalars(
                select(AuditJob)
                .where(AuditJob.repo_id == repo_id)
                .order_by(AuditJob.created_at)
            )
        ).all()
    )


async def _review_rows(db: FakeAsyncSession, repo: Repo) -> list[CodeReview]:
    """CodeReview rows, with the ``repo`` relationship a flush would have set.

    The fake store hands back live ORM objects rather than bound instances, so
    ``run_code_review_pipeline``'s ``selectinload(CodeReview.repo)`` finds
    nothing unless the relationship is populated the way an INSERT would.
    """
    rows = list((await db.scalars(select(CodeReview))).all())
    for row in rows:
        row.repo = repo
    return rows


def _wire_review_orchestrator(
    store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``review_orchestrator`` binds ``async_session_maker`` at import time."""
    monkeypatch.setattr(
        review_orchestrator, "async_session_maker", FakeSessionMaker(store)
    )


def _publish_recorder(calls: list[dict[str, Any]]):
    async def _record(**kwargs: Any) -> dict[str, int]:
        calls.append(kwargs)
        return {"id": 1}

    return _record


# ---------------------------------------------------------------------------
# B5 — the double-dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b5_pull_request_delivery_dispatches_exactly_one_pr_reviewer(
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    """One pull_request delivery, one review publisher.

    The double-dispatch is only reachable in the configuration an admin opted
    into — `enable_auditor_mode` + `audit_trigger_on_pr` — because that setting
    is off for every default repo and `evaluate_pr` refuses the PR trigger
    without it. With it on, the delivery used to claim an auditor `pr_audit` AND
    commit + dispatch a `CodeReview`; both POST to /pulls/{n}/reviews.

    Intended: exactly one publisher runs for the delivery, so at most one review
    can reach GitHub.
    """
    repo = await _seed_repo(fake_audit_db, fake_audit_user_factory)
    await _enable_auditor(fake_audit_db, repo)
    adapter = _hosting_adapter()

    with patch(
        "app.adapters.hosting.get_hosting_adapter",
        new_callable=AsyncMock,
        return_value=adapter,
    ):
        headers, body = _signed_headers("pull_request", _pr_payload())
        resp = await client.post("/webhooks/github", headers=headers, content=body)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "queued"

    jobs = await _audit_jobs(fake_audit_db, repo.id)
    reviews = await _review_rows(fake_audit_db, repo)

    assert len(jobs) + len(reviews) == 1, (
        "the delivery set in motion BOTH review publishers: "
        f"{len(jobs)} auditor pr_audit dispatch(es) and "
        f"{adapter.schedule_review.await_count} code-review dispatch(es) over "
        f"{len(reviews)} CodeReview row(s). Both POST to /pulls/{PR_NUMBER}/"
        "reviews, so the PR receives two competing formal reviews and the "
        "secondary-rate-limit budget is spent twice for one delivery "
        f"(audit_types={[j.audit_type for j in jobs]})"
    )


@pytest.mark.asyncio
async def test_b5_auditor_owned_delivery_posts_one_pr_review(
    audit_store: FakeStore,
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
    monkeypatch: pytest.MonkeyPatch,
):
    """The surviving publisher really does POST to /pulls/{n}/reviews once.

    Dispatch-level exclusivity is only half of B5: the harm is the number of
    requests to the endpoint GitHub documents as a secondary-rate-limit trigger.
    This drives whichever publishers the delivery set in motion all the way to
    their publish call and counts them.

    Intended: exactly one POST to /pulls/{n}/reviews for the delivery.
    """
    _wire_review_orchestrator(audit_store, monkeypatch)
    repo = await _seed_repo(fake_audit_db, fake_audit_user_factory)
    await _enable_auditor(fake_audit_db, repo)
    adapter = _hosting_adapter()

    with patch(
        "app.adapters.hosting.get_hosting_adapter",
        new_callable=AsyncMock,
        return_value=adapter,
    ):
        headers, body = _signed_headers("pull_request", _pr_payload())
        resp = await client.post("/webhooks/github", headers=headers, content=body)
    assert resp.status_code == 200, resp.text

    publish_calls: list[dict[str, Any]] = []
    record = _publish_recorder(publish_calls)

    with (
        patch(
            "app.services.audit_pipeline.get_auditor_installation_token",
            new_callable=AsyncMock,
            return_value="read-only-token",
        ),
        patch(
            "app.github_client.fetch_pull_request",
            new_callable=AsyncMock,
            return_value={"base": {"sha": BASE_SHA}, "head": {"sha": HEAD_SHA}},
        ),
        patch(
            "app.github_client.fetch_diff",
            new_callable=AsyncMock,
            return_value=_AUDIT_DIFF,
        ),
        patch(
            "app.github_client.fetch_file_content",
            new_callable=AsyncMock,
            return_value="const previous = 0;\nexport const value = 1;\n",
        ),
        patch(
            "app.subagents.auditor.LLMClient.complete",
            AsyncMock(return_value=_AUDITOR_LLM_RESPONSE),
        ),
        patch("app.github_client.create_pr_review", new=record),
        patch(
            "app.services.review_orchestrator.get_installation_token",
            new_callable=AsyncMock,
            return_value="write-token",
        ),
        patch(
            "app.services.review_orchestrator.fetch_pull_request",
            new_callable=AsyncMock,
            return_value={"head": {"sha": HEAD_SHA}},
        ),
        patch(
            "app.services.review_orchestrator.fetch_pull_request_diff",
            new_callable=AsyncMock,
            return_value=_AUDIT_DIFF,
        ),
        patch(
            "app.services.review_orchestrator.resolve_installation_id",
            new_callable=AsyncMock,
            return_value=424242,
        ),
        patch("app.subagents.code_reviewer.LLMClient") as code_llm_cls,
        patch("app.services.review_orchestrator.create_pull_request_review", new=record),
    ):
        code_llm_cls.return_value.complete = AsyncMock(
            return_value=_CODE_REVIEW_LLM_RESPONSE
        )

        summary = await audit_pipeline.dispatch_audit_jobs(
            batch_size=1, local_execute=True
        )
        assert summary.claimed == 1, "the auditor job was not claimed for execution"
        for review in await _review_rows(fake_audit_db, repo):
            await run_code_review_pipeline(review.id)

    assert len(publish_calls) == 1, (
        f"expected exactly one POST to /pulls/{PR_NUMBER}/reviews, got "
        f"{len(publish_calls)}"
    )
    assert publish_calls[0]["pr_number"] == PR_NUMBER


@pytest.mark.asyncio
async def test_b5_default_repo_still_gets_exactly_one_review(
    audit_store: FakeStore,
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
    monkeypatch: pytest.MonkeyPatch,
):
    """The default configuration is untouched: no auditor, one sentinel review.

    `enable_auditor_mode` ships False, so `evaluate_pr` refuses the PR trigger
    and the delivery belongs to the code-review sentinel alone. B5 must not have
    cost that path its review.

    Intended: one dispatched `CodeReview`, publishing exactly once.
    """
    _wire_review_orchestrator(audit_store, monkeypatch)
    repo = await _seed_repo(fake_audit_db, fake_audit_user_factory)
    adapter = _hosting_adapter()

    with patch(
        "app.adapters.hosting.get_hosting_adapter",
        new_callable=AsyncMock,
        return_value=adapter,
    ):
        headers, body = _signed_headers("pull_request", _pr_payload())
        resp = await client.post("/webhooks/github", headers=headers, content=body)

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["status"] == "queued"
    assert "review_id" in payload

    assert await _audit_jobs(fake_audit_db, repo.id) == []
    reviews = await _review_rows(fake_audit_db, repo)
    assert len(reviews) == 1, reviews
    assert adapter.schedule_review.await_count == 1

    publish_calls: list[dict[str, Any]] = []
    with (
        patch(
            "app.services.review_orchestrator.get_installation_token",
            new_callable=AsyncMock,
            return_value="write-token",
        ),
        patch(
            "app.services.review_orchestrator.fetch_pull_request",
            new_callable=AsyncMock,
            return_value={"head": {"sha": HEAD_SHA}},
        ),
        patch(
            "app.services.review_orchestrator.fetch_pull_request_diff",
            new_callable=AsyncMock,
            return_value=_AUDIT_DIFF,
        ),
        patch(
            "app.services.review_orchestrator.resolve_installation_id",
            new_callable=AsyncMock,
            return_value=424242,
        ),
        patch("app.subagents.code_reviewer.LLMClient") as code_llm_cls,
        patch(
            "app.services.review_orchestrator.create_pull_request_review",
            new=_publish_recorder(publish_calls),
        ),
    ):
        code_llm_cls.return_value.complete = AsyncMock(
            return_value=_CODE_REVIEW_LLM_RESPONSE
        )
        await run_code_review_pipeline(reviews[0].id)

    assert len(publish_calls) == 1, (
        f"expected exactly one POST to /pulls/{PR_NUMBER}/reviews, got "
        f"{len(publish_calls)}"
    )


# ---------------------------------------------------------------------------
# Blast radius — the auditor's other trigger paths must keep dispatching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b5_workflow_run_auditor_trigger_is_unaffected(
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    """B5 is scoped to pull_request; workflow_run keeps its auditor dispatch."""
    repo = await _seed_repo(fake_audit_db, fake_audit_user_factory)
    await _enable_auditor(fake_audit_db, repo)
    adapter = _hosting_adapter()

    with patch(
        "app.adapters.hosting.get_hosting_adapter",
        new_callable=AsyncMock,
        return_value=adapter,
    ):
        headers, body = _signed_headers("workflow_run", _workflow_payload())
        resp = await client.post("/webhooks/github", headers=headers, content=body)

    assert resp.status_code == 200, resp.text
    jobs = await _audit_jobs(fake_audit_db, repo.id)
    assert [j.audit_type for j in jobs] == ["ci_failure_audit"], jobs


@pytest.mark.asyncio
async def test_b5_manual_mention_still_reaches_a_pr_audit(
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    """`@haunter audit` on a review comment still queues the PR audit.

    B5 made the automatic PR trigger mutually exclusive with the sentinel. The
    manual mention is the auditor's remaining route to a PR audit and is the one
    an owner uses to ask for one explicitly, so it must survive: B5 would be a
    regression if the only way to reach a PR audit disappeared.
    """
    repo = await _seed_repo(fake_audit_db, fake_audit_user_factory)
    await _enable_auditor(fake_audit_db, repo)
    adapter = _hosting_adapter()

    with patch(
        "app.adapters.hosting.get_hosting_adapter",
        new_callable=AsyncMock,
        return_value=adapter,
    ):
        headers, body = _signed_headers(
            "pull_request_review_comment",
            _review_comment_payload(
                comment_id=9300001, body="@haunter audit this PR for security issues"
            ),
        )
        resp = await client.post("/webhooks/github", headers=headers, content=body)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "audit_queued", resp.text
    jobs = await _audit_jobs(fake_audit_db, repo.id)
    assert [j.audit_type for j in jobs] == ["manual_audit"], jobs
    assert jobs[0].pr_number == PR_NUMBER
    assert jobs[0].base_sha == BASE_SHA
    assert jobs[0].head_sha == HEAD_SHA


# ---------------------------------------------------------------------------
# Replay safety — the auditor-owned exit must behave under the replay ContextVars
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b5_replay_of_an_auditor_owned_delivery_reaches_the_same_decision(
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    """The new auditor-owned exit behaves correctly under the replay ContextVars.

    `replay_webhook_delivery` re-enters `github_webhook` under
    `_replay_of_var` / `_replay_repo_id_var` (webhooks.py:740-753) and then looks
    for the row the re-run appended. B5 gave the pull_request branch a NEW
    terminal exit, so two things had to keep holding: the exit records a row the
    replay route can anchor on, and that row is stamped with `replay_of` and
    scoped to the authorized repo id.

    Driven exactly the way the route drives it — the same `_synthetic_github_request`
    re-signing, the same ContextVars — because the route's own tenant scoping is a
    ``repos.user_id`` subquery that `tests/fake_audit_db.py` deliberately refuses
    to model (it fails loud on unmodelled SQL), so the route cannot be driven
    end-to-end on this harness.

    Intended: the replay re-reaches the auditor-owned decision and the row it
    appends carries `replay_of=<original>` and the pinned `repo_id`.
    """
    from app.webhooks import (
        _replay_of_var,
        _replay_repo_id_var,
        _synthetic_github_request,
        github_webhook,
    )

    repo = await _seed_repo(fake_audit_db, fake_audit_user_factory)
    await _enable_auditor(fake_audit_db, repo)
    adapter = _hosting_adapter()
    _, raw_body = _signed_headers("pull_request", _pr_payload())

    with patch(
        "app.adapters.hosting.get_hosting_adapter",
        new_callable=AsyncMock,
        return_value=adapter,
    ):
        live = await client.post(
            "/webhooks/github",
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": str(uuid.uuid4()),
                "X-Hub-Signature-256": "sha256="
                + hmac.new(
                    TEST_SECRET.encode("utf-8"), raw_body, hashlib.sha256
                ).hexdigest(),
            },
            content=raw_body,
        )
    assert live.status_code == 200, live.text
    assert live.json()["reason"] == "auditor pr audit owns this delivery"

    rows = list(
        (
            await fake_audit_db.scalars(
                select(WebhookDelivery)
                .where(WebhookDelivery.repo_id == repo.id)
                .order_by(WebhookDelivery.created_at)
            )
        ).all()
    )
    assert len(rows) == 1, rows
    original = rows[0]
    assert original.replay_of is None
    assert "review_owner=auditor" in (original.reason or "")

    # Re-drive the stored bytes under the replay ContextVars, as the route does.
    signature = "sha256=" + hmac.new(
        TEST_SECRET.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    with patch(
        "app.adapters.hosting.get_hosting_adapter",
        new_callable=AsyncMock,
        return_value=adapter,
    ):
        of_token = _replay_of_var.set(original.id)
        repo_token = _replay_repo_id_var.set(repo.id)
        try:
            decision = await github_webhook(
                request=_synthetic_github_request(raw_body),
                background_tasks=BackgroundTasks(),
                db=fake_audit_db,
                x_github_delivery=f"replay-{uuid.uuid4().hex}",
                x_github_event="pull_request",
                x_hub_signature_256=signature,
            )
        finally:
            _replay_repo_id_var.reset(repo_token)
            _replay_of_var.reset(of_token)

    assert decision["status"] == "queued"
    assert decision["reason"] == "auditor pr audit owns this delivery"

    replay_rows = list(
        (
            await fake_audit_db.scalars(
                select(WebhookDelivery).where(
                    WebhookDelivery.replay_of == original.id
                )
            )
        ).all()
    )
    assert len(replay_rows) == 1, replay_rows
    assert replay_rows[0].repo_id == repo.id, (
        "the replayed row was not scoped to the pinned repo id"
    )
    assert "review_owner=auditor" in (replay_rows[0].reason or "")


