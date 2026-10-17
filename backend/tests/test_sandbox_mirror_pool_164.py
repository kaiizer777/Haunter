r"""
Issue #164 repro: all sandbox verifications share one mirror repo.

Every verification run resolves to ``get_universal_sandbox_repo(org)`` — a
single ``{org}/{settings.github_sandbox_repo}`` — so concurrent runs contend
on the same repo and risk GitHub API rate limits. The fix adds a small
mirror pool (``settings.github_sandbox_repo_pool``) with deterministic
per-run routing (``select_sandbox_mirror``) so parallel verifications spread
across mirrors.

This file is **sync** and **DB-free**: routing is a pure function of
(settings, key). No fixture, no async, no engine.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
import respx

from app.config import settings
from app.sandbox.github_actions_runner import GitHubActionsSandboxRunner
from app.sandbox.mirror import (
    get_sandbox_mirror_pool,
    get_universal_sandbox_repo,
    select_sandbox_mirror,
)
from app.sandbox.runner import SandboxInput


def _set_pool(monkeypatch: pytest.MonkeyPatch, primary: str, pool: str) -> None:
    monkeypatch.setattr(settings, "github_sandbox_repo", primary)
    monkeypatch.setattr(settings, "github_sandbox_repo_pool", pool)


def test_pool_defaults_to_single_mirror(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no pool configured, every key routes to the universal mirror."""
    _set_pool(monkeypatch, "haunter-sandbox-runner", "")
    for key in ("run-a", "run-b", "run-c"):
        assert select_sandbox_mirror("test-org", key) == (
            "test-org/haunter-sandbox-runner"
        )
    assert get_sandbox_mirror_pool() == ["haunter-sandbox-runner"]


def test_pool_spreads_distinct_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a pool configured, distinct run keys spread across mirrors."""
    _set_pool(monkeypatch, "mirror-a", "mirror-b, mirror-c")
    seen = {select_sandbox_mirror("test-org", f"run-{i}") for i in range(50)}
    assert len(seen) > 1
    assert seen <= {
        "test-org/mirror-a",
        "test-org/mirror-b",
        "test-org/mirror-c",
    }


def test_pool_routing_is_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same key always routes to the same mirror (a run's attempts stay together)."""
    _set_pool(monkeypatch, "mirror-a", "mirror-b, mirror-c")
    for key in ("run-1", "run-2", "some-other-key"):
        first = select_sandbox_mirror("test-org", key)
        assert select_sandbox_mirror("test-org", key) == first


def test_pool_ignores_blanks_and_dupes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Blank entries are dropped; the primary and dupes appear exactly once."""
    _set_pool(monkeypatch, "mirror-a", " , mirror-b,,mirror-a, mirror-b ")
    assert get_sandbox_mirror_pool() == ["mirror-a", "mirror-b"]
    assert select_sandbox_mirror("test-org", "any-key").startswith("test-org/")


def test_pool_single_mirror_matches_universal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single-mirror routing is identical to get_universal_sandbox_repo."""
    _set_pool(monkeypatch, "custom-sandbox-runner", "")
    assert select_sandbox_mirror("my-org", "run-9") == get_universal_sandbox_repo(
        "my-org"
    )


def _mock_mirror_run(rx: respx.MockRouter, repo_full: str, branch: str):
    """Mock the GitHub API surface verify() touches for one mirror repo."""
    rx.get(f"/repos/{repo_full}").respond(200, json={"full_name": repo_full})
    tree_route = rx.post(f"/repos/{repo_full}/git/trees").respond(
        201, json={"sha": "tree_sha_pool"}
    )
    rx.post(f"/repos/{repo_full}/git/commits").respond(
        201, json={"sha": "commit_sha_pool"}
    )
    rx.post(f"/repos/{repo_full}/git/refs").respond(
        201, json={"ref": f"refs/heads/{branch}", "sha": "commit_sha_pool"}
    )
    rx.get(f"/repos/{repo_full}/actions/runs").respond(
        200,
        json={
            "workflow_runs": [
                {"id": 999, "status": "completed", "conclusion": "success"}
            ]
        },
    )
    rx.delete(f"/repos/{repo_full}/git/refs/heads/{branch}").respond(204)
    return tree_route


async def _run_verify(run_id: uuid.UUID, attempt: int = 1):
    """Run verify() for one attempt with a minimal patch (no seed tree)."""
    runner = GitHubActionsSandboxRunner()
    inp = SandboxInput(
        run_id=run_id,
        repo_ref="owner/repo",
        patch="--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-a\n+b\n",
        attempt_number=attempt,
        user_github_id=123,
    )
    return await runner.verify(inp)


def _base_settings(monkeypatch: pytest.MonkeyPatch, pool: str) -> None:
    monkeypatch.setattr(settings, "github_sandbox_app_id", "app_123")
    monkeypatch.setattr(settings, "github_sandbox_installation_id", "inst_123")
    monkeypatch.setattr(settings, "github_sandbox_org", "test-org")
    monkeypatch.setattr(settings, "github_sandbox_repo", "mirror-a")
    monkeypatch.setattr(settings, "github_sandbox_repo_pool", pool)
    monkeypatch.setattr(settings, "github_sandbox_poll_interval_seconds", 0.01)
    monkeypatch.setattr(settings, "github_sandbox_poll_timeout_seconds", 1.0)

