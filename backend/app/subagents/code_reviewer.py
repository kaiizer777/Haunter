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
import json
import logging
import re
import time
from typing import Any, Literal, Optional
import uuid

from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import LLMClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------

ReviewCategory = Literal["security", "logic", "performance", "api_compatibility"]
ReviewSeverity = Literal["low", "medium", "high", "critical"]


class ReviewFinding(BaseModel):
    """An individual actionable review finding tied to specific file lines."""

    file_path: str = Field(..., min_length=1, description="Relative path of the touched file")
    line_start: int = Field(..., ge=1, description="Starting line of the finding (1-indexed)")
    line_end: int = Field(..., ge=1, description="Ending line of the finding (1-indexed)")
    category: ReviewCategory
    severity: ReviewSeverity
    critique: str = Field(..., min_length=5, description="Specific critique explaining the failure mechanism")
    suggested_patch: Optional[str] = Field(
        None, description="Replacement code snippet for GitHub suggestion block"
    )

    @model_validator(mode="after")
    def validate_lines(self) -> "ReviewFinding":
        if self.line_end < self.line_start:
            self.line_end = self.line_start
        return self


class CodeReviewOutput(BaseModel):
    """Structured LLM output for code review."""

    risk_score: int = Field(ge=0, le=100, description="Overall risk score from 0 (safe) to 100 (critical)")
    summary: str = Field(..., min_length=5, description="Executive review summary")
    findings: list[ReviewFinding] = Field(default_factory=list)


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


def format_github_suggestion(finding: ReviewFinding) -> str:
    """
    Format a ReviewFinding into a GitHub comment markdown string.
    Embeds a ```suggestion ... ``` block if suggested_patch is provided.
    Strips any existing markdown code fences from the patch to prevent nested broken formatting.
    """
    header = f"**[{finding.category.upper()}] [{finding.severity.upper()}]**: {finding.critique}"
    if finding.suggested_patch is not None and finding.suggested_patch.strip():
        patch_clean = finding.suggested_patch.strip("\r\n")
        # Strip outer code fences if the model wrapped its patch in ```suggestion or ```lang
        if patch_clean.startswith("```"):
            lines = patch_clean.splitlines()
            if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].strip() == "```":
                patch_clean = "\n".join(lines[1:-1]).strip("\r\n")
            elif lines[0].startswith("```"):
                patch_clean = "\n".join(lines[1:]).strip("\r\n")
        return f"{header}\n\n```suggestion\n{patch_clean}\n```"
    return header


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
    user_content = f"Repository Context:\n{repo_context}\n\nGit Diff to Review:\n{diff_text}"
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
    if validation_error_context:
        messages.append({
            "role": "assistant",
            "content": "(prior response failed validation)",
        })
        messages.append({
            "role": "user",
            "content": (
                f"Your previous response failed JSON schema validation: {validation_error_context}\n"
                "Please correct the output. Return ONLY the strict JSON object."
            ),
        })
    return messages


# ---------------------------------------------------------------------------
# Subagent Runner
# ---------------------------------------------------------------------------


class ReviewAnalysisError(Exception):
    """Raised when review generation fails completely after retries."""


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
        max_tokens=4096,
    )

    content = response.get("content") or ""
    cleaned_json = _strip_markdown_fences(content)

    u1 = response.get("usage") or {}
    total_input = u1.get("input_tokens", 0)
    total_output = u1.get("output_tokens", 0)

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
        err_msg = str(first_err)
        logger.warning("code_reviewer: first parse failed (%s) — retrying with error context", err_msg)

    # Retry once with error feedback
    retry_messages = _build_review_messages(
        diff_text, repo_context, validation_error_context=err_msg
    )
    retry_response = await llm.complete(
        messages=retry_messages,
        db=db,
        repo_id=repo_id,
        max_tokens=4096,
    )
    retry_content = retry_response.get("content") or ""
    retry_cleaned = _strip_markdown_fences(retry_content)

    u2 = retry_response.get("usage") or {}
    total_input += u2.get("input_tokens", 0)
    total_output += u2.get("output_tokens", 0)

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
        logger.error("code_reviewer: retry also failed validation: %s", second_err)
        raise ReviewAnalysisError(
            f"Code reviewer output failed validation on both attempts. Details: {second_err}"
        ) from second_err
