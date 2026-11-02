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
from app.llm.prompts.audit_prompts import _fenced_block, redact_sensitive_text
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
        # No local (this-repository) closing reference found. Do not fall back
        # to a cross-repository reference — those are not local issues.
        if target_repo is None:
            return closing_refs[0].number
        return None

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


def _extract_diff_file_list(diff_text: str) -> list[str]:
    """Extract list of changed file paths from unified diff."""
    if not diff_text:
        return []
    files: list[str] = []
    seen: set[str] = set()
    for line in diff_text.splitlines():
        if line.startswith("diff --git a/"):
            parts = line.split(" b/", 1)
            if len(parts) == 2:
                path = parts[1].strip()
                if path and path not in seen:
                    seen.add(path)
                    files.append(path)
    return files


def _extract_response_content(resp: Any) -> str:
    """Extract content string from either dict-like or object response."""
    if isinstance(resp, dict):
        return str(resp.get("content") or "")
    if hasattr(resp, "content"):
        return str(resp.content or "")
    return str(resp or "")


def _extract_author(data: Mapping[str, Any]) -> str:
    """Extract author login from user dict or top-level author string."""
    user_info = data.get("user")
    if isinstance(user_info, dict) and user_info.get("login"):
        return str(user_info["login"])
    if data.get("author"):
        return str(data["author"])
    return ""


def _parse_json_object(text: str) -> Optional[dict[str, Any]]:
    """Safely extract a JSON dict from raw LLM output or fenced markdown."""
    if not text or not isinstance(text, str):
        return None
    cleaned = text.strip()
    if "```json" in cleaned:
        parts = cleaned.split("```json", 1)[1].split("```", 1)
        cleaned = parts[0].strip()
    elif "```" in cleaned:
        parts = cleaned.split("```", 1)[1].split("```", 1)
        cleaned = parts[0].strip()

    # Find the outermost { ... }
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        cleaned = cleaned[start : end + 1]

    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return None


