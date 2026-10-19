"""
A6 — the ``finish_reason`` policy must not be deny-by-default, and an applyable
GitHub suggestion must never carry a redaction placeholder.

Two independent review lenses landed on the same root cause: the truncation
classifier inverted the question. It asked *"is this reason one of the strings I
recognise as a clean stop?"* and treated **everything else** as a budget cut.
That is a deny-list wearing an allow-list's clothes, and it is unfixable by
editing the list: the default provider is OpenCode Zen, which fronts multiple
upstreams, so its ``finish_reason`` vocabulary is not knowable from this repo at
all. One unlisted-but-benign value — OpenAI's ``content_filter``, Anthropic's
``pause_turn``, any gateway's ``COMPLETED`` — turned a publishable review into a
terminal ``review.status = "error"`` carrying the *wrong* diagnosis, with no
retry. The honest question is the positive one: *which strings actually mean the
budget ran out?*

These tests pin the inverted policy:

1. Only an explicitly recognised truncation reason (``length``, ``max_tokens``,
   ``max_output_tokens``) is truncation — and it beats the token heuristic, so a
   provider that says ``stop`` at exactly the budget is believed.
2. Every other value, known *or* unknown, is not truncation. Absence is unknown,
   never "ok" and never "truncated".
3. A budget-exhaustion *suspected only* from ``usage`` is no longer a hard
   pre-parse failure. It is honoured only if the payload actually fails to parse,
   which is the only situation where "truncated" and "schema violation" are the
   same observable outcome and the truncated label is the actionable one.

Plus the two adjacent hazards that share the same blast radius: a redaction
placeholder inside a ``suggestion`` block (one click commits the placeholder),
and ``file_path`` being the one model-authored string the schema did not bound
while the docstring claimed every one of them was.

Mock only — HTTP is mocked at ``httpx``; no live provider or GitHub calls.
"""

from __future__ import annotations

import inspect
import json
import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.github_client import (
    MAX_API_RESPONSE_BYTES,
    GitHubResponseLimitError,
    GitHubUnprocessableEntityError,
    create_pr_review,
    fetch_review_threads,
    resolve_review_threads,
)
from app.log_hygiene import MAX_LOG_VALUE_CHARS
from app.subagents import code_reviewer
from app.subagents.code_reviewer import (
    REVIEW_MAX_TOKENS,
    ReviewAnalysisError,
    ReviewFinding,
    ReviewOutputTruncatedError,
    _detect_truncation,
    analyze_diff,
    format_github_suggestion,
)

# The two symbols this PR introduces are reached through the module inside each
# test rather than imported by name at module scope: `AttributeError` inside a
# test is a failing test, whereas a missing name in an import list is a
# collection error that hides whether every other assertion here would pass.
# Both attributes are documented on the constants themselves, so an AttributeError
# is a real failure, not a typo.

# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------

#: Captured before any ``patch("...httpx.AsyncClient", ...)`` runs, so a factory
#: that has to build a real client around a MockTransport cannot call itself.
_REAL_ASYNC_CLIENT = httpx.AsyncClient

_BASE_URL = "https://api.github.com"

#: A complete, valid review payload that consumes the whole budget. Used to
#: prove that a provider's own word beats ``usage.output_tokens``.
_COMPLETE_REVIEW_JSON = json.dumps(
    {
        "risk_score": 42,
        "summary": "One unhandled error path; nothing blocking merge.",
        "findings": [],
    }
)

#: The same payload cut off mid-array — a valid JSON prefix, unparseable whole.
_TRUNCATED_REVIEW_JSON = (
    '{"risk_score": 55, "summary": "Several issues found", "findings": ['
    '{"file_path": "app/db.py", "line_start": 10, "line_end": 12, '
    '"category": "logic", "severity": "medium", "critique": "Connection leak'
)


def _response(
    *,
    content: str = _COMPLETE_REVIEW_JSON,
    finish_reason: str | None = None,
    stop_reason: str | None = None,
    output_tokens: int = 120,
) -> dict[str, Any]:
    """One normalized adapter response, with either field name settable."""
    response: dict[str, Any] = {
        "content": content,
        "tool_calls": None,
        "usage": {"input_tokens": 800, "output_tokens": output_tokens},
        "latency_ms": 1200,
        "model": "test-model",
        "finish_reason": finish_reason,
    }
    if stop_reason is not None:
        response["stop_reason"] = stop_reason
    return response


