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
import os
import re
import time
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import github_client as gh
from app.llm import LLMClient
from app.models import Attempt, Repo, Run, RunStep
from app.subagents.ast_analyzer import (
    extract_generic_symbol_context,
    extract_python_ast_context,
    extract_stack_frames,
    format_ast_context,
)

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
    # GitHub runner registration tokens: ghr_...
    (re.compile(r"\bghr_[A-Za-z0-9_]{10,}"), "[REDACTED]"),
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


def _discover_candidate_files_to_inspect(logs_text: str, repo_paths: list[str]) -> tuple[list[str], list[str]]:
    """
    Identify up to 2 candidate implementation files related to the failure,
    plus up to 1 candidate test file.
    Matches test file stems against repo tree, prioritizing core/services logic over routers.
    """
    candidates: list[str] = []
    test_files: list[str] = []

    # 1. Match test file names (e.g. tests/test_analytics.py or test_analytics.py)
    test_file_matches = re.findall(r"(?:[\w\-/]+/)?(test_[\w\-]+)\.(?:py|ts|js)", logs_text or "")
    test_stems = set(test_file_matches)

    matching_files: list[str] = []
    for t_stem in test_stems:
        core_name = t_stem[5:] if t_stem.startswith("test_") else t_stem
        for p in repo_paths:
            basename = os.path.basename(p)
            base_stem = os.path.splitext(basename)[0]
            if not p.startswith("test") and "/test" not in p and "/tests/" not in p:
                if base_stem == core_name or base_stem == t_stem:
                    if p not in matching_files:
                        matching_files.append(p)
            else:
                if (base_stem == t_stem or base_stem == f"test_{core_name}") and p not in test_files:
                    test_files.append(p)

    # Prioritize core logic over routers: core/, services/, models/ > api/, routers/
    def _priority_score(path: str) -> int:
        norm = path.replace("\\", "/").lower()
        if "/core/" in norm or "/services/" in norm or "/models/" in norm:
            return 0
        if "/lib/" in norm or "/utils/" in norm:
            return 1
        if "/routers/" in norm or "/api/" in norm:
            return 3
        return 2

    matching_files.sort(key=_priority_score)
    for mf in matching_files:
        if mf not in candidates:
            candidates.append(mf)

    # 2. Match implementation files mentioned directly in tracebacks
    trace_files = re.findall(
        r"(?:File\s+[\"']|[\s/])([\w\-/]+\.(?:py|ts|js))[\"']?,\s+line\s+\d+",
        logs_text or "",
    )
    for tf in trace_files:
        norm_tf = tf.replace("\\", "/").strip()
        for p in repo_paths:
            if (p == norm_tf or p.endswith("/" + norm_tf)) and not p.startswith("test") and "/test" not in p and "/tests/" not in p:
                if p not in candidates:
                    candidates.append(p)

    return candidates[:2], test_files[:1]


def extract_failing_test_target(text: str) -> Optional[str]:
    """
    Extract candidate failing test path or test identifier from failure logs or diagnosis summary.

    Handles:
      1. Explicit markdown section: '## Failing Test Files ... ### <path>'
      2. Pytest FAILED report line: 'FAILED tests/test_foo.py::test_bar'
      3. Pytest path::test format: 'tests/test_foo.py::test_bar'
      4. Jest/Vitest FAIL report line: 'FAIL src/components/foo.test.ts'
      5. Standalone test file path: 'tests/test_something.py' or 'src/something.test.ts'
    """
    if not text or not text.strip():
        return None

    # 1. Section header in summary: "## Failing Test Files ... ### <path>"
    m_section = re.search(r"## Failing Test Files[^\n]*\n+###\s*([^\s\n\r`]+)", text)
    section_path = m_section.group(1).strip() if m_section else None

    # 2. Pytest explicit FAILED line: e.g. "FAILED tests/test_foo.py::test_bar"
    m_failed = re.search(
        r"(?:FAIL|FAILED)\s+([a-zA-Z0-9_\-./]+(?:::[a-zA-Z0-9_]+)?)",
        text,
    )
    if m_failed:
        cand = m_failed.group(1).strip().rstrip(":")
        return cand

    # 3. Path with test function: e.g. "tests/test_analytics.py::test_metrics_calc"
    m_py_func = re.search(
        r"([a-zA-Z0-9_\-./]+(?:test_[a-zA-Z0-9_\-]+|[a-zA-Z0-9_\-]+_test)\.(?:py|ts|js)::[a-zA-Z0-9_]+)",
        text,
    )
    if m_py_func:
        return m_py_func.group(1).strip()

    # 4. If section path found, use it
    if section_path:
        return section_path

    # 5. Standalone test file path: tests/test_foo.py or foo.test.ts or test_foo.py
    m_test_file = re.search(
        r"([a-zA-Z0-9_\-./]*(?:test_[a-zA-Z0-9_\-]+|[a-zA-Z0-9_\-]+_test|[a-zA-Z0-9_\-]+\.test|[a-zA-Z0-9_\-]+\.spec)\.(?:py|ts|js))",
        text,
    )
    if m_test_file:
        return m_test_file.group(1).strip()

    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _safe_fetch(coro: Any, label: str) -> Any:
    """
    Await `coro` with FETCH_TIMEOUT_S. On timeout or any exception, log a
    structured warning and return an empty string/result. Never propagates exceptions
    so asyncio.gather can still collect results from the other two fetches.
    """
    try:
        result = await asyncio.wait_for(coro, timeout=FETCH_TIMEOUT_S)
        if isinstance(result, list):
            return result
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


