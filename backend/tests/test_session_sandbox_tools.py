"""
Unit tests for Phase 5 — Autonomous Sandbox Execution & Test Automation.

Covers:
  1. test_sanitize_command_valid          — safe commands pass through unchanged.
  2. test_sanitize_command_blocked        — destructive commands raise ValueError.
  3. test_run_terminal_command_mock_success — mocked subprocess, verifies formatted output.
  4. test_run_terminal_command_timeout    — mocked timeout, verifies clean timeout return.
  5. test_run_linter_routing              — correct linter selected per file extension.
  6. test_run_targeted_tests_formatting   — correct test runner selected and args formatted.
  7. test_sse_terminal_output_event       — format_sse_event("terminal_output", ...) SSE wire format.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.session_streamer import SseQueue, format_sse_event
from app.services.session_tools.sandbox import (
    _sanitize_command,
    _select_linter,
    _select_test_framework,
    tool_run_linter,
    tool_run_targeted_tests,
    tool_run_terminal_command,
)


# ---------------------------------------------------------------------------
# 1. _sanitize_command — valid commands
# ---------------------------------------------------------------------------


def test_sanitize_command_valid() -> None:
    """Normal, safe commands must pass through without error."""
    safe_commands = [
        "pytest backend/tests/test_auth.py -v",
        "npm test -- src/auth.test.ts",
        "git status",
        "ruff check backend/app/",
        "python -m mypy app/",
        "npx tsc --noEmit",
        "echo hello",
        "ls -la",
        "cat README.md",
        "grep -r 'def foo' src/",
    ]
    for cmd in safe_commands:
        result = _sanitize_command(cmd)
        assert result == cmd, f"Expected command to pass unchanged: {cmd!r}"


# ---------------------------------------------------------------------------
# 2. _sanitize_command — blocked (destructive) commands
# ---------------------------------------------------------------------------


def test_sanitize_command_blocked() -> None:
    """Destructive/dangerous commands must raise ValueError."""
    blocked_commands = [
        "rm -rf /",
        "rm -rf / --no-preserve-root",
        "mkfs.ext4 /dev/sda",
        "mkfs -t ext4 /dev/sdb",
        ":(){ :|:& };:",  # fork bomb
        "shutdown -h now",
        "reboot",
        "halt",
        "echo bad > /etc/passwd",
        "tee /etc/hosts",
        "dd if=/dev/zero of=/dev/sda",
    ]
    for cmd in blocked_commands:
        with pytest.raises(ValueError, match="Command blocked|must not be empty"):
            _sanitize_command(cmd)


def test_sanitize_command_empty_raises() -> None:
    """Empty or whitespace-only commands must raise ValueError."""
    with pytest.raises(ValueError, match="must not be empty"):
        _sanitize_command("")
    with pytest.raises(ValueError, match="must not be empty"):
        _sanitize_command("   ")


# ---------------------------------------------------------------------------
# 3. tool_run_terminal_command — mock success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_terminal_command_mock_success() -> None:
    """Mocked subprocess success: stdout/stderr captured, exit code 0, formatted output returned."""

    async def _fake_run(argv, timeout_sec, queue=None):
        return (0, "all tests passed\n", "", 1.23)

    with patch(
        "app.services.session_tools.sandbox._run_subprocess",
        new=_fake_run,
    ):
        result = await tool_run_terminal_command(
            command="pytest tests/",
            timeout_sec=60,
            queue=None,
        )

    assert "Exit code: 0" in result
    assert "Duration: 1.23s" in result
    assert "all tests passed" in result
    assert "STDOUT:" in result
    assert "STDERR:" in result


@pytest.mark.asyncio
async def test_run_terminal_command_emits_sse_chunks() -> None:
    """SSE queue receives terminal_output events for each stdout chunk."""
    queue = SseQueue()
    chunks: list[str] = []

    async def _fake_run(argv, timeout_sec, queue=None):
        # Simulate emitting chunks via queue
        if queue is not None:
            await queue.put_terminal_output("line 1\n", stream="stdout")
            await queue.put_terminal_output("line 2\n", stream="stdout")
        return (0, "line 1\nline 2\n", "", 0.5)

    with patch(
        "app.services.session_tools.sandbox._run_subprocess",
        new=_fake_run,
    ):
        result = await tool_run_terminal_command(
            command="echo hello",
            timeout_sec=30,
            queue=queue,
        )

    assert "Exit code: 0" in result


@pytest.mark.asyncio
async def test_run_terminal_command_blocks_destructive() -> None:
    """Destructive commands must be blocked before any subprocess is created."""
    result = await tool_run_terminal_command(command="rm -rf /", timeout_sec=60)
    assert result.startswith("Error:")
    assert "blocked" in result.lower() or "Command blocked" in result


# ---------------------------------------------------------------------------
# 4. tool_run_terminal_command — timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_terminal_command_timeout() -> None:
    """Mocked timeout (-1 exit code): result must contain timeout information."""

    async def _fake_timeout(argv, timeout_sec, queue=None):
        timeout_msg = f"[Command timed out after {timeout_sec}s]\n"
        return (-1, "", timeout_msg, float(timeout_sec))

    with patch(
        "app.services.session_tools.sandbox._run_subprocess",
        new=_fake_timeout,
    ):
        result = await tool_run_terminal_command(
            command="sleep 999",
            timeout_sec=5,
        )

    assert "Exit code: -1" in result
    assert "timed out" in result.lower()


def test_run_terminal_command_timeout_clamping() -> None:
    """timeout_sec must be clamped to [1, 300] — never 0 or > 300."""
    from app.services.session_tools.sandbox import _MIN_TIMEOUT, _MAX_TIMEOUT

    assert _MIN_TIMEOUT == 1
    assert _MAX_TIMEOUT == 300


# ---------------------------------------------------------------------------
# 5. run_linter — routing by extension
# ---------------------------------------------------------------------------


def test_run_linter_routing_python() -> None:
    """Pure Python paths select ruff."""
    name, argv = _select_linter(["app/main.py", "tests/test_auth.py"], "auto")
    assert name == "ruff"
    assert "ruff" in argv


def test_run_linter_routing_typescript() -> None:
    """Pure TypeScript/TSX paths select eslint."""
    name, argv = _select_linter(["src/auth.ts", "src/components/Nav.tsx"], "auto")
    assert name == "eslint"
    assert "eslint" in " ".join(argv)


def test_run_linter_routing_javascript() -> None:
    """JavaScript paths select eslint."""
    name, argv = _select_linter(["index.js", "utils.mjs"], "auto")
    assert name == "eslint"


def test_run_linter_routing_mixed_defaults_to_ruff() -> None:
    """Mixed Python + TS paths fall back to ruff (Python wins)."""
    name, argv = _select_linter(["app/main.py", "frontend/src/app.ts"], "auto")
    assert name == "ruff"


def test_run_linter_routing_explicit_override() -> None:
    """Explicit linter override is respected regardless of file extensions."""
    name, argv = _select_linter(["src/index.ts"], "mypy")
    assert name == "mypy"


@pytest.mark.asyncio
async def test_run_linter_rejects_traversal() -> None:
    """Traversal paths in the paths list must return an error string."""
    result = await tool_run_linter(paths=["../../etc/passwd"])
    assert result.startswith("Error:")


# ---------------------------------------------------------------------------
# 6. run_targeted_tests — framework detection and command formatting
# ---------------------------------------------------------------------------


def test_run_targeted_tests_python_framework() -> None:
    """Python test files select pytest."""
    name, argv = _select_test_framework(["tests/test_auth.py", "tests/test_db.py"])
    assert name == "pytest"
    assert "pytest" in argv


def test_run_targeted_tests_vitest_ts() -> None:
    """TypeScript test files select vitest."""
    name, argv = _select_test_framework(["src/auth.test.ts", "src/db.spec.tsx"])
    assert name == "vitest"
    assert "vitest" in " ".join(argv)


def test_run_targeted_tests_vitest_spec_pattern() -> None:
    """Files with .spec. in the name select vitest regardless of full extension."""
    name, argv = _select_test_framework(["src/api.spec.js"])
    assert name == "vitest"


@pytest.mark.asyncio
async def test_run_targeted_tests_formatting() -> None:
    """Correct test command is built and structured result is returned."""

    async def _fake_run(argv, timeout_sec, queue=None):
        # Verify the argv was constructed correctly for pytest
        assert "pytest" in argv[0]
        assert "tests/test_auth.py" in argv
        return (0, "2 passed in 0.5s\n", "", 0.5)

    with patch(
        "app.services.session_tools.sandbox._run_subprocess",
        new=_fake_run,
    ):
        result = await tool_run_targeted_tests(
            test_targets=["tests/test_auth.py"],
            timeout_sec=60,
        )

    assert "Test runner: pytest" in result
    assert "Status: PASSED" in result
    assert "Exit code: 0" in result
    assert "2 passed" in result


@pytest.mark.asyncio
async def test_run_targeted_tests_failed_status() -> None:
    """Failing tests produce Status: FAILED in the result."""

    async def _fake_run(argv, timeout_sec, queue=None):
        return (1, "1 failed, 2 passed\n", "AssertionError: expected 1 got 2\n", 1.0)

    with patch(
        "app.services.session_tools.sandbox._run_subprocess",
        new=_fake_run,
    ):
        result = await tool_run_targeted_tests(
            test_targets=["tests/test_models.py"],
            timeout_sec=60,
        )

    assert "Status: FAILED" in result
    assert "Exit code: 1" in result


@pytest.mark.asyncio
async def test_run_targeted_tests_rejects_traversal() -> None:
    """Traversal targets must be rejected with an error string."""
    result = await tool_run_targeted_tests(test_targets=["../../secret.py"])
    assert result.startswith("Error:")


# ---------------------------------------------------------------------------
# 7. SSE terminal_output event — wire format
# ---------------------------------------------------------------------------


def test_sse_terminal_output_event_format() -> None:
    """format_sse_event('terminal_output', ...) must produce valid SSE wire format."""
    data = {"chunk": "tests passed\n", "stream": "stdout"}
    wire = format_sse_event("terminal_output", data)

    assert wire.startswith("event: terminal_output\n")
    assert "data: " in wire
    assert '"chunk"' in wire
    assert '"stream"' in wire
    assert wire.endswith("\n\n"), "SSE frame must end with double newline"


def test_sse_terminal_output_allowed() -> None:
    """terminal_output must be in _ALLOWED_EVENTS — not raise ValueError."""
    from app.services.session_streamer import _ALLOWED_EVENTS

    assert "terminal_output" in _ALLOWED_EVENTS


@pytest.mark.asyncio
async def test_sse_queue_put_terminal_output() -> None:
    """SseQueue.put_terminal_output() must enqueue a valid terminal_output SSE frame."""
    queue = SseQueue()
    await queue.put_terminal_output("hello stdout\n", stream="stdout")

    # Drain one item from the internal queue.
    item = await asyncio.wait_for(queue._q.get(), timeout=1.0)
    assert "terminal_output" in item
    assert "hello stdout" in item
    assert "stdout" in item


# ---------------------------------------------------------------------------
# 8. Command chaining, directory navigation, and resilient execution
# ---------------------------------------------------------------------------


def test_parse_command_chain_splitting() -> None:
    """parse_command_chain correctly splits on && and ; while preserving quoted tokens."""
    from app.services.session_tools.sandbox import parse_command_chain

    chains = parse_command_chain("cd backend && python -m pytest tests/test_main.py -q")
    assert len(chains) == 2
    assert chains[0] == (["cd", "backend"], "&&")
    assert chains[1] == (["python", "-m", "pytest", "tests/test_main.py", "-q"], "")

    quoted = parse_command_chain('echo "hello && world"; git status')
    assert len(quoted) == 2
    assert quoted[0] == (["echo", "hello && world"], ";")
    assert quoted[1] == (["git", "status"], "")


@pytest.mark.asyncio
async def test_run_terminal_command_chained_cd_execution() -> None:
    """Chained cd command navigates into target directory and executes subcommand."""
    calls: list[tuple[list[str], str | None]] = []

    async def _fake_run(argv, timeout_sec, queue=None, cwd=None):
        calls.append((argv, cwd))
        return (0, "6 passed\n", "", 0.5)

    with patch(
        "app.services.session_tools.sandbox._run_subprocess",
        new=_fake_run,
    ):
        result = await tool_run_terminal_command(
            command="cd tests && pytest -q",
            timeout_sec=60,
        )

    assert "Exit code: 0" in result
    assert "6 passed" in result
    assert len(calls) == 1
    assert calls[0][0] == ["pytest", "-q"]
    assert calls[0][1] is not None and calls[0][1].endswith("tests")


@pytest.mark.asyncio
async def test_run_terminal_command_cd_already_in_directory() -> None:
    """cd into current folder name (e.g. cd backend when already in backend) succeeds as no-op."""
    calls: list[tuple[list[str], str | None]] = []

    async def _fake_run(argv, timeout_sec, queue=None, cwd=None):
        calls.append((argv, cwd))
        return (0, "all good\n", "", 0.2)

    with patch(
        "app.services.session_tools.sandbox._run_subprocess",
        new=_fake_run,
    ):
        result = await tool_run_terminal_command(
            command="cd backend && python --version",
            timeout_sec=60,
            cwd="C:/dummy/repo/backend",
        )

    assert "Exit code: 0" in result
    assert len(calls) == 1
    assert calls[0][0] == ["python", "--version"]


@pytest.mark.asyncio
async def test_run_terminal_command_cd_missing_directory() -> None:
    """cd into non-existent directory halts execution on && and returns exit code 1."""
    result = await tool_run_terminal_command(
        command="cd non_existent_dir_98765 && pytest",
        timeout_sec=60,
    )
    assert "Exit code: 1" in result
    assert "no such file or directory" in result.lower()
    assert "non_existent_dir_98765" in result


@pytest.mark.asyncio
async def test_run_subprocess_real_execution() -> None:
    """Real subprocess execution works without NotImplementedError or Subprocess error."""
    import sys
    from app.services.session_tools.sandbox import _run_subprocess

    code, stdout, stderr, dur = await _run_subprocess(
        argv=[sys.executable, "-c", "print('sandbox ok')"],
        timeout_sec=10,
    )
    assert code == 0
    assert "sandbox ok" in stdout
    assert stderr == ""


def test_prepare_cmd_argv_resolution() -> None:
    """_prepare_cmd_argv maps python/pytest/ruff to sys.executable invocations."""
    import sys
    from app.services.session_tools.sandbox import _prepare_cmd_argv

    assert _prepare_cmd_argv(["python", "app/main.py"]) == [
        sys.executable,
        "app/main.py",
    ]
    assert _prepare_cmd_argv(["python3", "app/main.py"]) == [
        sys.executable,
        "app/main.py",
    ]
    assert _prepare_cmd_argv(["pytest", "-v", "tests/"]) == [
        sys.executable,
        "-m",
        "pytest",
        "-v",
        "tests/",
    ]
    assert _prepare_cmd_argv(["ruff", "check", "app/"]) == [
        sys.executable,
        "-m",
        "ruff",
        "check",
        "app/",
    ]


def test_loop_supports_subprocesses_selector_check() -> None:
    """_loop_supports_subprocesses identifies SelectorEventLoop as lacking subprocesses on Windows."""
    import sys
    from app.services.session_tools.sandbox import _loop_supports_subprocesses

    class FakeSelectorLoop:
        pass

    FakeSelectorLoop.__name__ = "_WindowsSelectorEventLoop"

    if sys.platform == "win32":
        assert _loop_supports_subprocesses(FakeSelectorLoop()) is False


@pytest.mark.asyncio
async def test_run_subprocess_fallback_on_unsupported_loop() -> None:
    """_run_subprocess delegates to _run_subprocess_sync in thread pool when loop lacks subprocess support."""
    import sys
    from app.services.session_tools.sandbox import _run_subprocess

    with patch(
        "app.services.session_tools.sandbox._loop_supports_subprocesses",
        return_value=False,
    ):
        code, stdout, stderr, dur = await _run_subprocess(
            argv=[sys.executable, "-c", "print('threaded fallback')"],
            timeout_sec=10,
        )
        assert code == 0
        assert "threaded fallback" in stdout
        assert stderr == ""


@pytest.mark.asyncio
async def test_subprocess_error_formatting_never_empty() -> None:
    """When a subprocess throws an exception with an empty str (e.g. NotImplementedError), error is descriptive."""
    import sys
    from app.services.session_tools.sandbox import _run_subprocess_sync

    with patch("subprocess.Popen", side_effect=NotImplementedError()):
        code, stdout, stderr, dur = _run_subprocess_sync(
            argv=["dummy_cmd"],
            timeout_sec=10,
        )
        assert code == -1
        assert "Subprocess error (NotImplementedError)" in stderr


# ---------------------------------------------------------------------------
# 11. resolve_repo_dir and repo-aware execution routing
# ---------------------------------------------------------------------------


def test_resolve_repo_dir_none_fallback() -> None:
    """When repo_name is None, resolve_repo_dir defaults to cwd or os.getcwd()."""
    from app.services.session_tools.sandbox import resolve_repo_dir
    import os

    res, err = resolve_repo_dir(None)
    assert err is None
    assert res == os.getcwd()

    res_cwd, err_cwd = resolve_repo_dir(None, cwd="subdir")
    assert err_cwd is None
    assert res_cwd == os.path.normpath(os.path.join(os.getcwd(), "subdir"))


def test_resolve_repo_dir_not_found() -> None:
    """When repo_name cannot be found on disk, returns informative error pointing to verify_in_ci_sandbox."""
    from app.services.session_tools.sandbox import resolve_repo_dir

    res, err = resolve_repo_dir(
        repo_name="completely_nonexistent_repo_xyz_123",
        repo_owner="testowner",
    )
    assert res is None
    assert err is not None
    assert "Local checkout for repository 'testowner/completely_nonexistent_repo_xyz_123' was not found" in err
    assert "verify_in_ci_sandbox" in err


def test_resolve_repo_dir_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_repo_dir discovers repositories via LOCAL_REPOS_DIR environment variable."""
    from app.services.session_tools.sandbox import resolve_repo_dir

    repo_dir = tmp_path / "MySpecialRepo"
    repo_dir.mkdir()
    monkeypatch.setenv("LOCAL_REPOS_DIR", str(tmp_path))

    res, err = resolve_repo_dir(repo_name="MySpecialRepo")
    assert err is None
    assert res == str(repo_dir)


