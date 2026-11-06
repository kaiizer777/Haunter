"""
Issue #276 — PR-apply dry-run + one LLM retry (test_pr_dry_run_retry_276.py).

Covers:
1. Fix-generator prompt contains the verbatim (byte-exact) diff rule and the
   _build_messages alternation invariant still holds.
2. The fail-closed applier still returns None for a paraphrased '-' line.
3. dry_run_patch_at_sha surfaces file+hunk+expected-vs-actual on mismatch and
   performs only GET requests (read-only: no blob/tree/commit/ref calls).
4. dry_run_patch_at_sha returns None for clean, new-file (404), and
   deletion-of-missing patches.
5. _describe_apply_mismatch reports end-of-file when the hunk runs past EOF.
6. Pipeline integration: first dry-run mismatch -> exactly one generate_fix
   regen with the mismatch as feedback -> proceeds to create_branch /
   commit_patch with the corrected patch (status pr_opened).
7. Pipeline integration: second dry-run mismatch -> error path, and
   create_branch / commit_patch / open_pr are never called (no orphan branch,
   no blob/tree/commit HTTP writes).
"""

from __future__ import annotations

import base64
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.github.pr import (
    _apply_unified_diff_to_content,
    _describe_apply_mismatch,
    dry_run_patch_at_sha,
)
from app.models import Attempt, Repo, Run, User
from app.orchestrator import handle_failed_run
from app.subagents.fix_generator import _build_messages
from tests.conftest import truncate_all


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


FILE_CONTENT = "line1\nline2\nline3\n"

PARAPHRASED_PATCH = (
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,3 +1,3 @@\n"
    " line1\n"
    "-PARAPHRASED line two\n"
    "+LINE2\n"
    " line3\n"
)

CLEAN_PATCH = (
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,3 +1,3 @@\n"
    " line1\n"
    "-line2\n"
    "+LINE2\n"
    " line3\n"
)


async def _create_test_user(db: AsyncSession, username: str = "dryrun-user") -> User:
    user = User(
        github_id=int(uuid.uuid4().int % 1_000_000_000 + 100_000_000),
        github_username=username,
        access_token="fake_dryrun_token_123",
    )
    db.add(user)
    await db.commit()
    return user


async def _create_test_repo(
    db: AsyncSession, user: User, name: str = "dryrun-repo"
) -> Repo:
    repo = Repo(
        user_id=user.id,
        owner="dryrun-org",
        name=name,
        default_branch="main",
    )
    db.add(repo)
    await db.commit()
    return repo


async def _create_test_run(
    db: AsyncSession,
    repo: Repo,
    status: str = "pending",
) -> Run:
    run = Run(
        repo_id=repo.id,
        github_run_id=int(uuid.uuid4().int % 1_000_000_000),
        github_delivery_id=str(uuid.uuid4()),
        head_sha="0123456789abcdef0123456789abcdef01234567",
        head_branch="main",
        status=status,
        conclusion="failure",
        diagnosis_summary="ImportError: no module named 'utils'",
    )
    db.add(run)
    await db.commit()
    return run


# ---------------------------------------------------------------------------
# 1. Prompt contains the verbatim rule; alternation invariant holds
# ---------------------------------------------------------------------------


def test_fix_prompt_requires_verbatim_diff_rule() -> None:
    """The system prompt must constrain -/context lines to byte-exact quotes."""
    combos = [
        {"prior_attempt": None},
        {
            "prior_attempt": SimpleNamespace(
                attempt_number=1, patch_text="", failure_reason=None
            )
        },
        {"prior_attempt": None, "validation_error_context": "bad format"},
    ]
    for kwargs in combos:
        messages = _build_messages(
            diagnosis_summary="ImportError: cannot import foo in app/models.py:10",
            **kwargs,  # type: ignore[arg-type]
        )
        assert messages[0]["role"] == "system"
        system_prompt = messages[0]["content"]
        assert "byte-exact" in system_prompt
        assert "paraphrase" in system_prompt
        # Alternation invariant: no two consecutive same-role messages.
        for i in range(1, len(messages)):
            assert messages[i]["role"] != messages[i - 1]["role"]


