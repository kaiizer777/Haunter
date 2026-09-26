"""
Phase 5.2 — Multi-Perspective Audit Core tests (future02.md §1.6,
`test_auditor_core.py`).

Covers:
  1. All 4 perspectives emit findings via ONE shared LLMClient over
     `asyncio.gather` (single instantiation, 4 completions).
  2. Severity aggregation: BLOCKER > WARNING > NOTE ordering + status label.
  3. §1.8 confidence policy: findings with confidence < 75 are demoted to
     [NOTE] and flagged informational-only; overall confidence < 75 marks
     the whole report informational.
  4. Report formatter emits every §1.4 section verbatim (header, Status,
     Confidence Score, Audit Target, Engine, Executive Summary, Findings,
     Remediation Diff, Guardian Mode footer).
  5. Read-only invariant: auditor + prompt sources reference no write APIs
     (no push / branch / PR / comment publishing calls); the runtime path
     only touches GitHub GET helpers.
  6. Empty diff short-circuits with zero findings and no LLM call.
  7. Deterministic diff inspection: file parsing + rule pre-screen hits.
  8. The independent worker path fetches bounded context, calls the core,
     stops without a diff, and records terminal failure without raising.

All LLM/GitHub side effects are mocked. No DB fixtures — this module is
fully hermetic (no TEST_DATABASE_URL required).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.github_client import (
    GitHubAuthError,
    GitHubNetworkError,
    GitHubRateLimitError,
    GitHubResourceNotFoundError,
)
from app.llm import LLMClient
from app.llm.prompts import audit_prompts
from app.models import Repo
from app.llm.prompts.audit_prompts import (
    MAX_REPORT_CHARS,
    MAX_SYSTEM_PROMPT_CHARS,
    MAX_USER_PROMPT_CHARS,
    build_status_label,
    format_audit_report,
)
from app.services import audit_pipeline
from app.subagents import auditor
from app.subagents.auditor import (
    AUDIT_MAX_DIFF_CHARS,
    MAX_LLM_RESPONSE_CHARS,
    AuditAnalysisError,
    AuditFinding,
    AuditResult,
    PerspectiveFinding,
    PerspectiveResult,
    build_ast_diff_summary,
    build_ast_diff_summary_async,
    build_diff_grounding,
    build_diff_line_index,
    parse_diff_files,
    run_audit,
    synthesize_findings,
)

@pytest.fixture(autouse=True)
def deny_external_http(monkeypatch: pytest.MonkeyPatch) -> None:
    original_send = httpx.AsyncClient.send

    async def guarded_send(self, request, **kwargs):
        if isinstance(self._transport, httpx.ASGITransport):
            return await original_send(self, request, **kwargs)
        raise AssertionError(f"external HTTP blocked in audit tests: {request.url.host}")

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


def _finding(
    file_path: str = "backend/app/auth.py",
    severity: str = "WARNING",
    confidence: int = 90,
    title: str = "Sample finding",
    suggested_fix: str | None = "fixed = True",
    line_start: int = 84,
    line_end: int = 86,
) -> dict:
    return {
        "file_path": file_path,
        "line_start": line_start,
        "line_end": line_end,
        "severity": severity,
        "category": "Test Category",
        "title": title,
        "impact": "Concrete breakage under a crafted input.",
        "suggested_fix": suggested_fix,
        "confidence": confidence,
    }


def _perspective_payload(summary: str, confidence: int, findings: list[dict]) -> str:
    return json.dumps(
        {"summary": summary, "confidence": confidence, "findings": findings}
    )


def _llm_response(content: str, model: str = "test-engine") -> dict:
    return {
        "content": content,
        "usage": {"input_tokens": 100, "output_tokens": 50},
        "latency_ms": 10,
        "model": model,
    }


def _four_perspective_router(confidences: dict[str, int] | None = None):
    """Route mocked completions by the `## Perspective\\n<name>` marker."""
    confidences = confidences or {}
    titles = {
        "security": ("Non-Constant-Time Token Comparison", "BLOCKER", 95),
        "correctness": ("Unhandled Null Dereference", "WARNING", 82),
        "performance": ("N+1 Query Pattern", "WARNING", 90),
        "architecture": ("Response Schema Drift", "NOTE", 88),
    }

    async def _complete(*, messages=None, **kwargs):
        user_text = " ".join(m.get("content", "") for m in (messages or []))
        perspective = next(
            p
            for p in ("security", "correctness", "performance", "architecture")
            if f"## Perspective\n{p}" in user_text
        )
        title, severity, default_conf = titles[perspective]
        conf = confidences.get(perspective, default_conf)
        return _llm_response(
            _perspective_payload(
                f"{perspective} verdict.",
                conf,
                [_finding(title=f"{title} ({perspective})", severity=severity, confidence=conf)],
            )
        )

    return _complete


def _patched_llm(side_effect):
    """Patch the auditor's LLMClient; return (class mock, instance mock)."""
    patcher = patch("app.subagents.auditor.LLMClient")
    mock_cls = patcher.start()
    instance = mock_cls.return_value
    instance.complete = AsyncMock(side_effect=side_effect)
    return patcher, mock_cls, instance


def _mock_llm_client(complete: AsyncMock) -> LLMClient:
    return cast(LLMClient, SimpleNamespace(complete=complete))


# ---------------------------------------------------------------------------
# 1. Four perspectives, one shared client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_four_perspectives_emit_findings_with_shared_client():
    patcher, mock_cls, instance = _patched_llm(_four_perspective_router())
    try:
        result = await run_audit(
            diff_text=SAMPLE_DIFF,
            repo_full_name="audit-org/audit-repo",
            ref="feature/audit-test",
            audit_id="audit-111111111111",
        )
    finally:
        patcher.stop()

    # One shared LLMClient instance across all four perspectives.
    assert mock_cls.call_count == 1
    assert instance.complete.await_count == 4
    assert {p.perspective for p in result.perspectives} == {
        "security",
        "correctness",
        "performance",
        "architecture",
    }
    assert {f.perspective for f in result.findings} == {
        "security",
        "correctness",
        "performance",
        "architecture",
    }
    # Mean of 95/82/90/88 -> 88.75 -> 89.
    assert result.confidence == 89
    assert result.engine == "test-engine"
    assert result.audit_id == "audit-111111111111"


# ---------------------------------------------------------------------------
# 2. Severity aggregation + status label
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_severity_aggregation_orders_blocker_first():
    patcher, _, _ = _patched_llm(_four_perspective_router())
    try:
        result = await run_audit(diff_text=SAMPLE_DIFF, audit_id="audit-222222222222")
    finally:
        patcher.stop()

    severities = [f.severity for f in result.findings]
    assert severities == ["BLOCKER", "WARNING", "WARNING", "NOTE"]
    assert result.severity_counts == {"BLOCKER": 1, "WARNING": 2, "NOTE": 1}
    assert len({finding.id for finding in result.findings}) == 4
    assert all(re.fullmatch(r"AUD-[0-9A-F]{12}", finding.id) for finding in result.findings)
    assert all(finding.description and finding.to_dict()["description"] for finding in result.findings)
    assert "Blockers Found" in result.status
    assert "(1 Blockers, 2 Warnings)" in result.status
    assert "Informational Only" not in result.status


# ---------------------------------------------------------------------------
# 3. §1.8 confidence policy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_low_confidence_finding_demoted_to_informational_note():
    patcher, _, _ = _patched_llm(_four_perspective_router({"correctness": 60}))
    try:
        result = await run_audit(diff_text=SAMPLE_DIFF, audit_id="audit-333333333333")
    finally:
        patcher.stop()

    correctness = next(f for f in result.findings if f.perspective == "correctness")
    assert correctness.confidence == 60
    assert correctness.severity == "NOTE"
    assert correctness.informational_only is True
    # Overall mean (95+60+90+88)/4 = 83.25 -> 83: report stays confident.
    assert result.confidence == 83
    assert "Informational Only" not in result.status
    assert "informational (low confidence" in result.report_markdown


@pytest.mark.asyncio
async def test_overall_low_confidence_marks_report_informational():
    low = {"security": 60, "correctness": 55, "performance": 50, "architecture": 70}
    patcher, _, _ = _patched_llm(_four_perspective_router(low))
    try:
        result = await run_audit(diff_text=SAMPLE_DIFF, audit_id="audit-444444444444")
    finally:
        patcher.stop()

    assert result.confidence == 59  # mean(60,55,50,70) = 58.75 -> 59
    assert all(f.severity == "NOTE" for f in result.findings)
    assert all(f.informational_only for f in result.findings)
    assert "Informational Only" in result.status


# ---------------------------------------------------------------------------
# 4. §1.4 formatter — exact sections
# ---------------------------------------------------------------------------


def test_formatter_emits_all_section_1_4_sections():
    report = format_audit_report(
        executive_summary="JWT rotation looks right except token comparison.",
        findings=[
            {
                "file_path": "backend/app/auth.py",
                "line_start": 84,
                "line_end": 84,
                "perspective": "security",
                "severity": "BLOCKER",
                "category": "Timing Attack / Insecure Crypto",
                "title": "Non-Constant-Time Token Comparison",
                "impact": "Standard == leaks timing information byte-by-byte.",
                "suggested_fix": "if not hmac.compare_digest(token, stored_token):",
                "confidence": 95,
                "informational_only": False,
            },
            {
                "file_path": "backend/app/routers/tokens.py",
                "line_start": 32,
                "line_end": 33,
                "perspective": "performance",
                "severity": "WARNING",
                "category": "Database Query Efficiency",
                "title": "Unindexed Tenant Query Filter",
                "impact": "Sequential scan on large sessions tables.",
                "suggested_fix": None,
                "confidence": 90,
                "informational_only": False,
            },
        ],
        confidence=94,
        status=build_status_label(1, 1, 94),
        audit_target="Commit `a8f3b21` / PR `#42`",
        engine="nemotron-3.5-lightning",
        remediation_diff="--- a/backend/app/auth.py\n+++ b/backend/app/auth.py",
        publish_allowed=True,
    )
    for required in (
        "## 🛡️ Haunter Autonomous Audit Report",
        "**Status:**",
        "**Confidence Score:**",
        "`94%`",
        "**Audit Target:**",
        "Commit `a8f3b21` / PR `#42`",
        "**Engine:**",
        "`nemotron-3.5-lightning`",
        "### 🔍 Executive Summary",
        "### 🚨 Findings & Recommendations",
        "[BLOCKER]",
        "[WARNING]",
        "backend/app/auth.py#L84",
        "### 🛠️ Remediation Unified Diff",
        "```diff",
        "Zero changes were committed to your branch.",
    ):
        assert required in report, f"missing formatter section: {required!r}"


def test_formatter_empty_findings_reports_clean():
    report = format_audit_report(
        executive_summary="Clean.",
        findings=[],
        confidence=100,
        status=build_status_label(0, 0, 100),
        audit_target="diff",
        engine="test-engine",
        remediation_diff="(no automated remediation suggested — see findings above)",
        publish_allowed=True,
    )
    assert "No actionable findings" in report
    assert "### 🛠️ Remediation Unified Diff" in report
    assert "✅ Looks Good" in report


# ---------------------------------------------------------------------------
# 5. Read-only invariant
# ---------------------------------------------------------------------------

_READ_ONLY_FORBIDDEN_TOKENS = (
    "create_pull_request",
    "update_branch_ref",
    "create_blob",
    "create_git_tree",
    "create_git_commit",
    "post_commit_comment",
    "create_commit_comment",
    "post_pr_comment",
    "create_pr_review",
    "create_pull_request_review",
    "schedule_pipeline",
    "schedule_review",
    "git push",
    "create_branch",
)


@pytest.mark.parametrize(
    "module_path",
    [
        Path("backend/app/subagents/auditor.py"),
        Path("backend/app/llm/prompts/audit_prompts.py"),
        Path("backend/app/services/audit_pipeline.py"),
        Path("backend/lambda_handler.py"),
    ],
)
def test_read_only_no_write_api_references(module_path: Path):
    """Auditor sources must never reference write APIs (future02 §1.8)."""
    try:
        source = module_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        source = Path(*module_path.parts[1:]).read_text(encoding="utf-8")
    lowered = source.lower()
    for token in _READ_ONLY_FORBIDDEN_TOKENS:
        assert token.lower() not in lowered, (
            f"{module_path}: read-only violation — references {token!r}"
        )


def test_audit_queue_adapter_path_has_no_repository_write_apis():
    from app.adapters.hosting import AWSHostingAdapter

    source = inspect.getsource(AWSHostingAdapter.schedule_audit).lower()
    for token in _READ_ONLY_FORBIDDEN_TOKENS:
        assert token.lower() not in source


@pytest.mark.asyncio
async def test_aws_audit_scheduler_self_invokes_with_fenced_token():
    from app.adapters.hosting import AWSHostingAdapter

    fence = audit_pipeline.audit_dispatch_fence_token(
        "audit-abcdef123456", 1, "audit-secret"
    )
    lambda_client = MagicMock()
    lambda_client.invoke.return_value = {"StatusCode": 202}
    with (
        patch("app.config.settings.aws_lambda_function_name", "haunter-test"),
        patch("app.config.settings.audit_self_invoke_secret", "audit-secret"),
        patch("boto3.client", return_value=lambda_client),
    ):
        await AWSHostingAdapter().schedule_audit("audit-abcdef123456", fence)

    kwargs = lambda_client.invoke.call_args.kwargs
    payload = json.loads(kwargs["Payload"])
    assert kwargs["FunctionName"] == "haunter-test"
    assert kwargs["InvocationType"] == "Event"
    assert payload["audit_id"] == "audit-abcdef123456"
    assert payload["dispatch_fence_token"] == fence
    assert audit_pipeline.verify_audit_self_invocation(
        payload["audit_id"], fence, payload["token"], "audit-secret"
    )
    # A child token minted for a different fence must not authenticate.
    other_fence = audit_pipeline.audit_dispatch_fence_token(
        "audit-abcdef123456", 2, "audit-secret"
    )
    assert not audit_pipeline.verify_audit_self_invocation(
        payload["audit_id"], other_fence, payload["token"], "audit-secret"
    )


@pytest.mark.asyncio
async def test_audit_scheduler_fails_closed_without_dedicated_secret():
    from app.adapters.hosting import AWSHostingAdapter

    fence = "a" * 64
    lambda_client = MagicMock()
    with (
        patch("app.config.settings.aws_lambda_function_name", "haunter-test"),
        patch("app.config.settings.audit_self_invoke_secret", None),
        patch("boto3.client", return_value=lambda_client),
        pytest.raises(RuntimeError, match="self-invocation secret"),
    ):
        await AWSHostingAdapter().schedule_audit("audit-abcdef123456", fence)

    lambda_client.invoke.assert_not_called()


@pytest.mark.asyncio
async def test_audit_scheduler_rejects_non_accepted_invocation_status():
    from app.adapters.hosting import AWSHostingAdapter

    lambda_client = MagicMock()
    lambda_client.invoke.return_value = {"StatusCode": 500}
    with (
        patch("app.config.settings.aws_lambda_function_name", "haunter-test"),
        patch("app.config.settings.audit_self_invoke_secret", "audit-secret"),
        patch("boto3.client", return_value=lambda_client),
        pytest.raises(RuntimeError, match="not accepted"),
    ):
        await AWSHostingAdapter().schedule_audit("audit-abcdef123456", "a" * 64)


def test_audit_dispatch_fence_is_bound_to_the_dispatch_attempt():
    audit_id = "audit-abcdef123456"
    first = audit_pipeline.audit_dispatch_fence_token(audit_id, 1, "audit-secret")
    second = audit_pipeline.audit_dispatch_fence_token(audit_id, 2, "audit-secret")
    assert first != second
    assert audit_pipeline.verify_audit_dispatch_fence(audit_id, 1, first, "audit-secret")
    assert not audit_pipeline.verify_audit_dispatch_fence(
        audit_id, 2, first, "audit-secret"
    )
    assert not audit_pipeline.verify_audit_dispatch_fence(
        audit_id, 1, first, "wrong-secret"
    )
    assert not audit_pipeline.verify_audit_dispatch_fence(audit_id, 1, None, "audit-secret")
    assert not audit_pipeline.verify_audit_dispatch_fence("audit-bbbbbbbbbbbb", 1, first, "audit-secret")
    for bad_attempt in (0, -1, 1001, True, "1"):
        with pytest.raises(ValueError):
            audit_pipeline.audit_dispatch_fence_token(
                audit_id, cast(int, bad_attempt), "audit-secret"
            )


def test_audit_self_invocation_rejects_missing_bad_tampered_and_mismatched_tokens():
    audit_id = "audit-abcdef123456"
    other_id = "audit-bbbbbbbbbbbb"
    fence = "f" * 64
    other_fence = "0" * 64
    token = audit_pipeline.audit_child_invocation_token(audit_id, fence, "audit-secret")
    assert audit_pipeline.verify_audit_self_invocation(
        audit_id, fence, token, "audit-secret"
    )
    assert not audit_pipeline.verify_audit_self_invocation(
        audit_id, fence, None, "audit-secret"
    )
    assert not audit_pipeline.verify_audit_self_invocation(
        audit_id, fence, "", "audit-secret"
    )
    assert not audit_pipeline.verify_audit_self_invocation(
        audit_id, fence, "bad", "audit-secret"
    )
    assert not audit_pipeline.verify_audit_self_invocation(
        audit_id, fence, "0" * 64, "audit-secret"
    )
    assert not audit_pipeline.verify_audit_self_invocation(
        other_id, fence, token, "audit-secret"
    )
    assert not audit_pipeline.verify_audit_self_invocation(
        audit_id, other_fence, token, "audit-secret"
    )
    assert not audit_pipeline.verify_audit_self_invocation(
        audit_id, "not-a-fence", token, "audit-secret"
    )
    assert not audit_pipeline.verify_audit_self_invocation(
        audit_id, fence, token, "wrong-secret"
    )


def test_self_invocation_tokens_are_domain_separated_by_kind():
    from app.self_invocation import (
        KIND_AUDIT,
        KIND_PIPELINE,
        KIND_REVIEW,
        self_invocation_token,
        verify_self_invocation,
    )

    identifier = "shared-identifier"
    tokens = {
        kind: self_invocation_token(kind, identifier, "audit-secret")
        for kind in (KIND_PIPELINE, KIND_REVIEW, KIND_AUDIT)
    }
    assert len(set(tokens.values())) == 3
    for kind, token in tokens.items():
        for other_kind in tokens:
            if other_kind == kind:
                assert verify_self_invocation(
                    kind, identifier, token, "audit-secret"
                )
            else:
                assert not verify_self_invocation(
                    other_kind, identifier, token, "audit-secret"
                )


def test_self_invocation_fails_closed_when_secret_is_missing():
    from app.self_invocation import (
        KIND_PIPELINE,
        SelfInvocationError,
        self_invocation_token,
        verify_self_invocation,
    )

    with patch("app.config.settings.audit_self_invoke_secret", None):
        with pytest.raises(SelfInvocationError):
            self_invocation_token(KIND_PIPELINE, "some-run")
        assert not verify_self_invocation(KIND_PIPELINE, "some-run", "0" * 64)
        assert not verify_self_invocation(KIND_PIPELINE, "some-run", None)


