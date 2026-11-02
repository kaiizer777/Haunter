"""Comprehensive fast unit tests for Auditor Phase 5 infrastructure:
1. Hunk-aware chunking per file (~500 lines, no interleaving, oversized hunks intact)
2. Inline suggestions scoping (independent vs multi-file dropping)
3. Deleted-line anchoring (LEFT side, deleted files +++ /dev/null, no ```suggestion on LEFT)
4. Suggested-code display rule (single-file, <=1 hunk, <30 lines; dropped otherwise)
"""

from __future__ import annotations

import pytest

from app.github.audit_publisher import (
    build_inline_review_comments,
    format_inline_comment_body,
    is_cross_file_finding,
    should_show_suggested_code,
    validate_finding_coordinates,
)
from app.subagents.audit_chunking import partition_diff_into_chunks
from app.subagents.auditor import (
    AuditFinding,
    DiffGrounding,
    PerspectiveFinding,
    _is_grounded,
    build_diff_grounding,
)
from app.subagents.code_reviewer import (
    ReviewFinding,
    format_github_suggestion,
    should_show_suggested_patch,
)


# ============================================================================
# 1. Hunk-Aware Chunking Tests
# ============================================================================


def test_chunking_per_file_no_interleaving():
    """Verify that multiple files are never interleaved into the same chunk."""
    diff = (
        "diff --git a/file1.py b/file1.py\n"
        "--- a/file1.py\n"
        "+++ b/file1.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-old1\n"
        "+new1\n"
        "diff --git a/file2.py b/file2.py\n"
        "--- a/file2.py\n"
        "+++ b/file2.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-old2\n"
        "+new2\n"
    )
    chunks = partition_diff_into_chunks(diff, max_chunk_lines=500)
    assert len(chunks) == 2
    assert "file1.py" in chunks[0]
    assert "file2.py" not in chunks[0]
    assert "file2.py" in chunks[1]
    assert "file1.py" not in chunks[1]


def test_chunking_preserves_oversized_hunk_intact():
    """An individual hunk exceeding 500 lines is kept intact in its own chunk with file header."""
    header = "diff --git a/massive.py b/massive.py\n--- a/massive.py\n+++ b/massive.py\n"
    hunk = "@@ -1,600 +1,600 @@\n" + "\n".join(f"+line_{i}" for i in range(600)) + "\n"
    diff = header + hunk

    chunks = partition_diff_into_chunks(diff, max_chunk_lines=500)
    assert len(chunks) == 1
    assert "diff --git a/massive.py b/massive.py" in chunks[0]
    assert "@@ -1,600 +1,600 @@" in chunks[0]
    assert len(chunks[0].splitlines()) > 600


def test_chunking_multi_hunk_within_file_splits_at_hunk_boundary():
    """A file with multiple hunks splits at hunk boundaries when approaching 500 lines."""
    header = "diff --git a/multi.py b/multi.py\n--- a/multi.py\n+++ b/multi.py\n"
    hunk1 = "@@ -1,250 +1,250 @@\n" + "\n".join(f"+h1_{i}" for i in range(250)) + "\n"
    hunk2 = "@@ -300,200 +300,200 @@\n" + "\n".join(f"+h2_{i}" for i in range(200)) + "\n"
    hunk3 = "@@ -600,200 +600,200 @@\n" + "\n".join(f"+h3_{i}" for i in range(200)) + "\n"
    diff = header + hunk1 + hunk2 + hunk3

    chunks = partition_diff_into_chunks(diff, max_chunk_lines=500)
    assert len(chunks) == 2
    # Chunk 1 has hunk 1 and hunk 2
    assert "h1_0" in chunks[0]
    assert "h2_0" in chunks[0]
    assert "h3_0" not in chunks[0]
    # Chunk 2 has hunk 3 and file header
    assert "diff --git a/multi.py b/multi.py" in chunks[1]
    assert "h3_0" in chunks[1]


