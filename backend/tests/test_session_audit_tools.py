"""
Comprehensive Hermetic Tests for Session Audit Tools & Slash Commands (Phase 7.1).

Covers:
  1. Tool declaration and JSON schema validity (TOOL_RUN_AUDIT_SCAN).
  2. Slash command parsing (/security-scan, /repo-audit, flags, positional targets, invalid input).
  3. /security-scan command execution (verifying only security perspective runs).
  4. /repo-audit command execution (verifying all 4 perspectives run).
  5. Path filtering across staged patches and unified diffs.
  6. Empty workspace / empty diff handling (zero changes, clean completion).
  7. Failure resilience (LLM errors, perspective failures, invalid arguments).
  8. Orchestrator slash command interception & tool dispatch.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.llm.client import LLMClient
from app.llm.exceptions import LLMError
from app.models import AgentSession, Repo
from app.services.session_orchestrator import SessionOrchestrator
from app.services.session_streamer import SseQueue
from app.services.session_tools.audit import (
    ALL_PERSPECTIVES,
    TOOL_RUN_AUDIT_SCAN,
    AuditSlashCommand,
    execute_audit_scan,
    filter_diff_by_path,
    handle_slash_command,
    matches_path_filter,
    parse_slash_command,
    tool_run_audit_scan,
)
from app.subagents.auditor import AuditAnalysisError, AuditFinding, AuditResult


# ---------------------------------------------------------------------------
# Fixtures and Mock Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_session() -> AgentSession:
    sess = MagicMock(spec=AgentSession)
    sess.id = uuid.uuid4()
    sess.user_id = uuid.uuid4()
    sess.repo_id = uuid.uuid4()
    sess.branch_name = "haunter/live-pair-42"
    sess.base_sha = "a" * 40
    sess.staged_patches = {}
    sess.conversation_history = []
    sess.status = "active"

    repo = MagicMock(spec=Repo)
    repo.id = sess.repo_id
    repo.owner = "test-org"
    repo.name = "test-repo"
    repo.default_branch = "main"
    sess.repo = repo
    return sess


def _make_perspective_llm_response(
    perspective: str,
    title: str = "Test Finding",
    severity: str = "WARNING",
    confidence: int = 90,
) -> dict[str, Any]:
    return {
        "content": json.dumps({
            "summary": f"{perspective.capitalize()} analysis completed successfully.",
            "confidence": confidence,
            "findings": [
                {
                    "file_path": "backend/app/auth.py",
                    "line_start": 84,
                    "line_end": 85,
                    "severity": severity,
                    "category": f"{perspective}_check",
                    "title": title,
                    "description": f"Detailed description for {perspective} issue.",
                    "suggested_fix": "import hmac\nif not hmac.compare_digest(token, stored): raise Unauthorized()",
                    "confidence": confidence,
                }
            ],
        }),
        "model": "zen-free",
        "usage": {"input_tokens": 120, "output_tokens": 85},
    }


SAMPLE_UNIFIED_DIFF = """diff --git a/backend/app/auth.py b/backend/app/auth.py
index 1111111..2222222 100644
--- a/backend/app/auth.py
+++ b/backend/app/auth.py
@@ -82,4 +82,5 @@ def verify_token(token, stored):
-    if token != stored:
+    import hmac
+    if not hmac.compare_digest(token, stored):
         raise Unauthorized()
diff --git a/frontend/src/App.tsx b/frontend/src/App.tsx
index 3333333..4444444 100644
--- a/frontend/src/App.tsx
+++ b/frontend/src/App.tsx
@@ -10,3 +10,4 @@ export function App() {
+    console.log("debug");
     return <div>Hello</div>;
 }