def test_self_invocation_secret_has_one_shared_fail_closed_resolver():
    """One implementation of "the secret must be configured", for every kind.

    The audit fence and child tokens used to carry a second copy of this rule
    that disagreed with the pipeline/review copy about an explicitly blank
    secret. Two copies of an authentication rule is a rule that can drift open,
    so the audit path must resolve through the shared resolver.
    """
    from app.self_invocation import (
        KIND_PIPELINE,
        KIND_REVIEW,
        SelfInvocationError,
        resolve_self_invocation_secret,
        self_invocation_token,
    )

    assert not hasattr(audit_pipeline, "_self_invoke_secret"), (
        "audit_pipeline must not keep a second copy of the secret rule"
    )

    with patch("app.config.settings.audit_self_invoke_secret", "configured-secret"):
        assert resolve_self_invocation_secret() == "configured-secret"
        # An explicitly supplied key is used verbatim and never falls back to
        # the setting, so a caller handed a blank one fails instead of being
        # silently authenticated with a different key.
        assert resolve_self_invocation_secret("other-secret") == "other-secret"
        for unusable in ("", "   ", "\t\n", 12345, b"bytes-secret"):
            with pytest.raises(SelfInvocationError):
                resolve_self_invocation_secret(cast(Any, unusable))

    for configured in (None, "", "   "):
        with patch("app.config.settings.audit_self_invoke_secret", configured):
            with pytest.raises(SelfInvocationError):
                resolve_self_invocation_secret()
            for kind in (KIND_PIPELINE, KIND_REVIEW):
                with pytest.raises(SelfInvocationError):
                    self_invocation_token(kind, "identifier")

    with patch("app.config.settings.audit_self_invoke_secret", "configured-secret"):
        # The audit constructions resolve through the same rule, so the
        # configured setting is exactly what they sign with.
        assert audit_pipeline.audit_dispatch_fence_token(
            "audit-abcdef123456", 1
        ) == audit_pipeline.audit_dispatch_fence_token(
            "audit-abcdef123456", 1, "configured-secret"
        )
        assert audit_pipeline.audit_child_invocation_token(
            "audit-abcdef123456", "f" * 64
        ) == audit_pipeline.audit_child_invocation_token(
            "audit-abcdef123456", "f" * 64, "configured-secret"
        )
        for unusable in ("", "   "):
            with pytest.raises(SelfInvocationError):
                audit_pipeline.audit_dispatch_fence_token(
                    "audit-abcdef123456", 1, cast(Any, unusable)
                )
            with pytest.raises(SelfInvocationError):
                audit_pipeline.audit_child_invocation_token(
                    "audit-abcdef123456", "f" * 64, cast(Any, unusable)
                )
            # Verification stays fail-closed rather than raising at the caller.
            assert not audit_pipeline.verify_audit_dispatch_fence(
                "audit-abcdef123456", 1, "0" * 64, cast(Any, unusable)
            )
            assert not audit_pipeline.verify_audit_self_invocation(
                "audit-abcdef123456", "f" * 64, "0" * 64, cast(Any, unusable)
            )


@pytest.mark.asyncio
async def test_pipeline_and_review_schedulers_require_the_dedicated_secret():
    from app.adapters.hosting import AWSHostingAdapter
    from app.self_invocation import (
        KIND_PIPELINE,
        KIND_REVIEW,
        SelfInvocationError,
        self_invocation_token,
    )

    adapter = AWSHostingAdapter()
    run_id = uuid.uuid4()
    review_id = uuid.uuid4()
    lambda_client = MagicMock()
    with (
        patch("app.config.settings.aws_lambda_function_name", "haunter-test"),
        patch("app.config.settings.audit_self_invoke_secret", None),
        patch("app.config.settings.github_webhook_secret", "webhook-secret"),
        patch("app.config.settings.session_secret_key", "session-secret"),
        patch("boto3.client", return_value=lambda_client),
    ):
        with pytest.raises(SelfInvocationError):
            await adapter.schedule_pipeline(run_id, MagicMock())
        with pytest.raises(SelfInvocationError):
            await adapter.schedule_review(review_id, MagicMock())
    lambda_client.invoke.assert_not_called()

    lambda_client.invoke.return_value = {"StatusCode": 202}
    with (
        patch("app.config.settings.aws_lambda_function_name", "haunter-test"),
        patch("app.config.settings.audit_self_invoke_secret", "audit-secret"),
        patch("boto3.client", return_value=lambda_client),
    ):
        await adapter.schedule_pipeline(run_id, MagicMock())
        await adapter.schedule_review(review_id, MagicMock())

    assert lambda_client.invoke.call_count == 2
    first = json.loads(lambda_client.invoke.call_args_list[0].kwargs["Payload"])
    second = json.loads(lambda_client.invoke.call_args_list[1].kwargs["Payload"])
    assert first["token"] == self_invocation_token(
        KIND_PIPELINE, str(run_id), "audit-secret"
    )
    assert second["token"] == self_invocation_token(
        KIND_REVIEW, str(review_id), "audit-secret"
    )


@pytest.mark.asyncio
async def test_lambda_handler_rejects_unauthenticated_pipeline_and_review_invocations():
    import lambda_handler

    run_id = str(uuid.uuid4())
    review_id = str(uuid.uuid4())
    with (
        patch("app.config.settings.audit_self_invoke_secret", None),
        patch("app.config.settings.github_webhook_secret", "webhook-secret"),
        patch("app.config.settings.session_secret_key", "session-secret"),
        patch("lambda_handler._run_pipeline", new_callable=AsyncMock) as run_pipeline,
        patch(
            "lambda_handler._run_review_pipeline", new_callable=AsyncMock
        ) as run_review,
    ):
        pipeline = lambda_handler.handler({"run_id": run_id}, SimpleNamespace())
        review = lambda_handler.handler(
            {"review_id": review_id}, SimpleNamespace()
        )
    run_pipeline.assert_not_awaited()
    run_review.assert_not_awaited()
    assert pipeline == {
        "error": "unauthorized pipeline invocation",
        "run_id": run_id,
    }
    assert review == {
        "error": "unauthorized review invocation",
        "review_id": review_id,
    }


def test_lambda_handler_audit_self_invocation_is_strictly_authenticated():
    import hashlib
    import hmac

    import lambda_handler

    audit_id = "audit-abcdef123456"
    fence = "c" * 64
    fallback_token = hmac.new(
        b"webhook-secret", audit_id.encode(), hashlib.sha256
    ).hexdigest()
    with (
        patch("app.config.settings.audit_self_invoke_secret", None),
        patch("app.config.settings.github_webhook_secret", "webhook-secret"),
        patch("app.config.settings.session_secret_key", "session-secret"),
        patch("lambda_handler._run_audit", new_callable=AsyncMock) as run_audit,
    ):
        result = lambda_handler.handler(
            {
                "audit_id": audit_id,
                "dispatch_fence_token": fence,
                "token": fallback_token,
            },
            SimpleNamespace(),
        )
    assert result == {"error": "unauthorized audit invocation"}
    run_audit.assert_not_awaited()

    valid_token = audit_pipeline.audit_child_invocation_token(
        audit_id, fence, "audit-secret"
    )
    with (
        patch("app.config.settings.audit_self_invoke_secret", "audit-secret"),
        patch(
            "lambda_handler._run_audit",
            new_callable=AsyncMock,
            return_value=True,
        ) as run_audit,
    ):
        missing = lambda_handler.handler(
            {"audit_id": audit_id}, SimpleNamespace()
        )
        missing_fence = lambda_handler.handler(
            {"audit_id": audit_id, "token": valid_token}, SimpleNamespace()
        )
        empty = lambda_handler.handler(
            {
                "audit_id": audit_id,
                "dispatch_fence_token": fence,
                "token": "",
            },
            SimpleNamespace(),
        )
        tampered = lambda_handler.handler(
            {
                "audit_id": audit_id,
                "dispatch_fence_token": fence,
                "token": "0" * 64,
            },
            SimpleNamespace(),
        )
        stale_fence = lambda_handler.handler(
            {
                "audit_id": audit_id,
                "dispatch_fence_token": "d" * 64,
                "token": valid_token,
            },
            SimpleNamespace(),
        )
        mismatched = lambda_handler.handler(
            {
                "audit_id": "audit-bbbbbbbbbbbb",
                "dispatch_fence_token": fence,
                "token": valid_token,
            },
            SimpleNamespace(),
        )
        accepted = lambda_handler.handler(
            {
                "audit_id": audit_id,
                "dispatch_fence_token": fence,
                "token": valid_token,
            },
            SimpleNamespace(),
        )
    assert missing == missing_fence == empty == tampered == stale_fence == mismatched
    assert missing == {"error": "unauthorized audit invocation"}
    run_audit.assert_awaited_once_with(audit_id, fence)
    assert accepted == {"status": "completed", "audit_id": audit_id}


def test_lambda_handler_rejects_pipeline_and_review_signed_with_the_wrong_key():
    """A well-formed token minted under a rotated or foreign key is not authenticated.

    Shape and length are indistinguishable from a valid token here, so this is
    the case that actually exercises the constant-time comparison rather than
    the cheap format pre-check.
    """
    import lambda_handler

    from app.self_invocation import (
        KIND_PIPELINE,
        KIND_REVIEW,
        self_invocation_token,
    )

    rotated_secret = "rotated-self-invoke-secret"
    run_id = str(uuid.uuid4())
    review_id = str(uuid.uuid4())
    with (
        patch("app.config.settings.audit_self_invoke_secret", "audit-secret"),
        patch("lambda_handler._run_pipeline", new_callable=AsyncMock) as run_pipeline,
        patch(
            "lambda_handler._run_review_pipeline", new_callable=AsyncMock
        ) as run_review,
    ):
        pipeline = lambda_handler.handler(
            {
                "run_id": run_id,
                "token": self_invocation_token(KIND_PIPELINE, run_id, rotated_secret),
            },
            SimpleNamespace(),
        )
        review = lambda_handler.handler(
            {
                "review_id": review_id,
                "token": self_invocation_token(
                    KIND_REVIEW, review_id, rotated_secret
                ),
            },
            SimpleNamespace(),
        )

    run_pipeline.assert_not_awaited()
    run_review.assert_not_awaited()
    assert pipeline == {
        "error": "unauthorized pipeline invocation",
        "run_id": run_id,
    }
    assert review == {
        "error": "unauthorized review invocation",
        "review_id": review_id,
    }


def test_lambda_handler_accepts_only_correctly_signed_pipeline_and_review_invocations():
    """Positive control: a valid signature still runs the pipeline and review.

    Without this the rejections above would also pass against a handler that
    refuses every self-invocation.
    """
    import lambda_handler

    from app.self_invocation import (
        KIND_PIPELINE,
        KIND_REVIEW,
        self_invocation_token,
    )

    run_id = str(uuid.uuid4())
    review_id = str(uuid.uuid4())
    with (
        patch("app.config.settings.audit_self_invoke_secret", "audit-secret"),
        patch("lambda_handler._run_pipeline", new_callable=AsyncMock) as run_pipeline,
        patch(
            "lambda_handler._run_review_pipeline", new_callable=AsyncMock
        ) as run_review,
    ):
        pipeline = lambda_handler.handler(
            {
                "run_id": run_id,
                "token": self_invocation_token(
                    KIND_PIPELINE, run_id, "audit-secret"
                ),
            },
            SimpleNamespace(),
        )
        review = lambda_handler.handler(
            {
                "review_id": review_id,
                "token": self_invocation_token(
                    KIND_REVIEW, review_id, "audit-secret"
                ),
            },
            SimpleNamespace(),
        )

    run_pipeline.assert_awaited_once_with(run_id)
    run_review.assert_awaited_once_with(review_id)
    assert pipeline == {"status": "completed", "run_id": run_id}
    assert review == {"status": "completed", "review_id": review_id}


def test_public_lambda_handler_refuses_operation_dispatch_events():
    import lambda_handler

    with patch(
        "app.services.audit_pipeline.dispatch_audit_jobs",
        new_callable=AsyncMock,
    ) as dispatch:
        result = lambda_handler.handler(
            {"operation": "dispatch_audits"},
            SimpleNamespace(),
        )

    dispatch.assert_not_awaited()
    assert result == {"error": "unsupported invocation"}


def test_iam_only_audit_dispatcher_handler_drives_the_durable_poller():
    import audit_dispatcher_handler

    summary = audit_pipeline.AuditDispatchSummary(
        recovered=2,
        claimed=3,
        scheduled=2,
        released=1,
        terminal=0,
    )
    with patch(
        "app.services.audit_pipeline.dispatch_audit_jobs",
        new_callable=AsyncMock,
        return_value=summary,
    ) as dispatch:
        result = audit_dispatcher_handler.handler(
            {"operation": "dispatch_audits"},
            SimpleNamespace(),
        )

    dispatch.assert_awaited_once_with()
    assert result == {
        "status": "completed",
        "recovered": 2,
        "claimed": 3,
        "scheduled": 2,
        "released": 1,
        "terminal": 0,
    }


def test_iam_only_audit_dispatcher_rejects_public_and_unknown_events():
    import audit_dispatcher_handler

    with patch(
        "app.services.audit_pipeline.dispatch_audit_jobs",
        new_callable=AsyncMock,
    ) as dispatch:
        public_event = audit_dispatcher_handler.handler(
            {"operation": "dispatch_audits", "requestContext": {"http": {}}},
            SimpleNamespace(),
        )
        unknown = audit_dispatcher_handler.handler(
            {"operation": "something_else"}, SimpleNamespace()
        )
        empty_event = audit_dispatcher_handler.handler({}, SimpleNamespace())
        not_a_dict = audit_dispatcher_handler.handler(cast(Any, None), SimpleNamespace())
    dispatch.assert_not_awaited()
    assert public_event == {"error": "unsupported invocation"}
    assert unknown == {"error": "unsupported invocation"}
    assert empty_event == {"error": "unsupported invocation"}
    assert not_a_dict == {"error": "invalid invocation"}


@pytest.mark.asyncio
async def test_dispatch_worker_survives_repeated_database_failures():
    dispatch = AsyncMock(
        side_effect=[
            RuntimeError("database unavailable"),
            ConnectionError("pool exhausted"),
            RuntimeError("transaction aborted"),
        ]
    )
    with patch("app.services.audit_pipeline.dispatch_audit_jobs", dispatch):
        await audit_pipeline._audit_dispatch_worker(
            local_execute=True,
            max_cycles=3,
        )

    assert dispatch.await_count == 3


@pytest.mark.asyncio
async def test_startup_workers_are_bounded_idempotent_and_stoppable():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_worker(*, local_execute: bool) -> None:
        assert local_execute is True
        entered.set()
        await release.wait()

    with patch(
        "app.services.audit_pipeline._audit_dispatch_worker",
        new_callable=AsyncMock,
        side_effect=blocked_worker,
    ) as worker:
        try:
            assert audit_pipeline.start_audit_dispatch_workers() == 2
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            assert audit_pipeline.start_audit_dispatch_workers() == 2
            assert worker.await_count == 2
        finally:
            release.set()
            await audit_pipeline.stop_audit_dispatch_workers()
    assert not audit_pipeline._audit_dispatch_workers


@pytest.mark.asyncio
async def test_fastapi_lifespan_starts_and_stops_dispatch_workers():
    import main

    with (
        patch("main.start_audit_dispatch_workers", return_value=2) as start,
        patch("main.stop_audit_dispatch_workers", new_callable=AsyncMock) as stop,
    ):
        async with main.lifespan(main.app):
            start.assert_called_once_with()
            stop.assert_not_awaited()
    stop.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_runtime_path_only_uses_github_get_helpers():
    with (
        patch(
            "app.github_client.fetch_diff",
            new_callable=AsyncMock,
            return_value="immutable diff",
        ) as mock_diff,
        patch(
            "app.github_client.post_pr_comment",
            new_callable=AsyncMock,
            side_effect=AssertionError("write API must not be called"),
        ) as mock_post,
        patch(
            "app.github_client.create_pull_request",
            new_callable=AsyncMock,
            side_effect=AssertionError("write API must not be called"),
        ) as mock_create,
    ):
        assert (
            await auditor.fetch_audit_diff(
                owner="o",
                repo="r",
                pr_number=42,
                base_sha="b" * 40,
                head_sha="a" * 40,
                token="installation-token",
            )
            == "immutable diff"
        )
        mock_diff.assert_awaited_once_with(
            owner="o",
            repo="r",
            sha="a" * 40,
            base_sha="b" * 40,
            token="installation-token",
            allow_global_token=False,
        )
        assert (
            await auditor.fetch_audit_diff(
                owner="o",
                repo="r",
                head_sha="a" * 40,
            )
            == "immutable diff"
        )
        assert mock_diff.await_count == 2
        mock_post.assert_not_called()
        mock_create.assert_not_called()

    with (
        patch(
            "app.github_client.fetch_diff",
            new_callable=AsyncMock,
            side_effect=RuntimeError("network down"),
        ),
        pytest.raises(auditor.AuditDiffFetchError) as exc_info,
    ):
        await auditor.fetch_audit_diff(
            owner="o",
            repo="r",
            head_sha="a" * 40,
        )
    assert exc_info.value.reason is auditor.AuditRemoteFailureReason.UNKNOWN


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (
            GitHubAuthError("auth"),
            auditor.AuditRemoteFailureReason.AUTH,
        ),
        (
            GitHubRateLimitError("rate"),
            auditor.AuditRemoteFailureReason.RATE_LIMIT,
        ),
        (
            GitHubNetworkError("network"),
            auditor.AuditRemoteFailureReason.NETWORK,
        ),
        (
            GitHubResourceNotFoundError("private"),
            auditor.AuditRemoteFailureReason.PRIVATE_OR_MISSING,
        ),
    ],
)
async def test_diff_fetch_failures_are_typed_and_preserve_cause(error, reason):
    with (
        patch(
            "app.github_client.fetch_diff",
            new_callable=AsyncMock,
            side_effect=error,
        ),
        pytest.raises(auditor.AuditDiffFetchError) as exc_info,
    ):
        await auditor.fetch_audit_diff(
            owner="owner",
            repo="repo",
            head_sha="a" * 40,
        )

    assert exc_info.value.reason is reason
    assert exc_info.value.__cause__ is error


@pytest.mark.asyncio
async def test_verified_empty_diff_is_not_a_fetch_failure():
    with patch(
        "app.github_client.fetch_diff",
        new_callable=AsyncMock,
        return_value="",
    ):
        result = await auditor.fetch_audit_diff(
            owner="owner",
            repo="repo",
            head_sha="a" * 40,
        )

    assert result == ""


@pytest.mark.asyncio
async def test_audit_fetch_rejects_invalid_github_coordinates_before_network():
    with (
        patch(
            "app.github_client.fetch_diff",
            new_callable=AsyncMock,
        ) as mock_diff,
        pytest.raises(ValueError),
    ):
        await auditor.fetch_audit_diff(
            owner="../o",
            repo="r",
            pr_number=1,
            base_sha="b" * 40,
            head_sha="a" * 40,
        )
    with patch(
        "app.github_client.fetch_diff",
        new_callable=AsyncMock,
    ) as mock_diff:
        with pytest.raises(ValueError):
            await auditor.fetch_audit_diff(
                owner="o",
                repo="r",
                pr_number=0,
                base_sha="b" * 40,
                head_sha="a" * 40,
            )
        with pytest.raises(ValueError):
            await auditor.fetch_audit_diff(
                owner="o",
                repo="r",
                head_sha="short",
            )
    mock_diff.assert_not_awaited()


@pytest.mark.asyncio
async def test_audit_github_paths_are_url_encoded_at_client_boundary():
    from app import github_client

    response = github_client.BoundedResponse(
        status_code=200,
        headers=httpx.Headers(),
        content=b"content",
    )
    with patch(
        "app.github_client._bounded_get",
        new_callable=AsyncMock,
        return_value=response,
    ) as mock_get:
        assert await github_client.fetch_file_content(
            owner="owner name",
            repo="repo/name",
            path="dir/a b#c.py",
            sha="a" * 40,
        ) == "content"

    request = mock_get.await_args
    assert request is not None
    url = request.args[1]
    assert "owner%20name" in url
    assert "repo%2Fname" in url
    assert "dir%2Fa%20b%23c.py" in url
    assert request.kwargs["params"] == {"ref": "a" * 40}


# ---------------------------------------------------------------------------
# 6. Empty diff short-circuit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_diff_short_circuits_without_llm():
    patcher, mock_cls, instance = _patched_llm(_four_perspective_router())
    try:
        result = await run_audit(diff_text="   \n", audit_id="audit-555555555555")
    finally:
        patcher.stop()

    assert result.findings == []
    assert result.confidence == 100
    assert instance.complete.await_count == 0
    assert mock_cls.call_count == 0
    assert "No actionable findings" in result.report_markdown


# ---------------------------------------------------------------------------
# 7. Deterministic diff inspection
# ---------------------------------------------------------------------------


def test_parse_diff_files_skips_dev_null_and_dedupes():
    files = parse_diff_files(SAMPLE_DIFF)
    assert files == ["backend/app/auth.py", "backend/app/routers/tokens.py"]
    assert parse_diff_files("--- /dev/null\n+++ b/new_file.py\n") == []
    assert parse_diff_files("") == []


