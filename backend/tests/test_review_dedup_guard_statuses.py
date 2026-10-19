"""
Reproduction for the two ``app/webhooks.py`` defects found in the adversarial
audit of the PR-review pipeline (PR #48).

===  =========================================================================
D1  ``REJECT #1`` — ``"suppressed"`` was outside BOTH dedup guards.
    ``review_orchestrator.py:775`` writes ``REVIEW_STATUS_SUPPRESSED`` when
    ``RepoSettings.enable_pr_comments`` is False (``live_studio_only`` ships it
    that way), but the guards matched only
    ``["pending", "in_progress", "completed"]``. So a GitHub redelivery of the
    same ``pull_request``/``push`` event — or an owner hitting
    ``POST /webhooks/deliveries/{id}/replay`` — found no matching row, minted a
    NEW ``CodeReview``, and re-ran the whole pipeline: diff fetch plus a PAID
    ``analyze_diff`` call, then suppressed again. Unbounded. Before this PR the
    same path wrote ``"completed"``, which the guard counted as handled, so the
    redelivery was a no-op.

D2  ``REJECT #2`` — ``except HTTPException: raise`` preceded the terminal write
    in ``_dispatch_review``, so an ``HTTPException`` from the hosting adapter
    re-raised BEFORE ``review.status = "error"``. The row stayed ``pending``,
    which the guard counts as HANDLED, so every later delivery answered
    ``duplicate``: the review was lost permanently with no error anywhere.
===  =========================================================================

Plus the drift guards that keep D1 from coming back: one shared vocabulary for
"already handled", used by both guard sites, with ``error`` deliberately outside
it.

Harness: the real ``POST /webhooks/github`` route over the ``client`` fixture and
the in-process ``tests/fake_audit_db.py`` store (real ``app.models`` rows behind
the real SQLAlchemy statements), real HMAC over the raw body, and the same patch
targets ``tests/test_review_webhook_lifecycle.py`` uses. The hosting adapter is
mocked only in the sense of a seam: ``schedule_review`` is handed to the REAL
``run_code_review_pipeline`` so the row reaches the REAL terminal status
production code writes for it, and ``LLMClient.complete`` is counted so "no
second paid call" is asserted rather than assumed.

D1's tests assert the LLM call count explicitly: the unbounded cost loop is the
defect, so the observable that matters is how many times the model was paid for.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import BackgroundTasks, HTTPException, status
from sqlalchemy import select

from app.config import settings
from app.models import CodeReview, Repo, RepoSettings, WebhookDelivery
from app.services import review_orchestrator
from app.services.review_orchestrator import (
    REVIEW_STATUS_COMPLETED,
    REVIEW_STATUS_ERROR,
    REVIEW_STATUS_SUPPRESSED,
    run_code_review_pipeline,
)
from tests.fake_audit_db import (  # noqa: F401  (pytest fixtures)
    FakeAsyncSession,
    FakeStore,
    FakeSessionMaker,
    audit_store,
    fake_audit_db,
    fake_audit_user_factory,
)

TEST_SECRET = settings.github_webhook_secret or "test_webhook_secret_key_12345"

OWNER = "dedup-guard-org"
REPO_NAME = "dedup-guard-repo"

PR_NUMBER = 42
COMMIT_SHA = "0123456789abcdef0123456789abcdef01234567"
BASE_SHA = "f" * 40

_LLM_RESPONSE = {
    "content": json.dumps(
        {"risk_score": 10, "summary": "Clean change, no issues found.", "findings": []}
    ),
    "usage": {"input_tokens": 100, "output_tokens": 20},
}


# ---------------------------------------------------------------------------
# Payloads / signing
# ---------------------------------------------------------------------------


def _signed_headers(
    event: str, payload: dict[str, Any]
) -> tuple[dict[str, str], bytes]:
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


def _pr_payload(*, action: str = "opened") -> dict[str, Any]:
    return {
        "action": action,
        "number": PR_NUMBER,
        "pull_request": {
            "number": PR_NUMBER,
            "state": "open",
            "draft": False,
            "head": {"ref": "feature/dedup", "sha": COMMIT_SHA},
            "base": {"ref": "main", "sha": BASE_SHA},
            "user": {"login": "dev-user", "type": "User"},
        },
        "repository": _repository(),
        "sender": {"login": "dev-user", "type": "User"},
    }


def _push_payload() -> dict[str, Any]:
    return {
        "ref": "refs/heads/main",
        "deleted": False,
        "head_commit": {
            "id": COMMIT_SHA,
            "author": {"name": "Senior Dev", "email": "dev@example.com"},
        },
        "repository": _repository(),
        "sender": {"login": "dev-user", "type": "User"},
    }


# ---------------------------------------------------------------------------
# Fake-store helpers
# ---------------------------------------------------------------------------


async def _seed_repo(
    db: FakeAsyncSession,
    user_factory,
    *,
    enable_pr_comments: bool,
) -> Repo:
    user = await user_factory(
        github_id=int(uuid.uuid4().int % 1_000_000_000 + 700_000_000),
        username=f"dedup-guard-user-{uuid.uuid4().hex[:6]}",
    )
    repo = Repo(user_id=user.id, owner=OWNER, name=REPO_NAME, default_branch="main")
    db.add(repo)
    await db.commit()
    db.add(RepoSettings(repo_id=repo.id, enable_pr_comments=enable_pr_comments))
    await db.commit()
    await db.refresh(repo)
    return repo


def _hosting_adapter() -> MagicMock:
    adapter = MagicMock()
    adapter.schedule_review = AsyncMock()
    adapter.schedule_pipeline = AsyncMock()
    adapter.schedule_audit = AsyncMock()
    return adapter


def _inline_pipeline_runner(
    db: FakeAsyncSession, repo: Repo
) -> "Callable[..., Awaitable[None]]":
    """Build the adapter seam that runs the real review pipeline.

    ``_dispatch_review`` calls ``adapter.schedule_review(review.id,
    background_tasks)``, so the seam takes both arguments. Running the real
    ``run_code_review_pipeline`` — rather than hand-writing ``suppressed`` onto
    the row — is what makes these tests exercise the status production code
    actually persists.

    The ``review.repo`` relationship is attached first because
    ``tests/fake_audit_db.py`` models statements, not ORM relationships: it
    resolves ``selectinload(CodeReview.repo)`` to nothing, and the pipeline ends a
    review in ``error`` ("Repository record not found") when the attribute is
    missing. The webhook mints ``CodeReview(repo_id=repo.id, ...)`` — the id is
    what production queries on — so this reproduces the relationship a real
    flush would have populated.
    """

    async def _run(review_id: uuid.UUID, _background_tasks: BackgroundTasks) -> None:
        row = (
            (await db.execute(select(CodeReview).where(CodeReview.id == review_id)))
            .scalars()
            .first()
        )
        if row is not None and row.repo is None:
            row.repo = repo
        await run_code_review_pipeline(review_id)

    return _run


def _wire_review_orchestrator(
    store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point the orchestrator at the in-process store.

    ``review_orchestrator`` binds ``async_session_maker`` at import time, so the
    attribute ``fake_audit_db`` patches on ``app.db`` is not what it uses.
    """
    monkeypatch.setattr(
        review_orchestrator, "async_session_maker", FakeSessionMaker(store)
    )


