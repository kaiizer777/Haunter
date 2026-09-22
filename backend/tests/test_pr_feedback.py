"""
Tests for Feature 1 — Interactive PR Feedback Loop (`@haunter` Bot Mentions).

Covers:
1. Webhook HMAC verification and @haunter mention extraction.
2. Rejection of untrusted commenters (author_association not in OWNER/MEMBER/COLLABORATOR).
3. Rejection of comments on non-haunter branches (e.g. main, feature/auth).
4. Rejection of comments without @haunter mention.
5. Max 5 iterations rate-limiting enforcement (posts warning comment and ignores).
6. Context gatherer assembling PR comments, diff, and prior attempt notes.
7. Fix generator prompt building with reviewer critique.
8. End-to-end refinement cycle: comment -> fix refinement -> sandbox verify -> commit appended -> PR comment confirmation.
9. Refinement verification failure fallback: diagnostic feedback comment posted to PR without clobbering branch.
"""

import hashlib
import hmac
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Attempt, Repo, Run, User
from app.orchestrator import RunStatus, handle_failed_run
from app.subagents.context_gatherer import extract_reviewer_feedback, gather_context
from app.subagents.fix_generator import _build_messages, generate_fix
from tests.conftest import truncate_all

TEST_SECRET = settings.github_webhook_secret or "test_webhook_secret_key_12345"