async def run_subagent_a_pr_analysis(
    *,
    owner: str,
    repo: str,
    pr_number: int,
    token: Optional[str] = None,
    pr_metadata: Optional[Mapping[str, Any]] = None,
    diff_text: str = "",
    llm: Optional[LLMClient] = None,
    model_config: Optional[ResolvedModelConfig] = None,
) -> PRAssessment:
    """Subagent A: Deep analysis of the main pull request."""
    started = time.monotonic()
    data: dict[str, Any] = dict(pr_metadata or {})
    title = str(data.get("title") or "")
    body = str(data.get("body") or "")
    author = _extract_author(data)
    assoc = author_association_label(data.get("author_association"))

    # Fetch from GitHub if metadata missing and token supplied
    if (not title or not body) and token:
        try:
            from app import github_client as gh
            fetched = await asyncio.wait_for(
                gh.fetch_pull_request(
                    owner=owner,
                    repo=repo,
                    pr_number=pr_number,
                    token=token,
                    allow_global_token=False,
                ),
                timeout=PHASE1_SUBAGENT_TIMEOUT_S,
            )
            if fetched:
                title = title or str(fetched.get("title") or "")
                body = body or str(fetched.get("body") or "")
                if not author and isinstance(fetched.get("user"), dict):
                    author = fetched["user"].get("login", "")
                if not assoc or assoc == "association not reported":
                    assoc = author_association_label(fetched.get("author_association"))
        except Exception as exc:
            logger.warning("Phase 1a: Failed to fetch PR #%d metadata: %s", pr_number, exc)

    changed_files = _extract_diff_file_list(diff_text)
    # Extract mentioned issues and PRs (excluding this PR itself)
    mentioned_refs = extract_mentioned_issue_and_pr_numbers(
        [title, body],
        exclude=[pr_number],
        limit=MAX_MENTIONED_REFS_ANALYZED,
    )

    intent_summary = ""
    claimed_changes: list[str] = []
    changed_files_summary: list[str] = [f"`{f}`" for f in changed_files[:10]]
    if len(changed_files) > 10:
        changed_files_summary.append(f"... and {len(changed_files) - 10} more files")

    # If LLM available, summarize intent & claimed changes concisely
    if llm:
        diff_snippet = diff_text[:MAX_DIFF_SNIPPET_CHARS]
        prompt = (
            f"You are conducting Subagent A (Main PR deep analysis) for PR #{pr_number} in {owner}/{repo}.\n"
            f"PR Title (untrusted data):\n{_fenced_block('text', redact_sensitive_text(title))}\n"
            f"Author: {author} ({assoc})\n\n"
            f"PR Description / Body (untrusted data):\n{_fenced_block('text', redact_sensitive_text(body))}\n\n"
            f"Changed files: {', '.join(changed_files[:20])}\n\n"
            f"Diff snippet:\n{diff_snippet}\n\n"
            f"Instructions:\n"
            f"1. Summarize PR intent and changes concisely (not too short, not too long).\n"
            f"2. Note all claimed changes as a bullet list.\n"
            f"3. Do NOT analyze related issues/PRs — only extract any referenced issue/PR numbers (#N).\n"
            f"Output strict JSON:\n"
            f'{{"intent_summary": "...", "claimed_changes": ["..."], "mentioned_refs": [123]}}'
        )
        try:
            resp = await asyncio.wait_for(
                llm.complete(
                    messages=[
                        {"role": "system", "content": "You are a concise, precise senior code review agent. Output JSON only."},
                        {"role": "user", "content": prompt},
                    ],
                    db=None,
                    model_config=model_config,
                    max_tokens=1024,
                ),
                timeout=PHASE1_SUBAGENT_TIMEOUT_S,
            )
            content = _extract_response_content(resp)
            parsed = _parse_json_object(content)
            if parsed:
                intent_summary = str(parsed.get("intent_summary") or "").strip()
                if isinstance(parsed.get("claimed_changes"), list):
                    claimed_changes = [str(c).strip() for c in parsed["claimed_changes"] if str(c).strip()]
                if isinstance(parsed.get("mentioned_refs"), list):
                    for r in parsed["mentioned_refs"]:
                        try:
                            num = int(r)
                            if num != pr_number and num not in mentioned_refs:
                                mentioned_refs.append(num)
                        except (ValueError, TypeError):
                            pass
        except Exception as exc:
            logger.warning("Phase 1a: Subagent A LLM call failed or timed out: %s", exc)

    # Fallback intent summary and claimed changes if LLM was skipped or returned empty
    if not intent_summary:
        first_line = body.strip().splitlines()[0] if body.strip() else ""
        intent_summary = f"{title}. {first_line}".strip()
    if not claimed_changes and body:
        for line in body.splitlines():
            line_s = line.strip()
            if line_s.startswith(("- ", "* ", "1. ", "2. ", "3. ")):
                claimed_changes.append(re.sub(r"^(?:[-*]|\d+\.)\s+", "", line_s))
            if len(claimed_changes) >= 8:
                break

    raw_markdown = (
        f"### PR #{pr_number}: {title}\n"
        f"- **Author**: {author} ({assoc})\n"
        f"- **Intent**: {intent_summary}\n"
        f"- **Claimed Changes**: {', '.join(claimed_changes) if claimed_changes else 'None explicitly listed'}\n"
        f"- **Changed Files**: {', '.join(changed_files_summary) if changed_files_summary else 'No files detected'}\n"
        f"- **Referenced Issues/PRs**: {', '.join(f'#{r}' for r in mentioned_refs) if mentioned_refs else 'None'}"
    )

    logger.info("Phase 1a: Subagent A completed for PR #%d in %.2fs", pr_number, time.monotonic() - started)
    return PRAssessment(
        pr_number=pr_number,
        title=title,
        author=author,
        author_association=assoc,
        intent_summary=intent_summary,
        claimed_changes=claimed_changes,
        changed_files_summary=changed_files_summary,
        mentioned_refs=mentioned_refs,
        raw_markdown=raw_markdown,
    )


def _fetched_issue_repo_full_name(data: Mapping[str, Any]) -> Optional[str]:
    if not isinstance(data, Mapping):
        return None
    repo_info = data.get("repository")
    if isinstance(repo_info, Mapping):
        full_name = repo_info.get("full_name")
        if isinstance(full_name, str) and "/" in full_name:
            return full_name.strip().lower()
    for key in ("repository_url", "url", "html_url"):
        value = data.get(key)
        if isinstance(value, str):
            match = re.search(r"/repos/([^/]+)/([^/]+)", value)
            if not match and key == "html_url":
                match = re.search(r"github\.com/([^/]+)/([^/]+)", value)
            if match:
                return f"{match.group(1)}/{match.group(2)}".lower()
    return None


def _repo_matches_target(data: Mapping[str, Any], owner: str, repo: str) -> bool:
    actual = _fetched_issue_repo_full_name(data)
    if actual is None:
        return True
    target = f"{owner.strip().lower()}/{repo.strip().lower()}"
    return actual == target


