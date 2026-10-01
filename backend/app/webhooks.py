"""
GitHub Webhook Ingestion Router.

Handles incoming GitHub webhook events with strict security controls:
1. Max payload size check (<2MB) before JSON parsing (413).
2. Constant-time HMAC-SHA256 signature verification via hmac.compare_digest
   on raw request bytes (401) — before any JSON parsing or routing.
3. Delivery-id deduplication backed by DB unique constraint to close race windows.
4. Repository tenant validation against registered repos.
5. Immediate 2xx response (<200ms) with async pipeline scheduling via BackgroundTasks.
6. Phase 5.1 (future02 §1.5) Auditor Mode trigger filter: pull_request
   (opened/synchronize), workflow_run.completed (failure/success), and
   issue_comment.created (@haunter audit) are evaluated against
   app.services.audit_pipeline triggers after signature verification,
   repo registration, branch guards, and the per-repo kill-switch, then
    dispatched through a durable, independent audit queue (never git push / PR creation).
"""

import json
import logging
import uuid
from datetime import datetime
from typing import Annotated, Any, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    status,
)
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import settings
from app.db import get_db
from app.log_hygiene import sanitize_log_value
from app.models import CodeReview, Repo, Run, User, WebhookDelivery
from app.schemas import (
    IssueCommentWebhookPayload,
    PullRequestReviewCommentWebhookPayload,
    WorkflowRunWebhookPayload,
)
from app.services import audit_pipeline, feature_enforcement
from app.services.repo_settings import get_repo_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# 2MB payload size limit to prevent memory-exhaustion DoS attacks
MAX_PAYLOAD_SIZE_BYTES = 2 * 1024 * 1024

# Feature 8 — bounds for persisted webhook health rows.
_WEBHOOK_REASON_MAX_CHARS = 500
_WEBHOOK_EVENT_MAX_CHARS = 64
_WEBHOOK_STATUS_MAX_CHARS = 32

# Collaborator authority allowlist for bot invocation
ALLOWED_AUTHOR_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
ALLOWED_WEBHOOK_EVENTS = frozenset(
    {
        "workflow_run",
        "issue_comment",
        "pull_request_review_comment",
        "pull_request",
        "push",
    }
)


def _log_repo(owner: Any, name: Any) -> str:
    """Sanitized `owner/name` for one log record.

    Owner and repository names are read out of an attacker-influenced JSON
    payload, so they reach the log pipeline through the canonical log sanitizer:
    control stripping (a newline in a name forges a second log line),
    credential redaction, and a length bound. The same two values are still used
    verbatim for the registration lookup and for the response body.
    """
    return f"{sanitize_log_value(owner, 100)}/{sanitize_log_value(name, 100)}"


def _log_webhook_decision(
    *,
    event: Any,
    delivery_id: Any,
    status: str,
    reason: Any,
    repo: Any = None,
    level: str = "info",
) -> None:
    """Structured log for ignored/skipped/duplicate webhook decisions.

    Emits a single key=value line so CloudWatch Insights can filter on
    ``webhook_decision``, ``event``, ``status``, and ``delivery_id`` without
    regex parsing. All values pass through the log sanitizer (delivery ids
    and reasons are attacker-influenced via headers/payload).
    """
    msg = (
        "webhook_decision event=%s status=%s reason=%s delivery_id=%s repo=%s"
        % (
            sanitize_log_value(event, 64),
            sanitize_log_value(status, 32),
            sanitize_log_value(reason, 255),
            sanitize_log_value(delivery_id, 64),
            sanitize_log_value(repo, 200) if repo is not None else "-",
        )
    )
    if level == "warning":
        logger.warning(msg)
    else:
        logger.info(msg)


def _truncate_reason(reason: Any) -> Optional[str]:
    """Bound persisted reason length; attacker-influenced, never stored raw unbounded."""
    if reason is None:
        return None
    text = str(reason)
    if len(text) > _WEBHOOK_REASON_MAX_CHARS:
        return text[:_WEBHOOK_REASON_MAX_CHARS]
    return text


