"""
Haunter subagents package.

Each subagent is a narrow, focused async function that takes distilled inputs,
calls LLMClient, persists a run_steps trace row, and returns a typed output.
Raw logs, diffs, and secrets never cross out of the subagent boundary.
"""

from app.subagents.auditor import (
    AuditAnalysisError,
    AuditFinding,
    AuditResult,
    PerspectiveResult,
    build_ast_diff_summary,
    build_remediation_diff,
    fetch_audit_diff,
    parse_diff_files,
    run_audit,
)
from app.subagents.code_reviewer import (
    CodeReviewOutput,
    ReviewAnalysisError,
    ReviewFinding,
    ReviewResult,
    analyze_diff,
    format_github_suggestion,
)

__all__ = [
    "AuditAnalysisError",
    "AuditFinding",
    "AuditResult",
    "PerspectiveResult",
    "build_ast_diff_summary",
    "build_remediation_diff",
    "fetch_audit_diff",
    "parse_diff_files",
    "run_audit",
    "CodeReviewOutput",
    "ReviewAnalysisError",
    "ReviewFinding",
    "ReviewResult",
    "analyze_diff",
    "format_github_suggestion",
]
