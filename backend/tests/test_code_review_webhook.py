"""
Integration tests for Code Review Webhooks and Orchestration (test_code_review_webhook.py).

Validates:
1. Pull request webhook ingestion (opened, synchronize).
2. Pull request guards (draft, closed, bot sender, haunter fix branch, unregistered repo).
3. Push webhook ingestion.
4. Push guards (tag push, deleted ref, bot author, haunter branch).
5. Orchestration review pipeline:
   - Submits REQUEST_CHANGES when risk_score >= 80.
   - Submits COMMENT when risk_score < 80.
   - Submits commit comment when pr_number is None.
6. DB persistence of CodeReview and API retrieval via /repos/{repo_id}/reviews and /reviews.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch
import uuid
import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import _sign_user_id
from app.config import settings
from app.models import CodeReview, Repo, User
from app.services.review_orchestrator import run_code_review_pipeline

TEST_SECRET = settings.github_webhook_secret or "test_webhook_secret_key_12345"


def sign_payload(secret: str, raw_body: bytes) -> str:
    sig = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def auth_cookie(user: User) -> dict[str, str]:
    signed = _sign_user_id(user.id)
    return {"haunter_session": signed}


@pytest.fixture
async def seeded_repo(db: AsyncSession) -> tuple[User, Repo]:
    user = User(
        github_id=987654321,
        github_username="sentinel-tester",
        role="user",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    repo = Repo(
        user_id=user.id,
        owner="sentinel-org",
        name="sentinel-repo",
        default_branch="main",
    )
    db.add(repo)
    await db.commit()
    await db.refresh(repo)

    return user, repo


@pytest.mark.asyncio
async def test_pr_webhook_opened_success(
    client: httpx.AsyncClient, db: AsyncSession, seeded_repo: tuple[User, Repo]
):
    """pull_request.opened creates pending CodeReview row and schedules pipeline."""
    _, repo = seeded_repo
    payload = {
        "action": "opened",
        "number": 42,
        "pull_request": {
            "number": 42,
            "state": "open",
            "draft": False,
            "head": {
                "ref": "feature/auth-hardening",
                "sha": "1111222233334444555566667777888899990000",
            },
            "user": {"login": "dev-user", "type": "User"},
        },
        "repository": {
            "name": repo.name,
            "owner": {"login": repo.owner},
        },
        "sender": {"login": "dev-user", "type": "User"},
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = sign_payload(TEST_SECRET, raw_body)

    with patch(
        "app.adapters.hosting.AWSHostingAdapter.schedule_review", new_callable=AsyncMock
    ) as mock_sched:
        resp = await client.post(
            "/webhooks/github",
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": str(uuid.uuid4()),
                "X-Hub-Signature-256": sig,
            },
            content=raw_body,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "queued"
        assert data["pr_number"] == 42
        assert mock_sched.called

    # Verify DB persistence
    review_id = uuid.UUID(data["review_id"])
    stmt = select(CodeReview).where(CodeReview.id == review_id)
    review_row = (await db.execute(stmt)).scalars().first()
    assert review_row is not None
    assert review_row.repo_id == repo.id
    assert review_row.commit_sha == "1111222233334444555566667777888899990000"
    assert review_row.pr_number == 42
    assert review_row.status == "pending"


@pytest.mark.asyncio
async def test_pr_webhook_guards(
    client: httpx.AsyncClient, db: AsyncSession, seeded_repo: tuple[User, Repo]
):
    """Draft, closed, bot, and haunter fix branch PRs are cleanly ignored."""
    _, repo = seeded_repo

    # 1. Draft PR
    draft_payload = {
        "action": "opened",
        "pull_request": {
            "number": 10,
            "state": "open",
            "draft": True,
            "head": {"ref": "fix"},
        },
        "repository": {"name": repo.name, "owner": {"login": repo.owner}},
    }
    raw = json.dumps(draft_payload).encode()
    resp = await client.post(
        "/webhooks/github",
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": sign_payload(TEST_SECRET, raw),
        },
        content=raw,
    )
    assert resp.json()["reason"] == "draft PR"

    # 2. Closed PR
    closed_payload = {
        "action": "opened",
        "pull_request": {
            "number": 11,
            "state": "closed",
            "draft": False,
            "head": {"ref": "fix"},
        },
        "repository": {"name": repo.name, "owner": {"login": repo.owner}},
    }
    raw = json.dumps(closed_payload).encode()
    resp = await client.post(
        "/webhooks/github",
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": sign_payload(TEST_SECRET, raw),
        },
        content=raw,
    )
    assert resp.json()["reason"] == "closed PR"

    # 3. Bot PR
    bot_payload = {
        "action": "opened",
        "pull_request": {
            "number": 12,
            "state": "open",
            "draft": False,
            "head": {"ref": "dependabot"},
        },
        "sender": {"login": "dependabot[bot]", "type": "Bot"},
        "repository": {"name": repo.name, "owner": {"login": repo.owner}},
    }
    raw = json.dumps(bot_payload).encode()
    resp = await client.post(
        "/webhooks/github",
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": sign_payload(TEST_SECRET, raw),
        },
        content=raw,
    )
    assert resp.json()["reason"] == "bot PR"

    # 4. Haunter branch PR (feedback loop guard)
    haunter_payload = {
        "action": "opened",
        "pull_request": {
            "number": 13,
            "state": "open",
            "draft": False,
            "head": {"ref": "haunter/fix-test"},
        },
        "repository": {"name": repo.name, "owner": {"login": repo.owner}},
    }
    raw = json.dumps(haunter_payload).encode()
    resp = await client.post(
        "/webhooks/github",
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": sign_payload(TEST_SECRET, raw),
        },
        content=raw,
    )
    assert resp.json()["reason"] == "haunter fix branch"


@pytest.mark.asyncio
async def test_push_webhook_success(
    client: httpx.AsyncClient, db: AsyncSession, seeded_repo: tuple[User, Repo]
):
    """push event creates pending CodeReview row with pr_number=None."""
    _, repo = seeded_repo
    payload = {
        "ref": "refs/heads/main",
        "deleted": False,
        "head_commit": {
            "id": "aaaabbbbccccddddeeeeffff0000111122223333",
            "author": {"name": "Senior Dev", "email": "dev@example.com"},
        },
        "repository": {
            "name": repo.name,
            "owner": {"login": repo.owner},
        },
        "sender": {"login": "dev-user", "type": "User"},
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = sign_payload(TEST_SECRET, raw_body)

    with patch(
        "app.adapters.hosting.AWSHostingAdapter.schedule_review", new_callable=AsyncMock
    ) as mock_sched:
        resp = await client.post(
            "/webhooks/github",
            headers={
                "X-GitHub-Event": "push",
                "X-GitHub-Delivery": str(uuid.uuid4()),
                "X-Hub-Signature-256": sig,
            },
            content=raw_body,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "queued"
        assert mock_sched.called

    review_id = uuid.UUID(data["review_id"])
    stmt = select(CodeReview).where(CodeReview.id == review_id)
    review_row = (await db.execute(stmt)).scalars().first()
    assert review_row is not None
    assert review_row.commit_sha == "aaaabbbbccccddddeeeeffff0000111122223333"
    assert review_row.pr_number is None


@pytest.mark.asyncio
async def test_push_webhook_guards(
    client: httpx.AsyncClient, db: AsyncSession, seeded_repo: tuple[User, Repo]
):
    """Tag pushes, deleted refs, and bot commits are cleanly ignored."""
    _, repo = seeded_repo

    # 1. Tag push
    tag_payload = {
        "ref": "refs/tags/v1.0.0",
        "repository": {"name": repo.name, "owner": {"login": repo.owner}},
    }
    raw = json.dumps(tag_payload).encode()
    resp = await client.post(
        "/webhooks/github",
        headers={
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": sign_payload(TEST_SECRET, raw),
        },
        content=raw,
    )
    assert resp.json()["reason"] == "tag push"

    # 2. Deleted ref
    del_payload = {
        "ref": "refs/heads/feature-branch",
        "deleted": True,
        "repository": {"name": repo.name, "owner": {"login": repo.owner}},
    }
    raw = json.dumps(del_payload).encode()
    resp = await client.post(
        "/webhooks/github",
        headers={
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": sign_payload(TEST_SECRET, raw),
        },
        content=raw,
    )
    assert resp.json()["reason"] == "deleted ref"


@pytest.mark.asyncio
async def test_orchestrator_pipeline_pr_request_changes(
    db: AsyncSession, seeded_repo: tuple[User, Repo]
):
    """When risk_score >= 80, orchestrator submits review with REQUEST_CHANGES event."""
    _, repo = seeded_repo

    review = CodeReview(
        repo_id=repo.id,
        commit_sha="abcd1234abcd1234abcd1234abcd1234abcd1234",
        pr_number=99,
        risk_score=0,
        summary="Pending review",
        findings=[],
        status="pending",
    )
    db.add(review)
    await db.commit()
    await db.refresh(review)

    mock_diff = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,1 +1,2 @@\n"
        " def run():\n"
        "+os.system(user_input)\n"
    )
    mock_llm_response = {
        "content": json.dumps(
            {
                "risk_score": 90,
                "summary": "Severe command injection vulnerability detected.",
                "findings": [
                    {
                        "file_path": "app.py",
                        "line_start": 2,
                        "line_end": 2,
                        "category": "security",
                        "severity": "critical",
                        "critique": "Arbitrary command execution via os.system.",
                        "suggested_patch": "subprocess.run(['safe_bin', shlex.quote(user_input)], check=True)",
                    }
                ],
            }
        ),
        "usage": {"input_tokens": 300, "output_tokens": 120},
    }

    with (
        patch(
            "app.services.review_orchestrator.fetch_pull_request_diff",
            new_callable=AsyncMock,
            return_value=mock_diff,
        ),
        patch("app.subagents.code_reviewer.LLMClient") as mock_llm_cls,
        patch(
            "app.services.review_orchestrator.create_pull_request_review",
            new_callable=AsyncMock,
        ) as mock_pr_review,
        patch(
            "app.services.review_orchestrator.get_installation_token",
            new_callable=AsyncMock,
            return_value="mock-token",
        ),
    ):
        mock_llm = mock_llm_cls.return_value
        mock_llm.complete = AsyncMock(return_value=mock_llm_response)

        await run_code_review_pipeline(review.id)

        assert mock_pr_review.called
        kwargs = mock_pr_review.call_args.kwargs
        assert kwargs["event"] == "REQUEST_CHANGES"
        assert kwargs["pr_number"] == 99
        assert len(kwargs["comments"]) == 1
        assert "```suggestion" in kwargs["comments"][0]["body"]

    # Verify DB update
    db.expire_all()
    stmt = select(CodeReview).where(CodeReview.id == review.id)
    updated = (await db.execute(stmt)).scalars().first()
    assert updated.status == "completed"
    assert updated.risk_score == 90
    assert len(updated.findings) == 1


@pytest.mark.asyncio
async def test_orchestrator_pipeline_pr_comment_and_push_comment(
    db: AsyncSession, seeded_repo: tuple[User, Repo]
):
    """When risk_score < 80, event is COMMENT. When push without PR, create_commit_comment is called."""
    _, repo = seeded_repo

    # 1. PR review with risk_score = 40 -> COMMENT
    pr_review = CodeReview(
        repo_id=repo.id,
        commit_sha="eeee1111eeee1111eeee1111eeee1111eeee1111",
        pr_number=101,
        risk_score=0,
        summary="Pending review",
        findings=[],
        status="pending",
    )
    db.add(pr_review)
    await db.commit()
    await db.refresh(pr_review)

    mock_llm_comment = {
        "content": json.dumps(
            {
                "risk_score": 40,
                "summary": "Minor unhandled edge case in query parsing.",
                "findings": [],
            }
        ),
        "usage": {"input_tokens": 200, "output_tokens": 50},
    }

    with (
        patch(
            "app.services.review_orchestrator.fetch_pull_request_diff",
            new_callable=AsyncMock,
            return_value="diff",
        ),
        patch("app.subagents.code_reviewer.LLMClient") as mock_llm_cls,
        patch(
            "app.services.review_orchestrator.create_pull_request_review",
            new_callable=AsyncMock,
        ) as mock_pr_review,
        patch(
            "app.services.review_orchestrator.get_installation_token",
            new_callable=AsyncMock,
            return_value="mock-token",
        ),
    ):
        mock_llm = mock_llm_cls.return_value
        mock_llm.complete = AsyncMock(return_value=mock_llm_comment)

        await run_code_review_pipeline(pr_review.id)
        assert mock_pr_review.called
        assert mock_pr_review.call_args.kwargs["event"] == "COMMENT"

    # 2. Push review (no PR) -> create_commit_comment
    push_review = CodeReview(
        repo_id=repo.id,
        commit_sha="ffff2222ffff2222ffff2222ffff2222ffff2222",
        pr_number=None,
        risk_score=0,
        summary="Pending review",
        findings=[],
        status="pending",
    )
    db.add(push_review)
    await db.commit()
    await db.refresh(push_review)

    with (
        patch(
            "app.services.review_orchestrator.fetch_diff",
            new_callable=AsyncMock,
            return_value="diff",
        ),
        patch("app.subagents.code_reviewer.LLMClient") as mock_llm_cls,
        patch(
            "app.services.review_orchestrator.create_commit_comment",
            new_callable=AsyncMock,
        ) as mock_commit_comment,
        patch(
            "app.services.review_orchestrator.get_installation_token",
            new_callable=AsyncMock,
            return_value="mock-token",
        ),
    ):
        mock_llm = mock_llm_cls.return_value
        mock_llm.complete = AsyncMock(return_value=mock_llm_comment)

        await run_code_review_pipeline(push_review.id)
        assert mock_commit_comment.called
        assert (
            mock_commit_comment.call_args.kwargs["commit_sha"] == push_review.commit_sha
        )


@pytest.mark.asyncio
async def test_get_reviews_api_endpoints(
    client: httpx.AsyncClient, db: AsyncSession, seeded_repo: tuple[User, Repo]
):
    """GET /repos/{repo_id}/reviews and GET /reviews return scoped results with pagination and filtering."""
    user, repo = seeded_repo

    # Add 2 reviews
    r1 = CodeReview(
        repo_id=repo.id,
        commit_sha="1111111111111111111111111111111111111111",
        pr_number=5,
        risk_score=85,
        summary="Critical vulnerability",
        findings=[
            {
                "file_path": "a.py",
                "line_start": 1,
                "line_end": 2,
                "category": "security",
                "severity": "critical",
                "critique": "Bug",
            }
        ],
        status="completed",
    )
    r2 = CodeReview(
        repo_id=repo.id,
        commit_sha="2222222222222222222222222222222222222222",
        pr_number=6,
        risk_score=20,
        summary="Safe change",
        findings=[],
        status="completed",
    )
    db.add_all([r1, r2])
    await db.commit()

    cookies = auth_cookie(user)

    # 1. GET /repos/{repo.id}/reviews
    resp = await client.get(f"/repos/{repo.id}/reviews", cookies=cookies)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["reviews"]) == 2

    # 2. With min_risk filter >= 50
    resp_risk = await client.get(
        f"/repos/{repo.id}/reviews?min_risk=50", cookies=cookies
    )
    assert resp_risk.status_code == 200
    data_risk = resp_risk.json()
    assert data_risk["total"] == 1
    assert data_risk["reviews"][0]["risk_score"] == 85

    # 3. GET /reviews (across all user repos)
    resp_all = await client.get("/reviews", cookies=cookies)
    assert resp_all.status_code == 200
    assert resp_all.json()["total"] == 2

    # 4. Unowned repo -> 404
    foreign_uuid = uuid.uuid4()
    resp_foreign = await client.get(f"/repos/{foreign_uuid}/reviews", cookies=cookies)
    assert resp_foreign.status_code == 404


@pytest.mark.asyncio
async def test_pr_webhook_duplicate_delivery(
    client: httpx.AsyncClient, db: AsyncSession, seeded_repo: tuple[User, Repo]
):
    """Duplicate PR webhook deliveries for the same commit are deduplicated without creating second review."""
    _, repo = seeded_repo
    payload = {
        "action": "opened",
        "number": 88,
        "pull_request": {
            "number": 88,
            "state": "open",
            "draft": False,
            "head": {
                "ref": "feature/dedup-test",
                "sha": "9999888877776666555544443333222211110000",
            },
            "user": {"login": "dev-user", "type": "User"},
        },
        "repository": {
            "name": repo.name,
            "owner": {"login": repo.owner},
        },
        "sender": {"login": "dev-user", "type": "User"},
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = sign_payload(TEST_SECRET, raw_body)

    with patch(
        "app.adapters.hosting.AWSHostingAdapter.schedule_review", new_callable=AsyncMock
    ) as mock_sched:
        # First delivery
        resp1 = await client.post(
            "/webhooks/github",
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": str(uuid.uuid4()),
                "X-Hub-Signature-256": sig,
            },
            content=raw_body,
        )
        assert resp1.status_code == 200
        assert resp1.json()["status"] == "queued"
        assert mock_sched.call_count == 1

        # Second (duplicate) delivery
        resp2 = await client.post(
            "/webhooks/github",
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": str(uuid.uuid4()),
                "X-Hub-Signature-256": sig,
            },
            content=raw_body,
        )
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "duplicate"
        assert mock_sched.call_count == 1

    # Verify only 1 review row exists in DB
    stmt = (
        select(func.count())
        .select_from(CodeReview)
        .where(
            CodeReview.repo_id == repo.id,
            CodeReview.commit_sha == "9999888877776666555544443333222211110000",
        )
    )
    count = await db.scalar(stmt)
    assert count == 1


@pytest.mark.asyncio
async def test_orchestrator_null_install_app_configured_backfills_and_completes(
    db: AsyncSession, seeded_repo: tuple[User, Repo]
):
    """R1: repo with github_install_id=None but App configured auto-resolves and completes.

    Regression for repos connected before github_install_id was wired into
    POST /repos: the orchestrator must backfill via installation lookup
    instead of falling back to token=None -> "Failed to fetch git diff".
    """
    _, repo = seeded_repo
    assert repo.github_install_id is None
    repo_id = repo.id

    review = CodeReview(
        repo_id=repo_id,
        commit_sha="abcd1234abcd1234abcd1234abcd1234abcd1234",
        pr_number=77,
        risk_score=0,
        summary="Pending review",
        findings=[],
        status="pending",
    )
    db.add(review)
    await db.commit()
    await db.refresh(review)

    mock_llm_response = {
        "content": json.dumps(
            {
                "risk_score": 25,
                "summary": "Clean change, no issues.",
                "findings": [],
            }
        ),
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }

    with (
        patch(
            "app.services.review_orchestrator.fetch_pull_request_diff",
            new_callable=AsyncMock,
            return_value="diff --git a/x.py b/x.py\n+pass",
        ),
        patch("app.subagents.code_reviewer.LLMClient") as mock_llm_cls,
        patch(
            "app.services.review_orchestrator.create_pull_request_review",
            new_callable=AsyncMock,
            return_value={},
        ) as mock_pr_review,
        patch(
            "app.services.review_orchestrator.get_installation_token",
            new_callable=AsyncMock,
            return_value="mock-token",
        ),
        patch(
            "app.services.review_orchestrator.resolve_installation_id",
            new_callable=AsyncMock,
            return_value=555666,
        ),
        patch("app.services.review_orchestrator.settings") as mock_settings,
    ):
        mock_settings.github_app_id = "test-app-id"
        mock_settings.github_app_private_key = "fake-pem"
        mock_llm = mock_llm_cls.return_value
        mock_llm.complete = AsyncMock(return_value=mock_llm_response)

        await run_code_review_pipeline(review.id)

        assert mock_pr_review.called

    db.expire_all()
    stmt = select(CodeReview).where(CodeReview.id == review.id)
    updated = (await db.execute(stmt)).scalars().first()
    assert updated is not None
    assert updated.status == "completed"
    assert updated.failure_reason is None

    # Backfill persisted on the repo row.
    repo_stmt = select(Repo).where(Repo.id == repo_id)
    repo_row = (await db.execute(repo_stmt)).scalars().first()
    assert repo_row is not None
    assert repo_row.github_install_id == 555666


@pytest.mark.asyncio
async def test_orchestrator_publish_failure_sets_error_with_failure_reason(
    db: AsyncSession, seeded_repo: tuple[User, Repo]
):
    """R2: GitHub POST failure must surface as status=error + failure_reason, keeping findings."""
    _, repo = seeded_repo

    review = CodeReview(
        repo_id=repo.id,
        commit_sha="eeee1111eeee1111eeee1111eeee1111eeee1111",
        pr_number=202,
        risk_score=0,
        summary="Pending review",
        findings=[],
        status="pending",
    )
    db.add(review)
    await db.commit()
    await db.refresh(review)

    mock_llm_response = {
        "content": json.dumps(
            {
                "risk_score": 90,
                "summary": "Critical issue found.",
                "findings": [
                    {
                        "file_path": "app.py",
                        "line_start": 1,
                        "line_end": 1,
                        "category": "security",
                        "severity": "critical",
                        "critique": "Command injection.",
                        "suggested_patch": "fix it",
                    }
                ],
            }
        ),
        "usage": {"input_tokens": 300, "output_tokens": 120},
    }

    long_error = "GitHub API returned error 500: " + "x" * 1000
    with (
        patch(
            "app.services.review_orchestrator.fetch_pull_request_diff",
            new_callable=AsyncMock,
            return_value="diff",
        ),
        patch("app.subagents.code_reviewer.LLMClient") as mock_llm_cls,
        patch(
            "app.services.review_orchestrator.create_pull_request_review",
            new_callable=AsyncMock,
            side_effect=Exception(long_error),
        ),
        patch(
            "app.services.review_orchestrator.get_installation_token",
            new_callable=AsyncMock,
            return_value="mock-token",
        ),
    ):
        mock_llm = mock_llm_cls.return_value
        mock_llm.complete = AsyncMock(return_value=mock_llm_response)

        await run_code_review_pipeline(review.id)

    db.expire_all()
    stmt = select(CodeReview).where(CodeReview.id == review.id)
    updated = (await db.execute(stmt)).scalars().first()
    assert updated is not None
    # Publish failure is visible — never silently completed.
    assert updated.status == "error"
    assert updated.failure_reason is not None
    assert len(updated.failure_reason) <= 500
    assert "Failed to publish PR review" in updated.failure_reason
    # Findings and summary are preserved for the dashboard.
    assert len(updated.findings) == 1
    assert updated.summary == "Critical issue found."
    assert updated.risk_score == 90


@pytest.mark.asyncio
async def test_orchestrator_422_inline_fallback_drops_comments_keeps_summary(
    db: AsyncSession, seeded_repo: tuple[User, Repo]
):
    """R2: invalid inline 422 comments are dropped, summary-only retry still completes."""
    _, repo = seeded_repo

    review = CodeReview(
        repo_id=repo.id,
        commit_sha="ffff3333ffff3333ffff3333ffff3333ffff3333",
        pr_number=203,
        risk_score=0,
        summary="Pending review",
        findings=[],
        status="pending",
    )
    db.add(review)
    await db.commit()
    await db.refresh(review)

    mock_llm_response = {
        "content": json.dumps(
            {
                "risk_score": 85,
                "summary": "Risky change.",
                "findings": [
                    {
                        "file_path": "app.py",
                        "line_start": 1,
                        "line_end": 5,
                        "category": "security",
                        "severity": "high",
                        "critique": "Review this range.",
                        "suggested_patch": "patch here",
                    }
                ],
            }
        ),
        "usage": {"input_tokens": 200, "output_tokens": 80},
    }

    calls: list[dict] = []

    async def _fake_post_review(**kwargs):  # type: ignore[no-untyped-def]
        calls.append(kwargs)
        if kwargs.get("comments"):
            raise Exception("GitHub API returned error 422: inline comment line out of range")
        return {"id": 1}

    mock_diff = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -0,0 +1,5 @@\n"
        "+line 1\n"
        "+line 2\n"
        "+line 3\n"
        "+line 4\n"
        "+line 5\n"
    )

    with (
        patch(
            "app.services.review_orchestrator.fetch_pull_request_diff",
            new_callable=AsyncMock,
            return_value=mock_diff,
        ),
        patch("app.subagents.code_reviewer.LLMClient") as mock_llm_cls,
        patch(
            "app.services.review_orchestrator.create_pull_request_review",
            new_callable=AsyncMock,
            side_effect=_fake_post_review,
        ),
        patch(
            "app.services.review_orchestrator.get_installation_token",
            new_callable=AsyncMock,
            return_value="mock-token",
        ),
    ):
        mock_llm = mock_llm_cls.return_value
        mock_llm.complete = AsyncMock(return_value=mock_llm_response)

        await run_code_review_pipeline(review.id)

    # First attempt with inline comments, second attempt summary-only.
    assert len(calls) == 2
    assert len(calls[0]["comments"]) == 1
    assert calls[1]["comments"] == []

    db.expire_all()
    stmt = select(CodeReview).where(CodeReview.id == review.id)
    updated = (await db.execute(stmt)).scalars().first()
    assert updated is not None
    assert updated.status == "completed"
    assert updated.failure_reason is None
    assert len(updated.findings) == 1
