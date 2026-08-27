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


async def execute_with_retry(
    func: Callable[[], Awaitable[dict[str, Any]]],
    max_attempts: int = 3,
    max_total_time: float = 60.0,
    initial_delay: float = 1.0,
    backoff_factor: float = 2.0,
    max_delay: float = 8.0,
) -> dict[str, Any]:
    """
    Execute an async LLM request callable with bounded retries and exponential backoff.

    - Retries on HTTP 429 (Rate Limit), 5xx (Server Error), and network/timeout exceptions.
    - Fails immediately on 400, 401, 403, 422 (client/auth/validation errors).
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