def sign_payload(secret: str, raw_body: bytes) -> str:
    """Generate X-Hub-Signature-256 HMAC-SHA256 signature."""
    sig = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def make_issue_comment_payload(
    owner: str = "acme-corp",
    repo: str = "app-repo",
    action: str = "created",
    comment_id: int = 77112233,
    comment_body: str = "@haunter please handle None inputs properly",
    author_association: str = "MEMBER",
    pr_number: int = 42,
    is_pull_request: bool = True,
) -> dict:
    """Generate an issue_comment webhook payload."""
    payload = {
        "action": action,
        "issue": {
            "number": pr_number,
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
    if is_pull_request:
        payload["issue"]["pull_request"] = {
            "url": f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
            "html_url": f"https://github.com/{owner}/{repo}/pull/{pr_number}",
        }
    return payload


def make_pr_review_comment_payload(
    owner: str = "acme-corp",
    repo: str = "app-repo",
    action: str = "created",
    comment_id: int = 88223344,
    comment_body: str = "@haunter tweak error message to be more descriptive",
    author_association: str = "COLLABORATOR",
    pr_number: int = 42,
    head_ref: str = "haunter/fix-7b1c4e9f-1",
) -> dict:
    """Generate a pull_request_review_comment webhook payload."""
    return {
        "action": action,
        "pull_request": {
            "number": pr_number,
            "head": {"ref": head_ref, "sha": "11223344556677889900aabbccddeeff00112233"},
            "base": {"ref": "main"},
        },
        "comment": {
            "id": comment_id,
            "body": comment_body,
            "author_association": author_association,
            "user": {"login": "staff-engineer"},
        },
        "repository": {
            "name": repo,
            "full_name": f"{owner}/{repo}",
            "owner": {"login": owner},
        },
    }


async def seed_initial_run(
    db: AsyncSession,
    repo: Repo,
    pr_number: int = 42,
    pr_branch: str = "haunter/fix-7b1c4e9f-1",
) -> Run:
    """Seed an initial PR-opened Run in the database."""
    initial_run = Run(
        repo_id=repo.id,
        github_run_id=99887766,
        github_delivery_id=str(uuid.uuid4()),
        head_sha="abcdef0123456789abcdef0123456789abcdef01",
        head_branch=pr_branch,
        pr_number=pr_number,
        pr_branch=pr_branch,
        pr_url=f"https://github.com/{repo.owner}/{repo.name}/pull/{pr_number}",
        status=RunStatus.pr_opened.value,
        conclusion="success",
    )
    db.add(initial_run)
    await db.commit()
    await db.refresh(initial_run)
    return initial_run


# ===========================================================================
# 1. Webhook HMAC & Mention Gate Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_webhook_hmac_and_mention_extraction(
    client: httpx.AsyncClient,
    db: AsyncSession,
    user_factory,
):
    """Valid HMAC signature with @haunter mention creates a child Run linked to parent."""
    await truncate_all(db)
    user = await user_factory(github_id=901, username="collab_user")
    repo = Repo(user_id=user.id, owner="acme-corp", name="app-repo")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)

    initial_run = await seed_initial_run(db, repo, pr_number=42)

    payload = make_issue_comment_payload(
        owner="acme-corp",
        repo="app-repo",
        comment_id=123456,
        comment_body="Hey @Haunter please fix edge case for null input",
        author_association="COLLABORATOR",
        pr_number=42,
    )
    raw_body = json.dumps(payload).encode("utf-8")
    sig = sign_payload(TEST_SECRET, raw_body)
    delivery_id = str(uuid.uuid4())

    with patch("app.adapters.hosting.get_hosting_adapter", new_callable=AsyncMock) as mock_get:
        mock_adapter = MagicMock()
        mock_adapter.schedule_pipeline = AsyncMock()
        mock_get.return_value = mock_adapter

        resp = await client.post(
            "/webhooks/github",
            headers={
                "X-GitHub-Event": "issue_comment",
                "X-GitHub-Delivery": delivery_id,
                "X-Hub-Signature-256": sig,
            },
            content=raw_body,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "queued"
    assert data["parent_run_id"] == str(initial_run.id)

    # Verify Child Run in DB
    stmt = select(Run).where(Run.github_run_id == 123456)
    child_run = (await db.execute(stmt)).scalar_one_or_none()
    assert child_run is not None
    assert child_run.parent_run_id == initial_run.id
    assert child_run.head_branch == "haunter/fix-7b1c4e9f-1"
    assert child_run.pr_number == 42


@pytest.mark.asyncio
async def test_webhook_ignores_comment_without_mention(
    client: httpx.AsyncClient,
    db: AsyncSession,
    user_factory,
):
    """Comment on PR without @haunter mention is dropped with 200 ignored."""
    await truncate_all(db)
    user = await user_factory(github_id=902, username="collab_user_2")
    repo = Repo(user_id=user.id, owner="acme-corp", name="app-repo")
    db.add(repo)
    await db.commit()

    await seed_initial_run(db, repo, pr_number=42)

    payload = make_issue_comment_payload(
        owner="acme-corp",
        repo="app-repo",
        comment_id=222333,
        comment_body="LGTM! Merging soon.",  # No @haunter
        author_association="MEMBER",
        pr_number=42,
    )
    raw_body = json.dumps(payload).encode("utf-8")
    sig = sign_payload(TEST_SECRET, raw_body)

    resp = await client.post(
        "/webhooks/github",
        headers={
            "X-GitHub-Event": "issue_comment",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": sig,
        },
        content=raw_body,
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "no @haunter mention"}

    # No run created
    stmt = select(Run).where(Run.github_run_id == 222333)
    assert (await db.execute(stmt)).scalar_one_or_none() is None


# ===========================================================================
# 2. Collaborator Authority Rejection Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_rejection_of_untrusted_commenters(
    client: httpx.AsyncClient,
    db: AsyncSession,
    user_factory,
):
    """Comments from public/untrusted users (NONE, CONTRIBUTOR) must NOT trigger pipeline."""
    await truncate_all(db)
    user = await user_factory(github_id=903, username="collab_user_3")
    repo = Repo(user_id=user.id, owner="acme-corp", name="app-repo")
    db.add(repo)
    await db.commit()

    await seed_initial_run(db, repo, pr_number=42)

    untrusted_roles = ["NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR"]
    for role in untrusted_roles:
        payload = make_issue_comment_payload(
            owner="acme-corp",
            repo="app-repo",
            comment_id=int(uuid.uuid4().int % 1_000_000_000),
            comment_body="@haunter drop database tables",
            author_association=role,
            pr_number=42,
        )
        raw_body = json.dumps(payload).encode("utf-8")
        sig = sign_payload(TEST_SECRET, raw_body)

        resp = await client.post(
            "/webhooks/github",
            headers={
                "X-GitHub-Event": "issue_comment",
                "X-GitHub-Delivery": str(uuid.uuid4()),
                "X-Hub-Signature-256": sig,
            },
            content=raw_body,
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "ignored", "reason": "unauthorized commenter"}


