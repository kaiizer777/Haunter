"""Unit tests for GitHub PR Review & Inline Annotations Publisher (Phase 5.3).

Tests future02.md §1.7 and §1.8 invariants:
1. Publishing PR reviews with inline comments when findings >= 75% confidence.
2. Suppression of findings with confidence < 75% from inline annotations.
3. Suppression of all publishing when publish_allowed is False.
4. Fallback to commit comment for push events / commit audits (pr_number is None).
5. Inline comment coordinate validation against diff hunks (preventing 422 errors).
6. Handling GitHub API errors gracefully without crashing the pipeline.
7. Verification that auditor mode never calls code mutation APIs (no push, no branch, no PR creation).
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app import github_client
from app.github.audit_publisher import (
    PublishResult,
    build_inline_review_comments,
    format_inline_comment_body,
    publish_audit_review,
    validate_finding_coordinates,
)
from app.github_client import (
    GitHubAuthError,
    GitHubClientError,
    GitHubNetworkError,
    GitHubRateLimitError,
    GitHubResourceNotFoundError,
)
from app.llm.prompts.audit_prompts import (
    INFORMATIONAL_CONFIDENCE_THRESHOLD,
    MAX_GITHUB_COMMENT_CHARS,
    format_audit_report,
)
from app.models import Repo
from app.services import audit_pipeline
from app.subagents.auditor import AuditFinding, AuditResult, build_diff_grounding


@pytest.fixture(autouse=True)
def deny_external_http(monkeypatch: pytest.MonkeyPatch) -> None:
    original_send = httpx.AsyncClient.send

    async def guarded_send(self, request, **kwargs):
        if isinstance(self._transport, (httpx.ASGITransport, httpx.MockTransport)):
            return await original_send(self, request, **kwargs)
        raise AssertionError(
            f"external HTTP blocked in publisher tests: {request.url.host}"
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", guarded_send)


SAMPLE_DIFF = """diff --git a/backend/app/auth.py b/backend/app/auth.py
--- a/backend/app/auth.py
+++ b/backend/app/auth.py
@@ -80,3 +80,9 @@ def rotate_refresh_token(token: str):
+import time
+
 def verify_token(token: str, stored_token: str):
+    if token == stored_token:
+        time.sleep(5)
+        return True
+    return False
 def refresh():
     pass