async def _record_webhook_delivery(
    db: AsyncSession,
    *,
    event: Any,
    delivery_id: Any,
    status_value: str,
    reason: Any,
    repo: Any = None,
    repo_id: Optional[uuid.UUID] = None,
    level: str = "info",
) -> None:
    """Persist a webhook decision alongside the structured log line.

    Feature 8 health log: every call emits the CloudWatch-parseable
    _log_webhook_decision line AND best-effort inserts a WebhookDelivery row.
    DB failures are swallowed (rollback + warning) so ingestion latency and
    2xx responses are never affected by health-log pressure.
    """
    _log_webhook_decision(
        event=event,
        delivery_id=delivery_id,
        status=status_value,
        reason=reason,
        repo=repo,
        level=level,
    )
    try:
        event_text = str(event)[:_WEBHOOK_EVENT_MAX_CHARS] if event is not None else "unknown"
        delivery_text = str(delivery_id)[:128] if delivery_id is not None else "unknown"
        status_text = str(status_value)[:_WEBHOOK_STATUS_MAX_CHARS]
        repo_text = str(repo)[:255] if repo is not None else None
        row = WebhookDelivery(
            event=event_text,
            delivery_id=delivery_text,
            status=status_text,
            reason=_truncate_reason(reason),
            repo=repo_text,
            repo_id=repo_id,
        )
        db.add(row)
        await db.commit()
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass
        logger.warning("webhook delivery health-log insert failed", exc_info=True)


class WebhookDeliveryOut(BaseModel):
    """Explicit response DTO — never leaks raw payloads or tokens."""

    id: uuid.UUID
    event: str
    delivery_id: str
    status: str
    reason: Optional[str] = None
    repo: Optional[str] = None
    repo_id: Optional[uuid.UUID] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class WebhookDeliveryListOut(BaseModel):
    deliveries: list[WebhookDeliveryOut]
    total: int

    model_config = ConfigDict(extra="forbid")


async def _user_repo_scope(
    db: AsyncSession, user: User
) -> tuple[list[uuid.UUID], list[str]]:
    """Return (repo_ids, repo_full_names) owned by the caller for tenant scoping."""
    result = await db.execute(select(Repo).where(Repo.user_id == user.id))
    repos = list(result.scalars().all())
    ids = [r.id for r in repos]
    names = [f"{r.owner}/{r.name}" for r in repos]
    return ids, names


