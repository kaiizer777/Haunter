"""
Conversational follow-up delivery tests.

Covers:
  1. `_post_followup_reply` answers through the GitHub review-thread replies
     API, addressed to the comment that carried the `@haunter` command.
  2. An `issue_comment` trigger has no review thread: GitHub 404 degrades to a
     pull request comment, which is still the conversation the reviewer read.
  3. Any other transport failure degrades to a pull request comment.
  4. A run with no PR number posts nothing.
  5. End to end: a `test-fix` follow-up run verifies, replies in the thread
     that asked, never commits, and terminates as `completed`.
  6. End to end: a `fix` follow-up run commits to the PR branch and replies in
     the same thread.
7. A `pull_request_review_comment` instruction reaches the fix generator: the
      review thread wins over the issue thread, and is redacted like every
      other gathered section.
  8. A reply is addressed to the review thread's *top-level* comment, because
      GitHub's replies endpoint rejects a nested id.
  9. A verdict that cannot be delivered never downgrades an otherwise
      successful run to `error`.
 10. `fetch_pr_review_comments` paginates, stays bounded, and refuses to
      follow an off-host `Link`.
 11. The triggering comment id selects the instruction, so a thread holding
      several `@haunter` comments still acts on the one that asked, and an
      exact match in one channel is never displaced by the other channel's
      `@haunter` fallback.
 12. The trigger is resolved from the whole review-comment list, so the
      20-comment context window cannot hide it on a busy PR; if the bounded
      fetch cannot supply it at all, a review trigger is fetched by id, an
      unretrievable one fails the run rather than acting on another request,
      and an issue trigger never makes that request at all.

The end-to-end cases drive the real orchestrator against the in-process store
from `tests/fake_audit_db.py`, so they are hermetic: no network, no
PostgreSQL, no `TEST_DATABASE_URL`.
"""

from __future__ import annotations

import json
import uuid
from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from app.github_client import (
    GitHubClientError,
    GitHubResourceNotFoundError,
    fetch_pr_review_comments,
)
from app.models import Attempt, Repo, RepoSettings, Run
from app.subagents.context_gatherer import (
    extract_reviewer_feedback,
    gather_context,
)
from app.orchestrator import RunStatus, _post_followup_reply, handle_failed_run

# `audit_store`, `fake_audit_db` and `fake_audit_user_factory` are pytest
# fixtures; importing them here is what registers them for this module.
from tests.fake_audit_db import (  # noqa: F401
    FakeAsyncSession,
    FakeStore,
    audit_store,
    fake_audit_db,
    fake_audit_user_factory,
)

PR_BRANCH = "haunter/fix-aabbccdd-1"
TRIGGER_COMMENT_ID = 778899

PATCH_TEXT = (
    "--- a/calc.py\n"
    "+++ b/calc.py\n"
    "@@ -1,2 +1,3 @@\n"
    "-def add(): pass\n"
    "+def add(a, b): return a + b\n"
)


# ---------------------------------------------------------------------------
# 1-4. Channel selection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reply_goes_to_the_originating_review_thread() -> None:
    with (
        patch(
            "app.github_client.post_review_thread_reply", new_callable=AsyncMock
        ) as reply,
        patch("app.github_client.post_pr_comment", new_callable=AsyncMock) as pr_post,
    ):
        channel = await _post_followup_reply(
            run_id=uuid.uuid4(),
            owner="acme",
            repo="app-repo",
            pr_number=42,
            in_reply_to_comment_id=TRIGGER_COMMENT_ID,
            body="verdict",
            token="t",
        )

    assert channel == "review_thread"
    reply.assert_called_once()
    assert reply.call_args[1]["pr_number"] == 42
    assert reply.call_args[1]["in_reply_to_comment_id"] == TRIGGER_COMMENT_ID
    assert reply.call_args[1]["body"] == "verdict"
    pr_post.assert_not_called()