def test_diff_grounding_enforces_exact_old_and_new_hunk_counts():
    valid = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -10,2 +20,2 @@\n"
        "-old\n"
        "+new\n"
        " context\n"
    )
    overflow = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -10,2 +20,2 @@\n"
        "-old\n"
        "+new\n"
        "+extra\n"
        "+third\n"
    )
    incomplete = valid.replace(" context\n", "")

    assert build_diff_line_index(valid) == {"app.py": {20, 21}}
    assert build_diff_line_index(valid + "+beyond\n") == {"app.py": {20, 21}}
    overflow_grounding = build_diff_grounding(overflow)
    assert overflow_grounding.line_index == {}
    assert "app.py" in overflow_grounding.rejected_paths
    incomplete_grounding = build_diff_grounding(incomplete)
    assert incomplete_grounding.line_index == {}
    assert incomplete_grounding.truncated_ranges["app.py"] == {(21, 21)}


def test_diff_grounding_rejects_non_actionable_file_shapes():
    binary = (
        "diff --git a/app.bin b/app.bin\n"
        "index 111..222 100644\n"
        "Binary files a/app.bin and b/app.bin differ\n"
    )
    mode_only = (
        "diff --git a/app.py b/app.py\n"
        "old mode 100644\n"
        "new mode 100755\n"
    )
    header_only = "--- a/app.py\n+++ b/app.py\n"
    deleted = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ /dev/null\n"
        "@@ -1,1 +0,0 @@\n"
        "-old\n"
    )

    assert build_diff_line_index(binary) == {}
    assert build_diff_line_index(mode_only) == {}
    assert build_diff_line_index(header_only) == {}
    assert build_diff_line_index(deleted) == {}
    assert build_diff_line_index(
        "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,1 @@\n+new\n"
    ) == {"new.py": {1}}


def test_diff_grounding_does_not_parse_file_headers_inside_incomplete_hunk():
    diff = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        " first\n"
        "+++ b/other.py\n"
        "+forged\n"
    )

    grounding = build_diff_grounding(diff)

    assert grounding.line_index == {}
    assert "app.py" in grounding.rejected_paths
    assert "other.py" not in grounding.line_index


def test_diff_clipping_rejects_findings_in_incomplete_final_hunk():
    complete = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,1 +1,3 @@\n"
        "+first\n"
        "+second\n"
    )
    grounding = build_diff_grounding(complete, max_chars=len(complete) - 2)
    finding = PerspectiveFinding.model_validate(
        _finding(file_path="app.py", line_start=1, line_end=1)
    )
    truncated_finding = PerspectiveFinding.model_validate(
        _finding(
            file_path="app.py",
            line_start=2,
            line_end=2,
            title="Truncated hunk finding",
        )
    )
    perspectives = [
        PerspectiveResult(
            perspective="security",
            summary="grounding",
            confidence=95,
            findings=[finding, truncated_finding],
        )
    ]

    synthesized = synthesize_findings(
        perspectives,
        complete,
        overall_confidence=95,
        max_chars=len(complete) - 2,
    )

    assert [item.line_start for item in synthesized] == [1]
    assert grounding.clipped is True
    assert grounding.truncated_ranges["app.py"] == {(2, 3)}


def test_ast_diff_summary_lists_files_and_rule_hits():
    summary = build_ast_diff_summary(SAMPLE_DIFF)
    assert "backend/app/auth.py" in summary
    assert "backend/app/routers/tokens.py" in summary
    assert "non-constant-time-token-compare" in summary
    assert "blocking-call-candidate" in summary
    assert "UNCONFIRMED" in summary


def test_ast_diff_summary_uses_parser_backed_changed_file_context():
    source = (
        "\n" * 79
        + "import time\n"
        + "\n"
        + "def verify_token(token: str, stored_token: str):\n"
        + "    return token == stored_token\n"
    )
    summary = build_ast_diff_summary(
        SAMPLE_DIFF,
        {"backend/app/auth.py": source},
    )
    assert "Parser-backed Python AST context" in summary
    assert "verify_token" in summary
    assert "**Signature:**" in summary


def test_ast_summary_redacts_before_its_own_truncation_boundary():
    secret = "sk-ant-api03-" + "Q" * 80
    source = "\n".join(
        ["# " + ("x" * 600) for _ in range(20)]
        + [f"password = '{secret}'"]
    )
    diff = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,1 +1,1 @@\n"
        "+value = 1\n"
    )
    summary = build_ast_diff_summary(diff, {"app.py": source})
    assert len(summary) <= audit_prompts.MAX_AST_CONTEXT_CHARS
    assert secret not in summary
    assert secret[:8] not in summary


def test_typescript_javascript_context_is_honest_bounded_structural_fallback():
    diff = (
        "diff --git a/src/app.ts b/src/app.ts\n"
        "--- a/src/app.ts\n"
        "+++ b/src/app.ts\n"
        "@@ -10,1 +10,2 @@\n"
        " const base = 1;\n"
        "+export const value = base + 1;\n"
    )
    source = "\n".join(f"const value{i} = {i};" for i in range(1, 30)) + "\n"

    summary = build_ast_diff_summary(diff, {"src/app.ts": source})
    report = format_audit_report(
        executive_summary="TypeScript review.",
        findings=[],
        confidence=90,
        status="status",
        audit_target="src/app.ts",
        engine="test-engine",
        remediation_diff="(none)",
        publish_allowed=True,
        analysis_metadata=summary,
    )

    assert "AST parsing unsupported for TypeScript" in summary
    assert "bounded structural fallback" in summary
    assert "not AST coverage" in summary
    assert "Parser-backed Python AST context:\n### `src/app.ts`" not in summary
    assert "AST parsing unsupported for TypeScript" in report
    assert "not AST coverage" in report


@pytest.mark.asyncio
async def test_source_context_fetches_typescript_and_javascript_at_pinned_sha():
    diff = (
        "diff --git a/src/a.ts b/src/a.ts\n"
        "--- a/src/a.ts\n"
        "+++ b/src/a.ts\n"
        "@@ -1,0 +1,1 @@\n"
        "+const a = 1;\n"
        "diff --git a/src/b.js b/src/b.js\n"
        "--- a/src/b.js\n"
        "+++ b/src/b.js\n"
        "@@ -1,0 +1,1 @@\n"
        "+const b = 1;\n"
    )
    with patch(
        "app.github_client.fetch_file_content",
        new_callable=AsyncMock,
        side_effect=lambda **kwargs: f"// {kwargs['path']}\n",
    ) as fetch_file:
        contexts = await auditor.fetch_audit_source_contexts(
            owner="owner",
            repo="repo",
            diff_text=diff,
            sha="a" * 40,
            token="read-only-token",
        )

    assert set(contexts) == {"src/a.ts", "src/b.js"}
    assert fetch_file.await_count == 2
    assert all(call.kwargs["sha"] == "a" * 40 for call in fetch_file.await_args_list)


@pytest.mark.asyncio
async def test_large_top_level_diff_parses_once_without_blocking_event_loop():
    line_count = 2_000
    added_lines = "".join(f"+value_{index} = {index}\n" for index in range(line_count))
    diff = (
        "diff --git a/large.py b/large.py\n"
        "--- a/large.py\n"
        "+++ b/large.py\n"
        f"@@ -0,0 +1,{line_count} @@\n"
        f"{added_lines}"
    )
    source = "".join(f"value_{index} = {index}\n" for index in range(line_count))
    real_parse = auditor.parse_python_source
    parse_calls = 0

    def counted_parse(value: str):
        nonlocal parse_calls
        parse_calls += 1
        time.sleep(0.05)
        return real_parse(value)

    with patch("app.subagents.auditor.parse_python_source", side_effect=counted_parse):
        task = asyncio.create_task(
            build_ast_diff_summary_async(diff, {"large.py": source})
        )
        heartbeat_count = 0
        while not task.done():
            await asyncio.sleep(0.005)
            heartbeat_count += 1
        summary = await task

    assert parse_calls == 1
    assert heartbeat_count >= 3
    assert "Parser-backed Python AST context" in summary
    assert "truncation" not in summary.lower()


# ---------------------------------------------------------------------------
# 8. Independent worker wiring: full read-only execution path
# ---------------------------------------------------------------------------


def _completed_audit_result() -> AuditResult:
    return AuditResult(
        audit_id="audit-888888888888",
        audit_type="pr_audit",
        repo_full_name="o/r",
        target_label="Branch `main` (o/r)",
        engine="test-engine",
        executive_summary="wired core verdict.",
        confidence=92,
        status=build_status_label(0, 1, 92),
        remediation_diff="(no automated remediation suggested; see findings above)",
        report_markdown="## 🛡️ Haunter Autonomous Audit Report",
        publish_allowed=True,
    )


@pytest.mark.asyncio
async def test_full_worker_path_fetches_context_and_calls_core_read_only():
    repo = cast(Repo, SimpleNamespace(id=101, owner="o", name="r"))
    with (
        patch(
            "app.services.audit_pipeline._resolve_audit_target",
            new_callable=AsyncMock,
            return_value=audit_pipeline.AuditTarget(
                base_sha=None,
                head_sha="a" * 40,
            ),
        ),
        patch(
            "app.subagents.auditor.fetch_audit_diff",
            new_callable=AsyncMock,
            return_value=SAMPLE_DIFF,
        ) as mock_fetch,
        patch(
            "app.subagents.auditor.fetch_audit_source_contexts",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "app.subagents.auditor.fetch_audit_ci_context",
            new_callable=AsyncMock,
            return_value="bounded CI failure output",
        ),
        patch(
            "app.subagents.auditor.run_audit",
            new_callable=AsyncMock,
            return_value=_completed_audit_result(),
        ) as mock_run,
    ):
        result = await audit_pipeline.execute_audit_job(
            audit_id="audit-888888888888",
            audit_type="ci_failure_audit",
            repo=repo,
            ref="main",
            pr_number=None,
            base_sha=None,
            head_sha=None,
            workflow_run_id=123,
            token="installation-token",
        )

    assert result.status == "completed"
    assert result.result.audit_id == "audit-888888888888"
    mock_fetch.assert_awaited_once_with(
        owner="o",
        repo="r",
        pr_number=None,
        base_sha=None,
        head_sha="a" * 40,
        token="installation-token",
    )
    assert "bounded CI failure output" in mock_run.call_args.kwargs["repo_context"]


@pytest.mark.asyncio
async def test_full_worker_path_stops_before_llm_without_diff():
    repo = cast(Repo, SimpleNamespace(id=101, owner="o", name="r"))
    with (
        patch(
            "app.services.audit_pipeline._resolve_audit_target",
            new_callable=AsyncMock,
            return_value=audit_pipeline.AuditTarget(
                base_sha=None,
                head_sha="a" * 40,
            ),
        ),
        patch(
            "app.subagents.auditor.fetch_audit_diff",
            new_callable=AsyncMock,
            return_value="",
        ),
        patch(
            "app.subagents.auditor.run_audit",
            new_callable=AsyncMock,
            side_effect=AssertionError("no LLM without diff context"),
        ) as mock_run,
    ):
        result = await audit_pipeline.execute_audit_job(
            audit_id="audit-999999999999",
            audit_type="pr_audit",
            repo=repo,
            ref="main",
            pr_number=42,
            base_sha="b" * 40,
            head_sha=None,
            workflow_run_id=None,
            token=None,
        )
    assert result.status == "skipped_no_diff"
    mock_run.assert_not_called()


@pytest.mark.asyncio
async def test_process_worker_marks_failure_without_raising():
    job = SimpleNamespace(
        audit_id="audit-aaaaaaaaaaaa",
        audit_type="ci_failure_audit",
        ref="main",
        pr_number=None,
        base_sha=None,
        head_sha="a" * 40,
        workflow_run_id=123,
    )
    repo = cast(Repo, SimpleNamespace(id=101, owner="o", name="r"))
    with (
        patch(
            "app.services.audit_pipeline._claim_processing_job",
            new_callable=AsyncMock,
            return_value=(job, repo, 1),
        ),
        patch(
            "app.services.audit_pipeline.get_auditor_installation_token",
            new_callable=AsyncMock,
            return_value="read-only-installation-token",
        ),
        patch(
            "app.services.audit_pipeline.execute_audit_job",
            new_callable=AsyncMock,
            side_effect=RuntimeError("LLM outage"),
        ),
        patch(
            "app.services.audit_pipeline._retry_or_fail_audit_job",
            new_callable=AsyncMock,
            return_value=False,
        ) as mock_retry,
    ):
        result = await audit_pipeline.process_audit_job("audit-aaaaaaaaaaaa", "a" * 64)
    assert result is False
    mock_retry.assert_awaited_once()
    retry_call = mock_retry.await_args
    assert retry_call is not None
    assert retry_call.args[0:2] == ("audit-aaaaaaaaaaaa", 1)


@pytest.mark.asyncio
async def test_worker_resolves_auditor_credentials_only_through_read_only_provider():
    job = SimpleNamespace(
        audit_id="audit-bbbbbbbbbbbb",
        audit_type="pr_audit",
        ref="main",
        pr_number=42,
        base_sha="b" * 40,
        head_sha="a" * 40,
        workflow_run_id=None,
    )
    repo = cast(
        Repo,
        SimpleNamespace(
            id=101,
            owner="o",
            name="r",
            auditor_github_install_id=55,
        ),
    )
    with (
        patch(
            "app.services.audit_pipeline._claim_processing_job",
            new_callable=AsyncMock,
            return_value=(job, repo, 1),
        ),
        patch(
            "app.services.audit_pipeline.get_auditor_installation_token",
            new_callable=AsyncMock,
            return_value="read-only-token",
        ) as read_credentials,
        patch(
            "app.github.pr.get_installation_token",
            new_callable=AsyncMock,
            side_effect=AssertionError("write-capable credential provider must not be used"),
        ) as write_credentials,
        patch(
            "app.services.audit_pipeline.execute_audit_job",
            new_callable=AsyncMock,
            return_value=audit_pipeline.AuditExecutionOutcome(
                status="completed",
                result=object(),
            ),
        ) as execute,
        patch(
            "app.services.audit_pipeline._complete_audit_job",
            new_callable=AsyncMock,
            return_value=True,
        ) as complete,
    ):
        result = await audit_pipeline.process_audit_job("audit-bbbbbbbbbbbb", "b" * 64)

    assert result is True
    read_credentials.assert_awaited_once_with(repo)
    write_credentials.assert_not_awaited()
    execute_call = execute.await_args
    assert execute_call is not None
    assert execute_call.kwargs["token"] == "read-only-token"
    assert execute_call.kwargs["base_sha"] == "b" * 40
    assert execute_call.kwargs["attempt"] == 1
    complete.assert_awaited_once_with("audit-bbbbbbbbbbbb", 1)


@pytest.mark.asyncio
async def test_worker_credential_failure_does_not_fall_back_or_execute():
    job = SimpleNamespace(
        audit_id="audit-cccccccccccc",
        audit_type="pr_audit",
        ref="main",
        pr_number=42,
        base_sha="b" * 40,
        head_sha="a" * 40,
        workflow_run_id=None,
    )
    repo = cast(Repo, SimpleNamespace(id=101, owner="o", name="r"))
    with (
        patch(
            "app.services.audit_pipeline._claim_processing_job",
            new_callable=AsyncMock,
            return_value=(job, repo, 1),
        ),
        patch(
            "app.services.audit_pipeline.get_auditor_installation_token",
            new_callable=AsyncMock,
            side_effect=RuntimeError("read-only credential unavailable"),
        ),
        patch(
            "app.github.pr.get_installation_token",
            new_callable=AsyncMock,
        ) as write_credentials,
        patch(
            "app.services.audit_pipeline.execute_audit_job",
            new_callable=AsyncMock,
        ) as execute,
        patch(
            "app.services.audit_pipeline._retry_or_fail_audit_job",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        result = await audit_pipeline.process_audit_job("audit-cccccccccccc", "c" * 64)

    assert result is False
    write_credentials.assert_not_awaited()
    execute.assert_not_awaited()


def test_audit_pipeline_has_no_write_capable_credential_reference():
    source = inspect.getsource(audit_pipeline)
    assert "app.github.pr" not in source
    assert "get_installation_token(repo)" not in source


@pytest.mark.asyncio
async def test_full_worker_path_resolves_manual_pr_sha_before_source_fetch():
    repo = cast(Repo, SimpleNamespace(id=101, owner="o", name="r"))
    with (
        patch(
            "app.github_client.fetch_pull_request",
            new_callable=AsyncMock,
            return_value={
                "base": {"sha": "a" * 40},
                "head": {"sha": "b" * 40},
            },
        ),
        patch(
            "app.subagents.auditor.fetch_audit_diff",
            new_callable=AsyncMock,
            return_value=SAMPLE_DIFF,
        ) as mock_fetch,
        patch(
            "app.subagents.auditor.fetch_audit_source_contexts",
            new_callable=AsyncMock,
            return_value={},
        ) as mock_sources,
        patch(
            "app.subagents.auditor.run_audit",
            new_callable=AsyncMock,
            return_value=_completed_audit_result(),
        ) as mock_run,
    ):
        await audit_pipeline.execute_audit_job(
            audit_id="audit-bbbbbbbbbbbb",
            audit_type="manual_audit",
            repo=repo,
            ref=None,
            pr_number=77,
            base_sha=None,
            head_sha=None,
            workflow_run_id=None,
            token="installation-token",
        )
    mock_fetch.assert_awaited_once_with(
        owner="o",
        repo="r",
        pr_number=77,
        base_sha="a" * 40,
        head_sha="b" * 40,
        token="installation-token",
    )
    source_call = mock_sources.await_args
    assert source_call is not None
    assert source_call.kwargs["sha"] == "b" * 40
    mock_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_pr_advancement_rejects_instead_of_mixing_commits():
    repo = cast(Repo, SimpleNamespace(id=101, owner="o", name="r"))
    first_target = {
        "base": {"sha": "a" * 40},
        "head": {"sha": "b" * 40},
    }
    advanced_target = {
        "base": {"sha": "a" * 40},
        "head": {"sha": "c" * 40},
    }
    with (
        patch(
            "app.github_client.fetch_pull_request",
            new_callable=AsyncMock,
            side_effect=[first_target, advanced_target],
        ),
        patch(
            "app.github_client.fetch_diff",
            new_callable=AsyncMock,
            return_value=SAMPLE_DIFF,
        ) as fetch_diff,
        patch(
            "app.subagents.auditor.fetch_audit_source_contexts",
            new_callable=AsyncMock,
        ) as fetch_sources,
        pytest.raises(auditor.AuditTargetChangedError),
    ):
        await audit_pipeline.execute_audit_job(
            audit_id="audit-dddddddddddd",
            audit_type="pr_audit",
            repo=repo,
            ref="feature",
            pr_number=42,
            base_sha="a" * 40,
            head_sha="b" * 40,
            workflow_run_id=None,
            token="read-only-token",
        )

    fetch_diff.assert_awaited_once_with(
        owner="o",
        repo="r",
        sha="b" * 40,
        base_sha="a" * 40,
        token="read-only-token",
        allow_global_token=False,
    )
    fetch_sources.assert_not_awaited()


# ---------------------------------------------------------------------------
# 9. Resilience: retry recovery + total failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_retry_recovers_malformed_perspective():
    seen: dict[str, int] = {}

    async def _flaky(*, messages=None, **kwargs):
        user_text = " ".join(m.get("content", "") for m in (messages or []))
        perspective = next(
            p
            for p in ("security", "correctness", "performance", "architecture")
            if f"## Perspective\n{p}" in user_text
        )
        seen[perspective] = seen.get(perspective, 0) + 1
        if perspective == "security" and seen[perspective] == 1:
            return _llm_response("definitely not json {{{")
        blurb = _four_perspective_router()
        return await blurb(messages=messages, **kwargs)

    patcher, _, instance = _patched_llm(_flaky)
    try:
        result = await run_audit(diff_text=SAMPLE_DIFF, audit_id="audit-666666666666")
    finally:
        patcher.stop()

    assert {f.perspective for f in result.findings} == {
        "security",
        "correctness",
        "performance",
        "architecture",
    }
    assert instance.complete.await_count == 5  # 4 first attempts + 1 retry


@pytest.mark.asyncio
async def test_all_perspectives_failing_raises_analysis_error():
    async def _always_bad(*, messages=None, **kwargs):
        return _llm_response("not json at all")

    patcher, _, _ = _patched_llm(_always_bad)
    try:
        with pytest.raises(AuditAnalysisError):
            await run_audit(diff_text=SAMPLE_DIFF, audit_id="audit-777777777777")
    finally:
        patcher.stop()


# ---------------------------------------------------------------------------
# 10. Prompt pack sanity: 4 checklists reference the skills ruleset
# ---------------------------------------------------------------------------


def test_prompt_pack_covers_ruleset_checklists():
    assert set(audit_prompts.PERSPECTIVES) == {
        "security",
        "correctness",
        "performance",
        "architecture",
    }
    security = audit_prompts.PERSPECTIVE_INSTRUCTIONS["security"].lower()
    for keyword in (
        "ssrf",
        "tenant",
        "compare_digest",
        "injection",
        "secret",
        "owasp",
    ):
        assert keyword in security, f"security checklist missing {keyword!r}"
    performance = audit_prompts.PERSPECTIVE_INSTRUCTIONS["performance"].lower()
    assert "n+1" in performance or "n + 1" in performance
    assert "async" in performance
    messages = audit_prompts.build_perspective_messages(
        "security", SAMPLE_DIFF, "ast summary", "repo ctx"
    )
    assert messages[0]["role"] == "system"
    assert "READ-ONLY" in messages[0]["content"]
    assert "backend/app/auth.py" in messages[1]["content"]


def test_finding_validation_rejects_unsafe_paths_extra_fields_and_coercion():
    payload = _finding()
    with pytest.raises(ValidationError):
        PerspectiveFinding.model_validate({**payload, "file_path": "../outside.py"})
    with pytest.raises(ValidationError):
        PerspectiveFinding.model_validate({**payload, "file_path": "/etc/passwd"})
    with pytest.raises(ValidationError):
        PerspectiveFinding.model_validate({**payload, "unexpected": True})
    with pytest.raises(ValidationError):
        PerspectiveFinding.model_validate({**payload, "confidence": "90"})

    normalized = PerspectiveFinding.model_validate(
        {**payload, "severity": "warning", "line_start": 20, "line_end": 10}
    )
    assert normalized.severity == "WARNING"
    assert normalized.line_end == normalized.line_start == 20
    assert normalized.description


@pytest.mark.parametrize(
    "path",
    [
        "backend/app/auth\n.py",
        "backend/app/auth\r.py",
        "backend/app/auth\t.py",
        "backend/app/auth .py",
        "backend/app/auth\u00a0.py",
        "backend/app/auth\u200b.py",
    ],
)
def test_finding_validation_rejects_all_whitespace_and_control_paths(path: str):
    with pytest.raises(ValidationError):
        PerspectiveFinding.model_validate({**_finding(), "file_path": path})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"audit_id": "bad"},
        {"audit_type": "unknown"},
        {"repo_full_name": "../owner/repo"},
        {"head_sha": "abc"},
        {"pr_number": 0},
        {"engine": "bad engine\n"},
    ],
)
async def test_run_audit_rejects_invalid_identity_and_github_coordinates(kwargs: dict):
    complete = AsyncMock()
    with pytest.raises(ValueError):
        await run_audit(
            diff_text=SAMPLE_DIFF,
            llm_client=_mock_llm_client(complete),
            **kwargs,
        )
    complete.assert_not_awaited()


