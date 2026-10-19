"""
Stage 1 / Repro Agent A — executable reproductions for the PR-review webhook
entry and review-lifecycle defects.

Every test in this file encodes the behaviour the pipeline is SUPPOSED to have
and is expected to FAIL against the current unfixed code. Each failure is the
reproduction; none of them may be weakened to go green.

Hermetic by construction: no network, no PostgreSQL, no GitHub, no LLM. The
webhook tests drive the real ``POST /webhooks/github`` route over the existing
``client`` fixture and the existing ``tests/fake_audit_db.py`` in-process store
(``fake_audit_db`` / ``fake_audit_user_factory``), which is the repo's own
harness for exactly this: real ``app.models`` rows behind the real SQLAlchemy
statements production code builds, with only the transport replaced. The
orchestrator tests wire that same store into
``app.services.review_orchestrator.async_session_maker`` (a module-level
``from app.db import async_session_maker``, so patching ``app.db`` is not
enough) and stub the LLM + GitHub calls the way
``tests/test_code_review_webhook.py`` already does.

Defect classes covered:

===  =========================================================================
A1   Dedup key is ``repo_id + commit_sha`` with no ``pr_number`` predicate, so
     a ``push`` review row (pr_number IS NULL) suppresses the ``pull_request``
     review for the same SHA and vice versa.
A2   The CodeReview row is committed BEFORE ``schedule_review``, so a dispatch
     failure orphans it in ``pending``/``in_progress`` — and the A1 dedup guard
     then swallows every redelivery as ``duplicate``.
A3   No wall-clock bound on ``run_code_review_pipeline``, and not every step is
     guarded, so a hang or an escaping error strands the review in
     ``in_progress`` forever.
A5   ``webhooks.github_webhook`` accepts only ``opened``/``synchronize``, so a
     draft→ready (``ready_for_review``) or reopened PR is never reviewed.
A6   ``enable_pr_comments=False`` marks the review ``completed`` with zero
     GitHub output, indistinguishable from a successfully published review.
===  =========================================================================
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.models import CodeReview, Repo, RepoSettings
from app.services import review_orchestrator
from app.services.review_orchestrator import run_code_review_pipeline
from app.subagents.code_reviewer import CodeReviewOutput, ReviewResult
from tests.fake_audit_db import (  # noqa: F401  (pytest fixtures)
    FakeAsyncSession,
    FakeStore,
    FakeSessionMaker,
    audit_store,
    fake_audit_db,
    fake_audit_user_factory,
)

TEST_SECRET = settings.github_webhook_secret or "test_webhook_secret_key_12345"

OWNER = "repro-org"
REPO_NAME = "repro-repo"

#: One commit SHA shared by every delivery below. The SHA is the whole dedup key
#: today, which is exactly what A1 is about.
COMMIT_SHA = "0123456789abcdef0123456789abcdef01234567"

#: The wall-clock bound the A3 timeout reproduction makes affordable. The real
#: bound is a production-grade number; the test only cares that SOME finite
#: bound exists and that exceeding it ends the review.
TIMEOUT_S = 0.05

_LLM_RESPONSE = {
    "content": json.dumps(
        {"risk_score": 10, "summary": "Clean change, no issues found.", "findings": []}
    ),
    "usage": {"input_tokens": 100, "output_tokens": 20},
}


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
    pr_number: int = 42,
    sha: str = COMMIT_SHA,
    draft: bool = False,
) -> dict[str, Any]:
    return {
        "action": action,
        "number": pr_number,
        "pull_request": {
            "number": pr_number,
            "state": "open",
            "draft": draft,
            "head": {"ref": "feature/repro", "sha": sha},
            "base": {"ref": "main", "sha": "f" * 40},
            "user": {"login": "dev-user", "type": "User"},
        },
        "repository": _repository(),
        "sender": {"login": "dev-user", "type": "User"},
    }


def _push_payload(*, sha: str = COMMIT_SHA) -> dict[str, Any]:
    return {
        "ref": "refs/heads/main",
        "deleted": False,
        "head_commit": {
            "id": sha,
            "author": {"name": "Senior Dev", "email": "dev@example.com"},
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
        github_id=int(uuid.uuid4().int % 1_000_000_000 + 300_000_000),
        username=f"repro-user-{uuid.uuid4().hex[:6]}",
    )
    repo = Repo(user_id=user.id, owner=owner, name=name, default_branch="main")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)
    return repo


async def _review_rows(db: FakeAsyncSession) -> list[CodeReview]:
    result = await db.execute(select(CodeReview))
    return list(result.scalars().all())


async def _reload_review(db: FakeAsyncSession, review_id: uuid.UUID) -> CodeReview:
    result = await db.execute(select(CodeReview).where(CodeReview.id == review_id))
    return result.scalars().first()


def _seed_review(
    db: FakeAsyncSession,
    repo: Repo,
    *,
    pr_number: int | None,
    sha: str = COMMIT_SHA,
) -> CodeReview:
    review = CodeReview(
        repo_id=repo.id,
        commit_sha=sha,
        pr_number=pr_number,
        risk_score=0,
        summary="Autonomous code review queued.",
        findings=[],
        status="pending",
    )
    # The fake store hands back live ORM objects, not bound instances, so the
    # relationship the orchestrator's selectinload(CodeReview.repo) expects has
    # to be populated the way a flush would.
    review.repo = repo
    db.add(review)
    return review


def _wire_review_orchestrator(
    store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point the orchestrator at the in-process store.

    ``review_orchestrator`` binds ``async_session_maker`` at import time
    (``from app.db import async_session_maker``), so the attribute on
    ``app.db`` that ``fake_audit_db`` patches is NOT what it uses.
    """
    monkeypatch.setattr(
        review_orchestrator, "async_session_maker", FakeSessionMaker(store)
    )


