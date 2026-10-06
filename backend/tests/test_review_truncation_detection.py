"""
A4 — review truncation detection and the shared ``finish_reason`` contract.

The code reviewer used to re-request the *same* output budget after a failed
parse, so a findings JSON that the provider cut off at the budget truncated
again and the run ended as a generic ``ValidationError`` — indistinguishable
from a real schema violation and impossible to act on. Detection is only
possible if the adapter contract surfaces the provider's own reason for ending
generation, so these tests pin three things:

1. Every adapter returns the *same* dict shape, including ``finish_reason`` —
   a consumer that sees the field on three providers and ``None`` on the fourth
   is the exact defect class this covers. Anthropic names the field
   ``stop_reason``; the adapter must map it onto the shared ``finish_reason`` key.
2. An absent reason means *unknown*, never "ok" — the
   ``usage.output_tokens >= max_tokens`` heuristic still has to fire.
3. A truncated response raises a distinct, actionable
   ``ReviewOutputTruncatedError`` and skips the budget-invariant retry, instead
   of replaying the same request.

Mock only — HTTP is mocked at ``httpx.AsyncClient.post``; no live provider calls.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.llm.anthropic import AnthropicAdapter
from app.llm.groq import GroqProvider
from app.llm.openai import OpenAIAdapter
from app.llm.opencode_zen import OpenCodeZenProvider
from app.subagents.code_reviewer import (
    REVIEW_MAX_TOKENS,
    ReviewAnalysisError,
    ReviewOutputTruncatedError,
    _detect_truncation,
    analyze_diff,
)

#: The shared contract every adapter must honour. Key-for-key parity across all
#: four adapters is asserted against this literal, not against another adapter.
EXPECTED_RESPONSE_KEYS = {
    "content",
    "tool_calls",
    "usage",
    "latency_ms",
    "model",
    "finish_reason",
}

_BASE_URL = "https://unit.test/v1"
_API_KEY = "unit-test-key"

#: A findings object cut off mid-array by the token budget — a valid JSON prefix,
#: unparseable as a whole.
TRUNCATED_REVIEW_JSON = (
    '{"risk_score": 55, "summary": "Several issues found", "findings": ['
    '{"file_path": "app/db.py", "line_start": 10, "line_end": 12, '
    '"category": "logic", "severity": "medium", "critique": "Connection leak'
)

_ADAPTERS = {
    "openai": lambda: OpenAIAdapter(base_url=_BASE_URL, api_key=_API_KEY),
    "groq": lambda: GroqProvider(base_url=_BASE_URL, api_key=_API_KEY),
    "opencode_zen": lambda: OpenCodeZenProvider(base_url=_BASE_URL, api_key=_API_KEY),
    "anthropic": lambda: AnthropicAdapter(base_url=_BASE_URL, api_key=_API_KEY),
}


def _openai_style_payload(finish_reason: str | None) -> dict:
    """A provider payload in the OpenAI-compatible shape (OpenAI/Groq/Zen)."""
    choice: dict = {"message": {"role": "assistant", "content": '{"risk_score": 10}'}}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return {
        "id": "chatcmpl-test",
        "model": "test-model",
        "choices": [choice],
        "usage": {"prompt_tokens": 100, "completion_tokens": 200},
    }


def _anthropic_payload(stop_reason: str | None) -> dict:
    """A provider payload in Anthropic's native shape (``stop_reason``)."""
    payload: dict = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-5",
        "content": [{"type": "text", "text": '{"risk_score": 10}'}],
        "usage": {"input_tokens": 100, "output_tokens": 200},
    }
    if stop_reason is not None:
        payload["stop_reason"] = stop_reason
    return payload


async def _call_adapter_with_payload(provider, payload: dict) -> dict:
    """Run one ``complete`` call against a canned provider payload."""
    request = httpx.Request("POST", f"{_BASE_URL}/messages")
    response = httpx.Response(200, json=payload, request=request)
    with patch.object(
        httpx.AsyncClient, "post", new=AsyncMock(return_value=response)
    ) as post:
        result = await provider.complete(messages=[{"role": "user", "content": "hi"}])
    assert post.await_count == 1
    return result


