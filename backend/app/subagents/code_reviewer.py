"""
Autonomous Push-Level Code Review Subagent — Feature 1.

Performs deep, AST-grounded code reviews on commit diffs and pull requests.
Evaluates changes across 4 core engineering dimensions:
1. Security & Vulnerability (OWASP, SQLi, SSRF, path traversal, secrets).
2. Logic & Edge Cases (null dereferences, race conditions, unhandled errors).
3. Performance & Resources (N+1 queries, unbounded memory, unclosed resources).
4. API Backward Compatibility (breaking exported symbols and contracts).

Strict invariants:
- Zero cosmetic/formatting nitpicks (whitespace, variable renames, styling).
- Strict Pydantic output schema parsing with 1 retry on ValidationError.
- Generates GitHub-compatible suggestion blocks for inline PR comments.
- Tracks input/output tokens and latency.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import time
from typing import Literal, NoReturn, Optional
import uuid

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import LLMClient
from app.llm.prompts.audit_prompts import (
    MAX_GITHUB_COMMENT_CHARS,
    MAX_INLINE_FIELD_CHARS,
    MAX_SUGGESTED_FIX_CHARS,
    sanitize_output_markdown,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------

ReviewCategory = Literal["security", "logic", "performance", "api_compatibility"]
ReviewSeverity = Literal["low", "medium", "high", "critical"]

#: Output-token budget requested per review attempt.
#:
#: Every adapter reports ``finish_reason`` verbatim, so a truncation is usually
#: directly observable. When it is not, ``usage.output_tokens`` is the fallback
#: evidence — see :func:`_detect_truncation` for why that fallback is a
#: *suspicion* to be confirmed by parsing rather than a verdict of its own.
REVIEW_MAX_TOKENS = 4096

#: Provider finish/stop reasons that are **positive evidence the output budget
#: ran out**. Anything outside this set is not truncation — see
#: :func:`_detect_truncation` for why that is the only safe default.
#:
#: Verified against the vocabularies this pipeline actually talks to:
#:
#: * ``length`` — OpenAI-compatible chat completions. Confirmed from the
#:   installed SDK's own enum: ``openai.types.chat.chat_completion.Choice``'s
#:   ``finish_reason`` is ``Literal["stop", "length", "tool_calls",
#:   "content_filter", "function_call"]``, i.e. exactly one of its five members
#:   is a truncation. ``app/llm/openai.py:124``, ``groq.py:114`` and
#:   ``opencode_zen.py:127`` all surface that field verbatim.
#: * ``max_tokens`` — Anthropic's native spelling. ``app/llm/anthropic.py:258``
#:   reads ``data["stop_reason"]`` and reports it under ``finish_reason``;
#:   ``max_tokens`` is the budget-cut member of Anthropic's ``stop_reason``.
#: * ``max_output_tokens`` — the OpenAI Responses API / Gemini-style spelling,
#:   reachable through an OpenAI-compatible gateway, which the Zen provider is.
#:
#: Deliberately *not* here: ``content_filter`` and ``function_call`` (OpenAI),
#: ``pause_turn``, ``refusal`` and ``model_context_window_exceeded`` (Anthropic),
#: and any gateway spelling (``eos``, ``COMPLETED``,
#: ``FINISH_REASON_UNSPECIFIED``). None of them means "the budget ran out", and
#: the Zen vocabulary is not knowable from this repo.
TRUNCATION_FINISH_REASONS = frozenset({"length", "max_tokens", "max_output_tokens"})

#: Ceiling on a model-authored ``file_path``. Matches the repo's own
#: repo-relative-path limit (``_validated_repo_relative_path`` in
#: ``app/llm/prompts/audit_prompts.py``, which rejects anything over 512 chars),
#: so the bound here and the validation at the render boundary agree instead of
#: disagreeing about the same string.
MAX_FILE_PATH_CHARS = 512


def _bound_model_text(value: object, maximum: int) -> object:
    """Clamp a model-authored string, redacting and normalizing on the way.

    Uses the repo's canonical structured-output boundary
    (:func:`sanitize_output_markdown`) so redaction, control-character stripping
    and the truncation marker are identical to every other place a model's prose
    is bounded. ``MAX_REPORT_CHARS``-class markdown is deliberately not escaped
    here: these fields end up inside fenced code blocks, and HTML-escaping a code
    snippet corrupts it.
    """
    if not isinstance(value, str):
        return value
    return sanitize_output_markdown(value, maximum)


class ReviewFinding(BaseModel):
    """An individual actionable review finding tied to specific file lines.

    Every model-authored string — ``file_path`` included — is bounded, and bounded
    by *truncation* rather than by rejection. These bounds are not cosmetic: the
    fields are concatenated verbatim into a GitHub review body, and that body is
    one atomic request together with every inline comment, so a single oversized
    field 422s the whole review and discards every other finding with it.
    Rejecting oversized output instead would turn a length problem into a parse
    failure — the retry asks the same model for the same thing and fails
    identically — so the value is clamped and the finding still ships.
    """

    file_path: str = Field(
        ...,
        min_length=1,
        max_length=MAX_FILE_PATH_CHARS,
        description="Relative path of the touched file",
    )
    line_start: int = Field(
        ..., ge=1, description="Starting line of the finding (1-indexed)"
    )
    line_end: int = Field(
        ..., ge=1, description="Ending line of the finding (1-indexed)"
    )
    category: ReviewCategory
    severity: ReviewSeverity
    critique: str = Field(
        ...,
        min_length=5,
        description="Specific critique explaining the failure mechanism",
    )
    suggested_patch: Optional[str] = Field(
        None, description="Replacement code snippet for GitHub suggestion block"
    )

    @field_validator("file_path", mode="before")
    @classmethod
    def _bound_file_path(cls, value: object) -> object:
        """Clamp a model-authored path to the repo's own path ceiling.

        Clamped rather than rejected, like every other field here. A clamped
        path is a path that will not match any diff hunk, so the finding is
        dropped and counted as ungrounded by the publish layer — a lost finding,
        which is the correct outcome. Rejecting instead would fail the whole
        findings object over one over-long string and take every other finding
        in it with it.
        """
        if not isinstance(value, str) or len(value) <= MAX_FILE_PATH_CHARS:
            return value
        return value[:MAX_FILE_PATH_CHARS]

    @field_validator("critique", mode="before")
    @classmethod
    def _bound_critique(cls, value: object) -> object:
        return _bound_model_text(value, MAX_INLINE_FIELD_CHARS)

    @field_validator("suggested_patch", mode="before")
    @classmethod
    def _bound_suggested_patch(cls, value: object) -> object:
        if value is None:
            return None
        return _bound_model_text(value, MAX_SUGGESTED_FIX_CHARS)

    @model_validator(mode="after")
    def validate_lines(self) -> "ReviewFinding":
        if self.line_end < self.line_start:
            self.line_end = self.line_start
        return self


class CodeReviewOutput(BaseModel):
    """Structured LLM output for code review."""

    risk_score: int = Field(
        ge=0, le=100, description="Overall risk score from 0 (safe) to 100 (critical)"
    )
    summary: str = Field(..., min_length=5, description="Executive review summary")
    findings: list[ReviewFinding] = Field(default_factory=list)

    @field_validator("summary", mode="before")
    @classmethod
    def _bound_summary(cls, value: object) -> object:
        return _bound_model_text(value, MAX_GITHUB_COMMENT_CHARS)


@dataclass
class ReviewResult:
    """Full execution result including parsed output and telemetry metrics."""

    output: CodeReviewOutput
    input_tokens: int
    output_tokens: int
    latency_ms: int


# ---------------------------------------------------------------------------
# Markdown / GitHub Suggestion Formatting
# ---------------------------------------------------------------------------

_FENCE_OPEN_RE: re.Pattern[str] = re.compile(
    r"^\s*```(?:[a-zA-Z0-9_+-]*)\s*$", re.MULTILINE
)
_FENCE_CLOSE_RE: re.Pattern[str] = re.compile(r"(?:\r?\n)?\s*```\s*$")


def _strip_markdown_fences(content: str) -> str:
    """Strip leading/trailing markdown fences and non-JSON preamble from LLM responses."""
    s = content.strip()
    if s.startswith("```"):
        s = _FENCE_OPEN_RE.sub("", s, count=1)
        if s.rstrip().endswith("```"):
            s = _FENCE_CLOSE_RE.sub("", s.rstrip(), count=1)
        s = s.strip()

    if not s.startswith("{"):
        first_brace = s.find("{")
        last_brace = s.rfind("}")
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            s = s[first_brace : last_brace + 1]

    return s


#: Every placeholder ``redact_sensitive_text`` can emit: the named forms
#: (``[REDACTED_API_KEY]``) and the bare assignment form (``[REDACTED]``).
_REDACTION_PLACEHOLDER_RE: re.Pattern[str] = re.compile(
    r"\[REDACTED(?:_[A-Z0-9_]+)?\]"
)


def format_github_suggestion(finding: ReviewFinding) -> str:
    """
    Format a ReviewFinding into a GitHub comment markdown string.
    Embeds a ```suggestion ... ``` block if suggested_patch is provided.
    Strips any existing markdown code fences from the patch to prevent nested broken formatting.

    Every field is clamped again here, not just at parse time: the finding may
    have been loaded out of ``code_reviews.findings`` (JSONB written by an older
    build, or by a caller that bypassed the schema), and a suggestion block that
    GitHub rejects takes down every other inline comment in the same atomic
    request with it.

    Redaction and applyability are in direct conflict inside a ``suggestion``
    block, and safety wins. ``suggested_patch`` goes through
    ``sanitize_output_markdown``, which applies the repo's canonical
    ``redact_sensitive_text`` — so a fix that legitimately contains a
    token-shaped literal (a Stripe test key in a fixture, a placeholder in a
    redaction test) comes back carrying ``[REDACTED_STRIPE_KEY]``. A
    ``suggestion`` block is *machine-applied*: one click commits its contents
    verbatim into the caller's source. Publishing one would be a supply-chain
    hazard the reviewer never sees coming, and a silently wrong fix is worse
    than no fix.

    So when redaction actually fired, the patch is still shown — redacted, as
    every other part of the comment is — but in a plain fenced block the reader
    must copy by hand, with the reason stated inline. The direction of the
    failure is deliberate: downgrade when in doubt.
    """
    critique = sanitize_output_markdown(finding.critique, MAX_INLINE_FIELD_CHARS)
    header = (
        f"**[{finding.category.upper()}] [{finding.severity.upper()}]**: {critique}"
    )
    if finding.suggested_patch is not None and finding.suggested_patch.strip():
        patch_clean = sanitize_output_markdown(
            finding.suggested_patch, MAX_SUGGESTED_FIX_CHARS
        ).strip("\r\n")
        # Strip outer code fences if the model wrapped its patch in ```suggestion or ```lang
        if patch_clean.startswith("```"):
            lines = patch_clean.splitlines()
            if (
                len(lines) >= 2
                and lines[0].startswith("```")
                and lines[-1].strip() == "```"
            ):
                patch_clean = "\n".join(lines[1:-1]).strip("\r\n")
            elif lines[0].startswith("```"):
                patch_clean = "\n".join(lines[1:]).strip("\r\n")
        if _REDACTION_PLACEHOLDER_RE.search(patch_clean):
            # A bare ``` is not enough on its own: the patch may contain a fence
            # run of its own, which would close the block early and dump the
            # remainder into the comment as prose. Size the fence past the
            # longest run present, exactly as audit_prompts._fence_for does.
            longest_run = max(
                (len(run) for run in re.findall(r"`+", patch_clean)), default=0
            )
            fence = "`" * max(3, longest_run + 1)
            return (
                f"{header}\n\n"
                "Not offered as an applyable suggestion: a value in the proposed "
                "change matched Haunter's secret-redaction patterns and was "
                "replaced with a placeholder below. Applying this block as-is "
                f"would commit the placeholder.\n\n{fence}\n{patch_clean}\n{fence}"
            )
        return f"{header}\n\n```suggestion\n{patch_clean}\n```"
    return header


def bound_github_body(body: str) -> str:
    """Clamp a rendered review/commit-comment body to what GitHub will accept.

    ``POST /pulls/{n}/reviews`` and ``POST /commits/{sha}/comments`` reject a body
    past 65 536 characters with HTTP 422. Bounding each field is not enough: the
    per-finding blocks are concatenated, so the total is a separate quantity with
    its own limit and needs its own clamp at the publish boundary.
    """
    return sanitize_output_markdown(str(body or ""), MAX_GITHUB_COMMENT_CHARS)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are Haunter's Autonomous Code Review Sentinel.
Your mission is to perform deep, AST-grounded, production-grade code reviews on Git diffs.
Evaluate the diff strictly along 4 core engineering dimensions:
1. Security & Vulnerability (OWASP Top 10, SQL injection, SSRF, command injection, path traversal, hardcoded secrets/tokens, insecure deserialization).
2. Logic & Edge Cases (null/undefined dereferences, off-by-one errors, race conditions, unhandled error states, corrupted state transitions).
3. Performance & Resources (N+1 queries, unbounded memory allocations, missing connection/file handle cleanup, blocking I/O on async event loops).
4. API Backward Compatibility (breaking changes to public/exported function signatures, models, or schema contracts).

STRICT DIRECTIVES:
- DISALLOW all cosmetic, stylistic, or formatting nitpicks (whitespace, indentation, naming preference, docstring wording, trailing commas). Only flag actionable bugs, vulnerabilities, performance degradations, or breaking changes.
- If there are no issues in the diff, return risk_score=0, summary="Code changes look clean and well-structured.", and findings=[].
- Calculate risk_score between 0 and 100:
  - 0-30: Safe, low risk.
  - 31-70: Moderate risk; edge cases or potential bottlenecks.
  - 71-100: High / Critical risk; severe vulnerabilities, critical logic flaws, or breaking changes.
- For findings with actionable fixes, provide `suggested_patch` as a replacement string that will drop directly into a GitHub ```suggestion block replacing lines line_start through line_end.
- Output MUST be valid JSON adhering exactly to this schema:
{
  "risk_score": <int between 0 and 100>,
  "summary": "<concise executive review summary>",
  "findings": [
    {
      "file_path": "<relative path>",
      "line_start": <int >= 1>,
      "line_end": <int >= 1>,
      "category": "security" | "logic" | "performance" | "api_compatibility",
      "severity": "low" | "medium" | "high" | "critical",
      "critique": "<specific critique explaining failure mechanism>",
      "suggested_patch": "<replacement code or null>"
    }
  ]
}"""


