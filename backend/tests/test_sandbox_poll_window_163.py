"""Issue #163 repro: sandbox poll window must cover real GitHub queue times.

Real GitHub Actions queue times routinely exceed the historic 120s fixed
poll window, so queued-but-healthy verifications false-fail and burn fix
attempts. The default poll budget must be >= 600s.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.sandbox.github_actions_runner import _poll_delay_seconds


@pytest.mark.fast
def test_default_poll_timeout_covers_github_queue_times() -> None:
    assert settings.github_sandbox_poll_timeout_seconds >= 600, (
        f"poll timeout default {settings.github_sandbox_poll_timeout_seconds}s "
        "is shorter than real GitHub Actions queue times (>= 600s required)"
    )


@pytest.mark.fast
def test_first_poll_uses_base_interval() -> None:
    assert _poll_delay_seconds(0, 10.0) == 10.0
    assert _poll_delay_seconds(-1, 10.0) == 10.0


@pytest.mark.fast
def test_poll_delay_grows_then_caps() -> None:
    assert _poll_delay_seconds(1, 10.0) == 15.0
    assert _poll_delay_seconds(2, 10.0) == 22.5
    assert _poll_delay_seconds(100, 10.0) == 60.0


@pytest.mark.fast
def test_poll_delay_rejects_bad_base_interval() -> None:
    assert _poll_delay_seconds(0, 0.0) == 10.0
    assert _poll_delay_seconds(0, -5.0) == 10.0
    assert _poll_delay_seconds(3, 0.0) <= 60.0