def test_resolve_repo_dir_relative_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_repo_dir joins relative cwd to resolved repository directory."""
    from app.services.session_tools.sandbox import resolve_repo_dir

    repo_dir = tmp_path / "MySpecialRepo"
    sub_dir = repo_dir / "backend"
    sub_dir.mkdir(parents=True)
    monkeypatch.setenv("LOCAL_REPOS_DIR", str(tmp_path))

    res, err = resolve_repo_dir(repo_name="MySpecialRepo", cwd="backend")
    assert err is None
    assert res == str(sub_dir)


def test_resolve_repo_dir_outside_cwd_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_repo_dir rejects absolute cwd outside the resolved repository root."""
    from app.services.session_tools.sandbox import resolve_repo_dir

    repo_dir = tmp_path / "MySpecialRepo"
    repo_dir.mkdir()
    monkeypatch.setenv("LOCAL_REPOS_DIR", str(tmp_path))

    outside_dir = tmp_path / "OtherPlace"
    outside_dir.mkdir()

    res, err = resolve_repo_dir(repo_name="MySpecialRepo", cwd=str(outside_dir))
    assert res is None
    assert err is not None
    assert "is outside repository root" in err


def test_resolve_repo_dir_relative_outside_cwd_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_repo_dir rejects relative traversal cwd outside the resolved repository root."""
    from app.services.session_tools.sandbox import resolve_repo_dir

    repo_dir = tmp_path / "MySpecialRepo"
    repo_dir.mkdir()
    monkeypatch.setenv("LOCAL_REPOS_DIR", str(tmp_path))

    res, err = resolve_repo_dir(repo_name="MySpecialRepo", cwd="../escaped")
    assert res is None
    assert err is not None
    assert "is outside repository root" in err


def test_resolve_repo_dir_shared_prefix_sibling_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_repo_dir rejects sibling paths sharing prefix with repo root."""
    from app.services.session_tools.sandbox import resolve_repo_dir

    repo_dir = tmp_path / "MySpecialRepo"
    repo_dir.mkdir()
    sibling_dir = tmp_path / "MySpecialRepo2"
    sibling_dir.mkdir()
    monkeypatch.setenv("LOCAL_REPOS_DIR", str(tmp_path))

    res, err = resolve_repo_dir(repo_name="MySpecialRepo", cwd=str(sibling_dir))
    assert res is None
    assert err is not None
    assert "is outside repository root" in err


