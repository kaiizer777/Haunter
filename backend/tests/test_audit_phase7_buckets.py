"""Fast unit tests for Phase 7 — Reclassify findings into must-fix vs should-fix buckets (#247)."""

from typing import Any
import pytest

from app.llm.prompts.audit_prompts import format_audit_report
from app.github.audit_publisher import (
    format_finding_comment_body,
    publish_audit_review,
)
from app.services.review_orchestrator import _count_findings_by_severity
from app.subagents.auditor import (
    AuditFinding,
    AuditResult,
    PerspectiveFinding,
    classify_finding_bucket,
)
from app.subagents.code_reviewer import ReviewFinding


def _make_perspective_finding(**kwargs: Any) -> PerspectiveFinding:
    defaults = {
        "file_path": "backend/app/auth.py",
        "line_start": 10,
        "line_end": 12,
        "severity": "WARNING",
        "category": "correctness",
        "title": "Possible off-by-one error",
        "description": "Validation check misses the upper boundary by one index.",
        "confidence": 90,
    }
    defaults.update(kwargs)
    return PerspectiveFinding(**defaults)


def _make_audit_finding(**kwargs: Any) -> AuditFinding:
    defaults = {
        "id": "AUD-TEST123456",
        "file_path": "backend/app/main.py",
        "line_start": 20,
        "line_end": 22,
        "perspective": "correctness",
        "severity": "WARNING",
        "category": "logic",
        "title": "Uncaught NoneType exception",
        "description": "Variable user can be None when session expired.",
        "suggested_fix": None,
        "confidence": 88,
        "informational_only": False,
        "non_commentable": False,
    }
    defaults.update(kwargs)
    return AuditFinding(**defaults)


def _make_review_finding(**kwargs: Any) -> ReviewFinding:
    defaults = {
        "file_path": "backend/app/api.py",
        "line_start": 5,
        "line_end": 7,
        "category": "logic",
        "severity": "medium",
        "critique": "Endpoint does not validate incoming payload size.",
        "suggested_code": None,
    }
    defaults.update(kwargs)
    return ReviewFinding(**defaults)


# ---------------------------------------------------------------------------
# 1. Classification Tests
# ---------------------------------------------------------------------------

def test_classify_critical_and_blocker_always_must_fix():
    f1 = {"severity": "BLOCKER", "confidence": 95, "title": "Crash on startup"}
    f2 = {"severity": "CRITICAL", "confidence": 90, "title": "Buffer overflow"}
    assert classify_finding_bucket(f1) == "must_fix"
    assert classify_finding_bucket(f2) == "must_fix"


def test_classify_high_good_risk_is_must_fix():
    # Category based
    for cat in ("security", "correctness", "logic", "breaking_change", "data_loss", "scope_drift"):
        f = {"severity": "HIGH", "category": cat, "confidence": 85, "title": "Important issue"}
        assert classify_finding_bucket(f) == "must_fix", f"Failed for category {cat}"

    # Keyword based in title or description
    f_crash = {"severity": "HIGH", "category": "architecture", "confidence": 85, "title": "Deadlock in worker loop"}
    assert classify_finding_bucket(f_crash) == "must_fix"

    f_leak = {"severity": "HIGH", "category": "performance", "confidence": 85, "title": "Severe memory leak in connection pool"}
    assert classify_finding_bucket(f_leak) == "must_fix"


def test_classify_high_low_priority_is_should_fix():
    f_style = {
        "severity": "HIGH",
        "category": "style",
        "confidence": 90,
        "title": "Variable naming convention violation",
        "description": "Rename snake_case to camelCase.",
    }
    assert classify_finding_bucket(f_style) == "should_fix"

    f_perf_minor = {
        "severity": "HIGH",
        "category": "performance",
        "confidence": 90,
        "title": "Minor perf optimization available",
        "description": "Consider using list comprehension instead of loop.",
    }
    assert classify_finding_bucket(f_perf_minor) == "should_fix"

    f_nice = {
        "severity": "HIGH",
        "category": "general",
        "confidence": 90,
        "title": "Nice-to-have cleanup in router",
        "description": "Could simplify helper function.",
    }
    assert classify_finding_bucket(f_nice) == "should_fix"


def test_classify_medium_good_is_should_fix():
    f_logic = {
        "severity": "MEDIUM",
        "category": "logic",
        "confidence": 85,
        "title": "Missing validation for negative numbers",
        "description": "Add boundary check for negative input values.",
    }
    assert classify_finding_bucket(f_logic) == "should_fix"

    f_warning = {
        "severity": "WARNING",
        "category": "security",
        "confidence": 85,
        "title": "Missing rate limit on login endpoint",
        "description": "Enforce brute-force protection.",
    }
    assert classify_finding_bucket(f_warning) == "should_fix"


