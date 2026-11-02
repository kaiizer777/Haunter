"""Finding matching and review thread resolution for Auditor Mode.

Matches prior audit findings against delta review findings, delta diffs,
and GitHub review threads. Identifies:
- Resolved findings (code modified/outdated and finding not re-flagged)
- Retained/unresolved findings (not touched, or touched but re-flagged)
- New findings (newly introduced in the delta diff)
- Threads to resolve via GitHub GraphQL resolveReviewThread
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.subagents.auditor import AuditFinding

logger = logging.getLogger(__name__)

_HUNK_OLD_RANGE_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"
)


@dataclass
class FindingResolutionResult:
    """Outcome of finding matching across a re-review delta."""

    resolved_findings: list[dict[str, Any]]
    retained_findings: list[dict[str, Any]]
    new_findings: list[dict[str, Any]]
    threads_to_resolve: list[str]
    resolved_count: int = 0

    @property
    def active_findings(self) -> list[dict[str, Any]]:
        """All currently active findings (retained prior + new delta)."""
        return self.retained_findings + self.new_findings


def _extract_diff_modified_ranges(diff_text: str) -> dict[str, list[tuple[int, int]]]:
    """Extract modified/removed old line ranges per file from unified diff hunk headers."""
    ranges: dict[str, list[tuple[int, int]]] = {}
    current_file: str | None = None

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                # b/path/to/file -> path/to/file
                b_path = parts[3]
                current_file = b_path[2:] if b_path.startswith("b/") else b_path
                if current_file not in ranges:
                    ranges[current_file] = []
            continue

        if line.startswith("--- ") and not current_file:
            path_part = line[4:].strip()
            if path_part.startswith("a/"):
                path_part = path_part[2:]
            current_file = path_part
            if current_file not in ranges:
                ranges[current_file] = []
            continue

        if line.startswith("+++ "):
            path_part = line[4:].strip()
            if path_part.startswith("b/"):
                path_part = path_part[2:]
            current_file = path_part
            if current_file not in ranges:
                ranges[current_file] = []
            continue

        if current_file and line.startswith("@@ "):
            m = _HUNK_OLD_RANGE_RE.match(line)
            if m:
                old_start = int(m.group(1))
                old_count = int(m.group(2)) if m.group(2) is not None else 1
                ranges[current_file].append((old_start, old_start + max(1, old_count)))

    return ranges


def _to_finding_dict(finding: Any) -> dict[str, Any]:
    """Convert an AuditFinding or mapping into a standard dict."""
    if hasattr(finding, "file_path") and hasattr(finding, "line_start"):
        return {
            "id": finding.id,
            "perspective": finding.perspective,
            "severity": finding.severity,
            "category": finding.category,
            "title": finding.title,
            "description": finding.description,
            "file_path": finding.file_path,
            "line_start": finding.line_start,
            "line_end": finding.line_end,
            "confidence": finding.confidence,
            "suggested_fix": finding.suggested_fix,
            "informational_only": finding.informational_only,
            "thread_id": getattr(finding, "thread_id", None),
        }
    return dict(finding)


def _findings_match(f1: dict[str, Any], f2: dict[str, Any]) -> bool:
    """Determine if two findings represent the same underlying issue."""
    path1 = str(f1.get("file_path", "")).strip().lstrip("/")
    path2 = str(f2.get("file_path", "")).strip().lstrip("/")
    if path1 != path2:
        return False

    s1, e1 = int(f1.get("line_start", 1)), int(f1.get("line_end", 1))
    s2, e2 = int(f2.get("line_start", 1)), int(f2.get("line_end", 1))

    # Overlapping or within 5 lines of each other
    lines_close = (max(s1, s2) <= min(e1, e2) + 5)
    if not lines_close:
        return False

    # Check category or title similarity
    cat1 = str(f1.get("category", "")).lower()
    cat2 = str(f2.get("category", "")).lower()
    title1 = str(f1.get("title", "")).lower()
    title2 = str(f2.get("title", "")).lower()

    if cat1 and cat2 and cat1 == cat2:
        return True
    if title1 and title2 and (title1 in title2 or title2 in title1):
        return True
    if s1 == s2 and e1 == e2:
        return True

    return False


def match_and_resolve_findings(
    prior_findings: Sequence[AuditFinding | Mapping[str, Any]],
    delta_findings: Sequence[AuditFinding | Mapping[str, Any]],
    delta_diff: str = "",
    review_threads: Sequence[Mapping[str, Any]] = (),
) -> FindingResolutionResult:
    """Match prior findings against delta review findings and diff hunks.

    Rules:
    - A prior finding is touched if its anchor lines were modified/removed in the delta diff
      OR if its associated review thread is marked `isOutdated: True` on GitHub.
    - If a prior finding is touched (or its thread was resolved on GitHub) and NOT re-flagged
      in delta_findings, it is marked RESOLVED.
    - If a prior finding was NOT touched in the delta diff, it remains RETAINED (unresolved).
    - If a touched prior finding IS re-flagged in delta_findings, it remains RETAINED.
    - Any delta finding that does not match a retained prior finding is marked NEW.
    - All threads corresponding to newly resolved findings are scheduled for GraphQL resolution.
    """
    prior_dicts = [_to_finding_dict(f) for f in prior_findings]
    delta_dicts = [_to_finding_dict(f) for f in delta_findings]

    from app.subagents.auditor import build_diff_grounding

    grounding = build_diff_grounding(delta_diff) if delta_diff else None
    modified_ranges = _extract_diff_modified_ranges(delta_diff) if delta_diff else {}

    # Build lookup of review threads by path and line
    thread_by_finding: dict[str, dict[str, Any]] = {}
    for thread in review_threads:
        t_path = str(thread.get("path", "")).strip().lstrip("/")
        t_line = thread.get("line") or thread.get("originalLine")
        t_id = thread.get("id")
        if not t_id or not t_path:
            continue
        # Key by path:line
        if t_line:
            thread_by_finding[f"{t_path}:{t_line}"] = dict(thread)

    resolved: list[dict[str, Any]] = []
    retained: list[dict[str, Any]] = []
    threads_to_resolve: list[str] = []

    matched_delta_indices: set[int] = set()

    for prior in prior_dicts:
        p_path = str(prior.get("file_path", "")).strip().lstrip("/")
        p_s = int(prior.get("line_start", 1))
        p_e = int(prior.get("line_end", p_s))

        # Check if finding already has a thread_id or matches a thread in review_threads
        matching_thread: dict[str, Any] | None = None
        prior_thread_id = prior.get("thread_id")
        if prior_thread_id:
            for t in review_threads:
                if t.get("id") == prior_thread_id:
                    matching_thread = dict(t)
                    break
        if not matching_thread:
            # Check by path and line
            for line_candidate in (p_e, p_s):
                key = f"{p_path}:{line_candidate}"
                if key in thread_by_finding:
                    matching_thread = thread_by_finding[key]
                    prior["thread_id"] = matching_thread.get("id")
                    break

        is_thread_outdated = bool(matching_thread and matching_thread.get("isOutdated"))
        is_thread_already_resolved = bool(matching_thread and matching_thread.get("isResolved"))

        # Check if anchor was modified/removed in delta diff
        anchor_modified = False
        if grounding and p_path in grounding.line_index:
            lines_modified = grounding.line_index[p_path]
            if any(line_no in lines_modified for line_no in range(p_s, p_e + 1)):
                anchor_modified = True

        if not anchor_modified and p_path in modified_ranges:
            for r_s, r_e in modified_ranges[p_path]:
                if max(p_s, r_s) <= min(p_e, r_e):
                    anchor_modified = True
                    break

        touched = anchor_modified or is_thread_outdated or is_thread_already_resolved

        # Check if re-flagged in delta_findings
        reflagged_idx: int | None = None
        for idx, delta in enumerate(delta_dicts):
            if idx in matched_delta_indices:
                continue
            if _findings_match(prior, delta):
                reflagged_idx = idx
                break

        if reflagged_idx is not None:
            # Touched or untouched, but re-flagged: finding remains active
            matched_delta_indices.add(reflagged_idx)
            retained.append(prior)
        else:
            if touched:
                # Touched and NOT re-flagged -> Fixed/Resolved!
                resolved.append(prior)
                if matching_thread and not matching_thread.get("isResolved"):
                    th_id = matching_thread.get("id")
                    if th_id and th_id not in threads_to_resolve:
                        threads_to_resolve.append(th_id)
            else:
                # Untouched and not re-flagged in delta -> Retained active
                retained.append(prior)

    # Any delta finding that was not matched to a prior finding is brand new
    new_findings: list[dict[str, Any]] = [
        delta for idx, delta in enumerate(delta_dicts)
        if idx not in matched_delta_indices
    ]

    logger.info(
        "match_and_resolve_findings complete: prior=%d delta=%d resolved=%d retained=%d new=%d threads_to_resolve=%d",
        len(prior_dicts),
        len(delta_dicts),
        len(resolved),
        len(retained),
        len(new_findings),
        len(threads_to_resolve),
    )

    return FindingResolutionResult(
        resolved_findings=resolved,
        retained_findings=retained,
        new_findings=new_findings,
        threads_to_resolve=threads_to_resolve,
        resolved_count=len(resolved),
    )
