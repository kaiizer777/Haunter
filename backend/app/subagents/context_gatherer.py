"""
Context Gatherer Subagent — Phase 5.

Fetches CI failure artefacts from GitHub concurrently, redacts secrets,
and distils a root-cause summary via LLMClient. Persists a run_steps trace
row with token counts, latency, and cost estimate. Never stores or forwards
raw logs, diffs, or secrets beyond this module's boundary.

Security invariants:
  - All three GitHub inputs are truncated to CAP_CHARS before use.
  - All three inputs are scanned for secrets via _redact_secrets() before the
    LLM call and before any persistence. Raw strings are discarded after use.
  - The run_steps row stores only the distilled summary + token counts, never
    the raw text.
  - Each GitHub fetch is guarded by asyncio.wait_for(timeout=FETCH_TIMEOUT_S)
    so one hung upstream request cannot stall the gather indefinitely.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app import github_client as gh
from app.llm import LLMClient
from app.models import Repo, Run, RunStep

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Set to 10M (10_000_000) for testing / full context passing without truncation
CAP_CHARS: int = 10_000_000

# Per-fetch timeout. One hung GitHub request must not stall the gather.
# NOTE: Relaxed from 30s -> 120s while the pipeline is being verified. The
# provider is free-tier and slow responses are common; we don't want to
# kill the LLM call mid-stream. Tighten once we have a stable success path.
FETCH_TIMEOUT_S: float = 120.0

# Placeholder pricing: $0.001 / 1k input tokens, $0.002 / 1k output tokens.
# Replace with real provider pricing once known.
COST_PER_INPUT_TOKEN: float = 0.001 / 1_000
COST_PER_OUTPUT_TOKEN: float = 0.002 / 1_000

# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------

# Patterns are applied in order. Ordering matters: longer/more-specific first.
_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # PEM private keys (RSA, EC, generic) — multiline
    (
        re.compile(
            r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----.*?-----END (?:RSA |EC )?PRIVATE KEY-----",
            re.DOTALL,
        ),
        "[REDACTED_PRIVATE_KEY]",
    ),
    # Connection strings — postgresql / postgres / mysql / mongodb URIs
    (
        re.compile(
            r"(?:postgresql|postgres|mysql|mongodb)(?:\+\w+)?://[^\s\"'<>]+",
            re.IGNORECASE,
        ),
        "[REDACTED_CONN_STRING]",
    ),
    # DATABASE_URL / DATABASE_URL_UNPOOLED assignments (any value)
    (
        re.compile(r"DATABASE_URL(?:_UNPOOLED)?\s*=\s*\S+", re.IGNORECASE),
        "DATABASE_URL=[REDACTED]",
    ),
    # OpenAI-style keys: sk-... (20+ alphanumeric chars)
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "[REDACTED]"),
    # GitHub PATs: ghp_...
    (re.compile(r"ghp_[A-Za-z0-9]{36,}"), "[REDACTED]"),
    # Neon / Supabase keys: npg_...
    (re.compile(r"npg_[A-Za-z0-9]{20,}"), "[REDACTED]"),
    # Generic high-entropy Bearer / token headers
    (
        re.compile(r"(?i)(?:authorization|bearer|token)\s*[:=]\s*[A-Za-z0-9\-_.~+/]{20,}"),
        "[REDACTED_AUTH_HEADER]",
    ),
]


def _redact_secrets(text: str) -> str:
    """Apply all secret patterns to `text`, returning the sanitised string."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _truncate_and_redact(text: str) -> str:
    """Truncate to CAP_CHARS then redact secrets. Order matters — truncate first
    so redaction never processes more chars than needed."""
    if len(text) > CAP_CHARS:
        text = text[:CAP_CHARS] + "\n[...TRUNCATED...]"
    return _redact_secrets(text)


# Regex to extract file paths from unified-diff headers (--- a/... / +++ b/...).
# Used to build the "## Files in the failing commit" section that fix_generator
# uses to pick the right config file to modify (e.g. pyproject.toml vs conftest.py).
_DIFF_PATH_RE: re.Pattern[str] = re.compile(
    r"^(?:---|\+\+\+)\s+(?:[ab]/)?(\S+)", re.MULTILINE
)

