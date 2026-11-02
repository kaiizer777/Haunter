"""Unit and integration tests for Phase 2: Re-review on new commit.

Covers:
- Delta diff detection and compare API call
- Hunk-aware chunking (<= 550 lines/chunk)
- Phase 1c context reuse from disk
- Finding matching & thread resolution (resolved vs retained vs new)
- In-place review update and footer swap
- Empty delta diff short-circuit
- Force-push compare fallback
"""

import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.github.audit_publisher import (
    load_dossier_meta,
    publish_audit_review,
    update_dossier_meta,
)
from app.llm.prompts.audit_prompts import format_audit_report, swap_report_footer
from app.models import Repo
from app.services.audit_pipeline import execute_audit_job
from app.subagents.audit_chunking import partition_diff_into_chunks
from app.subagents.audit_matcher import (
    FindingResolutionResult,
    match_and_resolve_findings,
)
from app.subagents.auditor import AuditFinding, AuditResult, run_audit


# ---------------------------------------------------------------------------
# 1. Hunk-Aware Chunking Tests
# ---------------------------------------------------------------------------


def test_chunking_small_diff_returns_single_chunk():
    diff = """diff --git a/foo.py b/foo.py
index 123..456 100644
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,4 @@
 def hello():
+    print("world")
     return True
"""
    chunks = partition_diff_into_chunks(diff, max_chunk_lines=500, hard_limit=550)
    assert len(chunks) == 1
    assert chunks[0] == diff


def test_chunking_respects_hunk_boundaries_and_hard_limit():
    # Build a multi-hunk diff exceeding 500 lines
    file_header = "diff --git a/big.py b/big.py\n--- a/big.py\n+++ b/big.py\n"
    hunk1 = "@@ -1,200 +1,200 @@\n" + "\n".join(f"+line1_{i}" for i in range(200)) + "\n"
    hunk2 = "@@ -300,200 +300,200 @@\n" + "\n".join(f"+line2_{i}" for i in range(200)) + "\n"
    hunk3 = "@@ -600,200 +600,200 @@\n" + "\n".join(f"+line3_{i}" for i in range(200)) + "\n"

    diff = file_header + hunk1 + hunk2 + hunk3
    assert len(diff.splitlines()) > 600

    chunks = partition_diff_into_chunks(diff, max_chunk_lines=500, hard_limit=550)
    assert len(chunks) >= 2
    for chunk in chunks:
        lines = chunk.splitlines()
        assert len(lines) <= 550, f"Chunk exceeded hard limit: {len(lines)}"
        # Each chunk retains the file header
        assert "diff --git a/big.py b/big.py" in chunk


def test_chunking_keeps_single_hunk_under_hard_limit_intact():
    # Single hunk that fits under the hard limit stays intact in its own chunk
    # with the file header (never split mid-hunk).
    file_header = "diff --git a/huge.py b/huge.py\n--- a/huge.py\n+++ b/huge.py\n"
    hunk = "@@ -1,540 +1,540 @@\n" + "\n".join(f"+huge_{i}" for i in range(540)) + "\n"
    diff = file_header + hunk

    chunks = partition_diff_into_chunks(diff, max_chunk_lines=500, hard_limit=550)
    assert len(chunks) == 1
    assert "diff --git a/huge.py b/huge.py" in chunks[0]
    assert "@@ -1,540 +1,540 @@" in chunks[0]
    assert len(chunks[0].splitlines()) <= 550


def test_chunking_quoted_paths_with_spaces():
    # Force the split path with tight limits so the quoted diff --git header is
    # parsed and carried into every sub-chunk.
    diff = '''diff --git "a/path with spaces/test.py" "b/path with spaces/test.py"
--- "a/path with spaces/test.py"
+++ "b/path with spaces/test.py"
@@ -1,2 +1,2 @@
-old line
+new line
@@ -10,2 +10,2 @@
-old line two
+new line two
'''
    chunks = partition_diff_into_chunks(diff, max_chunk_lines=4, hard_limit=8)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk.splitlines()) <= 8
        assert 'diff --git "a/path with spaces/test.py" "b/path with spaces/test.py"' in chunk


def test_chunking_empty_diff_returns_empty():
    assert partition_diff_into_chunks("") == []
    assert partition_diff_into_chunks("   \n  ") == []