async def run_subagent_b_issue_analysis(
    *,
    owner: str,
    repo: str,
    issue_number: int,
    token: Optional[str] = None,
    issue_data: Optional[Mapping[str, Any]] = None,
    pr_number: int = 0,
    llm: Optional[LLMClient] = None,
    model_config: Optional[ResolvedModelConfig] = None,
) -> Optional[IssueAssessment]:
    """Subagent B: Deep analysis of the primary issue."""
    started = time.monotonic()
    data: dict[str, Any] = dict(issue_data or {})
    title = str(data.get("title") or "")
    body = str(data.get("body") or "")
    author = _extract_author(data)
    assoc = author_association_label(data.get("author_association"))
    comments: list[str] = []

    if issue_data and not _repo_matches_target(data, owner, repo):
        logger.info(
            "Phase 1a: Issue #%d does not belong to %s/%s; skipping (cross-repo)",
            issue_number,
            owner,
            repo,
        )
        return None

    # Fetch from GitHub if data missing and token supplied
    if (not title or not body) and token:
        try:
            from app import github_client as gh
            fetched = await asyncio.wait_for(
                gh.fetch_issue(
                    owner=owner,
                    repo=repo,
                    issue_number=issue_number,
                    token=token,
                    allow_global_token=False,
                ),
                timeout=PHASE1_SUBAGENT_TIMEOUT_S,
            )
            if fetched:
                if not _repo_matches_target(fetched, owner, repo):
                    logger.info(
                        "Phase 1a: Fetched issue #%d does not belong to %s/%s; skipping (cross-repo)",
                        issue_number,
                        owner,
                        repo,
                    )
                    return None
                title = title or str(fetched.get("title") or "")
                body = body or str(fetched.get("body") or "")
                if not author and isinstance(fetched.get("user"), dict):
                    author = fetched["user"].get("login", "")
                if not assoc or assoc == "association not reported":
                    assoc = author_association_label(fetched.get("author_association"))
        except Exception as exc:
            logger.warning("Phase 1a: Failed to fetch issue #%d data: %s", issue_number, exc)

    # Fetch top discussion comments if token supplied
    if token and not data.get("comments"):
        try:
            from app import github_client as gh
            fetched_comments = await asyncio.wait_for(
                gh.fetch_issue_comments(
                    owner=owner,
                    repo=repo,
                    issue_number=issue_number,
                    token=token,
                    allow_global_token=False,
                ),
                timeout=PHASE1_SUBAGENT_TIMEOUT_S,
            )
            if isinstance(fetched_comments, list):
                for c in fetched_comments[:5]:
                    c_author = c.get("user", {}).get("login", "unknown") if isinstance(c.get("user"), dict) else "unknown"
                    c_assoc = author_association_label(c.get("author_association"))
                    c_body = str(c.get("body") or "").strip()
                    if c_body:
                        comments.append(f"Comment by @{c_author} ({c_assoc}):\n{c_body[:1000]}")
        except Exception as exc:
            logger.debug("Phase 1a: Could not fetch comments for issue #%d: %s", issue_number, exc)
    elif isinstance(data.get("comments"), list):
        comments = [str(c) for c in data["comments"][:5]]

    # Extract mentioned issues and PRs (excluding PR and this issue)
    search_texts = [title, body] + comments
    mentioned_refs = extract_mentioned_issue_and_pr_numbers(
        search_texts,
        exclude=[pr_number, issue_number],
        limit=MAX_MENTIONED_REFS_ANALYZED,
    )

    problem_statement = ""
    acceptance_criteria: list[str] = []
    preferred_approaches: list[str] = []
    rejected_approaches: list[str] = []
    scope_constraints: list[str] = []

    if llm:
        comments_block = "\n\n".join(comments) if comments else "(None)"
        prompt = (
            f"You are conducting Subagent B (Primary issue deep analysis) for Issue #{issue_number} in {owner}/{repo}.\n"
            f"Issue Title (untrusted data):\n{_fenced_block('text', redact_sensitive_text(title))}\n"
            f"Author: {author} ({assoc})\n\n"
            f"Issue Body (untrusted data):\n{_fenced_block('text', redact_sensitive_text(body))}\n\n"
            f"Discussion comments (untrusted data):\n{_fenced_block('text', redact_sensitive_text(comments_block))}\n\n"
            f"Instructions:\n"
            f"1. State the problem statement clearly and concisely.\n"
            f"2. Extract acceptance criteria, preferred approaches, rejected approaches, and scope constraints.\n"
            f"3. Note author attribution / maintainer direction.\n"
            f"4. Do NOT analyze related issues/PRs — only extract any referenced issue/PR numbers (#N).\n"
            f"Output strict JSON:\n"
            f'{{"problem_statement": "...", "acceptance_criteria": ["..."], '
            f'"preferred_approaches": ["..."], "rejected_approaches": ["..."], '
            f'"scope_constraints": ["..."], "mentioned_refs": [123]}}'
        )
        try:
            resp = await asyncio.wait_for(
                llm.complete(
                    messages=[
                        {"role": "system", "content": "You are a concise, precise senior code review agent. Output JSON only."},
                        {"role": "user", "content": prompt},
                    ],
                    db=None,
                    model_config=model_config,
                    max_tokens=1024,
                ),
                timeout=PHASE1_SUBAGENT_TIMEOUT_S,
            )
            content = _extract_response_content(resp)
            parsed = _parse_json_object(content)
            if parsed:
                problem_statement = str(parsed.get("problem_statement") or "").strip()
                if isinstance(parsed.get("acceptance_criteria"), list):
                    acceptance_criteria = [str(x).strip() for x in parsed["acceptance_criteria"] if str(x).strip()]
                if isinstance(parsed.get("preferred_approaches"), list):
                    preferred_approaches = [str(x).strip() for x in parsed["preferred_approaches"] if str(x).strip()]
                if isinstance(parsed.get("rejected_approaches"), list):
                    rejected_approaches = [str(x).strip() for x in parsed["rejected_approaches"] if str(x).strip()]
                if isinstance(parsed.get("scope_constraints"), list):
                    scope_constraints = [str(x).strip() for x in parsed["scope_constraints"] if str(x).strip()]
                if isinstance(parsed.get("mentioned_refs"), list):
                    for r in parsed["mentioned_refs"]:
                        try:
                            num = int(r)
                            if num != pr_number and num != issue_number and num not in mentioned_refs:
                                mentioned_refs.append(num)
                        except (ValueError, TypeError):
                            pass
        except Exception as exc:
            logger.warning("Phase 1a: Subagent B LLM call failed or timed out: %s", exc)

    if not problem_statement:
        problem_statement = f"{redact_sensitive_text(title)}. {redact_sensitive_text(body)[:300]}".strip()

    raw_markdown = (
        f"### Primary Issue #{issue_number}: {title}\n"
        f"- **Author**: {author} ({assoc})\n"
        f"- **Problem Statement**: {problem_statement}\n"
        f"- **Acceptance Criteria**: {', '.join(acceptance_criteria) if acceptance_criteria else 'Derived from issue description'}\n"
        f"- **Scope Constraints**: {', '.join(scope_constraints) if scope_constraints else 'Standard repo scope'}\n"
        f"- **Referenced Issues/PRs**: {', '.join(f'#{r}' for r in mentioned_refs) if mentioned_refs else 'None'}"
    )

    logger.info("Phase 1a: Subagent B completed for Issue #%d in %.2fs", issue_number, time.monotonic() - started)
    return IssueAssessment(
        issue_number=issue_number,
        title=title,
        author=author,
        author_attribution=assoc,
        problem_statement=problem_statement,
        acceptance_criteria=acceptance_criteria,
        preferred_approaches=preferred_approaches,
        rejected_approaches=rejected_approaches,
        scope_constraints=scope_constraints,
        mentioned_refs=mentioned_refs,
        raw_markdown=raw_markdown,
    )