def extract_reviewer_feedback(diagnosis_summary: str) -> Optional[str]:
    """
    Extract the reviewer feedback instruction from a PR feedback diagnosis summary.
    """
    if not diagnosis_summary:
        return None
    marker = "## Reviewer Feedback"
    if marker not in diagnosis_summary:
        return None
    section = diagnosis_summary.split(marker, 1)[1]
    if "\n## " in section:
        section = section.split("\n## ", 1)[0]
    return section.strip() or None


async def gather_pr_feedback_context(
    run: Run,
    repo: Repo,
    db: AsyncSession,
) -> str:
    """
    Context gatherer for interactive PR feedback loop (run.parent_run_id is set).

    Fetches:
      1. Reviewer comment body and preceding PR comment thread via GitHub Issues API.
      2. Previous verified attempt patch and strategy notes from parent run's Attempt.
      3. File diff of the existing PR branch.
    Redacts all secrets via _redact_secrets() before assembling into diagnosis_summary.
    """
    t0 = time.monotonic()
    owner = repo.owner
    name = repo.name
    pr_number = run.pr_number
    pr_branch = run.pr_branch or run.head_branch

    token: Optional[str] = None
    try:
        from app.github.pr import get_installation_token
        token = await get_installation_token(repo)
    except Exception:
        token = None

    # 1. Fetch parent run's attempt
    prior_patch = ""
    prior_strategy_notes = ""
    prior_attempt_num = 1
    if run.parent_run_id:
        stmt = (
            select(Attempt)
            .where(Attempt.run_id == run.parent_run_id)
            .order_by(Attempt.attempt_number.desc())
        )
        parent_attempts = (await db.scalars(stmt)).all()
        prior_attempt = next(
            (a for a in parent_attempts if a.verification_status == "pass"),
            parent_attempts[0] if parent_attempts else None,
        )
        if prior_attempt:
            prior_patch = prior_attempt.patch_text or ""
            prior_strategy_notes = prior_attempt.strategy_notes or ""
            prior_attempt_num = prior_attempt.attempt_number

    # 2. Fetch PR comments & diff concurrently
    comments_raw: Any = []
    diff_raw: str = ""
    if pr_number:
        comments_raw, diff_raw = await asyncio.gather(
            _safe_fetch(
                gh.fetch_pr_comments(owner=owner, repo=name, pr_number=pr_number, token=token),
                label="pr_comments",
            ),
            _safe_fetch(
                gh.fetch_diff(owner=owner, repo=name, sha=pr_branch, token=token),
                label="pr_branch_diff",
            ),
        )

    # 3. Format and extract reviewer critique from comment thread
    comments_list = comments_raw if isinstance(comments_raw, list) else []
    formatted_comments: list[str] = []
    latest_reviewer_instruction = ""

    for item in comments_list:
        if not isinstance(item, dict):
            continue
        c_body = item.get("body", "")
        author = item.get("user", {}).get("login", "unknown") if isinstance(item.get("user"), dict) else "unknown"
        assoc = item.get("author_association", "NONE")
        formatted_comments.append(f"Comment by @{author} ({assoc}):\n{c_body.strip()}\n")
        if "@haunter" in c_body.lower():
            latest_reviewer_instruction = c_body.strip()

    if not latest_reviewer_instruction and formatted_comments:
        latest_reviewer_instruction = formatted_comments[-1]

    # 4. Redact secrets across all assembled sections
    clean_instruction = _redact_secrets(latest_reviewer_instruction)
    clean_comments = _redact_secrets("\n---\n".join(formatted_comments)) if formatted_comments else "(no comments found)"
    clean_patch = _redact_secrets(prior_patch) if prior_patch else "(no previous patch)"
    clean_notes = _redact_secrets(prior_strategy_notes) if prior_strategy_notes else "(none)"
    clean_diff = _redact_secrets(str(diff_raw or "")) if diff_raw else "(no branch diff available)"

    # Truncate to CAP_CHARS
    clean_instruction = clean_instruction[:CAP_CHARS]
    clean_comments = clean_comments[:CAP_CHARS]
    clean_patch = clean_patch[:CAP_CHARS]
    clean_notes = clean_notes[:CAP_CHARS]
    clean_diff = clean_diff[:CAP_CHARS]

    # 5. Assemble diagnosis_summary
    sections = [
        f"## Reviewer Feedback\n{clean_instruction}",
        f"## Preceding PR Comments\n{clean_comments}",
        f"## Previous Verified Patch (Attempt #{prior_attempt_num})\n```diff\n{clean_patch}\n```",
        f"## Previous Strategy Notes\n{clean_notes}",
        f"## Existing PR Branch Diff\n```diff\n{clean_diff}\n```",
    ]
    summary = "\n\n".join(sections)

    latency_ms = int((time.monotonic() - t0) * 1000)
    await _persist_run_step(
        db=db,
        run_id=run.id,
        step_name="context_gatherer",
        input_tokens=0,
        output_tokens=0,
        latency_ms=latency_ms,
        error=False,
    )

    logger.info(
        "context_gatherer: run=%s assembled PR feedback context (comments=%d diff_len=%d)",
        run.id,
        len(comments_list),
        len(clean_diff),
    )
    return summary


