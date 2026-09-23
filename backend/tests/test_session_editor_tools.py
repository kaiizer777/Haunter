"""
Unit tests for Haunter Session Editor Tools (Phase 2).

Covers:
  1. test_str_replace_success            — unique match, valid diff, staged_patches updated.
  2. test_str_replace_not_found          — descriptive error when old_str absent.
  3. test_str_replace_duplicate_occurrence — ambiguity error when old_str appears >1.
  4. test_create_file                    — /dev/null to b/path diff, staged_patches updated.
  5. test_delete_file                    — a/path to /dev/null diff, staged_patches updated.
  6. test_apply_multi_patch_atomic_success — 2+ files, all committed atomically.
  7. test_apply_multi_patch_atomic_rollback — second edit fails; first NOT staged.
  8. test_path_traversal_blocked         — ../../etc/shadow rejected.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.services.session_streamer import SseQueue
from app.services.session_tools.editor import (
    tool_apply_multi_patch,
    tool_create_file,
    tool_delete_file,
    tool_str_replace,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_queue() -> SseQueue:
    """Return a SseQueue with a large enough internal buffer for tests."""
    return SseQueue(maxsize=256)


def _gh_patch(return_value: str | None):
    """Patch fetch_file_content in editor module."""
    return patch(
        "app.services.session_tools.editor.fetch_file_content",
        new_callable=AsyncMock,
        return_value=return_value,
    )


# ---------------------------------------------------------------------------
# 1. str_replace — success
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_str_replace_success() -> None:
    """Replaces a unique block, generates a valid unified diff, updates staged_patches."""
    original = "def foo():\n    return 1\n\ndef bar():\n    pass\n"
    old_str = "def foo():\n    return 1"
    new_str = "def foo():\n    return 42"

    staged: dict[str, str] = {}
    queue = _make_queue()

    with _gh_patch(original):
        result = await tool_str_replace(
            path="src/main.py",
            old_str=old_str,
            new_str=new_str,
            repo_owner="org",
            repo_name="repo",
            base_sha="abc123",
            staged_patches=staged,
            queue=queue,
        )

    assert result == "Successfully replaced code in 'src/main.py'."
    assert "src/main.py" in staged

    diff = staged["src/main.py"]
    # Unified diff must reference the correct file paths.
    assert "a/src/main.py" in diff
    assert "b/src/main.py" in diff
    # Diff must contain the removed and added lines.
    assert "-    return 1" in diff
    assert "+    return 42" in diff

    # SSE event must have been emitted.
    event_chunk = await asyncio.wait_for(queue._q.get(), timeout=1.0)
    assert "file_diff" in event_chunk
    assert "src/main.py" in event_chunk


# ---------------------------------------------------------------------------
# 2. str_replace — not found
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_str_replace_not_found() -> None:
    """Returns descriptive error when old_str doesn't exist in the file."""
    original = "def foo():\n    return 1\n"

    staged: dict[str, str] = {}
    queue = _make_queue()

    with _gh_patch(original):
        result = await tool_str_replace(
            path="src/main.py",
            old_str="def nonexistent():",
            new_str="def replaced():",
            repo_owner="org",
            repo_name="repo",
            base_sha="abc123",
            staged_patches=staged,
            queue=queue,
        )

    assert result.startswith("Error: old_str not found in 'src/main.py'")
    # staged_patches must remain untouched.
    assert staged == {}


# ---------------------------------------------------------------------------
# 3. str_replace — duplicate occurrence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_str_replace_duplicate_occurrence() -> None:
    """Returns ambiguity error when old_str appears more than once."""
    original = "pass\n# do stuff\npass\n# do more stuff\npass\n"

    staged: dict[str, str] = {}
    queue = _make_queue()

    with _gh_patch(original):
        result = await tool_str_replace(
            path="src/utils.py",
            old_str="pass",
            new_str="raise NotImplementedError",
            repo_owner="org",
            repo_name="repo",
            base_sha="abc123",
            staged_patches=staged,
            queue=queue,
        )

    assert "found 3 times" in result
    assert "Provide more surrounding lines" in result
    assert staged == {}


# ---------------------------------------------------------------------------
# 4. create_file
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_file() -> None:
    """Creates a new file staged as a /dev/null to b/path unified diff."""
    content = "# new module\n\ndef hello():\n    return 'world'\n"

    staged: dict[str, str] = {}
    queue = _make_queue()

    result = await tool_create_file(
        path="src/new_module.py",
        content=content,
        staged_patches=staged,
        queue=queue,
    )

    assert result == "Successfully staged new file 'src/new_module.py'."
    assert "src/new_module.py" in staged

    diff = staged["src/new_module.py"]
    assert "/dev/null" in diff
    assert "b/src/new_module.py" in diff
    # All content lines should appear as additions.
    assert "+# new module" in diff
    assert "+def hello():" in diff

    # SSE event.
    event_chunk = await asyncio.wait_for(queue._q.get(), timeout=1.0)
    assert "file_diff" in event_chunk
    assert "create" in event_chunk


