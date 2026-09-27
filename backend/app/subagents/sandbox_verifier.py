"""
Sandbox Verifier for Cloud Agentic Live Session — Phase 2 + Phase 4.1.

Dispatches staged session patches to the GitHub Actions sandbox runner,
translating the session's staged_patches dict into a SandboxInput.

The existing sandbox pipeline works through Attempt/Run ORM objects.
This module adapts the session's patch state (a dict of path -> unified diff)
into the SandboxInput format that the GitHubActionsSandboxRunner understands,
using a single concatenated patch string.

Design:
  - Concatenates all staged patches with separator headers into a single
    unified patch string so the sandbox can apply them atomically.
  - Uses SandboxInput validation to reject oversized patches before dispatch.
  - Returns a dict compatible with SandboxVerificationOut:
      {"status": str, "passed": bool, "run_url": Optional[str], "logs": Optional[str]}
  - Never raises — all errors are returned as status="failed" with logs.
  - Phase 4.1: optional SseQueue streaming — emits sandbox_queued /
    sandbox_progress / terminal_output / sandbox_status events around the
    runner call so the agent and frontend observe live CI progress.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any, Optional

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.models import AgentSession, Repo
    from app.services.session_streamer import SseQueue

# Phase 4.1 — timeout clamp for session CI verification (spec §4.3.1).
_VERIFY_MIN_TIMEOUT_SEC = 1
_VERIFY_MAX_TIMEOUT_SEC = 600
_VERIFY_DEFAULT_TIMEOUT_SEC = 180


def clamp_verify_timeout(timeout_sec: Any) -> int:
    """Clamp a caller-supplied timeout to [1, 600], falling back to 180."""
    try:
        value = int(timeout_sec)
    except (TypeError, ValueError):
        return _VERIFY_DEFAULT_TIMEOUT_SEC
    return max(_VERIFY_MIN_TIMEOUT_SEC, min(_VERIFY_MAX_TIMEOUT_SEC, value))


def _build_combined_patch(staged_patches: dict[str, str]) -> str:
    """
    Concatenate staged patches into a single unified diff string.

    Each file's diff is separated by a header comment so the sandbox
    runner can identify patch boundaries in logs.
    """
    parts: list[str] = []
    for path, diff in staged_patches.items():
        # Ensure each patch ends with a newline before the next separator.
        normalised = diff.rstrip("\n") + "\n"
        parts.append(f"# === patch: {path} ===\n{normalised}")
    return "\n".join(parts)


async def verify_session_patches(
    session: "AgentSession",
    repo: "Repo",
    staged_patches: dict[str, str],
    gh_token: Optional[str] = None,
    queue: "SseQueue | None" = None,
    workflow_file: Optional[str] = None,
    timeout_sec: int = _VERIFY_DEFAULT_TIMEOUT_SEC,
) -> dict[str, Any]:
    """
    Dispatch all staged patches from a session to the sandbox runner.

    Builds a SandboxInput from the session's staged_patches, invokes
    GitHubActionsSandboxRunner.verify(), and maps the SandboxResult to
    the shape expected by SandboxVerificationOut.

    Args:
        session:        AgentSession ORM object (provides base_sha, user_id).
        repo:           Repo ORM object (provides owner/name, github_install_id).
        staged_patches: dict of {file_path: unified_diff}.
        gh_token:       Optional GitHub App installation token.
        queue:          Optional SseQueue for live CI progress streaming.
                        Emits sandbox_queued, sandbox_progress, terminal_output,
                        and sandbox_status events around the runner call.
        workflow_file:  Optional workflow file override (e.g. 'ci.yml').
                        Recorded in streamed events; the runner still
                        auto-detects the py/ts template from file extensions.
        timeout_sec:    Max seconds to wait for the runner (clamped to
                        [1, 600]). Enforced via asyncio.wait_for around
                        runner.verify().

    Returns:
        dict with keys: status, passed, run_url, logs.
        Never raises — errors are captured and returned as status="failed".
    """
    from app.sandbox import SandboxInput
    from app.sandbox import _load_github_actions_runner

    timeout_sec = clamp_verify_timeout(timeout_sec)
    # NOTE: gh_token is retained for backward-compat with earlier callers but
    # intentionally unused — the sandbox runner authenticates via the GitHub
    # App installation token internally (see _load_github_actions_runner).
    _ = gh_token
    workflow_label = ((workflow_file or "").strip() or "auto")[:128]

    if not staged_patches:
        return {
            "status": "failed",
            "passed": False,
            "run_url": None,
            "logs": "No staged patches to verify. Stage at least one patch first.",
        }

    combined_patch = _build_combined_patch(staged_patches)
    repo_ref = f"{repo.owner}/{repo.name}"
    run_id = uuid.uuid4()

    async def _emit(coro: Any) -> None:
        # Best-effort SSE emission — streaming must never fail verification.
        try:
            await coro
        except Exception as exc:  # pragma: no cover
            logger.warning("sandbox_verifier: SSE emit failed: %s", exc)

    if queue is not None:
        await _emit(
            queue.put_sandbox_queued(run_url="pending", workflow_name=workflow_label)
        )
        await _emit(
            queue.put_sandbox_progress(step_name="dispatch", status="in_progress")
        )
        await _emit(queue.put_sandbox_status(status="queued", logs="CI sandbox queued"))
        await _emit(
            queue.put_terminal_output(
                f"[ci-sandbox] Dispatching {len(staged_patches)} file(s) "
                f"to GitHub Actions mirror (workflow={workflow_label}, "
                f"timeout={timeout_sec}s)...\n",
                stream="stdout",
            )
        )

    # Validate inputs before touching any external API.
    try:
        sandbox_input = SandboxInput(
            patch=combined_patch,
            repo_ref=repo_ref,
            run_id=run_id,
            base_sha=session.base_sha,
            file_paths=list(staged_patches.keys()),
            # user_github_id used by the runner to name the test-mirror repo.
            user_github_id=getattr(session, "user_github_id", None),
            head_sha=session.base_sha,
            # Sessions have no Attempt row — the GitHub Actions runner
            # requires attempt_number for ephemeral branch naming, so every
            # session verification uses attempt 1 as its own isolated run.
            attempt_number=1,
        )
    except Exception as exc:
        logger.error(
            "sandbox_verifier: SandboxInput validation failed for session=%s: %s",
            session.id,
            exc,
        )
        if queue is not None:
            await _emit(
                queue.put_terminal_output(
                    f"[ci-sandbox] Patch validation failed: {exc}\n",
                    stream="stderr",
                )
            )
            await _emit(queue.put_sandbox_status(status="failed", logs=str(exc)[:500]))
        return {
            "status": "failed",
            "passed": False,
            "run_url": None,
            "logs": f"Patch validation failed: {exc}",
        }

    if queue is not None:
        await _emit(
            queue.put_sandbox_progress(step_name="polling", status="in_progress")
        )
        await _emit(
            queue.put_terminal_output(
                "[ci-sandbox] Patch accepted. Polling GitHub Actions for completion...\n",
                stream="stdout",
            )
        )

    # Invoke the runner with a hard client-side timeout.
    t_start = time.monotonic()
    try:
        runner_cls = _load_github_actions_runner()
        runner = runner_cls()
        result = await asyncio.wait_for(
            runner.verify(sandbox_input), timeout=timeout_sec
        )
    except asyncio.TimeoutError:
        duration_s = time.monotonic() - t_start
        logger.error(
            "sandbox_verifier: runner.verify() timed out after %ds for session=%s",
            timeout_sec,
            session.id,
        )
        if queue is not None:
            await _emit(
                queue.put_terminal_output(
                    f"[ci-sandbox] Timed out after {timeout_sec}s waiting for CI.\n",
                    stream="stderr",
                )
            )
            await _emit(
                queue.put_sandbox_status(status="failed", logs="CI sandbox timed out")
            )
        return {
            "status": "failed",
            "passed": False,
            "run_url": None,
            "logs": (
                f"CI sandbox verification timed out after {timeout_sec}s "
                f"(waited {duration_s:.1f}s)."
            ),
        }
    except Exception as exc:
        logger.error(
            "sandbox_verifier: runner.verify() failed for session=%s: %s",
            session.id,
            exc,
        )
        if queue is not None:
            await _emit(
                queue.put_terminal_output(
                    f"[ci-sandbox] Runner error: {exc}\n",
                    stream="stderr",
                )
            )
            await _emit(queue.put_sandbox_status(status="failed", logs=str(exc)[:500]))
        return {
            "status": "failed",
            "passed": False,
            "run_url": None,
            "logs": f"Sandbox runner error: {exc}",
        }

    # Map SandboxResult to the verification response shape.
    passed: bool = bool(result.get("passed", False))
    reason: Optional[str] = result.get("reason")
    run_url: Optional[str] = result.get("run_url")

    status_str = "passed" if passed else "failed"

    logger.info(
        "sandbox_verifier: session=%s verify complete — passed=%s run_url=%s",
        session.id,
        passed,
        run_url,
    )

    if queue is not None:
        await _emit(
            queue.put_sandbox_progress(step_name="complete", status="completed")
        )
        log_tail = (reason or "").strip()
        if log_tail:
            # Cap streamed tail so a huge CI log cannot blow the SSE buffer.
            tail = log_tail[-3500:]
            await _emit(
                queue.put_terminal_output(
                    f"[ci-sandbox] CI {status_str} "
                    f"(run: {run_url or 'n/a'}).\n{tail}\n",
                    stream="stdout" if passed else "stderr",
                )
            )
        else:
            await _emit(
                queue.put_terminal_output(
                    f"[ci-sandbox] CI {status_str} (run: {run_url or 'n/a'}).\n",
                    stream="stdout" if passed else "stderr",
                )
            )
        await _emit(
            queue.put_sandbox_queued(
                run_url=run_url or "unknown",
                workflow_name=workflow_label,
            )
        )
        await _emit(
            queue.put_sandbox_status(
                status="passed" if passed else "failed",
                logs=(reason or "")[:2000],
            )
        )

    return {
        "status": status_str,
        "passed": passed,
        "run_url": run_url,
        "logs": reason,
    }
