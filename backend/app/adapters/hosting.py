"""
Hosting adapter — Phase 14.

Abstracts the mechanism for scheduling the async pipeline after the webhook
handler returns 2xx.

Problem: on AWS Lambda the execution context is frozen the moment the HTTP response is
sent — BackgroundTasks tasks never execute.

Solution: AWSHostingAdapter → boto3.lambda_client.invoke(InvocationType='Event', ...)

The lambda_handler.py entry point detects a "direct invocation" payload
({"run_id": "..."}) and calls handle_failed_run() directly, completing the loop.

Provider selection:
  1. DB key "hosting_provider" (system_configs table, TTL-cached 60s)
  2. HOSTING_PROVIDER env var (settings.hosting_provider)
  Default: "aws"

Security:
  - Provider values are allowlisted ("aws") before use.
  - Lambda role has AWSLambdaBasicExecutionRole + lambda:InvokeFunction on self only.
    No secretsmanager:GetSecretValue, no cross-tenant resource access.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from fastapi import BackgroundTasks

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System config helpers — 60s TTL cache for hot-switch without redeploy
# ---------------------------------------------------------------------------

_cfg_cache: dict[str, tuple[str, float]] = {}
_CACHE_TTL: float = 60.0

_ALLOWED_PROVIDERS: frozenset[str] = frozenset({"aws"})


async def _get_provider_config(key: str, env_default: str) -> str:
    """
    Read provider config from DB system_configs table (TTL-cached 60s).
    Falls back to env default if DB is unavailable or key not set.
    Always validates against allowlist — returns env_default on invalid value.
    """
    from app.db import async_session_maker
    from app.models import SystemConfig
    from sqlalchemy import select

    now = time.monotonic()
    cached_val, cached_at = _cfg_cache.get(key, ("", -_CACHE_TTL - 1))
    if now - cached_at < _CACHE_TTL and cached_val:
        return cached_val

    value = env_default
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(SystemConfig).where(SystemConfig.key == key)
            )
            row = result.scalar_one_or_none()
            if row:
                value = row.value
    except Exception as exc:
        logger.warning(
            "hosting: failed to read system_configs.%s from DB (%s), using env default %r",
            key,
            exc,
            env_default,
        )

    if value not in _ALLOWED_PROVIDERS:
        logger.error(
            "hosting: invalid provider %r for key=%s, falling back to %r",
            value,
            key,
            env_default,
        )
        value = env_default

    _cfg_cache[key] = (value, now)
    return value


async def get_active_hosting_provider() -> str:
    """Return the active HOSTING_PROVIDER (aws), hot-switchable via DB."""
    from app.config import settings

    return await _get_provider_config("hosting_provider", settings.hosting_provider)


async def get_active_sandbox_provider() -> str:
    """Return the active SANDBOX_PROVIDER (aws), hot-switchable via DB."""
    from app.config import settings

    return await _get_provider_config("sandbox_provider", settings.sandbox_provider)


def invalidate_provider_cache() -> None:
    """Clear the TTL cache — call after PUT /config/hosting writes to DB."""
    _cfg_cache.clear()


# ---------------------------------------------------------------------------
# Abstract adapter interface
# ---------------------------------------------------------------------------


class HostingAdapter(ABC):
    """Interface for scheduling the fix pipeline after webhook 2xx response."""

    @abstractmethod
    async def schedule_pipeline(
        self,
        run_id: UUID,
        background_tasks: "BackgroundTasks",
    ) -> None:
        """
        Schedule handle_failed_run(run_id) to execute asynchronously.
        Must not block — return as fast as possible.
        """

    @abstractmethod
    async def schedule_review(
        self,
        review_id: UUID,
        background_tasks: "BackgroundTasks",
    ) -> None:
        """
        Schedule run_code_review_pipeline(review_id) to execute asynchronously.
        Must not block — return as fast as possible.
        """

    @abstractmethod
    async def schedule_audit(self, audit_id: str, dispatch_fence_token: str) -> None:
        """Schedule one durable audit job without using request background tasks.

        `dispatch_fence_token` is the signed per-attempt fence minted by the
        dispatcher. It is bound into the child invocation token so a delayed
        child from an earlier dispatch attempt cannot be mistaken for the
        current one.
        """


# ---------------------------------------------------------------------------
# AWS adapter (Lambda) — async self-invoke via boto3
# ---------------------------------------------------------------------------


class AWSHostingAdapter(HostingAdapter):
    """
    Lambda adapter: async self-invocation via boto3.

    Lambda execution context freezes after the HTTP response is returned —
    BackgroundTasks would silently never run. Instead we invoke the same
    Lambda function asynchronously (InvocationType='Event', boto3 returns
    202 immediately) with payload {"run_id": "<uuid>"}.

    The lambda_handler.py entry point detects this payload and calls
    handle_failed_run() in the child invocation.

    IAM requirement on Lambda role:
      lambda:InvokeFunction on arn:aws:lambda:<region>:<account>:function:<self>
    """

    def __await__(self):
        async def _resolve():
            await get_active_hosting_provider()
            return self

        return _resolve().__await__()

    async def schedule_pipeline(
        self,
        run_id: UUID,
        background_tasks: "BackgroundTasks",
    ) -> None:
        import json

        from app.lambda_runtime import resolve_lambda_function_name
        from app.self_invocation import (
            KIND_PIPELINE,
            SelfInvocationError,
            self_invocation_token,
        )

        function_name = resolve_lambda_function_name()
        if not function_name:
            logger.error(
                "hosting(aws): AWS Lambda function name is not configured; "
                "falling back to in-process BackgroundTasks — pipeline may not execute on Lambda"
            )
            from app.orchestrator import handle_failed_run

            background_tasks.add_task(handle_failed_run, run_id)
            return

        try:
            token = self_invocation_token(KIND_PIPELINE, str(run_id))
        except SelfInvocationError:
            logger.error(
                "hosting(aws): self-invocation secret is not configured; "
                "refusing to invoke Lambda for run=%s",
                run_id,
            )
            raise

        payload = json.dumps({"run_id": str(run_id), "token": token}).encode()

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _invoke_lambda_async, function_name, payload)
        logger.info(
            "hosting(aws): async-invoked Lambda %s for run=%s",
            function_name,
            run_id,
        )

    async def schedule_review(
        self,
        review_id: UUID,
        background_tasks: "BackgroundTasks",
    ) -> None:
        import json

        from app.lambda_runtime import resolve_lambda_function_name
        from app.self_invocation import (
            KIND_REVIEW,
            SelfInvocationError,
            self_invocation_token,
        )

        function_name = resolve_lambda_function_name()
        if not function_name:
            logger.info(
                "hosting(aws): AWS Lambda function name is not configured; "
                "using in-process BackgroundTasks for code review"
            )
            from app.services.review_orchestrator import run_code_review_pipeline

            background_tasks.add_task(run_code_review_pipeline, review_id)
            return

        try:
            token = self_invocation_token(KIND_REVIEW, str(review_id))
        except SelfInvocationError:
            logger.error(
                "hosting(aws): self-invocation secret is not configured; "
                "refusing to invoke Lambda for review=%s",
                review_id,
            )
            raise

        payload = json.dumps({"review_id": str(review_id), "token": token}).encode()

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _invoke_lambda_async, function_name, payload)
        logger.info(
            "hosting(aws): async-invoked Lambda %s for review=%s",
            function_name,
            review_id,
        )

    async def schedule_audit(self, audit_id: str, dispatch_fence_token: str) -> None:
        import json

        from app.lambda_runtime import resolve_lambda_function_name
        from app.services.audit_pipeline import audit_child_invocation_token

        if not isinstance(audit_id, str) or not re.fullmatch(
            r"audit-[0-9a-f]{12}", audit_id, flags=re.ASCII
        ):
            raise ValueError("audit id is invalid")

        function_name = resolve_lambda_function_name()
        if not function_name:
            raise RuntimeError("AWS Lambda function name is not configured")

        token = audit_child_invocation_token(audit_id, dispatch_fence_token)
        payload = json.dumps(
            {
                "audit_id": audit_id,
                "dispatch_fence_token": dispatch_fence_token,
                "token": token,
            }
        ).encode()
        loop = asyncio.get_running_loop()
        # No wait_for: cancelling this coroutine cannot stop the executor
        # thread, so a timeout would release the dispatch lease while a
        # delayed child invoke was still in flight. The boto Config timeouts
        # bound the call and the thread always finishes before we return.
        await loop.run_in_executor(None, _invoke_lambda_async, function_name, payload)
        logger.info(
            "hosting(aws): async-invoked Lambda for audit_id=%s",
            audit_id,
        )


def _invoke_lambda_async(function_name: str, payload: bytes) -> None:
    """
    Synchronous boto3 call (run in executor to avoid blocking event loop).
    InvocationType='Event' — fire-and-forget, returns 202 immediately.
    """
    import boto3
    from botocore.config import Config

    from app.config import settings

    client = boto3.client(
        "lambda",
        region_name=settings.aws_region,
        config=Config(
            connect_timeout=2,
            read_timeout=3,
            retries={"max_attempts": 2},
        ),
    )
    resp = client.invoke(
        FunctionName=function_name,
        InvocationType="Event",  # async, returns 202 immediately
        Payload=payload,
    )
    status = resp.get("StatusCode", 0)
    if status != 202:
        logger.error(
            "hosting(aws): Lambda async invoke returned unexpected status %d for function=%s",
            status,
            function_name,
        )
        raise RuntimeError("Lambda async invocation was not accepted")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


async def get_hosting_adapter(provider: str | None = None) -> AWSHostingAdapter:
    """
    Return the appropriate HostingAdapter for the current HOSTING_PROVIDER.
    Provider value is hot-switchable via DB (60s TTL cache).
    """
    if provider is None:
        await _get_provider_config("HOSTING_PROVIDER", "aws")
    return AWSHostingAdapter()
