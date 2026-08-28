"""
Hosting adapter — Phase 14.

Abstracts the mechanism for scheduling the async pipeline after the webhook
handler returns 2xx.

Problem: FastAPI BackgroundTasks work on Cloud Run (process stays alive), but
on AWS Lambda the execution context is frozen the moment the HTTP response is
sent — BackgroundTasks tasks never execute.

Solution: two adapters, same interface.
  GCPHostingAdapter  → background_tasks.add_task(handle_failed_run, run_id)
  AWSHostingAdapter  → boto3.lambda_client.invoke(InvocationType='Event', ...)

The lambda_handler.py entry point detects a "direct invocation" payload
({"run_id": "..."}) and calls handle_failed_run() directly, completing the loop.

Provider selection:
  1. DB key "hosting_provider" (system_configs table, TTL-cached 60s)
  2. HOSTING_PROVIDER env var (settings.hosting_provider)
  Default: "gcp"

Security:
  - Provider values are allowlisted ("gcp" | "aws") before use.
  - Lambda role has AWSLambdaBasicExecutionRole + lambda:InvokeFunction on self only.
    No secretsmanager:GetSecretValue, no cross-tenant resource access.
"""

from __future__ import annotations

import asyncio
import logging
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

_ALLOWED_PROVIDERS: frozenset[str] = frozenset({"gcp", "aws"})


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
            if row and row.value in _ALLOWED_PROVIDERS:
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
    """Return the active HOSTING_PROVIDER (gcp|aws), hot-switchable via DB."""
    from app.config import settings
    return await _get_provider_config("hosting_provider", settings.hosting_provider)


async def get_active_sandbox_provider() -> str:
    """Return the active SANDBOX_PROVIDER (gcp|aws), hot-switchable via DB."""
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


# ---------------------------------------------------------------------------
# GCP adapter (Cloud Run) — standard BackgroundTasks
# ---------------------------------------------------------------------------


class GCPHostingAdapter(HostingAdapter):
    """
    Cloud Run adapter: uses FastAPI BackgroundTasks.

    Cloud Run keeps the process alive after the response is sent, so
    background tasks execute normally. Zero extra infrastructure needed.
    """

    async def schedule_pipeline(
        self,
        run_id: UUID,
        background_tasks: "BackgroundTasks",
    ) -> None:
        from app.orchestrator import handle_failed_run

        background_tasks.add_task(handle_failed_run, run_id)
        logger.info("hosting(gcp): queued handle_failed_run for run=%s", run_id)


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

    async def schedule_pipeline(
        self,
        run_id: UUID,
        background_tasks: "BackgroundTasks",
    ) -> None:
        import hashlib
        import hmac
        import json
        from app.config import settings

        function_name = settings.aws_lambda_function_name
        if not function_name:
            # Fall back to the env var Lambda automatically sets for itself
            import os
            function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")

        if not function_name:
            logger.error(
                "hosting(aws): AWS_LAMBDA_FUNCTION_NAME not set; "
                "falling back to GCP BackgroundTasks — pipeline may not execute on Lambda"
            )
            from app.orchestrator import handle_failed_run
            background_tasks.add_task(handle_failed_run, run_id)
            return

        # HMAC for pipeline self-invoke — prevents unauthenticated direct InvokeFunction
        # from triggering arbitrary run_id (S-06). Key is github_webhook_secret (or session_secret_key fallback).
        hmac_key = getattr(settings, "github_webhook_secret", None) or getattr(
            settings, "session_secret_key", ""
        )
        token = ""
        if hmac_key:
            token = hmac.new(
                hmac_key.encode(), str(run_id).encode(), hashlib.sha256
            ).hexdigest()
        payload_dict: dict[str, str] = {"run_id": str(run_id)}
        if token:
            payload_dict["token"] = token
        payload = json.dumps(payload_dict).encode()

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _invoke_lambda_async, function_name, payload)
        logger.info(
            "hosting(aws): async-invoked Lambda %s for run=%s",
            function_name,
            run_id,
        )


def _invoke_lambda_async(function_name: str, payload: bytes) -> None:
    """
    Synchronous boto3 call (run in executor to avoid blocking event loop).
    InvocationType='Event' — fire-and-forget, returns 202 immediately.
    """
    import boto3  # lazy — only imported when HOSTING_PROVIDER=aws

    from app.config import settings

    client = boto3.client("lambda", region_name=settings.aws_region)
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


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


async def get_hosting_adapter() -> HostingAdapter:
    """
    Return the appropriate HostingAdapter for the current HOSTING_PROVIDER.
    Provider value is hot-switchable via DB (60s TTL cache).
    """
    provider = await get_active_hosting_provider()
    if provider == "aws":
        return AWSHostingAdapter()
    return GCPHostingAdapter()