async def run_phase_1a(
    *,
    owner: str,
    repo: str,
    pr_number: int,
    token: Optional[str] = None,
    pr_metadata: Optional[Mapping[str, Any]] = None,
    issue_data: Optional[Mapping[str, Any]] = None,
    diff_text: str = "",
    llm: Optional[LLMClient] = None,
    model_config: Optional[ResolvedModelConfig] = None,
) -> tuple[PRAssessment, Optional[IssueAssessment]]:
    """Phase 1a: Parallel dispatch of Subagent A (PR) and Subagent B (Primary Issue).

    Subagent B is dispatched ONLY if a primary issue exists that the PR fixes.
    Returns (PRAssessment, Optional[IssueAssessment]) once both have reported back.
    """
    started = time.monotonic()
    pr_body = str((pr_metadata or {}).get("body") or "")
    primary_issue_number = detect_primary_issue_number(
        pr_body,
        pr_metadata=pr_metadata,
        owner=owner,
        repo=repo,
    )

    coro_a = run_subagent_a_pr_analysis(
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        token=token,
        pr_metadata=pr_metadata,
        diff_text=diff_text,
        llm=llm,
        model_config=model_config,
    )

    if primary_issue_number is not None and primary_issue_number > 0 and primary_issue_number != pr_number:
        logger.info("Phase 1a: Detected primary issue #%d for PR #%d — running Subagent A and B in parallel", primary_issue_number, pr_number)
        coro_b = run_subagent_b_issue_analysis(
            owner=owner,
            repo=repo,
            issue_number=primary_issue_number,
            token=token,
            issue_data=issue_data,
            pr_number=pr_number,
            llm=llm,
            model_config=model_config,
        )
        pr_assessment, issue_assessment = await asyncio.gather(coro_a, coro_b)
    else:
        logger.info("Phase 1a: No primary issue detected for PR #%d — omitting Subagent B", pr_number)
        pr_assessment = await coro_a
        issue_assessment = None

    logger.info("Phase 1a: Completed in %.2fs", time.monotonic() - started)
    return pr_assessment, issue_assessment


