"""
Fix Generator Subagent — Phase 6.

Given a root-cause diagnosis summary (from Context Gatherer) and optionally a
prior failed attempt's patch + failure reason, calls LLMClient in JSON mode to
produce a unified diff patch with a calibrated confidence score.

Security invariants:
  - Input is ONLY the distilled diagnosis_summary + prior attempt summary — no
    raw logs, no raw diffs, no secrets cross this boundary.
  - LLM output is parsed strictly via Pydantic (strict=True) — no regex
    extraction, no eval, no coercion of out-of-bounds values.
  - Patch is validated for path traversal before any DB insert:
    PurePosixPath rejects absolute paths, '..' components, .git/, .github/workflows/.
  - Attempt cap of 3 is enforced atomically via SELECT FOR UPDATE before any
    LLM call — a Phase 7 retry loop bug cannot create unbounded attempts.
  - Raw LLM content is discarded after parsing; only typed FixOutput fields
    are stored in the attempts table.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from pathlib import PurePosixPath
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import LLMClient
from app.models import Attempt, Run, RunStep

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_ATTEMPTS: int = 3

# Placeholder pricing — same as context_gatherer; replace with real figures.
COST_PER_INPUT_TOKEN: float = 0.001 / 1_000
COST_PER_OUTPUT_TOKEN: float = 0.002 / 1_000

# Paths that must never appear in a generated patch, even if explicitly requested.
_BLOCKED_PATH_PREFIXES: tuple[str, ...] = (".git/", ".github/workflows/")

# Regex to extract file paths from unified diff hunk headers.
# Matches both "--- a/path" / "+++ b/path" and "*** a/path" / "--- a/path".
_HUNK_PATH_RE: re.Pattern[str] = re.compile(
    r"^(?:---|\+\+\+|\*\*\*)\s+(?:[ab]/)?(\S+)", re.MULTILINE
)

# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class AttemptCapExceeded(Exception):
    """Raised when run already has MAX_ATTEMPTS attempts. No LLM call is made."""


class PatchRejected(Exception):
    """Raised when the generated patch fails sanity or path-traversal validation."""


class FixGenerationError(Exception):
    """Raised when LLM output fails Pydantic validation on both initial and retry calls."""


# ---------------------------------------------------------------------------
# Pydantic schema — strict mode: no coercion, no float→int, no str→int
# ---------------------------------------------------------------------------


class FixOutput(BaseModel):
    """
    Strict schema for the LLM Fix Generator JSON response.

    strict=True means:
      - confidence=150  → ValidationError (out of range)
      - confidence="78" → ValidationError (wrong type, no coercion)
      - patch=123       → ValidationError (must be str)
    """

    model_config = ConfigDict(strict=True)

    patch: str = Field(min_length=10)
    confidence: int = Field(ge=0, le=100)
    strategy_notes: Optional[str] = Field(default=None, max_length=500)


# ---------------------------------------------------------------------------
# Patch validation
# ---------------------------------------------------------------------------


def _validate_patch(patch: str) -> None:
    """
    Validate the patch string before storing or forwarding.

    Checks:
    1. Non-empty after strip.
    2. Contains at least one of: '@@', 'diff --git', or both '---' and '+++'.
    3. All file paths parsed from hunk headers pass path-traversal check:
       - Not absolute (no leading '/')
       - No '..' component
       - No blocked prefix (.git/, .github/workflows/)
       - Not empty
       - No '//' in the raw path string

    Raises:
        PatchRejected: with a human-readable reason.
    """
    stripped = patch.strip()
    if not stripped:
        raise PatchRejected("Patch is empty after stripping whitespace.")

    has_at_hunk = "@@" in stripped
    has_git_diff = "diff --git" in stripped
    has_unified = "---" in stripped and "+++" in stripped

    if not (has_at_hunk or has_git_diff or has_unified):
        raise PatchRejected(
            "Patch contains none of the required diff markers: '@@', 'diff --git', or '---'/'+++'."
        )

    # Extract all file paths from hunk headers
    raw_paths = _HUNK_PATH_RE.findall(stripped)

    # Even if no hunk headers found, don't reject — the markers above already
    # give a basic validity check. But if we do find paths, validate each one.
    for raw_path in raw_paths:
        _check_path(raw_path)


def _check_path(raw_path: str) -> None:
    """
    Reject a single file path extracted from a diff hunk header.

    Raises:
        PatchRejected: if the path violates any traversal or scope rule.
    """
    if not raw_path or raw_path.strip() == "":
        raise PatchRejected(f"Patch contains an empty file path in hunk header.")

    if "//" in raw_path:
        raise PatchRejected(
            f"Patch path {raw_path!r} contains '//'; possible injection attempt."
        )

    try:
        p = PurePosixPath(raw_path)
    except Exception as exc:
        raise PatchRejected(f"Patch path {raw_path!r} could not be parsed: {exc}") from exc

    if p.is_absolute():
        raise PatchRejected(
            f"Patch path {raw_path!r} is absolute — absolute paths are not allowed."
        )

    if ".." in p.parts:
        raise PatchRejected(
            f"Patch path {raw_path!r} contains '..' — path traversal is not allowed."
        )

    for prefix in _BLOCKED_PATH_PREFIXES:
        if raw_path.startswith(prefix) or raw_path == prefix.rstrip("/"):
            raise PatchRejected(
                f"Patch path {raw_path!r} targets a blocked prefix: {prefix!r}."
            )


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------


def _build_messages(
    diagnosis_summary: str,
    prior_attempt: Optional[Attempt],
    validation_error_context: Optional[str] = None,
) -> list[dict[str, str]]:
    """
    Build the messages list for the LLM call.

    On retry (validation_error_context is set), append the error as an
    additional user turn so the model knows exactly why the prior output
    was rejected — no guesswork.
    """
    system_prompt = (
        "You are Fix Generator — given root cause summary (+ prior failure if any), "
        "return a unified diff patch that fixes the CI failure + confidence 0-100 "
        "(calibrated: 90=almost certainly passes, 50=guess). "
        "Return JSON only, no markdown fences, no prose. "
        'Schema: {"patch": "<unified diff>", "confidence": <integer 0-100>, '
        '"strategy_notes": "<1-line optional>"}'
    )

    prior_section = ""
    if prior_attempt is not None:
        prior_section = (
            f"\n\n## Prior Failed Attempt #{prior_attempt.attempt_number}\n"
            f"### Patch Applied\n```\n{prior_attempt.patch_text}\n```\n"
            f"### Failure Reason\n{prior_attempt.failure_reason or '(no reason recorded)'}\n"
            "Do NOT repeat the same patch. Generate a different fix strategy."
        )

    user_content = (
        f"## Root Cause Summary\n{diagnosis_summary}"
        f"{prior_section}"
        "\n\nGenerate the fix now. Return JSON only."
    )

    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    if validation_error_context is not None:
        messages.append({
            "role": "assistant",
            "content": "(prior response was invalid)",
        })
        messages.append({
            "role": "user",
            "content": (
                f"Your previous response failed JSON schema validation: {validation_error_context}\n"
                "Fix the issues and return valid JSON only. "
                "confidence MUST be an integer 0-100. patch MUST be a non-empty string."
            ),
        })

    return messages


# ---------------------------------------------------------------------------
# Core LLM call + parse (with one retry on ValidationError)
# ---------------------------------------------------------------------------


async def _call_and_parse(
    messages: list[dict[str, str]],
    diagnosis_summary: str,
    prior_attempt: Optional[Attempt],
    db: AsyncSession,
    run_id: uuid.UUID,
    repo_id: Optional[uuid.UUID],
) -> tuple[FixOutput, dict]:
    """
    Call LLMClient and parse FixOutput from the response.

    On ValidationError, appends error context to messages and retries once.
    On second ValidationError raises FixGenerationError.

    Returns:
        (FixOutput, raw_response_dict) — response dict needed for token accounting.
    """
    llm = LLMClient(timeout=60.0)

    response = await llm.complete(
        messages=messages,
        db=db,
        repo_id=repo_id,
        response_format={"type": "json_object"},
        max_tokens=1024,
    )

    content: str = (response.get("content") or "").strip()

    try:
        fix_output = FixOutput.model_validate_json(content)
        return fix_output, response
    except ValidationError as first_err:
        logger.warning(
            "fix_generator: run=%s first parse failed (%s) — retrying with error context",
            run_id,
            first_err,
        )
        first_err_msg = str(first_err)

    # Retry once with validation error context injected
    error_context = first_err_msg
    retry_messages = _build_messages(
        diagnosis_summary=diagnosis_summary,
        prior_attempt=prior_attempt,
        validation_error_context=error_context,
    )

    retry_response = await llm.complete(
        messages=retry_messages,
        db=db,
        repo_id=repo_id,
        response_format={"type": "json_object"},
        max_tokens=1024,
    )

    retry_content: str = (retry_response.get("content") or "").strip()

    try:
        fix_output = FixOutput.model_validate_json(retry_content)
        # Accumulate token usage across both calls for the trace row
        usage = retry_response.get("usage", {})
        first_usage = response.get("usage", {})
        combined_usage = {
            "input_tokens": usage.get("input_tokens", 0) + first_usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0) + first_usage.get("output_tokens", 0),
        }
        retry_response = {**retry_response, "usage": combined_usage}
        return fix_output, retry_response
    except ValidationError as second_err:
        raise FixGenerationError(
            f"LLM output failed schema validation on both attempts. "
            f"First error: {first_err_msg}. Second error: {second_err}."
        ) from second_err


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def generate_fix(
    run: Run,
    diagnosis_summary: str,
    prior_attempt: Optional[Attempt],
    db: AsyncSession,
) -> Attempt:
    """
    Generate a fix patch for a failed CI run and persist it as an Attempt row.

    Args:
        run:               The Run ORM object (must be loaded in `db` session).
        diagnosis_summary: Distilled root-cause text from Context Gatherer.
                           Must be non-empty; raw logs must NOT be passed here.
        prior_attempt:     Previous Attempt if this is a retry, else None.
                           Only patch_text and failure_reason are used — not raw logs.
        db:                Active AsyncSession.

    Returns:
        The newly inserted Attempt ORM object.

    Raises:
        AttemptCapExceeded: If run already has MAX_ATTEMPTS (3) attempts.
                            No LLM call is made.
        PatchRejected:      If the generated patch fails path-traversal or
                            sanity validation. Attempt is NOT inserted.
        FixGenerationError: If LLM output fails Pydantic validation on both
                            initial and retry calls. Attempt is NOT inserted.
    """
    t0 = time.monotonic()

    # -------------------------------------------------------------------------
    # 1. Atomic attempt cap check — lock the Run row to prevent race conditions
    # -------------------------------------------------------------------------
    await db.execute(
        select(Run.id)
        .where(Run.id == run.id)
        .with_for_update()
    )

    count_result = await db.execute(
        select(func.count())
        .select_from(Attempt)
        .where(Attempt.run_id == run.id)
    )
    existing_count: int = count_result.scalar_one()

    if existing_count >= MAX_ATTEMPTS:
        raise AttemptCapExceeded(
            f"run {run.id} already has {existing_count} attempt(s); "
            f"cap is {MAX_ATTEMPTS}. No LLM call made."
        )

    attempt_number = existing_count + 1

    # -------------------------------------------------------------------------
    # 2. Build prompts — only distilled inputs cross this boundary
    # -------------------------------------------------------------------------
    messages = _build_messages(
        diagnosis_summary=diagnosis_summary,
        prior_attempt=prior_attempt,
    )

    # -------------------------------------------------------------------------
    # 3. LLM call + strict Pydantic parse (one retry on ValidationError)
    # -------------------------------------------------------------------------
    repo_id: Optional[uuid.UUID] = getattr(run, "repo_id", None)

    fix_output, response = await _call_and_parse(
        messages=messages,
        diagnosis_summary=diagnosis_summary,
        prior_attempt=prior_attempt,
        db=db,
        run_id=run.id,
        repo_id=repo_id,
    )

    latency_ms = int((time.monotonic() - t0) * 1000)

    # -------------------------------------------------------------------------
    # 4. Patch sanity + path-traversal validation BEFORE any DB insert
    # -------------------------------------------------------------------------
    _validate_patch(fix_output.patch)

    # -------------------------------------------------------------------------
    # 5. Insert Attempt row — patch stored as untrusted Text (escape on render)
    # -------------------------------------------------------------------------
    attempt = Attempt(
        run_id=run.id,
        attempt_number=attempt_number,
        patch_text=fix_output.patch,
        confidence_score=fix_output.confidence,
        strategy_notes=fix_output.strategy_notes,
        verification_status="pending",
    )
    db.add(attempt)
    await db.flush()  # get attempt.id before commit
    await db.commit()

    # -------------------------------------------------------------------------
    # 6. Persist RunStep trace — tokens + latency only, never raw content
    # -------------------------------------------------------------------------
    usage = response.get("usage", {})
    input_tokens: int = usage.get("input_tokens", 0)
    output_tokens: int = usage.get("output_tokens", 0)

    await _persist_run_step(
        db=db,
        run_id=run.id,
        step_name="fix_generator",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
    )

    logger.info(
        "fix_generator: run=%s attempt=%d confidence=%d latency_ms=%d",
        run.id,
        attempt_number,
        fix_output.confidence,
        latency_ms,
    )

    return attempt


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _persist_run_step(
    *,
    db: AsyncSession,
    run_id: uuid.UUID,
    step_name: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
) -> None:
    """Insert a RunStep trace row. Never stores patch content."""
    cost = (input_tokens * COST_PER_INPUT_TOKEN) + (output_tokens * COST_PER_OUTPUT_TOKEN)
    step = RunStep(
        run_id=run_id,
        step_name=step_name,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        cost_estimate=round(cost, 8),
    )
    db.add(step)
    await db.commit()
