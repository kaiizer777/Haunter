"""Shared AWS Lambda runtime detection."""

from __future__ import annotations

import os
from contextvars import ContextVar, Token
from typing import Any, Optional


def resolve_lambda_function_name(configured: Optional[str] = None) -> Optional[str]:
    if isinstance(configured, str):
        normalized = configured.strip()
        if normalized:
            return normalized
    from app.config import settings

    setting_value = settings.aws_lambda_function_name
    if isinstance(setting_value, str):
        normalized = setting_value.strip()
        if normalized:
            return normalized
    environment_value = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "").strip()
    return environment_value or None


def is_lambda_runtime() -> bool:
    return resolve_lambda_function_name() is not None


#: Fallback stream timeout (seconds) when no Lambda context is available,
#: e.g. local dev. Mirrors the Lambda ``timeout`` in ``infra/aws/lambda.tf``;
#: kept as the single copy of that value in Python.
LAMBDA_TIMEOUT_FALLBACK_S = 900.0

#: Headroom (seconds) subtracted from the Lambda remaining-time budget so a
#: stream guard trips before the hard kill instead of at it. Must exceed one
#: fully backed-off poll sleep plus a DB round trip.
LAMBDA_TIMEOUT_SAFETY_MARGIN_S = 30.0

#: Lambda invoke context for the in-flight request, stashed by
#: ``lambda_handler.handler``. Mangum does not forward the context into the
#: ASGI scope, so it is propagated via context variable instead.
_current_lambda_context: ContextVar[Optional[Any]] = ContextVar(
    "haunter_lambda_context", default=None
)


def set_lambda_context(context: Optional[Any]) -> Token[Optional[Any]]:
    """Stash the Lambda invoke ``context`` for the current request."""
    return _current_lambda_context.set(context)


def reset_lambda_context(token: Token[Optional[Any]]) -> None:
    """Clear a context previously stored with :func:`set_lambda_context`."""
    _current_lambda_context.reset(token)


def resolve_lambda_timeout_s() -> float:
    """Stream budget anchored to the real Lambda remaining time.

    Returns ``get_remaining_time_in_millis() / 1000`` minus the safety
    margin, floored at 0. Any missing/broken context (local dev, unit tests,
    unexpected shape) falls back to ``LAMBDA_TIMEOUT_FALLBACK_S``.
    """
    context = _current_lambda_context.get()
    if context is None:
        return LAMBDA_TIMEOUT_FALLBACK_S
    try:
        remaining_ms = context.get_remaining_time_in_millis()
    except Exception:
        return LAMBDA_TIMEOUT_FALLBACK_S
    try:
        remaining_s = float(remaining_ms) / 1000.0
    except (TypeError, ValueError):
        return LAMBDA_TIMEOUT_FALLBACK_S
    if remaining_s <= 0:
        return LAMBDA_TIMEOUT_FALLBACK_S
    return max(0.0, remaining_s - LAMBDA_TIMEOUT_SAFETY_MARGIN_S)
