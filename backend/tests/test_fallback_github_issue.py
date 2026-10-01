"""
Feature 3 — Fallback to GitHub Issue tests (test_fallback_github_issue.py).

Covers:
1. github_client.create_issue: 201 success (URL/number/labels/auth header),
   404, 401, 403 rate-limit, 422 validation, network error, malformed payload.
2. pr_writer.build_fallback_issue_content: title/body/labels shape, secret
   redaction, XSS escaping, no raw patch text, empty attempts/summary,
   title/body caps, attempt ordering.
3. Orchestrator exhaust path (DB): issue filed + URL/number persisted when
   file_issue_on_fallback is enabled (default); skipped when disabled;
   best-effort (issue failure still ends in fallback_commented).
"""

from __future__ import annotations

import uuid
from typing import Optional
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.github_client import (
    GITHUB_API_BASE,
    GitHubAuthError,
    GitHubClientError,
    GitHubRateLimitError,
    GitHubResourceNotFoundError,
    create_issue,
)
from app.models import Attempt, Repo, RepoSettings, Run, User
from app.subagents.pr_writer import (
    FALLBACK_ISSUE_LABELS,
    build_fallback_issue_content,
)
from tests.conftest import truncate_all

_ISSUE_URL = f"{GITHUB_API_BASE}/repos/owner/repo/issues"


def _make_run(**overrides) -> Run:
    kwargs = {
        "id": uuid.uuid4(),
        "repo_id": uuid.uuid4(),
        "github_run_id": 123456,
        "head_sha": "0123456789abcdef0123456789abcdef01234567",
        "head_branch": "main",
        "status": "fallback",
        "conclusion": "failure",
    }
    kwargs.update(overrides)
    return Run(**kwargs)


def _make_attempt(number: int, **overrides) -> Attempt:
    kwargs = {
        "id": uuid.uuid4(),
        "run_id": uuid.uuid4(),
        "attempt_number": number,
        "patch_text": "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x\n+y\n",
        "confidence_score": 80,
        "strategy_notes": f"strategy for attempt {number}",
        "verification_status": "fail",
        "failure_reason": f"pytest failed on attempt {number}",
    }
    kwargs.update(overrides)
    return Attempt(**kwargs)


# ---------------------------------------------------------------------------
# 1. github_client.create_issue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_create_issue_success():
    """201 returns the created issue dict; labels + auth header are sent."""
    payload = {
        "id": 111,
        "number": 7,
        "html_url": "https://github.com/owner/repo/issues/7",
        "title": "Haunter: CI failure",
    }
    route = respx.post(_ISSUE_URL).respond(status_code=201, json=payload)

    result = await create_issue(
        owner="owner",
        repo="repo",
        title="Haunter: CI failure",
        body="diagnosis body",
        labels=["haunter", "ci-failure"],
        token="tok123",
    )

    assert route.called
    sent = route.calls.last.request
    assert sent.headers.get("authorization") == "Bearer tok123"
    import json as _json

    sent_json = _json.loads(sent.read().decode("utf-8"))
    assert sent_json["title"] == "Haunter: CI failure"
    assert sent_json["body"] == "diagnosis body"
    assert sent_json["labels"] == ["haunter", "ci-failure"]
    assert result["number"] == 7
    assert result["html_url"] == "https://github.com/owner/repo/issues/7"


@pytest.mark.asyncio
@respx.mock
async def test_create_issue_omits_labels_when_empty():
    """No labels key is sent when labels is None."""
    payload = {"number": 8, "html_url": "https://github.com/owner/repo/issues/8"}
    route = respx.post(_ISSUE_URL).respond(status_code=201, json=payload)

    await create_issue(owner="owner", repo="repo", title="t", body="b")

    import json as _json

    sent_json = _json.loads(route.calls.last.request.read().decode("utf-8"))
    assert "labels" not in sent_json


@pytest.mark.asyncio
@respx.mock
async def test_create_issue_404():
    """404 raises GitHubResourceNotFoundError."""
    respx.post(_ISSUE_URL).respond(status_code=404, text="Not Found")
    with pytest.raises(GitHubResourceNotFoundError):
        await create_issue(owner="owner", repo="repo", title="t", body="b")


@pytest.mark.asyncio
@respx.mock
async def test_create_issue_errors():
    """401 auth, 403 rate-limit, and 422 validation map to typed errors."""
    respx.post(_ISSUE_URL).respond(status_code=401, text="Bad credentials")
    with pytest.raises(GitHubAuthError):
        await create_issue(owner="owner", repo="repo", title="t", body="b")

    respx.post(_ISSUE_URL).respond(status_code=403, text="rate limit exceeded")
    with pytest.raises(GitHubRateLimitError):
        await create_issue(owner="owner", repo="repo", title="t", body="b")

    respx.post(_ISSUE_URL).respond(status_code=422, text="Validation Failed")
    with pytest.raises(GitHubClientError):
        await create_issue(owner="owner", repo="repo", title="t", body="b")


