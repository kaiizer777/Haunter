"""
PR Writer Subagent — Phase 8.

Given a verified fix attempt and the root-cause diagnosis summary, calls LLMClient
to generate a PR title + description. Strict Pydantic validation is enforced; on
ValidationError the call is retried once with error context (same pattern as
fix_generator). Branch name is computed server-side — NEVER from LLM output.

Security invariants:
  - Branch name from pr_branch_name() only — regex-validated, length-capped.
  - LLM output validated through PROutput(max_length=72/3000) — no coercion.
  - No raw patch/logs cross the LLM boundary — only diagnosis_summary + patch[:3000].
  - PR text is html.escape'd + secret-redacted before any GitHub POST (in github/pr.py).
"""

from __future__ import annotations

import html
import logging
import re
import time
import uuid
from typing import Optional, Sequence, TypedDict

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import LLMClient
from app.models import Attempt, Run, RunStep

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COST_PER_INPUT_TOKEN: float = 0.001 / 1_000
COST_PER_OUTPUT_TOKEN: float = 0.002 / 1_000

# Regex for validating the server-generated branch name.
_BRANCH_SAFE_RE: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9/_\-\.]+$")
_BRANCH_MAX_LEN = 255

# Branches that Haunter must never push to directly.
_PROTECTED_BRANCH_NAMES: frozenset[str] = frozenset(
    {"main", "master", "develop", "dev", "release", "production"}
)

# ---------------------------------------------------------------------------
# Pydantic output schema
# ---------------------------------------------------------------------------


class PROutput(BaseModel):
    """
    Strict schema for the LLM PR Writer JSON response.

    title: PR title — 5 to 72 chars (GitHub recommendation ≤ 72).
    body:  PR description — 20 to 3000 chars.

    No coercion: str→int, int→str, etc. all raise ValidationError.
    """

    title: str = Field(min_length=5, max_length=72)
    body: str = Field(min_length=20, max_length=10_000_000)


# ---------------------------------------------------------------------------
# Branch name — server-side only, never from LLM
# ---------------------------------------------------------------------------


def pr_branch_name(
    run: Run, attempt: Attempt, default_branch: Optional[str] = None
) -> str:
    """
    Compute a safe branch name for the Haunter fix PR.

    Format: haunter/fix-{run_id_hex8}-{attempt_number}

    The name is:
      - Derived entirely from server-side values (run.id, attempt.attempt_number).
      - Validated against _BRANCH_SAFE_RE (rejects shell injection chars).
      - Length-capped at _BRANCH_MAX_LEN characters.
      - Checked against _PROTECTED_BRANCH_NAMES — if collision, -fix suffix appended.

    Args:
        run:            The Run ORM object.
        attempt:        The verified Attempt ORM object.
        default_branch: Repo default branch for collision check (e.g. 'main').

    Returns:
        A safe, unique branch name string.

    Raises:
        ValueError: If the computed name is still invalid after all mitigations.
    """
    base = f"haunter/fix-{run.id.hex[:8]}-{attempt.attempt_number}"

    # Collision guard against protected branches (shouldn't happen, but be defensive)
    if default_branch and base == default_branch:
        base = f"{base}-fix"
    if base in _PROTECTED_BRANCH_NAMES:
        base = f"{base}-fix"

    if len(base) > _BRANCH_MAX_LEN:
        base = base[:_BRANCH_MAX_LEN]

    if not _BRANCH_SAFE_RE.match(base):
        raise ValueError(
            f"Computed branch name {base!r} contains invalid characters. "
            "This indicates a bug in pr_branch_name() — run.id must be a UUID."
        )

    return base


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


def _build_pr_messages(
    diagnosis_summary: str,
    patch_text: str,
    validation_error_context: Optional[str] = None,
) -> list[dict[str, str]]:
    """
    Build LLM messages for PR generation.

    On retry (validation_error_context set), injects the error as an extra turn.
    """
    system_prompt = (
        "You are PR Writer — given a root-cause diagnosis and a verified fix patch, "
        "return a concise PR title (≤72 chars) and description (≤3000 chars) explaining "
        "the fix and root cause. "
        "Return JSON only, no markdown fences, no prose outside JSON. "
        "No markdown injection — plain text only in title and body. "
        'Schema: {"title": "<PR title ≤72 chars>", "body": "<PR description ≤3000 chars>"}'
    )

    user_content = (
        f"## Root Cause Diagnosis\n{diagnosis_summary}\n\n"
        f"## Verified Patch\n```diff\n{patch_text[:10_000_000]}\n```\n\n"
        "Write the PR title and body now. Return JSON only."
    )

    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    if validation_error_context is not None:
        messages.append(
            {
                "role": "assistant",
                "content": "(prior response was invalid)",
            }
        )
        messages.append(
            {
                "role": "user",
                "content": (
                    f"Your previous response failed validation: {validation_error_context}\n"
                    "Fix the issues. title must be 5–72 chars, body must be at least 20 chars. "
                    "Return valid JSON only."
                ),
            }
        )

    return messages


# ---------------------------------------------------------------------------
# LLM call + parse
# ---------------------------------------------------------------------------