# ---------------------------------------------------------------------------
# 2. Phase 1c Context Reuse from Disk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase1c_context_reuse_from_disk(tmp_path):
    dossier_dir = tmp_path / "dossier"
    dossier_dir.mkdir(parents=True, exist_ok=True)
    p1c_file = dossier_dir / "phase-1c.md"
    expected_content = "# Pre-synthesized Phase 1c Context\n## Primary Intent & Problem Statement\nExisting authority notes."
    p1c_file.write_text(expected_content, encoding="utf-8")

    mock_llm = MagicMock()
    mock_llm.complete = AsyncMock(
        return_value={"content": json.dumps({"findings": [], "summary": "clean", "confidence": 90})}
    )

    with patch("app.subagents.audit_context.build_phase1_context") as mock_build:
        result = await run_audit(
            diff_text="diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n",
            repo_full_name="testowner/testrepo",
            pr_number=42,
            dossier_dir=str(dossier_dir),
            llm_client=mock_llm,
            enable_phase1_context=True,
        )
        # build_phase1_context should be skipped because phase-1c.md was found on disk
        mock_build.assert_not_called()
        assert result.phase_1c_context == expected_content
        assert result.phase_1c_file_path == str(p1c_file.resolve())


# ---------------------------------------------------------------------------
# 3. Finding Matching & Thread Resolution
# ---------------------------------------------------------------------------


def test_finding_matching_resolved_when_touched_and_not_reflagged():
    prior_findings = [
        {
            "id": "f-1",
            "file_path": "auth.py",
            "line_start": 10,
            "line_end": 15,
            "title": "SQL Injection in query",
            "category": "security",
            "severity": "BLOCKER",
        },
        {
            "id": "f-2",
            "file_path": "utils.py",
            "line_start": 50,
            "line_end": 55,
            "title": "Unbounded memory allocation",
            "category": "performance",
            "severity": "WARNING",
        },
    ]

    # Delta diff only modifies auth.py at line 12
    delta_diff = """diff --git a/auth.py b/auth.py
--- a/auth.py
+++ b/auth.py
@@ -10,6 +10,6 @@
-query = f"SELECT * FROM users WHERE id = {user_id}"
+query = "SELECT * FROM users WHERE id = %s"
"""
    # Delta review finds no issues in auth.py, but finds a new issue in another file
    delta_findings = [
        {
            "id": "f-3",
            "file_path": "config.py",
            "line_start": 2,
            "line_end": 4,
            "title": "Hardcoded secret",
            "category": "security",
            "severity": "BLOCKER",
        }
    ]

    review_threads = [
        {"id": "PRRT_1", "path": "auth.py", "line": 15, "isResolved": False, "isOutdated": False},
        {"id": "PRRT_2", "path": "utils.py", "line": 55, "isResolved": False, "isOutdated": False},
    ]

    res = match_and_resolve_findings(
        prior_findings=prior_findings,
        delta_findings=delta_findings,
        delta_diff=delta_diff,
        review_threads=review_threads,
    )

    # f-1 was touched in delta diff and not re-flagged -> resolved!
    assert len(res.resolved_findings) == 1
    assert res.resolved_findings[0]["id"] == "f-1"
    assert res.resolved_count == 1
    assert res.threads_to_resolve == ["PRRT_1"]

    # f-2 was not touched -> retained!
    assert len(res.retained_findings) == 1
    assert res.retained_findings[0]["id"] == "f-2"

    # f-3 was not in prior findings -> brand new!
    assert len(res.new_findings) == 1
    assert res.new_findings[0]["id"] == "f-3"

    # Active findings should be retained + new
    assert len(res.active_findings) == 2


def test_finding_matching_retained_when_touched_but_reflagged():
    prior_findings = [
        {
            "id": "f-1",
            "file_path": "auth.py",
            "line_start": 10,
            "line_end": 15,
            "title": "SQL Injection in query",
            "category": "security",
            "severity": "BLOCKER",
        }
    ]

    delta_diff = """diff --git a/auth.py b/auth.py
--- a/auth.py
+++ b/auth.py
@@ -10,6 +10,6 @@
-query = f"SELECT * FROM users WHERE id = {user_id}"
+query = f"SELECT * FROM users WHERE id = {clean(user_id)}"
"""
    # Author attempted a fix, but auditor re-flags it!
    delta_findings = [
        {
            "id": "f-1-reflagged",
            "file_path": "auth.py",
            "line_start": 10,
            "line_end": 15,
            "title": "SQL Injection in query still present",
            "category": "security",
            "severity": "BLOCKER",
        }
    ]

    res = match_and_resolve_findings(
        prior_findings=prior_findings,
        delta_findings=delta_findings,
        delta_diff=delta_diff,
        review_threads=[],
    )

    assert len(res.resolved_findings) == 0
    assert len(res.retained_findings) == 1
    assert res.retained_findings[0]["id"] == "f-1"
    assert len(res.new_findings) == 0