@router.get("/deliveries", response_model=WebhookDeliveryListOut)
async def list_webhook_deliveries(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    event: Annotated[Optional[str], Query(max_length=64)] = None,
) -> WebhookDeliveryListOut:
    """List webhook deliveries for repos owned by the caller (health history).

    Tenant isolation: only rows whose repo_id or owner/name matches one of the
    caller's repos are returned. Unknown repos return an empty list, never 404,
    so the health tab renders an empty state instead of an error.
    """
    repo_ids, repo_names = await _user_repo_scope(db, current_user)
    if not repo_ids and not repo_names:
        return WebhookDeliveryListOut(deliveries=[], total=0)

    scope_clauses: list[Any] = []
    if repo_ids:
        scope_clauses.append(WebhookDelivery.repo_id.in_(repo_ids))
    if repo_names:
        scope_clauses.append(WebhookDelivery.repo.in_(repo_names))
    filters: list[Any] = [or_(*scope_clauses)]
    if event is not None:
        filters.append(WebhookDelivery.event == event)

    count_stmt = select(func.count(WebhookDelivery.id)).where(*filters)
    total = (await db.execute(count_stmt)).scalar_one() or 0

    stmt = (
        select(WebhookDelivery)
        .where(*filters)
        .order_by(WebhookDelivery.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return WebhookDeliveryListOut(
        deliveries=[WebhookDeliveryOut.model_validate(r) for r in rows],
        total=total,
    )


@router.post("/deliveries/{delivery_row_id}/replay", response_model=WebhookDeliveryOut)
async def replay_webhook_delivery(
    delivery_row_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WebhookDeliveryOut:
    """Audit-only replay marker for a webhook delivery.

    Inserts a new WebhookDelivery row with status="replayed" referencing the
    original. Never re-executes the pipeline, so replay cannot create duplicate
    Runs or re-trigger background work. Returns 404 for unknown or non-owned rows
    (no existence oracle across tenants).
    """
    repo_ids, repo_names = await _user_repo_scope(db, current_user)
    result = await db.execute(
        select(WebhookDelivery).where(WebhookDelivery.id == delivery_row_id)
    )
    original = result.scalar_one_or_none()
    if original is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delivery not found")
    owned = (original.repo_id is not None and original.repo_id in repo_ids) or (
        original.repo is not None and original.repo in repo_names
    )
    if not owned:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delivery not found")

    replay_reason = _truncate_reason(f"replay of {original.id}")
    replay_row = WebhookDelivery(
        event=original.event,
        delivery_id=original.delivery_id,
        status="replayed",
        reason=replay_reason,
        repo=original.repo,
        repo_id=original.repo_id,
    )
    db.add(replay_row)
    await db.commit()
    await db.refresh(replay_row)
    _log_webhook_decision(
        event=replay_row.event,
        delivery_id=replay_row.delivery_id,
        status="replayed",
        reason=f"replay of {original.id}",
        repo=replay_row.repo,
    )
    return WebhookDeliveryOut.model_validate(replay_row)


async def _read_limited_body(request: Request) -> bytes:
    """Read the request body, aborting as soon as the cap is exceeded.

    Stops consuming the stream on the first chunk that would cross the limit, so
    peak memory stays bounded by MAX_PAYLOAD_SIZE_BYTES regardless of what the
    client actually sends or how it frames the request.
    """
    buffer = bytearray()
    async for chunk in request.stream():
        if not chunk:
            continue
        if len(buffer) + len(chunk) > MAX_PAYLOAD_SIZE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Payload size exceeds 2MB limit",
            )
        buffer.extend(chunk)
    return bytes(buffer)


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
    # 1. Early Content-Length check (cheap reject; not trusted on its own)
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

    # 2. Read the body incrementally with a hard byte cap.
    # `await request.body()` buffers the entire request in memory first and only
    # then checks the size, so a chunked request without Content-Length could
    # force an unbounded allocation before the limit was ever consulted. The
    # stream is abandoned as soon as the cap is crossed.
    raw_body = await _read_limited_body(request)

    # 3. HMAC-SHA256 signature verification against the raw request bytes
    # (constant-time compare). Single implementation lives in
    # app.services.audit_pipeline.verify_github_signature (SHOULD-2) —
    # this router must not duplicate HMAC logic.
    webhook_secret = settings.github_webhook_secret
    if not webhook_secret:
        logger.error("GITHUB_WEBHOOK_SECRET is not configured on server")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing signature",
        )

    if not audit_pipeline.verify_github_signature(
        raw_body, x_hub_signature_256, webhook_secret
    ):
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
        logger.info(
            "Ignored webhook event: %s (delivery_id=%s)",
            x_github_event,
            x_github_delivery,
        )
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

        # MUST-1: registration and loop guards run BEFORE the auditor so
        # unregistered repos and Haunter's own fix branches never schedule
        # background audits.

        # Guard 1: Ignore CI events on Haunter's own fix branches
        # (feedback-loop guard — applies to the auditor path as well).
        if (
            payload.workflow_run.head_branch
            and payload.workflow_run.head_branch.startswith("haunter/")
        ):
            logger.info(
                "Ignored workflow_run (delivery_id=%s): branch=%s is a Haunter fix branch — feedback loop guard",
                x_github_delivery,
                payload.workflow_run.head_branch,
            )
            return {
                "status": "ignored",
                "reason": "haunter fix branch — feedback loop guard",
            }

        # Guard 2: Cross-check repository registration in DB
        owner = payload.repository.owner.login
        repo_name = payload.repository.name

        stmt = select(Repo).where(Repo.owner == owner, Repo.name == repo_name)
        result = await db.execute(stmt)
        repo = result.scalars().first()

        if not repo:
            logger.info(
                "Ignored webhook (delivery_id=%s): repository %s is not registered in Haunter",
                x_github_delivery,
                _log_repo(owner, repo_name),
            )
            return {"status": "ignored", "reason": "unregistered repository"}

        repo_settings = await get_repo_settings(db, repo.id)

        # Phase 6.3 Feature Enforcement: Branch Guard
        branch_decision = feature_enforcement.is_branch_allowed(
            payload.workflow_run.head_branch, repo_settings.allowed_branches
        )
        if not branch_decision.allowed:
            logger.info(
                "Ignored workflow_run (delivery_id=%s): %s",
                x_github_delivery,
                branch_decision.reason,
            )
            return {"status": "skipped", "reason": branch_decision.reason}

        # Phase 5.1 Auditor Mode: evaluate trigger filter AFTER HMAC
        # verification (above), schema validation, and the guards above.
        # Per-repo trigger (master kill-switch) is loaded for the registered
        # repo — a disabled trigger schedules nothing. Read-only dispatch —
        # never creates branches or PRs.
        try:
            _trigger = await audit_pipeline.get_auditor_trigger(db, repo.id)
            _decision = audit_pipeline.evaluate_workflow_run(
                _trigger, payload.action, payload.workflow_run.conclusion
            )
            if _decision.should_audit and _decision.audit_type:
                _repo_full = f"{owner}/{repo_name}"
                _audit_id = await audit_pipeline.dispatch_audit(
                    db=db,
                    repo_id=repo.id,
                    audit_type=_decision.audit_type,
                    repo_full_name=_repo_full,
                    delivery_id=x_github_delivery,
                    ref=payload.workflow_run.head_branch,
                    head_sha=payload.workflow_run.head_sha,
                    workflow_run_id=payload.workflow_run.id,
                    settings_version=_trigger.settings_version,
                )
                logger.info(
                    "Auditor queued audit_id=%s type=%s repo=%s delivery_id=%s",
                    _audit_id,
                    _decision.audit_type,
                    _log_repo(owner, repo_name),
                    x_github_delivery,
                )
                # Success runs are ignored by the fix pipeline but are a
                # first-class auditor trigger when on_ci_success is enabled.
                if (
                    payload.action != "completed"
                    or payload.workflow_run.conclusion != "failure"
                ):
                    return {
                        "status": "audit_queued",
                        "audit_id": _audit_id,
                        "audit_type": _decision.audit_type,
                        "repo": _repo_full,
                        "delivery_id": x_github_delivery,
                    }
            else:
                logger.info(
                    "Auditor skipped workflow_run (delivery_id=%s): %s",
                    x_github_delivery,
                    _decision.reason,
                )
        except audit_pipeline.AuditDeliveryConflictError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Delivery conflicts with a previously recorded audit payload",
            )
        except Exception as exc:
            logger.warning(
                "Auditor trigger evaluation failed (workflow_run): %s",
                type(exc).__name__,
            )

        # Filter action & conclusion: only completed + failure trigger the fix pipeline
        if (
            payload.action != "completed"
            or payload.workflow_run.conclusion != "failure"
        ):
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

        # Phase 6.3 Feature Enforcement: Autonomous Fix Guard
        auto_fix_decision = feature_enforcement.is_auto_fix_allowed(
            repo_settings.enable_auto_fix
        )
        if not auto_fix_decision.allowed:
            logger.info(
                "Ignored workflow_run (delivery_id=%s): %s",
                x_github_delivery,
                auto_fix_decision.reason,
            )
            return {"status": "skipped", "reason": auto_fix_decision.reason}

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
            await _record_webhook_delivery(
                db,
                event="workflow_run",
                delivery_id=x_github_delivery,
                status_value="duplicate",
                reason=f"github_run_id={payload.workflow_run.id}",
                repo=f"{owner}/{repo_name}",
                repo_id=repo.id,
            )
            return {
                "status": "duplicate",
                "delivery_id": x_github_delivery,
                "github_run_id": payload.workflow_run.id,
            }

        from app.adapters.hosting import get_hosting_adapter

        adapter = await get_hosting_adapter()
        await adapter.schedule_pipeline(new_run.id, background_tasks)

        await _record_webhook_delivery(
            db,
            event="workflow_run",
            delivery_id=x_github_delivery,
            status_value="queued",
            reason=f"github_run_id={payload.workflow_run.id} run_id={new_run.id}",
            repo=f"{owner}/{repo_name}",
            repo_id=repo.id,
        )

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

        # Check repository registration in DB
        repo_data = data.get("repository") or {}
        owner = repo_data.get("owner", {}).get("login") or repo_data.get(
            "owner", {}
        ).get("name")
        repo_name = repo_data.get("name")

        if not owner or not repo_name:
            logger.warning(
                "Ignored pull_request (delivery_id=%s): missing repo owner/name in payload",
                x_github_delivery,
            )
            return {"status": "ignored", "reason": "invalid repository payload"}

        stmt = select(Repo).where(Repo.owner == owner, Repo.name == repo_name)
        result = await db.execute(stmt)
        repo = result.scalars().first()

        if not repo:
            logger.info(
                "Ignored webhook (delivery_id=%s): repository %s is not registered in Haunter",
                x_github_delivery,
                _log_repo(owner, repo_name),
            )
            return {"status": "ignored", "reason": "unregistered repository"}

        repo_settings = await get_repo_settings(db, repo.id)

        # Guard 1: Ignore draft PRs (governed by repo_settings.ignore_draft_prs)
        is_draft = pr_data.get("draft") is True
        draft_decision = feature_enforcement.is_draft_pr_allowed(
            is_draft, repo_settings.ignore_draft_prs
        )
        if not draft_decision.allowed:
            logger.info(
                "Ignored pull_request (delivery_id=%s): %s",
                x_github_delivery,
                draft_decision.reason,
            )
            return {"status": "ignored", "reason": "draft PR"}

        # Guard 2: Ignore closed PRs
        if pr_data.get("state") == "closed":
            logger.info(
                "Ignored pull_request (delivery_id=%s): PR is closed", x_github_delivery
            )
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
            logger.info(
                "Ignored pull_request (delivery_id=%s): bot PR", x_github_delivery
            )
            return {"status": "ignored", "reason": "bot PR"}

        # Guard 4: Ignore Haunter fix branches (feedback loop guard)
        head_branch = pr_data.get("head", {}).get("ref") or ""
        if head_branch.startswith("haunter/"):
            logger.info(
                "Ignored pull_request (delivery_id=%s): haunter fix branch %s",
                x_github_delivery,
                head_branch,
            )
            return {"status": "ignored", "reason": "haunter fix branch"}

        # Guard 5: Branch Allowance Guard (Phase 6.3)
        base_branch = (
            pr_data.get("base", {}).get("ref") or repo.default_branch or "main"
        )
        branch_decision = feature_enforcement.is_pr_branch_allowed(
            target_branch=base_branch,
            head_branch=head_branch,
            allowed_branches=repo_settings.allowed_branches,
        )
        if not branch_decision.allowed:
            logger.info(
                "Ignored pull_request (delivery_id=%s): %s",
                x_github_delivery,
                branch_decision.reason,
            )
            return {"status": "skipped", "reason": branch_decision.reason}

        commit_sha = pr_data.get("head", {}).get("sha")
        base_sha = pr_data.get("base", {}).get("sha")
        pr_number = pr_data.get("number")
        if not commit_sha:
            logger.info(
                "Ignored pull_request (delivery_id=%s): missing head sha",
                x_github_delivery,
            )
            await _record_webhook_delivery(
                db,
                event="pull_request",
                delivery_id=x_github_delivery,
                status_value="ignored",
                reason="missing head sha",
                repo=f"{owner}/{repo_name}",
                repo_id=repo.id,
            )
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
                "Ignored duplicate pull_request review webhook for repo %s commit %s (review_id=%s)",
                _log_repo(owner, repo_name),
                sanitize_log_value(commit_sha, 64),
                existing_review.id,
            )
            await _record_webhook_delivery(
                db,
                event="pull_request",
                delivery_id=x_github_delivery,
                status_value="duplicate",
                reason=f"commit={commit_sha} review_id={existing_review.id}",
                repo=f"{owner}/{repo_name}",
                repo_id=repo.id,
            )
            return {
                "status": "duplicate",
                "review_id": str(existing_review.id),
                "repo": f"{owner}/{repo_name}",
                "pr_number": pr_number,
                "commit_sha": commit_sha,
                "delivery_id": x_github_delivery,
            }

        # Phase 5.1 Auditor Mode trigger filter (after HMAC verification and
        # sentinel guards above). Disabled features exit here with no audit
        # scheduled (<50ms, no DB mutation); enabled repos dispatch a
        # read-only background audit in parallel with the review pipeline.
        try:
            _trigger = await audit_pipeline.get_auditor_trigger(db, repo.id)
            _decision = audit_pipeline.evaluate_pr(_trigger, action)
            if _decision.should_audit and _decision.audit_type:
                _audit_id = await audit_pipeline.dispatch_audit(
                    db=db,
                    repo_id=repo.id,
                    audit_type=_decision.audit_type,
                    repo_full_name=f"{owner}/{repo_name}",
                    delivery_id=x_github_delivery,
                    ref=head_branch or None,
                    pr_number=pr_number,
                    base_sha=base_sha,
                    head_sha=commit_sha,
                    settings_version=_trigger.settings_version,
                )
                logger.info(
                    "Auditor queued audit_id=%s type=%s repo=%s pr=%s delivery_id=%s",
                    _audit_id,
                    _decision.audit_type,
                    _log_repo(owner, repo_name),
                    sanitize_log_value(pr_number, 16),
                    x_github_delivery,
                )
            else:
                logger.info(
                    "Auditor skipped pull_request (delivery_id=%s): %s",
                    x_github_delivery,
                    _decision.reason,
                )
        except audit_pipeline.AuditDeliveryConflictError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Delivery conflicts with a previously recorded audit payload",
            )
        except Exception as exc:
            logger.warning(
                "Auditor trigger evaluation failed (pull_request): %s",
                type(exc).__name__,
            )

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

        await _record_webhook_delivery(
            db,
            event="pull_request",
            delivery_id=x_github_delivery,
            status_value="queued",
            reason=f"pr_number={pr_number} commit={commit_sha}",
            repo=f"{owner}/{repo_name}",
            repo_id=repo.id,
        )

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
            logger.info(
                "Ignored push (delivery_id=%s): tag push %s", x_github_delivery, ref
            )
            return {"status": "ignored", "reason": "tag push"}

        # Guard 2: Ignore deleted refs
        if data.get("deleted") is True:
            logger.info(
                "Ignored push (delivery_id=%s): deleted ref %s", x_github_delivery, ref
            )
            return {"status": "ignored", "reason": "deleted ref"}

        # Guard 3: Ignore bot commits
        sender = data.get("sender") or {}
        if sender.get("type") == "Bot" or sender.get("login", "").endswith("[bot]"):
            logger.info(
                "Ignored push (delivery_id=%s): bot sender %s",
                x_github_delivery,
                sender.get("login"),
            )
            return {"status": "ignored", "reason": "bot push"}

        head_commit = data.get("head_commit") or {}
        author = head_commit.get("author") or {}
        author_name = (author.get("name") or "").lower()
        if "haunter" in author_name or "[bot]" in author_name:
            logger.info(
                "Ignored push (delivery_id=%s): bot commit author %s",
                x_github_delivery,
                author_name,
            )
            return {"status": "ignored", "reason": "bot commit"}

        # Guard 4: Ignore pushes to Haunter fix branches
        branch_name = ref.replace("refs/heads/", "")
        if branch_name.startswith("haunter/"):
            logger.info(
                "Ignored push (delivery_id=%s): haunter fix branch %s",
                x_github_delivery,
                branch_name,
            )
            return {"status": "ignored", "reason": "haunter fix branch"}

        # Guard 5: Check valid commit SHA
        commit_sha = head_commit.get("id") or data.get("after")
        if not commit_sha or commit_sha == "0000000000000000000000000000000000000000":
            logger.info(
                "Ignored push (delivery_id=%s): empty or null commit SHA",
                x_github_delivery,
            )
            return {"status": "ignored", "reason": "empty commit sha"}

        # Check repository registration in DB
        repo_data = data.get("repository") or {}
        owner = repo_data.get("owner", {}).get("name") or repo_data.get(
            "owner", {}
        ).get("login")
        repo_name = repo_data.get("name")

        if not owner or not repo_name:
            logger.warning(
                "Ignored push (delivery_id=%s): missing repo owner/name in payload",
                x_github_delivery,
            )
            return {"status": "ignored", "reason": "invalid repository payload"}

        stmt = select(Repo).where(Repo.owner == owner, Repo.name == repo_name)
        result = await db.execute(stmt)
        repo = result.scalars().first()

        if not repo:
            logger.info(
                "Ignored webhook (delivery_id=%s): repository %s is not registered in Haunter",
                x_github_delivery,
                _log_repo(owner, repo_name),
            )
            return {"status": "ignored", "reason": "unregistered repository"}

        repo_settings = await get_repo_settings(db, repo.id)
        branch_decision = feature_enforcement.is_branch_allowed(
            branch_name, repo_settings.allowed_branches
        )
        if not branch_decision.allowed:
            logger.info(
                "Ignored push (delivery_id=%s): %s",
                x_github_delivery,
                branch_decision.reason,
            )
            return {"status": "skipped", "reason": branch_decision.reason}

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
                "Ignored duplicate push review webhook for repo %s commit %s (review_id=%s)",
                _log_repo(owner, repo_name),
                sanitize_log_value(commit_sha, 64),
                existing_review.id,
            )
            await _record_webhook_delivery(
                db,
                event="push",
                delivery_id=x_github_delivery,
                status_value="duplicate",
                reason=f"commit={commit_sha} review_id={existing_review.id}",
                repo=f"{owner}/{repo_name}",
                repo_id=repo.id,
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

        await _record_webhook_delivery(
            db,
            event="push",
            delivery_id=x_github_delivery,
            status_value="queued",
            reason=f"commit={commit_sha}",
            repo=f"{owner}/{repo_name}",
            repo_id=repo.id,
        )

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
        logger.info(
            "Ignored %s (delivery_id=%s): action=%s (expected 'created')",
            x_github_event,
            x_github_delivery,
            action,
        )
        return {"status": "ignored", "reason": f"unsupported action: {action}"}

    # 2. Schema validation
    pr_head_branch: Optional[str] = None
    pr_head_sha: Optional[str] = None
    pr_base_sha: Optional[str] = None
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
            logger.info(
                "Ignored issue_comment (delivery_id=%s): comment is on an issue, not a pull request",
                x_github_delivery,
            )
            return {"status": "ignored", "reason": "comment on issue, not pull request"}
        pr_number = comment_payload.issue.number
        comment_obj = comment_payload.comment
        repo_owner = comment_payload.repository.owner.login
        repo_name = comment_payload.repository.name
    else:  # pull_request_review_comment
        try:
            pr_comment_payload = PullRequestReviewCommentWebhookPayload.model_validate(
                data
            )
        except ValidationError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=e.errors(),
            )
        pr_number = pr_comment_payload.pull_request.number
        pr_head_branch = pr_comment_payload.pull_request.head.ref
        pr_head_sha = pr_comment_payload.pull_request.head.sha
        pr_base_sha = (
            pr_comment_payload.pull_request.base.sha
            if pr_comment_payload.pull_request.base is not None
            else None
        )
        comment_obj = pr_comment_payload.comment
        repo_owner = pr_comment_payload.repository.owner.login
        repo_name = pr_comment_payload.repository.name

    # 3. Mention Gate: comment body must contain @haunter (case-insensitive)
    comment_body = comment_obj.body or ""
    if "@haunter" not in comment_body.lower():
        logger.info(
            "Ignored %s (delivery_id=%s): no @haunter mention in comment",
            x_github_event,
            x_github_delivery,
        )
        return {"status": "ignored", "reason": "no @haunter mention"}

    # 4. Collaborator Authority: author_association must be in OWNER, MEMBER, COLLABORATOR
    author_assoc = (comment_obj.author_association or "").upper()
    if author_assoc not in ALLOWED_AUTHOR_ASSOCIATIONS:
        logger.warning(
            "Ignored @haunter mention (delivery_id=%s) from untrusted user %r with author_association=%r",
            x_github_delivery,
            sanitize_log_value(getattr(comment_obj.user, "login", "unknown"), 100),
            sanitize_log_value(author_assoc, 32),
        )
        return {"status": "ignored", "reason": "unauthorized commenter"}

    # 5. Check repository registration in DB
    stmt = select(Repo).where(Repo.owner == repo_owner, Repo.name == repo_name)
    result = await db.execute(stmt)
    repo = result.scalars().first()
    if not repo:
        logger.info(
            "Ignored webhook (delivery_id=%s): repository %s is not registered",
            x_github_delivery,
            _log_repo(repo_owner, repo_name),
        )
        return {"status": "ignored", "reason": "unregistered repository"}

    # 5b. Phase 5.1 Auditor Mode manual-mention filter (after HMAC
    # verification, mention gate, and collaborator check above).
    # `@haunter audit` dispatches a read-only background audit that works on
    # any PR branch (no haunter/* requirement, no parent Run required).
    # Disabled features exit here with no audit scheduled.
    #
    # A manual audit is a PR audit: it must carry the exact base and head SHAs
    # the request was made against. issue_comment payloads do not include pull
    # request endpoints at all, so those requests are refused rather than
    # resolved against whatever the PR head happens to be when the audit runs.
    _auditor_scheduled = False
    _auditor_skip_reason: Optional[str] = None
    _audit_id: Optional[str] = None
    _audit_type: Optional[str] = None
    try:
        _trigger = await audit_pipeline.get_auditor_trigger(db, repo.id)
        _decision = audit_pipeline.evaluate_manual_comment(
            _trigger, action, comment_body
        )
        if _decision.should_audit and _decision.audit_type:
            if not pr_base_sha or not pr_head_sha:
                _auditor_skip_reason = (
                    "manual audit requires pinned pull request endpoints, "
                    "which this event type does not provide"
                )
                logger.info(
                    "Auditor skipped %s (delivery_id=%s): %s",
                    x_github_event,
                    x_github_delivery,
                    _auditor_skip_reason,
                )
            else:
                _audit_id = await audit_pipeline.dispatch_audit(
                    db=db,
                    repo_id=repo.id,
                    audit_type=_decision.audit_type,
                    repo_full_name=f"{repo_owner}/{repo_name}",
                    delivery_id=x_github_delivery,
                    ref=pr_head_branch,
                    pr_number=pr_number,
                    base_sha=pr_base_sha,
                    head_sha=pr_head_sha,
                    settings_version=_trigger.settings_version,
                )
                _auditor_scheduled = True
                _audit_type = _decision.audit_type
                logger.info(
                    "Auditor queued audit_id=%s type=%s repo=%s pr=%s delivery_id=%s",
                    _audit_id,
                    _decision.audit_type,
                    _log_repo(repo_owner, repo_name),
                    sanitize_log_value(pr_number, 16),
                    x_github_delivery,
                )
        else:
            logger.info(
                "Auditor skipped %s (delivery_id=%s): %s",
                x_github_event,
                x_github_delivery,
                _decision.reason,
            )
    except audit_pipeline.AuditDeliveryConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Delivery conflicts with a previously recorded audit payload",
        )
    except Exception as exc:
        logger.warning(
            "Auditor trigger evaluation failed (%s): %s",
            x_github_event,
            type(exc).__name__,
        )

    # 6. Look up initial / parent Run for this PR
    run_stmt = (
        select(Run)
        .where(Run.repo_id == repo.id, Run.pr_number == pr_number)
        .order_by(Run.created_at.asc())
    )
    run_res = await db.execute(run_stmt)
    initial_run = run_res.scalars().first()

    if not initial_run:
        # A manual `@haunter audit` request is still served by the auditor
        # even when no fix-pipeline Run exists for this PR.
        if _auditor_scheduled:
            return {
                "status": "audit_queued",
                "audit_id": _audit_id,
                "audit_type": _audit_type,
                "repo": f"{repo_owner}/{repo_name}",
                "pr_number": pr_number,
                "comment_id": comment_obj.id,
                "delivery_id": x_github_delivery,
            }
        logger.info(
            "Ignored @haunter mention (delivery_id=%s): no matching Haunter run for %s PR #%d",
            x_github_delivery,
            _log_repo(repo_owner, repo_name),
            pr_number,
        )
        return {
            "status": "ignored",
            "reason": _auditor_skip_reason or "no matching run for PR",
        }

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
        # Manual audit requests are branch-agnostic (read-only); the
        # haunter/* guard applies only to the fix pipeline below.
        if _auditor_scheduled:
            return {
                "status": "audit_queued",
                "audit_id": _audit_id,
                "audit_type": _audit_type,
                "repo": f"{repo_owner}/{repo_name}",
                "pr_number": pr_number,
                "comment_id": comment_obj.id,
                "delivery_id": x_github_delivery,
            }
        logger.warning(
            "Ignored @haunter mention on non-haunter branch %r for %s PR #%d",
            sanitize_log_value(pr_head_branch, 255),
            _log_repo(repo_owner, repo_name),
            pr_number,
        )
        return {
            "status": "ignored",
            "reason": _auditor_skip_reason or "non-haunter branch",
        }

    # 8. Rate Limit Guard: Max 5 refinement iterations per PR
    count_stmt = (
        select(func.count()).select_from(Run).where(Run.parent_run_id == initial_run.id)
    )
    child_count = await db.scalar(count_stmt) or 0
    if child_count >= 5:
        logger.warning(
            "Rate limit reached for PR #%d (already has %d refinement runs)",
            pr_number,
            child_count,
        )
        limit_msg = "⚠️ Haunter PR refinement limit reached (max 5 iterations per PR)."
        try:
            repo_settings = await get_repo_settings(db, repo.id)
            if repo_settings.enable_pr_comments:
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
            else:
                logger.info(
                    "PR comments disabled for repo %s; suppressed refinement limit comment on PR #%d",
                    _log_repo(repo.owner, repo.name),
                    pr_number,
                )
        except Exception as exc:
            logger.warning(
                "Failed to post rate limit notice comment on %s PR #%d: %s",
                _log_repo(repo.owner, repo.name),
                pr_number,
                type(exc).__name__,
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
