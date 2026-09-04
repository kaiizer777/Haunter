"""
Retry and resilience wrapper for LLM provider calls.

Implements a dual-policy retry architecture:
- Policy 1 (Rate Limit): HTTP 429 retries 4-5 times with exponential backoff and jitter.
- Policy 2 (Outage / Unsupported): HTTP 5xx, timeouts, drops, per-model 401/403/404 rejections,
  and upstream 400 errors execute exactly 1 attempt and fail immediately to trigger next-model fallback.
- Policy 3 (Global Auth): Global 401/403 fails fast across all models.
"""

import asyncio
import logging
import random
import time
from typing import Any, Awaitable, Callable

import httpx

from app.llm.exceptions import (
    LLMAuthenticationError,
    LLMError,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMTimeoutError,
)

logger = logging.getLogger(__name__)

# Phrases that indicate a 400/401/403/404 is a *per-model* "not supported" rejection
# rather than a key-level auth failure. OpenCode Zen returns 401 with a
# `ModelError` body when a specific model id is not on the account's tier —
# the key is fine, that model just isn't available.
_PER_MODEL_REJECTION_PHRASES: tuple[str, ...] = (
    "is not supported",
    "model not supported",
    "modelerror",
    "unknown model",
    "invalid model",
    "model does not exist",
)

# Phrases that indicate an HTTP 400 is an upstream provider or model server error
# rather than a client-side request validation failure.
_UPSTREAM_400_PHRASES: tuple[str, ...] = (
    "upstream",
    "provider error",
    "provider_error",
    "server error",
    "internal error",
    "overloaded",
    "capacity",
    "model error",
    "modelerror",
    "is not supported",
    "model not supported",
    "context length",
    "maximum context",
    "context_length_exceeded",
    "token limit",
    "max_tokens",
    "service unavailable",
    "bad gateway",
)


def _is_per_model_rejection(status_code: int, body: str) -> bool:
    """True when a 400/401/403/404 response body indicates a per-model rejection
    rather than a key-level auth failure or genuine auth problem."""
    if status_code not in (400, 401, 403, 404):
        return False
    body_lc = body.lower()
    return any(phrase in body_lc for phrase in _PER_MODEL_REJECTION_PHRASES)


def _is_upstream_400_error(body: str) -> bool:
    """True when an HTTP 400 error body reflects an upstream provider or model failure
    rather than a client-side request validation failure."""
    body_lc = body.lower()
    return any(phrase in body_lc for phrase in _UPSTREAM_400_PHRASES)


