"""
Tests for LLM exception hierarchy (backend/app/llm/exceptions.py).

Covers:
1. LLMError base attributes, message, and status_code.
2. Subclasses default status codes and messages (LLMTimeoutError, LLMAuthenticationError,
   LLMRateLimitError, LLMInvalidRequestError).
3. LLMRateLimitError attempts attribute.
4. _truncate_for_failure_reason truncation rules and ellipsis suffix.
5. LLMExhaustedFreeTierError formatting, last-error deduplication, truncation, and ordering.
"""

import pytest

from app.llm.exceptions import (
    LLMAuthenticationError,
    LLMError,
    LLMExhaustedFreeTierError,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMTimeoutError,
    _truncate_for_failure_reason,
)


class TestLLMError:
    def test_message_and_status_code(self) -> None:
        err = LLMError("custom error", 500)
        assert err.message == "custom error"
        assert err.status_code == 500
        assert str(err) == "custom error"
        assert isinstance(err, Exception)

    def test_default_status_code_is_none(self) -> None:
        err = LLMError("no status code")
        assert err.message == "no status code"
        assert err.status_code is None
        assert str(err) == "no status code"


class TestLLMTimeoutError:
    def test_default_values(self) -> None:
        err = LLMTimeoutError()
        assert err.message == "LLM request timed out"
        assert err.status_code == 504
        assert isinstance(err, LLMError)

    def test_custom_message(self) -> None:
        err = LLMTimeoutError("Gateway timed out after 30s")
        assert err.message == "Gateway timed out after 30s"
        assert err.status_code == 504


class TestLLMAuthenticationError:
    def test_default_values(self) -> None:
        err = LLMAuthenticationError()
        assert err.message == "LLM provider authentication failed"
        assert err.status_code == 401
        assert isinstance(err, LLMError)

    def test_custom_message(self) -> None:
        err = LLMAuthenticationError("Invalid API key")
        assert err.message == "Invalid API key"
        assert err.status_code == 401


class TestLLMRateLimitError:
    def test_default_values(self) -> None:
        err = LLMRateLimitError()
        assert err.message == "LLM provider rate limit exceeded"
        assert err.status_code == 429
        assert err.attempts == 1
        assert isinstance(err, LLMError)

    def test_custom_attempts_and_message(self) -> None:
        err = LLMRateLimitError("Rate limit persisted across all attempts", attempts=3)
        assert err.message == "Rate limit persisted across all attempts"
        assert err.status_code == 429
        assert err.attempts == 3


class TestLLMInvalidRequestError:
    def test_default_values(self) -> None:
        err = LLMInvalidRequestError()
        assert err.message == "Invalid LLM request payload"
        assert err.status_code == 400
        assert isinstance(err, LLMError)

    def test_custom_message(self) -> None:
        err = LLMInvalidRequestError("Missing required parameter: messages")
        assert err.message == "Missing required parameter: messages"
        assert err.status_code == 400


class TestTruncateHelper:
    def test_under_or_equal_budget_unchanged(self) -> None:
        assert _truncate_for_failure_reason("short", 10) == "short"
        assert _truncate_for_failure_reason("exact_10ch", 10) == "exact_10ch"
        assert _truncate_for_failure_reason("", 5) == ""

    def test_over_budget_truncated_with_ellipsis(self) -> None:
        # n = 5: max(0, 4) = 4 chars + ellipsis
        res = _truncate_for_failure_reason("123456", 5)
        assert res == "1234\u2026"
        assert len(res) == 5

    def test_zero_or_negative_budget(self) -> None:
        res = _truncate_for_failure_reason("hello", 0)
        assert res == "\u2026"


class TestLLMExhaustedFreeTierError:
    def test_empty_attempts_message(self) -> None:
        err = LLMExhaustedFreeTierError([])
        assert err.status_code == 502
        assert err.attempts == []
        expected = "All 0 free-tier models exhausted (0 attempts). Last errors: "
        assert err.message == expected
        assert str(err) == expected
        assert isinstance(err, LLMError)

    def test_single_attempt(self) -> None:
        err = LLMExhaustedFreeTierError([("m1", 1, "boom")])
        assert err.status_code == 502
        assert err.attempts == [("m1", 1, "boom")]
        assert "All 1 free-tier models exhausted (1 attempts). Last errors: m1: boom" == err.message

    def test_long_error_truncated_at_cap(self) -> None:
        max_chars = LLMExhaustedFreeTierError._PER_MODEL_ERR_MAX_CHARS
        exact_err = "e" * max_chars
        err_exact = LLMExhaustedFreeTierError([("m1", 1, exact_err)])
        assert f"m1: {exact_err}" in err_exact.message

        long_err = "e" * (max_chars + 10)
        err_long = LLMExhaustedFreeTierError([("m1", 1, long_err)])
        expected_truncated = ("e" * (max_chars - 1)) + "\u2026"
        assert f"m1: {expected_truncated}" in err_long.message

    def test_multiple_attempts_same_model_keeps_last_error(self) -> None:
        attempts = [
            ("nemotron-3.5", 1, "first error"),
            ("nemotron-3.5", 2, "final error"),
        ]
        err = LLMExhaustedFreeTierError(attempts)
        assert "first error" not in err.message
        assert "final error" in err.message
        assert "All 1 free-tier models exhausted (2 attempts). Last errors: nemotron-3.5: final error" == err.message

    @pytest.mark.parametrize(
        ("model_order", "expected_body"),
        [
            (
                [("model-a", 1, "err-a"), ("model-b", 1, "err-b")],
                "model-a: err-a, model-b: err-b",
            ),
            (
                [("model-b", 1, "err-b"), ("model-a", 1, "err-a")],
                "model-b: err-b, model-a: err-a",
            ),
            (
                [("m1", 1, "e1"), ("m2", 1, "e2"), ("m3", 1, "e3")],
                "m1: e1, m2: e2, m3: e3",
            ),
        ],
    )
    def test_message_matches_iteration_order(
        self,
        model_order: list[tuple[str, int, str]],
        expected_body: str,
    ) -> None:
        err = LLMExhaustedFreeTierError(model_order)
        assert expected_body in err.message
        assert err.attempts == model_order

    def test_custom_message_override(self) -> None:
        err = LLMExhaustedFreeTierError(
            [("m1", 1, "err")],
            message="Explicitly provided exhaustion message",
        )
        assert err.message == "Explicitly provided exhaustion message"
        assert str(err) == "Explicitly provided exhaustion message"
        assert err.status_code == 502