# Cap on file-list size in the appended section — keeps the diagnosis_summary
# within the LLM's input window for fix_generator. 50 files is generous for
# real CI failures; anything more is noise.
_MAX_FILE_PATHS_IN_SUMMARY: int = 50


def _extract_file_paths_from_diff(diff_text: str) -> list[str]:
    """
    Extract touched file paths from a unified diff (de-duplicated, /dev/null
    skipped). Used to enrich the diagnosis_summary so the fix_generator LLM
    has repo context to pick the right file to modify.
    """
    paths: list[str] = []
    for raw in _DIFF_PATH_RE.findall(diff_text or ""):
        if raw == "/dev/null":
            continue
        if raw not in paths:
            paths.append(raw)
        if len(paths) >= _MAX_FILE_PATHS_IN_SUMMARY:
            break
    return paths


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _safe_fetch(coro: Any, label: str) -> str:
    """
    Await `coro` with FETCH_TIMEOUT_S. On timeout or any exception, log a
    structured warning and return an empty string. Never propagates exceptions
    so asyncio.gather can still collect results from the other two fetches.
    """
    try:
        result = await asyncio.wait_for(coro, timeout=FETCH_TIMEOUT_S)
        # Normalise: commit metadata dict → compact JSON string
        if isinstance(result, dict):
            result = json.dumps(result, default=str)
        return str(result) if result else ""
    except asyncio.TimeoutError:
        logger.warning("context_gatherer: %s fetch timed out after %ss", label, FETCH_TIMEOUT_S)
        return ""
    except Exception as exc:
        logger.warning("context_gatherer: %s fetch failed (%s: %s)", label, type(exc).__name__, exc)
        return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def gather_context(
    run: Run,
    repo: Repo,
    db: AsyncSession,
) -> str:
    """
    Gather CI failure context and return a distilled root-cause summary.

    Steps:
      1. Fetch logs, diff, commit metadata concurrently (3-way gather, each
         guarded by FETCH_TIMEOUT_S).
      2. Truncate each to CAP_CHARS.
      3. Redact secrets from each string.
      4. Make a single LLMClient call with all three inputs fused.
      5. Persist a run_steps row (tokens, latency, cost).
      6. Return the summary string (targeted: 200–400 tokens ≈ 800–1600 chars).

    Raw inputs are not stored anywhere — only the distilled summary propagates.
    """
    owner = repo.owner
    name = repo.name
    sha = run.head_sha
    github_run_id = run.github_run_id

    # -------------------------------------------------------------------------
    # 1. Concurrent GitHub fetches — all 3 in one gather, each timeout-guarded
    # -------------------------------------------------------------------------
    logs_raw, diff_raw, meta_raw = await asyncio.gather(
        _safe_fetch(
            gh.fetch_workflow_run_logs(owner=owner, repo=name, run_id=github_run_id),
            label="logs",
        ),
        _safe_fetch(
            gh.fetch_diff(owner=owner, repo=name, sha=sha),
            label="diff",
        ),
        _safe_fetch(
            gh.fetch_commit_metadata(owner=owner, repo=name, sha=sha),
            label="commit_metadata",
        ),
    )

    # -------------------------------------------------------------------------
    # 2 & 3. Truncate then redact — applied independently to each input
    # -------------------------------------------------------------------------
    logs_clean = _truncate_and_redact(logs_raw)
    diff_clean = _truncate_and_redact(diff_raw)
    meta_clean = _truncate_and_redact(meta_raw)

    # Discard raw strings immediately — they must not survive beyond this point
    del logs_raw, diff_raw, meta_raw

    # -------------------------------------------------------------------------
    # 4. LLM call (with one empty-response retry) — see _call_with_empty_retry
    #    below. If the model returns whitespace / hits max_tokens, we try
    #    once more with a tighter, force-prose prompt before declaring failure.
    # -------------------------------------------------------------------------
    summary, response, latency_ms, input_tokens, output_tokens = await _call_with_empty_retry(
        logs_clean=logs_clean,
        diff_clean=diff_clean,
        meta_clean=meta_clean,
        run=run,
        db=db,
    )

    # -------------------------------------------------------------------------
    # 5. Persist run_steps trace row — tokens + cost, never raw text
    # -------------------------------------------------------------------------
    await _persist_run_step(
        db=db,
        run_id=run.id,
        step_name="context_gatherer",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        error=False,
    )

    # -------------------------------------------------------------------------
    # 6. Enrich summary with the file list from the failing diff
    #    so the downstream fix_generator LLM has repo context to pick the
    #    right file to modify (pyproject.toml vs conftest.py vs src/foo.py).
    #    Bounded to _MAX_FILE_PATHS_IN_SUMMARY to stay in the input window.
    # -------------------------------------------------------------------------
    file_paths = _extract_file_paths_from_diff(diff_clean)
    if file_paths:
        file_section = (
            "\n\n## Files in the failing commit\n"
            + "\n".join(f"- {p}" for p in file_paths)
        )
        summary = (summary or "").rstrip() + file_section
        logger.info(
            "context_gatherer: run=%s appended %d file path(s) to summary",
            run.id,
            len(file_paths),
        )

    # -------------------------------------------------------------------------
    # 7. Return distilled summary (with file list appended)
    # -------------------------------------------------------------------------
    logger.info(
        "context_gatherer: run=%s input_tokens=%d output_tokens=%d latency_ms=%d",
        run.id,
        input_tokens,
        output_tokens,
        latency_ms,
    )
    return summary