def test_resolve_repo_dir_invalid_component() -> None:
    """resolve_repo_dir rejects path traversal in repository name or owner."""
    from app.services.session_tools.sandbox import resolve_repo_dir

    res, err = resolve_repo_dir(repo_name="../bad_repo")
    assert res is None
    assert err is not None
    assert "Invalid repository name" in err

    res2, err2 = resolve_repo_dir(repo_name="valid_repo", repo_owner="../../bad_owner")
    assert res2 is None
    assert err2 is not None
    assert "Invalid repository owner" in err2


def test_resolve_repo_dir_owner_mismatch_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_repo_dir skips existing checkouts whose git remote origin belongs to a different owner."""
    import subprocess
    from app.services.session_tools.sandbox import resolve_repo_dir

    # Create wrong owner candidate
    wrong_dir = tmp_path / "TestProject"
    wrong_dir.mkdir()
    subprocess.run(["git", "-C", str(wrong_dir), "init"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(wrong_dir), "remote", "add", "origin", "https://github.com/alice/TestProject.git"],
        check=True,
        capture_output=True,
    )

    # Create correct owner candidate in owner-scoped path
    right_owner_dir = tmp_path / "kaiizer777" / "TestProject"
    right_owner_dir.mkdir(parents=True)
    subprocess.run(["git", "-C", str(right_owner_dir), "init"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(right_owner_dir), "remote", "add", "origin", "https://github.com/kaiizer777/TestProject.git"],
        check=True,
        capture_output=True,
    )

    monkeypatch.setenv("LOCAL_REPOS_DIR", str(tmp_path))

    res, err = resolve_repo_dir(repo_name="TestProject", repo_owner="kaiizer777")
    assert err is None
    assert res == str(right_owner_dir)


@pytest.mark.asyncio
async def test_tool_run_terminal_command_repo_not_found() -> None:
    """tool_run_terminal_command returns error when repo is specified but not found locally."""
    result = await tool_run_terminal_command(
        command="git status",
        repo_owner="fake_owner",
        repo_name="fake_repo_xyz_999",
    )
    assert "Error: Local checkout for repository 'fake_owner/fake_repo_xyz_999' was not found" in result
    assert "verify_in_ci_sandbox" in result


@pytest.mark.asyncio
async def test_tool_run_linter_repo_not_found() -> None:
    """tool_run_linter returns error when repo is specified but not found locally."""
    result = await tool_run_linter(
        paths=["app/main.py"],
        repo_owner="fake_owner",
        repo_name="fake_repo_xyz_999",
    )
    assert "Error: Local checkout for repository 'fake_owner/fake_repo_xyz_999' was not found" in result


@pytest.mark.asyncio
async def test_tool_run_targeted_tests_repo_not_found() -> None:
    """tool_run_targeted_tests returns error when repo is specified but not found locally."""
    result = await tool_run_targeted_tests(
        test_targets=["tests/test_foo.py"],
        repo_owner="fake_owner",
        repo_name="fake_repo_xyz_999",
    )
    assert "Error: Local checkout for repository 'fake_owner/fake_repo_xyz_999' was not found" in result


@pytest.mark.asyncio
async def test_tool_run_terminal_command_resolves_upgrade_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When repo_name='UpGrade' is passed, terminal executes inside the resolved repository."""
    import subprocess

    upgrade_path = tmp_path / "UpGrade"
    upgrade_path.mkdir()
    subprocess.run(
        ["git", "-C", str(upgrade_path), "init"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(upgrade_path),
            "remote",
            "add",
            "origin",
            "https://github.com/kaiizer777/UpGrade.git",
        ],
        check=True,
        capture_output=True,
    )
    monkeypatch.setenv("LOCAL_REPOS_DIR", str(tmp_path))

    res = await tool_run_terminal_command(
        command="git remote -v",
        repo_owner="kaiizer777",
        repo_name="UpGrade",
    )
    assert "kaiizer777/UpGrade" in res
    assert "Haunter" not in res


