"""Unified diff chunking utilities for Auditor Mode.

Partitions unified diffs along hunk boundaries (`@@ -a,b +c,d @@`)
while strictly respecting line limits (~500 lines/chunk), preserving
file headers for every chunk so perspective subagents retain file context.
Files are never interleaved across chunks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

DEFAULT_MAX_CHUNK_LINES = 500
HARD_CHUNK_LIMIT = 550

_DIFF_GIT_RE = re.compile(r'^diff --git (?:"?a/.*?"?) (?:"?b/.*?"?)$')
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
    - Chunk per file at ~500 lines, strictly aligned to diff hunks (never split a hunk across chunks).
    - Files must NOT be interleaved across chunks. Every chunk belongs to exactly one file.
    - If an individual hunk exceeds 500 lines, keep it intact in its own chunk with file header
      (no sub-hunk mid-body slicing).
    - Every chunk includes the file header for context.

    Args:
        diff_text: Full unified diff string.
        max_chunk_lines: Soft target maximum lines per chunk (default 500).
        hard_limit: Strict ceiling or compatibility limit (default 550).

    Returns:
        List of chunked diff strings. Empty list if diff_text is blank.
    """
    if not diff_text or not diff_text.strip():
        return []

    files = _parse_unified_diff(diff_text)
    if not files:
        return [diff_text]

    chunks: list[str] = []
    ends_with_newline = diff_text.endswith("\n")

    def append_chunk(lines: list[str]) -> None:
        if not lines:
            return
        res = "\n".join(lines)
        if ends_with_newline and not res.endswith("\n"):
            res += "\n"
        chunks.append(res)

    for file in files:
        file_header = file.header_lines

        # If file has no hunks (e.g. binary or mode change), output file header as a chunk
        if not file.hunks:
            append_chunk(file_header)
            continue

        current_chunk_lines: list[str] = list(file_header)

        for hunk in file.hunks:
            hunk_full_lines = [hunk.header] + hunk.lines
            hunk_len = len(hunk_full_lines)

            # Check if current chunk already has at least one hunk
            if len(current_chunk_lines) > len(file_header):
                if len(current_chunk_lines) + hunk_len > max_chunk_lines:
                    append_chunk(current_chunk_lines)
                    current_chunk_lines = list(file_header)

            current_chunk_lines.extend(hunk_full_lines)

            # If this hunk alone (with header) reached or exceeded max_chunk_lines:
            # Keep it intact in its own chunk and flush immediately so subsequent hunks start fresh.
            if len(current_chunk_lines) >= max_chunk_lines:
                append_chunk(current_chunk_lines)
                current_chunk_lines = list(file_header)

        # Flush any remaining hunks for this file
        if len(current_chunk_lines) > len(file_header):
            append_chunk(current_chunk_lines)

    return chunks if chunks else [diff_text]