# ============================================================================
# 2. Inline Suggestions Scoping Tests
# ============================================================================


def test_independent_single_file_suggestion_posted_inline():
    diff = (
        "diff --git a/process.py b/process.py\n"
        "--- a/process.py\n"
        "+++ b/process.py\n"
        "@@ -10,2 +10,3 @@\n"
        " def check():\n"
        "+    value = get_value()\n"
        "     return bool(value)\n"
    )
    finding = AuditFinding(
        id="AUD-LOCAL-001",
        file_path="process.py",
        line_start=11,
        line_end=11,
        perspective="security",
        severity="WARNING",
        category="validation",
        title="Input validation missing",
        description="Verify value is not empty",
        suggested_fix="    value = sanitize_input(get_value())",
        confidence=90,
    )
    comments, suppressed = build_inline_review_comments([finding], diff_text=diff)
    assert suppressed == 0
    assert len(comments) == 1
    assert comments[0]["path"] == "process.py"
    assert comments[0]["line"] == 11
    assert "```suggestion" in comments[0]["body"]
    assert "sanitize_input" in comments[0]["body"]


def test_multi_file_cross_file_suggestion_dropped_from_inline():
    """Multi-file finding is dropped from inline review comments."""
    diff = (
        "diff --git a/auth.py b/auth.py\n"
        "--- a/auth.py\n"
        "+++ b/auth.py\n"
        "@@ -10,1 +10,2 @@\n"
        " def check():\n"
        "+    token = get_token()\n"
    )
    multi_file_finding = AuditFinding(
        id="AUD-CROSS-001",
        file_path="auth.py",
        line_start=11,
        line_end=11,
        perspective="architecture",
        severity="WARNING",
        category="cross-service",
        title="Contract mismatch with billing",
        description="Requires updating both auth.py and billing.py",
        suggested_fix=(
            "diff --git a/auth.py b/auth.py\n--- a/auth.py\n+++ b/auth.py\n@@ -10,1 +10,1 @@\n+token = x\n"
            "diff --git a/billing.py b/billing.py\n--- a/billing.py\n+++ b/billing.py\n@@ -5,1 +5,1 @@\n+charge(x)\n"
        ),
        confidence=90,
    )
    assert is_cross_file_finding(multi_file_finding) is True

    comments, suppressed = build_inline_review_comments([multi_file_finding], diff_text=diff)
    assert len(comments) == 0
    assert suppressed == 1


# ============================================================================
# 3. Deleted-Line Anchoring (LEFT side) Tests
# ============================================================================


def test_diff_grounding_tracks_left_deleted_lines():
    diff = (
        "diff --git a/utils.py b/utils.py\n"
        "--- a/utils.py\n"
        "+++ b/utils.py\n"
        "@@ -15,4 +15,2 @@\n"
        " def calc():\n"
        "-    x = 1\n"
        "-    y = 2\n"
        "     return True\n"
    )
    grounding = build_diff_grounding(diff)
    assert 16 in grounding.left_line_index["utils.py"]
    assert 17 in grounding.left_line_index["utils.py"]
    assert grounding.left_lines == grounding.left_line_index


def test_deleted_file_dev_null_supported_in_grounding():
    """Deleted file (+++ /dev/null) is tracked in left_line_index and valid_paths."""
    diff = (
        "diff --git a/deprecated.py b/deprecated.py\n"
        "--- a/deprecated.py\n"
        "+++ /dev/null\n"
        "@@ -1,3 +0,0 @@\n"
        "-line1\n"
        "-line2\n"
        "-line3\n"
    )
    grounding = build_diff_grounding(diff)
    assert "deprecated.py" in grounding.valid_paths
    assert grounding.left_line_index["deprecated.py"] == {1, 2, 3}
    assert grounding.line_index.get("deprecated.py") is None


