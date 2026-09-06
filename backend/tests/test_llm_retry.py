"""
Tests for LLM retry logic and resilience policies (backend/app/llm/retry.py).

Covers:
1. _is_per_model_rejection: status codes, rejection phrases, case-insensitivity, non-rejections.
2. _is_upstream_400_error: all upstream phrases, case-insensitivity, non-upstream 400s.
3. execute_with_retry policies:
   - Policy 1 (429 rate limit): recovery on retry, exhaustion at max_attempts, backoff timing + jitter.
   - Policy 2 (Outage / Unsupported): 5xx, timeouts, httpx.ConnectError, general httpx.TransportError,
     per-model rejection (400/401/403/404), upstream 400, 404 not found.
   - Policy 3 (Global Auth): 401/403 fails fast with LLMAuthenticationError.
   - Client payload error: 400/422 fails fast with LLMInvalidRequestError.
   - Total budget cap: exhaustion mid-backoff and start-of-iteration.
"""

import asyncio
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from app.llm.exceptions import (
    LLMAuthenticationError,
    LLMError,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from app.llm.retry import (
    _PER_MODEL_REJECTION_PHRASES,
    _UPSTREAM_400_PHRASES,
    _is_per_model_rejection,
    _is_upstream_400_error,
    execute_with_retry,
)


def _make_http_status_error(status_code: int, text: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.com/v1/chat/completions")
    response = httpx.Response(status_code=status_code, text=text, request=request)
    return httpx.HTTPStatusError(message=f"HTTP {status_code}", request=request, response=response)


# ---------------------------------------------------------------------------
# 1. _is_per_model_rejection tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phrase", _PER_MODEL_REJECTION_PHRASES)
@pytest.mark.parametrize("status_code", [400, 401, 403, 404])
def test_is_per_model_rejection_all_phrases_and_statuses(phrase: str, status_code: int) -> None:
    body = f"Provider rejection detail: {phrase} on tier"
    assert _is_per_model_rejection(status_code, body) is True


@pytest.mark.parametrize(
    "mixed_case_phrase",
    [
        "IS NOT SUPPORTED",
        "Model Not Supported",
        "ModelError",
        "UNKNOWN MODEL",
        "Invalid Model",
        "MODEL DOES NOT EXIST",
    ],
)
def test_is_per_model_rejection_case_insensitive(mixed_case_phrase: str) -> None:
    assert _is_per_model_rejection(400, f"Error: {mixed_case_phrase}") is True


@pytest.mark.parametrize("status_code", [200, 201, 429, 500, 502, 503])
def test_is_per_model_rejection_status_code_filter(status_code: int) -> None:
    body = "model not supported"
    assert _is_per_model_rejection(status_code, body) is False


def test_is_per_model_rejection_unrelated_400() -> None:
    assert _is_per_model_rejection(400, "customer_key_invalid") is False
    assert _is_per_model_rejection(401, "Invalid bearer token signature") is False
    assert _is_per_model_rejection(404, "Endpoint /v1/chat not found") is False


# ---------------------------------------------------------------------------
# 2. _is_upstream_400_error tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phrase", _UPSTREAM_400_PHRASES)
def test_is_upstream_400_error_all_phrases(phrase: str) -> None:
    body = f"Provider error occurred: {phrase} detected in backend"
    assert _is_upstream_400_error(body) is True


@pytest.mark.parametrize(
    "phrase",
    [
        "UPSTREAM",
        "Provider Error",
        "PROVIDER_ERROR",
        "Server Error",
        "Internal Error",
        "OVERLOADED",
        "Capacity",
        "Model Error",
        "Context Length",
        "Maximum Context",
        "CONTEXT_LENGTH_EXCEEDED",
        "Token Limit",
        "Max_Tokens",
        "Service Unavailable",
        "Bad Gateway",
    ],
)
def test_is_upstream_400_error_case_insensitive(phrase: str) -> None:
    assert _is_upstream_400_error(f"Error detail: {phrase}") is True


def test_is_upstream_400_error_unrelated_body() -> None:
    assert _is_upstream_400_error("invalid json syntax in request") is False
    assert _is_upstream_400_error("field 'messages' is required") is False
    assert _is_upstream_400_error("customer_key_invalid") is False


# ---------------------------------------------------------------------------
# 3. execute_with_retry tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_with_retry_429_recovers_second_attempt() -> None:
    call_count = 0

    async def mock_func() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise _make_http_status_error(429, "rate limited")
        return {"content": "success", "latency_ms": 100}

    with patch("asyncio.sleep", return_value=None):
        result = await execute_with_retry(mock_func, max_attempts=3)

    assert result == {"content": "success", "latency_ms": 100}
    assert call_count == 2


@pytest.mark.asyncio
async def test_execute_with_retry_429_exhausts_max_attempts() -> None:
    call_count = 0

    async def always_429() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        raise _make_http_status_error(429, "rate limited")

    with patch("asyncio.sleep", return_value=None):
        with pytest.raises(LLMRateLimitError) as exc_info:
            await execute_with_retry(always_429, max_attempts=3)

    assert exc_info.value.attempts == 3
    assert exc_info.value.status_code == 429
    assert call_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [500, 502, 503, 504, 599])
async def test_execute_with_retry_5xx_fails_immediately(status_code: int) -> None:
    call_count = 0

    async def fail_5xx() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        raise _make_http_status_error(status_code, "server error")

    with pytest.raises(LLMError) as exc_info:
        await execute_with_retry(fail_5xx, max_attempts=3)

    assert exc_info.value.status_code == status_code
    assert call_count == 1


@pytest.mark.asyncio
async def test_execute_with_retry_connect_error_raises_timeout_error() -> None:
    call_count = 0
    request = httpx.Request("POST", "https://example.com/v1")

    async def fail_connect() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        raise httpx.ConnectError("Failed to establish connection", request=request)

    with pytest.raises(LLMTimeoutError) as exc_info:
        await execute_with_retry(fail_connect, max_attempts=3)

    assert "timed out" in exc_info.value.message.lower()
    assert exc_info.value.status_code == 504
    assert call_count == 1


@pytest.mark.asyncio
async def test_execute_with_retry_general_transport_error_raises_llm_error() -> None:
    call_count = 0
    request = httpx.Request("POST", "https://example.com/v1")

    async def fail_transport() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        raise httpx.ReadError("Connection dropped by peer", request=request)

    with pytest.raises(LLMError) as exc_info:
        await execute_with_retry(fail_transport, max_attempts=3)

    assert not isinstance(exc_info.value, LLMTimeoutError)
    assert "network transport error" in exc_info.value.message.lower()
    assert call_count == 1


@pytest.mark.asyncio
async def test_execute_with_retry_network_timeout_raises_timeout_error() -> None:
    call_count = 0

    async def fail_timeout() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        raise httpx.ReadTimeout("Read timed out")

    with pytest.raises(LLMTimeoutError) as exc_info:
        await execute_with_retry(fail_timeout, max_attempts=3)

    assert exc_info.value.status_code == 504
    assert call_count == 1


@pytest.mark.asyncio
async def test_execute_with_retry_asyncio_timeout_raises_timeout_error() -> None:
    call_count = 0

    async def fail_asyncio_timeout() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        raise asyncio.TimeoutError()

    with pytest.raises(LLMTimeoutError) as exc_info:
        await execute_with_retry(fail_asyncio_timeout, max_attempts=3)

    assert exc_info.value.status_code == 504
    assert call_count == 1


@pytest.mark.asyncio
async def test_execute_with_retry_per_model_rejection_fails_immediately() -> None:
    call_count = 0

    async def fail_per_model() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        raise _make_http_status_error(400, "model 'foo' is not supported on this endpoint")

    with pytest.raises(LLMError) as exc_info:
        await execute_with_retry(fail_per_model, max_attempts=3)

    assert exc_info.value.status_code == 400
    assert "Model rejected by provider" in exc_info.value.message
    assert call_count == 1


@pytest.mark.asyncio
async def test_execute_with_retry_client_schema_error_raises_invalid_request() -> None:
    call_count = 0

    async def fail_client_schema() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        raise _make_http_status_error(400, "customer_key_invalid")

    with pytest.raises(LLMInvalidRequestError) as exc_info:
        await execute_with_retry(fail_client_schema, max_attempts=3)

    assert exc_info.value.status_code == 400
    assert call_count == 1


@pytest.mark.asyncio
async def test_execute_with_retry_422_raises_invalid_request() -> None:
    call_count = 0

    async def fail_422() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        raise _make_http_status_error(422, "Unprocessable Entity")

    with pytest.raises(LLMInvalidRequestError) as exc_info:
        await execute_with_retry(fail_422, max_attempts=3)

    assert exc_info.value.status_code == 400
    assert call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403])