async def gather_context(
    run: Run,
    repo: Repo,
    db: AsyncSession,
) -> str:
    """
    Gather CI failure context and return a distilled root-cause summary.

    Steps:
      1. Concurrent fetch of workflow logs, commit diff, and commit metadata.
      2. Independent character truncation (CAP_CHARS).
      3. Independent secret redaction (_redact_secrets).
      4. LLM synthesis with empty-response retry (_call_with_empty_retry).
      5. Persist run_steps trace row (tokens, latency, cost).
      6. Enrich summary with file list from diff or repo tree.
      7. Inject source code of candidate failing files.
      8. Return distilled summary string.

    Raw inputs are not stored anywhere — only the distilled summary propagates.
    """
    if run.parent_run_id is not None:
        return await gather_pr_feedback_context(run=run, repo=repo, db=db)

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
    tree_paths: list[str] = []
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
    else:
        try:
            tree_paths = await gh.fetch_repo_tree_paths(owner=owner, repo=name, sha=sha)
            if tree_paths:
                tree_section = (
                    "\n\n## Repository Files\n"
                    + "\n".join(f"- {p}" for p in tree_paths)
                )
                summary = (summary or "").rstrip() + tree_section
                logger.info(
                    "context_gatherer: run=%s appended %d repository tree path(s) to summary",
                    run.id,
                    len(tree_paths),
                )
        except Exception as exc:
            logger.warning("context_gatherer: run=%s failed to fetch repo tree paths: %s", run.id, exc)

    # -------------------------------------------------------------------------
    # 7. Discover and inject source code of candidate failing files & tests
    # -------------------------------------------------------------------------
    fetched_contents: dict[str, str] = {}
    all_known_paths = file_paths or tree_paths
    if all_known_paths:
        try:
            candidate_files, test_files = _discover_candidate_files_to_inspect(logs_clean, all_known_paths)
            for cand_path in candidate_files:
                file_content = await gh.fetch_file_content(owner=owner, repo=name, path=cand_path, sha=sha)
                if file_content:
                    fetched_contents[cand_path] = file_content
                    lines = file_content.splitlines()
                    if len(lines) > 250:
                        capped = "\n".join(lines[:250]) + "\n... (truncated)"
                    else:
                        capped = file_content
                    clean_code = _redact_secrets(capped)
                    source_section = (
                        f"\n\n## Source Code of Failing Files\n"
                        f"### {cand_path}\n"
                        f"```python\n{clean_code}\n```\n"
                    )
                    summary = (summary or "").rstrip() + source_section
                    logger.info(
                        "context_gatherer: run=%s injected source code for %s (%d lines)",
                        run.id,
                        cand_path,
                        len(lines),
                    )

            for test_path in test_files:
                test_content = await gh.fetch_file_content(owner=owner, repo=name, path=test_path, sha=sha)
                if test_content:
                    fetched_contents[test_path] = test_content
                    lines = test_content.splitlines()
                    if len(lines) > 200:
                        capped = "\n".join(lines[:200]) + "\n... (truncated)"
                    else:
                        capped = test_content
                    clean_test = _redact_secrets(capped)
                    test_section = (
                        f"\n\n## Failing Test Files (for reference — do NOT modify test files)\n"
                        f"### {test_path}\n"
                        f"```python\n{clean_test}\n```\n"
                    )
                    summary = (summary or "").rstrip() + test_section
                    logger.info(
                        "context_gatherer: run=%s injected test file %s (%d lines)",
                        run.id,
                        test_path,
                        len(lines),
                    )
        except Exception as exc:
            logger.warning("context_gatherer: run=%s failed to inject candidate source/test code: %s", run.id, exc)

    # -------------------------------------------------------------------------
    # 8. Deep AST & Symbol Call-Graph Context Expansion (Feature 3)
    #    Parses stack trace frames and extracts enclosing function / class scopes
    #    for frames within the target repo (max 5 frames to guard rate limits).
    # -------------------------------------------------------------------------
    try:
        combined_paths = list(file_paths)
        if not tree_paths:
            try:
                tree_paths = await gh.fetch_repo_tree_paths(owner=owner, repo=name, sha=sha)
            except Exception as exc:
                logger.warning("context_gatherer: run=%s failed to fetch repo tree for AST: %s", run.id, exc)

        if tree_paths:
            for tp in tree_paths:
                if tp not in combined_paths:
                    combined_paths.append(tp)

        frames = extract_stack_frames(logs_clean, repo_paths=combined_paths or None, max_frames=5)
        if frames:
            ast_sections: list[str] = []
            for frame in frames:
                content = fetched_contents.get(frame.file_path)
                if not content:
                    content = await gh.fetch_file_content(owner=owner, repo=name, path=frame.file_path, sha=sha)
                    if content:
                        fetched_contents[frame.file_path] = content

                if content:
                    if frame.file_path.endswith(".py"):
                        ast_ctx = extract_python_ast_context(content, frame.line_number)
                    else:
                        ast_ctx = extract_generic_symbol_context(content, frame.line_number, frame.symbol_name)

                    if ast_ctx:
                        formatted = format_ast_context(frame, ast_ctx)
                        clean_formatted = _redact_secrets(formatted)
                        ast_sections.append(clean_formatted)

            if ast_sections:
                ast_header = "\n\n## Enclosing Scope & Symbol Context\n"
                summary = (summary or "").rstrip() + ast_header + "\n\n".join(ast_sections)
                logger.info(
                    "context_gatherer: run=%s injected AST & symbol context for %d frame(s)",
                    run.id,
                    len(ast_sections),
                )
    except Exception as exc:
        logger.warning("context_gatherer: run=%s failed to extract AST symbol context: %s", run.id, exc)

    # -------------------------------------------------------------------------
    # 9. Return distilled summary (with file list and symbol context appended)
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


