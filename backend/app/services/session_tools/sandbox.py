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
    re.compile(
        r"\brm\b[^|;&\n]*-[a-zA-Z]*r[a-zA-Z]*[^|;&\n]*/(?:\s|$|/)", re.IGNORECASE
    ),
    # Also catch rm -rf without trailing slash (e.g. rm -rf /)
    re.compile(
        r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*\s+/\s*$|-[a-zA-Z]*r[a-zA-Z]*\s+/$)",
        re.IGNORECASE | re.MULTILINE,
    ),
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
            cand = (
                shutil.which(f"{name}{ext}")
                if not name.lower().endswith(ext)
                else shutil.which(name)
            )
            if cand:
                return cand
    cand = shutil.which(name)
    return cand if cand else name


def _find_repo_python(cwd: str | None = None) -> str | None:
    """Find a local virtualenv python in cwd or adjacent directories if one exists."""
    if not cwd or not os.path.isdir(cwd):
        return None
    curr = os.path.abspath(cwd)
    for _ in range(5):
        for venv_name in (".venv", "venv", "env"):
            if sys.platform == "win32":
                cand = os.path.join(curr, venv_name, "Scripts", "python.exe")
                cand_sub = os.path.join(curr, "backend", venv_name, "Scripts", "python.exe")
            else:
                cand = os.path.join(curr, venv_name, "bin", "python")
                cand_sub = os.path.join(curr, "backend", venv_name, "bin", "python")
            if os.path.isfile(cand):
                return cand
            if os.path.isfile(cand_sub):
                return cand_sub
        parent = os.path.dirname(curr)
        if parent == curr:
            break
        curr = parent
    return None


def _has_runner(python_bin: str, runner_name: str) -> bool:
    """Check if a tool runner (e.g. 'pytest' or 'ruff') exists in the python environment."""
    bin_dir = os.path.dirname(python_bin)
    cand = os.path.join(
        bin_dir, f"{runner_name}.exe" if sys.platform == "win32" else runner_name
    )
    if os.path.isfile(cand):
        return True
    parent_dir = os.path.dirname(bin_dir)
    for site_sub in (
        os.path.join("Lib", "site-packages"),
        os.path.join("lib", f"python{sys.version_info.major}.{sys.version_info.minor}", "site-packages"),
        "site-packages",
    ):
        target_dir = os.path.join(parent_dir, site_sub)
        if os.path.isdir(target_dir):
            if os.path.isdir(os.path.join(target_dir, runner_name)):
                return True
            for entry in os.listdir(target_dir):
                if entry.lower().startswith(runner_name.lower()):
                    return True
    lib_dir = os.path.join(parent_dir, "lib")
    if os.path.isdir(lib_dir):
        try:
            for sub in os.listdir(lib_dir):
                if sub.startswith("python"):
                    sp = os.path.join(lib_dir, sub, "site-packages")
                    if os.path.isdir(sp) and (
                        os.path.isdir(os.path.join(sp, runner_name))
                        or any(e.lower().startswith(runner_name.lower()) for e in os.listdir(sp))
                    ):
                        return True
        except Exception:
            pass
    return False


def _prepare_cmd_argv(argv: list[str], cwd: str | None = None) -> list[str]:
    """Resolve Python, pytest, ruff, and binaries for reliable cross-platform execution."""
    cmd_argv = list(argv)
    if not cmd_argv:
        return cmd_argv
    bin_name = cmd_argv[0].lower()

    # On Windows, wrap shell builtins so subprocess.Popen succeeds with shell=False
    if sys.platform == "win32":
        if bin_name in (
            "dir", "del", "copy", "type", "cls", "mkdir", "md", "rmdir", "rd", "move", "echo", "time", "ver"
        ):
            for arg in cmd_argv[1:]:
                if any(char in arg for char in ("&", "|", "<", ">", "^")):
                    raise ValueError(
                        f"Shell metacharacters are not permitted in arguments for Windows built-in '{bin_name}'"
                    )
            return ["cmd", "/c"] + cmd_argv
        if bin_name == "cat" and not shutil.which("cat"):
            file_args = [a for a in cmd_argv[1:] if not a.startswith("-")]
            code = (
                "import sys\n"
                "err = 0\n"
                "for p in sys.argv[1:]:\n"
                "    try:\n"
                "        with open(p, 'rb') as f:\n"
                "            sys.stdout.buffer.write(f.read())\n"
                "    except Exception as e:\n"
                "        sys.stderr.write(f'cat: {p}: {e}\\n')\n"
                "        err = 1\n"
                "if err:\n"
                "    sys.exit(err)\n"
            )
            return [sys.executable, "-c", code] + file_args

    repo_python = _find_repo_python(cwd)
    effective_python = repo_python if repo_python else sys.executable

    if bin_name in ("python", "python3"):
        cmd_argv[0] = effective_python
    elif bin_name in ("pytest", "ruff"):
        runner_python = (
            repo_python
            if (repo_python and _has_runner(repo_python, bin_name))
            else sys.executable
        )
        cmd_argv = [runner_python, "-m", bin_name] + cmd_argv[1:]
    else:
        cmd_argv[0] = _resolve_binary(cmd_argv[0])
    return cmd_argv