# ---------------------------------------------------------------------------
# 2. Fail-closed applier still rejects paraphrased deletions with None
# ---------------------------------------------------------------------------


def test_apply_rejects_paraphrased_deletion_line() -> None:
    """A paraphrased '-' line must NOT apply (returns None, fail-closed)."""
    assert _apply_unified_diff_to_content(FILE_CONTENT, PARAPHRASED_PATCH) is None
    assert _apply_unified_diff_to_content(FILE_CONTENT, CLEAN_PATCH) is not None


# ---------------------------------------------------------------------------
# 3-5. dry_run_patch_at_sha behaviour (respx-mocked GitHub API)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_dry_run_reports_file_hunk_expected_actual() -> None:
    """Mismatch surfaces file + hunk header + expected-vs-actual, GETs only."""
    head_sha = "a" * 40
    with respx.mock(
        base_url="https://api.github.com", assert_all_called=False
    ) as rx:
        contents_route = rx.get(
            f"/repos/dryrun-org/dryrun-repo/contents/app.py?ref={head_sha}"
        ).mock(return_value=httpx.Response(200, json={"content": _b64(FILE_CONTENT)}))

        mismatch = await dry_run_patch_at_sha(
            owner="dryrun-org",
            repo="dryrun-repo",
            patch_text=PARAPHRASED_PATCH,
            base_sha=head_sha,
            token="fake-token",
        )

        http_calls_inside = list(rx.calls)

    assert mismatch is not None
    assert "app.py" in mismatch
    assert "@@ -1,3 +1,3 @@" in mismatch
    assert "PARAPHRASED line two" in mismatch
    assert "line2" in mismatch
    assert contents_route.called
    # Read-only: only the Contents GET, no blob/tree/commit/ref writes.
    assert http_calls_inside, "expected at least the Contents GET"
    assert {c.request.method for c in http_calls_inside} == {"GET"}


@pytest.mark.anyio
async def test_dry_run_clean_patch_returns_none() -> None:
    """An exactly-quoted patch dry-runs clean (None) with GETs only."""
    head_sha = "b" * 40
    with respx.mock(
        base_url="https://api.github.com", assert_all_called=False
    ) as rx:
        contents_route = rx.get(
            f"/repos/dryrun-org/dryrun-repo/contents/app.py?ref={head_sha}"
        ).mock(return_value=httpx.Response(200, json={"content": _b64(FILE_CONTENT)}))

        mismatch = await dry_run_patch_at_sha(
            owner="dryrun-org",
            repo="dryrun-repo",
            patch_text=CLEAN_PATCH,
            base_sha=head_sha,
            token="fake-token",
        )

        http_calls_inside = list(rx.calls)

    assert mismatch is None
    assert contents_route.called
    assert {c.request.method for c in http_calls_inside} == {"GET"}


@pytest.mark.anyio
async def test_dry_run_new_file_and_missing_deletion_target() -> None:
    """New-file patch with 404 contents applies; deletion of missing skips."""
    head_sha = "c" * 40
    new_file_patch = "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+hello\n"
    delete_missing_patch = (
        "--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-whatever\n"
    )
    with respx.mock(
        base_url="https://api.github.com", assert_all_called=False
    ) as rx:
        rx.get(f"/repos/dryrun-org/dryrun-repo/contents/new.py?ref={head_sha}").mock(
            return_value=httpx.Response(404)
        )
        rx.get(f"/repos/dryrun-org/dryrun-repo/contents/gone.py?ref={head_sha}").mock(
            return_value=httpx.Response(404)
        )

        new_result = await dry_run_patch_at_sha(
            owner="dryrun-org",
            repo="dryrun-repo",
            patch_text=new_file_patch,
            base_sha=head_sha,
            token="fake-token",
        )
        delete_result = await dry_run_patch_at_sha(
            owner="dryrun-org",
            repo="dryrun-repo",
            patch_text=delete_missing_patch,
            base_sha=head_sha,
            token="fake-token",
        )

        http_calls_inside = list(rx.calls)

    assert new_result is None
    assert delete_result is None
    assert {c.request.method for c in http_calls_inside} == {"GET"}


