"""
GitHub Webhook Ingestion Router.

Handles incoming GitHub webhook events with strict security controls:
1. Max payload size check (<2MB) before JSON parsing (413).
2. Constant-time HMAC-SHA256 signature verification via hmac.compare_digest (401).
3. Delivery-id deduplication backed by DB unique constraint to close race windows.
4. Repository tenant validation against registered repos.
5. Immediate 2xx response (<200ms) with async pipeline scheduling via BackgroundTasks.
"""

import hashlib
import hmac
import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.models import CodeReview, Repo, Run
from app.schemas import (
    IssueCommentWebhookPayload,
    PullRequestReviewCommentWebhookPayload,
    WorkflowRunWebhookPayload,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# 2MB payload size limit to prevent memory-exhaustion DoS attacks
MAX_PAYLOAD_SIZE_BYTES = 2 * 1024 * 1024

# Collaborator authority allowlist for bot invocation
ALLOWED_AUTHOR_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
ALLOWED_WEBHOOK_EVENTS = frozenset({
    "workflow_run",
    "issue_comment",
    "pull_request_review_comment",
    "pull_request",
    "push",
})



@router.post("/github")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    x_github_delivery: Optional[str] = Header(None, alias="X-GitHub-Delivery"),
    x_github_event: Optional[str] = Header(None, alias="X-GitHub-Event"),
    x_hub_signature_256: Optional[str] = Header(None, alias="X-Hub-Signature-256"),
) -> dict[str, Any]:
    """
    Ingest GitHub webhook events. Public endpoint secured exclusively via HMAC-SHA256.
    """
    # 1. Early Content-Length check
    content_length_header = request.headers.get("content-length")
    if content_length_header:
        try:
            content_length = int(content_length_header)
            if content_length > MAX_PAYLOAD_SIZE_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail="Payload size exceeds 2MB limit",
                )
        except ValueError:
            pass

    # 2. Read raw bytes directly from request stream before any JSON parsing
    raw_body = await request.body()
    if len(raw_body) > MAX_PAYLOAD_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Payload size exceeds 2MB limit",
        )

    # 3. HMAC-SHA256 signature verification (constant-time compare)
    webhook_secret = settings.github_webhook_secret
    if not webhook_secret:
        logger.error("GITHUB_WEBHOOK_SECRET is not configured on server")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing signature",
        )

    if not x_hub_signature_256 or not x_hub_signature_256.startswith("sha256="):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing signature",
        )

    received_sig = x_hub_signature_256[len("sha256=") :]
    expected_sig = hmac.new(
        webhook_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_sig, received_sig):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing signature",
        )

    # 4. Require delivery ID header
    if not x_github_delivery:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing X-GitHub-Delivery header",
        )

    # 5. Filter event type — accept workflow_run, issue_comment, pull_request_review_comment
    if x_github_event not in ALLOWED_WEBHOOK_EVENTS:
        logger.info("Ignored webhook event: %s (delivery_id=%s)", x_github_event, x_github_delivery)
        return {"status": "ignored", "reason": f"unsupported event: {x_github_event}"}

    # 6. Parse JSON payload
    try:
        data = json.loads(raw_body.decode("utf-8"))
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed JSON payload",
        )

    # -----------------------------------------------------------------------
    # Branch A: workflow_run (Autonomous CI failure diagnosis)
    # -----------------------------------------------------------------------
    if x_github_event == "workflow_run":
        try:
            payload = WorkflowRunWebhookPayload.model_validate(data)
        except ValidationError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=e.errors(),
            )

        # Filter action & conclusion: only completed + failure trigger the fix pipeline
        if payload.action != "completed" or payload.workflow_run.conclusion != "failure":
            logger.info(
                "Ignored workflow_run (delivery_id=%s): action=%s, conclusion=%s",
                x_github_delivery,
                payload.action,
                payload.workflow_run.conclusion,
            )
            return {
                "status": "ignored",
                "reason": f"action={payload.action}, conclusion={payload.workflow_run.conclusion}",
            }

        # Ignore CI failures on Haunter's own fix branches
        if payload.workflow_run.head_branch and payload.workflow_run.head_branch.startswith("haunter/"):
            logger.info(
                "Ignored workflow_run (delivery_id=%s): branch=%s is a Haunter fix branch — feedback loop guard",
                x_github_delivery,
                payload.workflow_run.head_branch,
            )
            return {"status": "ignored", "reason": "haunter fix branch — feedback loop guard"}

        # Cross-check repository registration in DB
        owner = payload.repository.owner.login
        repo_name = payload.repository.name

        stmt = select(Repo).where(Repo.owner == owner, Repo.name == repo_name)
        result = await db.execute(stmt)
        repo = result.scalars().first()

        if not repo:
            logger.info(
                "Ignored webhook (delivery_id=%s): repository %s/%s is not registered in Haunter",
                x_github_delivery,
                owner,
                repo_name,
            )
            return {"status": "ignored", "reason": "unregistered repository"}

        # Idempotent Run creation backed by DB unique constraint
        new_run = Run(
            repo_id=repo.id,
            github_run_id=payload.workflow_run.id,
            github_delivery_id=x_github_delivery,
            head_sha=payload.workflow_run.head_sha,
            head_branch=payload.workflow_run.head_branch or "main",
            status="pending",
            conclusion="failure",
        )

        db.add(new_run)
        try:
            await db.commit()
            await db.refresh(new_run)
        except IntegrityError:
            await db.rollback()
            logger.info(
                "Duplicate webhook delivery %s for github_run_id %s dropped idempotently",
                x_github_delivery,
                payload.workflow_run.id,
            )
            return {
                "status": "duplicate",
                "delivery_id": x_github_delivery,
                "github_run_id": payload.workflow_run.id,
            }

        from app.adapters.hosting import get_hosting_adapter
        adapter = await get_hosting_adapter()
        await adapter.schedule_pipeline(new_run.id, background_tasks)

        return {
            "status": "queued",
            "run_id": str(new_run.id),
            "github_run_id": new_run.github_run_id,
            "delivery_id": x_github_delivery,
        }

    # -----------------------------------------------------------------------
    # Branch C: pull_request (Autonomous Push-Level Code Review Sentinel)
    # -----------------------------------------------------------------------
    if x_github_event == "pull_request":
        action = data.get("action")
        if action not in ("opened", "synchronize"):
            logger.info(
                "Ignored pull_request (delivery_id=%s): action=%s (expected opened or synchronize)",
                x_github_delivery,
                action,
            )
            return {"status": "ignored", "reason": f"unsupported PR action: {action}"}

        pr_data = data.get("pull_request") or {}

        # Guard 1: Ignore draft PRs
        if pr_data.get("draft") is True:
            logger.info("Ignored pull_request (delivery_id=%s): PR is draft", x_github_delivery)
            return {"status": "ignored", "reason": "draft PR"}

        # Guard 2: Ignore closed PRs
        if pr_data.get("state") == "closed":
            logger.info("Ignored pull_request (delivery_id=%s): PR is closed", x_github_delivery)
            return {"status": "ignored", "reason": "closed PR"}

        # Guard 3: Ignore bot PRs
        sender = data.get("sender") or {}
        pr_user = pr_data.get("user") or {}
        if (
            sender.get("type") == "Bot"
            or sender.get("login", "").endswith("[bot]")
            or pr_user.get("type") == "Bot"
            or pr_user.get("login", "").endswith("[bot]")
        ):
            logger.info("Ignored pull_request (delivery_id=%s): bot PR", x_github_delivery)
            return {"status": "ignored", "reason": "bot PR"}

        # Guard 4: Ignore Haunter fix branches (feedback loop guard)
        head_branch = pr_data.get("head", {}).get("ref") or ""
        if head_branch.startswith("haunter/"):
            logger.info("Ignored pull_request (delivery_id=%s): haunter fix branch %s", x_github_delivery, head_branch)
            return {"status": "ignored", "reason": "haunter fix branch"}

        # Check repository registration in DB
        repo_data = data.get("repository") or {}
        owner = repo_data.get("owner", {}).get("login") or repo_data.get("owner", {}).get("name")
        repo_name = repo_data.get("name")

        if not owner or not repo_name:
            logger.warning("Ignored pull_request (delivery_id=%s): missing repo owner/name in payload", x_github_delivery)
            return {"status": "ignored", "reason": "invalid repository payload"}

        stmt = select(Repo).where(Repo.owner == owner, Repo.name == repo_name)
        result = await db.execute(stmt)
        repo = result.scalars().first()

        if not repo:
            logger.info(
                "Ignored webhook (delivery_id=%s): repository %s/%s is not registered in Haunter",
                x_github_delivery,
                owner,
                repo_name,
            )
            return {"status": "ignored", "reason": "unregistered repository"}

        commit_sha = pr_data.get("head", {}).get("sha")
        pr_number = pr_data.get("number")
        if not commit_sha:
            return {"status": "ignored", "reason": "missing head sha"}

        # Deduplication guard: ignore redundant deliveries for the same commit
        existing_review_stmt = select(CodeReview).where(
            CodeReview.repo_id == repo.id,
            CodeReview.commit_sha == commit_sha,
            CodeReview.status.in_(["pending", "in_progress", "completed"]),
        )
        existing_review_res = await db.execute(existing_review_stmt)
        existing_review = existing_review_res.scalars().first()
        if existing_review:
            logger.info(
                "Ignored duplicate pull_request review webhook for repo %s/%s commit %s (review_id=%s)",
                owner,
                repo_name,
                commit_sha,
                existing_review.id,
            )
            return {
                "status": "duplicate",
                "review_id": str(existing_review.id),
                "repo": f"{owner}/{repo_name}",
                "pr_number": pr_number,
                "commit_sha": commit_sha,
                "delivery_id": x_github_delivery,
            }

        new_review = CodeReview(
            repo_id=repo.id,
            commit_sha=commit_sha,
            pr_number=pr_number,
            risk_score=0,
            summary="Autonomous code review queued.",
            findings=[],
            status="pending",
        )
        db.add(new_review)
        await db.commit()
        await db.refresh(new_review)

        from app.adapters.hosting import get_hosting_adapter
        adapter = await get_hosting_adapter()
        await adapter.schedule_review(new_review.id, background_tasks)

        return {
            "status": "queued",
            "review_id": str(new_review.id),
            "repo": f"{owner}/{repo_name}",
            "pr_number": pr_number,
            "commit_sha": commit_sha,
            "delivery_id": x_github_delivery,
        }

    # -----------------------------------------------------------------------
    # Branch D: push (Autonomous Push-Level Code Review Sentinel)
    # -----------------------------------------------------------------------
    if x_github_event == "push":
        ref = data.get("ref") or ""

        # Guard 1: Ignore tag pushes
        if ref.startswith("refs/tags/"):
            logger.info("Ignored push (delivery_id=%s): tag push %s", x_github_delivery, ref)
            return {"status": "ignored", "reason": "tag push"}

        # Guard 2: Ignore deleted refs
        if data.get("deleted") is True:
            logger.info("Ignored push (delivery_id=%s): deleted ref %s", x_github_delivery, ref)
            return {"status": "ignored", "reason": "deleted ref"}

        # Guard 3: Ignore bot commits
        sender = data.get("sender") or {}
        if sender.get("type") == "Bot" or sender.get("login", "").endswith("[bot]"):
            logger.info("Ignored push (delivery_id=%s): bot sender %s", x_github_delivery, sender.get("login"))
            return {"status": "ignored", "reason": "bot push"}

        head_commit = data.get("head_commit") or {}
        author = head_commit.get("author") or {}
        author_name = (author.get("name") or "").lower()
        if "haunter" in author_name or "[bot]" in author_name:
            logger.info("Ignored push (delivery_id=%s): bot commit author %s", x_github_delivery, author_name)
            return {"status": "ignored", "reason": "bot commit"}

        # Guard 4: Ignore pushes to Haunter fix branches
        branch_name = ref.replace("refs/heads/", "")
        if branch_name.startswith("haunter/"):
            logger.info("Ignored push (delivery_id=%s): haunter fix branch %s", x_github_delivery, branch_name)
            return {"status": "ignored", "reason": "haunter fix branch"}

        # Guard 5: Check valid commit SHA
        commit_sha = head_commit.get("id") or data.get("after")
        if not commit_sha or commit_sha == "0000000000000000000000000000000000000000":
            logger.info("Ignored push (delivery_id=%s): empty or null commit SHA", x_github_delivery)
            return {"status": "ignored", "reason": "empty commit sha"}

        # Check repository registration in DB
        repo_data = data.get("repository") or {}
        owner = repo_data.get("owner", {}).get("name") or repo_data.get("owner", {}).get("login")
        repo_name = repo_data.get("name")

        if not owner or not repo_name:
            logger.warning("Ignored push (delivery_id=%s): missing repo owner/name in payload", x_github_delivery)
            return {"status": "ignored", "reason": "invalid repository payload"}

        stmt = select(Repo).where(Repo.owner == owner, Repo.name == repo_name)
        result = await db.execute(stmt)
        repo = result.scalars().first()

        if not repo:
            logger.info(
                "Ignored webhook (delivery_id=%s): repository %s/%s is not registered in Haunter",
                x_github_delivery,
                owner,
                repo_name,
            )
            return {"status": "ignored", "reason": "unregistered repository"}

        # Deduplication guard: ignore redundant deliveries for the same commit
        existing_review_stmt = select(CodeReview).where(
            CodeReview.repo_id == repo.id,
            CodeReview.commit_sha == commit_sha,
            CodeReview.status.in_(["pending", "in_progress", "completed"]),
        )
        existing_review_res = await db.execute(existing_review_stmt)
        existing_review = existing_review_res.scalars().first()
        if existing_review:
            logger.info(
                "Ignored duplicate push review webhook for repo %s/%s commit %s (review_id=%s)",
                owner,
                repo_name,
                commit_sha,
                existing_review.id,
            )
            return {
                "status": "duplicate",
                "review_id": str(existing_review.id),
                "repo": f"{owner}/{repo_name}",
                "commit_sha": commit_sha,
                "delivery_id": x_github_delivery,
            }

        new_review = CodeReview(
            repo_id=repo.id,
            commit_sha=commit_sha,
            pr_number=None,
            risk_score=0,
            summary="Autonomous code review queued.",
            findings=[],
            status="pending",
        )
        db.add(new_review)
        await db.commit()
        await db.refresh(new_review)

        from app.adapters.hosting import get_hosting_adapter
        adapter = await get_hosting_adapter()
        await adapter.schedule_review(new_review.id, background_tasks)

        return {
            "status": "queued",
            "review_id": str(new_review.id),
            "repo": f"{owner}/{repo_name}",
            "commit_sha": commit_sha,
            "delivery_id": x_github_delivery,
        }

    # -----------------------------------------------------------------------
    # Branch B: Interactive PR Feedback (issue_comment / pull_request_review_comment)
    # -----------------------------------------------------------------------
    # 1. Action filtering: must be 'created'
    action = data.get("action")
    if action != "created":
        logger.info("Ignored %s (delivery_id=%s): action=%s (expected 'created')", x_github_event, x_github_delivery, action)
        return {"status": "ignored", "reason": f"unsupported action: {action}"}

    # 2. Schema validation
    pr_head_branch: Optional[str] = None
    pr_number: int
    if x_github_event == "issue_comment":
        try:
            comment_payload = IssueCommentWebhookPayload.model_validate(data)
        except ValidationError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=e.errors(),
            )
        # Verify comment is on a Pull Request (not a pure issue)
        if not comment_payload.issue.pull_request:
            logger.info("Ignored issue_comment (delivery_id=%s): comment is on an issue, not a pull request", x_github_delivery)
            return {"status": "ignored", "reason": "comment on issue, not pull request"}
        pr_number = comment_payload.issue.number
        comment_obj = comment_payload.comment
        repo_owner = comment_payload.repository.owner.login
        repo_name = comment_payload.repository.name
    else:  # pull_request_review_comment
        try:
            pr_comment_payload = PullRequestReviewCommentWebhookPayload.model_validate(data)
        except ValidationError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=e.errors(),
            )
        pr_number = pr_comment_payload.pull_request.number
        pr_head_branch = pr_comment_payload.pull_request.head.ref
        comment_obj = pr_comment_payload.comment
        repo_owner = pr_comment_payload.repository.owner.login
        repo_name = pr_comment_payload.repository.name

    # 3. Mention Gate: comment body must contain @haunter (case-insensitive)
    comment_body = comment_obj.body or ""
    if "@haunter" not in comment_body.lower():
        logger.info("Ignored %s (delivery_id=%s): no @haunter mention in comment", x_github_event, x_github_delivery)
        return {"status": "ignored", "reason": "no @haunter mention"}

    # 4. Collaborator Authority: author_association must be in OWNER, MEMBER, COLLABORATOR
    author_assoc = (comment_obj.author_association or "").upper()
    if author_assoc not in ALLOWED_AUTHOR_ASSOCIATIONS:
        logger.warning(
            "Ignored @haunter mention (delivery_id=%s) from untrusted user %r with author_association=%r",
            x_github_delivery,
            getattr(comment_obj.user, "login", "unknown"),
            author_assoc,
        )
        return {"status": "ignored", "reason": "unauthorized commenter"}

    # 5. Check repository registration in DB
    stmt = select(Repo).where(Repo.owner == repo_owner, Repo.name == repo_name)
    result = await db.execute(stmt)
    repo = result.scalars().first()
    if not repo:
        logger.info(
            "Ignored webhook (delivery_id=%s): repository %s/%s is not registered",
            x_github_delivery,
            repo_owner,
            repo_name,
        )
        return {"status": "ignored", "reason": "unregistered repository"}

    # 6. Look up initial / parent Run for this PR
    run_stmt = (
        select(Run)
        .where(Run.repo_id == repo.id, Run.pr_number == pr_number)
        .order_by(Run.created_at.asc())
    )
    run_res = await db.execute(run_stmt)
    initial_run = run_res.scalars().first()

    if not initial_run:
        logger.info(
            "Ignored @haunter mention (delivery_id=%s): no matching Haunter run for %s/%s PR #%d",
            x_github_delivery,
            repo_owner,
            repo_name,
            pr_number,
        )
        return {"status": "ignored", "reason": "no matching run for PR"}

    # If initial_run is a child run, traverse up to the root parent run
    while initial_run.parent_run_id:
        parent_stmt = select(Run).where(Run.id == initial_run.parent_run_id)
        parent_res = await db.execute(parent_stmt)
        root_parent = parent_res.scalars().first()
        if not root_parent:
            break
        initial_run = root_parent

    # 7. Branch Guard: PR branch must start with haunter/
    if not pr_head_branch:
        pr_head_branch = initial_run.pr_branch or initial_run.head_branch

    if not pr_head_branch or not pr_head_branch.startswith("haunter/"):
        logger.warning(
            "Ignored @haunter mention on non-haunter branch %r for %s/%s PR #%d",
            pr_head_branch,
            repo_owner,
            repo_name,
            pr_number,
        )
        return {"status": "ignored", "reason": "non-haunter branch"}

    # 8. Rate Limit Guard: Max 5 refinement iterations per PR
    count_stmt = select(func.count()).select_from(Run).where(Run.parent_run_id == initial_run.id)
    child_count = await db.scalar(count_stmt) or 0
    if child_count >= 5:
        logger.warning(
            "Rate limit reached for PR #%d (already has %d refinement runs)",
            pr_number,
            child_count,
        )
        limit_msg = "⚠️ Haunter PR refinement limit reached (max 5 iterations per PR)."
        try:
            from app.github.pr import get_installation_token
            from app.github_client import post_pr_comment

            token = await get_installation_token(repo)
            await post_pr_comment(
                owner=repo.owner,
                repo=repo.name,
                pr_number=pr_number,
                body=limit_msg,
                token=token,
            )
        except Exception as exc:
            logger.warning(
                "Failed to post rate limit notice comment on %s/%s PR #%d: %s",
                repo.owner,
                repo.name,
                pr_number,
                exc,
            )
        return {"status": "ignored", "reason": "refinement limit reached"}

    # 9. Idempotent Child Run creation
    new_run = Run(
        repo_id=repo.id,
        parent_run_id=initial_run.id,
        github_run_id=comment_obj.id,
        github_delivery_id=x_github_delivery,
        head_sha=initial_run.head_sha,
        head_branch=pr_head_branch,
        pr_number=pr_number,
        pr_branch=pr_head_branch,
        status="pending",
        conclusion="feedback",
    )

    db.add(new_run)
    try:
        await db.commit()
        await db.refresh(new_run)
    except IntegrityError:
        await db.rollback()
        logger.info(
            "Duplicate webhook delivery %s for comment id %s dropped idempotently",
            x_github_delivery,
            comment_obj.id,
        )
        return {
            "status": "duplicate",
            "delivery_id": x_github_delivery,
            "comment_id": comment_obj.id,
        }

    # 10. Schedule pipeline asynchronously
    from app.adapters.hosting import get_hosting_adapter
    adapter = await get_hosting_adapter()
    await adapter.schedule_pipeline(new_run.id, background_tasks)

    return {
        "status": "queued",
        "run_id": str(new_run.id),
        "parent_run_id": str(initial_run.id),
        "comment_id": comment_obj.id,
        "delivery_id": x_github_delivery,
    }

