"""
Autonomous Code Review Orchestrator — Feature 1.

Orchestrates the asynchronous code review pipeline:
1. Loads pending CodeReview from DB.
2. Resolves GitHub App installation token.
3. Fetches pull request unified diff or commit diff via GitHub API.
4. Invokes the Review Subagent (code_reviewer.analyze_diff).
5. Persists findings, risk_score, summary, and token telemetry to code_reviews table.
6. Submits formal GitHub Pull Request Review (REQUEST_CHANGES or COMMENT) with inline
   ```suggestion blocks, or posts a commit comment on push events.
"""

from __future__ import annotations

import logging
from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import async_session_maker
from app.config import settings
from app.github.pr import get_installation_token, resolve_installation_id
from app.github_client import (
    create_commit_comment,
    create_pull_request_review,
    fetch_diff,
    fetch_pull_request_diff,
)
from app.models import CodeReview
from app.services.repo_settings import get_repo_settings
from app.subagents.code_reviewer import (
    analyze_diff,
    format_github_suggestion,
)

logger = logging.getLogger(__name__)

# Maximum chars stored in code_reviews.failure_reason (human-readable, no secrets).
_FAILURE_REASON_MAX_CHARS = 500


def _truncate_failure_reason(reason: object) -> str:
    """Truncate a publish/fetch failure to 500 chars for DB storage."""
    text = str(reason) if reason is not None else "unknown error"
    return text[:_FAILURE_REASON_MAX_CHARS]


async def _ensure_install_id(session: Any, repo: Any) -> None:
    """
    Backfill ``repo.github_install_id`` when missing but the GitHub App is configured.

    Resolves via ``GET /repos/{owner}/{repo}/installation`` (App JWT) and
    persists the result on the repo row. No-op when the id is already set or
    when App credentials are absent (dev fallback uses ``settings.github_token``).
    Never raises — failures are logged and the caller proceeds to token
    resolution, which will surface a clear error downstream.
    """
    existing = getattr(repo, "github_install_id", None)
    if isinstance(existing, int) and not isinstance(existing, bool) and existing > 0:
        return
    if not settings.github_app_id or not settings.github_app_private_key:
        return
    try:
        install_id = await resolve_installation_id(repo.owner, repo.name)
    except Exception as exc:
        logger.warning(
            "review_orchestrator: could not auto-resolve install id for %s/%s: %s",
            repo.owner,
            repo.name,
            exc,
        )
        return
    repo.github_install_id = install_id
    try:
        await session.commit()
    except Exception as exc:
        await session.rollback()
        logger.warning(
            "review_orchestrator: failed to backfill install id for %s/%s: %s",
            repo.owner,
            repo.name,
            exc,
        )
        return
    logger.info(
        "review_orchestrator: backfilled github_install_id for %s/%s",
        repo.owner,
        repo.name,
    )


def _build_risk_badge(risk_score: int) -> str:
    if risk_score <= 30:
        return "🟢 **Low Risk**"
    elif risk_score <= 70:
        return "🟡 **Moderate Risk**"
    return "🔴 **Critical / High Risk**"


