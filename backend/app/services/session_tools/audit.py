"""
Interactive In-Studio Auditor Commands & Session Audit Tools — Phase 7.1 (future02.md §3).

Provides:
  - TOOL_RUN_AUDIT_SCAN: OpenAI function tool schema for `run_audit_scan`.
  - AuditSlashCommand / parse_slash_command: Parses `/security-scan` and `/repo-audit` chat commands.
  - tool_run_audit_scan: Session tool implementation returning formatted string for LLM loop.
  - handle_slash_command: High-level slash command execution emitting SSE events and returning (AuditResult, str).
  - execute_audit_scan: Core executor returning AuditResult.
"""

from __future__ import annotations

import fnmatch
import logging
import re
import shlex
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.github_client import (
    GitHubClientError,
    fetch_diff,
)
from app.llm.client import LLMClient
from app.llm.exceptions import LLMError
from app.llm.prompts.audit_prompts import (
    PERSPECTIVES,
    PerspectiveName,
)
from app.models import AgentSession
from app.services.session_streamer import SseQueue
from app.subagents.auditor import (
    AuditAnalysisError,
    AuditResult,
    run_audit,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums and Types
# ---------------------------------------------------------------------------


class AuditTargetType(StrEnum):
    WORKSPACE = "workspace"
    BRANCH = "branch"
    DIFF = "diff"


class AuditScanProfile(StrEnum):
    FULL = "full"
    SECURITY_ONLY = "security_only"
    STRICT = "strict"


ALL_PERSPECTIVES: tuple[PerspectiveName, ...] = PERSPECTIVES
SECURITY_PERSPECTIVE: tuple[PerspectiveName, ...] = ("security",)


# ---------------------------------------------------------------------------
# OpenAI Tool Schema Declaration
# ---------------------------------------------------------------------------

TOOL_RUN_AUDIT_SCAN: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "run_audit_scan",
        "description": (
            "Run an interactive multi-perspective code audit or targeted security scan "
            "across the live session workspace, staged patches, or branch diff. "
            "Evaluates code for security vulnerabilities, AST anti-patterns, performance bottlenecks, "
            "and architectural flaws, returning structured findings with severity levels and fixes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target_type": {
                    "type": "string",
                    "enum": ["workspace", "branch", "diff"],
                    "description": (
                        "Scope to analyze: 'workspace' (active session workspace files), "
                        "'branch' (changes against base branch), or 'diff' (currently staged patches). "
                        "Default is 'diff'."
                    ),
                },
                "perspectives": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "security",
                            "correctness",
                            "performance",
                            "architecture",
                        ],
                    },
                    "description": (
                        "Optional list of audit perspectives to execute. Defaults to all 4 for a full audit, "
                        "or ['security'] for security scans."
                    ),
                },
                "scan_profile": {
                    "type": "string",
                    "enum": ["full", "security_only", "strict"],
                    "description": (
                        "Preset scan profile: 'security_only' runs the security perspective; "
                        "'full' or 'strict' runs all 4 perspectives."
                    ),
                },
                "path_filter": {
                    "type": "string",
                    "description": (
                        "Optional path prefix or glob to restrict analysis to specific files or directories "
                        "(e.g. 'backend/app/', 'auth.py')."
                    ),
                },
                "branch": {
                    "type": "string",
                    "description": (
                        "Optional base branch or ref to compare against when target_type is 'branch' "
                        "(defaults to repo default branch or 'main')."
                    ),
                },
            },
            "additionalProperties": False,
        },
    },
}


# ---------------------------------------------------------------------------
# Slash Command Dataclass and Parser
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditSlashCommand:
    command: Literal["/security-scan", "/repo-audit"]
    target_type: Literal["workspace", "branch", "diff"]
    perspectives: list[str]
    path_filter: Optional[str]
    branch: Optional[str]
    strict: bool
    scan_profile: str
    raw_prompt: str