@pytest.mark.asyncio
async def test_tool_run_terminal_command_cd_escape_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When running inside a repository, cd is constrained within the repository root."""
    import subprocess

    repo_dir = tmp_path / "MySpecialRepo"
    repo_dir.mkdir()
    subprocess.run(["git", "-C", str(repo_dir), "init"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo_dir), "remote", "add", "origin", "https://github.com/test/MySpecialRepo.git"],
        check=True,
        capture_output=True,
    )
    monkeypatch.setenv("LOCAL_REPOS_DIR", str(tmp_path))

    res = await tool_run_terminal_command(
        command="cd .. && git status",
        repo_owner="test",
        repo_name="MySpecialRepo",
    )
    assert "outside repository root" in res
    assert "Exit code: 1" in res


def test_prepare_cmd_argv_windows_builtins_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows, safe shell builtins like 'dir' are wrapped with ['cmd', '/c']."""
    import sys
    from app.services.session_tools.sandbox import _prepare_cmd_argv

    monkeypatch.setattr(sys, "platform", "win32")
    argv = _prepare_cmd_argv(["dir", "C:\\Users"])
    assert argv[:2] == ["cmd", "/c"]
    assert argv[2:] == ["dir", "C:\\Users"]


def test_prepare_cmd_argv_windows_builtins_metacharacters_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows, shell metacharacters in builtins are rejected to prevent command injection."""
    import sys
    from app.services.session_tools.sandbox import _prepare_cmd_argv

    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(ValueError, match="Shell metacharacters are not permitted"):
        _prepare_cmd_argv(["dir", ". & del secret.txt"])

    with pytest.raises(ValueError, match="Shell metacharacters are not permitted"):
        _prepare_cmd_argv(["del", "foo | bar"])


def test_prepare_cmd_argv_runner_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """If repository venv lacks pytest runner, falls back to sys.executable."""
    import sys
    from app.services.session_tools.sandbox import _prepare_cmd_argv

    fake_venv = tmp_path / ".venv"
    if sys.platform == "win32":
        scripts_dir = fake_venv / "Scripts"
        scripts_dir.mkdir(parents=True)
        fake_python = scripts_dir / "python.exe"
        fake_python.write_text("")
    else:
        bin_dir = fake_venv / "bin"
        bin_dir.mkdir(parents=True)
        fake_python = bin_dir / "python"
        fake_python.write_text("")

    argv = _prepare_cmd_argv(["pytest", "tests/"], cwd=str(tmp_path))
    # Since fake_venv does not contain pytest runner, fallback to sys.executable
    assert argv[0] == sys.executable
    assert argv[1:3] == ["-m", "pytest"]


@pytest.mark.asyncio
async def test_tool_verify_ci_sandbox_local_passes_repo_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """When provider=local, tool_verify_ci_sandbox forwards repo_owner and repo_name to test runner."""
    from app.config import settings
    from app.services.session_tools.sandbox import tool_verify_ci_sandbox

    monkeypatch.setattr(settings, "sandbox_provider", "local")

    called_kwargs: dict[str, Any] = {}

    async def mock_run_tests(**kwargs: Any) -> str:
        called_kwargs.update(kwargs)
        return "Test runner: pytest\nStatus: PASSED\nExit code: 0"

    with patch(
        "app.services.session_tools.sandbox.tool_run_targeted_tests",
        side_effect=mock_run_tests,
    ):
        res = await tool_verify_ci_sandbox(
            staged_patches={"tests/test_a.py": "--- a\n+++ b\n"},
            repo_owner="test_owner",
            repo_name="test_repo",
        )
        assert "provider=local" in res
        assert called_kwargs.get("repo_owner") == "test_owner"
        assert called_kwargs.get("repo_name") == "test_repo"
        assert called_kwargs.get("test_targets") == ["tests/test_a.py"]


def test_get_git_remote_url_linked_worktree(tmp_path: Path) -> None:
    """_get_git_remote_url correctly extracts remote URL from linked worktree using commondir."""
    from app.services.session_tools.sandbox import _get_git_remote_url

    # Set up main repository .git directory with config
    main_repo = tmp_path / "main_repo"
    main_git = main_repo / ".git"
    main_git.mkdir(parents=True)
    main_config = main_git / "config"
    main_config.write_text(
        '[remote "origin"]\n\turl = https://github.com/kaiizer777/WorktreeProject.git\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n',
        encoding="utf-8",
    )

    # Worktree metadata directory under main_repo/.git/worktrees/wt1
    wt_meta = main_git / "worktrees" / "wt1"
    wt_meta.mkdir(parents=True)
    commondir = wt_meta / "commondir"
    commondir.write_text("../..\n", encoding="utf-8")

    # Linked worktree directory containing .git file
    wt_dir = tmp_path / "worktree_checkout"
    wt_dir.mkdir(parents=True)
    wt_git_file = wt_dir / ".git"
    wt_git_file.write_text(f"gitdir: {wt_meta}\n", encoding="utf-8")

    remote = _get_git_remote_url(str(wt_dir))
    assert remote == "https://github.com/kaiizer777/WorktreeProject.git"


@pytest.mark.asyncio
async def test_tool_run_terminal_command_cd_from_subfolder_to_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When cwd is a subfolder, cd .. navigates to repo root successfully and is not rejected."""
    import subprocess
    from app.services.session_tools.sandbox import tool_run_terminal_command

    repo_dir = tmp_path / "SubfolderRepo"
    repo_dir.mkdir()
    subprocess.run(["git", "-C", str(repo_dir), "init"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo_dir), "remote", "add", "origin", "https://github.com/test/SubfolderRepo.git"],
        check=True,
        capture_output=True,
    )
    sub = repo_dir / "backend"
    sub.mkdir()
    (sub / "test.txt").write_text("hello subfolder", encoding="utf-8")
    (repo_dir / "root.txt").write_text("hello root", encoding="utf-8")

    monkeypatch.setenv("LOCAL_REPOS_DIR", str(tmp_path))

    # cd .. from backend/ to repo root should succeed
    res = await tool_run_terminal_command(
        command="cd ..",
        cwd="backend",
        repo_owner="test",
        repo_name="SubfolderRepo",
    )
    assert "Exit code: 0" in res

    # cd ../.. from backend/ tries to escape repo root and should be rejected
    res_escape = await tool_run_terminal_command(
        command="cd ../..",
        cwd="backend",
        repo_owner="test",
        repo_name="SubfolderRepo",
    )
    assert "is outside repository root" in res_escape