def test_classify_medium_trivial_nit_is_suggestion():
    f_nit = {
        "severity": "MEDIUM",
        "category": "nit",
        "confidence": 85,
        "title": "Nit: extra whitespace at line end",
        "description": "Remove trailing space.",
    }
    assert classify_finding_bucket(f_nit) == "suggestion"

    f_style = {
        "severity": "WARNING",
        "category": "style",
        "confidence": 85,
        "title": "Inconsistent formatting",
        "description": "Format using standard code style.",
    }
    assert classify_finding_bucket(f_style) == "suggestion"

    f_typo = {
        "severity": "MEDIUM",
        "category": "documentation",
        "confidence": 85,
        "title": "Typo in comment",
        "description": "Fix typo in docstring.",
    }
    assert classify_finding_bucket(f_typo) == "suggestion"


def test_classify_low_and_notes_are_suggestions():
    f_low = {"severity": "LOW", "confidence": 90, "title": "Consider refactoring"}
    f_note = {"severity": "NOTE", "confidence": 95, "title": "Informational note"}
    f_info = {"severity": "INFO", "confidence": 90, "title": "FYI"}
    assert classify_finding_bucket(f_low) == "suggestion"
    assert classify_finding_bucket(f_note) == "suggestion"
    assert classify_finding_bucket(f_info) == "suggestion"


def test_classify_low_confidence_or_informational_is_suggestion():
    # Finding confidence < 75 -> suggestion even if BLOCKER
    f_blocker_low_conf = {
        "severity": "BLOCKER",
        "category": "security",
        "confidence": 60,
        "title": "Unverified exploit candidate",
    }
    assert classify_finding_bucket(f_blocker_low_conf) == "suggestion"

    # overall_confidence < 75 -> suggestion
    f_high = {"severity": "HIGH", "category": "security", "confidence": 90, "title": "SQL injection"}
    assert classify_finding_bucket(f_high, overall_confidence=70) == "suggestion"

    # informational_only=True -> suggestion
    f_info_flag = {"severity": "BLOCKER", "confidence": 95, "informational_only": True, "title": "Flagged info"}
    assert classify_finding_bucket(f_info_flag) == "suggestion"


def test_classify_explicit_priority_bucket_respected():
    f_explicit_must = {"severity": "LOW", "confidence": 90, "priority_bucket": "must_fix", "title": "Promoted to must-fix"}
    assert classify_finding_bucket(f_explicit_must) == "must_fix"

    f_explicit_should = {"severity": "BLOCKER", "confidence": 90, "priority_bucket": "should-fix", "title": "Demoted to should-fix"}
    assert classify_finding_bucket(f_explicit_should) == "should_fix"

    f_explicit_sugg = {"severity": "HIGH", "confidence": 90, "priority_bucket": "suggestion", "title": "Demoted to suggestion"}
    assert classify_finding_bucket(f_explicit_sugg) == "suggestion"


# ---------------------------------------------------------------------------
# 2. Schema and Model Tests
# ---------------------------------------------------------------------------

def test_perspective_finding_priority_bucket():
    pf1 = _make_perspective_finding(priority_bucket="must-fix")
    assert pf1.priority_bucket == "must_fix"

    pf2 = _make_perspective_finding(priority_bucket="should_fix")
    assert pf2.priority_bucket == "should_fix"

    pf3 = _make_perspective_finding()
    assert pf3.priority_bucket is None


def test_audit_finding_priority_bucket_serialization():
    # Auto-classified in __post_init__ when not provided
    af_auto = _make_audit_finding(severity="BLOCKER", category="security")
    assert af_auto.priority_bucket == "must_fix"
    d_auto = af_auto.to_dict()
    assert d_auto["priority_bucket"] == "must_fix"
    r_auto = af_auto.to_report_dict()
    assert r_auto["priority_bucket"] == "must_fix"

    # Explicitly provided
    af_explicit = _make_audit_finding(severity="NOTE", priority_bucket="should_fix")
    assert af_explicit.priority_bucket == "should_fix"
    assert af_explicit._as_dict()["priority_bucket"] == "should_fix"


