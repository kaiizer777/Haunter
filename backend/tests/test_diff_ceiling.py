"""Diff-ceiling coherence tests for the push-level code reviewer.

Locks the fixes for the caffdd3 audit findings without touching any other
suite (in particular ``test_auditor_mention.py``):

1. Both storage caps apply — the line cap no longer bypasses the char cap.
2. The composed LLM prompt never exceeds ``MAX_USER_PROMPT_CHARS``.
3. Secrets in the diff are redacted before reaching the model.
4. The line-clip marker is present and distinguishable from the prompt bound.
5. ``repo_context`` is capped separately so it cannot crowd out ``diff_text``.
6. PEM redaction preserves newlines so finding line numbers still anchor.
7. The retry (2nd turn) is bounded + redacted like the first turn.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from app.llm.prompts.audit_prompts import (
    MAX_DIFF_CHARS,
    MAX_DIFF_LINES,
    MAX_REPO_CONTEXT_CHARS,
    MAX_USER_PROMPT_CHARS,
)
from app.subagents.code_reviewer import (
    _MAX_RETRY_FEEDBACK_CHARS,
    _bound_diff_for_storage,
    _build_review_messages,
    analyze_diff,
)

_CLEAN_LLM_RESPONSE = {
    "content": json.dumps(
        {
            "risk_score": 0,
            "summary": "Code changes look clean and well-structured.",
            "findings": [],
        }
    ),
    "usage": {"input_tokens": 10, "output_tokens": 5},
}

_LEAKED_KEY = "sk-abcdefghijklmnopqrstuvwxyz1234567890"


def _user_content(messages: list[dict[str, str]]) -> str:
    return messages[1]["content"]


def _all_user_text(messages: list[dict[str, str]]) -> str:
    """Every user turn the LLM receives (first + retry feedback)."""
    return "\n".join(m["content"] for m in messages if m["role"] == "user")


def _full_request_text(messages: list[dict[str, str]]) -> str:
    return "\n".join(m["content"] for m in messages)


def test_small_diff_passes_through_storage_untouched():
    diff = "diff --git a/f.py b/f.py\n+line1\n+line2\n"
    stored, clipped = _bound_diff_for_storage(diff)
    assert stored == diff
    assert clipped is False
    assert "[...DIFF TRUNCATED...]" not in stored


def test_both_caps_apply_to_many_long_lines():
    """30k+ lines of long lines must still be char-capped (old elif bypass)."""
    diff = ("x" * 100 + "\n") * (MAX_DIFF_LINES + 1_000)
    assert len(diff) > MAX_DIFF_CHARS
    stored, clipped = _bound_diff_for_storage(diff)
    assert clipped is True
    # Newline-aware clip minus marker: never exceeds the char ceiling itself.
    assert len(stored) <= MAX_DIFF_CHARS
    assert "[...DIFF TRUNCATED...]" in stored
    # Truncated back to a line boundary, not mid-line.
    assert stored.endswith("\n[...DIFF TRUNCATED...]\n")
    # Review surface keeps its head, not an arbitrary window.
    assert stored.startswith("x" * 100)


def test_line_clip_keeps_first_max_lines_and_marks():
    diff = "a\n" * MAX_DIFF_LINES + "b\n" * 5
    stored, clipped = _bound_diff_for_storage(diff)
    assert clipped is True
    assert "[...DIFF TRUNCATED...]" in stored
    assert "\nb\n" not in stored
    assert stored.startswith("a\n")


def test_prompt_bound_clamps_oversized_diff():
    messages = _build_review_messages("d" * 200_000)
    content = _user_content(messages)
    assert len(content) <= MAX_USER_PROMPT_CHARS
    assert "TRUNCATED" in content


def test_line_marker_survives_into_prompt_when_it_fits():
    """A line-clipped diff under the prompt bound keeps its storage marker."""
    # Mirrors analyze_diff: storage clip first, then prompt composition.
    stored, clipped = _bound_diff_for_storage("a\n" * MAX_DIFF_LINES + "b\n" * 5)
    assert clipped is True
    messages = _build_review_messages(stored)
    content = _user_content(messages)
    assert len(content) <= MAX_USER_PROMPT_CHARS
    assert "[...DIFF TRUNCATED...]" in content


def test_large_but_storable_diff_is_not_silently_truncated():
    """An 80k diff (past the old 60k cap, under every current cap) ships whole."""
    diff = "q" * 80_000
    messages = _build_review_messages(diff)
    content = _user_content(messages)
    assert diff in content
    assert "TRUNCATED" not in content


def test_diff_secrets_redacted_from_prompt():
    diff = f'+api_key = "{_LEAKED_KEY}"\n' + "z" * 1_000
    content = _user_content(_build_review_messages(diff))
    assert _LEAKED_KEY not in content
    assert "[REDACTED" in content


def test_repo_context_capped_separately_and_diff_share_reserved():
    """A huge repo_context cannot crowd diff_text out of the prompt budget."""
    diff = "DIFFMARKER_" + "d" * 10_000
    repo = "R" * 200_000
    messages = _build_review_messages(diff, repo)
    content = _user_content(messages)
    assert len(content) <= MAX_USER_PROMPT_CHARS
    # Diff head survives even though repo alone is 2x the budget.
    assert "DIFFMARKER_" in content
    # Repo portion respects its own cap (10k) rather than the full 90k.
    assert content.count("R") <= MAX_REPO_CONTEXT_CHARS + len("[TRUNCATED]")


def test_pem_redaction_preserves_line_count():
    """Multiline secrets keep their newlines so finding lines still anchor."""
    diff = (
        "a\n"
        "-----BEGIN PRIVATE KEY-----\n"
        "secret1\n"
        "secret2\n"
        "-----END PRIVATE KEY-----\n"
        "b\n"
    )
    content = _user_content(_build_review_messages(diff))
    assert "[REDACTED_PRIVATE_KEY]" in content
    assert "PRIVATE KEY-----" not in content.replace("[REDACTED_PRIVATE_KEY]", "")
    # 4 overhead newlines from the prompt template; diff newlines preserved.
    assert content.count("\n") == diff.count("\n") + 4


def test_retry_turn_stays_bounded_and_redacted():
    """The 2nd-turn feedback message respects the same ceiling as the first."""
    diff = f'+api_key = "{_LEAKED_KEY}"\n' + "d" * 200_000
    messages = _build_review_messages(
        diff, "repo", validation_error_context="x" * 100_000
    )
    assert len(messages) == 4
    first_user = messages[1]["content"]
    feedback_user = messages[3]["content"]
    assert len(first_user) <= MAX_USER_PROMPT_CHARS
    assert len(feedback_user) <= _MAX_RETRY_FEEDBACK_CHARS + 200
    # Full retry request (both user turns + ack) fits the prompt budget.
    assert len(first_user) + len(feedback_user) + len(messages[2]["content"]) <= (
        MAX_USER_PROMPT_CHARS + 200
    )
    assert _LEAKED_KEY not in _full_request_text(messages)
    assert "[REDACTED" in first_user


async def test_analyze_diff_end_to_end_caps_and_redacts():
    """Oversized, secret-bearing diff: the model sees <=90k, redacted chars."""
    diff = (
        f'+api_key = "{_LEAKED_KEY}"\n'
        + ("y" * 100 + "\n") * (MAX_DIFF_LINES + 500)
    )
    with patch("app.subagents.code_reviewer.LLMClient") as mock_client_cls:
        instance = mock_client_cls.return_value
        instance.complete = AsyncMock(return_value=dict(_CLEAN_LLM_RESPONSE))
        result = await analyze_diff(diff_text=diff)

    assert result.output.risk_score == 0
    assert result.diff_clipped is True
    sent_messages = instance.complete.call_args.kwargs["messages"]
    prompt = _user_content(sent_messages)
    assert len(prompt) <= MAX_USER_PROMPT_CHARS
    assert _LEAKED_KEY not in prompt
    assert "[REDACTED" in prompt
    # Full request (all turns) is bounded + redacted, not just the first turn.
    full = _full_request_text(sent_messages)
    assert _LEAKED_KEY not in full
    assert len(_all_user_text(sent_messages)) <= MAX_USER_PROMPT_CHARS + 200


async def test_analyze_diff_retry_request_stays_bounded_and_redacted():
    """Retry path: oversized secret diff + huge repo still fits + redacted."""
    diff = f'+api_key = "{_LEAKED_KEY}"\n' + "d" * 200_000
    repo = "R" * 200_000
    bad_first = {
        "content": '{"risk_score": 150, "summary": "Score too high"}',
        "usage": {"input_tokens": 400, "output_tokens": 50},
    }
    with patch("app.subagents.code_reviewer.LLMClient") as mock_client_cls:
        instance = mock_client_cls.return_value
        instance.complete = AsyncMock(
            side_effect=[bad_first, dict(_CLEAN_LLM_RESPONSE)]
        )
        result = await analyze_diff(diff_text=diff, repo_context=repo)

    assert result.output.risk_score == 0
    assert instance.complete.call_count == 2
    retry_messages = instance.complete.call_args.kwargs["messages"]
    assert len(retry_messages) == 4
    full = _full_request_text(retry_messages)
    assert _LEAKED_KEY not in full
    assert "R" * 100_000 not in full
    assert len(_all_user_text(retry_messages)) <= MAX_USER_PROMPT_CHARS + 200
    assert "[REDACTED" in retry_messages[1]["content"]


async def test_analyze_diff_small_diff_not_marked_clipped():
    """Small diffs propagate diff_clipped=False for the publish layer."""
    with patch("app.subagents.code_reviewer.LLMClient") as mock_client_cls:
        instance = mock_client_cls.return_value
        instance.complete = AsyncMock(return_value=dict(_CLEAN_LLM_RESPONSE))
        result = await analyze_diff(diff_text="diff --git a/f.py b/f.py\n+ok\n")

    assert result.diff_clipped is False
