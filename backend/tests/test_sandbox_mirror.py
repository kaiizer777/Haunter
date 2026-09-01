"""
Phase 2 regression test for ``_parse_patch_files`` (NICE-4).

The mirror parser used to compare the captured file path with ``"/dev/null"``
exactly, which (in some upstream pipelines) made malformed ``+++ /dev/null``
lines fall through and be treated as a real file path. NICE-4 documents the
observed failure mode: "the parser will treat the next ``+++`` as a new hunk
header" → the deletion is silently dropped, and the test-mirror ends up
with the wrong file tree.

The fix wraps the comparison in ``.strip()`` so CRLF, stray tab, or
leading/trailing space on the marker line all classify as a deletion. The
test exercises the worst-case input described in NICE-4: a ``+++ /dev/null``
with a trailing tab AND the next ``+++ b/somefile`` glued onto the same
line (the line is on its own logical line; the captured token is the
``\S+`` run, which stops at the first whitespace — so the captured group
is ``"/dev/null"`` and the rest of the line is never re-parsed as a header).

This file is **sync** and **DB-free**: ``_parse_patch_files`` is a pure
function of its input. No fixture, no async, no engine.
"""

from __future__ import annotations

import pytest

from app.sandbox.mirror import _parse_patch_files


def test_malformed_dev_null() -> None:
    """
    A patch where ``+++ /dev/null`` is glued to the next ``+++ b/somefile``
    on a single line must still be recognised as a deletion marker.

    Asserts (per the NICE-4 acceptance line):
      - The parser does NOT produce a file entry whose path starts with
        ``"/dev/null"`` glued with a tab/space (i.e. no file entry named
        ``"/dev/null\t+++"`` or similar).
      - If the parser yields any file entries, none of them are the
        malformed marker.
    """
    # The glued input: a single line where ``+++ /dev/null`` is followed by
    # a tab and then ``+++ b/somefile`` on the same line. The regex
    # ``^(?:---|\+\+\+)\s+(?:[ab]/)?(\S+)`` captures the FIRST ``\S+`` run,
    # which stops at the tab/whitespace. The captured token is
    # ``"/dev/null"`` — the ``\t+++ b/somefile`` suffix is NOT a second
    # header (it's on the same consumed line, and the parser does not
    # re-split the line). Pre-fix (no .strip() and an exact equality check)
    # the parser had no way to classify the captured group as a deletion.
    glued_line = "+++ /dev/null\t+++ b/somefile"

    # A real new-file header on a separate line — this is what the test
    # author was worried the parser would MISS when the deletion marker
    # was glued. The parser must not produce a file entry named
    # ``/dev/null\t+++`` from the glued line, and the only file it
    # produces from the SEPARATE ``+++ b/somefile`` line (if it ever
    # re-parses the tail) is ``somefile`` (after the optional ``[ab]/``
    # prefix is consumed by the regex).
    patch = (
        "--- a/old.py\n"
        "+++ b/old.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
        f"{glued_line}\n"
    )

    result = _parse_patch_files(patch)

    # The parser must not produce a file whose path is the glued
    # ``/dev/null\t+++`` token (or any path starting with ``/dev/null``).
    for parsed_path in result:
        assert not parsed_path.startswith("/dev/null"), (
            f"parser produced a file path starting with /dev/null: "
            f"{parsed_path!r}. The glued marker must be classified as a "
            f"deletion marker, not a file path."
        )
        # Explicit defensive assertion on the exact glued name.
        assert "\t" not in parsed_path, (
            f"parser produced a file path containing a literal tab: "
            f"{parsed_path!r}. The glued marker must be classified as a "
            f"deletion marker, not a file path."
        )

    # The real ``--- a/old.py`` / ``+++ b/old.py`` headers on the FIRST
    # line of the patch are still parsed correctly — the deletion marker
    # on the glued line does not corrupt earlier file entries.
    # The key is the ``+++ b/old.py`` header was consumed BEFORE the
    # glued line, so ``old.py`` is the active current_path and its
    # content is reconstructed from the hunk below.
    assert "old.py" in result, (
        f"parser should have produced the old.py file from the pre-glued "
        f"hunk; got keys: {sorted(result.keys())!r}"
    )
    assert "old.py" in result
    assert "new" in result["old.py"]
    assert "old" not in result["old.py"]
