"""
Haunter subagents package.

Each subagent is a narrow, focused async function that takes distilled inputs,
calls LLMClient, persists a run_steps trace row, and returns a typed output.
Raw logs, diffs, and secrets never cross out of the subagent boundary.
"""

from app.subagents.code_reviewer import (
    CodeReviewOutput,
    ReviewAnalysisError,
    ReviewFinding,
    ReviewResult,
    analyze_diff,
    format_github_suggestion,
)

__all__ = [
    "CodeReviewOutput",
    "ReviewAnalysisError",
    "ReviewFinding",
    "ReviewResult",
    "analyze_diff",
    "format_github_suggestion",
]

