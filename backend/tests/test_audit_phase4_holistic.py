"""Fast unit tests for Phase 4: Holistic auditor perspective & non-commentable findings.

Covers Issue #244:
- Full unchunked diff passed to holistic perspective (fixed count 1, full diff).
- PerspectiveFinding schema validation for both inline and non-commentable findings.
- AuditFinding serialization forwarding `non_commentable`.
- Synthesizer retains non-commentable findings despite grounding.
- `_render_structural_analysis` renders non-commentable bullets under Structural Analysis.
- `format_audit_report` renders non-commentable findings cleanly with architectural badge (no #L0).
- `build_inline_review_comments` suppresses non-commentable findings.
- Scoped posting does not count non-commentable findings toward the inline 8-slot cap.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from app.github.audit_publisher import (
    build_inline_review_comments,
    publish_audit_review,
)
from app.llm.prompts.audit_prompts import (
    _render_structural_analysis,
    format_audit_report,
)
from app.subagents.auditor import (
    AuditFinding,
    AuditResult,
    PerspectiveFinding,
    PerspectiveResult,
    run_audit,
    synthesize_findings,
)
from tests.test_auditor_core import SAMPLE_DIFF


# ---------------------------------------------------------------------------
# Test 1: PerspectiveFinding schema validation
# ---------------------------------------------------------------------------

def test_perspective_finding_schema_validation():
    # Valid inline finding
    inline = PerspectiveFinding(
        file_path="backend/app/auth.py",
        line_start=10,
        line_end=15,
        severity="WARNING",
        category="Correctness",
        title="Null dereference",
        description="Unhandled None items input.",
        suggested_fix="if not items: return 0.0",
        confidence=90,
        non_commentable=False,
    )
    assert inline.non_commentable is False
    assert inline.file_path == "backend/app/auth.py"
    assert inline.line_start == 10

    # Valid non-commentable finding with line 0 and general path
    non_comm = PerspectiveFinding(
        file_path="(general)",
        line_start=0,
        line_end=0,
        severity="WARNING",
        category="Scope-Drift",
        title="Unrequested analytics",
        description="Added analytics tracking outside PR requirements.",
        confidence=85,
        non_commentable=True,
    )
    assert non_comm.non_commentable is True
    assert non_comm.line_start == 0
    assert non_comm.file_path == "(general)"

    # Valid non-commentable finding with empty string path
    non_comm_empty_path = PerspectiveFinding(
        file_path="",
        line_start=0,
        line_end=0,
        severity="NOTE",
        category="Minimality",
        title="Formatting churn",
        description="Extraneous whitespace changes across touched files.",
        confidence=80,
        non_commentable=True,
    )
    assert non_comm_empty_path.non_commentable is True
    assert non_comm_empty_path.file_path == ""

    # Invalid: line_start 0 with non_commentable=False must fail validation
    with pytest.raises(ValidationError):
        PerspectiveFinding(
            file_path="backend/app/auth.py",
            line_start=0,
            line_end=0,
            severity="WARNING",
            category="Correctness",
            title="Invalid line start",
            description="Line start 0 is forbidden for commentable findings.",
            confidence=90,
            non_commentable=False,
        )

    # Invalid: empty file_path with non_commentable=False must fail validation
    with pytest.raises(ValidationError):
        PerspectiveFinding(
            file_path="",
            line_start=10,
            line_end=10,
            severity="WARNING",
            category="Correctness",
            title="Empty file path",
            description="Empty path is forbidden for commentable findings.",
            confidence=90,
            non_commentable=False,
        )


# ---------------------------------------------------------------------------
# Test 2: AuditFinding forwarding & serialization
# ---------------------------------------------------------------------------

def test_audit_finding_model_forwarding_and_serialization():
    finding = AuditFinding(
        id="AUD-TEST123456",
        file_path="(general)",
        line_start=0,
        line_end=0,
        perspective="holistic",
        severity="WARNING",
        category="Scope-Drift",
        title="Unrequested feature",
        description="Feature not requested in issue.",
        suggested_fix=None,
        confidence=88,
        informational_only=False,
        non_commentable=True,
    )

    as_dict = finding._as_dict()
    assert as_dict["non_commentable"] is True
    assert as_dict["line_start"] == 0

    to_dict = finding.to_dict()
    assert to_dict["non_commentable"] is True
    assert to_dict["category"] == "Scope-Drift"

    to_report = finding.to_report_dict()
    assert to_report["non_commentable"] is True


# ---------------------------------------------------------------------------
# Test 3: Synthesizer retains non-commentable findings despite grounding
# ---------------------------------------------------------------------------

def test_synthesizer_retains_non_commentable_findings():
    # Holistic non-commentable finding with line 0 (not in SAMPLE_DIFF)
    holistic_finding = PerspectiveFinding(
        file_path="(general)",
        line_start=0,
        line_end=0,
        severity="WARNING",
        category="Over-Engineering",
        title="Premature abstraction",
        description="Excess interface layers introduced for single consumer.",
        confidence=90,
        non_commentable=True,
    )

    # Inline ungrounded finding with line 9999 (not in SAMPLE_DIFF)
    ungrounded_inline = PerspectiveFinding(
        file_path="backend/app/auth.py",
        line_start=9999,
        line_end=9999,
        severity="BLOCKER",
        category="Security",
        title="Bogus bug",
        description="Phantom line that does not exist.",
        confidence=95,
        non_commentable=False,
    )

    result = PerspectiveResult(
        perspective="holistic",
        summary="Holistic review completed.",
        confidence=90,
        findings=[holistic_finding, ungrounded_inline],
        succeeded=True,
    )

    synthesized = synthesize_findings([result], SAMPLE_DIFF, overall_confidence=90)

    # Ungrounded inline finding must be dropped
    assert not any(f.title == "Bogus bug" for f in synthesized)

    # Non-commentable finding MUST be retained
    retained = next((f for f in synthesized if f.title == "Premature abstraction"), None)
    assert retained is not None
    assert retained.non_commentable is True
    assert retained.line_start == 0


# ---------------------------------------------------------------------------
# Test 4: _render_structural_analysis renders non-commentable bullets
# ---------------------------------------------------------------------------

def test_render_structural_analysis_bullets():
    non_commentables = [
        {
            "category": "Scope-Drift",
            "title": "Unrequested Telemetry",
            "description": "Added third-party analytics collector not specified in issue.",
        },
        {
            "category": "Over-Engineering",
            "title": "Abstract Factory Pattern",
            "description": "Introduced factory pattern for single implementation.",
        },
    ]

    rendered = _render_structural_analysis(
        metadata="Files changed: 1; +10 / -0 lines",
        blast_radius="**Low**",
        non_commentable_findings=non_commentables,
    )

    assert "### 🔬 Structural & Blast Radius Analysis" in rendered
    assert "- **Scope-Drift:** Unrequested Telemetry — Added third-party analytics collector not specified in issue." in rendered
    assert "- **Over-Engineering:** Abstract Factory Pattern — Introduced factory pattern for single implementation." in rendered


# ---------------------------------------------------------------------------
# Test 5: format_audit_report renders non-commentables cleanly
# ---------------------------------------------------------------------------

def test_format_audit_report_renders_non_commentables_cleanly():
    findings = [
        {
            "category": "Correctness",
            "title": "Null pointer error",
            "description": "Missing check on empty list.",
            "file_path": "backend/app/auth.py",
            "line_start": 80,
            "line_end": 82,
            "severity": "WARNING",
            "confidence": 90,
            "non_commentable": False,
        },
        {
            "category": "Direction",
            "title": "Deviates from repository design",
            "description": "Reimplemented custom event bus instead of using existing broker.",
            "file_path": "(general)",
            "line_start": 0,
            "line_end": 0,
            "severity": "WARNING",
            "confidence": 88,
            "non_commentable": True,
        },
    ]

    report = format_audit_report(
        pr_summary="Test PR",
        findings=findings,
        confidence=90,
        status="⚠️ Merge Blocked (Changes Needed)",
    )

    # Non-commentable finding gets architectural badge 🏛️ without broken #L0 link
    assert "🏛️ **Deviates from repository design**" in report
    assert "#L0" not in report

    # Inline finding retains code link
    assert "[Null pointer error](backend/app/auth.py#L80-L82)" in report

    # Non-commentable also appears under Structural & Blast Radius Analysis
    assert "- **Direction:** Deviates from repository design —" in report


# ---------------------------------------------------------------------------
# Test 6: build_inline_review_comments suppresses non-commentable findings
# ---------------------------------------------------------------------------

def test_build_inline_review_comments_suppresses_non_commentables():
    commentable = AuditFinding(
        id="AUD-000000000001",
        file_path="backend/app/auth.py",
        line_start=80,
        line_end=81,
        perspective="correctness",
        severity="WARNING",
        category="Correctness",
        title="Valid inline",
        description="Valid commentable finding.",
        suggested_fix="return True",
        confidence=90,
        non_commentable=False,
    )
    non_commentable = AuditFinding(
        id="AUD-000000000002",
        file_path="(general)",
        line_start=0,
        line_end=0,
        perspective="holistic",
        severity="WARNING",
        category="Scope-Drift",
        title="Architectural drift",
        description="General architectural finding.",
        suggested_fix=None,
        confidence=90,
        non_commentable=True,
    )

    comments, suppressed = build_inline_review_comments(
        [commentable, non_commentable],
        diff_text=SAMPLE_DIFF,
    )

    assert len(comments) == 1
    assert comments[0]["path"] == "backend/app/auth.py"
    assert suppressed == 1


# ---------------------------------------------------------------------------
# Test 7: Scoped posting does not count non-commentable towards inline cap
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scoped_posting_does_not_count_non_commentables_towards_cap():
    # 4 non-commentable findings
    non_commentables = [
        AuditFinding(
            id=f"AUD-NC-{i}",
            file_path="(general)",
            line_start=0,
            line_end=0,
            perspective="holistic",
            severity="WARNING",
            category="Scope-Drift",
            title=f"Non commentable {i}",
            description="PR-level architectural drift.",
            suggested_fix=None,
            confidence=90,
            non_commentable=True,
        )
        for i in range(4)
    ]

    # 8 commentable findings in backend/app/auth.py
    commentables = [
        AuditFinding(
            id=f"AUD-COMM-{i}",
            file_path="backend/app/auth.py",
            line_start=80 + (i % 8),
            line_end=80 + (i % 8),
            perspective="correctness",
            severity="WARNING",
            category="Correctness",
            title=f"Commentable {i}",
            description="Inline bug.",
            suggested_fix=None,
            confidence=85,
            non_commentable=False,
        )
        for i in range(8)
    ]

    all_findings = non_commentables + commentables

    result = AuditResult(
        audit_id="audit-test-cap",
        audit_type="pr_audit",
        repo_full_name="owner/repo",
        target_label="PR #1",
        engine="test-engine",
        executive_summary="Summary",
        findings=all_findings,
        confidence=90,
        status="⚠️ Merge Blocked (Changes Needed)",
        publish_allowed=True,
    )

    mock_client = AsyncMock()
    mock_client.create_pr_review = AsyncMock(return_value={"id": 12345})
    mock_client.get_dossier_meta = AsyncMock(return_value={})
    mock_client.save_dossier_meta = AsyncMock()

    with patch("app.github.audit_publisher.github_client", mock_client):
        pub_result = await publish_audit_review(
            result=result,
            pr_number=1,
            head_sha="abcdef1234567890abcdef1234567890abcdef12",
            diff_text=SAMPLE_DIFF,
            token="ghp_fake_token",
        )

    assert pub_result.published is True
    assert mock_client.create_pr_review.called
    review_call = mock_client.create_pr_review.call_args[1]

    posted_comments = review_call.get("comments", [])
    assert len(posted_comments) > 0
    assert all(c["path"] == "backend/app/auth.py" for c in posted_comments)


# ---------------------------------------------------------------------------
# Test 8: Full unchunked diff passed to holistic perspective
# ---------------------------------------------------------------------------

class _RecordingLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def complete(self, messages: list[dict[str, Any]], **kwargs) -> Any:
        content = " ".join(m.get("content", "") for m in messages)
        self.calls.append({"messages": messages, "content": content})
        return {
            "content": json.dumps(
                {
                    "summary": "Clean perspective verdict.",
                    "confidence": 92,
                    "findings": [],
                }
            ),
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "model": "test-engine",
        }


@pytest.mark.asyncio
async def test_full_unchunked_diff_passed_to_holistic_perspective():
    # Build a synthetic diff with 700 lines that triggers chunking (>550 lines)
    hunks = []
    for f_idx in range(15):
        lines = [f"+    value_{f_idx}_{line_idx} = {line_idx} * 2" for line_idx in range(50)]
        hunk = (
            f"diff --git a/backend/file_{f_idx}.py b/backend/file_{f_idx}.py\n"
            f"--- a/backend/file_{f_idx}.py\n"
            f"+++ b/backend/file_{f_idx}.py\n"
            f"@@ -1,5 +1,55 @@\n"
            + "\n".join(lines)
            + "\n"
        )
        hunks.append(hunk)

    large_diff = "\n".join(hunks)
    fake_llm = _RecordingLLM()

    result = await run_audit(
        diff_text=large_diff,
        repo_full_name="org/repo",
        pr_number=99,
        llm_client=fake_llm,
        engine="test-engine",
        enable_phase1_context=False,
    )

    assert isinstance(result, AuditResult)

    # Find the holistic perspective call
    holistic_calls = [
        c for c in fake_llm.calls if "## Perspective\nholistic" in c["content"]
    ]
    assert len(holistic_calls) == 1, "Holistic perspective must run exactly once (fixed count 1)"

    holistic_content = holistic_calls[0]["content"]
    # Holistic call must contain code from both early and late files in the diff (full diff)
    assert "file_0.py" in holistic_content
    assert "file_14.py" in holistic_content

    # The result perspectives must include 'holistic'
    perspectives_executed = {p.perspective for p in result.perspectives}
    assert "holistic" in perspectives_executed
