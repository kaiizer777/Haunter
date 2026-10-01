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
7. Feature 8 health log + replay: the decision branches that reach a registered
   repository also append a webhook_deliveries row (see
   _record_webhook_delivery), and an authenticated owner of the affected repo can
   re-drive a stored delivery through this same handler (see
   replay_webhook_delivery). The log-only early rejections above it do not write
   rows — there is no repo to attribute them to, since a delivery for an
   unregistered repository belongs to no tenant.
"""

import base64
import binascii
import contextvars
import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
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
from sqlalchemy import func, select
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

# Replay buffer cap. Payloads larger than this are recorded with payload=NULL
# and report as not replayable: a truncated body cannot be re-validated, and
# replaying one would silently evaluate a different payload than the one GitHub
# signed.
_WEBHOOK_PAYLOAD_MAX_BYTES = 128 * 1024

# Replay cooldown. A second replay of the same delivery inside this window is
# refused with 409 + Retry-After so a double-click cannot enqueue the same work
# twice. The underlying decision is already idempotent (unique constraints on
# runs.github_run_id / github_delivery_id, audit_jobs delivery fingerprints);
# this closes the window before those constraints are even consulted.
_REPLAY_COOLDOWN_SECONDS = 30

# Set for the duration of one replay_webhook_delivery() call so every
# _record_webhook_delivery() executed inside the re-driven github_webhook()
# stamps its row with replay_of=<replayed row>. ContextVar, not a parameter:
# threading it through the handler would touch every decision branch for a
# concern that only replay has.
_replay_of_var: contextvars.ContextVar[Optional[uuid.UUID]] = contextvars.ContextVar(
    "haunter_webhook_replay_of", default=None
)

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


def _encode_replay_buffer(raw_body: Optional[bytes]) -> Optional[str]:
    """Base64 the verified raw body so replay re-verifies the exact same bytes.

    Base64 rather than a text column: HMAC verification runs over raw bytes,
    so any decode/encode round-trip that is not byte-exact would produce a
    signature mismatch on replay. Oversized bodies return None, which marks
    the row not replayable instead of storing a body that cannot be trusted.
    """
    if raw_body is None or len(raw_body) > _WEBHOOK_PAYLOAD_MAX_BYTES:
        return None
    return base64.b64encode(raw_body).decode("ascii")


def _decode_replay_buffer(payload: Optional[str]) -> Optional[bytes]:
    if payload is None:
        return None
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        logger.warning("webhook replay buffer is not valid base64; refusing replay")
        return None


async def _record_webhook_delivery(
    db: AsyncSession,
    *,
    event: Any,
    delivery_id: Any,
    status_value: str,
    reason: Any,
    repo: Any = None,
    repo_id: Optional[uuid.UUID] = None,
    payload: Optional[bytes] = None,
    level: str = "info",
) -> None:
    """Persist a webhook decision alongside the structured log line.

    Feature 8 health log: every call emits the CloudWatch-parseable
    _log_webhook_decision line AND best-effort inserts a WebhookDelivery row.
    DB failures are swallowed (rollback + warning) so ingestion latency and
    2xx responses are never affected by health-log pressure.

    `payload` is the raw body whose HMAC already verified; it is stored as the
    replay buffer only. The signature header is never accepted here, so no
    credential material can reach the table.

    Inside replay_webhook_delivery() this stamps replay_of automatically so the
    re-driven handler's own record is linked to the delivery it re-ran.
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
            payload=_encode_replay_buffer(payload),
            replay_of=_replay_of_var.get(),
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
    """Explicit response DTO — never leaks the replay buffer or any credential."""

    id: uuid.UUID
    event: str
    delivery_id: str
    status: str
    reason: Optional[str] = None
    repo: Optional[str] = None
    repo_id: Optional[uuid.UUID] = None
    created_at: datetime
    # Whether this row can be replayed (a verified replay buffer was retained).
    replayable: bool = False
    replay_of: Optional[uuid.UUID] = None

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_row(cls, row: WebhookDelivery) -> "WebhookDeliveryOut":
        return cls(
            id=row.id,
            event=row.event,
            delivery_id=row.delivery_id,
            status=row.status,
            reason=row.reason,
            repo=row.repo,
            repo_id=row.repo_id,
            created_at=row.created_at,
            replayable=row.payload is not None,
            replay_of=row.replay_of,
        )