async def execute_with_retry(
    func: Callable[[], Awaitable[dict[str, Any]]],
    max_attempts: int = 5,
    max_total_time: float = 60.0,
    initial_delay: float = 1.0,
    backoff_factor: float = 2.0,
    max_delay: float = 15.0,
) -> dict[str, Any]:
    """
    Execute an async LLM request callable under the dual-policy retry architecture.

    - Policy 1 (Rate Limit): HTTP 429 retries up to max_attempts (default 5: 1s, 2s, 4s, 8s backoff + jitter).
      If 429 persists, raises LLMRateLimitError carrying attempt count so client can switch to next fallback.
    - Policy 2 (Outages / Unsupported): HTTP 5xx, network timeouts, connection drops, per-model 401/403/404
      rejections, and upstream 400 errors execute exactly 1 attempt and fail immediately (raising LLMError/LLMTimeoutError).
    - Policy 3 (Global Auth): Global 401/403 fails fast immediately across all models (raising LLMAuthenticationError).
    - Client payload errors (400/422 without upstream indicators): raises LLMInvalidRequestError immediately.
    """
    start_time = time.monotonic()
    last_exception: Exception | None = None
    last_status: int | None = None

    for attempt in range(1, max_attempts + 1):
        elapsed = time.monotonic() - start_time
        remaining_budget = max_total_time - elapsed
        if remaining_budget <= 0:
            logger.warning("LLM retry budget exhausted before attempt %d", attempt)
            raise LLMTimeoutError("LLM call exceeded total retry budget")

        try:
            return await func()

        except httpx.HTTPStatusError as exc:
            last_status = exc.response.status_code
            last_exception = exc
            response_body = exc.response.text or ""

            # Per-model rejection (400, 401, 403, 404): Policy 2 -> attempt count = 1, fail immediately for fallback
            if _is_per_model_rejection(last_status, response_body):
                logger.warning(
                    "LLM model rejected by provider (status=%d, body=%s) — "
                    "raising LLMError so fallback chain can try next model",
                    last_status,
                    response_body[:200],
                )
                raise LLMError(
                    f"Model rejected by provider (HTTP {last_status}): {response_body[:200]!r}",
                    status_code=last_status,
                ) from None

            # Global auth failure (401, 403): Policy 3 -> fail fast across all models
            if last_status in (401, 403):
                logger.error("Global LLM authentication failed with status %d", last_status)
                raise LLMAuthenticationError(
                    f"LLM authentication failed with status {last_status}: {response_body[:200]!r}"
                ) from None

            # Upstream error in HTTP 400: Policy 2 -> attempt count = 1, switch model
            if last_status == 400 and _is_upstream_400_error(response_body):
                logger.warning(
                    "Upstream error in HTTP 400: %s — raising LLMError for model fallback",
                    response_body[:200],
                )
                raise LLMError(
                    f"Upstream provider error (HTTP 400): {response_body[:200]!r}",
                    status_code=400,
                ) from None

            # Genuine client invalid request (400, 422): fail immediately
            if last_status in (400, 422):
                logger.error("LLM request rejected with status %d: %s", last_status, response_body[:200])
                raise LLMInvalidRequestError(f"Invalid LLM request payload (HTTP {last_status})") from None

            # 404 model / endpoint not found: Policy 2 -> attempt count = 1, switch model
            if last_status == 404:
                raise LLMError(f"LLM model or endpoint not found (404): {response_body[:200]!r}", status_code=404) from None

            # 5xx Server Error: Policy 2 -> attempt count = 1, do NOT retry on dead model, switch immediately
            if 500 <= last_status <= 599:
                logger.warning("LLM provider server error HTTP %d (attempt 1) — failing immediately for model fallback", last_status)
                raise LLMError(
                    f"LLM provider error ({last_status}): {response_body[:200]!r}",
                    status_code=last_status,
                ) from None

            # 429 Rate Limit: Policy 1 -> retry up to max_attempts with backoff + jitter
            if last_status == 429:
                if attempt >= max_attempts:
                    logger.warning("LLM rate limit (429) persisted after %d attempts", max_attempts)
                    raise LLMRateLimitError(
                        f"LLM rate limit (429) persisted after {max_attempts} attempts",
                        attempts=attempt,
                    ) from None

                # Calculate backoff delay with jitter
                delay = min(initial_delay * (backoff_factor ** (attempt - 1)), max_delay)
                jitter = random.uniform(0.1, 0.5)
                total_delay = delay + jitter

                if time.monotonic() - start_time + total_delay >= max_total_time:
                    raise LLMTimeoutError("LLM total time budget exceeded during rate limit backoff")

                logger.info(
                    "LLM 429 on attempt %d/%d, retrying in %.2fs...",
                    attempt,
                    max_attempts,
                    total_delay,
                )
                await asyncio.sleep(total_delay)
                continue

            # Fallthrough for any unexpected status
            raise LLMError(f"LLM provider error ({last_status})", status_code=last_status) from None

        except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
            # Policy 2: Network timeout -> attempt count = 1, fail immediately for fallback
            logger.warning("LLM call timed out on attempt 1 — failing immediately for model fallback: %s", exc)
            raise LLMTimeoutError(f"LLM request timed out: {exc}") from None

        except httpx.TransportError as exc:
            # Policy 2: Connection drop -> attempt count = 1, fail immediately for fallback
            logger.warning("LLM transport error on attempt 1 — failing immediately for model fallback: %s", exc)
            raise LLMError(f"LLM network transport error: {exc}") from None

    if last_status == 429:
        raise LLMRateLimitError(
            f"LLM rate limit (429) persisted after {max_attempts} attempts",
            attempts=max_attempts,
        )
    raise LLMError("LLM call failed after maximum retries")
