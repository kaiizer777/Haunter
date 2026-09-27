"""
Phase 4.1 — Backend Tool verify_in_ci_sandbox & Mirror Bridge.

Covers (future.md §4.3.1 + §4.4 Phase 1):
  1. Tool definition present in SessionOrchestrator._TOOLS with exact spec shape.
  2. System prompt advertises verify_in_ci_sandbox.
  3. Timeout clamp to [1, 600] (default 180).
  4. Empty patches handling (no verifier call, error string).
  5. Local vs github_actions routing via settings.sandbox_provider.
  6. Streaming events emitted (sandbox_queued, terminal_output, sandbox_status,
     sandbox_progress).
  7. Result formatting (PASSED/FAILED, exit code, duration, logs, run URL).
  8. Orchestrator dispatch reads staged_patches + repo and delegates.
  9. verify_session_patches streams intermediate polling logs to queue.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.session_streamer import SseQueue, _ALLOWED_EVENTS, format_sse_event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(repo: Any | None = None) -> MagicMock:
    session = MagicMock()
    session.id = uuid.uuid4()
    session.base_sha = "a" * 40
    session.staged_patches = {
        "src/auth.py": "--- a/src/auth.py\n+++ b/src/auth.py\n@@ -1 +1 @@\n-old\n+new\n"
    }
    session.user_github_id = None
    if repo is not None:
        session.repo = repo
    return session


def _make_repo() -> MagicMock:
    repo = MagicMock()
    repo.owner = "test-org"
    repo.name = "test-repo"
    return repo


async def _drain_queue(queue: SseQueue) -> list[str]:
    items: list[str] = []
    while True:
        try:
            item = await asyncio.wait_for(queue._q.get(), timeout=0.5)
        except asyncio.TimeoutError:
            break
        items.append(item)
    return items


# ---------------------------------------------------------------------------
# 1. Tool definition present (exact spec §4.3.1 shape)
# ---------------------------------------------------------------------------


def test_tool_definition_present() -> None:
    from app.services.session_orchestrator import _TOOLS

    defs = [
        t for t in _TOOLS if t.get("function", {}).get("name") == "verify_in_ci_sandbox"
    ]
    assert (
        len(defs) == 1
    ), "verify_in_ci_sandbox must be registered exactly once in _TOOLS"
    fn = defs[0]["function"]
    assert "isolated GitHub Actions CI sandbox mirror repo" in fn["description"]
    assert "streams CI logs live" in fn["description"]
    assert "self-healing" in fn["description"]

    params = fn["parameters"]
    assert params["type"] == "object"
    props = params["properties"]
    assert "workflow_file" in props
    assert props["workflow_file"]["type"] == "string"
    assert "timeout_sec" in props
    assert props["timeout_sec"]["type"] == "integer"
    assert "180" in props["timeout_sec"]["description"]
    assert "600" in props["timeout_sec"]["description"]


def test_system_prompt_advertises_tool() -> None:
    from app.services.session_orchestrator import _build_system_prompt

    prompt = _build_system_prompt(
        repo_owner="o",
        repo_name="r",
        branch_name="b",
        base_sha="a" * 40,
        staged_patches={},
    )
    assert "verify_in_ci_sandbox(workflow_file, timeout_sec)" in prompt


def test_tool_exported_from_sandbox_module() -> None:
    from app.services.session_tools import sandbox as sandbox_mod

    assert hasattr(sandbox_mod, "tool_verify_ci_sandbox")
    assert hasattr(sandbox_mod, "clamp_ci_timeout")
    assert sandbox_mod._CI_MAX_TIMEOUT == 600
    assert sandbox_mod._CI_MIN_TIMEOUT == 1
    assert sandbox_mod._CI_DEFAULT_TIMEOUT == 180


# ---------------------------------------------------------------------------
# 2. SSE events allowed + helpers
# ---------------------------------------------------------------------------


def test_sse_events_allowed() -> None:
    assert "sandbox_queued" in _ALLOWED_EVENTS
    assert "sandbox_progress" in _ALLOWED_EVENTS
    # Pre-existing events still allowed (no regressions).
    assert "sandbox_status" in _ALLOWED_EVENTS
    assert "terminal_output" in _ALLOWED_EVENTS


def test_sse_helpers_wire_format() -> None:
    wire = format_sse_event("sandbox_queued", {"run_url": "u", "workflow_name": "w"})
    assert wire.startswith("event: sandbox_queued\n")
    assert wire.endswith("\n\n")

    wire2 = format_sse_event(
        "sandbox_progress", {"step_name": "dispatch", "status": "in_progress"}
    )
    assert wire2.startswith("event: sandbox_progress\n")

    with pytest.raises(ValueError):
        format_sse_event("bogus_event_xyz", {})


@pytest.mark.asyncio
async def test_sse_queue_helpers_emit() -> None:
    queue = SseQueue()
    await queue.put_sandbox_queued(
        run_url="https://example.com/run/1", workflow_name="ci.yml"
    )
    await queue.put_sandbox_progress(step_name="dispatch", status="in_progress")
    items = await _drain_queue(queue)
    assert any("sandbox_queued" in i and "ci.yml" in i for i in items)
    assert any("sandbox_progress" in i and "dispatch" in i for i in items)

    with pytest.raises(ValueError):
        await queue.put_sandbox_progress(step_name="x", status="bogus")


# ---------------------------------------------------------------------------
# 3. Timeout clamp
# ---------------------------------------------------------------------------


def test_clamp_ci_timeout_bounds() -> None:
    from app.services.session_tools.sandbox import clamp_ci_timeout

    assert clamp_ci_timeout(180) == 180
    assert clamp_ci_timeout(0) == 1
    assert clamp_ci_timeout(-5) == 1
    assert clamp_ci_timeout(9999) == 600
    assert clamp_ci_timeout(600) == 600
    assert clamp_ci_timeout(None) == 180
    assert clamp_ci_timeout("bad") == 180


@pytest.mark.asyncio
async def test_timeout_clamped_before_github_dispatch() -> None:
    from app.services.session_tools.sandbox import tool_verify_ci_sandbox

    session = _make_session()
    repo = _make_repo()
    patches = {"src/a.py": "@@ diff\n"}

    seen: dict[str, Any] = {}

    async def _fake_verify(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"status": "passed", "passed": True, "run_url": "u", "logs": "ok"}

    with (
        patch("app.config.settings.sandbox_provider", "github_actions", create=True),
        patch(
            "app.subagents.sandbox_verifier.verify_session_patches",
            side_effect=_fake_verify,
        ),
    ):
        await tool_verify_ci_sandbox(
            timeout_sec=9999,
            queue=None,
            session=session,
            repo=repo,
            staged_patches=patches,
        )
    assert seen.get("timeout_sec") == 600

    seen.clear()
    with (
        patch("app.config.settings.sandbox_provider", "github_actions", create=True),
        patch(
            "app.subagents.sandbox_verifier.verify_session_patches",
            side_effect=_fake_verify,
        ),
    ):
        await tool_verify_ci_sandbox(
            timeout_sec=0,
            queue=None,
            session=session,
            repo=repo,
            staged_patches=patches,
        )
    assert seen.get("timeout_sec") == 1


# ---------------------------------------------------------------------------
# 4. Empty patches handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_patches_returns_error_without_dispatch() -> None:
    from app.services.session_tools.sandbox import tool_verify_ci_sandbox

    with patch(
        "app.subagents.sandbox_verifier.verify_session_patches",
        new_callable=AsyncMock,
    ) as mock_verify:
        result = await tool_verify_ci_sandbox(
            queue=None, session=_make_session(), repo=_make_repo(), staged_patches={}
        )
    assert result.startswith("Error:")
    assert "No staged patches" in result
    mock_verify.assert_not_called()


@pytest.mark.asyncio
async def test_empty_patches_falls_back_to_session_state() -> None:
    """When staged_patches arg is omitted, session.staged_patches is used."""
    from app.services.session_tools.sandbox import tool_verify_ci_sandbox

    session = _make_session()
    repo = _make_repo()

    async def _fake_verify(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["staged_patches"] == session.staged_patches
        return {"status": "passed", "passed": True, "run_url": "u", "logs": "ok"}

    with (
        patch("app.config.settings.sandbox_provider", "github_actions", create=True),
        patch(
            "app.subagents.sandbox_verifier.verify_session_patches",
            side_effect=_fake_verify,
        ),
    ):
        result = await tool_verify_ci_sandbox(queue=None, session=session, repo=repo)
    assert "PASSED" in result


@pytest.mark.asyncio
async def test_missing_session_repo_in_github_mode_errors() -> None:
    from app.services.session_tools.sandbox import tool_verify_ci_sandbox

    with patch("app.config.settings.sandbox_provider", "github_actions", create=True):
        result = await tool_verify_ci_sandbox(
            queue=None,
            session=None,
            repo=None,
            staged_patches={"a.py": "diff"},
        )
    assert result.startswith("Error:")
    assert "Session and repo context" in result


# ---------------------------------------------------------------------------
# 5. Local vs github routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_provider_routes_to_targeted_tests() -> None:
    from app.services.session_tools.sandbox import tool_verify_ci_sandbox

    session = _make_session()
    repo = _make_repo()
    patches = {"src/auth.py": "@@ diff\n", "tests/test_auth.py": "@@ diff\n"}

    with (
        patch("app.config.settings.sandbox_provider", "local", create=True),
        patch(
            "app.services.session_tools.sandbox.tool_run_targeted_tests",
            new_callable=AsyncMock,
            return_value="Test runner: pytest\nStatus: PASSED\nExit code: 0",
        ) as mock_local,
        patch(
            "app.subagents.sandbox_verifier.verify_session_patches",
            new_callable=AsyncMock,
        ) as mock_gh,
    ):
        result = await tool_verify_ci_sandbox(
            queue=None,
            session=session,
            repo=repo,
            staged_patches=patches,
            timeout_sec=120,
        )

    mock_local.assert_awaited_once()
    mock_gh.assert_not_called()
    assert "provider=local" in result
    assert "PASSED" in result
    # Local path forwards the clamped timeout.
    assert mock_local.call_args.kwargs["timeout_sec"] == 120


@pytest.mark.asyncio
async def test_github_provider_routes_to_verify_session_patches() -> None:
    from app.services.session_tools.sandbox import tool_verify_ci_sandbox

    session = _make_session()
    repo = _make_repo()
    patches = {"src/auth.py": "@@ diff\n"}

    with (
        patch("app.config.settings.sandbox_provider", "github_actions", create=True),
        patch(
            "app.services.session_tools.sandbox.tool_run_targeted_tests",
            new_callable=AsyncMock,
        ) as mock_local,
        patch(
            "app.subagents.sandbox_verifier.verify_session_patches",
            new_callable=AsyncMock,
            return_value={
                "status": "passed",
                "passed": True,
                "run_url": "https://github.com/o/r/actions/runs/1",
                "logs": "All green",
            },
        ) as mock_gh,
    ):
        result = await tool_verify_ci_sandbox(
            workflow_file="ci.yml",
            timeout_sec=200,
            queue=None,
            session=session,
            repo=repo,
            staged_patches=patches,
            gh_token="tok",
        )

    mock_local.assert_not_called()
    mock_gh.assert_awaited_once()
    kwargs = mock_gh.call_args.kwargs
    assert kwargs["session"] is session
    assert kwargs["repo"] is repo
    assert kwargs["staged_patches"] == patches
    assert kwargs["gh_token"] == "tok"
    assert kwargs["workflow_file"] == "ci.yml"
    assert kwargs["timeout_sec"] == 200
    assert "PASSED" in result


# ---------------------------------------------------------------------------
# 6. Streaming events emitted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_github_path_emits_streaming_events() -> None:
    from app.services.session_tools.sandbox import tool_verify_ci_sandbox

    queue = SseQueue()
    session = _make_session()
    repo = _make_repo()

    with (
        patch("app.config.settings.sandbox_provider", "github_actions", create=True),
        patch(
            "app.subagents.sandbox_verifier.verify_session_patches",
            new_callable=AsyncMock,
            return_value={
                "status": "passed",
                "passed": True,
                "run_url": "https://github.com/o/r/actions/runs/9",
                "logs": "All green",
            },
        ),
    ):
        await tool_verify_ci_sandbox(
            queue=queue,
            session=session,
            repo=repo,
            staged_patches={"src/a.py": "@@ d\n"},
        )

    items = await _drain_queue(queue)
    blob = "\n".join(items)
    # verify_session_patches streams these; the tool must not swallow them.
    assert "sandbox_queued" in blob
    assert "terminal_output" in blob
    assert "sandbox_status" in blob


@pytest.mark.asyncio
async def test_local_path_emits_sandbox_queued() -> None:
    from app.services.session_tools.sandbox import tool_verify_ci_sandbox

    queue = SseQueue()
    with (
        patch("app.config.settings.sandbox_provider", "local", create=True),
        patch(
            "app.services.session_tools.sandbox.tool_run_targeted_tests",
            new_callable=AsyncMock,
            return_value="Test runner: pytest\nStatus: PASSED",
        ),
    ):
        await tool_verify_ci_sandbox(
            queue=queue,
            session=_make_session(),
            repo=_make_repo(),
            staged_patches={"src/a.py": "@@ d\n"},
        )
    items = await _drain_queue(queue)
    assert any("sandbox_queued" in i for i in items)


@pytest.mark.asyncio
async def test_verify_session_patches_streams_polling_logs() -> None:
    """Direct verifier call with a queue must emit terminal_output."""
    from app.subagents.sandbox_verifier import verify_session_patches

    queue = SseQueue()
    session = _make_session()
    repo = _make_repo()

    fake_runner = MagicMock()
    fake_runner.verify = AsyncMock(
        return_value={
            "passed": True,
            "reason": "ok",
            "run_url": "https://x/run/1",
            "duration_ms": 10,
        }
    )
    with patch(
        "app.sandbox._load_github_actions_runner", return_value=lambda: fake_runner
    ):
        result = await verify_session_patches(
            session=session,
            repo=repo,
            staged_patches={"src/a.py": "--- a\n+++ b\n@@\n"},
            queue=queue,
            workflow_file="ci.yml",
            timeout_sec=60,
        )
    assert result["passed"] is True
    items = await _drain_queue(queue)
    blob = "\n".join(items)
    assert "terminal_output" in blob
    assert "sandbox_queued" in blob
    assert "sandbox_progress" in blob


# ---------------------------------------------------------------------------
# 7. Result formatting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_result_formatting_passed() -> None:
    from app.services.session_tools.sandbox import tool_verify_ci_sandbox

    with (
        patch("app.config.settings.sandbox_provider", "github_actions", create=True),
        patch(
            "app.subagents.sandbox_verifier.verify_session_patches",
            new_callable=AsyncMock,
            return_value={
                "status": "passed",
                "passed": True,
                "run_url": "https://github.com/o/r/actions/runs/42",
                "logs": "All tests passed",
            },
        ),
    ):
        result = await tool_verify_ci_sandbox(
            workflow_file="ci.yml",
            queue=None,
            session=_make_session(),
            repo=_make_repo(),
            staged_patches={"src/a.py": "@@ d\n"},
        )
    assert "CI sandbox verification: PASSED" in result
    assert "Exit code: 0" in result
    assert "Duration:" in result
    assert "https://github.com/o/r/actions/runs/42" in result
    assert "All tests passed" in result
    assert "Workflow: ci.yml" in result


@pytest.mark.asyncio
async def test_result_formatting_failed() -> None:
    from app.services.session_tools.sandbox import tool_verify_ci_sandbox

    with (
        patch("app.config.settings.sandbox_provider", "github_actions", create=True),
        patch(
            "app.subagents.sandbox_verifier.verify_session_patches",
            new_callable=AsyncMock,
            return_value={
                "status": "failed",
                "passed": False,
                "run_url": "https://github.com/o/r/actions/runs/43",
                "logs": "FAILED tests/test_auth.py::test_login\nAssertionError: boom",
            },
        ),
    ):
        result = await tool_verify_ci_sandbox(
            queue=None,
            session=_make_session(),
            repo=_make_repo(),
            staged_patches={"src/a.py": "@@ d\n"},
        )
    assert "CI sandbox verification: FAILED" in result
    assert "Exit code: 1" in result
    assert "AssertionError: boom" in result


# ---------------------------------------------------------------------------
# 8. Orchestrator dispatch
# ---------------------------------------------------------------------------


def _make_orchestrator() -> Any:
    from app.services.session_orchestrator import SessionOrchestrator

    orch = SessionOrchestrator(session_id=uuid.uuid4(), db=MagicMock(), gh_token="tok")
    orch._llm = AsyncMock()
    return orch


@pytest.mark.asyncio
async def test_orchestrator_dispatch_delegates_with_session_state() -> None:
    orch = _make_orchestrator()
    session = _make_session(repo=_make_repo())
    staged = {"src/auth.py": "@@ diff\n"}
    queue = AsyncMock()

    with patch(
        "app.services.session_orchestrator.tool_verify_ci_sandbox",
        new_callable=AsyncMock,
        return_value="CI sandbox verification: PASSED\nExit code: 0",
    ) as mock_tool:
        result = await orch._dispatch_tool(
            tool_name="verify_in_ci_sandbox",
            args={"workflow_file": "ci.yml", "timeout_sec": 200},
            repo_owner="test-org",
            repo_name="test-repo",
            base_sha="a" * 40,
            staged_patches=staged,
            queue=queue,
            session=session,
        )

    assert "PASSED" in result
    mock_tool.assert_awaited_once()
    kwargs = mock_tool.call_args.kwargs
    assert kwargs["staged_patches"] is staged
    assert kwargs["session"] is session
    assert kwargs["timeout_sec"] == 200
    assert kwargs["workflow_file"] == "ci.yml"


@pytest.mark.asyncio
async def test_orchestrator_dispatch_requires_session() -> None:
    orch = _make_orchestrator()
    result = await orch._dispatch_tool(
        tool_name="verify_in_ci_sandbox",
        args={},
        repo_owner="o",
        repo_name="r",
        base_sha="a" * 40,
        staged_patches={},
        queue=AsyncMock(),
        session=None,
    )
    assert result.startswith("Error:")
    assert "Session context" in result
