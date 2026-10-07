"""
Unit tests for the PR Review Auto-Fix Execution feature
(`app.webhooks._handle_review_auto_fix_command`, Branch B).

Validates:
  1. Remediation diff in DB CodeReview -> commits patch, posts comment with commit link.
  2. Remediation diff in GitHub review (e.g. haunter-auditor[bot]) -> extracts diff, commits patch, posts comment.
  3. No remediation diff exists -> posts clean informative comment without error.
  4. Error handling when commit_patch fails (e.g. branch protection / conflicts) -> posts clean error comment.
  5. Resolves head branch via fetch_pull_request when not in payload (issue_comment).
  6. Supports both `@haunter fix` and `@haunter address`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.config import settings
from app.github.pr import GitHubPRError
from app.models import CodeReview, Repo
from tests.fake_audit_db import (  # noqa: F401
    FakeAsyncSession,
    FakeStore,
    audit_store,
    fake_audit_db,
    fake_audit_user_factory,
)

TEST_SECRET = settings.github_webhook_secret or "test_webhook_secret_key_12345"

OWNER = "auto-fix-org"
REPO = "auto-fix-repo"
USER_BRANCH = "feat/my-feature"
PR_NUMBER = 77
TRIGGER_COMMENT_ID = 998811


@pytest.fixture(autouse=True)
def deny_external_http(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Prevent tests from making unintended real HTTP calls."""
    original_send = httpx.AsyncClient.send

    async def guarded_send(self, request, **kwargs):
        if isinstance(self._transport, httpx.ASGITransport):
            return await original_send(self, request, **kwargs)
        raise AssertionError(f"external HTTP blocked: {request.url.host}")

    monkeypatch.setattr(httpx.AsyncClient, "send", guarded_send)
    yield


def signed(secret: str, payload: dict[str, Any]) -> str:
    digest = hmac.new(
        secret.encode("utf-8"), json.dumps(payload).encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"sha256={digest}"


def issue_comment_payload(
    *,
    body: str = "@haunter fix",
    comment_id: int = TRIGGER_COMMENT_ID,
    author_association: str = "MEMBER",
    pr_number: int = PR_NUMBER,
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
            "user": {"login": "dev-engineer"},
        },
        "repository": {
            "name": repo,
            "full_name": f"{owner}/{repo}",
            "owner": {"login": owner},
        },
    }


def review_comment_payload(
    *,
    body: str = "@haunter fix",
    comment_id: int = TRIGGER_COMMENT_ID,
    author_association: str = "MEMBER",
    pr_number: int = PR_NUMBER,
    head_ref: str = USER_BRANCH,
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
            "user": {"login": "dev-engineer"},
        },
        "repository": {
            "name": repo,
            "full_name": f"{owner}/{repo}",
            "owner": {"login": owner},
        },
    }


async def seed_repo(
    session: FakeAsyncSession,
    user_factory,
    *,
    owner: str = OWNER,
    repo_name: str = REPO,
) -> Repo:
    user = await user_factory(
        github_id=int(uuid.uuid4().int % 1_000_000_000 + 400_000_000),
        username=f"fix-user-{uuid.uuid4().hex[:6]}",
    )
    repo = Repo(user_id=user.id, owner=owner, name=repo_name)
    session.add(repo)
    await session.commit()
    return repo


SAMPLE_DIFF = """--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,2 @@
-def add(a, b): return a - b
+def add(a, b): return a + b
"""


def test_extract_diff_direct():
    from app.webhooks import _extract_remediation_diff_from_body

    body = f"""### 🛠️ Remediation Unified Diff
```diff
{SAMPLE_DIFF}
```
"""
    res = _extract_remediation_diff_from_body(body)
    assert res is not None
    assert "def add(a, b)" in res