# ---------------------------------------------------------------------------
# 4. In-Place Review Update and Footer Swap
# ---------------------------------------------------------------------------


def test_report_footer_swap():
    initial_report = format_audit_report(
        pr_summary="Initial summary",
        must_fix_count=1,
        should_fix_count=0,
        confidence=90,
    )
    assert "*Generated autonomously by Haunter Guardian Mode. Zero changes were committed to your branch.*" in initial_report
    assert "resolved since last review" not in initial_report

    updated = swap_report_footer(initial_report, resolved_since_last_review=3)
    assert "resolved since last review: 3" in updated
    assert "*Generated autonomously by Haunter Guardian Mode. Zero changes were committed to your branch. · resolved since last review: 3*" in updated


@pytest.mark.asyncio
async def test_update_pr_review_github_client():
    from app import github_client

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.is_error = False
    mock_resp.json.return_value = {"id": 999, "body": "updated body"}

    with patch("httpx.AsyncClient.put", new_callable=AsyncMock) as mock_put:
        mock_put.return_value = mock_resp
        res = await github_client.update_pr_review(
            owner="octocat",
            repo="Hello-World",
            pr_number=1,
            review_id=999,
            body="updated body",
            token="ghp_test",
        )
        assert res["id"] == 999
        assert mock_put.call_count == 1
        args, kwargs = mock_put.call_args
        assert "/pulls/1/reviews/999" in args[0]
        assert kwargs["json"] == {"body": "updated body"}


@pytest.mark.asyncio
async def test_publisher_in_place_update_and_meta(tmp_path):
    dossier_dir = tmp_path / "dossier"

    finding = AuditFinding(
        id="f-1",
        perspective="architecture",
        severity="BLOCKER",
        category="security",
        title="Sample issue",
        description="detail",
        file_path="app.py",
        line_start=10,
        line_end=12,
        confidence=90,
        suggested_fix="use safe call",
    )
    result = AuditResult(
        audit_id="audit-123",
        audit_type="pr_audit",
        repo_full_name="org/repo",
        target_label="PR #5 (a" + "0" * 39 + ")",
        engine="gpt-4o",
        executive_summary="Summary",
        findings=[finding],
        confidence=90,
        status="⛔ Must Not Merge",
        publish_allowed=True,
        report_markdown="Initial report body\n---\n*Generated autonomously by Haunter Guardian Mode. Zero changes were committed to your branch.*",
    )

    with patch("app.github_client.update_pr_review", new_callable=AsyncMock) as mock_update, \
         patch("app.github_client.create_pr_review", new_callable=AsyncMock) as mock_create:
        mock_update.return_value = {"id": 12345, "body": "updated"}

        pub_res = await publish_audit_review(
            result=result,
            owner="org",
            repo="repo",
            head_sha="a" * 40,
            pr_number=5,
            review_id=12345,
            resolved_since_last_review=2,
            dossier_dir=str(dossier_dir),
            token="ghp_test",
        )

        assert pub_res.published is True
        mock_update.assert_called_once()
        # Verify footer swap in update body
        body_sent = mock_update.call_args.kwargs["body"]
        assert "resolved since last review: 2" in body_sent

        # Verify meta.json updated
        meta = load_dossier_meta(owner="org", repo="repo", pr_number=5, dossier_dir=str(dossier_dir))
        assert meta is not None
        assert meta["last_reviewed_sha"] == "a" * 40
        assert meta["review_id"] == 12345
        assert meta["resolved_findings_count"] == 2


# ---------------------------------------------------------------------------
# 5. Pipeline Re-Review Delta & Fallback Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_re_review_delta_diff_flow(tmp_path):
    dossier_dir = tmp_path / "dossier"
    sha_v1 = "1" * 40
    sha_v2 = "2" * 40
    sha_base = "0" * 40

    update_dossier_meta(
        owner="org",
        repo="repo",
        pr_number=10,
        head_sha=sha_v1,
        review_id=555,
        active_findings=[{"id": "old_f", "file_path": "a.py", "line_start": 5, "line_end": 6, "title": "Old bug", "category": "security"}],
        dossier_dir=str(dossier_dir),
    )

    repo_mock = MagicMock(spec=Repo)
    repo_mock.id = uuid.uuid4()
    repo_mock.owner = "org"
    repo_mock.name = "repo"

    delta_diff = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -5,2 +5,2 @@