@pytest.mark.asyncio
async def test_issue_comment_trigger_falls_back_to_a_pr_comment() -> None:
    with (
        patch(
            "app.github_client.post_review_thread_reply",
            new_callable=AsyncMock,
            side_effect=GitHubResourceNotFoundError("not a review thread"),
        ),
        patch("app.github_client.post_pr_comment", new_callable=AsyncMock) as pr_post,
    ):
        channel = await _post_followup_reply(
            run_id=uuid.uuid4(),
            owner="acme",
            repo="app-repo",
            pr_number=42,
            in_reply_to_comment_id=TRIGGER_COMMENT_ID,
            body="verdict",
            token="t",
        )

    assert channel == "pr_comment"
    pr_post.assert_called_once()
    assert pr_post.call_args[1]["body"] == "verdict"


@pytest.mark.asyncio
async def test_transport_failure_falls_back_to_a_pr_comment() -> None:
    with (
        patch(
            "app.github_client.post_review_thread_reply",
            new_callable=AsyncMock,
            side_effect=GitHubClientError("502"),
        ),
        patch("app.github_client.post_pr_comment", new_callable=AsyncMock) as pr_post,
    ):
        channel = await _post_followup_reply(
            run_id=uuid.uuid4(),
            owner="acme",
            repo="app-repo",
            pr_number=42,
            in_reply_to_comment_id=TRIGGER_COMMENT_ID,
            body="verdict",
            token="t",
        )

    assert channel == "pr_comment"
    pr_post.assert_called_once()


@pytest.mark.asyncio
async def test_run_without_a_pr_number_posts_nothing() -> None:
    with (
        patch(
            "app.github_client.post_review_thread_reply", new_callable=AsyncMock
        ) as reply,
        patch("app.github_client.post_pr_comment", new_callable=AsyncMock) as pr_post,
    ):
        channel = await _post_followup_reply(
            run_id=uuid.uuid4(),
            owner="acme",
            repo="app-repo",
            pr_number=None,
            in_reply_to_comment_id=TRIGGER_COMMENT_ID,
            body="verdict",
            token="t",
        )

    assert channel == "no_pr"
    reply.assert_not_called()
    pr_post.assert_not_called()


# ---------------------------------------------------------------------------
# 5-6. End-to-end pipeline
# ---------------------------------------------------------------------------


async def seed_followup(
    session: FakeAsyncSession,
    user_factory,
    *,
    conclusion: str,
    comment_id: int = TRIGGER_COMMENT_ID,
    reply_to_comment_id: int | None = None,
) -> tuple[Repo, Run]:
    """A repo whose PR run already has one verified attempt to refine."""
    user = await user_factory(github_id=4242, username="conv-pipeline-user")
    repo = Repo(user_id=user.id, owner="acme-corp", name="app-repo")
    session.add(repo)
    # Pre-seed governance so get_repo_settings() reads a row instead of
    # building an in-memory default.
    session.add(
        RepoSettings(
            repo_id=repo.id,
            preset="standard",
            enable_auto_fix=True,
            enable_sandbox_verification=True,
            enable_pr_comments=True,
        )
    )
    await session.commit()

    root = Run(
        repo_id=repo.id,
        github_run_id=99887766,
        github_delivery_id=str(uuid.uuid4()),
        head_sha="abcdef0123456789abcdef0123456789abcdef01",
        head_branch=PR_BRANCH,
        pr_number=42,
        pr_branch=PR_BRANCH,
        pr_url="https://github.com/acme-corp/app-repo/pull/42",
        status=RunStatus.pr_opened.value,
        conclusion="success",
    )
    session.add(root)
    await session.commit()

    session.add(
        Attempt(
            run_id=root.id,
            attempt_number=1,
            patch_text=PATCH_TEXT,
            confidence_score=90,
            strategy_notes="Initial fix",
            verification_status="pass",
        )
    )

    child = Run(
        repo_id=repo.id,
        parent_run_id=root.id,
        trigger_comment_id=comment_id,
        reply_to_comment_id=reply_to_comment_id,
        head_sha=root.head_sha,
        head_branch=PR_BRANCH,
        pr_number=42,
        pr_branch=PR_BRANCH,
        status=RunStatus.pending.value,
        conclusion=conclusion,
    )
    session.add(child)
    await session.commit()
    return repo, child