@pytest.fixture
def suppressed_llm() -> Iterator[dict[str, int]]:
    """Stub every outbound call the pipeline makes, and count the LLM ones.

    Yields the counter so a test can assert how many times the model was paid
    for — the unbounded cost loop is the defect, so "how many LLM calls" is the
    observable that matters. ``enable_pr_comments=False`` is what makes the real
    pipeline take the ``REVIEW_STATUS_SUPPRESSED`` exit at
    review_orchestrator.py:775 rather than publishing.
    """
    llm: dict[str, int] = {"calls": 0}

    def _llm_factory(*_args: Any, **_kwargs: Any) -> MagicMock:
        client = MagicMock()

        async def _complete(*_a: Any, **_k: Any) -> dict[str, Any]:
            llm["calls"] += 1
            return _LLM_RESPONSE

        client.complete = _complete
        return client

    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "app.services.review_orchestrator.get_installation_token",
                new_callable=AsyncMock,
                return_value="mock-token",
            )
        )
        stack.enter_context(
            patch(
                "app.services.review_orchestrator.get_repo_settings",
                new_callable=AsyncMock,
                return_value=RepoSettings(enable_pr_comments=False),
            )
        )
        stack.enter_context(
            patch(
                "app.services.review_orchestrator.fetch_pull_request_diff",
                new_callable=AsyncMock,
                return_value="diff --git a/x.py b/x.py\n+x = 1",
            )
        )
        stack.enter_context(
            patch(
                "app.services.review_orchestrator.fetch_diff",
                new_callable=AsyncMock,
                return_value="diff --git a/x.py b/x.py\n+x = 1",
            )
        )
        stack.enter_context(
            patch(
                "app.services.review_orchestrator.create_pull_request_review",
                new_callable=AsyncMock,
                return_value={"id": 1},
            )
        )
        stack.enter_context(
            patch("app.subagents.code_reviewer.LLMClient", new=_llm_factory)
        )
        yield llm


