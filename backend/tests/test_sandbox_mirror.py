r"""
Phase 2 regression test for ``_parse_patch_files`` (NICE-4).

The mirror parser used to compare the captured file path with ``"/dev/null"``
exactly, which (in some upstream pipelines) made malformed ``+++ /dev/null``
lines fall through and be treated as a real file path. NICE-4 documents the
observed failure mode: "the parser will treat the next ``+++`` as a new hunk
header" → the deletion is silently dropped, and the test-mirror ends up
with the wrong file tree.

The fix wraps the comparison in ``.strip()`` so CRLF, stray tab, or
leading/trailing space on the marker line all classify as a deletion. The
test exercises the worst-case input described in NICE-4: a ``+++ /dev/null``
with a trailing tab AND the next ``+++ b/somefile`` glued onto the same
line (the line is on its own logical line; the captured token is the
``\S+`` run, which stops at the first whitespace — so the captured group
is ``"/dev/null"`` and the rest of the line is never re-parsed as a header).

This file is **sync** and **DB-free**: ``_parse_patch_files`` is a pure
function of its input. No fixture, no async, no engine.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from app.config import settings
from app.sandbox.github_actions_runner import (
    GitHubActionsSandboxRunner,
    _delete_branch_ref,
)
from app.sandbox.mirror import (
    _parse_patch_files,
    get_or_create_test_mirror,
    get_universal_sandbox_repo,
    push_patch_to_mirror,
)
from app.sandbox.runner import SandboxInput


def test_malformed_dev_null() -> None:
    """
    A patch where ``+++ /dev/null`` is glued to the next ``+++ b/somefile``
    on a single line must still be recognised as a deletion marker.

    Asserts (per the NICE-4 acceptance line):
      - The parser does NOT produce a file entry whose path starts with
        ``"/dev/null"`` glued with a tab/space (i.e. no file entry named
        ``"/dev/null\t+++"`` or similar).
      - If the parser yields any file entries, none of them are the
        malformed marker.
    """
    # The glued input: a single line where ``+++ /dev/null`` is followed by
    # a tab and then ``+++ b/somefile`` on the same line. The regex
    # ``^(?:---|\+\+\+)\s+(?:[ab]/)?(\S+)`` captures the FIRST ``\S+`` run,
    # which stops at the tab/whitespace. The captured token is
    # ``"/dev/null"`` — the ``\t+++ b/somefile`` suffix is NOT a second
    # header (it's on the same consumed line, and the parser does not
    # re-split the line). Pre-fix (no .strip() and an exact equality check)
    # the parser had no way to classify the captured group as a deletion.
    glued_line = "+++ /dev/null\t+++ b/somefile"

    # A real new-file header on a separate line — this is what the test
    # author was worried the parser would MISS when the deletion marker
    # was glued. The parser must not produce a file entry named
    # ``/dev/null\t+++`` from the glued line, and the only file it
    # produces from the SEPARATE ``+++ b/somefile`` line (if it ever
    # re-parses the tail) is ``somefile`` (after the optional ``[ab]/``
    # prefix is consumed by the regex).
    patch = (
        "--- a/old.py\n"
        "+++ b/old.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
        f"{glued_line}\n"
    )

    result = _parse_patch_files(patch)

    # The parser must not produce a file whose path is the glued
    # ``/dev/null\t+++`` token (or any path starting with ``/dev/null``).
    for parsed_path in result:
        assert not parsed_path.startswith("/dev/null"), (
            f"parser produced a file path starting with /dev/null: "
            f"{parsed_path!r}. The glued marker must be classified as a "
            f"deletion marker, not a file path."
        )
        # Explicit defensive assertion on the exact glued name.
        assert "\t" not in parsed_path, (
            f"parser produced a file path containing a literal tab: "
            f"{parsed_path!r}. The glued marker must be classified as a "
            f"deletion marker, not a file path."
        )

    # The real ``--- a/old.py`` / ``+++ b/old.py`` headers on the FIRST
    # line of the patch are still parsed correctly — the deletion marker
    # on the glued line does not corrupt earlier file entries.
    # The key is the ``+++ b/old.py`` header was consumed BEFORE the
    # glued line, so ``old.py`` is the active current_path and its
    # content is reconstructed from the hunk below.
    assert "old.py" in result, (
        f"parser should have produced the old.py file from the pre-glued "
        f"hunk; got keys: {sorted(result.keys())!r}"
    )
    assert "old.py" in result
    assert "new" in result["old.py"]
    assert "old" not in result["old.py"]


# ---------------------------------------------------------------------------
# Universal mirror repo resolution tests
# ---------------------------------------------------------------------------


def test_get_universal_sandbox_repo_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_universal_sandbox_repo returns {org}/{settings.github_sandbox_repo}."""
    monkeypatch.setattr(settings, "github_sandbox_repo", "haunter-sandbox-runner")
    repo = get_universal_sandbox_repo("test-org")
    assert repo == "test-org/haunter-sandbox-runner"


