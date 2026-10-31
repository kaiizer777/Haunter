"""Unit tests for Fixer 2 Backend Orchestrator, Patch Applier, Checkpoints, Sandbox & Redaction.

Session-related cases removed (session router/orchestrator/streamer/tools deleted).
Remaining coverage: 1-byte newline deletion fix in patch_applier.py and mirror.py.
"""

from __future__ import annotations

from app.sandbox.mirror import apply_unified_diff as mirror_apply_unified_diff
from app.services.patch_applier import apply_unified_diff as patch_applier_apply_diff


# ---------------------------------------------------------------------------
# 1. 1-byte newline deletion fix
# ---------------------------------------------------------------------------


def test_patch_applier_full_file_deletion_returns_empty_string() -> None:
    """apply_unified_diff in patch_applier.py must return '' (0 bytes) when diff deletes all lines."""
    original = "line 1\nline 2\nline 3\n"
    diff = (
        "--- a/file.txt\n"
        "+++ /dev/null\n"
        "@@ -1,3 +0,0 @@\n"
        "-line 1\n"
        "-line 2\n"
        "-line 3\n"
    )
    result = patch_applier_apply_diff(original, diff)
    assert result == "", f"Expected empty string for full deletion, got {result!r}"
    assert len(result.encode("utf-8")) == 0


def test_mirror_apply_unified_diff_full_file_deletion_returns_empty_string() -> None:
    """apply_unified_diff in mirror.py must return '' (0 bytes) when diff deletes all lines."""
    original = "single line\n"
    diff = (
        "--- a/single.txt\n"
        "+++ /dev/null\n"
        "@@ -1,1 +0,0 @@\n"
        "-single line\n"
    )
    result = mirror_apply_unified_diff(original, diff)
    assert result == "", f"Expected empty string for full deletion in mirror, got {result!r}"
    assert len(result.encode("utf-8")) == 0
