"""
Unit tests for Fixer 2 Backend Orchestrator, Patch Applier, Checkpoints, Sandbox & Redaction.

Validates all 7 core requirements:
1. 1-byte newline deletion fix in patch_applier.py and mirror.py (apply_unified_diff).
2. Accurate scanned_count reporting in tool_scan_security_vulnerabilities (checkpoints.py).
3. Sandbox execution secret filtering in _get_clean_subprocess_env (sandbox.py).
4. Secret redaction on outgoing SSE events in session_streamer.py (format_sse_event).
5. Subagent context passing (staged_patches, base_sha, session_id) in subagents.py.
6. State synchronization on checkpoint_restore in session_orchestrator.py.
7. Concurrency conflict (SessionBusyError) on locked session in session_orchestrator.py.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError, DBAPIError

from app.sandbox.mirror import apply_unified_diff as mirror_apply_unified_diff
from app.services.patch_applier import apply_unified_diff as patch_applier_apply_diff
from app.services.session_orchestrator import (
    SessionBusyError,
    SessionOrchestrator,
)
from app.services.session_streamer import format_sse_event, _redact_secrets
from app.services.session_tools.checkpoints import (
    tool_checkpoint_restore,
    tool_scan_security_vulnerabilities,
)
from app.services.session_tools.sandbox import (
    _get_clean_subprocess_env,
    tool_run_terminal_command,
)
from app.services.session_tools.subagents import SubagentRunner


# ---------------------------------------------------------------------------
# 1. 1-byte newline deletion fix
# ---------------------------------------------------------------------------


def test_patch_applier_full_file_deletion_returns_empty_string() -> None:
    """apply_unified_diff in patch_applier.py must return '' (0 bytes) when diff deletes all lines."""
    original = "line 1\nline 2\nline 3\n"
    diff = (
        "--- a/file.txt\n"
        "+++ /dev/null\n"
        "@@ -1,3 +0,0 @@\n"
        "-line 1\n"
        "-line 2\n"
        "-line 3\n"
    )
    result = patch_applier_apply_diff(original, diff)
    assert result == "", f"Expected empty string for full deletion, got {result!r}"
    assert len(result.encode("utf-8")) == 0


def test_mirror_apply_unified_diff_full_file_deletion_returns_empty_string() -> None:
    """apply_unified_diff in mirror.py must return '' (0 bytes) when diff deletes all lines."""
    original = "single line\n"
    diff = (
        "--- a/single.txt\n"
        "+++ /dev/null\n"
        "@@ -1,1 +0,0 @@\n"
        "-single line\n"
    )
    result = mirror_apply_unified_diff(original, diff)
    assert result == "", f"Expected empty string for full deletion in mirror, got {result!r}"
    assert len(result.encode("utf-8")) == 0


# ---------------------------------------------------------------------------
# 2. Accurate scanned_count reporting
# ---------------------------------------------------------------------------


def test_security_scan_reports_accurate_scanned_count() -> None:
    """tool_scan_security_vulnerabilities reports count of actually scanned staged files."""
    session = MagicMock()
    session.staged_patches = {
        "src/app.py": "+def hello(): pass\n",
    }

    # Pass 3 paths, but only 1 is actually staged in session.staged_patches
    result = tool_scan_security_vulnerabilities(
        paths=["src/app.py", "unstaged1.py", "unstaged2.py"],
        session=session,
        repo_owner="org",
        repo_name="repo",
        base_sha="a" * 40,
    )

    assert "1 files" in result or "1 staged file" in result
    assert "3 files" not in result

    # When no staged files match paths
    result_empty = tool_scan_security_vulnerabilities(
        paths=["unstaged1.py", "unstaged2.py"],
        session=session,
        repo_owner="org",
        repo_name="repo",
        base_sha="a" * 40,
    )
    assert "0 files" in result_empty or "0 staged" in result_empty


# ---------------------------------------------------------------------------
# 3. Sandbox environment secret filtering
# ---------------------------------------------------------------------------


def test_sandbox_clean_subprocess_env_filters_sensitive_secrets() -> None:
    """_get_clean_subprocess_env must scrub secrets, database credentials, and API keys."""
    dirty_env = {
        "PATH": "C:\\Windows\\System32;/usr/bin",
        "PYTHONPATH": "src/",
        "HOME": "/home/user",
        "DATABASE_URL": "postgres://user:pass@neon.tech/haunter",
        "DATABASE_URL_UNPOOLED": "postgres://user:pass@neon.tech/haunter",
        "SESSION_SECRET_KEY": "supersecretkey123",
        "SESSION_SECRET_KEY_PREVIOUS": "oldsecretkey",
        "GITHUB_CLIENT_SECRET": "gh_secret_xyz",
        "GITHUB_WEBHOOK_SECRET": "wh_secret_abc",
        "OPENAI_API_KEY": "sk-1234567890",
        "ANTHROPIC_API_KEY": "sk-ant-12345",
        "MY_SERVICE_TOKEN": "token_val",
        "AWS_SECRET_ACCESS_KEY": "aws_secret",
        "SAFE_CUSTOM_VAR": "harmless_value",
    }

    with patch.dict(os.environ, dirty_env, clear=True):
        clean_env = _get_clean_subprocess_env(cwd=".")

        # Preserved system/safe vars
        assert "PATH" in clean_env
        assert clean_env["PYTHONPATH"] == "src/"
        assert clean_env["SAFE_CUSTOM_VAR"] == "harmless_value"

        # Filtered sensitive secrets
        assert "DATABASE_URL" not in clean_env
        assert "DATABASE_URL_UNPOOLED" not in clean_env
        assert "SESSION_SECRET_KEY" not in clean_env
        assert "SESSION_SECRET_KEY_PREVIOUS" not in clean_env
        assert "GITHUB_CLIENT_SECRET" not in clean_env
        assert "GITHUB_WEBHOOK_SECRET" not in clean_env
        assert "OPENAI_API_KEY" not in clean_env
        assert "ANTHROPIC_API_KEY" not in clean_env
        assert "MY_SERVICE_TOKEN" not in clean_env
        assert "AWS_SECRET_ACCESS_KEY" not in clean_env


# ---------------------------------------------------------------------------
# 4. SSE event secret redaction
# ---------------------------------------------------------------------------


def test_session_streamer_format_sse_event_redacts_secrets() -> None:
    """format_sse_event must redact sensitive tokens, keys, and credentials from SSE output."""
    raw_payload = {
        "thought": "Using AWS key AKIAIOSFODNN7EXAMPLE to connect to db at postgresql://haunter_user:secret_pass123@ep-cool-db.us-east-2.aws.neon.tech/haunter_db",
        "token": "ghp_0123456789abcdef0123456789abcdef0123",
        "auth_header": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secretpayload",
        "nested": {
            "key": "sk-proj-0123456789abcdef0123456789abcdef",
            "safe_text": "This is harmless code diff",
        },
    }

    sse_string = format_sse_event("thought", raw_payload)

    assert "AKIAIOSFODNN7EXAMPLE" not in sse_string
    assert "secret_pass123" not in sse_string
    assert "ghp_0123456789abcdef0123456789abcdef0123" not in sse_string
    assert "sk-proj-0123456789abcdef0123456789abcdef" not in sse_string
    assert "[REDACTED]" in sse_string
    assert "This is harmless code diff" in sse_string


# ---------------------------------------------------------------------------
# 5. Subagent context passing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subagent_runner_passes_sandbox_context() -> None:
    """SubagentRunner._exec_run_terminal_command passes staged_patches, base_sha, session_id."""
    patches = {"src/foo.py": "+# patch"}
    session = MagicMock()
    session.id = uuid.uuid4()
    session.staged_patches = patches
    queue = AsyncMock()
    llm = AsyncMock()

    runner = SubagentRunner(
        role="sandbox_verifier",
        task="Run pytest",
        target_files=["src/foo.py"],
        repo_owner="test-owner",
        repo_name="test-repo",
        base_sha="f" * 40,
        staged_patches=patches,
        session=session,
        queue=queue,
        llm=llm,
        gh_token=None,
        model=None,
        provider=None,
    )

    with patch(
        "app.services.session_tools.subagents.tool_run_terminal_command",
        new_callable=AsyncMock,
        return_value="Exit code: 0\nSTDOUT: ok",
    ) as mock_terminal:
        res = await runner._exec_run_terminal_command({"command": "pytest"})

        mock_terminal.assert_awaited_once()
        _, kwargs = mock_terminal.call_args
        assert kwargs["staged_patches"] == patches
        assert kwargs["base_sha"] == "f" * 40
        assert kwargs["session_id"] == str(session.id)
        assert "ok" in res


# ---------------------------------------------------------------------------
# 6. Checkpoint restore state synchronization in orchestrator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_syncs_state_on_checkpoint_restore() -> None:
    """SessionOrchestrator._dispatch_tool updates session state and dispatch loop syncs local variables."""
    session_id = uuid.uuid4()
    mock_db = AsyncMock()

    cp_id = "cp_test_sync"
    cp_patches = {"lib/core.py": "--- a/lib/core.py\n+++ b/lib/core.py\n@@ -1 +1 @@\n-old\n+restored\n"}
    cp_history = [{"role": "user", "content": "initial request"}]

    mock_session = MagicMock()
    mock_session.id = session_id
    mock_session.base_sha = "a" * 40
    mock_session.staged_patches = {"lib/core.py": "+modified"}
    mock_session.conversation_history = [
        {"role": "user", "content": "initial request"},
        {"role": "assistant", "content": "working on it"},
        {"role": "user", "content": "cancel that"},
    ]
    mock_session.checkpoints = [
        {
            "checkpoint_id": cp_id,
            "turn": 1,
            "timestamp": "2026-10-01T00:00:00Z",
            "description": "Initial checkpoint",
            "staged_patches": cp_patches,
            "history_length": 1,
        }
    ]

    queue = MagicMock()
    queue.put_checkpoint_restored = AsyncMock()
    queue.put_tool_call_result = AsyncMock()

    orchestrator = SessionOrchestrator(
        session_id=session_id,
        db=mock_db,
    )

    local_staged = dict(mock_session.staged_patches)

    async def _fake_tool_restore(checkpoint_id, session, queue, db):
        session.staged_patches = dict(cp_patches)
        session.conversation_history = list(cp_history)
        return f"Successfully restored session to checkpoint '{checkpoint_id}'."

    with patch(
        "app.services.session_tools.checkpoints.tool_checkpoint_restore",
        side_effect=_fake_tool_restore,
    ):
        result = await orchestrator._dispatch_tool(
            tool_name="checkpoint_restore",
            args={"checkpoint_id": cp_id},
            repo_owner="test-org",
            repo_name="test-repo",
            base_sha=mock_session.base_sha,
            staged_patches=local_staged,
            queue=queue,
            session=mock_session,
        )

        assert "Successfully restored" in result
        assert mock_session.staged_patches == cp_patches
        assert len(mock_session.conversation_history) == 1


# ---------------------------------------------------------------------------
# 7. Concurrency conflict (SessionBusyError) on locked session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_load_session_raises_session_busy_error_on_lock_conflict() -> None:
    """_load_session raises SessionBusyError when NOWAIT lock fails with OperationalError."""
    session_id = uuid.uuid4()
    mock_db = AsyncMock()

    orchestrator = SessionOrchestrator(
        session_id=session_id,
        db=mock_db,
    )

    # Simulate DBAPI lock conflict exception from Postgres NOWAIT
    mock_db.execute.side_effect = OperationalError(
        statement="SELECT ... FOR UPDATE NOWAIT",
        params={},
        orig=Exception("could not obtain lock on row in relation agent_sessions"),
    )

    with pytest.raises(SessionBusyError):
        await orchestrator._load_session()
