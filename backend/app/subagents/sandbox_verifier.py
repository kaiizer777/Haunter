"""
Sandbox Verifier for Cloud Agentic Live Session — Phase 2.

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
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any, Optional

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.models import AgentSession, Repo


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

    Returns:
        dict with keys: status, passed, run_url, logs.
        Never raises — errors are captured and returned as status="failed".
    """
    from app.sandbox import SandboxInput
    from app.sandbox import _load_github_actions_runner

    combined_patch = _build_combined_patch(staged_patches)
    repo_ref = f"{repo.owner}/{repo.name}"
    run_id = uuid.uuid4()

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
        )
    except Exception as exc:
        logger.error(
            "sandbox_verifier: SandboxInput validation failed for session=%s: %s",
            session.id, exc,
        )
        return {
            "status": "failed",
            "passed": False,
            "run_url": None,
            "logs": f"Patch validation failed: {exc}",
        }

    # Invoke the runner.
    try:
        runner_cls = _load_github_actions_runner()
        runner = runner_cls()
        result = await runner.verify(sandbox_input)
    except Exception as exc:
        logger.error(
            "sandbox_verifier: runner.verify() failed for session=%s: %s",
            session.id, exc,
        )
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
        session.id, passed, run_url,
    )

    return {
        "status": status_str,
        "passed": passed,
        "run_url": run_url,
        "logs": reason,
    }
