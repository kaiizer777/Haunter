"""GitHub pull request review and commit comment publisher for Auditor Mode (Phase 5.3).

Publishes deterministic, multi-perspective audit findings and actionable markdown reports
to GitHub Pull Requests (`POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews`) or
fallback Commit Comments (`POST /repos/{owner}/{repo}/commits/{sha}/comments`).

Operating Invariants (future02.md §1.8):
1. Auditor mode never mutates existing code, creates branches, or opens unsolicited PRs.
2. If confidence score falls below 75%, findings are suppressed from inline annotations
   or downgraded to informational notes only.
3. If publish_allowed is False, all publishing is suppressed (no PR reviews, no inline annotations).
4. Inline comment coordinates are strictly validated against diff hunks to prevent 422 errors.
5. GitHub API failures are handled gracefully and never crash the orchestrator or corrupt audit records.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Optional, Sequence

from app import github_client
from app.diff_validation import coerce_unified_diff
from app.llm.prompts.audit_prompts import (
    INFORMATIONAL_CONFIDENCE_THRESHOLD,
    swap_report_footer,
    MAX_CATEGORY_CHARS,
    MAX_GITHUB_COMMENT_CHARS,
    MAX_INLINE_FIELD_CHARS,
    MAX_SUGGESTED_FIX_CHARS,
    MAX_TITLE_CHARS,
    redact_sensitive_text,
    sanitize_output_markdown,
    sanitize_output_path,
    sanitize_output_text,
)
from app.subagents.auditor import (
    AuditFinding,
    AuditResult,
    DiffGrounding,
    build_diff_grounding,
    select_scoped_findings,
)

logger = logging.getLogger(__name__)

# Re-export client helpers for convenience as specified in future02.md §1.7
create_pr_review = github_client.create_pr_review
create_commit_comment = github_client.create_commit_comment
post_pr_comment = github_client.post_pr_comment
update_pr_review = github_client.update_pr_review


@dataclass(frozen=True)
class PublishResult:
    """Outcome of attempting to publish an audit report to GitHub."""

    published: bool
    status: Literal["published", "suppressed", "error", "skipped"]
    target_type: Literal["pull_request_review", "commit_comment", "none"]
    comments_count: int = 0
    suppressed_findings_count: int = 0
    event: Optional[str] = None
    error: Optional[str] = None
    response: Optional[dict[str, Any]] = None
    finding_comments: list[dict[str, Any]] = field(default_factory=list)
    posted_counts: Optional[dict[str, int]] = None


def validate_finding_coordinates(
    finding: AuditFinding,
    grounding: DiffGrounding,
) -> Optional[dict[str, Any]]:
    """Validate that a finding points to a real file and line number within the diff hunks.

    GitHub API rejects review comments with 422 Unprocessable Entity if the line
    is outside the modified diff hunks. This validator verifies the coordinates.

    Returns:
        dict with coordinate keys (`path`, `line`, `side`, optional `start_line`, `start_side`)
        or None if invalid.
    """
    file_path = sanitize_output_path(finding.file_path)
    lines_in_diff = grounding.line_index.get(file_path)
    if not lines_in_diff:
        return None

    target_line: Optional[int] = None
    # Prefer line_end as the anchor if it is in the diff hunk
    if finding.line_end in lines_in_diff:
        target_line = finding.line_end
    elif finding.line_start in lines_in_diff:
        target_line = finding.line_start
    else:
        # Check if any line in the range [line_start, line_end] is present in diff
        for line_num in range(finding.line_start, finding.line_end + 1):
            if line_num in lines_in_diff:
                target_line = line_num
                break

    if target_line is None:
        return None

    coords: dict[str, Any] = {
        "path": file_path,
        "line": target_line,
        "side": "RIGHT",
    }

    # If this is a multi-line finding and start_line is also in the diff hunk
    if finding.line_start < target_line and finding.line_start in lines_in_diff:
        coords["start_line"] = finding.line_start
        coords["start_side"] = "RIGHT"

    return coords


def format_inline_comment_body(
    finding: AuditFinding,
    *,
    is_informational: bool = False,
) -> str:
    """Format markdown content for an inline GitHub review comment."""
    severity_icon = {
        "BLOCKER": "🚨",
        "WARNING": "⚠️",
        "NOTE": "💡",
    }.get(finding.severity, "🔍")

    title = sanitize_output_text(finding.title, MAX_TITLE_CHARS)
    category = sanitize_output_text(finding.category, MAX_CATEGORY_CHARS)
    description = sanitize_output_text(finding.description, MAX_INLINE_FIELD_CHARS)

    lines: list[str] = []
    if finding.severity == "NOTE":
        lines.append(f"### 💡 Suggestion: {title}")
    else:
        lines.append(f"### {severity_icon} [{finding.severity}] {title}")
    lines.append("")

    if is_informational or finding.informational_only:
        lines.append(
            f"> [!TIP]\n"
            f"> **💡 Suggestion** (Confidence: `{finding.confidence}%`) — Manual verification advised.\n"
        )
        lines.append(f"**Category:** `{category}`")
    else:
        if finding.severity == "BLOCKER":
            lines.append(
                "> [!CAUTION]\n> **Blocker Finding** — Requires resolution before merge.\n"
            )
        elif finding.severity == "WARNING":
            lines.append(
                "> [!WARNING]\n> **Action Recommended** — Review proposed remediation.\n"
            )
        lines.append(
            f"**Category:** `{category}` • **Confidence:** `{finding.confidence}%`"
        )

    lines.append("")
    lines.append(f"**Problem:**\n{description}")

    if finding.suggested_fix and not (is_informational or finding.informational_only):
        clean_fix = redact_sensitive_text(finding.suggested_fix).strip()
        if clean_fix:
            bounded_fix = sanitize_output_text(clean_fix, MAX_SUGGESTED_FIX_CHARS)
            # Strip outer code fences if present
            if bounded_fix.startswith("```"):
                lines_fix = bounded_fix.splitlines()
                if (
                    len(lines_fix) >= 2
                    and lines_fix[0].startswith("```")
                    and lines_fix[-1].strip() == "```"
                ):
                    bounded_fix = "\n".join(lines_fix[1:-1]).strip()
            lines.append("")
            lines.append("**Suggested Remediation:**")
            # A full unified diff (`diff --git`, hunk headers, ---/+++ file
            # lines) is never valid ```suggestion content — GitHub applies a
            # suggestion block verbatim, so a diff pasted into one corrupts the
            # file. Render diff-shaped fixes as ```diff instead. The `--- `
            # match requires a trailing space plus path (diff header form), so
            # a bare YAML `---` document marker is not misclassified as a diff.
            stripped_fix = bounded_fix.lstrip()
            is_unified_diff = stripped_fix.startswith(
                ("diff --git", "@@ ", "--- ", "+++ ")
            )
            if is_unified_diff:
                lines.append(f"```diff\n{bounded_fix}\n```")
            else:
                lines.append(f"```suggestion\n{bounded_fix}\n```")

    lines.append("")
    lines.append("---")
    lines.append(f"*⚡ Haunter Auditor Finding ID: `{finding.id}`*")

    return redact_sensitive_text("\n".join(lines))


def format_finding_comment_body(
    finding: AuditFinding | Mapping[str, Any],
) -> str:
    """Format markdown content for a separate GitHub review comment for a finding."""
    if isinstance(finding, AuditFinding):
        severity = finding.severity
        title = finding.title
        file_path = finding.file_path
        line_start = finding.line_start
        line_end = finding.line_end
        description = finding.description
        suggested_fix = finding.suggested_fix
        finding_id = finding.id
    else:
        severity = str(finding.get("severity", "NOTE"))
        title = str(finding.get("title", "Audit Finding"))
        file_path = str(finding.get("file_path", "unknown"))
        try:
            line_start = int(finding.get("line_start", 1))
        except (TypeError, ValueError):
            line_start = 1
        try:
            line_end = int(finding.get("line_end", line_start))
        except (TypeError, ValueError):
            line_end = line_start
        description = str(finding.get("description", finding.get("impact", "")))
        suggested_fix = finding.get("suggested_fix")
        finding_id = str(finding.get("id", ""))

    sev_upper = severity.upper()
    if sev_upper == "BLOCKER":
        badge = "⛔ Must-Fix:"
    elif sev_upper == "WARNING":
        badge = "⚠️ Should-Fix:"
    else:
        badge = "💡 Suggestion:"

    clean_title = sanitize_output_text(title, MAX_TITLE_CHARS)
    clean_path = sanitize_output_path(file_path)
    clean_desc = sanitize_output_text(description, MAX_INLINE_FIELD_CHARS)

    line_ref = f"{line_start}" if line_start == line_end else f"{line_start}-{line_end}"

    lines = [
        f"### {badge} {clean_title}",
        "",
        f"**Location:** `{clean_path}#{line_ref}`",
        "",
        f"**What's Happening:**\n{clean_desc}",
        "",
    ]

    if suggested_fix:
        clean_fix = redact_sensitive_text(str(suggested_fix)).strip()
        validated_diff = coerce_unified_diff(clean_fix, file_path=clean_path)
        if validated_diff is not None:
            bounded_diff = sanitize_output_text(
                validated_diff, MAX_SUGGESTED_FIX_CHARS
            )
            lines.append("**Actionable Remediation Code Diff:**")
            lines.append(f"```diff\n{bounded_diff}\n```")
        else:
            # Prose is not a patch. Emit it as text so a diff-scraping
            # auto-fix command can never mistake it for an applicable hunk.
            bounded_fix = sanitize_output_text(clean_fix, MAX_SUGGESTED_FIX_CHARS)
            lines.append("**Suggested Remediation (manual):**")
            lines.append(f"```text\n{bounded_fix}\n```")
    else:
        lines.append("**Suggested Remediation (manual):**")
        lines.append(
            "```text\n"
            f"Manual remediation recommended for {clean_path} at line {line_ref}."
            "\n```"
        )

    if finding_id:
        lines.append("")
        lines.append("---")
        lines.append(f"*⚡ Haunter Guardian Review Comment · Finding ID: `{finding_id}`*")

    return redact_sensitive_text("\n".join(lines))


def format_finding_comment(
    finding: AuditFinding | Mapping[str, Any],
) -> str:
    """Format markdown content for an individual GitHub review comment for a finding."""
    return format_finding_comment_body(finding)


def build_inline_review_comments(
    findings: Sequence[AuditFinding],
    *,
    grounding: Optional[DiffGrounding] = None,
    diff_text: Optional[str] = None,
    min_confidence: int = INFORMATIONAL_CONFIDENCE_THRESHOLD,
    allow_informational: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """Filter, validate, and construct inline review comments for a GitHub PR review.

    Policy rules:
    - Findings with confidence < min_confidence (default 75%) are suppressed unless
      allow_informational is explicitly True (where they are downgraded to informational notes).
    - Findings outside the inspected diff hunks are suppressed from inline comments
      to avoid GitHub 422 Unprocessable Entity errors.

    Returns:
        tuple of (valid_comments_list, suppressed_findings_count)
    """
    if grounding is None and diff_text is not None:
        grounding = build_diff_grounding(diff_text)

    comments: list[dict[str, Any]] = []
    suppressed_count = 0

    for finding in findings:
        # Explicitly skip non-commentable or PR-level findings
        if getattr(finding, "non_commentable", False) or getattr(finding, "line_start", 1) == 0:
            suppressed_count += 1
            continue

        # Confidence check
        if finding.confidence < min_confidence:
            if not allow_informational:
                suppressed_count += 1
                continue
            is_informational = True
        else:
            is_informational = finding.informational_only

        # Coordinate validation against diff
        if grounding is not None:
            coords = validate_finding_coordinates(finding, grounding)
            if coords is None:
                suppressed_count += 1
                continue
        else:
            file_path = sanitize_output_path(finding.file_path)
            coords = {
                "path": file_path,
                "line": finding.line_end,
                "side": "RIGHT",
            }
            if finding.line_start < finding.line_end:
                coords["start_line"] = finding.line_start
                coords["start_side"] = "RIGHT"

        body = format_inline_comment_body(finding, is_informational=is_informational)
        coords["body"] = body
        comments.append(coords)

    return comments, suppressed_count


def _bound_github_body(body: Any, fallback: Any = "") -> str:
    """Clamp a rendered body to what GitHub will accept on a comment/review.

    Every field is already bounded individually, but the *total* was not: a
    report with 25 findings, each within its per-field budget, can still exceed
    GitHub's 65 536-character ceiling and be rejected with HTTP 422. Because the
    review body and its inline comments are one atomic request, that rejection
    discards every inline comment with it — a per-field bound cannot protect
    them. This is the publish-boundary clamp the auditor was missing.
    """
    text = sanitize_output_markdown(str(body or fallback or ""), MAX_GITHUB_COMMENT_CHARS)
    # sanitize_output_markdown counts its truncation marker *inside* `maximum` and
    # only shortens afterwards (control strip, CRLF normalisation), so this clamp
    # cannot change the value. It is kept as an unconditional slice rather than a
    # dead `if len(...) <= MAX` branch so the ceiling GitHub enforces stays
    # visible here, at the one boundary that has to honour it.
    return text[:MAX_GITHUB_COMMENT_CHARS]


async def publish_finding_comments(
    *,
    result: AuditResult,
    owner: Optional[str] = None,
    repo: Optional[str] = None,
    pr_number: Optional[int] = None,
    token: Optional[str] = None,
    allow_global_token: bool = False,
    min_confidence: int = INFORMATIONAL_CONFIDENCE_THRESHOLD,
) -> list[dict[str, Any]]:
    """Publish each finding as a separate comment on the pull request.

    Formats each finding with `format_finding_comment_body` and publishes
    via `github_client.post_pr_comment`.
    """
    resolved_owner = owner
    resolved_repo = repo
    if (not resolved_owner or not resolved_repo) and result.repo_full_name:
        if "/" in result.repo_full_name:
            resolved_owner, _, resolved_repo = result.repo_full_name.partition("/")

    if not resolved_owner or not resolved_repo:
        logger.warning(
            "publish_finding_comments missing owner/repo: %s", result.audit_id
        )
        return []

    if pr_number is None:
        logger.warning(
            "publish_finding_comments requires pr_number: %s", result.audit_id
        )
        return []

    responses: list[dict[str, Any]] = []
    for finding in result.findings:
        if finding.confidence < min_confidence:
            continue
        body = format_finding_comment_body(finding)
        resp = await github_client.post_pr_comment(
            owner=resolved_owner,
            repo=resolved_repo,
            pr_number=pr_number,
            body=body,
            token=token,
        )
        responses.append(resp)
    return responses





def update_dossier_meta(
    *,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    review_id: Optional[int | str],
    active_findings: Sequence[Any],
    resolved_count: int = 0,
    dossier_dir: Optional[str] = None,
    audit_id: str = "",
    posted_counts: Optional[Mapping[str, int]] = None,
) -> None:
    """Persist or update PR review state in {dossier_dir}/meta.json."""
    try:
        import json
        import tempfile
        from pathlib import Path

        if dossier_dir:
            target_dir = Path(dossier_dir)
        else:
            safe_repo = f"{owner}_{repo}".replace("/", "_") if owner and repo else "repo"
            target_dir = Path(tempfile.gettempdir()) / "haunter-dossier" / safe_repo / f"PR-{pr_number}"

        target_dir.mkdir(parents=True, exist_ok=True)
        meta_file = target_dir / "meta.json"

        serialized_findings = []
        for f in active_findings:
            if hasattr(f, "to_report_dict"):
                serialized_findings.append(f.to_report_dict())
            elif isinstance(f, dict):
                serialized_findings.append(f)
            elif hasattr(f, "__dict__"):
                serialized_findings.append({k: v for k, v in f.__dict__.items() if not k.startswith("_")})

        meta_data: dict[str, Any] = {
            "last_reviewed_sha": head_sha,
            "review_id": review_id,
            "active_findings": serialized_findings,
            "resolved_findings_count": resolved_count,
            "last_audit_id": audit_id,
        }
        if posted_counts is not None:
            meta_data["posted_counts"] = dict(posted_counts)

        meta_file.write_text(json.dumps(meta_data, indent=2), encoding="utf-8")
        logger.info("Updated dossier meta.json at %s (review_id=%s, sha=%s)", meta_file, review_id, head_sha)
    except Exception as exc:
        logger.warning("Could not update dossier meta.json: %s", exc)


def load_dossier_meta(
    *,
    owner: str,
    repo: str,
    pr_number: int,
    dossier_dir: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Load PR review state from {dossier_dir}/meta.json if present."""
    try:
        import json
        import tempfile
        from pathlib import Path

        if dossier_dir:
            target_dir = Path(dossier_dir)
        else:
            safe_repo = f"{owner}_{repo}".replace("/", "_") if owner and repo else "repo"
            target_dir = Path(tempfile.gettempdir()) / "haunter-dossier" / safe_repo / f"PR-{pr_number}"

        meta_file = target_dir / "meta.json"
        if meta_file.is_file():
            return json.loads(meta_file.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not load dossier meta.json: %s", exc)
    return None


async def publish_audit_review(
    *,
    result: AuditResult,
    owner: Optional[str] = None,
    repo: Optional[str] = None,
    head_sha: Optional[str] = None,
    pr_number: Optional[int] = None,
    diff_text: Optional[str] = None,
    token: Optional[str] = None,
    publish_allowed: Optional[bool] = None,
    request_changes_on_blocker: bool = False,
    allow_informational_inline: bool = False,
    allow_global_token: bool = False,
    raise_on_error: bool = False,
    post_finding_comments: bool = False,
    review_id: Optional[int | str] = None,
    resolved_since_last_review: Optional[int] = None,
    new_findings: Optional[Sequence[Any]] = None,
    active_findings: Optional[Sequence[Any]] = None,
    dossier_dir: Optional[str] = None,
) -> PublishResult:
    """Publish an audit report to GitHub as a PR Review or Commit Comment.

    Invariants:
    - NEVER pushes code, creates branches, or mutates repository state.
    - Honors `publish_allowed`: if False, completely suppresses publishing.
    - `effective_allowed = publish_allowed is True and confidence >= 75`.
    - If confidence < 75%, inline comments are suppressed and review event is COMMENT only.
    - If pr_number is present: posts via `create_pr_review`.
    - If pr_number is None: falls back to `create_commit_comment` on head_sha.
    - All coordinates validated against diff hunks.
    - Handles GitHub API errors without crashing the audit pipeline.
    """
    # Resolve owner and repo
    resolved_owner = owner
    resolved_repo = repo
    if (not resolved_owner or not resolved_repo) and result.repo_full_name:
        if "/" in result.repo_full_name:
            resolved_owner, _, resolved_repo = result.repo_full_name.partition("/")

    if not resolved_owner or not resolved_repo:
        logger.warning("audit publisher_missing_repo audit_id=%s", result.audit_id)
        return PublishResult(
            published=False,
            status="skipped",
            target_type="none",
            error="Missing repository coordinates",
        )

    # Resolve head commit SHA
    resolved_head_sha = head_sha
    if not resolved_head_sha:
        sha_match = re.search(r"\b([0-9a-fA-F]{40})\b", result.target_label)
        if sha_match:
            resolved_head_sha = sha_match.group(1).lower()

    # Determine publishing policy
    actual_publish_allowed = (
        result.publish_allowed if publish_allowed is None else bool(publish_allowed)
    )
    effective_allowed = (
        actual_publish_allowed is True
        and result.confidence >= INFORMATIONAL_CONFIDENCE_THRESHOLD
    )

    # Honor publish_allowed: if False, completely suppress publishing
    if not actual_publish_allowed:
        logger.info(
            "audit publish_suppressed audit_id=%s reason=publish_allowed_false",
            result.audit_id,
        )
        return PublishResult(
            published=False,
            status="suppressed",
            target_type="none",
            error="publish_allowed is False",
        )

    if new_findings is not None:
        converted_new: list[AuditFinding] = []
        for f in new_findings:
            if isinstance(f, AuditFinding):
                converted_new.append(f)
            elif isinstance(f, Mapping):
                converted_new.append(
                    AuditFinding(
                        id=str(f.get("id") or ""),
                        file_path=str(f.get("file_path") or ""),
                        line_start=int(f.get("line_start") or 1),
                        line_end=int(f.get("line_end") or 1),
                        perspective=f.get("perspective", "code_quality"),
                        severity=f.get("severity", "LOW"),
                        category=str(f.get("category") or ""),
                        title=str(f.get("title") or ""),
                        description=str(f.get("description") or ""),
                        suggested_fix=f.get("suggested_fix"),
                        confidence=int(f.get("confidence") or 0),
                        informational_only=bool(f.get("informational_only", False)),
                    )
                )
            else:
                converted_new.append(f)
        new_findings = converted_new

    resolved_pr_number = pr_number
    target_type: Literal["pull_request_review", "commit_comment"] = (
        "pull_request_review" if resolved_pr_number is not None else "commit_comment"
    )

    try:
        if resolved_pr_number is not None:
            # PR Review Context
            if not resolved_head_sha:
                resolved_head_sha = ""

            # Determine review event: COMMENT vs REQUEST_CHANGES
            if effective_allowed and request_changes_on_blocker:
                has_confident_blocker = any(
                    f.severity == "BLOCKER"
                    and f.confidence >= INFORMATIONAL_CONFIDENCE_THRESHOLD
                    for f in result.findings
                )
                event = "REQUEST_CHANGES" if has_confident_blocker else "COMMENT"
            else:
                event = "COMMENT"

            # Determine inline comments based on confidence threshold and scoped posting policy
            if effective_allowed:
                commentable_findings = [
                    f
                    for f in result.findings
                    if not (getattr(f, "non_commentable", False) or getattr(f, "line_start", 1) == 0)
                ]
                scoped_findings, posted_counts = select_scoped_findings(
                    commentable_findings, limit=8
                )
                comments, _ = build_inline_review_comments(
                    scoped_findings,
                    diff_text=diff_text,
                    min_confidence=INFORMATIONAL_CONFIDENCE_THRESHOLD,
                    allow_informational=allow_informational_inline,
                )
                suppressed_count = len(result.findings) - len(comments)
            else:
                scoped_findings = []
                posted_counts = {"C": 0, "H": 0, "M": 0, "L": 0, "I": 0}
                comments = []
                suppressed_count = len(result.findings)

            review_body = result.report_markdown or result.executive_summary
            review_body = swap_report_footer(
                review_body,
                resolved_since_last_review=resolved_since_last_review,
                posted_counts=posted_counts if effective_allowed else {},
            )
            review_body = _bound_github_body(review_body)

            if review_id is not None:
                # In-place update of existing PR review body
                resp = await github_client.update_pr_review(
                    owner=resolved_owner,
                    repo=resolved_repo,
                    pr_number=resolved_pr_number,
                    review_id=review_id,
                    body=review_body,
                    token=token,
                    allow_global_token=allow_global_token,
                )
                logger.info(
                    "audit review_updated in-place audit_id=%s pr=%d review_id=%s resolved_since_last=%s",
                    result.audit_id,
                    resolved_pr_number,
                    review_id,
                    resolved_since_last_review,
                )
                # Post inline comments ONLY for brand-new findings, scoped via select_scoped_findings
                comments = []
                suppressed_count = 0
                if effective_allowed and new_findings:
                    commentable_new = [
                        f
                        for f in new_findings
                        if not (
                            (isinstance(f, Mapping) and (f.get("non_commentable") or f.get("line_start") == 0))
                            or getattr(f, "non_commentable", False)
                            or getattr(f, "line_start", 1) == 0
                        )
                    ]
                    scoped_new, _ = select_scoped_findings(commentable_new, limit=8)
                    comments, _ = build_inline_review_comments(
                        scoped_new,
                        diff_text=diff_text,
                        min_confidence=INFORMATIONAL_CONFIDENCE_THRESHOLD,
                        allow_informational=allow_informational_inline,
                    )
                    suppressed_count = len(new_findings) - len(comments)
                    if comments:
                        await github_client.create_pr_review(
                            owner=resolved_owner,
                            repo=resolved_repo,
                            pr_number=resolved_pr_number,
                            commit_sha=resolved_head_sha,
                            body="",
                            comments=comments,
                            event="COMMENT",
                            token=token,
                            allow_global_token=allow_global_token,
                        )
            else:
                resp = await github_client.create_pr_review(
                    owner=resolved_owner,
                    repo=resolved_repo,
                    pr_number=resolved_pr_number,
                    commit_sha=resolved_head_sha,
                    body=review_body,
                    comments=comments,
                    event=event,
                    token=token,
                    allow_global_token=allow_global_token,
                )
                logger.info(
                    "audit review_published audit_id=%s pr=%d comments=%d suppressed=%d event=%s",
                    result.audit_id,
                    resolved_pr_number,
                    len(comments),
                    suppressed_count,
                    event,
                )

            final_review_id = review_id or (resp.get("id") if isinstance(resp, dict) else None)
            findings_for_meta = active_findings if active_findings is not None else result.findings
            update_dossier_meta(
                owner=resolved_owner,
                repo=resolved_repo,
                pr_number=resolved_pr_number,
                head_sha=resolved_head_sha,
                review_id=final_review_id,
                active_findings=findings_for_meta,
                resolved_count=resolved_since_last_review or 0,
                dossier_dir=dossier_dir,
                audit_id=result.audit_id,
                posted_counts=posted_counts,
            )

            finding_comments_resps: list[dict[str, Any]] = []
            if post_finding_comments and effective_allowed:
                findings_to_post = new_findings if (review_id is not None and new_findings is not None) else result.findings
                commentable_to_post = [
                    f
                    for f in findings_to_post
                    if not (
                        (isinstance(f, Mapping) and (f.get("non_commentable") or f.get("line_start") == 0))
                        or getattr(f, "non_commentable", False)
                        or getattr(f, "line_start", 1) == 0
                    )
                ]
                scoped_to_post, _ = select_scoped_findings(commentable_to_post, limit=8)
                temp_result = AuditResult(
                    audit_id=result.audit_id,
                    audit_type=result.audit_type,
                    repo_full_name=result.repo_full_name,
                    target_label=result.target_label,
                    engine=result.engine,
                    executive_summary=result.executive_summary,
                    findings=scoped_to_post,
                    confidence=result.confidence,
                    status=result.status,
                    remediation_diff=result.remediation_diff,
                    report_markdown=result.report_markdown,
                )
                finding_comments_resps = await publish_finding_comments(
                    result=temp_result,
                    owner=resolved_owner,
                    repo=resolved_repo,
                    pr_number=resolved_pr_number,
                    token=token,
                    allow_global_token=allow_global_token,
                )

            return PublishResult(
                published=True,
                status="published",
                target_type="pull_request_review",
                comments_count=len(comments),
                suppressed_findings_count=suppressed_count,
                event=event,
                response=resp,
                finding_comments=finding_comments_resps,
                posted_counts=posted_counts,
            )
        else:
            # Commit Context (fallback for push events / commit audits)
            if not resolved_head_sha:
                logger.warning(
                    "audit commit_comment_skipped audit_id=%s reason=missing_commit_sha",
                    result.audit_id,
                )
                return PublishResult(
                    published=False,
                    status="skipped",
                    target_type="commit_comment",
                    error="Missing head_sha for commit comment",
                )

            comment_body = _bound_github_body(
                result.report_markdown, result.executive_summary
            )
            resp = await github_client.create_commit_comment(
                owner=resolved_owner,
                repo=resolved_repo,
                commit_sha=resolved_head_sha,
                body=comment_body,
                token=token,
                allow_global_token=allow_global_token,
            )
            logger.info(
                "audit commit_comment_published audit_id=%s sha=%s",
                result.audit_id,
                resolved_head_sha[:10]
                if len(resolved_head_sha) >= 10
                else resolved_head_sha,
            )
            return PublishResult(
                published=True,
                status="published",
                target_type="commit_comment",
                comments_count=0,
                suppressed_findings_count=len(result.findings),
                response=resp,
            )
    except Exception as exc:
        err_name = type(exc).__name__
        logger.warning(
            "audit publish_error audit_id=%s target_type=%s error_type=%s",
            result.audit_id,
            target_type,
            err_name,
        )
        if raise_on_error:
            raise
        return PublishResult(
            published=False,
            status="error",
            target_type=target_type,
            error=f"{err_name}: {exc}",
        )
