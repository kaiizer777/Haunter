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

    def __init__(self, message: str = "LLM provider rate limit exceeded") -> None:
        super().__init__(message=message, status_code=429)


class LLMInvalidRequestError(LLMError):
    """Raised when provider rejects request due to invalid schema/payload."""

    def __init__(self, message: str = "Invalid LLM request payload") -> None:
        super().__init__(message=message, status_code=400)