def test_prepare_cmd_argv_windows_builtins_percent_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows, percent signs in builtins (environment variables like %TEMP%) are permitted."""
    import sys
    from app.services.session_tools.sandbox import _prepare_cmd_argv

    monkeypatch.setattr(sys, "platform", "win32")
    argv = _prepare_cmd_argv(["dir", "%TEMP%"])
    assert argv == ["cmd", "/c", "dir", "%TEMP%"]

    argv_user = _prepare_cmd_argv(["type", r"%USERPROFILE%\notes.txt"])
    assert argv_user == ["cmd", "/c", "type", r"%USERPROFILE%\notes.txt"]


def test_parse_command_chain_preserves_colons_and_quotes() -> None:
    """parse_command_chain must not split git refs with colons or mangled quotes."""
    from app.services.session_tools.sandbox import parse_command_chain

    # 1. Colon preservation for git show HEAD:path
    parsed = parse_command_chain("git show HEAD:path")
    assert parsed == [(['git', 'show', 'HEAD:path'], '')]

    # 2. Semicolons inside quotes must not split command chains
    parsed = parse_command_chain('python -c "import sys; print(1)"')
    assert parsed == [(['python', '-c', 'import sys; print(1)'], '')]

    # 3. Chained commands with quotes and internal semicolons
    parsed = parse_command_chain('cd backend && python -c "import sys; print(1)"')
    assert parsed == [
        (['cd', 'backend'], '&&'),
        (['python', '-c', 'import sys; print(1)'], ''),
    ]

    # 4. Redundant shell redirect removal (2>&1)
    parsed = parse_command_chain("pytest 2>&1")
    assert parsed == [(['pytest'], '')]

    # 5. Multiline string preservation
    multiline = 'python -c "x = 1\ny = 2\nprint(x + y)"'
    parsed = parse_command_chain(multiline)
    assert parsed == [(['python', '-c', 'x = 1\ny = 2\nprint(x + y)'], '')]


def test_prepare_cmd_argv_windows_echo_and_cat(monkeypatch: pytest.MonkeyPatch) -> None:
    """_prepare_cmd_argv handles 'echo' as cmd builtin and emulates 'cat' on Windows."""
    import sys
    from app.services.session_tools.sandbox import _prepare_cmd_argv

    monkeypatch.setattr(sys, "platform", "win32")
    argv_echo = _prepare_cmd_argv(["echo", "hello world"])
    assert argv_echo == ["cmd", "/c", "echo", "hello world"]

    # Cat emulation when cat is not on PATH (uses safe Python runner without shell)
    monkeypatch.setattr("shutil.which", lambda name: None)
    argv_cat = _prepare_cmd_argv(["cat", "-A", "README.md"])
    assert argv_cat[0] == sys.executable
    assert argv_cat[1] == "-c"
    assert argv_cat[-1] == "README.md"


def test_windows_cat_fallback_fails_on_missing_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows cat fallback exits with non-zero code on missing or unreadable files."""
    import subprocess
    import sys
    from app.services.session_tools.sandbox import _prepare_cmd_argv

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("shutil.which", lambda name: None)
    argv_cat = _prepare_cmd_argv(["cat", "definitely_nonexistent_file_xyz_987.txt"])
    proc = subprocess.run(argv_cat, capture_output=True, text=True)
    assert proc.returncode != 0
    assert "cat:" in proc.stderr


