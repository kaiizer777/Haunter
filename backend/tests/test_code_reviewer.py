"""
Unit tests for the Code Review Subagent (test_code_reviewer.py).

Validates:
1. Pydantic validation on valid LLM structured outputs.
2. Pydantic rejection of invalid categories, severities, and risk scores (<0 or >100).
3. Markdown fence and preamble stripping (_strip_markdown_fences).
4. GitHub suggestion block formatting (format_github_suggestion).
5. Empty diff short-circuiting in analyze_diff.
6. Successful analyze_diff execution via mock LLM.
7. Retry on first ValidationError and recovery on second attempt.
8. ReviewAnalysisError on repeated validation failures.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch
import pytest
from pydantic import ValidationError

from app.subagents.code_reviewer import (
    CodeReviewOutput,
    ReviewAnalysisError,
    ReviewFinding,
    ReviewResult,
    _strip_markdown_fences,
    analyze_diff,
    format_github_suggestion,
)


def test_review_finding_schema_valid():
    """Valid finding parses cleanly."""
    finding = ReviewFinding(
        file_path="backend/app/auth.py",
        line_start=15,
        line_end=18,
        category="security",
        severity="high",
        critique="Hardcoded JWT secret detected in fallback path.",
        suggested_patch='jwt_secret = settings.jwt_secret\nif not jwt_secret:\n    raise AuthError("JWT secret missing")',
    )
    assert finding.category == "security"
    assert finding.severity == "high"
    assert finding.line_start == 15
    assert finding.line_end == 18
    assert finding.suggested_patch is not None


def test_review_finding_schema_invalid_category():
    """Invalid category raises ValidationError."""
    with pytest.raises(ValidationError):
        ReviewFinding(
            file_path="test.py",
            line_start=1,
            line_end=1,
            category="cosmetic",  # Not in Literal allowlist
            severity="low",
            critique="Spacing is inconsistent",
        )


def test_review_finding_schema_invalid_severity():
    """Invalid severity raises ValidationError."""
    with pytest.raises(ValidationError):
        ReviewFinding(
            file_path="test.py",
            line_start=1,
            line_end=1,
            category="logic",
            severity="extreme",  # Not in Literal allowlist
            critique="Off-by-one error",
        )


def test_code_review_output_valid():
    """Valid CodeReviewOutput parses cleanly."""
    raw = {
        "risk_score": 85,
        "summary": "Critical SQL injection detected in raw query concatenation.",
        "findings": [
            {
                "file_path": "backend/app/db.py",
                "line_start": 42,
                "line_end": 44,
                "category": "security",
                "severity": "critical",
                "critique": "Unsanitized user input formatted directly into raw SQL query.",
                "suggested_patch": "stmt = select(User).where(User.username == username)",
            }
        ],
    }
    output = CodeReviewOutput.model_validate(raw)
    assert output.risk_score == 85
    assert len(output.findings) == 1
    assert output.findings[0].category == "security"


def test_code_review_output_risk_score_bounds():
    """Risk score must be between 0 and 100."""
    with pytest.raises(ValidationError):
        CodeReviewOutput(risk_score=105, summary="Invalid score", findings=[])

    with pytest.raises(ValidationError):
        CodeReviewOutput(risk_score=-5, summary="Invalid score", findings=[])

    safe = CodeReviewOutput(risk_score=0, summary="Clean changes", findings=[])
    assert safe.risk_score == 0

    crit = CodeReviewOutput(risk_score=100, summary="Maximum risk", findings=[])
    assert crit.risk_score == 100


def test_strip_markdown_fences():
    """Handles standard JSON, fenced ```json ... ```, and conversational preamble."""
    clean = '{"risk_score": 10, "summary": "Looks good", "findings": []}'
    assert _strip_markdown_fences(clean) == clean

    fenced = '```json\n{"risk_score": 10, "summary": "Looks good", "findings": []}\n```'
    assert json.loads(_strip_markdown_fences(fenced))["risk_score"] == 10

    preamble = (
        'Here is the analysis:\n```\n{"risk_score": 25, "summary": "Edge case in regex", "findings": []}\n```'
    )
    assert json.loads(_strip_markdown_fences(preamble))["risk_score"] == 25

    conversational = (
        'Certainly! Below is the requested code review in JSON format:\n\n'
        '{"risk_score": 40, "summary": "Unhandled null pointer", "findings": []}'
    )
    assert json.loads(_strip_markdown_fences(conversational))["risk_score"] == 40


def test_format_github_suggestion():
    """Verifies markdown formatting with and without suggestion block."""
    with_patch = ReviewFinding(
        file_path="main.py",
        line_start=10,
        line_end=12,
        category="security",
        severity="critical",
        critique="Potential SQL injection via raw formatting.",
        suggested_patch="cursor.execute('SELECT * FROM users WHERE id = %s', (user_id,))",
    )
    formatted = format_github_suggestion(with_patch)
    assert "**[SECURITY] [CRITICAL]**" in formatted
    assert "```suggestion" in formatted
    assert "cursor.execute('SELECT * FROM users WHERE id = %s', (user_id,))" in formatted

    without_patch = ReviewFinding(
        file_path="main.py",
        line_start=20,
        line_end=22,
        category="performance",
        severity="medium",
        critique="N+1 query pattern in loop.",
        suggested_patch=None,
    )
    formatted_no_patch = format_github_suggestion(without_patch)
    assert "**[PERFORMANCE] [MEDIUM]**" in formatted_no_patch
    assert "```suggestion" not in formatted_no_patch


@pytest.mark.asyncio
async def test_analyze_diff_empty():
    """Empty or whitespace diff returns clean 0-risk score without calling LLM."""
    res = await analyze_diff(diff_text="")
    assert res.output.risk_score == 0
    assert len(res.output.findings) == 0
    assert res.input_tokens == 0


@pytest.mark.asyncio
async def test_analyze_diff_success():
    """Mock LLM response parses successfully on first try."""
    mock_llm_response = {
        "content": json.dumps({
            "risk_score": 45,
            "summary": "Minor unhandled exception on network timeout.",
            "findings": [
                {
                    "file_path": "backend/app/client.py",
                    "line_start": 25,
                    "line_end": 28,
                    "category": "logic",
                    "severity": "medium",
                    "critique": "httpx.TimeoutException not caught.",
                    "suggested_patch": "except (httpx.RequestError, httpx.TimeoutException) as exc:",
                }
            ],
        }),
        "usage": {"input_tokens": 500, "output_tokens": 150},
        "latency_ms": 320,
        "model": "test-model",
    }

    with patch("app.subagents.code_reviewer.LLMClient") as mock_client_cls:
        instance = mock_client_cls.return_value
        instance.complete = AsyncMock(return_value=mock_llm_response)

        res = await analyze_diff(diff_text="diff --git a/client.py ...")
        assert res.output.risk_score == 45
        assert len(res.output.findings) == 1
        assert res.output.findings[0].category == "logic"
        assert res.input_tokens == 500
        assert res.output_tokens == 150


@pytest.mark.asyncio
async def test_analyze_diff_retry_and_recover():
    """Malformed first LLM response triggers retry with error context and recovers."""
    bad_first_response = {
        "content": '{"risk_score": 150, "summary": "Score too high"}',  # 150 invalid
        "usage": {"input_tokens": 400, "output_tokens": 50},
    }
    good_second_response = {
        "content": json.dumps({
            "risk_score": 60,
            "summary": "Corrected score and findings.",
            "findings": [],
        }),
        "usage": {"input_tokens": 450, "output_tokens": 60},
    }

    with patch("app.subagents.code_reviewer.LLMClient") as mock_client_cls:
        instance = mock_client_cls.return_value
        instance.complete = AsyncMock(side_effect=[bad_first_response, good_second_response])

        res = await analyze_diff(diff_text="diff --git a/test.py ...")
        assert res.output.risk_score == 60
        assert instance.complete.call_count == 2
        assert res.input_tokens == 850
        assert res.output_tokens == 110


@pytest.mark.asyncio
async def test_analyze_diff_double_failure_raises():
    """Both attempts invalid raises ReviewAnalysisError."""
    bad_response = {
        "content": "Not even json",
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }

    with patch("app.subagents.code_reviewer.LLMClient") as mock_client_cls:
        instance = mock_client_cls.return_value
        instance.complete = AsyncMock(return_value=bad_response)

        with pytest.raises(ReviewAnalysisError):
            await analyze_diff(diff_text="diff --git a/test.py ...")


def test_review_finding_line_normalization():
    """If line_end < line_start, line_end is normalized to line_start."""
    finding = ReviewFinding(
        file_path="main.py",
        line_start=25,
        line_end=15,  # inverted
        category="logic",
        severity="medium",
        critique="Potential null reference",
        suggested_patch=None,
    )
    assert finding.line_start == 25
    assert finding.line_end == 25


def test_format_github_suggestion_strips_nested_fences():
    """format_github_suggestion strips triple backtick fences if LLM already fenced the patch."""
    finding = ReviewFinding(
        file_path="main.py",
        line_start=10,
        line_end=12,
        category="security",
        severity="critical",
        critique="Use parameterized query",
        suggested_patch="```python\ncursor.execute('SELECT * FROM t WHERE id = %s', (id,))\n```",
    )
    formatted = format_github_suggestion(finding)
    assert formatted.count("```suggestion") == 1
    assert "```python" not in formatted
    assert "cursor.execute('SELECT * FROM t WHERE id = %s', (id,))" in formatted