def test_synthesis_filters_ungrounded_duplicates_and_assigns_stable_ids():
    finding = PerspectiveFinding.model_validate(_finding(title="Stable finding"))
    duplicate = finding.model_copy(update={"confidence": 85})
    wrong_file = finding.model_copy(
        update={"file_path": "backend/app/not_touched.py", "title": "Wrong file"}
    )
    wrong_line = finding.model_copy(update={"line_start": 500, "line_end": 500, "title": "Wrong line"})
    perspectives = [
        PerspectiveResult(
            perspective="security",
            summary="security verdict",
            confidence=95,
            findings=[finding, wrong_file, wrong_line],
        ),
        PerspectiveResult(
            perspective="correctness",
            summary="correctness verdict",
            confidence=85,
            findings=[duplicate],
        ),
    ]

    first = synthesize_findings(perspectives, SAMPLE_DIFF, overall_confidence=90)
    second = synthesize_findings(list(reversed(perspectives)), SAMPLE_DIFF, overall_confidence=90)

    assert len(first) == len(second) == 1
    assert first[0].id == second[0].id
    assert first[0].perspective == "security"
    assert re.fullmatch(r"AUD-[0-9A-F]{12}", first[0].id)
    assert first[0].description == finding.description


def test_confidence_dedup_keeps_high_confidence_duplicate_in_either_input_order():
    low = PerspectiveFinding.model_validate(
        _finding(title="Same duplicate", confidence=40)
    )
    high = low.model_copy(update={"confidence": 99})
    low_result = PerspectiveResult(
        perspective="security",
        summary="low",
        confidence=90,
        findings=[low],
    )
    high_result = PerspectiveResult(
        perspective="correctness",
        summary="high",
        confidence=90,
        findings=[high],
    )

    forward = synthesize_findings(
        [low_result, high_result],
        SAMPLE_DIFF,
        overall_confidence=90,
    )
    reverse = synthesize_findings(
        [high_result, low_result],
        SAMPLE_DIFF,
        overall_confidence=90,
    )

    assert len(forward) == len(reverse) == 1
    assert forward[0].confidence == reverse[0].confidence == 99
    assert forward[0].perspective == reverse[0].perspective == "correctness"


@pytest.mark.parametrize(
    ("diff", "line"),
    [
        (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n"
            "-secret = True\n",
            1,
        ),
        (
            "diff --git a/app.py b/app.py\n"
            "old mode 100644\n"
            "new mode 100755\n"
            "--- a/app.py\n"
            "+++ b/app.py\n",
            1,
        ),
        (
            "diff --git a/image.png b/image.png\n"
            "Binary files a/image.png and b/image.png differ\n",
            1,
        ),
        (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n",
            1,
        ),
        (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,5 +1,5 @@\n"
            "+only_observed_line = True\n",
            2,
        ),
        (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,1 +1,1 @@\n"
            "+value = 1\n",
            84,
        ),
    ],
)
def test_grounding_rejects_unobserved_diff_lines(diff: str, line: int):
    candidate = PerspectiveFinding.model_validate(
        _finding(file_path="app.py", line_start=line, line_end=line)
    )
    if line == 1 and "image.png" in diff:
        candidate = candidate.model_copy(update={"file_path": "image.png"})
    result = PerspectiveResult(
        perspective="security",
        summary="candidate",
        confidence=90,
        findings=[candidate],
    )
    assert synthesize_findings([result], diff, overall_confidence=90) == []


def test_grounding_index_contains_only_observed_added_and_context_lines():
    diff = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -10,3 +10,3 @@\n"
        " context_before\n"
        "-removed = True\n"
        "+added = True\n"
        " context_after\n"
    )
    assert build_diff_line_index(diff) == {"app.py": {10, 11, 12}}


def test_formatter_sanitizes_generated_markdown_and_bounds_report_output():
    malicious = {
        "id": "AUD-ABC123",
        "file_path": "backend/app/auth.py",
        "line_start": 84,
        "line_end": 84,
        "perspective": "security",
        "severity": "BLOCKER",
        "category": "Output Safety",
        "title": "Malicious ![remote](https://tracker.invalid) @alice\n## forged heading",
        "description": "<script>alert(1)</script> @victim",
        "suggested_fix": "```python\nprint('safe')\n```",
        "confidence": 99,
        "informational_only": False,
    }
    generated_findings = [
        {
            **malicious,
            "id": f"AUD-{index:06d}",
            "title": f"Finding {index} " + "x" * 220,
            "description": "y" * 1_100,
            "suggested_fix": "```\n" + "z" * 2_800,
        }
        for index in range(39)
    ]
    findings = [malicious, *generated_findings]
    report = format_audit_report(
        executive_summary="<script>alert(2)</script> @summary-user",
        findings=findings,
        confidence=99,
        status=build_status_label(1, 0, 99),
        audit_target="Commit `abc123` / PR `#42`",
        engine="test-engine",
        remediation_diff="```diff\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n```",
        publish_allowed=True,
    )

    assert len(report) <= MAX_REPORT_CHARS
    assert "<script>" not in report
    assert "&lt;script&gt;" in report
    assert "&#64;alice" in report and "&#64;summary-user" in report
    assert "![remote]" not in report
    assert "Commit `abc123` / PR `#42`" in report
    assert 0 < report.count("#### ") <= audit_prompts.MAX_REPORT_FINDINGS
    assert "additional findings omitted" in report
    assert "### 🛠️ Remediation Unified Diff" in report


def test_formatter_escapes_generated_html_paths():
    report = format_audit_report(
        executive_summary="safe",
        findings=[
            {
                "id": "AUD-HTML1",
                "file_path": "backend/<script>alert(1)</script>.py",
                "line_start": 1,
                "line_end": 1,
                "perspective": "security",
                "severity": "WARNING",
                "category": "Output safety",
                "title": "HTML path",
                "description": "safe",
                "suggested_fix": None,
                "confidence": 90,
                "informational_only": False,
            }
        ],
        confidence=90,
        status=build_status_label(0, 1, 90),
        audit_target="PR #1",
        engine="test-engine",
        remediation_diff="(none)",
        publish_allowed=True,
    )
    assert "<script>" not in report
    assert "backend/&lt;script&gt;alert(1)&lt;/script&gt;.py" in report


