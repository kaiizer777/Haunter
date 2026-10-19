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


@pytest.mark.fast
async def test_queued_verification_resolves_with_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Queued-but-healthy runs resolve instead of false-failing (#163)."""
    import uuid
    from unittest.mock import AsyncMock, patch

    from app.sandbox.github_actions_runner import GitHubActionsSandboxRunner
    from app.sandbox.runner import SandboxInput

    monkeypatch.setattr(settings, "github_sandbox_app_id", "test-app-id")
    monkeypatch.setattr(settings, "github_sandbox_installation_id", "test-install")

    seen_delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        seen_delays.append(delay)

    queued: list[list[dict[str, str]]] = [[], []]
    done = [{"status": "completed", "conclusion": "success"}]

    async def fake_runs(
        client: object, repo_full: str, head_sha: str, *, token: str
    ) -> list[dict[str, str]]:
        return queued.pop(0) if queued else done

    with (
        patch(
            "app.sandbox.github_actions_runner.mint_installation_token",
            AsyncMock(return_value="tok"),
        ),
        patch(
            "app.sandbox.github_actions_runner.get_or_create_test_mirror",
            AsyncMock(return_value="org/mirror"),
        ),
        patch(
            "app.sandbox.github_actions_runner.push_patch_to_mirror",
            AsyncMock(return_value="abc123"),
        ),
        patch(
            "app.sandbox.github_actions_runner.render_workflow_for_run",
            AsyncMock(side_effect=lambda content, run_id: content),
        ),
        patch(
            "app.sandbox.github_actions_runner._list_workflow_runs",
            side_effect=fake_runs,
        ),
        patch(
            "app.sandbox.github_actions_runner._delete_branch_ref",
            AsyncMock(),
        ),
        patch(
            "app.sandbox.github_actions_runner.asyncio.sleep",
            side_effect=fake_sleep,
        ),
    ):
        inp = SandboxInput(
            patch="diff --git a/a.py b/a.py\n",
            repo_ref="owner/repo",
            run_id=uuid.uuid4(),
            file_paths=["a.py"],
            user_github_id=123,
            attempt_number=1,
        )
        result = await GitHubActionsSandboxRunner().verify(inp)

    assert result["passed"] is True
    assert seen_delays == [10.0, 15.0, 22.5]