def enter_pipeline(stack: ExitStack) -> None:
    """Stub every external dependency the refinement pipeline reaches for."""
    for ctx in (
        patch(
            "app.github_client.fetch_pr_comments",
            new_callable=AsyncMock,
            return_value=[
                {
                    "user": {"login": "staff"},
                    "author_association": "OWNER",
                    "body": "@haunter check the zero divisor",
                }
            ],
        ),
        patch(
            "app.github_client.fetch_pr_review_comments",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("app.github_client.fetch_diff", new_callable=AsyncMock, return_value=""),
        patch(
            "app.llm.LLMClient.complete",
            new_callable=AsyncMock,
            return_value={
                "content": json.dumps(
                    {
                        "patch": PATCH_TEXT,
                        "confidence": 92,
                        "strategy_notes": "Guard the zero divisor",
                    }
                ),
                "usage": {"input_tokens": 120, "output_tokens": 60},
            },
        ),
        patch(
            "app.sandbox.verify",
            new_callable=AsyncMock,
            return_value={
                "status": "pass",
                "failure_reason": None,
                "build_duration_ms": 900,
            },
        ),
        patch(
            "app.github.pr.get_installation_token",
            new_callable=AsyncMock,
            return_value="fake_token",
        ),
    ):
        stack.enter_context(ctx)


def enter_spy(stack: ExitStack, target: str) -> Any:
    """Enter a patch and return the mock so the test can assert on it."""
    return stack.enter_context(patch(target, new_callable=AsyncMock))


@pytest.mark.asyncio
async def test_test_fix_run_replies_in_thread_and_never_commits(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
) -> None:
    _, child = await seed_followup(
        fake_audit_db, fake_audit_user_factory, conclusion="test-fix"
    )

    with ExitStack() as stack:
        enter_pipeline(stack)
        commit_patch = enter_spy(stack, "app.github.pr.commit_patch")
        thread_reply = enter_spy(stack, "app.github_client.post_review_thread_reply")
        pr_post = enter_spy(stack, "app.github_client.post_pr_comment")
        await handle_failed_run(child.id)

    # Verify-only: the PR branch is left untouched.
    commit_patch.assert_not_called()
    # The verdict is answered in the thread that asked for it.
    thread_reply.assert_called_once()
    reply_kwargs = thread_reply.call_args[1]
    assert reply_kwargs["pr_number"] == 42
    assert reply_kwargs["in_reply_to_comment_id"] == TRIGGER_COMMENT_ID
    assert "test-fix" in reply_kwargs["body"]
    assert "No commit was made to the PR branch" in reply_kwargs["body"]
    pr_post.assert_not_called()
    # Neither pr_opened nor fallback_commented describes "verified but not
    # committed", so the run lands on the legacy terminal `completed`.
    assert child.status == RunStatus.completed.value


@pytest.mark.asyncio
async def test_reply_targets_the_thread_ancestor_not_a_nested_comment(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
) -> None:
    """A command sent as a reply must be answered on its top-level thread.

    GitHub's replies endpoint rejects a nested `comment_id` with 422, which
    would degrade a threaded verdict into an unrelated PR-level comment.
    """
    _, child = await seed_followup(
        fake_audit_db,
        fake_audit_user_factory,
        conclusion="test-fix",
        comment_id=778899,
        reply_to_comment_id=778800,
    )

    with ExitStack() as stack:
        enter_pipeline(stack)
        commit_patch = enter_spy(stack, "app.github.pr.commit_patch")
        thread_reply = enter_spy(stack, "app.github_client.post_review_thread_reply")
        pr_post = enter_spy(stack, "app.github_client.post_pr_comment")
        await handle_failed_run(child.id)

    commit_patch.assert_not_called()
    thread_reply.assert_called_once()
    assert thread_reply.call_args[1]["in_reply_to_comment_id"] == 778800
    # Still threaded, not degraded to the PR conversation.
    pr_post.assert_not_called()
    assert child.status == RunStatus.completed.value


@pytest.mark.asyncio
async def test_top_level_comment_replies_to_itself(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
) -> None:
    """With no ancestor recorded, the triggering comment is the thread root."""
    _, child = await seed_followup(
        fake_audit_db,
        fake_audit_user_factory,
        conclusion="test-fix",
        comment_id=778899,
        reply_to_comment_id=None,
    )

    with ExitStack() as stack:
        enter_pipeline(stack)
        thread_reply = enter_spy(stack, "app.github_client.post_review_thread_reply")
        await handle_failed_run(child.id)

    thread_reply.assert_called_once()
    assert thread_reply.call_args[1]["in_reply_to_comment_id"] == 778899


@pytest.mark.asyncio
async def test_failed_reply_does_not_downgrade_a_verified_test_fix(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
) -> None:
    """A verdict we could not post must not be reported as a failed run.

    The patch already passed sandbox verification, so a comment transport
    failure must leave the run `completed`, not flip it to `error`.
    """
    _, child = await seed_followup(
        fake_audit_db, fake_audit_user_factory, conclusion="test-fix"
    )

    with ExitStack() as stack:
        enter_pipeline(stack)
        commit_patch = enter_spy(stack, "app.github.pr.commit_patch")
        stack.enter_context(
            patch(
                "app.github_client.post_review_thread_reply",
                new_callable=AsyncMock,
                side_effect=GitHubClientError("boom"),
            )
        )
        stack.enter_context(
            patch(
                "app.github_client.post_pr_comment",
                new_callable=AsyncMock,
                side_effect=GitHubClientError("boom"),
            )
        )
        await handle_failed_run(child.id)

    commit_patch.assert_not_called()
    assert child.status == RunStatus.completed.value
    assert child.conclusion == "test-fix"


@pytest.mark.asyncio
async def test_failed_reply_does_not_downgrade_a_committed_fix(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
) -> None:
    """The commit landed, so a failed confirmation comment cannot undo it."""
    commit_sha = "abcdef99887766554433221100aabbccddeeff11"
    _, child = await seed_followup(
        fake_audit_db, fake_audit_user_factory, conclusion="feedback", comment_id=778800
    )

    with ExitStack() as stack:
        enter_pipeline(stack)
        commit_patch = stack.enter_context(
            patch(
                "app.github.pr.commit_patch",
                new_callable=AsyncMock,
                return_value=commit_sha,
            )
        )
        stack.enter_context(
            patch(
                "app.github_client.post_review_thread_reply",
                new_callable=AsyncMock,
                side_effect=GitHubClientError("boom"),
            )
        )
        stack.enter_context(
            patch(
                "app.github_client.post_pr_comment",
                new_callable=AsyncMock,
                side_effect=GitHubClientError("boom"),
            )
        )
        await handle_failed_run(child.id)

    commit_patch.assert_called_once()
    assert child.status == RunStatus.pr_opened.value


@pytest.mark.asyncio
async def test_fix_run_commits_and_replies_in_thread(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
) -> None:
    commit_sha = "abcdef99887766554433221100aabbccddeeff11"
    _, child = await seed_followup(
        fake_audit_db, fake_audit_user_factory, conclusion="feedback", comment_id=778800
    )

    with ExitStack() as stack:
        enter_pipeline(stack)
        commit_patch = stack.enter_context(
            patch(
                "app.github.pr.commit_patch",
                new_callable=AsyncMock,
                return_value=commit_sha,
            )
        )
        thread_reply = enter_spy(stack, "app.github_client.post_review_thread_reply")
        pr_post = enter_spy(stack, "app.github_client.post_pr_comment")
        await handle_failed_run(child.id)

    commit_patch.assert_called_once()
    assert commit_patch.call_args[1]["branch"] == PR_BRANCH
    thread_reply.assert_called_once()
    assert thread_reply.call_args[1]["in_reply_to_comment_id"] == 778800
    assert commit_sha[:7] in thread_reply.call_args[1]["body"]
    pr_post.assert_not_called()
    assert child.status == RunStatus.pr_opened.value


@pytest.mark.asyncio
async def test_refinement_with_pr_comments_disabled_posts_nothing(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
    audit_store: FakeStore,
) -> None:
    """`enable_pr_comments=false` is the repo's kill switch for bot chatter."""
    commit_sha = "abcdef99887766554433221100aabbccddeeff11"
    _, child = await seed_followup(
        fake_audit_db, fake_audit_user_factory, conclusion="feedback", comment_id=778801
    )
    governance = next(
        row
        for row in audit_store.rows(RepoSettings.__table__)
        if row.repo_id == child.repo_id
    )
    governance.enable_pr_comments = False

    with ExitStack() as stack:
        enter_pipeline(stack)
        stack.enter_context(
            patch(
                "app.github.pr.commit_patch",
                new_callable=AsyncMock,
                return_value=commit_sha,
            )
        )
        thread_reply = enter_spy(stack, "app.github_client.post_review_thread_reply")
        pr_post = enter_spy(stack, "app.github_client.post_pr_comment")
        await handle_failed_run(child.id)

    thread_reply.assert_not_called()
    pr_post.assert_not_called()
    assert child.status == RunStatus.pr_opened.value


# ---------------------------------------------------------------------------
# 7. Thread context reaches the fix generator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_thread_instruction_wins_over_the_issue_thread(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
) -> None:
    """A `pull_request_review_comment` request only exists in the review thread.

    The Issues conversation API does not return inline review comments, so if the
    gatherer ignored the review thread the fix generator would be handed the
    wrong instruction (here, a stale one from the issue thread).
    """
    repo, child = await seed_followup(
        fake_audit_db, fake_audit_user_factory, conclusion="feedback"
    )
    issue_thread = [
        {
            "user": {"login": "someone"},
            "author_association": "MEMBER",
            "body": "@haunter please bump the version constant",
        }
    ]
    review_thread = [
        {
            "user": {"login": "reviewer"},
            "path": "calc.py",
            "line": 12,
            "body": "@haunter this line divides by a zero denominator",
        }
    ]

    with (
        patch(
            "app.github_client.fetch_pr_comments",
            new_callable=AsyncMock,
            return_value=issue_thread,
        ),
        patch(
            "app.github_client.fetch_pr_review_comments",
            new_callable=AsyncMock,
            return_value=review_thread,
        ),
        patch(
            "app.github_client.fetch_diff",
            new_callable=AsyncMock,
            return_value="diff --git a/calc.py b/calc.py",
        ),
    ):
        summary = await gather_context(run=child, repo=repo, db=fake_audit_db)

    instruction = extract_reviewer_feedback(summary)
    assert instruction is not None
    assert "zero denominator" in instruction
    assert "bump the version constant" not in instruction
    # The review thread is also reported in full, with its diff anchor.
    assert "## Review Thread Context" in summary
    assert "calc.py:12" in summary