class WebhookDeliveryListOut(BaseModel):
    deliveries: list[WebhookDeliveryOut]
    total: int

    model_config = ConfigDict(extra="forbid")


class WebhookReplayResultOut(BaseModel):
    """Result of re-driving one stored delivery through the live handler."""

    original_id: uuid.UUID
    # Row appended by the re-run. Null when the handler's decision path records
    # no health row (the log-only "ignored" branches), which is itself part of
    # the reported outcome.
    replay_id: Optional[uuid.UUID] = None
    delivery_id: str
    event: str
    # The exact body POST /webhooks/github returned for this re-run, so the UI
    # shows the decision that was reached rather than "replay succeeded".
    decision: dict[str, Any]
    replayed_at: datetime

    model_config = ConfigDict(extra="forbid")


def _owned_repo_ids(user_id: uuid.UUID):
    """Subquery of repo ids owned by `user_id` — the single tenant boundary.

    Takes the id, not the ORM `User`, because the replay path needs this
    subquery again AFTER github_webhook() has run, and that handler can commit
    or roll back the shared session — a rollback expires every instance in it,
    so re-reading `current_user.id` there would raise MissingGreenlet.

    Scoped on repo_id only. `WebhookDelivery.repo` is a denormalized
    owner/name string and is NOT a tenant boundary: repos permits two different
    users to register the same owner/name (uq_repo_user_owner_name is per user),
    so matching deliveries on that string would surface one tenant's webhook
    history to another. Keep this as a subquery rather than materialising the
    id list in Python so a user with many repos cannot inflate the IN clause.
    """
    return select(Repo.id).where(Repo.user_id == user_id).scalar_subquery()


def _repo_lookup_stmt(
    owner: Any, name: Any, replay_repo_id: Optional[uuid.UUID]
):
    """Resolve the Repo a delivery applies to.

    Live ingestion resolves by owner/name, which is the only repository identity
    GitHub's payload carries. Replay is different: the caller was already
    authorized against one specific `repos` row, and `repos` allows two users to
    register the SAME owner/name. An owner/name lookup during replay could
    therefore resolve to another tenant's Repo, and the resolved row is what
    selects repo settings, queues the audit/run/review, and receives the
    persisted row — a cross-tenant write. During replay the authorized id is
    therefore the constraint, so the decision cannot drift onto another
    registration of the same name.

    A replayed delivery whose repo has since been deleted resolves to None and
    takes the same "unregistered repository" branch a live delivery would.
    """
    if replay_repo_id is not None:
        return select(Repo).where(Repo.id == replay_repo_id)
    return select(Repo).where(Repo.owner == owner, Repo.name == name)