async def _analyze_with(response: dict[str, Any]):
    """Run ``analyze_diff`` against a single canned adapter response."""
    with patch("app.subagents.code_reviewer.LLMClient") as mock_client_cls:
        instance = mock_client_cls.return_value
        instance.complete = AsyncMock(return_value=response)
        result = await analyze_diff(diff_text="diff --git a/app/db.py b/app/db.py")
    return result, instance


# ---------------------------------------------------------------------------
# 1. Only a recognised truncation reason is truncation
# ---------------------------------------------------------------------------

#: Positive evidence of a budget cut, and where each string was verified.
TRUNCATION_CASES = [
    ("length", "openai.types.chat.chat_completion.Choice.finish_reason (openai 2.20.0)"),
    ("max_tokens", "Anthropic Messages ``stop_reason`` (app/llm/anthropic.py:258)"),
    ("max_output_tokens", "OpenAI Responses API ``incomplete_details.reason``"),
]


@pytest.mark.parametrize(("reason", "verified_in"), TRUNCATION_CASES)
def test_recognised_truncation_reasons_are_detected(reason, verified_in):
    assert reason in code_reviewer.TRUNCATION_FINISH_REASONS
    detected = _detect_truncation(
        _response(finish_reason=reason, output_tokens=12), REVIEW_MAX_TOKENS
    )
    assert detected == reason


@pytest.mark.parametrize("reason", ["length", "LENGTH", "  Length  ", "max_tokens"])
def test_truncation_reason_is_case_and_whitespace_insensitive(reason):
    assert _detect_truncation(_response(finish_reason=reason), REVIEW_MAX_TOKENS)


async def test_truncation_raises_distinct_error_and_skips_the_retry():
    """A budget cut is the one failure the identical retry cannot fix."""
    with patch("app.subagents.code_reviewer.LLMClient") as mock_client_cls:
        client = mock_client_cls.return_value
        client.complete = AsyncMock(
            return_value=_response(
                content=_TRUNCATED_REVIEW_JSON,
                finish_reason="length",
                output_tokens=REVIEW_MAX_TOKENS,
            )
        )
        with pytest.raises(ReviewOutputTruncatedError) as exc_info:
            await analyze_diff(diff_text="diff --git a/app/db.py b/app/db.py")

    assert isinstance(exc_info.value, ReviewAnalysisError)
    assert "length" in str(exc_info.value)
    assert str(REVIEW_MAX_TOKENS) in str(exc_info.value)
    assert client.complete.call_count == 1, (
        "the retry re-requests the identical budget, so it truncates identically"
    )


# ---------------------------------------------------------------------------
# 2. Everything else is not truncation — the deny-by-default regression
# ---------------------------------------------------------------------------

#: Deliberate provider stops (OpenAI + Anthropic vocabularies), non-stop
#: terminations that are not budget cuts (``content_filter``, ``pause_turn``,
#: ``refusal``, ``function_call``, ``model_context_window_exceeded``), and
#: OpenAI-compatible gateway spellings this repo cannot enumerate in advance.
NON_TRUNCATION_REASONS = [
    "stop",
    "end_turn",
    "stop_sequence",
    "tool_calls",
    "tool_use",
    "refusal",
    "content_filter",
    "function_call",
    "pause_turn",
    "model_context_window_exceeded",
    "eos",
    "COMPLETED",
    "FINISH_REASON_UNSPECIFIED",
    "haunter_reason_invented_after_this_commit",
]


@pytest.mark.parametrize("reason", NON_TRUNCATION_REASONS)
def test_no_other_finish_reason_is_truncation(reason):
    """Known-benign, unknown, and gateway spellings alike are not truncation.

    Asserted at ``output_tokens == max_tokens`` deliberately: the provider's own
    word must win over the budget heuristic. A response that consumed the whole
    budget *and* reported ``stop``/``content_filter`` is complete, and treating
    it as a budget cut is the false positive that loses a publishable review.
    """
    response = _response(
        finish_reason=reason, output_tokens=REVIEW_MAX_TOKENS
    )
    assert _detect_truncation(response, REVIEW_MAX_TOKENS) is None