async def run_single_mention_subagent(
    *,
    owner: str,
    repo: str,
    ref_number: int,
    token: Optional[str] = None,
    item_data: Optional[Mapping[str, Any]] = None,
    llm: Optional[LLMClient] = None,
    model_config: Optional[ResolvedModelConfig] = None,
) -> Optional[MentionAssessment]:
    """Phase 1b subagent: Deep analysis of a single mentioned issue or PR.

    STRICT RECURSION BOUND: Final nested level only. Never crawls deeper references.
    """
    started = time.monotonic()
    data: dict[str, Any] = dict(item_data or {})
    title = str(data.get("title") or "")
    body = str(data.get("body") or "")
    state = str(data.get("state") or "open").lower()
    ref_type = "pull_request" if ("pull_request" in data or data.get("is_pr")) else "issue"
    if data.get("merged") or data.get("merged_at"):
        state = "merged"

    if item_data and not _repo_matches_target(data, owner, repo):
        logger.info(
            "Phase 1b: Mentioned #%d does not belong to %s/%s; skipping (cross-repo)",
            ref_number,
            owner,
            repo,
        )
        return None

    # Fetch from GitHub if data missing and token supplied
    if (not title or not body) and token:
        try:
            from app import github_client as gh
            fetched = await asyncio.wait_for(
                gh.fetch_issue(
                    owner=owner,
                    repo=repo,
                    issue_number=ref_number,
                    token=token,
                    allow_global_token=False,
                ),
                timeout=PHASE1_SUBAGENT_TIMEOUT_S,
            )
            if fetched:
                if not _repo_matches_target(fetched, owner, repo):
                    logger.info(
                        "Phase 1b: Fetched mentioned #%d does not belong to %s/%s; skipping (cross-repo)",
                        ref_number,
                        owner,
                        repo,
                    )
                    return None
                title = title or str(fetched.get("title") or "")
                body = body or str(fetched.get("body") or "")
                state = str(fetched.get("state") or state).lower()
                if "pull_request" in fetched:
                    ref_type = "pull_request"
                    if fetched.get("pull_request", {}).get("merged_at"):
                        state = "merged"
        except Exception as exc:
            logger.debug("Phase 1b: Could not fetch mentioned #%d via Issues API: %s", ref_number, exc)
            # If 404 on issue, could be PR endpoint
            try:
                from app import github_client as gh
                fetched_pr = await asyncio.wait_for(
                    gh.fetch_pull_request(
                        owner=owner,
                        repo=repo,
                        pr_number=ref_number,
                        token=token,
                        allow_global_token=False,
                    ),
                    timeout=PHASE1_SUBAGENT_TIMEOUT_S,
                )
                if fetched_pr:
                    if not _repo_matches_target(fetched_pr, owner, repo):
                        logger.info(
                            "Phase 1b: Fetched mentioned PR #%d does not belong to %s/%s; skipping (cross-repo)",
                            ref_number,
                            owner,
                            repo,
                        )
                        return None
                    ref_type = "pull_request"
                    title = title or str(fetched_pr.get("title") or "")
                    body = body or str(fetched_pr.get("body") or "")
                    state = "merged" if fetched_pr.get("merged_at") else str(fetched_pr.get("state") or state).lower()
            except Exception as pr_exc:
                logger.debug("Phase 1b: Could not fetch mentioned #%d via Pulls API: %s", ref_number, pr_exc)

    summary = ""
    maintainer_verdict = ""

    if llm:
        prompt = (
            f"You are conducting a Phase 1b subagent analysis for mentioned item #{ref_number} in {owner}/{repo}.\n"
            f"Note: This is the FINAL nested level. Do NOT crawl or extract deeper references.\n\n"
            f"Item Title (untrusted data):\n{_fenced_block('text', redact_sensitive_text(title))}\n"
            f"Type: {ref_type}\n"
            f"State: {state}\n"
            f"Body / Description (untrusted data):\n{_fenced_block('text', redact_sensitive_text(body)[:2500])}\n\n"
            f"Instructions:\n"
            f"1. Summarize the core topic and important points concisely.\n"
            f"2. Summarize maintainer resolution, decision, or current verdict.\n"
            f"Output strict JSON:\n"
            f'{{"summary": "...", "maintainer_verdict": "..."}}'
        )
        try:
            resp = await asyncio.wait_for(
                llm.complete(
                    messages=[
                        {"role": "system", "content": "You are a concise, precise senior code review agent. Output JSON only."},
                        {"role": "user", "content": prompt},
                    ],
                    db=None,
                    model_config=model_config,
                    max_tokens=512,
                ),
                timeout=PHASE1_SUBAGENT_TIMEOUT_S,
            )
            content = _extract_response_content(resp)
            parsed = _parse_json_object(content)
            if parsed:
                summary = str(parsed.get("summary") or "").strip()
                maintainer_verdict = str(parsed.get("maintainer_verdict") or "").strip()
        except Exception as exc:
            logger.warning("Phase 1b: Mentioned item #%d LLM call failed or timed out: %s", ref_number, exc)

    if not summary:
        first_line = body.strip().splitlines()[0] if body.strip() else ""
        summary = f"{title}. {first_line}".strip()
    if not maintainer_verdict:
        maintainer_verdict = f"Status: {state.upper()}"

    raw_markdown = (
        f"- **#{ref_number} ({ref_type}, {state})**: {title}\n"
        f"  - *Summary*: {summary}\n"
        f"  - *Verdict*: {maintainer_verdict}"
    )

    logger.debug("Phase 1b: Completed subagent for mention #%d in %.2fs", ref_number, time.monotonic() - started)
    return MentionAssessment(
        ref_number=ref_number,
        ref_type=ref_type,
        title=title,
        state=state,
        summary=summary,
        maintainer_verdict=maintainer_verdict,
        raw_markdown=raw_markdown,
    )