# ---------------------------------------------------------------------------
# 5. delete_file
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_file() -> None:
    """Stages deletion with an a/path to /dev/null unified diff."""
    original = "def to_remove():\n    pass\n"

    staged: dict[str, str] = {}
    queue = _make_queue()

    with _gh_patch(original):
        result = await tool_delete_file(
            path="src/old.py",
            repo_owner="org",
            repo_name="repo",
            base_sha="abc123",
            staged_patches=staged,
            queue=queue,
        )

    assert result == "Successfully staged deletion of 'src/old.py'."
    assert "src/old.py" in staged

    diff = staged["src/old.py"]
    assert "a/src/old.py" in diff
    assert "/dev/null" in diff
    # Deleted lines show as removals.
    assert "-def to_remove():" in diff

    # SSE event.
    event_chunk = await asyncio.wait_for(queue._q.get(), timeout=1.0)
    assert "file_diff" in event_chunk
    assert "delete" in event_chunk


# ---------------------------------------------------------------------------
# 6. apply_multi_patch — atomic success
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_apply_multi_patch_atomic_success() -> None:
    """Applies edits to 2 files atomically; both are staged and events emitted."""
    file_a = "x = 1\ny = 2\n"
    file_b = "alpha = 'a'\nbeta = 'b'\n"

    staged: dict[str, str] = {}
    queue = _make_queue()

    patches_input = [
        {"type": "str_replace", "path": "mod_a.py", "old_str": "x = 1", "new_str": "x = 99"},
        {"type": "str_replace", "path": "mod_b.py", "old_str": "alpha = 'a'", "new_str": "alpha = 'z'"},
    ]

    async def fake_fetch(owner, repo, path, sha, token=None):
        if "mod_a" in path:
            return file_a
        return file_b

    with patch(
        "app.services.session_tools.editor.fetch_file_content",
        new_callable=AsyncMock,
        side_effect=fake_fetch,
    ):
        result = await tool_apply_multi_patch(
            patches=patches_input,
            repo_owner="org",
            repo_name="repo",
            base_sha="abc123",
            staged_patches=staged,
            queue=queue,
        )

    assert result == "Successfully applied 2 file edits atomically."
    assert "mod_a.py" in staged
    assert "mod_b.py" in staged

    assert "-x = 1" in staged["mod_a.py"]
    assert "+x = 99" in staged["mod_a.py"]
    assert "-alpha = 'a'" in staged["mod_b.py"]
    assert "+alpha = 'z'" in staged["mod_b.py"]

    # Two SSE events should have been emitted.
    ev1 = await asyncio.wait_for(queue._q.get(), timeout=1.0)
    ev2 = await asyncio.wait_for(queue._q.get(), timeout=1.0)
    assert "file_diff" in ev1
    assert "file_diff" in ev2


# ---------------------------------------------------------------------------
# 7. apply_multi_patch — atomic rollback on second failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_apply_multi_patch_atomic_rollback() -> None:
    """Second edit fails; verifies that the first edit was NOT staged (full rollback)."""
    file_a = "x = 1\ny = 2\n"

    staged: dict[str, str] = {}
    queue = _make_queue()

    patches_input = [
        # First op is valid.
        {"type": "str_replace", "path": "mod_a.py", "old_str": "x = 1", "new_str": "x = 99"},
        # Second op targets a non-existent old_str — must fail.
        {"type": "str_replace", "path": "mod_a.py", "old_str": "DOES_NOT_EXIST", "new_str": "whatever"},
    ]

    with _gh_patch(file_a):
        result = await tool_apply_multi_patch(
            patches=patches_input,
            repo_owner="org",
            repo_name="repo",
            base_sha="abc123",
            staged_patches=staged,
            queue=queue,
        )

    # Must return an error about the second patch.
    assert "Error" in result
    assert "patch[1]" in result

    # staged_patches must be completely untouched — no partial writes.
    assert staged == {}

    # No SSE events should have been emitted (nothing committed).
    assert queue._q.empty()


# ---------------------------------------------------------------------------
# 8. Path traversal blocked
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_path_traversal_blocked() -> None:
    """../../etc/shadow is rejected across all editor tools."""
    staged: dict[str, str] = {}
    queue = _make_queue()

    # str_replace
    result = await tool_str_replace(
        path="../../etc/shadow",
        old_str="root",
        new_str="hacked",
        repo_owner="org",
        repo_name="repo",
        base_sha="abc",
        staged_patches=staged,
        queue=queue,
    )
    assert "Error" in result
    assert "traversal" in result.lower() or "rejected" in result.lower()

    # create_file
    result = await tool_create_file(
        path="../../etc/shadow",
        content="malicious",
        staged_patches=staged,
        queue=queue,
    )
    assert "Error" in result

    # delete_file
    result = await tool_delete_file(
        path="../../etc/shadow",
        repo_owner="org",
        repo_name="repo",
        base_sha="abc",
        staged_patches=staged,
        queue=queue,
    )
    assert "Error" in result

    # apply_multi_patch with traversal path
    result = await tool_apply_multi_patch(
        patches=[{"type": "create_file", "path": "../../etc/shadow", "content": "bad"}],
        repo_owner="org",
        repo_name="repo",
        base_sha="abc",
        staged_patches=staged,
        queue=queue,
    )
    assert "Error" in result

    # All checks — nothing should have been staged.
    assert staged == {}