diff --git a/backend/app/routers/tokens.py b/backend/app/routers/tokens.py
--- a/backend/app/routers/tokens.py
+++ b/backend/app/routers/tokens.py
@@ -0,0 +28,4 @@ async def list_sessions():
+    sessions = db.query(Session).filter(Session.tenant_id == tenant_id).all()
+    for s in sessions:
+        s.owner = db.query(User).filter(User.id == s.owner_id).first()
+    return sessions
"""


def _make_finding(
    *,
    id: str = "AUD-1234567890AB",
    file_path: str = "backend/app/auth.py",
    line_start: int = 84,
    line_end: int = 86,
    severity: str = "WARNING",
    category: str = "Security",
    title: str = "Non-Constant-Time Token Comparison",
    description: str = "Comparing tokens with == leaks timing info.",
    suggested_fix: str | None = "hmac.compare_digest(token, stored_token)",
    confidence: int = 90,
    informational_only: bool = False,
) -> AuditFinding:
    return AuditFinding(
        id=id,
        file_path=file_path,
        line_start=line_start,
        line_end=line_end,
        perspective="security",
        severity=cast(Any, severity),
        category=category,
        title=title,
        description=description,
        suggested_fix=suggested_fix,
        confidence=confidence,
        informational_only=informational_only,
    )


def _make_audit_result(
    *,
    audit_id: str = "audit-1234567890ab",
    audit_type: str = "pr_audit",
    confidence: int = 90,
    publish_allowed: bool = True,
    findings: list[AuditFinding] | None = None,
    repo_full_name: str = "octocat/hello-world",
    target_label: str = "Commit `a8f3b21` / PR `#42`",
) -> AuditResult:
    f_list = findings if findings is not None else [_make_finding()]
    report_md = format_audit_report(
        executive_summary="Audit found security and performance considerations.",
        findings=[f.to_report_dict() for f in f_list],
        confidence=confidence,
        status="⚠️ Action Recommended",
        audit_target=target_label,
        engine="nemotron-3.5-lightning",
        remediation_diff="--- a/backend/app/auth.py\n+++ b/backend/app/auth.py\n@@ -84,1 +84,1 @@\n",
        publish_allowed=publish_allowed,
    )
    return AuditResult(
        audit_id=audit_id,
        audit_type=audit_type,
        repo_full_name=repo_full_name,
        target_label=target_label,
        engine="nemotron-3.5-lightning",
        executive_summary="Audit found security and performance considerations.",
        findings=f_list,
        confidence=confidence,
        status="⚠️ Action Recommended",
        report_markdown=report_md,
        publish_allowed=publish_allowed,
    )


# ==============================================================================
# 1. Coordinate Validation Tests
# ==============================================================================


def test_validate_coordinates_valid_line_in_diff():
    grounding = build_diff_grounding(SAMPLE_DIFF)
    finding = _make_finding(file_path="backend/app/auth.py", line_start=84, line_end=86)
    coords = validate_finding_coordinates(finding, grounding)

    assert coords is not None
    assert coords["path"] == "backend/app/auth.py"
    assert coords["line"] == 86
    assert coords["side"] == "RIGHT"
    assert coords["start_line"] == 84
    assert coords["start_side"] == "RIGHT"


def test_validate_coordinates_single_line_in_diff():
    grounding = build_diff_grounding(SAMPLE_DIFF)
    finding = _make_finding(file_path="backend/app/auth.py", line_start=82, line_end=82)
    coords = validate_finding_coordinates(finding, grounding)

    assert coords is not None
    assert coords["path"] == "backend/app/auth.py"
    assert coords["line"] == 82
    assert "start_line" not in coords


def test_validate_coordinates_line_outside_diff_rejected():
    grounding = build_diff_grounding(SAMPLE_DIFF)
    finding = _make_finding(
        file_path="backend/app/auth.py", line_start=500, line_end=502
    )
    coords = validate_finding_coordinates(finding, grounding)

    assert coords is None


def test_validate_coordinates_file_not_in_diff_rejected():
    grounding = build_diff_grounding(SAMPLE_DIFF)
    finding = _make_finding(
        file_path="backend/app/unrelated.py", line_start=10, line_end=12
    )
    coords = validate_finding_coordinates(finding, grounding)

    assert coords is None


# ==============================================================================
# 2. Inline Review Comments Builder & Confidence Threshold Tests
# ==============================================================================


def test_build_inline_comments_filters_low_confidence():
    f_high = _make_finding(id="AUD-HIGH", confidence=90, line_start=84, line_end=84)
    f_low = _make_finding(id="AUD-LOW", confidence=60, line_start=85, line_end=85)

    comments, suppressed = build_inline_review_comments(
        [f_high, f_low],
        diff_text=SAMPLE_DIFF,
        min_confidence=INFORMATIONAL_CONFIDENCE_THRESHOLD,
    )

    assert len(comments) == 1
    assert suppressed == 1
    assert comments[0]["line"] == 84
    assert "AUD-HIGH" in comments[0]["body"]
    assert "AUD-LOW" not in comments[0]["body"]


def test_build_inline_comments_filters_invalid_coordinates():
    f_valid = _make_finding(id="AUD-VALID", confidence=90, line_start=84, line_end=84)
    f_invalid = _make_finding(
        id="AUD-INVALID",
        file_path="backend/app/nonexistent.py",
        confidence=90,
        line_start=10,
        line_end=10,
    )

    comments, suppressed = build_inline_review_comments(
        [f_valid, f_invalid],
        diff_text=SAMPLE_DIFF,
    )

    assert len(comments) == 1
    assert suppressed == 1
    assert comments[0]["path"] == "backend/app/auth.py"


def test_format_inline_comment_body_redacts_sensitive_data():
    finding = _make_finding(
        description="Found key AKIAIOSFODNN7EXAMPLE in code.",
        suggested_fix="Use os.getenv('AWS_KEY')",
    )
    body = format_inline_comment_body(finding)

    assert "AKIAIOSFODNN7EXAMPLE" not in body
    assert "[REDACTED_AWS_KEY]" in body


# ==============================================================================
# 3. PR Review Publishing Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_publish_pr_review_success_with_inline_comments():
    result = _make_audit_result(
        confidence=90,
        publish_allowed=True,
        findings=[
            _make_finding(id="AUD-1", confidence=92, line_start=84, line_end=84),
            _make_finding(id="AUD-2", confidence=85, line_start=85, line_end=85),
        ],
    )

    mock_client = AsyncMock()
    mock_client.create_pr_review.return_value = {"id": 1001, "state": "COMMENTED"}

    with patch(
        "app.github.audit_publisher.github_client.create_pr_review",
        mock_client.create_pr_review,
    ):
        outcome = await publish_audit_review(
            result=result,
            owner="octocat",
            repo="hello-world",
            pr_number=42,
            head_sha="a" * 40,
            diff_text=SAMPLE_DIFF,
            token="ghs_test123",
        )

    assert outcome.published is True
    assert outcome.status == "published"
    assert outcome.target_type == "pull_request_review"
    assert outcome.comments_count == 2
    assert outcome.suppressed_findings_count == 0
    assert outcome.event == "COMMENT"

    mock_client.create_pr_review.assert_awaited_once()
    kwargs = mock_client.create_pr_review.call_args.kwargs
    assert kwargs["owner"] == "octocat"
    assert kwargs["repo"] == "hello-world"
    assert kwargs["pr_number"] == 42
    assert kwargs["commit_sha"] == "a" * 40
    assert len(kwargs["comments"]) == 2
    assert kwargs["event"] == "COMMENT"
    assert kwargs["token"] == "ghs_test123"


@pytest.mark.asyncio
async def test_publish_pr_review_suppresses_low_confidence_inline_findings():
    result = _make_audit_result(
        confidence=85,
        publish_allowed=True,
        findings=[
            _make_finding(id="AUD-HIGH", confidence=90, line_start=84, line_end=84),
            _make_finding(id="AUD-LOW", confidence=65, line_start=85, line_end=85),
        ],
    )

    mock_client = AsyncMock()
    mock_client.create_pr_review.return_value = {"id": 1002}

    with patch(
        "app.github.audit_publisher.github_client.create_pr_review",
        mock_client.create_pr_review,
    ):
        outcome = await publish_audit_review(
            result=result,
            owner="octocat",
            repo="hello-world",
            pr_number=42,
            head_sha="a" * 40,
            diff_text=SAMPLE_DIFF,
            token="ghs_test123",
        )

    assert outcome.published is True
    assert outcome.comments_count == 1
    assert outcome.suppressed_findings_count == 1

    comments = mock_client.create_pr_review.call_args.kwargs["comments"]
    assert len(comments) == 1
    assert "AUD-HIGH" in comments[0]["body"]
    assert "AUD-LOW" not in comments[0]["body"]


@pytest.mark.asyncio
async def test_publish_pr_review_overall_confidence_under_75_suppresses_all_inline():
    result = _make_audit_result(
        confidence=68,  # under 75%
        publish_allowed=True,
        findings=[
            _make_finding(id="AUD-1", confidence=90, line_start=84, line_end=84),
        ],
    )

    mock_client = AsyncMock()
    mock_client.create_pr_review.return_value = {"id": 1003}

    with patch(
        "app.github.audit_publisher.github_client.create_pr_review",
        mock_client.create_pr_review,
    ):
        outcome = await publish_audit_review(
            result=result,
            owner="octocat",
            repo="hello-world",
            pr_number=42,
            head_sha="a" * 40,
            diff_text=SAMPLE_DIFF,
            token="ghs_test123",
        )

    assert outcome.published is True
    assert outcome.comments_count == 0
    assert outcome.suppressed_findings_count == 1

    comments = mock_client.create_pr_review.call_args.kwargs["comments"]
    assert comments == []


@pytest.mark.asyncio
async def test_publish_suppressed_when_publish_allowed_is_false():
    result = _make_audit_result(
        confidence=95,
        publish_allowed=False,  # disabled!
    )

    mock_pr = AsyncMock()
    mock_commit = AsyncMock()

    with (
        patch("app.github.audit_publisher.github_client.create_pr_review", mock_pr),
        patch(
            "app.github.audit_publisher.github_client.create_commit_comment",
            mock_commit,
        ),
    ):
        outcome = await publish_audit_review(
            result=result,
            owner="octocat",
            repo="hello-world",
            pr_number=42,
            head_sha="a" * 40,
        )

    assert outcome.published is False
    assert outcome.status == "suppressed"
    assert outcome.target_type == "none"
    mock_pr.assert_not_called()
    mock_commit.assert_not_called()


@pytest.mark.asyncio
async def test_publish_allowed_override_to_false():
    result = _make_audit_result(confidence=95, publish_allowed=True)

    mock_pr = AsyncMock()
    with patch("app.github.audit_publisher.github_client.create_pr_review", mock_pr):
        outcome = await publish_audit_review(
            result=result,
            owner="octocat",
            repo="hello-world",
            pr_number=42,
            head_sha="a" * 40,
            publish_allowed=False,  # explicit override
        )

    assert outcome.published is False
    assert outcome.status == "suppressed"
    mock_pr.assert_not_called()


@pytest.mark.asyncio
async def test_publish_request_changes_on_blocker_policy():
    result = _make_audit_result(
        confidence=90,
        publish_allowed=True,
        findings=[
            _make_finding(
                severity="BLOCKER", confidence=95, line_start=84, line_end=84
            ),
        ],
    )

    mock_pr = AsyncMock()
    mock_pr.return_value = {"id": 1004}

    with patch("app.github.audit_publisher.github_client.create_pr_review", mock_pr):
        outcome = await publish_audit_review(
            result=result,
            owner="octocat",
            repo="hello-world",
            pr_number=42,
            head_sha="a" * 40,
            diff_text=SAMPLE_DIFF,
            request_changes_on_blocker=True,
        )

    assert outcome.published is True
    assert outcome.event == "REQUEST_CHANGES"
    assert mock_pr.call_args.kwargs["event"] == "REQUEST_CHANGES"


# ==============================================================================
# 4. Commit Comment Fallback Tests (Push / Commit Contexts)
# ==============================================================================


@pytest.mark.asyncio
async def test_fallback_to_commit_comment_when_pr_number_is_none():
    result = _make_audit_result(
        audit_type="ci_failure_audit",
        confidence=90,
        publish_allowed=True,
    )

    mock_pr = AsyncMock()
    mock_commit = AsyncMock()
    mock_commit.return_value = {"id": 2001}

    with (
        patch("app.github.audit_publisher.github_client.create_pr_review", mock_pr),
        patch(
            "app.github.audit_publisher.github_client.create_commit_comment",
            mock_commit,
        ),
    ):
        outcome = await publish_audit_review(
            result=result,
            owner="octocat",
            repo="hello-world",
            pr_number=None,  # Commit context!
            head_sha="b" * 40,
            token="ghs_test123",
        )

    assert outcome.published is True
    assert outcome.status == "published"
    assert outcome.target_type == "commit_comment"
    mock_pr.assert_not_called()
    mock_commit.assert_awaited_once()

    kwargs = mock_commit.call_args.kwargs
    assert kwargs["owner"] == "octocat"
    assert kwargs["repo"] == "hello-world"
    assert kwargs["commit_sha"] == "b" * 40
    assert kwargs["token"] == "ghs_test123"
    assert "## 🛡️ Haunter Autonomous Audit Report" in kwargs["body"]


@pytest.mark.asyncio
async def test_commit_comment_skipped_if_head_sha_missing():
    result = _make_audit_result(
        audit_type="ci_success_audit",
        target_label="Branch `main`",
        confidence=90,
        publish_allowed=True,
    )

    mock_commit = AsyncMock()
    with patch(
        "app.github.audit_publisher.github_client.create_commit_comment", mock_commit
    ):
        outcome = await publish_audit_review(
            result=result,
            owner="octocat",
            repo="hello-world",
            pr_number=None,
            head_sha=None,
        )

    assert outcome.published is False
    assert outcome.status == "skipped"
    assert outcome.target_type == "commit_comment"
    mock_commit.assert_not_called()


# ==============================================================================
# 5. Error Handling Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_github_api_error_does_not_crash_publisher():
    result = _make_audit_result(confidence=90, publish_allowed=True)

    mock_pr = AsyncMock()
    mock_pr.side_effect = GitHubRateLimitError("GitHub API rate limit exceeded")

    with patch("app.github.audit_publisher.github_client.create_pr_review", mock_pr):
        outcome = await publish_audit_review(
            result=result,
            owner="octocat",
            repo="hello-world",
            pr_number=42,
            head_sha="a" * 40,
            raise_on_error=False,
        )

    assert outcome.published is False
    assert outcome.status == "error"
    assert outcome.target_type == "pull_request_review"
    assert "GitHubRateLimitError" in (outcome.error or "")


@pytest.mark.asyncio
async def test_github_api_error_raises_when_requested():
    result = _make_audit_result(confidence=90, publish_allowed=True)

    mock_pr = AsyncMock()
    mock_pr.side_effect = GitHubClientError("500 Internal Server Error")

    with patch("app.github.audit_publisher.github_client.create_pr_review", mock_pr):
        with pytest.raises(GitHubClientError):
            await publish_audit_review(
                result=result,
                owner="octocat",
                repo="hello-world",
                pr_number=42,
                head_sha="a" * 40,
                raise_on_error=True,
            )


@pytest.mark.asyncio
async def test_audit_pipeline_wiring_surfaces_publisher_failure():
    repo = cast(Repo, SimpleNamespace(id="repo-1", owner="octocat", name="hello-world"))
    target = audit_pipeline.AuditTarget(base_sha="b" * 40, head_sha="a" * 40)
    fake_result = _make_audit_result(confidence=90, publish_allowed=True)

    mock_pub = AsyncMock()
    mock_pub.side_effect = RuntimeError("Network partition")

    with (
        patch(
            "app.services.audit_pipeline._resolve_audit_target",
            new_callable=AsyncMock,
            return_value=target,
        ),
        patch(
            "app.services.audit_pipeline._fetch_pr_target",
            new_callable=AsyncMock,
            return_value=target,
        ),
        patch(
            "app.subagents.auditor.fetch_audit_diff",
            new_callable=AsyncMock,
            return_value=SAMPLE_DIFF,
        ),
        patch(
            "app.subagents.auditor.fetch_audit_source_contexts",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "app.subagents.auditor.build_ast_diff_summary_async",
            new_callable=AsyncMock,
            return_value="",
        ),
        patch(
            "app.subagents.auditor.run_audit",
            new_callable=AsyncMock,
            return_value=fake_result,
        ) as mock_run_audit,
        patch("app.github.audit_publisher.publish_audit_review", mock_pub),
    ):
        outcome = await audit_pipeline.execute_audit_job(
            audit_id="audit-1234567890ab",
            audit_type="pr_audit",
            repo=repo,
            ref="main",
            pr_number=42,
            base_sha="b" * 40,
            head_sha="a" * 40,
            workflow_run_id=None,
            token="test-token",
        )

    # A publish failure is a visible publish failure, not a completed audit: the
    # model ran and its report exists, but GitHub never received it, so recording
    # "completed" would report a success that did not happen.
    assert outcome.status == "publish_failed"
    # The report survives the failure. The job row is closed out, not discarded,
    # so the audit record still identifies what was analysed.
    assert outcome.result is fake_result
    assert outcome.result.audit_id == "audit-1234567890ab"
    # The failure is neither swallowed nor re-analysed: the publisher was reached
    # exactly once and the model ran exactly once, so nothing here loops back
    # through the analysis to rebuild a report GitHub has already refused.
    mock_pub.assert_awaited_once()
    mock_run_audit.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_audit_job_routes_publish_failure_terminal_without_retry():
    """A publish failure must close the job out, never requeue it.

    Retrying would re-run the model to rebuild a report GitHub has already
    refused, burning tokens to hit the same rejection. Pin both halves of that:
    the terminal close-out is taken, and neither retry path is.
    """
    job = SimpleNamespace(
        audit_id="audit-1234567890ab",
        audit_type="pr_audit",
        ref="main",
        pr_number=42,
        base_sha="b" * 40,
        head_sha="a" * 40,
        workflow_run_id=None,
    )
    repo = cast(Repo, SimpleNamespace(id="repo-1", owner="octocat", name="hello-world"))
    publish_failed = audit_pipeline.AuditExecutionOutcome(
        status="publish_failed", result=_make_audit_result(publish_allowed=True)
    )

    mock_fail_publish = AsyncMock(return_value=True)
    mock_retry_or_fail = AsyncMock()
    mock_record_failure = AsyncMock()

    with (
        patch(
            "app.services.audit_pipeline._claim_processing_job",
            new_callable=AsyncMock,
            return_value=(job, repo, 1),
        ),
        patch(
            "app.services.audit_pipeline.get_auditor_installation_token",
            new_callable=AsyncMock,
            return_value="ghs_token",
        ),
        patch(
            "app.services.audit_pipeline.execute_audit_job",
            new_callable=AsyncMock,
            return_value=publish_failed,
        ),
        patch("app.services.audit_pipeline._fail_publish_audit_job", mock_fail_publish),
        patch(
            "app.services.audit_pipeline._retry_or_fail_audit_job",
            mock_retry_or_fail,
        ),
        patch(
            "app.services.audit_pipeline._record_processing_failure",
            mock_record_failure,
        ),
    ):
        handled = await audit_pipeline.process_audit_job(
            "audit-1234567890ab", "fence-token-1234"
        )

    assert handled is True
    mock_fail_publish.assert_awaited_once_with("audit-1234567890ab", 1)
    # `_record_processing_failure` is the invalid-outcome path: reaching it would
    # mean `publish_failed` was not recognised as a known outcome and the whole
    # audit was treated as a defect to retry.
    mock_record_failure.assert_not_called()
    mock_retry_or_fail.assert_not_called()


# ==============================================================================
# 6. Direct GitHub Client Methods Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_oversized_report_is_clamped_to_github_comment_limit():
    """The clamp is pinned at GitHub's real limit, not the internal report budget.

    `MAX_REPORT_CHARS` (100_000) is what we are willing to ask a model for;
    `MAX_GITHUB_COMMENT_CHARS` (65_536) is what GitHub will accept. A report over
    the second is rejected whole with HTTP 422, taking every inline comment with
    it, so the published body must respect the smaller of the two.
    """
    oversized = "# Report\n\n" + ("x" * (MAX_GITHUB_COMMENT_CHARS + 5_000))
    result = _make_audit_result(confidence=90, publish_allowed=True)
    object.__setattr__(result, "report_markdown", oversized)

    mock_pr = AsyncMock()
    mock_pr.return_value = {"id": 3001}

    with patch("app.github.audit_publisher.github_client.create_pr_review", mock_pr):
        outcome = await publish_audit_review(
            result=result,
            owner="octocat",
            repo="hello-world",
            pr_number=42,
            head_sha="a" * 40,
            diff_text=SAMPLE_DIFF,
            token="ghs_test123",
        )

    assert outcome.published is True
    body = mock_pr.call_args.kwargs["body"]
    assert len(body) <= MAX_GITHUB_COMMENT_CHARS, (
        f"published review body of {len(body)} chars exceeds GitHub's "
        f"{MAX_GITHUB_COMMENT_CHARS}-character comment limit"
    )
    # Truncation, not rejection: the audit still publishes what fits.
    assert body.endswith("\n[TRUNCATED]")


@pytest.mark.asyncio
async def test_oversized_report_is_clamped_on_commit_comment_path():
    """Same clamp on the commit-comment fallback, the second publish site."""
    oversized = "# Report\n\n" + ("x" * (MAX_GITHUB_COMMENT_CHARS + 5_000))
    result = _make_audit_result(
        audit_type="ci_failure_audit", confidence=90, publish_allowed=True
    )
    object.__setattr__(result, "report_markdown", oversized)

    mock_commit = AsyncMock()
    mock_commit.return_value = {"id": 3002}

    with patch(
        "app.github.audit_publisher.github_client.create_commit_comment", mock_commit
    ):
        outcome = await publish_audit_review(
            result=result,
            owner="octocat",
            repo="hello-world",
            pr_number=None,
            head_sha="b" * 40,
            token="ghs_test123",
        )

    assert outcome.published is True
    body = mock_commit.call_args.kwargs["body"]
    assert len(body) <= MAX_GITHUB_COMMENT_CHARS, (
        f"published commit comment of {len(body)} chars exceeds GitHub's "
        f"{MAX_GITHUB_COMMENT_CHARS}-character comment limit"
    )


@pytest.mark.asyncio
async def test_github_client_create_pr_review_request_format():
    called_request = {}

    def custom_handler(request: httpx.Request) -> httpx.Response:
        called_request["url"] = str(request.url)
        called_request["method"] = request.method
        called_request["headers"] = dict(request.headers)
        called_request["body"] = json.loads(request.read())
        return httpx.Response(200, json={"id": 555, "state": "COMMENTED"})

    transport = httpx.MockTransport(custom_handler)

    with patch(
        "app.github_client.httpx.AsyncClient",
        return_value=httpx.AsyncClient(transport=transport),
    ):
        res = await github_client.create_pr_review(
            owner="octocat",
            repo="hello-world",
            pr_number=42,
            commit_sha="a" * 40,
            body="Review summary",
            comments=[
                {"path": "file.py", "line": 10, "side": "RIGHT", "body": "Fix here"}
            ],
            event="COMMENT",
            token="test-token",
        )

    assert res["id"] == 555
    assert called_request["method"] == "POST"
    assert "/repos/octocat/hello-world/pulls/42/reviews" in called_request["url"]
    assert called_request["headers"]["authorization"] == "Bearer test-token"
    assert called_request["body"]["commit_id"] == "a" * 40
    assert called_request["body"]["body"] == "Review summary"
    assert called_request["body"]["event"] == "COMMENT"
    assert len(called_request["body"]["comments"]) == 1


@pytest.mark.asyncio
async def test_github_client_create_commit_comment_request_format():
    called_request = {}

    def custom_handler(request: httpx.Request) -> httpx.Response:
        called_request["url"] = str(request.url)
        called_request["method"] = request.method
        called_request["headers"] = dict(request.headers)
        called_request["body"] = json.loads(request.read())
        return httpx.Response(201, json={"id": 777})

    transport = httpx.MockTransport(custom_handler)

    with patch(
        "app.github_client.httpx.AsyncClient",
        return_value=httpx.AsyncClient(transport=transport),
    ):
        res = await github_client.create_commit_comment(
            owner="octocat",
            repo="hello-world",
            commit_sha="c" * 40,
            body="Commit comment body",
            path="backend/app/auth.py",
            line=84,
            token="test-token",
        )

    assert res["id"] == 777
    assert called_request["method"] == "POST"
    assert (
        f"/repos/octocat/hello-world/commits/{'c'*40}/comments" in called_request["url"]
    )
    assert called_request["headers"]["authorization"] == "Bearer test-token"
    assert called_request["body"]["body"] == "Commit comment body"
    assert called_request["body"]["path"] == "backend/app/auth.py"
    assert called_request["body"]["line"] == 84


@pytest.mark.asyncio
async def test_github_client_create_pr_review_error_mapping():
    def not_found_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    with patch(
        "app.github_client.httpx.AsyncClient",
        return_value=httpx.AsyncClient(
            transport=httpx.MockTransport(not_found_handler)
        ),
    ):
        with pytest.raises(GitHubResourceNotFoundError):
            await github_client.create_pr_review(
                owner="octocat",
                repo="hello-world",
                pr_number=42,
                commit_sha="a" * 40,
                body="Review",
                token="test-token",
            )


# ==============================================================================
# 7. Invariant: Auditor Mode Never Calls Mutation APIs
# ==============================================================================


def test_auditor_publisher_never_calls_code_mutation_apis():
    """Ensure audit_publisher source does not reference repository mutating APIs."""
    from app.github import audit_publisher as pub_module

    source = inspect.getsource(pub_module).lower()
    mutation_tokens = (
        "create_pull_request",
        "update_branch_ref",
        "create_blob",
        "create_git_tree",
        "create_git_commit",
        "git push",
        "create_branch",
    )
    for token in mutation_tokens:
        assert (
            token not in source
        ), f"audit_publisher.py violates read-only invariant: contains {token!r}"


# ==============================================================================
# 8. Visual Polish & Layout Hierarchy Tests
# ==============================================================================


def test_audit_report_markdown_visual_hierarchy_and_tables():
    """Verify executive summary table, badges, alert callout, findings table, and details blocks."""
    f1 = _make_finding(
        id="AUD-SEC-01",
        file_path="backend/app/auth.py",
        line_start=84,
        line_end=84,
        severity="BLOCKER",
        category="Timing Attack",
        title="Non-Constant-Time Token Comparison",
        description="Timing side-channel in token validation.",
        suggested_fix="hmac.compare_digest(token, stored_token)",
        confidence=95,
    )
    result = _make_audit_result(
        confidence=95,
        publish_allowed=True,
        findings=[f1],
        target_label="PR `#42` (`a8f3b21`)",
    )

    report = result.report_markdown

    # 1. 3-Tier Sequence: Tier 1 PR Summary Callout
    assert "## 🛡️ Haunter Autonomous Audit Report" in report
    assert "> [!NOTE]" in report
    assert "**PR Summary:**" in report

    # 2. Tier 2: Clean Overview Table (Status, Confidence 0-10, Blockers, Blast Radius)
    assert "| Status | Confidence | Blockers | Blast Radius |" in report
    assert "| :--- | :---: | :--- | :--- |" in report
    assert "**10/10**" in report
    assert "Must-Fix: `1` · Should-Fix: `0`" in report
    assert "⛔ Do Not Merge" in report

    # 3. Tier 3: Structural & Blast Radius Analysis
    assert "### 🔬 Structural & Blast Radius Analysis" in report
    assert "- **Impact Surface:**" in report
    assert "- **Files Modified:**" in report
    assert "- **Touched Components:**" in report
    assert "- **Risk Assessment:**" in report

    # 4. Scannable Findings Table & Deep Dives
    assert "### 📋 Findings Overview" in report
    assert "| Severity | Perspective | File & Line | Summary | Confidence |" in report
    assert "[BLOCKER]" in report
    assert "`backend/app/auth.py#L84`" in report

    # 5. Collapsible Deep-Dives
    assert "<details open>" in report
    assert "<b>Detailed Findings Breakdown</b>" in report
    assert "</details>" in report

    # 6. Collapsible Remediation Diff
    assert "<b>Proposed Remediation Unified Diff</b>" in report
    assert "```diff" in report

    # 7. Strict Negative Assertion: Zero Model Exposure
    for forbidden_model in ("space-bunny-free", "nemotron", "gpt-4o", "claude", "test-engine"):
        assert forbidden_model not in report

    # 8. Polished Footer
    assert "Generated autonomously by Haunter Guardian Mode" in report


def test_format_inline_comment_body_renders_suggestion_block():
    """Verify inline comments render single-click suggestion blocks with bolded Problem and Remediation."""
    finding = _make_finding(
        description="Comparing tokens with == leaks timing info.",
        suggested_fix="return hmac.compare_digest(token, stored_token)",
    )
    body = format_inline_comment_body(finding)

    assert "**Problem:**" in body
    assert "**Suggested Remediation:**" in body
    assert "```suggestion\nreturn hmac.compare_digest(token, stored_token)\n```" in body
    assert "> [!WARNING]" in body