async def run_phase_1b(
    *,
    owner: str,
    repo: str,
    pr_assessment: PRAssessment,
    issue_assessment: Optional[IssueAssessment] = None,
    token: Optional[str] = None,
    mention_items_data: Optional[Mapping[int, Mapping[str, Any]]] = None,
    llm: Optional[LLMClient] = None,
    model_config: Optional[ResolvedModelConfig] = None,
    limit: int = MAX_MENTIONED_REFS_ANALYZED,
) -> list[MentionAssessment]:
    """Phase 1b: Nested mention analysis (n subagents, final nested level).

    Collects all distinct issue and PR references mentioned across Phase 1a outputs,
    excluding the PR number and the primary issue number, and dispatches n subagents
    concurrently.
    """
    started = time.monotonic()
    raw_refs: list[int] = list(pr_assessment.mentioned_refs)
    if issue_assessment:
        raw_refs.extend(issue_assessment.mentioned_refs)

    excluded: set[int] = {pr_assessment.pr_number}
    if issue_assessment:
        excluded.add(issue_assessment.issue_number)

    distinct_refs: list[int] = []
    seen: set[int] = set()
    for ref in raw_refs:
        if ref not in excluded and ref not in seen:
            seen.add(ref)
            distinct_refs.append(ref)
            if len(distinct_refs) >= limit:
                break

    if not distinct_refs:
        logger.info("Phase 1b: No mentioned issues/PRs to analyze across Phase 1a outputs")
        return []

    logger.info(
        "Phase 1b: Dispatching %d nested mention subagents concurrently for refs: %s",
        len(distinct_refs),
        distinct_refs,
    )

    items_data_map = mention_items_data or {}
    tasks = [
        run_single_mention_subagent(
            owner=owner,
            repo=repo,
            ref_number=ref_num,
            token=token,
            item_data=items_data_map.get(ref_num),
            llm=llm,
            model_config=model_config,
        )
        for ref_num in distinct_refs
    ]

    results = await asyncio.gather(*tasks)
    filtered = [r for r in results if r is not None]
    logger.info("Phase 1b: Completed all %d mention subagents in %.2fs", len(filtered), time.monotonic() - started)
    return filtered


