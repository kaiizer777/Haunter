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
    ("SHELL_INJECTION", "HIGH", r'subprocess\.(?:Popen|run|call)\(.*shell\s*=\s*True'),
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


async def tool_checkpoint_restore(
    checkpoint_id: str,
    session: AgentSession,
    queue: SseQueue,
    db: AsyncSession,
) -> str:
    """
    Restore session state to a prior checkpoint by checkpoint_id.

    - Reverts session.staged_patches to the snapshot.
    - Truncates session.conversation_history to the snapshotted length.
    - Emits checkpoint_restored SSE event so Monaco editor buffers sync.
    - Persists and commits changes to the DB.

    Returns a human-readable result string for the LLM.
    """
    checkpoints: list[dict[str, Any]] = list(session.checkpoints or [])
    cp = next((c for c in checkpoints if c.get("checkpoint_id") == checkpoint_id), None)

    if cp is None:
        return f"Error: Checkpoint '{checkpoint_id}' not found."

    # Restore state.
    session.staged_patches = dict(cp["staged_patches"])
    history_length: int = cp["history_length"]
    session.conversation_history = list((session.conversation_history or [])[:history_length])

    # Emit SSE event to sync frontend buffers.
    await queue.put_checkpoint_restored(
        checkpoint_id=checkpoint_id,
        staged_patches=dict(session.staged_patches),
    )

    # Persist.
    await db.commit()

    return (
        f"Successfully restored session to checkpoint '{checkpoint_id}' "
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

    for file_path in paths:
        content: str | None = None

        # Prefer staged patch content (the lines being committed).
        if file_path in staged:
            diff_text = staged[file_path]
            # Extract only added/context lines from the unified diff.
            lines = [
                line[1:] if line.startswith("+") and not line.startswith("+++") else line
                for line in diff_text.splitlines()
                if not line.startswith("-") and not line.startswith("---")
            ]
            content = "\n".join(lines)
        else:
            # No staged patch — nothing to scan for this path.
            continue

        if content is None:
            continue

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
                    violations.append({
                        "file": file_path,
                        "line": line_no,
                        "rule": rule_name,
                        "severity": severity,
                        "finding": _redact(m.group(0)),
                    })

        # Run injection patterns.
        for rule_name, severity, pattern in _INJECTION_PATTERNS:
            try:
                compiled = re.compile(pattern, re.DOTALL)
            except re.error:
                continue
            for line_no, line_text in enumerate(content_lines, start=1):
                m = compiled.search(line_text)
                if m:
                    violations.append({
                        "file": file_path,
                        "line": line_no,
                        "rule": rule_name,
                        "severity": severity,
                        "finding": _redact(m.group(0)),
                    })

    if not violations:
        return (
            f"Security scan passed: 0 secrets or SQL injection flaws detected "
            f"across {len(paths)} files."
        )

    lines = [
        f"Security scan found {len(violations)} violation(s) across {len(paths)} file(s):",
        "",
    ]
    for v in violations:
        lines.append(
            f"  [{v['severity']}] {v['file']}:{v['line']} — {v['rule']}: {v['finding']}"
        )
    lines.append("")
    lines.append("Fix all CRITICAL and HIGH violations before committing.")
    return "\n".join(lines)
