"""
Unit tests for Haunter Session Editor Tools (Phase 2).

Covers:
  1. test_str_replace_success                  — unique match, valid diff, staged_patches updated.
  2. test_str_replace_not_found                — descriptive error when old_str absent.
  3. test_str_replace_duplicate_occurrence     — ambiguity error when old_str appears >1.
  4. test_create_file                          — /dev/null to b/path diff, staged_patches updated.
  5. test_delete_file                          — a/path to /dev/null diff, staged_patches updated.
  6. test_apply_multi_patch_atomic_success     — 2+ files, all committed atomically.
  7. test_apply_multi_patch_atomic_rollback    — second edit fails; first NOT staged.
  8. test_path_traversal_blocked               — ../../etc/shadow rejected.
  9. test_apply_multi_patch_sequential_same_file — two sequential edits on same file produce a
                                                   correct base->final diff that applies cleanly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
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
        {
            "type": "str_replace",
            "path": "mod_a.py",
            "old_str": "x = 1",
            "new_str": "x = 99",
        },
        {
            "type": "str_replace",
            "path": "mod_b.py",
            "old_str": "alpha = 'a'",
            "new_str": "alpha = 'z'",
        },
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
        {
            "type": "str_replace",
            "path": "mod_a.py",
            "old_str": "x = 1",
            "new_str": "x = 99",
        },
        # Second op targets a non-existent old_str — must fail.
        {
            "type": "str_replace",
            "path": "mod_a.py",
            "old_str": "DOES_NOT_EXIST",
            "new_str": "whatever",
        },
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


# ---------------------------------------------------------------------------
# 9. apply_multi_patch — sequential edits on the same file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_multi_patch_sequential_same_file() -> None:
    """
    Two sequential str_replace ops on the same file produce a base->final unified diff
    that contains both changes and applies cleanly against the original file at commit time.

    Regression test for the _apply_staged_diff / difflib.restore() bug where the second
    edit would fail because _resolve_current_content fell back to base content instead of
    the post-first-edit content.
    """
    from app.services.patch_applier import apply_unified_diff

    original = "x = 1\ny = 2\nz = 3\n"
    staged: dict[str, str] = {}
    queue = _make_queue()

    patches_input = [
        {
            "type": "str_replace",
            "path": "mod.py",
            "old_str": "x = 1",
            "new_str": "x = 99",
        },
        # Second edit on the same file — requires seeing the post-first-edit state.
        {
            "type": "str_replace",
            "path": "mod.py",
            "old_str": "y = 2",
            "new_str": "y = 200",
        },
    ]

    with _gh_patch(original):
        result = await tool_apply_multi_patch(
            patches=patches_input,
            repo_owner="org",
            repo_name="repo",
            base_sha="abc123",
            staged_patches=staged,
            queue=queue,
        )

    assert result == "Successfully applied 2 file edits atomically.", result
    assert "mod.py" in staged

    diff = staged["mod.py"]
    # The committed diff must encode BOTH changes relative to the original base.
    assert "-x = 1" in diff
    assert "+x = 99" in diff
    assert "-y = 2" in diff
    assert "+y = 200" in diff

    # The diff must apply cleanly against the ORIGINAL file (simulating commit-time behaviour).
    patched = apply_unified_diff(original, diff)
    assert "x = 99" in patched
    assert "y = 200" in patched
    assert "z = 3" in patched


@pytest.mark.asyncio
async def test_editor_tools_sync_to_local_disk(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When a local checkout is found, editor tools read and write directly to disk."""
    from pathlib import Path
    from app.services.session_tools.editor import (
        tool_create_file,
        tool_delete_file,
        tool_str_replace,
    )

    owner_dir = tmp_path / "test"
    owner_dir.mkdir()
    repo_dir = owner_dir / "LocalRepo"
    repo_dir.mkdir()
    monkeypatch.setenv("LOCAL_REPOS_DIR", str(tmp_path))

    src_dir = repo_dir / "src"
    src_dir.mkdir()
    main_file = src_dir / "main.py"
    main_file.write_text("def test():\n    return 10\n", encoding="utf-8")

    staged: dict[str, str] = {}
    queue = _make_queue()

    # 1. str_replace reads from disk and writes updated code to disk
    res = await tool_str_replace(
        path="src/main.py",
        old_str="return 10",
        new_str="return 42",
        repo_owner="test",
        repo_name="LocalRepo",
        base_sha="any",
        staged_patches=staged,
        queue=queue,
    )
    assert "Successfully replaced code" in res
    assert "return 42" in main_file.read_text(encoding="utf-8")
    assert "src/main.py" in staged

    # 2. create_file creates the file on disk
    res_create = await tool_create_file(
        path="src/helper.py",
        content="HELPER = True\n",
        staged_patches=staged,
        queue=queue,
        repo_owner="test",
        repo_name="LocalRepo",
    )
    assert "Successfully staged new file" in res_create
    helper_file = src_dir / "helper.py"
    assert helper_file.is_file()
    assert helper_file.read_text(encoding="utf-8") == "HELPER = True\n"

    # 3. delete_file deletes the file on disk
    res_del = await tool_delete_file(
        path="src/helper.py",
        repo_owner="test",
        repo_name="LocalRepo",
        base_sha="any",
        staged_patches=staged,
        queue=queue,
    )
    assert "Removed newly-created file" in res_del
    assert "src/helper.py" not in staged
    assert not helper_file.exists()