async def run_phase_1c_synthesis(
    *,
    owner: str,
    repo: str,
    pr_assessment: PRAssessment,
    issue_assessment: Optional[IssueAssessment] = None,
    mention_assessments: Sequence[MentionAssessment] = (),
    dossier_dir: Optional[str] = None,
    llm: Optional[LLMClient] = None,
    model_config: Optional[ResolvedModelConfig] = None,
) -> Phase1cResult:
    """Phase 1c: 1 synthesis subagent consumes all 1a and 1b outputs.

    Compiles a cohesive Markdown context block with clear sections:
    1. Primary Intent & Problem Statement
    2. Pull Request Scope & Claimed Changes
    3. Authority Verdicts & Maintainer Direction
    4. Scope Alignment & Constraints
    5. Mentioned References & Historical Context

    Persists the single context file to {dossier_dir}/phase-1c.md or tempdir fallback.
    """
    started = time.monotonic()
    pr_num = pr_assessment.pr_number
    primary_intent = pr_assessment.intent_summary
    if issue_assessment and issue_assessment.problem_statement:
        primary_intent = f"Issue #{issue_assessment.issue_number}: {issue_assessment.problem_statement}\nPR #{pr_num}: {pr_assessment.intent_summary}"

    scope_alignment_notes = ""

    # Synthesize via LLM if available
    if llm:
        raw_1a_b = (
            f"=== Subagent A (PR) ===\n{pr_assessment.raw_markdown}\n\n"
            f"=== Subagent B (Primary Issue) ===\n{issue_assessment.raw_markdown if issue_assessment else '(No linked primary issue)'}\n\n"
            f"=== Phase 1b (Mentioned Items) ===\n"
            + ("\n".join(m.raw_markdown for m in mention_assessments) if mention_assessments else "(No mentioned items)")
        )
        prompt = (
            f"You are the Phase 1c Context Compiler for PR #{pr_num} in {owner}/{repo}.\n"
            f"Synthesize the following 1a and 1b assessments into high-signal scope alignment notes.\n\n"
            f"{raw_1a_b}\n\n"
            f"Instructions:\n"
            f"1. Evaluate whether the PR changes align directly with the primary issue and maintainer direction or drift out of scope.\n"
            f"2. Summarize any constraints or boundary decisions.\n"
            f"Output strict JSON:\n"
            f'{{"primary_intent": "...", "scope_alignment_notes": "..."}}'
        )
        try:
            resp = await asyncio.wait_for(
                llm.complete(
                    messages=[
                        {"role": "system", "content": "You are a concise, precise senior code review agent. Output JSON only."},
                        {"role": "user", "content": prompt},
                    ],
                    db=None,
                    model_config=model_config,
                    max_tokens=1024,
                ),
                timeout=PHASE1_SUBAGENT_TIMEOUT_S,
            )
            content = _extract_response_content(resp)
            parsed = _parse_json_object(content)
            if parsed:
                if parsed.get("primary_intent"):
                    primary_intent = str(parsed["primary_intent"]).strip()
                if parsed.get("scope_alignment_notes"):
                    scope_alignment_notes = str(parsed["scope_alignment_notes"]).strip()
        except Exception as exc:
            logger.warning("Phase 1c: LLM synthesis failed or timed out: %s", exc)

    if not scope_alignment_notes:
        if issue_assessment:
            scope_alignment_notes = (
                f"PR claims to fix #{issue_assessment.issue_number}. "
                f"Constraints: {', '.join(issue_assessment.scope_constraints) if issue_assessment.scope_constraints else 'Standard repository scope'}."
            )
        else:
            scope_alignment_notes = "PR has no closing issue reference; changes evaluated against declared PR intent."

    # Build Section 1: Primary Intent & Problem Statement
    sec1 = [
        "## Primary Intent & Problem Statement",
        f"- **Goal**: {primary_intent}",
    ]
    if issue_assessment:
        sec1.append(f"- **Primary Issue**: #{issue_assessment.issue_number} — {issue_assessment.title}")
        if issue_assessment.acceptance_criteria:
            sec1.append("- **Acceptance Criteria**:")
            for ac in issue_assessment.acceptance_criteria:
                sec1.append(f"  * {ac}")

    # Build Section 2: Pull Request Scope & Claimed Changes
    sec2 = [
        "## Pull Request Scope & Claimed Changes",
        f"- **PR Title**: #{pr_num} — {pr_assessment.title}",
        f"- **Author**: {pr_assessment.author} ({pr_assessment.author_association})",
    ]
    if pr_assessment.claimed_changes:
        sec2.append("- **Claimed Changes**:")
        for cc in pr_assessment.claimed_changes:
            sec2.append(f"  * {cc}")
    if pr_assessment.changed_files_summary:
        sec2.append(f"- **Affected Files**: {', '.join(pr_assessment.changed_files_summary)}")

    # Build Section 3: Authority Verdicts & Maintainer Direction
    sec3 = ["## Authority Verdicts & Maintainer Direction"]
    if issue_assessment:
        sec3.append(f"- **Issue Author / Direction**: {issue_assessment.author} ({issue_assessment.author_attribution})")
        if issue_assessment.preferred_approaches:
            sec3.append(f"- **Preferred Approaches**: {', '.join(issue_assessment.preferred_approaches)}")
        if issue_assessment.rejected_approaches:
            sec3.append(f"- **Rejected Approaches**: {', '.join(issue_assessment.rejected_approaches)}")
    else:
        sec3.append("- No linked issue authority statement recorded.")

    # Build Section 4: Scope Alignment & Constraints
    sec4 = [
        "## Scope Alignment & Constraints",
        f"- **Alignment Assessment**: {scope_alignment_notes}",
    ]
    if issue_assessment and issue_assessment.scope_constraints:
        sec4.append(f"- **Scope Boundaries**: {', '.join(issue_assessment.scope_constraints)}")

    # Build Section 5: Mentioned References & Historical Context
    sec5 = ["## Mentioned References & Historical Context"]
    if mention_assessments:
        for m in mention_assessments:
            sec5.append(m.raw_markdown)
    else:
        sec5.append("- None referenced across PR and primary issue.")

    sections = [
        f"# Phase 1 Context Dossier: PR #{pr_num} ({owner}/{repo})" if owner and repo else f"# Phase 1 Context Dossier: PR #{pr_num}",
        "\n".join(sec1),
        "\n".join(sec2),
        "\n".join(sec3),
        "\n".join(sec4),
        "\n".join(sec5),
    ]

    compiled_markdown = "\n\n".join(sections).strip()
    if len(compiled_markdown) > MAX_PHASE_1C_CONTEXT_CHARS:
        compiled_markdown = compiled_markdown[:MAX_PHASE_1C_CONTEXT_CHARS] + "\n\n... (Phase 1 context truncated to budget)"

    # Persist the single context file
    file_path: Optional[str] = None
    try:
        if dossier_dir:
            target_dir = Path(dossier_dir)
        else:
            safe_repo = f"{owner}_{repo}".replace("/", "_") if owner and repo else "repo"
            target_dir = Path(tempfile.gettempdir()) / "haunter-dossier" / safe_repo / f"PR-{pr_num}"
        target_dir.mkdir(parents=True, exist_ok=True)
        out_file = target_dir / "phase-1c.md"
        out_file.write_text(compiled_markdown, encoding="utf-8")
        file_path = str(out_file.resolve())
        logger.info("Phase 1c: Successfully persisted context file to %s (%d chars)", file_path, len(compiled_markdown))
    except Exception as exc:
        logger.warning("Phase 1c: Could not persist file to disk: %s", exc)

    logger.info("Phase 1c: Synthesis completed in %.2fs", time.monotonic() - started)
    return Phase1cResult(
        context_markdown=compiled_markdown,
        file_path=file_path,
        primary_intent=primary_intent,
        scope_alignment_notes=scope_alignment_notes,
        pr_assessment=pr_assessment,
        issue_assessment=issue_assessment,
        mention_assessments=list(mention_assessments),
    )


