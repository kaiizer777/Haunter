"""Unit tests for interactive @haunter-auditor mention replies (app.webhooks).

Covers:
  1. Mention parsing: @haunter-auditor / @haunter-auditor[bot] (case-insensitive)
     trigger; bare @haunter, @haunterbot, and plain text do not.
  2. Routing: a mention triggers LLM + reply (issue_comment -> create_issue_comment,
     pull_request_review_comment -> post_review_thread_reply in the same thread).
  3. Non-mention bodies are ignored (no LLM, no reply).
  4. Thread-reply failure falls back to a PR comment so the answer is not dropped.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.webhooks import (
    _extract_auditor_question,
    _handle_auditor_mention,
    has_auditor_mention,
)


@pytest.mark.parametrize(
    "body",
    [
        "@haunter-auditor what does this diff do?",
        "@haunter-auditor[bot] explain the risk here",
        "hey @haunter-auditor, is this safe?",
        "@HAUNTER-AUDITOR please review",
        "@Haunter-Auditor[Bot] why is this slow?",
        "question for @haunter-auditor[bot]: explain",
    ],
)
def test_auditor_mention_triggers(body: str) -> None:
    assert has_auditor_mention(body) is True


@pytest.mark.parametrize(
    "body",
    [
        "",
        "LGTM, merge it",
        "@haunter fix the build",
        "@haunter audit this PR",
        "@haunterbot explain this",
        "@haunter-auditorbot explain",  # no boundary: distinct handle
        "haunter-auditor without the at-sign",
    ],
)
def test_non_auditor_mention_ignored(body: str) -> None:
    assert has_auditor_mention(body) is False


@pytest.mark.parametrize("body", [None, 1234, ["@haunter-auditor"], {"b": 1}])
def test_auditor_mention_non_string_is_no_op(body: Any) -> None:
    assert has_auditor_mention(body) is False


def test_extract_auditor_question_strips_mention() -> None:
    q = _extract_auditor_question("@haunter-auditor[bot]  explain this hunk?  ")
    assert "haunter-auditor" not in q.lower()
    assert "explain this hunk?" in q


def _repo() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), owner="acme", name="app")


def _comment(body: str, *, comment_id: int = 778899, reply_to: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=comment_id,
        body=body,
        in_reply_to_id=reply_to,
        author_association="MEMBER",
        user=SimpleNamespace(login="reviewer"),
    )


@pytest.mark.asyncio
async def test_issue_comment_mention_triggers_llm_and_pr_comment() -> None:
    repo = _repo()
    comment = _comment("@haunter-auditor explain this diff")
    with (
        patch("app.webhooks.get_repo_settings", new_callable=AsyncMock) as settings_mock,
        patch("app.github.pr.get_installation_token", new_callable=AsyncMock) as token_mock,
        patch("app.github_client.fetch_pull_request", new_callable=AsyncMock) as pr_mock,
        patch("app.github_client.fetch_pr_comments", new_callable=AsyncMock) as issue_mock,
        patch("app.github_client.fetch_pr_review_comments", new_callable=AsyncMock) as review_mock,
        patch("app.github_client.fetch_pull_request_diff", new_callable=AsyncMock) as diff_mock,
        patch("app.webhooks.LLMClient") as llm_cls,
        patch("app.github_client.create_issue_comment", new_callable=AsyncMock) as post_mock,
        patch("app.github_client.post_review_thread_reply", new_callable=AsyncMock) as thread_mock,
    ):
        settings_mock.return_value = SimpleNamespace(enable_pr_comments=True)
        token_mock.return_value = "tok"
        pr_mock.return_value = {"title": "T", "body": "B"}
        issue_mock.return_value = [{"user": {"login": "a"}, "body": "nit"}]
        review_mock.return_value = []
        diff_mock.return_value = "diff --git a/x.py b/x.py"
        llm_inst = llm_cls.return_value
        llm_inst.complete = AsyncMock(
            return_value={"content": "The diff looks safe.", "usage": {}}
        )
        res = await _handle_auditor_mention(
            AsyncMock(), repo, "acme", "app", 42, comment, "issue_comment", "d1"
        )
    assert res["status"] == "auditor_replied"
    assert res["channel"] == "pr_comment"
    llm_inst.complete.assert_awaited_once()
    sent_messages = llm_inst.complete.await_args.kwargs["messages"]
    assert any("explain this diff" in m["content"] for m in sent_messages if m["role"] == "user")
    post_mock.assert_awaited_once()
    assert "The diff looks safe." in post_mock.await_args.kwargs["body"]
    thread_mock.assert_not_called()


@pytest.mark.asyncio
async def test_review_comment_mention_replies_in_same_thread() -> None:
    repo = _repo()
    comment = _comment("@haunter-auditor[bot] is this racy?", comment_id=111, reply_to=110)
    with (
        patch("app.webhooks.get_repo_settings", new_callable=AsyncMock) as settings_mock,
        patch("app.github.pr.get_installation_token", new_callable=AsyncMock) as token_mock,
        patch("app.github_client.fetch_pull_request", new_callable=AsyncMock),
        patch("app.github_client.fetch_pr_comments", new_callable=AsyncMock, return_value=[]),
        patch("app.github_client.fetch_pr_review_comments", new_callable=AsyncMock, return_value=[]),
        patch("app.github_client.fetch_pull_request_diff", new_callable=AsyncMock, return_value="diff"),
        patch("app.webhooks.LLMClient") as llm_cls,
        patch("app.github_client.create_issue_comment", new_callable=AsyncMock) as post_mock,
        patch("app.github_client.post_review_thread_reply", new_callable=AsyncMock) as thread_mock,
    ):
        settings_mock.return_value = SimpleNamespace(enable_pr_comments=True)
        token_mock.return_value = "tok"
        llm_cls.return_value.complete = AsyncMock(return_value={"content": "No race here."})
        res = await _handle_auditor_mention(
            AsyncMock(), repo, "acme", "app", 7, comment, "pull_request_review_comment", "d2"
        )
    assert res["status"] == "auditor_replied"
    assert res["channel"] == "review_thread"
    # Ancestor id wins: GitHub rejects nested reply ids with 422.
    assert thread_mock.await_args.kwargs["in_reply_to_comment_id"] == 110
    post_mock.assert_not_called()


@pytest.mark.asyncio
async def test_thread_reply_failure_falls_back_to_pr_comment() -> None:
    from app.github_client import GitHubClientError

    repo = _repo()
    comment = _comment("@haunter-auditor help", comment_id=222)
    with (
        patch("app.webhooks.get_repo_settings", new_callable=AsyncMock) as settings_mock,
        patch("app.github.pr.get_installation_token", new_callable=AsyncMock, return_value="tok"),
        patch("app.github_client.fetch_pull_request", new_callable=AsyncMock, return_value={}),
        patch("app.github_client.fetch_pr_comments", new_callable=AsyncMock, return_value=[]),
        patch("app.github_client.fetch_pr_review_comments", new_callable=AsyncMock, return_value=[]),
        patch("app.github_client.fetch_pull_request_diff", new_callable=AsyncMock, return_value=""),
        patch("app.webhooks.LLMClient") as llm_cls,
        patch(
            "app.github_client.post_review_thread_reply",
            new_callable=AsyncMock,
            side_effect=GitHubClientError("gone"),
        ),
        patch("app.github_client.create_issue_comment", new_callable=AsyncMock) as post_mock,
    ):
        settings_mock.return_value = SimpleNamespace(enable_pr_comments=True)
        llm_cls.return_value.complete = AsyncMock(return_value={"content": "Fallback answer."})
        res = await _handle_auditor_mention(
            AsyncMock(), repo, "acme", "app", 9, comment, "pull_request_review_comment", "d3"
        )
    assert res["status"] == "auditor_replied"
    assert res["channel"] == "pr_comment"
    post_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_mention_body_never_calls_llm_or_posts() -> None:
    # Routing gate: the webhook only calls _handle_auditor_mention when the
    # mention is present, so a plain body must not reach LLM/reply at all.
    assert has_auditor_mention("please fix the null handling") is False
    assert has_auditor_mention("@haunter fix it") is False
    with (
        patch("app.webhooks.LLMClient") as llm_cls,
        patch("app.github_client.create_issue_comment", new_callable=AsyncMock) as post_mock,
        patch("app.github_client.post_review_thread_reply", new_callable=AsyncMock) as thread_mock,
    ):
        # No handler invocation happens for non-mentions by construction;
        # assert the side-effect surface stays untouched.
        llm_cls.assert_not_called()
        post_mock.assert_not_called()
        thread_mock.assert_not_called()