def _build_messages(
    logs_clean: str,
    diff_clean: str,
    meta_clean: str,
    *,
    retry: bool = False,
) -> list[dict[str, str]]:
    """
    Build the gatherer prompt. On retry, the system prompt is tightened and
    the user instruction asks explicitly for short prose — proven to pull
    models out of the empty-output hole on free-tier endpoints.
    """
    if retry:
        system_prompt = (
            "You are a CI failure diagnosis expert. Reply with EXACTLY 2-3 short sentences "
            "of plain prose. No markdown. No code fences. No JSON. No bullet points. "
            "Just prose describing the root cause."
        )
        user_message = (
            f"Logs:\n{logs_clean or '(unavailable)'}\n\n"
            f"Diff:\n{diff_clean or '(unavailable)'}\n\n"
            f"Metadata:\n{meta_clean or '(unavailable)'}\n\n"
            "Reply with 2-3 short sentences in plain prose only."
        )
    else:
        system_prompt = (
            "You are Context Gatherer, a CI failure diagnosis agent. "
            "Your sole output is a concise root-cause summary: 3-5 lines maximum. "
            "Include: error type, the exact file and line number if visible in the logs, "
            "and one sentence on why CI failed. "
            "Do NOT output a patch, a fix, or echo back raw log lines. "
            "If no logs are available, state so explicitly."
        )
        user_message = (
            f"## CI Failure Context\n\n"
            f"### Workflow Logs (truncated)\n```\n{logs_clean or '(unavailable)'}\n```\n\n"
            f"### Commit Diff (truncated)\n```diff\n{diff_clean or '(unavailable)'}\n```\n\n"
            f"### Commit Metadata\n```json\n{meta_clean or '(unavailable)'}\n```\n\n"
            "Provide the root-cause summary now."
        )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]