async def build_phase1_context(
    *,
    owner: str = "",
    repo: str = "",
    pr_number: Optional[int] = None,
    token: Optional[str] = None,
    diff_text: str = "",
    pr_metadata: Optional[Mapping[str, Any]] = None,
    issue_data: Optional[Mapping[str, Any]] = None,
    mention_items_data: Optional[Mapping[int, Mapping[str, Any]]] = None,
    dossier_dir: Optional[str] = None,
    llm: Optional[LLMClient] = None,
    model_config: Optional[ResolvedModelConfig] = None,
) -> Optional[Phase1cResult]:
    """Top-level Phase 1 context orchestrator.

    Runs Phase 1a (2 parallel primary subagents) ->
         Phase 1b (n nested mention subagents, final nested level) ->
         Phase 1c (single final context file compiler).

    Degrades gracefully: if no pr_number is provided or on unexpected failure,
    returns None or a degraded Phase1cResult without raising.
    """
    if pr_number is None or pr_number <= 0:
        return None

    started = time.monotonic()
    logger.info("Phase 1: Starting context pipeline for PR #%d in %s/%s", pr_number, owner, repo)

    try:
        # Phase 1a: 2 parallel primary subagents
        pr_assessment, issue_assessment = await run_phase_1a(
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            token=token,
            pr_metadata=pr_metadata,
            issue_data=issue_data,
            diff_text=diff_text,
            llm=llm,
            model_config=model_config,
        )

        # Phase 1b: n nested mention subagents (final nested level)
        mention_assessments = await run_phase_1b(
            owner=owner,
            repo=repo,
            pr_assessment=pr_assessment,
            issue_assessment=issue_assessment,
            token=token,
            mention_items_data=mention_items_data,
            llm=llm,
            model_config=model_config,
        )

        # Phase 1c: Single final context compiler & file persistence
        result = await run_phase_1c_synthesis(
            owner=owner,
            repo=repo,
            pr_assessment=pr_assessment,
            issue_assessment=issue_assessment,
            mention_assessments=mention_assessments,
            dossier_dir=dossier_dir,
            llm=llm,
            model_config=model_config,
        )

        logger.info("Phase 1: Completed entire pipeline for PR #%d in %.2fs", pr_number, time.monotonic() - started)
        return result

    except Exception as exc:
        logger.warning("Phase 1: Pipeline encountered error; degrading gracefully: %s", exc, exc_info=True)
        fallback_md = f"# Phase 1 Context Dossier: PR #{pr_number}\n\n*(Degraded Phase 1 context: {exc})*"
        return Phase1cResult(
            context_markdown=fallback_md,
            degraded=True,
            degraded_reason=str(exc),
        )



