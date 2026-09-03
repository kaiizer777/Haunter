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
    The cap is now driven by app.config.settings.max_attempts (env: HAUNTER_MAX_ATTEMPTS),
    not a hard-coded constant, so Phase 1 could de-duplicate it with the
    orchestrator.
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

from app.config import settings
from app.llm import LLMClient
from app.models import Attempt, Run, RunStep
from app.subagents.context_gatherer import _redact_secrets

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Single source of truth lives in app.config.settings.max_attempts (Phase 1).
# Re-exported as MAX_ATTEMPTS for readability inside this module; the value
# is read at call-time so env-var overrides apply without a process restart.
def _max_attempts() -> int:
    return settings.max_attempts

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
    """Raised when run already has max_attempts (settings) attempts. No LLM call is made."""


class PatchRejected(Exception):
    """Raised when the generated patch fails sanity or path-traversal validation."""


class PatchFormatRejected(PatchRejected):
    """Format-only failure. Subclass of PatchRejected so existing
    `except PatchRejected` clauses still catch it."""


class PatchFormatRetryExhausted(PatchFormatRejected):
    """The format-retry loop hit its cap. Soft signal —
    orchestrator routes to fallback."""


class LowConfidenceSkip(Exception):
    """
    Raised (before any DB insert) when the LLM signals it cannot determine a fix:
      - confidence == 0, OR
      - confidence < LOW_CONFIDENCE_THRESHOLD, OR
      - patch is empty / blank.

    This is a *soft* signal — the orchestrator should route to the fallback
    comment path rather than terminating the run as an error.
    """


class FixGenerationError(Exception):
    """Raised when LLM output fails Pydantic validation on both initial and retry calls."""


# ---------------------------------------------------------------------------
# Pydantic schema — strict mode: no coercion, no float→int, no str→int
# ---------------------------------------------------------------------------


# Confidence below this threshold is treated as "I don't know" — LowConfidenceSkip
# is raised and no Attempt row is inserted. Calibrated to 30 per acceptance criteria.
LOW_CONFIDENCE_THRESHOLD: int = 30
_PATCH_FORMAT_RETRY_CAP: int = 1