def _outcome(review: CodeReview) -> tuple[str, str | None]:
    """The pair a dashboard reader sees as 'did this produce anything?'."""
    return (review.status, review.failure_reason)


# ---------------------------------------------------------------------------
# A1 — the dedup key has no pr_number predicate, so push and pull_request
#      suppress each other for the same commit SHA.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a1_push_review_row_does_not_suppress_the_pull_request_review(
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    """A commit-scoped (push) review row must not suppress the PR-scoped review.

    GitHub fires `push` and then `pull_request` for the same commit whenever a PR
    head advances. The push handler commits a CodeReview row with
    pr_number=None; the pull_request handler then dedups on repo_id +
    commit_sha + status and answers "duplicate", so the PR is never reviewed and
    nothing is ever posted to the PR.

    Intended: the PR delivery queues its own review, the resulting CodeReview row
    is PR-scoped (pr_number == 42), and the work is dispatched.
    """
    await _seed_repo(fake_audit_db, fake_audit_user_factory)
    adapter = _hosting_adapter()

    with patch(
        "app.adapters.hosting.get_hosting_adapter",
        new_callable=AsyncMock,
        return_value=adapter,
    ):
        push_headers, push_body = _signed_headers("push", _push_payload())
        push_resp = await client.post(
            "/webhooks/github", headers=push_headers, content=push_body
        )
        assert push_resp.status_code == 200
        assert push_resp.json()["status"] == "queued"

        pr_headers, pr_body = _signed_headers("pull_request", _pr_payload())
        pr_resp = await client.post(
            "/webhooks/github", headers=pr_headers, content=pr_body
        )

    assert pr_resp.status_code == 200
    body = pr_resp.json()
    assert (
        body["status"] == "queued"
    ), f"PR review was suppressed by the commit-scoped push row: {body}"
    assert body["pr_number"] == 42
    assert adapter.schedule_review.await_count == 2, (
        "the PR delivery must dispatch its own review work; "
        f"schedule_review was awaited {adapter.schedule_review.await_count}x"
    )

    pr_rows = [r for r in await _review_rows(fake_audit_db) if r.pr_number == 42]
    assert len(pr_rows) == 1, f"expected exactly one PR-scoped review row, got {pr_rows}"
    assert pr_rows[0].commit_sha == COMMIT_SHA


