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
from app.llm.prompts.audit_prompts import (
    INFORMATIONAL_CONFIDENCE_THRESHOLD,
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
)

logger = logging.getLogger(__name__)

# Re-export client helpers for convenience as specified in future02.md §1.7
create_pr_review = github_client.create_pr_review
create_commit_comment = github_client.create_commit_comment
post_pr_comment = github_client.post_pr_comment


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
        "**Actionable Remediation Code Diff:**",
    ]

    diff_body = ""
    if suggested_fix:
        clean_fix = redact_sensitive_text(str(suggested_fix)).strip()
        if clean_fix.startswith("```"):
            fence_lines = clean_fix.splitlines()
            if (
                len(fence_lines) >= 2
                and fence_lines[0].startswith("```")
                and fence_lines[-1].strip() == "```"
            ):
                clean_fix = "\n".join(fence_lines[1:-1]).strip()

        stripped_fix = clean_fix.lstrip()
        if stripped_fix.startswith(("diff --git", "--- ", "+++ ")):
            diff_body = clean_fix
        elif stripped_fix.startswith("@@"):
            diff_body = f"--- a/{clean_path}\n+++ b/{clean_path}\n{clean_fix}"
        else:
            snippet_lines = clean_fix.splitlines()
            has_diff_markers = any(line.startswith(("-", "+")) for line in snippet_lines)
            if has_diff_markers:
                diff_body = (
                    f"--- a/{clean_path}\n"
                    f"+++ b/{clean_path}\n"
                    f"@@ -{line_start},1 +{line_start},1 @@\n"
                    + clean_fix
                )
            else:
                diff_lines = [
                    f"--- a/{clean_path}",
                    f"+++ b/{clean_path}",
                    f"@@ -{line_start},1 +{line_start},{max(1, len(snippet_lines))} @@",
                ]
                for s_line in snippet_lines:
                    diff_lines.append(f"+ {s_line}")
                diff_body = "\n".join(diff_lines)
    else:
        diff_body = (
            f"--- a/{clean_path}\n"
            f"+++ b/{clean_path}\n"
            f"@@ -{line_start},1 +{line_start},1 @@\n"
            f"# Manual remediation recommended for {clean_path} at line {line_ref}."
        )

    bounded_diff = sanitize_output_text(diff_body, MAX_SUGGESTED_FIX_CHARS)
    lines.append(f"```diff\n{bounded_diff}\n```")

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

            # Determine inline comments based on confidence threshold
            if effective_allowed:
                comments, suppressed_count = build_inline_review_comments(
                    result.findings,
                    diff_text=diff_text,
                    min_confidence=INFORMATIONAL_CONFIDENCE_THRESHOLD,
                    allow_informational=allow_informational_inline,
                )
            else:
                comments = []
                suppressed_count = len(result.findings)

            review_body = _bound_github_body(
                result.report_markdown, result.executive_summary
            )

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
            finding_comments_resps: list[dict[str, Any]] = []
            if post_finding_comments and effective_allowed:
                finding_comments_resps = await publish_finding_comments(
                    result=result,
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