@pytest.mark.asyncio
async def test_triggering_comment_is_selected_over_the_last_mention(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
) -> None:
    """The instruction used must be the one that asked, not the newest mention.

    A thread can carry several `@haunter` comments. Answering the newest one
    would silently act on a different request than the Run was created for,
    so the triggering comment id wins over positional guessing.
    """
    repo, child = await seed_followup(
        fake_audit_db,
        fake_audit_user_factory,
        conclusion="feedback",
        comment_id=5550001,
    )
    issue_thread = [
        {
            "id": 5550001,
            "user": {"login": "reviewer"},
            "author_association": "MEMBER",
            "body": "@haunter handle the AUTH_TOKEN retry loop",
        },
        {
            "id": 5550009,
            "user": {"login": "other"},
            "author_association": "MEMBER",
            "body": "@haunter actually please rename the module instead",
        },
    ]

    with (
        patch(
            "app.github_client.fetch_pr_comments",
            new_callable=AsyncMock,
            return_value=issue_thread,
        ),
        patch(
            "app.github_client.fetch_pr_review_comments",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "app.github_client.fetch_diff",
            new_callable=AsyncMock,
            return_value="diff --git a/auth.py b/auth.py",
        ),
    ):
        summary = await gather_context(run=child, repo=repo, db=fake_audit_db)

    instruction = extract_reviewer_feedback(summary)
    assert instruction is not None
    assert "AUTH_TOKEN retry loop" in instruction
    assert "rename the module" not in instruction


