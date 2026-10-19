"""Unit tests for strict unified-diff validation (issue #61).

Every remediation patch is model-authored and may be auto-applied to a user's
branch, so the validator must reject prose, patch envelopes, truncation,
traversal and any other body that the applier could not apply exactly.
"""

from __future__ import annotations

import pytest

from app.diff_validation import (
    InvalidUnifiedDiffError,
    coerce_unified_diff,
    is_valid_unified_diff,
    validate_unified_diff,
)

MODIFY_DIFF = (
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,2 +1,2 @@\n"
    " keep\n"
    "-old\n"
    "+new\n"
)

CREATE_DIFF = (
    "--- /dev/null\n"
    "+++ b/new.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+one\n"
    "+two\n"
)

DELETE_DIFF = (
    "--- a/gone.py\n"
    "+++ /dev/null\n"
    "@@ -1,2 +0,0 @@\n"
    "-one\n"
    "-two\n"
)

GIT_DIFF = (
    "diff --git a/app.py b/app.py\n"
    "index abcdef1..abcdef2 100644\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1 +1 @@ def add(a, b):\n"
    "-    return a - b\n"
    "+    return a + b\n"
)


@pytest.mark.parametrize(
    "patch",
    [
        pytest.param(MODIFY_DIFF, id="modify"),
        pytest.param(CREATE_DIFF, id="create-file"),
        pytest.param(DELETE_DIFF, id="delete-file"),
        pytest.param(GIT_DIFF, id="git-metadata-and-section-heading"),
        pytest.param(MODIFY_DIFF + CREATE_DIFF, id="multiple-file-sections"),
        pytest.param(
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-old\n"
            "+new\n"
            "\\ No newline at end of file\n",
            id="no-newline-marker",
        ),
        pytest.param(
            "diff --git a/new.py b/new.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/new.py\n"
            "@@ -0,0 +1,1 @@\n"
            "+created\n",
            id="git-new-file",
        ),
    ],
)
def test_valid_diffs_are_accepted(patch: str) -> None:
    validate_unified_diff(patch)
    assert is_valid_unified_diff(patch) is True


@pytest.mark.parametrize(
    "patch",
    [
        pytest.param("", id="empty"),
        pytest.param("   \n\n", id="whitespace-only"),
        pytest.param(
            "Run cd backend && python scripts/check_test_count.py --write",
            id="prose",
        ),
        pytest.param(
            "(no automated remediation suggested; see findings above)",
            id="sentinel",
        ),
        pytest.param(
            "*** Begin Patch\n"
            "*** Update File: app.py\n"
            "*** End Patch\n",
            id="begin-patch-envelope",
        ),
        pytest.param(
            MODIFY_DIFF + "*** Update File: other.py\n",
            id="embedded-envelope-marker",
        ),
        pytest.param(
            "--- a/app.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-old\n"
            "+new\n",
            id="missing-new-header",
        ),
        pytest.param(
            "+++ b/app.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-old\n"
            "+new\n",
            id="new-header-without-old",
        ),
        pytest.param(
            "@@ -1,1 +1,1 @@\n"
            "-old\n"
            "+new\n",
            id="hunk-without-file-headers",
        ),
        pytest.param(
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-old\n"
            "+new\n",
            id="truncated-hunk",
        ),
        pytest.param(
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-old\n"
            "+new\n"
            "+surplus\n",
            id="hunk-body-overruns-header",
        ),
        pytest.param(
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,1 +1,1 @@\n"
            " context\n"
            " surplus context\n",
            id="surplus-context-line",
        ),
        pytest.param(
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-old\n"
            "--- a/other.py\n"
            "+++ b/other.py\n"
            "+new\n",
            id="file-header-inside-open-hunk",
        ),
        pytest.param(
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-old\n"
            "+new\n"
            "\n"
            "trailing prose\n",
            id="trailing-prose",
        ),
        pytest.param(
            "--- a/../etc/passwd\n"
            "+++ b/../etc/passwd\n"
            "@@ -1,1 +1,1 @@\n"
            "-root\n"
            "+toor\n",
            id="path-traversal",
        ),
        pytest.param(
            "--- a//etc/passwd\n"
            "+++ b//etc/passwd\n"
            "@@ -1,1 +1,1 @@\n"
            "-root\n"
            "+toor\n",
            id="empty-path-segment",
        ),
        pytest.param(
            "--- a/.git/config\n"
            "+++ b/.git/config\n"
            "@@ -1,1 +1,1 @@\n"
            "-a\n"
            "+b\n",
            id="dot-git-path",
        ),
        pytest.param(
            "--- a/.github/workflows/ci.yml\n"
            "+++ b/.github/workflows/ci.yml\n"
            "@@ -1,1 +1,1 @@\n"
            "-a\n"
            "+b\n",
            id="github-workflow-path",
        ),
        pytest.param(
            "--- /dev/null\n"
            "+++ /dev/null\n"
            "@@ -0,0 +0,0 @@\n",
            id="no-old-or-new-path",
        ),
        pytest.param(
            "--- a/app.py\n"
            "+++ b/app.py\n",
            id="headers-without-hunks",
        ),
        pytest.param(
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,1 +1,1 @@\n"
            "\n"
            "-old\n"
            "+new\n",
            id="blank-line-inside-hunk",
        ),
    ],
)
def test_invalid_diffs_are_rejected(patch: str) -> None:
    with pytest.raises(InvalidUnifiedDiffError):
        validate_unified_diff(patch)
    assert is_valid_unified_diff(patch) is False


def test_coerce_unwraps_one_outer_fence() -> None:
    fenced = f"```diff\n{MODIFY_DIFF}```"

    assert coerce_unified_diff(fenced) == MODIFY_DIFF


def test_coerce_completes_bare_hunk_with_file_path() -> None:
    bare_hunk = "@@ -1,1 +1,1 @@\n-old\n+new\n"

    coerced = coerce_unified_diff(bare_hunk, file_path="backend/app.py")

    assert coerced == (
        "--- a/backend/app.py\n"
        "+++ b/backend/app.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
    )


def test_coerce_rejects_bare_hunk_without_file_path() -> None:
    assert coerce_unified_diff("@@ -1,1 +1,1 @@\n-old\n+new\n") is None


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Run cd backend && python scripts/check_test_count.py", id="prose"),
        pytest.param("*** Begin Patch\n*** End Patch", id="envelope"),
        pytest.param("--- a/app.py\n+++ b/app.py\n", id="headers-only"),
    ],
)
def test_coerce_never_synthesizes_a_hunk_from_text(text: str) -> None:
    assert coerce_unified_diff(text, file_path="backend/app.py") is None


def test_coerce_does_not_double_prefix_a_complete_diff() -> None:
    assert coerce_unified_diff(MODIFY_DIFF, file_path="app.py") == MODIFY_DIFF
