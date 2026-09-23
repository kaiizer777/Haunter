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
    assert _validate_file_path("backend/app/services/recon.py") == "backend/app/services/recon.py"
    assert _validate_file_path(".") == "."


# ---------------------------------------------------------------------------
# read_file_slice Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_file_slice_indexing_and_bounds() -> None:
    """read_file_slice must return 1-based line-numbered slices."""
    file_content = "line 1\nline 2\nline 3\nline 4\nline 5"

    with patch("app.services.session_tools.recon.fetch_file_content", new_callable=AsyncMock) as mock_fetch:
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

    with patch("app.services.session_tools.recon.fetch_file_content", new_callable=AsyncMock) as mock_fetch:
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
    with patch("app.services.session_tools.recon.fetch_file_content", new_callable=AsyncMock) as mock_fetch:
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

    with patch("app.services.session_tools.recon.fetch_git_tree", new_callable=AsyncMock) as mock_tree_fetch:
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
        assert comp_matches == ["src/components/button.tsx", "src/components/ui/input.tsx"]

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
        patch("app.services.session_tools.recon.fetch_git_tree", new_callable=AsyncMock) as mock_tree_fetch,
        patch("app.services.session_tools.recon.fetch_file_content", side_effect=_mock_content),
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

    with patch("app.services.session_tools.recon.fetch_git_tree", new_callable=AsyncMock) as mock_tree_fetch:
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
    with patch("app.services.session_orchestrator.tool_read_file_slice", new_callable=AsyncMock) as m:
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
            token="dummy-token",
        )

    # 2. grep_search dispatch
    with patch("app.services.session_orchestrator.tool_grep_search", new_callable=AsyncMock) as m:
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
            token="dummy-token",
        )

    # 3. glob_files dispatch
    with patch("app.services.session_orchestrator.tool_glob_files", new_callable=AsyncMock) as m:
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

    # 4. list_directory dispatch
    with patch("app.services.session_orchestrator.tool_list_directory", new_callable=AsyncMock) as m:
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

    # 5. Error containment (e.g. ValueError or GitHubClientError returns error string)
    with patch("app.services.session_orchestrator.tool_read_file_slice", side_effect=ValueError("Invalid range")):
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
