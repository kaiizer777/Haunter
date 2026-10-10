"""
Unit and integration tests for Haunter Session Recon & Navigation Tools.

Covers:
- read_file_slice: 1-based indexing, start > end error, out of bounds, path traversal.
- glob_files: recursive wildcard matching (**), pattern filters, hidden file exclusions, traversal.
- grep_search: regex & substring matching, case sensitivity, path prefix, max_results cap.
- list_directory: depth limiting, subfolder exploration, path traversal.
- Orchestrator dispatch: tool routing and safe error string propagation.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.github_client import GitHubClientError
from app.services.session_orchestrator import SessionOrchestrator
from app.services.session_streamer import SseQueue
from app.services.session_tools.recon import (
    _validate_file_path,
    glob_files,
    grep_search,
    list_directory,
    read_file_slice,
    tool_glob_files,
    tool_grep_search,
    tool_list_directory,
    tool_read_file_slice,
)


# ---------------------------------------------------------------------------
# Path Traversal Protection
# ---------------------------------------------------------------------------


def test_validate_file_path_rejects_traversal() -> None:
    """Directory traversal and absolute paths must be rejected with ValueError."""
    with pytest.raises(ValueError, match="Directory traversal rejected"):
        _validate_file_path("../../etc/passwd")

    with pytest.raises(ValueError, match="Directory traversal rejected"):
        _validate_file_path("src/../../secret.txt")

    with pytest.raises(ValueError, match="Absolute path rejected"):
        _validate_file_path("/etc/passwd")

    with pytest.raises(ValueError, match="Path contains disallowed characters"):
        _validate_file_path("src/main.py; rm -rf /")

    # Valid paths must pass unchanged
    assert _validate_file_path("src/main.py") == "src/main.py"
    assert (
        _validate_file_path("backend/app/services/recon.py")
        == "backend/app/services/recon.py"
    )
    assert _validate_file_path(".") == "."


# ---------------------------------------------------------------------------
# read_file_slice Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_file_slice_indexing_and_bounds() -> None:
    """read_file_slice must return 1-based line-numbered slices."""
    file_content = "line 1\nline 2\nline 3\nline 4\nline 5"

    with patch(
        "app.services.session_tools.recon.fetch_file_content", new_callable=AsyncMock
    ) as mock_fetch:
        mock_fetch.return_value = file_content

        # Slice lines 2 to 4
        result = await tool_read_file_slice(
            path="src/app.py",
            start_line=2,
            end_line=4,
            owner="org",
            repo="repo",
            base_sha="sha1",
        )
        assert result == "2: line 2\n3: line 3\n4: line 4"

        # Slice single line
        single = await tool_read_file_slice(
            path="src/app.py",
            start_line=1,
            end_line=1,
            owner="org",
            repo="repo",
            base_sha="sha1",
        )
        assert single == "1: line 1"

        # End line exceeds total lines -> clamp to end of file
        clamped = await tool_read_file_slice(
            path="src/app.py",
            start_line=3,
            end_line=20,
            owner="org",
            repo="repo",
            base_sha="sha1",
        )
        assert clamped == "3: line 3\n4: line 4\n5: line 5"


@pytest.mark.asyncio
async def test_read_file_slice_validation_errors() -> None:
    """read_file_slice must raise ValueError on start > end, start < 1, or start > total."""
    file_content = "alpha\nbeta\ngamma"

    with patch(
        "app.services.session_tools.recon.fetch_file_content", new_callable=AsyncMock
    ) as mock_fetch:
        mock_fetch.return_value = file_content

        # start_line > end_line
        with pytest.raises(ValueError, match="cannot exceed end_line"):
            await tool_read_file_slice(
                path="src/test.py",
                start_line=3,
                end_line=1,
                owner="org",
                repo="repo",
                base_sha="sha1",
            )

        # start_line < 1
        with pytest.raises(ValueError, match="Line numbers are 1-based"):
            await tool_read_file_slice(
                path="src/test.py",
                start_line=0,
                end_line=2,
                owner="org",
                repo="repo",
                base_sha="sha1",
            )

        # start_line > total_lines
        with pytest.raises(ValueError, match="exceeds total line count"):
            await tool_read_file_slice(
                path="src/test.py",
                start_line=10,
                end_line=15,
                owner="org",
                repo="repo",
                base_sha="sha1",
            )

        # Path traversal
        with pytest.raises(ValueError, match="Directory traversal rejected"):
            await tool_read_file_slice(
                path="../../etc/passwd",
                start_line=1,
                end_line=5,
                owner="org",
                repo="repo",
                base_sha="sha1",
            )


@pytest.mark.asyncio
async def test_read_file_slice_file_not_found() -> None:
    """read_file_slice must return descriptive message when file does not exist."""
    with patch(
        "app.services.session_tools.recon.fetch_file_content", new_callable=AsyncMock
    ) as mock_fetch:
        mock_fetch.return_value = None

        result = await read_file_slice(
            path="missing.py",
            start_line=1,
            end_line=5,
            owner="org",
            repo="repo",
            base_sha="sha1",
        )
        assert result == "File not found: 'missing.py'"


# ---------------------------------------------------------------------------
# glob_files Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_glob_files_wildcards() -> None:
    """glob_files must match paths using git-style recursive wildcards."""
    mock_tree = {
        "tree": [
            {"path": "src/auth.py", "type": "blob"},
            {"path": "src/components/button.tsx", "type": "blob"},
            {"path": "src/components/ui/input.tsx", "type": "blob"},
            {"path": "backend/app/services/auth_service.py", "type": "blob"},
            {"path": "backend/app/services/user_service.py", "type": "blob"},
            {"path": ".github/workflows/ci.yml", "type": "blob"},
            {"path": ".env", "type": "blob"},
            {"path": "README.md", "type": "blob"},
        ]
    }

    with patch(
        "app.services.session_tools.recon.fetch_git_tree", new_callable=AsyncMock
    ) as mock_tree_fetch:
        mock_tree_fetch.return_value = mock_tree

        # Match all auth files anywhere
        auth_matches = await tool_glob_files(
            pattern="**/*auth*.py",
            owner="org",
            repo="repo",
            base_sha="sha1",
        )
        assert auth_matches == ["backend/app/services/auth_service.py", "src/auth.py"]

        # Match components tsx
        comp_matches = await glob_files(
            pattern="src/components/**/*.tsx",
            owner="org",
            repo="repo",
            base_sha="sha1",
        )
        assert comp_matches == [
            "src/components/button.tsx",
            "src/components/ui/input.tsx",
        ]

        # Exclude hidden by default
        all_hidden_excluded = await tool_glob_files(
            pattern="**/*",
            exclude_hidden=True,
            owner="org",
            repo="repo",
            base_sha="sha1",
        )
        assert ".github/workflows/ci.yml" not in all_hidden_excluded
        assert ".env" not in all_hidden_excluded
        assert "README.md" in all_hidden_excluded

        # Include hidden when exclude_hidden=False
        with_hidden = await tool_glob_files(
            pattern="**/*",
            exclude_hidden=False,
            owner="org",
            repo="repo",
            base_sha="sha1",
        )
        assert ".env" in with_hidden
        assert ".github/workflows/ci.yml" in with_hidden

        # Path traversal rejection
        with pytest.raises(ValueError, match="Directory traversal rejected"):
            await tool_glob_files(
                pattern="../../etc/*",
                owner="org",
                repo="repo",
                base_sha="sha1",
            )


# ---------------------------------------------------------------------------
# grep_search Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grep_search_regex_and_substring() -> None:
    """grep_search must support substring and regex matching with case sensitivity."""
    mock_tree = {
        "tree": [
            {"path": "src/auth.py", "type": "blob"},
            {"path": "src/user.py", "type": "blob"},
            {"path": "logo.png", "type": "blob"},  # Binary should be skipped
        ]
    }

    files = {
        "src/auth.py": (
            "import jwt\n"
            "SECRET_KEY = 'supersecret'\n"
            "def verify_token(token: str):\n"
            "    return True\n"
        ),
        "src/user.py": (
            "from auth import verify_token\n"
            "def get_user_profile(user_id: int):\n"
            "    # TODO: verify_token\n"
            "    return {'id': user_id}\n"
        ),
    }

    async def _mock_content(owner, repo, path, sha, token=None):
        return files.get(path)

    with (
        patch(
            "app.services.session_tools.recon.fetch_git_tree", new_callable=AsyncMock
        ) as mock_tree_fetch,
        patch(
            "app.services.session_tools.recon.fetch_file_content",
            side_effect=_mock_content,
        ),
    ):
        mock_tree_fetch.return_value = mock_tree

        # 1. Case-insensitive substring search
        res = await tool_grep_search(
            query="secret_key",
            case_sensitive=False,
            owner="org",
            repo="repo",
            base_sha="sha1",
        )
        assert "src/auth.py:2: SECRET_KEY = 'supersecret'" in res

        # 2. Case-sensitive substring search (negative and positive)
        res_case_neg = await grep_search(
            query="secret_key",
            case_sensitive=True,
            owner="org",
            repo="repo",
            base_sha="sha1",
        )
        assert "No matches found" in res_case_neg

        res_case_pos = await grep_search(
            query="SECRET_KEY",
            case_sensitive=True,
            owner="org",
            repo="repo",
            base_sha="sha1",
        )
        assert "src/auth.py:2: SECRET_KEY = 'supersecret'" in res_case_pos

        # 3. Regex search for function definitions
        res_regex = await tool_grep_search(
            query=r"def\s+\w+\(",
            owner="org",
            repo="repo",
            base_sha="sha1",
        )
        assert "src/auth.py:3: def verify_token(token: str):" in res_regex
        assert "src/user.py:2: def get_user_profile(user_id: int):" in res_regex

        # 4. Path prefix narrowing
        res_prefix = await tool_grep_search(
            query="verify_token",
            path_prefix="src/user.py",
            owner="org",
            repo="repo",
            base_sha="sha1",
        )
        assert "src/auth.py" not in res_prefix
        assert "src/user.py:1: from auth import verify_token" in res_prefix

        # 5. Max results capping
        res_capped = await tool_grep_search(
            query="verify_token",
            max_results=1,
            owner="org",
            repo="repo",
            base_sha="sha1",
        )
        lines = res_capped.splitlines()
        assert len(lines) == 1

        # 6. Path traversal rejection
        with pytest.raises(ValueError, match="Directory traversal rejected"):
            await tool_grep_search(
                query="test",
                path_prefix="../../etc",
                owner="org",
                repo="repo",
                base_sha="sha1",
            )


# ---------------------------------------------------------------------------
# list_directory Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_directory_depth_limiting() -> None:
    """list_directory must respect hierarchical depth limits."""
    mock_tree = {
        "tree": [
            {"path": "backend/app/main.py", "type": "blob"},
            {"path": "backend/app/services/recon.py", "type": "blob"},
            {"path": "backend/tests/test_recon.py", "type": "blob"},
            {"path": "frontend/src/index.tsx", "type": "blob"},
            {"path": "README.md", "type": "blob"},
        ]
    }

    with patch(
        "app.services.session_tools.recon.fetch_git_tree", new_callable=AsyncMock
    ) as mock_tree_fetch:
        mock_tree_fetch.return_value = mock_tree

        # Root listing with depth 1
        d1 = await tool_list_directory(
            path=".",
            depth=1,
            owner="org",
            repo="repo",
            base_sha="sha1",
        )
        assert "backend/" in d1
        assert "frontend/" in d1
        assert "README.md" in d1
        assert "backend/app/" not in d1
        assert "backend/app/main.py" not in d1

        # Root listing with depth 2
        d2 = await list_directory(
            path=".",
            depth=2,
            owner="org",
            repo="repo",
            base_sha="sha1",
        )
        assert "backend/" in d2
        assert "backend/app/" in d2
        assert "backend/tests/" in d2
        assert "frontend/" in d2
        assert "frontend/src/" in d2
        assert "README.md" in d2
        assert "backend/app/services/" not in d2
        assert "backend/app/main.py" not in d2

        # Subdirectory listing (backend) with depth 1
        sub_d1 = await tool_list_directory(
            path="backend",
            depth=1,
            owner="org",
            repo="repo",
            base_sha="sha1",
        )
        assert "app/" in sub_d1
        assert "tests/" in sub_d1
        assert "app/main.py" not in sub_d1

        # Subdirectory listing (backend) with depth 2
        sub_d2 = await tool_list_directory(
            path="backend",
            depth=2,
            owner="org",
            repo="repo",
            base_sha="sha1",
        )
        assert "app/" in sub_d2
        assert "app/main.py" in sub_d2
        assert "tests/" in sub_d2
        assert "tests/test_recon.py" in sub_d2
        assert "app/services/recon.py" not in sub_d2

        # Path traversal rejection
        with pytest.raises(ValueError, match="Directory traversal rejected"):
            await tool_list_directory(
                path="../../etc/passwd",
                owner="org",
                repo="repo",
                base_sha="sha1",
            )


# ---------------------------------------------------------------------------
# Orchestrator Dispatch Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_dispatch_recon_tools() -> None:
    """SessionOrchestrator._dispatch_tool routes recon tools cleanly without raising unhandled errors."""
    mock_db = AsyncMock()
    orchestrator = SessionOrchestrator(
        session_id=None,  # type: ignore[arg-type]
        db=mock_db,
        gh_token="dummy-token",
    )
    mock_queue = AsyncMock(spec=SseQueue)

    # 1. read_file_slice dispatch
    with patch(
        "app.services.session_orchestrator.tool_read_file_slice", new_callable=AsyncMock
    ) as m:
        m.return_value = "1: print('hello')"
        res = await orchestrator._dispatch_tool(
            tool_name="read_file_slice",
            args={"path": "src/main.py", "start_line": 1, "end_line": 1},
            repo_owner="org",
            repo_name="repo",
            base_sha="sha1",
            staged_patches={},
            queue=mock_queue,
        )
        assert res == "1: print('hello')"
        m.assert_awaited_once_with(
            path="src/main.py",
            start_line=1,
            end_line=1,
            owner="org",
            repo="repo",
            base_sha="sha1",
            staged_patches={},
            token="dummy-token",
            session_id=None,
        )

    # 2. grep_search dispatch
    with patch(
        "app.services.session_orchestrator.tool_grep_search", new_callable=AsyncMock
    ) as m:
        m.return_value = "src/main.py:10: def foo():"
        res = await orchestrator._dispatch_tool(
            tool_name="grep_search",
            args={"query": "def foo", "max_results": 10},
            repo_owner="org",
            repo_name="repo",
            base_sha="sha1",
            staged_patches={},
            queue=mock_queue,
        )
        assert res == "src/main.py:10: def foo():"
        m.assert_awaited_once_with(
            query="def foo",
            path_prefix="",
            case_sensitive=False,
            max_results=10,
            owner="org",
            repo="repo",
            base_sha="sha1",
            staged_patches={},
            token="dummy-token",
            session_id=None,
        )

    # 3. glob_files dispatch
    with patch(
        "app.services.session_orchestrator.tool_glob_files", new_callable=AsyncMock
    ) as m:
        m.return_value = ["src/a.py", "src/b.py"]
        res = await orchestrator._dispatch_tool(
            tool_name="glob_files",
            args={"pattern": "src/*.py"},
            repo_owner="org",
            repo_name="repo",
            base_sha="sha1",
            staged_patches={},
            queue=mock_queue,
        )
        assert res == "src/a.py\nsrc/b.py"
        m.assert_awaited_once_with(
            pattern="src/*.py",
            exclude_hidden=True,
            owner="org",
            repo="repo",
            base_sha="sha1",
            staged_patches={},
            token="dummy-token",
        )

    # 4. list_directory dispatch
    with patch(
        "app.services.session_orchestrator.tool_list_directory", new_callable=AsyncMock
    ) as m:
        m.return_value = ["app/", "main.py"]
        res = await orchestrator._dispatch_tool(
            tool_name="list_directory",
            args={"path": ".", "depth": 1},
            repo_owner="org",
            repo_name="repo",
            base_sha="sha1",
            staged_patches={},
            queue=mock_queue,
        )
        assert res == "app/\nmain.py"
        m.assert_awaited_once_with(
            path=".",
            depth=1,
            owner="org",
            repo="repo",
            base_sha="sha1",
            staged_patches={},
            token="dummy-token",
        )

    # 5. Error containment (e.g. ValueError or GitHubClientError returns error string)
    with patch(
        "app.services.session_orchestrator.tool_read_file_slice",
        side_effect=ValueError("Invalid range"),
    ):
        res = await orchestrator._dispatch_tool(
            tool_name="read_file_slice",
            args={"path": "src/main.py", "start_line": 5, "end_line": 1},
            repo_owner="org",
            repo_name="repo",
            base_sha="sha1",
            staged_patches={},
            queue=mock_queue,
        )
        assert res == "Error: Invalid range"


@pytest.mark.asyncio
async def test_tool_git_diff_working_tree_and_staged() -> None:
    from app.services.session_tools.git import tool_git_diff

    staged = {
        "app/test.py": "--- a/app/test.py\n+++ b/app/test.py\n@@ -1 +1 @@\n-old\n+new\n"
    }

    # Test querying staged patches
    diff_staged = await tool_git_diff(
        base="HEAD",
        head="staged",
        owner="org",
        repo="repo",
        token="token",
        staged_patches=staged,
    )
    assert "app/test.py" in diff_staged
    assert "+new" in diff_staged

    # Test querying working tree with no local checkout returns explicit error
    diff_worktree_no_local = await tool_git_diff(
        base="HEAD",
        head="working",
        owner="org",
        repo="repo",
        token="token",
        staged_patches=staged,
    )
    assert "No local repository checkout available in this environment. Use head='staged' to inspect in-memory staged patches." in diff_worktree_no_local

    # Test base == head with staged patches notifies user
    diff_same = await tool_git_diff(
        base="main",
        head="main",
        owner="org",
        repo="repo",
        token="token",
        staged_patches=staged,
    )
    assert "No differences between ref 'main' and 'main'" in diff_same
    assert "Note: 1 uncommitted file(s) are currently staged in the session" in diff_same

    # Test rejection of option injection (e.g. --output=...)
    diff_injection = await tool_git_diff(
        base="--output=/tmp/pwn",
        head="working",
        owner="org",
        repo="repo",
        token="token",
    )
    assert "Error: invalid ref" in diff_injection

    # Test empty staged query returns clean message rather than falling through to worktree
    diff_empty_staged = await tool_git_diff(
        base="HEAD",
        head="staged",
        owner="org",
        repo="repo",
        token="token",
        staged_patches={},
    )
    assert diff_empty_staged == "No staged patches currently in session."


@pytest.mark.asyncio
async def test_tool_git_diff_untracked_symlink_ignored(tmp_path) -> None:
    """Working tree diff ignores untracked symlinks or paths escaping repo root."""
    import subprocess
    from unittest.mock import patch
    from app.services.session_tools.git import tool_git_diff

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    # Simulate git diff returning empty, but status returning an untracked symlink
    mock_diff = subprocess.CompletedProcess(args=["git", "diff"], returncode=0, stdout="", stderr="")
    mock_status = subprocess.CompletedProcess(args=["git", "status"], returncode=0, stdout="?? outside_link\n", stderr="")

    with patch("app.services.session_tools.sandbox.resolve_repo_dir", return_value=(str(repo_dir), None)), \
         patch("subprocess.run", side_effect=[mock_diff, mock_status]):
        res = await tool_git_diff(
            base="HEAD",
            head="working",
            owner="org",
            repo="repo",
            token="token",
            staged_patches={},
        )
    # The file does not exist or is not valid inside repo, so diff returns no uncommitted differences
    assert "outside_link" not in res
    assert "No uncommitted working-tree differences found." in res


# ---------------------------------------------------------------------------
# Issue #66 Comprehensive Tests (Staged Read Tools & Git Diff Scoping)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_read_content_staged_overlay() -> None:
    """resolve_read_content properly overlays staged modifications, creations, and deletions."""
    from app.services.session_tools.recon import resolve_read_content

    base_content = "def foo():\n    return 42\n"
    mod_diff = "--- a/src/foo.py\n+++ b/src/foo.py\n@@ -1,2 +1,2 @@\n def foo():\n-    return 42\n+    return 100\n"
    create_diff = "--- /dev/null\n+++ b/src/bar.py\n@@ -0,0 +1,2 @@\n+def bar():\n+    return 'new'\n"
    del_diff = "--- a/src/baz.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-def baz():\n-    pass\n"

    staged = {
        "src/foo.py": mod_diff,
        "src/bar.py": create_diff,
        "src/baz.py": del_diff,
    }

    with patch("app.services.session_tools.recon.fetch_file_content", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.side_effect = lambda owner, repo, path, sha, token: base_content if path == "src/foo.py" else None

        # 1. Modified file reflects patched content
        res_mod = await resolve_read_content(
            path="src/foo.py",
            repo_owner="org",
            repo_name="repo",
            base_sha="sha1",
            staged_patches=staged,
        )
        assert res_mod == "def foo():\n    return 100\n"

        # 2. Created file returns full created content
        res_create = await resolve_read_content(
            path="src/bar.py",
            repo_owner="org",
            repo_name="repo",
            base_sha="sha1",
            staged_patches=staged,
        )
        assert res_create == "def bar():\n    return 'new'"

        # 3. Deleted file returns None
        res_del = await resolve_read_content(
            path="src/baz.py",
            repo_owner="org",
            repo_name="repo",
            base_sha="sha1",
            staged_patches=staged,
        )
        assert res_del is None


@pytest.mark.asyncio
async def test_read_file_and_slice_consistency() -> None:
    """_tool_read_file and tool_read_file_slice return identical content on staged edits."""
    from app.services.session_orchestrator import SessionOrchestrator
    from app.services.session_tools.recon import tool_read_file_slice

    orchestrator = SessionOrchestrator(session_id=None, db=AsyncMock(), gh_token="dummy-token")
    base_file = "line1\nline2\nline3\nline4\n"
    diff = "--- a/app/main.py\n+++ b/app/main.py\n@@ -2,2 +2,2 @@\n-line2\n-line3\n+line2_updated\n+line3_updated\n"
    staged = {"app/main.py": diff}

    with patch("app.services.session_tools.recon.fetch_file_content", new_callable=AsyncMock, return_value=base_file):
        full_read = await orchestrator._tool_read_file(
            args={"path": "app/main.py"},
            repo_owner="org",
            repo_name="repo",
            base_sha="sha1",
            staged_patches=staged,
        )
        slice_read = await tool_read_file_slice(
            path="app/main.py",
            start_line=1,
            end_line=4,
            owner="org",
            repo="repo",
            base_sha="sha1",
            staged_patches=staged,
        )

        expected_lines = ["line1", "line2_updated", "line3_updated", "line4"]
        assert full_read.splitlines() == expected_lines
        assert [line.split(": ", 1)[1] for line in slice_read.splitlines()] == expected_lines


@pytest.mark.asyncio
async def test_recon_tools_reflect_staged_overlay() -> None:
    """glob_files, grep_search, and list_directory reflect staged creations, modifications, deletions."""
    mock_tree = {
        "tree": [
            {"path": "src/existing.py", "type": "blob"},
            {"path": "src/deleted.py", "type": "blob"},
        ]
    }
    staged = {
        "src/existing.py": "--- a/src/existing.py\n+++ b/src/existing.py\n@@ -1 +1 @@\n-val = 1\n+val = 999_SPECIAL\n",
        "src/created.py": "--- /dev/null\n+++ b/src/created.py\n@@ -0,0 +1 @@\n+val = 999_SPECIAL\n",
        "src/deleted.py": "--- a/src/deleted.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-val = 999_SPECIAL\n",
    }

    with patch("app.services.session_tools.recon.fetch_git_tree", new_callable=AsyncMock, return_value=mock_tree), \
         patch("app.services.session_tools.recon.fetch_file_content", new_callable=AsyncMock, return_value="val = 1\n"):

        # 1. glob_files includes created, excludes deleted
        globs = await tool_glob_files(
            pattern="src/*.py",
            owner="org",
            repo="repo",
            base_sha="sha1",
            staged_patches=staged,
        )
        assert "src/created.py" in globs
        assert "src/existing.py" in globs
        assert "src/deleted.py" not in globs

        # 2. grep_search finds matches in created and modified, skips deleted
        grep_res = await tool_grep_search(
            query="999_SPECIAL",
            owner="org",
            repo="repo",
            base_sha="sha1",
            staged_patches=staged,
        )
        assert "src/existing.py:1: val = 999_SPECIAL" in grep_res
        assert "src/created.py:1: val = 999_SPECIAL" in grep_res
        assert "src/deleted.py" not in grep_res

        # 3. list_directory includes new paths
        dir_entries = await tool_list_directory(
            path="src",
            depth=1,
            owner="org",
            repo="repo",
            base_sha="sha1",
            staged_patches=staged,
        )
        assert "created.py" in dir_entries
        assert "existing.py" in dir_entries


@pytest.mark.asyncio
async def test_tool_git_diff_path_and_stat_scoping() -> None:
    """tool_git_diff filters by path and outputs git diffstat formatting with stat_only=True."""
    from app.services.session_tools.git import tool_git_diff

    multi_diff = (
        "diff --git a/src/a.py b/src/a.py\n"
        "--- a/src/a.py\n"
        "+++ b/src/a.py\n"
        "@@ -1,2 +1,3 @@\n"
        " a\n"
        "-old_a\n"
        "+new_a\n"
        "+extra_a\n"
        "diff --git a/src/b.py b/src/b.py\n"
        "--- a/src/b.py\n"
        "+++ b/src/b.py\n"
        "@@ -1 +1 @@\n"
        "-old_b\n"
        "+new_b\n"
    )

    with patch("app.services.session_tools.git.fetch_diff", new_callable=AsyncMock, return_value=multi_diff):
        # 1. Path filter returns only targeted file
        diff_a = await tool_git_diff(
            base="main",
            head="feat",
            path="src/a.py",
            owner="org",
            repo="repo",
        )
        assert "src/a.py" in diff_a
        assert "src/b.py" not in diff_a

        # 2. stat_only returns diffstat format
        stat_res = await tool_git_diff(
            base="main",
            head="feat",
            stat_only=True,
            owner="org",
            repo="repo",
        )
        assert "src/a.py | 3 ++-" in stat_res
        assert "src/b.py | 2 +-" in stat_res
        assert "2 files changed, 3 insertions(+), 2 deletions(-)" in stat_res

    # 3. head='staged' with path filter and stat_only
    staged = {
        "src/x.py": "--- a/src/x.py\n+++ b/src/x.py\n@@ -1 +1,2 @@\n-old\n+new\n+added\n",
        "src/y.py": "--- a/src/y.py\n+++ b/src/y.py\n@@ -1 +1 @@\n-y\n+y2\n",
    }
    staged_stat = await tool_git_diff(
        base="HEAD",
        head="staged",
        stat_only=True,
        staged_patches=staged,
    )
    assert "src/x.py | 3 ++-" in staged_stat
    assert "src/y.py | 2 +-" in staged_stat
    assert "2 files changed, 3 insertions(+), 2 deletions(-)" in staged_stat

    staged_scoped = await tool_git_diff(
        base="HEAD",
        head="staged",
        path="src/x.py",
        staged_patches=staged,
    )
    assert "src/x.py" in staged_scoped
    assert "src/y.py" not in staged_scoped


@pytest.mark.asyncio
async def test_list_staged_files_tool_and_prompt_formatting() -> None:
    """_tool_list_staged_files formats statuses and system prompt incorporates staged changes."""
    from app.services.session_orchestrator import SessionOrchestrator, _build_system_prompt

    orchestrator = SessionOrchestrator(session_id=None, db=AsyncMock(), gh_token="dummy-token")
    staged = {
        "src/mod.py": "--- a/src/mod.py\n+++ b/src/mod.py\n@@ -1 +1 @@\n-1\n+2\n",
        "src/new.py": "--- /dev/null\n+++ b/src/new.py\n@@ -0,0 +1 @@\n+created\n",
        "src/del.py": "--- a/src/del.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-deleted\n",
    }

    result = orchestrator._tool_list_staged_files(staged_patches=staged)
    assert "Staged files (3):" in result
    assert "  - src/del.py (deleted)" in result
    assert "  - src/mod.py (modified)" in result
    assert "  - src/new.py (created)" in result

    prompt = _build_system_prompt(
        repo_owner="test-owner",
        repo_name="test-repo",
        branch_name="fix-branch",
        base_sha="12345678abcdef",
        staged_patches=staged,
    )
    assert "list_staged_files" in prompt
    assert "- src/mod.py" in prompt
    assert "- src/new.py" in prompt
    assert "- src/del.py" in prompt