def test_audit_result_bucket_counts():
    f1 = _make_audit_finding(id="AUD-1", severity="BLOCKER", category="security")  # must_fix
    f2 = _make_audit_finding(id="AUD-2", severity="WARNING", category="logic")     # should_fix
    f3 = _make_audit_finding(id="AUD-3", severity="NOTE", category="style")        # suggestion
    f4 = _make_audit_finding(id="AUD-4", severity="WARNING", category="style", title="Nit: spacing") # suggestion

    res = AuditResult(
        audit_id="audit-123456789012",
        audit_type="pr_audit",
        repo_full_name="org/repo",
        target_label="PR #1",
        engine="test",
        executive_summary="Summary",
        findings=[f1, f2, f3, f4],
        confidence=90,
    )

    assert res.must_fix_count == 1
    assert res.should_fix_count == 1
    assert res.suggestion_count == 2

    res_dict = res.to_dict()
    assert res_dict["must_fix_count"] == 1
    assert res_dict["should_fix_count"] == 1
    assert res_dict["suggestion_count"] == 2


# ---------------------------------------------------------------------------
# 3. Main Comment Formatting & Badges
# ---------------------------------------------------------------------------

def test_format_audit_report_reflects_priority_buckets():
    findings = [
        # Must-Fix: High correctness
        {
            "severity": "HIGH",
            "category": "correctness",
            "title": "Critical boundary violation",
            "description": "Throws IndexError on empty input.",
            "file_path": "backend/app/calc.py",
            "line_start": 10,
            "line_end": 10,
            "confidence": 90,
        },
        # Should-Fix: High style (low-priority high)
        {
            "severity": "HIGH",
            "category": "style",
            "title": "Style: CamelCase in python module",
            "description": "Follow PEP8 naming.",
            "file_path": "backend/app/calc.py",
            "line_start": 15,
            "line_end": 15,
            "confidence": 90,
        },
        # Should-Fix: Medium logic (good medium)
        {
            "severity": "MEDIUM",
            "category": "logic",
            "title": "Uncaught ValueError on parsing",
            "description": "Wrap int conversion in try-except.",
            "file_path": "backend/app/calc.py",
            "line_start": 20,
            "line_end": 20,
            "confidence": 85,
        },
        # Suggestion: Medium style (trivial medium)
        {
            "severity": "MEDIUM",
            "category": "style",
            "title": "Nit: redundant parenthesis",
            "description": "Remove unnecessary parens.",
            "file_path": "backend/app/calc.py",
            "line_start": 25,
            "line_end": 25,
            "confidence": 85,
        },
        # Suggestion: Low
        {
            "severity": "LOW",
            "category": "general",
            "title": "Consider docstring expansion",
            "description": "Add return type description.",
            "file_path": "backend/app/calc.py",
            "line_start": 30,
            "line_end": 30,
            "confidence": 80,
        },
    ]

    report = format_audit_report(
        pr_summary="Test PR",
        findings=findings,
        confidence=90,
    )

    # Main table counts
    assert "Must-Fix: <code>1</code>" in report
    assert "Should-Fix: <code>2</code>" in report

    # Badges in summary
    assert "⛔ **Must-Fix:** [Critical boundary violation]" in report
    assert "⚠️ **Should-Fix:** [Style: CamelCase in python module]" in report
    assert "⚠️ **Should-Fix:** [Uncaught ValueError on parsing]" in report
    assert "💡 **Suggestion:** [Nit: redundant parenthesis]" in report
    assert "💡 **Suggestion:** [Consider docstring expansion]" in report


# ---------------------------------------------------------------------------
# 4. Comment Publisher Badges and Gate
# ---------------------------------------------------------------------------

def test_format_finding_comment_body_badges():
    f_must = {
        "severity": "HIGH",
        "category": "security",
        "title": "SQL Injection vulnerability",
        "description": "Sanitize user input before query.",
        "file_path": "backend/db.py",
        "line_start": 42,
        "line_end": 42,
    }
    body_must = format_finding_comment_body(f_must)
    assert "### ⛔ Must-Fix: SQL Injection vulnerability" in body_must

    f_should = {
        "severity": "HIGH",
        "category": "style",
        "title": "Style: indentation error",
        "description": "Adjust indentation.",
        "file_path": "backend/db.py",
        "line_start": 10,
        "line_end": 10,
    }
    body_should = format_finding_comment_body(f_should)
    assert "### ⚠️ Should-Fix: Style: indentation error" in body_should

    f_sugg = {
        "severity": "MEDIUM",
        "category": "style",
        "title": "Nit: trailing space",
        "description": "Clean up space.",
        "file_path": "backend/db.py",
        "line_start": 5,
        "line_end": 5,
    }
    body_sugg = format_finding_comment_body(f_sugg)
    assert "### 💡 Suggestion: Nit: trailing space" in body_sugg