@pytest.mark.asyncio
async def test_a1_pr_review_row_does_not_suppress_the_commit_scoped_push_review(
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    """Mirror of the test above: the two rows are different units of work.

    A PR review posts an inline formal review on the PR; a push review posts a
    commit comment. They share a commit SHA and nothing else, so one must never
    dedupe the other away.

    Intended: the push delivery queues its own commit-scoped (pr_number IS NULL)
    review even when a PR-scoped row already exists for the same SHA.
    """
    await _seed_repo(fake_audit_db, fake_audit_user_factory)
    adapter = _hosting_adapter()

    with patch(
        "app.adapters.hosting.get_hosting_adapter",
        new_callable=AsyncMock,
        return_value=adapter,
    ):
        pr_headers, pr_body = _signed_headers("pull_request", _pr_payload())
        pr_resp = await client.post(
            "/webhooks/github", headers=pr_headers, content=pr_body
        )
        assert pr_resp.status_code == 200
        assert pr_resp.json()["status"] == "queued"

        push_headers, push_body = _signed_headers("push", _push_payload())
        push_resp = await client.post(
            "/webhooks/github", headers=push_headers, content=push_body
        )

    assert push_resp.status_code == 200
    body = push_resp.json()
    assert (
        body["status"] == "queued"
    ), f"commit-scoped push review was suppressed by the PR-scoped row: {body}"
    assert adapter.schedule_review.await_count == 2, (
        "the push delivery must dispatch its own review work; "
        f"schedule_review was awaited {adapter.schedule_review.await_count}x"
    )

    rows = await _review_rows(fake_audit_db)
    commit_rows = [
        r
        for r in rows
        if r.pr_number is None and r.commit_sha == COMMIT_SHA
    ]
    assert len(commit_rows) == 1, (
        f"expected exactly one commit-scoped review row, got {commit_rows}"
    )


# ---------------------------------------------------------------------------
# A2 — the row is committed before dispatch, so a dispatch failure orphans it
#      and every redelivery is swallowed as "duplicate".
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a2_dispatch_failure_is_durably_recorded_on_the_review_row(
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    """A failed schedule_review must not leave an indistinguishable orphan.

    webhooks.github_webhook commits the CodeReview row and only then calls
    adapter.schedule_review. AWSHostingAdapter re-raises SelfInvocationError
    bare and _invoke_lambda_async raises RuntimeError on a non-202, so the row
    already exists, is still "pending", and carries no failure_reason — the one
    signal an operator has to tell "queued" from "never dispatched".

    Intended: the dispatch failure is durably recorded on the review row, and
    the row does not remain in the "pending" state that makes redelivery dedupe.
    """
    await _seed_repo(fake_audit_db, fake_audit_user_factory)
    adapter = _hosting_adapter()
    adapter.schedule_review = AsyncMock(
        side_effect=RuntimeError("Lambda async invocation was not accepted")
    )

    escaped: BaseException | None = None
    with patch(
        "app.adapters.hosting.get_hosting_adapter",
        new_callable=AsyncMock,
        return_value=adapter,
    ):
        headers, body = _signed_headers("pull_request", _pr_payload())
        try:
            await client.post("/webhooks/github", headers=headers, content=body)
        except Exception as exc:  # noqa: BLE001 — recorded, asserted on below
            escaped = exc

    rows = await _review_rows(fake_audit_db)
    assert len(rows) == 1, f"expected one review row, got {rows}"
    review = rows[0]

    assert review.failure_reason, (
        "the dispatch failure left no durable trace: the review is pending with "
        f"failure_reason=None (dispatch error escaped as {escaped!r})"
    )
    assert review.status != "pending", (
        "a review that was never dispatched must not stay in the pending state, "
        "because the dedup guard treats pending as 'already handled' and every "
        f"redelivery is then answered 'duplicate' (dispatch error: {escaped!r})"
    )


@pytest.mark.asyncio
async def test_a2_redelivery_after_a_dispatch_failure_can_still_schedule_the_review(
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    """GitHub redelivers; the second delivery must be able to do the work.

    The orphan left behind by a failed dispatch has status "pending", which the
    dedup guard counts as handled — so the retry is answered "duplicate" and the
    review is never run, even though the hosting adapter would now succeed.

    Intended: with a healthy adapter, a second delivery of the same event
    schedules the review.
    """
    await _seed_repo(fake_audit_db, fake_audit_user_factory)
    adapter = _hosting_adapter()
    adapter.schedule_review = AsyncMock(
        side_effect=[RuntimeError("Lambda async invocation was not accepted"), None]
    )

    with patch(
        "app.adapters.hosting.get_hosting_adapter",
        new_callable=AsyncMock,
        return_value=adapter,
    ):
        first_headers, payload = _signed_headers("pull_request", _pr_payload())
        try:
            await client.post("/webhooks/github", headers=first_headers, content=payload)
        except Exception:  # noqa: BLE001 — the escaping 500 IS the first defect
            pass

        # Redelivery: new X-GitHub-Delivery, byte-identical signed payload.
        retry_headers, retry_body = _signed_headers("pull_request", _pr_payload())
        retry_resp = await client.post(
            "/webhooks/github", headers=retry_headers, content=retry_body
        )

    assert retry_resp.status_code == 200
    body = retry_resp.json()
    assert (
        body["status"] == "queued"
    ), f"redelivery was swallowed by the orphaned pending row: {body}"
    assert adapter.schedule_review.await_count == 2, (
        "the redelivery must reach schedule_review; it was awaited "
        f"{adapter.schedule_review.await_count}x"
    )


# ---------------------------------------------------------------------------
# A3 — no wall-clock bound, and a step outside every try, so a hang or an
#      escaping error strands the review in "in_progress" forever.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a3_unguarded_step_failure_does_not_strand_the_review_in_progress(
    audit_store: FakeStore,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
    monkeypatch: pytest.MonkeyPatch,
):
    """Every failure path must end in a recorded terminal state.

    review_orchestrator.run_code_review_pipeline wraps the diff fetch, the LLM
    analysis and the GitHub publish in try/except that each set status="error"
    and a failure_reason — but `get_repo_settings` (used to read
    enable_pr_comments) sits outside every one of them. An exception there
    escapes the function with the row already flipped to "in_progress" at
    function entry, and "in_progress" is never revisited: the dedup guard counts
    it as handled, and no reaper exists.

    Intended: the review ends in a terminal state with the failure recorded.
    """
    _wire_review_orchestrator(audit_store, monkeypatch)
    repo = await _seed_repo(fake_audit_db, fake_audit_user_factory)
    review = _seed_review(fake_audit_db, repo, pr_number=42)
    await fake_audit_db.commit()
    await fake_audit_db.refresh(review)
    review_id = review.id

    escaped: BaseException | None = None
    with (
        patch(
            "app.services.review_orchestrator.get_installation_token",
            new_callable=AsyncMock,
            return_value="mock-token",
        ),
        patch(
            "app.services.review_orchestrator.fetch_pull_request_diff",
            new_callable=AsyncMock,
            return_value="diff --git a/x.py b/x.py\n+x = 1",
        ),
        patch("app.subagents.code_reviewer.LLMClient") as llm_cls,
        patch(
            "app.services.review_orchestrator.create_pull_request_review",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "app.services.review_orchestrator.get_repo_settings",
            new_callable=AsyncMock,
            side_effect=RuntimeError("repo settings unavailable"),
        ),
    ):
        llm_cls.return_value.complete = AsyncMock(return_value=_LLM_RESPONSE)
        try:
            await run_code_review_pipeline(review_id)
        except Exception as exc:  # noqa: BLE001 — recorded, asserted on below
            escaped = exc

    refreshed = await _reload_review(fake_audit_db, review_id)
    assert refreshed.status not in ("pending", "in_progress"), (
        "the review was stranded: a settings-lookup failure left status="
        f"{refreshed.status!r} with no terminal outcome "
        f"(exception escaped as {escaped!r})"
    )
    assert refreshed.failure_reason, (
        "the failure left no durable trace on the review row "
        f"(exception escaped as {escaped!r})"
    )


@pytest.mark.asyncio
async def test_a3_hung_analysis_is_bounded_and_ends_the_review(
    audit_store: FakeStore,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
    monkeypatch: pytest.MonkeyPatch,
):
    """A pipeline that hangs must not sit in "in_progress" until Lambda kills it.

    app/orchestrator.py bounds its body with asyncio.wait_for(...,
    ORCHESTRATOR_TIMEOUT_S) and records "orchestrator wall-clock timeout".
    review_orchestrator.py has no bound at all, so a hung GitHub fetch or a hung
    LLM call leaves the row "in_progress" with risk_score=0 forever.

    The bound is made affordable here two ways so this test does not prescribe
    where the fix puts it: a module-level knob (the ORCHESTRATOR_TIMEOUT_S
    pattern) is set to TIMEOUT_S, and asyncio.wait_for is clamped to TIMEOUT_S
    so an inline literal timeout fires just the same.
    """
    _wire_review_orchestrator(audit_store, monkeypatch)
    monkeypatch.setattr(
        review_orchestrator, "REVIEW_TIMEOUT_S", TIMEOUT_S, raising=False
    )

    real_wait_for = asyncio.wait_for

    async def _clamped_wait_for(awaitable: Any, timeout: Any = None) -> Any:
        if not isinstance(timeout, (int, float)) or timeout > TIMEOUT_S:
            timeout = TIMEOUT_S
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(asyncio, "wait_for", _clamped_wait_for)

    repo = await _seed_repo(fake_audit_db, fake_audit_user_factory)
    review = _seed_review(fake_audit_db, repo, pr_number=42)
    await fake_audit_db.commit()
    await fake_audit_db.refresh(review)
    review_id = review.id

    async def _slow_analyze_diff(**_kwargs: Any) -> ReviewResult:
        # Comfortably longer than the bound under test: without a wall-clock
        # bound this returns normally and the review is reported as "completed".
        await asyncio.sleep(TIMEOUT_S * 8)
        return ReviewResult(
            output=CodeReviewOutput(
                risk_score=10, summary="Clean change, no issues found.", findings=[]
            ),
            input_tokens=1,
            output_tokens=1,
            latency_ms=int(TIMEOUT_S * 8 * 1000),
        )

    with (
        patch(
            "app.services.review_orchestrator.get_installation_token",
            new_callable=AsyncMock,
            return_value="mock-token",
        ),
        patch(
            "app.services.review_orchestrator.fetch_pull_request_diff",
            new_callable=AsyncMock,
            return_value="diff --git a/x.py b/x.py\n+x = 1",
        ),
        patch(
            "app.services.review_orchestrator.analyze_diff", new=_slow_analyze_diff
        ),
        patch(
            "app.services.review_orchestrator.create_pull_request_review",
            new_callable=AsyncMock,
            return_value={},
        ),
    ):
        await run_code_review_pipeline(review_id)

    refreshed = await _reload_review(fake_audit_db, review_id)
    assert refreshed.status == "error", (
        "a review whose analysis outran the wall-clock bound must end in error, "
        f"got status={refreshed.status!r}"
    )
    assert refreshed.failure_reason, (
        "the wall-clock timeout left no durable trace on the review row"
    )


# ---------------------------------------------------------------------------
# A5 — only "opened"/"synchronize" are accepted, so a draft→ready or reopened
#      PR is never reviewed.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["ready_for_review", "reopened"])
async def test_a5_ready_for_review_and_reopened_prs_are_reviewed(
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
    action: str,
):
    """A draft promoted to ready is reviewable work and must be reviewed.

    webhooks.github_webhook returns {"status": "ignored", "reason": "unsupported
    PR action: <action>"} for anything outside ("opened", "synchronize"). With
    repo_settings.ignore_draft_prs defaulting to True, a PR opened as a draft is
    dropped at open time and then dropped again at ready_for_review, so it is
    never reviewed at all. `reopened` is the same hole after a close/reopen.

    Intended: the delivery is not rejected as an unsupported action, a review is
    dispatched, and a PR-scoped CodeReview row exists.
    """
    await _seed_repo(fake_audit_db, fake_audit_user_factory)
    adapter = _hosting_adapter()

    with patch(
        "app.adapters.hosting.get_hosting_adapter",
        new_callable=AsyncMock,
        return_value=adapter,
    ):
        # GitHub sends draft=false on ready_for_review: the PR is no longer a
        # draft, so the draft guard must not be what stops it either.
        headers, body = _signed_headers(
            "pull_request", _pr_payload(action=action, draft=False)
        )
        resp = await client.post("/webhooks/github", headers=headers, content=body)

    assert resp.status_code == 200
    payload = resp.json()
    assert payload.get("reason") != f"unsupported PR action: {action}", (
        f"pull_request.{action} is dropped as an unsupported action: {payload}"
    )
    assert adapter.schedule_review.await_count == 1, (
        f"pull_request.{action} must dispatch review work; schedule_review was "
        f"awaited {adapter.schedule_review.await_count}x"
    )
    pr_rows = [r for r in await _review_rows(fake_audit_db) if r.pr_number == 42]
    assert len(pr_rows) == 1, f"expected one PR-scoped review row, got {pr_rows}"


# ---------------------------------------------------------------------------
# A6 — a suppressed review is reported as a completed review with no output.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a6_suppressed_review_is_distinguishable_from_a_published_one(
    audit_store: FakeStore,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
    monkeypatch: pytest.MonkeyPatch,
):
    """Nothing was published, so nothing may claim to have been published.

    review_orchestrator sets status="completed" and returns when
    repo_settings.enable_pr_comments is False — without touching GitHub and
    without setting failure_reason. The persisted outcome is then byte-identical
    to a review that really did post a COMMENT/REQUEST_CHANGES, so the dashboard
    and any consumer of /reviews cannot tell the two apart.

    Intended: the outcome of the suppressed run differs from the outcome of a
    genuinely published review.
    """
    _wire_review_orchestrator(audit_store, monkeypatch)

    repo_on = await _seed_repo(
        fake_audit_db, fake_audit_user_factory, name="comments-enabled"
    )
    repo_off = await _seed_repo(
        fake_audit_db, fake_audit_user_factory, name="comments-disabled"
    )
    off_settings = RepoSettings(repo_id=repo_off.id, enable_pr_comments=False)
    fake_audit_db.add(off_settings)
    published = _seed_review(
        fake_audit_db, repo_on, pr_number=42, sha="a" * 40
    )
    suppressed = _seed_review(
        fake_audit_db, repo_off, pr_number=43, sha="b" * 40
    )
    await fake_audit_db.commit()
    await fake_audit_db.refresh(published)
    await fake_audit_db.refresh(suppressed)
    published_id, suppressed_id = published.id, suppressed.id

    with (
        patch(
            "app.services.review_orchestrator.get_installation_token",
            new_callable=AsyncMock,
            return_value="mock-token",
        ),
        patch(
            "app.services.review_orchestrator.fetch_pull_request_diff",
            new_callable=AsyncMock,
            return_value="diff --git a/x.py b/x.py\n+x = 1",
        ),
        patch("app.subagents.code_reviewer.LLMClient") as llm_cls,
        patch(
            "app.services.review_orchestrator.create_pull_request_review",
            new_callable=AsyncMock,
            return_value={"id": 1},
        ) as publish,
    ):
        llm_cls.return_value.complete = AsyncMock(return_value=_LLM_RESPONSE)

        await run_code_review_pipeline(published_id)
        published_outcome = _outcome(await _reload_review(fake_audit_db, published_id))

        await run_code_review_pipeline(suppressed_id)
        suppressed_outcome = _outcome(
            await _reload_review(fake_audit_db, suppressed_id)
        )

    # Baseline: this run really did reach GitHub, so its outcome is the "success"
    # the suppressed run must not be mistaken for.
    assert publish.await_count == 1
    assert published_outcome == ("completed", None), (
        f"baseline publish run produced an unexpected outcome: {published_outcome}"
    )
    assert suppressed_outcome != published_outcome, (
        "a review suppressed by enable_pr_comments=False produced zero GitHub "
        f"output but is indistinguishable from a published one: {suppressed_outcome}"
    )