async def _review_rows(db: FakeAsyncSession, repo: Repo) -> list[CodeReview]:
    result = await db.execute(select(CodeReview).where(CodeReview.repo_id == repo.id))
    return list(result.scalars().all())


async def _pr_scoped_rows(db: FakeAsyncSession, repo: Repo) -> list[CodeReview]:
    return [r for r in await _review_rows(db, repo) if r.pr_number == PR_NUMBER]


async def _push_scoped_rows(db: FakeAsyncSession, repo: Repo) -> list[CodeReview]:
    return [r for r in await _review_rows(db, repo) if r.pr_number is None]


# ---------------------------------------------------------------------------
# D1 — "suppressed" is outside both dedup guards (HIGH: unbounded cost loop)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redelivered_pull_request_after_a_suppressed_review_is_deduplicated(
    client: httpx.AsyncClient,
    audit_store: FakeStore,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
    monkeypatch: pytest.MonkeyPatch,
    suppressed_llm: dict[str, int],
):
    """A redelivered pull_request whose review ended "suppressed" must dedupe.

    ``live_studio_only`` ships ``enable_pr_comments=False``. The first delivery
    mints a CodeReview, the pipeline runs and ends ``suppressed`` (nothing
    reached GitHub), and then GitHub redelivers the same event. The guard did
    not list ``suppressed``, so it found no row, minted a SECOND CodeReview and
    re-ran the pipeline — a second diff fetch and a second paid ``analyze_diff``
    call, for a review that can never be published. The same repeats on every
    further redelivery.

    Intended: the redelivery is answered ``duplicate``, no second row is minted,
    and the model is not paid a second time.
    """
    _wire_review_orchestrator(audit_store, monkeypatch)
    repo = await _seed_repo(
        fake_audit_db, fake_audit_user_factory, enable_pr_comments=False
    )
    llm = suppressed_llm
    adapter = _hosting_adapter()
    adapter.schedule_review = AsyncMock(
        side_effect=_inline_pipeline_runner(fake_audit_db, repo)
    )

    with patch(
        "app.adapters.hosting.get_hosting_adapter",
        new_callable=AsyncMock,
        return_value=adapter,
    ):
        first_headers, first_body = _signed_headers("pull_request", _pr_payload())
        first = await client.post(
            "/webhooks/github", headers=first_headers, content=first_body
        )
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "queued", first.text

        rows = await _pr_scoped_rows(fake_audit_db, repo)
        assert len(rows) == 1, rows
        assert rows[0].status == REVIEW_STATUS_SUPPRESSED, (
            "the pipeline did not take the real suppressed exit, so this test "
            f"would not exercise the defect: status={rows[0].status!r} "
            f"reason={rows[0].failure_reason!r}"
        )
        assert llm["calls"] == 1, llm

        # GitHub redelivery: new X-GitHub-Delivery, byte-identical payload.
        redelivered_headers, redelivered_body = _signed_headers(
            "pull_request", _pr_payload()
        )
        redelivered = await client.post(
            "/webhooks/github",
            headers=redelivered_headers,
            content=redelivered_body,
        )

    assert redelivered.status_code == 200, redelivered.text
    body = redelivered.json()
    assert body["status"] == "duplicate", (
        "a review that ended 'suppressed' is outside the dedup guard, so the "
        f"redelivery re-minted and re-ran it: {body}"
    )

    rows_after = await _pr_scoped_rows(fake_audit_db, repo)
    assert len(rows_after) == 1, (
        f"the redelivery minted a second PR-scoped review row: {rows_after}"
    )
    assert llm["calls"] == 1, (
        "the redelivery paid for another LLM review call — this is the "
        f"unbounded cost loop: {llm}"
    )
    assert adapter.schedule_review.await_count == 1, (
        "the redelivery re-dispatched the review; schedule_review was awaited "
        f"{adapter.schedule_review.await_count}x"
    )