def matches_path_filter(file_path: str, path_filter: str | None) -> bool:
    """Check whether a repository-relative path matches an optional path filter."""
    if not path_filter or not path_filter.strip():
        return True
    clean_filter = path_filter.strip().replace("\\", "/").rstrip("/")
    clean_path = file_path.replace("\\", "/").lstrip("/")

    if clean_path == clean_filter or clean_path.startswith(clean_filter + "/"):
        return True
    if fnmatch.fnmatch(clean_path, clean_filter) or fnmatch.fnmatch(
        clean_path, f"*{clean_filter}*"
    ):
        return True
    return False


def filter_diff_by_path(diff_text: str, path_filter: str | None) -> str:
    """Filter a unified diff string to keep only hunks matching the path filter."""
    if not path_filter or not path_filter.strip() or not diff_text.strip():
        return diff_text

    clean_filter = path_filter.strip().replace("\\", "/").rstrip("/")
    chunks = re.split(r"(?=^diff --git )", diff_text, flags=re.MULTILINE)
    matched_chunks: list[str] = []

    for chunk in chunks:
        if not chunk.strip():
            continue
        first_line = chunk.splitlines()[0]
        match = re.match(r"^diff --git a/(.+?) b/(.+)$", first_line)
        if match:
            path_a, path_b = match.group(1), match.group(2)
            if matches_path_filter(path_b, clean_filter) or matches_path_filter(
                path_a, clean_filter
            ):
                matched_chunks.append(chunk)
        elif matches_path_filter(first_line, clean_filter):
            matched_chunks.append(chunk)

    return "".join(matched_chunks)


def parse_slash_command(text: str) -> Optional[AuditSlashCommand]:
    """
    Parse chat text to determine if it is an interactive slash command.

    Supported commands:
      - /security-scan [filter] [--path=...] [--target=...] [--branch=...]
      - /repo-audit [filter] [--path=...] [--strict] [--target=...] [--branch=...]

    Returns None if text does not match a supported slash audit command.
    """
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None

    try:
        tokens = shlex.split(stripped)
    except ValueError:
        # Fallback split on whitespace if quotes are unclosed
        tokens = stripped.split()

    if not tokens:
        return None

    cmd_token = tokens[0].lower()
    if cmd_token not in {"/security-scan", "/repo-audit"}:
        return None

    path_filter: Optional[str] = None
    target_type: Literal["workspace", "branch", "diff"] = "workspace"
    branch: Optional[str] = None
    strict: bool = False
    explicit_perspectives: Optional[list[str]] = None
    scan_profile_override: Optional[str] = None

    idx = 1
    while idx < len(tokens):
        token = tokens[idx]
        if token.startswith("--path="):
            path_filter = token.split("=", 1)[1].strip()
        elif token in {"--path", "-p"}:
            if idx + 1 < len(tokens) and not tokens[idx + 1].startswith("-"):
                idx += 1
                path_filter = tokens[idx].strip()
        elif token.startswith("--target="):
            val = token.split("=", 1)[1].strip().lower()
            if val in {"workspace", "branch", "diff"}:
                target_type = val  # type: ignore[assignment]
        elif token in {"--target", "-t"}:
            if idx + 1 < len(tokens) and not tokens[idx + 1].startswith("-"):
                idx += 1
                val = tokens[idx].strip().lower()
                if val in {"workspace", "branch", "diff"}:
                    target_type = val  # type: ignore[assignment]
        elif token.startswith("--branch="):
            branch = token.split("=", 1)[1].strip()
        elif token in {"--branch", "-b"}:
            if idx + 1 < len(tokens) and not tokens[idx + 1].startswith("-"):
                idx += 1
                branch = tokens[idx].strip()
        elif token == "--strict":
            strict = True
        elif token.startswith("--profile="):
            scan_profile_override = token.split("=", 1)[1].strip().lower()
        elif token.startswith("--perspectives="):
            raw_p = token.split("=", 1)[1].strip()
            explicit_perspectives = [
                p.strip().lower() for p in raw_p.split(",") if p.strip()
            ]
        elif not token.startswith("-") and path_filter is None:
            # Positional target or filter
            path_filter = token.strip()
        idx += 1

    if cmd_token == "/security-scan":
        command_literal: Literal["/security-scan", "/repo-audit"] = "/security-scan"
        perspectives = explicit_perspectives or ["security"]
        profile = scan_profile_override or "security_only"
    else:
        command_literal = "/repo-audit"
        perspectives = explicit_perspectives or list(ALL_PERSPECTIVES)
        profile = scan_profile_override or ("strict" if strict else "full")

    # Clean path_filter quotes if any
    if path_filter:
        path_filter = path_filter.strip("'\"")

    return AuditSlashCommand(
        command=command_literal,
        target_type=target_type,
        perspectives=perspectives,
        path_filter=path_filter,
        branch=branch,
        strict=strict,
        scan_profile=profile,
        raw_prompt=stripped,
    )