# ---------------------------------------------------------------------------
# 1. Adapter shape parity + Anthropic stop_reason -> finish_reason mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider_name", sorted(_ADAPTERS))
async def test_every_adapter_returns_the_same_response_shape(provider_name):
    """All four adapters return identical keys, ``finish_reason`` included."""
    factory = _ADAPTERS[provider_name]
    payload = (
        _anthropic_payload("end_turn")
        if provider_name == "anthropic"
        else _openai_style_payload("stop")
    )

    result = await _call_adapter_with_payload(factory(), payload)

    assert set(result) == EXPECTED_RESPONSE_KEYS, (
        f"{provider_name} response shape diverges from the shared contract: "
        f"{sorted(result)}"
    )


@pytest.mark.parametrize(
    ("stop_reason", "expected_finish_reason"),
    [
        ("max_tokens", "max_tokens"),
        ("end_turn", "end_turn"),
        ("tool_use", "tool_use"),
        (None, None),
    ],
)
async def test_anthropic_maps_stop_reason_to_finish_reason(
    stop_reason, expected_finish_reason
):
    """Anthropic's ``stop_reason`` is surfaced verbatim as ``finish_reason``.

    Reported verbatim and never synthesized: an absent ``stop_reason`` must come
    back as ``None`` (unknown), not as a fabricated ``"end_turn"``.
    """
    result = await _call_adapter_with_payload(
        AnthropicAdapter(base_url=_BASE_URL, api_key=_API_KEY),
        _anthropic_payload(stop_reason),
    )

    assert "finish_reason" in result
    assert result["finish_reason"] == expected_finish_reason


# ---------------------------------------------------------------------------
# 2. _detect_truncation semantics
# ---------------------------------------------------------------------------


def test_detect_truncation_reads_finish_reason():
    """An explicit non-completion finish reason is authoritative."""
    response = {
        "finish_reason": "length",
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }
    assert _detect_truncation(response, REVIEW_MAX_TOKENS) == "length"


def test_detect_truncation_reads_raw_anthropic_stop_reason():
    """A raw provider payload carrying ``stop_reason`` is judged the same way."""
    response = {
        "stop_reason": "max_tokens",
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }
    assert _detect_truncation(response, REVIEW_MAX_TOKENS) == "max_tokens"


def test_detect_truncation_accepts_completion_finish_reasons():
    """Deliberate stops — OpenAI-style and Anthropic-style — are not truncation."""
    for reason in ("stop", "end_turn", "stop_sequence", "tool_calls", "tool_use"):
        response = {
            "finish_reason": reason,
            "usage": {"input_tokens": 10, "output_tokens": 10},
        }
        assert _detect_truncation(response, REVIEW_MAX_TOKENS) is None


def test_absent_finish_reason_is_unknown_not_ok():
    """Absent reason falls through to the token heuristic, which still fires."""
    response = {"usage": {"input_tokens": 10, "output_tokens": REVIEW_MAX_TOKENS}}
    detected = _detect_truncation(response, REVIEW_MAX_TOKENS)
    assert detected is not None
    assert "inferred" in detected


def test_absent_finish_reason_with_small_usage_is_not_truncation():
    """Absent reason + unused budget is genuinely complete, not a false positive."""
    response = {"usage": {"input_tokens": 10, "output_tokens": 25}}
    assert _detect_truncation(response, REVIEW_MAX_TOKENS) is None


def test_anthropic_truncation_with_unused_token_budget_is_still_detected():
    """The provider's word beats the heuristic: a sliver of the budget, still cut.

    Without the ``stop_reason`` mapping this response looks complete, the parse
    fails, and the run is misreported as a schema violation.
    """
    response = {"finish_reason": "max_tokens", "usage": {"output_tokens": 12}}
    assert _detect_truncation(response, REVIEW_MAX_TOKENS) == "max_tokens"