@pytest.mark.parametrize("reason", ["content_filter", "COMPLETED", "pause_turn"])
async def test_non_truncation_finish_reason_review_proceeds(reason):
    """End-to-end: an unlisted-but-benign reason must still publish a review."""
    result, instance = await _analyze_with(
        _response(finish_reason=reason, output_tokens=REVIEW_MAX_TOKENS)
    )

    assert result.output.risk_score == 42
    assert result.output.summary.startswith("One unhandled error path")
    assert instance.complete.call_count == 1


async def test_unknown_finish_reason_does_not_break_a_real_parse():
    """A first-parse failure on an unknown reason still gets its one retry."""
    bad = _response(
        content='{"risk_score": 150, "summary": "Score above the ceiling"}',
        finish_reason="haunter_reason_invented_after_this_commit",
    )
    good = _response(finish_reason="haunter_reason_invented_after_this_commit")

    with patch("app.subagents.code_reviewer.LLMClient") as mock_client_cls:
        instance = mock_client_cls.return_value
        instance.complete = AsyncMock(side_effect=[bad, good])
        result = await analyze_diff(diff_text="diff --git a/app/db.py b/app/db.py")

    assert result.output.risk_score == 42
    assert instance.complete.call_count == 2


def test_raw_anthropic_stop_reason_is_read_the_same_way():
    """``stop_reason`` (Anthropic's raw field) is judged as ``finish_reason``."""
    truncated = _response(
        finish_reason=None, stop_reason="max_tokens", output_tokens=12
    )
    assert _detect_truncation(truncated, REVIEW_MAX_TOKENS) == "max_tokens"

    complete = _response(
        finish_reason=None, stop_reason="pause_turn", output_tokens=REVIEW_MAX_TOKENS
    )
    assert _detect_truncation(complete, REVIEW_MAX_TOKENS) is None


# ---------------------------------------------------------------------------
# 3. Absence is unknown — never "ok", never a hard truncation verdict
# ---------------------------------------------------------------------------


def test_absent_reason_with_the_whole_budget_spent_is_still_a_truncation_signal():
    """``usage.output_tokens >= max_tokens`` remains the fallback evidence."""
    detected = _detect_truncation(
        _response(output_tokens=REVIEW_MAX_TOKENS), REVIEW_MAX_TOKENS
    )
    assert detected is not None
    assert "inferred" in detected


def test_absent_reason_with_unused_budget_is_not_truncation():
    assert _detect_truncation(_response(output_tokens=25), REVIEW_MAX_TOKENS) is None


async def test_absent_reason_at_the_budget_with_unparseable_output_is_truncated():
    """The suspected budget cut is honoured exactly when it changes the outcome.

    ``output_tokens == max_tokens`` on its own no longer fails the review before
    the payload is even looked at: a complete JSON object that happens to end on
    the boundary is indistinguishable from one that was cut, and pre-emptively
    throwing it away costs a publishable review for a cosmetic mislabel. It only
    becomes a truncation verdict when the payload in fact fails to parse, which
    is the one case where "truncated" is the actionable diagnosis.
    """
    response = _response(
        content=_TRUNCATED_REVIEW_JSON, output_tokens=REVIEW_MAX_TOKENS
    )

    with patch("app.subagents.code_reviewer.LLMClient") as mock_client_cls:
        client = mock_client_cls.return_value
        client.complete = AsyncMock(return_value=response)
        with pytest.raises(ReviewOutputTruncatedError) as exc_info:
            await analyze_diff(diff_text="diff --git a/app/db.py b/app/db.py")

    assert "inferred" in str(exc_info.value)
    assert client.complete.call_count == 1, (
        "a suspected budget cut must not burn the budget-invariant retry"
    )


async def test_absent_reason_at_the_budget_with_complete_output_succeeds():
    """The false positive this inversion exists to prevent: review is kept."""
    response = _response(output_tokens=REVIEW_MAX_TOKENS)

    result, instance = await _analyze_with(response)

    assert result.output.risk_score == 42
    assert result.output.summary.startswith("One unhandled error path")
    assert instance.complete.call_count == 1


async def test_absent_reason_with_unused_budget_succeeds():
    result, _ = await _analyze_with(_response(output_tokens=90))
    assert result.output.risk_score == 42


# ---------------------------------------------------------------------------
# 4. S1 — a suggestion block must never contain a redaction placeholder
# ---------------------------------------------------------------------------

#: A fix that legitimately carries a Stripe test key, which the repo's canonical
#: secret patterns match. Redaction is correct here; an *applyable* suggestion
#: block is not — one click commits ``[REDACTED_STRIPE_KEY]`` into the caller's
#: source.
PATCH_WITH_SECRET_LITERAL = 'client = StripeClient("sk_test_abcdefghij1234567890")'


