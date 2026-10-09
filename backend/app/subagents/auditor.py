"""Concurrent, deterministic, read-only multi-perspective audit core."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import unicodedata
import uuid
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Literal, Mapping, Optional, Sequence

import httpx
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import LLMClient
from app.llm.prompts.audit_prompts import (
    INFORMATIONAL_CONFIDENCE_THRESHOLD,
    MAX_AST_CONTEXT_CHARS,
    MAX_CATEGORY_CHARS,
    MAX_DIFF_CHARS,
    MAX_ENGINE_CHARS,
    MAX_INLINE_FIELD_CHARS,
    MAX_REMEDIATION_DIFF_CHARS,
    MAX_REPO_CONTEXT_CHARS,
    MAX_REPORT_CHARS,
    MAX_STATUS_CHARS,
    MAX_SUGGESTED_FIX_CHARS,
    MAX_TARGET_CHARS,
    MAX_TITLE_CHARS,
    PERSPECTIVES,
    PerspectiveName,
    build_perspective_messages,
    build_status_label,
    derive_blast_radius,
    format_audit_report,
    format_confidence_score,
    redact_sensitive_text,
    redact_sensitive_text_preserving_lines,
    sanitize_output_markdown,
    sanitize_output_path,
    sanitize_output_text,
)
from app.log_hygiene import sanitize_log_value
from app.subagents.ast_analyzer import (
    StackFrame,
    extract_python_ast_context_from_index,
    format_ast_context,
    parse_python_source,
)

logger = logging.getLogger(__name__)

AUDIT_MAX_DIFF_CHARS = MAX_DIFF_CHARS
PERSPECTIVE_TIMEOUT_S = 75.0
PERSPECTIVE_DEADLINE_S = 100.0
PERSPECTIVE_CONCURRENCY_LIMIT = 4
AUDIT_FETCH_TIMEOUT_S = 35.0
MAX_LLM_RESPONSE_CHARS = 32_000
MAX_FINDINGS_PER_PERSPECTIVE = 20
MAX_LINE_NUMBER = 10_000_000
MAX_GROUNDING_SPAN = 200
MAX_EXECUTIVE_SUMMARY_CHARS = 1_200
FALLBACK_ENGINE_LABEL = "haunter-auditor"

AuditSeverity = Literal["BLOCKER", "WARNING", "NOTE"]
_SEVERITY_RANK: dict[AuditSeverity, int] = {"BLOCKER": 0, "WARNING": 1, "NOTE": 2}
_PERSPECTIVE_RANK: dict[str, int] = {
    name: index for index, name in enumerate(PERSPECTIVES)
}
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)$")
_NEW_SYMBOL_RE = re.compile(
    r"^\+\s*(?:async\s+def\s+|def\s+|class\s+|export\s+(?:default\s+)?(?:async\s+)?function\s+|function\s+|(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*=)"
)
_REPO_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,255}$", re.ASCII)
_AUDIT_ID_RE = re.compile(r"^audit-[0-9a-f]{12}$", re.ASCII)
_AUDIT_TYPE_RE = re.compile(
    r"^(?:pr_audit|ci_failure_audit|ci_success_audit|manual_audit|session_audit|security_scan)$",
    re.ASCII,
)
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$", re.ASCII)
_ENGINE_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,160}$", re.ASCII)
MAX_RAW_DIFF_CHARS = 2_000_000
MAX_AST_SOURCE_FILE_CHARS = 100_000
MAX_AST_SOURCE_FILES = 8
MAX_CI_CONTEXT_CHARS = 20_000
MAX_DIFF_HUNK_LINES = 20_000
AST_ANALYSIS_TIMEOUT_S = 3.0
STRUCTURAL_SOURCE_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".jsx")

_RULE_PRESCREEN: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "hardcoded-secret-candidate",
        re.compile(
            r"(?i)(?:password|passwd|secret|api[_-]?key|token)\s*[:=]\s*['\"][^'\"]+['\"]|AKIA[0-9A-Z]{16,}"
        ),
        "Determine whether credential material is hardcoded; distinguish fixtures from production code",
    ),
    (
        "non-constant-time-token-compare",
        re.compile(r"(?i)\btoken\b.{0,40}==|==.{0,40}\btoken\b"),
        "Confirm secret comparison is constant-time",
    ),
    (
        "unsafe-execution-or-deserialization",
        re.compile(
            r"\b(?:eval|exec)\s*\(|shell\s*=\s*True|pickle\.loads?\s*\(|yaml\.load\s*\("
        ),
        "Confirm untrusted input can reach the unsafe operation",
    ),
    (
        "raw-sql-interpolation",
        re.compile(r"(?i)f['\"]\s*SELECT|SELECT.{0,80}\.format\s*\(|execute\s*\(.*%\s"),
        "Trace untrusted input into SQL construction",
    ),
    (
        "outbound-url-fetch",
        re.compile(
            r"(?i)(?:requests\.(?:get|post)|httpx\.(?:get|post|request)|urllib\.request)"
        ),
        "Check SSRF controls, redirect handling, and resolved-address pinning",
    ),
    (
        "blocking-call-candidate",
        re.compile(r"\btime\.sleep\s*\(|requests\.(?:get|post)\s*\(|\bopen\s*\("),
        "Check whether blocking I/O runs on an async event loop",
    ),
    (
        "query-loop-candidate",
        re.compile(r"^\s*(?:async\s+)?for\s+.+:\s*$", re.MULTILINE),
        "Check for N+1 queries and unbounded iteration",
    ),
    (
        "new-handler",
        re.compile(
            r"^\+.*@(?:app|router)\.(?:get|post|put|patch|delete)\s*\(|^\+.*(?:async\s+)?def\s+\w+"
        ),
        "Check authentication, object authorization, and strict schema validation",
    ),
)


def _redact_audit_secrets(value: str) -> str:
    return redact_sensitive_text(value)


def _clip(value: str, maximum: int, marker: str = "\n[TRUNCATED]") -> str:
    if len(value) <= maximum:
        return value
    suffix = marker[: max(0, maximum)]
    return value[: max(0, maximum - len(suffix))] + suffix


def _clean_generated_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    redacted = _redact_audit_secrets(value)
    return (
        _CONTROL_RE.sub("", redacted).replace("\r\n", "\n").replace("\r", "\n").strip()
    )


def _safe_int(value: Any, minimum: int, maximum: int, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = fallback
    return max(minimum, min(maximum, parsed))


def _validate_repo_coordinates(owner: Any, repo: Any) -> tuple[str, str]:
    if not isinstance(owner, str) or not isinstance(repo, str):
        raise ValueError("GitHub owner and repository must be strings")
    if not _REPO_COMPONENT_RE.fullmatch(owner) or not _REPO_COMPONENT_RE.fullmatch(
        repo
    ):
        raise ValueError("GitHub owner or repository is invalid")
    return owner, repo


def _validate_repo_full_name(value: str) -> str:
    owner, separator, repo = value.partition("/")
    if not separator or "/" in repo:
        raise ValueError("repository must use owner/name format")
    return "/".join(_validate_repo_coordinates(owner, repo))


def _validate_audit_id(value: str) -> str:
    if not isinstance(value, str) or not _AUDIT_ID_RE.fullmatch(value):
        raise ValueError("audit_id is invalid")
    return value


def _validate_audit_type(value: str) -> str:
    if not isinstance(value, str) or not _AUDIT_TYPE_RE.fullmatch(value):
        raise ValueError("audit_type is invalid")
    return value


def _validate_sha(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise ValueError("head_sha must be a 40-character hexadecimal SHA")
    return value


def _validate_pr_number(value: Optional[int]) -> Optional[int]:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= 2_147_483_647
    ):
        raise ValueError("pull request number is invalid")
    return value


def _normalize_repo_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return PurePosixPath(normalized).as_posix()


def _validate_repo_path(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if any(
        character.isspace() or unicodedata.category(character).startswith("C")
        for character in value
    ):
        raise ValueError("file_path contains whitespace or control characters")
    normalized = _normalize_repo_path(value)
    if normalized in ("", ".") or len(normalized) > 512:
        raise ValueError("file_path must be a bounded repository-relative path")
    if normalized.startswith("/") or _WINDOWS_DRIVE_RE.match(normalized):
        raise ValueError("file_path must be repository-relative")
    if ".." in PurePosixPath(normalized).parts:
        raise ValueError("file_path must not traverse parent directories")
    if "`" in normalized:
        raise ValueError("file_path contains forbidden characters")
    return normalized


class PerspectiveFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    file_path: str = Field(min_length=1, max_length=512)
    line_start: int = Field(ge=1, le=MAX_LINE_NUMBER)
    line_end: int = Field(ge=1, le=MAX_LINE_NUMBER)
    severity: AuditSeverity
    category: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=3, max_length=240)
    description: str = Field(
        min_length=5,
        max_length=1_200,
        validation_alias=AliasChoices("description", "impact"),
    )
    suggested_fix: Optional[str] = Field(
        default=None, max_length=MAX_SUGGESTED_FIX_CHARS
    )
    confidence: int = Field(ge=0, le=100)

    @field_validator("file_path", mode="before")
    @classmethod
    def _validate_file_path(cls, value: Any) -> Any:
        return _validate_repo_path(value)

    @field_validator("severity", mode="before")
    @classmethod
    def _normalize_severity(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("category", "title", "description", "suggested_fix", mode="before")
    @classmethod
    def _clean_text(cls, value: Any) -> Any:
        return _clean_generated_text(value)

    @model_validator(mode="after")
    def _normalize_line_range(self) -> "PerspectiveFinding":
        if self.line_end < self.line_start:
            self.line_end = self.line_start
        return self


class PerspectiveOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    summary: str = Field(min_length=1, max_length=MAX_EXECUTIVE_SUMMARY_CHARS)
    confidence: int = Field(ge=0, le=100)
    findings: list[PerspectiveFinding] = Field(
        default_factory=list,
        max_length=MAX_FINDINGS_PER_PERSPECTIVE,
    )

    @field_validator("summary", mode="before")
    @classmethod
    def _clean_summary(cls, value: Any) -> Any:
        return _clean_generated_text(value)


@dataclass(frozen=True)
class PerspectiveResult:
    perspective: PerspectiveName
    summary: str
    confidence: int
    findings: list[PerspectiveFinding] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    model: str = ""
    succeeded: bool = True


@dataclass(frozen=True)
class AuditFinding:
    id: str
    file_path: str
    line_start: int
    line_end: int
    perspective: PerspectiveName
    severity: AuditSeverity
    category: str
    title: str
    description: str
    suggested_fix: Optional[str]
    confidence: int
    informational_only: bool = False

    @property
    def impact(self) -> str:
        return self.description

    def _as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "file_path": self.file_path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "perspective": self.perspective,
            "severity": self.severity,
            "category": self.category,
            "title": self.title,
            "description": self.description,
            "suggested_fix": self.suggested_fix,
            "confidence": self.confidence,
            "informational_only": self.informational_only,
        }

    def to_dict(self) -> dict[str, Any]:
        """Sanitized structured-output boundary.

        A finding is built from model output and from diff-derived paths, and it
        leaves the process as JSON that a dashboard, an API client or a comment
        bot may render. Every free-text field therefore passes the canonical
        output sanitizer: credential redaction, control-character stripping, a
        length bound, and HTML escaping so no consumer can be handed a live
        ``<script>`` tag by a finding title.
        """
        raw = self._as_dict()
        suggested = raw["suggested_fix"]
        return {
            **raw,
            "file_path": sanitize_output_path(raw["file_path"]),
            "category": sanitize_output_text(raw["category"], MAX_CATEGORY_CHARS),
            "title": sanitize_output_text(raw["title"], MAX_TITLE_CHARS),
            "description": sanitize_output_text(
                raw["description"], MAX_INLINE_FIELD_CHARS
            ),
            "suggested_fix": (
                sanitize_output_text(suggested, MAX_SUGGESTED_FIX_CHARS)
                if isinstance(suggested, str) and suggested.strip()
                else None
            ),
        }

    def to_report_dict(self) -> dict[str, Any]:
        """Unescaped projection for :func:`format_audit_report`.

        The Markdown renderer escapes, re-bounds and neutralizes markup for every
        field it prints, so it must not be fed the output-boundary projection:
        it would escape the entities in that projection a second time and a
        reader would see ``&amp;lt;`` where the code said ``&lt;``.
        """
        return self._as_dict()


@dataclass
class AuditResult:
    audit_id: str
    audit_type: str
    repo_full_name: str
    target_label: str
    engine: str
    executive_summary: str
    findings: list[AuditFinding] = field(default_factory=list)
    confidence: int = 0
    status: str = ""
    remediation_diff: str = ""
    report_markdown: str = ""
    analysis_metadata: str = ""
    perspectives: list[PerspectiveResult] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    publish_allowed: bool = False

    @property
    def severity_counts(self) -> dict[str, int]:
        counts = {"BLOCKER": 0, "WARNING": 0, "NOTE": 0}
        for finding in self.findings:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
        return counts

    @property
    def informational_only(self) -> bool:
        return self.confidence < INFORMATIONAL_CONFIDENCE_THRESHOLD

    def to_dict(self) -> dict[str, Any]:
        """Sanitized structured-output boundary.

        Same contract as :meth:`AuditFinding.to_dict`, applied to the result
        envelope: the audit target, executive summary and analysis metadata all
        carry model- and repository-derived text and leave the process as JSON.
        ``report_markdown`` is exempt from HTML escaping because the renderer
        already escaped every field it emitted.
        """
        return {
            "audit_id": self.audit_id,
            "audit_type": self.audit_type,
            "repo_full_name": sanitize_output_path(self.repo_full_name),
            "target_label": sanitize_output_text(self.target_label, MAX_TARGET_CHARS),
            "engine": sanitize_output_text(self.engine, MAX_ENGINE_CHARS),
            "status": sanitize_output_text(self.status, MAX_STATUS_CHARS),
            "confidence": self.confidence,
            "informational_only": self.informational_only,
            "publish_allowed": self.publish_allowed,
            "severity_counts": dict(self.severity_counts),
            "executive_summary": sanitize_output_text(
                self.executive_summary, MAX_EXECUTIVE_SUMMARY_CHARS
            ),
            "findings": [finding.to_dict() for finding in self.findings],
            "report_markdown": sanitize_output_markdown(
                self.report_markdown, MAX_REPORT_CHARS
            ),
            "analysis_metadata": sanitize_output_text(
                self.analysis_metadata, MAX_AST_CONTEXT_CHARS
            ),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
        }


class AuditAnalysisError(Exception):
    """Raised when every audit perspective fails."""


@dataclass(frozen=True)
class DiffGrounding:
    line_index: dict[str, set[int]]
    valid_paths: frozenset[str]
    rejected_paths: frozenset[str]
    truncated_ranges: dict[str, set[tuple[int, int]]]
    #: Hunks whose declared old/new counts were satisfied exactly, with nothing
    #: outside them folded in. A hunk that overflowed its declared counts, whose
    #: body never arrived, or whose line numbers are out of range is *not*
    #: counted: reporting it as accepted is what let a diff with zero usable
    #: hunks announce "accepted hunks: 2".
    hunk_count: int = 0
    #: `+`/`-` lines inside accepted hunks only. Never a raw scan of the diff
    #: text, which would count the body of a hunk that was thrown away.
    added_lines: int = 0
    removed_lines: int = 0
    #: True when the diff text was cut before its last line. Every count above
    #: then describes the inspected prefix, and the report says so.
    clipped: bool = False


class AuditRemoteFailureReason(StrEnum):
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    NETWORK = "network"
    TIMEOUT = "timeout"
    PRIVATE_OR_MISSING = "private_or_missing"
    RESPONSE_LIMIT = "response_limit"
    TARGET_CHANGED = "target_changed"
    UNKNOWN = "unknown"


class AuditRemoteFetchError(RuntimeError):
    def __init__(self, reason: AuditRemoteFailureReason, message: str):
        super().__init__(message)
        self.reason = reason


class AuditDiffFetchError(AuditRemoteFetchError):
    pass


class AuditTargetFetchError(AuditRemoteFetchError):
    pass


class AuditCIContextFetchError(AuditRemoteFetchError):
    """A CI log fetch failed, so the audit ran without its primary evidence.

    Distinct from a fetch that succeeded and returned nothing. The CI log is the
    evidence a CI-triggered audit exists to reason about, and a run that could not
    read it has not audited the failure: it has audited a diff in isolation. That
    is a degraded or failed job, never a completed one.
    """


class AuditTargetChangedError(AuditRemoteFetchError):
    def __init__(self) -> None:
        super().__init__(
            AuditRemoteFailureReason.TARGET_CHANGED,
            "pull request target changed after audit dispatch",
        )


class AuditResponseTooLargeError(Exception):
    """Raised when a model response exceeds the bounded output contract."""


def _truncate_diff(diff_text: str) -> tuple[str, bool]:
    if len(diff_text) <= AUDIT_MAX_DIFF_CHARS:
        return diff_text, False
    marker = "\n[...DIFF TRUNCATED...]\n"
    prefix, _ = _diff_prefix(
        diff_text,
        AUDIT_MAX_DIFF_CHARS - len(marker),
    )
    return prefix + marker, True


@dataclass
class _DiffHunkState:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    old_seen: int = 0
    new_seen: int = 0
    next_old: int = 0
    next_new: int = 0
    added: int = 0
    removed: int = 0
    lines: set[int] = field(default_factory=set)

    @property
    def satisfied(self) -> bool:
        return self.old_seen == self.old_count and self.new_seen == self.new_count


def _diff_header_path(line: str, prefix: str) -> tuple[bool, Optional[str]]:
    if not line.startswith(prefix):
        return False, None
    raw = line[len(prefix) :].split("\t", 1)[0].strip().strip("'\"")
    if raw == "/dev/null":
        return True, None
    if raw.startswith("a/") or raw.startswith("b/"):
        raw = raw[2:]
    try:
        normalized = _validate_repo_path(raw)
    except ValueError:
        return False, None
    return False, normalized if isinstance(normalized, str) else None


def _diff_prefix(
    diff_text: str,
    maximum: int,
) -> tuple[str, bool]:
    raw = str(diff_text or "")
    if len(raw) <= maximum:
        return raw, False
    prefix = raw[:maximum]
    if not prefix.endswith("\n"):
        boundary = prefix.rfind("\n")
        prefix = prefix[: boundary + 1] if boundary >= 0 else ""
    return prefix, True


def build_diff_grounding(
    diff_text: str,
    *,
    max_chars: int = AUDIT_MAX_DIFF_CHARS,
) -> DiffGrounding:
    if isinstance(max_chars, bool) or not 1 <= max_chars <= MAX_RAW_DIFF_CHARS:
        raise ValueError("diff grounding limit is invalid")
    text, clipped = _diff_prefix(str(diff_text or ""), max_chars)
    index: dict[str, set[int]] = {}
    valid_paths: set[str] = set()
    rejected_paths: set[str] = set()
    truncated_ranges: dict[str, set[tuple[int, int]]] = {}
    current_path: Optional[str] = None
    old_dev_null = False
    new_dev_null = False
    old_path: Optional[str] = None
    new_path: Optional[str] = None
    hunk: Optional[_DiffHunkState] = None
    hunk_count = 0
    added_lines = 0
    removed_lines = 0

    def reject_current() -> None:
        if current_path is not None:
            rejected_paths.add(current_path)

    def invalidate_hunk() -> None:
        nonlocal hunk
        reject_current()
        hunk = None

    def accept_hunk() -> None:
        """Fold a hunk whose declared counts are exactly met into the result.

        Only ever called for a satisfied hunk, so a body that overflowed its
        header, a body that never arrived, and a header whose line numbers are
        out of range contribute neither a groundable line nor a count. That is
        what keeps the reported statistics a statement about evidence the report
        can actually cite.
        """
        nonlocal hunk, hunk_count, added_lines, removed_lines
        if hunk is None:
            return
        if current_path is not None:
            if hunk.lines:
                index.setdefault(current_path, set()).update(hunk.lines)
            valid_paths.add(current_path)
            hunk_count += 1
            added_lines += hunk.added
            removed_lines += hunk.removed
        hunk = None

    def mark_truncated() -> None:
        """Close out a hunk whose declared counts were never reached.

        The body may simply have been cut off by the caller's character bound,
        which `clipped` reports, and the lines that did arrive stay usable. A
        hunk that ran short because the diff text was malformed is dropped
        instead, and its file is rejected.
        """
        nonlocal hunk_count, added_lines, removed_lines
        if hunk is None or current_path is None:
            return
        if clipped:
            if hunk.lines:
                index.setdefault(current_path, set()).update(hunk.lines)
            valid_paths.add(current_path)
            hunk_count += 1
            added_lines += hunk.added
            removed_lines += hunk.removed
        if hunk.new_count > 0 and hunk.next_new <= hunk.new_start + hunk.new_count - 1:
            truncated_ranges.setdefault(current_path, set()).add(
                (hunk.next_new, hunk.new_start + hunk.new_count - 1)
            )
        if not clipped:
            # Only a genuinely short body means a malformed diff. A clipped one
            # was just cut off by the character bound, and rejecting it here put
            # the same path in `rejected_paths` and `valid_paths` at once: the
            # summary then announced a file it had just accepted as "Rejected
            # non-text or malformed", which is a statement about the diff the
            # report is citing, not about the auditor's own bound. The truncation
            # is reported on its own line, by `truncated_ranges` and `clipped`.
            reject_current()

    for line in text.splitlines():
        if hunk is not None:
            if (
                line.startswith("diff --git ")
                or line.startswith("--- ")
                or line.startswith("+++ ")
                or line.startswith("Binary files ")
                or line.startswith("GIT binary patch")
            ):
                # A file header is never read while a hunk body is open. The
                # `+++` of a forged or trailing hunk would otherwise be taken
                # for the start of another file, and everything after an
                # unsatisfied hunk is ungroundable, so the walk stops here
                # rather than guessing where the real content resumes.
                invalidate_hunk()
                break
            if line.startswith("\\ No newline at end of file"):
                continue
            valid_line = True
            if line.startswith("+"):
                valid_line = hunk.new_seen < hunk.new_count
                if valid_line:
                    hunk.lines.add(hunk.next_new)
                    hunk.next_new += 1
                    hunk.new_seen += 1
                    hunk.added += 1
            elif line.startswith("-"):
                valid_line = hunk.old_seen < hunk.old_count
                if valid_line:
                    hunk.next_old += 1
                    hunk.old_seen += 1
                    hunk.removed += 1
            elif line.startswith(" "):
                valid_line = (
                    hunk.old_seen < hunk.old_count and hunk.new_seen < hunk.new_count
                )
                if valid_line:
                    hunk.lines.add(hunk.next_new)
                    hunk.next_old += 1
                    hunk.next_new += 1
                    hunk.old_seen += 1
                    hunk.new_seen += 1
            else:
                valid_line = False
            if not valid_line:
                # A declared count is already met, so this line is one past what
                # the header promised: the header and the body disagree, and the
                # whole hunk — not just the surplus line — is untrustworthy.
                #
                # Scoped to an open count, deliberately. A hunk is closed by
                # `accept_hunk()` the moment both declared counts are satisfied,
                # so surplus *trailing* lines are never seen by this branch: they
                # are walked as top-level records, and a `+`/`-` line among them
                # matches no header, hunk or file shape and is simply skipped.
                # Detecting that would mean holding the hunk open for one extra
                # line, which this parser does not do. What is caught here is a
                # body that overruns *one side* — `@@ -10,2 +20,1 @@` with three
                # `-` lines — where a count is still open and the surplus is
                # visible while the walk is inside the hunk.
                invalidate_hunk()
                continue
            if hunk.satisfied:
                accept_hunk()
            continue

        if line.startswith("diff --git "):
            current_path = None
            old_dev_null = False
            new_dev_null = False
            old_path = None
            new_path = None
            continue
        if line.startswith("--- "):
            old_dev_null, old_path = _diff_header_path(line, "--- ")
            if not old_dev_null and old_path is None:
                reject_current()
            continue
        if line.startswith("+++ "):
            new_dev_null, new_path = _diff_header_path(line, "+++ ")
            if (
                new_dev_null
                or new_path is None
                or (not old_dev_null and old_path is None)
            ):
                # A deleted file (`+++ /dev/null`), an unusable header, or a
                # new-side header with no old-side counterpart has no new-file
                # line for a finding to be grounded on.
                current_path = None
                reject_current()
                continue
            current_path = new_path
            continue
        if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            reject_current()
            current_path = None
            continue
        hunk_match = _HUNK_RE.match(line)
        if hunk_match is None:
            continue
        if current_path is None:
            reject_current()
            continue
        old_start = int(hunk_match.group(1))
        old_count = int(hunk_match.group(2) or 1)
        new_start = int(hunk_match.group(3))
        new_count = int(hunk_match.group(4) or 1)
        if (
            old_start > MAX_LINE_NUMBER
            or new_start > MAX_LINE_NUMBER
            or old_count < 0
            or new_count < 0
            or old_count > MAX_DIFF_HUNK_LINES
            or new_count > MAX_DIFF_HUNK_LINES
        ):
            # A header that cannot be placed inside the file, or that declares
            # more lines than a hunk is allowed to carry, is rejected whole and
            # is not counted as accepted.
            reject_current()
            continue
        hunk = _DiffHunkState(
            old_start=old_start,
            old_count=old_count,
            new_start=new_start,
            new_count=new_count,
            next_old=old_start,
            next_new=new_start,
        )
        if hunk.satisfied:
            # A header that declares no lines at all — `@@ -1,0 +1,0 @@` — is
            # satisfied on sight. Accepting it here instead of leaving it open
            # keeps the next file header from reading as a forged hunk body.
            accept_hunk()

    if hunk is not None:
        mark_truncated()
    return DiffGrounding(
        line_index={path: lines for path, lines in index.items() if lines},
        valid_paths=frozenset(valid_paths),
        rejected_paths=frozenset(rejected_paths),
        truncated_ranges=truncated_ranges,
        hunk_count=hunk_count,
        added_lines=added_lines,
        removed_lines=removed_lines,
        clipped=clipped,
    )


def build_diff_line_index(
    diff_text: str,
    *,
    max_chars: int = AUDIT_MAX_DIFF_CHARS,
) -> dict[str, set[int]]:
    return build_diff_grounding(diff_text, max_chars=max_chars).line_index


def parse_diff_files(diff_text: str) -> list[str]:
    return sorted(
        build_diff_grounding(
            diff_text,
            max_chars=MAX_RAW_DIFF_CHARS,
        ).valid_paths
    )


def _diff_grounding_stats(grounding: DiffGrounding) -> tuple[int, int, int]:
    """Files, added lines and removed lines, all from accepted hunks only.

    Reading the counts off the raw diff text instead would count the body of
    every hunk the parser threw away, so a diff with no usable hunk at all would
    still announce a plausible-looking tally.
    """
    return len(grounding.valid_paths), grounding.added_lines, grounding.removed_lines


def _bounded_structural_fallback(
    path: str,
    source: str,
    changed_lines: set[int],
) -> str:
    lines = source.splitlines()
    selected_line = min(changed_lines)
    start_line = max(1, selected_line - 20)
    end_line = min(len(lines), start_line + 60)
    excerpt = "\n".join(lines[start_line - 1 : end_line])
    language = {
        ".ts": "TypeScript",
        ".tsx": "TypeScript JSX",
        ".js": "JavaScript",
        ".jsx": "JavaScript JSX",
    }.get(PurePosixPath(path).suffix, "unsupported")
    return (
        f"AST parsing unsupported for {language} file {path}; "
        "using a bounded structural fallback that is not AST coverage. "
        f"Changed-line anchor: {selected_line}; excerpt lines {start_line}-{end_line}.\n"
        f"{excerpt}"
    )


def _redact_preserving_line_anchors(value: str) -> str:
    """Redact a rendered, line-anchored snippet without moving its lines.

    Everything an AST context is anchored by — `Line 118`, `Lines 112-133`, and
    the reader's own mapping of a snippet line back to a file line — is only
    meaningful if redaction leaves the line count alone. The canonical redactor
    collapses a PEM private key to one token, which would silently shift every
    later anchor in the same snippet.
    """
    return redact_sensitive_text_preserving_lines(value)


def _build_ast_diff_summary_sync(
    diff_text: str,
    file_contexts: Optional[Mapping[str, str]] = None,
) -> str:
    text, clipped = _diff_prefix(str(diff_text or ""), AUDIT_MAX_DIFF_CHARS)
    grounding = build_diff_grounding(
        diff_text,
        max_chars=AUDIT_MAX_DIFF_CHARS,
    )
    files = sorted(grounding.valid_paths)
    file_count, added, removed = _diff_grounding_stats(grounding)
    statistics = (
        f"Files changed: {file_count}; accepted hunks: {grounding.hunk_count}; "
        f"+{added} added / -{removed} removed lines."
    )
    if clipped:
        # The counts describe a prefix, and a report that does not say so is read
        # as a statement about the whole commit. Labelled as prefix statistics,
        # never presented as complete.
        statistics = (
            f"Prefix statistics only — the audited diff was truncated to "
            f"{AUDIT_MAX_DIFF_CHARS} characters, so every count on this line "
            f"covers the inspected prefix and not the commit. {statistics}"
        )
    parts = [statistics]
    if files:
        parts.append("Touched files:\n" + "\n".join(f"- {path}" for path in files[:50]))
        if len(files) > 50:
            parts.append(f"{len(files) - 50} additional touched files omitted")
    else:
        parts.append(
            "No complete valid text hunks were available for grounded analysis."
        )
    if clipped or grounding.truncated_ranges:
        parts.append(
            "Diff or hunk truncation was detected; affected line ranges are untrusted."
        )
    if grounding.rejected_paths:
        parts.append(
            "Rejected non-text or malformed paths: "
            + ", ".join(sorted(grounding.rejected_paths)[:50])
        )

    parsed_contexts: list[str] = []
    structural_contexts: list[str] = []
    contexts = file_contexts or {}
    # Only a file with at least one accepted new-side line can anchor AST
    # context. A touched file whose hunks are pure deletions — `@@ -1,1 +0,0 @@`
    # — is in `valid_paths` but has no line in `line_index`, and indexing it here
    # is what used to raise KeyError on `min(line_index[path])`.
    groundable = [path for path in files if grounding.line_index.get(path)]
    for path in groundable[:MAX_AST_SOURCE_FILES]:
        # Parsed raw, never pre-redacted: redaction is applied to the rendered
        # snippet below, with a line-preserving redactor. Redacting first would
        # either break the parse (a secret inside a string literal) or report
        # scope boundaries for text that is no longer the file being audited.
        source = contexts.get(path, "")
        if (
            not isinstance(source, str)
            or not source
            or len(source) > MAX_AST_SOURCE_FILE_CHARS
        ):
            continue
        if path.endswith(".py"):
            parsed = parse_python_source(source)
            if parsed is None:
                continue
            context = None
            selected_line = min(grounding.line_index[path])
            for line_number in sorted(grounding.line_index[path]):
                try:
                    candidate = extract_python_ast_context_from_index(
                        parsed, line_number
                    )
                except ValueError:
                    continue
                context = candidate
                selected_line = line_number
                if candidate.get("enclosing_symbol"):
                    break
            if context is not None:
                frame = StackFrame(file_path=path, line_number=selected_line)
                parsed_contexts.append(
                    _redact_preserving_line_anchors(format_ast_context(frame, context))
                )
        elif PurePosixPath(path).suffix in (".ts", ".tsx", ".js", ".jsx"):
            structural_contexts.append(
                _redact_preserving_line_anchors(
                    _bounded_structural_fallback(
                        path,
                        source,
                        grounding.line_index[path],
                    )
                )
            )
    if parsed_contexts:
        parts.append(
            "Parser-backed Python AST context:\n"
            + "\n\n".join(parsed_contexts[:MAX_AST_SOURCE_FILES])
        )
    else:
        parts.append(
            "No complete bounded Python source was available for parser-backed AST analysis."
        )
    if structural_contexts:
        parts.append(
            "Unsupported AST languages and bounded structural fallbacks (not AST):\n"
            + "\n\n".join(structural_contexts[:MAX_AST_SOURCE_FILES])
        )

    symbols = [
        match.group(0)[1:].strip()[:240]
        for line in text.splitlines()
        if (match := _NEW_SYMBOL_RE.match(line))
    ]
    if symbols:
        parts.append(
            "New symbol declarations observed (untrusted regex metadata, not AST):\n"
            + "\n".join(f"- {symbol}" for symbol in symbols[:50])
        )
    hits = [
        f"- {name}: {hint}"
        for name, pattern, hint in _RULE_PRESCREEN
        if pattern.search(text)
    ]
    if hits:
        parts.append(
            "UNCONFIRMED deterministic leads; verify each against changed lines before reporting:\n"
            + "\n".join(hits)
        )
    # Defence in depth over the whole rendered summary, still line-preserving so
    # a residual secret cannot move the scope anchors the snippets just declared.
    clean_summary = _redact_preserving_line_anchors("\n".join(parts))
    return _clip(clean_summary, MAX_AST_CONTEXT_CHARS)


def build_ast_diff_summary(
    diff_text: str,
    file_contexts: Optional[Mapping[str, str]] = None,
) -> str:
    return _build_ast_diff_summary_sync(diff_text, file_contexts)


async def build_ast_diff_summary_async(
    diff_text: str,
    file_contexts: Optional[Mapping[str, str]] = None,
) -> str:
    return await asyncio.wait_for(
        asyncio.to_thread(
            _build_ast_diff_summary_sync,
            diff_text,
            file_contexts,
        ),
        timeout=AST_ANALYSIS_TIMEOUT_S,
    )


_FENCE_OPEN_RE = re.compile(r"^\s*```(?:[a-zA-Z0-9_+-]*)\s*$", re.MULTILINE)
_FENCE_CLOSE_RE = re.compile(r"(?:\r?\n)?\s*```\s*$")


def _strip_markdown_fences(content: str) -> str:
    text = (content or "").strip()
    if text.startswith("```"):
        text = _FENCE_OPEN_RE.sub("", text, count=1)
        if text.rstrip().endswith("```"):
            text = _FENCE_CLOSE_RE.sub("", text.rstrip(), count=1)
        text = text.strip()
    if not text.startswith("{"):
        first_brace = text.find("{")
        last_brace = text.rfind("}")
        if first_brace >= 0 and last_brace > first_brace:
            text = text[first_brace : last_brace + 1]
    return text


def _validation_feedback(error: ValidationError) -> str:
    parts: list[str] = []
    for item in error.errors(
        include_url=False, include_context=False, include_input=False
    )[:8]:
        location = ".".join(str(part) for part in item.get("loc", ())) or "root"
        message = str(item.get("msg", "invalid value"))[:120]
        parts.append(f"{location}: {message}")
    return "; ".join(parts)[:800]


def _response_content(response: Any) -> str:
    if not isinstance(response, dict):
        raise ValueError("LLM response must be an object")
    content = response.get("content")
    if not isinstance(content, str):
        raise ValueError("LLM response content must be text")
    if len(content) > MAX_LLM_RESPONSE_CHARS:
        raise AuditResponseTooLargeError("LLM response exceeded the audit output limit")
    return _strip_markdown_fences(content)


def _usage_value(response: dict[str, Any], key: str) -> int:
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return 0
    return _safe_int(usage.get(key, 0), 0, 2_147_483_647, 0)


def _safe_metadata(value: Any, maximum: int) -> str:
    """Make an untrusted value safe to interpolate into a log line.

    Single entry point for every path, coordinate and identifier this module
    logs, so control stripping, credential redaction and the length bound cannot
    be forgotten at one call site.
    """
    return sanitize_log_value(value, maximum)


def _sanitize_perspective_output(output: PerspectiveOutput) -> PerspectiveOutput:
    data = output.model_dump()
    data["summary"] = _clean_generated_text(_redact_audit_secrets(output.summary))
    sanitized_findings: list[dict[str, object]] = []
    for finding in output.findings:
        values = finding.model_dump()
        for field_name in ("category", "title", "description", "suggested_fix"):
            value = values.get(field_name)
            if isinstance(value, str):
                values[field_name] = _clean_generated_text(_redact_audit_secrets(value))
        sanitized_findings.append(values)
    data["findings"] = sanitized_findings
    return PerspectiveOutput.model_validate(data)


async def _run_single_perspective(
    *,
    perspective: PerspectiveName,
    diff_text: str,
    ast_summary: str,
    repo_context: str,
    llm: LLMClient,
    repo_id: Optional[uuid.UUID],
) -> PerspectiveResult:
    started = time.monotonic()
    messages: list[dict[str, Any]] = [
        dict(message)
        for message in build_perspective_messages(
            perspective,
            diff_text,
            ast_summary,
            repo_context,
        )
    ]
    response = await llm.complete(
        messages=messages,
        db=None,
        repo_id=repo_id,
        max_tokens=4_096,
    )
    input_tokens = _usage_value(response, "input_tokens")
    output_tokens = _usage_value(response, "output_tokens")
    try:
        output = _sanitize_perspective_output(
            PerspectiveOutput.model_validate_json(_response_content(response))
        )
    except ValidationError as first_error:
        logger.warning(
            "auditor perspective=%s schema_retry=true error_type=ValidationError",
            perspective,
        )
        retry_messages = list(messages)
        retry_messages.extend(
            [
                {
                    "role": "assistant",
                    "content": "The previous response failed schema validation.",
                },
                {
                    "role": "user",
                    "content": (
                        "Correct these schema errors: "
                        + _validation_feedback(first_error)
                        + ". Return only the strict JSON object."
                    ),
                },
            ]
        )
        retry_response = await llm.complete(
            messages=retry_messages,
            db=None,
            repo_id=repo_id,
            max_tokens=4_096,
        )
        response = retry_response
        input_tokens += _usage_value(retry_response, "input_tokens")
        output_tokens += _usage_value(retry_response, "output_tokens")
        output = _sanitize_perspective_output(
            PerspectiveOutput.model_validate_json(_response_content(retry_response))
        )
    model = _safe_metadata(response.get("model"), MAX_ENGINE_CHARS)
    return PerspectiveResult(
        perspective=perspective,
        summary=output.summary,
        confidence=output.confidence,
        findings=list(output.findings),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=int((time.monotonic() - started) * 1000),
        model=model,
        succeeded=True,
    )


def calculate_overall_confidence(
    perspectives: Sequence[PerspectiveResult],
    expected_perspectives: Sequence[PerspectiveName] = PERSPECTIVES,
) -> int:
    by_name = {result.perspective: result for result in perspectives}
    target_perspectives = expected_perspectives or PERSPECTIVES
    complete = all(
        perspective in by_name and by_name[perspective].succeeded
        for perspective in target_perspectives
    )
    score = sum(
        by_name[perspective].confidence
        if perspective in by_name and by_name[perspective].succeeded
        else 0
        for perspective in target_perspectives
    )
    average = round(score / len(target_perspectives)) if target_perspectives else 0
    return average if complete else min(average, INFORMATIONAL_CONFIDENCE_THRESHOLD - 1)


def _stable_finding_id(
    *,
    perspective: str,
    file_path: str,
    line_start: int,
    line_end: int,
    category: str,
    title: str,
) -> str:
    material = "\x1f".join(
        (
            perspective,
            file_path.casefold(),
            str(line_start),
            str(line_end),
            category.casefold(),
            title.casefold(),
        )
    )
    return "AUD-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:12].upper()


def _apply_confidence_policy(
    finding: PerspectiveFinding,
    perspective: PerspectiveName,
    overall_confidence: int = 100,
) -> AuditFinding:
    informational_only = (
        finding.confidence < INFORMATIONAL_CONFIDENCE_THRESHOLD
        or overall_confidence < INFORMATIONAL_CONFIDENCE_THRESHOLD
    )
    severity: AuditSeverity = "NOTE" if informational_only else finding.severity
    category = _clean_generated_text(_redact_audit_secrets(finding.category))
    title = _clean_generated_text(_redact_audit_secrets(finding.title))
    description = _clean_generated_text(_redact_audit_secrets(finding.description))
    suggested_fix = (
        _clean_generated_text(_redact_audit_secrets(finding.suggested_fix))
        if finding.suggested_fix and not informational_only
        else None
    )
    return AuditFinding(
        id=_stable_finding_id(
            perspective=perspective,
            file_path=finding.file_path,
            line_start=finding.line_start,
            line_end=finding.line_end,
            category=category,
            title=title,
        ),
        file_path=finding.file_path,
        line_start=finding.line_start,
        line_end=finding.line_end,
        perspective=perspective,
        severity=severity,
        category=category,
        title=title,
        description=description,
        suggested_fix=suggested_fix,
        confidence=finding.confidence,
        informational_only=informational_only,
    )


def _finding_key(finding: PerspectiveFinding) -> tuple[str, int, int, str, str]:
    return (
        finding.file_path.casefold(),
        finding.line_start,
        finding.line_end,
        finding.category.casefold(),
        finding.title.casefold(),
    )


def _is_grounded(
    finding: PerspectiveFinding,
    grounding: DiffGrounding,
) -> bool:
    observed_lines = grounding.line_index.get(finding.file_path)
    if (
        not observed_lines
        or finding.line_end - finding.line_start >= MAX_GROUNDING_SPAN
    ):
        return False
    if not all(
        line_number in observed_lines
        for line_number in range(finding.line_start, finding.line_end + 1)
    ):
        return False
    return not any(
        finding.line_start <= truncated_end and truncated_start <= finding.line_end
        for truncated_start, truncated_end in grounding.truncated_ranges.get(
            finding.file_path,
            set(),
        )
    )


def synthesize_findings(
    perspectives: Sequence[PerspectiveResult],
    diff_text: str,
    *,
    overall_confidence: Optional[int] = None,
    max_chars: int = AUDIT_MAX_DIFF_CHARS,
) -> list[AuditFinding]:
    grounding = build_diff_grounding(
        str(diff_text or ""),
        max_chars=max_chars,
    )
    resolved_confidence = (
        calculate_overall_confidence(perspectives)
        if overall_confidence is None
        else max(0, min(100, overall_confidence))
    )
    candidates: list[tuple[PerspectiveResult, PerspectiveFinding]] = []
    for result in perspectives:
        if not result.succeeded:
            continue
        for finding in result.findings:
            if _is_grounded(finding, grounding):
                candidates.append((result, finding))
    candidates.sort(
        key=lambda item: (
            -item[1].confidence,
            _PERSPECTIVE_RANK.get(item[0].perspective, len(PERSPECTIVES)),
            item[1].file_path,
            item[1].line_start,
            item[1].line_end,
            item[1].title,
            item[1].description,
        )
    )
    seen: set[tuple[str, int, int, str, str]] = set()
    synthesized: list[AuditFinding] = []
    for result, finding in candidates:
        key = _finding_key(finding)
        if key in seen:
            continue
        seen.add(key)
        synthesized.append(
            _apply_confidence_policy(finding, result.perspective, resolved_confidence)
        )
    synthesized.sort(
        key=lambda finding: (
            _SEVERITY_RANK.get(finding.severity, 2),
            -finding.confidence,
            finding.file_path,
            finding.line_start,
            finding.id,
        )
    )
    return synthesized


def build_remediation_diff(findings: Sequence[AuditFinding]) -> str:
    sections: list[str] = []
    used = 0
    for finding in findings:
        if finding.informational_only:
            continue
        suggested = _redact_audit_secrets(finding.suggested_fix or "").strip()
        if not suggested:
            continue
        suggested = _clip(suggested, MAX_SUGGESTED_FIX_CHARS)
        if "--- " in suggested and "+++ " in suggested and "@@" in suggested:
            section = suggested
        else:
            line_count = max(1, finding.line_end - finding.line_start + 1)
            section = (
                f"--- a/{finding.file_path}\n"
                f"+++ b/{finding.file_path}\n"
                f"@@ -{finding.line_start},{line_count} +{finding.line_start},{line_count} @@\n"
                + "\n".join(
                    f"+{line}" if line.strip() else "+"
                    for line in suggested.splitlines()
                )
            )
        remaining = MAX_REMEDIATION_DIFF_CHARS - used
        if remaining <= 0:
            break
        section = _clip(section, remaining)
        sections.append(section)
        used += len(section) + 1
    if not sections:
        return "(no automated remediation suggested; see findings above)"
    return "\n".join(sections)


def _synthesize_executive_summary(
    perspectives: Sequence[PerspectiveResult],
    finding_count: int,
) -> str:
    summaries: list[str] = []
    seen: set[str] = set()
    for result in perspectives:
        summary = _clean_generated_text(_redact_audit_secrets(result.summary))
        normalized = summary.casefold()
        if summary and normalized not in seen:
            seen.add(normalized)
            summaries.append(summary)
    failed = sum(1 for result in perspectives if not result.succeeded)
    text = " ".join(summaries)
    if finding_count == 0:
        text += " No actionable findings were confirmed."
    if failed:
        text += f" {failed} perspective(s) failed and were excluded from actionable conclusions."
    return _clip(
        text.strip() or "No reliable perspective verdict was available.",
        MAX_EXECUTIVE_SUMMARY_CHARS,
    )


def _target_label(
    explicit: str,
    *,
    ref: Optional[str],
    head_sha: Optional[str],
    pr_number: Optional[int],
    repo_full_name: str,
) -> str:
    if explicit:
        label = explicit
    else:
        parts: list[str] = []
        if head_sha and re.fullmatch(r"[0-9a-fA-F]{7,64}", head_sha):
            parts.append(f"Commit `{head_sha[:12]}`")
        if isinstance(pr_number, int) and pr_number > 0:
            parts.append(f"PR `#{pr_number}`")
        if ref:
            parts.append(f"Branch `{ref}`")
        label = " / ".join(parts) if parts else "Unified diff"
    if repo_full_name and repo_full_name not in label:
        label = f"{label} ({repo_full_name})"
    return _clip(_redact_audit_secrets(label), 600)


async def fetch_audit_source_contexts(
    *,
    owner: str,
    repo: str,
    diff_text: str,
    sha: Optional[str],
    token: Optional[str] = None,
) -> dict[str, str]:
    owner, repo = _validate_repo_coordinates(owner, repo)
    validated_sha = _validate_sha(sha)
    if validated_sha is None:
        return {}
    grounding = await asyncio.wait_for(
        asyncio.to_thread(
            build_diff_grounding,
            diff_text,
            max_chars=MAX_RAW_DIFF_CHARS,
        ),
        timeout=AST_ANALYSIS_TIMEOUT_S,
    )
    paths = sorted(
        path
        for path in grounding.valid_paths
        if path.endswith(STRUCTURAL_SOURCE_SUFFIXES)
    )[:MAX_AST_SOURCE_FILES]
    if not paths:
        return {}
    semaphore = asyncio.Semaphore(PERSPECTIVE_CONCURRENCY_LIMIT)
    from app import github_client as github

    async def fetch_one(path: str) -> tuple[str, Optional[str]]:
        try:
            async with semaphore:
                content = await asyncio.wait_for(
                    github.fetch_file_content(
                        owner=owner,
                        repo=repo,
                        path=path,
                        sha=validated_sha,
                        token=token,
                        allow_global_token=False,
                    ),
                    timeout=AUDIT_FETCH_TIMEOUT_S,
                )
            if not isinstance(content, str) or len(content) > MAX_AST_SOURCE_FILE_CHARS:
                return path, None
            # Returned verbatim. This content exists to be *parsed*, and the
            # parser needs the file exactly as it is on disk: redaction here would
            # either fail the parse (a secret inside a string literal) or, worse,
            # succeed against text whose line numbering no longer matches the
            # file, so every reported scope boundary would point at the wrong
            # line. Redaction happens on the rendered snippet instead, with a
            # line-preserving redactor — see _redact_preserving_line_anchors.
            return path, content
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "auditor source_fetch_failed owner=%s repo=%s path=%s error_type=%s",
                _safe_metadata(owner, 100),
                _safe_metadata(repo, 100),
                _safe_metadata(path, 512),
                type(exc).__name__,
            )
            return path, None

    fetched = await asyncio.gather(*(fetch_one(path) for path in paths))
    return {path: content for path, content in fetched if content is not None}


async def fetch_audit_ci_context(
    *,
    owner: str,
    repo: str,
    run_id: int,
    token: Optional[str] = None,
) -> str:
    """Fetch the bounded CI log context, or fail with a typed reason.

    A swallowed failure here used to return an empty string, so an expired
    installation token, a rate limit, a network fault or a private repository
    produced a report that looked complete and cited no log. The job must be able
    to tell "the run has no logs" apart from "we could not read the run", so the
    first returns text and the second raises.
    """
    owner, repo = _validate_repo_coordinates(owner, repo)
    if (
        isinstance(run_id, bool)
        or not isinstance(run_id, int)
        or not 1 <= run_id <= 9_223_372_036_854_775_807
    ):
        raise ValueError("workflow run id is invalid")
    from app import github_client as github

    try:
        content = await asyncio.wait_for(
            github.fetch_workflow_run_logs(
                owner=owner,
                repo=repo,
                run_id=run_id,
                token=token,
                allow_global_token=False,
            ),
            timeout=AUDIT_FETCH_TIMEOUT_S,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        reason = classify_audit_remote_failure(exc)
        logger.warning(
            "auditor ci_fetch_failed owner=%s repo=%s run_id=%s reason=%s error_type=%s",
            _safe_metadata(owner, 100),
            _safe_metadata(repo, 100),
            run_id,
            reason.value,
            type(exc).__name__,
        )
        raise AuditCIContextFetchError(
            reason,
            f"CI log context fetch failed ({reason.value})",
        ) from exc
    if not isinstance(content, str):
        raise AuditCIContextFetchError(
            AuditRemoteFailureReason.UNKNOWN,
            "CI log context response was not text",
        )
    return _clip(_redact_audit_secrets(content), MAX_CI_CONTEXT_CHARS)


def classify_audit_remote_failure(exc: BaseException) -> AuditRemoteFailureReason:
    from app.github_client import (
        GitHubAuthError,
        GitHubNetworkError,
        GitHubRateLimitError,
        GitHubResourceNotFoundError,
        GitHubResponseLimitError,
    )

    if isinstance(exc, GitHubAuthError):
        return AuditRemoteFailureReason.AUTH
    if isinstance(exc, GitHubRateLimitError):
        return AuditRemoteFailureReason.RATE_LIMIT
    if isinstance(exc, GitHubResourceNotFoundError):
        return AuditRemoteFailureReason.PRIVATE_OR_MISSING
    if isinstance(exc, GitHubResponseLimitError):
        return AuditRemoteFailureReason.RESPONSE_LIMIT
    if isinstance(exc, GitHubNetworkError):
        return AuditRemoteFailureReason.NETWORK
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return AuditRemoteFailureReason.TIMEOUT
    if isinstance(exc, httpx.RequestError):
        return AuditRemoteFailureReason.NETWORK
    return AuditRemoteFailureReason.UNKNOWN


async def fetch_audit_diff(
    *,
    owner: str,
    repo: str,
    pr_number: Optional[int] = None,
    base_sha: Optional[str] = None,
    head_sha: Optional[str] = None,
    token: Optional[str] = None,
) -> str:
    owner, repo = _validate_repo_coordinates(owner, repo)
    normalized_pr = _validate_pr_number(pr_number)
    validated_base = _validate_sha(base_sha)
    validated_head = _validate_sha(head_sha)
    if normalized_pr is not None and (validated_base is None or validated_head is None):
        raise ValueError("pull request audit requires pinned base and head SHAs")
    if normalized_pr is None and validated_head is None:
        raise ValueError("commit audit requires a head SHA")
    from app import github_client as github

    try:
        return await asyncio.wait_for(
            github.fetch_diff(
                owner=owner,
                repo=repo,
                sha=validated_head or "",
                base_sha=validated_base,
                token=token,
                allow_global_token=False,
            ),
            timeout=AUDIT_FETCH_TIMEOUT_S,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        reason = classify_audit_remote_failure(exc)
        logger.warning(
            "auditor diff_fetch_failed owner=%s repo=%s pr=%s reason=%s error_type=%s",
            _safe_metadata(owner, 100),
            _safe_metadata(repo, 100),
            normalized_pr,
            reason.value,
            type(exc).__name__,
        )
        raise AuditDiffFetchError(
            reason,
            f"audited diff fetch failed ({reason.value})",
        ) from exc


async def run_audit(
    *,
    diff_text: str,
    ast_context: str = "",
    repo_context: str = "",
    db: Optional[AsyncSession] = None,
    repo_id: Optional[uuid.UUID] = None,
    audit_id: Optional[str] = None,
    audit_type: str = "pr_audit",
    repo_full_name: str = "",
    ref: Optional[str] = None,
    head_sha: Optional[str] = None,
    pr_number: Optional[int] = None,
    target_label: str = "",
    engine: str = "",
    llm_client: Optional[LLMClient] = None,
    perspectives: Optional[Sequence[PerspectiveName]] = None,
) -> AuditResult:
    """Run four fixed, bounded perspectives and return a synthesized read-only report."""
    started = time.monotonic()
    resolved_audit_type = _validate_audit_type(audit_type)
    resolved_audit_id = _validate_audit_id(audit_id or f"audit-{uuid.uuid4().hex[:12]}")
    if repo_full_name:
        repo_full_name = _validate_repo_full_name(repo_full_name)
    resolved_pr_number = _validate_pr_number(pr_number)
    resolved_head_sha = _validate_sha(head_sha)
    if engine and not _ENGINE_RE.fullmatch(engine):
        raise ValueError("engine is invalid")
    if ref is not None and (
        not isinstance(ref, str)
        or len(ref) > 255
        or any(
            character.isspace() or unicodedata.category(character).startswith("C")
            for character in ref
        )
    ):
        raise ValueError("ref is invalid")
    raw_diff = str(diff_text or "")
    if len(raw_diff) > MAX_RAW_DIFF_CHARS:
        raise ValueError("diff exceeds the bounded audit input limit")
    resolved_target = _target_label(
        target_label,
        ref=ref,
        head_sha=resolved_head_sha,
        pr_number=resolved_pr_number,
        repo_full_name=repo_full_name,
    )
    if not raw_diff.strip():
        empty = AuditResult(
            audit_id=resolved_audit_id,
            audit_type=resolved_audit_type,
            repo_full_name=repo_full_name,
            target_label=resolved_target,
            engine=engine or FALLBACK_ENGINE_LABEL,
            executive_summary="No code changes detected in the audited diff.",
            confidence=100,
            status=build_status_label(0, 0, 100),
            remediation_diff="(no automated remediation suggested; see findings above)",
            analysis_metadata="No diff was supplied; no AST or structural source analysis was performed.",
            publish_allowed=True,
        )
        report = format_audit_report(
            pr_summary=empty.executive_summary,
            executive_summary=empty.executive_summary,
            findings=[],
            confidence=empty.confidence,
            status=empty.status,
            blast_radius="**Isolated** (No changes)",
            audit_target=empty.target_label,
            engine=empty.engine,
            must_fix_count=0,
            should_fix_count=0,
            remediation_diff=empty.remediation_diff,
            analysis_metadata=empty.analysis_metadata,
            publish_allowed=empty.publish_allowed,
        )
        return AuditResult(
            audit_id=empty.audit_id,
            audit_type=empty.audit_type,
            repo_full_name=empty.repo_full_name,
            target_label=empty.target_label,
            engine=empty.engine,
            executive_summary=empty.executive_summary,
            confidence=empty.confidence,
            status=empty.status,
            remediation_diff=empty.remediation_diff,
            report_markdown=report,
            analysis_metadata=empty.analysis_metadata,
            publish_allowed=empty.publish_allowed,
        )

    # The redactor must be the line-preserving one. Grounding is built from
    # `raw_diff` (see the `synthesize_findings` call below), and the model
    # answers with a line number it read off *this* text. The canonical
    # redactor collapses a multi-line secret — a PEM private key added by a diff
    # — to a single `[REDACTED_PRIVATE_KEY]` token, which silently shifts every
    # later line of the diff the model is shown: it would count `+time.sleep(5)`
    # on visible line 7 and cite new-file line 3, a line inside the key it never
    # saw. The grounding index is right and the citation is wrong, so the finding
    # survives, pointing at the wrong code. The line-preserving redactor
    # re-emits the newlines the match consumed, so model-visible line N is raw
    # line N. See _redact_preserving_line_anchors.
    clean_diff = _redact_preserving_line_anchors(raw_diff)
    clipped_diff, was_truncated = _truncate_diff(clean_diff)
    clean_diff = clipped_diff
    ast_summary = await build_ast_diff_summary_async(raw_diff)
    supplied_ast = _redact_audit_secrets(str(ast_context or ""))
    if supplied_ast.strip():
        supplied_ast = _clip(supplied_ast, MAX_AST_CONTEXT_CHARS // 2)
        ast_summary = _clip(
            f"{ast_summary}\n\nCaller-supplied parser context:\n{supplied_ast}",
            MAX_AST_CONTEXT_CHARS,
        )
    if was_truncated:
        ast_summary = _clip(
            ast_summary
            + f"\nInput was truncated to {AUDIT_MAX_DIFF_CHARS} characters.",
            MAX_AST_CONTEXT_CHARS,
        )
    clean_repo_context = _clip(
        _redact_audit_secrets(str(repo_context or "")),
        MAX_REPO_CONTEXT_CHARS,
    )
    llm = llm_client or LLMClient(timeout=PERSPECTIVE_TIMEOUT_S)
    semaphore = asyncio.Semaphore(PERSPECTIVE_CONCURRENCY_LIMIT)

    async def run_one(
        perspective: PerspectiveName,
    ) -> tuple[PerspectiveResult, Optional[BaseException]]:
        try:
            async with semaphore:
                result = await asyncio.wait_for(
                    _run_single_perspective(
                        perspective=perspective,
                        diff_text=clean_diff,
                        ast_summary=ast_summary,
                        repo_context=clean_repo_context,
                        llm=llm,
                        repo_id=repo_id,
                    ),
                    timeout=PERSPECTIVE_DEADLINE_S,
                )
            return result, None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "auditor perspective=%s failed error_type=%s",
                perspective,
                type(exc).__name__,
            )
            return (
                PerspectiveResult(
                    perspective=perspective,
                    summary="",
                    confidence=0,
                    findings=[],
                    succeeded=False,
                ),
                exc,
            )

    active_perspectives = (
        [p for p in perspectives if p in PERSPECTIVES]
        if perspectives is not None
        else list(PERSPECTIVES)
    )
    if not active_perspectives:
        active_perspectives = list(PERSPECTIVES)

    completed = await asyncio.gather(
        *(run_one(perspective) for perspective in active_perspectives)
    )
    executed_perspectives = [res for res, _ in completed]
    if not any(res.succeeded for res in executed_perspectives):
        raise AuditAnalysisError("All audit perspectives failed; nothing to synthesize")

    overall_confidence = calculate_overall_confidence(
        executed_perspectives,
        expected_perspectives=active_perspectives,
    )
    findings = await asyncio.wait_for(
        asyncio.to_thread(
            synthesize_findings,
            executed_perspectives,
            raw_diff,
            overall_confidence=overall_confidence,
        ),
        timeout=AST_ANALYSIS_TIMEOUT_S,
    )
    counts = Counter(finding.severity for finding in findings)
    status = build_status_label(
        counts.get("BLOCKER", 0),
        counts.get("WARNING", 0),
        overall_confidence,
    )
    executive_summary = _synthesize_executive_summary(
        executed_perspectives, len(findings)
    )
    remediation_diff = build_remediation_diff(findings)
    models = [res.model for res in executed_perspectives if res.model]
    resolved_engine = (
        Counter(models).most_common(1)[0][0]
        if models
        else (engine or FALLBACK_ENGINE_LABEL)
    )
    if not _ENGINE_RE.fullmatch(resolved_engine):
        resolved_engine = engine or FALLBACK_ENGINE_LABEL
    publish_allowed = overall_confidence >= INFORMATIONAL_CONFIDENCE_THRESHOLD
    result = AuditResult(
        audit_id=resolved_audit_id,
        audit_type=resolved_audit_type,
        repo_full_name=repo_full_name,
        target_label=resolved_target,
        engine=resolved_engine,
        executive_summary=executive_summary,
        findings=findings,
        confidence=overall_confidence,
        status=status,
        remediation_diff=remediation_diff,
        analysis_metadata=ast_summary,
        perspectives=executed_perspectives,
        input_tokens=sum(res.input_tokens for res in executed_perspectives),
        output_tokens=sum(res.output_tokens for res in executed_perspectives),
        latency_ms=int((time.monotonic() - started) * 1000),
        publish_allowed=publish_allowed,
    )
    report = format_audit_report(
        pr_summary=result.executive_summary,
        executive_summary=result.executive_summary,
        findings=[finding.to_report_dict() for finding in result.findings],
        confidence=result.confidence,
        status=result.status,
        audit_target=result.target_label,
        engine=result.engine,
        must_fix_count=counts.get("BLOCKER", 0),
        should_fix_count=counts.get("WARNING", 0),
        remediation_diff=result.remediation_diff,
        analysis_metadata=result.analysis_metadata,
        publish_allowed=result.publish_allowed,
    )
    result = AuditResult(
        audit_id=result.audit_id,
        audit_type=result.audit_type,
        repo_full_name=result.repo_full_name,
        target_label=result.target_label,
        engine=result.engine,
        executive_summary=result.executive_summary,
        findings=result.findings,
        confidence=result.confidence,
        status=result.status,
        remediation_diff=result.remediation_diff,
        report_markdown=report,
        analysis_metadata=result.analysis_metadata,
        perspectives=result.perspectives,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        latency_ms=result.latency_ms,
        publish_allowed=result.publish_allowed,
    )
    logger.info(
        "auditor complete audit_id=%s type=%s repo=%s findings=%d blockers=%d warnings=%d confidence=%d latency_ms=%d",
        result.audit_id,
        resolved_audit_type,
        _safe_metadata(repo_full_name, 200),
        len(result.findings),
        counts.get("BLOCKER", 0),
        counts.get("WARNING", 0),
        result.confidence,
        result.latency_ms,
    )
    return result