# ---------------------------------------------------------------------------
# Core Audit Scan Execution
# ---------------------------------------------------------------------------


async def execute_audit_scan(
    *,
    target_type: str = "diff",
    perspectives: Optional[Sequence[str]] = None,
    scan_profile: Optional[str] = None,
    path_filter: Optional[str] = None,
    branch: Optional[str] = None,
    session: AgentSession,
    repo_owner: str,
    repo_name: str,
    base_sha: str,
    staged_patches: dict[str, str],
    queue: Optional[SseQueue] = None,
    llm: Optional[LLMClient] = None,
    gh_token: Optional[str] = None,
    db: Optional[AsyncSession] = None,
) -> AuditResult:
    """
    Execute on-demand audit scan across workspace, branch, or staged patches.

    Returns the synthesized AuditResult and streams SSE events if queue is provided.
    """
    # 1. Resolve active perspectives
    resolved_perspectives: list[PerspectiveName]
    if scan_profile == "security_only":
        resolved_perspectives = ["security"]
    elif perspectives is not None:
        resolved_perspectives = [
            p
            for p in perspectives
            if p in ALL_PERSPECTIVES  # type: ignore[misc]
        ] or list(ALL_PERSPECTIVES)
    elif scan_profile in ("full", "strict"):
        resolved_perspectives = list(ALL_PERSPECTIVES)
    else:
        resolved_perspectives = list(ALL_PERSPECTIVES)

    scan_type_label = (
        "security_scan" if resolved_perspectives == ["security"] else "repo_audit"
    )

    # 2. Gather code diff based on target_type
    diff_text = ""
    scanned_files: list[str] = []

    target_type_clean = (target_type or "diff").lower()
    if target_type_clean not in {"workspace", "branch", "diff"}:
        target_type_clean = "diff"

    if target_type_clean == "diff":
        matching_patches = {
            p: d
            for p, d in staged_patches.items()
            if matches_path_filter(p, path_filter)
        }
        scanned_files = list(matching_patches.keys())
        diff_text = "\n".join(matching_patches.values())

    elif target_type_clean == "branch":
        base_ref = branch or base_sha or "main"
        head_ref = session.branch_name
        try:
            raw_branch_diff = await fetch_diff(
                owner=repo_owner,
                repo=repo_name,
                sha=head_ref,
                base_sha=base_ref,
                token=gh_token,
            )
            diff_text = filter_diff_by_path(raw_branch_diff, path_filter)
            # Find file paths in the branch diff
            for line in diff_text.splitlines():
                if line.startswith("diff --git a/"):
                    match = re.match(r"^diff --git a/(.+?) b/(.+)$", line)
                    if match:
                        scanned_files.append(match.group(2))
        except GitHubClientError as exc:
            logger.warning(
                "Failed to fetch branch diff for %s/%s: %s", repo_owner, repo_name, exc
            )
            diff_text = ""

    elif target_type_clean == "workspace":
        # First check staged patches
        matching_staged = {
            p: d
            for p, d in staged_patches.items()
            if matches_path_filter(p, path_filter)
        }
        if matching_staged:
            scanned_files = list(matching_staged.keys())
            diff_text = "\n".join(matching_staged.values())
        else:
            # If no staged patches, attempt to fetch branch diff
            try:
                base_ref = branch or base_sha or "main"
                raw_diff = await fetch_diff(
                    owner=repo_owner,
                    repo=repo_name,
                    sha=session.branch_name,
                    base_sha=base_ref,
                    token=gh_token,
                )
                diff_text = filter_diff_by_path(raw_diff, path_filter)
                for line in diff_text.splitlines():
                    if line.startswith("diff --git a/"):
                        match = re.match(r"^diff --git a/(.+?) b/(.+)$", line)
                        if match:
                            scanned_files.append(match.group(2))
            except Exception as exc:
                logger.debug(
                    "Workspace branch comparison fallback produced no diff: %s", exc
                )
                diff_text = ""

    # 3. Emit SSE audit_scan_start
    if queue is not None:
        await queue.put_audit_scan_start(
            scan_type=scan_type_label,
            total_files_estimated=max(len(scanned_files), 1),
            target_path=path_filter or "all",
        )

    # 4. Stream progressive file scan events if files are identified
    if queue is not None and scanned_files:
        total = len(scanned_files)
        for idx, file_name in enumerate(scanned_files, start=1):
            await queue.put_audit_progress(
                files_scanned=idx,
                current_file=file_name,
                total_files=total,
            )

    # 5. Run audit core
    llm_instance = llm or LLMClient()
    repo_full_name = f"{repo_owner}/{repo_name}"

    audit_result = await run_audit(
        diff_text=diff_text,
        ast_context="",
        repo_context=f"Live Session {session.id} — branch: {session.branch_name}, target: {target_type_clean}",
        db=db,
        repo_id=session.repo_id,
        audit_type="session_audit",
        repo_full_name=repo_full_name,
        target_label=f"session:{session.id}:{path_filter or target_type_clean}",
        llm_client=llm_instance,
        perspectives=resolved_perspectives,
    )

    # 6. Emit SSE audit_report
    if queue is not None:
        findings_payload = [
            {
                **f.to_dict(),
                "can_auto_fix": bool(f.suggested_fix),
            }
            for f in audit_result.findings
        ]
        await queue.put_audit_report(
            scan_type=scan_type_label,
            findings=findings_payload,
            health_score=audit_result.confidence,
            summary=audit_result.executive_summary,
            audit_id=audit_result.audit_id,
            severity_counts=audit_result.severity_counts,
            report_markdown=audit_result.report_markdown,
        )

    return audit_result


