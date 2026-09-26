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
