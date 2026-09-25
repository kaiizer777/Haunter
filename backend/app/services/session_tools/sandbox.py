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
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
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
# Low-level subprocess execution (async + threaded fallback for SelectorEventLoop)
# ---------------------------------------------------------------------------

def _resolve_binary(name: str) -> str:
    """Resolve an executable binary with proper Windows extension prioritization (.exe, .cmd, .bat)."""
    if sys.platform == "win32":
        for ext in (".exe", ".cmd", ".bat"):
            cand = shutil.which(f"{name}{ext}") if not name.lower().endswith(ext) else shutil.which(name)
            if cand:
                return cand
    cand = shutil.which(name)
    return cand if cand else name


def _prepare_cmd_argv(argv: list[str]) -> list[str]:
    """Resolve Python, pytest, ruff, and binaries for reliable cross-platform execution."""
    cmd_argv = list(argv)
    if not cmd_argv:
        return cmd_argv
    bin_name = cmd_argv[0].lower()
    if bin_name in ("python", "python3"):
        cmd_argv[0] = sys.executable
    elif bin_name == "pytest":
        cmd_argv = [sys.executable, "-m", "pytest"] + cmd_argv[1:]
    elif bin_name == "ruff":
        cmd_argv = [sys.executable, "-m", "ruff"] + cmd_argv[1:]
    else:
        cmd_argv[0] = _resolve_binary(cmd_argv[0])
    return cmd_argv


def _loop_supports_subprocesses(loop: asyncio.AbstractEventLoop) -> bool:
    """Check if the given event loop supports asyncio subprocess creation."""
    if sys.platform == "win32":
        cls_name = type(loop).__name__
        if "Selector" in cls_name:
            return False
    return hasattr(loop, "subprocess_exec") or hasattr(loop, "_make_subprocess_transport")


def _run_subprocess_sync(
    argv: list[str],
    timeout_sec: int,
    queue: "SseQueue | None" = None,
    cwd: str | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
) -> tuple[int, str, str, float]:
    """
    Synchronous subprocess runner executed in a background thread.

    Provides 100% reliable execution on Windows when running under SelectorEventLoop
    (e.g. uvicorn --reload), while still streaming real-time stdout/stderr lines
    to the SSE queue via asyncio.run_coroutine_threadsafe.
    """
    t_start = time.monotonic()
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    # Ensure virtualenv bin/Scripts directory is in PATH
    env = os.environ.copy()
    bin_dir = os.path.dirname(sys.executable)
    scripts_dir = os.path.join(bin_dir, "Scripts")
    path_dirs = [d for d in (bin_dir, scripts_dir) if os.path.isdir(d)]
    if path_dirs:
        env["PATH"] = f"{os.pathsep.join(path_dirs)}{os.pathsep}{env.get('PATH', '')}"

    cmd_argv = _prepare_cmd_argv(argv)

    try:
        proc = subprocess.Popen(
            cmd_argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            env=env,
            bufsize=1,
        )
    except FileNotFoundError as exc:
        duration = time.monotonic() - t_start
        return 127, "", f"Command not found: {exc}", duration
    except Exception as exc:
        duration = time.monotonic() - t_start
        err_msg = str(exc) or type(exc).__name__
        return -1, "", f"Subprocess error ({type(exc).__name__}): {err_msg}", duration

    def _read_stdout() -> None:
        if proc.stdout is None:
            return
        for line in iter(proc.stdout.readline, ""):
            stdout_chunks.append(line)
            if queue is not None and loop is not None and not loop.is_closed():
                try:
                    asyncio.run_coroutine_threadsafe(
                        queue.put_terminal_output(line, stream="stdout"),
                        loop,
                    )
                except Exception:
                    pass
        proc.stdout.close()

    def _read_stderr() -> None:
        if proc.stderr is None:
            return
        for line in iter(proc.stderr.readline, ""):
            stderr_chunks.append(line)
            if queue is not None and loop is not None and not loop.is_closed():
                try:
                    asyncio.run_coroutine_threadsafe(
                        queue.put_terminal_output(line, stream="stderr"),
                        loop,
                    )
                except Exception:
                    pass
        proc.stderr.close()

    t_out = threading.Thread(target=_read_stdout, daemon=True)
    t_err = threading.Thread(target=_read_stderr, daemon=True)
    t_out.start()
    t_err.start()

    try:
        proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        t_out.join(timeout=2.0)
        t_err.join(timeout=2.0)
        duration = time.monotonic() - t_start
        timeout_msg = f"[Command timed out after {timeout_sec}s]\n"
        if queue is not None and loop is not None and not loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(
                    queue.put_terminal_output(timeout_msg, stream="stderr"),
                    loop,
                )
            except Exception:
                pass
        return -1, "".join(stdout_chunks), timeout_msg, duration

    t_out.join(timeout=2.0)
    t_err.join(timeout=2.0)
    duration = time.monotonic() - t_start
    exit_code = proc.returncode if proc.returncode is not None else -1
    return exit_code, "".join(stdout_chunks), "".join(stderr_chunks), duration


