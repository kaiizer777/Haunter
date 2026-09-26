"""
Phase 5.1 — Webhook Routing & Trigger Filter Engine tests
(future02.md §1.5, `test_audit_webhook_routing.py`).

Covers:
  1. HMAC valid signature accepted (PR opened -> queued).
  2. HMAC invalid / missing signature rejected (401, before any processing).
  3. Raw-bytes `hmac.compare_digest` semantics (whitespace-invariant,
     single-byte tamper rejected, `compare_digest` actually invoked).
  4. pull_request opened / synchronize dispatch audit; unsupported action ignored.
  5. workflow_run.completed failure dispatches; success ignored by default
     and queued when `on_ci_success` is enabled.
  6. issue_comment.created `@haunter audit` dispatches (branch-agnostic,
     no parent Run required).
  7. Disabled-feature early exit (no audit scheduled, fast, no mutation).

All GitHub/LLM side effects are mocked. No secrets are logged.

Database strategy
-----------------
Most of this module runs fully hermetic against the in-process store from
``tests/fake_audit_db.py``: the webhook endpoint, ``get_auditor_trigger``,
``claim_audit_delivery`` and the whole durable-outbox state machine
(``claim_next_queued_job`` → ``_claim_processing_job`` →
``_complete_audit_job`` / ``_retry_or_fail_audit_job`` → ``release_dispatch_claim``
→ ``recover_expired_audit_leases``) execute the real statements against real ORM
rows. No network, no ``TEST_DATABASE_URL``, no 30-second truncate round trips.

Exactly six tests are marked ``@pytest.mark.db`` and still require PostgreSQL,
because what they assert is enforced by the database rather than by Python:
the ``uq_audit_jobs_delivery_id`` race, ``FOR UPDATE SKIP LOCKED``, the migrated
``audit_jobs`` CHECK constraints, and the ``repo_settings`` row/server-default
round trip. Run them with ``pytest -m db``; ``pytest -m "not db"`` is the fast
hermetic suite.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Union, cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.github_client import (
    GitHubAuthError,
    GitHubNetworkError,
    GitHubRateLimitError,
    GitHubResourceNotFoundError,
)
from app.models import AuditJob, Repo, RepoSettings
from app.services import audit_pipeline
from app.services.audit_pipeline import AuditorTriggerSettings
from tests.conftest import async_session_maker, truncate_all

# `audit_store`, `fake_audit_db` and `fake_audit_user_factory` are pytest
# fixtures; importing them here is what registers them for this module.
from tests.fake_audit_db import (  # noqa: F401
    FakeAsyncSession,
    UnsupportedStatementError,
    audit_store,
    fake_audit_db,
    fake_audit_user_factory,
)

#: Either a real PostgreSQL `AsyncSession` (the `@pytest.mark.db` tests) or the
#: in-process `FakeAsyncSession`. Both satisfy the small surface used below.
AuditSession = Union[AsyncSession, FakeAsyncSession]


def as_session(fake_audit_db: AuditSession) -> AsyncSession:
    """Hand the in-process store to code that is typed against `AsyncSession`.

    The fake implements exactly the `add`/`execute`/`scalar`/`scalars`/
    `commit`/`rollback`/`refresh`/`expire_all` surface the audit pipeline and
    the webhook router use, and raises `UnsupportedStatementError` for anything
    else, so it is a faithful stand-in rather than a loose mock.
    """
    return cast(AsyncSession, fake_audit_db)


TEST_SECRET = settings.github_webhook_secret or "test_webhook_secret_key_12345"
TEST_SELF_INVOKE_SECRET = "routing-test-self-invoke-secret"
ENABLED_TRIGGER = AuditorTriggerSettings(
    enabled=True,
    on_pr=True,
    on_ci_failure=True,
    on_ci_success=True,
    on_manual_mention=True,
)


@pytest.fixture(autouse=True)
def self_invoke_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[None, None, None]:
    """The dispatcher signs a per-attempt fence with this secret.

    Without it, `claim_next_queued_job` fails closed by design, so every test in
    this module that reaches the outbox needs it configured.
    """
    monkeypatch.setattr(
        settings, "audit_self_invoke_secret", TEST_SELF_INVOKE_SECRET
    )
    yield


@pytest.fixture(autouse=True)
def deny_external_http(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[None, None, None]:
    original_send = httpx.AsyncClient.send

    async def guarded_send(self, request, **kwargs):
        if isinstance(self._transport, httpx.ASGITransport):
            return await original_send(self, request, **kwargs)
        raise AssertionError(f"external HTTP blocked in routing tests: {request.url.host}")

    monkeypatch.setattr(httpx.AsyncClient, "send", guarded_send)
    yield


def sign_payload(secret: str, raw_body: bytes) -> str:
    sig = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def make_pr_payload(
    owner: str,
    repo: str,
    action: str = "opened",
    pr_number: int = 42,
    head_ref: str = "feature/audit-test",
    head_sha: str = "1111222233334444555566667777888899990000",
) -> dict:
    return {
        "action": action,
        "number": pr_number,
        "pull_request": {
            "number": pr_number,
            "state": "open",
            "draft": False,
            "head": {"ref": head_ref, "sha": head_sha},
            "base": {"ref": "main", "sha": "9999888877776666555544443333222211110000"},
            "user": {"login": "dev-user", "type": "User"},
        },
        "repository": {"name": repo, "owner": {"login": owner}},
        "sender": {"login": "dev-user", "type": "User"},
    }


def make_workflow_payload(
    owner: str = "audit-org",
    repo: str = "audit-repo",
    action: str = "completed",
    conclusion: str | None = "failure",
    run_id: int = 5001001,
    head_sha: str = "0123456789abcdef0123456789abcdef01234567",
    head_branch: str = "main",
) -> dict:
    return {
        "action": action,
        "workflow_run": {
            "id": run_id,
            "head_sha": head_sha,
            "head_branch": head_branch,
            "conclusion": conclusion,
            "html_url": f"https://github.com/{owner}/{repo}/actions/runs/{run_id}",
        },
        "repository": {
            "name": repo,
            "full_name": f"{owner}/{repo}",
            "owner": {"login": owner},
        },
    }


def make_issue_comment_payload(
    owner: str = "audit-org",
    repo: str = "audit-repo",
    comment_body: str = "@haunter audit this PR for security issues",
    author_association: str = "MEMBER",
    pr_number: int = 77,
    comment_id: int = 9101001,
) -> dict:
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
            "body": comment_body,
            "author_association": author_association,
            "user": {"login": "senior-reviewer"},
        },
        "repository": {
            "name": repo,
            "full_name": f"{owner}/{repo}",
            "owner": {"login": owner},
        },
    }


def make_review_comment_payload(
    owner: str = "audit-org",
    repo: str = "audit-repo",
    comment_body: str = "@haunter audit this PR for security issues",
    author_association: str = "MEMBER",
    pr_number: int = 88,
    comment_id: int = 9202001,
    base_sha: str | None = "a" * 40,
    head_sha: str | None = "b" * 40,
) -> dict:
    """pull_request_review_comment carries PR endpoints, unlike issue_comment."""
    payload: dict = {
        "action": "created",
        "comment": {
            "id": comment_id,
            "body": comment_body,
            "author_association": author_association,
            "user": {"login": "senior-reviewer"},
        },
        "repository": {
            "name": repo,
            "full_name": f"{owner}/{repo}",
            "owner": {"login": owner},
        },
    }
    if base_sha is None:
        payload["pull_request"] = {
            "number": pr_number,
            "head": {"ref": "feature/rc", "sha": head_sha},
            "base": None,
        }
    else:
        payload["pull_request"] = {
            "number": pr_number,
            "head": {"ref": "feature/rc", "sha": head_sha},
            "base": {"ref": "main", "sha": base_sha},
        }
    return payload


async def seed_repo(
    fake_audit_db: AuditSession,
    fake_audit_user_factory,
    owner: str,
    name: str,
) -> Repo:
    user = await fake_audit_user_factory(
        github_id=int(uuid.uuid4().int % 1_000_000_000 + 200_000_000),
        username=f"audit-user-{uuid.uuid4().hex[:6]}",
    )
    repo = Repo(user_id=user.id, owner=owner, name=name)
    fake_audit_db.add(repo)
    await fake_audit_db.commit()
    await fake_audit_db.refresh(repo)
    return repo


async def set_repo_auditor_settings(
    fake_audit_db: AuditSession,
    repo: Repo,
    *,
    enabled: bool = True,
    on_pr: bool = True,
    on_ci_failure: bool = True,
    on_ci_success: bool = False,
    on_manual_mention: bool = True,
) -> RepoSettings:
    row = RepoSettings(
        repo_id=repo.id,
        enable_auditor_mode=enabled,
        audit_trigger_on_pr=on_pr,
        audit_trigger_on_ci_failure=on_ci_failure,
        audit_trigger_on_ci_success=on_ci_success,
        audit_trigger_on_manual_mention=on_manual_mention,
    )
    fake_audit_db.add(row)
    await fake_audit_db.commit()
    await fake_audit_db.refresh(row)
    return row


async def persist_audit_job(
    fake_audit_db: AuditSession,
    repo: Repo,
    delivery_id: str,
) -> str:
    claim = await audit_pipeline.claim_audit_delivery(
        db=as_session(fake_audit_db),
        repo_id=repo.id,
        audit_type="ci_failure_audit",
        repo_full_name=f"{repo.owner}/{repo.name}",
        delivery_id=delivery_id,
        head_sha="c" * 40,
        workflow_run_id=7001,
    )
    assert claim.claimed is True
    return claim.audit_id


async def post_signed(
    client: httpx.AsyncClient,
    event: str,
    payload: dict,
    raw_body: bytes | None = None,
) -> httpx.Response:
    body = raw_body if raw_body is not None else json.dumps(payload).encode("utf-8")
    return await client.post(
        "/webhooks/github",
        headers={
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": sign_payload(TEST_SECRET, body),
        },
        content=body,
    )


# ---------------------------------------------------------------------------
# 1-2. HMAC valid / invalid / missing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_hmac_missing_signature_rejected(client: httpx.AsyncClient):
    resp = await client.post(
        "/webhooks/github",
        headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": str(uuid.uuid4())},
        json={"action": "opened"},
    )
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Invalid or missing signature"}


@pytest.mark.asyncio
async def test_audit_hmac_invalid_signature_rejected(client: httpx.AsyncClient):
    raw_body = json.dumps(make_pr_payload("o", "r")).encode("utf-8")
    resp = await client.post(
        "/webhooks/github",
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": "sha256=" + "0" * 64,
        },
        content=raw_body,
    )
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Invalid or missing signature"}


@pytest.mark.asyncio
async def test_audit_hmac_malformed_prefix_rejected(client: httpx.AsyncClient):
    raw_body = json.dumps({"action": "opened"}).encode("utf-8")
    resp = await client.post(
        "/webhooks/github",
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": "md5=deadbeef",
        },
        content=raw_body,
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 3. Raw-bytes compare_digest semantics
# ---------------------------------------------------------------------------


def test_audit_verify_signature_uses_compare_digest():
    body = json.dumps({"action": "opened"}).encode("utf-8")
    good = sign_payload(TEST_SECRET, body)
    with patch("hmac.compare_digest", wraps=hmac.compare_digest) as spy:
        assert audit_pipeline.verify_github_signature(body, good, TEST_SECRET) is True
        assert spy.called
    # Single-byte tamper must fail even though JSON semantics are identical.
    tampered = bytearray(body)
    tampered[0] ^= 0x01
    assert audit_pipeline.verify_github_signature(bytes(tampered), good, TEST_SECRET) is False
    # Malformed / missing inputs never raise — they return False (→ 401).
    assert audit_pipeline.verify_github_signature(body, None, TEST_SECRET) is False
    assert audit_pipeline.verify_github_signature(body, "no-prefix", TEST_SECRET) is False
    assert audit_pipeline.verify_github_signature(body, good, None) is False
    assert audit_pipeline.verify_github_signature("not-bytes", good, TEST_SECRET) is False  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "signature",
    [
        "sha256=" + "é" * 64,
        "sha256=" + "0" * 63,
        "sha256=" + "0" * 65,
        "sha256=" + "g" * 64,
        "sha256= sha256=" + "0" * 64,
    ],
)
def test_audit_signature_rejects_malformed_unicode_length_and_non_hex(signature: str):
    with patch("hmac.compare_digest", wraps=hmac.compare_digest) as spy:
        assert audit_pipeline.verify_github_signature(b"{}", signature, TEST_SECRET) is False
    spy.assert_not_called()


@pytest.mark.asyncio
async def test_audit_raw_body_whitespace_invariant(
    client: httpx.AsyncClient, fake_audit_db: FakeAsyncSession, fake_audit_user_factory
):
    """Signature is computed over raw bytes: indented payload still verifies."""
    await seed_repo(fake_audit_db, fake_audit_user_factory, "ws-audit-org", "ws-audit-repo")
    payload = make_pr_payload(
        "ws-audit-org",
        "ws-audit-repo",
        action="opened",
        pr_number=61,
        head_sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    raw_body = json.dumps(payload, indent=4).encode("utf-8") + b"  \n"
    sig = sign_payload(TEST_SECRET, raw_body)
    with patch(
        "app.adapters.hosting.AWSHostingAdapter.schedule_review",
        new_callable=AsyncMock,
    ):
        resp = await client.post(
            "/webhooks/github",
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": str(uuid.uuid4()),
                "X-Hub-Signature-256": sig,
                "Content-Type": "application/json",
            },
            content=raw_body,
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"


# ---------------------------------------------------------------------------
# 3b. Body limit enforced before buffering (Phase 5.2 fix 4)
# ---------------------------------------------------------------------------

#: Frames the oversized body the way a real chunked upload arrives. The gap
#: between the limit and this chunk size is what makes "aborted early" a
#: measurable claim rather than an assumption.
WEBHOOK_STREAM_CHUNK_BYTES = 64 * 1024


class CountingChunkedBody:
    """Chunked request body that records how much of it was actually produced.

    httpx frames an async iterator as a chunked body with no Content-Length
    header, so the cheap header pre-check cannot be what rejects the request.
    """

    def __init__(self, payload: bytes, chunk_size: int = WEBHOOK_STREAM_CHUNK_BYTES) -> None:
        self._payload = payload
        self._chunk_size = chunk_size
        self.bytes_produced = 0

    async def _iterate(self):
        for offset in range(0, len(self._payload), self._chunk_size):
            chunk = self._payload[offset : offset + self._chunk_size]
            self.bytes_produced += len(chunk)
            yield chunk

    def __aiter__(self):
        return self._iterate()


@pytest.mark.asyncio
async def test_chunked_oversized_webhook_body_is_rejected_before_hmac(
    client: httpx.AsyncClient, fake_audit_db: FakeAsyncSession
):
    """No Content-Length: the byte cap must stop the stream, not buffer it.

    `await request.body()` would allocate the whole body first and only then
    check the size, so the assertion is on where the read stopped.
    """
    from app import webhooks

    limit = webhooks.MAX_PAYLOAD_SIZE_BYTES
    oversized = b"A" * (limit + (WEBHOOK_STREAM_CHUNK_BYTES * 2))
    body = CountingChunkedBody(oversized)

    resp = await client.post(
        "/webhooks/github",
        headers={
            "X-GitHub-Event": "workflow_run",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            # Correctly signed: the cap has to reject this before the signature
            # is ever consulted, and not because the signature was wrong.
            "X-Hub-Signature-256": sign_payload(TEST_SECRET, oversized),
        },
        content=body,
    )

    assert resp.status_code == 413
    assert resp.json() == {"detail": "Payload size exceeds 2MB limit"}
    # Abandoned at the cap plus at most one chunk, instead of draining it all.
    assert body.bytes_produced <= limit + WEBHOOK_STREAM_CHUNK_BYTES
    assert body.bytes_produced < len(oversized)


@pytest.mark.asyncio
async def test_chunked_oversized_webhook_body_at_the_exact_boundary(
    client: httpx.AsyncClient, fake_audit_db: FakeAsyncSession
):
    """The cap is exact: limit bytes are read, limit + 1 is refused."""
    from app import webhooks

    limit = webhooks.MAX_PAYLOAD_SIZE_BYTES

    async def post(payload: bytes) -> httpx.Response:
        return await client.post(
            "/webhooks/github",
            headers={
                "X-GitHub-Event": "ping",
                "X-GitHub-Delivery": str(uuid.uuid4()),
                "X-Hub-Signature-256": sign_payload(TEST_SECRET, payload),
            },
            content=CountingChunkedBody(payload),
        )

    at_limit = await post(b"a" * limit)
    over_limit = await post(b"a" * (limit + 1))

    # A correctly signed body of exactly the cap still reaches HMAC verification,
    # which then rejects it as an unsupported event rather than as oversized.
    assert at_limit.status_code == 200
    assert at_limit.json()["reason"] == "unsupported event: ping"
    assert over_limit.status_code == 413


@pytest.mark.asyncio
async def test_chunked_valid_webhook_body_is_hmac_verified_over_streamed_bytes(
    client: httpx.AsyncClient, fake_audit_db: FakeAsyncSession
):
    """The bytes the cap collected are the bytes the signature covers.

    A one-byte tamper on a streamed body must fail even though the JSON
    semantics are identical, and an unsigned body must fail too.
    """
    raw_body = json.dumps(
        {
            "action": "ping",
            "hook": {"id": 1, "type": "Repository"},
            "repository": {"name": "r", "owner": {"login": "o"}},
        }
    ).encode()

    async def post(signature: str | None) -> httpx.Response:
        headers = {
            "X-GitHub-Event": "ping",
            "X-GitHub-Delivery": str(uuid.uuid4()),
        }
        if signature is not None:
            headers["X-Hub-Signature-256"] = signature
        return await client.post(
            "/webhooks/github",
            headers=headers,
            content=CountingChunkedBody(raw_body, chunk_size=8),
        )

    accepted = await post(sign_payload(TEST_SECRET, raw_body))
    tampered_payload = bytearray(raw_body)
    tampered_payload[-2] ^= 0x01
    tampered = await post(sign_payload(TEST_SECRET, bytes(tampered_payload)))
    unsigned = await post(None)
    wrong_key = await post(sign_payload("not-the-webhook-secret", raw_body))

    assert accepted.status_code == 200
    assert accepted.json()["status"] == "ignored"
    for rejected in (tampered, unsigned, wrong_key):
        assert rejected.status_code == 401
        assert rejected.json() == {"detail": "Invalid or missing signature"}


def test_webhook_ingress_never_buffers_the_body_unbounded():
    """Regression guard: `await request.body()` re-opens the DoS vector."""
    import inspect

    from app import webhooks

    def code_only(source: str) -> str:
        return " ".join(
            line.split("#")[0].strip()
            for line in source.splitlines()
            if not line.strip().startswith("#")
        )

    handler_code = code_only(inspect.getsource(webhooks.github_webhook))
    assert "request.body()" not in handler_code
    assert "await _read_limited_body(request)" in handler_code

    reader_code = code_only(inspect.getsource(webhooks._read_limited_body))
    assert "request.stream()" in reader_code
    assert "request.body()" not in reader_code
    # HMAC over the collected raw bytes, strictly before any JSON parsing.
    assert handler_code.index("verify_github_signature(") < handler_code.index(
        "json.loads("
    )


# ---------------------------------------------------------------------------
# 4. pull_request opened / synchronize
# ---------------------------------------------------------------------------



@pytest.mark.asyncio
async def test_audit_pr_opened_dispatches(
    client: httpx.AsyncClient, fake_audit_db: FakeAsyncSession, fake_audit_user_factory
):
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory, "pr-audit-org", "pr-audit-repo")
    await set_repo_auditor_settings(fake_audit_db, repo)
    payload = make_pr_payload(
        "pr-audit-org", "pr-audit-repo", action="opened", pr_number=42,
        head_sha="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    )
    with patch(
        "app.adapters.hosting.AWSHostingAdapter.schedule_review",
        new_callable=AsyncMock,
    ):
        resp = await post_signed(client, "pull_request", payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    job = await fake_audit_db.scalar(
        select(AuditJob).where(AuditJob.repo_id == repo.id, AuditJob.audit_type == "pr_audit")
    )
    assert job is not None
    assert job.status == "queued"
    assert job.next_attempt_at is not None
    assert job.base_sha == "9999888877776666555544443333222211110000"
    assert job.head_sha == "b" * 40


@pytest.mark.asyncio
async def test_slow_audit_worker_does_not_block_existing_review_work(
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory, "independent-org", "independent-repo")
    await set_repo_auditor_settings(fake_audit_db, repo)
    base_sha = "9999888877776666555544443333222211110000"
    first_payload = make_pr_payload(
        "independent-org",
        "independent-repo",
        action="opened",
        pr_number=61,
        head_sha="c" * 40,
    )
    second_payload = make_pr_payload(
        "independent-org",
        "independent-repo",
        action="synchronize",
        pr_number=62,
        head_sha="e" * 40,
    )
    audit_started = asyncio.Event()
    release_audit = asyncio.Event()
    review_scheduled = asyncio.Event()
    adapter = MagicMock()
    adapter.schedule_audit = AsyncMock(
        side_effect=AssertionError("webhook path must not schedule audit execution")
    )

    async def slow_review(*_args, **_kwargs):
        review_scheduled.set()

    async def slow_diff(**_kwargs):
        audit_started.set()
        await release_audit.wait()
        return ""

    adapter.schedule_review = AsyncMock(side_effect=slow_review)
    llm_complete = AsyncMock()
    with (
        patch(
            "app.adapters.hosting.get_hosting_adapter",
            new_callable=AsyncMock,
            return_value=adapter,
        ),
        patch(
            "app.services.audit_pipeline.get_auditor_installation_token",
            new_callable=AsyncMock,
            return_value="read-only-token",
        ),
        patch(
            "app.github_client.fetch_pull_request",
            new_callable=AsyncMock,
            return_value={
                "base": {"sha": base_sha},
                "head": {"sha": "c" * 40},
            },
        ),
        patch(
            "app.github_client.fetch_diff",
            new_callable=AsyncMock,
            side_effect=slow_diff,
        ),
        patch("app.subagents.auditor.LLMClient.complete", llm_complete),
    ):
        first_response = await post_signed(client, "pull_request", first_payload)
        review_scheduled.clear()
        dispatch_claim = await audit_pipeline.claim_next_queued_job()
        assert dispatch_claim is not None
        worker = asyncio.create_task(
            audit_pipeline.process_audit_job(
                dispatch_claim.audit_id, dispatch_claim.dispatch_fence_token
            )
        )
        await asyncio.wait_for(audit_started.wait(), timeout=20.0)
        second_response = await asyncio.wait_for(
            post_signed(client, "pull_request", second_payload),
            timeout=10.0,
        )
        await asyncio.wait_for(review_scheduled.wait(), timeout=2.0)
        release_audit.set()
        assert await asyncio.wait_for(worker, timeout=5.0) is True

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    adapter.schedule_review.assert_awaited()
    adapter.schedule_audit.assert_not_awaited()
    llm_complete.assert_not_awaited()
    jobs = list(
        (
            await fake_audit_db.scalars(
                select(AuditJob)
                .where(AuditJob.repo_id == repo.id)
                .order_by(AuditJob.created_at)
            )
        ).all()
    )
    assert len(jobs) == 2
    assert jobs[0].status == "skipped_no_diff"
    assert jobs[0].base_sha == base_sha
    assert jobs[0].head_sha == "c" * 40
    assert jobs[1].status == "queued"


@pytest.mark.asyncio
async def test_real_outbox_worker_runs_read_only_report_path_without_core_patches(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory, "full-audit-org", "full-audit-repo")
    await set_repo_auditor_settings(fake_audit_db, repo)
    base_sha = "a" * 40
    head_sha = "b" * 40
    diff = (
        "diff --git a/src/app.ts b/src/app.ts\n"
        "--- a/src/app.ts\n"
        "+++ b/src/app.ts\n"
        "@@ -1,0 +1,1 @@\n"
        "+export const value = 1;\n"
    )
    source = "const previous = 0;\nexport const value = 1;\n"
    llm_content = json.dumps(
        {"summary": "No grounded issue found.", "confidence": 90, "findings": []}
    )
    llm_response = {
        "content": llm_content,
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "latency_ms": 1,
        "model": "test-engine",
    }
    claim = await audit_pipeline.claim_audit_delivery(
        db=as_session(fake_audit_db),
        repo_id=repo.id,
        audit_type="pr_audit",
        repo_full_name="full-audit-org/full-audit-repo",
        delivery_id=f"full-{uuid.uuid4().hex}",
        ref="feature/full",
        pr_number=42,
        base_sha=base_sha,
        head_sha=head_sha,
    )
    fake_audit_db.expire_all()
    persisted = await fake_audit_db.scalar(
        select(AuditJob).where(AuditJob.audit_id == claim.audit_id)
    )
    assert persisted is not None
    assert persisted.status == "queued"
    assert persisted.next_attempt_at <= datetime.now(timezone.utc)
    llm_complete = AsyncMock(return_value=llm_response)

    with (
        patch(
            "app.services.audit_pipeline.get_auditor_installation_token",
            new_callable=AsyncMock,
            return_value="read-only-token",
        ),
        patch(
            "app.github_client.fetch_pull_request",
            new_callable=AsyncMock,
            return_value={
                "base": {"sha": base_sha},
                "head": {"sha": head_sha},
            },
        ) as fetch_target,
        patch(
            "app.github_client.fetch_diff",
            new_callable=AsyncMock,
            return_value=diff,
        ) as fetch_diff,
        patch(
            "app.github_client.fetch_file_content",
            new_callable=AsyncMock,
            return_value=source,
        ) as fetch_source,
        patch("app.subagents.auditor.LLMClient.complete", llm_complete),
    ):
        summary = await audit_pipeline.dispatch_audit_jobs(
            batch_size=1,
            local_execute=True,
        )

    assert summary.claimed == 1
    assert fetch_target.await_count == 2
    # The auditor must never fall back to the global personal access token; that
    # read-only invariant is also asserted in test_auditor_core.py.
    fetch_diff.assert_awaited_once_with(
        owner="full-audit-org",
        repo="full-audit-repo",
        sha=head_sha,
        base_sha=base_sha,
        token="read-only-token",
        allow_global_token=False,
    )
    fetch_source.assert_awaited_once_with(
        owner="full-audit-org",
        repo="full-audit-repo",
        path="src/app.ts",
        sha=head_sha,
        token="read-only-token",
        allow_global_token=False,
    )
    assert llm_complete.await_count == 4
    fake_audit_db.expire_all()
    job = await fake_audit_db.scalar(
        select(AuditJob).where(AuditJob.audit_id == claim.audit_id)
    )
    assert job is not None
    assert (job.status, job.last_error) == ("completed", None)
    assert job.base_sha == base_sha
    assert job.head_sha == head_sha
    assert job.attempts == 1
    assert job.dispatch_attempts == 1


@pytest.mark.asyncio
async def test_manual_audit_pins_both_endpoints_before_empty_diff_fetch(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    """A PR-numbered delivery is pinned at intake, and the worker honours it.

    `claim_audit_delivery` refuses any PR audit without both endpoints, so the
    delivery is created pinned here and the empty diff is fetched against
    exactly those SHAs — never against the PR's current head.
    """
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory, "manual-pin-org", "manual-pin-repo")
    # The worker re-verifies the persisted trigger before doing any external
    # work, so this repo has to be one where the auditor is actually enabled.
    await set_repo_auditor_settings(fake_audit_db, repo)
    base_sha = "a" * 40
    head_sha = "b" * 40
    claim = await audit_pipeline.claim_audit_delivery(
        db=as_session(fake_audit_db),
        repo_id=repo.id,
        audit_type="manual_audit",
        repo_full_name="manual-pin-org/manual-pin-repo",
        delivery_id=f"manual-pin-{uuid.uuid4().hex}",
        pr_number=77,
        base_sha=base_sha,
        head_sha=head_sha,
    )
    with (
        patch(
            "app.services.audit_pipeline.get_auditor_installation_token",
            new_callable=AsyncMock,
            return_value="read-only-token",
        ),
        patch(
            "app.github_client.fetch_pull_request",
            new_callable=AsyncMock,
            return_value={
                "base": {"sha": base_sha},
                "head": {"sha": head_sha},
            },
        ),
        patch(
            "app.github_client.fetch_diff",
            new_callable=AsyncMock,
            return_value="",
        ) as fetch_diff,
    ):
        summary = await audit_pipeline.dispatch_audit_jobs(
            batch_size=1,
            local_execute=True,
        )

    assert summary.claimed == 1
    fetch_diff.assert_awaited_once_with(
        owner="manual-pin-org",
        repo="manual-pin-repo",
        sha=head_sha,
        base_sha=base_sha,
        token="read-only-token",
        allow_global_token=False,
    )
    fake_audit_db.expire_all()
    job = await fake_audit_db.scalar(
        select(AuditJob).where(AuditJob.audit_id == claim.audit_id)
    )
    assert job is not None
    assert job.status == "skipped_no_diff"
    assert job.base_sha == base_sha
    assert job.head_sha == head_sha


@pytest.mark.asyncio
async def test_audit_pr_synchronize_dispatches(
    client: httpx.AsyncClient, fake_audit_db: FakeAsyncSession, fake_audit_user_factory
):
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory, "sync-audit-org", "sync-audit-repo")
    await set_repo_auditor_settings(fake_audit_db, repo)
    payload = make_pr_payload(
        "sync-audit-org", "sync-audit-repo", action="synchronize", pr_number=43,
        head_sha="cccccccccccccccccccccccccccccccccccccccc",
    )
    with patch(
        "app.adapters.hosting.AWSHostingAdapter.schedule_review",
        new_callable=AsyncMock,
    ):
        resp = await post_signed(client, "pull_request", payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    job = await fake_audit_db.scalar(
        select(AuditJob).where(AuditJob.repo_id == repo.id, AuditJob.audit_type == "pr_audit")
    )
    assert job is not None
    assert job.pr_number == 43
    assert job.base_sha == "9999888877776666555544443333222211110000"
    assert job.head_sha == "c" * 40
    assert job.status == "queued"


@pytest.mark.asyncio
async def test_audit_pr_unsupported_action_ignored(
    client: httpx.AsyncClient, fake_audit_db: FakeAsyncSession, fake_audit_user_factory
):
    await seed_repo(fake_audit_db, fake_audit_user_factory, "ign-audit-org", "ign-audit-repo")
    payload = make_pr_payload("ign-audit-org", "ign-audit-repo", action="labeled", pr_number=44)
    with patch(
        "app.services.audit_pipeline.dispatch_audit",
        return_value="audit-test",
    ) as spy_dispatch:
        resp = await post_signed(client, "pull_request", payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    spy_dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# 5. workflow_run failure / success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_workflow_run_failure_dispatches(
    client: httpx.AsyncClient, fake_audit_db: FakeAsyncSession, fake_audit_user_factory
):
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory, "ci-audit-org", "ci-audit-repo")
    await set_repo_auditor_settings(fake_audit_db, repo)
    payload = make_workflow_payload(
        owner="ci-audit-org", repo="ci-audit-repo", conclusion="failure", run_id=6001001
    )
    with patch("app.adapters.hosting.get_hosting_adapter", new_callable=AsyncMock) as mock_get:
        mock_adapter = MagicMock()
        mock_adapter.schedule_pipeline = AsyncMock()
        mock_get.return_value = mock_adapter
        resp = await post_signed(client, "workflow_run", payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    job = await fake_audit_db.scalar(
        select(AuditJob).where(
            AuditJob.repo_id == repo.id,
            AuditJob.audit_type == "ci_failure_audit",
        )
    )
    assert job is not None
    assert job.status == "queued"


@pytest.mark.asyncio
async def test_audit_workflow_run_success_ignored_by_default(
    client: httpx.AsyncClient, fake_audit_db: FakeAsyncSession, fake_audit_user_factory
):
    """Safe default `on_ci_success=False` keeps green builds silent."""
    await seed_repo(fake_audit_db, fake_audit_user_factory, "ok-audit-org", "ok-audit-repo")
    payload = make_workflow_payload(
        owner="ok-audit-org", repo="ok-audit-repo", conclusion="success", run_id=6001002
    )
    with patch(
        "app.services.audit_pipeline.dispatch_audit", return_value="audit-test"
    ) as spy_dispatch:
        resp = await post_signed(client, "workflow_run", payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    spy_dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_audit_workflow_run_success_queued_when_enabled(
    client: httpx.AsyncClient, fake_audit_db: FakeAsyncSession, fake_audit_user_factory
):
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory, "ok2-audit-org", "ok2-audit-repo")
    await set_repo_auditor_settings(fake_audit_db, repo, on_ci_success=True)
    payload = make_workflow_payload(
        owner="ok2-audit-org", repo="ok2-audit-repo", conclusion="success", run_id=6001003
    )
    resp = await post_signed(client, "workflow_run", payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "audit_queued"
    assert data["audit_type"] == "ci_success_audit"
    job = await fake_audit_db.scalar(
        select(AuditJob).where(
            AuditJob.repo_id == repo.id,
            AuditJob.audit_type == "ci_success_audit",
        )
    )
    assert job is not None
    assert job.status == "queued"


# ---------------------------------------------------------------------------
# 6. issue_comment @haunter audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_issue_comment_manual_audit_is_refused_without_pinned_endpoints(
    client: httpx.AsyncClient, fake_audit_db: FakeAsyncSession, fake_audit_user_factory
):
    """issue_comment carries no PR endpoints, so a manual audit cannot be pinned.

    Queuing one anyway would let the worker resolve the PR's *current* head at
    execution time and audit a commit the requester never referenced.
    """
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory, "m-audit-org", "m-audit-repo")
    await set_repo_auditor_settings(fake_audit_db, repo)
    payload = make_issue_comment_payload(
        owner="m-audit-org",
        repo="m-audit-repo",
        comment_body="@haunter audit this PR for security issues please",
        pr_number=77,
        comment_id=9101001,
    )
    resp = await post_signed(client, "issue_comment", payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ignored"
    assert "pinned pull request endpoints" in data["reason"]
    job = await fake_audit_db.scalar(
        select(AuditJob).where(
            AuditJob.repo_id == repo.id,
            AuditJob.audit_type == "manual_audit",
        )
    )
    assert job is None


@pytest.mark.asyncio
async def test_issue_comment_manual_audit_never_adopts_mismatched_endpoints(
    client: httpx.AsyncClient, fake_audit_db: FakeAsyncSession, fake_audit_user_factory
):
    """Endpoints in the payload are not the PR's endpoints, so they are refused.

    An `issue_comment` payload has no trustworthy PR endpoints of its own. A
    sender that bolts a `pull_request` object onto the payload — or names two
    commits in the comment body — must not have those SHAs adopted: they are not
    the commits the request was made against, and the worker would then pin the
    audit to commits the requester never referenced. The request is refused, not
    silently re-pinned.
    """
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory, "m3-audit-org", "m3-audit-repo")
    await set_repo_auditor_settings(fake_audit_db, repo)
    stale_base = "9" * 40
    stale_head = "8" * 40
    payload = make_issue_comment_payload(
        owner="m3-audit-org",
        repo="m3-audit-repo",
        comment_body=(
            "@haunter audit base "
            f"{stale_base} head {stale_head} please"
        ),
        pr_number=78,
        comment_id=9101002,
    )
    # A forged endpoints block the issue_comment schema does not carry.
    payload["issue"]["pull_request"]["base"] = {"ref": "main", "sha": stale_base}
    payload["issue"]["pull_request"]["head"] = {"ref": "stale", "sha": stale_head}

    resp = await post_signed(client, "issue_comment", payload)

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ignored"
    assert "pinned pull request endpoints" in data["reason"]
    job = await fake_audit_db.scalar(
        select(AuditJob).where(
            AuditJob.repo_id == repo.id,
            AuditJob.audit_type == "manual_audit",
        )
    )
    assert job is None
    # Nothing anywhere in the outbox was pinned to the injected SHAs.
    stale = (
        await fake_audit_db.scalars(
            select(AuditJob).where(
                AuditJob.repo_id == repo.id,
                (AuditJob.base_sha == stale_base) | (AuditJob.head_sha == stale_head),
            )
        )
    ).all()
    assert stale == []


@pytest.mark.asyncio
async def test_review_comment_manual_audit_is_pinned_to_the_reviewed_commits(

    client: httpx.AsyncClient, fake_audit_db: FakeAsyncSession, fake_audit_user_factory
):
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory, "rc-audit-org", "rc-audit-repo")
    await set_repo_auditor_settings(fake_audit_db, repo)
    base_sha = "1" * 40
    head_sha = "2" * 40
    payload = make_review_comment_payload(
        owner="rc-audit-org",
        repo="rc-audit-repo",
        pr_number=91,
        base_sha=base_sha,
        head_sha=head_sha,
    )
    resp = await post_signed(client, "pull_request_review_comment", payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "audit_queued"
    assert data["audit_type"] == "manual_audit"
    job = await fake_audit_db.scalar(
        select(AuditJob).where(
            AuditJob.repo_id == repo.id,
            AuditJob.audit_type == "manual_audit",
        )
    )
    assert job is not None
    assert job.status == "queued"
    assert job.pr_number == 91
    assert job.base_sha == base_sha
    assert job.head_sha == head_sha


@pytest.mark.asyncio
async def test_review_comment_manual_audit_is_refused_without_base_sha(
    client: httpx.AsyncClient, fake_audit_db: FakeAsyncSession, fake_audit_user_factory
):
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory, "rc2-audit-org", "rc2-audit-repo")
    await set_repo_auditor_settings(fake_audit_db, repo)
    payload = make_review_comment_payload(
        owner="rc2-audit-org",
        repo="rc2-audit-repo",
        pr_number=92,
        base_sha=None,
        head_sha="2" * 40,
    )
    resp = await post_signed(client, "pull_request_review_comment", payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    job = await fake_audit_db.scalar(
        select(AuditJob).where(
            AuditJob.repo_id == repo.id,
            AuditJob.audit_type == "manual_audit",
        )
    )
    assert job is None


@pytest.mark.asyncio
async def test_audit_comment_without_audit_keyword_does_not_dispatch_auditor(
    client: httpx.AsyncClient, fake_audit_db: FakeAsyncSession, fake_audit_user_factory
):
    """Plain `@haunter fix …` (no `audit` keyword) must not schedule an audit."""
    await seed_repo(fake_audit_db, fake_audit_user_factory, "m2-audit-org", "m2-audit-repo")
    payload = make_issue_comment_payload(
        owner="m2-audit-org",
        repo="m2-audit-repo",
        comment_body="@haunter please fix the null handling",
        pr_number=78,
        comment_id=9101002,
    )
    with patch(
        "app.services.audit_pipeline.dispatch_audit", return_value="audit-test"
    ) as spy_dispatch:
        resp = await post_signed(client, "issue_comment", payload)
    assert resp.status_code == 200
    # Fix pipeline ignores (no parent Run) — auditor must stay silent too.
    assert resp.json()["status"] == "ignored"
    spy_dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# 7. Disabled-feature early exit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_disabled_feature_early_exit_pr(
    client: httpx.AsyncClient, fake_audit_db: FakeAsyncSession, fake_audit_user_factory
):
    """Auditor disabled → no audit scheduled; review pipeline unaffected."""
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory, "off-audit-org", "off-audit-repo")
    await set_repo_auditor_settings(
        fake_audit_db,
        repo,
        enabled=False,
        on_pr=False,
        on_ci_failure=False,
        on_ci_success=False,
        on_manual_mention=False,
    )
    payload = make_pr_payload(
        "off-audit-org", "off-audit-repo", action="opened", pr_number=45,
        head_sha="dddddddddddddddddddddddddddddddddddddddd",
    )
    with patch(
        "app.adapters.hosting.AWSHostingAdapter.schedule_review",
        new_callable=AsyncMock,
    ):
        resp = await post_signed(client, "pull_request", payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    count = await fake_audit_db.scalar(
        select(func.count()).select_from(AuditJob).where(AuditJob.repo_id == repo.id)
    )
    assert count == 0


@pytest.mark.asyncio
async def test_audit_disabled_feature_early_exit_manual(
    client: httpx.AsyncClient, fake_audit_db: FakeAsyncSession, fake_audit_user_factory
):
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory, "off2-audit-org", "off2-audit-repo")
    await set_repo_auditor_settings(
        fake_audit_db,
        repo,
        enabled=False,
        on_pr=False,
        on_ci_failure=False,
        on_ci_success=False,
        on_manual_mention=False,
    )
    payload = make_issue_comment_payload(
        owner="off2-audit-org",
        repo="off2-audit-repo",
        comment_body="@haunter audit this please",
        pr_number=79,
        comment_id=9101003,
    )
    resp = await post_signed(client, "issue_comment", payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    count = await fake_audit_db.scalar(
        select(func.count()).select_from(AuditJob).where(AuditJob.repo_id == repo.id)
    )
    assert count == 0


def test_audit_trigger_unit_disabled_short_circuits():
    """Pure unit check: disabled master switch suppresses every event type."""
    disabled = AuditorTriggerSettings(enabled=False)
    assert audit_pipeline.evaluate_pr(disabled, "opened").should_audit is False
    assert audit_pipeline.evaluate_workflow_run(
        disabled, "completed", "failure"
    ).should_audit is False
    assert audit_pipeline.evaluate_manual_comment(
        disabled, "created", "@haunter audit"
    ).should_audit is False
    assert audit_pipeline.should_audit_event(
        disabled, "pull_request", {"action": "opened"}
    ).should_audit is False


@pytest.mark.asyncio
async def test_audit_get_trigger_fallback_without_db():
    """No DB context fails closed to the full_autonomous auditor-off default."""
    trigger = await audit_pipeline.get_auditor_trigger(None, None)
    assert trigger.enabled is False
    assert trigger.on_pr is True
    assert trigger.on_ci_failure is True
    assert trigger.on_ci_success is False
    assert trigger.on_manual_mention is True


@pytest.mark.asyncio
async def test_auditor_trigger_missing_settings_table_fails_closed():
    mock_db = AsyncMock()
    mock_db.scalar.side_effect = RuntimeError("relation repo_settings does not exist")

    trigger = await audit_pipeline.get_auditor_trigger(mock_db, uuid.uuid4())

    assert trigger.enabled is False
    mock_db.scalar.assert_awaited_once()
    mock_db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_auditor_trigger_missing_settings_row_fails_closed(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory, "settings-missing-org", "settings-missing-repo")

    trigger = await audit_pipeline.get_auditor_trigger(as_session(fake_audit_db), repo.id)

    assert trigger.enabled is False
    assert trigger == audit_pipeline.default_auditor_trigger()


@pytest.mark.asyncio
async def test_fake_audit_store_fails_loudly_on_unmodelled_sql(
    fake_audit_db: FakeAsyncSession,
):
    """The hermetic store must never quietly disagree with real PostgreSQL.

    Everything outside the modelled statement set raises
    `UnsupportedStatementError` instead of doing something plausible-but-wrong, so
    a new query shape in production breaks this suite loudly rather than passing
    against a fake that silently ignored it.
    """
    # A plain INSERT would require real unique/foreign-key enforcement.
    with pytest.raises(UnsupportedStatementError):
        await fake_audit_db.execute(
            pg_insert(AuditJob).values(
                audit_id=f"audit-{uuid.uuid4().hex[:12]}",
                repo_id=uuid.uuid4(),
                delivery_id=f"unmodelled-{uuid.uuid4().hex}",
                delivery_fingerprint="c" * 64,
                settings_version=1,
                audit_type="pr_audit",
                status="queued",
            )
        )
    with pytest.raises(UnsupportedStatementError):
        await fake_audit_db.execute(delete(AuditJob))
    # An aggregate the store does not model must not be reported as a count.
    with pytest.raises(UnsupportedStatementError):
        await fake_audit_db.scalar(select(func.max(AuditJob.attempts)))
    # Rows must be left untouched by the rejected statements.
    assert await fake_audit_db.scalar(
        select(func.count()).select_from(AuditJob)
    ) == 0


@pytest.mark.asyncio
async def test_auditor_trigger_db_error_fails_closed_and_rolls_back():
    mock_db = AsyncMock()
    mock_db.scalar.side_effect = RuntimeError("database unavailable")

    trigger = await audit_pipeline.get_auditor_trigger(mock_db, uuid.uuid4())

    assert trigger.enabled is False
    mock_db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_auditor_trigger_malformed_row_fails_closed():
    mock_db = AsyncMock()
    mock_db.scalar.return_value = SimpleNamespace(
        enable_auditor_mode="true",
        audit_trigger_on_pr=True,
        audit_trigger_on_ci_failure=True,
        audit_trigger_on_ci_success=False,
        audit_trigger_on_manual_mention=True,
    )

    trigger = await audit_pipeline.get_auditor_trigger(mock_db, uuid.uuid4())

    assert trigger.enabled is False


@pytest.mark.asyncio
@pytest.mark.db
async def test_auditor_trigger_explicit_enabled_row_is_real_db_opt_in(
    db: AsyncSession,
    user_factory,
):
    """A real `repo_settings` row round-trips its booleans and settings version.

    Genuine SQL: the booleans and `settings_version` come back from a
    PostgreSQL row (not Python values), and `get_auditor_trigger` must reject a
    value that is not a real `bool` after that round trip.
    """
    await truncate_all(db)
    repo = await seed_repo(db, user_factory, "settings-enabled-org", "settings-enabled-repo")
    await set_repo_auditor_settings(db, repo, on_ci_success=True)

    trigger = await audit_pipeline.get_auditor_trigger(db, repo.id)

    assert trigger == ENABLED_TRIGGER


# ---------------------------------------------------------------------------
# MUST-1 regression: workflow_run auditor runs only for registered repos
# with an enabled per-repo trigger (repo lookup above auditor block,
# haunter/* guard + kill-switch enforced on this path).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_workflow_run_unregistered_repo_ignored_no_dispatch(
    client: httpx.AsyncClient, fake_audit_db: FakeAsyncSession, fake_audit_user_factory
):
    """Unregistered workflow_run.failure -> ignored, auditor never dispatched."""
    payload = make_workflow_payload(
        owner="ghost-org", repo="ghost-repo", conclusion="failure", run_id=6001991
    )
    with patch(
        "app.services.audit_pipeline.dispatch_audit", return_value="audit-test"
    ) as spy_dispatch:
        resp = await post_signed(client, "workflow_run", payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    spy_dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_audit_workflow_run_disabled_trigger_no_dispatch(
    client: httpx.AsyncClient, fake_audit_db: FakeAsyncSession, fake_audit_user_factory
):
    """Disabled per-repo trigger -> no audit dispatched; fix pipeline unaffected."""
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory, "kill-org", "kill-repo")
    await set_repo_auditor_settings(
        fake_audit_db,
        repo,
        enabled=False,
        on_pr=False,
        on_ci_failure=False,
        on_ci_success=False,
        on_manual_mention=False,
    )
    payload = make_workflow_payload(
        owner="kill-org", repo="kill-repo", conclusion="failure", run_id=6001992
    )
    with patch("app.adapters.hosting.get_hosting_adapter", new_callable=AsyncMock) as mock_get:
        mock_adapter = MagicMock()
        mock_adapter.schedule_pipeline = AsyncMock()
        mock_get.return_value = mock_adapter
        resp = await post_signed(client, "workflow_run", payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    count = await fake_audit_db.scalar(
        select(func.count()).select_from(AuditJob).where(AuditJob.repo_id == repo.id)
    )
    assert count == 0


@pytest.mark.asyncio
async def test_audit_workflow_run_haunter_branch_ignored_no_dispatch(
    client: httpx.AsyncClient, fake_audit_db: FakeAsyncSession, fake_audit_user_factory
):
    """haunter/* branch guard applies to the auditor path: no dispatch."""
    await seed_repo(fake_audit_db, fake_audit_user_factory, "loop-org", "loop-repo")
    payload = make_workflow_payload(
        owner="loop-org",
        repo="loop-repo",
        conclusion="failure",
        run_id=6001993,
        head_branch="haunter/fix-123",
    )
    with patch(
        "app.services.audit_pipeline.dispatch_audit", return_value="audit-test"
    ) as spy_dispatch:
        resp = await post_signed(client, "workflow_run", payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    spy_dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# SHOULD-3: durable delivery idempotency and post-claim queue dispatch.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.db
async def test_audit_delivery_claim_is_atomic_across_worker_sessions(
    db: AsyncSession,
    user_factory,
):
    """Two concurrent sessions race the same `delivery_id`.

    Genuine SQL: this asserts that PostgreSQL's `INSERT ... ON CONFLICT DO
    NOTHING (uq_audit_jobs_delivery_id)` resolves the race, not a Python-level
    check, so it stays on a real database.
    """
    await truncate_all(db)
    repo = await seed_repo(db, user_factory, "idem-org", "idem-repo")
    delivery_id = f"deliv-test-{uuid.uuid4().hex[:8]}"

    async with async_session_maker() as first_db, async_session_maker() as second_db:
        first, second = await asyncio.gather(
            audit_pipeline.claim_audit_delivery(
                db=first_db,
                repo_id=repo.id,
                audit_type="pr_audit",
                repo_full_name="idem-org/idem-repo",
                delivery_id=delivery_id,
                pr_number=42,
                base_sha="b" * 40,
                head_sha="a" * 40,
            ),
            audit_pipeline.claim_audit_delivery(
                db=second_db,
                repo_id=repo.id,
                audit_type="pr_audit",
                repo_full_name="idem-org/idem-repo",
                delivery_id=delivery_id,
                pr_number=42,
                base_sha="b" * 40,
                head_sha="a" * 40,
            ),
        )

    assert first.audit_id == second.audit_id
    assert sorted((first.claimed, second.claimed)) == [False, True]
    count = await db.scalar(
        select(func.count()).select_from(AuditJob).where(AuditJob.delivery_id == delivery_id)
    )
    assert count == 1


@pytest.mark.asyncio
@pytest.mark.db
async def test_concurrent_dispatch_claim_has_single_winner(
    db: AsyncSession,
    user_factory,
):
    """Genuine SQL: `SELECT ... FOR UPDATE SKIP LOCKED` must give one winner."""
    await truncate_all(db)
    repo = await seed_repo(db, user_factory, "claim-org", "claim-repo")
    audit_id = await persist_audit_job(db, repo, f"claim-{uuid.uuid4().hex}")

    async with async_session_maker(), async_session_maker():
        first, second = await asyncio.gather(
            audit_pipeline.claim_next_queued_job(),
            audit_pipeline.claim_next_queued_job(),
        )

    claims = [claim for claim in (first, second) if claim is not None]
    assert len(claims) == 1
    assert claims[0].audit_id == audit_id
    assert claims[0].dispatch_attempts == 1
    job = await db.scalar(select(AuditJob).where(AuditJob.audit_id == audit_id))
    assert job is not None
    assert job.status == "dispatching"
    assert job.lease_expires_at is not None


@pytest.mark.asyncio
async def test_dispatch_scheduler_failure_requeues_then_fails_terminally(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory, "dispatch-fail-org", "dispatch-fail-repo")
    audit_id = await persist_audit_job(fake_audit_db, repo, f"dispatch-fail-{uuid.uuid4().hex}")
    failing_adapter = MagicMock()
    failing_adapter.schedule_audit = AsyncMock(side_effect=RuntimeError("scheduler down"))

    with (
        patch(
            "app.adapters.hosting.get_hosting_adapter",
            new_callable=AsyncMock,
            return_value=failing_adapter,
        ),
        patch("app.services.audit_pipeline.random.uniform", return_value=1.0),
    ):
        first_summary = await audit_pipeline.dispatch_audit_jobs(batch_size=1)

    job = await fake_audit_db.scalar(select(AuditJob).where(AuditJob.audit_id == audit_id))
    assert first_summary.claimed == 1
    assert first_summary.released == 1
    assert first_summary.terminal == 0
    assert job is not None
    assert job.status == "queued"
    assert job.dispatch_attempts == 1
    assert job.last_error == "RuntimeError"
    assert job.lease_expires_at is None
    assert job.next_attempt_at is not None

    await fake_audit_db.execute(
        update(AuditJob)
        .where(AuditJob.audit_id == audit_id)
        .values(
            status="queued",
            dispatch_attempts=audit_pipeline.AUDIT_MAX_DISPATCH_ATTEMPTS - 1,
            next_attempt_at=datetime.now(timezone.utc),
        )
    )
    await fake_audit_db.commit()
    with patch(
        "app.adapters.hosting.get_hosting_adapter",
        new_callable=AsyncMock,
        return_value=failing_adapter,
    ):
        final_summary = await audit_pipeline.dispatch_audit_jobs(batch_size=1)

    await fake_audit_db.refresh(job)
    assert final_summary.terminal == 1
    assert job.status == "failed"
    assert job.dispatch_attempts == audit_pipeline.AUDIT_MAX_DISPATCH_ATTEMPTS


@pytest.mark.asyncio
async def test_expired_dispatch_and_processing_leases_are_recovered(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory, "lease-org", "lease-repo")
    running_id = await persist_audit_job(fake_audit_db, repo, f"running-{uuid.uuid4().hex}")
    dispatching_id = await persist_audit_job(fake_audit_db, repo, f"dispatching-{uuid.uuid4().hex}")
    expired = datetime.now(timezone.utc) - timedelta(minutes=5)
    await fake_audit_db.execute(
        update(AuditJob)
        .where(AuditJob.audit_id == running_id)
        .values(
            status="running",
            attempts=1,
            lease_expires_at=expired,
            next_attempt_at=datetime.now(timezone.utc),
        )
    )
    await fake_audit_db.execute(
        update(AuditJob)
        .where(AuditJob.audit_id == dispatching_id)
        .values(
            status="dispatching",
            dispatch_attempts=1,
            lease_expires_at=expired,
            next_attempt_at=datetime.now(timezone.utc),
        )
    )
    await fake_audit_db.commit()

    recovered = await audit_pipeline.recover_expired_audit_leases()

    assert recovered == (1, 1)
    jobs = list(
        (
            await fake_audit_db.scalars(
                select(AuditJob).where(AuditJob.audit_id.in_([running_id, dispatching_id]))
            )
        ).all()
    )
    assert {job.status for job in jobs} == {"queued"}
    assert all(job.lease_expires_at is None for job in jobs)
    assert all(job.last_error == "LeaseExpired" for job in jobs)
    reclaimed = await audit_pipeline.claim_next_queued_job()
    assert reclaimed is not None
    assert reclaimed.audit_id in {running_id, dispatching_id}


@pytest.mark.asyncio
async def test_stale_lease_holder_cannot_overwrite_newer_attempt(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    """A superseded attempt can never write over the attempt that replaced it.

    `claim_next_queued_job` bumps the dispatch counter and mints the new fence
    in the same conditional UPDATE, so the "newer attempt" is set up here the
    same way: counter *and* fence move together.
    """
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory, "fencing-org", "fencing-repo")
    await set_repo_auditor_settings(fake_audit_db, repo)
    audit_id = await persist_audit_job(fake_audit_db, repo, f"fencing-{uuid.uuid4().hex}")
    stale_dispatch = await audit_pipeline.claim_next_queued_job()
    assert stale_dispatch is not None
    superseded_fence = audit_pipeline.audit_dispatch_fence_token(
        audit_id, 2, TEST_SELF_INVOKE_SECRET
    )
    await fake_audit_db.execute(
        update(AuditJob)
        .where(AuditJob.audit_id == audit_id)
        .values(
            status="dispatching",
            dispatch_attempts=2,
            dispatch_fence_token=superseded_fence,
        )
    )
    await fake_audit_db.commit()

    # The attempt-1 holder's release is fenced out: its counter no longer matches.
    assert (
        await audit_pipeline.release_dispatch_claim(
            stale_dispatch,
            RuntimeError("stale scheduler failure"),
        )
        is False
    )
    fake_audit_db.expire_all()
    job = await fake_audit_db.scalar(select(AuditJob).where(AuditJob.audit_id == audit_id))
    assert job is not None
    assert job.status == "dispatching"
    assert job.dispatch_attempts == 2
    assert job.dispatch_fence_token == superseded_fence

    # The superseded fence cannot claim processing.
    stale_processing = await audit_pipeline._claim_processing_job(
        audit_id, stale_dispatch.dispatch_fence_token
    )
    assert stale_processing is None
    # The current attempt's own fence can, and it takes processing attempt 1.
    current_fence_processing = await audit_pipeline._claim_processing_job(
        audit_id,
        superseded_fence,
    )
    assert current_fence_processing is not None
    assert isinstance(current_fence_processing, tuple)
    claimed_attempt: int = int(current_fence_processing[2])
    assert claimed_attempt == 1

    # A newer processing attempt has since taken the job over.
    newer_attempt: int = claimed_attempt + 1
    await fake_audit_db.execute(
        update(AuditJob)
        .where(AuditJob.audit_id == audit_id)
        .values(status="running", attempts=newer_attempt)
    )
    await fake_audit_db.commit()
    # Every write carrying the superseded attempt number is fenced out.
    assert (
        await audit_pipeline._retry_or_fail_audit_job(
            audit_id,
            claimed_attempt,
            RuntimeError("stale processing failure"),
        )
        is False
    )
    fake_audit_db.expire_all()
    job = await fake_audit_db.scalar(select(AuditJob).where(AuditJob.audit_id == audit_id))
    assert job is not None
    assert job.status == "running"
    assert job.attempts == newer_attempt
    assert await audit_pipeline._complete_audit_job(audit_id, claimed_attempt) is False
    fake_audit_db.expire_all()
    job = await fake_audit_db.scalar(select(AuditJob).where(AuditJob.audit_id == audit_id))
    assert job is not None
    assert job.status == "running"
    assert job.attempts == newer_attempt


@pytest.mark.asyncio
async def test_pr_advancement_requeues_without_mixed_source_context(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory, "advance-org", "advance-repo")
    await set_repo_auditor_settings(fake_audit_db, repo)
    base_sha = "a" * 40

    pinned_head = "b" * 40
    advanced_head = "c" * 40
    claim = await audit_pipeline.claim_audit_delivery(
        db=as_session(fake_audit_db),
        repo_id=repo.id,
        audit_type="pr_audit",
        repo_full_name="advance-org/advance-repo",
        delivery_id=f"advance-{uuid.uuid4().hex}",
        pr_number=42,
        base_sha=base_sha,
        head_sha=pinned_head,
    )
    with (
        patch(
            "app.services.audit_pipeline.get_auditor_installation_token",
            new_callable=AsyncMock,
            return_value="read-only-token",
        ),
        patch(
            "app.github_client.fetch_pull_request",
            new_callable=AsyncMock,
            side_effect=[
                {"base": {"sha": base_sha}, "head": {"sha": pinned_head}},
                {"base": {"sha": base_sha}, "head": {"sha": advanced_head}},
            ],
        ),
        patch(
            "app.github_client.fetch_diff",
            new_callable=AsyncMock,
            return_value=(
                "diff --git a/app.ts b/app.ts\n"
                "--- a/app.ts\n"
                "+++ b/app.ts\n"
                "@@ -1,0 +1,1 @@\n"
                "+const value = 1;\n"
            ),
        ),
        patch(
            "app.github_client.fetch_file_content",
            new_callable=AsyncMock,
        ) as fetch_source,
    ):
        dispatch_claim = await audit_pipeline.claim_next_queued_job()
        assert dispatch_claim is not None
        assert (
            await audit_pipeline.process_audit_job(
                claim.audit_id, dispatch_claim.dispatch_fence_token
            )
            is False
        )

    fetch_source.assert_not_awaited()
    fake_audit_db.expire_all()
    job = await fake_audit_db.scalar(select(AuditJob).where(AuditJob.audit_id == claim.audit_id))
    assert job is not None
    assert job.status == "queued"
    assert job.base_sha == base_sha
    assert job.head_sha == pinned_head
    assert job.last_error == "AuditTargetChangedError"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        GitHubAuthError("auth"),
        GitHubRateLimitError("rate"),
        GitHubNetworkError("network"),
        GitHubResourceNotFoundError("private"),
    ],
)
async def test_typed_diff_fetch_failure_requeues_instead_of_completing(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
    error: Exception,
):
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory, "fetch-fail-org", "fetch-fail-repo")
    await set_repo_auditor_settings(fake_audit_db, repo)
    audit_id = await persist_audit_job(fake_audit_db, repo, f"fetch-fail-{uuid.uuid4().hex}")


    with (
        patch(
            "app.services.audit_pipeline.get_auditor_installation_token",
            new_callable=AsyncMock,
            return_value="read-only-token",
        ),
        patch(
            "app.github_client.fetch_diff",
            new_callable=AsyncMock,
            side_effect=error,
        ),
    ):
        dispatch_claim = await audit_pipeline.claim_next_queued_job()
        assert dispatch_claim is not None
        assert (
            await audit_pipeline.process_audit_job(
                audit_id, dispatch_claim.dispatch_fence_token
            )
            is False
        )

    fake_audit_db.expire_all()
    job = await fake_audit_db.scalar(select(AuditJob).where(AuditJob.audit_id == audit_id))
    assert job is not None
    assert job.status == "queued"
    assert job.attempts == 1
    assert job.last_error == "AuditDiffFetchError"


@pytest.mark.asyncio
async def test_processing_failure_retries_then_fails_terminally(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory, "process-fail-org", "process-fail-repo")
    await set_repo_auditor_settings(fake_audit_db, repo)
    audit_id = await persist_audit_job(fake_audit_db, repo, f"process-fail-{uuid.uuid4().hex}")


    with (
        patch(
            "app.services.audit_pipeline.get_auditor_installation_token",
            new_callable=AsyncMock,
            return_value="read-only-token",
        ),
        patch(
            "app.services.audit_pipeline.execute_audit_job",
            new_callable=AsyncMock,
            side_effect=RuntimeError("audit failed"),
        ),
        patch("app.services.audit_pipeline.random.uniform", return_value=1.0),
    ):
        for expected_attempt in range(1, audit_pipeline.AUDIT_MAX_PROCESSING_ATTEMPTS + 1):
            claim = await audit_pipeline.claim_next_queued_job()
            assert claim is not None
            assert (
                await audit_pipeline.process_audit_job(
                    claim.audit_id, claim.dispatch_fence_token
                )
                is False
            )
            fake_audit_db.expire_all()
            job = await fake_audit_db.scalar(
                select(AuditJob).where(AuditJob.audit_id == audit_id)
            )
            assert job is not None
            assert job.attempts == expected_attempt
            if expected_attempt < audit_pipeline.AUDIT_MAX_PROCESSING_ATTEMPTS:
                assert job.status == "queued"
                await fake_audit_db.execute(
                    update(AuditJob)
                    .where(AuditJob.audit_id == audit_id)
                    .values(next_attempt_at=datetime.now(timezone.utc))
                )
                await fake_audit_db.commit()
            else:
                assert job.status == "failed"
                assert job.last_error == "RuntimeError"


@pytest.mark.asyncio
async def test_dispatch_persists_idempotently_without_calling_scheduler(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory, "queue-org", "queue-repo")
    delivery_id = f"queue-{uuid.uuid4().hex[:12]}"
    adapter = MagicMock()
    adapter.schedule_audit = AsyncMock(
        side_effect=AssertionError("durable persistence must not schedule execution")
    )

    with patch(
        "app.adapters.hosting.get_hosting_adapter",
        new_callable=AsyncMock,
        return_value=adapter,
    ):
        first = await audit_pipeline.dispatch_audit(
            db=as_session(fake_audit_db),
            repo_id=repo.id,
            audit_type="pr_audit",
            repo_full_name="queue-org/queue-repo",
            delivery_id=delivery_id,
            pr_number=42,
            base_sha="a" * 40,
            head_sha="b" * 40,
        )
        second = await audit_pipeline.dispatch_audit(
            db=as_session(fake_audit_db),
            repo_id=repo.id,
            audit_type="pr_audit",
            repo_full_name="queue-org/queue-repo",
            delivery_id=delivery_id,
            pr_number=42,
            base_sha="a" * 40,
            head_sha="b" * 40,
        )

    assert first == second
    adapter.schedule_audit.assert_not_awaited()
    job = await fake_audit_db.scalar(select(AuditJob).where(AuditJob.audit_id == first))
    assert job is not None
    assert job.status == "queued"
    assert job.base_sha == "a" * 40
    assert job.head_sha == "b" * 40
    assert job.dispatch_attempts == 0


# ---------------------------------------------------------------------------
# SHOULD-4: manual-mention requires a whole-word `audit`.
# ---------------------------------------------------------------------------


def test_audit_manual_comment_requires_whole_word():
    assert audit_pipeline.is_manual_audit_comment("@haunter audit this PR") is True
    assert audit_pipeline.is_manual_audit_comment("@HAUNTER please AUDIT this") is True
    # Near-misses must not trigger.
    assert audit_pipeline.is_manual_audit_comment("@haunter auditing the logs") is False
    # \b splits on the hyphen, so "audit-trail" still counts as the word.
    assert audit_pipeline.is_manual_audit_comment("@haunter audit-trail review") is True
    assert audit_pipeline.is_manual_audit_comment("@haunter please fix this") is False
    assert audit_pipeline.is_manual_audit_comment(None) is False


# ---------------------------------------------------------------------------
# Phase 5.2: delivery payload binding, dispatch fencing, and DB constraints.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delayed_child_from_superseded_attempt_cannot_claim_the_job(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    """A child invoked by attempt 1 must not run after attempt 2 took over.

    Reproduces the original defect: the dispatcher released/expired the lease
    while an orphaned executor thread could still deliver its child, and the
    child then claimed a job that a newer attempt already owned.
    """
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory, "fence-org", "fence-repo")
    await set_repo_auditor_settings(fake_audit_db, repo)
    audit_id = await persist_audit_job(fake_audit_db, repo, f"fence-{uuid.uuid4().hex}")


    first = await audit_pipeline.claim_next_queued_job()
    assert first is not None
    assert first.audit_id == audit_id
    assert first.dispatch_attempts == 1

    # The lease expires without the child ever arriving; recovery requeues and
    # the next poll mints a second, different fence.
    await fake_audit_db.execute(
        update(AuditJob)
        .where(AuditJob.audit_id == audit_id)
        .values(
            lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
    )
    await fake_audit_db.commit()
    await audit_pipeline.recover_expired_audit_leases()
    fake_audit_db.expire_all()
    requeued = await fake_audit_db.scalar(select(AuditJob).where(AuditJob.audit_id == audit_id))
    assert requeued is not None
    assert requeued.status == "queued"
    assert requeued.dispatch_fence_token is None

    second = await audit_pipeline.claim_next_queued_job()
    assert second is not None
    assert second.audit_id == audit_id
    assert second.dispatch_attempts == 2
    assert second.dispatch_fence_token != first.dispatch_fence_token

    # The delayed attempt-1 child is rejected and changes nothing.
    assert (
        await audit_pipeline.process_audit_job(
            audit_id, first.dispatch_fence_token
        )
        is True
    )
    fake_audit_db.expire_all()
    job = await fake_audit_db.scalar(select(AuditJob).where(AuditJob.audit_id == audit_id))
    assert job is not None
    assert job.status == "dispatching"
    assert job.attempts == 0
    assert job.dispatch_fence_token == second.dispatch_fence_token

    # A forged fence for the current attempt is rejected too.
    forged = "0" * 64
    assert await audit_pipeline._claim_processing_job(audit_id, forged) is None
    # The current attempt is the only one that may claim.
    assert await audit_pipeline._claim_processing_job(
        audit_id, second.dispatch_fence_token
    ) is not None


@pytest.mark.asyncio
async def test_replay_with_identical_payload_deduplicates(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory, "fp-same-org", "fp-same-repo")
    delivery_id = f"fp-same-{uuid.uuid4().hex[:12]}"
    payload: dict[str, Any] = {
        "db": as_session(fake_audit_db),
        "repo_id": repo.id,
        "audit_type": "pr_audit",
        "repo_full_name": f"{repo.owner}/{repo.name}",
        "delivery_id": delivery_id,
        "pr_number": 5,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "settings_version": 3,
    }
    first = await audit_pipeline.claim_audit_delivery(**payload)
    second = await audit_pipeline.claim_audit_delivery(**payload)
    assert first.claimed is True
    assert second.claimed is False
    assert first.audit_id == second.audit_id


@pytest.mark.asyncio
async def test_replay_with_different_payload_is_a_conflict(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory, "fp-diff-org", "fp-diff-repo")
    delivery_id = f"fp-diff-{uuid.uuid4().hex[:12]}"
    base: dict[str, Any] = {
        "db": as_session(fake_audit_db),
        "repo_id": repo.id,
        "audit_type": "pr_audit",
        "repo_full_name": f"{repo.owner}/{repo.name}",
        "delivery_id": delivery_id,
        "pr_number": 5,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "settings_version": 1,
    }
    await audit_pipeline.claim_audit_delivery(**base)

    for changed in (
        {"head_sha": "c" * 40},
        {"base_sha": "c" * 40},
        {"pr_number": 6},
        {"audit_type": "manual_audit"},
        {"settings_version": 2},
    ):
        with pytest.raises(audit_pipeline.AuditDeliveryConflictError):
            await audit_pipeline.claim_audit_delivery(**{**base, **changed})

    fake_audit_db.expire_all()
    job = await fake_audit_db.scalar(select(AuditJob).where(AuditJob.delivery_id == delivery_id))
    assert job is not None
    assert job.status == "queued"
    assert job.head_sha == "b" * 40


@pytest.mark.asyncio
async def test_webhook_returns_409_when_a_delivery_is_replayed_with_new_content(
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory, "conflict-org", "conflict-repo")
    await set_repo_auditor_settings(fake_audit_db, repo)
    delivery_id = f"conflict-{uuid.uuid4().hex[:12]}"
    first = make_pr_payload(
        "conflict-org", "conflict-repo", action="opened", pr_number=21, head_sha="d" * 40
    )
    second = make_pr_payload(
        "conflict-org", "conflict-repo", action="opened", pr_number=21, head_sha="e" * 40
    )
    for payload in (first, second):
        body = json.dumps(payload).encode("utf-8")
        resp = await client.post(
            "/webhooks/github",
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": delivery_id,
                "X-Hub-Signature-256": sign_payload(TEST_SECRET, body),
            },
            content=body,
        )
    assert resp.status_code == 409
    assert "conflicts" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_dispatch_claim_persists_the_fence_and_clears_it_on_release(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory, "fence-rel-org", "fence-rel-repo")
    audit_id = await persist_audit_job(fake_audit_db, repo, f"fence-rel-{uuid.uuid4().hex}")
    claim = await audit_pipeline.claim_next_queued_job()
    assert claim is not None
    assert claim.audit_id == audit_id
    assert len(claim.dispatch_fence_token) == 64
    fake_audit_db.expire_all()
    job = await fake_audit_db.scalar(select(AuditJob).where(AuditJob.audit_id == audit_id))
    assert job is not None
    assert job.status == "dispatching"
    assert job.dispatch_fence_token == claim.dispatch_fence_token

    await audit_pipeline.release_dispatch_claim(
        claim, RuntimeError("scheduler down")
    )
    fake_audit_db.expire_all()
    job = await fake_audit_db.scalar(select(AuditJob).where(AuditJob.audit_id == audit_id))
    assert job is not None
    assert job.status == "queued"
    assert job.dispatch_fence_token is None


@pytest.mark.asyncio
@pytest.mark.db
async def test_database_rejects_an_unpinned_pending_pull_request_audit(
    db: AsyncSession,
    user_factory,
):
    """The endpoint pin is enforced by the schema, not only by the service.

    Genuine SQL: this asserts the migrated `ck_audit_jobs_pr_endpoints` CHECK
    constraint, so it must run against a real PostgreSQL database.
    """
    await truncate_all(db)
    repo = await seed_repo(db, user_factory, "dbpin-org", "dbpin-repo")
    with pytest.raises(IntegrityError):
        db.add(
            AuditJob(
                audit_id=f"audit-{uuid.uuid4().hex[:12]}",
                repo_id=repo.id,
                delivery_id=f"dbpin-{uuid.uuid4().hex}",
                delivery_fingerprint="a" * 64,
                settings_version=1,
                audit_type="pr_audit",
                pr_number=11,
                head_sha="b" * 40,
                status="queued",
            )
        )
        await db.commit()
    await db.rollback()


@pytest.mark.asyncio
@pytest.mark.db
async def test_database_rejects_a_dispatching_job_without_a_fence(
    db: AsyncSession,
    user_factory,
):
    """Genuine SQL: asserts the migrated `ck_audit_jobs_dispatch_fence` CHECK."""
    await truncate_all(db)
    repo = await seed_repo(db, user_factory, "dbfence-org", "dbfence-repo")
    with pytest.raises(IntegrityError):
        db.add(
            AuditJob(
                audit_id=f"audit-{uuid.uuid4().hex[:12]}",
                repo_id=repo.id,
                delivery_id=f"dbfence-{uuid.uuid4().hex}",
                delivery_fingerprint="b" * 64,
                settings_version=1,
                audit_type="ci_failure_audit",
                head_sha="c" * 40,
                workflow_run_id=42,
                status="dispatching",
                dispatch_fence_token=None,
            )
        )
        await db.commit()
    await db.rollback()


@pytest.mark.asyncio
@pytest.mark.db
async def test_settings_version_is_persisted_with_the_audit_job(
    db: AsyncSession,
    user_factory,
):
    """Genuine SQL: `repo_settings.settings_version` and its server default."""
    await truncate_all(db)
    repo = await seed_repo(db, user_factory, "ver-org", "ver-repo")
    row = await set_repo_auditor_settings(db, repo)
    db.expire_all()
    refreshed = await db.scalar(
        select(RepoSettings).where(RepoSettings.repo_id == repo.id)
    )
    assert refreshed is not None
    assert refreshed.settings_version >= 1
    trigger = await audit_pipeline.get_auditor_trigger(db, repo.id)
    assert trigger.settings_version == refreshed.settings_version
    assert row.settings_version == refreshed.settings_version

# ---------------------------------------------------------------------------
# Phase 5.2: the per-repo kill switch is re-verified before any external work.
# ---------------------------------------------------------------------------


async def _seed_queued_pr_job(
    fake_audit_db: FakeAsyncSession,
    repo: Repo,
) -> str:
    claim = await audit_pipeline.claim_audit_delivery(
        db=as_session(fake_audit_db),
        repo_id=repo.id,
        audit_type="pr_audit",
        repo_full_name=f"{repo.owner}/{repo.name}",
        delivery_id=f"killswitch-{uuid.uuid4().hex}",
        pr_number=5,
        base_sha="a" * 40,
        head_sha="b" * 40,
    )
    assert claim.claimed is True
    return claim.audit_id


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["disabled", "deleted"])
async def test_kill_switch_stops_a_queued_job_before_any_external_work(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
    mode: str,
):
    """A queued job must re-read the persisted trigger before doing anything.

    The trigger is evaluated at intake, but a job can sit queued for minutes
    behind a dispatch batch or a rate-limited GitHub App credential mint. If the
    operator turns Auditor Mode off in that window, the job must not fetch an
    installation token, call GitHub, or spend a token on a model. A *deleted*
    `RepoSettings` row is a deleted configuration and must fail closed exactly
    like a disabled one.
    """
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory, "kill-org", "kill-repo")
    if mode == "disabled":
        await set_repo_auditor_settings(fake_audit_db, repo, enabled=False)
    audit_id = await _seed_queued_pr_job(fake_audit_db, repo)
    # The row really is in the state this case is about.
    stored = await fake_audit_db.scalar(
        select(RepoSettings).where(RepoSettings.repo_id == repo.id)
    )
    assert (stored is None) == (mode == "deleted")
    if stored is not None:
        assert stored.enable_auditor_mode is False


    with (
        patch(
            "app.services.audit_pipeline.get_auditor_installation_token",
            new_callable=AsyncMock,
            side_effect=AssertionError("no credential may be fetched after the kill switch"),
        ) as credentials,
        patch(
            "app.services.audit_pipeline.execute_audit_job",
            new_callable=AsyncMock,
            side_effect=AssertionError("no audit may run after the kill switch"),
        ) as execute,
        patch(
            "app.subagents.auditor.LLMClient.complete",
            new_callable=AsyncMock,
            side_effect=AssertionError("no model may be called after the kill switch"),
        ) as llm,
        patch(
            "app.github_client.fetch_diff",
            new_callable=AsyncMock,
            side_effect=AssertionError("no GitHub read may happen after the kill switch"),
        ) as fetch_diff,
    ):
        claim = await audit_pipeline.claim_next_queued_job()
        assert claim is not None
        assert claim.audit_id == audit_id
        # True, not False: the job reached a terminal state rather than a
        # retryable failure, so the dispatcher must not try it again.
        assert await audit_pipeline.process_audit_job(
            audit_id, claim.dispatch_fence_token
        ) is True

    credentials.assert_not_awaited()
    execute.assert_not_awaited()
    llm.assert_not_awaited()
    fetch_diff.assert_not_awaited()

    fake_audit_db.expire_all()
    job = await fake_audit_db.scalar(select(AuditJob).where(AuditJob.audit_id == audit_id))
    assert job is not None
    assert job.status == "failed"
    assert job.last_error == audit_pipeline.AUDITOR_DISABLED_ERROR
    assert job.dispatch_fence_token is None
    assert job.lease_expires_at is None
    # No processing attempt was ever started.
    assert job.attempts == 0
    # ...and it is never picked up again.
    assert await audit_pipeline.claim_next_queued_job() is None


@pytest.mark.asyncio
async def test_kill_switch_recheck_follows_a_settings_change_after_dispatch(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    """The check is on the persisted row at claim time, not on intake state.

    The job is dispatched while the auditor is enabled, the operator flips the
    kill switch, and only then does the child arrive. The late child must still
    be refused, which is the whole point of re-reading rather than trusting the
    decision the webhook made minutes earlier.
    """
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory, "flip-org", "flip-repo")
    await set_repo_auditor_settings(fake_audit_db, repo, enabled=True)
    audit_id = await _seed_queued_pr_job(fake_audit_db, repo)

    claim = await audit_pipeline.claim_next_queued_job()
    assert claim is not None
    fake_audit_db.expire_all()
    settings_row = await fake_audit_db.scalar(
        select(RepoSettings).where(RepoSettings.repo_id == repo.id)
    )
    assert settings_row is not None
    settings_row.enable_auditor_mode = False
    await fake_audit_db.commit()

    with patch(
        "app.services.audit_pipeline.get_auditor_installation_token",
        new_callable=AsyncMock,
        side_effect=AssertionError("no credential may be fetched after the kill switch"),
    ):
        assert await audit_pipeline.process_audit_job(
            audit_id, claim.dispatch_fence_token
        ) is True

    fake_audit_db.expire_all()
    job = await fake_audit_db.scalar(select(AuditJob).where(AuditJob.audit_id == audit_id))
    assert job is not None
    assert job.status == "failed"
    assert job.last_error == audit_pipeline.AUDITOR_DISABLED_ERROR
    assert job.attempts == 0


@pytest.mark.asyncio
async def test_claim_outcome_keeps_a_refusal_distinct_from_a_superseded_child(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    """`None` and `AUDITOR_DISABLED` are different claims, not the same answer.

    `None` means "you are not the current dispatch attempt, leave the job alone";
    the refusal means "you are, and the job is now terminal". Collapsing them
    would let a stale child look like a successful kill-switch stop, or hide a
    real refusal behind a silent no-op.
    """
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory, "outcome-org", "outcome-repo")
    audit_id = await _seed_queued_pr_job(fake_audit_db, repo)
    claim = await audit_pipeline.claim_next_queued_job()
    assert claim is not None

    # No RepoSettings row at all: a deleted configuration.
    refusal = await audit_pipeline._claim_processing_job(
        audit_id, claim.dispatch_fence_token
    )
    assert refusal is audit_pipeline.ProcessingClaimOutcome.AUDITOR_DISABLED
    assert refusal is not None

    # A job that is not dispatching any more yields None, never the refusal.
    assert await audit_pipeline._claim_processing_job(audit_id, claim.dispatch_fence_token) is None
    assert await audit_pipeline._claim_processing_job(
        "audit-ffffffffffff", claim.dispatch_fence_token
    ) is None

@pytest.mark.asyncio
async def test_kill_switch_lookup_db_error_leaves_job_recoverable_dispatching(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
    monkeypatch: pytest.MonkeyPatch,
):
    """A transient DB fault on settings lookup leaves the job in dispatching state.

    The in-transaction read must not catch DB errors and assume AuditorDisabled.
    A raising lookup must abort the claim attempt, leave the dispatching lease
    intact so outbox recovery can retry it, and never mark the job as terminal failed.
    """
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory, "transient-org", "transient-repo")
    await set_repo_auditor_settings(fake_audit_db, repo, enabled=True)
    audit_id = await _seed_queued_pr_job(fake_audit_db, repo)

    claim = await audit_pipeline.claim_next_queued_job()
    assert claim is not None

    real_scalar = FakeAsyncSession.scalar

    async def flaky_scalar(self, statement, *args, **kwargs):
        if "repo_settings" in str(statement):
            raise RuntimeError("transient db error on settings lookup")
        return await real_scalar(self, statement, *args, **kwargs)

    monkeypatch.setattr(FakeAsyncSession, "scalar", flaky_scalar)
    success = await audit_pipeline.process_audit_job(
        audit_id, claim.dispatch_fence_token
    )
    assert success is False

    monkeypatch.undo()
    fake_audit_db.expire_all()
    job = await fake_audit_db.scalar(select(AuditJob).where(AuditJob.audit_id == audit_id))
    assert job is not None
    assert job.status == "dispatching"
    assert job.last_error != audit_pipeline.AUDITOR_DISABLED_ERROR