# ---------------------------------------------------------------------------
# Tool and Command Adapters
# ---------------------------------------------------------------------------


async def tool_run_audit_scan(
    args: dict[str, Any],
    session: AgentSession,
    repo_owner: str,
    repo_name: str,
    base_sha: str,
    staged_patches: dict[str, str],
    queue: Optional[SseQueue] = None,
    llm: Optional[LLMClient] = None,
    gh_token: Optional[str] = None,
    db: Optional[AsyncSession] = None,
) -> str:
    """
    Session tool execution adapter for `run_audit_scan`.

    Returns a clean string representation of the audit findings for the agent.
    """
    target_type = str(args.get("target_type") or "diff")
    perspectives = args.get("perspectives")
    scan_profile = args.get("scan_profile")
    path_filter = args.get("path_filter")
    branch = args.get("branch")

    try:
        result = await execute_audit_scan(
            target_type=target_type,
            perspectives=perspectives,
            scan_profile=scan_profile,
            path_filter=path_filter,
            branch=branch,
            session=session,
            repo_owner=repo_owner,
            repo_name=repo_name,
            base_sha=base_sha,
            staged_patches=staged_patches,
            queue=queue,
            llm=llm,
            gh_token=gh_token,
            db=db,
        )
    except AuditAnalysisError as exc:
        logger.error("tool_run_audit_scan analysis error: %s", exc)
        return f"Audit scan failed: {exc}. All perspectives encountered errors."
    except LLMError as exc:
        logger.error("tool_run_audit_scan LLM error: %s", exc)
        return f"Audit scan failed due to LLM provider error: {exc}"
    except Exception as exc:
        logger.exception("tool_run_audit_scan unexpected error: %s", exc)
        return f"Audit scan encountered an error: {exc}"

    findings_summary = (
        f"Findings ({len(result.findings)}): "
        f"{result.severity_counts.get('BLOCKER', 0)} Blocker(s), "
        f"{result.severity_counts.get('WARNING', 0)} Warning(s), "
        f"{result.severity_counts.get('NOTE', 0)} Note(s)."
    )

    return (
        f"Audit scan completed successfully.\n"
        f"Target: {result.target_label}\n"
        f"Score: {result.confidence}/100 | Status: {result.status}\n"
        f"{findings_summary}\n\n"
        f"Executive Summary:\n{result.executive_summary}\n\n"
        f"{result.report_markdown}"
    )


