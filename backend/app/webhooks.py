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
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.models import Repo, Run
from app.orchestrator import handle_failed_run
from app.schemas import WorkflowRunWebhookPayload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# 2MB payload size limit to prevent memory-exhaustion DoS attacks
MAX_PAYLOAD_SIZE_BYTES = 2 * 1024 * 1024


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

    # 5. Filter event type — ignore all events other than workflow_run with fast 200
    if x_github_event != "workflow_run":
        logger.info("Ignored webhook event: %s (delivery_id=%s)", x_github_event, x_github_delivery)
        return {"status": "ignored", "reason": f"unsupported event: {x_github_event}"}

    # 6. Parse and validate JSON schema
    try:
        data = json.loads(raw_body.decode("utf-8"))
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed JSON payload",
        )

    try:
        payload = WorkflowRunWebhookPayload.model_validate(data)
    except ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=e.errors(),
        )

    # 7. Filter action & conclusion: only completed + failure trigger the fix pipeline
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

    # 8. Cross-check repository registration in DB
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

    # 9. Idempotent Run creation backed by DB unique constraint
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

    # 10. Schedule pipeline asynchronously and return fast 2xx
    background_tasks.add_task(handle_failed_run, new_run.id)

    return {
        "status": "queued",
        "run_id": str(new_run.id),
        "github_run_id": new_run.github_run_id,
        "delivery_id": x_github_delivery,
    }