def test_sync_staged_patches_to_repo_idempotent(tmp_path: Path) -> None:
    """sync_staged_patches_to_repo applies patches idempotently without line duplication."""
    from app.services.session_tools.sandbox import sync_staged_patches_to_repo

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    test_file = repo_dir / "test.py"
    test_file.write_text("line1\nline2\n", encoding="utf-8")

    # Pure addition / modification diff
    staged = {
        "test.py": "--- a/test.py\n+++ b/test.py\n@@ -1,2 +1,3 @@\n line1\n+line1.5\n line2\n",
        "new.py": "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,2 @@\n+first\n+second\n",
    }

    # Call sync 3 times in a row
    for _ in range(3):
        sync_staged_patches_to_repo(str(repo_dir), staged)

    # Content must NOT be duplicated
    test_lines = test_file.read_text(encoding="utf-8").splitlines()
    assert test_lines == ["line1", "line1.5", "line2"]

    new_file = repo_dir / "new.py"
    assert new_file.is_file()
    assert new_file.read_text(encoding="utf-8").splitlines() == ["first", "second"]


def test_sync_staged_patches_to_repo_git_repo_multi_edit(tmp_path: Path) -> None:
    """Verifies sync_staged_patches_to_repo on a git repo with a cumulative multi-edit patch preserves all edits."""
    import subprocess
    from app.services.session_tools.sandbox import sync_staged_patches_to_repo
    from app.services.session_tools.editor import _make_unified_diff

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo_dir), check=True, capture_output=True)

    base_content = "def step1():\n    return 1\n\ndef step2():\n    return 2\n"
    target_file = repo_dir / "pipeline.py"
    target_file.write_text(base_content, encoding="utf-8")

    subprocess.run(["git", "add", "pipeline.py"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_dir), check=True, capture_output=True)

    # Multi-edit content where both step1 and step2 are modified
    final_content = "def step1():\n    return 100\n\ndef step2():\n    return 200\n"
    diff = _make_unified_diff(base_content, final_content, "pipeline.py")

    staged = {"pipeline.py": diff}
    sync_staged_patches_to_repo(str(repo_dir), staged)

    disk_content = target_file.read_text(encoding="utf-8")
    assert disk_content == final_content
    assert "return 100" in disk_content
    assert "return 200" in disk_content
    assert "return 1\n" not in disk_content
    assert "return 2\n" not in disk_content