async def handle_slash_command(
    cmd: AuditSlashCommand,
    session: AgentSession,
    repo_owner: str,
    repo_name: str,
    base_sha: str,
    staged_patches: dict[str, str],
    queue: Optional[SseQueue] = None,
    llm: Optional[LLMClient] = None,
    gh_token: Optional[str] = None,
    db: Optional[AsyncSession] = None,
) -> tuple[AuditResult, str]:
    """
    Execute an intercepted interactive slash command (/security-scan or /repo-audit).

    Returns a tuple of (AuditResult, formatted_assistant_response_markdown).
    """
    try:
        result = await execute_audit_scan(
            target_type=cmd.target_type,
            perspectives=cmd.perspectives,
            scan_profile=cmd.scan_profile,
            path_filter=cmd.path_filter,
            branch=cmd.branch,
            session=session,
            repo_owner=repo_owner,
            repo_name=repo_name,
            base_sha=base_sha,
            staged_patches=staged_patches,
            queue=queue,
            llm=llm,
            gh_token=gh_token,
            db=db,
        )
    except Exception as exc:
        logger.exception("handle_slash_command error for %s: %s", cmd.command, exc)
        if queue is not None:
            await queue.put_error(f"Audit scan failed: {exc}", "AUDIT_SCAN_ERROR")
        empty_result = AuditResult(
            audit_id=f"audit-{uuid.uuid4().hex[:12]}",
            audit_type="session_audit",
            repo_full_name=f"{repo_owner}/{repo_name}",
            target_label=f"session:{session.id}",
            engine="haunter-auditor",
            executive_summary=f"Audit scan failed: {exc}",
            findings=[],
            confidence=0,
            status="Audit Failed",
            remediation_diff="",
            report_markdown=f"### ⚠️ Audit Scan Error\n\nThe scan could not complete: `{exc}`.",
            analysis_metadata="",
            perspectives=[],
            publish_allowed=False,
        )
        return empty_result, empty_result.report_markdown

    header = (
        "🛡️ **Repository Security Scan**"
        if cmd.command == "/security-scan"
        else "🔍 **Codebase Multi-Perspective Audit**"
    )
    scope_desc = f"Target Scope: `{cmd.path_filter or cmd.target_type}`"
    score_desc = f"Health Score: **{result.confidence}/100** ({result.status})"

    response_text = (
        f"{header}\n\n"
        f"{scope_desc} • {score_desc}\n\n"
        f"{result.executive_summary}\n\n"
        f"{result.report_markdown}"
    )

    return result, response_text