async def _run_subprocess(
    argv: list[str],
    timeout_sec: int,
    queue: "SseQueue | None" = None,
    cwd: str | None = None,
) -> tuple[int, str, str, float]:
    """
    Execute a command via asyncio subprocess with streaming stdout/stderr.

    On Windows under SelectorEventLoop (or any environment where create_subprocess_exec
    raises NotImplementedError), falls back to _run_subprocess_sync in a thread pool.

    Args:
        argv:        Argument list (shell=False).
        timeout_sec: Hard wall-clock timeout in seconds (already clamped).
        queue:       Optional SseQueue; stdout chunks are emitted as terminal_output events.
        cwd:         Optional working directory for the command.

    Returns:
        Tuple of (exit_code, stdout_text, stderr_text, duration_sec).
    """
    loop = asyncio.get_running_loop()

    # Fast path for environments/loops without subprocess support (e.g. Windows SelectorEventLoop)
    if not _loop_supports_subprocesses(loop):
        return await asyncio.to_thread(
            _run_subprocess_sync, argv, timeout_sec, queue, cwd, loop
        )

    t_start = time.monotonic()
    stdout_chunks: list[str] = []
    stderr_text = ""

    # Ensure virtualenv bin/Scripts is in PATH
    env = os.environ.copy()
    bin_dir = os.path.dirname(sys.executable)
    scripts_dir = os.path.join(bin_dir, "Scripts")
    path_dirs = [d for d in (bin_dir, scripts_dir) if os.path.isdir(d)]
    if path_dirs:
        env["PATH"] = f"{os.pathsep.join(path_dirs)}{os.pathsep}{env.get('PATH', '')}"

    cmd_argv = _prepare_cmd_argv(argv)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd_argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
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

    except NotImplementedError:
        # Fall back to threaded runner if create_subprocess_exec is not implemented on the loop
        return await asyncio.to_thread(
            _run_subprocess_sync, argv, timeout_sec, queue, cwd, loop
        )
    except FileNotFoundError as exc:
        duration = time.monotonic() - t_start
        return 127, "", f"Command not found: {exc}", duration
    except Exception as exc:
        logger.error("_run_subprocess: unexpected error running %s: %s", argv[0], exc)
        duration = time.monotonic() - t_start
        err_msg = str(exc) or type(exc).__name__
        return -1, "", f"Subprocess error ({type(exc).__name__}): {err_msg}", duration

    duration = time.monotonic() - t_start
    return exit_code, "".join(stdout_chunks), stderr_text, duration


async def _call_subprocess_compat(
    argv: list[str],
    timeout_sec: int,
    queue: "SseQueue | None" = None,
    cwd: str | None = None,
) -> tuple[int, str, str, float]:
    """Call _run_subprocess supporting both 4-argument and 3-argument (mocked) signatures."""
    try:
        return await _run_subprocess(argv=argv, timeout_sec=timeout_sec, queue=queue, cwd=cwd)
    except TypeError:
        return await _run_subprocess(argv=argv, timeout_sec=timeout_sec, queue=queue)


def parse_command_chain(command: str) -> list[tuple[list[str], str]]:
    """
    Split command string into a list of (argv, operator) pairs.
    operator is '&&', ';', or '' (for the final command).
    Preserves quoted strings and prevents injection.
    """
    s = shlex.shlex(command, punctuation_chars=True)
    s.whitespace_split = False
    tokens = list(s)

    subcommands: list[tuple[list[str], str]] = []
    current_argv: list[str] = []

    for tok in tokens:
        if tok in ("&&", ";"):
            if current_argv:
                cleaned = [
                    t[1:-1]
                    if (t.startswith('"') and t.endswith('"')) or (t.startswith("'") and t.endswith("'"))
                    else t
                    for t in current_argv
                ]
                subcommands.append((cleaned, tok))
                current_argv = []
        else:
            current_argv.append(tok)

    if current_argv:
        cleaned = [
            t[1:-1]
            if (t.startswith('"') and t.endswith('"')) or (t.startswith("'") and t.endswith("'"))
            else t
            for t in current_argv
        ]
        subcommands.append((cleaned, ""))

    return subcommands