def test_get_universal_sandbox_repo_custom(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_universal_sandbox_repo reflects configured repo name."""
    monkeypatch.setattr(settings, "github_sandbox_repo", "custom-sandbox-runner")
    repo = get_universal_sandbox_repo("my-org")
    assert repo == "my-org/custom-sandbox-runner"


# ---------------------------------------------------------------------------
# get_or_create_test_mirror tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_or_create_test_mirror_existing_200() -> None:
    """Existing mirror repository returns full name on 200 without creating."""
    with respx.mock(base_url="https://api.github.com") as rx:
        rx.get("/repos/test-org/haunter-sandbox-runner").respond(
            200, json={"full_name": "test-org/haunter-sandbox-runner"}
        )
        async with httpx.AsyncClient() as client:
            repo_full = await get_or_create_test_mirror(
                client, "test-org", token="test_token"
            )
        assert repo_full == "test-org/haunter-sandbox-runner"


@pytest.mark.asyncio
async def test_get_or_create_test_mirror_creates_on_404() -> None:
    """Missing mirror repository (404) triggers repo creation with private:true, auto_init:true."""
    with respx.mock(base_url="https://api.github.com") as rx:
        rx.get("/repos/test-org/haunter-sandbox-runner").respond(404)
        rx.get("/orgs/test-org").respond(200)
        post_route = rx.post("/orgs/test-org/repos").respond(
            201, json={"full_name": "test-org/haunter-sandbox-runner"}
        )

        async with httpx.AsyncClient() as client:
            repo_full = await get_or_create_test_mirror(
                client, "test-org", token="test_token"
            )

        assert repo_full == "test-org/haunter-sandbox-runner"
        assert post_route.called
        payload = json.loads(post_route.calls.last.request.content)
        assert payload["name"] == "haunter-sandbox-runner"
        assert payload["private"] is True
        assert payload["auto_init"] is True


# ---------------------------------------------------------------------------
# push_patch_to_mirror tests (Orphan Tree & Commit)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_patch_to_mirror_orphan_tree_and_commit() -> None:
    """push_patch_to_mirror creates an orphan root tree (base_tree=None) and orphan commit (parents=[])."""
    patch = (
        "--- a/lib.py\n"
        "+++ b/lib.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-old_code\n"
        "+new_code\n"
    )
    branch = "sandbox/run-12345-att-1"
    repo_full = "test-org/haunter-sandbox-runner"

    with respx.mock(base_url="https://api.github.com") as rx:
        tree_route = rx.post(f"/repos/{repo_full}/git/trees").respond(
            201, json={"sha": "tree_orphan_sha_123"}
        )
        commit_route = rx.post(f"/repos/{repo_full}/git/commits").respond(
            201, json={"sha": "commit_orphan_sha_456"}
        )
        ref_route = rx.post(f"/repos/{repo_full}/git/refs").respond(
            201,
            json={
                "ref": f"refs/heads/{branch}",
                "object": {"sha": "commit_orphan_sha_456"},
            },
        )

        seed_files = {"src/app.py": b"print('hello')"}

        async with httpx.AsyncClient() as client:
            sha = await push_patch_to_mirror(
                client,
                repo_full,
                branch=branch,
                patch_text=patch,
                workflow_filename="haunter-test-py.yml",
                workflow_content="name: CI\n",
                commit_message="haunter attempt 1",
                token="test_token",
                seed_files=seed_files,
            )

        assert sha == "commit_orphan_sha_456"

        # Assert tree payload: base_tree is None, tree contains seed, workflow, and patch
        tree_payload = json.loads(tree_route.calls.last.request.content)
        assert tree_payload["base_tree"] is None
        paths = [e["path"] for e in tree_payload["tree"]]
        assert "src/app.py" in paths
        assert ".github/workflows/haunter-test-py.yml" in paths
        assert "lib.py" in paths

        # Assert commit payload: parents is empty list (orphan commit)
        commit_payload = json.loads(commit_route.calls.last.request.content)
        assert commit_payload["parents"] == []
        assert commit_payload["tree"] == "tree_orphan_sha_123"

        # Assert ref payload points to refs/heads/{branch}
        ref_payload = json.loads(ref_route.calls.last.request.content)
        assert ref_payload["ref"] == f"refs/heads/{branch}"
        assert ref_payload["sha"] == "commit_orphan_sha_456"


# ---------------------------------------------------------------------------
# _delete_branch_ref tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_branch_ref_success_204() -> None:
    """_delete_branch_ref sends DELETE and handles 204 successfully."""
    repo_full = "test-org/haunter-sandbox-runner"
    branch = "sandbox/run-abc-att-1"
    with respx.mock(base_url="https://api.github.com") as rx:
        del_route = rx.delete(f"/repos/{repo_full}/git/refs/heads/{branch}").respond(204)
        async with httpx.AsyncClient() as client:
            await _delete_branch_ref(client, repo_full, branch, token="tok_123")
        assert del_route.called


@pytest.mark.asyncio
async def test_delete_branch_ref_already_gone_404() -> None:
    """_delete_branch_ref gracefully handles 404 (already gone) without raising."""
    repo_full = "test-org/haunter-sandbox-runner"
    branch = "sandbox/run-xyz-att-2"
    with respx.mock(base_url="https://api.github.com") as rx:
        del_route = rx.delete(f"/repos/{repo_full}/git/refs/heads/{branch}").respond(404)
        async with httpx.AsyncClient() as client:
            await _delete_branch_ref(client, repo_full, branch, token="tok_123")
        assert del_route.called


# ---------------------------------------------------------------------------
# End-to-end GitHubActionsSandboxRunner.verify() with branch cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_orphan_branch_and_finally_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """verify() targets universal repo, creates orphan commit, and always calls _delete_branch_ref in finally."""
    monkeypatch.setattr(settings, "github_sandbox_app_id", "app_123")
    monkeypatch.setattr(settings, "github_sandbox_installation_id", "inst_123")
    monkeypatch.setattr(settings, "github_sandbox_org", "test-org")
    monkeypatch.setattr(settings, "github_sandbox_repo", "haunter-sandbox-runner")
    monkeypatch.setattr(settings, "github_sandbox_poll_interval_seconds", 0.01)
    monkeypatch.setattr(settings, "github_sandbox_poll_timeout_seconds", 1.0)

    run_id = uuid.uuid4()
    branch = f"sandbox/run-{run_id}-att-1"
    repo_full = "test-org/haunter-sandbox-runner"

    # Mock token mint
    monkeypatch.setattr(
        "app.sandbox.github_actions_runner.mint_installation_token",
        AsyncMock(return_value="mock_inst_token"),
    )

    with respx.mock(base_url="https://api.github.com") as rx:
        rx.get(f"/repos/{repo_full}").respond(200, json={"full_name": repo_full})
        tree_route = rx.post(f"/repos/{repo_full}/git/trees").respond(
            201, json={"sha": "tree_sha_1"}
        )
        commit_route = rx.post(f"/repos/{repo_full}/git/commits").respond(
            201, json={"sha": "commit_sha_1"}
        )
        ref_route = rx.post(f"/repos/{repo_full}/git/refs").respond(
            201, json={"ref": f"refs/heads/{branch}", "sha": "commit_sha_1"}
        )
        # Poll actions runs -> completed with success
        rx.get(f"/repos/{repo_full}/actions/runs").respond(
            200,
            json={
                "workflow_runs": [
                    {"id": 999, "status": "completed", "conclusion": "success"}
                ]
            },
        )
        del_route = rx.delete(f"/repos/{repo_full}/git/refs/heads/{branch}").respond(204)

        runner = GitHubActionsSandboxRunner()
        inp = SandboxInput(
            run_id=run_id,
            repo_ref="owner/repo",
            patch="--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-a\n+b\n",
            attempt_number=1,
            user_github_id=123,
        )
        result = await runner.verify(inp)

        assert result["passed"] is True
        assert result["reason"] is None

        # a) Single repo target haunter-sandbox-runner
        assert tree_route.calls.last.request.url.path == f"/repos/{repo_full}/git/trees"

        # b) Orphan commit creation with parents: []
        commit_payload = json.loads(commit_route.calls.last.request.content)
        assert commit_payload["parents"] == []

        # c) Correct sandbox/run-... branch naming
        ref_payload = json.loads(ref_route.calls.last.request.content)
        assert ref_payload["ref"] == f"refs/heads/{branch}"

        # d) Branch deletion API call is invoked in finally
        assert del_route.called
        assert del_route.calls.last.request.url.path == f"/repos/{repo_full}/git/refs/heads/{branch}"


@pytest.mark.asyncio
async def test_verify_branch_cleanup_on_failure_in_finally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """verify() deletes the branch ref in finally even when the workflow run fails."""
    monkeypatch.setattr(settings, "github_sandbox_app_id", "app_123")
    monkeypatch.setattr(settings, "github_sandbox_installation_id", "inst_123")
    monkeypatch.setattr(settings, "github_sandbox_org", "test-org")
    monkeypatch.setattr(settings, "github_sandbox_repo", "haunter-sandbox-runner")
    monkeypatch.setattr(settings, "github_sandbox_poll_interval_seconds", 0.01)
    monkeypatch.setattr(settings, "github_sandbox_poll_timeout_seconds", 1.0)

    run_id = uuid.uuid4()
    branch = f"sandbox/run-{run_id}-att-2"
    repo_full = "test-org/haunter-sandbox-runner"

    monkeypatch.setattr(
        "app.sandbox.github_actions_runner.mint_installation_token",
        AsyncMock(return_value="mock_inst_token"),
    )

    with respx.mock(base_url="https://api.github.com") as rx:
        rx.get(f"/repos/{repo_full}").respond(200, json={"full_name": repo_full})
        rx.post(f"/repos/{repo_full}/git/trees").respond(
            201, json={"sha": "tree_sha_fail"}
        )
        rx.post(f"/repos/{repo_full}/git/commits").respond(
            201, json={"sha": "commit_sha_fail"}
        )
        rx.post(f"/repos/{repo_full}/git/refs").respond(
            201, json={"ref": f"refs/heads/{branch}", "sha": "commit_sha_fail"}
        )
        # Poll actions runs -> failure
        rx.get(f"/repos/{repo_full}/actions/runs").respond(
            200,
            json={
                "workflow_runs": [
                    {"id": 888, "status": "completed", "conclusion": "failure"}
                ]
            },
        )
        rx.get(f"/repos/{repo_full}/actions/runs/888/jobs").respond(
            200,
            json={
                "jobs": [
                    {
                        "id": 1,
                        "name": "test",
                        "conclusion": "failure",
                        "steps": [{"name": "Run tests", "conclusion": "failure"}],
                    }
                ]
            },
        )
        del_route = rx.delete(f"/repos/{repo_full}/git/refs/heads/{branch}").respond(204)

        runner = GitHubActionsSandboxRunner()
        inp = SandboxInput(
            run_id=run_id,
            repo_ref="owner/repo",
            patch="--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-a\n+b\n",
            attempt_number=2,
            user_github_id=123,
        )
        result = await runner.verify(inp)

        assert result["passed"] is False
        assert "failure" in result["reason"]
        # Assert cleanup still happened in finally
        assert del_route.called