@pytest.mark.asyncio
async def test_publish_audit_review_must_fix_requests_changes(monkeypatch):
    # Finding is severity WARNING but priority_bucket="must_fix" (good high risk)
    f = _make_audit_finding(
        severity="WARNING",
        category="security",
        title="Critical auth bypass",
        priority_bucket="must_fix",
        confidence=90,
    )
    res = AuditResult(
        audit_id="audit-987654321012",
        audit_type="pr_audit",
        repo_full_name="org/repo",
        target_label="PR #42",
        engine="test",
        executive_summary="Summary",
        findings=[f],
        confidence=90,
        publish_allowed=True,
    )

    published_event = None

    async def fake_create_pr_review(*args, **kwargs):
        nonlocal published_event
        published_event = kwargs.get("event")
        return {"id": 1, "body": kwargs.get("body", "")}

    monkeypatch.setattr("app.github_client.create_pr_review", fake_create_pr_review)

    await publish_audit_review(
        result=res,
        owner="org",
        repo="repo",
        pr_number=42,
        request_changes_on_blocker=True,
    )

    # Must-fix finding triggered REQUEST_CHANGES even though severity was WARNING
    assert published_event == "REQUEST_CHANGES"


# ---------------------------------------------------------------------------
# 5. Review Orchestrator Findings Count
# ---------------------------------------------------------------------------

def test_orchestrator_count_findings_by_severity():
    f_crit = _make_review_finding(severity="critical", category="security")
    f_good_high = _make_review_finding(severity="high", category="logic", critique="Crash on null pointer")
    f_low_high = _make_review_finding(severity="high", category="performance", critique="Minor perf slowdown in loop")
    f_good_med = _make_review_finding(severity="medium", category="logic", critique="Edge case unhandled")
    f_nit_med = _make_review_finding(severity="medium", category="logic", critique="Nit: rename variable for clarity")
    f_low = _make_review_finding(severity="low", category="api_compatibility")

    findings = [f_crit, f_good_high, f_low_high, f_good_med, f_nit_med, f_low]
    blockers, warnings, suggestions = _count_findings_by_severity(findings)

    # blockers: critical + good high = 2
    assert blockers == 2
    # warnings: low-priority high + good medium = 2
    assert warnings == 2
    # suggestions: trivial medium + low = 2
    assert suggestions == 2


# ---------------------------------------------------------------------------
# 6. Batch 2: Priority Bucketing & Degradation Alignment Tests
# ---------------------------------------------------------------------------

def test_degraded_audit_result_bucket_counts_demoted():
    """When AuditResult confidence < 75 (e.g. analysis_degraded), all counts demote to suggestion."""
    f1 = _make_audit_finding(id="AUD-1", severity="BLOCKER", category="security", priority_bucket="must_fix", confidence=90)
    f2 = _make_audit_finding(id="AUD-2", severity="WARNING", category="logic", priority_bucket="should_fix", confidence=85)
    f3 = _make_audit_finding(id="AUD-3", severity="NOTE", category="style", priority_bucket="suggestion", confidence=80)

    res = AuditResult(
        audit_id="audit-degraded-1",
        audit_type="pr_audit",
        repo_full_name="org/repo",
        target_label="PR #1",
        engine="test",
        executive_summary="Summary",
        findings=[f1, f2, f3],
        confidence=74,  # Degraded / below 75 threshold
        analysis_degraded=True,
    )

    assert res.must_fix_count == 0
    assert res.should_fix_count == 0
    assert res.suggestion_count == 3
    d = res.to_dict()
    assert d["must_fix_count"] == 0
    assert d["should_fix_count"] == 0
    assert d["suggestion_count"] == 3


def test_format_audit_report_badges_and_counts_agree_on_degraded():
    """format_audit_report must not show must-fix count in summary table when confidence < 75."""
    findings = [
        {
            "id": "AUD-1",
            "file_path": "backend/app/auth.py",
            "line_start": 10,
            "line_end": 10,
            "severity": "BLOCKER",
            "category": "security",
            "title": "Degraded blocker finding",
            "confidence": 95,
            "priority_bucket": "must_fix",
        }
    ]

    report = format_audit_report(
        pr_summary="Degraded audit summary",
        findings=findings,
        confidence=74,
        must_fix_count=1,  # Stale must_fix_count passed by caller
    )

    assert "Must-Fix: <code>0</code>" in report
    assert "Should-Fix: <code>0</code>" in report
    assert "💡 **Suggestion:** [Degraded blocker finding]" in report
    assert "⛔ **Must-Fix:**" not in report