@pytest.mark.asyncio
async def test_redelivered_push_after_a_suppressed_review_is_deduplicated(
    client: httpx.AsyncClient,
    audit_store: FakeStore,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
    monkeypatch: pytest.MonkeyPatch,
    suppressed_llm: dict[str, int],
):
    """The push guard is a second copy of the same list, so it drifted too.

    ``webhooks.github_webhook``'s ``push`` branch carries its own dedup guard with
    the same hand-maintained status list. Fixing only the ``pull_request`` guard
    would leave the commit-scoped review re-running the whole pipeline on every
    redelivery.

    Intended: the redelivered push is answered ``duplicate``, mints no second
    commit-scoped row, and costs no second LLM call.
    """
    _wire_review_orchestrator(audit_store, monkeypatch)
    repo = await _seed_repo(
        fake_audit_db, fake_audit_user_factory, enable_pr_comments=False
    )
    llm = suppressed_llm
    adapter = _hosting_adapter()
    adapter.schedule_review = AsyncMock(
        side_effect=_inline_pipeline_runner(fake_audit_db, repo)
    )

    with patch(
        "app.adapters.hosting.get_hosting_adapter",
        new_callable=AsyncMock,
        return_value=adapter,
    ):
        first_headers, first_body = _signed_headers("push", _push_payload())
        first = await client.post(
            "/webhooks/github", headers=first_headers, content=first_body
        )
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "queued", first.text

        rows = await _push_scoped_rows(fake_audit_db, repo)
        assert len(rows) == 1, rows
        assert rows[0].status == REVIEW_STATUS_SUPPRESSED, (
            "the pipeline did not take the real suppressed exit: "
            f"status={rows[0].status!r} reason={rows[0].failure_reason!r}"
        )
        assert llm["calls"] == 1, llm

        redelivered_headers, redelivered_body = _signed_headers("push", _push_payload())
        redelivered = await client.post(
            "/webhooks/github",
            headers=redelivered_headers,
            content=redelivered_body,
        )

    assert redelivered.status_code == 200, redelivered.text
    body = redelivered.json()
    assert body["status"] == "duplicate", (
        f"the push dedup guard does not list 'suppressed': {body}"
    )

    rows_after = await _push_scoped_rows(fake_audit_db, repo)
    assert len(rows_after) == 1, (
        f"the redelivered push minted a second commit-scoped review row: {rows_after}"
    )
    assert llm["calls"] == 1, f"the redelivered push paid for another LLM call: {llm}"
    assert adapter.schedule_review.await_count == 1, (
        f"the redelivered push re-dispatched: {adapter.schedule_review.await_count}x"
    )