@pytest.mark.asyncio
async def test_issue_trigger_is_not_hijacked_by_a_review_thread_mention(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
) -> None:
    """An exact issue-comment match outranks any review-thread mention.

    The run was created for one specific comment. A review thread that also
    contains `@haunter` must not displace it, or the fix generator answers a
    request nobody made for this run.
    """
    repo, child = await seed_followup(
        fake_audit_db,
        fake_audit_user_factory,
        conclusion="feedback",
        comment_id=5550001,
    )
    issue_thread = [
        {
            "id": 5550001,
            "user": {"login": "reviewer"},
            "author_association": "MEMBER",
            "body": "@haunter fix the NULL deref in parser.py",
        }
    ]
    review_thread = [
        {
            "id": 8880001,
            "user": {"login": "someone-else"},
            "path": "other.py",
            "line": 3,
            "body": "@haunter please rewrite the entire module",
        }
    ]

    with (
        patch(
            "app.github_client.fetch_pr_comments",
            new_callable=AsyncMock,
            return_value=issue_thread,
        ),
        patch(
            "app.github_client.fetch_pr_review_comments",
            new_callable=AsyncMock,
            return_value=review_thread,
        ),
        patch(
            "app.github_client.fetch_diff",
            new_callable=AsyncMock,
            return_value="diff --git a/parser.py b/parser.py",
        ),
    ):
        summary = await gather_context(run=child, repo=repo, db=fake_audit_db)

    instruction = extract_reviewer_feedback(summary)
    assert instruction is not None
    assert "NULL deref" in instruction
    assert "rewrite the entire module" not in instruction


