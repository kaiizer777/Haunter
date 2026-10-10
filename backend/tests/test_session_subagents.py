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


# ---------------------------------------------------------------------------
# 9. Issue #68: sandbox_verifier allowlist includes read_file
# ---------------------------------------------------------------------------


def test_sandbox_verifier_allowlist_has_read_file() -> None:
    """Issue #68: sandbox_verifier must be permitted to read files."""
    allowed = ROLE_CONFIGS["sandbox_verifier"].allowed_tools
    assert "read_file" in allowed
    assert "read_file_slice" in allowed
    assert "glob_files" in allowed
    assert "run_targeted_tests" in allowed


# ---------------------------------------------------------------------------
# 10. Issue #68: Subagent read_file observes staged patches (created, modified, deleted)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subagent_read_file_observes_staged_content() -> None:
    """Issue #68: _exec_read_file observes staged creations, modifications, and deletions."""
    staged: dict[str, str] = {
        "created.py": "--- /dev/null\n+++ b/created.py\n@@ -0,0 +1 @@\n+print('hello world')\n",
        "modified.py": "--- a/modified.py\n+++ b/modified.py\n@@ -1 +1 @@\n-old_val\n+new_val\n",
        "deleted.py": "--- a/deleted.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-old_code\n",
    }
    runner, _queue, _llm, _ = _make_runner(
        role="sandbox_verifier",
        task="Verify staged changes.",
        staged_patches=staged,
    )

    with patch(
        "app.services.session_tools.recon.fetch_file_content",
        new_callable=AsyncMock,
    ) as mock_fetch:
        async def _fetch(owner: str, repo: str, path: str, sha: str, token: str | None = None) -> str | None:
            if path == "modified.py":
                return "old_val\n"
            if path == "clean.py":
                return "clean_code\n"
            return None

        mock_fetch.side_effect = _fetch

        # 1. Staged creation
        res_created = await runner._exec_read_file({"path": "created.py"})
        assert "print('hello world')" in res_created

        # 2. Staged modification overlaid on base
        res_mod = await runner._exec_read_file({"path": "modified.py"})
        assert "new_val" in res_mod
        assert "old_val" not in res_mod

        # 3. Staged deletion
        res_del = await runner._exec_read_file({"path": "deleted.py"})
        assert "deleted in staged changes" in res_del

        # 4. Clean un-staged file from base
        res_clean = await runner._exec_read_file({"path": "clean.py"})
        assert res_clean == "clean_code\n"


# ---------------------------------------------------------------------------
# 11. Issue #68: Subagent git_diff observes staged patches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subagent_git_diff_observes_staged_patches() -> None:
    """Issue #68: _exec_git_diff passes staged_patches into tool_git_diff."""
    staged: dict[str, str] = {
        "src/auth.py": "--- a/src/auth.py\n+++ b/src/auth.py\n@@ -1 +1 @@\n-old\n+new\n"
    }
    runner, _queue, _llm, _ = _make_runner(
        role="code_guardian",
        task="Review staged diff.",
        staged_patches=staged,
    )

    diff_res = await runner._exec_git_diff({"base": "HEAD", "head": "staged"})
    assert "src/auth.py" in diff_res
    assert "+new" in diff_res


# ---------------------------------------------------------------------------
# 12. Issue #68: sandbox_verifier passes staged_patches to run_targeted_tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sandbox_verifier_run_targeted_tests_passes_staged_context() -> None:
    """Issue #68: _exec_run_targeted_tests passes staged_patches, base_sha, and session_id."""
    staged: dict[str, str] = {
        "tests/test_x.py": "--- a/tests/test_x.py\n+++ b/tests/test_x.py\n@@ -1 +1 @@\n-pass\n+fail\n"
    }
    runner, _queue, _llm, _ = _make_runner(
        role="sandbox_verifier",
        task="Run test targets.",
        staged_patches=staged,
    )

    with patch(
        "app.services.session_tools.subagents.tool_run_targeted_tests",
        new_callable=AsyncMock,
    ) as mock_tests:
        mock_tests.return_value = "Test runner: pytest\nStatus: FAILED\nExit code: 1"
        res = await runner._exec_run_targeted_tests({"test_targets": ["tests/test_x.py"]})

        assert "Status: FAILED" in res
        mock_tests.assert_awaited_once_with(
            test_targets=["tests/test_x.py"],
            timeout_sec=120,
            queue=runner.queue,
            cwd=None,
            repo_owner="test-org",
            repo_name="test-repo",
            staged_patches=staged,
            base_sha="a" * 40,
            session_id="session-test",
        )


# ---------------------------------------------------------------------------
# 13. Issue #68: End-to-end regression: staged breakage causes test failure in subagent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sandbox_verifier_e2e_regression_broken_staged_test() -> None:
    """Regression test: stage a change that breaks tests, invoke sandbox_verifier, assert it reports failure."""
    staged: dict[str, str] = {
        "tests/test_math.py": "--- a/tests/test_math.py\n+++ b/tests/test_math.py\n@@ -1 +1 @@\n-assert 1 == 1\n+assert 1 == 2\n"
    }
    runner, queue, llm, _ = _make_runner(
        role="sandbox_verifier",
        task="Verify test suite against staged changes.",
        staged_patches=staged,
    )

    # Step 1: LLM reads staged file
    step1 = {
        "content": None,
        "tool_calls": [_tool_call("read_file", {"path": "tests/test_math.py"}, call_id="c1")],
        "usage": {"input_tokens": 15, "output_tokens": 5},
    }
    # Step 2: LLM runs targeted tests
    step2 = {
        "content": None,
        "tool_calls": [_tool_call("run_targeted_tests", {"test_targets": ["tests/test_math.py"]}, call_id="c2")],
        "usage": {"input_tokens": 20, "output_tokens": 5},
    }
    # Step 3: LLM summarizes failure
    step3 = {
        "content": "Verification FAILED: assert 1 == 2 failed in tests/test_math.py.",
        "tool_calls": None,
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }
    llm.complete.side_effect = [step1, step2, step3]

    with patch(
        "app.services.session_tools.recon.fetch_file_content",
        new_callable=AsyncMock,
        return_value="assert 1 == 1\n",
    ), patch(
        "app.services.session_tools.subagents.tool_run_targeted_tests",
        new_callable=AsyncMock,
        return_value="Test runner: pytest\nTargets: tests/test_math.py\nStatus: FAILED\nExit code: 1\nOUTPUT:\nAssertionError: assert 1 == 2",
    ) as mock_tests:
        summary = await runner.run()

        assert "Verification FAILED" in summary
        assert "assert 1 == 2 failed" in summary
        mock_tests.assert_awaited_once_with(
            test_targets=["tests/test_math.py"],
            timeout_sec=120,
            queue=queue,
            cwd=None,
            repo_owner="test-org",
            repo_name="test-repo",
            staged_patches=staged,
            base_sha="a" * 40,
            session_id="session-test",
        )