def _finding(patch: str | None) -> ReviewFinding:
    return ReviewFinding(
        file_path="app/payments.py",
        line_start=42,
        line_end=42,
        category="security",
        severity="high",
        critique="The Stripe key is embedded in the source instead of the vault.",
        suggested_patch=patch,
    )


def test_redaction_inside_a_suggestion_block_downgrades_to_plain_code():
    finding = _finding(PATCH_WITH_SECRET_LITERAL)
    body = format_github_suggestion(finding)

    assert "[REDACTED_STRIPE_KEY]" in body, (
        "the placeholder must still be visible — redaction, not deletion, is the "
        "safe outcome for a secret-shaped literal"
    )
    assert "```suggestion" not in body, (
        "a suggestion block containing a redaction placeholder is one click from "
        "committing the placeholder into the caller's source"
    )
    assert "StripeClient(" in body, "the finding's proposed fix must still ship"
    assert "**" in body, "the critique header must survive the downgrade"


def test_a_clean_patch_still_produces_an_applyable_suggestion():
    body = format_github_suggestion(_finding("client = StripeClient(STRIPE_KEY)"))
    assert "```suggestion" in body
    assert "[REDACTED" not in body


def test_a_finding_without_a_patch_is_unchanged():
    body = format_github_suggestion(_finding(None))
    assert "```suggestion" not in body
    assert "**" in body


# ---------------------------------------------------------------------------
# 5. S7 — every model-authored string on a finding is actually bounded
# ---------------------------------------------------------------------------


def test_file_path_is_bounded_to_the_repo_path_limit():
    ceiling = code_reviewer.MAX_FILE_PATH_CHARS
    finding = ReviewFinding(
        file_path="app/" + ("n" * (ceiling * 4)),
        line_start=1,
        line_end=1,
        category="logic",
        severity="low",
        critique="Unbounded path was persisted into the findings JSONB.",
        suggested_patch=None,
    )
    assert len(finding.file_path) <= ceiling


# ---------------------------------------------------------------------------
# 6. REJECT #2 — the 422 body reaches the log through ``sanitize_log_value``
# ---------------------------------------------------------------------------

async def test_422_body_is_sanitised_in_the_log(caplog):
    """GitHub's 422 body is upstream-controlled text; the log line must not be.

    It carries a newline (which forges a second log record), a bidi override
    (which hides text from the operator reading the line while leaving it
    visible in the raw bytes), a token-shaped literal (which persists a
    credential outside every rotation path), and 4 000 characters of flooding.
    """
    hostile_body = (
        "Validation Failed\n"
        "gh\u202epwned=1\u202c "
        "github_token=ghp_" + "A" * 30 + "\n"
    ) + ("flood " * 900)
    response = httpx.Response(
        422,
        text=hostile_body,
        request=httpx.Request(
            "POST", f"{_BASE_URL}/repos/o/r/pulls/7/reviews"
        ),
    )

    with caplog.at_level(logging.ERROR, logger="app.github_client"):
        with patch.object(
            httpx.AsyncClient, "post", new=AsyncMock(return_value=response)
        ):
            with pytest.raises(GitHubUnprocessableEntityError):
                await create_pr_review(
                    owner="o",
                    repo="r",
                    pr_number=7,
                    commit_sha="a" * 40,
                    body="looks fine",
                )

    record = next(
        entry for entry in caplog.records if "rejected the review payload" in entry.getMessage()
    )
    message = record.getMessage()
    assert "ghp_" + "A" * 30 not in message
    assert "[REDACTED" in message
    assert "\u202e" not in message
    assert "\u202c" not in message
    assert "\nValidation Failed" not in message
    assert message.count("\n") == 0, "a newline forges a second log record"
    assert len(message) <= MAX_LOG_VALUE_CHARS + len(
        "GitHub API rejected the review payload for o/r PR #7: "
    ), (
        f"the body must be bounded by MAX_LOG_VALUE_CHARS ({MAX_LOG_VALUE_CHARS}), "
        f"not merely redacted: got {len(message)} chars"
    )


# ---------------------------------------------------------------------------
# 7. S5 — the resolve log names the thread, not its length
# ---------------------------------------------------------------------------