@pytest.mark.asyncio
async def test_trigger_is_found_outside_the_review_context_window(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
) -> None:
    """The 20-comment context window must not hide the triggering comment.

    Only the last 20 review comments are formatted for context, but the
    trigger is resolved from the whole fetched list first. Otherwise a busy
    PR pushes the triggering comment out of the window and a newer
    `@haunter` comment is silently acted on instead.
    """
    repo, child = await seed_followup(
        fake_audit_db,
        fake_audit_user_factory,
        conclusion="feedback",
        comment_id=5550001,
    )
    review_thread = [
        {"id": 5550001, "path": "parser.py", "line": 9, "body": "@haunter fix the NULL deref"}
    ]
    # 25 newer comments, so the trigger sits outside [-20:].
    review_thread += [
        {
            "id": 7000000 + n,
            "path": f"f{n}.py",
            "line": n,
            "body": "@haunter unrelated nit, ignore" if n == 24 else f"nit {n}",
        }
        for n in range(25)
    ]

    with (
        patch(
            "app.github_client.fetch_pr_comments",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "app.github_client.fetch_pr_review_comments",
            new_callable=AsyncMock,
            return_value=review_thread,
        ),
        patch(
            "app.github_client.fetch_diff",
            new_callable=AsyncMock,
            return_value="diff --git a/parser.py b/parser.py",
        ),
    ):
        summary = await gather_context(run=child, repo=repo, db=fake_audit_db)

    instruction = extract_reviewer_feedback(summary)
    assert instruction is not None
    assert "NULL deref" in instruction
    assert "unrelated nit" not in instruction
    # The context window is still applied to the reported thread.
    assert "nit 4\n" not in summary


