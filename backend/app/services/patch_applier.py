"""
Patch application service for the Cloud Agentic Live Session commit pipeline.

Implements a deterministic unified diff applier using stdlib only.
No external dependencies (no unidiff, no python-patch).

Public API:
    apply_unified_diff(original: str, patch_text: str) -> str

Raises:
    ValueError: If the patch cannot be applied cleanly (hunk mismatch, context mismatch).
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Unified diff hunk header pattern: @@ -start,count +start,count @@
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def apply_unified_diff(original: str, patch_text: str) -> str:
    """
    Apply a unified diff patch to an original file string.

    Handles standard unified diff output with optional filename headers
    (--- a/... and +++ b/...) and one or more @@ ... @@ hunks.

    Parameters:
        original: The original file content as a string.
        patch_text: Unified diff text (output of `git diff` / `diff -u`).

    Returns:
        Patched file content as a string.

    Raises:
        ValueError: If any hunk cannot be applied cleanly due to context mismatch
                    or out-of-range line references.
    """
    if not patch_text or not patch_text.strip():
        return original

    # Normalise line endings to LF for processing; restore caller's EOL style at the end.
    original_has_crlf = "\r\n" in original
    original_lines = original.splitlines(keepends=False)

    patch_lines = patch_text.splitlines(keepends=False)

    # Parse all hunks from the diff.
    hunks = _parse_hunks(patch_lines)

    if not hunks:
        # No hunks found — diff may only contain filename headers; return original.
        return original

    result_lines = list(original_lines)
    # Apply hunks in reverse order so that earlier hunk applications do not
    # shift the line indices used by later hunks.
    offset = 0  # cumulative line delta applied so far

    for hunk in hunks:
        result_lines, offset = _apply_hunk(result_lines, hunk, offset)

    output = "\n".join(result_lines)
    if original_has_crlf:
        output = output.replace("\n", "\r\n")
    # Preserve trailing newline if original had one.
    if original.endswith(("\n", "\r\n")) and not output.endswith(("\n", "\r\n")):
        output += "\r\n" if original_has_crlf else "\n"
    return output


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_hunks(patch_lines: list[str]) -> list[dict]:
    """
    Parse unified diff text into a list of hunk dicts.

    Each hunk dict:
        old_start: int   1-based start line in original (from @@ -N,M @@)
        old_count: int   number of original lines the hunk covers
        new_start: int   1-based start line in patched output
        new_count: int   number of patched lines the hunk covers
        lines: list[str] raw hunk lines ('+', '-', ' ', '\\' prefixed)
    """
    hunks: list[dict] = []
    current: dict | None = None

    for line in patch_lines:
        m = _HUNK_HEADER_RE.match(line)
        if m:
            if current is not None:
                hunks.append(current)
            old_start = int(m.group(1))
            old_count = int(m.group(2)) if m.group(2) is not None else 1
            new_start = int(m.group(3))
            new_count = int(m.group(4)) if m.group(4) is not None else 1
            current = {
                "old_start": old_start,
                "old_count": old_count,
                "new_start": new_start,
                "new_count": new_count,
                "lines": [],
            }
            continue

        if current is not None:
            # Only capture lines that are part of the hunk body.
            # Skip filename headers (--- / +++), diff meta, and git binary patch.
            if line.startswith(("---", "+++", "diff ", "index ", "new file", "deleted file", "Binary")):
                if not hunks and current["lines"]:
                    # Hunk already started — this is a new file marker inside content (rare).
                    pass
                elif not current["lines"]:
                    # Header line before any hunk body — skip.
                    continue
            current["lines"].append(line)
        # Lines before the first hunk (filename headers etc.) are ignored.

    if current is not None:
        hunks.append(current)

    return hunks


def _apply_hunk(result_lines: list[str], hunk: dict, offset: int) -> tuple[list[str], int]:
    """
    Apply a single parsed hunk to result_lines (0-indexed internally).

    Parameters:
        result_lines: Current state of the file as a list of strings (no newlines).
        hunk: Parsed hunk dict from _parse_hunks.
        offset: Cumulative line-count delta from previously applied hunks.

    Returns:
        (updated_result_lines, new_offset)

    Raises:
        ValueError: Context line mismatch.
    """
    # Convert 1-based diff line number to 0-based index, adjusted by offset.
    start_idx = hunk["old_start"] - 1 + offset

    # Build the sequence of operations from hunk lines.
    ops: list[tuple[str, str]] = []  # (op_type, content) — op_type in {' ', '+', '-', '\\'}
    for raw in hunk["lines"]:
        if not raw:
            # Empty line within hunk body is a context line with no content.
            ops.append((" ", ""))
            continue
        prefix = raw[0]
        content = raw[1:]
        if prefix in (" ", "+", "-"):
            ops.append((prefix, content))
        elif prefix == "\\":
            # "\ No newline at end of file" — informational, skip.
            continue
        else:
            # Unexpected prefix in hunk body — treat as context.
            ops.append((" ", raw))

    # Validate and apply.
    read_idx = start_idx  # cursor into result_lines for '-' and ' ' ops
    new_lines: list[str] = []

    for op_type, content in ops:
        if op_type == " ":
            # Context line — must match result_lines[read_idx].
            if read_idx >= len(result_lines):
                raise ValueError(
                    f"Patch hunk at line {hunk['old_start']}: context line overruns file "
                    f"(read_idx={read_idx}, file_len={len(result_lines)})"
                )
            existing = result_lines[read_idx]
            if existing != content:
                raise ValueError(
                    f"Patch hunk at line {hunk['old_start']}: context mismatch at line {read_idx + 1}.\n"
                    f"  Expected: {content!r}\n"
                    f"  Got:      {existing!r}"
                )
            new_lines.append(content)
            read_idx += 1
        elif op_type == "-":
            # Remove line — advance past it without adding to output.
            if read_idx >= len(result_lines):
                raise ValueError(
                    f"Patch hunk at line {hunk['old_start']}: removal line overruns file "
                    f"(read_idx={read_idx}, file_len={len(result_lines)})"
                )
            existing = result_lines[read_idx]
            if existing != content:
                raise ValueError(
                    f"Patch hunk at line {hunk['old_start']}: removal mismatch at line {read_idx + 1}.\n"
                    f"  Expected to remove: {content!r}\n"
                    f"  Got:                {existing!r}"
                )
            read_idx += 1
        elif op_type == "+":
            # Insert line.
            new_lines.append(content)

    # Splice the hunk into result_lines:
    #   result_lines[start_idx : read_idx] is replaced by new_lines.
    updated = result_lines[:start_idx] + new_lines + result_lines[read_idx:]
    new_offset = offset + (len(new_lines) - (read_idx - start_idx))
    return updated, new_offset
