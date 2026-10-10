"""
Session Time Machine & Pre-Commit Security — Cloud Agentic Live Session Phase 7.

Provides:
  create_checkpoint()                  — Snapshot staged_patches + history length after a turn.
  tool_checkpoint_restore()            — Rewind session state to a prior checkpoint.
  tool_scan_security_vulnerabilities() — Secret scanner + SQL injection detector for pre-commit safety.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentSession
from app.services.session_streamer import SseQueue

# Maximum number of checkpoints retained per session.
_MAX_CHECKPOINTS = 20

# ---------------------------------------------------------------------------
# Secret & injection regex patterns
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: list[tuple[str, str, str]] = [
    # (rule_name, severity, pattern)
    ("AWS_ACCESS_KEY", "CRITICAL", r"AKIA[0-9A-Z]{16}"),
    ("GITHUB_PAT", "CRITICAL", r"gh[pousr]_[A-Za-z0-9_]{36,}"),
    ("API_KEY_SK", "CRITICAL", r"(sk-[A-Za-z0-9_-]{20,})"),
    ("PRIVATE_KEY", "CRITICAL", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("DATABASE_URL_WITH_PASSWORD", "HIGH", r"postgres(?:ql)?://[^:]+:([^@]+)@"),
]

_INJECTION_PATTERNS: list[tuple[str, str, str]] = [
    ("SQL_INJECTION_F_STRING", "HIGH", r'execute\(\s*f["\'].*\{.*\}'),
    ("SQL_INJECTION_PERCENT", "HIGH", r'\.execute\(\s*["\'].*%s'),
    ("SHELL_INJECTION", "HIGH", r"subprocess\.(?:Popen|run|call)\(.*shell\s*=\s*True"),
]


def _redact(match_text: str) -> str:
    """Replace all but the first 4 chars with asterisks for safe reporting."""
    visible = match_text[:4]
    return visible + "*" * max(4, len(match_text) - 4)


# ---------------------------------------------------------------------------
# Checkpoint management
# ---------------------------------------------------------------------------


def create_checkpoint(
    session: AgentSession,
    description: str,
    turn: int,
) -> dict[str, Any]:
    """
    Snapshot current session state as a named checkpoint.

    Appends to session.checkpoints (capped at _MAX_CHECKPOINTS).
    Caller must persist the session object to the DB.

    Returns the checkpoint dict.
    """
    checkpoint: dict[str, Any] = {
        "checkpoint_id": f"cp_{uuid.uuid4().hex[:8]}",
        "turn": turn,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "description": description,
        "staged_patches": dict(session.staged_patches or {}),
        "history_length": len(session.conversation_history or []),
    }

    current: list[dict[str, Any]] = list(session.checkpoints or [])
    current.append(checkpoint)
    # Cap to the most recent _MAX_CHECKPOINTS entries.
    session.checkpoints = current[-_MAX_CHECKPOINTS:]
    return checkpoint


def tool_checkpoint_list(session: AgentSession) -> str:
    """
    List all available checkpoints for the session.

    Returns a human-readable summary of available rollback checkpoints.
    """
    checkpoints: list[dict[str, Any]] = list(session.checkpoints or [])
    if not checkpoints:
        return "No checkpoints available for this session."

    lines: list[str] = [
        f"Available checkpoints ({len(checkpoints)}):",
    ]
    for cp in checkpoints:
        cp_id = cp.get("checkpoint_id", "unknown")
        turn = cp.get("turn", "?")
        ts = cp.get("timestamp", "")
        desc = cp.get("description", "No description")
        staged = cp.get("staged_patches", {}) or {}
        staged_files = list(staged.keys())
        if staged_files:
            files_str = f"{len(staged_files)} file(s) staged: {', '.join(staged_files)}"
        else:
            files_str = "0 files staged"
        ts_str = f", {ts}" if ts else ""
        lines.append(f"  - {cp_id} (Turn {turn}{ts_str}): {desc} [{files_str}]")

    lines.append("")
    lines.append(
        "Use checkpoint_restore(checkpoint_id='<id>') or "
        "checkpoint_restore(checkpoint_id='latest') to restore."
    )
    return "\n".join(lines)


async def tool_checkpoint_restore(
    checkpoint_id: str,
    session: AgentSession,
    queue: SseQueue,
    db: AsyncSession,
    staged_patches: dict[str, str] | None = None,
    conversation_history: list[dict[str, Any]] | None = None,
) -> str:
    """
    Restore session state to a prior checkpoint by checkpoint_id or 'latest'.

    - Reverts session.staged_patches to the snapshot.
    - Mutates turn-local staged_patches dict in-place if provided.
    - Truncates session.conversation_history to the snapshotted length.
    - Truncates turn-local conversation_history list in-place if provided.
    - Emits checkpoint_restored SSE event so Monaco editor buffers sync.
    - Flushes restored state to the active DB transaction (committed at turn end).

    Returns a human-readable result string for the LLM.
    """
    checkpoints: list[dict[str, Any]] = list(session.checkpoints or [])
    if not checkpoints:
        return f"Error: Checkpoint '{checkpoint_id}' not found."

    target = checkpoint_id.strip()
    cp: dict[str, Any] | None = None

    if target.lower() in ("latest", "last", "-1"):
        cp = checkpoints[-1]
    else:
        # Match exact checkpoint_id first
        cp = next((c for c in checkpoints if c.get("checkpoint_id") == target), None)
        # Fallback: match by turn number or negative offset if numeric
        if cp is None:
            try:
                turn_num = int(target)
                if turn_num < 0 and abs(turn_num) <= len(checkpoints):
                    cp = checkpoints[turn_num]
                else:
                    cp = next((c for c in checkpoints if c.get("turn") == turn_num), None)
            except (ValueError, IndexError):
                pass

    if cp is None:
        return f"Error: Checkpoint '{checkpoint_id}' not found."

    restored_cp_id = cp["checkpoint_id"]

    # Restore state on session ORM model.
    session.staged_patches = dict(cp["staged_patches"])
    history_length: int = cp["history_length"]
    session.conversation_history = list(
        (session.conversation_history or [])[:history_length]
    )

    # Mutate caller's turn-local structures in-place to prevent post-turn clobbering.
    if staged_patches is not None:
        staged_patches.clear()
        staged_patches.update(dict(session.staged_patches))

    if conversation_history is not None:
        conversation_history.clear()
        conversation_history.extend(list(session.conversation_history))

    # Emit SSE event to sync frontend buffers.
    await queue.put_checkpoint_restored(
        checkpoint_id=restored_cp_id,
        staged_patches=dict(session.staged_patches),
    )

    # Stage changes in the active transaction without prematurely releasing row locks.
    # The orchestrator commits the transaction at the end of the turn.
    await db.flush()

    return (
        f"Successfully restored session to checkpoint '{restored_cp_id}' "
        f"({len(session.staged_patches)} files staged)."
    )


# ---------------------------------------------------------------------------
# Pre-commit security scanner
# ---------------------------------------------------------------------------


def tool_scan_security_vulnerabilities(
    paths: list[str],
    session: AgentSession,
    repo_owner: str,
    repo_name: str,
    base_sha: str,
    gh_token: str | None = None,
) -> str:
    """
    Scan target file paths for secrets and injection flaws.

    Content is sourced from session.staged_patches (inline diffs are parsed
    for added lines with '+'). Skips binary content.

    Returns:
        A structured violation report, or a clean-pass message.
    """
    if not paths:
        return "Security scan passed: 0 secrets or SQL injection flaws detected across 0 files."

    violations: list[dict[str, Any]] = []

    staged: dict[str, str] = dict(session.staged_patches or {})
    scanned_count = 0

    for file_path in paths:
        content: str | None = None

        # Prefer staged patch content (the lines being committed).
        if file_path in staged:
            diff_text = staged[file_path]
            # Extract only added/context lines from the unified diff.
            lines = [
                line[1:]
                if line.startswith("+") and not line.startswith("+++")
                else line
                for line in diff_text.splitlines()
                if not line.startswith("-") and not line.startswith("---")
            ]
            content = "\n".join(lines)
        else:
            # No staged patch — nothing to scan for this path.
            continue

        if content is None:
            continue

        scanned_count += 1
        content_lines = content.splitlines()

        # Run secret patterns.
        for rule_name, severity, pattern in _SECRET_PATTERNS:
            try:
                compiled = re.compile(pattern)
            except re.error:
                continue
            for line_no, line_text in enumerate(content_lines, start=1):
                m = compiled.search(line_text)
                if m:
                    violations.append(
                        {
                            "file": file_path,
                            "line": line_no,
                            "rule": rule_name,
                            "severity": severity,
                            "finding": _redact(m.group(0)),
                        }
                    )

        # Run injection patterns.
        for rule_name, severity, pattern in _INJECTION_PATTERNS:
            try:
                compiled = re.compile(pattern, re.DOTALL)
            except re.error:
                continue
            for line_no, line_text in enumerate(content_lines, start=1):
                m = compiled.search(line_text)
                if m:
                    violations.append(
                        {
                            "file": file_path,
                            "line": line_no,
                            "rule": rule_name,
                            "severity": severity,
                            "finding": _redact(m.group(0)),
                        }
                    )

    if not violations:
        return (
            f"Security scan passed: 0 secrets or SQL injection flaws detected "
            f"across {scanned_count} files."
        )

    lines = [
        f"Security scan found {len(violations)} violation(s) across {scanned_count} file(s):",
        "",
    ]
    for v in violations:
        lines.append(
            f"  [{v['severity']}] {v['file']}:{v['line']} — {v['rule']}: {v['finding']}"
        )
    lines.append("")
    lines.append("Fix all CRITICAL and HIGH violations before committing.")
    return "\n".join(lines)
