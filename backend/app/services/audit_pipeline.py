"""Auditor trigger evaluation, durable delivery claims, and bounded workers."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import random
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Literal, Optional, cast

from sqlalchemy import case, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.github.auditor import get_auditor_installation_token
from app.models import AuditJob, Repo, RepoSettings
from app.self_invocation import (
    KIND_AUDIT,
    SelfInvocationError,
    resolve_self_invocation_secret,
    self_invocation_token,
    verify_self_invocation,
)

logger = logging.getLogger(__name__)

AuditType = Literal["pr_audit", "ci_failure_audit", "ci_success_audit", "manual_audit"]

AUDIT_PR_ACTIONS = frozenset({"opened", "synchronize"})
AUDIT_WORKFLOW_CONCLUSIONS = frozenset({"failure", "success"})
AUDIT_TYPES = frozenset(
    {"pr_audit", "ci_failure_audit", "ci_success_audit", "manual_audit"}
)

_MANUAL_MENTION = "@haunter"
_MANUAL_AUDIT_WORD_RE = re.compile(r"\baudit\b", re.IGNORECASE)
_AUDIT_ID_RE = re.compile(r"^audit-[0-9a-f]{12}$", re.ASCII)
_REPO_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,255}$", re.ASCII)
_DELIVERY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$", re.ASCII)
_FENCE_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$", re.ASCII)
_SIGNATURE_RE = re.compile(r"^sha256=[0-9a-fA-F]{64}$", re.ASCII)
_MAX_PR_NUMBER = 2_147_483_647
_MAX_WORKFLOW_RUN_ID = 9_223_372_036_854_775_807
AUDIT_JOB_TIMEOUT_S = 180.0
AUDIT_TOKEN_TIMEOUT_S = 15.0
AUDIT_DISPATCH_LEASE_S = 30.0
AUDIT_PROCESSING_LEASE_S = AUDIT_JOB_TIMEOUT_S + 60.0
AUDIT_MAX_PROCESSING_ATTEMPTS = 3
AUDIT_MAX_DISPATCH_ATTEMPTS = 5
_MAX_DISPATCH_ATTEMPTS = 1000
AUDIT_RETRY_BASE_S = 2.0
AUDIT_RETRY_MAX_S = 300.0
AUDIT_DISPATCH_BATCH_SIZE = 10
AUDIT_DISPATCH_WORKER_COUNT = 2
AUDIT_DISPATCH_POLL_S = 1.0
#: `audit_jobs.last_error` recorded when the per-repo kill switch refuses a job.
#: Bounded by the column width and never carries attacker-controlled text.
AUDITOR_DISABLED_ERROR = "AuditorDisabled"

_audit_dispatch_workers: set[asyncio.Task[None]] = set()


@dataclass(frozen=True)
class AuditorTriggerSettings:
    enabled: bool = False
    on_pr: bool = True
    on_ci_failure: bool = True
    on_ci_success: bool = False
    on_manual_mention: bool = True
    settings_version: int = 1


DEFAULT_AUDITOR_TRIGGER = AuditorTriggerSettings()


def default_auditor_trigger() -> AuditorTriggerSettings:
    return DEFAULT_AUDITOR_TRIGGER


@dataclass(frozen=True)
class AuditDecision:
    should_audit: bool
    reason: str
    audit_type: Optional[AuditType] = None


class AuditDeliveryConflictError(RuntimeError):
    """A delivery id was replayed with a different audit payload."""


class ProcessingClaimOutcome(StrEnum):
    """Terminal claim outcomes that are not "this child may run the job".

    `AUDITOR_DISABLED` is a successful claim of a *refusal*: the fence verified
    and the job is this attempt's, but the persisted per-repo trigger says the
    auditor is off (or its `RepoSettings` row is gone), so the job is closed out
    terminally and no credential is fetched and no model is called. It is kept
    distinct from `None`, which means "this caller is not the current dispatch
    attempt" and must leave the job alone entirely.
    """

    AUDITOR_DISABLED = "auditor_disabled"


@dataclass(frozen=True)
class AuditDeliveryClaim:
    audit_id: str
    claimed: bool


@dataclass(frozen=True)
class AuditDispatchClaim:
    audit_id: str
    dispatch_attempts: int
    dispatch_fence_token: str


@dataclass(frozen=True)
class AuditDispatchSummary:
    recovered: int = 0
    claimed: int = 0
    scheduled: int = 0
    released: int = 0
    terminal: int = 0


@dataclass(frozen=True)
class AuditTarget:
    base_sha: Optional[str]
    head_sha: str


@dataclass(frozen=True)
class AuditExecutionOutcome:
    status: Literal["completed", "skipped_no_diff"]
    result: Any = None


def verify_github_signature(
    raw_body: bytes,
    signature_header: Optional[str],
    secret: Optional[str],
) -> bool:
    if not isinstance(raw_body, (bytes, bytearray)):
        return False
    if not isinstance(secret, str) or not secret:
        return False
    if not isinstance(signature_header, str) or not _SIGNATURE_RE.fullmatch(
        signature_header
    ):
        return False
    received_hex = signature_header.removeprefix("sha256=")
    expected_hex = hmac.new(
        secret.encode("utf-8"),
        bytes(raw_body),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected_hex, received_hex)


def audit_child_invocation_token(
    audit_id: str,
    dispatch_fence_token: str,
    secret: Optional[str] = None,
) -> str:
    """Mint the HMAC that authenticates one audit child invocation.

    The token is bound to both the audit id and the per-attempt dispatch fence
    so a child invoked by an earlier dispatch attempt can never authenticate
    as the current attempt.
    """
    if not isinstance(audit_id, str) or not _AUDIT_ID_RE.fullmatch(audit_id):
        raise ValueError("audit id is invalid")
    fence = _validate_fence_token(dispatch_fence_token)
    return self_invocation_token(
        KIND_AUDIT,
        f"{audit_id}:{fence}",
        resolve_self_invocation_secret(secret),
    )


def verify_audit_self_invocation(
    audit_id: str,
    dispatch_fence_token: Optional[str],
    token: Optional[str],
    secret: Optional[str] = None,
) -> bool:
    """Fail closed: any missing secret, malformed fence, or mismatch is False."""
    if not isinstance(audit_id, str) or not _AUDIT_ID_RE.fullmatch(audit_id):
        return False
    try:
        fence = _validate_fence_token(dispatch_fence_token)
        resolved_secret = resolve_self_invocation_secret(secret)
    except Exception:  # noqa: BLE001 — a verifier that cannot verify must return False
        # Resolving the secret is itself part of the verification. An
        # unconfigured or unimportable secret is an authentication failure,
        # never an exception for the caller to handle, and never a reason to
        # fall open.
        return False
    return verify_self_invocation(
        KIND_AUDIT,
        f"{audit_id}:{fence}",
        token,
        resolved_secret,
    )


def _fence_message(audit_id: str, dispatch_attempts: int) -> str:
    if not isinstance(audit_id, str) or not _AUDIT_ID_RE.fullmatch(audit_id):
        raise ValueError("audit id is invalid")
    if (
        isinstance(dispatch_attempts, bool)
        or not isinstance(dispatch_attempts, int)
        or not 1 <= dispatch_attempts <= _MAX_DISPATCH_ATTEMPTS
    ):
        raise ValueError("dispatch attempt is invalid")
    return f"{audit_id}:{dispatch_attempts}"


def audit_dispatch_fence_token(
    audit_id: str,
    dispatch_attempts: int,
    secret: Optional[str] = None,
) -> str:
    """Mint the per-attempt dispatch fence stored on the job row."""
    return hmac.new(
        resolve_self_invocation_secret(secret).encode("utf-8"),
        f"fence:{_fence_message(audit_id, dispatch_attempts)}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_audit_dispatch_fence(
    audit_id: str,
    dispatch_attempts: int,
    token: Optional[str],
    secret: Optional[str] = None,
) -> bool:
    if not isinstance(token, str) or not _FENCE_TOKEN_RE.fullmatch(token):
        return False
    try:
        expected = audit_dispatch_fence_token(audit_id, dispatch_attempts, secret)
    except (TypeError, ValueError, SelfInvocationError, RuntimeError):
        return False
    return hmac.compare_digest(expected, token)


def is_pr_audit_event(action: Optional[str]) -> bool:
    return action in AUDIT_PR_ACTIONS


def is_workflow_audit_conclusion(conclusion: Optional[str]) -> bool:
    return conclusion in AUDIT_WORKFLOW_CONCLUSIONS


def is_manual_audit_comment(body: Optional[str]) -> bool:
    if not body:
        return False
    return (
        _MANUAL_MENTION in body.lower()
        and _MANUAL_AUDIT_WORD_RE.search(body) is not None
    )


def evaluate_pr(
    trigger: AuditorTriggerSettings,
    action: Optional[str],
) -> AuditDecision:
    if not trigger.enabled:
        return AuditDecision(False, "auditor disabled")
    if not trigger.on_pr:
        return AuditDecision(False, "pr trigger disabled")
    if not is_pr_audit_event(action):
        return AuditDecision(False, f"unsupported PR action: {action}")
    return AuditDecision(True, "pr trigger matched", "pr_audit")


def evaluate_workflow_run(
    trigger: AuditorTriggerSettings,
    action: Optional[str],
    conclusion: Optional[str],
) -> AuditDecision:
    if not trigger.enabled:
        return AuditDecision(False, "auditor disabled")
    if action != "completed":
        return AuditDecision(False, f"unsupported workflow_run action: {action}")
    if conclusion == "failure":
        if not trigger.on_ci_failure:
            return AuditDecision(False, "ci-failure trigger disabled")
        return AuditDecision(True, "ci-failure trigger matched", "ci_failure_audit")
    if conclusion == "success":
        if not trigger.on_ci_success:
            return AuditDecision(False, "ci-success trigger disabled")
        return AuditDecision(True, "ci-success trigger matched", "ci_success_audit")
    return AuditDecision(
        False, f"unsupported workflow_run conclusion: {conclusion}"
    )


def evaluate_manual_comment(
    trigger: AuditorTriggerSettings,
    action: Optional[str],
    body: Optional[str],
) -> AuditDecision:
    if not trigger.enabled:
        return AuditDecision(False, "auditor disabled")
    if not trigger.on_manual_mention:
        return AuditDecision(False, "manual-mention trigger disabled")
    if action != "created":
        return AuditDecision(False, f"unsupported comment action: {action}")
    if not is_manual_audit_comment(body):
        return AuditDecision(False, "no @haunter audit request")
    return AuditDecision(True, "manual-mention trigger matched", "manual_audit")


def should_audit_event(
    trigger: AuditorTriggerSettings,
    github_event: str,
    payload: dict[str, Any],
) -> AuditDecision:
    if github_event == "pull_request":
        return evaluate_pr(trigger, payload.get("action"))
    if github_event == "workflow_run":
        run = payload.get("workflow_run") or {}
        return evaluate_workflow_run(
            trigger,
            payload.get("action"),
            run.get("conclusion"),
        )
    if github_event in ("issue_comment", "pull_request_review_comment"):
        comment = payload.get("comment") or {}
        return evaluate_manual_comment(
            trigger,
            payload.get("action"),
            comment.get("body"),
        )
    return AuditDecision(False, f"unsupported event: {github_event}")


def _valid_trigger_row(row: Optional[RepoSettings]) -> AuditorTriggerSettings:
    if row is None:
        return DEFAULT_AUDITOR_TRIGGER
    values = {
        "enabled": row.enable_auditor_mode,
        "on_pr": row.audit_trigger_on_pr,
        "on_ci_failure": row.audit_trigger_on_ci_failure,
        "on_ci_success": row.audit_trigger_on_ci_success,
        "on_manual_mention": row.audit_trigger_on_manual_mention,
    }
    if any(type(value) is not bool for value in values.values()):
        return DEFAULT_AUDITOR_TRIGGER
    version = getattr(row, "settings_version", None)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return AuditorTriggerSettings(**values)
    return AuditorTriggerSettings(**values, settings_version=version)


async def get_auditor_trigger(
    db: Optional[AsyncSession] = None,
    repo_id: Any | None = None,
) -> AuditorTriggerSettings:
    if db is None or repo_id is None:
        return DEFAULT_AUDITOR_TRIGGER
    try:
        row = await db.scalar(
            select(RepoSettings).where(RepoSettings.repo_id == repo_id)
        )
        return _valid_trigger_row(row)
    except Exception as exc:
        logger.warning(
            "auditor trigger lookup failed closed error_type=%s",
            type(exc).__name__,
        )
        try:
            await db.rollback()
        except Exception:
            pass
        return DEFAULT_AUDITOR_TRIGGER


def _validate_repo_full_name(value: str) -> tuple[str, str]:
    if not isinstance(value, str):
        raise ValueError("repository must be owner/name")
    owner, separator, name = value.partition("/")
    if (
        not separator
        or "/" in name
        or not _REPO_COMPONENT_RE.fullmatch(owner)
        or not _REPO_COMPONENT_RE.fullmatch(name)
    ):
        raise ValueError("repository must be owner/name")
    return owner, name


def _validate_optional_ref(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 255
        or any(
            character.isspace()
            or unicodedata.category(character).startswith("C")
            for character in value
        )
    ):
        raise ValueError("ref is invalid")
    return value


def _validate_optional_pr(value: Optional[int]) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_PR_NUMBER:
        raise ValueError("pull request number is invalid")
    return value


def _validate_optional_sha(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise ValueError("head SHA is invalid")
    return value.lower()


def _validate_fence_token(value: Optional[str]) -> str:
    if not isinstance(value, str) or not _FENCE_TOKEN_RE.fullmatch(value):
        raise ValueError("dispatch fence token is invalid")
    return value


def _validate_settings_version(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("settings version is invalid")
    if value > 2_147_483_647:
        raise ValueError("settings version is invalid")
    return value


def _validate_optional_run_id(value: Optional[int]) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_WORKFLOW_RUN_ID:
        raise ValueError("workflow run id is invalid")
    return value


def audit_delivery_fingerprint(
    *,
    repo_id: Any,
    audit_type: str,
    repo_full_name: str,
    ref: Optional[str],
    pr_number: Optional[int],
    base_sha: Optional[str],
    head_sha: Optional[str],
    workflow_run_id: Optional[int],
    settings_version: int,
) -> str:
    """Canonical SHA-256 identity of an audit payload.

    A delivery id alone is not an idempotency key: GitHub can (and does) reuse
    a delivery id, and a replay with different content must never resolve to the
    previously persisted job. The fingerprint binds repo, type, target, both
    comparison endpoints, and the settings version that decided the trigger, so
    a replay is either an exact duplicate or a hard conflict.
    """
    canonical = json.dumps(
        {
            "audit_type": audit_type,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "pr_number": pr_number,
            "ref": ref,
            "repo_full_name": repo_full_name,
            "repo_id": str(repo_id),
            "settings_version": settings_version,
            "workflow_run_id": workflow_run_id,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


async def claim_audit_delivery(
    *,
    db: AsyncSession,
    repo_id: Any,
    audit_type: AuditType,
    repo_full_name: str,
    delivery_id: str,
    ref: Optional[str] = None,
    pr_number: Optional[int] = None,
    base_sha: Optional[str] = None,
    head_sha: Optional[str] = None,
    workflow_run_id: Optional[int] = None,
    settings_version: int = 1,
) -> AuditDeliveryClaim:
    _validate_repo_full_name(repo_full_name)
    if audit_type not in AUDIT_TYPES:
        raise ValueError("audit type is invalid")
    if not isinstance(delivery_id, str) or not _DELIVERY_ID_RE.fullmatch(delivery_id):
        raise ValueError("delivery id is invalid")
    validated_ref = _validate_optional_ref(ref)
    validated_pr = _validate_optional_pr(pr_number)
    validated_base_sha = _validate_optional_sha(base_sha)
    validated_head_sha = _validate_optional_sha(head_sha)
    validated_settings_version = _validate_settings_version(settings_version)
    if validated_pr is not None and (
        validated_base_sha is None or validated_head_sha is None
    ):
        raise ValueError("pull request audit requires base and head SHAs")
    validated_run_id = _validate_optional_run_id(workflow_run_id)
    fingerprint = audit_delivery_fingerprint(
        repo_id=repo_id,
        audit_type=audit_type,
        repo_full_name=repo_full_name,
        ref=validated_ref,
        pr_number=validated_pr,
        base_sha=validated_base_sha,
        head_sha=validated_head_sha,
        workflow_run_id=validated_run_id,
        settings_version=validated_settings_version,
    )
    audit_id = f"audit-{uuid.uuid4().hex[:12]}"
    statement = (
        pg_insert(AuditJob)
        .values(
            audit_id=audit_id,
            repo_id=repo_id,
            delivery_id=delivery_id,
            delivery_fingerprint=fingerprint,
            settings_version=validated_settings_version,
            audit_type=audit_type,
            ref=validated_ref,
            pr_number=validated_pr,
            base_sha=validated_base_sha,
            head_sha=validated_head_sha,
            workflow_run_id=validated_run_id,
            status="queued",
        )
        .on_conflict_do_nothing(constraint="uq_audit_jobs_delivery_id")
        .returning(AuditJob.audit_id)
    )
    try:
        result = await db.execute(statement)
        inserted_audit_id = result.scalar_one_or_none()
        if inserted_audit_id is not None:
            await db.commit()
            return AuditDeliveryClaim(audit_id=inserted_audit_id, claimed=True)
        existing = (
            await db.execute(
                select(AuditJob.audit_id, AuditJob.delivery_fingerprint).where(
                    AuditJob.delivery_id == delivery_id
                )
            )
        ).one_or_none()
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    if existing is None or not isinstance(existing.audit_id, str):
        raise RuntimeError("durable audit claim could not be resolved")
    if not isinstance(existing.delivery_fingerprint, str) or not hmac.compare_digest(
        existing.delivery_fingerprint, fingerprint
    ):
        raise AuditDeliveryConflictError(
            f"delivery_id={delivery_id} was replayed with a different audit payload"
        )
    return AuditDeliveryClaim(audit_id=existing.audit_id, claimed=False)


async def dispatch_audit(
    *,
    db: AsyncSession,
    repo_id: Any,
    audit_type: AuditType,
    repo_full_name: str,
    delivery_id: str,
    ref: Optional[str] = None,
    pr_number: Optional[int] = None,
    base_sha: Optional[str] = None,
    head_sha: Optional[str] = None,
    workflow_run_id: Optional[int] = None,
    settings_version: int = 1,
) -> str:
    claim = await claim_audit_delivery(
        db=db,
        repo_id=repo_id,
        audit_type=audit_type,
        repo_full_name=repo_full_name,
        delivery_id=delivery_id,
        ref=ref,
        pr_number=pr_number,
        base_sha=base_sha,
        head_sha=head_sha,
        workflow_run_id=workflow_run_id,
        settings_version=settings_version,
    )
    if not claim.claimed:
        logger.info(
            "audit duplicate delivery_id=%s audit_id=%s",
            delivery_id,
            claim.audit_id,
        )
    return claim.audit_id


async def _fetch_pr_target(
    *,
    repo: Repo,
    pr_number: int,
    token: Optional[str],
    expected_base_sha: Optional[str],
    expected_head_sha: Optional[str],
    enforce_expected: bool,
) -> AuditTarget:
    from app import github_client as github
    from app.subagents import auditor

    try:
        metadata = await asyncio.wait_for(
            github.fetch_pull_request(
                owner=repo.owner,
                repo=repo.name,
                pr_number=pr_number,
                token=token,
                allow_global_token=False,
            ),
            timeout=auditor.AUDIT_FETCH_TIMEOUT_S,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        reason = auditor.classify_audit_remote_failure(exc)
        logger.warning(
            "audit pull_request_target_fetch_failed repo_id=%s pr=%s reason=%s error_type=%s",
            repo.id,
            pr_number,
            reason.value,
            type(exc).__name__,
        )
        raise auditor.AuditTargetFetchError(
            reason,
            f"pull request target fetch failed ({reason.value})",
        ) from exc
    try:
        base = metadata["base"]["sha"]
        head = metadata["head"]["sha"]
        resolved_base = _validate_optional_sha(base)
        resolved_head = _validate_optional_sha(head)
    except (KeyError, TypeError, ValueError) as exc:
        raise auditor.AuditTargetFetchError(
            auditor.AuditRemoteFailureReason.UNKNOWN,
            "pull request target response was malformed",
        ) from exc
    if resolved_base is None or resolved_head is None:
        raise auditor.AuditTargetFetchError(
            auditor.AuditRemoteFailureReason.UNKNOWN,
            "pull request target omitted immutable SHAs",
        )
    if enforce_expected and (
        (expected_base_sha is not None and resolved_base != expected_base_sha)
        or (expected_head_sha is not None and resolved_head != expected_head_sha)
    ):
        raise auditor.AuditTargetChangedError()
    return AuditTarget(base_sha=resolved_base, head_sha=resolved_head)


async def _resolve_audit_target(
    *,
    repo: Repo,
    pr_number: Optional[int],
    base_sha: Optional[str],
    head_sha: Optional[str],
    token: Optional[str],
) -> AuditTarget:
    if pr_number is None:
        resolved_head = _validate_optional_sha(head_sha)
        if resolved_head is None:
            raise ValueError("stored audit head SHA is missing")
        return AuditTarget(base_sha=None, head_sha=resolved_head)
    # Every PR-number audit — including manual_audit, issue_comment, and
    # pull_request_review_comment deliveries — is pinned at intake. Adopting
    # whatever the PR head happens to be at execution time would audit a
    # different commit than the one the trigger was evaluated against.
    return await _fetch_pr_target(
        repo=repo,
        pr_number=pr_number,
        token=token,
        expected_base_sha=base_sha,
        expected_head_sha=head_sha,
        enforce_expected=True,
    )


async def _pin_audit_target(
    audit_id: str,
    attempt: int,
    target: AuditTarget,
) -> bool:
    from app.db import async_session_maker

    async with async_session_maker() as db:
        try:
            result = await db.execute(
                update(AuditJob)
                .where(
                    AuditJob.audit_id == audit_id,
                    AuditJob.status == "running",
                    AuditJob.attempts == attempt,
                )
                .values(
                    base_sha=target.base_sha,
                    head_sha=target.head_sha,
                )
                .returning(AuditJob.audit_id)
            )
            updated_id = result.scalar_one_or_none()
            await db.commit()
            return updated_id is not None
        except Exception:
            await db.rollback()
            raise


async def execute_audit_job(
    *,
    audit_id: str,
    audit_type: str,
    repo: Repo,
    ref: Optional[str],
    pr_number: Optional[int],
    base_sha: Optional[str],
    head_sha: Optional[str],
    workflow_run_id: Optional[int],
    token: Optional[str],
    attempt: Optional[int] = None,
) -> AuditExecutionOutcome:
    from app.subagents import auditor

    if audit_type not in AUDIT_TYPES:
        raise ValueError("stored audit type is invalid")
    resolved_audit_type = cast(AuditType, audit_type)
    owner, name = _validate_repo_full_name(f"{repo.owner}/{repo.name}")
    resolved_pr = _validate_optional_pr(pr_number)
    target = await _resolve_audit_target(
        repo=repo,
        pr_number=resolved_pr,
        base_sha=base_sha,
        head_sha=head_sha,
        token=token,
    )
    if attempt is not None and not await _pin_audit_target(
        audit_id,
        attempt,
        target,
    ):
        raise auditor.AuditTargetChangedError()
    diff_text = await auditor.fetch_audit_diff(
        owner=owner,
        repo=name,
        pr_number=resolved_pr,
        base_sha=target.base_sha,
        head_sha=target.head_sha,
        token=token,
    )
    if not isinstance(diff_text, str):
        # A client that answered a diff request with something that is not text
        # has not told us the commit has no changes. Closing the job as
        # `skipped_no_diff` here would record a terminal success for a target
        # nobody ever read.
        raise auditor.AuditDiffFetchError(
            auditor.AuditRemoteFailureReason.UNKNOWN,
            "audited diff fetch returned a non-text response",
        )
    if not diff_text.strip():
        # Reached only after a fetch that returned 2xx with an empty body:
        # `fetch_audit_diff` raises a typed error for every non-success status,
        # so "no changes" here means GitHub was asked and reported none.
        return AuditExecutionOutcome(status="skipped_no_diff")
    if resolved_pr is not None:
        await _fetch_pr_target(
            repo=repo,
            pr_number=resolved_pr,
            token=token,
            expected_base_sha=target.base_sha,
            expected_head_sha=target.head_sha,
            enforce_expected=True,
        )
    source_contexts = await auditor.fetch_audit_source_contexts(
        owner=owner,
        repo=name,
        diff_text=diff_text,
        sha=target.head_sha,
        token=token,
    )
    ci_context = ""
    if workflow_run_id is not None:
        ci_context = await auditor.fetch_audit_ci_context(
            owner=owner,
            repo=name,
            run_id=workflow_run_id,
            token=token,
        )
    ast_context = await auditor.build_ast_diff_summary_async(
        diff_text,
        source_contexts,
    )
    repository_context = "\n".join(
        part
        for part in (
            f"Repository: {owner}/{name}",
            f"Audit type: {resolved_audit_type}",
            f"Base commit: {target.base_sha or 'not applicable'}",
            f"Head commit: {target.head_sha}",
            f"Pull request: {resolved_pr if resolved_pr is not None else 'none'}",
            f"CI context:\n{ci_context}" if ci_context else "",
        )
        if part
    )
    result = await auditor.run_audit(
        diff_text=diff_text,
        ast_context=ast_context,
        repo_context=repository_context,
        repo_id=repo.id,
        audit_id=audit_id,
        audit_type=resolved_audit_type,
        repo_full_name=f"{owner}/{name}",
        ref=ref,
        head_sha=target.head_sha,
        pr_number=resolved_pr,
    )
    try:
        from app.github.audit_publisher import publish_audit_review

        await publish_audit_review(
            result=result,
            owner=owner,
            repo=name,
            head_sha=target.head_sha,
            pr_number=resolved_pr,
            diff_text=diff_text,
            token=token,
        )
    except Exception as pub_exc:
        logger.warning(
            "audit publish_failed audit_id=%s error_type=%s",
            audit_id,
            type(pub_exc).__name__,
        )
    return AuditExecutionOutcome(status="completed", result=result)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe_error_code(exc: BaseException) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]", "", type(exc).__name__)[:64]
    return value or "AuditError"


def _retry_delay_seconds(attempt: int) -> float:
    ceiling = min(
        AUDIT_RETRY_MAX_S,
        AUDIT_RETRY_BASE_S * (2 ** max(0, attempt - 1)),
    )
    return random.uniform(0.0, ceiling)


async def recover_expired_audit_leases() -> tuple[int, int]:
    from app.db import async_session_maker

    async with async_session_maker() as db:
        try:
            now = _utcnow()
            running = await db.execute(
                update(AuditJob)
                .where(
                    AuditJob.status == "running",
                    AuditJob.lease_expires_at.is_not(None),
                    AuditJob.lease_expires_at < now,
                )
                .values(
                    status=case(
                        (AuditJob.attempts >= AUDIT_MAX_PROCESSING_ATTEMPTS, "failed"),
                        else_="queued",
                    ),
                    next_attempt_at=now,
                    lease_expires_at=None,
                    last_error="LeaseExpired",
                )
            )
            dispatching = await db.execute(
                update(AuditJob)
                .where(
                    AuditJob.status == "dispatching",
                    AuditJob.lease_expires_at.is_not(None),
                    AuditJob.lease_expires_at < now,
                )
                .values(
                    status=case(
                        (
                            AuditJob.dispatch_attempts
                            >= AUDIT_MAX_DISPATCH_ATTEMPTS,
                            "failed",
                        ),
                        else_="queued",
                    ),
                    dispatch_fence_token=None,
                    next_attempt_at=now,
                    lease_expires_at=None,
                    last_error="LeaseExpired",
                )
            )
            await db.commit()
            return int(getattr(running, "rowcount", 0)), int(
                getattr(dispatching, "rowcount", 0)
            )
        except Exception:
            await db.rollback()
            raise


async def claim_next_queued_job() -> Optional[AuditDispatchClaim]:
    """Claim one queued job and mint the fence token for this dispatch attempt.

    The candidate row is locked before the attempt counter is read, so the fence
    is always minted for the exact attempt that gets persisted. A delayed child
    from attempt N is rejected after attempt N+1 supersedes it.
    """
    from app.db import async_session_maker

    async with async_session_maker() as db:
        try:
            now = _utcnow()
            candidate = (
                select(AuditJob.audit_id, AuditJob.dispatch_attempts)
                .where(
                    AuditJob.status == "queued",
                    AuditJob.next_attempt_at <= now,
                )
                .order_by(AuditJob.next_attempt_at, AuditJob.created_at, AuditJob.audit_id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            locked = (await db.execute(candidate)).one_or_none()
            if locked is None:
                await db.rollback()
                return None
            previous_attempts = int(locked.dispatch_attempts)
            next_attempt = previous_attempts + 1
            fence_token = audit_dispatch_fence_token(locked.audit_id, next_attempt)
            result = await db.execute(
                update(AuditJob)
                .where(
                    AuditJob.audit_id == locked.audit_id,
                    AuditJob.status == "queued",
                    AuditJob.dispatch_attempts == previous_attempts,
                )
                .values(
                    status="dispatching",
                    dispatch_attempts=next_attempt,
                    dispatch_fence_token=fence_token,
                    next_attempt_at=now,
                    lease_expires_at=now + timedelta(seconds=AUDIT_DISPATCH_LEASE_S),
                    last_error=None,
                )
                .returning(AuditJob.audit_id)
            )
            updated_id = result.scalar_one_or_none()
            if updated_id is None:
                await db.rollback()
                return None
            await db.commit()
            return AuditDispatchClaim(
                audit_id=locked.audit_id,
                dispatch_attempts=next_attempt,
                dispatch_fence_token=fence_token,
            )
        except Exception:
            await db.rollback()
            raise


async def release_dispatch_claim(
    claim: AuditDispatchClaim,
    exc: BaseException,
) -> bool:
    from app.db import async_session_maker

    terminal = claim.dispatch_attempts >= AUDIT_MAX_DISPATCH_ATTEMPTS
    async with async_session_maker() as db:
        try:
            now = _utcnow()
            result = await db.execute(
                update(AuditJob)
                .where(
                    AuditJob.audit_id == claim.audit_id,
                    AuditJob.status == "dispatching",
                    AuditJob.dispatch_attempts == claim.dispatch_attempts,
                    AuditJob.dispatch_fence_token == claim.dispatch_fence_token,
                )
                .values(
                    status="failed" if terminal else "queued",
                    dispatch_fence_token=None,
                    next_attempt_at=(
                        now
                        if terminal
                        else now + timedelta(seconds=_retry_delay_seconds(claim.dispatch_attempts))
                    ),
                    lease_expires_at=None,
                    last_error=_safe_error_code(exc),
                )
                .returning(AuditJob.audit_id)
            )
            updated_id = result.scalar_one_or_none()
            await db.commit()
            return terminal if updated_id is not None else False
        except Exception:
            await db.rollback()
            raise


async def _claim_processing_job(
    audit_id: str,
    dispatch_fence_token: str,
) -> Optional[tuple[AuditJob, Repo, int] | ProcessingClaimOutcome]:
    """Move a dispatching job to running only for the current signed fence.

    Returns None when the caller is not the current dispatch attempt: an
    expired lease that was already redispatched, a superseded fence, a
    malformed token, or a job that is not dispatching at all.

    Returns :attr:`ProcessingClaimOutcome.AUDITOR_DISABLED` when this caller
    *is* the current attempt but the persisted per-repo trigger refuses the work.
    The re-check reads `RepoSettings` inside the same transaction that takes the
    claim, so an operator flipping the kill switch cannot slip between the check
    and the claim and leave a disabled job running with credentials in hand.

    Propagates a database error from that re-check rather than reporting a
    refusal. "The trigger says off" and "we could not read the trigger" are
    different facts with opposite consequences — the first is terminal, the
    second must stay retryable — so a failed lookup is never collapsed into
    `AUDITOR_DISABLED`.
    """
    from app.db import async_session_maker

    async with async_session_maker() as db:
        try:
            now = _utcnow()
            locked = (
                await db.execute(
                    select(
                        AuditJob.audit_id,
                        AuditJob.repo_id,
                        AuditJob.status,
                        AuditJob.dispatch_attempts,
                        AuditJob.dispatch_fence_token,
                        AuditJob.lease_expires_at,
                    )
                    .where(AuditJob.audit_id == audit_id)
                    .with_for_update()
                )
            ).one_or_none()
            if locked is None or locked.status != "dispatching":
                await db.rollback()
                return None
            if locked.lease_expires_at is None or locked.lease_expires_at <= now:
                await db.rollback()
                return None
            if not verify_audit_dispatch_fence(
                audit_id, locked.dispatch_attempts, dispatch_fence_token
            ):
                logger.warning(
                    "audit processing_claim_rejected audit_id=%s reason=fence_mismatch",
                    audit_id,
                )
                await db.rollback()
                return None
            if not isinstance(locked.dispatch_fence_token, str) or not hmac.compare_digest(
                locked.dispatch_fence_token, dispatch_fence_token
            ):
                logger.warning(
                    "audit processing_claim_rejected audit_id=%s reason=superseded_attempt",
                    audit_id,
                )
                await db.rollback()
                return None
            # Per-repo kill switch, re-verified at execution time. The trigger was
            # evaluated at intake; a job can sit queued for minutes, and the
            # operator's decision to stop auditing this repo has to win over work
            # that was already scheduled. A missing `RepoSettings` row is a
            # deleted configuration, which fails closed like a disabled one —
            # `_valid_trigger_row` is what decides that, so there is one rule for
            # "absent" and one for "enabled", not two.
            #
            # The row is read inline rather than through `get_auditor_trigger`
            # because that helper is deliberately fail-closed: it swallows a
            # database error, returns the disabled default, and issues its own
            # `db.rollback()`. Inside this transaction both are wrong. The
            # rollback would release the `SELECT ... FOR UPDATE` claim lock on the
            # job row while the transaction stays open and then commits a
            # terminal status change, and a transient connection fault would be
            # indistinguishable from an operator's decision — a job permanently
            # failed as `AuditorDisabled` for an infrastructure error, never
            # retried. A real error is allowed to propagate instead: the
            # transaction is discarded by the handler below, `process_audit_job`
            # reports a non-terminal failure, and the job keeps its dispatching
            # lease so lease recovery re-queues it.
            settings_row = await db.scalar(
                select(RepoSettings).where(RepoSettings.repo_id == locked.repo_id)
            )
            trigger = _valid_trigger_row(settings_row)
            if not trigger.enabled:
                refused = await db.execute(
                    update(AuditJob)
                    .where(
                        AuditJob.audit_id == audit_id,
                        AuditJob.status == "dispatching",
                        AuditJob.dispatch_fence_token == dispatch_fence_token,
                    )
                    .values(
                        status="failed",
                        dispatch_fence_token=None,
                        next_attempt_at=now,
                        lease_expires_at=None,
                        last_error=AUDITOR_DISABLED_ERROR,
                    )
                    .returning(AuditJob.audit_id)
                )
                closed = refused.scalar_one_or_none()
                await db.commit()
                logger.warning(
                    "audit processing_refused audit_id=%s reason=auditor_disabled persisted=%s",
                    audit_id,
                    closed is not None,
                )
                return ProcessingClaimOutcome.AUDITOR_DISABLED if closed is not None else None
            result = await db.execute(
                update(AuditJob)
                .where(
                    AuditJob.audit_id == audit_id,
                    AuditJob.status == "dispatching",
                    AuditJob.dispatch_fence_token == dispatch_fence_token,
                    AuditJob.lease_expires_at.is_not(None),
                    AuditJob.lease_expires_at > now,
                )
                .values(
                    status="running",
                    attempts=AuditJob.attempts + 1,
                    dispatch_fence_token=None,
                    lease_expires_at=now
                    + timedelta(seconds=AUDIT_PROCESSING_LEASE_S),
                    last_error=None,
                )
                .returning(AuditJob.audit_id, AuditJob.attempts)
            )
            row = result.one_or_none()
            if row is None:
                await db.rollback()
                return None
            loaded = (
                await db.execute(
                    select(AuditJob, Repo)
                    .join(Repo, Repo.id == AuditJob.repo_id)
                    .where(AuditJob.audit_id == row.audit_id)
                )
            ).one_or_none()
            if loaded is None:
                await db.rollback()
                return None
            await db.commit()
            return loaded[0], loaded[1], row.attempts
        except Exception:
            await db.rollback()
            raise


async def _finish_audit_job(
    audit_id: str,
    attempt: int,
    status: Literal["completed", "skipped_no_diff"],
) -> bool:
    from app.db import async_session_maker

    async with async_session_maker() as db:
        try:
            now = _utcnow()
            result = await db.execute(
                update(AuditJob)
                .where(
                    AuditJob.audit_id == audit_id,
                    AuditJob.status == "running",
                    AuditJob.attempts == attempt,
                )
                .values(
                    status=status,
                    next_attempt_at=now,
                    lease_expires_at=None,
                    last_error=None,
                )
                .returning(AuditJob.audit_id)
            )
            updated_id = result.scalar_one_or_none()
            await db.commit()
            return updated_id is not None
        except Exception:
            await db.rollback()
            raise


async def _complete_audit_job(audit_id: str, attempt: int) -> bool:
    return await _finish_audit_job(audit_id, attempt, "completed")


async def _skip_audit_job(audit_id: str, attempt: int) -> bool:
    return await _finish_audit_job(audit_id, attempt, "skipped_no_diff")


async def _retry_or_fail_audit_job(
    audit_id: str,
    attempt: int,
    exc: BaseException,
) -> bool:
    from app.db import async_session_maker

    terminal = attempt >= AUDIT_MAX_PROCESSING_ATTEMPTS
    async with async_session_maker() as db:
        try:
            now = _utcnow()
            result = await db.execute(
                update(AuditJob)
                .where(
                    AuditJob.audit_id == audit_id,
                    AuditJob.status == "running",
                    AuditJob.attempts == attempt,
                )
                .values(
                    status="failed" if terminal else "queued",
                    next_attempt_at=(
                        now
                        if terminal
                        else now + timedelta(seconds=_retry_delay_seconds(attempt))
                    ),
                    lease_expires_at=None,
                    last_error=_safe_error_code(exc),
                )
                .returning(AuditJob.audit_id)
            )
            updated_id = result.scalar_one_or_none()
            await db.commit()
            return terminal if updated_id is not None else False
        except Exception:
            await db.rollback()
            raise


async def _record_processing_failure(
    audit_id: str,
    attempt: int,
    exc: BaseException,
) -> None:
    try:
        terminal = await _retry_or_fail_audit_job(audit_id, attempt, exc)
    except asyncio.CancelledError:
        raise
    except Exception as status_exc:
        logger.warning(
            "audit failure_state_persist_failed audit_id=%s error_type=%s",
            audit_id,
            type(status_exc).__name__,
        )
        return
    logger.warning(
        "audit processing_failed audit_id=%s terminal=%s error_type=%s",
        audit_id,
        terminal,
        _safe_error_code(exc),
    )


async def process_audit_job(audit_id: str, dispatch_fence_token: str) -> bool:
    """Run one audit attempt, but only as the current signed dispatch attempt.

    `dispatch_fence_token` must be the fence minted for the attempt that is
    currently persisted on the job. A delayed child from a superseded dispatch
    attempt returns True without touching job state: the work it was asked to do
    is already owned by a newer attempt, and failing the job here would punish
    the newer attempt for the older one's lateness.

    A :attr:`ProcessingClaimOutcome.AUDITOR_DISABLED` claim also returns True,
    because the job is already terminal in that case: the claim closed it out
    rather than starting it, so there is no failure to record and — critically —
    no credential was fetched and no model was called.
    """
    if not isinstance(audit_id, str) or not _AUDIT_ID_RE.fullmatch(audit_id):
        raise ValueError("audit id is invalid")
    try:
        loaded = await _claim_processing_job(audit_id, dispatch_fence_token)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "audit processing_claim_failed audit_id=%s error_type=%s",
            audit_id,
            _safe_error_code(exc),
        )
        return False
    if loaded is None or loaded is ProcessingClaimOutcome.AUDITOR_DISABLED:
        return True
    job, repo, attempt = loaded
    try:
        token = await asyncio.wait_for(
            get_auditor_installation_token(repo),
            timeout=AUDIT_TOKEN_TIMEOUT_S,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await _record_processing_failure(audit_id, attempt, exc)
        return False
    try:
        outcome = await asyncio.wait_for(
            execute_audit_job(
                audit_id=job.audit_id,
                audit_type=job.audit_type,
                repo=repo,
                ref=job.ref,
                pr_number=job.pr_number,
                base_sha=job.base_sha,
                head_sha=job.head_sha,
                workflow_run_id=job.workflow_run_id,
                token=token,
                attempt=attempt,
            ),
            timeout=AUDIT_JOB_TIMEOUT_S,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await _record_processing_failure(audit_id, attempt, exc)
        return False
    if not isinstance(outcome, AuditExecutionOutcome):
        await _record_processing_failure(
            audit_id,
            attempt,
            RuntimeError("InvalidAuditExecutionOutcome"),
        )
        return False
    if outcome.status not in ("completed", "skipped_no_diff"):
        await _record_processing_failure(
            audit_id,
            attempt,
            RuntimeError("InvalidAuditExecutionOutcome"),
        )
        return False
    try:
        if outcome.status == "skipped_no_diff":
            state_updated = await _skip_audit_job(audit_id, attempt)
        else:
            state_updated = await _complete_audit_job(audit_id, attempt)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "audit completion_state_persist_failed audit_id=%s error_type=%s",
            audit_id,
            _safe_error_code(exc),
        )
        return False
    if not state_updated:
        logger.warning(
            "audit completion_state_fenced audit_id=%s attempt=%s",
            audit_id,
            attempt,
        )
        return False
    return True


async def dispatch_audit_jobs(
    *,
    batch_size: int = AUDIT_DISPATCH_BATCH_SIZE,
    local_execute: bool = False,
) -> AuditDispatchSummary:
    if isinstance(batch_size, bool) or not 1 <= batch_size <= 100:
        raise ValueError("audit dispatch batch size is invalid")
    running_recovered, dispatch_recovered = await recover_expired_audit_leases()
    recovered = running_recovered + dispatch_recovered
    adapter = None
    if not local_execute:
        try:
            from app.adapters.hosting import get_hosting_adapter

            adapter = await get_hosting_adapter()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "audit dispatcher_unavailable error_type=%s",
                _safe_error_code(exc),
            )
            return AuditDispatchSummary(recovered=recovered)
    if not local_execute and adapter is None:
        return AuditDispatchSummary(recovered=recovered)
    claimed = 0
    scheduled = 0
    released = 0
    terminal = 0
    for _ in range(batch_size):
        try:
            claim = await claim_next_queued_job()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "audit dispatch_claim_failed error_type=%s",
                _safe_error_code(exc),
            )
            break
        if claim is None:
            break
        claimed += 1
        if local_execute:
            try:
                await process_audit_job(claim.audit_id, claim.dispatch_fence_token)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "audit local_dispatch_failed audit_id=%s error_type=%s",
                    claim.audit_id,
                    _safe_error_code(exc),
                )
            continue
        assert adapter is not None
        try:
            await adapter.schedule_audit(claim.audit_id, claim.dispatch_fence_token)
            scheduled += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                is_terminal = await release_dispatch_claim(claim, exc)
                released += 1
                terminal += int(is_terminal)
            except Exception as release_exc:
                logger.warning(
                    "audit dispatch_release_failed audit_id=%s error_type=%s",
                    claim.audit_id,
                    _safe_error_code(release_exc),
                )
    return AuditDispatchSummary(
        recovered=recovered,
        claimed=claimed,
        scheduled=scheduled,
        released=released,
        terminal=terminal,
    )


async def _audit_dispatch_worker(
    *,
    local_execute: bool,
    max_cycles: Optional[int] = None,
) -> None:
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        try:
            summary = await dispatch_audit_jobs(
                batch_size=AUDIT_DISPATCH_BATCH_SIZE,
                local_execute=local_execute,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "audit dispatch_cycle_failed error_type=%s",
                _safe_error_code(exc),
            )
            summary = AuditDispatchSummary()
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            return
        delay = 0.0 if summary.claimed == AUDIT_DISPATCH_BATCH_SIZE else AUDIT_DISPATCH_POLL_S
        await asyncio.sleep(delay)


def start_audit_dispatch_workers() -> int:
    """Start local audit dispatch workers, or refuse to run inside Lambda.

    A Lambda-like runtime freezes execution when the invocation returns, so an
    in-process audit worker there would either be killed mid-run or, worse,
    survive long enough to claim a job it can never finish. Dispatch in Lambda
    is driven by the IAM-only scheduled dispatcher instead, which schedules
    audit children as separate invocations.
    """
    from app.lambda_runtime import is_lambda_runtime

    if _audit_dispatch_workers:
        return len(_audit_dispatch_workers)
    if is_lambda_runtime():
        logger.info("audit dispatch workers not started in Lambda runtime")
        return 0
    for _ in range(AUDIT_DISPATCH_WORKER_COUNT):
        task = asyncio.create_task(
            _audit_dispatch_worker(local_execute=True),
            name="haunter-audit-dispatch",
        )
        _audit_dispatch_workers.add(task)
        task.add_done_callback(_audit_dispatch_workers.discard)
    return len(_audit_dispatch_workers)


async def stop_audit_dispatch_workers() -> None:
    tasks = tuple(_audit_dispatch_workers)
    _audit_dispatch_workers.clear()
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