"""


# ---------------------------------------------------------------------------
# 1. Tool Declaration and JSON Schema Validity
# ---------------------------------------------------------------------------

def test_tool_declaration_schema():
    assert TOOL_RUN_AUDIT_SCAN["type"] == "function"
    fn = TOOL_RUN_AUDIT_SCAN["function"]
    assert fn["name"] == "run_audit_scan"
    assert "description" in fn
    assert len(fn["description"]) > 20

    params = fn["parameters"]
    assert params["type"] == "object"
    assert params["additionalProperties"] is False

    props = params["properties"]
    assert "target_type" in props
    assert props["target_type"]["enum"] == ["workspace", "branch", "diff"]
    assert "perspectives" in props
    assert props["perspectives"]["type"] == "array"
    assert "scan_profile" in props
    assert props["scan_profile"]["enum"] == ["full", "security_only", "strict"]
    assert "path_filter" in props
    assert "branch" in props


# ---------------------------------------------------------------------------
# 2. Slash Command Parser Tests
# ---------------------------------------------------------------------------

def test_parse_slash_command_security_scan_defaults():
    cmd = parse_slash_command("/security-scan")
    assert cmd is not None
    assert cmd.command == "/security-scan"
    assert cmd.target_type == "workspace"
    assert cmd.perspectives == ["security"]
    assert cmd.scan_profile == "security_only"
    assert cmd.path_filter is None
    assert cmd.branch is None
    assert cmd.strict is False


def test_parse_slash_command_security_scan_with_positional_path():
    cmd = parse_slash_command("/security-scan backend/app/")
    assert cmd is not None
    assert cmd.command == "/security-scan"
    assert cmd.path_filter == "backend/app/"
    assert cmd.perspectives == ["security"]


def test_parse_slash_command_security_scan_with_flags():
    cmd = parse_slash_command("/security-scan --path=src/auth.py --target=diff --branch=main")
    assert cmd is not None
    assert cmd.path_filter == "src/auth.py"
    assert cmd.target_type == "diff"
    assert cmd.branch == "main"
    assert cmd.perspectives == ["security"]


def test_parse_slash_command_short_flags():
    cmd = parse_slash_command("/security-scan -p backend/app -t branch -b dev")
    assert cmd is not None
    assert cmd.path_filter == "backend/app"
    assert cmd.target_type == "branch"
    assert cmd.branch == "dev"


def test_parse_slash_command_repo_audit_defaults():
    cmd = parse_slash_command("/repo-audit")
    assert cmd is not None
    assert cmd.command == "/repo-audit"
    assert cmd.target_type == "workspace"
    assert cmd.perspectives == list(ALL_PERSPECTIVES)
    assert cmd.scan_profile == "full"
    assert cmd.strict is False


def test_parse_slash_command_repo_audit_strict_flag():
    cmd = parse_slash_command("/repo-audit --strict")
    assert cmd is not None
    assert cmd.command == "/repo-audit"
    assert cmd.strict is True
    assert cmd.scan_profile == "strict"
    assert cmd.perspectives == list(ALL_PERSPECTIVES)


def test_parse_slash_command_repo_audit_custom_perspectives():
    cmd = parse_slash_command("/repo-audit --perspectives=security,architecture --path=backend/")
    assert cmd is not None
    assert cmd.perspectives == ["security", "architecture"]
    assert cmd.path_filter == "backend/"


def test_parse_slash_command_non_command_returns_none():
    assert parse_slash_command("Can you scan the code for bugs?") is None
    assert parse_slash_command("/help") is None
    assert parse_slash_command("/commit -m 'test'") is None
    assert parse_slash_command("") is None
    assert parse_slash_command("   ") is None


# ---------------------------------------------------------------------------
# 3. Path Filtering and Diff Slicing
# ---------------------------------------------------------------------------

def test_matches_path_filter():
    assert matches_path_filter("backend/app/auth.py", None) is True
    assert matches_path_filter("backend/app/auth.py", "") is True
    assert matches_path_filter("backend/app/auth.py", "backend") is True
    assert matches_path_filter("backend/app/auth.py", "backend/") is True
    assert matches_path_filter("backend/app/auth.py", "backend/app") is True
    assert matches_path_filter("backend/app/auth.py", "backend/app/auth.py") is True
    assert matches_path_filter("backend/app/auth.py", "*.py") is True
    assert matches_path_filter("backend/app/auth.py", "frontend") is False
    assert matches_path_filter("frontend/src/App.tsx", "backend/") is False


def test_filter_diff_by_path():
    backend_only = filter_diff_by_path(SAMPLE_UNIFIED_DIFF, "backend/")
    assert "backend/app/auth.py" in backend_only
    assert "frontend/src/App.tsx" not in backend_only

    frontend_only = filter_diff_by_path(SAMPLE_UNIFIED_DIFF, "frontend/")
    assert "frontend/src/App.tsx" in frontend_only
    assert "backend/app/auth.py" not in frontend_only

    nonexistent = filter_diff_by_path(SAMPLE_UNIFIED_DIFF, "services/")
    assert nonexistent == ""


# ---------------------------------------------------------------------------
# 4. /security-scan Command Execution (Only Security Perspective)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_security_scan_runs_only_security_perspective(mock_session):
    staged = {
        "backend/app/auth.py": (
            "diff --git a/backend/app/auth.py b/backend/app/auth.py\n"
            "--- a/backend/app/auth.py\n"
            "+++ b/backend/app/auth.py\n"
            "@@ -84,1 +84,2 @@\n"
            "-if token != stored:\n"
            "+import hmac\n"
            "+if not hmac.compare_digest(token, stored): raise Unauthorized()\n"
        )
    }

    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.complete = AsyncMock(
        return_value=_make_perspective_llm_response(
            "security", title="Non-Constant-Time Token Compare"
        )
    )

    queue = SseQueue()
    cmd = parse_slash_command("/security-scan")
    assert cmd is not None

    result, response_text = await handle_slash_command(
        cmd=cmd,
        session=mock_session,
        repo_owner="test-org",
        repo_name="test-repo",
        base_sha="a" * 40,
        staged_patches=staged,
        queue=queue,
        llm=mock_llm,
    )

    # Assert only 1 LLM completion call for security perspective
    assert mock_llm.complete.call_count == 1
    call_args = mock_llm.complete.call_args[1]
    system_content = call_args["messages"][0]["content"]
    assert "security" in system_content.lower()

    # Assert result conforms to AuditResult
    assert isinstance(result, AuditResult)
    assert len(result.findings) >= 1
    assert result.findings[0].perspective == "security"
    assert "Repository Security Scan" in response_text

    # Verify SSE events were emitted
    events = []
    while not queue._q.empty():
        item = queue._q.get_nowait()
        if isinstance(item, str):
            events.append(item)

    event_text = "".join(events)
    assert "event: audit_scan_start" in event_text
    assert "event: audit_progress" in event_text
    assert "event: audit_report" in event_text
    assert "security_scan" in event_text


# ---------------------------------------------------------------------------
# 5. /repo-audit Command Execution (All 4 Perspectives)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_repo_audit_runs_all_four_perspectives(mock_session):
    staged = {
        "backend/app/auth.py": (
            "diff --git a/backend/app/auth.py b/backend/app/auth.py\n"
            "--- a/backend/app/auth.py\n"
            "+++ b/backend/app/auth.py\n"
            "@@ -84,1 +84,2 @@\n"
            "+import hmac\n"
            "+if not hmac.compare_digest(token, stored): raise Unauthorized()\n"
        )
    }

    call_index = 0

    async def mock_complete(*args, **kwargs):
        nonlocal call_index
        perspective = ALL_PERSPECTIVES[call_index % len(ALL_PERSPECTIVES)]
        call_index += 1
        return _make_perspective_llm_response(
            perspective, title=f"Finding for {perspective}"
        )

    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.complete = AsyncMock(side_effect=mock_complete)

    queue = SseQueue()
    cmd = parse_slash_command("/repo-audit --strict")
    assert cmd is not None

    result, response_text = await handle_slash_command(
        cmd=cmd,
        session=mock_session,
        repo_owner="test-org",
        repo_name="test-repo",
        base_sha="a" * 40,
        staged_patches=staged,
        queue=queue,
        llm=mock_llm,
    )

    # Assert exactly 4 LLM calls (one per perspective)
    assert mock_llm.complete.call_count == 4
    assert isinstance(result, AuditResult)
    assert len(result.perspectives) == 4
    assert {p.perspective for p in result.perspectives} == set(ALL_PERSPECTIVES)
    assert "Codebase Multi-Perspective Audit" in response_text


# ---------------------------------------------------------------------------
# 6. Empty Workspace and Empty Diff Handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_workspace_and_diff_handling(mock_session):
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.complete = AsyncMock()

    queue = SseQueue()
    result = await execute_audit_scan(
        target_type="diff",
        session=mock_session,
        repo_owner="test-org",
        repo_name="test-repo",
        base_sha="a" * 40,
        staged_patches={},  # Empty staged patches
        queue=queue,
        llm=mock_llm,
    )

    # When diff is empty, short-circuit occurs without LLM calls
    assert mock_llm.complete.call_count == 0
    assert result.confidence == 100
    assert len(result.findings) == 0
    assert "No code changes detected" in result.executive_summary


@pytest.mark.asyncio
async def test_path_filter_matches_nothing(mock_session):
    staged = {
        "frontend/src/App.tsx": "diff --git a/frontend/src/App.tsx b/frontend/src/App.tsx\n..."
    }
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.complete = AsyncMock()

    result = await execute_audit_scan(
        target_type="diff",
        path_filter="backend/",  # Filter eliminates the frontend patch
        session=mock_session,
        repo_owner="test-org",
        repo_name="test-repo",
        base_sha="a" * 40,
        staged_patches=staged,
        llm=mock_llm,
    )

    assert mock_llm.complete.call_count == 0
    assert result.confidence == 100
    assert len(result.findings) == 0


# ---------------------------------------------------------------------------
# 7. Failure Resilience (LLM Error, Invalid Arguments)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_failure_resilience_llm_error(mock_session):
    staged = {
        "backend/app/auth.py": (
            "diff --git a/backend/app/auth.py b/backend/app/auth.py\n"
            "--- a/backend/app/auth.py\n"
            "+++ b/backend/app/auth.py\n"
            "@@ -84,1 +84,2 @@\n"
            "+pass\n"
        )
    }
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.complete = AsyncMock(side_effect=LLMError("OpenCode Zen rate limit 429"))

    res_str = await tool_run_audit_scan(
        args={"target_type": "diff", "perspectives": ["security"]},
        session=mock_session,
        repo_owner="test-org",
        repo_name="test-repo",
        base_sha="a" * 40,
        staged_patches=staged,
        llm=mock_llm,
    )

    # Asserts that the tool handles the exception gracefully without raising
    assert "Audit scan failed" in res_str
    assert "All perspectives encountered errors" in res_str or "LLM" in res_str


@pytest.mark.asyncio
async def test_handle_slash_command_catches_errors(mock_session):
    staged = {
        "backend/app/auth.py": "diff --git a/backend/app/auth.py b/backend/app/auth.py\n+pass\n"
    }
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.complete = AsyncMock(side_effect=RuntimeError("Subprocess failed"))

    cmd = parse_slash_command("/security-scan")
    assert cmd is not None

    queue = SseQueue()
    result, response_text = await handle_slash_command(
        cmd=cmd,
        session=mock_session,
        repo_owner="test-org",
        repo_name="test-repo",
        base_sha="a" * 40,
        staged_patches=staged,
        queue=queue,
        llm=mock_llm,
    )

    assert result.confidence == 0
    assert "Audit Scan Error" in response_text


# ---------------------------------------------------------------------------
# 8. Orchestrator Integration (Slash Command Interception & Tool Dispatch)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_orchestrator_intercepts_security_scan_command(mock_session):
    orchestrator = SessionOrchestrator(
        session_id=mock_session.id,
        db=AsyncMock(),
        gh_token="dummy-token",
    )
    orchestrator._load_session = AsyncMock(return_value=mock_session)
    orchestrator._persist = AsyncMock()

    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.complete = AsyncMock(
        return_value=_make_perspective_llm_response("security")
    )
    orchestrator._llm = mock_llm

    queue = SseQueue()
    await orchestrator.run(
        user_message="/security-scan backend/app",
        queue=queue,
    )

    # Verify session conversation history was appended with user command + assistant audit report
    assert len(mock_session.conversation_history) == 2
    assert mock_session.conversation_history[0]["content"] == "/security-scan backend/app"
    assert "Repository Security Scan" in mock_session.conversation_history[1]["content"]

    # Verify persistence was called
    orchestrator._persist.assert_awaited_once()


@pytest.mark.asyncio
async def test_orchestrator_dispatches_run_audit_scan_tool(mock_session):
    orchestrator = SessionOrchestrator(
        session_id=mock_session.id,
        db=AsyncMock(),
        gh_token="dummy-token",
    )
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.complete = AsyncMock(
        return_value=_make_perspective_llm_response("security")
    )
    orchestrator._llm = mock_llm

    staged = {
        "backend/app/auth.py": "diff --git a/backend/app/auth.py b/backend/app/auth.py\n+import hmac\n"
    }

    tool_res = await orchestrator._dispatch_tool(
        tool_name="run_audit_scan",
        args={"target_type": "diff", "perspectives": ["security"]},
        repo_owner="test-org",
        repo_name="test-repo",
        base_sha="a" * 40,
        staged_patches=staged,
        queue=SseQueue(),
        session=mock_session,
    )

    assert "Audit scan completed successfully" in tool_res
    assert "Score:" in tool_res
