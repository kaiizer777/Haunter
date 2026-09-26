"""
Haunter LLM prompt packs.
"""

from app.llm.prompts.audit_prompts import (
    AUDIT_SYSTEM_PROMPT,
    PERSPECTIVE_INSTRUCTIONS,
    PERSPECTIVES,
    build_perspective_messages,
    build_status_label,
    format_audit_report,
)

__all__ = [
    "AUDIT_SYSTEM_PROMPT",
    "PERSPECTIVE_INSTRUCTIONS",
    "PERSPECTIVES",
    "build_perspective_messages",
    "build_status_label",
    "format_audit_report",
]