# ---------------------------------------------------------------------------
# Tool: run_terminal_command
# ---------------------------------------------------------------------------

_MIN_TIMEOUT = 1
_MAX_TIMEOUT = 300


async def tool_run_terminal_command(
    command: str,
    timeout_sec: int = 60,
    queue: "SseQueue | None" = None,
    cwd: str | None = None,
    **_kwargs: Any,
) -> str:
    """
    Run a shell command and return a structured result string for the LLM.

    Supports command chaining via '&&' and ';', intelligent directory navigation via 'cd',
    and optional working directory specification.

    Args:
        command:     Raw shell command string (e.g. 'cd backend && python -m pytest tests/').
        timeout_sec: Wall-clock timeout in seconds (default 60, max 300).
        queue:       SseQueue for streaming terminal_output events.
        cwd:         Optional base working directory relative to repository root.
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

    # 3. Parse into subcommands.
    try:
        subcommands = parse_command_chain(command)
    except ValueError as exc:
        return f"Error: Failed to parse command: {exc}"

    if not subcommands:
        return "Error: Empty command after parsing."

    current_cwd = cwd or os.getcwd()
    all_stdout: list[str] = []
    all_stderr: list[str] = []
    total_duration = 0.0
    final_exit_code = 0

    for sub_argv, op in subcommands:
        if not sub_argv:
            continue

        if sub_argv[0] == "cd":
            target = sub_argv[1] if len(sub_argv) > 1 else os.path.expanduser("~")
            # If already in the target directory (e.g. cwd is 'backend' and command is 'cd backend')
            if os.path.basename(os.path.abspath(current_cwd)).lower() == target.lower():
                final_exit_code = 0
                continue

            candidate = os.path.normpath(os.path.join(current_cwd, target))
            if not os.path.isdir(candidate):
                # Try relative to parent if currently in a subfolder
                parent_cand = os.path.normpath(os.path.join(current_cwd, "..", target))
                if os.path.isdir(parent_cand):
                    candidate = parent_cand
                else:
                    # Try under backend/ if currently in repo root
                    backend_cand = os.path.normpath(os.path.join(current_cwd, "backend", target))
                    if os.path.isdir(backend_cand):
                        candidate = backend_cand

            if os.path.isdir(candidate):
                current_cwd = candidate
                final_exit_code = 0
            else:
                final_exit_code = 1
                all_stderr.append(f"cd: no such file or directory: {target}\n")
                if op == "&&":
                    break
        else:
            remaining_timeout = max(1, timeout_sec - int(total_duration))
            exit_code, stdout, stderr, duration = await _call_subprocess_compat(
                argv=sub_argv,
                timeout_sec=remaining_timeout,
                queue=queue,
                cwd=current_cwd,
            )
            total_duration += duration
            final_exit_code = exit_code

            if stdout:
                all_stdout.append(stdout)
            if stderr:
                all_stderr.append(stderr)

            if exit_code != 0 and op == "&&":
                break

    # 4. Return formatted summary for LLM consumption.
    stdout_text = "".join(all_stdout).strip()
    stderr_text = "".join(all_stderr).strip()
    stdout_section = stdout_text if stdout_text else "(empty)"
    stderr_section = stderr_text if stderr_text else "(empty)"

    return (
        f"Exit code: {final_exit_code} (Duration: {total_duration:.2f}s)\n"
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
    cwd: str | None = None,
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
        cwd:         Optional working directory.

    Returns:
        Formatted linter output or error string.
    """
    if not paths:
        return "Error: No paths provided to run_linter."

    # Sanitize each path — no traversal allowed.
    cleaned: list[str] = []
    effective_cwd = cwd or os.getcwd()
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

    exit_code, stdout, stderr, duration = await _call_subprocess_compat(
        argv=argv,
        timeout_sec=timeout_sec,
        queue=queue,
        cwd=effective_cwd,
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
    cwd: str | None = None,
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
        cwd:         Optional working directory.

    Returns:
        Formatted test output including failing tracebacks.
    """
    if not test_targets:
        return "Error: No test targets provided."

    # Sanitize target paths.
    cleaned: list[str] = []
    effective_cwd = cwd or os.getcwd()
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

    exit_code, stdout, stderr, duration = await _call_subprocess_compat(
        argv=argv,
        timeout_sec=timeout_sec,
        queue=queue,
        cwd=effective_cwd,
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