@pytest.mark.asyncio
async def test_four_perspectives_execute_concurrently_with_fixed_bound():
    all_started = asyncio.Event()
    active = 0
    max_active = 0

    async def _complete(*, messages=None, **kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        if active == 4:
            all_started.set()
        try:
            await asyncio.wait_for(all_started.wait(), timeout=0.5)
        finally:
            active -= 1
        user_text = " ".join(message.get("content", "") for message in messages or [])
        perspective = next(
            name
            for name in audit_prompts.PERSPECTIVES
            if f"## Perspective\n{name}" in user_text
        )
        return _llm_response(_perspective_payload(f"{perspective} clean", 100, []))

    complete = AsyncMock(side_effect=_complete)
    result = await run_audit(
        diff_text=SAMPLE_DIFF,
        llm_client=_mock_llm_client(complete),
    )

    assert complete.await_count == 4
    assert all(call.kwargs["db"] is None for call in complete.await_args_list)
    assert max_active == 4
    assert result.confidence == 100
    assert result.findings == []


@pytest.mark.asyncio
async def test_partial_llm_failure_degrades_without_logging_error_payload(caplog):
    async def _complete(*, messages=None, **kwargs):
        user_text = " ".join(message.get("content", "") for message in messages or [])
        perspective = next(
            name
            for name in audit_prompts.PERSPECTIVES
            if f"## Perspective\n{name}" in user_text
        )
        if perspective == "security":
            raise RuntimeError("sensitive-error-payload")
        return _llm_response(_perspective_payload(f"{perspective} clean", 100, []))

    complete = AsyncMock(side_effect=_complete)
    with caplog.at_level(logging.WARNING):
        result = await run_audit(
            diff_text=SAMPLE_DIFF,
            llm_client=_mock_llm_client(complete),
        )

    assert complete.await_count == 4
    assert result.perspectives[0].succeeded is False
    assert result.confidence == 74
    assert result.informational_only is True
    assert result.publish_allowed is False
    assert "Informational Only" in result.status
    assert "**Publication Policy:** Suppressed" in result.report_markdown
    assert "### ℹ️ Informational Audit Result" in result.report_markdown
    assert "sensitive-error-payload" not in caplog.text


@pytest.mark.asyncio
async def test_prompt_inputs_are_bounded_redacted_and_explicitly_truncated():
    direct_messages = audit_prompts.build_perspective_messages(
        "security",
        "d" * (AUDIT_MAX_DIFF_CHARS * 2),
        "a" * (audit_prompts.MAX_AST_CONTEXT_CHARS * 2),
        "r" * (audit_prompts.MAX_REPO_CONTEXT_CHARS * 2),
    )
    assert len(direct_messages[0]["content"]) <= MAX_SYSTEM_PROMPT_CHARS
    assert len(direct_messages[1]["content"]) <= MAX_USER_PROMPT_CHARS

    leaked_openai = "sk-abcdefghijklmnopqrstuvwxyz1234567890"
    leaked_connection = "postgresql://admin:password@database.invalid/prod"
    leaked_auth = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz1234567890"
    tainted_diff = SAMPLE_DIFF.replace(
        "+import time",
        f"+import time\n+OPENAI_KEY = '{leaked_openai}'",
    ) + ("x" * AUDIT_MAX_DIFF_CHARS)
    captured: list[list[dict[str, str]]] = []

    async def _complete(*, messages=None, **kwargs):
        captured.append(messages or [])
        user_text = " ".join(message.get("content", "") for message in messages or [])
        perspective = next(
            name
            for name in audit_prompts.PERSPECTIVES
            if f"## Perspective\n{name}" in user_text
        )
        return _llm_response(_perspective_payload(f"{perspective} clean", 100, []))

    result = await run_audit(
        diff_text=tainted_diff,
        ast_context=f"{leaked_auth}\n" + ("a" * audit_prompts.MAX_AST_CONTEXT_CHARS),
        repo_context=f"{leaked_connection}\n" + ("r" * audit_prompts.MAX_REPO_CONTEXT_CHARS),
        llm_client=_mock_llm_client(AsyncMock(side_effect=_complete)),
    )
    serialized_prompts = json.dumps(captured)

    assert len(captured) == 4
    assert all(len(messages[0]["content"]) <= MAX_SYSTEM_PROMPT_CHARS for messages in captured)
    assert all(len(messages[1]["content"]) <= MAX_USER_PROMPT_CHARS for messages in captured)
    assert "TRUNCATED" in captured[0][1]["content"]
    assert leaked_openai not in serialized_prompts
    assert leaked_connection not in serialized_prompts
    assert leaked_auth not in serialized_prompts
    assert "[REDACTED" in serialized_prompts
    assert leaked_openai not in result.report_markdown
    assert result.findings == []


async def _async_chunks(payload: bytes, size: int):
    for offset in range(0, len(payload), size):
        yield payload[offset : offset + size]


def _credential_stream_client(payload: bytes, status_code: int = 201) -> MagicMock:
    """Build a mock httpx client whose streaming POST yields `payload`."""
    stream = MagicMock()
    response = MagicMock(status_code=status_code, is_error=status_code >= 400)
    response.headers = {}
    response.aiter_bytes = MagicMock(return_value=_async_chunks(payload, 8 * 1024))
    stream.__aenter__ = AsyncMock(return_value=response)
    stream.__aexit__ = AsyncMock(return_value=None)
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.stream = MagicMock(return_value=stream)
    return client


@pytest.mark.asyncio
async def test_read_only_auditor_provider_uses_separate_app_and_allowlists_scopes():
    from app.github import auditor as auditor_credentials

    repo = SimpleNamespace(auditor_github_install_id=55)
    body = json.dumps(
        {
            "token": "read-only-installation-token",
            "permissions": {
                "contents": "read",
                "pull_requests": "read",
                "metadata": "read",
            },
        }
    ).encode()
    client = _credential_stream_client(body)

    with (
        patch("app.github.auditor.settings.github_auditor_app_id", "12345"),
        patch(
            "app.github.auditor.settings.github_auditor_app_private_key",
            "separate-read-only-key",
        ),
        patch(
            "app.github.auditor._build_read_only_jwt",
            return_value="read-only-app-jwt",
        ),
        patch("app.github.auditor.httpx.AsyncClient", return_value=client),
        patch.dict(auditor_credentials._TOKEN_CACHE, {}, clear=True),
    ):
        token = await auditor_credentials.get_auditor_installation_token(repo)

    assert token == "read-only-installation-token"
    request = client.stream.call_args
    assert request is not None
    assert request.args[1].endswith("/app/installations/55/access_tokens")
    assert request.kwargs["headers"]["Authorization"] == "Bearer read-only-app-jwt"
    assert request.kwargs["headers"]["X-GitHub-Api-Version"] == "2022-11-28"


@pytest.mark.asyncio
async def test_read_only_auditor_provider_rejects_oversized_response_stream():
    from app.github import auditor as auditor_credentials

    repo = SimpleNamespace(auditor_github_install_id=55)
    oversized = json.dumps(
        {
            "token": "x" * (auditor_credentials.MAX_CREDENTIAL_RESPONSE_BYTES + 1),
            "permissions": {
                "contents": "read",
                "pull_requests": "read",
                "metadata": "read",
            },
        }
    ).encode()
    client = _credential_stream_client(oversized)

    with (
        patch("app.github.auditor.settings.github_auditor_app_id", "12345"),
        patch(
            "app.github.auditor.settings.github_auditor_app_private_key",
            "separate-read-only-key",
        ),
        patch(
            "app.github.auditor._build_read_only_jwt",
            return_value="read-only-app-jwt",
        ),
        patch("app.github.auditor.httpx.AsyncClient", return_value=client),
        patch.dict(auditor_credentials._TOKEN_CACHE, {}, clear=True),
        pytest.raises(
            auditor_credentials.AuditorCredentialResponseTooLargeError
        ),
    ):
        await auditor_credentials.get_auditor_installation_token(repo)

    assert auditor_credentials._TOKEN_CACHE == {}


@pytest.mark.asyncio
async def test_read_only_auditor_provider_rejects_oversized_declared_content_length():
    from app.github import auditor as auditor_credentials

    repo = SimpleNamespace(auditor_github_install_id=55)
    client = _credential_stream_client(b'{"token":"t","permissions":{}}')
    client.stream.return_value.__aenter__.return_value.headers = {
        "content-length": str(auditor_credentials.MAX_CREDENTIAL_RESPONSE_BYTES + 1)
    }

    with (
        patch("app.github.auditor.settings.github_auditor_app_id", "12345"),
        patch(
            "app.github.auditor.settings.github_auditor_app_private_key",
            "separate-read-only-key",
        ),
        patch(
            "app.github.auditor._build_read_only_jwt",
            return_value="read-only-app-jwt",
        ),
        patch("app.github.auditor.httpx.AsyncClient", return_value=client),
        patch.dict(auditor_credentials._TOKEN_CACHE, {}, clear=True),
        pytest.raises(
            auditor_credentials.AuditorCredentialResponseTooLargeError
        ),
    ):
        await auditor_credentials.get_auditor_installation_token(repo)


def test_read_only_permission_allowlist_is_closed():
    from app.github import auditor as auditor_credentials

    read_only = {
        "contents": "read",
        "pull_requests": "read",
        "metadata": "read",
    }
    auditor_credentials._validate_read_only_permissions({"permissions": read_only})
    auditor_credentials._validate_read_only_permissions(
        {"permissions": {**read_only, "single_file": "read"}}
    )

    rejected = [
        {"permissions": {**read_only, "contents": "write"}},
        {"permissions": {**read_only, "administration": "read"}},
        {"permissions": {**read_only, "issues": "write"}},
        {"permissions": {**read_only, "metadata": "admin"}},
        {"permissions": {**read_only, "contents": None}},
        {"permissions": {"contents": "read", "metadata": "read"}},
        {"permissions": {"contents": "none", "pull_requests": "read", "metadata": "read"}},
        {"permissions": {}},
        {"permissions": "read"},
        {},
    ]
    for payload in rejected:
        with pytest.raises(auditor_credentials.AuditorCredentialError):
            auditor_credentials._validate_read_only_permissions(payload)


def test_auditor_reads_never_fall_back_to_the_global_personal_access_token():
    from app import github_client as github

    with (
        patch("app.github_client.settings.github_token", "global-pat"),
        pytest.raises(GitHubAuthError),
    ):
        github._build_headers(token=None, allow_global_token=False)
        github._build_headers(token="   ", allow_global_token=False)

    with patch("app.github_client.settings.github_token", "global-pat"):
        allowed = github._build_headers(token=None)
        assert allowed["Authorization"] == "Bearer global-pat"
        explicit = github._build_headers(token="tok", allow_global_token=False)
        assert explicit["Authorization"] == "Bearer tok"


def test_auditor_fetch_helpers_disable_global_token_fallback():
    import inspect as _inspect

    from app.subagents import auditor as audit_subagent

    for func in (
        audit_subagent.fetch_audit_diff,
        audit_subagent.fetch_audit_source_contexts,
        audit_subagent.fetch_audit_ci_context,
    ):
        source = _inspect.getsource(func)
        assert "allow_global_token=False" in source, func.__name__

    from app.services import audit_pipeline

    assert (
        "allow_global_token=False"
        in _inspect.getsource(audit_pipeline._fetch_pr_target)
    )


@pytest.mark.asyncio
async def test_read_only_auditor_provider_fails_closed_when_configuration_is_missing():
    from app.github import auditor as auditor_credentials

    with (
        patch("app.github.auditor.settings.github_auditor_app_id", None),
        patch("app.github.auditor.settings.github_auditor_app_private_key", None),
        pytest.raises(auditor_credentials.AuditorCredentialError),
    ):
        await auditor_credentials.get_auditor_installation_token(
            SimpleNamespace(auditor_github_install_id=55)
        )
    with pytest.raises(auditor_credentials.AuditorCredentialError):
        await auditor_credentials.get_auditor_installation_token(
            SimpleNamespace(auditor_github_install_id=None)
        )


@pytest.mark.asyncio
async def test_auditor_installation_token_must_be_non_blank():
    """A present-but-blank token is an unset credential, not a usable one.

    It passes a bare truthiness check, would sit in the token cache for the
    whole TTL, and would only surface much later as an opaque 401 from GitHub.
    """
    from app.github import auditor as auditor_credentials

    read_only = {"contents": "read", "pull_requests": "read", "metadata": "read"}

    def provider(token_value: str) -> MagicMock:
        return _credential_stream_client(
            json.dumps({"token": token_value, "permissions": read_only}).encode()
        )

    for blank in ("", "   ", "\t\n", "\r\n "):
        with (
            patch("app.github.auditor.settings.github_auditor_app_id", "12345"),
            patch(
                "app.github.auditor.settings.github_auditor_app_private_key",
                "separate-read-only-key",
            ),
            patch(
                "app.github.auditor._build_read_only_jwt",
                return_value="read-only-app-jwt",
            ),
            patch("app.github.auditor.httpx.AsyncClient", return_value=provider(blank)),
            patch.dict(auditor_credentials._TOKEN_CACHE, {}, clear=True),
            pytest.raises(auditor_credentials.AuditorCredentialError),
        ):
            await auditor_credentials.get_auditor_installation_token(
                SimpleNamespace(auditor_github_install_id=55)
            )
            # A blank credential must never be cached: caching it would serve
            # the same unusable token for the whole TTL.
            assert auditor_credentials._TOKEN_CACHE == {}

    # A token with real content is used verbatim, so a credential that happens
    # to carry surrounding whitespace is not silently rewritten.
    padded = "  read-only-installation-token  "
    with (
        patch("app.github.auditor.settings.github_auditor_app_id", "12345"),
        patch(
            "app.github.auditor.settings.github_auditor_app_private_key",
            "separate-read-only-key",
        ),
        patch(
            "app.github.auditor._build_read_only_jwt",
            return_value="read-only-app-jwt",
        ),
        patch("app.github.auditor.httpx.AsyncClient", return_value=provider(padded)),
        patch.dict(auditor_credentials._TOKEN_CACHE, {}, clear=True),
    ):
        token = await auditor_credentials.get_auditor_installation_token(
            SimpleNamespace(auditor_github_install_id=55)
        )
        assert token == padded
        assert auditor_credentials._TOKEN_CACHE[55][0] == padded


@pytest.mark.asyncio
async def test_oversized_llm_output_is_rejected_without_unbounded_retry():
    complete = AsyncMock(
        return_value=_llm_response("x" * (MAX_LLM_RESPONSE_CHARS + 1))
    )
    with pytest.raises(AuditAnalysisError):
        await run_audit(
            diff_text=SAMPLE_DIFF,
            llm_client=_mock_llm_client(complete),
        )
    assert complete.await_count == 4


def test_single_redactor_covers_generic_and_provider_credentials():
    secrets = {
        "generic": "password = 'correct-horse-battery-staple'",
        "aws": "AKIA" + "A" * 16,
        "aws_secret": "aws_secret_access_key = '" + "E" * 40 + "'",
        "anthropic": "sk-ant-api03-" + "B" * 32,
        "groq": "gsk_" + "G" * 40,
        "groq_assignment": "GROQ_API_KEY = 'gsk_" + "H" * 40 + "'",
        "stripe": "sk_live_" + "C" * 24,
        "github": "ghp_" + "D" * 36,
        "authorization_opaque": "Authorization: opaqueprovidercredential123456",
        "authorization_token": "Authorization: token opaqueprovidercredential123456",
        "authorization_apikey": "Proxy-Authorization: ApiKey opaqueprovidercredential123456",
    }
    redacted = audit_prompts.redact_sensitive_text("\n".join(secrets.values()))
    for secret in secrets.values():
        assert secret not in redacted
    assert redacted.count("[REDACTED") >= len(secrets)


@pytest.mark.asyncio
async def test_generated_groq_findings_and_summaries_are_redacted_everywhere(
    caplog: pytest.LogCaptureFixture,
):
    secret = "gsk_" + "M" * 48
    tainted_finding = _finding(
        title=f"credential {secret}",
        suggested_fix=f"GROQ_API_KEY = '{secret}'",
    )
    payload = _perspective_payload(
        f"credential leaked in summary: {secret}",
        95,
        [tainted_finding],
    )
    complete = AsyncMock(return_value=_llm_response(payload))

    with caplog.at_level(logging.DEBUG):
        result = await run_audit(
            diff_text=SAMPLE_DIFF,
            llm_client=_mock_llm_client(complete),
        )

    serialized = json.dumps(
        {
            "perspectives": repr(result.perspectives),
            "findings": [item.to_dict() for item in result.findings],
            "executive_summary": result.executive_summary,
            "remediation": result.remediation_diff,
            "report": result.report_markdown,
        }
    )
    assert secret not in serialized
    assert secret[:12] not in serialized
    assert secret not in caplog.text
    assert all(secret not in item.summary for item in result.perspectives)
    assert all(
        secret not in json.dumps(item.to_dict())
        for item in result.findings
    )


@pytest.mark.asyncio
async def test_ci_log_groq_and_authorization_values_are_redacted_before_clipping():
    groq_secret = "gsk_" + "N" * 48
    authorization_secret = "opaqueAuthorizationCredential123456789"
    logs = (
        ("x" * auditor.MAX_CI_CONTEXT_CHARS)
        + f"\nAuthorization: {authorization_secret}"
        + f"\nGROQ_API_KEY={groq_secret}"
    )
    with patch(
        "app.github_client.fetch_workflow_run_logs",
        new_callable=AsyncMock,
        return_value=logs,
    ):
        context = await auditor.fetch_audit_ci_context(
            owner="o",
            repo="r",
            run_id=123,
            token="read-only-token",
        )

    assert groq_secret not in context
    assert authorization_secret not in context
    assert len(context) <= auditor.MAX_CI_CONTEXT_CHARS


def test_final_report_redacts_groq_and_opaque_authorization_values():
    groq_secret = "gsk_" + "Q" * 48
    authorization_secret = "opaqueAuthorizationCredential987654321"
    report = format_audit_report(
        executive_summary=f"summary {groq_secret}",
        findings=[
            {
                **_finding(
                        title=f"title Authorization: {authorization_secret}",
                    suggested_fix=f"Authorization: {authorization_secret}",
                ),
                "description": f"description {groq_secret}",
                "perspective": "security",
                "informational_only": False,
                "id": "AUD-TEST",
            }
        ],
        confidence=95,
        status="status",
        audit_target="target",
        engine="test-engine",
        remediation_diff=f"Authorization: {authorization_secret}\n+{groq_secret}",
        publish_allowed=True,
    )

    assert groq_secret not in report
    assert authorization_secret not in report
    assert groq_secret[:12] not in report
    assert authorization_secret[:12] not in report


@pytest.mark.asyncio
async def test_redaction_precedes_diff_truncation_boundary():
    secret = "sk-ant-api03-" + "Z" * 80
    split_at = AUDIT_MAX_DIFF_CHARS - 8
    tainted_diff = ("x" * split_at) + secret + ("y" * 100)
    captured: list[str] = []

    async def _complete(*, messages=None, **kwargs):
        captured.extend(message.get("content", "") for message in messages or [])
        return _llm_response(_perspective_payload("clean", 100, []))

    result = await run_audit(
        diff_text=tainted_diff,
        llm_client=_mock_llm_client(AsyncMock(side_effect=_complete)),
    )

    serialized = "\n".join(captured)
    assert secret not in serialized
    assert secret[:8] not in serialized
    assert secret not in result.report_markdown


def test_informational_findings_are_not_publishable_actionable_remediation():
    finding = PerspectiveFinding.model_validate(
        _finding(confidence=40, suggested_fix="password = 'unsafe'")
    )
    perspectives = [
        PerspectiveResult(
            perspective="security",
            summary="uncertain",
            confidence=90,
            findings=[finding],
        )
    ]
    synthesized = synthesize_findings(perspectives, SAMPLE_DIFF, overall_confidence=90)
    assert synthesized[0].informational_only is True
    assert synthesized[0].suggested_fix is None
    report = format_audit_report(
        executive_summary="Uncertain observation.",
        findings=[synthesized[0].to_dict()],
        confidence=90,
        status=build_status_label(0, 0, 90),
        audit_target="PR #1",
        engine="test-engine",
        remediation_diff=auditor.build_remediation_diff(synthesized),
        publish_allowed=True,
    )
    assert "Informational only — no automated remediation" in report
    assert "password =" not in report


def test_publication_policy_fails_closed_for_non_boolean_values():
    report = format_audit_report(
        executive_summary="Untrusted policy value.",
        findings=[],
        confidence=100,
        status=build_status_label(0, 0, 100),
        audit_target="PR #1",
        engine="test-engine",
        remediation_diff="(none)",
        publish_allowed="true",  # type: ignore[arg-type]
    )
    assert "**Publication Policy:** Suppressed" in report
    assert "### ℹ️ Informational Audit Result" in report


# ---------------------------------------------------------------------------
# Phase 5.2 infrastructure/security: delivery fingerprints, dispatch fencing,
# pinned PR endpoints, streaming webhook limit, and the shared Lambda runtime
# resolver. All hermetic — no TEST_DATABASE_URL, no network, no LLM.
# ---------------------------------------------------------------------------


def _code_only(source: str) -> str:
    """Drop whole-line comments so prose about an API does not read as its use."""
    return "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )


def _fingerprint_base() -> dict[str, Any]:
    return {
        "repo_id": "repo-1",
        "audit_type": "pr_audit",
        "repo_full_name": "o/r",
        "ref": "main",
        "pr_number": 42,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "workflow_run_id": None,
        "settings_version": 1,
    }


def test_delivery_fingerprint_is_deterministic_and_field_sensitive():
    from app.services import audit_pipeline as pipeline

    base = _fingerprint_base()
    reference = pipeline.audit_delivery_fingerprint(**base)
    assert len(reference) == 64
    assert pipeline.audit_delivery_fingerprint(**base) == reference

    for field, value in (
        ("repo_id", "repo-2"),
        ("audit_type", "manual_audit"),
        ("repo_full_name", "o/other"),
        ("ref", "haunter/fix"),
        ("pr_number", 43),
        ("base_sha", "c" * 40),
        ("head_sha", "d" * 40),
        ("workflow_run_id", 99),
        ("settings_version", 2),
    ):
        assert pipeline.audit_delivery_fingerprint(**{**base, field: value}) != reference


@pytest.mark.asyncio
async def test_delivery_intake_requires_pinned_endpoints_for_every_pr_audit_type():
    repo_id = "00000000-0000-0000-0000-000000000001"
    for audit_type in sorted(audit_pipeline.AUDIT_TYPES):
        with pytest.raises(ValueError, match="base and head SHAs"):
            await audit_pipeline.claim_audit_delivery(
                db=cast(AsyncSession, MagicMock()),
                repo_id=repo_id,
                audit_type=cast(Any, audit_type),
                repo_full_name="o/r",
                delivery_id="delivery-1",
                pr_number=7,
            )


def test_delivery_intake_rejects_invalid_settings_version():
    with pytest.raises(ValueError, match="settings version"):
        audit_pipeline._validate_settings_version(0)
    with pytest.raises(ValueError, match="settings version"):
        audit_pipeline._validate_settings_version(True)
    with pytest.raises(ValueError, match="settings version"):
        audit_pipeline._validate_settings_version("2")
    assert audit_pipeline._validate_settings_version(7) == 7


@pytest.mark.asyncio
async def test_execution_refuses_to_adopt_current_pr_state_for_any_audit_type():
    from app.subagents.auditor import AuditTargetChangedError

    repo = cast(Repo, SimpleNamespace(id=1, owner="o", name="r"))
    pinned_base = "a" * 40
    pinned_head = "b" * 40
    advanced_head = "c" * 40

    with patch(
        "app.github_client.fetch_pull_request",
        new_callable=AsyncMock,
        return_value={
            "base": {"sha": pinned_base},
            "head": {"sha": advanced_head},
        },
    ) as fetch_pr:
        with pytest.raises(AuditTargetChangedError):
            await audit_pipeline._resolve_audit_target(
                repo=repo,
                pr_number=5,
                base_sha=pinned_base,
                head_sha=pinned_head,
                token="read-only-token",
            )
    fetch_pr.assert_awaited_once()
    assert fetch_pr.await_args is not None
    assert fetch_pr.await_args.kwargs["allow_global_token"] is False

    # An unpinned stored job cannot resolve a PR target at all: it fails rather
    # than reading the PR's current head.
    with pytest.raises(ValueError, match="head SHA is missing"):
        await audit_pipeline._resolve_audit_target(
            repo=repo,
            pr_number=None,
            base_sha=None,
            head_sha=None,
            token="read-only-token",
        )


def test_lambda_runtime_resolver_is_shared_by_workers_and_hosting():
    from app import lambda_runtime

    with patch.dict("os.environ", {}, clear=True):
        with patch("app.config.settings.aws_lambda_function_name", None):
            assert lambda_runtime.resolve_lambda_function_name() is None
            assert lambda_runtime.is_lambda_runtime() is False
        with patch("app.config.settings.aws_lambda_function_name", "   "):
            assert lambda_runtime.resolve_lambda_function_name() is None
            assert lambda_runtime.is_lambda_runtime() is False
        with patch("app.config.settings.aws_lambda_function_name", "haunter-prod"):
            assert lambda_runtime.resolve_lambda_function_name() == "haunter-prod"
            assert lambda_runtime.is_lambda_runtime() is True
            assert lambda_runtime.resolve_lambda_function_name("explicit") == "explicit"
    with patch("app.config.settings.aws_lambda_function_name", None):
        with patch.dict("os.environ", {"AWS_LAMBDA_FUNCTION_NAME": "env-fn"}):
            assert lambda_runtime.resolve_lambda_function_name() == "env-fn"
            assert lambda_runtime.is_lambda_runtime() is True


def test_audit_dispatch_workers_refuse_to_run_inside_lambda():
    audit_pipeline._audit_dispatch_workers.clear()
    with (
        patch.dict("os.environ", {"AWS_LAMBDA_FUNCTION_NAME": "haunter-prod"}),
        patch("app.config.settings.aws_lambda_function_name", "haunter-prod"),
        patch("app.services.audit_pipeline.asyncio.create_task") as create_task,
    ):
        assert audit_pipeline.start_audit_dispatch_workers() == 0
    create_task.assert_not_called()


@pytest.mark.asyncio
async def test_audit_dispatch_workers_start_locally_in_process_mode():
    audit_pipeline._audit_dispatch_workers.clear()
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("app.config.settings.aws_lambda_function_name", None),
    ):
        count = audit_pipeline.start_audit_dispatch_workers()
        assert count == audit_pipeline.AUDIT_DISPATCH_WORKER_COUNT
        try:
            assert len(audit_pipeline._audit_dispatch_workers) == count
        finally:
            await audit_pipeline.stop_audit_dispatch_workers()
    assert audit_pipeline._audit_dispatch_workers == set()


def test_audit_scheduler_never_orphans_an_in_flight_invoke_thread():
    from app.adapters import hosting

    source = _code_only(inspect.getsource(hosting.AWSHostingAdapter.schedule_audit))
    # wait_for() cancels the coroutine but cannot stop the executor thread, which
    # would let a late invoke race the released dispatch lease.
    assert "wait_for" not in source
    assert "run_in_executor" in source