def test_apply_staged_diff_preserves_full_file() -> None:
    """Verifies that _apply_staged_diff preserves all context lines outside hunks without truncation."""
    from app.services.session_tools.editor import _apply_staged_diff

    long_file = "\n".join(f"line_{i}" for i in range(1, 100)) + "\n"
    diff = (
        "--- a/file.py\n"
        "+++ b/file.py\n"
        "@@ -50,3 +50,3 @@\n"
        " line_50\n"
        "-line_51\n"
        "+line_51_modified\n"
        " line_52\n"
    )
    result = _apply_staged_diff(long_file, diff)
    assert "line_1" in result
    assert "line_51_modified" in result
    assert "line_99" in result
    assert len(result.splitlines()) == 99


@pytest.mark.asyncio
async def test_resolve_current_content_deleted_staged() -> None:
    """Verifies that _resolve_current_content returns None for files deleted in staged patches."""
    from app.services.session_tools.editor import _resolve_current_content

    staged = {
        "deleted.py": "--- a/deleted.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-some code\n"
    }
    content = await _resolve_current_content(
        path="deleted.py",
        staged_patches=staged,
        repo_owner="test",
        repo_name="LocalRepo",
        base_sha="any",
        gh_token=None,
    )
    assert content is None


@pytest.mark.asyncio
async def test_resolve_current_content_unsynced_disk_preserves_staged_patch(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When disk file is stale (sync failed or missed) but a clean base is
    available, _resolve_current_content still returns staged content."""
    from app.services.session_tools.editor import _resolve_current_content

    owner_dir = tmp_path / "test"
    owner_dir.mkdir()
    repo_dir = owner_dir / "LocalRepo"
    repo_dir.mkdir()
    monkeypatch.setenv("LOCAL_REPOS_DIR", str(tmp_path))

    test_file = repo_dir / "mod.py"
    # Unpatched content on disk
    test_file.write_text("x = 1\ny = 2\n", encoding="utf-8")

    staged = {
        "mod.py": "--- a/mod.py\n+++ b/mod.py\n@@ -1,2 +1,2 @@\n-x = 1\n+x = 99\n y = 2\n"
    }

    # Clean base available via remote fetch (no git repo in tmp dir).
    with _gh_patch("x = 1\ny = 2\n"):
        content = await _resolve_current_content(
            path="mod.py",
            staged_patches=staged,
            repo_owner="test",
            repo_name="LocalRepo",
            base_sha="any",
            gh_token=None,
        )
    assert content is not None
    assert "x = 99" in content
    assert "x = 1" not in content


@pytest.mark.asyncio
async def test_resolve_current_content_no_clean_base_returns_disk_as_is(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a clean base, disk is the only ground truth: the staged diff is
    NOT reapplied on top of disk content (prevents double diff application)."""
    from app.services.session_tools.editor import _resolve_current_content

    owner_dir = tmp_path / "test"
    owner_dir.mkdir()
    repo_dir = owner_dir / "LocalRepo"
    repo_dir.mkdir()
    monkeypatch.setenv("LOCAL_REPOS_DIR", str(tmp_path))

    disk_content = "x = 99\ny = 2\n"
    test_file = repo_dir / "mod.py"
    test_file.write_text(disk_content, encoding="utf-8")

    staged = {
        "mod.py": "--- a/mod.py\n+++ b/mod.py\n@@ -1,2 +1,2 @@\n-x = 1\n+x = 99\n y = 2\n"
    }

    async def _no_base(*args, **kwargs):
        return None

    with (
        patch(
            "app.services.session_tools.editor._get_clean_base",
            side_effect=_no_base,
        ),
        patch(
            "app.services.session_tools.editor.fetch_file_content",
            new_callable=AsyncMock,
            side_effect=Exception("no remote"),
        ),
    ):
        content = await _resolve_current_content(
            path="mod.py",
            staged_patches=staged,
            repo_owner="test",
            repo_name="LocalRepo",
            base_sha="any",
            gh_token=None,
        )
    assert content == disk_content


@pytest.mark.asyncio
async def test_resolve_current_content_unstaged_preserves_terminal_edits(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unstaged file resolves to its on-disk content even when a clean base
    is available — terminal and editor share the local checkout, so returning
    the base revision here would silently discard terminal modifications."""
    from app.services.session_tools.editor import _resolve_current_content

    owner_dir = tmp_path / "test"
    owner_dir.mkdir()
    repo_dir = owner_dir / "LocalRepo"
    repo_dir.mkdir()
    monkeypatch.setenv("LOCAL_REPOS_DIR", str(tmp_path))

    terminal_edited = "x = 1\ny = 2\n# terminal edit\n"
    clean = "x = 1\ny = 2\n"
    (repo_dir / "mod.py").write_text(terminal_edited, encoding="utf-8")

    with patch(
        "app.services.session_tools.editor._get_clean_base",
        new_callable=AsyncMock,
        return_value=clean,
    ):
        content = await _resolve_current_content(
            path="mod.py",
            staged_patches={},
            repo_owner="test",
            repo_name="LocalRepo",
            base_sha="abc123",
            gh_token=None,
        )
    assert content == terminal_edited


@pytest.mark.asyncio
async def test_resolve_current_content_unstaged_no_disk_falls_back_to_clean_base() -> None:
    """An unstaged file with no local checkout entry resolves via the clean base."""
    from app.services.session_tools.editor import _resolve_current_content

    clean = "x = 1\ny = 2\n"
    with patch(
        "app.services.session_tools.editor._get_clean_base",
        new_callable=AsyncMock,
        return_value=clean,
    ):
        content = await _resolve_current_content(
            path="mod.py",
            staged_patches={},
            repo_owner="",
            repo_name="",
            base_sha="abc123",
            gh_token=None,
        )
    assert content == clean


@pytest.mark.asyncio
async def test_str_replace_staged_file_fails_closed_without_clean_base(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second edit on an already-staged file fails closed when the base
    revision is unresolvable, instead of diffing against the intermediate
    buffer and dropping the first edit."""
    owner_dir = tmp_path / "test"
    owner_dir.mkdir()
    repo_dir = owner_dir / "LocalRepo"
    repo_dir.mkdir()
    monkeypatch.setenv("LOCAL_REPOS_DIR", str(tmp_path))

    (repo_dir / "mod.py").write_text("x = 99\ny = 2\n", encoding="utf-8")
    staged = {
        "mod.py": "--- a/mod.py\n+++ b/mod.py\n@@ -1,2 +1,2 @@\n-x = 1\n+x = 99\n y = 2\n"
    }
    queue = _make_queue()

    async def _no_base(*args, **kwargs):
        return None

    with patch(
        "app.services.session_tools.editor._get_clean_base",
        side_effect=_no_base,
    ):
        result = await tool_str_replace(
            path="mod.py",
            old_str="y = 2",
            new_str="y = 200",
            repo_owner="test",
            repo_name="LocalRepo",
            base_sha="abc123",
            staged_patches=staged,
            queue=queue,
        )

    assert "Cannot resolve base revision" in result
    assert "Staged patch preserved" in result
    # First edit's staged patch untouched; nothing emitted.
    assert staged["mod.py"].startswith("--- a/mod.py")
    assert "+x = 99" in staged["mod.py"]
    assert queue._q.empty()


@pytest.mark.asyncio
async def test_apply_multi_patch_staged_file_fails_closed_without_clean_base() -> None:
    """A multi-patch batch touching an already-staged file fails atomically
    when the base revision is unresolvable — staged_patches left untouched."""
    staged = {
        "mod.py": "--- a/mod.py\n+++ b/mod.py\n@@ -1,2 +1,2 @@\n-x = 1\n+x = 99\n y = 2\n"
    }
    queue = _make_queue()

    async def _no_base(*args, **kwargs):
        return None

    async def _resolve_current(
        path,
        staged_patches,
        repo_owner,
        repo_name,
        base_sha,
        gh_token=None,
        session_id=None,
        **kwargs,
    ):
        return "x = 99\ny = 2\n"

    with patch(
        "app.services.session_tools.editor._get_clean_base",
        side_effect=_no_base,
    ), patch(
        "app.services.session_tools.editor._resolve_current_content",
        side_effect=_resolve_current,
    ):
        result = await tool_apply_multi_patch(
            patches=[
                {
                    "type": "str_replace",
                    "path": "mod.py",
                    "old_str": "y = 2",
                    "new_str": "y = 200",
                }
            ],
            repo_owner="org",
            repo_name="repo",
            base_sha="abc123",
            staged_patches=staged,
            queue=queue,
        )

    assert "Cannot resolve base revision" in result
    assert "Staged patch preserved" in result
    assert "+x = 99" in staged["mod.py"]
    assert "y = 200" not in staged["mod.py"]
    assert queue._q.empty()


@pytest.mark.asyncio
async def test_resolve_current_content_created_staged_preserves_content() -> None:
    """A created file staged with /dev/null returns its content even without local disk file."""
    from app.services.session_tools.editor import _resolve_current_content

    staged = {
        "new_file.py": "--- /dev/null\n+++ b/new_file.py\n@@ -0,0 +1,2 @@\n+print('hello')\n+print('world')\n"
    }
    content = await _resolve_current_content(
        path="new_file.py",
        staged_patches=staged,
        repo_owner="test",
        repo_name="LocalRepo",
        base_sha="any",
        gh_token=None,
    )
    assert content is not None
    assert "print('hello')" in content
    assert "print('world')" in content


@pytest.mark.asyncio
async def test_sequential_str_replace_preserves_earlier_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    2+ sequential tool_str_replace calls on the same file verify that staged_patches[path]
    encodes both changes as a cumulative diff and syncing to disk preserves both changes.
    """
    from app.services.session_tools.sandbox import sync_staged_patches_to_repo

    owner_dir = tmp_path / "owner"
    owner_dir.mkdir()
    repo_dir = owner_dir / "repo"
    repo_dir.mkdir()
    monkeypatch.setenv("LOCAL_REPOS_DIR", str(tmp_path))

    original = "def foo():\n    return 1\n\ndef bar():\n    pass\n"
    test_file = repo_dir / "main.py"
    test_file.write_text(original, encoding="utf-8")

    staged: dict[str, str] = {}
    queue = _make_queue()

    with _gh_patch(original):
        # Edit 1: return 1 -> return 42
        res1 = await tool_str_replace(
            path="main.py",
            old_str="return 1",
            new_str="return 42",
            repo_owner="owner",
            repo_name="repo",
            base_sha="sha123",
            staged_patches=staged,
            queue=queue,
        )
        assert "Successfully replaced" in res1

        # Edit 2: pass -> return 'bar'
        res2 = await tool_str_replace(
            path="main.py",
            old_str="pass",
            new_str="return 'bar'",
            repo_owner="owner",
            repo_name="repo",
            base_sha="sha123",
            staged_patches=staged,
            queue=queue,
        )
        assert "Successfully replaced" in res2

    diff = staged["main.py"]
    # Cumulative diff must encode both edits
    assert "-    return 1" in diff
    assert "+    return 42" in diff
    assert "-    pass" in diff
    assert "+    return 'bar'" in diff

    # Working tree on disk has both changes
    disk_content = test_file.read_text(encoding="utf-8")
    assert "return 42" in disk_content
    assert "return 'bar'" in disk_content
    assert "return 1" not in disk_content
    assert "pass" not in disk_content

    # Sync to repo preserves both changes without erasing earlier edits
    sync_staged_patches_to_repo(str(repo_dir), staged)
    post_sync = test_file.read_text(encoding="utf-8")
    assert post_sync == disk_content


def test_is_diff_applied_no_false_positive_with_repeated_lines() -> None:
    """
    Files with repeated lines (e.g. multiple pass statements) must never falsely mark
    an unapplied diff as applied.
    """
    from app.services.session_tools.editor import _is_diff_applied

    base = "def a():\n    pass\n\ndef b():\n    pass\n\ndef c():\n    pass\n"
    diff_add_pass = (
        "--- a/sample.py\n"
        "+++ b/sample.py\n"
        "@@ -6,3 +6,6 @@\n"
        " def c():\n"
        "     pass\n"
        "+\n"
        "+def d():\n"
        "+    pass\n"
    )

    # In base content, 'pass' is already present, but def d() does NOT exist
    assert _is_diff_applied(base, diff_add_pass) is False
    assert _is_diff_applied(base, diff_add_pass, base_content=base) is False

    # When diff is actually applied:
    applied_content = base + "\ndef d():\n    pass\n"
    assert _is_diff_applied(applied_content, diff_add_pass) is True
    assert _is_diff_applied(applied_content, diff_add_pass, base_content=base) is True


@pytest.mark.asyncio
async def test_resolve_current_content_created_file_reflects_terminal_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    When a staged created file is subsequently edited via terminal on disk,
    _resolve_current_content reflects the terminal modifications and updates staged_patches.
    """
    from app.services.session_tools.editor import _resolve_current_content

    owner_dir = tmp_path / "owner"
    owner_dir.mkdir()
    repo_dir = owner_dir / "repo"
    repo_dir.mkdir()
    monkeypatch.setenv("LOCAL_REPOS_DIR", str(tmp_path))

    created_file = repo_dir / "service.py"
    initial_content = "def start():\n    pass\n"
    created_file.write_text(initial_content, encoding="utf-8")

    staged = {
        "service.py": "--- /dev/null\n+++ b/service.py\n@@ -0,0 +1,2 @@\n+def start():\n+    pass\n"
    }

    # Simulate terminal command modifying the created file
    terminal_edited = "def start():\n    pass\n\ndef stop():\n    pass\n"
    created_file.write_text(terminal_edited, encoding="utf-8")

    content = await _resolve_current_content(
        path="service.py",
        staged_patches=staged,
        repo_owner="owner",
        repo_name="repo",
        base_sha="any",
        gh_token=None,
    )

    assert content == terminal_edited
    assert "def stop():" in staged["service.py"]


@pytest.mark.asyncio
async def test_sequential_str_replace_empty_base_retains_all_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A file that is empty (0 bytes) at session base accumulates sequential edits
    into one cumulative diff. clean_base == "" is falsy and must be
    distinguished from None via `is not None` — otherwise the second edit diffs
    against the already-edited buffer and drops the first edit.
    """
    from app.sandbox.mirror import apply_unified_diff

    owner_dir = tmp_path / "owner"
    owner_dir.mkdir()
    repo_dir = owner_dir / "repo"
    repo_dir.mkdir()
    monkeypatch.setenv("LOCAL_REPOS_DIR", str(tmp_path))

    # Terminal writes content into the base-empty file (still unstaged).
    test_file = repo_dir / "empty.py"
    test_file.write_text("x = 1\ny = 2\n", encoding="utf-8")

    staged: dict[str, str] = {}
    queue = _make_queue()

    async def _empty_base(*args, **kwargs):
        return ""

    with patch(
        "app.services.session_tools.editor._get_clean_base",
        side_effect=_empty_base,
    ):
        res1 = await tool_str_replace(
            path="empty.py",
            old_str="x = 1",
            new_str="x = 99",
            repo_owner="owner",
            repo_name="repo",
            base_sha="sha123",
            staged_patches=staged,
            queue=queue,
        )
        assert "Successfully replaced" in res1

        res2 = await tool_str_replace(
            path="empty.py",
            old_str="y = 2",
            new_str="y = 200",
            repo_owner="owner",
            repo_name="repo",
            base_sha="sha123",
            staged_patches=staged,
            queue=queue,
        )
        assert "Successfully replaced" in res2

    diff = staged["empty.py"]
    # The cumulative diff from the empty base must encode BOTH additions.
    assert "+x = 99" in diff
    assert "+y = 200" in diff

    # The diff applies cleanly against the empty base (commit-time behaviour).
    assert apply_unified_diff("", diff).splitlines() == ["x = 99", "y = 200"]


@pytest.mark.asyncio
async def test_apply_multi_patch_empty_base_retains_all_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batch of sequential edits on a base-empty file produces a cumulative
    base->final diff retaining every change (explicit `is not None` handling)."""
    from app.sandbox.mirror import apply_unified_diff

    owner_dir = tmp_path / "owner"
    owner_dir.mkdir()
    repo_dir = owner_dir / "repo"
    repo_dir.mkdir()
    monkeypatch.setenv("LOCAL_REPOS_DIR", str(tmp_path))

    test_file = repo_dir / "empty.py"
    test_file.write_text("x = 1\ny = 2\n", encoding="utf-8")

    staged: dict[str, str] = {}
    queue = _make_queue()

    async def _empty_base(*args, **kwargs):
        return ""

    with patch(
        "app.services.session_tools.editor._get_clean_base",
        side_effect=_empty_base,
    ):
        result = await tool_apply_multi_patch(
            patches=[
                {
                    "type": "str_replace",
                    "path": "empty.py",
                    "old_str": "x = 1",
                    "new_str": "x = 99",
                },
                {
                    "type": "str_replace",
                    "path": "empty.py",
                    "old_str": "y = 2",
                    "new_str": "y = 200",
                },
            ],
            repo_owner="owner",
            repo_name="repo",
            base_sha="sha123",
            staged_patches=staged,
            queue=queue,
        )

    assert result == "Successfully applied 2 file edits atomically.", result
    diff = staged["empty.py"]
    assert "+x = 99" in diff
    assert "+y = 200" in diff
    assert apply_unified_diff("", diff).splitlines() == ["x = 99", "y = 200"]


@pytest.mark.asyncio
async def test_str_replace_terminal_created_file_generates_creation_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file created via terminal (absent at base_sha) gets a /dev/null
    creation diff, and subsequent edits rebuild cumulatively against /dev/null."""
    import subprocess

    owner_dir = tmp_path / "owner"
    owner_dir.mkdir()
    repo_dir = owner_dir / "repo"
    repo_dir.mkdir()
    monkeypatch.setenv("LOCAL_REPOS_DIR", str(tmp_path))

    (repo_dir / "keep.py").write_text("x = 1\n", encoding="utf-8")
    for cmd in (
        ["git", "init"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test User"],
        ["git", "add", "."],
        ["git", "commit", "-m", "init"],
    ):
        subprocess.run(cmd, cwd=str(repo_dir), check=True, capture_output=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_dir),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    terminal_content = "a = 1\nb = 2\n"
    (repo_dir / "new_term.py").write_text(terminal_content, encoding="utf-8")

    staged: dict[str, str] = {}
    queue = _make_queue()

    with patch(
        "app.services.session_tools.editor.fetch_file_content",
        new_callable=AsyncMock,
        return_value=None,
    ):
        res1 = await tool_str_replace(
            path="new_term.py",
            old_str="a = 1",
            new_str="a = 99",
            repo_owner="owner",
            repo_name="repo",
            base_sha=sha,
            staged_patches=staged,
            queue=queue,
        )
    assert "Successfully replaced" in res1
    diff1 = staged["new_term.py"]
    assert "--- /dev/null" in diff1
    assert "+++ b/new_term.py" in diff1

    with patch(
        "app.services.session_tools.editor.fetch_file_content",
        new_callable=AsyncMock,
        return_value=None,
    ):
        res2 = await tool_str_replace(
            path="new_term.py",
            old_str="b = 2",
            new_str="b = 200",
            repo_owner="owner",
            repo_name="repo",
            base_sha=sha,
            staged_patches=staged,
            queue=queue,
        )
    assert "Successfully replaced" in res2
    diff2 = staged["new_term.py"]
    assert "--- /dev/null" in diff2
    assert "a = 99" in diff2
    assert "b = 200" in diff2


# ---------------------------------------------------------------------------
# Issue #63 Regression Tests: create -> delete leaves staged_patches empty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_issue_63_single_tool_create_then_delete_unstages_patch() -> None:
    """Issue #63: create_file followed by delete_file removes staged patch and leaves git_diff clean."""
    from app.services.session_tools.git import git_diff

    staged: dict[str, str] = {}
    queue = _make_queue()
    path = "src/ephemeral.py"

    # Step 1: create_file stages a creation diff.
    res_create = await tool_create_file(
        path=path,
        content="def ephemeral():\n    return True\n",
        staged_patches=staged,
        queue=queue,
    )
    assert "Successfully staged new file" in res_create
    assert path in staged
    assert "--- /dev/null" in staged[path]

    diff_staged = await git_diff(
        base="abc123",
        head="staged",
        owner="owner",
        repo="repo",
        staged_patches=staged,
    )
    assert path in diff_staged

    # Step 2: delete_file on newly-created file unstages the file without phantom deletion.
    res_del = await tool_delete_file(
        path=path,
        repo_owner="owner",
        repo_name="repo",
        base_sha="abc123",
        staged_patches=staged,
        queue=queue,
    )
    assert res_del == f"Removed newly-created file '{path}'."
    assert path not in staged
    assert len(staged) == 0

    # Step 3: git_diff verifies no staged patch remains.
    diff_after = await git_diff(
        base="abc123",
        head="staged",
        owner="owner",
        repo="repo",
        staged_patches=staged,
    )
    assert path not in diff_after
    assert diff_after == "No staged patches currently in session."


@pytest.mark.asyncio
async def test_issue_63_apply_multi_patch_create_then_delete_same_batch() -> None:
    """Issue #63: apply_multi_patch with create_file and delete_file in same batch leaves staged_patches empty."""
    from app.services.session_tools.git import git_diff

    staged: dict[str, str] = {}
    queue = _make_queue()
    path = "src/batch_temp.py"

    patches = [
        {"type": "create_file", "path": path, "content": "x = 1\n"},
        {"type": "delete_file", "path": path},
    ]

    res = await tool_apply_multi_patch(
        patches=patches,
        repo_owner="owner",
        repo_name="repo",
        base_sha="abc123",
        staged_patches=staged,
        queue=queue,
    )
    assert "Successfully applied 2 file edits atomically." in res
    assert path not in staged
    assert len(staged) == 0

    diff_after = await git_diff(
        base="abc123",
        head="staged",
        owner="owner",
        repo="repo",
        staged_patches=staged,
    )
    assert path not in diff_after
    assert diff_after == "No staged patches currently in session."


@pytest.mark.asyncio
async def test_issue_63_apply_multi_patch_delete_previously_staged_creation() -> None:
    """Issue #63: apply_multi_patch delete on file created in earlier turn unstages it cleanly."""
    from app.services.session_tools.git import git_diff

    staged: dict[str, str] = {}
    queue = _make_queue()
    path = "src/prior_temp.py"

    # Turn 1: create file
    await tool_create_file(
        path=path,
        content="prior = 42\n",
        staged_patches=staged,
        queue=queue,
    )
    assert path in staged

    # Turn 2: delete file via batch
    patches = [{"type": "delete_file", "path": path}]
    res = await tool_apply_multi_patch(
        patches=patches,
        repo_owner="owner",
        repo_name="repo",
        base_sha="abc123",
        staged_patches=staged,
        queue=queue,
    )
    assert "Successfully applied 1 file edit atomically." in res
    assert path not in staged
    assert len(staged) == 0

    diff_after = await git_diff(
        base="abc123",
        head="staged",
        owner="owner",
        repo="repo",
        staged_patches=staged,
    )
    assert path not in diff_after
    assert diff_after == "No staged patches currently in session."