def test_validate_finding_coordinates_anchors_deleted_line_to_left():
    diff = (
        "diff --git a/utils.py b/utils.py\n"
        "--- a/utils.py\n"
        "+++ b/utils.py\n"
        "@@ -10,3 +10,2 @@\n"
        " start\n"
        "-deleted_fn()\n"
        " end\n"
    )
    grounding = build_diff_grounding(diff)
    finding = AuditFinding(
        id="AUD-DEL-001",
        file_path="utils.py",
        line_start=11,
        line_end=11,
        perspective="bug",
        severity="WARNING",
        category="removal",
        title="Removed essential function",
        description="deleted_fn was required by callers",
        suggested_fix=None,
        confidence=90,
    )
    coords = validate_finding_coordinates(finding, grounding)
    assert coords is not None
    assert coords["path"] == "utils.py"
    assert coords["line"] == 11
    assert coords["side"] == "LEFT"


def test_format_inline_comment_body_on_left_side_does_not_emit_suggestion():
    """GitHub rejects ```suggestion on LEFT side comments; must emit ```diff or text instead."""
    finding = AuditFinding(
        id="AUD-DEL-002",
        file_path="utils.py",
        line_start=11,
        line_end=11,
        perspective="bug",
        severity="WARNING",
        category="removal",
        title="Restore deleted check",
        description="Checking permissions is required here",
        suggested_fix="    if not check(): raise PermissionError()",
        confidence=85,
    )
    body = format_inline_comment_body(finding, side="LEFT")
    assert "```suggestion" not in body
    assert "```diff" in body or "Manual remediation" in body
    assert "*⚡ Haunter Auditor Finding ID: `AUD-DEL-002`*" in body


# ============================================================================
# 4. Suggested-Code Display Rule Tests
# ============================================================================


def test_should_show_suggested_code_rules():
    # 1. Simple single-file fix under 30 lines -> True
    finding = AuditFinding(
        id="AUD-1",
        file_path="app.py",
        line_start=1,
        line_end=1,
        perspective="correctness",
        severity="NOTE",
        category="refactor",
        title="x",
        description="y",
        suggested_fix="def fix():\n    return 42",
        confidence=90,
    )
    assert should_show_suggested_code(finding) is True

    # 2. Fix with >= 30 lines -> False (strictly under 30 lines)
    long_fix = "\n".join(f"line_{i} = {i}" for i in range(30))
    assert should_show_suggested_code(finding, suggested_code=long_fix) is False

    # 3. Fix with 29 lines -> True
    valid_fix = "\n".join(f"line_{i} = {i}" for i in range(29))
    assert should_show_suggested_code(finding, suggested_code=valid_fix) is True

    # 4. Complex multi-hunk fix (>1 hunk header @@) -> False
    multi_hunk_fix = (
        "@@ -1,2 +1,2 @@\n-a\n+b\n"
        "@@ -20,2 +20,2 @@\n-c\n+d\n"
    )
    assert should_show_suggested_code(finding, suggested_code=multi_hunk_fix) is False

    # 5. Multi-file fix -> False
    multi_file_fix = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n+1\n"
        "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1,1 +1,1 @@\n+2\n"
    )
    assert should_show_suggested_code(finding, suggested_code=multi_file_fix) is False


def test_code_reviewer_drops_oversized_suggested_patch():
    """In code_reviewer.py, suggested_patch >= 30 lines drops the ```suggestion block."""
    long_patch = "\n".join(f"    val_{i} = {i}" for i in range(32))
    rf = ReviewFinding(
        file_path="service.py",
        line_start=10,
        line_end=15,
        category="logic",
        severity="high",
        critique="The loop needs to be completely rewritten for streaming.",
        suggested_patch=long_patch,
    )
    assert should_show_suggested_patch(rf, long_patch) is False

    formatted = format_github_suggestion(rf)
    assert "```suggestion" not in formatted
    assert "Manual remediation recommended" in formatted
    assert "The loop needs to be completely rewritten for streaming." in formatted