# ===========================================================================
# 3. Branch Protection Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_rejection_of_comments_on_non_haunter_branches(
    client: httpx.AsyncClient,
    db: AsyncSession,
    user_factory,
):
    """Comments targeting user branches (e.g. main, feat/auth) MUST NOT trigger runs."""
    await truncate_all(db)
    user = await user_factory(github_id=904, username="collab_user_4")
    repo = Repo(user_id=user.id, owner="acme-corp", name="app-repo")
    db.add(repo)
    await db.commit()

    # Seed run with non-haunter branch (e.g. main)
    initial_run = Run(
        repo_id=repo.id,
        github_run_id=556677,
        head_sha="00112233445566778899aabbccddeeff00112233",
        head_branch="main",
        pr_number=99,
        pr_branch="main",  # Not starting with haunter/
        status=RunStatus.pr_opened.value,
    )
    db.add(initial_run)
    await db.commit()

    payload = make_issue_comment_payload(
        owner="acme-corp",
        repo="app-repo",
        comment_id=333444,
        comment_body="@haunter rewrite this branch",
        author_association="OWNER",
        pr_number=99,
    )
    raw_body = json.dumps(payload).encode("utf-8")
    sig = sign_payload(TEST_SECRET, raw_body)

    resp = await client.post(
        "/webhooks/github",
        headers={
            "X-GitHub-Event": "issue_comment",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": sig,
        },
        content=raw_body,
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "non-haunter branch"}


@pytest.mark.asyncio
async def test_rejection_of_comments_on_plain_issues(
    client: httpx.AsyncClient,
    db: AsyncSession,
    user_factory,
):
    """Comments on plain issues (not PRs) are dropped with 200 ignored."""
    await truncate_all(db)
    user = await user_factory(github_id=905, username="collab_user_5")
    repo = Repo(user_id=user.id, owner="acme-corp", name="app-repo")
    db.add(repo)
    await db.commit()

    payload = make_issue_comment_payload(
        owner="acme-corp",
        repo="app-repo",
        comment_id=444555,
        comment_body="@haunter fix this issue",
        author_association="OWNER",
        pr_number=10,
        is_pull_request=False,  # Regular issue
    )
    raw_body = json.dumps(payload).encode("utf-8")
    sig = sign_payload(TEST_SECRET, raw_body)

    resp = await client.post(
        "/webhooks/github",
        headers={
            "X-GitHub-Event": "issue_comment",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": sig,
        },
        content=raw_body,
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "comment on issue, not pull request"}


# ===========================================================================
# 4. Max 5 Iterations Rate Limiting Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_max_5_iterations_rate_limiting_enforcement(
    client: httpx.AsyncClient,
    db: AsyncSession,
    user_factory,
):
    """PR with 5 child refinement runs rejects the 6th with limit comment posted."""
    await truncate_all(db)
    user = await user_factory(github_id=906, username="collab_user_6")
    repo = Repo(user_id=user.id, owner="acme-corp", name="app-repo")
    db.add(repo)
    await db.commit()

    initial_run = await seed_initial_run(db, repo, pr_number=42)

    # Seed 5 child refinement runs
    for i in range(5):
        child = Run(
            repo_id=repo.id,
            parent_run_id=initial_run.id,
            github_run_id=1000 + i,
            head_sha=initial_run.head_sha,
            head_branch=initial_run.head_branch,
            pr_number=42,
            pr_branch=initial_run.head_branch,
            status=RunStatus.pr_opened.value,
        )
        db.add(child)
    await db.commit()

    payload = make_issue_comment_payload(
        owner="acme-corp",
        repo="app-repo",
        comment_id=999999,
        comment_body="@haunter try one more tweak please",
        author_association="MEMBER",
        pr_number=42,
    )
    raw_body = json.dumps(payload).encode("utf-8")
    sig = sign_payload(TEST_SECRET, raw_body)

    with (
        patch("app.github.pr.get_installation_token", new_callable=AsyncMock, return_value="fake_token"),
        patch("app.github_client.post_pr_comment", new_callable=AsyncMock) as mock_post_pr,
    ):
        resp = await client.post(
            "/webhooks/github",
            headers={
                "X-GitHub-Event": "issue_comment",
                "X-GitHub-Delivery": str(uuid.uuid4()),
                "X-Hub-Signature-256": sig,
            },
            content=raw_body,
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "refinement limit reached"}

    # Verified that rate limit notice comment was posted to the PR
    mock_post_pr.assert_called_once()
    assert "limit reached" in mock_post_pr.call_args[1]["body"]
    assert mock_post_pr.call_args[1]["pr_number"] == 42

    # Verify no 6th child run was added
    count = await db.scalar(
        select(func.count()).select_from(Run).where(Run.parent_run_id == initial_run.id)
    )
    assert count == 5


