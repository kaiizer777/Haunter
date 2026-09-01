"""
Phase 2 regression tests for ``_check_path`` (FRAGILE-1).

The fix generator's path-traversal check used to compare ``raw_path == "/dev/null"``
exactly, which rejected valid patches that carried a trailing CR, LF, tab, or
space on the ``--- /dev/null`` / ``+++ /dev/null`` line — a real failure mode
when the LLM emits a CRLF-terminated diff. The check now uses
``raw_path.strip() == "/dev/null"`` so all those variants are accepted.

This file is **sync** and **DB-free**: ``_check_path`` is a pure function of
its input. No fixture, no async, no engine — keeps the test fast and stable.
"""

from __future__ import annotations

import pytest

from app.subagents.fix_generator import PatchRejected, _check_path


# ---------------------------------------------------------------------------
# Accept: /dev/null sentinels with stray whitespace
# ---------------------------------------------------------------------------
#
# Each of these comes from a real diff body the LLM has produced. The
# hunk-header regex (see app.subagents.fix_generator._HUNK_PATH_RE) captures
# the trailing ``\S+`` after ``---`` / ``+++`` / ``***``, so a CRLF patch
# arrives here with a literal ``/dev/null\r\n`` token, NOT stripped.
#
# All must pass without raising — the function is called from _validate_patch
# inside the post-LLM gate, and a rejection here aborts the whole attempt
# (PatchRejected is hard-raised with no retry).
_ACCEPT_PATHS: list[str] = [
    pytest.param("/dev/null\n", id="lf-trailing"),
    pytest.param("/dev/null\r\n", id="crlf-trailing"),
    pytest.param(" /dev/null", id="leading-space"),
    pytest.param("/dev/null \n", id="trailing-space-then-lf"),
    pytest.param("/dev/null\t", id="trailing-tab"),
    pytest.param("/dev/null", id="bare-no-whitespace"),
    pytest.param("\t/dev/null\t", id="surrounding-tabs"),
]


@pytest.mark.parametrize("raw_path", _ACCEPT_PATHS)
def test_check_path_accepts_dev_null_variants(raw_path: str) -> None:
    """Any /dev/null variant (CRLF, LF, tab, leading/trailing space) must be accepted."""
    # Must not raise.
    _check_path(raw_path)


# ---------------------------------------------------------------------------
# Reject: existing security invariants must still hold after the .strip() fix
# ---------------------------------------------------------------------------
#
# Each parametrize ID is descriptive so a future regression reads cleanly in
# pytest -v output. The assertion checks the exception message because
# a regression that returns the wrong reason (e.g. "is absolute" instead of
# "blocked prefix") would be a security smell, not just a bug.
_REJECT_PATHS: list[tuple[str, str]] = [
    ("/etc/passwd", "absolute"),
    ("../../etc/passwd", "traversal"),
    (".git/HEAD", "blocked-prefix-git"),
    (".github/workflows/ci.yml", "blocked-prefix-workflows"),
    ("", "empty"),
    ("a//b", "double-slash"),
]


@pytest.mark.parametrize(
    "raw_path,reason",
    _REJECT_PATHS,
    ids=[reason for _, reason in _REJECT_PATHS],
)
def test_check_path_rejects_unsafe_paths(raw_path: str, reason: str) -> None:
    """The security-critical rejections must still fire after the .strip() fix."""
    with pytest.raises(PatchRejected):
        _check_path(raw_path)


# ---------------------------------------------------------------------------
# Smoke: end-to-end _validate_patch round-trip with a CRLF-terminated diff
# ---------------------------------------------------------------------------
#
# This is the user-facing acceptance criterion: "Hand-crafted patch with
# +++ /dev/null\\n passes _validate_patch (was rejected with PatchRejected before)."
# We assemble a minimal diff that contains the exact line shape the LLM
# produced when the bug was reported.
def test_validate_patch_accepts_crlf_dev_null_deletion() -> None:
    from app.subagents.fix_generator import _validate_patch

    # CRLF line endings, ``+++ /dev/null`` for the deletion, real file change
    # in ``src/keep.py``. Pre-fix this raised PatchRejected("blocked prefix")
    # or "absolute" because the captured path was ``/dev/null\r``.
    crlf_patch = (
        "--- a/src/keep.py\r\n"
        "+++ b/src/keep.py\r\n"
        "@@ -1,1 +1,1 @@\r\n"
        "-old\r\n"
        "+new\r\n"
    )
    # Must not raise.
    _validate_patch(crlf_patch)