@pytest.mark.asyncio
async def test_trigger_outside_the_fetch_ceiling_is_recovered_by_id(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
) -> None:
    """A review trigger beyond the fetch ceiling is fetched by id.

    The bounded review fetch keeps only the oldest window, so on a PR with a
    very long review history the triggering comment can be absent from it.
    Acting on an older `@haunter` request instead would patch the wrong thing.
    """
    repo, child = await seed_followup(
        fake_audit_db,
        fake_audit_user_factory,
        conclusion="feedback",
        comment_id=5550001,
        reply_to_comment_id=5550000,
    )
    # A window the size of the fetch ceiling: the trigger is outside it, and the
    # ceiling is what makes a by-id recovery the correct response.
    review_thread = [
        {
            "id": 7000000 + n,
            "path": f"f{n}.py",
            "line": n,
            "body": f"@haunter stale request {n}" if n == 499 else f"nit {n}",
        }
        for n in range(500)
    ]

    with (
        patch(
            "app.github_client.fetch_pr_comments",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "app.github_client.fetch_pr_review_comments",
            new_callable=AsyncMock,
            return_value=review_thread,
        ),
        patch(
            "app.github_client.fetch_review_comment",
            new_callable=AsyncMock,
            return_value={
                "id": 5550001,
                "body": "@haunter fix the actual reported defect",
                "path": "real.py",
                "line": 4,
            },
        ) as by_id,
        patch(
            "app.github_client.fetch_diff",
            new_callable=AsyncMock,
            return_value="diff --git a/real.py b/real.py",
        ),
    ):
        summary = await gather_context(run=child, repo=repo, db=fake_audit_db)

    by_id.assert_called_once()
    assert by_id.call_args[1]["comment_id"] == 5550001
    instruction = extract_reviewer_feedback(summary)
    assert instruction is not None
    assert "actual reported defect" in instruction
    assert "stale request" not in instruction


@pytest.mark.asyncio
async def test_unretrievable_review_trigger_refuses_to_act(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
) -> None:
    """If the trigger cannot be retrieved, fail loudly instead of guessing.

    Silently falling back would commit a patch for a request the reviewer
    never made on this run.
    """
    repo, child = await seed_followup(
        fake_audit_db,
        fake_audit_user_factory,
        conclusion="feedback",
        comment_id=5550001,
        reply_to_comment_id=5550000,
    )

    with (
        patch(
            "app.github_client.fetch_pr_comments",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "app.github_client.fetch_pr_review_comments",
            new_callable=AsyncMock,
            return_value=[
                {
                    "id": 7000000 + n,
                    "path": f"f{n}.py",
                    "line": n,
                    "body": f"@haunter stale request {n}" if n == 499 else f"nit {n}",
                }
                for n in range(500)
            ],
        ),
        patch(
            "app.github_client.fetch_review_comment",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "app.github_client.fetch_diff",
            new_callable=AsyncMock,
            return_value="diff --git a/f.py b/f.py",
        ),
        pytest.raises(ValueError, match="refusing to act on a different"),
    ):
        await gather_context(run=child, repo=repo, db=fake_audit_db)


@pytest.mark.asyncio
async def test_issue_trigger_never_fetches_review_comments_by_id(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
) -> None:
    """An issue-comment trigger is never in the review list; no id lookup.

    `reply_to_comment_id` is only set for review comments, so this path must
    stay closed for issue comments rather than firing a pointless request.
    """
    repo, child = await seed_followup(
        fake_audit_db,
        fake_audit_user_factory,
        conclusion="feedback",
        comment_id=5550001,
        reply_to_comment_id=None,
    )

    with (
        patch(
            "app.github_client.fetch_pr_comments",
            new_callable=AsyncMock,
            return_value=[
                {
                    "id": 5550001,
                    "user": {"login": "reviewer"},
                    "author_association": "MEMBER",
                    "body": "@haunter fix the issue-thread defect",
                }
            ],
        ),
        patch(
            "app.github_client.fetch_pr_review_comments",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "app.github_client.fetch_review_comment",
            new_callable=AsyncMock,
            return_value={"id": 5550001, "body": "should not be used"},
        ) as by_id,
        patch(
            "app.github_client.fetch_diff",
            new_callable=AsyncMock,
            return_value="diff --git a/x.py b/x.py",
        ),
    ):
        summary = await gather_context(run=child, repo=repo, db=fake_audit_db)

    by_id.assert_not_called()
    instruction = extract_reviewer_feedback(summary)
    assert instruction is not None
    assert "issue-thread defect" in instruction