def test_format_audit_report_posted_counts_validation():
    """posted_counts rejects invalid string formats but accepts valid badge strings or mappings."""
    # Valid mapping
    rep_map = format_audit_report(
        pr_summary="Test",
        findings=[],
        confidence=90,
        posted_counts={"C": 2, "H": 3},
    )
    assert "· C2 H3*" in rep_map

    # Valid string
    rep_str_valid = format_audit_report(
        pr_summary="Test",
        findings=[],
        confidence=90,
        posted_counts="C1 H2 M1",
    )
    assert "· C1 H2 M1*" in rep_str_valid

    # Malicious or invalid string is sanitized/ignored
    rep_str_invalid = format_audit_report(
        pr_summary="Test",
        findings=[],
        confidence=90,
        posted_counts="<script>alert(1)</script>",
    )
    assert "<script>" not in rep_str_invalid
    assert "*Generated autonomously by Haunter Guardian Mode. Zero changes were committed to your branch.*" in rep_str_invalid


def test_audit_matcher_to_finding_dict_preserves_priority_bucket():
    from app.subagents.audit_matcher import _to_finding_dict

    f = _make_audit_finding(priority_bucket="must_fix")
    d = _to_finding_dict(f)
    assert d.get("priority_bucket") == "must_fix"

    m = {"file_path": "a.py", "line_start": 1, "priority_bucket": "should_fix"}
    dm = _to_finding_dict(m)
    assert dm.get("priority_bucket") == "should_fix"


@pytest.mark.asyncio
async def test_publish_audit_review_preserves_priority_bucket_in_delta_path(monkeypatch, tmp_path):
    """Publishing delta new_findings mappings preserves their priority_bucket when converting to AuditFinding."""
    new_findings_mappings = [
        {
            "id": "AUD-NEW-1",
            "file_path": "backend/app/auth.py",
            "line_start": 84,
            "line_end": 84,
            "perspective": "security",
            "severity": "WARNING",  # Normally would map to should-fix
            "category": "security",
            "title": "Promoted must-fix finding",
            "description": "Crucial security finding explicitly marked must_fix",
            "confidence": 90,
            "priority_bucket": "must_fix",
        }
    ]

    res = AuditResult(
        audit_id="audit-delta-1234",
        audit_type="pr_audit",
        repo_full_name="org/repo",
        target_label="PR #100",
        engine="test",
        executive_summary="Summary",
        findings=[],
        confidence=90,
        publish_allowed=True,
    )

    async def fake_update_pr_review(*args, **kwargs):
        return {"id": 99}

    async def fake_create_pr_review(*args, **kwargs):
        return {"id": 99, "body": kwargs.get("body", "")}

    monkeypatch.setattr("app.github_client.create_pr_review", fake_create_pr_review)
    monkeypatch.setattr("app.github_client.update_pr_review", fake_update_pr_review)

    await publish_audit_review(
        result=res,
        owner="org",
        repo="repo",
        pr_number=100,
        head_sha="abcdef1234567890",
        review_id=99,
        new_findings=new_findings_mappings,
        active_findings=new_findings_mappings,
        dossier_dir=str(tmp_path),
    )

    import json
    meta_path = tmp_path / "meta.json"
    assert meta_path.exists()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert len(meta["active_findings"]) == 1
    assert meta["active_findings"][0]["priority_bucket"] == "must_fix"

    # Also test comment body formatter with converted finding
    converted = AuditFinding(
        id=new_findings_mappings[0]["id"],
        file_path=new_findings_mappings[0]["file_path"],
        line_start=new_findings_mappings[0]["line_start"],
        line_end=new_findings_mappings[0]["line_end"],
        perspective=new_findings_mappings[0]["perspective"],
        severity=new_findings_mappings[0]["severity"],
        category=new_findings_mappings[0]["category"],
        title=new_findings_mappings[0]["title"],
        description=new_findings_mappings[0]["description"],
        suggested_fix=None,
        confidence=new_findings_mappings[0]["confidence"],
        priority_bucket=new_findings_mappings[0]["priority_bucket"],
    )
    comment_body = format_finding_comment_body(converted)
    assert "### ⛔ Must-Fix: Promoted must-fix finding" in comment_body