@router.get("/deliveries", response_model=WebhookDeliveryListOut)
async def list_webhook_deliveries(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    event: Annotated[Optional[str], Query(max_length=64)] = None,
) -> WebhookDeliveryListOut:
    """List webhook deliveries for repos owned by the caller (health history).

    Tenant isolation is enforced in SQL via repo_id -> repos.user_id. A caller
    with no repos gets an empty list (never 404) so the health tab renders its
    empty state instead of an error.
    """
    filters: list[Any] = [
        WebhookDelivery.repo_id.in_(_owned_repo_ids(current_user.id))
    ]
    if event is not None:
        filters.append(WebhookDelivery.event == event)

    total = (
        await db.execute(select(func.count(WebhookDelivery.id)).where(*filters))
    ).scalar_one()
    rows = (
        (
            await db.execute(
                select(WebhookDelivery)
                .where(*filters)
                .order_by(WebhookDelivery.created_at.desc(), WebhookDelivery.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return WebhookDeliveryListOut(
        deliveries=[WebhookDeliveryOut.from_row(r) for r in rows],
        total=total or 0,
    )


def _synthetic_github_request(payload: bytes) -> Request:
    """A minimal in-process Request carrying `payload` as the request body.

    Replay re-enters github_webhook() directly instead of reimplementing any
    of its logic, so the body has to arrive the same way it does on the wire:
    one stream, a Content-Length header for the early size check, and no body
    on any subsequent receive (Starlette's stream() stops at more_body=False).
    """
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/webhooks/github",
        "raw_path": b"/webhooks/github",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"localhost"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode("ascii")),
        ],
        "client": ("127.0.0.1", 0),
        "server": ("localhost", 80),
    }
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(scope, receive)


@router.post(
    "/deliveries/{delivery_row_id}/replay",
    response_model=WebhookReplayResultOut,
    status_code=status.HTTP_200_OK,
)
async def replay_webhook_delivery(
    delivery_row_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WebhookReplayResultOut:
    """Re-drive a stored webhook delivery through the live ingestion handler.

    Replay is not a reimplementation of the decision: it reconstructs the
    original request from the stored, already-HMAC-verified body and calls
    github_webhook() itself. Signature verification, registration guards, the
    feedback-loop branch guard, trigger evaluation, and the idempotency
    constraints all run again, so the outcome is the decision the live path
    would reach today — which is why enabling a disabled trigger and replaying
    an "ignored" delivery can legitimately queue work that did not run before.

    Safety properties:
      - Signature verification is NOT bypassed. The stored bytes are re-signed
        with the server's own GITHUB_WEBHOOK_SECRET and github_webhook()
        verifies that signature with the same hmac.compare_digest check the
        public endpoint uses. The secret never leaves the server, and no
        signature header was ever persisted.
      - Tenant scoping is enforced in SQL (repo_id -> repos.user_id) before any
        work happens; a non-owned row is a 404, indistinguishable from unknown,
        so there is no cross-tenant existence oracle. The authorized repo id is
        then pinned into the re-run via replay_repo_id, so the handler cannot
        resolve a different tenant's registration of the same owner/name.
      - Repeated calls are bounded: a second replay of the same row inside
        _REPLAY_COOLDOWN_SECONDS is refused with 409 + Retry-After, and the
        handler's own unique constraints make a replayed delivery land on
        "duplicate" instead of creating a second Run. Every accepted replay
        anchors a row with replay_of=<original>, including one that re-ran a
        branch the handler records nothing for, so the cooldown has an anchor on
        every path.
    """
    original = (
        await db.execute(
            select(WebhookDelivery).where(
                WebhookDelivery.id == delivery_row_id,
                WebhookDelivery.repo_id.in_(_owned_repo_ids(current_user.id)),
            )
        )
    ).scalar_one_or_none()
    if original is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Delivery not found"
        )

    # Snapshot every value we still need into locals before re-entering the
    # handler. github_webhook() may commit or roll back the shared session, and
    # a rollback expires every instance in it — re-reading `original.<col>`
    # afterwards would raise MissingGreenlet instead of returning the decision.
    original_id = original.id
    original_event = original.event
    original_delivery_id = original.delivery_id
    original_repo = original.repo
    original_repo_id = original.repo_id
    current_user_id = current_user.id

    payload = _decode_replay_buffer(original.payload)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This delivery cannot be replayed — its payload was too large to "
                "retain, so the decision cannot be re-evaluated."
            ),
        )

    webhook_secret = settings.github_webhook_secret
    if not webhook_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook replay unavailable: GITHUB_WEBHOOK_SECRET is not configured",
        )

    cooldown_floor = datetime.now(timezone.utc) - timedelta(seconds=_REPLAY_COOLDOWN_SECONDS)
    recent_replay = (
        await db.execute(
            select(WebhookDelivery.id).where(
                WebhookDelivery.replay_of == original_id,
                WebhookDelivery.created_at >= cooldown_floor,
            )
        )
    ).first()
    if recent_replay is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            # FastAPI builds a fresh JSONResponse from the exception, so a
            # header set on the injected Response would be discarded — the
            # Retry-After contract has to ride on the exception itself.
            headers={"Retry-After": str(_REPLAY_COOLDOWN_SECONDS)},
            detail=(
                "This delivery was already replayed in the last "
                f"{_REPLAY_COOLDOWN_SECONDS}s."
            ),
        )

    # Re-sign the stored bytes so github_webhook() runs its real HMAC check.
    # This is the server authenticating its own verified payload — not a forged
    # GitHub request, and not a bypass: verification still executes and would
    # still fail on any byte that differs from what was stored.
    signature = "sha256=" + hmac.new(
        webhook_secret.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()

    token = _replay_of_var.set(original_id)
    try:
        decision = await github_webhook(
            request=_synthetic_github_request(payload),
            background_tasks=background_tasks,
            db=db,
            x_github_delivery=original_delivery_id,
            x_github_event=original_event,
            x_hub_signature_256=signature,
            replay_repo_id=original_repo_id,
        )
    finally:
        _replay_of_var.reset(token)

    # Scoped to the caller for the same reason the row lookup above is: github_webhook()
    # resolves Repo by owner/name alone, so a re-run can legitimately land on a
    # different tenant's registration of the same owner/name. Without this scope
    # the reported replay_id could name a row the caller cannot see.
    replay_row = (
        await db.execute(
            select(WebhookDelivery)
            .where(
                WebhookDelivery.replay_of == original_id,
                WebhookDelivery.repo_id.in_(_owned_repo_ids(current_user_id)),
            )
            .order_by(WebhookDelivery.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if replay_row is None:
        # The re-run took a decision branch that records no health row of its own
        # (an unregistered repository, a bot PR, a tag push). Anchor the attempt
        # so an accepted replay is always visible in the health log AND the
        # cooldown above always has a row to find on the next call. Without this
        # an ignored-branch delivery could be replayed unboundedly, because
        # nothing would ever satisfy `replay_of == original_id`.
        #
        # The replay ContextVar was already reset when the handler returned, so
        # re-arm it here or the anchor row lands with replay_of=NULL.
        anchor_token = _replay_of_var.set(original_id)
        try:
            await _record_webhook_delivery(
                db,
                event=original_event,
                delivery_id=original_delivery_id,
                status_value="replayed",
                reason=f"replay of {original_id}",
                repo=original_repo,
                repo_id=original_repo_id,
            )
        finally:
            _replay_of_var.reset(anchor_token)

        replay_row = (
            await db.execute(
                select(WebhookDelivery)
                .where(
                    WebhookDelivery.replay_of == original_id,
                    WebhookDelivery.repo_id.in_(_owned_repo_ids(current_user_id)),
                )
                .order_by(WebhookDelivery.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    _log_webhook_decision(
        event=original_event,
        delivery_id=original_delivery_id,
        status="replayed",
        reason=f"replay of {original_id}",
        repo=original_repo,
    )

    return WebhookReplayResultOut(
        original_id=original_id,
        replay_id=replay_row.id if replay_row is not None else None,
        delivery_id=original_delivery_id,
        event=original_event,
        decision=decision,
        replayed_at=datetime.now(timezone.utc),
    )


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
    replay_repo_id: Optional[uuid.UUID] = None,
) -> dict[str, Any]:
    """
    Ingest GitHub webhook events. Public endpoint secured exclusively via HMAC-SHA256.

    `replay_repo_id` is set only by replay_webhook_delivery(), never by a real
    GitHub request. It pins the Repo lookup to the row the caller was authorized
    against so a replay cannot drift onto another tenant's registration of the
    same owner/name — see _repo_lookup_stmt.
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

        stmt = _repo_lookup_stmt(owner, repo_name, replay_repo_id)
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
        # repo.id is read BEFORE the commit: db.rollback() in the duplicate
        # handler expires every instance in the session, and re-reading an
        # expired ORM attribute outside a greenlet context raises
        # MissingGreenlet — which would turn a duplicate delivery into a 500.
        repo_id = repo.id
        new_run = Run(
            repo_id=repo_id,
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
                repo_id=repo_id,
                payload=raw_body,
            )
            return {
                "status": "duplicate",
                "delivery_id": x_github_delivery,
                "github_run_id": payload.workflow_run.id,
            }

        from app.adapters.hosting import get_hosting_adapter

        adapter = await get_hosting_adapter()
        await adapter.schedule_pipeline(new_run.id, background_tasks)

        # Snapshot before the recorder: its failure path rolls the session back,
        # and a rollback expires every instance in it, so reading new_run.<attr>
        # afterwards would raise MissingGreenlet and turn an already-scheduled
        # run into a 500.
        queued_run_id = str(new_run.id)
        queued_github_run_id = new_run.github_run_id

        await _record_webhook_delivery(
            db,
            event="workflow_run",
            delivery_id=x_github_delivery,
            status_value="queued",
            reason=f"github_run_id={payload.workflow_run.id} run_id={new_run.id}",
            repo=f"{owner}/{repo_name}",
            repo_id=repo.id,
            payload=raw_body,
        )

        return {
            "status": "queued",
            "run_id": queued_run_id,
            "github_run_id": queued_github_run_id,
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

        stmt = _repo_lookup_stmt(owner, repo_name, replay_repo_id)
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
                payload=raw_body,
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
            # Snapshotted before the recorder for the same rollback/expire reason
            # as the queued branch below.
            duplicate_review_id = str(existing_review.id)
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
                payload=raw_body,
            )
            return {
                "status": "duplicate",
                "review_id": duplicate_review_id,
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

        # Snapshotted before the recorder — see the workflow_run branch.
        queued_review_id = str(new_review.id)

        await _record_webhook_delivery(
            db,
            event="pull_request",
            delivery_id=x_github_delivery,
            status_value="queued",
            reason=f"pr_number={pr_number} commit={commit_sha}",
            repo=f"{owner}/{repo_name}",
            repo_id=repo.id,
            payload=raw_body,
        )

        return {
            "status": "queued",
            "review_id": queued_review_id,
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

        stmt = _repo_lookup_stmt(owner, repo_name, replay_repo_id)
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
            # Snapshotted before the recorder — see the workflow_run branch.
            duplicate_review_id = str(existing_review.id)
            await _record_webhook_delivery(
                db,
                event="push",
                delivery_id=x_github_delivery,
                status_value="duplicate",
                reason=f"commit={commit_sha} review_id={existing_review.id}",
                repo=f"{owner}/{repo_name}",
                repo_id=repo.id,
                payload=raw_body,
            )
            return {
                "status": "duplicate",
                "review_id": duplicate_review_id,
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

        # Snapshotted before the recorder — see the workflow_run branch.
        queued_review_id = str(new_review.id)

        await _record_webhook_delivery(
            db,
            event="push",
            delivery_id=x_github_delivery,
            status_value="queued",
            reason=f"commit={commit_sha}",
            repo=f"{owner}/{repo_name}",
            repo_id=repo.id,
            payload=raw_body,
        )

        return {
            "status": "queued",
            "review_id": queued_review_id,
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
    stmt = _repo_lookup_stmt(repo_owner, repo_name, replay_repo_id)
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
    # comment_obj.id is read BEFORE the commit for the same reason as the
    # workflow_run branch above: db.rollback() expires every instance in the
    # session, and re-reading an expired ORM attribute outside a greenlet
    # context raises MissingGreenlet instead of returning the 200 duplicate.
    comment_id = comment_obj.id
    new_run = Run(
        repo_id=repo.id,
        parent_run_id=initial_run.id,
        github_run_id=comment_id,
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
            comment_id,
        )
        return {
            "status": "duplicate",
            "delivery_id": x_github_delivery,
            "comment_id": comment_id,
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