async def test_resolve_failure_logs_the_thread_id_not_its_length(caplog):
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"errors": [{"message": "Could not resolve to a node"}]}
        )

    with caplog.at_level(logging.WARNING, logger="app.github_client"):
        with patch(
            "app.github_client.httpx.AsyncClient",
            side_effect=lambda *a, **k: _REAL_ASYNC_CLIENT(
                transport=httpx.MockTransport(_handler)
            ),
        ):
            resolved = await resolve_review_threads("o", "r", ["PRRT_deadbeef"])

    assert resolved == 0
    message = next(
        entry.getMessage()
        for entry in caplog.records
        if "resolve_review_threads graphql_errors" in entry.getMessage()
    )
    assert "thread_id=PRRT_deadbeef" in message
    assert "thread_id=14" not in message, (
        "len(thread_id) was logged instead of the id; the operator cannot act on "
        "a length"
    )


# ---------------------------------------------------------------------------
# 8. S6 — the GraphQL helpers are byte-bounded like every other read here
# ---------------------------------------------------------------------------


def _graphql_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _client_factory(handler):
    return lambda *a, **k: _REAL_ASYNC_CLIENT(transport=_graphql_transport(handler))


@pytest.mark.parametrize("with_content_length", [True, False])
async def test_oversized_graphql_body_is_refused_not_buffered(with_content_length):
    """A GraphQL error body is unbounded upstream text, exactly like a diff.

    ``_bounded_get``/``MAX_API_RESPONSE_BYTES`` is the module's own convention for
    every other GitHub read; the two GraphQL helpers used a bare
    ``client.post``, so this is the same protection they were missing.

    Parametrised on Content-Length because the streamed total is the only number
    a lying or absent header cannot understate — a chunked response has to be
    caught by the running total alone.
    """

    def _filler() -> str:
        return "A" * (MAX_API_RESPONSE_BYTES + 1_000)

    def _handler(request: httpx.Request) -> httpx.Response:
        # A *valid* payload in each helper's own shape, padded past the cap. An
        # unbounded read parses these and returns real data, so the assertions
        # below can only pass because the ceiling was enforced — not because the
        # body happened to be unparseable, which is the vacuous way this test
        # could have passed.
        query = str(json.loads(request.content).get("query") or "")
        if "resolveReviewThread" in query:
            body = json.dumps(
                {
                    "data": {
                        "resolveReviewThread": {"thread": {"isResolved": True}},
                        "padding": _filler(),
                    }
                }
            ).encode()
        else:
            body = json.dumps(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                    "nodes": [{"id": "PRRT_x"}],
                                }
                            }
                        },
                        "padding": _filler(),
                    }
                }
            ).encode()
        if with_content_length:
            return httpx.Response(
                200, content=body, headers={"content-length": str(len(body))}
            )
        return httpx.Response(200, content=body)

    with patch(
        "app.github_client.httpx.AsyncClient",
        side_effect=_client_factory(_handler),
    ):
        assert await fetch_review_threads("o", "r", 7) == []
        assert await resolve_review_threads("o", "r", ["PRRT_x"]) == 0


async def test_graphql_limit_error_does_not_escape_either_helper():
    """Both helpers promise to raise nothing; the limit error must not escape."""

    async def _oversized(client, url, **kwargs):
        raise GitHubResponseLimitError("GitHub response exceeded 100 bytes")

    with patch(
        "app.github_client.httpx.AsyncClient",
        side_effect=_client_factory(lambda request: httpx.Response(200, json={})),
    ), patch("app.github_client._bounded_post", new=_oversized):
        assert await fetch_review_threads("o", "r", 7) == []
        assert await resolve_review_threads("o", "r", ["PRRT_x"]) == 0


# ---------------------------------------------------------------------------
# 9. S3/S4 — the dead bot-identity surface is gone, not merely unreferenced
# ---------------------------------------------------------------------------


def test_dead_bot_identity_cache_surface_is_removed():
    from app import github_client

    assert not hasattr(github_client, "clear_bot_identity_cache"), (
        "a cache-invalidation helper nothing calls is dead code whose docstring "
        "claims a purpose nothing uses"
    )
    assert "force_refresh" not in inspect.signature(
        github_client.fetch_bot_identity
    ).parameters, (
        "a keyword-only escape hatch no caller passes, on a cache already keyed "
        "per credential so a rotated token cannot read a stale entry"
    )
