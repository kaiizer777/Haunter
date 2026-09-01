"""
Retry and resilience wrapper for LLM provider calls.

Implements exponential backoff with jitter, capped attempt count, and bounded total time
to prevent upstream degradation from blocking background workers.
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

# Non-retryable HTTP status codes
_FATAL_STATUS_CODES = {400, 401, 403, 404, 422}

# Phrases that indicate a 401/403 is a *per-model* "not supported" rejection
# rather than a key-level auth failure. OpenCode Zen returns 401 with a
# `ModelError` body when a specific model id is not on the account's tier —
# the key is fine, that model just isn't available. Treating those as fatal
# would abort the entire fallback chain (client.complete() also re-raises
# LLMAuthenticationError), so we downgrade them to a plain LLMError which
# the client will skip and move on to the next model.
_PER_MODEL_REJECTION_PHRASES: tuple[str, ...] = (
    "is not supported",
    "model not supported",
    "modelerror",
    "unknown model",
    "invalid model",
    "model does not exist",
)


def _is_per_model_rejection(status_code: int, body: str) -> bool:
    """True when a 401/403/404 response body indicates a per-model rejection
    rather than a key-level auth failure or genuine auth problem."""
    if status_code not in (401, 403, 404):
        return False
    body_lc = body.lower()
    return any(phrase in body_lc for phrase in _PER_MODEL_REJECTION_PHRASES)


async def execute_with_retry(
    func: Callable[[], Awaitable[dict[str, Any]]],
    max_attempts: int = 8,
    max_total_time: float = 300.0,
    initial_delay: float = 1.0,
    backoff_factor: float = 2.0,
    max_delay: float = 30.0,
) -> dict[str, Any]:
    """
    Execute an async LLM request callable with bounded retries and exponential backoff.

    - Retries on HTTP 429 (Rate Limit), 5xx (Server Error), and network/timeout exceptions.
    - Fails immediately on 400, 401, 403, 422 (client/auth/validation errors).
      EXCEPTION: 401/403/404 whose body contains a per-model rejection phrase
      (e.g. "Model ling-3-free is not supported") is downgraded to a plain
      ``LLMError`` so the outer fallback chain can move on to the next model.
    - Capped at max_attempts AND max_total_time.
    """
    start_time = time.monotonic()
    last_exception: Exception | None = None
    last_status: int | None = None

    for attempt in range(1, max_attempts + 1):
        elapsed = time.monotonic() - start_time
        remaining_budget = max_total_time - elapsed
        if remaining_budget <= 0:
            logger.warning("LLM retry budget exhausted before attempt %d", attempt)
            raise LLMTimeoutError("LLM call exceeded total retry budget (60s)")

        try:
            return await func()

        except httpx.HTTPStatusError as exc:
            last_status = exc.response.status_code
            last_exception = exc
            response_body = exc.response.text or ""

            # Per-model rejection (e.g. "Model ling-3-free is not supported") is
            # not an auth failure — the key is fine, that model just isn't on
            # this tier. Surface as a plain LLMError so the client walks the
            # fallback chain instead of aborting.
            if last_status in (401, 403, 404) and _is_per_model_rejection(last_status, response_body):
                logger.warning(
                    "LLM model rejected by provider (status=%d, body=%s) — "
                    "downgrading to LLMError so fallback chain can try next model",
                    last_status,
                    response_body[:200],
                )
                raise LLMError(
                    f"Model rejected by provider (HTTP {last_status}): "
                    f"{response_body[:200]!r}",
                    status_code=last_status,
                ) from None

            if last_status in (401, 403):
                logger.error("LLM authentication failed with status %d", last_status)
                raise LLMAuthenticationError() from None

            if last_status in (400, 422):
                logger.error("LLM request rejected with status %d", last_status)
                raise LLMInvalidRequestError() from None

            if last_status == 404:
                raise LLMError("LLM model or endpoint not found (404)", status_code=404) from None

            # 429 or 5xx are retryable
            if attempt == max_attempts:
                if last_status == 429:
                    raise LLMRateLimitError() from None
                raise LLMError(f"LLM provider error ({last_status}) after {max_attempts} attempts", status_code=last_status) from None

        except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
            last_exception = exc
            if attempt == max_attempts:
                raise LLMTimeoutError("LLM request timed out after retries") from None

        except httpx.TransportError as exc:
            last_exception = exc
            if attempt == max_attempts:
                raise LLMError("LLM network transport error after retries") from None

        # Calculate backoff delay with jitter
        delay = min(initial_delay * (backoff_factor ** (attempt - 1)), max_delay)
        jitter = random.uniform(0.1, 0.5)
        total_delay = delay + jitter

        # Ensure delay does not exceed total budget
        if time.monotonic() - start_time + total_delay >= max_total_time:
            raise LLMTimeoutError("LLM total time budget exceeded during backoff")

        logger.info(
            "LLM call attempt %d failed (status=%s, exc=%s), retrying in %.2fs...",
            attempt,
            last_status,
            type(last_exception).__name__,
            total_delay,
        )
        await asyncio.sleep(total_delay)

    if last_status == 429:
        raise LLMRateLimitError()
    raise LLMError("LLM call failed after maximum retries")