# ---------------------------------------------------------------------------
# 3. analyze_diff: distinct error + skipped budget-invariant retry
# ---------------------------------------------------------------------------


async def test_truncated_response_raises_actionable_error_and_skips_retry():
    """A truncated findings JSON fails fast with a distinct, actionable error."""
    truncated_response = {
        "content": TRUNCATED_REVIEW_JSON,
        "tool_calls": None,
        "usage": {"input_tokens": 800, "output_tokens": REVIEW_MAX_TOKENS},
        "latency_ms": 4100,
        "model": "test-model",
        "finish_reason": "length",
    }

    with patch("app.subagents.code_reviewer.LLMClient") as mock_client_cls:
        instance = mock_client_cls.return_value
        instance.complete = AsyncMock(return_value=truncated_response)

        with pytest.raises(ReviewOutputTruncatedError) as exc_info:
            await analyze_diff(diff_text="diff --git a/app/db.py b/app/db.py")

        message = str(exc_info.value)
        assert isinstance(exc_info.value, ReviewAnalysisError)
        assert "truncated" in message
        assert "length" in message
        assert str(REVIEW_MAX_TOKENS) in message
        # The retry re-requests the identical budget, so it must not run.
        assert instance.complete.call_count == 1


async def test_schema_violation_still_retries():
    """The retry is not disabled — a genuine schema violation still gets one retry."""
    bad_response = {
        "content": '{"risk_score": 150, "summary": "Score too high"}',
        "usage": {"input_tokens": 400, "output_tokens": 50},
        "finish_reason": "stop",
    }
    good_response = {
        "content": json.dumps(
            {
                "risk_score": 60,
                "summary": "Corrected score and findings.",
                "findings": [],
            }
        ),
        "usage": {"input_tokens": 450, "output_tokens": 60},
        "finish_reason": "stop",
    }

    with patch("app.subagents.code_reviewer.LLMClient") as mock_client_cls:
        instance = mock_client_cls.return_value
        instance.complete = AsyncMock(side_effect=[bad_response, good_response])

        result = await analyze_diff(diff_text="diff --git a/app/db.py b/app/db.py")

        assert result.output.risk_score == 60
        assert instance.complete.call_count == 2


async def test_anthropic_truncated_review_raises_truncation_error():
    """End-to-end: an Anthropic response cut at the budget is not a parse failure.

    Drives the real ``AnthropicAdapter`` so the ``stop_reason`` -> ``finish_reason``
    mapping and the reviewer's detection are covered together. Output consumed a
    fraction of the budget, so only the provider's own signal can catch this.
    """
    truncated_payload = {
        "id": "msg_truncated",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-5",
        "content": [{"type": "text", "text": TRUNCATED_REVIEW_JSON}],
        "stop_reason": "max_tokens",
        "usage": {"input_tokens": 900, "output_tokens": 37},
    }
    request = httpx.Request("POST", f"{_BASE_URL}/messages")
    canned = httpx.Response(200, json=truncated_payload, request=request)
    adapter = AnthropicAdapter(base_url=_BASE_URL, api_key=_API_KEY)

    with patch("app.subagents.code_reviewer.LLMClient") as mock_client_cls:
        instance = mock_client_cls.return_value

        async def _fake_complete(**_kwargs) -> dict:
            with patch.object(
                httpx.AsyncClient, "post", new=AsyncMock(return_value=canned)
            ):
                return await adapter.complete(
                    messages=[{"role": "user", "content": "review this"}],
                    max_tokens=REVIEW_MAX_TOKENS,
                )

        instance.complete = AsyncMock(side_effect=_fake_complete)

        with pytest.raises(ReviewOutputTruncatedError) as exc_info:
            await analyze_diff(diff_text="diff --git a/app/db.py b/app/db.py")

        assert "max_tokens" in str(exc_info.value)
        assert instance.complete.call_count == 1
