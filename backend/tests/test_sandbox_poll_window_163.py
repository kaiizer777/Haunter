"""Issue #163 repro: sandbox poll window must cover real GitHub queue times.

Real GitHub Actions queue times routinely exceed the historic 120s fixed
poll window, so queued-but-healthy verifications false-fail and burn fix
attempts. The default poll budget must be >= 600s.
"""

from __future__ import annotations

import pytest

from app.config import settings


@pytest.mark.fast
def test_default_poll_timeout_covers_github_queue_times() -> None:
    assert settings.github_sandbox_poll_timeout_seconds >= 600, (
        f"poll timeout default {settings.github_sandbox_poll_timeout_seconds}s "
        "is shorter than real GitHub Actions queue times (>= 600s required)"
    )