class PRGenerationError(Exception):
    """Raised when LLM output fails PROutput validation on both calls."""


async def _call_and_parse_pr(
    diagnosis_summary: str,
    patch_text: str,
    db: AsyncSession,
    run_id: uuid.UUID,
    repo_id: Optional[uuid.UUID],
) -> tuple[PROutput, dict]:
    """
    Call LLM and parse PROutput. Retry once on ValidationError.

    Returns:
        (PROutput, raw_response_dict with combined usage)

    Raises:
        PRGenerationError: If both calls produce invalid output.
    """
    llm = LLMClient(timeout=60.0)
    messages = _build_pr_messages(diagnosis_summary, patch_text)

    response = await llm.complete(
        messages=messages,
        db=db,
        repo_id=repo_id,
        max_tokens=10_000_000,
    )
    content: str = (response.get("content") or "").strip()

    try:
        pr_output = PROutput.model_validate_json(content)
        return pr_output, response
    except ValidationError as first_err:
        first_err_msg = str(first_err)
        logger.warning(
            "pr_writer: run=%s first parse failed (%s) — retrying",
            run_id,
            first_err_msg,
        )

    # Retry with error context
    retry_messages = _build_pr_messages(
        diagnosis_summary, patch_text, validation_error_context=first_err_msg
    )
    retry_response = await llm.complete(
        messages=retry_messages,
        db=db,
        repo_id=repo_id,
        max_tokens=10_000_000,
    )
    retry_content: str = (retry_response.get("content") or "").strip()

    try:
        pr_output = PROutput.model_validate_json(retry_content)
        # Combine token usage across both calls
        u1 = response.get("usage", {})
        u2 = retry_response.get("usage", {})
        combined = {
            "input_tokens": u1.get("input_tokens", 0) + u2.get("input_tokens", 0),
            "output_tokens": u1.get("output_tokens", 0) + u2.get("output_tokens", 0),
        }
        return pr_output, {**retry_response, "usage": combined}
    except ValidationError as second_err:
        raise PRGenerationError(
            f"LLM PR output failed validation on both attempts. "
            f"First: {first_err_msg}. Second: {second_err}."
        ) from second_err


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def generate_pr_text(
    run: Run,
    verified_attempt: Attempt,
    diagnosis_summary: str,
    db: AsyncSession,
) -> dict[str, str]:
    """
    Generate PR title and body for a verified fix attempt.

    Args:
        run:              The Run ORM object.
        verified_attempt: The Attempt whose verification_status == 'pass'.
        diagnosis_summary: Root-cause text from Context Gatherer (already redacted).
        db:               Active AsyncSession for RunStep trace.

    Returns:
        {"title": str, "body": str} — both validated through PROutput schema.
        Note: caller is responsible for html.escape + secret-redaction before GitHub POST
        (handled in github/pr.py open_pr()).

    Raises:
        PRGenerationError: If LLM fails both attempts.
    """
    t0 = time.monotonic()
    repo_id: Optional[uuid.UUID] = getattr(run, "repo_id", None)

    pr_output, response = await _call_and_parse_pr(
        diagnosis_summary=diagnosis_summary,
        patch_text=verified_attempt.patch_text,
        db=db,
        run_id=run.id,
        repo_id=repo_id,
    )

    latency_ms = int((time.monotonic() - t0) * 1000)
    usage = response.get("usage", {})
    input_tokens: int = usage.get("input_tokens", 0)
    output_tokens: int = usage.get("output_tokens", 0)

    # Persist RunStep trace — tokens + latency only, never PR content
    cost = (input_tokens * COST_PER_INPUT_TOKEN) + (
        output_tokens * COST_PER_OUTPUT_TOKEN
    )
    step = RunStep(
        run_id=run.id,
        step_name="pr_writer",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        cost_estimate=round(cost, 8),
    )
    db.add(step)
    await db.commit()

    logger.info(
        "pr_writer: run=%s input_tokens=%d output_tokens=%d latency_ms=%d title_len=%d",
        run.id,
        input_tokens,
        output_tokens,
        latency_ms,
        len(pr_output.title),
    )

    return {"title": pr_output.title, "body": pr_output.body}


# ---------------------------------------------------------------------------
# Fallback issue content — Feature 3 (exhaust path, no LLM)
# ---------------------------------------------------------------------------

# GitHub issue titles over ~120 chars get truncated in list views; bodies are
# capped for readability (the full diagnosis stays on the run detail page).
# Per-attempt text is capped so the per-attempt table stays intact when the
# body cap is reached — a row cut mid-cell is worse than a shortened cell.
_FALLBACK_ISSUE_TITLE_MAX_LEN = 120
_FALLBACK_ISSUE_BODY_MAX_LEN = 8000
_FALLBACK_ISSUE_ATTEMPT_FIELD_MAX_LEN = 300

# Labels applied to every filed fallback issue for triage filtering.
FALLBACK_ISSUE_LABELS: tuple[str, ...] = ("haunter", "ci-failure")