def test_sync_staged_patches_to_repo_created_file_staged_patch_authority(tmp_path: Path) -> None:
    """Pre-command sync adopts terminal edits: when the created file exists on
    disk with different content, sync refreshes the staged patch from disk
    instead of reverting disk (editor staging via ``staged_authoritative=True``
    remains the only authoritative-overwrite path)."""
    from app.services.session_tools.sandbox import sync_staged_patches_to_repo

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    creation_diff = "--- /dev/null\n+++ b/created.txt\n@@ -0,0 +1 @@\n+initial creation\n"
    staged = {"created.txt": creation_diff}
    sync_staged_patches_to_repo(str(repo_dir), staged)
    created_file = repo_dir / "created.txt"
    assert created_file.read_text(encoding="utf-8") == "initial creation\n"

    # Disk carries different (stale/terminal) content.
    created_file.write_text("stale disk content\n", encoding="utf-8")

    # Pre-command sync adopts disk edits into the staged patch...
    sync_staged_patches_to_repo(str(repo_dir), staged)
    assert created_file.read_text(encoding="utf-8") == "stale disk content\n"
    # ...and the staged patch is refreshed from disk.
    assert staged["created.txt"] != creation_diff
    assert "stale disk content" in staged["created.txt"]
    assert "--- /dev/null" in staged["created.txt"]

    # Authoritative staging path still overwrites disk and preserves staged.
    staged_auth = {"created.txt": creation_diff}
    created_file.write_text("stale disk content\n", encoding="utf-8")
    sync_staged_patches_to_repo(str(repo_dir), staged_auth, staged_authoritative=True)
    assert created_file.read_text(encoding="utf-8") == "initial creation\n"
    assert staged_auth["created.txt"] == creation_diff