def _is_empty_summary(content: Optional[str]) -> bool:
    if not content:
        return True
    cleaned = re.sub(r"```[a-zA-Z]*\n?```", "", content)
    cleaned = cleaned.replace("```", "").strip()
    return not bool(cleaned)


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

    first_latency_ms = int(
        response.get("latency_ms")
        if response.get("latency_ms") is not None
        else (time.monotonic() - t0) * 1000
    )
    first_content = (response.get("content") or "").strip()
    first_usage = response.get("usage", {}) or {}
    first_in = int(first_usage.get("input_tokens", 0))
    first_out = int(first_usage.get("output_tokens", 0))

    if not _is_empty_summary(first_content):
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

    retry_latency_ms = int(
        retry_response.get("latency_ms")
        if retry_response.get("latency_ms") is not None
        else (time.monotonic() - t1) * 1000
    )
    retry_content = (retry_response.get("content") or "").strip()
    retry_usage = retry_response.get("usage", {}) or {}
    retry_in = int(retry_usage.get("input_tokens", 0))
    retry_out = int(retry_usage.get("output_tokens", 0))

    if not _is_empty_summary(retry_content):
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
    await _persist_run_step(
        db=db,
        run_id=run.id,
        step_name="context_gatherer",
        input_tokens=total_in,
        output_tokens=total_out,
        latency_ms=total_latency,
        error=True,
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