@pytest.mark.asyncio
@respx.mock
async def test_create_issue_network_error():
    """Network failure raises GitHubClientError with the class name."""
    respx.post(_ISSUE_URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(GitHubClientError) as exc_info:
        await create_issue(owner="owner", repo="repo", title="t", body="b")
    assert "ConnectError" in str(exc_info.value)


@pytest.mark.asyncio
@respx.mock
async def test_create_issue_malformed_response():
    """200 without html_url/number raises GitHubClientError."""
    respx.post(_ISSUE_URL).respond(status_code=201, json={"id": 1})
    with pytest.raises(GitHubClientError):
        await create_issue(owner="owner", repo="repo", title="t", body="b")


# ---------------------------------------------------------------------------
# 2. pr_writer.build_fallback_issue_content
# ---------------------------------------------------------------------------


def test_build_fallback_issue_content_shape():
    """Title/body/labels are populated from run + attempts."""
    run = _make_run()
    attempts = [_make_attempt(2), _make_attempt(1)]
    content = build_fallback_issue_content(
        run=run,
        diagnosis_summary="ImportError: No module named 'utils'",
        attempts=attempts,
        owner="acme",
        repo="shop",
    )
    assert content["labels"] == list(FALLBACK_ISSUE_LABELS) == ["haunter", "ci-failure"]
    assert "main" in content["title"]
    assert "0123456" in content["title"]
    assert "acme/shop" in content["body"]
    assert "ImportError" in content["body"]
    assert str(run.id) in content["body"]
    # Attempts rendered in ascending order
    first_row = content["body"].index("| 1 |")
    second_row = content["body"].index("| 2 |")
    assert first_row < second_row
    assert "strategy for attempt 1" in content["body"]
    assert "pytest failed on attempt 2" in content["body"]


def test_build_fallback_issue_content_never_includes_patch_text():
    """Raw patch text, logs, and stack internals stay out of the issue body."""
    run = _make_run()
    secret_patch = "SECRET_PATCH_MARKER_XYZ"
    attempt = _make_attempt(1, patch_text=secret_patch)
    content = build_fallback_issue_content(
        run=run,
        diagnosis_summary="SomeError: boom",
        attempts=[attempt],
        owner="acme",
        repo="shop",
    )
    assert secret_patch not in content["body"]
    assert secret_patch not in content["title"]


def test_build_fallback_issue_content_redacts_secrets_and_escapes_html():
    """GitHub tokens are redacted and HTML is escaped in the issue body."""
    run = _make_run()
    content = build_fallback_issue_content(
        run=run,
        diagnosis_summary="Failed with token ghp_abcdefghij1234567890ABCDEFGHIJ123456 and <script>alert(1)</script>",
        attempts=[],
        owner="acme",
        repo="shop",
    )
    assert "ghp_abcdefghij1234567890ABCDEFGHIJ123456" not in content["body"]
    assert "<script>" not in content["body"]
    assert "&lt;script&gt;" in content["body"]


def test_build_fallback_issue_content_empty_attempts_and_summary():
    """Empty attempts/summary degrade to explicit placeholder text."""
    run = _make_run()
    content = build_fallback_issue_content(
        run=run,
        diagnosis_summary=None,
        attempts=[],
        owner="acme",
        repo="shop",
    )
    assert "(no diagnosis available)" in content["body"]
    assert "(no attempts recorded)" in content["body"]


def test_build_fallback_issue_content_caps():
    """Title and body respect their max lengths."""
    run = _make_run(head_branch="x" * 300)
    content = build_fallback_issue_content(
        run=run,
        diagnosis_summary="y" * 50_000,
        attempts=[_make_attempt(1, failure_reason="z" * 5_000)],
        owner="acme",
        repo="shop",
    )
    assert len(content["title"]) <= 120
    assert len(content["body"]) <= 8000
    # Per-attempt failure reason is truncated, full 5k blob is not embedded
    assert "z" * 5_000 not in content["body"]


def test_build_fallback_issue_title_keeps_sha_and_stays_well_formed():
    """A long branch truncates; the SHA and the backticks survive."""
    run = _make_run(head_branch="feature/" + "long-monorepo-path-" * 30)
    content = build_fallback_issue_content(
        run=run,
        diagnosis_summary="SomeError: boom",
        attempts=[],
        owner="acme",
        repo="shop",
    )
    title = content["title"]
    assert len(title) <= 120
    # The SHA is the triage identifier — it must never be the thing cut off.
    assert title.endswith("(0123456)")
    assert title.count("`") == 2
    assert title.startswith("Haunter: CI failure on `")


def test_build_fallback_issue_title_without_sha():
    """A run with no head_sha still yields a well-formed, capped title."""
    run = _make_run(head_sha="")
    content = build_fallback_issue_content(
        run=run,
        diagnosis_summary="SomeError: boom",
        attempts=[],
        owner="acme",
        repo="shop",
    )
    title = content["title"]
    assert len(title) <= 120
    assert title == "Haunter: CI failure on `main` needs attention"
    assert title.count("`") == 2


def test_build_fallback_issue_content_caps_attempt_notes():
    """Oversized strategy notes are truncated so the table row stays intact."""
    run = _make_run()
    attempt = _make_attempt(1, strategy_notes="n" * 9_000)
    content = build_fallback_issue_content(
        run=run,
        diagnosis_summary="SomeError: boom",
        attempts=[attempt],
        owner="acme",
        repo="shop",
    )
    assert "n" * 9_000 not in content["body"]
    # Header + separator + one complete row must all be present.
    assert "| Attempt | Confidence | Strategy | Failure reason |" in content["body"]
    assert "| --- | --- | --- | --- |" in content["body"]
    rows = [ln for ln in content["body"].splitlines() if ln.startswith("| 1 |")]
    assert len(rows) == 1
    # A row cut mid-cell would not have all four cells closed.
    assert rows[0].count("|") == 5
    assert rows[0].endswith("|")


# ---------------------------------------------------------------------------
# 3. Orchestrator exhaust path (DB)
# ---------------------------------------------------------------------------


async def _create_test_user(db: AsyncSession, username: str = "fb-user") -> User:
    user = User(
        github_id=int(uuid.uuid4().int % 1_000_000_000 + 100_000_000),
        github_username=username,
        access_token="fake_fallback_token_123",
    )
    db.add(user)
    await db.commit()
    return user


async def _create_test_repo(db: AsyncSession, user: User) -> Repo:
    repo = Repo(
        user_id=user.id,
        owner="fb-org",
        name="fb-repo",
        default_branch="main",
    )
    db.add(repo)
    await db.commit()
    return repo


async def _create_test_run(db: AsyncSession, repo: Repo) -> Run:
    run = Run(
        repo_id=repo.id,
        github_run_id=int(uuid.uuid4().int % 1_000_000_000),
        github_delivery_id=str(uuid.uuid4()),
        head_sha="0123456789abcdef0123456789abcdef01234567",
        head_branch="main",
        status="pending",
        conclusion="failure",
    )
    db.add(run)
    await db.commit()
    return run


def _mock_attempt(run_id: uuid.UUID, n: int) -> Attempt:
    return Attempt(
        id=uuid.uuid4(),
        run_id=run_id,
        attempt_number=n,
        patch_text="--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x\n+y\n",
        confidence_score=70,
        strategy_notes=f"fallback_note_{n}",
    )


@pytest.mark.anyio
async def test_exhaust_path_files_issue_when_flag_enabled(db: AsyncSession) -> None:
    """Default settings file an issue: URL/number persisted, comment posted."""
    await truncate_all(db)
    user = await _create_test_user(db)
    repo = await _create_test_repo(db, user)
    run = await _create_test_run(db, repo)
    run_id = run.id

    counter = 0

    async def mock_generate_fix(run, diagnosis_summary, prior_attempt, db, review_feedback=None):
        nonlocal counter
        counter += 1
        attempt = _mock_attempt(run.id, counter)
        db.add(attempt)
        await db.commit()
        return attempt

    fail_results = [
        {
            "status": "fail",
            "failure_reason": f"distinct tail #{i} " + chr(ord("A") + i) * 200,
            "build_duration_ms": 100,
        }
        for i in range(1, settings.max_attempts + 1)
    ]

    from app.orchestrator import handle_failed_run

    with (
        patch("app.orchestrator.gather_context", AsyncMock(return_value="Root cause X")),
        patch("app.subagents.fix_generator.generate_fix", side_effect=mock_generate_fix),
        patch("app.sandbox.verify", AsyncMock(side_effect=fail_results)),
        patch("app.github.pr.get_installation_token", AsyncMock(return_value="ghs_tok")),
        patch("app.github_client.post_commit_comment", new_callable=AsyncMock),
        patch(
            "app.github_client.create_issue",
            AsyncMock(
                return_value={
                    "html_url": "https://github.com/fb-org/fb-repo/issues/9",
                    "number": 9,
                }
            ),
        ) as mock_issue,
    ):
        await handle_failed_run(run_id)

    db.expire_all()
    db_run = (await db.execute(select(Run).where(Run.id == run_id))).scalar_one()
    assert db_run.status == "fallback_commented"
    assert db_run.fallback_issue_url == "https://github.com/fb-org/fb-repo/issues/9"
    assert db_run.fallback_issue_number == 9

    mock_issue.assert_called_once()
    kwargs = mock_issue.call_args.kwargs
    assert kwargs["owner"] == "fb-org"
    assert kwargs["repo"] == "fb-repo"
    assert kwargs["token"] == "ghs_tok"
    assert kwargs["labels"] == ["haunter", "ci-failure"]
    assert "Root cause X" in kwargs["body"]


@pytest.mark.anyio
async def test_exhaust_path_skips_issue_when_flag_disabled(db: AsyncSession) -> None:
    """file_issue_on_fallback=False suppresses the issue but keeps the comment."""
    await truncate_all(db)
    user = await _create_test_user(db)
    repo = await _create_test_repo(db, user)
    db.add(RepoSettings(repo_id=repo.id, file_issue_on_fallback=False))
    await db.commit()
    run = await _create_test_run(db, repo)
    run_id = run.id

    counter = 0

    async def mock_generate_fix(run, diagnosis_summary, prior_attempt, db, review_feedback=None):
        nonlocal counter
        counter += 1
        attempt = _mock_attempt(run.id, counter)
        db.add(attempt)
        await db.commit()
        return attempt

    fail_results = [
        {
            "status": "fail",
            "failure_reason": f"distinct tail #{i} " + chr(ord("A") + i) * 200,
            "build_duration_ms": 100,
        }
        for i in range(1, settings.max_attempts + 1)
    ]

    from app.orchestrator import handle_failed_run

    with (
        patch("app.orchestrator.gather_context", AsyncMock(return_value="Root cause Y")),
        patch("app.subagents.fix_generator.generate_fix", side_effect=mock_generate_fix),
        patch("app.sandbox.verify", AsyncMock(side_effect=fail_results)),
        patch("app.github.pr.get_installation_token", AsyncMock(return_value="ghs_tok")),
        patch(
            "app.github_client.post_commit_comment", new_callable=AsyncMock
        ) as mock_comment,
        patch("app.github_client.create_issue", new_callable=AsyncMock) as mock_issue,
    ):
        await handle_failed_run(run_id)

    db.expire_all()
    db_run = (await db.execute(select(Run).where(Run.id == run_id))).scalar_one()
    assert db_run.status == "fallback_commented"
    assert db_run.fallback_issue_url is None
    assert db_run.fallback_issue_number is None
    mock_comment.assert_called_once()
    mock_issue.assert_not_called()


@pytest.mark.anyio
async def test_exhaust_path_issue_failure_is_best_effort(db: AsyncSession) -> None:
    """A failing create_issue never blocks the fallback_commented transition."""
    await truncate_all(db)
    user = await _create_test_user(db)
    repo = await _create_test_repo(db, user)
    run = await _create_test_run(db, repo)
    run_id = run.id

    counter = 0

    async def mock_generate_fix(run, diagnosis_summary, prior_attempt, db, review_feedback=None):
        nonlocal counter
        counter += 1
        attempt = _mock_attempt(run.id, counter)
        db.add(attempt)
        await db.commit()
        return attempt

    fail_results = [
        {
            "status": "fail",
            "failure_reason": f"distinct tail #{i} " + chr(ord("A") + i) * 200,
            "build_duration_ms": 100,
        }
        for i in range(1, settings.max_attempts + 1)
    ]

    from app.orchestrator import handle_failed_run

    with (
        patch("app.orchestrator.gather_context", AsyncMock(return_value="Root cause Z")),
        patch("app.subagents.fix_generator.generate_fix", side_effect=mock_generate_fix),
        patch("app.sandbox.verify", AsyncMock(side_effect=fail_results)),
        patch("app.github.pr.get_installation_token", AsyncMock(return_value="ghs_tok")),
        patch("app.github_client.post_commit_comment", new_callable=AsyncMock),
        patch(
            "app.github_client.create_issue",
            AsyncMock(side_effect=RuntimeError("issues API down")),
        ),
    ):
        await handle_failed_run(run_id)

    db.expire_all()
    db_run = (await db.execute(select(Run).where(Run.id == run_id))).scalar_one()
    assert db_run.status == "fallback_commented"
    assert db_run.fallback_issue_url is None
    assert db_run.fallback_issue_number is None
