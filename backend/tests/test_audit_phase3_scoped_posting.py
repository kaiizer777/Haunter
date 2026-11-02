"""Unit tests for Phase 3: Scoped posting policy and canonical severity tracking.

Covers:
- Canonical severity normalization (CRITICAL/BLOCKER -> C, HIGH -> H, MEDIUM/WARNING -> M,
  LOW/NOTE -> L, IMPROVEMENT/INFORMATIONAL/INFO or informational_only=True -> I)
- Intra-bucket sorting: confidence descending, then line_start ascending
- Scoped posting algorithm (select_scoped_findings):
  * Fewer than 8 findings (all posted, e.g. "C1 H2 M2 L1")
  * Filling to 8 via priority order M -> L -> I
  * C + H == 8 (exact cap, 0 M posted, "C4 H4")
  * C + H > 8 (overflow exception, all C+H posted, 0 M/L/I posted, "C6 H4")
- Footer badge formatting: zero-count severities omitted, strict C H M L I order
- Report footer formatting and swap regex with both posted counts and resolved counts
- Integration with publish_audit_review & dossier meta persistence
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.github.audit_publisher import (
    load_dossier_meta,
    publish_audit_review,
    update_dossier_meta,
)
from app.llm.prompts.audit_prompts import (
    format_audit_report,
    format_posted_counts_badge,
    swap_report_footer,
)
from app.subagents.auditor import (
    AuditFinding,
    AuditResult,
    CANONICAL_SEVERITY_MAP,
    normalize_severity,
    select_scoped_findings,
)


def _make_finding(
    *,
    id: str = "AUD-1",
    severity: str = "WARNING",
    confidence: int = 90,
    line_start: int = 10,
    line_end: int = 15,
    informational_only: bool = False,
    file_path: str = "app/main.py",
) -> AuditFinding:
    return AuditFinding(
        id=id,
        file_path=file_path,
        line_start=line_start,
        line_end=line_end,
        perspective="security",
        severity=severity,  # type: ignore[arg-type]
        category="security/test",
        title=f"Finding {id}",
        description=f"Description for {id}",
        suggested_fix=None,
        confidence=confidence,
        informational_only=informational_only,
    )


# ---------------------------------------------------------------------------
# 1. Canonical Severity Normalization Tests
# ---------------------------------------------------------------------------


def test_normalize_severity_string_and_enums():
    # Critical / Blocker -> C
    assert normalize_severity("CRITICAL") == "C"
    assert normalize_severity("BLOCKER") == "C"
    assert normalize_severity("critical") == "C"
    assert normalize_severity("C") == "C"

    # High -> H
    assert normalize_severity("HIGH") == "H"
    assert normalize_severity("high") == "H"
    assert normalize_severity("H") == "H"

    # Medium / Warning -> M
    assert normalize_severity("MEDIUM") == "M"
    assert normalize_severity("WARNING") == "M"
    assert normalize_severity("warning") == "M"
    assert normalize_severity("M") == "M"

    # Low / Note -> L
    assert normalize_severity("LOW") == "L"
    assert normalize_severity("NOTE") == "L"
    assert normalize_severity("note") == "L"
    assert normalize_severity("L") == "L"

    # Improvement / Informational / Info -> I
    assert normalize_severity("IMPROVEMENT") == "I"
    assert normalize_severity("INFORMATIONAL") == "I"
    assert normalize_severity("INFO") == "I"
    assert normalize_severity("info") == "I"
    assert normalize_severity("I") == "I"


def test_normalize_severity_informational_only_override():
    # informational_only=True overrides any severity to I
    assert normalize_severity("BLOCKER", informational_only=True) == "I"
    assert normalize_severity("CRITICAL", informational_only=True) == "I"
    assert normalize_severity("HIGH", informational_only=True) == "I"

    # From finding object
    f_info = _make_finding(severity="BLOCKER", informational_only=True)
    assert normalize_severity(f_info) == "I"

    # From dict
    d_info = {"severity": "HIGH", "informational_only": True}
    assert normalize_severity(d_info) == "I"


def test_normalize_severity_from_audit_finding_object():
    f_crit = _make_finding(severity="BLOCKER")
    f_warn = _make_finding(severity="WARNING")
    f_note = _make_finding(severity="NOTE")

    assert normalize_severity(f_crit) == "C"
    assert normalize_severity(f_warn) == "M"
    assert normalize_severity(f_note) == "L"


# ---------------------------------------------------------------------------
# 2. Footer Badge Formatting Tests
# ---------------------------------------------------------------------------


def test_format_posted_counts_badge_zero_count_severities_omitted():
    # Only non-zero counts appear, in canonical C H M L I order
    badge = format_posted_counts_badge({"C": 3, "H": 5, "M": 0, "L": 0, "I": 0})
    assert badge == "C3 H5"

    badge2 = format_posted_counts_badge({"C": 1, "H": 2, "M": 5})
    assert badge2 == "C1 H2 M5"

    badge3 = format_posted_counts_badge({"C": 1, "M": 1})
    assert badge3 == "C1 M1"

    # All zeros or empty
    assert format_posted_counts_badge({}) == ""
    assert format_posted_counts_badge({"C": 0, "H": 0, "M": 0, "L": 0, "I": 0}) == ""


def test_format_posted_counts_badge_preserves_strict_severity_ordering():
    # Even if keys in dict are reversed, output is strictly C H M L I
    shuffled = {"I": 4, "L": 3, "M": 2, "H": 1, "C": 5}
    assert format_posted_counts_badge(shuffled) == "C5 H1 M2 L3 I4"


# ---------------------------------------------------------------------------
# 3. Scoped Posting Algorithm (select_scoped_findings) Tests
# ---------------------------------------------------------------------------


def test_scoped_posting_fewer_than_8_findings():
    # 1 C, 2 H, 2 M, 1 L (total 6 < 8): all 6 posted
    findings = [
        _make_finding(id="C1", severity="BLOCKER", confidence=95, line_start=10),
        _make_finding(id="H1", severity="HIGH", confidence=90, line_start=20),
        _make_finding(id="H2", severity="HIGH", confidence=85, line_start=30),
        _make_finding(id="M1", severity="WARNING", confidence=80, line_start=40),
        _make_finding(id="M2", severity="WARNING", confidence=75, line_start=50),
        _make_finding(id="L1", severity="NOTE", confidence=70, line_start=60),
    ]
    posted, counts = select_scoped_findings(findings, limit=8)
    assert len(posted) == 6
    assert counts == {"C": 1, "H": 2, "M": 2, "L": 1, "I": 0}
    assert format_posted_counts_badge(counts) == "C1 H2 M2 L1"


def test_scoped_posting_filling_to_8_via_priority_m_then_l_then_i():
    # 1 C, 1 H, 4 M, 4 L, 3 I (total 13)
    # Target: 1 C + 1 H = 2; fill 6 slots: 4 M, then 2 L (0 I). Total = 8.
    findings = [
        _make_finding(id="C1", severity="BLOCKER", confidence=90, line_start=1),
        _make_finding(id="H1", severity="HIGH", confidence=90, line_start=2),
        _make_finding(id="M1", severity="WARNING", confidence=85, line_start=3),
        _make_finding(id="M2", severity="WARNING", confidence=80, line_start=4),
        _make_finding(id="M3", severity="WARNING", confidence=75, line_start=5),
        _make_finding(id="M4", severity="WARNING", confidence=70, line_start=6),
        _make_finding(id="L1", severity="NOTE", confidence=90, line_start=7),
        _make_finding(id="L2", severity="NOTE", confidence=85, line_start=8),
        _make_finding(id="L3", severity="NOTE", confidence=80, line_start=9),
        _make_finding(id="L4", severity="NOTE", confidence=75, line_start=10),
        _make_finding(id="I1", severity="INFO", confidence=99, line_start=11),
        _make_finding(id="I2", severity="INFO", confidence=95, line_start=12),
        _make_finding(id="I3", severity="INFO", confidence=90, line_start=13),
    ]
    posted, counts = select_scoped_findings(findings, limit=8)
    assert len(posted) == 8
    assert counts == {"C": 1, "H": 1, "M": 4, "L": 2, "I": 0}
    assert format_posted_counts_badge(counts) == "C1 H1 M4 L2"

    posted_ids = [f.id for f in posted]
    assert "C1" in posted_ids
    assert "H1" in posted_ids
    assert {"M1", "M2", "M3", "M4"}.issubset(set(posted_ids))
    # Higher confidence L1 and L2 must be taken before L3, L4
    assert "L1" in posted_ids
    assert "L2" in posted_ids
    assert "L3" not in posted_ids
    assert "I1" not in posted_ids


def test_scoped_posting_exact_cap_c_plus_h_equals_8():
    # 4 C, 4 H, 3 M (total 11)
    # len(C) + len(H) == 8 == limit: post ONLY all 4 C and 4 H; 0 M posted
    findings = [
        *[_make_finding(id=f"C{i}", severity="BLOCKER", confidence=90, line_start=i) for i in range(1, 5)],
        *[_make_finding(id=f"H{i}", severity="HIGH", confidence=85, line_start=i + 10) for i in range(1, 5)],
        *[_make_finding(id=f"M{i}", severity="WARNING", confidence=80, line_start=i + 20) for i in range(1, 4)],
    ]
    posted, counts = select_scoped_findings(findings, limit=8)
    assert len(posted) == 8
    assert counts == {"C": 4, "H": 4, "M": 0, "L": 0, "I": 0}
    assert format_posted_counts_badge(counts) == "C4 H4"
    posted_ids = [f.id for f in posted]
    assert all(f"C{i}" in posted_ids for i in range(1, 5))
    assert all(f"H{i}" in posted_ids for i in range(1, 5))
    assert not any(f.id.startswith("M") for f in posted)


def test_scoped_posting_overflow_c_plus_h_exceeds_limit():
    # 6 C, 4 H, 5 M, 2 L (total 17)
    # len(C) + len(H) = 10 > limit (8): overflow exception -> all 6 C + 4 H posted, 0 M/L/I
    findings = [
        *[_make_finding(id=f"C{i}", severity="BLOCKER", confidence=95, line_start=i) for i in range(1, 7)],
        *[_make_finding(id=f"H{i}", severity="HIGH", confidence=85, line_start=i + 10) for i in range(1, 5)],
        *[_make_finding(id=f"M{i}", severity="WARNING", confidence=90, line_start=i + 20) for i in range(1, 6)],
        *[_make_finding(id=f"L{i}", severity="NOTE", confidence=80, line_start=i + 30) for i in range(1, 3)],
    ]
    posted, counts = select_scoped_findings(findings, limit=8)
    assert len(posted) == 10
    assert counts == {"C": 6, "H": 4, "M": 0, "L": 0, "I": 0}
    assert format_posted_counts_badge(counts) == "C6 H4"
    posted_ids = [f.id for f in posted]
    assert all(f"C{i}" in posted_ids for i in range(1, 7))
    assert all(f"H{i}" in posted_ids for i in range(1, 5))
    assert not any(f.id.startswith("M") or f.id.startswith("L") for f in posted)


def test_scoped_posting_intra_bucket_sorting():
    # Within a bucket, sort by confidence desc, then line_start asc
    findings = [
        _make_finding(id="M-conf80-line10", severity="WARNING", confidence=80, line_start=10),
        _make_finding(id="M-conf95-line50", severity="WARNING", confidence=95, line_start=50),
        _make_finding(id="M-conf95-line20", severity="WARNING", confidence=95, line_start=20),
        _make_finding(id="M-conf70-line5", severity="WARNING", confidence=70, line_start=5),
    ]
    posted, counts = select_scoped_findings(findings, limit=4)
    ordered_ids = [f.id for f in posted]
    assert ordered_ids == [
        "M-conf95-line20",  # conf 95, line 20 < line 50
        "M-conf95-line50",  # conf 95, line 50
        "M-conf80-line10",  # conf 80
        "M-conf70-line5",   # conf 70
    ]


def test_scoped_posting_with_dict_findings():
    # Also functions seamlessly with dict finding projections
    dict_findings = [
        {"severity": "BLOCKER", "confidence": 90, "line_start": 5, "id": "C1"},
        {"severity": "HIGH", "confidence": 85, "line_start": 10, "id": "H1"},
        {"severity": "WARNING", "confidence": 80, "line_start": 15, "id": "M1"},
    ]
    posted, counts = select_scoped_findings(dict_findings, limit=8)
    assert len(posted) == 3
    assert counts == {"C": 1, "H": 1, "M": 1, "L": 0, "I": 0}
    assert format_posted_counts_badge(counts) == "C1 H1 M1"


# ---------------------------------------------------------------------------
# 4. Report Footer Formatting and Swap Tests
# ---------------------------------------------------------------------------


def test_format_audit_report_with_posted_counts():
    report = format_audit_report(
        pr_summary="Test PR",
        confidence=90,
        posted_counts={"C": 3, "H": 5},
    )
    expected_footer = "*Generated autonomously by Haunter Guardian Mode. Zero changes were committed to your branch. · C3 H5*"
    assert expected_footer in report


def test_format_audit_report_with_posted_counts_and_resolved_count():
    report = format_audit_report(
        pr_summary="Test PR",
        confidence=90,
        posted_counts={"C": 2, "H": 4, "M": 1},
        resolved_since_last_review=3,
    )
    expected_footer = "*Generated autonomously by Haunter Guardian Mode. Zero changes were committed to your branch. · C2 H4 M1 · resolved since last review: 3*"
    assert expected_footer in report


def test_swap_report_footer_with_both_posted_counts_and_resolved_counts():
    initial_report = format_audit_report(
        pr_summary="Initial summary",
        confidence=90,
    )
    # Starts with base footer
    assert "*Generated autonomously by Haunter Guardian Mode. Zero changes were committed to your branch.*" in initial_report

    # Step 1: swap in posted counts badge
    with_badge = swap_report_footer(initial_report, posted_counts={"C": 3, "H": 5})
    assert "*Generated autonomously by Haunter Guardian Mode. Zero changes were committed to your branch. · C3 H5*" in with_badge

    # Step 2: on re-review, add resolved count while preserving posted counts badge
    with_both = swap_report_footer(with_badge, resolved_since_last_review=4)
    assert "*Generated autonomously by Haunter Guardian Mode. Zero changes were committed to your branch. · C3 H5 · resolved since last review: 4*" in with_both

    # Step 3: update both posted counts and resolved count simultaneously
    updated_both = swap_report_footer(with_both, resolved_since_last_review=5, posted_counts={"C": 2, "H": 3})
    assert "*Generated autonomously by Haunter Guardian Mode. Zero changes were committed to your branch. · C2 H3 · resolved since last review: 5*" in updated_both
    assert "C3 H5" not in updated_both


def test_swap_report_footer_no_prior_footer_appends_cleanly():
    raw_markdown = "# PR Review\n\nSome findings."
    swapped = swap_report_footer(raw_markdown, resolved_since_last_review=2, posted_counts={"C": 1, "M": 2})
    assert "*Generated autonomously by Haunter Guardian Mode. Zero changes were committed to your branch. · C1 M2 · resolved since last review: 2*" in swapped


# ---------------------------------------------------------------------------
# 5. Publisher Integration and Dossier Meta Persistence Tests
# ---------------------------------------------------------------------------


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


@pytest.mark.asyncio
async def test_publish_audit_review_applies_scoped_posting_and_updates_meta(tmp_path):
    # 12 findings: 2 C, 2 H, 4 M, 4 L
    # Scoped policy should post 8: 2 C, 2 H, 4 M (0 L).
    findings = [
        _make_finding(id="C1", severity="BLOCKER", confidence=90, file_path="backend/app/auth.py", line_start=80, line_end=80),
        _make_finding(id="C2", severity="BLOCKER", confidence=90, file_path="backend/app/auth.py", line_start=81, line_end=81),
        _make_finding(id="H1", severity="HIGH", confidence=85, file_path="backend/app/auth.py", line_start=82, line_end=82),
        _make_finding(id="H2", severity="HIGH", confidence=85, file_path="backend/app/auth.py", line_start=83, line_end=83),
        _make_finding(id="M1", severity="WARNING", confidence=80, file_path="backend/app/auth.py", line_start=84, line_end=84),
        _make_finding(id="M2", severity="WARNING", confidence=80, file_path="backend/app/auth.py", line_start=85, line_end=85),
        _make_finding(id="M3", severity="WARNING", confidence=80, file_path="backend/app/auth.py", line_start=86, line_end=86),
        _make_finding(id="M4", severity="WARNING", confidence=80, file_path="backend/app/auth.py", line_start=87, line_end=87),
        _make_finding(id="L1", severity="NOTE", confidence=75, file_path="backend/app/auth.py", line_start=88, line_end=88),
        _make_finding(id="L2", severity="NOTE", confidence=75, file_path="backend/app/routers/tokens.py", line_start=28, line_end=28),
        _make_finding(id="L3", severity="NOTE", confidence=75, file_path="backend/app/routers/tokens.py", line_start=29, line_end=29),
        _make_finding(id="L4", severity="NOTE", confidence=75, file_path="backend/app/routers/tokens.py", line_start=30, line_end=30),
    ]

    result = AuditResult(
        audit_id="audit-phase3-1",
        audit_type="full",
        repo_full_name="octocat/hello-world",
        target_label="PR #42",
        engine="haunter-v1",
        executive_summary="Summary of audit",
        findings=findings,
        confidence=90,
        status="⚠️ Merge Blocked (Changes Needed)",
        remediation_diff="",
        publish_allowed=True,
        report_markdown="# Audit Report\n\n*Generated autonomously by Haunter Guardian Mode. Zero changes were committed to your branch.*",
    )

    mock_client = AsyncMock()
    mock_client.create_pr_review.return_value = {"id": 9901}

    dossier_dir = tmp_path / "dossier"

    with patch("app.github.audit_publisher.github_client.create_pr_review", mock_client.create_pr_review):
        outcome = await publish_audit_review(
            result=result,
            owner="octocat",
            repo="hello-world",
            pr_number=42,
            head_sha="f" * 40,
            diff_text=SAMPLE_DIFF,
            token="ghs_test",
            dossier_dir=str(dossier_dir),
        )

    assert outcome.published is True
    # Scoped limit = 8 inline comments posted
    assert outcome.comments_count == 8
    # 12 total findings - 8 posted = 4 suppressed findings
    assert outcome.suppressed_findings_count == 4
    assert outcome.posted_counts == {"C": 2, "H": 2, "M": 4, "L": 0, "I": 0}

    # Review body posted to GitHub must contain the scoped badge in footer
    review_call_args = mock_client.create_pr_review.call_args.kwargs
    posted_body = review_call_args["body"]
    assert "· C2 H2 M4*" in posted_body

    # Unposted findings (all 12 findings) remain preserved in dossier meta.json
    meta = load_dossier_meta(owner="octocat", repo="hello-world", pr_number=42, dossier_dir=str(dossier_dir))
    assert meta is not None
    assert len(meta["active_findings"]) == 12
    assert meta["posted_counts"] == {"C": 2, "H": 2, "M": 4, "L": 0, "I": 0}
