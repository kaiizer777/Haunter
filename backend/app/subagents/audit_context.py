"""Auditor Phase 1 — Bulletproof context block pipeline.

Implements the 3-stage Phase 1 pipeline for pull request audits:
- Phase 1a: 2 parallel primary subagents (Subagent A for main PR deep analysis,
  Subagent B for primary issue deep analysis).
- Phase 1b: n nested mention subagents analyzing distinct mentioned issues/PRs
  (final nested level; strict recursion bound).
- Phase 1c: Single final context file compiler synthesizing all 1a and 1b outputs
  into a single markdown context block saved to disk ({dossier_dir}/phase-1c.md)
  and passed downstream to Phase 2 and Phase 3.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Mapping, Optional, Sequence

from app.llm import LLMClient, ResolvedModelConfig
from app.subagents.issue_context import (
    author_association_label,
    extract_textual_mentions,
    parse_closing_issue_refs,
)

logger = logging.getLogger(__name__)

# Bounded caps to prevent prompt explosion or rate-limit exhaustion
MAX_MENTIONED_REFS_ANALYZED = 8
PHASE1_SUBAGENT_TIMEOUT_S = 30.0
MAX_PHASE_1C_CONTEXT_CHARS = 30_000
MAX_DIFF_SNIPPET_CHARS = 10_000


@dataclass
class PRAssessment:
    """Subagent A output: deep analysis of the main pull request."""

    pr_number: int
    title: str = ""
    author: str = ""
    author_association: str = ""
    intent_summary: str = ""
    claimed_changes: list[str] = field(default_factory=list)
    changed_files_summary: list[str] = field(default_factory=list)
    mentioned_refs: list[int] = field(default_factory=list)
    raw_markdown: str = ""


@dataclass
class IssueAssessment:
    """Subagent B output: deep analysis of the primary issue linked to the PR."""

    issue_number: int
    title: str = ""
    author: str = ""
    author_attribution: str = ""
    problem_statement: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)
    preferred_approaches: list[str] = field(default_factory=list)
    rejected_approaches: list[str] = field(default_factory=list)
    scope_constraints: list[str] = field(default_factory=list)
    mentioned_refs: list[int] = field(default_factory=list)
    raw_markdown: str = ""


@dataclass
class MentionAssessment:
    """Phase 1b output: analysis of an issue or PR mentioned in Phase 1a outputs."""

    ref_number: int
    ref_type: str = "issue"  # "issue" | "pull_request"
    title: str = ""
    state: str = ""  # "open" | "closed" | "merged"
    summary: str = ""
    maintainer_verdict: str = ""
    raw_markdown: str = ""


@dataclass
class Phase1cResult:
    """Phase 1c output: the single compiled context block and persisted file."""

    context_markdown: str
    file_path: Optional[str] = None
    primary_intent: str = ""
    scope_alignment_notes: str = ""
    pr_assessment: Optional[PRAssessment] = None
    issue_assessment: Optional[IssueAssessment] = None
    mention_assessments: list[MentionAssessment] = field(default_factory=list)
    degraded: bool = False
    degraded_reason: Optional[str] = None


def detect_primary_issue_number(
    pr_body: str,
    *,
    pr_metadata: Optional[Mapping[str, Any]] = None,
    owner: str = "",
    repo: str = "",
) -> Optional[int]:
    """Identify the primary issue number fixed/closed by the PR."""
    if pr_metadata:
        # Check explicit metadata fields if provided
        for key in ("primary_issue_number", "linked_issue", "closing_issue"):
            val = pr_metadata.get(key)
            if isinstance(val, int) and val > 0:
                return val
            if isinstance(val, str) and val.isdigit():
                return int(val)

        # Check closingIssuesReferences from GraphQL if present
        closing_refs = pr_metadata.get("closingIssuesReferences") or pr_metadata.get("closing_issues")
        if isinstance(closing_refs, list) and closing_refs:
            first = closing_refs[0]
            if isinstance(first, int):
                return first
            if isinstance(first, dict) and "number" in first:
                try:
                    return int(first["number"])
                except (ValueError, TypeError):
                    pass

    # Parse closing keywords in PR body: fixes #123, closes #456, resolves #789
    closing_refs = parse_closing_issue_refs(pr_body or "")
    if closing_refs:
        target_repo = f"{owner.strip().lower()}/{repo.strip().lower()}" if owner and repo else None
        for ref in closing_refs:
            if ref.repo is None:
                return ref.number
            if target_repo and ref.repo.lower() == target_repo:
                return ref.number
        # Fall back to first closing ref
        return closing_refs[0].number

    return None


def extract_mentioned_issue_and_pr_numbers(
    texts: Sequence[str],
    *,
    exclude: Sequence[int] = (),
    limit: int = MAX_MENTIONED_REFS_ANALYZED,
) -> list[int]:
    """Extract distinct issue and PR numbers mentioned across text blocks."""
    mentions, _ = extract_textual_mentions(texts, limit=limit * 2, exclude=exclude)
    # Deduplicate in order of appearance, keeping only numbers for this repo (repo == "")
    seen: set[int] = set()
    numbers: list[int] = []
    excluded_set = set(exclude)
    for m in mentions:
        if not m.repo and m.number not in excluded_set and m.number not in seen:
            seen.add(m.number)
            numbers.append(m.number)
            if len(numbers) >= limit:
                break
    return numbers