def test_describe_mismatch_past_eof_reports_end_of_file() -> None:
    """A hunk reaching past EOF reports the file ending, not a wrong line."""
    short = "only-one-line\n"
    patch = "--- a/f\n+++ b/f\n@@ -1,3 +1,3 @@\n only-one-line\n-second\n-third\n"
    assert _apply_unified_diff_to_content(short, patch) is None
    report = _describe_apply_mismatch(short, patch, "f")
    assert "f" in report
    assert "@@ -1,3 +1,3 @@" in report
    assert "ends" in report


# ---------------------------------------------------------------------------
# 6-7. Pipeline integration: one regen, then proceed or error
# ---------------------------------------------------------------------------


def _mock_generate_fix_with(
    specs: list[dict[str, Any]], call_log: list[dict[str, Any]]
):  # type: ignore[no-untyped-def]
    async def _mock(run, diagnosis_summary, prior_attempt, db, review_feedback=None):  # type: ignore[no-untyped-def]
        idx = len(call_log)
        spec = specs[idx]
        call_log.append(
            {"prior_attempt": prior_attempt, "review_feedback": review_feedback}
        )
        attempt = Attempt(
            id=uuid.uuid4(),
            run_id=run.id,
            attempt_number=spec["number"],
            patch_text=spec["patch"],
            confidence_score=95,
            strategy_notes="dry-run regen spec",
        )
        db.add(attempt)
        await db.commit()
        return attempt

    return _mock


@pytest.mark.anyio
async def test_pipeline_pr_dry_run_mismatch_regenerates_once_then_proceeds(
    db: AsyncSession,
) -> None:
    """First dry-run mismatch -> exactly one regen with feedback -> PR opens."""
    await truncate_all(db)
    user = await _create_test_user(db)
    repo = await _create_test_repo(db, user)
    run = await _create_test_run(db, repo, status="pending")
    run_id = run.id

    bad_patch = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x\n+y\n"
    good_patch = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x\n+fixed\n"
    call_log: list[dict[str, Any]] = []
    generate_mock = _mock_generate_fix_with(
        [{"number": 1, "patch": bad_patch}, {"number": 2, "patch": good_patch}],
        call_log,
    )

    with (
        patch("app.orchestrator.gather_context", AsyncMock(return_value="Diagnosis")),
        patch(
            "app.subagents.fix_generator.generate_fix", side_effect=generate_mock
        ),
        patch(
            "app.sandbox.verify",
            AsyncMock(
                return_value={
                    "status": "pass",
                    "failure_reason": None,
                    "build_duration_ms": 100,
                }
            ),
        ),
        patch(
            "app.subagents.pr_writer.generate_pr_text",
            AsyncMock(return_value={"title": "fix: dry run", "body": "body"}),
        ),
        patch("app.github.pr.get_installation_token", AsyncMock(return_value="tok")),
        patch(
            "app.github.pr.dry_run_patch_at_sha",
            AsyncMock(
                side_effect=[
                    "patch does not apply to app.py: hunk @@ -1 +1 @@: "
                    "expected deletion line 'zzz' but target line 1 is 'x'.",
                    None,
                ]
            ),
        ),
        patch("app.github.pr.create_branch", AsyncMock(return_value=None)),
        patch(
            "app.github.pr.commit_patch", AsyncMock(return_value=None)
        ) as mock_commit_patch,
        patch(
            "app.github.pr.open_pr",
            AsyncMock(
                return_value={
                    "html_url": "https://github.com/dryrun-org/dryrun-repo/pull/7",
                    "number": 7,
                }
            ),
        ) as mock_open_pr,
    ):
        await handle_failed_run(run_id)

    # Exactly one regen, with the mismatch as feedback on the prior attempt.
    assert len(call_log) == 2
    assert call_log[0]["prior_attempt"] is None
    assert call_log[0]["review_feedback"] is None
    assert call_log[1]["prior_attempt"] is not None
    assert call_log[1]["prior_attempt"].attempt_number == 1
    assert "does NOT apply" in (call_log[1]["review_feedback"] or "")
    assert "byte-exact" in (call_log[1]["review_feedback"] or "")

    # Proceeds with the corrected patch.
    assert mock_commit_patch.call_count == 1
    assert mock_commit_patch.call_args[1]["patch_text"] == good_patch
    mock_open_pr.assert_called_once()

    db.expire_all()
    refreshed = await db.execute(select(Run).where(Run.id == run_id))
    db_run = refreshed.scalar_one()
    assert db_run.status == "pr_opened"
    assert db_run.failure_reason is None

    attempts = (
        (await db.execute(select(Attempt).where(Attempt.run_id == run_id)))
        .scalars()
        .all()
    )
    assert len(attempts) == 2


