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
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import OperationalError, DBAPIError

from app.github.pr import GitHubPRValidationError
from app.github_client import GitHubResourceNotFoundError
from app.routers.sessions import commit_session, create_session
from app.schemas import SessionCommitIn, SessionCreateIn

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


# ---------------------------------------------------------------------------
# 8. Block existing protected branch on commit_session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_session_blocks_existing_protected_branch() -> None:
    """commit_session must invoke _validate_branch(target_branch, allow_protected=False) before _update_ref."""
    session_id = uuid.uuid4()
    user_id = uuid.uuid4()
    mock_db = AsyncMock()
    mock_user = MagicMock(id=user_id)

    mock_repo = MagicMock()
    mock_repo.owner = "test-org"
    mock_repo.name = "test-repo"
    mock_repo.default_branch = "dev"  # default_branch is dev

    mock_session = MagicMock()
    mock_session.id = session_id
    mock_session.user_id = user_id
    mock_session.status = "active"
    mock_session.branch_name = "main"  # protected branch!
    mock_session.base_sha = "a" * 40
    mock_session.staged_patches = {"foo.py": "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-a\n+b\n"}
    mock_session.repo = mock_repo

    mock_db_result = MagicMock()
    mock_db_result.scalars.return_value.first.return_value = mock_session
    mock_db.execute.return_value = mock_db_result

    body = SessionCommitIn(title="Fix bug")

    mock_update_ref = AsyncMock()
    mock_create_blob = AsyncMock(return_value="blob_sha")
    mock_create_tree = AsyncMock(return_value="tree_sha")
    mock_create_commit = AsyncMock(return_value="commit_sha")

    with (
        patch("app.routers.sessions.get_installation_token", new_callable=AsyncMock, return_value="tok"),
        patch("app.github_client.fetch_file_content", new_callable=AsyncMock, return_value="a\n"),
        patch("app.services.patch_applier.apply_unified_diff", return_value="b\n"),
        patch("app.github_client.create_blob", mock_create_blob),
        patch("app.github_client.create_git_tree", mock_create_tree),
        patch("app.github_client.create_git_commit", mock_create_commit),
        patch("app.github_client.update_branch_ref", mock_update_ref),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await commit_session(
                session_id=session_id,
                body=body,
                current_user=mock_user,
                db=mock_db,
            )

    assert exc_info.value.status_code == 400
    assert "Cannot target protected branch 'main' directly" in exc_info.value.detail
    mock_create_blob.assert_not_called()
    mock_create_tree.assert_not_called()
    mock_create_commit.assert_not_called()
    mock_update_ref.assert_not_called()


# ---------------------------------------------------------------------------
# 9. Checkpoint restore failure does not sync orchestrator state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_restore_failure_does_not_sync_orchestrator_state() -> None:
    """When checkpoint_restore fails, local staged_patches and conversation_history must NOT sync."""
    session_id = uuid.uuid4()
    mock_db = AsyncMock()

    mock_session = MagicMock()
    mock_session.id = session_id
    mock_session.branch_name = "feat/test"
    mock_session.base_sha = "a" * 40
    mock_session.staged_patches = {"restored.py": "+restored"}
    mock_session.conversation_history = [{"role": "user", "content": "restored history"}]
    mock_session.checkpoints = []
    mock_session.repo = MagicMock(owner="org", name="repo")

    orchestrator = SessionOrchestrator(session_id=session_id, db=mock_db)

    fake_tool_call = {
        "id": "tc_restore_fail",
        "type": "function",
        "function": {
            "name": "checkpoint_restore",
            "arguments": json.dumps({"checkpoint_id": "non_existent_cp"}),
        },
    }

    orchestrator._llm = AsyncMock()
    orchestrator._llm.complete.side_effect = [
        {"content": None, "tool_calls": [fake_tool_call], "model": "test-model"},
        {"content": "Understood, restore failed.", "tool_calls": None, "model": "test-model"},
    ]

    orchestrator._load_session = AsyncMock(return_value=mock_session)
    orchestrator._persist = AsyncMock()

    original_staged = {"pre_turn.py": "+local"}
    original_history = [{"role": "user", "content": "original 1"}, {"role": "assistant", "content": "reply 1"}]
    mock_session.staged_patches = dict(original_staged)
    mock_session.conversation_history = list(original_history)

    queue = AsyncMock()

    await orchestrator._execute(
        user_message="restore bad checkpoint",
        queue=queue,
    )

    orchestrator._persist.assert_awaited_once()
    persisted_patches = orchestrator._persist.call_args[0][2]
    assert "pre_turn.py" in persisted_patches
    assert "restored.py" not in persisted_patches


# ---------------------------------------------------------------------------
# 10. Checkpoint restore success rebuilds messages LLM context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_restore_success_rebuilds_messages_context() -> None:
    """When checkpoint_restore succeeds, messages LLM context is rebuilt from restored conversation_history."""
    session_id = uuid.uuid4()
    mock_db = AsyncMock()

    cp_id = "cp_good"
    cp_patches = {"restored.py": "+restored_code"}

    mock_session = MagicMock()
    mock_session.id = session_id
    mock_session.branch_name = "feat/test"
    mock_session.base_sha = "a" * 40
    mock_session.staged_patches = {"bloat.py": "+bloat"}
    mock_session.conversation_history = [
        {"role": "user", "content": "turn 1 request"},
        {"role": "assistant", "content": "turn 1 reply"},
        {"role": "user", "content": "turn 2 request"},
        {"role": "assistant", "content": "turn 2 reply"},
        {"role": "user", "content": "turn 3 request"},
        {"role": "assistant", "content": "turn 3 reply"},
    ]
    mock_session.checkpoints = [
        {
            "checkpoint_id": cp_id,
            "turn": 1,
            "timestamp": "2026-10-01T00:00:00Z",
            "description": "turn 1 checkpoint",
            "staged_patches": cp_patches,
            "history_length": 2,
        }
    ]
    mock_session.repo = MagicMock(owner="org", name="repo")

    orchestrator = SessionOrchestrator(session_id=session_id, db=mock_db)

    fake_tool_call = {
        "id": "tc_restore_ok",
        "type": "function",
        "function": {
            "name": "checkpoint_restore",
            "arguments": json.dumps({"checkpoint_id": cp_id}),
        },
    }

    orchestrator._llm = AsyncMock()
    orchestrator._llm.complete.side_effect = [
        {"content": None, "tool_calls": [fake_tool_call], "model": "test-model"},
        {"content": "Session restored.", "tool_calls": None, "model": "test-model"},
    ]

    orchestrator._load_session = AsyncMock(return_value=mock_session)
    orchestrator._persist = AsyncMock()

    queue = AsyncMock()

    await orchestrator._execute(
        user_message="rollback to turn 1",
        queue=queue,
    )

    assert orchestrator._llm.complete.call_count == 2
    second_call_messages = orchestrator._llm.complete.call_args_list[1].kwargs["messages"]

    messages_content = [m.get("content", "") for m in second_call_messages if m.get("content")]
    all_content_str = " ".join(str(c) for c in messages_content)

    assert "turn 1 request" in all_content_str
    assert "turn 2 request" not in all_content_str
    assert "turn 3 request" not in all_content_str


# ---------------------------------------------------------------------------
# 11. DB connection failure raises normally without SessionBusyError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_load_session_db_connection_failure_raises_normally() -> None:
    """_load_session must allow standard DB connection failures to raise OperationalError/DBAPIError."""
    session_id = uuid.uuid4()
    mock_db = AsyncMock()

    orchestrator = SessionOrchestrator(
        session_id=session_id,
        db=mock_db,
    )

    mock_db.execute.side_effect = OperationalError(
        statement="SELECT ...",
        params={},
        orig=Exception("connection to server on socket failed: Connection refused"),
    )

    with pytest.raises(OperationalError):
        await orchestrator._load_session()


# ---------------------------------------------------------------------------
# 12. Expanded secret redaction patterns and refined Bearer/Postgres regexes
# ---------------------------------------------------------------------------


def test_session_streamer_expanded_patterns_and_refinements() -> None:
    """session_streamer._SECRET_PATTERNS redacts ghr, npg, mysql/mongo, auth headers, token query params, and preserves prose."""
    # 1. ghr_ (GitHub recovery / runner codes)
    res = _redact_secrets("ghr_1234567890abcdef")
    assert "ghr_1234567890abcdef" not in res
    assert "[REDACTED_GITHUB_TOKEN]" in res

    # 2. npg_ (Neon Postgres keys)
    res = _redact_secrets("npg_1234567890abcdef1234")
    assert "npg_1234567890abcdef1234" not in res
    assert "[REDACTED_API_KEY]" in res

    # 3. MySQL URLs with credentials
    res = _redact_secrets("mysql://dbuser:mypassword123@db.example.com:3306/haunter")
    assert "mypassword123" not in res
    assert "mysql://dbuser:[REDACTED_PASSWORD]@db.example.com:3306/haunter" in res

    # 4. Mongo URLs with credentials (mongodb:// and mongodb+srv://)
    res_mongo = _redact_secrets("mongodb://mongouser:secretpass456@cluster.mongodb.net/prod")
    assert "secretpass456" not in res_mongo
    assert "mongodb://mongouser:[REDACTED_PASSWORD]@cluster.mongodb.net/prod" in res_mongo

    res_srv = _redact_secrets("mongodb+srv://admin:clusterpass789@prod.mongodb.net/test")
    assert "clusterpass789" not in res_srv
    assert "mongodb+srv://admin:[REDACTED_PASSWORD]@prod.mongodb.net/test" in res_srv

    # 5. Authorization: Bearer ... headers
    res_auth = _redact_secrets("Authorization: Bearer secrettoken1234567890")
    assert "secrettoken1234567890" not in res_auth
    assert "Authorization: Bearer [REDACTED]" in res_auth

    # 6. token=... query parameters
    res_query = _redact_secrets("https://api.example.com/v1?token=querysecret1234567890&page=1")
    assert "querysecret1234567890" not in res_query
    assert "token=[REDACTED]" in res_query

    # 7. Refined Bearer token pattern: preserves prose "bearer token handling"
    prose = "Please review the bearer token handling and validation logic."
    assert _redact_secrets(prose) == prose

    # 8. Refined PostgreSQL password regex: excludes whitespace and '/' from user and password classes
    not_url = "postgresql:// /path/to/file:notpassword@example.com"
    assert "notpassword" in _redact_secrets(not_url)

    pg_url = "postgresql://myuser:realpass123@neon.tech/db"
    assert "realpass123" not in _redact_secrets(pg_url)
    assert "postgresql://myuser:[REDACTED_PASSWORD]@neon.tech/db" in _redact_secrets(pg_url)


# ---------------------------------------------------------------------------
# 13. Topic branch initialization fallback to default_branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_session_topic_branch_initializes_with_base_default_branch() -> None:
    """When client requests a topic branch that doesn't exist on GitHub, base_sha is resolved from default_branch."""
    user_id = uuid.uuid4()
    mock_user = MagicMock(id=user_id)
    repo_id = uuid.uuid4()
    mock_repo = MagicMock()
    mock_repo.id = repo_id
    mock_repo.user_id = user_id
    mock_repo.owner = "test-org"
    mock_repo.name = "test-repo"
    mock_repo.default_branch = "main"

    mock_db = AsyncMock()
    mock_db.add = MagicMock()
    mock_repo_result = MagicMock()
    mock_repo_result.scalars.return_value.first.return_value = mock_repo
    mock_db.execute.return_value = mock_repo_result
    mock_db.scalar.return_value = 0

    body = SessionCreateIn(
        repo_id=repo_id,
        branch_name="feat/new-topic",
        title="Topic Session",
    )

    async def _mock_fetch_branch_sha(owner, repo, branch, token=None):
        if branch == "feat/new-topic":
            raise GitHubResourceNotFoundError("Branch feat/new-topic not found")
        if branch == "main":
            return "base-sha-12345"
        raise GitHubResourceNotFoundError(f"Branch {branch} not found")

    async def _fake_refresh(obj):
        obj.id = uuid.uuid4()
        obj.created_at = datetime.now(timezone.utc)
        obj.updated_at = datetime.now(timezone.utc)

    mock_db.refresh.side_effect = _fake_refresh

    with (
        patch("app.routers.sessions.get_installation_token", new_callable=AsyncMock, return_value="mock-token"),
        patch("app.routers.sessions.fetch_branch_sha", side_effect=_mock_fetch_branch_sha),
    ):
        session_out = await create_session(body=body, current_user=mock_user, db=mock_db)

    assert session_out.branch_name == "feat/new-topic"
    assert session_out.base_sha == "base-sha-12345"
    assert session_out.status == "active"
    mock_db.add.assert_called_once()
    mock_db.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# 14. Checkpoint restore truncates current-turn intermediate tool entries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_restore_truncates_current_turn_intermediate_tool_entries() -> None:
    """On successful checkpoint_restore mid-turn, intermediate tool entries from that turn are pruned from history."""
    session_id = uuid.uuid4()
    mock_db = AsyncMock()

    cp_id = "cp_clean"
    cp_patches = {"clean.py": "+clean"}
    initial_history = [{"role": "user", "content": "turn 0"}]

    mock_session = MagicMock()
    mock_session.id = session_id
    mock_session.branch_name = "feat/test"
    mock_session.base_sha = "a" * 40
    mock_session.staged_patches = dict(cp_patches)
    mock_session.conversation_history = list(initial_history)
    mock_session.checkpoints = [
        {
            "checkpoint_id": cp_id,
            "turn": 1,
            "timestamp": "2026-10-01T00:00:00Z",
            "description": "clean checkpoint",
            "staged_patches": cp_patches,
            "history_length": 1,
        }
    ]
    mock_session.repo = MagicMock(owner="org", name="repo")

    orchestrator = SessionOrchestrator(session_id=session_id, db=mock_db)

    tc_stage = {
        "id": "tc_stage_1",
        "type": "function",
        "function": {
            "name": "stage_patch",
            "arguments": json.dumps({"path": "bad.py", "patch": "+bad"}),
        },
    }
    tc_restore = {
        "id": "tc_restore_2",
        "type": "function",
        "function": {
            "name": "checkpoint_restore",
            "arguments": json.dumps({"checkpoint_id": cp_id}),
        },
    }

    orchestrator._llm = AsyncMock()
    orchestrator._llm.complete.side_effect = [
        {"content": None, "tool_calls": [tc_stage], "model": "test-model"},
        {"content": None, "tool_calls": [tc_restore], "model": "test-model"},
        {"content": "Restored successfully.", "tool_calls": None, "model": "test-model"},
    ]

    orchestrator._load_session = AsyncMock(return_value=mock_session)
    orchestrator._persist = AsyncMock()

    queue = AsyncMock()

    await orchestrator._execute(
        user_message="stage then rollback",
        queue=queue,
    )

    orchestrator._persist.assert_awaited_once()
    persisted_history = orchestrator._persist.call_args[0][1]

    # Verify that the intermediate stage_patch is NOT in persisted history
    persisted_tool_names = []
    for msg in persisted_history:
        if "tool_calls" in msg:
            for tc in msg["tool_calls"]:
                persisted_tool_names.append(tc.get("function", {}).get("name"))

    assert "stage_patch" not in persisted_tool_names
    assert "checkpoint_restore" in persisted_tool_names
    assert any(m.get("content") == "Restored successfully." for m in persisted_history)


# ---------------------------------------------------------------------------
# 15. Checkpoint restore flushes without committing mid-turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_restore_flushes_without_committing_mid_turn() -> None:
    """tool_checkpoint_restore must use db.flush() instead of db.commit() to avoid releasing row locks mid-turn."""
    session = MagicMock()
    session.staged_patches = {"bad.py": "+bad"}
    session.conversation_history = [{"role": "user", "content": "1"}, {"role": "assistant", "content": "2"}]
    cp_id = "cp_flush_test"
    session.checkpoints = [
        {
            "checkpoint_id": cp_id,
            "turn": 1,
            "timestamp": "2026-10-01T00:00:00Z",
            "description": "cp",
            "staged_patches": {"good.py": "+good"},
            "history_length": 1,
        }
    ]

    queue = AsyncMock()
    mock_db = AsyncMock()

    result = await tool_checkpoint_restore(
        checkpoint_id=cp_id,
        session=session,
        queue=queue,
        db=mock_db,
    )

    assert "Successfully restored" in result
    mock_db.flush.assert_awaited_once()
    mock_db.commit.assert_not_called()