@pytest.mark.asyncio
async def test_review_thread_comments_are_secret_redacted(
    fake_audit_db: FakeAsyncSession,
    fake_audit_user_factory,
) -> None:
    """Reviewer prose is attacker-influenced and reaches the LLM verbatim."""
    repo, child = await seed_followup(
        fake_audit_db, fake_audit_user_factory, conclusion="feedback"
    )

    with (
        patch(
            "app.github_client.fetch_pr_comments",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "app.github_client.fetch_pr_review_comments",
            new_callable=AsyncMock,
            return_value=[
                {
                    "user": {"login": "leak"},
                    "path": "calc.py",
                    "line": 3,
                    "body": "@haunter use ghp_abcdefghijklmnopqrstuvwxyz0123456789",
                }
            ],
        ),
        patch(
            "app.github_client.fetch_diff",
            new_callable=AsyncMock,
            return_value="diff --git a/calc.py b/calc.py",
        ),
    ):
        summary = await gather_context(run=child, repo=repo, db=fake_audit_db)

    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in summary


# ---------------------------------------------------------------------------
# 8. Review-comment pagination (`fetch_pr_review_comments`)
# ---------------------------------------------------------------------------

_PAGE_2 = "https://api.github.com/repos/acme-corp/app-repo/pulls/42/comments?per_page=100&page=2"
_COMMENTS_URL = (
    "https://api.github.com/repos/acme-corp/app-repo/pulls/42/comments"
)


@pytest.mark.asyncio
@respx.mock
async def test_review_comments_follow_pagination() -> None:
    """GitHub pages oldest-first; a second page must not be dropped.

    The consumer keeps only the newest review comments, so without following
    `Link` the most recent `@haunter` instruction would silently vanish on a
    busy PR and the fix generator would act on a stale one.
    """
    route = respx.get(_COMMENTS_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json=[{"id": 1, "body": "oldest"}],
                headers={
                    "link": f'<{_PAGE_2}>; rel="next", <x>; rel="last"'
                },
            ),
            httpx.Response(200, json=[{"id": 2, "body": "newest"}]),
        ]
    )

    comments = await fetch_pr_review_comments(
        owner="acme-corp", repo="app-repo", pr_number=42
    )

    assert [c["id"] for c in comments] == [1, 2]
    assert route.call_count == 2
    # The first request must ask for the maximum page size, otherwise the
    # walk spends its page budget on a fraction of the thread.
    assert route.calls[0].request.url.params["per_page"] == "100"


@pytest.mark.asyncio
@respx.mock
async def test_review_comments_stop_at_the_page_ceiling() -> None:
    """A runaway thread cannot make the fetch unbounded."""
    pages: list[str] = []
    for n in range(2, 12):
        url = (
            "https://api.github.com/repos/acme-corp/app-repo"
            f"/pulls/42/comments?per_page=100&page={n}"
        )
        pages.append(url)
        respx.get(url).respond(
            200,
            json=[{"id": n}],
            headers={"link": f'<{pages[-1]}>; rel="next"'},
        )
    respx.get(_COMMENTS_URL).respond(
        200,
        json=[{"id": 1}],
        headers={"link": f'<{pages[0]}>; rel="next"'},
    )

    comments = await fetch_pr_review_comments(
        owner="acme-corp", repo="app-repo", pr_number=42
    )

    # Absolute value, not the constant: asserting against MAX_REVIEW_COMMENT_PAGES
    # would pass for any value of it and prove nothing.
    assert len(comments) == 5


@pytest.mark.asyncio
@respx.mock
async def test_review_comments_ignore_an_off_host_next_link() -> None:
    """The `Link` header must not be able to redirect the fetch off GitHub."""
    respx.get(_COMMENTS_URL).respond(
        200,
        json=[{"id": 1}],
        headers={"link": '<https://evil.example.com/steal>; rel="next"'},
    )
    off_host = respx.get("https://evil.example.com/steal").respond(
        200, json=[{"id": 999}]
    )

    comments = await fetch_pr_review_comments(
        owner="acme-corp", repo="app-repo", pr_number=42
    )

    assert [c["id"] for c in comments] == [1]
    assert off_host.call_count == 0