def _loop_supports_subprocesses(loop: asyncio.AbstractEventLoop) -> bool:
    """Check if the given event loop supports asyncio subprocess creation."""
    if sys.platform == "win32":
        cls_name = type(loop).__name__
        if "Selector" in cls_name:
            return False
    return hasattr(loop, "subprocess_exec") or hasattr(
        loop, "_make_subprocess_transport"
    )


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
    repo_python = _find_repo_python(cwd)
    effective_python = repo_python if repo_python else sys.executable
    bin_dir = os.path.dirname(effective_python)
    scripts_dir = os.path.join(bin_dir, "Scripts")
    path_dirs = [d for d in (bin_dir, scripts_dir) if os.path.isdir(d)]
    if path_dirs:
        env["PATH"] = f"{os.pathsep.join(path_dirs)}{os.pathsep}{env.get('PATH', '')}"

    try:
        cmd_argv = _prepare_cmd_argv(argv, cwd=cwd)
    except ValueError as exc:
        duration = time.monotonic() - t_start
        return 1, "", f"Error: {exc}", duration

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
    repo_python = _find_repo_python(cwd)
    effective_python = repo_python if repo_python else sys.executable
    bin_dir = os.path.dirname(effective_python)
    scripts_dir = os.path.join(bin_dir, "Scripts")
    path_dirs = [d for d in (bin_dir, scripts_dir) if os.path.isdir(d)]
    if path_dirs:
        env["PATH"] = f"{os.pathsep.join(path_dirs)}{os.pathsep}{env.get('PATH', '')}"

    try:
        cmd_argv = _prepare_cmd_argv(argv, cwd=cwd)
    except ValueError as exc:
        duration = time.monotonic() - t_start
        return 1, "", f"Error: {exc}", duration

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
        return await _run_subprocess(
            argv=argv, timeout_sec=timeout_sec, queue=queue, cwd=cwd
        )
    except TypeError:
        return await _run_subprocess(argv=argv, timeout_sec=timeout_sec, queue=queue)


def parse_command_chain(command: str) -> list[tuple[list[str], str]]:
    """
    Split command string into a list of (argv, operator) pairs.
    operator is '&&', ';', or '' (for the final command).

    Robustly handles:
      - Quotes ('...' and "...") and escaped characters without mangling arguments.
      - Preserves arguments with colons (e.g. 'git show HEAD:path').
      - Strips redundant shell redirection tokens ('2>&1', '1>&2', etc.) that break shell=False subprocesses.
      - Preserves multi-line strings and newlines inside quoted strings.
    """
    if not command or not command.strip():
        return []

    # 1. Split into subcommands on '&&' and ';' strictly outside quoted blocks
    raw_subcommands: list[tuple[str, str]] = []
    current: list[str] = []
    in_single_quote = False
    in_double_quote = False
    escape = False

    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if escape:
            current.append(ch)
            escape = False
            i += 1
            continue

        if ch == "\\" and not in_single_quote:
            escape = True
            current.append(ch)
            i += 1
            continue

        if ch == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            current.append(ch)
            i += 1
            continue

        if ch == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            current.append(ch)
            i += 1
            continue

        if not in_single_quote and not in_double_quote:
            if command[i : i + 2] == "&&":
                raw_subcommands.append(("".join(current).strip(), "&&"))
                current = []
                i += 2
                continue
            elif ch == ";":
                raw_subcommands.append(("".join(current).strip(), ";"))
                current = []
                i += 1
                continue

        current.append(ch)
        i += 1

    if current:
        tail = "".join(current).strip()
        if tail:
            raw_subcommands.append((tail, ""))

    # 2. For each subcommand, parse into argv using shlex.split with posix mode
    subcommands: list[tuple[list[str], str]] = []
    for sub_str, op in raw_subcommands:
        if not sub_str:
            continue
        try:
            tokens = shlex.split(sub_str, posix=True)
        except ValueError:
            # Fallback on non-posix if unclosed quote
            tokens = shlex.split(sub_str, posix=False)

        # Clean out common shell redirects that break subprocess with shell=False
        cleaned_tokens = [t for t in tokens if t not in ("2>&1", "1>&2", ">&1", ">&2")]
        if cleaned_tokens:
            subcommands.append((cleaned_tokens, op))

    return subcommands