@pytest.mark.asyncio
async def test_a_review_that_ended_in_error_is_still_re_drivable(
    client: httpx.AsyncClient,
    audit_store: FakeStore,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
    monkeypatch: pytest.MonkeyPatch,
    suppressed_llm: dict[str, int],
):
    """Regression guard: "error" must stay OUTSIDE the handled-status set.

    Adding ``suppressed`` to the guard must not accidentally sweep ``error`` in
    with it. A failed review has to remain re-drivable: it is the one outcome
    whose correct response to a redelivery is "try again", and ``error`` is
    exactly why the guard's status list is not simply "every terminal status"
    (``review_orchestrator`` also uses ``error`` for the stale-recovery sweep).

    Intended: a review that failed dispatch leaves ``error`` behind, and the next
    delivery of the same event queues and dispatches a fresh review.
    """
    _wire_review_orchestrator(audit_store, monkeypatch)
    repo = await _seed_repo(
        fake_audit_db, fake_audit_user_factory, enable_pr_comments=False
    )
    adapter = _hosting_adapter()
    adapter.schedule_review = AsyncMock(
        side_effect=[
            RuntimeError("Lambda async invocation was not accepted"),
            _inline_pipeline_runner(fake_audit_db, repo),
        ]
    )

    with patch(
        "app.adapters.hosting.get_hosting_adapter",
        new_callable=AsyncMock,
        return_value=adapter,
    ):
        first_headers, first_body = _signed_headers("pull_request", _pr_payload())
        try:
            await client.post(
                "/webhooks/github", headers=first_headers, content=first_body
            )
        except Exception:  # noqa: BLE001 — the escaping 503 IS the contract here
            pass

        rows = await _pr_scoped_rows(fake_audit_db, repo)
        assert len(rows) == 1, rows
        assert rows[0].status == REVIEW_STATUS_ERROR, (
            "the failed dispatch did not record a terminal error state: "
            f"status={rows[0].status!r} reason={rows[0].failure_reason!r}"
        )

        retry_headers, retry_body = _signed_headers("pull_request", _pr_payload())
        retried = await client.post(
            "/webhooks/github", headers=retry_headers, content=retry_body
        )

    assert retried.status_code == 200, retried.text
    body = retried.json()
    assert body["status"] == "queued", (
        f"a review that ended in 'error' was swallowed as handled and can never "
        f"be retried: {body}"
    )
    assert adapter.schedule_review.await_count == 2, (
        "the retry did not reach schedule_review; it was awaited "
        f"{adapter.schedule_review.await_count}x"
    )
    assert len(await _pr_scoped_rows(fake_audit_db, repo)) == 2


def test_both_dedup_guards_share_one_handled_status_vocabulary():
    """The list must be defined once, not copied into two guard queries.

    Two hand-copied lists are the whole reason this defect existed: the same
    change that added ``REVIEW_STATUS_SUPPRESSED`` to the pipeline updated
    neither guard, and the drift was invisible because both lists looked right.
    A third terminal status added later must fail here, not in production.

    Asserted at the source level because the two guards are two separate
    ``select()`` statements inside one 1900-line handler; there is no single
    call site to unit-test.
    """
    import app.webhooks as webhooks_module

    source = inspect.getsource(webhooks_module)

    assert (
        'CodeReview.status.in_(["pending", "in_progress", "completed"])' not in source
    ), (
        "a hand-copied handled-status list is back in a dedup guard; both sites "
        "must read the single shared _HANDLED_REVIEW_STATUSES definition"
    )
    assert source.count("CodeReview.status.in_(_HANDLED_REVIEW_STATUSES)") == 2, (
        "expected exactly two dedup guards (pull_request and push) to read the "
        "shared vocabulary"
    )


