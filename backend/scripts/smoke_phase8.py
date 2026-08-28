"""
Phase 8 End-to-End Smoke Script.

Exercises the full pipeline:
  webhook -> handle_failed_run -> context_gatherer -> fix_generator
  -> verifier -> pr_writer -> PR opened   (or)
  -> fallback_commented

Usage:
    uv run python scripts/smoke_phase8.py [--repo owner/name] [--run-id UUID]

Modes:
  - LIVE:  GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY + install_id in DB -> real GitHub API calls.
  - MOCK:  env not configured -> all GitHub + LLM calls stubbed, real DB writes.

Exit codes:
  0  -- terminal status reached (pr_opened or fallback_commented)
  1  -- pipeline errored or unexpected status
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

# ---------------------------------------------------------------------------
# Bootstrap path so imports resolve correctly when run from backend/scripts/
# ---------------------------------------------------------------------------

_BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND_DIR))

# ---------------------------------------------------------------------------
# Configure logging before any app imports
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("smoke_phase8")

# Suppress noisy library loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Constants -- never change GITHUB_APP_* in this script; read from env only.
# ---------------------------------------------------------------------------

_MOCK_INSTALL_TOKEN = "ghs_SMOKE_MOCK_INSTALLATION_TOKEN"
_MOCK_PATCH = (
    "--- a/app/models.py\n"
    "+++ b/app/models.py\n"
    "@@ -10,1 +10,1 @@\n"
    "-import foo\n"
    "+import bar\n"
)
_MOCK_DIAGNOSIS = (
    "ImportError: cannot import name 'foo' from 'app.models' (app/models.py:10). "
    "The module 'foo' was removed from requirements.txt in the failing commit."
)
_MOCK_FIX_JSON = (
    '{"patch": "--- a/app/models.py\\n+++ b/app/models.py\\n@@ -10,1 +10,1 @@\\n-import foo\\n+import bar\\n"'
    ', "confidence": 87, "strategy_notes": "Replace bad import with bar"}'
)
_MOCK_PR_JSON = (
    '{"title": "fix: replace missing import in app/models.py"'
    ', "body": "Root cause: foo module removed from requirements.\\nFix: replaced import with bar."}'
)


def _is_live_mode() -> bool:
    """True when GitHub App credentials are available in env."""
    return bool(os.environ.get("GITHUB_APP_ID") and os.environ.get("GITHUB_APP_PRIVATE_KEY"))


async def _get_or_create_smoke_run(
    repo_owner: str,
    repo_name: str,
    run_id_arg: uuid.UUID | None,
) -> tuple[Any, Any, uuid.UUID]:
    """
    Find existing run by run_id_arg, OR create a smoke run + repo + user if not found.

    Returns (run, repo, run.id).
    """
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.db import async_session_maker
    from app.models import Repo, Run, User

    async with async_session_maker() as db:
        # --- Attempt to find existing run ---
        if run_id_arg is not None:
            result = await db.execute(select(Run).where(Run.id == run_id_arg))
            run = result.scalar_one_or_none()
            if run is None:
                logger.error("Run %s not found in DB.", run_id_arg)
                sys.exit(1)

            repo_result = await db.execute(
                select(Repo)
                .where(Repo.id == run.repo_id)
                .options(selectinload(Repo.user))
            )
            repo = repo_result.scalar_one_or_none()
            if repo is None:
                logger.error("Repo for run %s not found in DB.", run_id_arg)
                sys.exit(1)

            logger.info(
                "Found existing run=%s repo=%s/%s status=%s",
                run.id, repo.owner, repo.name, run.status,
            )
            return run, repo, run.id

        # --- Create smoke user + repo + run (single commit, flush to get IDs) ---
        user = User(
            github_id=int(uuid.uuid4().int % 1_000_000_000 + 100_000_000),
            github_username="smoke-test-user",
            access_token=None,
        )
        db.add(user)
        await db.flush()
        await db.refresh(user)

        unique_suffix = uuid.uuid4().hex[:6]
        repo = Repo(
            user_id=user.id,
            owner=repo_owner,
            name=f"{repo_name}-{unique_suffix}",
            default_branch="main",
            github_install_id=None,
        )
        if os.environ.get("GITHUB_INSTALL_ID"):
            try:
                repo.github_install_id = int(os.environ["GITHUB_INSTALL_ID"])
            except ValueError:
                logger.warning("GITHUB_INSTALL_ID is not a valid integer -- ignoring.")

        db.add(repo)
        await db.flush()
        await db.refresh(repo)

        fake_sha = "a" * 40
        run = Run(
            repo_id=repo.id,
            github_run_id=int(uuid.uuid4().int % 9_000_000_000 + 1_000_000_000),
            github_delivery_id=str(uuid.uuid4()),
            head_sha=fake_sha,
            head_branch="main",
            status="pending",
            conclusion="failure",
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        await db.refresh(repo)

        logger.info(
            "Created smoke run=%s repo=%s/%s (status=pending)",
            run.id, repo.owner, repo.name,
        )
        return run, repo, run.id


def _build_happy_path_patches() -> list:
    """
    Stubs for happy-path mock: verifier passes on first attempt -> pr_opened.

    Patches ALL import paths for LLM: context_gatherer, fix_generator, pr_writer
    each use their own LLMClient import, so each must be mocked independently.
    """
    _gather_response = {
        "content": _MOCK_DIAGNOSIS,
        "usage": {"input_tokens": 100, "output_tokens": 30},
        "latency_ms": 150,
        "model": "nemotron-3.5-lightning-free",
    }
    _fix_response = {
        "content": _MOCK_FIX_JSON,
        "usage": {"input_tokens": 200, "output_tokens": 80},
        "latency_ms": 350,
        "model": "nemotron-3.5-lightning-free",
    }
    _pr_response = {
        "content": _MOCK_PR_JSON,
        "usage": {"input_tokens": 120, "output_tokens": 50},
        "latency_ms": 220,
        "model": "nemotron-3.5-lightning-free",
    }

    return [
        patch("app.subagents.context_gatherer.gh.fetch_workflow_run_logs",
              new_callable=AsyncMock,
              return_value="2024-01-01T00:00:00Z ImportError: No module named 'foo'"),
        patch("app.subagents.context_gatherer.gh.fetch_diff",
              new_callable=AsyncMock,
              return_value="-import foo\n"),
        patch("app.subagents.context_gatherer.gh.fetch_commit_metadata",
              new_callable=AsyncMock,
              return_value={"sha": "a" * 40, "commit": {"message": "remove foo"}}),
        patch("app.subagents.context_gatherer.LLMClient.complete",
              new_callable=AsyncMock,
              side_effect=[_gather_response, _fix_response, _pr_response]),
        patch("app.sandbox.verifier.verify_patch",
              new_callable=AsyncMock,
              return_value={"status": "pass", "failure_reason": None, "build_duration_ms": 1234}),
        patch("app.github.pr.get_installation_token",
              new_callable=AsyncMock,
              return_value=_MOCK_INSTALL_TOKEN),
        patch("app.github.pr.create_branch", new_callable=AsyncMock),
        patch("app.github.pr.commit_patch", new_callable=AsyncMock, return_value="b" * 40),
        patch("app.github.pr.open_pr",
              new_callable=AsyncMock,
              return_value={
                  "html_url": "https://github.com/smoke-org/smoke-repo/pull/42",
                  "number": 42,
              }),
        patch("app.github_client.post_commit_comment", new_callable=AsyncMock),
    ]


def _build_fallback_patches() -> list:
    """Stubs for forced-fallback mock: verifier always fails -> fallback_commented."""
    _gather_response = {
        "content": _MOCK_DIAGNOSIS,
        "usage": {"input_tokens": 100, "output_tokens": 30},
        "latency_ms": 150,
        "model": "nemotron-3.5-lightning-free",
    }
    _fix_response = {
        "content": _MOCK_FIX_JSON,
        "usage": {"input_tokens": 200, "output_tokens": 80},
        "latency_ms": 350,
        "model": "nemotron-3.5-lightning-free",
    }

    return [
        patch("app.subagents.context_gatherer.gh.fetch_workflow_run_logs",
              new_callable=AsyncMock,
              return_value="ImportError: No module named 'foo'"),
        patch("app.subagents.context_gatherer.gh.fetch_diff",
              new_callable=AsyncMock, return_value="-import foo\n"),
        patch("app.subagents.context_gatherer.gh.fetch_commit_metadata",
              new_callable=AsyncMock, return_value={"sha": "a" * 40}),
        patch("app.subagents.context_gatherer.LLMClient.complete",
              new_callable=AsyncMock, side_effect=[_gather_response, _fix_response, _fix_response, _fix_response]),
        patch("app.sandbox.verifier.verify_patch",
              new_callable=AsyncMock,
              return_value={"status": "fail", "failure_reason": "smoke forced fail", "build_duration_ms": 200}),
        patch("app.github.pr.get_installation_token",
              new_callable=AsyncMock, return_value=_MOCK_INSTALL_TOKEN),
        patch("app.github_client.post_commit_comment", new_callable=AsyncMock),
    ]


async def run_smoke(
    repo_owner: str,
    repo_name: str,
    run_id_arg: uuid.UUID | None,
    force_fallback: bool,
) -> int:
    """Main smoke driver. Returns exit code."""
    from sqlalchemy import select

    from app.db import async_session_maker
    from app.models import Run
    from app.orchestrator import handle_failed_run

    live = _is_live_mode()

    print()
    print("=" * 64)
    print("  Haunter Phase 8 -- Smoke Test")
    print("=" * 64)
    print(f"  Mode:   {'LIVE (real GitHub API)' if live else 'MOCK (stubbed GitHub + LLM)'}")
    print(f"  Repo:   {repo_owner}/{repo_name}")
    if force_fallback:
        print("  Path:   FORCED FALLBACK (verifier always fails)")
    print()

    if not live:
        print(
            "  NOTE: live smoke requires GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY +\n"
            "        GITHUB_INSTALL_ID set in env. Running mock smoke instead.\n"
        )

    run, repo, run_id = await _get_or_create_smoke_run(repo_owner, repo_name, run_id_arg)

    if live:
        logger.info("LIVE MODE: invoking handle_failed_run with real credentials.")
        await handle_failed_run(run_id)
    else:
        patches = _build_fallback_patches() if force_fallback else _build_happy_path_patches()
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            await handle_failed_run(run_id)

    async with async_session_maker() as db:
        result = await db.execute(select(Run).where(Run.id == run_id))
        final_run = result.scalar_one_or_none()

    if final_run is None:
        print("\n  FAIL  Run not found after pipeline. Something went very wrong.\n")
        return 1

    status = final_run.status
    print("=" * 64)
    print(f"  Final status:  {status}")

    if status == "pr_opened":
        print(f"  PR URL:        {final_run.pr_url}")
        print(f"  PR number:     #{final_run.pr_number}")
        print(f"  PR branch:     {final_run.pr_branch}")
        print()
        if live:
            print("  LIVE SMOKE PASSED -- real PR opened on GitHub.")
        else:
            print("  MOCK SMOKE PASSED -- pr_opened state reached (mock mode).")
            print("  Set GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY + GITHUB_INSTALL_ID for live smoke.")

    elif status == "fallback_commented":
        print()
        if force_fallback:
            print("  MOCK SMOKE PASSED (fallback path) -- fallback_commented state reached.")
        else:
            print("  SMOKE PASSED -- fallback_commented state reached.")
        if not live:
            print("  Set GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY + GITHUB_INSTALL_ID for live smoke.")

    elif status == "error":
        print()
        print("  FAIL  Pipeline reached error state. Check logs above for root cause.")
        return 1

    else:
        print()
        print(f"  UNEXPECTED terminal status: {status!r}")
        return 1

    print("=" * 64)
    print()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Haunter Phase 8 smoke test -- full pipeline end-to-end.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--repo",
        default="smoke-org/smoke-repo",
        help="Repository in owner/name format (default: smoke-org/smoke-repo)",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Existing run UUID to re-drive (default: create a fresh smoke run)",
    )
    parser.add_argument(
        "--fallback",
        action="store_true",
        help="Force verifier to always fail -- exercises the fallback_commented path.",
    )
    args = parser.parse_args()

    if "/" not in args.repo:
        parser.error("--repo must be in owner/name format, e.g. 'my-org/my-repo'")

    owner, name = args.repo.split("/", 1)

    run_id_arg: uuid.UUID | None = None
    if args.run_id:
        try:
            run_id_arg = uuid.UUID(args.run_id)
        except ValueError:
            parser.error(f"--run-id {args.run_id!r} is not a valid UUID")

    exit_code = asyncio.run(
        run_smoke(
            repo_owner=owner,
            repo_name=name,
            run_id_arg=run_id_arg,
            force_fallback=args.fallback,
        )
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
