"""
Hermetic test suite for Phase 6.3 Feature Enforcement & Governance Interceptors.

Covers:
1. Branch filtering:
   - Exact match
   - Glob / wildcard matching ('release/*', 'feature-*')
   - Branch ref normalization (refs/heads/)
   - Rejected branch with explicit reason
   - Empty allowed_branches allows all
   - PR target/head branch allowance logic
2. Draft PR filtering:
   - ignore_draft_prs=True skips draft PRs
   - ignore_draft_prs=False allows draft PRs
   - Non-draft PRs always proceed
3. Autonomous fix toggle (enable_auto_fix):
   - enable_auto_fix=False skips CI fix generation / PR creation
   - Webhook returns HTTP 200 with skipped reason and skips Run creation
   - Orchestrator terminates pipeline early with recorded failure_reason
4. Sandbox verification toggle (enable_sandbox_verification):
   - enable_sandbox_verification=False bypasses sandbox CI verification
   - Attempt verification status treated as pass without running sandbox runner
5. PR comments toggle (enable_pr_comments):
   - enable_pr_comments=False suppresses PR and commit comment posting
   - Review orchestrator skips comment publishing when disabled
6. Cost ceiling enforcement (max_cost_per_run_cents):
   - Under budget allowed
   - Accumulated run cost >= ceiling rejected with budget exceeded reason
   - 0 cents allows unbounded execution
7. Webhook integration behavior:
   - Webhook returns 200 with skipped reason when interceptor rejects event
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
from sqlalchemy import func, select

from app.config import settings
from app.models import Attempt, CodeReview, Repo, RepoSettings, Run, RunStep, User
from app.orchestrator import RunStatus, _orchestrator_pipeline_body
from app.services.feature_enforcement import (
    EnforcementDecision,
    check_cost_ceiling,
    check_feature_enforcement,
    is_auto_fix_allowed,
    is_branch_allowed,
    is_draft_pr_allowed,
    is_pr_branch_allowed,
    is_pr_comments_allowed,
    is_sandbox_verification_allowed,
    match_branch,
)
from app.services.repo_settings import get_repo_settings
from main import app
from tests.fake_audit_db import (  # noqa: F401
    FakeAsyncSession,
    audit_store,
    fake_audit_db,
    fake_audit_user_factory,
)

TEST_SECRET = settings.github_webhook_secret or "test_webhook_secret_key_12345"


def sign_payload(secret: str, raw_body: bytes) -> str:
    sig = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


@pytest.fixture
def client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def post_signed(
    client: httpx.AsyncClient,
    event: str,
    payload: dict[str, Any],
    secret: str = TEST_SECRET,
    delivery_id: str | None = None,
) -> httpx.Response:
    raw_body = json.dumps(payload).encode("utf-8")
    sig = sign_payload(secret, raw_body)
    headers = {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery_id or str(uuid.uuid4()),
        "X-Hub-Signature-256": sig,
        "Content-Type": "application/json",
    }
    return await client.post("/webhooks/github", headers=headers, content=raw_body)


async def seed_test_repo(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
    owner: str = "enforce-org",
    name: str = "enforce-repo",
    default_branch: str = "main",
) -> tuple[User, Repo]:
    user = await fake_audit_user_factory(
        github_id=int(uuid.uuid4().int % 1_000_000_000 + 100_000_000),
        username=f"user-{uuid.uuid4().hex[:6]}",
    )
    repo = Repo(
        id=uuid.uuid4(),
        user_id=user.id,
        owner=owner,
        name=name,
        default_branch=default_branch,
    )
    fake_audit_db.add(repo)
    await fake_audit_db.commit()
    await fake_audit_db.refresh(repo)
    return user, repo


# ===========================================================================
# 1. Unit Tests: Branch Filtering & Matching Logic
# ===========================================================================


def test_match_branch():
    """Branch matching supports exact match and wildcard patterns with prefix stripping."""
    assert match_branch("main", "main") is True
    assert match_branch("refs/heads/main", "main") is True
    assert match_branch("main", "refs/heads/main") is True
    assert match_branch("release/1.0", "release/*") is True
    assert match_branch("refs/heads/release/2.5.1", "release/*") is True
    assert match_branch("feature/login", "feature/*") is True
    assert match_branch("hotfix-99", "hotfix-*") is True
    assert match_branch("main", "release/*") is False
    assert match_branch("develop", "main") is False
    assert match_branch("", "main") is False
    assert match_branch("main", "") is False


def test_is_branch_allowed():
    """is_branch_allowed validates branches against configured allowed_branches."""
    # Empty or None allows all
    assert is_branch_allowed("main", []).allowed is True
    assert is_branch_allowed("feature/any", []).allowed is True
    assert is_branch_allowed("feature/any", None).allowed is True

    # Exact matches
    allowed = ["main", "master", "release/*"]
    assert is_branch_allowed("main", allowed).allowed is True
    assert is_branch_allowed("master", allowed).allowed is True
    assert is_branch_allowed("refs/heads/main", allowed).allowed is True

    # Wildcard matches
    assert is_branch_allowed("release/v1.0.0", allowed).allowed is True
    assert is_branch_allowed("release/prod", allowed).allowed is True

    # Disallowed branches
    rejected = is_branch_allowed("feature/new-login", allowed)
    assert rejected.allowed is False
    assert "not in allowed branches" in rejected.reason

    # Empty branch name
    assert is_branch_allowed("", allowed).allowed is False
    assert is_branch_allowed(None, allowed).allowed is False


def test_is_pr_branch_allowed():
    """is_pr_branch_allowed succeeds if either target branch or head branch matches."""
    allowed = ["main", "release/*"]

    # Target branch (base) matches
    res1 = is_pr_branch_allowed("main", "feature/my-feat", allowed)
    assert res1.allowed is True
    assert "target branch" in res1.reason

    # Head branch matches
    res2 = is_pr_branch_allowed("dev", "release/patch", allowed)
    assert res2.allowed is True
    assert "head branch" in res2.reason

    # Neither matches
    res3 = is_pr_branch_allowed("dev", "feature/xyz", allowed)
    assert res3.allowed is False
    assert "neither target branch" in res3.reason

    # Empty allowed allows all
    assert is_pr_branch_allowed("dev", "feature/xyz", []).allowed is True


# ===========================================================================
# 2. Unit Tests: Feature Toggles, Draft PRs, & Cost Ceiling
# ===========================================================================


def test_is_draft_pr_allowed():
    """Draft PR filtering strictly adheres to ignore_draft_prs setting."""
    # When ignore_draft_prs=True: draft PRs are skipped
    assert is_draft_pr_allowed(is_draft=True, ignore_draft_prs=True).allowed is False
    # When ignore_draft_prs=False: draft PRs are allowed
    assert is_draft_pr_allowed(is_draft=True, ignore_draft_prs=False).allowed is True
    # Non-draft PRs always allowed
    assert is_draft_pr_allowed(is_draft=False, ignore_draft_prs=True).allowed is True
    assert is_draft_pr_allowed(is_draft=False, ignore_draft_prs=False).allowed is True


def test_is_auto_fix_allowed():
    """enable_auto_fix flag gate."""
    assert is_auto_fix_allowed(enable_auto_fix=True).allowed is True
    dec = is_auto_fix_allowed(enable_auto_fix=False)
    assert dec.allowed is False
    assert "auto_fix disabled" in dec.reason


def test_is_sandbox_verification_allowed():
    """enable_sandbox_verification flag gate."""
    assert (
        is_sandbox_verification_allowed(enable_sandbox_verification=True).allowed
        is True
    )
    dec = is_sandbox_verification_allowed(enable_sandbox_verification=False)
    assert dec.allowed is False
    assert "sandbox verification disabled" in dec.reason


def test_is_pr_comments_allowed():
    """enable_pr_comments flag gate."""
    assert is_pr_comments_allowed(enable_pr_comments=True).allowed is True
    dec = is_pr_comments_allowed(enable_pr_comments=False)
    assert dec.allowed is False
    assert "PR comments disabled" in dec.reason


def test_check_cost_ceiling():
    """check_cost_ceiling bounds execution by max_cost_per_run_cents."""
    # Within budget
    assert (
        check_cost_ceiling(current_cost_cents=25.0, max_cost_per_run_cents=100).allowed
        is True
    )
    assert (
        check_cost_ceiling(current_cost_cents=99.9, max_cost_per_run_cents=100).allowed
        is True
    )

    # At or above budget
    assert (
        check_cost_ceiling(current_cost_cents=100.0, max_cost_per_run_cents=100).allowed
        is False
    )
    assert (
        check_cost_ceiling(current_cost_cents=125.0, max_cost_per_run_cents=100).allowed
        is False
    )

    # 0 cents implies unlimited budget
    assert (
        check_cost_ceiling(current_cost_cents=500.0, max_cost_per_run_cents=0).allowed
        is True
    )


@pytest.mark.asyncio
async def test_composite_check_feature_enforcement(
    fake_audit_db: FakeAsyncSession, fake_audit_user_factory
):
    """check_feature_enforcement evaluates rules in sequence and halts on first rejection."""
    _, repo = await seed_test_repo(fake_audit_db, fake_audit_user_factory)
    settings_row = RepoSettings(
        repo_id=repo.id,
        enable_auto_fix=False,
        allowed_branches=["main"],
        ignore_draft_prs=True,
    )
    fake_audit_db.add(settings_row)
    await fake_audit_db.commit()

    # Draft PR rejection takes precedence for pull_request event
    dec_draft = await check_feature_enforcement(
        db=fake_audit_db,
        repo_id=repo.id,
        event_type="pull_request",
        is_draft=True,
        settings=settings_row,
    )
    assert dec_draft.allowed is False
    assert dec_draft.feature == "draft_pr"

    # Branch rejection
    dec_branch = await check_feature_enforcement(
        db=fake_audit_db,
        repo_id=repo.id,
        branch="feature/rogue",
        settings=settings_row,
    )
    assert dec_branch.allowed is False
    assert dec_branch.feature == "branch"

    # Auto-fix rejection on workflow_run
    dec_fix = await check_feature_enforcement(
        db=fake_audit_db,
        repo_id=repo.id,
        event_type="workflow_run",
        branch="main",
        settings=settings_row,
    )
    assert dec_fix.allowed is False
    assert dec_fix.feature == "auto_fix"


# ===========================================================================
# 3. Webhook Integration Tests: Branch & Draft & Auto-Fix Enforcement
# ===========================================================================


@pytest.mark.asyncio
async def test_webhook_workflow_run_branch_filtering(
    client: httpx.AsyncClient, fake_audit_db: FakeAsyncSession, fake_audit_user_factory
):
    """workflow_run skips cleanly when head_branch is not in allowed_branches."""
    _, repo = await seed_test_repo(
        fake_audit_db, fake_audit_user_factory, "branch-org", "branch-repo"
    )
    settings_row = RepoSettings(
        repo_id=repo.id,
        allowed_branches=["main", "release/*"],
        enable_auto_fix=True,
    )
    fake_audit_db.add(settings_row)
    await fake_audit_db.commit()

    # 1. Disallowed branch: feature/unapproved -> skipped
    payload_disallowed = {
        "action": "completed",
        "workflow_run": {
            "id": 901001,
            "head_sha": "a" * 40,
            "head_branch": "feature/unapproved",
            "conclusion": "failure",
            "html_url": "https://github.com/branch-org/branch-repo/actions/runs/901001",
        },
        "repository": {
            "name": repo.name,
            "full_name": f"{repo.owner}/{repo.name}",
            "owner": {"login": repo.owner},
        },
    }
    resp = await post_signed(client, "workflow_run", payload_disallowed)
    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"
    assert "not in allowed branches" in resp.json()["reason"]

    # No Run created
    runs_count = await fake_audit_db.scalar(
        select(func.count()).select_from(Run).where(Run.repo_id == repo.id)
    )
    assert runs_count == 0

    # 2. Allowed wildcard branch: release/v2.1 -> queued
    payload_allowed = {
        "action": "completed",
        "workflow_run": {
            "id": 901002,
            "head_sha": "b" * 40,
            "head_branch": "release/v2.1",
            "conclusion": "failure",
            "html_url": "https://github.com/branch-org/branch-repo/actions/runs/901002",
        },
        "repository": {
            "name": repo.name,
            "full_name": f"{repo.owner}/{repo.name}",
            "owner": {"login": repo.owner},
        },
    }
    with patch(
        "app.adapters.hosting.get_hosting_adapter", new_callable=AsyncMock
    ) as mock_get:
        mock_adapter = MagicMock()
        mock_adapter.schedule_pipeline = AsyncMock()
        mock_get.return_value = mock_adapter

        resp_ok = await post_signed(client, "workflow_run", payload_allowed)
        assert resp_ok.status_code == 200
        assert resp_ok.json()["status"] == "queued"
        assert mock_adapter.schedule_pipeline.called


@pytest.mark.asyncio
async def test_webhook_workflow_run_empty_branches_allows_all(
    client: httpx.AsyncClient, fake_audit_db: FakeAsyncSession, fake_audit_user_factory
):
    """Empty allowed_branches list permits any branch to proceed."""
    _, repo = await seed_test_repo(
        fake_audit_db, fake_audit_user_factory, "open-org", "open-repo"
    )
    settings_row = RepoSettings(
        repo_id=repo.id,
        allowed_branches=[],
        enable_auto_fix=True,
    )
    fake_audit_db.add(settings_row)
    await fake_audit_db.commit()

    payload = {
        "action": "completed",
        "workflow_run": {
            "id": 901003,
            "head_sha": "c" * 40,
            "head_branch": "custom-dev-experiment",
            "conclusion": "failure",
            "html_url": "https://github.com/open-org/open-repo/actions/runs/901003",
        },
        "repository": {
            "name": repo.name,
            "full_name": f"{repo.owner}/{repo.name}",
            "owner": {"login": repo.owner},
        },
    }
    with patch(
        "app.adapters.hosting.get_hosting_adapter", new_callable=AsyncMock
    ) as mock_get:
        mock_adapter = MagicMock()
        mock_adapter.schedule_pipeline = AsyncMock()
        mock_get.return_value = mock_adapter

        resp = await post_signed(client, "workflow_run", payload)
        assert resp.status_code == 200
        assert resp.json()["status"] == "queued"


@pytest.mark.asyncio
async def test_webhook_workflow_run_enable_auto_fix_false_skips(
    client: httpx.AsyncClient, fake_audit_db: FakeAsyncSession, fake_audit_user_factory
):
    """workflow_run skips queuing autonomous fix Run when enable_auto_fix=False."""
    _, repo = await seed_test_repo(
        fake_audit_db, fake_audit_user_factory, "nofix-org", "nofix-repo"
    )
    settings_row = RepoSettings(
        repo_id=repo.id,
        enable_auto_fix=False,
        allowed_branches=["main"],
    )
    fake_audit_db.add(settings_row)
    await fake_audit_db.commit()

    payload = {
        "action": "completed",
        "workflow_run": {
            "id": 901004,
            "head_sha": "d" * 40,
            "head_branch": "main",
            "conclusion": "failure",
            "html_url": "https://github.com/nofix-org/nofix-repo/actions/runs/901004",
        },
        "repository": {
            "name": repo.name,
            "full_name": f"{repo.owner}/{repo.name}",
            "owner": {"login": repo.owner},
        },
    }
    resp = await post_signed(client, "workflow_run", payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"
    assert "auto_fix disabled" in resp.json()["reason"]

    # Verify no Run row persisted in DB
    runs_count = await fake_audit_db.scalar(
        select(func.count()).select_from(Run).where(Run.repo_id == repo.id)
    )
    assert runs_count == 0


@pytest.mark.asyncio
async def test_webhook_pull_request_draft_filtering(
    client: httpx.AsyncClient, fake_audit_db: FakeAsyncSession, fake_audit_user_factory
):
    """pull_request honors ignore_draft_prs setting."""
    _, repo = await seed_test_repo(
        fake_audit_db, fake_audit_user_factory, "draft-org", "draft-repo"
    )
    settings_row = RepoSettings(
        repo_id=repo.id,
        ignore_draft_prs=True,
        allowed_branches=["main"],
    )
    fake_audit_db.add(settings_row)
    await fake_audit_db.commit()

    # 1. Draft PR with ignore_draft_prs=True -> ignored
    payload_draft = {
        "action": "opened",
        "number": 51,
        "pull_request": {
            "number": 51,
            "state": "open",
            "draft": True,
            "head": {"ref": "feature/wip", "sha": "1" * 40},
            "base": {"ref": "main", "sha": "2" * 40},
            "user": {"login": "dev-user", "type": "User"},
        },
        "repository": {"name": repo.name, "owner": {"login": repo.owner}},
        "sender": {"login": "dev-user", "type": "User"},
    }
    resp1 = await post_signed(client, "pull_request", payload_draft)
    assert resp1.status_code == 200
    assert resp1.json()["status"] == "ignored"
    assert resp1.json()["reason"] == "draft PR"

    # 2. Update setting: ignore_draft_prs=False -> draft PR proceeds
    settings_row.ignore_draft_prs = False
    await fake_audit_db.commit()

    with patch(
        "app.adapters.hosting.AWSHostingAdapter.schedule_review", new_callable=AsyncMock
    ) as mock_sched:
        resp2 = await post_signed(client, "pull_request", payload_draft)
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "queued"
        assert mock_sched.called


@pytest.mark.asyncio
async def test_webhook_pull_request_branch_filtering(
    client: httpx.AsyncClient, fake_audit_db: FakeAsyncSession, fake_audit_user_factory
):
    """pull_request skips when neither target nor head branch is in allowed_branches."""
    _, repo = await seed_test_repo(
        fake_audit_db, fake_audit_user_factory, "prbranch-org", "prbranch-repo"
    )
    settings_row = RepoSettings(
        repo_id=repo.id,
        allowed_branches=["main", "release/*"],
    )
    fake_audit_db.add(settings_row)
    await fake_audit_db.commit()

    # PR targeting 'develop' from 'feature/xyz' (neither allowed)
    payload_rejected = {
        "action": "opened",
        "number": 52,
        "pull_request": {
            "number": 52,
            "state": "open",
            "draft": False,
            "head": {"ref": "feature/xyz", "sha": "3" * 40},
            "base": {"ref": "develop", "sha": "4" * 40},
            "user": {"login": "dev-user", "type": "User"},
        },
        "repository": {"name": repo.name, "owner": {"login": repo.owner}},
        "sender": {"login": "dev-user", "type": "User"},
    }
    resp = await post_signed(client, "pull_request", payload_rejected)
    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"
    assert "neither target branch" in resp.json()["reason"]


# ===========================================================================
# 4. Orchestrator Pipeline Enforcement Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_orchestrator_auto_fix_disabled_terminates_pipeline(
    fake_audit_db: FakeAsyncSession, fake_audit_user_factory
):
    """Orchestrator immediately aborts fix generation when enable_auto_fix=False."""
    user, repo = await seed_test_repo(
        fake_audit_db, fake_audit_user_factory, "orch-org", "orch-repo"
    )
    settings_row = RepoSettings(
        repo_id=repo.id,
        enable_auto_fix=False,
    )
    fake_audit_db.add(settings_row)

    run = Run(
        id=uuid.uuid4(),
        repo_id=repo.id,
        github_run_id=8801,
        github_delivery_id=str(uuid.uuid4()),
        head_sha="5" * 40,
        head_branch="main",
        status="pending",
        conclusion="failure",
    )
    fake_audit_db.add(run)
    await fake_audit_db.commit()

    state = {"step": "pending", "decisions": []}
    await _orchestrator_pipeline_body(
        db=fake_audit_db,
        run_id=run.id,
        run=run,
        repo=repo,
        state=state,
    )

    assert "auto_fix_disabled" in state["decisions"]
    assert run.status == RunStatus.error.value
    assert "auto_fix disabled" in (run.failure_reason or "")


@pytest.mark.asyncio
async def test_orchestrator_sandbox_verification_disabled_bypasses_sandbox(
    fake_audit_db: FakeAsyncSession, fake_audit_user_factory
):
    """When enable_sandbox_verification=False, orchestrator bypasses sandbox verification,
    records bypass in decisions and attempt strategy notes, sets verification_status=pass,
    and proceeds to PR creation."""
    # Unit assertion
    assert (
        is_sandbox_verification_allowed(enable_sandbox_verification=True).allowed
        is True
    )
    assert (
        is_sandbox_verification_allowed(enable_sandbox_verification=False).allowed
        is False
    )

    # Behavioral pipeline execution
    user, repo = await seed_test_repo(
        fake_audit_db, fake_audit_user_factory, "orch-sandbox-org", "orch-sandbox-repo"
    )
    settings_row = RepoSettings(
        repo_id=repo.id,
        enable_auto_fix=True,
        enable_sandbox_verification=False,
    )
    fake_audit_db.add(settings_row)

    run = Run(
        id=uuid.uuid4(),
        repo_id=repo.id,
        github_run_id=8802,
        github_delivery_id=str(uuid.uuid4()),
        head_sha="6" * 40,
        head_branch="main",
        status="pending",
        conclusion="failure",
    )
    fake_audit_db.add(run)
    await fake_audit_db.commit()

    generated_attempt = Attempt(
        id=uuid.uuid4(),
        run_id=run.id,
        attempt_number=1,
        patch_text="diff --git a/file.py b/file.py\n+fixed",
        confidence_score=90,
        strategy_notes="Initial fix",
    )

    state = {"step": "pending", "decisions": []}

    with (
        patch("app.orchestrator.gather_context", new_callable=AsyncMock) as mock_gather,
        patch(
            "app.subagents.fix_generator.generate_fix", new_callable=AsyncMock
        ) as mock_generate_fix,
        patch("app.sandbox.verify", new_callable=AsyncMock) as mock_sandbox_verify,
        patch(
            "app.subagents.pr_writer.generate_pr_text", new_callable=AsyncMock
        ) as mock_gen_pr,
        patch(
            "app.github.pr.get_installation_token", new_callable=AsyncMock
        ) as mock_token,
        patch("app.github.pr.create_branch", new_callable=AsyncMock),
        patch("app.github.pr.commit_patch", new_callable=AsyncMock),
        patch("app.github.pr.open_pr", new_callable=AsyncMock) as mock_open_pr,
    ):
        mock_gather.return_value = "diagnosis: generic failure"
        mock_generate_fix.return_value = generated_attempt
        fake_audit_db.add(generated_attempt)
        await fake_audit_db.commit()

        mock_gen_pr.return_value = {"title": "Fix bug", "body": "Detailed fix body"}
        mock_token.return_value = "ghs_testtoken123"
        mock_open_pr.return_value = {
            "html_url": "https://github.com/orch-sandbox-org/orch-sandbox-repo/pull/1",
            "number": 1,
        }

        await _orchestrator_pipeline_body(
            db=fake_audit_db,
            run_id=run.id,
            run=run,
            repo=repo,
            state=state,
        )

    # Sandbox verify was NOT called
    mock_sandbox_verify.assert_not_called()

    # Decisions recorded bypass and pass
    assert "sandbox_verification_skipped" in state["decisions"]
    assert "verification_passed" in state["decisions"]

    # Attempt updated to pass with bypass notes
    assert generated_attempt.verification_status == "pass"
    assert "[sandbox verification bypassed by repo settings]" in (
        generated_attempt.strategy_notes or ""
    )

    # Progressed to pr_opened
    assert run.status == RunStatus.pr_opened.value


@pytest.mark.asyncio
async def test_orchestrator_pr_comments_disabled_suppresses_comments(
    fake_audit_db: FakeAsyncSession, fake_audit_user_factory
):
    """When enable_pr_comments=False, fallback comments are suppressed.
    When enable_pr_comments=True, fallback comment is posted."""
    # Unit assertions
    assert is_pr_comments_allowed(enable_pr_comments=True).allowed is True
    assert is_pr_comments_allowed(enable_pr_comments=False).allowed is False

    from app.subagents.fix_generator import AttemptCapExceeded

    # Part 1: Comments disabled -> comment suppressed
    user, repo = await seed_test_repo(
        fake_audit_db, fake_audit_user_factory, "orch-comment-org", "orch-comment-repo"
    )
    settings_row = RepoSettings(
        repo_id=repo.id,
        enable_auto_fix=True,
        enable_pr_comments=False,
    )
    fake_audit_db.add(settings_row)

    run = Run(
        id=uuid.uuid4(),
        repo_id=repo.id,
        github_run_id=8803,
        github_delivery_id=str(uuid.uuid4()),
        head_sha="7" * 40,
        head_branch="main",
        status="pending",
        conclusion="failure",
    )
    fake_audit_db.add(run)

    attempt_1 = Attempt(
        id=uuid.uuid4(),
        run_id=run.id,
        attempt_number=1,
        patch_text="diff --git a/a.py b/a.py",
        confidence_score=80,
    )
    fake_audit_db.add(attempt_1)
    await fake_audit_db.commit()

    state = {"step": "pending", "decisions": []}

    with (
        patch.object(settings, "max_attempts", 1),
        patch("app.orchestrator.gather_context", new_callable=AsyncMock) as mock_gather,
        patch(
            "app.subagents.fix_generator.generate_fix", new_callable=AsyncMock
        ) as mock_generate_fix,
        patch("app.sandbox.verify", new_callable=AsyncMock) as mock_verify,
        patch(
            "app.github.pr.get_installation_token", new_callable=AsyncMock
        ) as mock_token,
        patch(
            "app.github_client.post_commit_comment", new_callable=AsyncMock
        ) as mock_post_commit_comment,
        patch(
            "app.github_client.post_pr_comment", new_callable=AsyncMock
        ) as mock_post_pr_comment,
    ):
        mock_gather.return_value = "diagnosis: cannot fix"
        mock_generate_fix.return_value = attempt_1
        mock_verify.return_value = {
            "status": "fail",
            "failure_reason": "test still failed",
            "build_duration_ms": 100,
        }
        mock_token.return_value = "ghs_testtoken123"

        await _orchestrator_pipeline_body(
            db=fake_audit_db,
            run_id=run.id,
            run=run,
            repo=repo,
            state=state,
        )

        # PR comments disabled -> no comment posted
        mock_post_commit_comment.assert_not_called()
        mock_post_pr_comment.assert_not_called()
        assert run.status == RunStatus.fallback_commented.value

    # Part 2: Comments enabled -> post_commit_comment invoked
    settings_row.enable_pr_comments = True
    await fake_audit_db.commit()

    run_enabled = Run(
        id=uuid.uuid4(),
        repo_id=repo.id,
        github_run_id=8804,
        github_delivery_id=str(uuid.uuid4()),
        head_sha="8" * 40,
        head_branch="main",
        status="pending",
        conclusion="failure",
    )
    fake_audit_db.add(run_enabled)

    attempt_2 = Attempt(
        id=uuid.uuid4(),
        run_id=run_enabled.id,
        attempt_number=1,
        patch_text="diff --git a/b.py b/b.py",
        confidence_score=80,
    )
    fake_audit_db.add(attempt_2)
    await fake_audit_db.commit()

    state_enabled = {"step": "pending", "decisions": []}

    with (
        patch.object(settings, "max_attempts", 1),
        patch("app.orchestrator.gather_context", new_callable=AsyncMock) as mock_gather,
        patch(
            "app.subagents.fix_generator.generate_fix", new_callable=AsyncMock
        ) as mock_generate_fix,
        patch("app.sandbox.verify", new_callable=AsyncMock) as mock_verify,
        patch(
            "app.github.pr.get_installation_token", new_callable=AsyncMock
        ) as mock_token,
        patch(
            "app.github_client.post_commit_comment", new_callable=AsyncMock
        ) as mock_post_commit_comment,
        patch(
            "app.github_client.post_pr_comment", new_callable=AsyncMock
        ) as mock_post_pr_comment,
    ):
        mock_gather.return_value = "diagnosis: cannot fix"
        mock_generate_fix.return_value = attempt_2
        mock_verify.return_value = {
            "status": "fail",
            "failure_reason": "test still failed",
            "build_duration_ms": 100,
        }
        mock_token.return_value = "ghs_testtoken123"

        await _orchestrator_pipeline_body(
            db=fake_audit_db,
            run_id=run_enabled.id,
            run=run_enabled,
            repo=repo,
            state=state_enabled,
        )

        mock_post_commit_comment.assert_called_once()
        assert run_enabled.status == RunStatus.fallback_commented.value


@pytest.mark.asyncio
async def test_orchestrator_cost_ceiling_enforcement_logic(
    fake_audit_db: FakeAsyncSession, fake_audit_user_factory
):
    """When accumulated step cost reaches or exceeds max_cost_per_run_cents,
    orchestrator aborts fix attempts, breaks out to fallback, and persists failure_reason."""
    # Unit assertions
    assert (
        check_cost_ceiling(current_cost_cents=45.0, max_cost_per_run_cents=50).allowed
        is True
    )
    assert (
        check_cost_ceiling(current_cost_cents=50.0, max_cost_per_run_cents=50).allowed
        is False
    )
    assert (
        check_cost_ceiling(current_cost_cents=75.5, max_cost_per_run_cents=50).allowed
        is False
    )

    # Behavioral pipeline execution
    user, repo = await seed_test_repo(
        fake_audit_db, fake_audit_user_factory, "orch-cost-org", "orch-cost-repo"
    )
    settings_row = RepoSettings(
        repo_id=repo.id,
        enable_auto_fix=True,
        max_cost_per_run_cents=50,  # 50 cents limit
    )
    fake_audit_db.add(settings_row)

    run = Run(
        id=uuid.uuid4(),
        repo_id=repo.id,
        github_run_id=8805,
        github_delivery_id=str(uuid.uuid4()),
        head_sha="9" * 40,
        head_branch="main",
        status="pending",
        conclusion="failure",
    )
    fake_audit_db.add(run)

    # Accumulate 60 cents in RunStep (> 50 cents max limit)
    step = RunStep(
        id=uuid.uuid4(),
        run_id=run.id,
        step_name="context_gathering",
        cost_estimate=0.60,
    )
    fake_audit_db.add(step)
    await fake_audit_db.commit()

    state = {"step": "pending", "decisions": []}

    with (
        patch("app.orchestrator.gather_context", new_callable=AsyncMock) as mock_gather,
        patch(
            "app.subagents.fix_generator.generate_fix", new_callable=AsyncMock
        ) as mock_generate_fix,
        patch(
            "app.github.pr.get_installation_token", new_callable=AsyncMock
        ) as mock_token,
        patch("app.github_client.post_commit_comment", new_callable=AsyncMock),
    ):
        mock_gather.return_value = "diagnosis: generic failure"
        mock_token.return_value = "ghs_testtoken123"

        await _orchestrator_pipeline_body(
            db=fake_audit_db,
            run_id=run.id,
            run=run,
            repo=repo,
            state=state,
        )

        # 1. Fix generation was NEVER called because cost ceiling tripped before attempt 1
        mock_generate_fix.assert_not_called()

        # 2. Decision tracked in state
        assert "cost_ceiling_exceeded" in state["decisions"]

        # 3. Fallback state reached
        assert run.status == RunStatus.fallback_commented.value

        # 4. failure_reason was persisted to the run row in DB
        assert run.failure_reason is not None
        assert "exceeds maximum cost limit" in run.failure_reason
        assert "60.00¢" in run.failure_reason or "60" in run.failure_reason


@pytest.mark.asyncio
async def test_webhook_push_branch_filtering(
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    """push webhook filters by repo_settings.allowed_branches."""
    _, repo = await seed_test_repo(
        fake_audit_db, fake_audit_user_factory, "pushbranch-org", "pushbranch-repo"
    )
    settings_row = RepoSettings(
        repo_id=repo.id,
        allowed_branches=["main", "release/*"],
    )
    fake_audit_db.add(settings_row)
    await fake_audit_db.commit()

    # 1. Push to disallowed branch 'refs/heads/feature-abc' -> skipped
    payload_disallowed = {
        "ref": "refs/heads/feature-abc",
        "head_commit": {
            "id": "a" * 40,
            "author": {"name": "developer", "email": "dev@example.com"},
        },
        "repository": {
            "name": repo.name,
            "owner": {"login": repo.owner},
        },
        "sender": {"login": "developer", "type": "User"},
    }
    resp1 = await post_signed(client, "push", payload_disallowed)
    assert resp1.status_code == 200
    assert resp1.json()["status"] == "skipped"
    assert "not in allowed branches" in resp1.json()["reason"]

    # 2. Push to wildcard-matching branch 'refs/heads/release/v1.0' -> queued
    payload_allowed = {
        "ref": "refs/heads/release/v1.0",
        "head_commit": {
            "id": "b" * 40,
            "author": {"name": "developer", "email": "dev@example.com"},
        },
        "repository": {
            "name": repo.name,
            "owner": {"login": repo.owner},
        },
        "sender": {"login": "developer", "type": "User"},
    }
    with patch(
        "app.adapters.hosting.AWSHostingAdapter.schedule_review", new_callable=AsyncMock
    ) as mock_sched:
        resp2 = await post_signed(client, "push", payload_allowed)
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "queued"
        assert mock_sched.called


@pytest.mark.asyncio
async def test_webhook_issue_comment_refinement_rate_limit_comment_suppressed(
    client: httpx.AsyncClient,
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
):
    """Refinement limit comment on PR is suppressed when enable_pr_comments=False."""
    _, repo = await seed_test_repo(
        fake_audit_db, fake_audit_user_factory, "refine-org", "refine-repo"
    )
    settings_row = RepoSettings(
        repo_id=repo.id,
        enable_pr_comments=False,
    )
    fake_audit_db.add(settings_row)

    # Seed root parent Run for PR #10
    root_run = Run(
        id=uuid.uuid4(),
        repo_id=repo.id,
        github_run_id=9900,
        github_delivery_id=str(uuid.uuid4()),
        head_sha="1" * 40,
        head_branch="haunter/fix-test",
        pr_number=10,
        pr_branch="haunter/fix-test",
        status="pr_opened",
        conclusion="failure",
    )
    fake_audit_db.add(root_run)

    # Seed 5 child runs to trigger rate limit (>= 5)
    for i in range(5):
        child = Run(
            id=uuid.uuid4(),
            repo_id=repo.id,
            parent_run_id=root_run.id,
            github_run_id=9901 + i,
            github_delivery_id=str(uuid.uuid4()),
            head_sha="1" * 40,
            head_branch="haunter/fix-test",
            pr_number=10,
            pr_branch="haunter/fix-test",
            status="pr_opened",
            conclusion="failure",
        )
        fake_audit_db.add(child)
    await fake_audit_db.commit()

    payload = {
        "action": "created",
        "issue": {
            "number": 10,
            "pull_request": {
                "url": f"https://api.github.com/repos/{repo.owner}/{repo.name}/pulls/10",
                "html_url": f"https://github.com/{repo.owner}/{repo.name}/pull/10",
            },
        },
        "comment": {
            "id": 881122,
            "body": "@haunter please refine this patch",
            "author_association": "MEMBER",
            "user": {"login": "senior-dev"},
        },
        "repository": {
            "name": repo.name,
            "full_name": f"{repo.owner}/{repo.name}",
            "owner": {"login": repo.owner},
        },
    }

    with patch(
        "app.github_client.post_pr_comment", new_callable=AsyncMock
    ) as mock_pr_comment:
        resp = await post_signed(client, "issue_comment", payload)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"
        assert resp.json()["reason"] == "refinement limit reached"
        # Since enable_pr_comments=False, comment should NOT have been posted
        mock_pr_comment.assert_not_called()

    # Now toggle enable_pr_comments=True
    settings_row.enable_pr_comments = True
    await fake_audit_db.commit()

    with (
        patch(
            "app.github.pr.get_installation_token", new_callable=AsyncMock
        ) as mock_token,
        patch(
            "app.github_client.post_pr_comment", new_callable=AsyncMock
        ) as mock_pr_comment,
    ):
        mock_token.return_value = "ghs_token_abc"
        resp = await post_signed(client, "issue_comment", payload)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"
        assert resp.json()["reason"] == "refinement limit reached"
        # Since enable_pr_comments=True, comment SHOULD have been posted
        mock_pr_comment.assert_called_once()