def sync_staged_patches_to_repo(
    repo_root: str,
    staged_patches: dict[str, str] | None,
    base_sha: str | None = None,
    session_id: str | None = None,
    staged_authoritative: bool = False,
) -> None:
    """
    Ensure all patches currently staged in memory/DB are synced to the local repository checkout.
    This guarantees that local terminal commands, linters, and test runners execute
    against the actual patched code, eliminating runner/editor checkout mismatches.
    Protects against symlink escapes, makes application idempotent, and preserves full file content.

    When ``session_id`` is provided and a session-isolated directory exists
    under ``repo_root``, patches are synced to the session checkout instead.

    When ``staged_authoritative`` is False (default, pre-command sync for
    terminal/linter/tests), direct disk modifications (e.g. ``sed -i``) are
    adopted: if disk differs from both the clean base and the expected staged
    content, ``staged_patches[path]`` is rebuilt from disk instead of
    reverting disk. When True (editor staging path), the staged patch wins
    and disk is overwritten.
    """
    if not staged_patches or not repo_root or not os.path.isdir(repo_root):
        return

    from app.sandbox.mirror import apply_unified_diff

    real_root = os.path.realpath(repo_root)
    if session_id and session_id.strip() and _is_safe_session_component(session_id):
        session_root = _resolve_session_root(real_root, session_id.strip())
        if session_root:
            real_root = session_root

    for rel_path, diff in staged_patches.items():
        if not diff or not diff.strip():
            continue
        try:
            target_path = os.path.normpath(os.path.join(real_root, rel_path))
            real_target = os.path.realpath(target_path)
            real_parent = os.path.realpath(os.path.dirname(target_path))

            # Defense-in-depth: Ensure target and its parent do not escape repo boundary via symlinks
            if os.path.commonpath([real_root, real_parent]) != real_root:
                logger.warning("sandbox: rejected path outside repository root: %r", rel_path)
                continue
            if os.path.commonpath([real_root, real_target]) != real_root:
                logger.warning("sandbox: rejected symlink escaping repository root: %r", rel_path)
                continue
            if os.path.islink(target_path):
                logger.warning("sandbox: rejected write to symlink: %r", rel_path)
                continue

            # If it's a file deletion patch
            if "--- " in diff and "+++ /dev/null" in diff:
                if os.path.isfile(real_target):
                    os.remove(real_target)
                continue

            # If it's a file creation patch
            if "--- /dev/null" in diff:
                expected_content = apply_unified_diff("", diff)
                if (
                    diff.endswith("\n")
                    and "\\ No newline at end of file" not in diff
                    and expected_content
                    and not expected_content.endswith("\n")
                ):
                    expected_content += "\n"
                if os.path.isfile(real_target):
                    with open(real_target, "r", encoding="utf-8", errors="replace") as f:
                        disk_content = f.read()
                    if disk_content == expected_content:
                        continue  # already synchronized, avoid rewriting
                    if not staged_authoritative:
                        # Terminal modified the created file directly: adopt disk
                        # as ground truth and refresh the staged patch instead
                        # of reverting disk.
                        import difflib

                        staged_patches[rel_path] = "".join(
                            difflib.unified_diff(
                                [],
                                disk_content.splitlines(keepends=True),
                                fromfile="/dev/null",
                                tofile=f"b/{rel_path}",
                            )
                        )
                        logger.info(
                            "sandbox: adopted terminal edits on disk for created file %r into staged patch",
                            rel_path,
                        )
                        continue
                    # Authoritative staging path (editor _tool_stage_patch):
                    # write the staged creation content to disk even when the
                    # file already exists with different content. Never mutate
                    # the incoming staged patch with old disk content.
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                with open(target_path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(expected_content)
                logger.info("sandbox: synced staged created file %r to local disk", rel_path)
                continue

            # If it's a modify patch:
            # Reconstruct target content from clean git base bound to the session's
            # base_sha when available; only fall back to local HEAD when no base_sha
            # is provided (local HEAD can differ from the session base commit).
            clean_base: str | None = None
            git_path = rel_path.replace("\\", "/")
            try:
                if base_sha and base_sha.strip():
                    res = subprocess.run(
                        ["git", "-C", repo_root, "show", f"{base_sha.strip()}:{git_path}"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if res.returncode == 0:
                        clean_base = res.stdout
                else:
                    res = subprocess.run(
                        ["git", "-C", repo_root, "show", f"HEAD:{git_path}"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if res.returncode == 0:
                        clean_base = res.stdout
            except Exception:
                clean_base = None

            if clean_base is not None:
                new_content = apply_unified_diff(clean_base, diff)
                if os.path.isfile(real_target):
                    with open(real_target, "r", encoding="utf-8", errors="replace") as f:
                        disk_content = f.read()
                    if disk_content == new_content:
                        continue  # already synchronized, avoid duplicate diff application
                    if not staged_authoritative and disk_content != clean_base:
                        # Disk was modified directly (e.g. sed -i) on top of or
                        # instead of the staged state: adopt disk and refresh
                        # the staged patch rather than reverting terminal edits.
                        import difflib

                        staged_patches[rel_path] = "".join(
                            difflib.unified_diff(
                                clean_base.splitlines(keepends=True),
                                disk_content.splitlines(keepends=True),
                                fromfile=f"a/{rel_path}",
                                tofile=f"b/{rel_path}",
                            )
                        )
                        logger.info(
                            "sandbox: adopted terminal edits on disk for %r into staged patch",
                            rel_path,
                        )
                        continue
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                with open(target_path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(new_content)
                logger.info("sandbox: synced staged patch for %r from clean base to local disk", rel_path)
            else:
                # Fallback when file is not in git HEAD:
                base_content = ""
                if os.path.isfile(real_target):
                    with open(real_target, "r", encoding="utf-8", errors="replace") as f:
                        base_content = f.read()

                # If the diff was already applied to base_content, skip to prevent line duplication
                from app.services.session_tools.editor import _is_diff_applied
                if base_content and _is_diff_applied(base_content, diff):
                    continue

                new_content = apply_unified_diff(base_content, diff)
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                with open(target_path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(new_content)
                logger.info("sandbox: synced staged patch for %r to local disk", rel_path)
        except Exception as exc:
            logger.warning("sandbox: failed to sync staged patch %r to disk: %s", rel_path, exc)


_SAFE_REPO_COMPONENT_RE = re.compile(r"^[a-zA-Z0-9_.-]+$")


def _is_safe_repo_component(val: str) -> bool:
    """Check that a repository or owner component contains no path traversal characters."""
    if not val or ".." in val or "/" in val or "\\" in val:
        return False
    return bool(_SAFE_REPO_COMPONENT_RE.match(val))


def _get_git_remote_url(repo_root: str) -> str | None:
    """
    Extract the origin remote URL from a local git repository without running subprocesses.

    Reads .git/config directly or follows gitdir links for submodules and worktrees.
    """
    git_dir = os.path.join(repo_root, ".git")
    config_file: str | None = None
    if os.path.isdir(git_dir):
        config_file = os.path.join(git_dir, "config")
    elif os.path.isfile(git_dir):
        try:
            with open(git_dir, "r", encoding="utf-8", errors="ignore") as f:
                first_line = f.readline().strip()
                if first_line.startswith("gitdir:"):
                    gd = first_line.split(":", 1)[1].strip()
                    gd_abs = gd if os.path.isabs(gd) else os.path.join(repo_root, gd)
                    common = os.path.join(gd_abs, "commondir")
                    if os.path.isfile(common):
                        try:
                            with open(common, "r", encoding="utf-8", errors="ignore") as cf:
                                cd = cf.read().strip()
                            gd_abs = cd if os.path.isabs(cd) else os.path.join(gd_abs, cd)
                        except Exception:
                            pass
                    cand_config = os.path.join(gd_abs, "config")
                    if os.path.isfile(cand_config):
                        config_file = cand_config
        except Exception:
            pass

    if config_file and os.path.isfile(config_file):
        try:
            with open(config_file, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            match = re.search(
                r'\[remote\s+["\']?origin["\']?\][^\[]*?url\s*=\s*([^\r\n]+)',
                content,
                re.IGNORECASE,
            )
            if match:
                return match.group(1).strip()
            match_any = re.search(
                r'\[remote\s+[^\]]+\][^\[]*?url\s*=\s*([^\r\n]+)',
                content,
                re.IGNORECASE,
            )
            if match_any:
                return match_any.group(1).strip()
        except Exception:
            pass

    return None


def _candidate_matches_owner(candidate_dir: str, repo_owner: str | None) -> bool:
    """
    Verify that an existing checkout candidate matches the expected repository owner.

    If repo_owner is not specified, accepts any candidate.
    If repo_owner is specified:
      - Validates the Git origin URL against repo_owner.
      - If no remote is configured, checks whether the candidate directory is nested under an owner folder.
      - Skips candidate directories that belong to a different owner.
    """
    if not repo_owner or not repo_owner.strip():
        return True

    owner_lower = repo_owner.strip().lower()
    remote_url = _get_git_remote_url(candidate_dir)
    if remote_url:
        norm_url = remote_url.lower().replace("\\", "/")
        if f"/{owner_lower}/" in norm_url or f":{owner_lower}/" in norm_url:
            return True
        return False

    parent_basename = os.path.basename(os.path.dirname(os.path.abspath(candidate_dir))).lower()
    if parent_basename == owner_lower:
        return True

    return False


# ---------------------------------------------------------------------------
# Repository Working Directory Resolver
# ---------------------------------------------------------------------------


_SAFE_SESSION_COMPONENT_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,128}$")


def _is_safe_session_component(val: str | None) -> bool:
    """Validate a session_id for safe use as a path component (no traversal)."""
    if not val:
        return False
    stripped = val.strip()
    if not stripped or ".." in stripped or "/" in stripped or "\\" in stripped:
        return False
    return bool(_SAFE_SESSION_COMPONENT_RE.match(stripped))


def _resolve_session_root(main_root_real: str, session_id: str) -> str | None:
    """
    Return a session-isolated directory under the main checkout when present.

    Checks, in order:
      1. ``<repo>/.haunter_sessions/<session_id>``
      2. ``<repo>/.worktrees/session-<session_id>``
      3. ``<repo>/session-<session_id>``
      4. ``<parent>/.haunter_sessions/<session_id>/<repo_basename>``
    Returns None when no session-isolated directory exists (caller falls
    back safely to the main checkout).

    Hardening: empty/uninitialized directories are skipped; candidates that
    escape the repository boundary via symlinks are rejected unless they are
    legitimate git worktrees; candidates must look like a checkout (valid
    git repo/worktree or containing files).
    """
    sid = session_id.strip()
    base = os.path.basename(main_root_real)
    parent = os.path.dirname(main_root_real)
    candidates = [
        os.path.join(main_root_real, ".haunter_sessions", sid),
        os.path.join(main_root_real, ".worktrees", f"session-{sid}"),
        os.path.join(main_root_real, f"session-{sid}"),
        os.path.join(parent, ".haunter_sessions", sid, base),
    ]
    session_base_real = os.path.realpath(os.path.join(parent, ".haunter_sessions", sid))

    def _is_valid_checkout(cand_real: str) -> bool:
        try:
            entries = os.listdir(cand_real)
        except Exception:
            return False
        if not entries:
            return False
        if os.path.exists(os.path.join(cand_real, ".git")):
            return True
        try:
            rev = subprocess.run(
                ["git", "-C", cand_real, "rev-parse", "--is-inside-work-tree"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if rev.returncode == 0 and rev.stdout.strip().lower() == "true":
                return True
        except Exception:
            pass
        # Non-empty directory containing files qualifies as a checkout root
        # (e.g. plain session dir synced without .git metadata).
        return True

    def _is_legitimate_worktree(cand_real: str, worktrees: set[str]) -> bool:
        try:
            norm = os.path.normcase(os.path.realpath(cand_real))
        except Exception:
            return False
        return norm in worktrees

    worktree_paths: set[str] = set()
    try:
        _wt = subprocess.run(
            ["git", "-C", main_root_real, "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if _wt.returncode == 0:
            for _line in _wt.stdout.splitlines():
                if _line.startswith("worktree "):
                    _wp = _line[len("worktree ") :].strip()
                    if _wp:
                        try:
                            worktree_paths.add(os.path.normcase(os.path.realpath(_wp)))
                        except Exception:
                            continue
    except Exception:
        pass

    for idx, cand in enumerate(candidates):
        try:
            if not os.path.isdir(cand):
                continue
            cand_real = os.path.realpath(cand)
            if not os.path.isdir(cand_real):
                continue
            # Skip empty/uninitialized session dirs; fall back to main checkout.
            try:
                if not os.listdir(cand_real):
                    continue
            except Exception:
                continue
            # Symlink-boundary check: candidates under the main checkout must
            # resolve inside it; the parent-scoped candidate must resolve
            # inside its session base. Legitimate git worktrees are exempt.
            if idx < 3:
                try:
                    inside = os.path.commonpath([main_root_real, cand_real]) == main_root_real
                except ValueError:
                    inside = False
                if not inside and not _is_legitimate_worktree(cand_real, worktree_paths):
                    logger.warning("sandbox: rejected session dir escaping repo boundary: %r", cand)
                    continue
            else:
                try:
                    inside = os.path.commonpath([session_base_real, cand_real]) == session_base_real
                except ValueError:
                    inside = False
                if not inside and not _is_legitimate_worktree(cand_real, worktree_paths):
                    logger.warning("sandbox: rejected session dir escaping session base: %r", cand)
                    continue
            if not _is_valid_checkout(cand_real):
                continue
            return cand_real
        except Exception:
            continue
    # Best-effort git worktree lookup: a worktree whose path ends with
    # session-<id> is treated as the session checkout when it exists on disk.
    try:
        res = subprocess.run(
            ["git", "-C", main_root_real, "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                if line.startswith("worktree "):
                    wt_path = line[len("worktree ") :].strip()
                    if wt_path.endswith(f"session-{sid}") and os.path.isdir(wt_path):
                        wt_real = os.path.realpath(wt_path)
                        try:
                            if not os.listdir(wt_real):
                                continue
                        except Exception:
                            continue
                        return wt_real
    except Exception:
        pass
    return None


def resolve_repo_dir(
    repo_name: str | None = None,
    repo_owner: str | None = None,
    cwd: str | None = None,
    session_id: str | None = None,
) -> tuple[str | None, str | None]:
    """
    Resolve the absolute working directory for repository execution.

    If repo_name is provided, locates the local repository checkout on disk:
      1. Explicit environment variables (LOCAL_REPOS_DIR, REPOS_DIR, WORKSPACE_DIR).
      2. Traversal up from current working directory (checking matching basename, children, and siblings).
      3. Common developer directories (~/Desktop, ~, ~/projects, ~/repos, ~/workspace).

    When ``session_id`` is provided and a session-isolated worktree or
    session-scoped directory (``.haunter_sessions/<session_id>`` or
    ``session-<session_id>`` worktree) exists, it is preferred; otherwise
    falls back safely to the main checkout.

    Returns:
      (effective_cwd, None) on success.
      (None, error_message) if repo_name is provided but cannot be found locally.
    """
    if not repo_name or not repo_name.strip():
        base_dir = os.path.realpath(os.getcwd())
        if cwd and cwd.strip():
            stripped = cwd.strip()
            target_candidate = (
                stripped if os.path.isabs(stripped) else os.path.join(base_dir, stripped)
            )
            return os.path.realpath(target_candidate), None
        return base_dir, None

    clean_repo = repo_name.strip()
    if clean_repo.endswith(".git"):
        clean_repo = clean_repo[:-4]

    clean_owner = repo_owner.strip() if repo_owner and repo_owner.strip() else None

    # Defense-in-depth: Validate components to reject directory traversal
    if not _is_safe_repo_component(clean_repo):
        return None, f"Error: Invalid repository name {repo_name!r}."
    if clean_owner and not _is_safe_repo_component(clean_owner):
        return None, f"Error: Invalid repository owner {repo_owner!r}."

    candidates: list[str] = []

    # 1. Environment variables
    for env_var in ("LOCAL_REPOS_DIR", "REPOS_DIR", "WORKSPACE_DIR", "PROJECTS_DIR"):
        val = os.getenv(env_var)
        if val and os.path.isdir(val):
            if clean_owner:
                candidates.append(os.path.join(val, clean_owner, clean_repo))
            candidates.append(os.path.join(val, clean_repo))

    # 2. Traversal up from current working directory (e.g. backend/ -> Haunter/ -> Desktop/)
    curr = os.path.abspath(os.getcwd())
    for _ in range(5):
        if os.path.basename(curr).lower() == clean_repo.lower():
            candidates.append(curr)
        candidates.append(os.path.join(curr, clean_repo))
        if clean_owner:
            candidates.append(os.path.join(curr, clean_owner, clean_repo))
        for sub in ("projects", "repos", "workspace", "src"):
            if clean_owner:
                candidates.append(os.path.join(curr, sub, clean_owner, clean_repo))
            candidates.append(os.path.join(curr, sub, clean_repo))
        parent = os.path.dirname(curr)
        if parent == curr:
            break
        curr = parent

    # 3. User home and desktop common developer locations
    home = os.path.expanduser("~")
    for base in (
        os.path.join(home, "Desktop"),
        home,
        os.path.join(home, "projects"),
        os.path.join(home, "repos"),
        os.path.join(home, "workspace"),
        os.path.join(home, "source", "repos"),
        os.path.join(home, "src"),
    ):
        if clean_owner:
            candidates.append(os.path.join(base, clean_owner, clean_repo))
        candidates.append(os.path.join(base, clean_repo))

    resolved_repo_root: str | None = None
    seen: set[str] = set()
    for cand in candidates:
        norm = os.path.normpath(cand)
        if norm in seen:
            continue
        seen.add(norm)
        if os.path.isdir(norm) and _candidate_matches_owner(norm, clean_owner):
            resolved_repo_root = norm
            break

    if not resolved_repo_root:
        repo_desc = f"{clean_owner}/{clean_repo}" if clean_owner else clean_repo
        return None, (
            f"Error: Local checkout for repository {repo_desc!r} was not found on this machine. "
            "Local terminal commands and test runners require a local clone of the repository. "
            "To verify staged patches in an isolated cloud environment, use 'verify_in_ci_sandbox'."
        )

    root_real = os.path.realpath(resolved_repo_root)

    if session_id and session_id.strip() and _is_safe_session_component(session_id):
        session_root = _resolve_session_root(root_real, session_id.strip())
        if session_root:
            root_real = session_root
            resolved_repo_root = session_root

    if cwd and cwd.strip():
        stripped_cwd = cwd.strip()
        target_candidate = (
            stripped_cwd
            if os.path.isabs(stripped_cwd)
            else os.path.join(root_real, stripped_cwd)
        )
        target_real = os.path.realpath(target_candidate)
        try:
            inside = (
                os.path.normcase(os.path.commonpath([root_real, target_real]))
                == os.path.normcase(root_real)
            )
        except ValueError:
            inside = False

        if not inside:
            return None, (
                f"Error: Working directory {cwd!r} is outside repository root {resolved_repo_root!r}."
            )
        return target_real, None

    return root_real, None


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
    repo_owner: str | None = None,
    repo_name: str | None = None,
    session_id: str | None = None,
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
        repo_owner:  Optional repository owner (e.g. 'kaiizer777').
        repo_name:   Optional repository name (e.g. 'UpGrade').
        **_kwargs:   Accepts and ignores session/staged_patches for signature consistency.

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

    effective_session_id = session_id or _kwargs.get("session_id")
    resolved_cwd, err = resolve_repo_dir(
        repo_name=repo_name,
        repo_owner=repo_owner,
        cwd=cwd,
        session_id=effective_session_id,
    )
    if err:
        return err
    current_cwd = resolved_cwd or os.path.realpath(os.getcwd())

    repo_boundary_root: str | None = None
    if repo_name and repo_name.strip():
        if cwd and cwd.strip():
            root_dir, _ = resolve_repo_dir(
                repo_name=repo_name,
                repo_owner=repo_owner,
                cwd=None,
                session_id=effective_session_id,
            )
            repo_boundary_root = os.path.realpath(root_dir) if root_dir else None
        elif resolved_cwd:
            repo_boundary_root = os.path.realpath(resolved_cwd)

    target_sync_root = repo_boundary_root or resolved_cwd
    if target_sync_root and _kwargs.get("staged_patches"):
        sync_staged_patches_to_repo(
            target_sync_root,
            _kwargs.get("staged_patches"),
            base_sha=_kwargs.get("base_sha"),
            session_id=effective_session_id,
        )

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
                    backend_cand = os.path.normpath(
                        os.path.join(current_cwd, "backend", target)
                    )
                    if os.path.isdir(backend_cand):
                        candidate = backend_cand

            if os.path.isdir(candidate):
                cand_real = os.path.realpath(candidate)
                if repo_boundary_root:
                    try:
                        inside = (
                            os.path.normcase(os.path.commonpath([repo_boundary_root, cand_real]))
                            == os.path.normcase(repo_boundary_root)
                        )
                    except ValueError:
                        inside = False
                    if not inside:
                        final_exit_code = 1
                        all_stderr.append(
                            f"cd: target directory {target!r} is outside repository root {repo_boundary_root!r}\n"
                        )
                        if op == "&&":
                            break
                        continue

                current_cwd = cand_real
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
    repo_owner: str | None = None,
    repo_name: str | None = None,
    session_id: str | None = None,
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
        repo_owner:  Optional repository owner (e.g. 'kaiizer777').
        repo_name:   Optional repository name (e.g. 'UpGrade').

    Returns:
        Formatted linter output or error string.
    """
    if not paths:
        return "Error: No paths provided to run_linter."

    effective_session_id = session_id or _kwargs.get("session_id")
    resolved_cwd, err = resolve_repo_dir(
        repo_name=repo_name,
        repo_owner=repo_owner,
        cwd=cwd,
        session_id=effective_session_id,
    )
    if err:
        return err
    effective_cwd = resolved_cwd or os.getcwd()

    repo_boundary_root: str | None = None
    if repo_name and repo_name.strip():
        root_dir, _ = resolve_repo_dir(
            repo_name=repo_name,
            repo_owner=repo_owner,
            cwd=None,
            session_id=effective_session_id,
        )
        repo_boundary_root = os.path.realpath(root_dir) if root_dir else None
    target_sync_root = repo_boundary_root or effective_cwd
    if target_sync_root and _kwargs.get("staged_patches"):
        sync_staged_patches_to_repo(
            target_sync_root,
            _kwargs.get("staged_patches"),
            base_sha=_kwargs.get("base_sha"),
            session_id=effective_session_id,
        )

    # Sanitize each path — no traversal allowed.
    cleaned: list[str] = []
    for p in paths:
        stripped = p.strip()
        if ".." in stripped or stripped.startswith("/"):
            return (
                f"Error: Path rejected — traversal or absolute path not allowed: {p!r}"
            )
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
        if (
            any(lower.endswith(e) for e in (".ts", ".tsx", ".js", ".jsx"))
            or ".spec." in lower
            or ".test." in lower
        ):
            return "vitest", ["npx", "vitest", "run"]

    # Default: pytest for .py and anything unknown.
    return "pytest", ["pytest", "-v"]


async def tool_run_targeted_tests(
    test_targets: list[str],
    timeout_sec: int = 120,
    queue: "SseQueue | None" = None,
    cwd: str | None = None,
    repo_owner: str | None = None,
    repo_name: str | None = None,
    session_id: str | None = None,
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
        repo_owner:  Optional repository owner (e.g. 'kaiizer777').
        repo_name:   Optional repository name (e.g. 'UpGrade').

    Returns:
        Formatted test output including failing tracebacks.
    """
    if not test_targets:
        return "Error: No test targets provided."

    effective_session_id = session_id or _kwargs.get("session_id")
    resolved_cwd, err = resolve_repo_dir(
        repo_name=repo_name,
        repo_owner=repo_owner,
        cwd=cwd,
        session_id=effective_session_id,
    )
    if err:
        return err
    effective_cwd = resolved_cwd or os.getcwd()

    repo_boundary_root: str | None = None
    if repo_name and repo_name.strip():
        root_dir, _ = resolve_repo_dir(
            repo_name=repo_name,
            repo_owner=repo_owner,
            cwd=None,
            session_id=effective_session_id,
        )
        repo_boundary_root = os.path.realpath(root_dir) if root_dir else None
    target_sync_root = repo_boundary_root or effective_cwd
    if target_sync_root and _kwargs.get("staged_patches"):
        sync_staged_patches_to_repo(
            target_sync_root,
            _kwargs.get("staged_patches"),
            base_sha=_kwargs.get("base_sha"),
            session_id=effective_session_id,
        )

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


# ---------------------------------------------------------------------------
# Tool: verify_in_ci_sandbox (future.md §4.3.1 — Phase 4.1)
# ---------------------------------------------------------------------------

_CI_MIN_TIMEOUT = 1
_CI_MAX_TIMEOUT = 600
_CI_DEFAULT_TIMEOUT = 180


def clamp_ci_timeout(timeout_sec: Any) -> int:
    """Clamp CI sandbox timeout to [1, 600]; fall back to 180 on bad input."""
    try:
        value = int(timeout_sec)
    except (TypeError, ValueError):
        return _CI_DEFAULT_TIMEOUT
    return max(_CI_MIN_TIMEOUT, min(_CI_MAX_TIMEOUT, value))


async def tool_verify_ci_sandbox(
    workflow_file: str | None = None,
    timeout_sec: int = _CI_DEFAULT_TIMEOUT,
    queue: "SseQueue | None" = None,
    session: Any | None = None,
    repo: Any | None = None,
    staged_patches: dict[str, str] | None = None,
    gh_token: str | None = None,
    repo_owner: str | None = None,
    repo_name: str | None = None,
    **_kwargs: Any,
) -> str:
    """
    Dispatch staged patches to the isolated GitHub Actions CI sandbox.

    Hybrid engine (SANDBOX_PROVIDER aware):
      - "local"          → fast local tool_run_targeted_tests on staged files.
      - "github_actions" → verify_session_patches (mirror repo + Actions poll).

    Args:
        workflow_file:  Optional workflow file to trigger (e.g. 'ci.yml').
                        Default auto-detects from file extensions.
        timeout_sec:    Max seconds to wait (default 180, max 600, clamped).
        queue:          SseQueue for sandbox_queued / terminal_output streaming.
        session:        AgentSession ORM (provides base_sha, staged_patches).
        repo:           Repo ORM (provides owner/name).
        staged_patches: dict of {path: unified_diff}. Falls back to
                        session.staged_patches when omitted.
        gh_token:       Optional GitHub installation token.
        repo_owner:     Optional repository owner (e.g. 'kaiizer777').
        repo_name:      Optional repository name (e.g. 'UpGrade').
        **_kwargs:      Accepts and ignores cwd and other orchestrator extras
                        for signature consistency.

    Returns:
        Formatted result with passed/failed, exit code, duration, and logs.
    """
    from app.config import settings

    timeout_sec = clamp_ci_timeout(timeout_sec)

    # Resolve staged patches — an explicit dict (even if empty) wins so an
    # empty orchestrator state correctly reports "no patches" instead of
    # silently falling back to a stale session object. Only when the caller
    # omits staged_patches (None) do we fall back to session.staged_patches.
    if staged_patches is not None:
        patches: dict[str, str] = dict(staged_patches)
    elif session is not None:
        try:
            patches = dict(getattr(session, "staged_patches", None) or {})
        except Exception:
            patches = {}
    else:
        patches = {}

    if not patches:
        return "Error: No staged patches to verify. Stage at least one patch first."

    # Defense-in-depth: validate staged patch paths before sorted()/combined
    # patch construction — skip invalid entries with a warning, never crash.
    from app.services.session_tools.recon import (
        _validate_file_path as _validate_ci_path,
    )

    _valid_patches: dict[str, str] = {}
    for _p, _d in patches.items():
        try:
            _validate_ci_path(_p)
        except Exception as exc:
            logger.warning(
                "tool_verify_ci_sandbox: skipping invalid patch path %r: %s", _p, exc
            )
            continue
        _valid_patches[_p] = _d
    patches = _valid_patches
    if not patches:
        return "Error: No staged patches to verify. Stage at least one patch first."

    # Normalise workflow label for display (runner auto-detects regardless).
    workflow_label = ((workflow_file or "").strip() or "auto")[:128]
    if workflow_file is not None:
        workflow_file = workflow_file.strip() or None

    provider = str(
        getattr(settings, "sandbox_provider", "github_actions") or "github_actions"
    )
    provider = provider.lower().strip()

    # ---- Local fast path: run staged files through the local test runner.
    if provider == "local":
        test_targets = sorted(patches.keys())
        if queue is not None:
            try:
                await queue.put_sandbox_queued(
                    run_url="local", workflow_name=workflow_label
                )
            except Exception as exc:  # pragma: no cover
                logger.warning("tool_verify_ci_sandbox: SSE emit failed: %s", exc)
            try:
                await queue.put_terminal_output(
                    f"[ci-sandbox] Local provider: running {len(test_targets)} "
                    f"target(s) via tool_run_targeted_tests "
                    f"(timeout={timeout_sec}s)...\n",
                    stream="stdout",
                )
            except Exception as exc:  # pragma: no cover
                logger.warning("tool_verify_ci_sandbox: SSE emit failed: %s", exc)

        effective_owner = (
            repo_owner
            or getattr(repo, "owner", None)
            or getattr(session, "repo_owner", None)
            or _kwargs.get("repo_owner")
        )
        effective_name = (
            repo_name
            or getattr(repo, "name", None)
            or getattr(session, "repo_name", None)
            or _kwargs.get("repo_name")
        )

        # NOTE: effective local cap is 300 via tool_run_targeted_tests (pre-existing); 600s timeout is clamped downstream.
        local_result = await tool_run_targeted_tests(
            test_targets=test_targets,
            timeout_sec=timeout_sec,
            queue=queue,
            repo_owner=effective_owner,
            repo_name=effective_name,
        )
        return (
            f"CI sandbox verification (provider=local, workflow={workflow_label}):\n"
            f"{local_result}"
        )

    # ---- GitHub Actions path: bridge through verify_session_patches.
    if session is None or repo is None:
        return (
            "Error: Session and repo context are required for CI sandbox "
            "verification (provider=github_actions)."
        )

    from app.subagents.sandbox_verifier import verify_session_patches

    # Dispatcher-level start events (spec §4.3.1): sandbox_queued +
    # terminal_output. The verifier itself emits further polling progress;
    # emitting here guarantees the frontend sees dispatch immediately even
    # when the verifier is mocked in tests.
    if queue is not None:
        try:
            await queue.put_sandbox_queued(
                run_url="pending", workflow_name=workflow_label
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("tool_verify_ci_sandbox: SSE emit failed: %s", exc)
        try:
            await queue.put_terminal_output(
                f"[ci-sandbox] Dispatching {len(patches)} file(s) "
                f"(workflow={workflow_label}, timeout={timeout_sec}s)...\n",
                stream="stdout",
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("tool_verify_ci_sandbox: SSE emit failed: %s", exc)

    t_start = time.monotonic()
    result = await verify_session_patches(
        session=session,
        repo=repo,
        staged_patches=patches,
        gh_token=gh_token,
        queue=queue,
        workflow_file=workflow_file,
        timeout_sec=timeout_sec,
    )
    duration = time.monotonic() - t_start

    passed = bool(result.get("passed", False))
    status = str(result.get("status", "passed" if passed else "failed"))
    run_url = result.get("run_url") or "(none)"
    raw_logs = result.get("logs") or ""
    logs = raw_logs.strip() if isinstance(raw_logs, str) else str(raw_logs).strip()
    logs = logs or "(empty)"
    # Cap logs in the LLM tool response so a huge CI tail cannot blow context.
    if len(logs) > 6000:
        logs = logs[-6000:]
    verdict = "PASSED" if passed else "FAILED"
    exit_code = 0 if passed else 1

    if queue is not None:
        try:
            await queue.put_sandbox_progress(step_name="complete", status="completed")
        except Exception as exc:  # pragma: no cover
            logger.warning("tool_verify_ci_sandbox: SSE emit failed: %s", exc)
        try:
            await queue.put_sandbox_status(
                status="passed" if passed else "failed",
                logs=logs[:2000],
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("tool_verify_ci_sandbox: SSE emit failed: %s", exc)

    return (
        f"CI sandbox verification: {verdict}\n"
        f"Workflow: {workflow_label}\n"
        f"Status: {status}\n"
        f"Exit code: {exit_code} (Duration: {duration:.2f}s)\n"
        f"Run URL: {run_url}\n"
        f"LOGS:\n{logs}"
    )