def test_handled_status_vocabulary_covers_every_terminal_status_but_error():
    """``_HANDLED_REVIEW_STATUSES`` must track the pipeline's own vocabulary.

    Derived from ``review_orchestrator._TERMINAL_REVIEW_STATUSES`` — the machine-
    readable statement of "a CodeReview that will never transition again" — so a
    terminal status added there later is a failing test here rather than a
    production cost loop. Reaching into the private name is deliberate and is the
    only way to assert the invariant; the pipeline's terminal set and the
    webhook's handled set have to agree, and nothing else states that.

    ``error`` is the one member deliberately excluded, and the exclusion is
    asserted from the other direction too, so a well-meaning "just use the
    terminal set" cannot land.
    """
    from app.services.review_orchestrator import _TERMINAL_REVIEW_STATUSES
    from app.webhooks import _HANDLED_REVIEW_STATUSES

    unhandled_terminals = (
        _TERMINAL_REVIEW_STATUSES
        - {REVIEW_STATUS_ERROR}
        - set(_HANDLED_REVIEW_STATUSES)
    )
    assert unhandled_terminals == set(), (
        "these terminal statuses are written by review_orchestrator but are "
        "outside the webhook dedup guard, so a redelivery of the delivery that "
        "produced one will mint a second review and re-run the pipeline: "
        f"{sorted(unhandled_terminals)}"
    )
    assert REVIEW_STATUS_ERROR in _TERMINAL_REVIEW_STATUSES, (
        "review_orchestrator no longer treats 'error' as terminal; this test's "
        "exclusion below needs revisiting"
    )
    assert REVIEW_STATUS_ERROR not in _HANDLED_REVIEW_STATUSES, (
        "'error' is the one terminal status that must stay re-drivable"
    )
    assert set(_HANDLED_REVIEW_STATUSES) >= {
        REVIEW_STATUS_SUPPRESSED,
        REVIEW_STATUS_COMPLETED,
        "pending",
        "in_progress",
    }