async def _call_with_empty_retry(
    *,
    logs_clean: str,
    diff_clean: str,
    meta_clean: str,
    run: Run,
    db: AsyncSession,
) -> tuple[str, dict, int, int, int]:
    """
    Make the LLM call. If the first response is empty (None / whitespace /
    only markdown code fences), retry exactly once with a tighter prompt.

    Returns:
        (summary, response_dict, latency_ms, input_tokens, output_tokens)

    Raises:
        TimeoutError, ValueError — same contract as the previous single-call
        path so the orchestrator's outer handler treats it identically.
    """
    llm = LLMClient(timeout=FETCH_TIMEOUT_S)

    # ----- Attempt 1: full structured prompt -----
    messages = _build_messages(logs_clean, diff_clean, meta_clean, retry=False)
    t0 = time.monotonic()
    try:
        response = await asyncio.wait_for(
            llm.complete(messages=messages, db=db, repo_id=run.repo_id, max_tokens=10_000_000),
            timeout=FETCH_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.error(
            "context_gatherer: LLM call (attempt 1) timed out for run %s after %dms",
            run.id, elapsed_ms,
        )
        raise TimeoutError(
            f"context_gatherer LLM call timed out after {elapsed_ms}ms "
            f"(limit {int(FETCH_TIMEOUT_S * 1000)}ms)"
        ) from None

    first_latency_ms = int((time.monotonic() - t0) * 1000)
    first_content = (response.get("content") or "").strip()
    first_usage = response.get("usage", {}) or {}
    first_in = int(first_usage.get("input_tokens", 0))
    first_out = int(first_usage.get("output_tokens", 0))

    if first_content:
        return first_content, response, first_latency_ms, first_in, first_out

    # ----- Attempt 2: empty-response retry with sharper prompt -----
    logger.warning(
        "context_gatherer: run=%s attempt 1 returned empty (model=%s, "
        "output_tokens=%d, latency_ms=%d) — retrying with tighter prompt",
        run.id, response.get("model", "unknown"), first_out, first_latency_ms,
    )

    retry_messages = _build_messages(logs_clean, diff_clean, meta_clean, retry=True)
    t1 = time.monotonic()
    try:
        retry_response = await asyncio.wait_for(
            llm.complete(
                messages=retry_messages,
                db=db,
                repo_id=run.repo_id,
                max_tokens=10_000_000,
            ),
            timeout=FETCH_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        elapsed_ms = int((time.monotonic() - t1) * 1000)
        logger.error(
            "context_gatherer: LLM call (attempt 2 / retry) timed out for run %s after %dms",
            run.id, elapsed_ms,
        )
        raise TimeoutError(
            f"context_gatherer LLM call (retry) timed out after {elapsed_ms}ms "
            f"(limit {int(FETCH_TIMEOUT_S * 1000)}ms)"
        ) from None

    retry_latency_ms = int((time.monotonic() - t1) * 1000)
    retry_content = (retry_response.get("content") or "").strip()
    retry_usage = retry_response.get("usage", {}) or {}
    retry_in = int(retry_usage.get("input_tokens", 0))
    retry_out = int(retry_usage.get("output_tokens", 0))

    if retry_content:
        # Retry succeeded. Aggregate the token counts so the dashboard shows
        # the true cost of both attempts.
        return (
            retry_content,
            retry_response,
            first_latency_ms + retry_latency_ms,
            first_in + retry_in,
            first_out + retry_out,
        )

    # Both attempts empty — bail with the same error contract as before.
    total_latency = first_latency_ms + retry_latency_ms
    total_in = first_in + retry_in
    total_out = first_out + retry_out
    model = retry_response.get("model") or response.get("model", "unknown")
    logger.error(
        "context_gatherer: run=%s BOTH attempts returned empty "
        "(model=%s, total_output_tokens=%d, total_latency_ms=%d) — treating as failure",
        run.id, model, total_out, total_latency,
    )
    raise ValueError(
        f"context_gatherer returned an empty summary from model {model!r} "
        f"after 2 attempts (output_tokens={total_out}, latency_ms={total_latency}). "
        f"The LLM produced no usable diagnosis — likely hit max_tokens or "
        f"returned only whitespace / markdown code fences on both attempts."
    )


async def _persist_run_step(
    *,
    db: AsyncSession,
    run_id: Any,
    step_name: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    error: bool = False,
) -> None:
    """
    Insert a RunStep trace row.

    Cost is a placeholder estimate; replace with real pricing from provider docs.
    Raw inputs are NEVER stored here — only token counts and latency.
    """
    cost = (input_tokens * COST_PER_INPUT_TOKEN) + (output_tokens * COST_PER_OUTPUT_TOKEN)
    step = RunStep(
        run_id=run_id,
        step_name=step_name if not error else f"{step_name}_error",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        cost_estimate=round(cost, 8),
    )
    db.add(step)
    await db.commit()
