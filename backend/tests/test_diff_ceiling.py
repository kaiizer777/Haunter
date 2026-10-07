"""Diff-ceiling coherence tests for the push-level code reviewer.

Locks the fixes for the caffdd3 audit findings without touching any other
suite (in particular ``test_auditor_mention.py``):

1. Both storage caps apply — the line cap no longer bypasses the char cap.
2. The composed LLM prompt never exceeds ``MAX_USER_PROMPT_CHARS``.
3. Secrets in the diff are redacted before reaching the model.
4. The line-clip marker is present and distinguishable from the prompt bound.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from app.llm.prompts.audit_prompts import (
    MAX_DIFF_CHARS,
    MAX_DIFF_LINES,
    MAX_USER_PROMPT_CHARS,
)
from app.subagents.code_reviewer import (
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


def test_small_diff_passes_through_storage_untouched():
    diff = "diff --git a/f.py b/f.py\n+line1\n+line2\n"
    assert _bound_diff_for_storage(diff) == diff
    assert "[...DIFF TRUNCATED...]" not in _bound_diff_for_storage(diff)


def test_both_caps_apply_to_many_long_lines():
    """30k+ lines of long lines must still be char-capped (old elif bypass)."""
    diff = ("x" * 100 + "\n") * (MAX_DIFF_LINES + 1_000)
    assert len(diff) > MAX_DIFF_CHARS
    stored = _bound_diff_for_storage(diff)
    assert len(stored) <= MAX_DIFF_CHARS + len("\n[...DIFF TRUNCATED...]\n")
    assert "[...DIFF TRUNCATED...]" in stored
    # Review surface keeps its head, not an arbitrary window.
    assert stored.startswith("x" * 100)


def test_line_clip_keeps_first_max_lines_and_marks():
    diff = "a\n" * MAX_DIFF_LINES + "b\n" * 5
    stored = _bound_diff_for_storage(diff)
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
    stored = _bound_diff_for_storage("a\n" * MAX_DIFF_LINES + "b\n" * 5)
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
    sent_messages = instance.complete.call_args.kwargs["messages"]
    prompt = _user_content(sent_messages)
    assert len(prompt) <= MAX_USER_PROMPT_CHARS
    assert _LEAKED_KEY not in prompt
    assert "[REDACTED" in prompt