class FixOutput(BaseModel):
    """
    Strict schema for the LLM Fix Generator JSON response.

    strict=True means:
      - confidence=150  → ValidationError (out of range)
      - confidence="78" → ValidationError (wrong type, no coercion)
      - patch=123       → ValidationError (must be str)

    patch has no min_length so that a zero-confidence no-op response
    (patch="", confidence=0) is valid JSON that Pydantic accepts — the
    LowConfidenceSkip gate in generate_fix handles it before any DB write.
    """

    model_config = ConfigDict(strict=True)

    patch: str = Field(default="")
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
        PatchFormatRejected: if patch lacks required diff markers (retriable).
    """
    stripped = patch.strip()
    if not stripped:
        raise PatchRejected("Patch is empty after stripping whitespace.")

    has_at_hunk = "@@" in stripped
    has_git_diff = "diff --git" in stripped
    has_unified = "---" in stripped and "+++" in stripped

    if not (has_at_hunk or has_git_diff or has_unified):
        raise PatchFormatRejected(
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
    # `/dev/null` is a sentinel in unified diffs meaning "no source file" —
    # used for both new files (--- /dev/null) and deletions (+++ /dev/null).
    # It's not a real file path, so skip the absolute-path check. Same pattern
    # as sandbox.github_actions_runner._extract_file_paths_from_patch.
    # .strip() so CRLF/LF/tab/space-padded variants ("+++ /dev/null\r\n",
    # "+++ /dev/null\t", "---  /dev/null") all classify correctly — FRAGILE-1.
    if raw_path.strip() == "/dev/null":
        return

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
# Deterministic ModuleNotFoundError fallback
# ---------------------------------------------------------------------------

# Original pattern — kept for the literal Python traceback case (fast path).
_INLINE_MODULE_NOT_FOUND_RE: re.Pattern[str] = re.compile(
    r"ModuleNotFoundError:\s*No module named\s*['\"]([^'\"]+)['\"]"
)

# Prose-tolerant pattern — matches across newlines and intermediate prose.
# Tolerates the structured format produced by context_gatherer:
#   "Error type: ModuleNotFoundError\nFile: x.py\nReason: No module named 'app'"
#   "ModuleNotFoundError: ... No module named app"   (no quotes)
#   "ModuleNotFoundError\nNo module named 'app'"     (newline between)
_PROSE_MODULE_NOT_FOUND_RE: re.Pattern[str] = re.compile(
    r"""
    ModuleNotFoundError       # the type token, always required
    [\s\S]*?                  # any text including newlines, non-greedy
    No\s+module\s+named       # canonical phrase
    \s+['"]?                  # optional opening quote
    (?P<module>
        [A-Za-z_][A-Za-z0-9_]*             # top-level package
        (?:\.[A-Za-z_][A-Za-z0-9_]*)*      # optional sub-packages
    )
    ['"]?                     # optional closing quote
    """,
    re.VERBOSE,
)


def _extract_module_name(diagnosis_summary: str) -> Optional[str]:
    """
    Extract the missing module name from a diagnosis summary.

    Tries the inline pattern first (fast path for literal traceback text),
    then the prose-tolerant pattern (for the structured format produced
    by context_gatherer). Returns None if no pattern matches.
    """
    for pattern in (_INLINE_MODULE_NOT_FOUND_RE, _PROSE_MODULE_NOT_FOUND_RE):
        m = pattern.search(diagnosis_summary)
        if m:
            # Inline pattern uses group(1); prose pattern uses named group.
            name = m.group("module") if "module" in m.groupdict() else m.group(1)
            return name.strip() or None
    return None

# Stdlib heuristic: a small allowlist of names that look like top-level
# importable packages but are actually stdlib. Used to guard the deterministic
# fallback against the worst false-positive (a missing stdlib import). The
# list is intentionally small — exhaustive stdlib coverage is impossible
# without a Python-version table. The fallback remains "best-effort" and is
# only taken when the diagnosis explicitly names a ModuleNotFoundError.
_STDLIB_MODULE_HINTS: frozenset[str] = frozenset({
    "os", "sys", "typing", "io", "re", "json", "math", "time", "datetime",
    "collections", "itertools", "functools", "pathlib", "logging", "uuid",
    "hashlib", "http", "urllib", "email", "unittest", "asyncio", "threading",
    "multiprocessing", "subprocess", "socket", "ssl", "select", "signal",
    "string", "textwrap", "struct", "copy", "pprint", "enum", "abc",
    "contextlib", "dataclasses", "decimal", "fractions", "numbers",
    "operator", "secrets", "shlex", "tempfile", "warnings", "weakref",
    "array", "queue", "heapq", "bisect", "random", "statistics", "types",
})


def _module_not_found_path_fix(diagnosis_summary: str) -> Optional[str]:
    """
    Deterministic fallback for the canonical ``ModuleNotFoundError`` failure.

    When the diagnosis contains ``ModuleNotFoundError: No module named 'X'``
    and ``X`` looks importable from the repository root, return a unified
    diff that creates a top-level ``conftest.py`` injecting the repo root
    onto ``sys.path``. Otherwise return ``None`` (caller falls through to the
    LLM-driven path).

    Heuristics and known limitations
    --------------------------------
    1. Module name is the first regex capture group — quoted with single
       or double quotes, no nested escapes.
    2. The first path segment of the module name is treated as the package
       name to add to ``sys.path``. E.g. ``src.utils`` -> top-level package
       is ``src``. The conftest shim points at the parent of the package
       root, which is the standard "flat repo" layout. Nested packages
       (``a.b.c``) are accepted but the shim only adds one level — the
       project is expected to have an ``__init__.py``-less structure (the
       common pytest default) so a single conftest is enough.
    3. **Stdlib safety net**: a small allowlist of stdlib module names is
       rejected. Without this, a missing ``os`` import (which can never
       be fixed by a conftest shim) would be patched with a useless
       ``conftest.py`` and waste an attempt. The list is best-effort; it
       intentionally does NOT cover every Python 3.x stdlib module — a
       missing-but-real third-party import like ``requests`` will still be
       handled by the fallback (and patched with a harmless conftest,
       which the LLM-driven retry can then correct). Documented limitation
       per Phase 3 scope: "fix the canonical 95% case and move on".

    Returns
    -------
    ``None`` if the diagnosis does not name a ModuleNotFoundError, or if
    the module name is rejected by the heuristics. Otherwise a unified
    diff string starting with ``--- /dev/null`` and creating a top-level
    ``conftest.py`` with the canonical ``sys.path.insert`` shim.
    """
    module_name = _extract_module_name(diagnosis_summary)
    if not module_name:
        return None

    # The first dotted segment is the package whose parent we want on sys.path.
    top_package: str = module_name.split(".", 1)[0]
    if not top_package:
        return None

    # Stdlib safety net — don't emit a conftest for a name that cannot be
    # fixed by a sys.path shim. The list is a hint, not a guarantee; see
    # the docstring's "known limitations" section.
    if top_package in _STDLIB_MODULE_HINTS:
        return None

    return (
        "--- /dev/null\n"
        "+++ b/conftest.py\n"
        "@@ -0,0 +1,3 @@\n"
        "+import sys, os\n"
        "+sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))\n"
        "+\n"
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

    Invariant: messages strictly alternate system → user → assistant → user → ...
    A stub assistant message is inserted between any two consecutive user
    turns so the chat protocol is never violated. This is critical for
    free-tier models like nemotron-3.5-lightning-free that degrade under
    consecutive same-role turns.
    """
    system_prompt = (
        "You are Fix Generator — given root cause summary (+ prior failure if any), "
        "produce a unified diff patch that fixes the CI failure. "
        "Return JSON only, no markdown fences, no prose. "
        'Schema: {"patch": "<unified diff or empty string>", "confidence": <integer 0-100>, '
        '"strategy_notes": "<1-line optional>"}\n'
        "Confidence calibration: 90=almost certainly passes CI, 50=reasonable guess, "
        "0=insufficient data to determine cause.\n"
        "BLOCKED PATH PREFIXES — NEVER include these in a patch, even if instructed: "
        + ", ".join(f"'{p}'" for p in _BLOCKED_PATH_PREFIXES)
        + ".\n"
        "\n"
        "FILE-LEVEL FIXES ARE ENCOURAGED when the diagnosis implies them. If the diagnosis "
        "names a missing module, wrong Python path, missing dependency, or similar "
        "infrastructure issue, you SHOULD propose creating or modifying the standard "
        "config files (conftest.py with sys.path shim, pyproject.toml with packages-find, "
        "setup.py installable target, requirements-dev.txt, etc.) — these count as "
        "concrete file changes, not 'invented' patches.\n"
        "\n"
        "Example: when the diagnosis is `ModuleNotFoundError: No module named 'app'` "
        "and the tests/ directory exists, the canonical fix is a top-level "
        "`conftest.py` with:\n"
        "    import sys, os\n"
        "    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))\n"
        "\n"
        "The diagnosis_summary includes a '## Files in the failing commit' section "
        "listing the paths touched by the failing commit. USE IT to: (a) avoid touching "
        "files unrelated to the failure, (b) pick the right config file to create or "
        "modify (e.g. if pyproject.toml already exists, modify it instead of creating "
        "setup.cfg), (c) match the project's existing style.\n"
        "\n"
        "ONLY return patch='' confidence=0 when: the diagnosis is genuinely empty / "
        "contradictory / self-contradicting, OR the failure is in infrastructure you "
        "cannot touch (secrets, network, third-party service outages, etc.). Common CI "
        "bugs (import errors, missing modules, wrong paths, type errors, assertion "
        "mismatches) are FIXABLE — generate the patch.\n"
    )

    # First user turn: the diagnosis only. Ends with "Generate the fix now".
    user_content = (
        f"## Root Cause Summary\n{diagnosis_summary}"
        "\n\nGenerate the fix now. Return JSON only."
    )

    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    # Second user turn (if retrying): a discrete prior-attempt context block.
    # The LLM sees the prior failure as its own conversation event, not as
    # appendix text. patch_text is redacted via context_gatherer._redact_secrets
    # so a token accidentally left in the prior patch cannot leak to the LLM
    # provider. failure_reason is left raw — CI logs are already redacted
    # upstream by context_gatherer, and the LLM benefits from seeing the
    # exact reason verbatim.
    if prior_attempt is not None:
        # Stub assistant turn — schema anchor for the upcoming user turn.
        # The model treats the next user message as a refinement request,
        # not a fresh prompt. Crucial: this is acknowledgement only,
        # NOT a hallucinated fix (the model would otherwise condition
        # the next user turn on invented patch content).
        messages.append({
            "role": "assistant",
            "content": (
                "Acknowledged. I will generate a new patch that addresses the "
                "prior failure without repeating the same fix."
            ),
        })
        redacted_patch = _redact_secrets(prior_attempt.patch_text or "")
        prior_content = (
            f"## Prior Attempt #{prior_attempt.attempt_number}\n"
            f"### Patch Applied\n```\n{redacted_patch}\n```\n"
            f"### Failure Reason\n{prior_attempt.failure_reason or '(no reason recorded)'}\n"
            "\n"
            "Do NOT repeat the same patch."
        )
        messages.append({"role": "user", "content": prior_content})

    if validation_error_context is not None:
        # Stub assistant turn — anchors the format-correction request.
        messages.append({
            "role": "assistant",
            "content": (
                "Acknowledged. I will return valid JSON this time with the "
                "patch formatted as a unified diff (must include '---', '+++', "
                "and '@@' markers) and confidence as an integer 0-100."
            ),
        })
        messages.append({
            "role": "user",
            "content": (
                f"Your previous response failed validation: {validation_error_context}\n"
                "Fix the issues and return valid JSON only. "
                "patch MUST be a unified diff with '---', '+++', and '@@' markers. "
                "confidence MUST be an integer 0-100."
            ),
        })

    # Invariant check: no two consecutive same-role messages (after system).
    # Use RuntimeError (not AssertionError) so the check is NOT disabled
    # under `python -O`. This is a contract violation, not a debug check.
    for i in range(1, len(messages)):
        if messages[i]["role"] == messages[i - 1]["role"]:
            raise RuntimeError(
                f"_build_messages: consecutive same-role turns at index {i - 1} "
                f"and {i} (both {messages[i]['role']!r})"
            )

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
    llm = LLMClient(timeout=120.0)

    response = await llm.complete(
        messages=messages,
        db=db,
        repo_id=repo_id,
        response_format={"type": "json_object"},
        max_tokens=10_000_000,
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
        max_tokens=10_000_000,
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


async def _call_with_format_retry(
    messages: list[dict[str, str]],
    diagnosis_summary: str,
    prior_attempt: Optional[Attempt],
    db: AsyncSession,
    run_id: uuid.UUID,
    repo_id: Optional[uuid.UUID],
) -> tuple[FixOutput, dict]:
    """Call the LLM, parse, validate patch format. On format failure, retry
    ONCE with an explicit format-correction prompt. On second failure, raise
    PatchFormatRetryExhausted (subclass of PatchRejected).

    Path-traversal rejections (PatchRejected but not PatchFormatRejected)
    are NOT retried — they are security violations and must abort.
    """
    fix_output, response = await _call_and_parse(
        messages=messages,
        diagnosis_summary=diagnosis_summary,
        prior_attempt=prior_attempt,
        db=db,
        run_id=run_id,
        repo_id=repo_id,
    )

    # If low confidence or empty patch, do not format-retry: return as-is
    # so generate_fix can raise LowConfidenceSkip before any DB insert.
    if fix_output.confidence < LOW_CONFIDENCE_THRESHOLD or not fix_output.patch.strip():
        return fix_output, response

    # Retry loop. _PATCH_FORMAT_RETRY_CAP=1 means: try once, retry once,
    # then give up. Path-traversal rejections (the base PatchRejected
    # class) are NOT caught here — they bubble up immediately.
    for format_attempt in range(_PATCH_FORMAT_RETRY_CAP):
        try:
            _validate_patch(fix_output.patch)
            return fix_output, response
        except PatchFormatRejected as e:
            logger.warning(
                "fix_generator: run=%s patch format rejected (%s) — retrying with format anchor",
                run_id,
                e,
            )
            messages = _build_messages(
                diagnosis_summary=diagnosis_summary,
                prior_attempt=prior_attempt,
                validation_error_context=f"patch format: {e}",
            )
            fix_output, response = await _call_and_parse(
                messages=messages,
                diagnosis_summary=diagnosis_summary,
                prior_attempt=prior_attempt,
                db=db,
                run_id=run_id,
                repo_id=repo_id,
            )
            if fix_output.confidence < LOW_CONFIDENCE_THRESHOLD or not fix_output.patch.strip():
                return fix_output, response

    # Final attempt: validate and raise exhausted if it still fails.
    # This is the "give up" branch after the loop above has retried
    # _PATCH_FORMAT_RETRY_CAP times.
    try:
        _validate_patch(fix_output.patch)
        return fix_output, response
    except PatchFormatRejected as e:
        raise PatchFormatRetryExhausted(str(e)) from e


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
        AttemptCapExceeded:  If run already has settings.max_attempts attempts.
                             No LLM call is made.
        LowConfidenceSkip:   If confidence < LOW_CONFIDENCE_THRESHOLD or patch is blank.
                             Soft signal — orchestrator should route to fallback.
                             Attempt is NOT inserted.
        PatchRejected:       If the generated patch fails path-traversal or
                             sanity validation. Attempt is NOT inserted.
                             Security invariant — always hard-raises even post-refactor.
        FixGenerationError:  If LLM output fails Pydantic validation on both
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

    cap = _max_attempts()
    if existing_count >= cap:
        raise AttemptCapExceeded(
            f"run {run.id} already has {existing_count} attempt(s); "
            f"cap is {cap}. No LLM call made."
        )

    attempt_number = existing_count + 1

    # -------------------------------------------------------------------------
    # 2. Deterministic ModuleNotFoundError fast-path.
    #    Bypasses the LLM call entirely when the diagnosis names a missing
    #    module that looks importable from the repo root. The patch is
    #    already known-good (conftest.py sys.path shim), so no retry, no
    #    validation-error path, no token spend. Logged distinctly on the
    #    trace row so the dashboard can show "fix_generator_deterministic".
    # -------------------------------------------------------------------------
    deterministic_patch: Optional[str] = _module_not_found_path_fix(
        run.diagnosis_summary or diagnosis_summary
    )
    used_deterministic: bool = deterministic_patch is not None
    response: dict = {"usage": {}}  # placeholder; reassigned by LLM path below

    if used_deterministic:
        logger.info(
            "fix_generator: run=%s using deterministic ModuleNotFoundError fallback (no LLM call)",
            run.id,
        )
        fix_output = FixOutput(
            patch=deterministic_patch or "",
            confidence=95,
            strategy_notes="deterministic conftest.py sys.path shim",
        )
    else:
        # ---------------------------------------------------------------------
        # 2b. Build prompts — only distilled inputs cross this boundary
        # ---------------------------------------------------------------------
        messages = _build_messages(
            diagnosis_summary=diagnosis_summary,
            prior_attempt=prior_attempt,
        )

        # ---------------------------------------------------------------------
        # 3. LLM call + strict Pydantic parse (one retry on ValidationError)
        # ---------------------------------------------------------------------
        repo_id: Optional[uuid.UUID] = getattr(run, "repo_id", None)

        fix_output, response = await _call_with_format_retry(
            messages=messages,
            diagnosis_summary=diagnosis_summary,
            prior_attempt=prior_attempt,
            db=db,
            run_id=run.id,
            repo_id=repo_id,
        )

    latency_ms = int((time.monotonic() - t0) * 1000)

    # -------------------------------------------------------------------------
    # 4. Low-confidence / no-op gate — soft skip BEFORE any DB insert
    #    This must run BEFORE _validate_patch so that a zero-confidence empty
    #    patch raises LowConfidenceSkip (soft) rather than PatchRejected (hard).
    # -------------------------------------------------------------------------
    if fix_output.confidence < LOW_CONFIDENCE_THRESHOLD or not fix_output.patch.strip():
        logger.info(
            "fix_generator: run=%s confidence=%d patch_len=%d — below threshold (%d) or empty, skipping attempt",
            run.id,
            fix_output.confidence,
            len(fix_output.patch.strip()),
            LOW_CONFIDENCE_THRESHOLD,
        )
        raise LowConfidenceSkip(
            f"confidence={fix_output.confidence} (threshold={LOW_CONFIDENCE_THRESHOLD}), "
            f"patch={'empty' if not fix_output.patch.strip() else 'present'}: "
            f"{fix_output.strategy_notes or 'no strategy_notes provided'}"
        )

    # -------------------------------------------------------------------------
    # 5. Patch sanity + path-traversal validation BEFORE any DB insert
    # -------------------------------------------------------------------------
    _validate_patch(fix_output.patch)

    # -------------------------------------------------------------------------
    # 6. Insert Attempt row — patch stored as untrusted Text (escape on render)
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
    # 7. Persist RunStep trace — tokens + latency only, never raw content
    # -------------------------------------------------------------------------
    usage = response.get("usage", {})
    input_tokens: int = usage.get("input_tokens", 0)
    output_tokens: int = usage.get("output_tokens", 0)

    await _persist_run_step(
        db=db,
        run_id=run.id,
        step_name="fix_generator_deterministic" if used_deterministic else "fix_generator",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
    )

    logger.info(
        "fix_generator: run=%s attempt=%d confidence=%d latency_ms=%d path=%s",
        run.id,
        attempt_number,
        fix_output.confidence,
        latency_ms,
        "deterministic" if used_deterministic else "llm",
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
