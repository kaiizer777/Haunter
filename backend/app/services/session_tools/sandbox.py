"""
Sandbox Execution Tools — Live Pairing Session Phase 5.

Provides three agent-callable tools for running shell commands, linters, and
tests inside the session's execution environment, streaming live terminal output
over SSE and returning structured results for LLM self-correction loops.

Execution model:
  Commands are dispatched via asyncio.create_subprocess_exec on the server process.
  Stdout/stderr are streamed in real-time to the SSE queue via put_terminal_output().
  A strict command-injection and destructive-command guard blocks dangerous patterns
  before any subprocess is created.

Security:
  - _sanitize_command() raises ValueError on destructive/injection patterns.
  - timeout_sec is hard-clamped to [1, 300] — never bypassed.
  - Commands are split via shlex.split() and executed with shell=False to prevent
    shell injection (no interpolation of $VAR, no glob expansion, no pipe chaining).
  - Never emits internal paths or stack traces to the SSE stream.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.services.session_streamer import SseQueue

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Security: banned destructive/dangerous command patterns
# ---------------------------------------------------------------------------

# Each entry is a compiled regex.  A match on the raw command string causes
# _sanitize_command() to raise ValueError — no subprocess is created.
_BANNED_PATTERNS: list[re.Pattern[str]] = [
    # Recursive deletion targeting / or root-level paths  (rm -rf /, rm -Rf /, etc.)
    re.compile(r"\brm\b[^|;&\n]*-[a-zA-Z]*r[a-zA-Z]*[^|;&\n]*/(?:\s|$|/)", re.IGNORECASE),
    # Also catch rm -rf without trailing slash (e.g. rm -rf /)
    re.compile(r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*\s+/\s*$|-[a-zA-Z]*r[a-zA-Z]*\s+/$)", re.IGNORECASE | re.MULTILINE),
    # Broader rm -rf / catch: flag before slash root
    re.compile(r"\brm\b.*?-[a-zA-Z]*r[a-zA-Z]*\s+/(?:\s|$)", re.IGNORECASE),
    # mkfs / disk format
    re.compile(r"\bmkfs\b", re.IGNORECASE),
    # Fork bomb
    re.compile(r":\(\)\s*\{.*\|.*&.*\}", re.DOTALL),
    # Shutdown / reboot
    re.compile(r"\b(shutdown|reboot|halt|poweroff|init\s+0|init\s+6)\b", re.IGNORECASE),
    # Writing to /etc
    re.compile(r">\s*/etc/", re.IGNORECASE),
    re.compile(r"\btee\s+/etc/", re.IGNORECASE),
    # dd if= targeting root disk
    re.compile(r"\bdd\b.*\bof=/dev/(s|v|xv|h)d[a-z]\b", re.IGNORECASE),
    # Wiping /dev/sda etc directly
    re.compile(r"\bof=/dev/(s|v|xv|h)d[a-z][0-9]?\b", re.IGNORECASE),
    # chmod 777 on / or /etc
    re.compile(r"\bchmod\b.*\b(777|a\+rwx)\b.*/", re.IGNORECASE),
    # Null out system binaries
    re.compile(r">\s*/(bin|sbin|usr)/", re.IGNORECASE),
]

# Destructive flags/paths that should always be blocked regardless of tool
_DESTRUCTIVE_PATHS: list[str] = ["/etc", "/bin", "/sbin", "/usr/bin", "/dev/"]


def _sanitize_command(command: str) -> str:
    """
    Validate a shell command against known destructive patterns.

    Args:
        command: Raw command string to validate.

    Returns:
        The original command string (unchanged) if safe.

    Raises:
        ValueError: If the command matches a banned destructive pattern or is empty.
    """
    if not command or not command.strip():
        raise ValueError("Command must not be empty.")

    for pattern in _BANNED_PATTERNS:
        if pattern.search(command):
            raise ValueError(
                f"Command blocked: matches destructive pattern {pattern.pattern!r}. "
                "Refusing to execute."
            )

    return command


# ---------------------------------------------------------------------------
# _run_subprocess — low-level async subprocess helper
# ---------------------------------------------------------------------------

async def _run_subprocess(
    argv: list[str],
    timeout_sec: int,
    queue: "SseQueue | None" = None,
) -> tuple[int, str, str, float]:
    """
    Execute a command via asyncio subprocess with streaming stdout/stderr.

    Args:
        argv:        Argument list (shell=False).
        timeout_sec: Hard wall-clock timeout in seconds (already clamped).
        queue:       Optional SseQueue; stdout chunks are emitted as terminal_output events.

    Returns:
        Tuple of (exit_code, stdout_text, stderr_text, duration_sec).

    Notes:
        - Shell=False: no shell interpolation, no pipe chaining.
        - stdout and stderr are collected fully; stdout is also streamed chunk-by-chunk
          if a queue is provided.
    """
    t_start = time.monotonic()
    stdout_chunks: list[str] = []
    stderr_text = ""

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Stream stdout in real time.
        async def _drain_stdout() -> None:
            assert proc.stdout is not None
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace")
                stdout_chunks.append(text)
                if queue is not None:
                    try:
                        await queue.put_terminal_output(text, stream="stdout")
                    except Exception as exc:  # pragma: no cover
                        logger.warning("_run_subprocess: queue emit failed: %s", exc)

        async def _drain_stderr() -> None:
            assert proc.stderr is not None
            raw = await proc.stderr.read()
            nonlocal stderr_text
            stderr_text = raw.decode("utf-8", errors="replace")

        try:
            await asyncio.wait_for(
                asyncio.gather(_drain_stdout(), _drain_stderr()),
                timeout=timeout_sec,
            )
            await proc.wait()
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            duration = time.monotonic() - t_start
            timeout_msg = f"[Command timed out after {timeout_sec}s]\n"
            if queue is not None:
                try:
                    await queue.put_terminal_output(timeout_msg, stream="stderr")
                except Exception:  # pragma: no cover
                    pass
            return -1, "".join(stdout_chunks), timeout_msg, duration

        exit_code = proc.returncode if proc.returncode is not None else -1

    except FileNotFoundError as exc:
        duration = time.monotonic() - t_start
        return 127, "", f"Command not found: {exc}", duration
    except Exception as exc:
        logger.error("_run_subprocess: unexpected error running %s: %s", argv[0], exc)
        duration = time.monotonic() - t_start
        return -1, "", f"Subprocess error: {exc}", duration

    duration = time.monotonic() - t_start
    return exit_code, "".join(stdout_chunks), stderr_text, duration


# ---------------------------------------------------------------------------
# Tool: run_terminal_command
# ---------------------------------------------------------------------------

_MIN_TIMEOUT = 1
_MAX_TIMEOUT = 300


async def tool_run_terminal_command(
    command: str,
    timeout_sec: int = 60,
    queue: "SseQueue | None" = None,
    **_kwargs: Any,
) -> str:
    """
    Run a shell command and return a structured result string for the LLM.

    Security: command is validated via _sanitize_command() before execution.
    Timeout is clamped to [1, 300] seconds.

    Args:
        command:     Raw shell command string (will be split via shlex.split).
        timeout_sec: Wall-clock timeout in seconds (default 60, max 300).
        queue:       SseQueue for streaming terminal_output events.
        **_kwargs:   Accepts and ignores session/repo/staged_patches for signature consistency.

    Returns:
        Formatted string:
            Exit code: {exit_code} (Duration: {duration:.2f}s)
            STDOUT:
            {stdout}
            STDERR:
            {stderr}
    """
    # 1. Validate command.
    try:
        _sanitize_command(command)
    except ValueError as exc:
        return f"Error: {exc}"

    # 2. Clamp timeout.
    timeout_sec = max(_MIN_TIMEOUT, min(_MAX_TIMEOUT, timeout_sec))

    # 3. Parse into argv — shell=False prevents injection.
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return f"Error: Failed to parse command: {exc}"

    if not argv:
        return "Error: Empty command after parsing."

    # 4. Execute.
    exit_code, stdout, stderr, duration = await _run_subprocess(
        argv=argv,
        timeout_sec=timeout_sec,
        queue=queue,
    )

    # 5. Return formatted summary for LLM consumption.
    stdout_section = stdout.strip() if stdout.strip() else "(empty)"
    stderr_section = stderr.strip() if stderr.strip() else "(empty)"

    return (
        f"Exit code: {exit_code} (Duration: {duration:.2f}s)\n"
        f"STDOUT:\n{stdout_section}\n"
        f"STDERR:\n{stderr_section}"
    )


# ---------------------------------------------------------------------------
# Tool: run_linter
# ---------------------------------------------------------------------------

def _select_linter(paths: list[str], linter: str = "auto") -> tuple[str, list[str]]:
    """
    Choose the appropriate linter command for the given file paths.

    Returns:
        Tuple of (linter_name, argv_prefix).
        The caller appends the paths to argv_prefix to build the full command.
    """
    if linter != "auto":
        # User-specified linter — use as-is.
        return linter, shlex.split(linter)

    # Infer from extensions.
    exts: set[str] = set()
    for p in paths:
        dot = p.rfind(".")
        if dot != -1:
            exts.add(p[dot:].lower())

    ts_exts = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
    py_exts = {".py", ".pyx", ".pyw"}

    has_ts = bool(exts & ts_exts)
    has_py = bool(exts & py_exts)

    if has_ts and not has_py:
        return "eslint", ["npx", "--no-install", "eslint", "--no-eslintrc", "-c", "{}"]
    # Default: ruff for Python and mixed/unknown.
    return "ruff", ["ruff", "check"]


async def tool_run_linter(
    paths: list[str],
    linter: str = "auto",
    timeout_sec: int = 60,
    queue: "SseQueue | None" = None,
    **_kwargs: Any,
) -> str:
    """
    Run static analysis on the specified file paths.

    Auto-detects linter:
      .py / mixed → ruff check <paths>
      .ts / .tsx / .js → npx eslint --no-eslintrc -c {} <paths>

    Args:
        paths:       List of relative file paths to lint.
        linter:      "auto" (default) or explicit linter name for override.
        timeout_sec: Command timeout in seconds (clamped to [1, 300]).
        queue:       SseQueue for streaming terminal_output events.

    Returns:
        Formatted linter output or error string.
    """
    if not paths:
        return "Error: No paths provided to run_linter."

    # Sanitize each path — no traversal allowed.
    cleaned: list[str] = []
    for p in paths:
        stripped = p.strip()
        if ".." in stripped or stripped.startswith("/"):
            return f"Error: Path rejected — traversal or absolute path not allowed: {p!r}"
        cleaned.append(stripped)

    linter_name, argv_prefix = _select_linter(cleaned, linter)
    argv = argv_prefix + cleaned

    command_str = shlex.join(argv)
    try:
        _sanitize_command(command_str)
    except ValueError as exc:
        return f"Error: {exc}"

    timeout_sec = max(_MIN_TIMEOUT, min(_MAX_TIMEOUT, timeout_sec))

    exit_code, stdout, stderr, duration = await _run_subprocess(
        argv=argv,
        timeout_sec=timeout_sec,
        queue=queue,
    )

    stdout_section = stdout.strip() if stdout.strip() else "(no issues found)"
    stderr_section = stderr.strip() if stderr.strip() else "(empty)"

    return (
        f"Linter: {linter_name}\n"
        f"Files: {', '.join(cleaned)}\n"
        f"Exit code: {exit_code} (Duration: {duration:.2f}s)\n"
        f"OUTPUT:\n{stdout_section}\n"
        f"STDERR:\n{stderr_section}"
    )


# ---------------------------------------------------------------------------
# Tool: run_targeted_tests
# ---------------------------------------------------------------------------

def _select_test_framework(targets: list[str]) -> tuple[str, list[str]]:
    """
    Infer the test runner from target file extensions.

    Returns:
        Tuple of (framework_name, argv_prefix).
    """
    for t in targets:
        lower = t.lower()
        # Vitest targets: .ts/.tsx/.js or spec.*/test.* patterns
        if any(lower.endswith(e) for e in (".ts", ".tsx", ".js", ".jsx")) or \
                ".spec." in lower or ".test." in lower:
            return "vitest", ["npx", "vitest", "run"]

    # Default: pytest for .py and anything unknown.
    return "pytest", ["pytest", "-v"]


async def tool_run_targeted_tests(
    test_targets: list[str],
    timeout_sec: int = 120,
    queue: "SseQueue | None" = None,
    **_kwargs: Any,
) -> str:
    """
    Execute targeted test files via the appropriate test runner.

    Auto-detects framework:
      .py files    → pytest -v <targets>
      .ts/.tsx etc → npx vitest run <targets>

    Args:
        test_targets: List of test file paths or pytest node IDs.
        timeout_sec:  Command timeout in seconds (clamped to [1, 300]).
        queue:        SseQueue for streaming terminal_output events.

    Returns:
        Formatted test output including failing tracebacks.
    """
    if not test_targets:
        return "Error: No test targets provided."

    # Sanitize target paths.
    cleaned: list[str] = []
    for t in test_targets:
        stripped = t.strip()
        if ".." in stripped or stripped.startswith("/"):
            return f"Error: Target rejected — traversal or absolute path not allowed: {t!r}"
        cleaned.append(stripped)

    framework, argv_prefix = _select_test_framework(cleaned)
    argv = argv_prefix + cleaned

    command_str = shlex.join(argv)
    try:
        _sanitize_command(command_str)
    except ValueError as exc:
        return f"Error: {exc}"

    timeout_sec = max(_MIN_TIMEOUT, min(_MAX_TIMEOUT, timeout_sec))

    exit_code, stdout, stderr, duration = await _run_subprocess(
        argv=argv,
        timeout_sec=timeout_sec,
        queue=queue,
    )

    stdout_section = stdout.strip() if stdout.strip() else "(no output)"
    stderr_section = stderr.strip() if stderr.strip() else "(empty)"
    status_label = "PASSED" if exit_code == 0 else "FAILED"

    return (
        f"Test runner: {framework}\n"
        f"Targets: {', '.join(cleaned)}\n"
        f"Status: {status_label}\n"
        f"Exit code: {exit_code} (Duration: {duration:.2f}s)\n"
        f"OUTPUT:\n{stdout_section}\n"
        f"STDERR:\n{stderr_section}"
    )
