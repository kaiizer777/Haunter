"""
Haunter LLM prompt packs.
"""

from app.llm.prompts.audit_prompts import (
    AUDIT_SYSTEM_PROMPT,
    PERSPECTIVE_INSTRUCTIONS,
    PERSPECTIVES,
    build_perspective_messages,
    build_status_from_score,
    build_status_label,
    calculate_pr_confidence_score,
    clean_pr_summary,
    derive_blast_radius,
    format_audit_report,
    format_confidence_score,
)

__all__ = [
    "AUDIT_SYSTEM_PROMPT",
    "PERSPECTIVE_INSTRUCTIONS",
    "PERSPECTIVES",
    "build_perspective_messages",
    "build_status_from_score",
    "build_status_label",
    "calculate_pr_confidence_score",
    "clean_pr_summary",
    "derive_blast_radius",
    "format_audit_report",
    "format_confidence_score",
]