# Fixed title scaffolding around the (truncatable) branch name.
_FALLBACK_ISSUE_TITLE_PREFIX = "Haunter: CI failure on `"
_FALLBACK_ISSUE_TITLE_SUFFIX = "` needs attention"


class FallbackIssueContent(TypedDict):
    """Payload for a filed fallback tracking issue."""

    title: str
    body: str
    labels: list[str]


def _build_fallback_issue_title(branch: str, sha7: str) -> str:
    """
    Assemble the issue title, truncating the branch rather than the title.

    GitHub rejects titles over 256 chars and list views clip well before that,
    so the title has to be capped. Truncating the assembled title instead
    would cut the commit SHA — the one identifier a human needs to triage the
    issue — and leave an unbalanced backtick. Reserving the scaffolding and the
    SHA up front keeps the title well-formed at any branch length.
    """
    sha_suffix = f" ({sha7})" if sha7 else ""
    budget = (
        _FALLBACK_ISSUE_TITLE_MAX_LEN
        - len(_FALLBACK_ISSUE_TITLE_PREFIX)
        - len(_FALLBACK_ISSUE_TITLE_SUFFIX)
        - len(sha_suffix)
    )
    return (
        f"{_FALLBACK_ISSUE_TITLE_PREFIX}{branch[:budget]}"
        f"{_FALLBACK_ISSUE_TITLE_SUFFIX}{sha_suffix}"
    )


def build_fallback_issue_content(
    run: Run,
    diagnosis_summary: Optional[str],
    attempts: Sequence[Attempt],
    *,
    owner: str,
    repo: str,
) -> FallbackIssueContent:
    """
    Build sanitized GitHub issue title/body/labels for the exhaust path.

    Pure function — no I/O, no LLM calls, no DB writes. The caller
    (orchestrator) POSTs the result via ``github_client.create_issue`` and
    persists the returned URL/number on the run.

    Sanitisation mirrors the commit-comment fallback path:
      - Secrets redacted via ``context_gatherer._redact_secrets``.
      - User-derived strings html-escaped to neutralise stored HTML/JS.
      - NEVER includes raw patch text, full CI logs, or stack traces —
        only the distilled diagnosis plus per-attempt strategy notes and
        truncated failure reasons.

    Args:
        run:                The exhausted Run ORM object.
        diagnosis_summary:  Distilled root-cause text (may be None).
        attempts:           Attempts evaluated for this run (may be empty).
        owner:              Repository owner login (for display only).
        repo:               Repository name (for display only).

    Returns:
        {"title": str, "body": str, "labels": list[str]}.
    """
    from app.subagents.context_gatherer import _redact_secrets

    sha7 = (run.head_sha or "")[:7]
    branch = run.head_branch or "unknown branch"
    safe_owner = html.escape(owner, quote=False)
    safe_repo = html.escape(repo, quote=False)
    safe_branch = html.escape(branch, quote=False)

    raw_summary = diagnosis_summary or "(no diagnosis available)"
    safe_summary = html.escape(_redact_secrets(raw_summary), quote=False)

    ordered = sorted(attempts, key=lambda a: a.attempt_number)
    if ordered:
        rows: list[str] = []
        for attempt in ordered:
            confidence = attempt.confidence_score
            confidence_str = str(confidence) if confidence is not None else "n/a"
            notes = html.escape(
                _redact_secrets(attempt.strategy_notes or "(no notes)")[
                    :_FALLBACK_ISSUE_ATTEMPT_FIELD_MAX_LEN
                ],
                quote=False,
            )
            failure = html.escape(
                _redact_secrets(attempt.failure_reason or "(no failure reason)")[
                    :_FALLBACK_ISSUE_ATTEMPT_FIELD_MAX_LEN
                ],
                quote=False,
            )
            rows.append(
                f"| {attempt.attempt_number} | {confidence_str} "
                f"| {notes} | {failure} |"
            )
        attempts_table = (
            "| Attempt | Confidence | Strategy | Failure reason |\n"
            "| --- | --- | --- | --- |\n" + "\n".join(rows)
        )
        attempts_line = f"{len(ordered)} attempt(s) evaluated, all failed verification."
    else:
        attempts_table = "(no attempts recorded)"
        attempts_line = "No fix attempts were recorded."

    title = _build_fallback_issue_title(safe_branch, sha7)

    body = (
        "## Haunter diagnosis — automated fix exhausted\n\n"
        f"**Repo:** {safe_owner}/{safe_repo}\n"
        f"**Branch:** `{safe_branch}` @ `{sha7 or 'unknown sha'}`\n"
        f"**Run:** `{run.id}`\n"
        f"**Attempts:** {attempts_line}\n\n"
        "### Root cause\n\n"
        f"{safe_summary}\n\n"
        "### Attempted strategies\n\n"
        f"{attempts_table}\n\n"
        "---\n"
        "*Posted by Haunter after all fix attempts failed verification. "
        "No files were modified and no PR was opened.*"
    )
    body = body[:_FALLBACK_ISSUE_BODY_MAX_LEN]

    return {"title": title, "body": body, "labels": list(FALLBACK_ISSUE_LABELS)}