async def run_code_review_pipeline(review_id: uuid.UUID) -> None:
    """
    Execute end-to-end code review pipeline for a CodeReview row.
    Runs asynchronously in BackgroundTasks or via AWS Lambda self-invocation.
    """
    logger.info("review_orchestrator: starting code review for review_id=%s", review_id)

    async with async_session_maker() as session:
        stmt = (
            select(CodeReview)
            .options(selectinload(CodeReview.repo))
            .where(CodeReview.id == review_id)
        )
        res = await session.execute(stmt)
        review = res.scalars().first()

        if not review:
            logger.error("review_orchestrator: review_id=%s not found", review_id)
            return

        if review.status not in ("pending", "in_progress"):
            logger.info(
                "review_orchestrator: review_id=%s already in terminal status=%s",
                review_id,
                review.status,
            )
            return

        review.status = "in_progress"
        await session.commit()
        await session.refresh(review)

        repo = review.repo
        if not repo:
            logger.error(
                "review_orchestrator: repo not found for review_id=%s", review_id
            )
            review.status = "error"
            review.summary = "Repository record not found."
            await session.commit()
            return

        # 1. Resolve installation token (backfill install id first for legacy repos)
        await _ensure_install_id(session, repo)
        try:
            token = await get_installation_token(repo)
        except Exception as exc:
            logger.warning(
                "review_orchestrator: failed to resolve token for repo %s/%s "
                "(has_install_id=%s app_configured=%s): %s",
                repo.owner,
                repo.name,
                bool(getattr(repo, "github_install_id", None)),
                bool(settings.github_app_id and settings.github_app_private_key),
                exc,
            )
            token = None

        # 2. Fetch diff
        diff_text = ""
        try:
            if review.pr_number:
                try:
                    diff_text = await fetch_pull_request_diff(
                        owner=repo.owner,
                        repo=repo.name,
                        pr_number=review.pr_number,
                        token=token,
                    )
                except Exception as pr_diff_err:
                    logger.warning(
                        "review_orchestrator: fetch_pull_request_diff failed for %s/%s PR #%d (%s), falling back to commit diff",
                        repo.owner,
                        repo.name,
                        review.pr_number,
                        pr_diff_err,
                    )
                    diff_text = await fetch_diff(
                        owner=repo.owner,
                        repo=repo.name,
                        sha=review.commit_sha,
                        token=token,
                    )
            else:
                diff_text = await fetch_diff(
                    owner=repo.owner,
                    repo=repo.name,
                    sha=review.commit_sha,
                    token=token,
                )
        except Exception as diff_err:
            logger.error(
                "review_orchestrator: failed to fetch diff for %s/%s @ %s: %s",
                repo.owner,
                repo.name,
                review.commit_sha,
                diff_err,
            )
            review.status = "error"
            review.summary = f"Failed to fetch git diff: {diff_err}"
            review.failure_reason = _truncate_failure_reason(
                f"Failed to fetch git diff: {diff_err}"
            )
            await session.commit()
            return

        # 3. Analyze diff via subagent
        repo_context = f"{repo.owner}/{repo.name}"
        if repo.language_hint:
            repo_context += f" (primary language: {repo.language_hint})"

        try:
            result = await analyze_diff(
                diff_text=diff_text,
                repo_context=repo_context,
                db=session,
                repo_id=repo.id,
            )
        except Exception as llm_err:
            logger.exception(
                "review_orchestrator: LLM review analysis failed for review_id=%s: %s",
                review_id,
                llm_err,
            )
            review.status = "error"
            review.summary = f"Code review analysis failed: {llm_err}"
            review.failure_reason = _truncate_failure_reason(
                f"Code review analysis failed: {llm_err}"
            )
            await session.commit()
            return

        # 4. Persist analysis results — status stays in_progress until the
        # GitHub publish below succeeds. Marking completed here would hide
        # POST failures behind a success status (R2).
        review.risk_score = result.output.risk_score
        review.summary = result.output.summary
        review.findings = [f.model_dump() for f in result.output.findings]
        review.input_tokens = result.input_tokens
        review.output_tokens = result.output_tokens
        review.failure_reason = None
        await session.commit()
        await session.refresh(review)

        logger.info(
            "review_orchestrator: review analyzed for %s/%s @ %s (risk_score=%d, findings=%d, latency=%dms)",
            repo.owner,
            repo.name,
            review.commit_sha,
            review.risk_score,
            len(result.output.findings),
            result.latency_ms,
        )

        # 5. Submit review to GitHub — only now may the row become completed.
        # Publish failures set status=error + failure_reason (truncated 500)
        # while keeping findings/summary so the dashboard stays truthful.
        repo_settings = await get_repo_settings(session, repo.id)
        risk_badge = _build_risk_badge(review.risk_score)
        if not repo_settings.enable_pr_comments:
            logger.info(
                "review_orchestrator: PR comments disabled by repo settings — suppressing review comment on %s/%s",
                repo.owner,
                repo.name,
            )
            review.status = "completed"
            await session.commit()
            await session.refresh(review)
            logger.info(
                "review_orchestrator: review completed (comments suppressed) for %s/%s @ %s",
                repo.owner,
                repo.name,
                review.commit_sha,
            )
            return
        if review.pr_number:
            # PR Review: comments array + overall event
            comments: list[dict[str, Any]] = []
            for f in result.output.findings:
                comment_dict: dict[str, Any] = {
                    "path": f.file_path,
                    "line": f.line_end,
                    "side": "RIGHT",
                    "body": format_github_suggestion(f),
                }
                if f.line_start < f.line_end:
                    comment_dict["start_line"] = f.line_start
                    comment_dict["start_side"] = "RIGHT"
                comments.append(comment_dict)

            review_event = "REQUEST_CHANGES" if review.risk_score >= 80 else "COMMENT"
            body = (
                f"### 🛡️ Haunter Autonomous Code Review\n\n"
                f"**Risk Score**: {review.risk_score}/100 — {risk_badge}\n\n"
                f"{review.summary}\n\n"
                f"*Actionable findings: {len(comments)} flagged across 4 engineering dimensions.*"
            )

            try:
                await create_pull_request_review(
                    owner=repo.owner,
                    repo=repo.name,
                    pr_number=review.pr_number,
                    commit_sha=review.commit_sha,
                    body=body,
                    comments=comments,
                    event=review_event,
                    token=token,
                )
                logger.info(
                    "review_orchestrator: posted PR review on %s/%s PR #%d (event=%s)",
                    repo.owner,
                    repo.name,
                    review.pr_number,
                    review_event,
                )
                review.status = "completed"
                await session.commit()
                await session.refresh(review)
                logger.info(
                    "review_orchestrator: review completed for %s/%s @ %s (risk_score=%d, findings=%d)",
                    repo.owner,
                    repo.name,
                    review.commit_sha,
                    review.risk_score,
                    len(result.output.findings),
                )
            except Exception as gh_err:
                is_422 = "422" in str(gh_err)
                logger.warning(
                    "review_orchestrator: failed to post PR review with inline comments "
                    "on %s/%s PR #%d (invalid_inline_422=%s): %s. "
                    "Dropping inline comments and retrying summary-only review.",
                    repo.owner,
                    repo.name,
                    review.pr_number,
                    is_422,
                    gh_err,
                )
                try:
                    fallback_body = body + "\n\n### 🔍 Detailed Findings\n"
                    for i, f in enumerate(result.output.findings, 1):
                        fallback_body += f"\n{i}. **{f.file_path}:{f.line_start}-{f.line_end}** ({f.category.upper()} / {f.severity.upper()}):\n   {f.critique}\n"
                        if f.suggested_patch:
                            fallback_body += (
                                f"\n   ```\n   {f.suggested_patch.strip()}\n   ```\n"
                            )

                    await create_pull_request_review(
                        owner=repo.owner,
                        repo=repo.name,
                        pr_number=review.pr_number,
                        commit_sha=review.commit_sha,
                        body=fallback_body,
                        comments=[],
                        event=review_event,
                        token=token,
                    )
                    logger.info(
                        "review_orchestrator: posted fallback summary PR review on %s/%s PR #%d (event=%s)",
                        repo.owner,
                        repo.name,
                        review.pr_number,
                        review_event,
                    )
                    review.status = "completed"
                    await session.commit()
                    await session.refresh(review)
                except Exception as fallback_err:
                    logger.error(
                        "review_orchestrator: fallback PR review also failed on %s/%s PR #%d: %s",
                        repo.owner,
                        repo.name,
                        review.pr_number,
                        fallback_err,
                    )
                    # Findings/summary are kept; only the status signals the
                    # publish failure so the dashboard stays truthful.
                    review.status = "error"
                    review.failure_reason = _truncate_failure_reason(
                        f"Failed to publish PR review: {fallback_err}"
                    )
                    await session.commit()
                    await session.refresh(review)
        else:
            # Commit comment for push without PR
            findings_md = ""
            if result.output.findings:
                findings_md = "\n\n### 🔍 Actionable Findings\n"
                for i, f in enumerate(result.output.findings, 1):
                    findings_md += f"\n{i}. **{f.file_path}:{f.line_start}-{f.line_end}** ({f.category.upper()} / {f.severity.upper()}):\n   {f.critique}\n"
                    if f.suggested_patch:
                        findings_md += (
                            f"\n   ```\n   {f.suggested_patch.strip()}\n   ```\n"
                        )

            body = (
                f"### 🛡️ Haunter Push-Level Code Review\n\n"
                f"**Risk Score**: {review.risk_score}/100 — {risk_badge}\n\n"
                f"{review.summary}"
                f"{findings_md}"
            )

            try:
                await create_commit_comment(
                    owner=repo.owner,
                    repo=repo.name,
                    commit_sha=review.commit_sha,
                    body=body,
                    token=token,
                )
                logger.info(
                    "review_orchestrator: posted commit comment on %s/%s @ %s",
                    repo.owner,
                    repo.name,
                    review.commit_sha,
                )
                review.status = "completed"
                await session.commit()
                await session.refresh(review)
            except Exception as gh_err:
                logger.error(
                    "review_orchestrator: failed to post commit comment on %s/%s @ %s: %s",
                    repo.owner,
                    repo.name,
                    review.commit_sha,
                    gh_err,
                )
                review.status = "error"
                review.failure_reason = _truncate_failure_reason(
                    f"Failed to publish commit comment: {gh_err}"
                )
                await session.commit()
                await session.refresh(review)