def _build_review_messages(
    diff_text: str,
    repo_context: str = "",
    validation_error_context: Optional[str] = None,
) -> list[dict[str, str]]:
    user_content = (
        f"Repository Context:\n{repo_context}\n\nGit Diff to Review:\n{diff_text}"
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
    if validation_error_context:
        messages.append(
            {
                "role": "assistant",
                "content": "(prior response failed validation)",
            }
        )
        messages.append(
            {
                "role": "user",
                "content": (
                    f"Your previous response failed JSON schema validation: {validation_error_context}\n"
                    "Please correct the output. Return ONLY the strict JSON object."
                ),
            }
        )
    return messages


# ---------------------------------------------------------------------------
# Subagent Runner
# ---------------------------------------------------------------------------


class ReviewAnalysisError(Exception):
    """Raised when review generation fails completely after retries."""


class ReviewOutputTruncatedError(ReviewAnalysisError):
    """Raised when the model stopped mid-object because it hit the token budget.

    Distinct from :class:`ReviewAnalysisError` because the remedy is different:
    the output was well-formed until the budget ran out, so asking the same model
    the same question with the same budget truncates at the same place. The
    budget-invariant retry is therefore skipped for this error and only for this
    error — retrying a genuine schema violation is exactly what the retry is for.
    """


def _raise_truncated(
    reason: str,
    max_tokens: int,
    *,
    attempt: str,
    prefix: str = "",
    cause: Optional[BaseException] = None,
) -> NoReturn:
    """Log and raise :class:`ReviewOutputTruncatedError` for one attempt.

    ``NoReturn`` rather than ``None`` so a caller cannot read past the call: both
    truncation sites sit inside an ``except`` handler where falling through would
    silently continue a path the code has just decided is unrecoverable.

    ``prefix`` distinguishes the retry's message from the first attempt's; the
    operator reading the log needs to know which call produced the cut, because
    whether the *first* attempt also came back truncated is what says whether the
    budget is too small for this diff at all.
    """
    logger.error(
        "code_reviewer: %soutput truncated (%s, reason=%s max_tokens=%d) — "
        "skipping the budget-invariant retry",
        prefix,
        attempt,
        reason,
        max_tokens,
    )
    raise ReviewOutputTruncatedError(
        f"Code reviewer {prefix}output was truncated by the provider "
        f"(finish_reason={reason}, max_tokens={max_tokens}). "
        "The review JSON is incomplete."
    ) from cause


def _reported_finish_reason(response: dict) -> Optional[str]:
    """The provider's own reason for ending generation, normalised, or ``None``.

    ``stop_reason`` is Anthropic's raw field name; adapters normalise it to
    ``finish_reason``, and reading it too means a raw provider payload handed
    straight to this function is judged the same way as a normalised one.

    ``None`` means *unknown* — the field was absent, blank, or not a string. It
    never means "ok": an absent reason is the case the budget heuristic exists
    for.
    """
    for key in ("finish_reason", "stop_reason"):
        value = response.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None


def _budget_exhausted(response: dict, max_tokens: int) -> bool:
    """True when the response consumed the entire requested output budget.

    The only signal available from a provider that omits ``finish_reason``. It
    is a *suspicion*, not a verdict: a generation that stops exactly on the
    budget boundary is indistinguishable from one that was cut there, and the
    two are only separable by parsing the payload.
    """
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return False
    output_tokens = usage.get("output_tokens")
    if isinstance(output_tokens, int) and not isinstance(output_tokens, bool):
        return output_tokens >= max_tokens
    return False


def _provider_reported_truncation(response: dict) -> bool:
    """True when the provider itself named a reason in :data:`TRUNCATION_FINISH_REASONS`.

    The distinction the caller needs, and the reason ``_detect_truncation``
    cannot express on its own: a *reported* cut is a verdict that fails the
    review before the payload is even looked at, while an *inferred* one is only
    a suspicion that has to be confirmed by the parse failing.
    """
    reason = _reported_finish_reason(response)
    return reason is not None and reason in TRUNCATION_FINISH_REASONS


def _detect_truncation(response: dict, max_tokens: int) -> Optional[str]:
    """Return the reason this response looks truncated, or ``None``.

    Truncation is detected by *positive* evidence only — the provider naming a
    member of :data:`TRUNCATION_FINISH_REASONS`, or the budget being spent to the
    last token. An absent or unrecognised ``finish_reason`` is **unknown**, and
    unknown is never truncation.

    That default is the whole point. The previous policy asked the inverse
    question — "is this reason one of the strings I recognise as a clean stop?"
    — and called everything else truncated. It was unfixable by editing the list:
    the default provider is OpenCode Zen, which fronts multiple upstreams, so its
    vocabulary is not knowable from this repo. One unlisted-but-benign value
    (``content_filter``, ``pause_turn``, a gateway's ``COMPLETED``) turned a
    publishable review into a terminal error labelled "truncated", with no retry
    and a diagnosis that pointed at the wrong cause. An allow-list of *truncation*
    reasons cannot do that: a value nobody anticipated cannot be mistaken for a
    budget cut, it just falls through to normal parsing.

    When the provider does speak, its word is final — including against the
    heuristic. A response that reports ``stop`` or ``content_filter`` while
    spending the entire budget is complete, and the budget being spent is
    incidental.

    Returns one of:

    * the normalised reason, when the provider named a truncation reason;
    * ``"length (inferred from usage.output_tokens)"``, when no reason was
      reported and the whole budget was spent — the caller's cue that this is
      inferred rather than confirmed;
    * ``None``, when there is no evidence of a budget cut.
    """
    reason = _reported_finish_reason(response)
    if reason is not None:
        return reason if reason in TRUNCATION_FINISH_REASONS else None
    if _budget_exhausted(response, max_tokens):
        return "length (inferred from usage.output_tokens)"
    return None


async def analyze_diff(
    diff_text: str,
    repo_context: str = "",
    db: Optional[AsyncSession] = None,
    repo_id: Optional[uuid.UUID] = None,
) -> ReviewResult:
    """
    Execute autonomous code review on a unified diff.
    Invokes LLMClient, parses CodeReviewOutput with 1 retry on ValidationError,
    and returns ReviewResult with telemetry.

    A response the provider *reports* as truncated raises
    :class:`ReviewOutputTruncatedError` without the retry: the retry
    re-requests the identical budget, so it truncates identically and the run
    would end as a generic parse failure that says nothing about the real cause.

    A truncation merely *suspected* from the token count is not a verdict. It
    only becomes one when the payload in fact fails to parse — which is the one
    situation where "truncated" and "schema violation" are the same observable
    outcome and the truncated label is the actionable one. A complete JSON
    object that happens to end on the budget boundary is a review worth
    publishing, and discarding it costs a publishable review for a cosmetic
    mislabel.
    """
    # Short-circuit if diff is empty or whitespace
    if not diff_text or not diff_text.strip():
        return ReviewResult(
            output=CodeReviewOutput(
                risk_score=0,
                summary="No code changes detected in diff.",
                findings=[],
            ),
            input_tokens=0,
            output_tokens=0,
            latency_ms=0,
        )

    llm = LLMClient(timeout=60.0)
    start_time = time.monotonic()

    messages = _build_review_messages(diff_text, repo_context)
    response = await llm.complete(
        messages=messages,
        db=db,
        repo_id=repo_id,
        max_tokens=REVIEW_MAX_TOKENS,
    )

    content = response.get("content") or ""
    cleaned_json = _strip_markdown_fences(content)

    u1 = response.get("usage") or {}
    total_input = u1.get("input_tokens", 0)
    total_output = u1.get("output_tokens", 0)

    truncation = _detect_truncation(response, REVIEW_MAX_TOKENS)
    if _provider_reported_truncation(response):
        # The provider named a truncation reason. That is a verdict, not a
        # suspicion: the review cannot be trusted and the retry cannot change it.
        _raise_truncated(truncation, REVIEW_MAX_TOKENS, attempt="first attempt")

    try:
        output = CodeReviewOutput.model_validate_json(cleaned_json)
        latency = int((time.monotonic() - start_time) * 1000)
        return ReviewResult(
            output=output,
            input_tokens=total_input,
            output_tokens=total_output,
            latency_ms=latency,
        )
    except ValidationError as first_err:
        # A suspected budget cut is confirmed here, by the parse failing. Until
        # this point the suspicion was worth logging but not worth losing a
        # review over.
        if truncation is not None:
            _raise_truncated(
                truncation,
                REVIEW_MAX_TOKENS,
                attempt="first attempt",
                cause=first_err,
            )
        err_msg = str(first_err)
        logger.warning(
            "code_reviewer: first parse failed (%s) — retrying with error context",
            err_msg,
        )

    # Retry once with error feedback
    retry_messages = _build_review_messages(
        diff_text, repo_context, validation_error_context=err_msg
    )
    retry_response = await llm.complete(
        messages=retry_messages,
        db=db,
        repo_id=repo_id,
        max_tokens=REVIEW_MAX_TOKENS,
    )
    retry_content = retry_response.get("content") or ""
    retry_cleaned = _strip_markdown_fences(retry_content)

    u2 = retry_response.get("usage") or {}
    total_input += u2.get("input_tokens", 0)
    total_output += u2.get("output_tokens", 0)

    retry_truncation = _detect_truncation(retry_response, REVIEW_MAX_TOKENS)
    if _provider_reported_truncation(retry_response):
        _raise_truncated(
            retry_truncation, REVIEW_MAX_TOKENS, attempt="retry", prefix="retry "
        )

    try:
        output = CodeReviewOutput.model_validate_json(retry_cleaned)
        latency = int((time.monotonic() - start_time) * 1000)
        return ReviewResult(
            output=output,
            input_tokens=total_input,
            output_tokens=total_output,
            latency_ms=latency,
        )
    except ValidationError as second_err:
        if retry_truncation is not None:
            _raise_truncated(
                retry_truncation,
                REVIEW_MAX_TOKENS,
                attempt="retry",
                prefix="retry ",
                cause=second_err,
            )
        logger.error("code_reviewer: retry also failed validation: %s", second_err)
        raise ReviewAnalysisError(
            f"Code reviewer output failed validation on both attempts. Details: {second_err}"
        ) from second_err