-old line
+fixed line
"""

    with patch("app.github_client.fetch_pull_request", new_callable=AsyncMock) as mock_pr, \
         patch("app.github_client.fetch_diff", new_callable=AsyncMock) as mock_diff, \
         patch("app.github_client.fetch_review_threads", new_callable=AsyncMock) as mock_threads, \
         patch("app.github_client.resolve_review_threads", new_callable=AsyncMock) as mock_resolve, \
         patch("app.github_client.update_pr_review", new_callable=AsyncMock) as mock_update, \
         patch("app.github_client.fetch_file_content", new_callable=AsyncMock) as mock_fetch_file, \
         patch("app.subagents.auditor.run_audit", new_callable=AsyncMock) as mock_run_audit, \
         patch("app.services.audit_pipeline._pin_audit_target", new_callable=AsyncMock, return_value=True):

        mock_pr.return_value = {
            "base": {"sha": sha_base},
            "head": {"sha": sha_v2},
        }
        mock_diff.return_value = delta_diff
        mock_threads.return_value = [{"id": "PRRT_99", "path": "a.py", "line": 6, "isResolved": False, "isOutdated": False}]
        mock_resolve.return_value = 1
        mock_update.return_value = {"id": 555, "body": "updated"}

        dummy_result = AuditResult(
            audit_id="audit-999",
            audit_type="pr_audit",
            repo_full_name="org/repo",
            target_label="PR #10",
            engine="gpt-4o",
            executive_summary="clean",
            findings=[],
            confidence=95,
            status="✅ Great to Merge",
            publish_allowed=True,
            report_markdown="Report body\n---\n*Generated autonomously by Haunter Guardian Mode. Zero changes were committed to your branch.*",
        )
        mock_run_audit.return_value = dummy_result

        with patch("app.github.audit_publisher.load_dossier_meta", return_value={
            "last_reviewed_sha": sha_v1,
            "review_id": 555,
            "active_findings": [{"id": "old_f", "file_path": "a.py", "line_start": 5, "line_end": 6, "title": "Old bug", "category": "security"}],
        }):
            outcome = await execute_audit_job(
                audit_id="audit-999",
                audit_type="pr_audit",
                repo=repo_mock,
                ref="refs/pull/10/head",
                pr_number=10,
                base_sha=sha_base,
                head_sha=sha_v2,
                workflow_run_id=None,
                token="ghp_test",
            )

        assert outcome.status == "completed"
        # Verified compare API called with base_sha=v1 and sha=v2
        mock_diff.assert_called_once_with(
            owner="org",
            repo="repo",
            sha=sha_v2,
            base_sha=sha_v1,
            token="ghp_test",
            allow_global_token=False,
        )
        # Verified review threads resolved
        mock_resolve.assert_called_once_with(
            owner="org",
            repo="repo",
            thread_ids=["PRRT_99"],
            token="ghp_test",
        )
        # Verified in-place update called with review_id 555
        mock_update.assert_called_once()
        assert mock_update.call_args.kwargs["review_id"] == 555


@pytest.mark.asyncio
async def test_pipeline_empty_delta_diff_short_circuit():
    repo_mock = MagicMock(spec=Repo)
    repo_mock.id = uuid.uuid4()
    repo_mock.owner = "org"
    repo_mock.name = "repo"

    sha_v1 = "1" * 40
    sha_v2 = "2" * 40
    sha_base = "0" * 40

    with patch("app.github_client.fetch_pull_request", new_callable=AsyncMock) as mock_pr, \
         patch("app.github_client.fetch_diff", new_callable=AsyncMock) as mock_diff, \
         patch("app.github.audit_publisher.load_dossier_meta", return_value={"last_reviewed_sha": sha_v1, "review_id": 111}), \
         patch("app.services.audit_pipeline._pin_audit_target", new_callable=AsyncMock, return_value=True):

        mock_pr.return_value = {
            "base": {"sha": sha_base},
            "head": {"sha": sha_v2},
        }
        mock_diff.return_value = ""

        outcome = await execute_audit_job(
            audit_id="audit-empty",
            audit_type="pr_audit",
            repo=repo_mock,
            ref="refs/pull/10/head",
            pr_number=10,
            base_sha=sha_base,
            head_sha=sha_v2,
            workflow_run_id=None,
            token="ghp_test",
        )

        assert outcome.status == "skipped_no_diff"


@pytest.mark.asyncio
async def test_pipeline_force_push_compare_fallback():
    repo_mock = MagicMock(spec=Repo)
    repo_mock.id = uuid.uuid4()
    repo_mock.owner = "org"
    repo_mock.name = "repo"

    sha_v1 = "1" * 40
    sha_v2 = "2" * 40
    sha_base = "0" * 40

    with patch("app.github_client.fetch_pull_request", new_callable=AsyncMock) as mock_pr, \
         patch("app.github_client.fetch_diff", side_effect=Exception("404 Compare commits failed")), \
         patch("app.subagents.auditor.fetch_audit_diff", new_callable=AsyncMock) as mock_fetch_full, \
         patch("app.github_client.fetch_file_content", new_callable=AsyncMock) as mock_fetch_file, \
         patch("app.github.audit_publisher.load_dossier_meta", return_value={"last_reviewed_sha": sha_v1, "review_id": 111}), \
         patch("app.services.audit_pipeline._pin_audit_target", new_callable=AsyncMock, return_value=True), \
         patch("app.subagents.auditor.run_audit", new_callable=AsyncMock) as mock_run_audit, \
         patch("app.github.audit_publisher.publish_audit_review", new_callable=AsyncMock) as mock_pub:

        mock_pr.return_value = {
            "base": {"sha": sha_base},
            "head": {"sha": sha_v2},
        }
        mock_fetch_full.return_value = "diff --git a/full.py b/full.py\n--- a/full.py\n+++ b/full.py\n@@ -1 +1 @@\n-old\n+new\n"
        mock_run_audit.return_value = AuditResult(
            audit_id="audit-fallback",
            audit_type="pr_audit",
            repo_full_name="org/repo",
            target_label="PR #10",
            engine="gpt-4o",
            executive_summary="clean",
            findings=[],
            confidence=90,
            status="✅ Great to Merge",
        )
        mock_pub.return_value = MagicMock(published=True)

        outcome = await execute_audit_job(
            audit_id="audit-fallback",
            audit_type="pr_audit",
            repo=repo_mock,
            ref="refs/pull/10/head",
            pr_number=10,
            base_sha=sha_base,
            head_sha=sha_v2,
            workflow_run_id=None,
            token="ghp_test",
        )

        assert outcome.status == "completed"
        # Verified fallback to fetch_audit_diff
        mock_fetch_full.assert_called_once()


@pytest.mark.asyncio
async def test_publish_audit_review_with_dict_new_findings_and_active_findings(tmp_path):
    from unittest.mock import AsyncMock, patch
    from app.github.audit_publisher import publish_audit_review, load_dossier_meta

    result = AuditResult(
        audit_id="audit-dict-test",
        audit_type="pr_audit",
        repo_full_name="owner/repo",
        target_label="PR #5",
        engine="gpt-4o",
        executive_summary="Summary",
        findings=[],
        confidence=95,
        status="✅ Great to Merge",
        publish_allowed=True,
    )

    diff = "diff --git a/test.py b/test.py\n--- a/test.py\n+++ b/test.py\n@@ -1,1 +1,1 @@\n-old\n+new\n"
    dict_new_findings = [
        {
            "id": "new-1",
            "file_path": "test.py",
            "line_start": 1,
            "line_end": 1,
            "severity": "HIGH",
            "confidence": 90,
            "title": "Security flaw",
            "category": "security",
            "description": "Details",
            "suggested_fix": None,
            "informational_only": False,
        }
    ]
    active_findings = [
        {"id": "retained-1", "file_path": "old.py", "line_start": 10, "line_end": 12, "severity": "MEDIUM"},
        {"id": "new-1", "file_path": "test.py", "line_start": 1, "line_end": 1, "severity": "HIGH"},
    ]

    mock_client = AsyncMock()
    mock_client.update_pr_review = AsyncMock(return_value={"id": 101, "body": "ok"})
    mock_client.create_pr_review = AsyncMock(return_value={"id": 102})

    with patch("app.github.audit_publisher.github_client.update_pr_review", mock_client.update_pr_review), \
         patch("app.github.audit_publisher.github_client.create_pr_review", mock_client.create_pr_review):
        outcome = await publish_audit_review(
            result=result,
            owner="owner",
            repo="repo",
            pr_number=5,
            head_sha="a" * 40,
            diff_text=diff,
            token="token",
            review_id=101,
            new_findings=dict_new_findings,
            active_findings=active_findings,
            dossier_dir=str(tmp_path),
        )

    assert outcome.published is True
    assert mock_client.create_pr_review.called
    meta = load_dossier_meta(owner="owner", repo="repo", pr_number=5, dossier_dir=str(tmp_path))
    assert meta is not None
    # active_findings should have both retained and new findings preserved
    assert len(meta["active_findings"]) == 2
    assert meta["active_findings"][0]["id"] == "retained-1"
    assert meta["active_findings"][1]["id"] == "new-1"


@pytest.mark.asyncio
async def test_pipeline_re_review_fetch_threads_failure_resilience():
    from unittest.mock import AsyncMock, patch, MagicMock
    import uuid
    from app.services.audit_pipeline import execute_audit_job
    from app.models import Repo

    repo_mock = MagicMock(spec=Repo)
    repo_mock.id = uuid.uuid4()
    repo_mock.owner = "org"
    repo_mock.name = "repo"

    delta_diff = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,1 +1,1 @@
-old line
+fixed line
"""

    with patch("app.github_client.fetch_pull_request", new_callable=AsyncMock) as mock_pr, \
         patch("app.github_client.fetch_diff", new_callable=AsyncMock) as mock_diff, \
         patch("app.github_client.fetch_review_threads", new_callable=AsyncMock) as mock_threads, \
         patch("app.github_client.update_pr_review", new_callable=AsyncMock) as mock_update, \
         patch("app.github_client.fetch_file_content", new_callable=AsyncMock) as mock_fetch_file, \
         patch("app.subagents.auditor.run_audit", new_callable=AsyncMock) as mock_run_audit, \
         patch("app.services.audit_pipeline._pin_audit_target", new_callable=AsyncMock, return_value=True), \
         patch("app.github.audit_publisher.publish_audit_review", new_callable=AsyncMock) as mock_pub, \
         patch("app.github.audit_publisher.load_dossier_meta", return_value={
             "last_reviewed_sha": "1" * 40,
             "review_id": 555,
             "active_findings": [{"id": "old_f", "file_path": "a.py", "line_start": 1, "line_end": 1, "title": "Old bug", "category": "security"}],
         }):

        mock_pr.return_value = {
            "base": {"sha": "0" * 40},
            "head": {"sha": "2" * 40},
        }
        mock_diff.return_value = delta_diff
        # Simulate fetch_review_threads failure
        mock_threads.side_effect = RuntimeError("GraphQL network failure")
        mock_run_audit.return_value = AuditResult(
            audit_id="audit-thread-fail",
            audit_type="pr_audit",
            repo_full_name="org/repo",
            target_label="PR #10",
            engine="gpt-4o",
            executive_summary="clean",
            findings=[],
            confidence=95,
            status="✅ Great to Merge",
            publish_allowed=True,
        )
        mock_pub.return_value = MagicMock(published=True)

        outcome = await execute_audit_job(
            audit_id="audit-thread-fail",
            audit_type="pr_audit",
            repo=repo_mock,
            ref="refs/pull/10/head",
            pr_number=10,
            base_sha="0" * 40,
            head_sha="2" * 40,
            workflow_run_id=None,
            token="ghp_test",
        )

        assert outcome.status == "completed"
        # Verified publish_audit_review was still invoked with active_findings despite thread fetch failure
        mock_pub.assert_called_once()
        pub_kwargs = mock_pub.call_args.kwargs
        assert pub_kwargs["review_id"] == 555
        assert pub_kwargs["active_findings"] is not None


def test_finding_matching_retained_when_in_unchanged_hunk_context():
    from app.subagents.audit_matcher import match_and_resolve_findings

    prior_findings = [
        {
            "id": "f-ctx",
            "file_path": "service.py",
            "line_start": 5,
            "line_end": 7,
            "title": "Old bug in context",
            "category": "correctness",
            "severity": "MEDIUM",
        }
    ]

    # Delta diff hunk touches line 1-2, but lines 3-10 are untouched context
    delta_diff = """diff --git a/service.py b/service.py
--- a/service.py
+++ b/service.py
@@ -1,8 +1,8 @@
-old line 1
+new line 1
 context line 2
 context line 3
 context line 4
 context line 5
 context line 6
 context line 7
 context line 8
"""

    res = match_and_resolve_findings(
        prior_findings=prior_findings,
        delta_findings=[],
        delta_diff=delta_diff,
        review_threads=[],
    )

    # Finding on lines 5-7 was in unchanged context: NOT touched, must be RETAINED!
    assert len(res.resolved_findings) == 0
    assert len(res.retained_findings) == 1
    assert res.retained_findings[0]["id"] == "f-ctx"
