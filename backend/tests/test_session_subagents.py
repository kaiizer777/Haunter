"""
Unit tests for Subagent Core Phase 1 (future.md §1.3.3).

All tests are unit-level. LLMClient.complete and SseQueue are mocked —
no real GitHub calls, no real LLM calls.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.session_streamer import format_sse_event
from app.services.session_tools.subagents import (
    ROLE_CONFIGS,
    VALID_ROLES,
    SubagentRunner,
)


def _make_runner(
    role: str = "repo_navigator",
    task: str = "Explore the auth module.",
    staged_patches: dict[str, str] | None = None,
    llm_response: dict[str, Any] | None = None,
    target_files: list[str] | None = None,
) -> tuple[SubagentRunner, AsyncMock, AsyncMock, dict[str, str]]:
    """Build a SubagentRunner with mocked session/queue/llm."""
    patches: dict[str, str] = staged_patches if staged_patches is not None else {}
    session = MagicMock()
    session.staged_patches = patches
    session.id = "session-test"
    queue = AsyncMock()
    llm = AsyncMock()
    if llm_response is not None:
        llm.complete.return_value = llm_response
    runner = SubagentRunner(
        role=role,
        task=task,
        target_files=target_files or [],
        repo_owner="test-org",
        repo_name="test-repo",
        base_sha="a" * 40,
        staged_patches=patches,
        session=session,
        queue=queue,
        llm=llm,
        gh_token=None,
        model=None,
        provider=None,
    )
    return runner, queue, llm, patches


def _tool_call(
    name: str, args: dict[str, Any], call_id: str = "call_1"
) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


# ---------------------------------------------------------------------------
# 1. Role allowlists are subsets of the parent _TOOLS surface
# ---------------------------------------------------------------------------


def test_role_config_tool_allowlist() -> None:
    from app.services.session_orchestrator import _TOOLS

    assert VALID_ROLES == frozenset(
        {
            "repo_navigator",
            "feature_architect",
            "bug_hunter",
            "sandbox_verifier",
            "code_guardian",
        }
    )
    assert set(ROLE_CONFIGS.keys()) == set(VALID_ROLES)

    parent_names = {t["function"]["name"] for t in _TOOLS}
    for role, config in ROLE_CONFIGS.items():
        assert config.role == role
        assert config.max_iterations > 0
        unknown = set(config.allowed_tools) - parent_names
        assert not unknown, f"role={role} has unknown tools: {sorted(unknown)}"

    # code_guardian is read-only by design — no editor tools.
    guardian_tools = set(ROLE_CONFIGS["code_guardian"].allowed_tools)
    assert not (
        guardian_tools
        & {"str_replace", "create_file", "delete_file", "apply_multi_patch"}
    )


# ---------------------------------------------------------------------------
# 2. repo_navigator blocks editor tools at the dispatch layer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repo_navigator_blocks_editor_tools() -> None:
    runner, _queue, _llm, _patches = _make_runner(role="repo_navigator")
    result = await runner._dispatch_subagent_tool(
        "str_replace",
        {"path": "src/auth.py", "old_str": "a", "new_str": "b"},
    )
    assert "not permitted" in result
    assert "repo_navigator" in result
    assert _patches == {}


# ---------------------------------------------------------------------------
# 3. run() emits subagent_start / subagent_done with no tool calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subagent_runner_emits_sse_events() -> None:
    # format_sse_event must accept the two new event names.
    format_sse_event("subagent_start", {"role": "repo_navigator", "task": "t"})
    format_sse_event(
        "subagent_done",
        {"role": "repo_navigator", "summary": "s", "patches_modified": []},
    )

    runner, queue, _llm, _patches = _make_runner(
        role="repo_navigator",
        task="Map the auth module.",
        llm_response={
            "content": "Exploration complete: auth lives in src/auth.py.",
            "tool_calls": None,
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "latency_ms": 100,
            "model": "test-model",
        },
    )
    summary = await runner.run()

    assert "Exploration complete" in summary
    queue.put_subagent_start.assert_awaited_once_with(
        role="repo_navigator", task="Map the auth module."
    )
    queue.put_subagent_done.assert_awaited_once()
    _, done_kwargs = queue.put_subagent_done.call_args
    assert done_kwargs["role"] == "repo_navigator"
    assert done_kwargs["patches_modified"] == []
    queue.put_tool_call.assert_not_awaited()


# ---------------------------------------------------------------------------
# 4. Recursive invoke_subagent is blocked, no crash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subagent_runner_blocks_recursive_invoke() -> None:
    first = {
        "content": None,
        "tool_calls": [
            _tool_call("invoke_subagent", {"role": "repo_navigator", "task": "recurse"})
        ],
        "usage": {},
        "latency_ms": 10,
        "model": "test-model",
    }
    second = {
        "content": None,
        "tool_calls": None,
        "usage": {},
        "latency_ms": 10,
        "model": "test-model",
    }
    runner, queue, llm, _patches = _make_runner(role="repo_navigator")
    llm.complete.side_effect = [first, second]

    summary = await runner.run()

    assert "invoke_subagent is not available" in summary
    queue.put_tool_call.assert_awaited_once()
    tool_args = queue.put_tool_call.call_args
    assert tool_args[0][0] == "invoke_subagent"


# ---------------------------------------------------------------------------
# 5. LLM loop respects per-role max_iterations with truncation notice
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subagent_runner_respects_max_iterations() -> None:
    max_iters = ROLE_CONFIGS["sandbox_verifier"].max_iterations
    runner, queue, llm, _patches = _make_runner(
        role="sandbox_verifier", task="Run all checks."
    )
    llm.complete.return_value = {
        "content": None,
        "tool_calls": [_tool_call("glob_files", {"pattern": "**/*.py"})],
        "usage": {},
        "latency_ms": 10,
        "model": "test-model",
    }

    with patch(
        "app.services.session_tools.subagents.tool_glob_files",
        new_callable=AsyncMock,
        return_value=["src/a.py"],
    ):
        summary = await runner.run()

    assert llm.complete.await_count == max_iters
    assert "max_iterations" in summary
    assert "truncated" in summary.lower()
    queue.put_subagent_start.assert_awaited_once()
    queue.put_subagent_done.assert_awaited_once()


# ---------------------------------------------------------------------------
# 6. staged_patches shared by reference with the parent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_staged_patches_shared_ref() -> None:
    parent_patches: dict[str, str] = {}
    runner, _queue, llm, runner_patches = _make_runner(
        role="feature_architect",
        task="Fix the login handler.",
        staged_patches=parent_patches,
    )
    assert runner_patches is parent_patches
    assert runner.staged_patches is parent_patches

    first = {
        "content": None,
        "tool_calls": [
            _tool_call(
                "str_replace",
                {"path": "src/auth.py", "old_str": "old", "new_str": "new"},
            )
        ],
        "usage": {},
        "latency_ms": 10,
        "model": "test-model",
    }
    second = {
        "content": "Fix staged for src/auth.py.",
        "tool_calls": None,
        "usage": {},
        "latency_ms": 10,
        "model": "test-model",
    }
    llm.complete.side_effect = [first, second]

    async def _fake_str_replace(**kwargs: Any) -> str:
        kwargs["staged_patches"]["src/auth.py"] = "diff-content"
        return "Successfully replaced code in 'src/auth.py'."

    with patch(
        "app.services.session_tools.subagents.tool_str_replace",
        side_effect=_fake_str_replace,
    ):
        summary = await runner.run()

    assert "src/auth.py" in parent_patches
    assert parent_patches["src/auth.py"] == "diff-content"
    assert "Fix staged" in summary


# ---------------------------------------------------------------------------
# 7. _exec_scan_security syncs turn-local patches to session (distinct objects)
# ---------------------------------------------------------------------------


def test_exec_scan_security_syncs_distinct_session_dict() -> None:
    """Regression: scanner reads session.staged_patches, edits live turn-local.

    Wires session.staged_patches and runner.staged_patches as DISTINCT objects
    (unlike _make_runner's shared wiring) to prove the sync before scanning.
    Without the sync, the scanner would see an empty session dict and pass.
    """
    turn_local: dict[str, str] = {
        "src/auth.py": '@@ -0,0 +1 @@\n+api_key = "AKIAIOSFODNN7EXAMPLE"\n',
    }
    runner, _queue, _llm, _patches = _make_runner(
        role="feature_architect",
        task="Scan staged secret.",
        staged_patches=turn_local,
    )
    # Break the shared wiring: session holds a stale, DISTINCT empty dict.
    runner.session.staged_patches = {}
    assert runner.session.staged_patches is not runner.staged_patches

    result = runner._exec_scan_security({"paths": ["src/auth.py"]})

    assert "AWS_ACCESS_KEY" in result
    assert runner.session.staged_patches == turn_local
    assert runner.session.staged_patches is not runner.staged_patches


# ---------------------------------------------------------------------------
# 8. Observability: run() emits structured subagent_telemetry log (Phase 1.4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subagent_telemetry_logged() -> None:
    runner, _queue, _llm, _patches = _make_runner(
        role="repo_navigator",
        task="Map the auth module.",
        llm_response={
            "content": "Exploration complete: auth lives in src/auth.py.",
            "tool_calls": None,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "latency_ms": 100,
            "model": "test-model",
        },
    )
    with patch("app.services.session_tools.subagents.logger.info") as mock_info:
        summary = await runner.run()

    assert "Exploration complete" in summary
    telemetry_calls = [
        c
        for c in mock_info.call_args_list
        if c[0] and str(c[0][0]).startswith("subagent_telemetry")
    ]
    assert telemetry_calls, "expected a subagent_telemetry log line"
    fmt, role, session_id, in_tokens, out_tokens, latency_ms, iterations = (
        telemetry_calls[0][0]
    )
    assert role == "repo_navigator"
    assert session_id == "session-test"
    assert in_tokens == 10
    assert out_tokens == 5
    assert isinstance(latency_ms, int) and latency_ms >= 0
    assert iterations == 1