async def test_execute_with_retry_auth_fails_immediately(status_code: int) -> None:
    call_count = 0

    async def fail_auth() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        raise _make_http_status_error(status_code, "Invalid API key")

    with pytest.raises(LLMAuthenticationError) as exc_info:
        await execute_with_retry(fail_auth, max_attempts=3)

    assert exc_info.value.status_code == 401
    assert call_count == 1


@pytest.mark.asyncio
async def test_execute_with_retry_404_endpoint_not_found() -> None:
    call_count = 0

    async def fail_404() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        raise _make_http_status_error(404, "Endpoint not found")

    with pytest.raises(LLMError) as exc_info:
        await execute_with_retry(fail_404, max_attempts=3)

    assert exc_info.value.status_code == 404
    assert "LLM model or endpoint not found" in exc_info.value.message
    assert call_count == 1


@pytest.mark.asyncio
async def test_execute_with_retry_upstream_400_fails_immediately() -> None:
    call_count = 0

    async def fail_upstream() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        raise _make_http_status_error(400, "error: context length exceeded")

    with pytest.raises(LLMError) as exc_info:
        await execute_with_retry(fail_upstream, max_attempts=3)

    assert exc_info.value.status_code == 400
    assert "Upstream provider error" in exc_info.value.message
    assert call_count == 1