# ---------------------------------------------------------------------------
# Replay-path proof — the guard must hold under the replay ContextVars
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_of_a_suppressed_review_delivery_is_deduplicated(
    client: httpx.AsyncClient,
    audit_store: FakeStore,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
    monkeypatch: pytest.MonkeyPatch,
    suppressed_llm: dict[str, int],
):
    """``POST /webhooks/deliveries/{id}/replay`` is the other way in.

    ``replay_webhook_delivery`` re-enters ``github_webhook`` on the stored bytes
    under ``_replay_of_var`` / ``_replay_repo_id_var``, so an owner could replay a
    suppressed review delivery as many times as the cooldown allowed, paying for
    a fresh ``analyze_diff`` each time. Driven exactly the way the route drives
    it — same re-signing, same ContextVars — because the route's tenant scoping is
    a ``repos.user_id`` subquery the in-process store deliberately refuses to
    model.

    Intended: the replay is answered ``duplicate``, mints no second row, pays no
    second LLM call, and appends exactly one row stamped ``replay_of=<original>``
    and scoped to the pinned repo id (so the route's cooldown anchor exists and
    the replay is bounded).
    """
    from app.webhooks import (
        _replay_of_var,
        _replay_repo_id_var,
        _synthetic_github_request,
        github_webhook,
    )

    _wire_review_orchestrator(audit_store, monkeypatch)
    repo = await _seed_repo(
        fake_audit_db, fake_audit_user_factory, enable_pr_comments=False
    )
    llm = suppressed_llm
    adapter = _hosting_adapter()
    adapter.schedule_review = AsyncMock(
        side_effect=_inline_pipeline_runner(fake_audit_db, repo)
    )
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
    assert live.json()["status"] == "queued", live.text

    rows = await _pr_scoped_rows(fake_audit_db, repo)
    assert len(rows) == 1, rows
    assert rows[0].status == REVIEW_STATUS_SUPPRESSED, rows[0].status
    assert llm["calls"] == 1, llm

    originals = list(
        (
            await fake_audit_db.scalars(
                select(WebhookDelivery)
                .where(WebhookDelivery.repo_id == repo.id)
                .order_by(WebhookDelivery.created_at)
            )
        ).all()
    )
    assert len(originals) == 1, originals
    original = originals[0]
    assert original.replay_of is None

    signature = (
        "sha256="
        + hmac.new(TEST_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    )
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

    assert decision["status"] == "duplicate", (
        f"replaying a suppressed review re-ran the pipeline: {decision}"
    )
    assert len(await _pr_scoped_rows(fake_audit_db, repo)) == 1, (
        "the replay minted a second review row"
    )
    assert llm["calls"] == 1, f"the replay paid for another LLM call: {llm}"

    replay_rows = list(
        (
            await fake_audit_db.scalars(
                select(WebhookDelivery).where(WebhookDelivery.replay_of == original.id)
            )
        ).all()
    )
    assert len(replay_rows) == 1, replay_rows
    assert replay_rows[0].repo_id == repo.id, (
        "the replayed row was not scoped to the pinned repo id, so the route's "
        "replay-row lookup could not find it"
    )
    assert replay_rows[0].status == "duplicate", replay_rows[0].status


# ---------------------------------------------------------------------------
# D2 — `except HTTPException: raise` precedes the terminal-state write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_exception_from_the_hosting_adapter_records_a_terminal_state(
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    """An HTTPException must not bypass the terminal write in _dispatch_review.

    ``_dispatch_review`` re-raised ``HTTPException`` bare *before* it set
    ``review.status = "error"``. The CodeReview row is committed before dispatch,
    so the row stayed ``pending`` — a status the dedup guard counts as HANDLED.
    Every later delivery of that event was then answered ``duplicate``: the
    review was lost permanently, with a green 5xx as the only trace and nothing
    on the row to say it was never dispatched.

    Currently unreachable — nothing in ``app/adapters/hosting.py`` raises
    HTTPException today — which is exactly what makes it dangerous: it is the
    stuck state the function was written to eliminate, one ``except`` clause away.

    Intended: the row ends in a terminal state carrying a bounded reason, and a
    later delivery of the same event can still drive the review.
    """
    repo = await _seed_repo(
        fake_audit_db, fake_audit_user_factory, enable_pr_comments=True
    )
    adapter = _hosting_adapter()
    adapter.schedule_review = AsyncMock(
        side_effect=HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Lambda self-invocation is already in flight",
        )
    )

    with patch(
        "app.adapters.hosting.get_hosting_adapter",
        new_callable=AsyncMock,
        return_value=adapter,
    ):
        headers, body = _signed_headers("pull_request", _pr_payload())
        try:
            await client.post("/webhooks/github", headers=headers, content=body)
        except Exception:  # noqa: BLE001 — the escaping 503 is expected
            pass

        rows = await _pr_scoped_rows(fake_audit_db, repo)
        assert len(rows) == 1, rows
        review = rows[0]
        assert review.status == REVIEW_STATUS_ERROR, (
            "an HTTPException from the hosting adapter re-raised before the "
            "terminal write, so the review is left in a status the dedup guard "
            f"counts as handled: status={review.status!r}"
        )
        assert review.failure_reason, (
            "the dispatch failure left no durable trace on the review row"
        )
        # Normalising the response to the module's own 503 must not cost the
        # adapter's diagnosis: it is persisted on the row instead.
        assert "Lambda self-invocation is already in flight" in (
            review.failure_reason or ""
        ), review.failure_reason

        # The consequence that matters: it must still be re-drivable.
        adapter.schedule_review = AsyncMock()
        retry_headers, retry_body = _signed_headers("pull_request", _pr_payload())
        retried = await client.post(
            "/webhooks/github", headers=retry_headers, content=retry_body
        )

    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "queued", (
        "the review is permanently lost: a delivery that never dispatched left "
        f"the row handled, so the retry is swallowed: {retried.text}"
    )
