"""Unified diff chunking utilities for Auditor Mode.

Partitions unified diffs along hunk boundaries (`@@ -a,b +c,d @@`)
while strictly respecting line limits (<=550 lines/chunk), preserving
file headers for every chunk so perspective subagents retain file context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

DEFAULT_MAX_CHUNK_LINES = 500
HARD_CHUNK_LIMIT = 550

_DIFF_GIT_RE = re.compile(r"^diff --git a/.* b/.*$")
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


@dataclass
class _DiffHunk:
    header: str
    lines: list[str] = field(default_factory=list)


@dataclass
class _DiffFile:
    header_lines: list[str] = field(default_factory=list)
    hunks: list[_DiffHunk] = field(default_factory=list)


def _parse_unified_diff(diff_text: str) -> list[_DiffFile]:
    """Parse unified diff text into files and their respective hunks."""
    files: list[_DiffFile] = []
    current_file: _DiffFile | None = None
    current_hunk: _DiffHunk | None = None

    for line in diff_text.splitlines():
        # Check for new file header
        if _DIFF_GIT_RE.match(line):
            if current_hunk is not None and current_file is not None:
                current_file.hunks.append(current_hunk)
                current_hunk = None
            if current_file is not None:
                files.append(current_file)
            current_file = _DiffFile(header_lines=[line])
            continue

        # Check for hunk header
        if _HUNK_HEADER_RE.match(line):
            if current_file is None:
                # Diff did not start with `diff --git`
                current_file = _DiffFile(header_lines=[])
            if current_hunk is not None:
                current_file.hunks.append(current_hunk)
            current_hunk = _DiffHunk(header=line, lines=[])
            continue

        # Content line
        if current_hunk is not None:
            current_hunk.lines.append(line)
        elif current_file is not None:
            current_file.header_lines.append(line)
        else:
            # Preamble line before any diff header
            current_file = _DiffFile(header_lines=[line])

    if current_hunk is not None and current_file is not None:
        current_file.hunks.append(current_hunk)
    if current_file is not None:
        files.append(current_file)

    return files


def partition_diff_into_chunks(
    diff_text: str,
    max_chunk_lines: int = DEFAULT_MAX_CHUNK_LINES,
    hard_limit: int = HARD_CHUNK_LIMIT,
) -> list[str]:
    """Partition unified diff into chunks along hunk boundaries.

    Invariants:
    - Never slices through a hunk if it fits into a chunk within hard_limit.
    - If a single hunk exceeds hard_limit, it is split safely into sub-hunks.
    - Every chunk includes the file header for context.
    - No chunk exceeds hard_limit lines (unless the file header alone exceeds hard_limit).

    Args:
        diff_text: Full unified diff string.
        max_chunk_lines: Soft target maximum lines per chunk (default 500).
        hard_limit: Strict ceiling on lines per chunk (default 550).

    Returns:
        List of chunked diff strings. Empty list if diff_text is blank.
    """
    if not diff_text or not diff_text.strip():
        return []

    lines = diff_text.splitlines()
    if len(lines) <= hard_limit:
        return [diff_text]

    files = _parse_unified_diff(diff_text)
    if not files:
        return [diff_text]

    chunks: list[str] = []
    current_chunk_lines: list[str] = []
    # Track the file header currently active in the chunk
    current_chunk_file_header: list[str] = []

    def flush_chunk():
        nonlocal current_chunk_lines, current_chunk_file_header
        if current_chunk_lines:
            chunk_str = "\n".join(current_chunk_lines).strip()
            if chunk_str:
                chunks.append(chunk_str)
            current_chunk_lines = []
            current_chunk_file_header = []

    for file in files:
        file_header = file.header_lines

        # If file has no hunks (e.g. binary or mode change), add file header directly
        if not file.hunks:
            needed = len(file_header)
            if current_chunk_lines and (len(current_chunk_lines) + needed > max_chunk_lines):
                flush_chunk()
            current_chunk_lines.extend(file_header)
            continue

        for hunk in file.hunks:
            hunk_full_lines = [hunk.header] + hunk.lines
            hunk_len = len(hunk_full_lines)

            # Determine header lines needed if we add this hunk to current chunk
            needs_header = (current_chunk_file_header != file_header)
            header_to_add = file_header if needs_header else []
            cost = len(header_to_add) + hunk_len

            # Check if it fits in the current chunk
            if current_chunk_lines and (len(current_chunk_lines) + cost > max_chunk_lines):
                flush_chunk()
                needs_header = True
                header_to_add = file_header
                cost = len(header_to_add) + hunk_len

            # If it fits within hard_limit in a fresh or current chunk:
            if cost <= hard_limit:
                current_chunk_lines.extend(header_to_add)
                current_chunk_lines.extend(hunk_full_lines)
                current_chunk_file_header = file_header
            else:
                # The hunk itself (plus file header) exceeds hard_limit.
                # Flush whatever is in current chunk first.
                flush_chunk()

                # Slice the large hunk into sub-hunks
                header_len = len(file_header)
                available_per_chunk = max(10, hard_limit - header_len - 1)  # 1 line for hunk header

                body_lines = hunk.lines
                offset = 0
                part_idx = 1

                while offset < len(body_lines):
                    slice_lines = body_lines[offset : offset + available_per_chunk]
                    offset += len(slice_lines)

                    sub_chunk_lines: list[str] = list(file_header)
                    if part_idx == 1:
                        sub_chunk_lines.append(hunk.header)
                    else:
                        sub_chunk_lines.append(f"{hunk.header} (part {part_idx})")
                    sub_chunk_lines.extend(slice_lines)

                    chunk_str = "\n".join(sub_chunk_lines).strip()
                    if chunk_str:
                        chunks.append(chunk_str)
                    part_idx += 1

                current_chunk_lines = []
                current_chunk_file_header = []

    flush_chunk()
    return chunks if chunks else [diff_text]