@pytest.mark.asyncio
async def test_execute_with_retry_unexpected_status_fallthrough() -> None:
    call_count = 0

    async def fail_418() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        raise _make_http_status_error(418, "I'm a teapot")

    with pytest.raises(LLMError) as exc_info:
        await execute_with_retry(fail_418, max_attempts=3)

    assert exc_info.value.status_code == 418
    assert call_count == 1


@pytest.mark.asyncio
async def test_execute_with_retry_budget_exhausted_mid_backoff() -> None:
    call_count = 0

    async def fail_429() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        raise _make_http_status_error(429, "rate limited")

    with pytest.raises(LLMTimeoutError) as exc_info:
        await execute_with_retry(
            fail_429,
            max_attempts=3,
            max_total_time=0.05,
            initial_delay=1.0,
        )

    assert "budget exceeded" in exc_info.value.message.lower()
    assert call_count == 1


@pytest.mark.asyncio
async def test_execute_with_retry_budget_exhausted_at_loop_start() -> None:
    call_count = 0

    async def mock_func() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        return {"content": "ok"}

    with pytest.raises(LLMTimeoutError) as exc_info:
        await execute_with_retry(
            mock_func,
            max_attempts=3,
            max_total_time=0.0,
        )

    assert "exceeded total retry budget" in exc_info.value.message.lower()
    assert call_count == 0


@pytest.mark.asyncio
async def test_execute_with_retry_exponential_backoff_timing_and_jitter() -> None:
    sleep_durations: list[float] = []

    async def capture_sleep(delay: float) -> None:
        sleep_durations.append(delay)

    call_count = 0

    async def fail_429_thrice() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        if call_count <= 3:
            raise _make_http_status_error(429, "rate limited")
        return {"content": "ok"}

    initial_delay = 1.0
    backoff_factor = 2.0
    max_delay = 15.0

    with patch("asyncio.sleep", side_effect=capture_sleep):
        res = await execute_with_retry(
            fail_429_thrice,
            max_attempts=4,
            max_total_time=60.0,
            initial_delay=initial_delay,
            backoff_factor=backoff_factor,
            max_delay=max_delay,
        )

    assert res == {"content": "ok"}
    assert call_count == 4
    # Exactly 3 retries -> 3 sleep calls
    assert len(sleep_durations) == 3

    for attempt_idx, slept in enumerate(sleep_durations, start=1):
        expected_base = min(initial_delay * (backoff_factor ** (attempt_idx - 1)), max_delay)
        # jitter is random.uniform(0.1, 0.5)
        min_expected = expected_base + 0.1
        max_expected = expected_base + 0.5
        assert min_expected <= slept <= max_expected, (
            f"Attempt {attempt_idx} sleep {slept:.3f}s outside bounds [{min_expected:.3f}, {max_expected:.3f}]"
        )