def test_webhook_body_read_stops_at_the_limit_instead_of_buffering():
    from app import webhooks

    class _ChunkedRequest:
        def __init__(self, chunks):
            self._chunks = list(chunks)

        async def stream(self):
            for chunk in self._chunks:
                yield chunk

    limit = webhooks.MAX_PAYLOAD_SIZE_BYTES
    body = asyncio.run(
        webhooks._read_limited_body(
            cast(Any, _ChunkedRequest([b"a" * 1024, b"b" * 1024]))
        )
    )
    assert body == b"a" * 1024 + b"b" * 1024

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            webhooks._read_limited_body(
                cast(
                    Any,
                    _ChunkedRequest(
                        [b"a" * (limit // 2), b"b" * (limit // 2), b"c" * 64]
                    ),
                )
            )
        )
    assert excinfo.value.status_code == 413


def test_webhook_ingress_streams_the_body_rather_than_buffering_it():
    from app import webhooks

    source = _code_only(inspect.getsource(webhooks.github_webhook))
    assert "await request.body()" not in source
    assert "_read_limited_body(request)" in source

# ---------------------------------------------------------------------------
# 9. Output-boundary sanitization of AuditFinding / AuditResult
# ---------------------------------------------------------------------------


def _hostile_finding(secret: str) -> AuditFinding:
    return AuditFinding(
        id="AUD-BOUNDARY1",
        file_path="app/<script>alert(1)</script>.py",
        line_start=12,
        line_end=14,
        perspective="security",
        severity="BLOCKER",
        category=f"<img src=x>{secret[:4]}",
        title=f"<script>alert(2)</script> @victim {secret}",
        description=f"leaked {secret} in the description",
        suggested_fix=f"API_KEY = '{secret}'",
        confidence=99,
        informational_only=False,
    )


def test_audit_finding_and_result_dicts_are_sanitized_at_the_output_boundary():
    secret = "sk-ant-api03-" + "Q" * 80
    finding = _hostile_finding(secret)
    result = AuditResult(
        audit_id="audit-aaaaaaaaaaaa",
        audit_type="pr_audit",
        repo_full_name="o/r",
        target_label=f"<script>alert(3)</script> {secret}",
        engine="test-engine",
        executive_summary=f"<b>summary</b> {secret}",
        findings=[finding],
        confidence=99,
        status="status",
        report_markdown="## report\n- unrendered markdown stays intact: a && b",
        analysis_metadata=f"<i>metadata</i> {secret}",
    )

    payload = json.dumps(result.to_dict())
    finding_payload = json.dumps(finding.to_dict())

    # No live tag survives in either projection.
    assert "<script>" not in payload
    assert "<img" not in payload
    assert "&lt;script&gt;" in finding_payload
    # No credential-like value survives in either projection.
    assert secret not in payload
    assert secret[:24] not in payload
    assert secret[:24] not in finding_payload
    assert "API_KEY = [REDACTED]" in finding_payload
    assert "REDACTED_ANTHROPIC_KEY" in result.to_dict()["executive_summary"]

    # The renderer owns its own Markdown escaping, so a rendered report is only
    # redaction- and bound-checked here: escaping it again would double-encode
    # the entities `format_audit_report` already emitted.
    assert "a && b" in result.to_dict()["report_markdown"]
    # The envelope's own free-text fields are escaped like the finding's.
    assert "&lt;script&gt;" in result.to_dict()["target_label"]
    assert "&lt;b&gt;summary&lt;/b&gt;" in result.to_dict()["executive_summary"]
    assert "&lt;i&gt;metadata&lt;/i&gt;" in result.to_dict()["analysis_metadata"]


def test_output_boundary_rejects_paths_that_are_not_repo_relative():
    hostile = AuditFinding(
        id="AUD-BOUNDARY2",
        file_path="../../etc/passwd",
        line_start=1,
        line_end=1,
        perspective="security",
        severity="NOTE",
        category="Path",
        title="Traversal",
        description="A path that escapes the repository.",
        suggested_fix=None,
        confidence=80,
    )
    assert hostile.to_dict()["file_path"] == "unknown"
    assert hostile.to_dict()["suggested_fix"] is None
    # An absent repository stays absent rather than becoming the renderer's
    # "unknown" sentinel: a caller reading JSON must be able to tell them apart.
    assert AuditResult(
        audit_id="audit-bbbbbbbbbbbb",
        audit_type="pr_audit",
        repo_full_name="",
        target_label="Unified diff",
        engine="test-engine",
        executive_summary="clean",
    ).to_dict()["repo_full_name"] == ""


def test_report_projection_is_unescaped_so_the_renderer_does_not_double_escape():
    finding = _hostile_finding("sk-ant-api03-" + "Q" * 80)
    report = format_audit_report(
        executive_summary="safe",
        findings=[finding.to_report_dict()],
        confidence=99,
        status=build_status_label(1, 0, 99),
        audit_target="PR #1",
        engine="test-engine",
        remediation_diff="(none)",
        publish_allowed=True,
    )
    # Escaped exactly once by the renderer, not twice.
    assert "&amp;lt;" not in report
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in report
    assert finding.to_report_dict()["title"] == finding.title


# ---------------------------------------------------------------------------
# 10. Line-preserving redaction for AST context
# ---------------------------------------------------------------------------


def test_redaction_preserves_line_count_for_line_anchored_snippets():
    pem = (
        'KEY = """-----BEGIN RSA PRIVATE KEY-----\n'
        "AAAABBBBCCCCDDDD\n"
        "EEEEFFFFGGGGHHHH\n"
        "-----END RSA PRIVATE KEY-----\"\"\"\n"
        "trailing = 1\n"
    )
    collapsed = audit_prompts.redact_sensitive_text(pem)
    preserved = audit_prompts.redact_sensitive_text_preserving_lines(pem)

    # The canonical redactor still collapses, which is what prose and prompt
    # budgets want.
    assert "AAAABBBB" not in collapsed
    assert collapsed.count("\n") == 2
    # The line-preserving variant redacts the same span and keeps every line
    # addressable: line 6 is still `trailing = 1`, not line 3.
    assert "AAAABBBB" not in preserved
    assert "EEEEFFFF" not in preserved
    assert preserved.count("\n") == pem.count("\n")
    assert preserved.splitlines()[-1] == "trailing = 1"
    assert preserved.splitlines()[0].count("REDACTED_PRIVATE_KEY") == 1
    # A single-line secret is byte-identical between the two redactors.
    single = "password = 'hunter2-long-enough-value'\n"
    assert (
        audit_prompts.redact_sensitive_text_preserving_lines(single)
        == audit_prompts.redact_sensitive_text(single)
    )


def test_ast_context_is_parsed_from_raw_source_and_redacted_line_preserving():
    """A multiline private key must not move the reported scope boundaries.

    Redacting a rendered snippet with the canonical redactor collapses a
    five-line PEM block to a single `[REDACTED_PRIVATE_KEY]` token, so the
    snippet renders four lines while still being labelled `Lines 82-87`. Every
    later anchor in the same summary shifts with it, and the reader — or the
    model — maps a rendered line back to the wrong source line. The parser must
    see the file as it is, and only the rendered snippet may be redacted, by a
    redactor that gives the newlines back.
    """
    function_line = 82
    function_body = [
        "def sign_payload(payload: bytes) -> str:",
        '    key = """-----BEGIN RSA PRIVATE KEY-----',
        "    AAAABBBBCCCCDDDD",
        "    EEEEFFFFGGGGHHHH",
        '    -----END RSA PRIVATE KEY-----"""',
        "    return hashlib.sha256(payload + key).hexdigest()",
    ]
    source = "\n".join(
        ["import hashlib", ""] + [""] * (function_line - 3) + function_body
    )
    source_lines = source.splitlines()
    assert source_lines[function_line - 1] == function_body[0]
    assert source_lines[function_line + 4] == function_body[-1]

    summary = build_ast_diff_summary(SAMPLE_DIFF, {"backend/app/auth.py": source})

    # Structure came from the raw file, so the anchors address the real lines.
    assert f"### `backend/app/auth.py` (Line {function_line}, in `sign_payload`)" in summary
    assert f"**Enclosing Scope (Lines {function_line}-{function_line + 5}):**" in summary
    # The rendered scope is exactly as wide as the range it is labelled with, and
    # its first and last lines are the real source lines at that range. The four
    # lines the key occupied come back as four empty lines rather than vanishing.
    fenced = summary.split("**Enclosing Scope", 1)[1].split("```python\n", 1)[1]
    scope_lines = fenced.split("```", 1)[0].splitlines()
    assert len(scope_lines) == len(function_body)
    assert scope_lines[0] == source_lines[function_line - 1]
    assert scope_lines[-1] == source_lines[function_line + 4]
    # The whole key span, BEGIN marker included, is replaced by one placeholder
    # and the three newlines it consumed are handed back, so the closing quotes
    # still land on the source line they were on.
    assert scope_lines[1] == '    key = """[REDACTED_PRIVATE_KEY]'
    assert scope_lines[2:5] == ["", "", '"""']
    for leaked in ("AAAABBBB", "CCCCDDDD", "EEEEFFFF", "GGGGHHHH", "BEGIN RSA PRIVATE KEY"):
        assert leaked not in summary


#: A diff whose added lines are a multi-line PEM private key followed by a real
#: defect. The key is four new-side lines wide, so any redactor that collapses it
#: shifts every line the model reads after it.
PEM_SHIFTING_DIFF = (
    "diff --git a/app.py b/app.py\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,0 +1,6 @@\n"
    '+KEY = """-----BEGIN RSA PRIVATE KEY-----\n'
    "+AAAABBBB\n"
    "+CCCCDDDD\n"
    '+-----END RSA PRIVATE KEY-----"""\n'
    "+import time\n"
    "+time.sleep(5)\n"
)
#: The new-file line the blocking call really sits on, in `PEM_SHIFTING_DIFF`.
PEM_DEFECT_LINE = 6


def _fenced_diff_of(user_content: str) -> str:
    """The unified diff exactly as the perspective prompt hands it to the model."""
    assert "```diff\n" in user_content
    return user_content.split("```diff\n", 1)[1].rsplit("\n```", 1)[0]


def _hunk_body_lines(diff_body: str) -> list[str]:
    """The body of the single hunk, in order, the way the model counts it.

    A model resolves a citation by reading the `@@ -a,b +c,d @@` header and
    counting the d lines of body it is shown under it, which is the only reading
    that stays correct when a line is a redaction placeholder rather than a `+`
    line. Returns the body lines only, so a caller can map a body position to a
    new-file line number.
    """
    header = next(
        line for line in diff_body.splitlines() if line.startswith("@@ ")
    )
    declared = int(re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", header).group(2) or 1)
    body: list[str] = []
    for line in diff_body.splitlines()[diff_body.splitlines().index(header) + 1 :]:
        if line.startswith(("diff --git ", "@@ ")):
            if body:
                break
            continue
        if len(body) >= declared:
            break
        body.append(line)
    assert len(body) == declared, f"visible hunk body contradicts its own header: {header}"
    return body


@pytest.mark.asyncio
async def test_the_diff_handed_to_the_model_keeps_raw_line_numbering():
    """A finding cited on a `+` line after a multi-line secret is still grounded.

    Grounding is built from `raw_diff`, so a citation is only trustworthy if the
    model's copy of the diff is numbered the same way. Redacting with the
    canonical redactor collapsed the four-line key to one token: the model then
    read a hunk whose visible body was three lines long under a header that
    declared six, cited new-file line 3, and the finding was grounded — on a line
    inside the key it had never seen, while the blocking call it meant to report
    was left unreviewed. The model-visible diff must keep `raw_diff`'s line
    numbering, which is what the line-preserving redactor is for.
    """
    seen: list[str] = []

    async def _complete(*, messages=None, **kwargs):
        user_content = (messages or [])[-1]["content"]
        body = _fenced_diff_of(user_content)
        seen.append(body)
        cited = _hunk_body_lines(body).index("+time.sleep(5)") + 1
        perspective = next(
            (p for p in ("security", "correctness", "performance", "architecture") if f"## Perspective\n{p}" in user_content),
            str(len(seen)),
        )
        return _llm_response(
            _perspective_payload(
                "blocking call on the event loop",
                95,
                [
                    _finding(
                        file_path="app.py",
                        line_start=cited,
                        line_end=cited,
                        severity="WARNING",
                        confidence=95,
                        title=f"Blocking time.sleep on the event loop ({perspective})",
                    )
                ],
            )
        )

    result = await run_audit(
        diff_text=PEM_SHIFTING_DIFF,
        audit_id="audit-666666666666",
        llm_client=_mock_llm_client(AsyncMock(side_effect=_complete)),
    )

    assert len(seen) == 4
    # Model-visible line N is raw line N: the four header lines, then six new-side
    # lines, with the key's four lines still occupying four of them.
    assert {len(body.splitlines()) for body in seen} == {
        len(PEM_SHIFTING_DIFF.splitlines())
    }
    # The model sees exactly the six new-side lines the hunk header declares, so a
    # body position resolves to a new-file line number instead of a guess.
    for body in seen:
        assert len(_hunk_body_lines(body)) == 6
    # Redaction is unchanged: the key is gone from every prompt.
    for leaked in ("AAAABBBB", "CCCCDDDD", "BEGIN RSA PRIVATE KEY"):
        assert all(leaked not in body for body in seen)

    # The citation the model made from its own copy is the line the blocking call
    # is really on, and it survived grounding rather than being filtered out.
    findings = [f for f in result.findings if f.title.startswith("Blocking time.sleep")]
    assert len(findings) == 4
    assert {f.line_start for f in findings} == {PEM_DEFECT_LINE}
    assert {f.line_end for f in findings} == {PEM_DEFECT_LINE}
    assert PEM_DEFECT_LINE in build_diff_line_index(PEM_SHIFTING_DIFF)["app.py"]
    # The line that number names is the blocking call, not part of the key.
    raw_new_lines = [
        line[1:]
        for line in PEM_SHIFTING_DIFF.splitlines()
        if line.startswith(("+", " ")) and not line.startswith("+++")
    ]
    assert raw_new_lines[PEM_DEFECT_LINE - 1] == "time.sleep(5)"




def test_source_context_is_returned_verbatim_so_the_parser_sees_real_lines():
    """`fetch_audit_source_contexts` must not pre-redact what it fetches."""
    pem_source = (
        'KEY = """-----BEGIN RSA PRIVATE KEY-----\n'
        "AAAABBBB\n"
        '-----END RSA PRIVATE KEY-----"""\n'
    )
    diff = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1,1 +1,2 @@\n"
        " x = 0\n"
        "+x = 1\n"
    )

    async def _fetch(**_kwargs: Any) -> str:
        return pem_source

    async def _run() -> dict[str, str]:
        with patch(
            "app.github_client.fetch_file_content",
            new_callable=AsyncMock,
            side_effect=_fetch,
        ):
            return await auditor.fetch_audit_source_contexts(
                owner="owner",
                repo="repo",
                diff_text=diff,
                sha="a" * 40,
                token="read-only-token",
            )

    contexts = asyncio.run(_run())
    assert contexts["a.py"] == pem_source
    # ...and the summary that reaches the model still has no key material.
    summary = build_ast_diff_summary(diff, contexts)
    assert "AAAABBBB" not in summary


# ---------------------------------------------------------------------------
# 11. Log hygiene: untrusted names, paths and identifiers
# ---------------------------------------------------------------------------


def test_log_hygiene_control_strips_bounds_and_redacts_every_untrusted_value():
    from app.log_hygiene import MAX_LOG_VALUE_CHARS, sanitize_log_value

    # A newline in an attacker-chosen name would otherwise forge a second log
    # record; zero-width and bidi characters would hide text from a reviewer.
    forged = "build\nERROR forged_level=warning\u200b\u202e secret=ghp_" + "D" * 36
    cleaned = sanitize_log_value(forged)
    assert "\n" not in cleaned
    assert "\u200b" not in cleaned
    assert "\u202e" not in cleaned
    assert "ghp_" + "D" * 36 not in cleaned
    assert "[REDACTED" in cleaned
    assert "ERROR forged_level=warning" in cleaned

    # Length is bounded, and the bound is the caller's to choose.
    assert len(sanitize_log_value("a" * 5_000)) == MAX_LOG_VALUE_CHARS
    assert len(sanitize_log_value("a" * 5_000, 32)) == 32
    assert sanitize_log_value("a" * 5_000, 32).endswith("...")
    # A non-string and an absent value are handled, and absence stays absence.
    assert sanitize_log_value(None) == ""
    assert sanitize_log_value(1234) == "1234"
    assert sanitize_log_value("a\tb\r\nc") == "a b c"
    # A private key spanning many lines collapses and is still recognised.
    assert "BEGIN RSA PRIVATE KEY" not in sanitize_log_value(
        "-----BEGIN RSA PRIVATE KEY-----\nAAAA\n-----END RSA PRIVATE KEY-----"
    )


def test_archive_entry_names_are_sanitized_before_they_reach_a_log_line(
    caplog: pytest.LogCaptureFixture,
):
    import io
    import zipfile

    from app import github_client

    hostile_name = "0_x.txt\nWARN github forged=1\u200b\u202e" + "A" * 4_000
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(hostile_name, "a" * 4_096)
    payload = buffer.getvalue()

    with caplog.at_level(logging.DEBUG), patch.object(
        github_client, "MAX_ZIP_ENTRY_BYTES", 1
    ):
        with pytest.raises(github_client.GitHubResponseLimitError):
            github_client._extract_log_archive(payload)

    assert hostile_name not in caplog.text
    assert "\nWARN github forged=1" not in caplog.text
    assert "\u200b" not in caplog.text
    assert "\u202e" not in caplog.text
    # The bounded, control-stripped, redacted form of the name is what got logged:
    # the newline is collapsed and the 4000-character run is cut to the bound.
    assert "0_x.txt WARN github forged=1" in caplog.text
    assert re.search(r"A{20,}", caplog.text) is not None
    assert re.search(r"A{201,}", caplog.text) is None



def test_archive_entry_names_in_extracted_log_text_are_sanitized():
    import io
    import zipfile

    from app import github_client

    hostile_name = "2_run.txt\n=== injected ===.txt"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(hostile_name, "boom\n")
    extracted = github_client._extract_log_archive(buffer.getvalue())

    assert "=== File: 2_run.txt === injected ===.txt ===\nboom" in extracted
    assert f"=== File: {hostile_name} ===" not in extracted



def test_webhook_repo_names_reach_logs_only_through_the_sanitizer():
    from app import webhooks

    hostile = "acme\nINFO forged=yes\u200b"
    logged = webhooks._log_repo(hostile, hostile)
    assert logged == "acme INFO forged=yes/acme INFO forged=yes"
    assert "\n" not in logged
    assert "\u200b" not in logged
    # The helper is what every repo-name log interpolation uses.
    assert webhooks._log_repo("owner", "repo") == "owner/repo"
    source = _code_only(inspect.getsource(webhooks.github_webhook))
    assert "repository %s/%s" not in source
    assert "repo=%s/%s" not in source


def test_ci_job_and_step_names_are_sanitized_before_reaching_failure_reason():
    from app.sandbox import github_actions_runner

    hostile = "build\nERROR forged=1\u200b"

    class _Response:
        status_code = 404

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "jobs": [
                    {
                        "id": 1,
                        "name": hostile,
                        "conclusion": "failure",
                        "steps": [{"name": hostile, "conclusion": "failure"}],
                    }
                ]
            }

    class _Client:
        async def get(self, *_args: Any, **_kwargs: Any) -> _Response:
            return _Response()

    async def _run() -> str:
        return await github_actions_runner._get_workflow_run_log_tail(
            cast(Any, _Client()),
            "o/r",
            99,
            token="read-only-token",
        )

    summary = asyncio.run(_run())
    assert "build ERROR forged=1" in summary
    assert hostile not in summary
    assert "ERROR forged=1:" not in summary
    assert "\u200b" not in summary



def test_auditor_log_helper_is_the_single_canonical_log_sanitizer():
    from app.log_hygiene import MAX_LOG_VALUE_CHARS, sanitize_log_value

    # `_safe_metadata` is the only path the auditor module has for an untrusted
    # path, owner or repository name, so it must be the shared contract rather
    # than a second copy of it.
    assert auditor._safe_metadata("a\nb", 100) == sanitize_log_value("a\nb", 100)
    assert auditor._safe_metadata("x" * 4_000, 64) == "x" * 61 + "..."
    assert len(auditor._safe_metadata("x" * 4_000, MAX_LOG_VALUE_CHARS)) == MAX_LOG_VALUE_CHARS
    assert auditor._safe_metadata("ghp_" + "D" * 36, 200).startswith("[REDACTED_GITHUB_TOKEN]")



# ---------------------------------------------------------------------------
# 12. Migration safety for the pinned-endpoint / fenced-dispatch migrations
# ---------------------------------------------------------------------------


def _audit_migration(revision: str) -> Any:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    return script.get_revision(revision).module