@pytest.mark.asyncio
async def test_auto_fix_applies_patch_from_code_review_db(
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    """When a CodeReview in DB has suggested_patch, @haunter fix commits it and posts comment."""
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory)

    code_review = CodeReview(
        repo_id=repo.id,
        commit_sha="a" * 40,
        pr_number=PR_NUMBER,
        risk_score=75,
        summary="Bug in add function",
        findings=[
            {
                "file_path": "calculator.py",
                "line_start": 1,
                "line_end": 2,
                "title": "Wrong operator",
                "critique": "Subtraction instead of addition",
                "suggested_patch": SAMPLE_DIFF,
            }
        ],
        status="completed",
    )
    fake_audit_db.add(code_review)
    await fake_audit_db.commit()

    payload = review_comment_payload(body="@haunter fix", head_ref=USER_BRANCH)

    with (
        patch("app.github.pr.get_installation_token", new_callable=AsyncMock) as mock_tok,
        patch("app.github.pr.commit_patch", new_callable=AsyncMock) as mock_commit,
        patch("app.github_client.create_issue_comment", new_callable=AsyncMock) as mock_comment,
    ):
        mock_tok.return_value = "ghs_mock_token_123"
        mock_commit.return_value = "commit_sha_new_456"
        mock_comment.return_value = {"id": 12345}

        resp = await client.post(
            "/webhooks/github",
            content=json.dumps(payload).encode("utf-8"),
            headers={
                "X-GitHub-Event": "pull_request_review_comment",
                "X-GitHub-Delivery": str(uuid.uuid4()),
                "X-Hub-Signature-256": signed(TEST_SECRET, payload),
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "applied"
    assert data["commit_sha"] == "commit_sha_new_456"

    mock_commit.assert_called_once()
    assert mock_commit.call_args.kwargs["branch"] == USER_BRANCH
    assert mock_commit.call_args.kwargs["patch_text"] == SAMPLE_DIFF

    mock_comment.assert_called_once()
    comment_body = mock_comment.call_args.kwargs["body"]
    assert "Applied suggested remediation" in comment_body
    assert "commit_sha_new_456" in comment_body
    assert USER_BRANCH in comment_body


@pytest.mark.asyncio
async def test_auto_fix_applies_patch_from_github_audit_review(
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    """When DB has no CodeReview, diff is extracted from haunter-auditor[bot] PR review."""
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory)

    review_body = f"""### 🔍 Executive Summary
Security issue found.

---

### 🚨 Findings & Recommendations
#### 1. ⛔ Issue in logic

---

### 🛠️ Remediation Unified Diff
```diff
{SAMPLE_DIFF}
```
*Generated autonomously by Haunter Guardian Mode. Zero changes were committed to your branch.*
"""

    mock_gh_reviews = [
        {
            "id": 9991,
            "user": {"login": "haunter-auditor[bot]"},
            "body": review_body,
            "state": "COMMENTED",
        }
    ]

    payload = review_comment_payload(body="@haunter address", head_ref=USER_BRANCH)

    with (
        patch("app.github.pr.get_installation_token", new_callable=AsyncMock) as mock_tok,
        patch("app.github_client.fetch_pull_request_reviews", new_callable=AsyncMock) as mock_reviews,
        patch("app.github.pr.commit_patch", new_callable=AsyncMock) as mock_commit,
        patch("app.github_client.create_issue_comment", new_callable=AsyncMock) as mock_comment,
    ):
        mock_tok.return_value = "ghs_mock_token_123"
        mock_reviews.return_value = mock_gh_reviews
        mock_commit.return_value = "audit_fix_sha_789"
        mock_comment.return_value = {"id": 12346}

        resp = await client.post(
            "/webhooks/github",
            content=json.dumps(payload).encode("utf-8"),
            headers={
                "X-GitHub-Event": "pull_request_review_comment",
                "X-GitHub-Delivery": str(uuid.uuid4()),
                "X-Hub-Signature-256": signed(TEST_SECRET, payload),
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "applied"
    assert data["commit_sha"] == "audit_fix_sha_789"

    mock_commit.assert_called_once()
    assert "def add(a, b)" in mock_commit.call_args.kwargs["patch_text"]

    mock_comment.assert_called_once()
    assert "audit_fix_sha_789" in mock_comment.call_args.kwargs["body"]


@pytest.mark.asyncio
async def test_auto_fix_resolves_head_branch_for_issue_comment(
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    """issue_comment payload lacks head branch; auto-fix queries fetch_pull_request to resolve it."""
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory)

    code_review = CodeReview(
        repo_id=repo.id,
        commit_sha="b" * 40,
        pr_number=PR_NUMBER,
        risk_score=50,
        summary="Minor typo",
        findings=[
            {
                "file_path": "calculator.py",
                "suggested_patch": SAMPLE_DIFF,
            }
        ],
        status="completed",
    )
    fake_audit_db.add(code_review)
    await fake_audit_db.commit()

    payload = issue_comment_payload(body="@haunter fix")

    with (
        patch("app.github.pr.get_installation_token", new_callable=AsyncMock) as mock_tok,
        patch("app.github_client.fetch_pull_request", new_callable=AsyncMock) as mock_pr,
        patch("app.github.pr.commit_patch", new_callable=AsyncMock) as mock_commit,
        patch("app.github_client.create_issue_comment", new_callable=AsyncMock) as mock_comment,
    ):
        mock_tok.return_value = "ghs_mock_token_123"
        mock_pr.return_value = {"head": {"ref": "resolved-branch-name"}}
        mock_commit.return_value = "resolved_commit_sha_111"
        mock_comment.return_value = {"id": 12347}

        resp = await client.post(
            "/webhooks/github",
            content=json.dumps(payload).encode("utf-8"),
            headers={
                "X-GitHub-Event": "issue_comment",
                "X-GitHub-Delivery": str(uuid.uuid4()),
                "X-Hub-Signature-256": signed(TEST_SECRET, payload),
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "applied"
    assert data["commit_sha"] == "resolved_commit_sha_111"

    mock_pr.assert_called_once()
    assert mock_commit.call_args.kwargs["branch"] == "resolved-branch-name"


@pytest.mark.asyncio
async def test_auto_fix_no_patch_found(
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    """When neither DB nor GitHub has actionable remediation diff, post informative message."""
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory)

    payload = review_comment_payload(body="@haunter fix", head_ref=USER_BRANCH)

    with (
        patch("app.github.pr.get_installation_token", new_callable=AsyncMock) as mock_tok,
        patch("app.github_client.fetch_pull_request_reviews", new_callable=AsyncMock) as mock_reviews,
        patch("app.github_client.create_issue_comment", new_callable=AsyncMock) as mock_comment,
    ):
        mock_tok.return_value = "ghs_mock_token_123"
        mock_reviews.return_value = []
        mock_comment.return_value = {"id": 12348}

        resp = await client.post(
            "/webhooks/github",
            content=json.dumps(payload).encode("utf-8"),
            headers={
                "X-GitHub-Event": "pull_request_review_comment",
                "X-GitHub-Delivery": str(uuid.uuid4()),
                "X-Hub-Signature-256": signed(TEST_SECRET, payload),
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "no_patch"

    mock_comment.assert_called_once()
    assert "No actionable remediation diff was found" in mock_comment.call_args.kwargs["body"]


@pytest.mark.asyncio
async def test_auto_fix_commit_patch_failure(
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    """When commit_patch fails (e.g. branch protection / conflict), post clean error comment."""
    repo = await seed_repo(fake_audit_db, fake_audit_user_factory)

    code_review = CodeReview(
        repo_id=repo.id,
        commit_sha="c" * 40,
        pr_number=PR_NUMBER,
        risk_score=90,
        summary="Security flaw",
        findings=[
            {
                "file_path": "calculator.py",
                "suggested_patch": SAMPLE_DIFF,
            }
        ],
        status="completed",
    )
    fake_audit_db.add(code_review)
    await fake_audit_db.commit()

    payload = review_comment_payload(body="@haunter fix", head_ref=USER_BRANCH)

    with (
        patch("app.github.pr.get_installation_token", new_callable=AsyncMock) as mock_tok,
        patch("app.github.pr.commit_patch", new_callable=AsyncMock) as mock_commit,
        patch("app.github_client.create_issue_comment", new_callable=AsyncMock) as mock_comment,
    ):
        mock_tok.return_value = "ghs_mock_token_123"
        mock_commit.side_effect = GitHubPRError("Protected branch: pull request required")
        mock_comment.return_value = {"id": 12349}

        resp = await client.post(
            "/webhooks/github",
            content=json.dumps(payload).encode("utf-8"),
            headers={
                "X-GitHub-Event": "pull_request_review_comment",
                "X-GitHub-Delivery": str(uuid.uuid4()),
                "X-Hub-Signature-256": signed(TEST_SECRET, payload),
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "error"
    assert "Protected branch" in data["reason"]

    mock_comment.assert_called_once()
    assert "Failed to apply remediation" in mock_comment.call_args.kwargs["body"]
    assert "Protected branch" in mock_comment.call_args.kwargs["body"]
