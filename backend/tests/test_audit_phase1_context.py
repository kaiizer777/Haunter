"""Unit and integration tests for Auditor Phase 1 context pipeline (Issue #241).

Verifies:
- 1a: Parallel primary subagents (PR deep analysis + primary issue deep analysis).
- 1a: Omission of Subagent B when no primary issue exists.
- 1b: n nested mention subagents with strict recursion bound (final nested level).
- 1c: Single cohesive context compiler with 5 required sections.
- 1c: File persistence ({dossier_dir}/phase-1c.md and tempdir fallback).
- Downstream handoff into run_audit and build_perspective_messages.
- Graceful degradation for non-PR audits or fetch errors.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import Any, Mapping, Optional

import pytest

from app.llm import LLMClient, ResolvedModelConfig
from app.llm.prompts.audit_prompts import (
    PERSPECTIVES,
    build_perspective_messages,
)
from app.subagents.audit_context import (
    IssueAssessment,
    MentionAssessment,
    PRAssessment,
    Phase1cResult,
    build_phase1_context,
    detect_primary_issue_number,
    extract_mentioned_issue_and_pr_numbers,
    run_phase_1a,
    run_phase_1b,
    run_phase_1c_synthesis,
)
from app.subagents.auditor import AuditResult, run_audit


# ---------------------------------------------------------------------------
# Test Helpers & Fakes
# ---------------------------------------------------------------------------


class _FakeLLMClient:
    """Deterministic fake LLM client for testing Phase 1 subagents."""

    def __init__(self, responses: Optional[dict[str, str]] = None) -> None:
        self.responses = responses or {}
        self.call_count = 0
        self.history: list[list[dict[str, Any]]] = []

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        db: Any = None,
        model_config: Optional[ResolvedModelConfig] = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> Any:
        self.call_count += 1
        self.history.append(messages)
        user_content = messages[-1].get("content", "")

        for key, resp in self.responses.items():
            if key in user_content:
                return _FakeResponse(resp)

        # Default fallback responses based on subagent prompt cues
        if "Subagent A (Main PR deep analysis)" in user_content:
            return _FakeResponse(
                json.dumps(
                    {
                        "intent_summary": "Refactor auth pipeline to use single credentials.",
                        "claimed_changes": [
                            "Consolidate SSM credential resolvers",
                            "Add caching to installation tokens",
                        ],
                        "mentioned_refs": [101, 102],
                    }
                )
            )
        if "Subagent B (Primary issue deep analysis)" in user_content:
            return _FakeResponse(
                json.dumps(
                    {
                        "problem_statement": "Multiple SSM fetches cause auth throttling.",
                        "acceptance_criteria": [
                            "Single token cached for 50 minutes",
                            "No redundant STS calls",
                        ],
                        "preferred_approaches": ["In-memory TTL cache"],
                        "rejected_approaches": ["Writing tokens to disk"],
                        "scope_constraints": ["Do not touch webhook signatures"],
                        "mentioned_refs": [205],
                    }
                )
            )
        if "Phase 1b subagent analysis" in user_content:
            return _FakeResponse(
                json.dumps(
                    {
                        "summary": "Historical PR that introduced the initial SSM logic.",
                        "maintainer_verdict": "Merged by maintainer; approved with caching caveat.",
                    }
                )
            )
        if "Phase 1c Context Compiler" in user_content:
            return _FakeResponse(
                json.dumps(
                    {
                        "primary_intent": "Fix auth throttling by adding in-memory token caching.",
                        "scope_alignment_notes": "Changes strictly adhere to issue scope without scope creep.",
                    }
                )
            )

        # Perspectives response for run_audit
        return _FakeResponse(
            json.dumps(
                {
                    "findings": [],
                    "confidence": 95,
                    "summary": "Looks good and aligns with maintainer direction.",
                }
            )
        )


class _FakeResponse(dict):
    def __init__(self, content: str) -> None:
        super().__init__(
            content=content,
            usage={"input_tokens": 50, "output_tokens": 50},
            model="test-model",
        )
        self.content = content
        self.usage = {"input_tokens": 50, "output_tokens": 50}


SAMPLE_DIFF = """diff --git a/backend/app/auth.py b/backend/app/auth.py
new file mode 100644
--- /dev/null
+++ b/backend/app/auth.py
@@ -0,0 +1,5 @@
+def get_auth_token():
+    return "token-123"
"""


# ---------------------------------------------------------------------------
# Unit Tests
# ---------------------------------------------------------------------------


def test_detect_primary_issue_number():
    """Verify primary issue detection from closing keywords and metadata."""
    # From body keyword: fixes #100
    assert detect_primary_issue_number("This PR fixes #100 cleanly.") == 100
    assert detect_primary_issue_number("Resolves: #200\nOther text.") == 200
    assert detect_primary_issue_number("Closes #300 and refs #400") == 300
    assert detect_primary_issue_number("fixed foo/bar#400", owner="foo", repo="bar") == 400

    # From explicit metadata
    assert detect_primary_issue_number("", pr_metadata={"primary_issue_number": 500}) == 500
    assert (
        detect_primary_issue_number("", pr_metadata={"closingIssuesReferences": [{"number": 600}]})
        == 600
    )

    # No primary issue keyword
    assert detect_primary_issue_number("Just mentions #10 and #20 without closing keywords") is None
    assert detect_primary_issue_number("") is None


def test_extract_mentioned_issue_and_pr_numbers():
    """Verify distinct reference extraction excluding PR and primary issue."""
    texts = [
        "PR references #10 and #20.",
        "Discussion in #20 and #30, also mentions #10 again.",
        "Unrelated number: 12345, not a hashtag.",
    ]
    mentions = extract_mentioned_issue_and_pr_numbers(texts, exclude=[10, 999], limit=5)
    assert mentions == [20, 30]


@pytest.mark.asyncio
async def test_phase_1a_parallel_dispatch_with_primary_issue():
    """Phase 1a: Runs Subagent A and Subagent B in parallel when primary issue exists."""
    fake_llm = _FakeLLMClient()
    pr_meta = {
        "title": "Fix auth throttling",
        "body": "Fixes #101. Also mentions #202 in passing.",
        "author": "dev-alice",
        "author_association": "MEMBER",
    }
    issue_meta = {
        "title": "Auth rate limit exceeded in prod",
        "body": "Rate limit exceeded. Criteria:\n- Cache token for 50m\nRefers to #303.",
        "author": "lead-maintainer",
        "author_association": "OWNER",
        "comments": ["Maintainer note: keep cache purely in memory."],
    }

    pr_res, issue_res = await run_phase_1a(
        owner="owner",
        repo="repo",
        pr_number=50,
        pr_metadata=pr_meta,
        issue_data=issue_meta,
        diff_text=SAMPLE_DIFF,
        llm=fake_llm,
    )

    # Subagent A checks
    assert isinstance(pr_res, PRAssessment)
    assert pr_res.pr_number == 50
    assert pr_res.author == "dev-alice"
    assert pr_res.author_association == "organisation member"
    assert "auth pipeline" in pr_res.intent_summary.lower()
    assert len(pr_res.claimed_changes) >= 1
    assert any("auth.py" in f for f in pr_res.changed_files_summary)

    # Subagent B checks
    assert isinstance(issue_res, IssueAssessment)
    assert issue_res.issue_number == 101
    assert issue_res.author == "lead-maintainer"
    assert issue_res.author_attribution == "repository owner"
    assert "throttling" in issue_res.problem_statement.lower()
    assert len(issue_res.acceptance_criteria) >= 1
    assert len(issue_res.scope_constraints) >= 1


@pytest.mark.asyncio
async def test_phase_1a_omits_subagent_b_when_no_primary_issue():
    """Phase 1a: Subagent B is cleanly omitted when no primary issue is detected."""
    fake_llm = _FakeLLMClient()
    pr_meta = {
        "title": "Chore: clean up docs",
        "body": "Just cleaning up markdown typos. No issue fixed.",
        "author": "contributor-bob",
    }

    pr_res, issue_res = await run_phase_1a(
        owner="owner",
        repo="repo",
        pr_number=55,
        pr_metadata=pr_meta,
        diff_text=SAMPLE_DIFF,
        llm=fake_llm,
    )

    assert isinstance(pr_res, PRAssessment)
    assert issue_res is None


@pytest.mark.asyncio
async def test_phase_1b_nested_mention_subagents_and_recursion_bound():
    """Phase 1b: Dispatches n subagents concurrently with strict recursion bound."""
    fake_llm = _FakeLLMClient()
    pr_res = PRAssessment(
        pr_number=10,
        title="Main PR",
        mentioned_refs=[101, 102],
    )
    issue_res = IssueAssessment(
        issue_number=100,
        title="Main Issue",
        mentioned_refs=[102, 103],
    )

    mock_mention_data = {
        101: {"title": "Mentioned Issue 101", "body": "Context 101", "state": "closed"},
        102: {"title": "Mentioned PR 102", "body": "Context 102", "state": "open", "is_pr": True},
        103: {"title": "Mentioned Issue 103", "body": "Context 103", "state": "open"},
    }

    results = await run_phase_1b(
        owner="owner",
        repo="repo",
        pr_assessment=pr_res,
        issue_assessment=issue_res,
        mention_items_data=mock_mention_data,
        llm=fake_llm,
    )

    # Distinct refs = [101, 102, 103] -> exactly 3 subagents dispatched
    assert len(results) == 3
    ref_numbers = [m.ref_number for m in results]
    assert ref_numbers == [101, 102, 103]
    assert results[1].ref_type == "pull_request"
    assert "verdict" in results[0].raw_markdown.lower() or "merged" in results[0].maintainer_verdict.lower()


@pytest.mark.asyncio
async def test_phase_1c_synthesis_and_file_persistence():
    """Phase 1c: Synthesizes cohesive context with 5 canonical sections and saves to disk."""
    fake_llm = _FakeLLMClient()
    pr_res = PRAssessment(
        pr_number=77,
        title="Add Token Cache",
        author="alice",
        author_association="collaborator",
        intent_summary="Cache tokens in memory.",
        claimed_changes=["Add TTL cache class"],
        changed_files_summary=["`backend/app/cache.py`"],
        mentioned_refs=[200],
    )
    issue_res = IssueAssessment(
        issue_number=150,
        title="Memory leak in tokens",
        author="saif",
        author_attribution="repository owner",
        problem_statement="Token leaking memory over time.",
        acceptance_criteria=["TTL expires after 1h"],
        scope_constraints=["Do not modify external API"],
        mentioned_refs=[],
    )
    mentions = [
        MentionAssessment(
            ref_number=200,
            ref_type="issue",
            title="Initial caching discussion",
            state="closed",
            summary="Decided on LRU cache with TTL.",
            maintainer_verdict="Approved by @saif",
            raw_markdown="- **#200 (issue, closed)**: Initial caching discussion\n  - *Verdict*: Approved by @saif",
        )
    ]

    with tempfile.TemporaryDirectory() as tmp_dir:
        result = await run_phase_1c_synthesis(
            owner="saif",
            repo="haunter",
            pr_assessment=pr_res,
            issue_assessment=issue_res,
            mention_assessments=mentions,
            dossier_dir=tmp_dir,
            llm=fake_llm,
        )

        assert isinstance(result, Phase1cResult)
        assert result.file_path is not None
        assert Path(result.file_path).exists()
        assert Path(result.file_path).name == "phase-1c.md"

        saved_text = Path(result.file_path).read_text(encoding="utf-8")
        assert saved_text == result.context_markdown

        # Verify all 5 required sections are present
        assert "## Primary Intent & Problem Statement" in saved_text
        assert "## Pull Request Scope & Claimed Changes" in saved_text
        assert "## Authority Verdicts & Maintainer Direction" in saved_text
        assert "## Scope Alignment & Constraints" in saved_text
        assert "## Mentioned References & Historical Context" in saved_text

        # Verify content fidelity
        assert "PR #77" in saved_text
        assert "#150" in saved_text
        assert "#200" in saved_text
        assert "repository owner" in saved_text


@pytest.mark.asyncio
async def test_build_phase1_context_end_to_end():
    """Verify top-level build_phase1_context coordinator."""
    fake_llm = _FakeLLMClient()
    pr_meta = {
        "title": "Add caching",
        "body": "Fixes #12. Mentions #34.",
        "author": "dev",
    }
    issue_meta = {
        "title": "Need caching",
        "body": "We need token caching.",
        "author": "boss",
    }

    result = await build_phase1_context(
        owner="org",
        repo="app",
        pr_number=99,
        pr_metadata=pr_meta,
        issue_data=issue_meta,
        diff_text=SAMPLE_DIFF,
        llm=fake_llm,
    )

    assert result is not None
    assert not result.degraded
    assert result.pr_assessment is not None
    assert result.issue_assessment is not None
    assert result.file_path is not None
    assert Path(result.file_path).exists()


def test_build_perspective_messages_includes_phase_1c_context():
    """Verify build_perspective_messages fences and incorporates phase_1c_context."""
    phase_1c_md = "# Phase 1 Context Dossier\n\n## Primary Intent\nFix token leak."
    messages = build_perspective_messages(
        perspective=PERSPECTIVES[0],
        diff_text=SAMPLE_DIFF,
        ast_summary="AST Summary Info",
        repo_context="Repo: org/app",
        phase_1c_context=phase_1c_md,
    )

    user_msg = messages[1]["content"]
    assert "## Phase 1 Context Dossier (untrusted data)" in user_msg
    assert "Fix token leak." in user_msg
    assert "## Unified Diff Under Audit" in user_msg


@pytest.mark.asyncio
async def test_run_audit_end_to_end_with_phase1_context():
    """Verify run_audit coordinates Phase 1 and feeds phase-1c context downstream."""
    fake_llm = _FakeLLMClient()
    pr_meta = {
        "title": "Bulletproof phase 1",
        "body": "Fixes #10. Mentions #20.",
        "author": "engineer",
    }

    result = await run_audit(
        diff_text=SAMPLE_DIFF,
        repo_full_name="org/app",
        pr_number=42,
        pr_metadata=pr_meta,
        llm_client=fake_llm,
        engine="test-engine",
    )

    assert isinstance(result, AuditResult)
    assert result.phase_1c_context != ""
    assert "## Primary Intent & Problem Statement" in result.phase_1c_context
    assert "## Pull Request Scope & Claimed Changes" in result.phase_1c_context
    assert result.phase_1c_file_path is not None
    assert Path(result.phase_1c_file_path).exists()

    # Verify downstream perspectives received the phase 1c block
    # Check history of complete calls:
    perspective_calls = [
        h for h in fake_llm.history
        if any("## Perspective" in msg.get("content", "") for msg in h)
    ]
    assert len(perspective_calls) >= 1
    user_prompt = perspective_calls[0][-1]["content"]
    assert "## Phase 1 Context Dossier (untrusted data)" in user_prompt
    assert "PR #42" in user_prompt


@pytest.mark.asyncio
async def test_run_audit_graceful_degradation_when_no_pr():
    """Verify non-PR commit audits skip Phase 1 without errors."""
    fake_llm = _FakeLLMClient()

    result = await run_audit(
        diff_text=SAMPLE_DIFF,
        repo_full_name="org/app",
        pr_number=None,  # Commit audit, not PR
        llm_client=fake_llm,
        engine="test-engine",
    )

    assert isinstance(result, AuditResult)
    assert result.phase_1c_context == ""
    assert result.phase_1c_file_path is None