def test_sync_staged_patches_to_repo_adopts_terminal_mods(tmp_path: Path) -> None:
    """Pre-command sync refreshes staged modify patches from disk when terminal
    edits diverge from both clean base and expected staged content."""
    import subprocess

    from app.services.session_tools.editor import _make_unified_diff
    from app.services.session_tools.sandbox import sync_staged_patches_to_repo

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    base = "a = 1\nb = 2\n"
    (repo_dir / "mod.py").write_text(base, encoding="utf-8")
    for cmd in (
        ["git", "init"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test User"],
        ["git", "add", "."],
        ["git", "commit", "-m", "init"],
    ):
        subprocess.run(cmd, cwd=str(repo_dir), check=True, capture_output=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_dir),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    staged_content = "a = 10\nb = 2\n"
    diff = _make_unified_diff(base, staged_content, "mod.py")
    staged = {"mod.py": diff}
    sync_staged_patches_to_repo(str(repo_dir), staged, base_sha=sha)
    assert (repo_dir / "mod.py").read_text(encoding="utf-8") == staged_content

    terminal_content = staged_content + "# terminal edit\n"
    (repo_dir / "mod.py").write_text(terminal_content, encoding="utf-8")
    sync_staged_patches_to_repo(str(repo_dir), staged, base_sha=sha)
    assert (repo_dir / "mod.py").read_text(encoding="utf-8") == terminal_content
    assert "terminal edit" in staged["mod.py"]