class _OpRecorder:
    """Records the schema operations a migration performs, in order.

    Constraint names and column names are passed positionally by the Alembic
    operation proxy, so lookups match on either the positional or the keyword
    form rather than assuming one of them.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        def _record(*args: Any, **kwargs: Any) -> None:
            self.calls.append((name, args, kwargs))

        return _record

    def executed(self) -> list[str]:
        return [str(args[0]) for name, args, _ in self.calls if name == "execute"]

    def index_of(self, name: str, needle: str) -> int:
        for position, (call, args, kwargs) in enumerate(self.calls):
            if call != name:
                continue
            rendered = " ".join(str(arg) for arg in args) + " " + repr(kwargs)
            if needle in rendered:
                return position
        raise AssertionError(f"no recorded {name} mentioning {needle!r}")


def test_audit_migrations_resolve_legacy_rows_before_the_constraints_apply(
    monkeypatch: pytest.MonkeyPatch,
):
    """The pin and fence constraints must land on already-resolved rows.

    `ck_audit_jobs_pr_endpoints` forbids a queued/dispatching/running PR audit
    without both comparison endpoints, and `ck_audit_jobs_dispatch_fence`
    forbids a dispatching job with no fence. Adding either constraint before the
    legacy rows have been resolved fails the whole migration, and adding it
    after but with the resolution statements missing would leave pre-existing
    jobs in a state the new code refuses to execute.
    """
    module = _audit_migration("c7a1b2c3d4e5")
    recorder = _OpRecorder()
    monkeypatch.setattr(module, "op", recorder)

    module.upgrade()

    statements = recorder.executed()
    assert len(statements) == 4

    fingerprint_backfill = next(
        index
        for index, statement in enumerate(statements)
        if "delivery_fingerprint" in statement
    )
    dispatching_requeue = next(
        index
        for index, statement in enumerate(statements)
        if "WHERE status = 'dispatching'" in statement
    )
    pinned_requeue = next(
        index
        for index, statement in enumerate(statements)
        if "base_sha IS NOT NULL AND head_sha IS NOT NULL" in statement
    )
    unpinned_failure = next(
        index
        for index, statement in enumerate(statements)
        if "LegacyAuditEndpointsUnpinned" in statement
    )
    not_null_tighten = recorder.index_of("alter_column", "delivery_fingerprint")
    endpoint_constraint = recorder.index_of(
        "create_check_constraint", "ck_audit_jobs_pr_endpoints"
    )
    fence_constraint = recorder.index_of(
        "create_check_constraint", "ck_audit_jobs_dispatch_fence"
    )

    # The fingerprint digest exists for every row before NOT NULL is enforced.
    assert fingerprint_backfill < not_null_tighten
    # Legacy PR jobs are resolved before the endpoint constraint applies: the
    # ones that still carry both endpoints go back on the queue, and the ones
    # that cannot recover their endpoints fail terminally and explicitly.
    assert max(pinned_requeue, unpinned_failure) < endpoint_constraint
    assert "status = 'failed'" in statements[unpinned_failure]
    assert "base_sha IS NULL OR head_sha IS NULL" in statements[unpinned_failure]
    assert "status = 'queued'" in statements[pinned_requeue]
    # A job that was mid-dispatch holds no fence and can never be authenticated,
    # so it is returned to the queue before the fence constraint applies.
    assert dispatching_requeue < fence_constraint
    assert "DispatchFenceBackfill" in statements[dispatching_requeue]
    assert "dispatch_fence_token = NULL" in statements[dispatching_requeue]
    # Every statement resolves legacy state on `audit_jobs`; none of them can
    # run against a table that has not been migrated.
    assert all("audit_jobs" in statement for statement in statements)



def test_destructive_audit_migration_downgrades_are_refused_and_valid_one_is_a_noop(
    monkeypatch: pytest.MonkeyPatch,
):
    """A downgrade that would destroy data is refused, not silently executed."""
    for revision, expected in (
        ("a4b7c9d2e6f1", "repo_settings"),
        ("b6d8f0a2c4e6", "base_sha"),
    ):
        module = _audit_migration(revision)
        recorder = _OpRecorder()
        monkeypatch.setattr(module, "op", recorder)
        with pytest.raises(RuntimeError) as excinfo:
            module.downgrade()
        message = str(excinfo.value)
        assert "refusing to downgrade" in message
        assert revision in message
        # The error names what would be destroyed, so the operator can act.
        assert expected in message
        # Nothing was dropped on the way to the error.
        assert recorder.calls == []

    # The newest revision's downgrade is the safe direction: it keeps the added
    # columns and constraints, so rolling back to the previous code revision
    # loses nothing. The older revision ignores the extra columns.
    head = _audit_migration("c7a1b2c3d4e5")
    recorder = _OpRecorder()
    monkeypatch.setattr(head, "op", recorder)
    assert head.downgrade() is None
    assert recorder.calls == []
    assert head.downgrade.__doc__ is not None


# ---------------------------------------------------------------------------
# 13. Exact diff grounding: declared hunk counts enforced, statistics counted
#     over accepted hunks only, and every non-actionable or unplaceable file
#     shape rejected instead of half-parsed.
# ---------------------------------------------------------------------------


MALFORMED_HUNK_HEADERS = (
    # An old-side line number past the auditable maximum.
    "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
    "@@ -99999999,1 +1,1 @@\n-old\n+new\n",
    # A new-side line number past the auditable maximum.
    "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
    "@@ -1,1 +99999999,1 @@\n-old\n+new\n",
    # A declared old count past the per-hunk line cap.
    "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
    "@@ -1,99999999 +1,1 @@\n-old\n+new\n",
    # A declared new count past the per-hunk line cap.
    "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
    "@@ -1,1 +1,99999999 @@\n-old\n+new\n",
    # A header that is not a hunk header at all: nothing may be indexed from it.
    "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
    "@@ -x,1 +1,1 @@\n-old\n+new\n",
    # A hunk header with no file header to attach it to.
    "@@ -1,1 +1,1 @@\n-old\n+new\n",
)


@pytest.mark.parametrize("diff", MALFORMED_HUNK_HEADERS)
def test_malformed_and_out_of_range_hunk_headers_are_rejected_and_uncounted(diff: str):
    grounding = build_diff_grounding(diff)

    assert grounding.line_index == {}
    assert grounding.hunk_count == 0
    assert (grounding.added_lines, grounding.removed_lines) == (0, 0)
    assert "app.py" not in grounding.valid_paths
    summary = build_ast_diff_summary(diff)
    assert "accepted hunks: 0" in summary
    assert "+0 added / -0 removed lines" in summary


def test_overlong_hunk_body_invalidates_the_whole_hunk_and_counts_nothing():
    diff = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -10,2 +20,2 @@\n"
        "-old\n"
        "+new\n"
        "+extra\n"
        "+third\n"
    )
    grounding = build_diff_grounding(diff)

    assert grounding.line_index == {}
    assert grounding.hunk_count == 0
    assert (grounding.added_lines, grounding.removed_lines) == (0, 0)
    assert "app.py" in grounding.rejected_paths
    # One accepted hunk in the same diff is what proves the counter counts
    # accepted hunks rather than hunks-plus-rejections.
    accepted = build_diff_grounding(diff.replace("+extra\n+third\n", " context\n"))
    assert accepted.hunk_count == 1
    assert (accepted.added_lines, accepted.removed_lines) == (1, 1)
    assert accepted.line_index == {"app.py": {20, 21}}


def test_incomplete_hunk_is_never_counted_or_grounded():
    incomplete = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-a\n"
        "+b\n"
    )
    grounding = build_diff_grounding(incomplete)

    assert grounding.line_index == {}
    assert grounding.hunk_count == 0
    assert (grounding.added_lines, grounding.removed_lines) == (0, 0)
    assert "app.py" in grounding.rejected_paths
    assert grounding.truncated_ranges["app.py"] == {(2, 2)}


def test_clipped_statistics_are_labelled_as_prefix_statistics():
    # One file, one hunk whose body is far past the auditor's character bound, so
    # the character cut lands inside the new-side block: the partial hunk stays
    # usable, and the counts describe the inspected prefix only.
    body = "".join(f"-old{index}\n" for index in range(5_000)) + "".join(
        f"+new{index}\n" for index in range(5_000)
    )
    diff = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,5000 +1,5000 @@\n"
        f"{body}"
    )
    assert len(diff) > AUDIT_MAX_DIFF_CHARS

    grounding = build_diff_grounding(diff)
    summary = build_ast_diff_summary(diff)

    assert grounding.clipped is True
    assert grounding.hunk_count == 1
    assert 0 < grounding.added_lines < 5_000
    assert grounding.removed_lines == 5_000
    # The part of the new side the clip never reached is untrusted.
    assert list(grounding.truncated_ranges["app.py"]) == [
        (1 + grounding.added_lines, 5_000)
    ]
    assert "Prefix statistics only" in summary
    assert "not the commit" in summary
    # The label replaces the unqualified claim rather than sitting beside it.
    assert not summary.startswith("Files changed:")
    assert "Diff or hunk truncation was detected" in summary
    # An unclipped diff makes no such claim.
    assert "Prefix statistics" not in build_ast_diff_summary(SAMPLE_DIFF)
    assert build_ast_diff_summary(SAMPLE_DIFF).startswith("Files changed:")


def test_deletion_only_hunk_is_excluded_from_ast_contexts_instead_of_raising():
    diff = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,1 +1,0 @@\n"
        "-old\n"
    )
    source = "def handler():\n    return 1\n"

    grounding = build_diff_grounding(diff)
    # The file is genuinely touched, but it has no new-side line to anchor on.
    assert grounding.valid_paths == frozenset({"app.py"})
    assert grounding.line_index == {}
    assert grounding.hunk_count == 1
    assert (grounding.added_lines, grounding.removed_lines) == (0, 1)
    # The whole point: no new-side lines means no AST context, not a KeyError.
    summary = build_ast_diff_summary(diff, {"app.py": source})
    assert "app.py" in summary
    assert "Parser-backed Python AST context" not in summary
    # And no finding can be grounded on it.
    perspectives = [
        PerspectiveResult(
            perspective="security",
            summary="deletion only",
            confidence=95,
            findings=[
                PerspectiveFinding.model_validate(
                    _finding(file_path="app.py", line_start=1, line_end=1)
                )
            ],
        )
    ]
    assert synthesize_findings(perspectives, diff, overall_confidence=95) == []


@pytest.mark.parametrize(
    ("name", "diff"),
    [
        (
            "binary",
            "diff --git a/app.bin b/app.bin\nindex 111..222 100644\n"
            "Binary files a/app.bin and b/app.bin differ\n",
        ),
        (
            "git_binary_patch",
            "diff --git a/app.bin b/app.bin\nindex 111..222 100644\n"
            "GIT binary patch\nliteral 8\n",
        ),
        (
            "mode_only",
            "diff --git a/app.py b/app.py\nold mode 100644\nnew mode 100755\n",
        ),
        ("header_only", "--- a/app.py\n+++ b/app.py\n"),
        (
            "deleted_file",
            "diff --git a/app.py b/app.py\n--- a/app.py\n+++ /dev/null\n"
            "@@ -1,1 +0,0 @@\n-old\n",
        ),
        (
            "new_header_escapes_the_repo",
            "diff --git a/app.py b/app.py\n--- a/app.py\n"
            "+++ b/../../etc/passwd\n@@ -1,1 +1,1 @@\n-a\n+b\n",
        ),
        (
            "hunk_without_a_new_side_header",
            "diff --git a/app.py b/app.py\n--- a/app.py\n@@ -1,1 +1,1 @@\n-a\n+b\n",
        ),
    ],
)
def test_non_actionable_file_shapes_are_rejected_and_count_nothing(name: str, diff: str):
    grounding = build_diff_grounding(diff)

    assert grounding.line_index == {}, name
    assert grounding.hunk_count == 0, name
    assert (grounding.added_lines, grounding.removed_lines) == (0, 0), name
    assert grounding.valid_paths == frozenset(), name
    assert "accepted hunks: 0" in build_ast_diff_summary(diff), name


def test_header_validation_indexes_a_rename_under_its_new_path():
    diff = (
        "diff --git a/old_name.py b/new_name.py\n"
        "similarity index 92%\n"
        "rename from old_name.py\n"
        "rename to new_name.py\n"
        "--- a/old_name.py\n"
        "+++ b/new_name.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-before\n"
        "+after\n"
    )
    grounding = build_diff_grounding(diff)

    assert grounding.line_index == {"new_name.py": {1}}
    assert grounding.valid_paths == frozenset({"new_name.py"})
    assert grounding.rejected_paths == frozenset()
    assert grounding.hunk_count == 1
    # A rename with no content change declares no hunk, so it grounds nothing:
    # it is not offered as an analyzable file and is not counted.
    pure_rename = diff[: diff.index("@@ -1,1 +1,1 @@")]
    pure = build_diff_grounding(pure_rename)
    assert pure.line_index == {}
    assert pure.hunk_count == 0
    assert pure.valid_paths == frozenset()
    assert "No complete valid text hunks" in build_ast_diff_summary(
        pure_rename, {"new_name.py": "x = 1\n"}
    )


def test_new_file_with_a_zero_old_count_is_grounded_on_its_new_lines():
    grounding = build_diff_grounding(
        "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,2 @@\n+one\n+two\n"
    )

    assert grounding.line_index == {"new.py": {1, 2}}
    assert grounding.hunk_count == 1
    assert (grounding.added_lines, grounding.removed_lines) == (2, 0)


def test_zero_length_hunk_header_does_not_abort_the_rest_of_the_diff():
    # A header that declares no lines is satisfied on sight. Left open, the next
    # file header would be read as a forged hunk body and the walk would stop.
    diff = (
        "diff --git a/first.py b/first.py\n"
        "--- a/first.py\n"
        "+++ b/first.py\n"
        "@@ -1,0 +1,0 @@\n"
        "diff --git a/second.py b/second.py\n"
        "--- a/second.py\n"
        "+++ b/second.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-a\n"
        "+b\n"
    )
    grounding = build_diff_grounding(diff)

    assert grounding.line_index == {"second.py": {1}}
    assert grounding.rejected_paths == frozenset()


# ---------------------------------------------------------------------------
# 14. Typed fetch failures: a body that was promised as an archive is never
#     re-read as text, a failed CI-log fetch never becomes an empty context, and
#     only a verified empty diff reaches `skipped_no_diff`.
# ---------------------------------------------------------------------------


def _log_zip_bytes(*names_and_bodies: tuple[str, str]) -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in names_and_bodies:
            archive.writestr(name, body)
    return buffer.getvalue()


MALFORMED_ARCHIVE_BODIES = (
    # A truncated zip: the central directory is gone.
    _log_zip_bytes(("0_test.txt", "FAILED test_foo\n"))[:-8],
    # An HTML error page served under a zip content type.
    b"<!DOCTYPE html><html><body>Sign in to continue</body></html>",
    # Raw bytes with no container at all.
    b"\x00\x01\x02\xff\xfe" * 64,
)


@pytest.mark.parametrize("body", MALFORMED_ARCHIVE_BODIES)
@pytest.mark.parametrize(
    "content_type",
    [
        "application/zip",
        "application/zip; charset=binary",
        "APPLICATION/X-ZIP-COMPRESSED",
    ],
)
def test_a_malformed_body_served_as_an_archive_raises_a_typed_archive_error(
    body: bytes, content_type: str
):
    from app import github_client

    with pytest.raises(github_client.GitHubArchiveError):
        github_client._extract_log_archive(body, content_type)


def test_text_fallback_is_permitted_only_for_verified_text():
    from app import github_client

    plain = b"pytest tests/ -v\nFAILED test_foo\n"
    # Verified text, declared as text, and declared as nothing at all.
    assert (
        github_client._extract_log_archive(plain, "text/plain; charset=utf-8")
        == plain.decode()
    )
    assert github_client._extract_log_archive(plain, None) == plain.decode()
    # Undeclared, but unambiguously text: still allowed.
    assert github_client._extract_log_archive(plain, "application/octet-stream") == (
        plain.decode()
    )
    # Not text. A NUL byte is the cheapest binary signal, and a body that will
    # not decode as UTF-8 is not a log stream.
    for body in (b"\x00\x01\x02", b"\xff\xfe\xfd\xfc", b"ok\x00then-nul"):
        with pytest.raises(github_client.GitHubArchiveError):
            github_client._extract_log_archive(body, "text/plain; charset=utf-8")
    # A valid archive is extracted whatever it is declared as.
    archive = _log_zip_bytes(("1_test.txt", "FAILED test_bar\n"))
    assert "FAILED test_bar" in github_client._extract_log_archive(
        archive, "application/octet-stream"
    )


#: Factories, not instances: the same exception object raised by several tests
#: accumulates every earlier traceback and makes a failure unreadable.
CI_FETCH_FAILURES = (
    (lambda: GitHubAuthError("Bad credentials"), auditor.AuditRemoteFailureReason.AUTH),
    (
        lambda: GitHubRateLimitError("rate limit exceeded"),
        auditor.AuditRemoteFailureReason.RATE_LIMIT,
    ),
    (
        lambda: GitHubResourceNotFoundError("logs not found"),
        auditor.AuditRemoteFailureReason.PRIVATE_OR_MISSING,
    ),
    (lambda: GitHubNetworkError("connection reset"), auditor.AuditRemoteFailureReason.NETWORK),
    (lambda: httpx.ConnectError("no route"), auditor.AuditRemoteFailureReason.NETWORK),
    (lambda: httpx.ReadTimeout("too slow"), auditor.AuditRemoteFailureReason.TIMEOUT),
)


@pytest.mark.parametrize(("failure", "reason"), CI_FETCH_FAILURES)
@pytest.mark.asyncio
async def test_a_failed_ci_log_fetch_propagates_a_typed_reason(
    failure: Any, reason: auditor.AuditRemoteFailureReason
):
    with patch(
        "app.github_client.fetch_workflow_run_logs",
        new_callable=AsyncMock,
        side_effect=failure(),
    ):
        with pytest.raises(auditor.AuditCIContextFetchError) as excinfo:
            await auditor.fetch_audit_ci_context(
                owner="o", repo="r", run_id=123, token="read-only-token"
            )
    assert excinfo.value.reason is reason
    assert str(excinfo.value)


@pytest.mark.asyncio
async def test_a_non_text_ci_log_response_is_also_a_typed_failure():
    with patch(
        "app.github_client.fetch_workflow_run_logs",
        new_callable=AsyncMock,
        return_value=None,
    ):
        with pytest.raises(auditor.AuditCIContextFetchError):
            await auditor.fetch_audit_ci_context(
                owner="o", repo="r", run_id=123, token="read-only-token"
            )


@pytest.mark.asyncio
async def test_a_successful_but_empty_ci_log_fetch_is_not_a_failure():
    # A run that genuinely has no logs is an answer, not an error, and must not
    # be confused with a run we could not read.
    with patch(
        "app.github_client.fetch_workflow_run_logs",
        new_callable=AsyncMock,
        return_value="",
    ):
        assert (
            await auditor.fetch_audit_ci_context(
                owner="o", repo="r", run_id=123, token="read-only-token"
            )
            == ""
        )


def _audit_job_repo() -> Repo:
    return cast(Repo, SimpleNamespace(id=101, owner="o", name="r"))


@pytest.mark.parametrize(("failure", "reason"), CI_FETCH_FAILURES)
@pytest.mark.asyncio
async def test_a_failed_ci_log_fetch_never_completes_or_skips_the_job(
    failure: Any, reason: auditor.AuditRemoteFailureReason
):
    with (
        patch(
            "app.services.audit_pipeline._resolve_audit_target",
            new_callable=AsyncMock,
            return_value=audit_pipeline.AuditTarget(base_sha=None, head_sha="a" * 40),
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
            "app.github_client.fetch_workflow_run_logs",
            new_callable=AsyncMock,
            side_effect=failure(),
        ),
        patch(
            "app.subagents.auditor.run_audit",
            new_callable=AsyncMock,
            side_effect=AssertionError("a job with no CI evidence must not audit"),
        ) as mock_run,
    ):
        with pytest.raises(auditor.AuditCIContextFetchError) as excinfo:
            await audit_pipeline.execute_audit_job(
                audit_id="audit-888888888888",
                audit_type="ci_failure_audit",
                repo=_audit_job_repo(),
                ref="main",
                pr_number=None,
                base_sha=None,
                head_sha=None,
                workflow_run_id=123,
                token="installation-token",
            )
    assert excinfo.value.reason is reason
    mock_run.assert_not_called()


@pytest.mark.parametrize(("failure", "reason"), CI_FETCH_FAILURES)
@pytest.mark.asyncio
async def test_a_failed_diff_fetch_never_reaches_a_terminal_state(
    failure: Any, reason: auditor.AuditRemoteFailureReason
):
    # The GitHub client is patched, not the auditor wrapper, so the wrapper's own
    # classification is what is under test.
    with (
        patch(
            "app.services.audit_pipeline._resolve_audit_target",
            new_callable=AsyncMock,
            return_value=audit_pipeline.AuditTarget(base_sha=None, head_sha="a" * 40),
        ),
        patch(
            "app.github_client.fetch_diff",
            new_callable=AsyncMock,
            side_effect=failure(),
        ),
        patch(
            "app.subagents.auditor.run_audit",
            new_callable=AsyncMock,
            side_effect=AssertionError("no audit without a diff"),
        ) as mock_run,
    ):
        with pytest.raises(auditor.AuditDiffFetchError) as excinfo:
            await audit_pipeline.execute_audit_job(
                audit_id="audit-888888888888",
                audit_type="ci_failure_audit",
                repo=_audit_job_repo(),
                ref="main",
                pr_number=None,
                base_sha=None,
                head_sha=None,
                workflow_run_id=None,
                token="installation-token",
            )
    assert excinfo.value.reason is reason
    mock_run.assert_not_called()


@pytest.mark.asyncio
async def test_only_a_verified_empty_diff_reaches_skipped_no_diff():
    base_patches = (
        patch(
            "app.services.audit_pipeline._resolve_audit_target",
            new_callable=AsyncMock,
            return_value=audit_pipeline.AuditTarget(base_sha=None, head_sha="a" * 40),
        ),
        patch(
            "app.subagents.auditor.run_audit",
            new_callable=AsyncMock,
            side_effect=AssertionError("no audit without a diff"),
        ),
    )

    # Verified: GitHub answered 2xx with an empty body.
    with base_patches[0], base_patches[1], patch(
        "app.subagents.auditor.fetch_audit_diff",
        new_callable=AsyncMock,
        return_value="  \n\t\n",
    ):
        outcome = await audit_pipeline.execute_audit_job(
            audit_id="audit-999999999999",
            audit_type="pr_audit",
            repo=_audit_job_repo(),
            ref="main",
            pr_number=42,
            base_sha="b" * 40,
            head_sha=None,
            workflow_run_id=None,
            token="installation-token",
        )
    assert outcome.status == "skipped_no_diff"

    # Unverified: a client that answered a diff request with a non-text body has
    # not reported that the commit has no changes.
    with base_patches[0], base_patches[1], patch(
        "app.subagents.auditor.fetch_audit_diff",
        new_callable=AsyncMock,
        return_value=None,
    ):
        with pytest.raises(auditor.AuditDiffFetchError) as excinfo:
            await audit_pipeline.execute_audit_job(
                audit_id="audit-999999999999",
                audit_type="pr_audit",
                repo=_audit_job_repo(),
                ref="main",
                pr_number=42,
                base_sha="b" * 40,
                head_sha=None,
                workflow_run_id=None,
                token="installation-token",
            )
    assert excinfo.value.reason is auditor.AuditRemoteFailureReason.UNKNOWN


@pytest.mark.asyncio
async def test_a_failed_ci_log_fetch_is_recorded_as_a_failure_and_not_completed():
    job = SimpleNamespace(
        audit_id="audit-aaaaaaaaaaaa",
        audit_type="ci_failure_audit",
        ref="main",
        pr_number=None,
        base_sha=None,
        head_sha="a" * 40,
        workflow_run_id=123,
    )
    with (
        patch(
            "app.services.audit_pipeline._claim_processing_job",
            new_callable=AsyncMock,
            return_value=(job, _audit_job_repo(), 1),
        ),
        patch(
            "app.services.audit_pipeline.get_auditor_installation_token",
            new_callable=AsyncMock,
            return_value="read-only-installation-token",
        ),
        patch(
            "app.services.audit_pipeline.execute_audit_job",
            new_callable=AsyncMock,
            side_effect=auditor.AuditCIContextFetchError(
                auditor.AuditRemoteFailureReason.AUTH, "ci logs unavailable"
            ),
        ),
        patch(
            "app.services.audit_pipeline._complete_audit_job",
            new_callable=AsyncMock,
        ) as complete,
        patch(
            "app.services.audit_pipeline._skip_audit_job",
            new_callable=AsyncMock,
        ) as skip,
        patch(
            "app.services.audit_pipeline._retry_or_fail_audit_job",
            new_callable=AsyncMock,
            return_value=False,
        ) as retry,
    ):
        assert await audit_pipeline.process_audit_job(
            "audit-aaaaaaaaaaaa", "0" * 64
        ) is False
    complete.assert_not_called()
    skip.assert_not_called()
    retry.assert_awaited_once()
    assert retry.await_args is not None
    assert isinstance(retry.await_args.args[2], auditor.AuditCIContextFetchError)


# ---------------------------------------------------------------------------
# 15. Publication policy: the confidence score and the caller's flag are combined
#     in one place, and a finding's informational status is derived rather than
#     read off a caller-supplied flag.
# ---------------------------------------------------------------------------


ACTIONABLE_REMEDIATION = (
    "--- a/backend/app/auth.py\n"
    "+++ b/backend/app/auth.py\n"
    "@@ -1,1 +1,1 @@\n"
    "-if token == stored:\n"
    "+if hmac.compare_digest(token, stored):\n"
)


@pytest.mark.parametrize("confidence", [0, 40, 50, 74])
def test_a_report_below_the_threshold_never_renders_an_actionable_remediation_diff(
    confidence: int,
):
    report = format_audit_report(
        executive_summary="The evidence does not support a verdict.",
        findings=[],
        confidence=confidence,
        status=build_status_label(0, 0, confidence),
        audit_target="PR #1",
        engine="test-engine",
        remediation_diff=ACTIONABLE_REMEDIATION,
        # The caller claims the report is publishable. The confidence score is
        # the evidence, and a claim does not outweigh it.
        publish_allowed=True,
    )
    assert "**Publication Policy:** Suppressed" in report
    assert "### 🛠️ Remediation Unified Diff" not in report
    assert "### ℹ️ Informational Audit Result" in report
    assert "hmac.compare_digest" not in report
    assert "if token == stored" not in report
    # The header and the body can no longer disagree with each other: exactly
    # one terminal section, and it is the informational one.
    terminal = [
        line
        for line in report.splitlines()
        if line.startswith("### ") and "Executive Summary" not in line
        and "Findings & Recommendations" not in line
    ]
    assert terminal == ["### ℹ️ Informational Audit Result"]


@pytest.mark.parametrize("confidence", [75, 76, 90, 100])
def test_a_report_at_or_above_the_threshold_still_renders_its_remediation(
    confidence: int,
):
    report = format_audit_report(
        executive_summary="A concrete defect is grounded in the diff.",
        findings=[],
        confidence=confidence,
        status=build_status_label(0, 1, confidence),
        audit_target="PR #1",
        engine="test-engine",
        remediation_diff=ACTIONABLE_REMEDIATION,
        publish_allowed=True,
    )
    assert "**Publication Policy:** Allowed" in report
    assert "### 🛠️ Remediation Unified Diff" in report
    assert "### ℹ️ Informational Audit Result" not in report
    assert "hmac.compare_digest" in report


def test_publish_allowed_false_suppresses_a_high_confidence_report():
    report = format_audit_report(
        executive_summary="Refused by policy.",
        findings=[],
        confidence=100,
        status=build_status_label(0, 0, 100),
        audit_target="PR #1",
        engine="test-engine",
        remediation_diff=ACTIONABLE_REMEDIATION,
        publish_allowed=False,
    )
    assert "**Publication Policy:** Suppressed" in report
    assert "### 🛠️ Remediation Unified Diff" not in report


@pytest.mark.parametrize(
    ("confidence", "informational_only", "expects_suggested_fix"),
    [
        # A low-confidence finding claiming to be actionable.
        (40, False, False),
        # A high-confidence finding claiming to be informational. The flag is
        # advisory only, so the finding is still shown with its fix.
        (90, True, True),
        # The threshold itself, both ways round.
        (75, False, True),
        (74, True, False),
    ],
)
def test_a_finding_informational_status_is_derived_from_its_own_confidence(
    confidence: int, informational_only: bool, expects_suggested_fix: bool
):
    report = format_audit_report(
        executive_summary="A report whose own confidence is publishable.",
        findings=[
            {
                **_finding(confidence=confidence, suggested_fix="hmac.compare_digest(a, b)"),
                "id": "AUD-1",
                "perspective": "security",
                "informational_only": informational_only,
            }
        ],
        confidence=90,
        status=build_status_label(1, 0, 90),
        audit_target="PR #1",
        engine="test-engine",
        remediation_diff="(none)",
        publish_allowed=True,
    )
    assert ("hmac.compare_digest" in report) is expects_suggested_fix
    assert ("informational (low confidence" in report) is not expects_suggested_fix
    assert (
        "Informational only — no automated remediation." in report
    ) is not expects_suggested_fix


def test_a_low_confidence_report_suppresses_per_finding_fixes_too():
    report = format_audit_report(
        executive_summary="A report whose own confidence is not publishable.",
        findings=[
            {
                **_finding(confidence=95, suggested_fix="hmac.compare_digest(a, b)"),
                "id": "AUD-1",
                "perspective": "security",
                "informational_only": False,
            }
        ],
        confidence=50,
        status=build_status_label(1, 0, 50),
        audit_target="PR #1",
        engine="test-engine",
        remediation_diff=ACTIONABLE_REMEDIATION,
        publish_allowed=True,
    )
    assert "**Publication Policy:** Suppressed" in report
    assert "hmac.compare_digest" not in report


# ---------------------------------------------------------------------------
# 16. Report metadata injection: no field of a rendered report can become a
#     live link, a remote image, or a bare autolinked URL.
# ---------------------------------------------------------------------------


INJECTED_URLS = (
    "https://evil.invalid/pixel.png",
    "http://evil.invalid/beacon",
    "https://user:pw@evil.invalid/",
    "www.evil.invalid",
    "HTTPS://EVIL.INVALID/",
)


def _assert_no_renderable_url(report: str) -> None:
    """No injected construct may survive as something a renderer can follow.

    The host text is deliberately left readable — neutralization breaks the
    scheme, it does not scrub prose — so the check is on what would render: an
    autolinkable scheme, a bare `www.` host, an image, or a link target. The
    report's own `[BLOCKER]` tags are part of the template, not the injection.
    """
    assert re.search(r"(?i)https?:/{1,3}", report) is None, report
    assert re.search(r"(?i)\bwww[.][^\u200b]", report) is None, report
    # An image and a link both need an unescaped opening bracket. The closing
    # `](` may remain — escaped on the left it renders as the literal text.
    assert re.search(r"(?<!\\)!\[", report) is None, report
    assert re.search(r"(?<!\\)\[[^\]\n]*\]\(", report) is None, report
    # Everything the text said is still there to read.
    assert re.search(r"(?i)evil[.]invalid", report) is not None, report


@pytest.mark.parametrize("url", INJECTED_URLS)
def test_metadata_cannot_render_links_images_or_raw_urls(url: str):
    metadata = (
        f"Files changed: 1 ![tracker]({url}) and [click here]({url}) "
        f"plus a bare {url} and a forged heading\n## forged"
    )
    report = format_audit_report(
        executive_summary="clean",
        findings=[],
        confidence=90,
        status=build_status_label(0, 0, 90),
        audit_target="PR #1",
        engine="test-engine",
        remediation_diff="(none)",
        publish_allowed=True,
        analysis_metadata=metadata,
    )
    assert "**Structural Analysis:**" in report
    _assert_no_renderable_url(report)
    # The text is still readable: only the scheme is broken.
    assert "Files changed: 1" in report


def test_model_produced_titles_and_descriptions_cannot_render_links_or_raw_urls():
    for url in INJECTED_URLS:
        report = format_audit_report(
            executive_summary=f"Verdict traced to {url} and ![i]({url})",
            findings=[
                {
                    "id": "AUD-URL1",
                    "file_path": "backend/app/auth.py",
                    "line_start": 84,
                    "line_end": 84,
                    "perspective": "security",
                    "severity": "BLOCKER",
                    "category": "Exfiltration",
                    "title": f"Token exfiltrated to {url}",
                    "description": (
                        f"Leaks the token to [the collector]({url}) and to {url}, "
                        f"rendered as ![pixel]({url})."
                    ),
                    "suggested_fix": "None",
                    "confidence": 90,
                }
            ],
            confidence=90,
            status=build_status_label(1, 0, 90),
            audit_target=f"PR #1 {url}",
            engine="test-engine",
            remediation_diff="(none)",
            publish_allowed=True,
            analysis_metadata=f"Audited {url}",
        )
        _assert_no_renderable_url(report)
        # The finding itself is still reported, only inert.
        assert "#### 1." in report
        assert "**Impact:**" in report


def test_url_neutralization_does_not_mangle_ordinary_prose_or_code():
    report = format_audit_report(
        executive_summary="Callers pass (token, stored) to hmac.compareDigest; "
        "the [0] index and !flag are unaffected.",
        findings=[],
        confidence=90,
        status=build_status_label(0, 0, 90),
        audit_target="Commit `abc123def456` / PR `#42`",
        engine="test-engine",
        remediation_diff="(none)",
        publish_allowed=True,
        analysis_metadata="Fetched via api.github.com; host mongodb.example:27017",
    )
    assert "hmac.compareDigest" in report
    # A host that is not a linkable scheme stays readable, and no zero-width
    # space was slipped into ordinary text.
    assert "api.github.com" in report
    assert "mongodb.example:27017" in report
    assert "\u200b" not in report
    # Bracket characters that were never markup are escaped, not dropped.
    assert "\\[0\\]" in report
    assert "\\!flag" in report


# ---------------------------------------------------------------------------
# 17. Model/migration parity: the model declares every CHECK constraint the
#     migrations create, so `alembic check` reports no drift.
# ---------------------------------------------------------------------------


def _model_check_constraints() -> dict[str, str]:
    from sqlalchemy import CheckConstraint

    from app.models import User

    return {
        str(constraint.name): " ".join(str(constraint.sqltext).split())
        for constraint in getattr(User.__table__, "constraints", [])
        if isinstance(constraint, CheckConstraint)
    }


def test_users_role_constraint_matches_the_migration(monkeypatch: pytest.MonkeyPatch):
    """The model must declare exactly what migration c3d4e5f6a7b8 creates.

    A CHECK constraint that exists only in the database is invisible to the
    metadata Alembic autogenerates against, so `alembic check` proposes dropping
    and recreating it on every run. The migration itself is history and is left
    exactly as it is; only the model side is asserted here.
    """
    from sqlalchemy import CheckConstraint

    module = _audit_migration("c3d4e5f6a7b8")
    recorder = _OpRecorder()
    monkeypatch.setattr(module, "op", recorder)

    module.upgrade()

    created = [
        (args, kwargs)
        for name, args, kwargs in recorder.calls
        if name == "create_check_constraint"
    ]
    assert len(created) == 1
    args, kwargs = created[0]
    name = args[0] if args else kwargs["constraint_name"]
    table = args[1] if len(args) > 1 else kwargs["table_name"]
    expression = args[2] if len(args) > 2 else kwargs["condition"]
    assert name == "check_user_role"
    assert table == "users"
    # The downgrade still removes exactly what the upgrade created.
    monkeypatch.setattr(module, "op", _OpRecorder())
    module.downgrade()
    dropped = [
        (args, kwargs)
        for call, args, kwargs in module.op.calls
        if call == "drop_constraint"
    ]
    assert len(dropped) == 1
    assert (dropped[0][0][0] if dropped[0][0] else dropped[0][1]["constraint_name"]) == name
    assert (dropped[0][0][1] if len(dropped[0][0]) > 1 else dropped[0][1]["table_name"]) == table

    model_constraints = _model_check_constraints()
    assert name in model_constraints, sorted(model_constraints)
    assert model_constraints[name] == " ".join(str(expression).split())


def test_every_migration_check_constraint_is_declared_on_its_model(
    monkeypatch: pytest.MonkeyPatch,
):
    """No table may carry a database-only CHECK constraint.

    Replays every migration in order, tracking the *effective* definition of each
    CHECK constraint — a `drop_constraint` followed by a `create_check_constraint`
    of the same name replaces it, exactly as it would in the database — and
    requires the model to declare the same name, table and expression the
    migrations end at. That is the property that keeps `alembic check` at zero
    drift.

    A migration that creates a constraint but cannot be inspected here is a
    failure, not a skip: a silently uninspected migration is exactly how a drift
    regression comes back.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import CheckConstraint

    from app.models import Base

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    declared: dict[str, dict[str, str]] = {
        name: {
            str(constraint.name): " ".join(str(constraint.sqltext).split())
            for constraint in cast(Any, table).constraints
            if isinstance(constraint, CheckConstraint)
        }
        for name, table in Base.metadata.tables.items()
    }

    effective: dict[tuple[str, str], str] = {}
    inspected = 0
    for revision in reversed(list(script.walk_revisions())):
        module = revision.module
        declares = "create_check_constraint" in Path(str(module.__file__)).read_text(
            encoding="utf-8"
        )
        recorder = _OpRecorder()
        monkeypatch.setattr(module, "op", recorder)
        try:
            module.upgrade()
        except Exception as exc:  # noqa: BLE001 — reported, never swallowed
            if declares:
                raise AssertionError(
                    f"{revision.revision} declares a CHECK constraint but could not "
                    f"be inspected: {type(exc).__name__}"
                ) from exc
            continue
        if not declares:
            continue
        inspected += 1
        for name, args, kwargs in recorder.calls:
            if name == "drop_constraint":
                key = (
                    str(args[1] if len(args) > 1 else kwargs["table_name"]),
                    str(args[0] if args else kwargs["constraint_name"]),
                )
                effective.pop(key, None)
            elif name == "create_check_constraint":
                key = (
                    str(args[1] if len(args) > 1 else kwargs["table_name"]),
                    str(args[0] if args else kwargs["constraint_name"]),
                )
                effective[key] = " ".join(
                    str(args[2] if len(args) > 2 else kwargs["condition"]).split()
                )

    missing = [
        f"{table}.{name} migration={expression!r} "
        f"model={declared.get(table, {}).get(name)!r}"
        for (table, name), expression in sorted(effective.items())
        if declared.get(table, {}).get(name) != expression
    ]
    assert inspected >= 1
    assert effective, "no CHECK constraint was replayed from the migrations"
    assert missing == []


