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
from dataclasses import dataclass
from typing import Any, Literal, Optional, Sequence

from app import github_client
from app.llm.prompts.audit_prompts import (
    INFORMATIONAL_CONFIDENCE_THRESHOLD,
    MAX_CATEGORY_CHARS,
    MAX_INLINE_FIELD_CHARS,
    MAX_SUGGESTED_FIX_CHARS,
    MAX_TITLE_CHARS,
    redact_sensitive_text,
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
    if (
        finding.line_start < target_line
        and finding.line_start in lines_in_diff
    ):
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
        "NOTE": "ℹ️",
    }.get(finding.severity, "🔍")

    title = sanitize_output_text(finding.title, MAX_TITLE_CHARS)
    category = sanitize_output_text(finding.category, MAX_CATEGORY_CHARS)
    description = sanitize_output_text(finding.description, MAX_INLINE_FIELD_CHARS)

    lines: list[str] = []
    if is_informational or finding.informational_only:
        lines.append(f"> ℹ️ **[INFORMATIONAL NOTE]** (Confidence: `{finding.confidence}%`)")
        lines.append(f"### {severity_icon} [{finding.severity}] {title}")
        lines.append(f"**Category:** {category}")
    else:
        lines.append(f"### {severity_icon} [{finding.severity}] {title}")
        lines.append(f"**Category:** {category} | **Confidence:** `{finding.confidence}%`")

    lines.append("")
    lines.append(description)

    if finding.suggested_fix and not (is_informational or finding.informational_only):
        clean_fix = redact_sensitive_text(finding.suggested_fix).strip()
        if clean_fix:
            bounded_fix = sanitize_output_text(clean_fix, MAX_SUGGESTED_FIX_CHARS)
            lines.append("")
            lines.append("**Suggested Remediation:**")
            lines.append("```python" if "\n" in bounded_fix else "```")
            lines.append(bounded_fix)
            lines.append("```")

    lines.append("")
    lines.append(f"*Haunter Auditor Finding ID: `{finding.id}`*")

    return redact_sensitive_text("\n".join(lines))


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
                    f.severity == "BLOCKER" and f.confidence >= INFORMATIONAL_CONFIDENCE_THRESHOLD
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

            review_body = result.report_markdown or result.executive_summary

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
            return PublishResult(
                published=True,
                status="published",
                target_type="pull_request_review",
                comments_count=len(comments),
                suppressed_findings_count=suppressed_count,
                event=event,
                response=resp,
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

            comment_body = result.report_markdown or result.executive_summary
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
                resolved_head_sha[:10] if len(resolved_head_sha) >= 10 else resolved_head_sha,
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
