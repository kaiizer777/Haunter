"""
Exception hierarchy for Haunter LLM module.

Security invariant:
- Exception messages and representations must NEVER leak API keys, authorization
  headers, or internal token secrets.
"""


class LLMError(Exception):
    """Base exception for all LLM client errors."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class LLMTimeoutError(LLMError):
    """Raised when LLM request times out or overall retry budget is exceeded."""

    def __init__(self, message: str = "LLM request timed out") -> None:
        super().__init__(message=message, status_code=504)


class LLMAuthenticationError(LLMError):
    """Raised on 401/403 authentication or authorization failure."""

    def __init__(self, message: str = "LLM provider authentication failed") -> None:
        super().__init__(message=message, status_code=401)


class LLMRateLimitError(LLMError):
    """Raised when rate limits (429) persist across all retry attempts."""

    def __init__(
        self,
        message: str = "LLM provider rate limit exceeded",
        attempts: int = 1,
    ) -> None:
        super().__init__(message=message, status_code=429)
        self.attempts = attempts


class LLMInvalidRequestError(LLMError):
    """Raised when provider rejects request due to invalid schema/payload."""

    def __init__(self, message: str = "Invalid LLM request payload") -> None:
        super().__init__(message=message, status_code=400)


def _truncate_for_failure_reason(s: str, n: int) -> str:
    """Truncate a string to at most n characters, appending an ellipsis when cut.

    The orchestrator's ``_format_failure_reason`` truncates the final stored
    value to 500 chars; this helper trims individual error tokens so the
    composed multi-model message still fits with room for the stage prefix.
    """
    if len(s) <= n:
        return s
    return s[: max(0, n - 1)] + "\u2026"


class LLMExhaustedFreeTierError(LLMError):
    """
    All free-tier models on the configured provider were retried per
    ``ATTEMPTS_PER_MODEL`` and every one failed. Carries the full attempt log
    so the orchestrator's ``_format_failure_reason`` can surface per-model
    last-error info on the dashboard.

    Attributes:
        attempts: Ordered list of ``(model_name, attempt_index, error_message)``
            tuples — one per HTTP call that failed across the whole chain.
    """

    # Per-model last-error truncation budget. Keeps the composed message under
    # the orchestrator's 500-char ``failure_reason`` cap even when 7 models
    # are listed: 7 * (~32 + 4) ~ 252 chars body + ~80 header = ~332 chars,
    # well below the 500-char limit.
    _PER_MODEL_ERR_MAX_CHARS = 32

    def __init__(
        self,
        attempts: list[tuple[str, int, str]],
        message: str | None = None,
    ) -> None:
        self.attempts: list[tuple[str, int, str]] = list(attempts)
        if message is None:
            message = self._build_message(self.attempts)
        super().__init__(message=message, status_code=502)
        self.message = message

    @classmethod
    def _build_message(cls, attempts: list[tuple[str, int, str]]) -> str:
        # Surface the LAST error per model (not every attempt) so the
        # message stays compact. Later attempts overwrite earlier ones
        # because we walk the chain model-by-model, attempt-by-attempt.
        per_model_last_err: dict[str, str] = {}
        for model, _idx, err in attempts:
            per_model_last_err[model] = err

        parts = [
            f"{model}: {_truncate_for_failure_reason(err, cls._PER_MODEL_ERR_MAX_CHARS)}"
            for model, err in per_model_last_err.items()
        ]
        body = ", ".join(parts)
        return (
            f"All {len(per_model_last_err)} free-tier models exhausted "
            f"({len(attempts)} attempts). Last errors: {body}"
        )