@pytest.mark.anyio
async def test_pipeline_pr_dry_run_second_mismatch_errors_without_mutations(
    db: AsyncSession,
) -> None:
    """Second dry-run mismatch -> error path; branch/commit/PR never called."""
    await truncate_all(db)
    user = await _create_test_user(db)
    repo = await _create_test_repo(db, user)
    run = await _create_test_run(db, repo, status="pending")
    run_id = run.id

    bad_patch = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x\n+y\n"
    still_bad_patch = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x\n+z\n"
    call_log: list[dict[str, Any]] = []
    generate_mock = _mock_generate_fix_with(
        [
            {"number": 1, "patch": bad_patch},
            {"number": 2, "patch": still_bad_patch},
        ],
        call_log,
    )

    with (
        patch("app.orchestrator.gather_context", AsyncMock(return_value="Diagnosis")),
        patch(
            "app.subagents.fix_generator.generate_fix", side_effect=generate_mock
        ),
        patch(
            "app.sandbox.verify",
            AsyncMock(
                return_value={
                    "status": "pass",
                    "failure_reason": None,
                    "build_duration_ms": 100,
                }
            ),
        ),
        patch(
            "app.subagents.pr_writer.generate_pr_text",
            AsyncMock(return_value={"title": "fix: dry run", "body": "body"}),
        ),
        patch("app.github.pr.get_installation_token", AsyncMock(return_value="tok")),
        patch(
            "app.github.pr.dry_run_patch_at_sha",
            AsyncMock(
                side_effect=[
                    "patch does not apply to app.py: first mismatch",
                    "patch does not apply to app.py: second mismatch",
                ]
            ),
        ),
        patch(
            "app.github.pr.create_branch", AsyncMock(return_value=None)
        ) as mock_create_branch,
        patch(
            "app.github.pr.commit_patch", AsyncMock(return_value=None)
        ) as mock_commit_patch,
        patch(
            "app.github.pr.open_pr",
            AsyncMock(
                return_value={
                    "html_url": "https://github.com/dryrun-org/dryrun-repo/pull/7",
                    "number": 7,
                }
            ),
        ) as mock_open_pr,
    ):
        await handle_failed_run(run_id)

    # Exactly one regen, then the error path.
    assert len(call_log) == 2

    # No branch created, no commit, no PR: nothing was mutated on GitHub.
    mock_create_branch.assert_not_called()
    mock_commit_patch.assert_not_called()
    mock_open_pr.assert_not_called()

    db.expire_all()
    refreshed = await db.execute(select(Run).where(Run.id == run_id))
    db_run = refreshed.scalar_one()
    assert db_run.status == "error"
    assert db_run.failure_reason is not None
    assert "pr_writer" in db_run.failure_reason
    assert "still does not apply" in db_run.failure_reason