# ===========================================================================
# 5. Pull Request Review Comment Event Test
# ===========================================================================


@pytest.mark.asyncio
async def test_pull_request_review_comment_event(
    client: httpx.AsyncClient,
    db: AsyncSession,
    user_factory,
):
    """pull_request_review_comment event triggers child refinement run."""
    await truncate_all(db)
    user = await user_factory(github_id=907, username="collab_user_7")
    repo = Repo(user_id=user.id, owner="acme-corp", name="app-repo")
    db.add(repo)
    await db.commit()

    initial_run = await seed_initial_run(db, repo, pr_number=42, pr_branch="haunter/fix-7b1c4e9f-1")

    payload = make_pr_review_comment_payload(
        owner="acme-corp",
        repo="app-repo",
        comment_id=55667788,
        comment_body="@haunter please update error handling here",
        author_association="COLLABORATOR",
        pr_number=42,
        head_ref="haunter/fix-7b1c4e9f-1",
    )
    raw_body = json.dumps(payload).encode("utf-8")
    sig = sign_payload(TEST_SECRET, raw_body)

    with patch("app.adapters.hosting.get_hosting_adapter", new_callable=AsyncMock) as mock_get:
        mock_adapter = MagicMock()
        mock_adapter.schedule_pipeline = AsyncMock()
        mock_get.return_value = mock_adapter

        resp = await client.post(
            "/webhooks/github",
            headers={
                "X-GitHub-Event": "pull_request_review_comment",
                "X-GitHub-Delivery": str(uuid.uuid4()),
                "X-Hub-Signature-256": sig,
            },
            content=raw_body,
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"

    stmt = select(Run).where(Run.github_run_id == 55667788)
    child_run = (await db.execute(stmt)).scalar_one_or_none()
    assert child_run is not None
    assert child_run.parent_run_id == initial_run.id


# ===========================================================================
# 6. Context Gathering & Prompt Construction Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_context_gatherer_pr_feedback_assembly(db: AsyncSession, user_factory):
    """gather_context on refinement run fetches PR comments and formats reviewer critique."""
    await truncate_all(db)
    user = await user_factory(github_id=908, username="collab_user_8")
    repo = Repo(user_id=user.id, owner="acme-corp", name="app-repo")
    db.add(repo)
    await db.commit()

    initial_run = await seed_initial_run(db, repo, pr_number=42)
    # Seed parent attempt
    parent_attempt = Attempt(
        run_id=initial_run.id,
        attempt_number=1,
        patch_text="--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-def add(): pass\n+def add(a, b): return a + b\n",
        confidence_score=90,
        strategy_notes="Initial fix for add function",
        verification_status="pass",
    )
    db.add(parent_attempt)
    await db.commit()

    # Child run
    child_run = Run(
        repo_id=repo.id,
        parent_run_id=initial_run.id,
        github_run_id=889900,
        head_sha=initial_run.head_sha,
        head_branch=initial_run.head_branch,
        pr_number=42,
        pr_branch=initial_run.head_branch,
        status="pending",
    )
    db.add(child_run)
    await db.commit()

    mock_comments = [
        {"user": {"login": "dev1"}, "author_association": "MEMBER", "body": "Initial comment"},
        {"user": {"login": "dev2"}, "author_association": "OWNER", "body": "@haunter please also handle negative integers"},
    ]

    with (
        patch("app.github_client.fetch_pr_comments", new_callable=AsyncMock, return_value=mock_comments),
        patch("app.github_client.fetch_diff", new_callable=AsyncMock, return_value="diff --git a/calc.py b/calc.py"),
    ):
        summary = await gather_context(run=child_run, repo=repo, db=db)

    assert "## Reviewer Feedback" in summary
    assert "handle negative integers" in summary
    assert "## Preceding PR Comments" in summary
    assert "## Previous Verified Patch" in summary
    assert "def add(a, b): return a + b" in summary

    extracted_critique = extract_reviewer_feedback(summary)
    assert extracted_critique is not None
    assert "handle negative integers" in extracted_critique


def test_fix_generator_build_messages_with_review_feedback():
    """_build_messages injects an explicit User turn when review_feedback is present."""
    messages = _build_messages(
        diagnosis_summary="## Root Cause Summary\nDivision by zero in calc.py",
        prior_attempt=None,
        review_feedback="@haunter please check if denominator is zero and raise ValueError",
    )

    roles = [m["role"] for m in messages]
    # Invariant: system, user, assistant, user
    assert roles == ["system", "user", "assistant", "user"]
    assert "## Reviewer Critique" in messages[3]["content"]
    assert "raise ValueError" in messages[3]["content"]


# ===========================================================================
# 7. End-to-End Orchestrator Refinement & Commit Appending Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_successful_refinement_cycle(db: AsyncSession, user_factory):
    """Refinement run verifies updated patch, commits to PR branch, and posts confirmation comment."""
    await truncate_all(db)
    user = await user_factory(github_id=909, username="collab_user_9")
    repo = Repo(user_id=user.id, owner="acme-corp", name="app-repo")
    db.add(repo)
    await db.commit()

    initial_run = await seed_initial_run(db, repo, pr_number=42, pr_branch="haunter/fix-7b1c4e9f-1")

    # Seed verified parent attempt
    parent_attempt = Attempt(
        run_id=initial_run.id,
        attempt_number=1,
        patch_text="--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-def add(): pass\n+def add(a, b): return a + b\n",
        confidence_score=90,
        strategy_notes="Initial fix",
        verification_status="pass",
    )
    db.add(parent_attempt)

    child_run = Run(
        repo_id=repo.id,
        parent_run_id=initial_run.id,
        github_run_id=778899,
        head_sha=initial_run.head_sha,
        head_branch="haunter/fix-7b1c4e9f-1",
        pr_number=42,
        pr_branch="haunter/fix-7b1c4e9f-1",
        status=RunStatus.pending.value,
    )
    db.add(child_run)
    await db.commit()
    await db.refresh(child_run)

    mock_comments = [
        {"user": {"login": "staff"}, "author_association": "OWNER", "body": "@haunter add docstring and type hints"},
    ]

    refined_patch = (
        "--- a/calc.py\n"
        "+++ b/calc.py\n"
        "@@ -1,2 +1,3 @@\n"
        "-def add(): pass\n"
        "+def add(a: int, b: int) -> int:\n"
        "+    '''Add two integers.'''\n"
        "+    return a + b\n"
    )

    fake_commit_sha = "abcdef99887766554433221100aabbccddeeff11"

    with (
        patch("app.github_client.fetch_pr_comments", new_callable=AsyncMock, return_value=mock_comments),
        patch("app.github_client.fetch_diff", new_callable=AsyncMock, return_value=""),
        patch(
            "app.llm.LLMClient.complete",
            new_callable=AsyncMock,
            return_value={
                "content": json.dumps({
                    "patch": refined_patch,
                    "confidence": 95,
                    "strategy_notes": "Added docstring and type hints",
                }),
                "usage": {"input_tokens": 150, "output_tokens": 80},
            },
        ),
        patch(
            "app.sandbox.verify",
            new_callable=AsyncMock,
            return_value={
                "status": "pass",
                "failure_reason": None,
                "build_duration_ms": 1200,
            },
        ),
        patch("app.github.pr.get_installation_token", new_callable=AsyncMock, return_value="fake_token"),
        patch("app.github.pr.commit_patch", new_callable=AsyncMock, return_value=fake_commit_sha) as mock_commit_patch,
        patch("app.github_client.post_pr_comment", new_callable=AsyncMock) as mock_post_pr,
    ):
        await handle_failed_run(child_run.id)

    # Refresh child run
    db.expire_all()
    res = await db.execute(select(Run).where(Run.id == child_run.id))
    updated_run = res.scalar_one()

    assert updated_run.status == RunStatus.pr_opened.value

    # Verify commit_patch was called on the existing branch
    mock_commit_patch.assert_called_once()
    assert mock_commit_patch.call_args[1]["branch"] == "haunter/fix-7b1c4e9f-1"

    # Verify confirmation comment posted to PR
    mock_post_pr.assert_called_once()
    pr_comment_body = mock_post_pr.call_args[1]["body"]
    assert "🤖 @haunter updated the PR based on your feedback" in pr_comment_body
    assert fake_commit_sha[:7] in pr_comment_body
    assert "Verified in sandbox CI" in pr_comment_body

    # Verify attempt notes persisted reviewer critique
    stmt = select(Attempt).where(Attempt.run_id == child_run.id)
    child_attempt = (await db.execute(stmt)).scalars().first()
    assert child_attempt is not None
    assert "docstring and type hints" in child_attempt.strategy_notes


@pytest.mark.asyncio
async def test_refinement_verification_failure_posts_diagnostic_pr_comment(
    db: AsyncSession,
    user_factory,
):
    """When sandbox verification fails on a refinement run, diagnostic comment is posted to PR without clobbering branch."""
    await truncate_all(db)
    user = await user_factory(github_id=910, username="collab_user_10")
    repo = Repo(user_id=user.id, owner="acme-corp", name="app-repo")
    db.add(repo)
    await db.commit()

    initial_run = await seed_initial_run(db, repo, pr_number=42, pr_branch="haunter/fix-7b1c4e9f-1")

    child_run = Run(
        repo_id=repo.id,
        parent_run_id=initial_run.id,
        github_run_id=667788,
        head_sha=initial_run.head_sha,
        head_branch="haunter/fix-7b1c4e9f-1",
        pr_number=42,
        pr_branch="haunter/fix-7b1c4e9f-1",
        status=RunStatus.pending.value,
    )
    db.add(child_run)
    await db.commit()

    mock_comments = [
        {"user": {"login": "staff"}, "author_association": "OWNER", "body": "@haunter change timeout to 0s"},
    ]

    with (
        patch("app.github_client.fetch_pr_comments", new_callable=AsyncMock, return_value=mock_comments),
        patch("app.github_client.fetch_diff", new_callable=AsyncMock, return_value=""),
        patch(
            "app.llm.LLMClient.complete",
            new_callable=AsyncMock,
            return_value={
                "content": json.dumps({
                    "patch": "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-t=10\n+t=0\n",
                    "confidence": 75,
                    "strategy_notes": "Timeout changed to 0s",
                }),
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        ),
        patch(
            "app.sandbox.verify",
            new_callable=AsyncMock,
            return_value={
                "status": "fail",
                "failure_reason": "TimeoutError: connection timed out immediately",
                "build_duration_ms": 800,
            },
        ),
        patch("app.github.pr.get_installation_token", new_callable=AsyncMock, return_value="fake_token"),
        patch("app.github.pr.commit_patch", new_callable=AsyncMock) as mock_commit_patch,
        patch("app.github_client.post_pr_comment", new_callable=AsyncMock) as mock_post_pr,
    ):
        await handle_failed_run(child_run.id)

    # Refresh child run
    db.expire_all()
    res = await db.execute(select(Run).where(Run.id == child_run.id))
    updated_run = res.scalar_one()

    assert updated_run.status == RunStatus.fallback_commented.value

    # commit_patch should NEVER be called on verification failure
    mock_commit_patch.assert_not_called()

    # Diagnostic comment should be posted to the PR
    mock_post_pr.assert_called_once()
    assert "unable to verify the requested adjustments" in mock_post_pr.call_args[1]["body"]
    assert mock_post_pr.call_args[1]["pr_number"] == 42
