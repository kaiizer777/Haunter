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


def _extract_diff_changed_old_lines(diff_text: str) -> dict[str, set[int]]:
    """Extract set of line numbers in the old file that were modified or removed.

    Ignores untouched context lines so findings on context lines are not falsely marked resolved.
    """
    changed_lines: dict[str, set[int]] = {}
    current_file: str | None = None
    cur_old = 0

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            current_file = None
            cur_old = 0
            m = re.match(r'^diff --git (?:"?a/(.*?)"?) (?:"?b/(.*?)"?)$', line)
            if m:
                b_path = m.group(2)
                a_path = m.group(1)
                target = b_path if b_path != "/dev/null" else a_path
                current_file = target.strip().strip("'\"").lstrip("/")
                changed_lines.setdefault(current_file, set())
            else:
                parts = line.split()
                if len(parts) >= 4:
                    b_path = parts[3]
                    current_file = (b_path[2:] if b_path.startswith("b/") else b_path).strip().strip("'\"").lstrip("/")
                    changed_lines.setdefault(current_file, set())
            continue

        if line.startswith("--- ") and not current_file:
            path_part = line[4:].strip().strip("'\"")
            if path_part.startswith("a/"):
                path_part = path_part[2:]
            if path_part != "/dev/null":
                current_file = path_part.lstrip("/")
                changed_lines.setdefault(current_file, set())
            continue

        if line.startswith("+++ "):
            path_part = line[4:].strip().strip("'\"")
            if path_part.startswith("b/"):
                path_part = path_part[2:]
            if path_part != "/dev/null" and not current_file:
                current_file = path_part.lstrip("/")
                changed_lines.setdefault(current_file, set())
            continue

        if current_file and line.startswith("@@ "):
            m = _HUNK_OLD_RANGE_RE.match(line)
            if m:
                cur_old = int(m.group(1))
            else:
                cur_old = 0
            continue

        if current_file and cur_old > 0:
            if line.startswith("-"):
                changed_lines[current_file].add(cur_old)
                cur_old += 1
            elif line.startswith("+"):
                pass
            elif line.startswith(" "):
                cur_old += 1
            elif line.startswith(r"\ No"):
                pass

    return changed_lines


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
            "priority_bucket": getattr(finding, "priority_bucket", None),
        }
    d = dict(finding)
    if "priority_bucket" not in d and hasattr(finding, "priority_bucket"):
        d["priority_bucket"] = getattr(finding, "priority_bucket")
    return d


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

    changed_old_lines = _extract_diff_changed_old_lines(delta_diff) if delta_diff else {}

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

        # Check if anchor was modified/removed in delta diff (checking only changed old-side lines)
        anchor_modified = False
        if p_path in changed_old_lines:
            file_changed = changed_old_lines[p_path]
            if any(line_no in file_changed for line_no in range(p_s, p_e + 1)):
                anchor_modified = True

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