# ---------------------------------------------------------------------------
# 18. Worker-mode detection: the audit worker and the hosting adapter resolve the
#     Lambda function name through the same resolver, env fallback included.
# ---------------------------------------------------------------------------


def test_the_hosting_adapter_has_no_second_function_name_resolver():
    from app.adapters import hosting

    source = _code_only(inspect.getsource(hosting))
    # One resolver, not two: every schedule_* path goes through it, and neither
    # the environment variable nor the setting is read here. A second copy of
    # the rule is a second answer, and the two answers can disagree about
    # whether this process is Lambda.
    assert source.count("resolve_lambda_function_name(") == 3
    assert "AWS_LAMBDA_FUNCTION_NAME" not in source
    assert "aws_lambda_function_name" not in source


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("environment", "configured", "expected"),
    [
        ({}, None, None),
        ({}, "   ", None),
        ({}, "configured-fn", "configured-fn"),
        ({"AWS_LAMBDA_FUNCTION_NAME": "env-fn"}, None, "env-fn"),
        ({"AWS_LAMBDA_FUNCTION_NAME": "   "}, None, None),
        # A configured name wins over the environment fallback, as it does for
        # the worker's own resolver.
        ({"AWS_LAMBDA_FUNCTION_NAME": "env-fn"}, "configured-fn", "configured-fn"),
    ],
)
async def test_both_resolvers_agree_including_the_environment_fallback(
    environment: dict[str, str], configured: str | None, expected: str | None
):
    from app import lambda_runtime
    from app.adapters.hosting import AWSHostingAdapter

    fence = audit_pipeline.audit_dispatch_fence_token(
        "audit-abcdef123456", 1, "audit-secret"
    )
    lambda_client = MagicMock()
    lambda_client.invoke.return_value = {"StatusCode": 202}
    # Not clear=True: botocore still needs a real environment to be imported, and
    # only AWS_LAMBDA_FUNCTION_NAME is part of the rule under test.
    with patch.dict("os.environ", environment), patch(
        "app.config.settings.aws_lambda_function_name", configured
    ), patch(
        "app.config.settings.audit_self_invoke_secret", "audit-secret"
    ), patch(
        "boto3.client", return_value=lambda_client
    ):
        if "AWS_LAMBDA_FUNCTION_NAME" not in environment:
            os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
        resolved = lambda_runtime.resolve_lambda_function_name()
        assert resolved == expected
        assert lambda_runtime.is_lambda_runtime() is (expected is not None)
        if expected is None:
            with pytest.raises(RuntimeError, match="function name is not configured"):
                await AWSHostingAdapter().schedule_audit("audit-abcdef123456", fence)
        else:
            # The adapter independently resolved the same name from the same
            # inputs, through the one shared resolver.
            await AWSHostingAdapter().schedule_audit("audit-abcdef123456", fence)
            assert (
                lambda_client.invoke.call_args.kwargs["FunctionName"] == expected
            )
    if expected is None:
        lambda_client.invoke.assert_not_called()


@pytest.mark.asyncio
async def test_a_lambda_like_runtime_refuses_to_execute_audit_jobs_in_process():
    from app.lambda_runtime import is_lambda_runtime, resolve_lambda_function_name

    audit_pipeline._audit_dispatch_workers.clear()
    with (
        # The environment fallback alone is enough to look like Lambda. A
        # deployment that sets only AWS_LAMBDA_FUNCTION_NAME must not start an
        # in-process audit worker either, and the child the dispatcher schedules
        # must carry the same resolved name.
        patch.dict("os.environ", {"AWS_LAMBDA_FUNCTION_NAME": "haunter-prod"}),
        patch("app.config.settings.aws_lambda_function_name", None),
        patch("app.services.audit_pipeline.asyncio.create_task") as create_task,
    ):
        assert resolve_lambda_function_name() == "haunter-prod"
        assert is_lambda_runtime() is True
        assert audit_pipeline.start_audit_dispatch_workers() == 0
    create_task.assert_not_called()
    assert audit_pipeline._audit_dispatch_workers == set()

    # The durable dispatcher schedules a Lambda child there rather than running
    # the job in this process, which Lambda would freeze on return.
    from app.adapters.hosting import AWSHostingAdapter

    adapter = MagicMock(spec=AWSHostingAdapter)
    with (
        patch.dict("os.environ", {"AWS_LAMBDA_FUNCTION_NAME": "haunter-prod"}),
        patch("app.config.settings.aws_lambda_function_name", None),
        patch(
            "app.services.audit_pipeline.recover_expired_audit_leases",
            new_callable=AsyncMock,
            return_value=(0, 0),
        ),
        patch(
            "app.services.audit_pipeline.claim_next_queued_job",
            new_callable=AsyncMock,
            return_value=audit_pipeline.AuditDispatchClaim(
                audit_id="audit-aaaaaaaaaaaa",
                dispatch_attempts=1,
                dispatch_fence_token="0" * 64,
            ),
        ),
        patch(
            "app.adapters.hosting.get_hosting_adapter",
            new_callable=AsyncMock,
            return_value=adapter,
        ),
        patch(
            "app.services.audit_pipeline.process_audit_job", new_callable=AsyncMock
        ) as in_process,
    ):
        summary = await audit_pipeline.dispatch_audit_jobs(batch_size=1)

    assert summary.scheduled == 1
    in_process.assert_not_called()
    adapter.schedule_audit.assert_awaited_once_with("audit-aaaaaaaaaaaa", "0" * 64)

