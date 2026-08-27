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

# Hard cap per GitHub input (chars). Keeps prompt within model context window
# and prevents accidental cost explosions on huge monorepo logs.
CAP_CHARS: int = 8_000

# Per-fetch timeout. One hung GitHub request must not stall the gather > 30s.
FETCH_TIMEOUT_S: float = 30.0

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
    # 4. Single LLM call — all three inputs fused into one user message
    # -------------------------------------------------------------------------
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

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    llm = LLMClient(timeout=FETCH_TIMEOUT_S)

    t0 = time.monotonic()
    try:
        response = await asyncio.wait_for(
            llm.complete(messages=messages, db=db, repo_id=repo.id, max_tokens=512),
            timeout=FETCH_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.error(
            "context_gatherer: LLM call timed out for run %s", run.id
        )
        await _persist_run_step(
            db=db,
            run_id=run.id,
            step_name="context_gatherer",
            input_tokens=0,
            output_tokens=0,
            latency_ms=int((time.monotonic() - t0) * 1000),
            error=True,
        )
        raise
    latency_ms = int((time.monotonic() - t0) * 1000)

    summary: str = (response.get("content") or "").strip()
    usage: dict[str, int] = response.get("usage", {})
    input_tokens: int = usage.get("input_tokens", 0)
    output_tokens: int = usage.get("output_tokens", 0)

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
    # 6. Return distilled summary only
    # -------------------------------------------------------------------------
    logger.info(
        "context_gatherer: run=%s input_tokens=%d output_tokens=%d latency_ms=%d",
        run.id,
        input_tokens,
        output_tokens,
        latency_ms,
    )
    return summary


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
