"""Regression tests for CodeRabbit findings: discard reverse-apply + session isolation."""

from __future__ import annotations

import difflib
import os
import subprocess
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


def _make_orchestrator():
    from app.services.session_orchestrator import SessionOrchestrator

    orch = SessionOrchestrator(
        session_id=uuid.uuid4(),
        db=MagicMock(),
        gh_token=None,
    )
    orch._llm = AsyncMock()
    return orch


def _init_git_repo(repo_dir: Path) -> None:
    for cmd in (
        ["git", "init"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test User"],
        ["git", "add", "."],
        ["git", "commit", "-m", "init"],
    ):
        subprocess.run(cmd, cwd=str(repo_dir), check=True, capture_output=True)


def _make_diff(old: str, new: str, path: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def test_discard_patch_reverse_applies_preserves_unrelated_lines(
    tmp_path: Path, monkeypatch,
) -> None:
    """Discarding a staged modify must reverse-apply, keeping terminal edits."""
    orch = _make_orchestrator()
    owner_dir = tmp_path / "owner"
    owner_dir.mkdir()
    repo_dir = owner_dir / "repo"
    repo_dir.mkdir()
    monkeypatch.setenv("LOCAL_REPOS_DIR", str(tmp_path))

    base_lines = [f"line{i}\n" for i in range(1, 11)]
    base = "".join(base_lines)
    target = repo_dir / "mod.py"
    target.write_text(base, encoding="utf-8")
    _init_git_repo(repo_dir)

    # Staged change: line2 -> line2_staged.
    staged_lines = list(base_lines)
    staged_lines[1] = "line2_staged\n"
    staged_content = "".join(staged_lines)
    staged_diff = _make_diff(base, staged_content, "mod.py")

    # Terminal edit on an unrelated line + staged content on disk.
    disk_lines = list(staged_lines)
    disk_lines[8] = "line9_terminal\n"
    target.write_text("".join(disk_lines), encoding="utf-8")

    staged = {"mod.py": staged_diff}
    result = orch._tool_discard_patch(
        args={"path": "mod.py"},
        staged_patches=staged,
        repo_owner="owner",
        repo_name="repo",
    )

    assert "discarded" in result.lower()
    assert "mod.py" not in staged
    final = target.read_text(encoding="utf-8")
    assert "line2_staged" not in final
    assert "line2\n" in final
    assert "line9_terminal" in final


def test_discard_patch_keeps_entry_on_reverse_failure(
    tmp_path: Path, monkeypatch,
) -> None:
    """When reverse-apply fails, the staged entry is kept and disk untouched."""
    orch = _make_orchestrator()
    owner_dir = tmp_path / "owner"
    owner_dir.mkdir()
    repo_dir = owner_dir / "repo"
    repo_dir.mkdir()
    monkeypatch.setenv("LOCAL_REPOS_DIR", str(tmp_path))

    base = "a = 1\nb = 2\nc = 3\n"
    target = repo_dir / "mod.py"
    target.write_text(base, encoding="utf-8")
    _init_git_repo(repo_dir)

    staged_diff = _make_diff(base, "a = 1\nb = 99\nc = 3\n", "mod.py")
    # Conflicting terminal edit on the same line: disk has b = TERMINAL.
    target.write_text("a = 1\nb = TERMINAL\nc = 3\n", encoding="utf-8")

    staged = {"mod.py": staged_diff}
    result = orch._tool_discard_patch(
        args={"path": "mod.py"},
        staged_patches=staged,
        repo_owner="owner",
        repo_name="repo",
    )

    assert "failed" in result.lower() or "preserved" in result.lower()
    assert "mod.py" in staged
    assert target.read_text(encoding="utf-8") == "a = 1\nb = TERMINAL\nc = 3\n"


def test_discard_patch_removes_created_untracked_file(
    tmp_path: Path, monkeypatch,
) -> None:
    """Newly created (--- /dev/null) untracked files are safely removed."""
    orch = _make_orchestrator()
    owner_dir = tmp_path / "owner"
    owner_dir.mkdir()
    repo_dir = owner_dir / "repo"
    repo_dir.mkdir()
    (repo_dir / "keep.py").write_text("x = 1\n", encoding="utf-8")
    _init_git_repo(repo_dir)
    monkeypatch.setenv("LOCAL_REPOS_DIR", str(tmp_path))

    created = repo_dir / "new_mod.py"
    created.write_text("hello\n", encoding="utf-8")
    staged_diff = "".join(
        difflib.unified_diff(
            [],
            ["hello\n"],
            fromfile="/dev/null",
            tofile="b/new_mod.py",
        )
    )
    staged = {"new_mod.py": staged_diff}
    result = orch._tool_discard_patch(
        args={"path": "new_mod.py"},
        staged_patches=staged,
        repo_owner="owner",
        repo_name="repo",
    )
    assert "discarded" in result.lower()
    assert "new_mod.py" not in staged
    assert not created.exists()


def test_resolve_repo_dir_session_isolation(
    tmp_path: Path, monkeypatch,
) -> None:
    """resolve_repo_dir with session_id prefers the session-scoped directory."""
    from app.services.session_tools.sandbox import resolve_repo_dir

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    session_id = "sess-abc-123"
    session_dir = repo_dir / ".haunter_sessions" / session_id
    session_dir.mkdir(parents=True)
    # Valid checkout marker: empty/uninitialized session dirs are skipped.
    (session_dir / ".gitkeep").write_text("session checkout\n", encoding="utf-8")
    monkeypatch.setenv("LOCAL_REPOS_DIR", str(tmp_path))

    main_root, err_main = resolve_repo_dir(repo_name="repo")
    assert err_main is None
    assert os.path.realpath(main_root or "") == os.path.realpath(str(repo_dir))

    sess_root, err_sess = resolve_repo_dir(repo_name="repo", session_id=session_id)
    assert err_sess is None
    assert sess_root is not None
    assert os.path.realpath(sess_root) == os.path.realpath(str(session_dir))
    assert os.path.realpath(sess_root) != os.path.realpath(str(repo_dir))

    # Unknown session falls back safely to the main checkout.
    fallback_root, err_fb = resolve_repo_dir(repo_name="repo", session_id="no-such-session")
    assert err_fb is None
    assert os.path.realpath(fallback_root or "") == os.path.realpath(str(repo_dir))

    # Worktree-style session dir is also honoured.
    wt_id = "sess-worktree-1"
    wt_dir = repo_dir / f"session-{wt_id}"
    wt_dir.mkdir()
    (wt_dir / ".gitkeep").write_text("worktree checkout\n", encoding="utf-8")
    wt_root, err_wt = resolve_repo_dir(repo_name="repo", session_id=wt_id)
    assert err_wt is None
    assert os.path.realpath(wt_root or "") == os.path.realpath(str(wt_dir))


def test_sync_to_local_disk_respects_session_id(
    tmp_path: Path, monkeypatch,
) -> None:
    """_sync_to_local_disk writes to the session checkout when session_id given."""
    from app.services.session_tools.editor import _sync_to_local_disk

    owner_dir = tmp_path / "owner"
    owner_dir.mkdir()
    repo_dir = owner_dir / "repo"
    repo_dir.mkdir()
    session_id = "sess-sync-1"
    session_dir = repo_dir / ".haunter_sessions" / session_id
    session_dir.mkdir(parents=True)
    (session_dir / ".gitkeep").write_text("session checkout\n", encoding="utf-8")
    monkeypatch.setenv("LOCAL_REPOS_DIR", str(tmp_path))

    _sync_to_local_disk(
        "hello.py", "print('hi')\n", "repo", "owner", action="write", session_id=session_id
    )
    assert (session_dir / "hello.py").is_file()
    assert not (repo_dir / "hello.py").is_file()


def test_discard_patch_mismatched_headers_does_not_touch_other_file(
    tmp_path: Path, monkeypatch,
) -> None:
    """Discard validates diff headers and uses --include: a staged diff whose
    headers reference victim.py must not modify victim.py when discarding
    target.py."""
    orch = _make_orchestrator()
    owner_dir = tmp_path / "owner"
    owner_dir.mkdir()
    repo_dir = owner_dir / "repo"
    repo_dir.mkdir()
    monkeypatch.setenv("LOCAL_REPOS_DIR", str(tmp_path))

    victim_base = "v = 1\n"
    victim_modified = "v = 99\n"
    target_base = "t = 1\n"
    (repo_dir / "victim.py").write_text(victim_base, encoding="utf-8")
    (repo_dir / "target.py").write_text(target_base, encoding="utf-8")
    _init_git_repo(repo_dir)

    # Staged entry keyed as target.py but headers touch victim.py.
    malicious_diff = _make_diff(victim_base, victim_modified, "victim.py")
    (repo_dir / "victim.py").write_text(victim_modified, encoding="utf-8")

    staged = {"target.py": malicious_diff}
    result = orch._tool_discard_patch(
        args={"path": "target.py"},
        staged_patches=staged,
        repo_owner="owner",
        repo_name="repo",
    )

    assert "failed" in result.lower() or "preserved" in result.lower()
    assert "target.py" in staged
    assert (repo_dir / "victim.py").read_text(encoding="utf-8") == victim_modified
    assert (repo_dir / "target.py").read_text(encoding="utf-8") == target_base


def test_discard_patch_with_spaces_in_filename(
    tmp_path: Path, monkeypatch,
) -> None:
    """Discard must handle filenames with spaces via space-safe ---/+++ parsing."""
    orch = _make_orchestrator()
    owner_dir = tmp_path / "owner"
    owner_dir.mkdir()
    repo_dir = owner_dir / "repo"
    repo_dir.mkdir()
    monkeypatch.setenv("LOCAL_REPOS_DIR", str(tmp_path))

    spaced_rel = "folder/file with spaces.py"
    spaced_file = repo_dir / spaced_rel
    spaced_file.parent.mkdir(parents=True, exist_ok=True)
    base = "x = 1\n"
    staged_content = "x = 2\n"
    spaced_file.write_text(base, encoding="utf-8")
    _init_git_repo(repo_dir)

    staged_diff = _make_diff(base, staged_content, spaced_rel)
    # Diff includes a diff --git line with spaces plus ---/+++ headers.
    staged_diff = (
        f"diff --git a/{spaced_rel} b/{spaced_rel}\n" + staged_diff
    )
    spaced_file.write_text(staged_content, encoding="utf-8")

    staged = {spaced_rel: staged_diff}
    result = orch._tool_discard_patch(
        args={"path": spaced_rel},
        staged_patches=staged,
        repo_owner="owner",
        repo_name="repo",
    )

    assert "discarded" in result.lower()
    assert spaced_rel not in staged
    assert spaced_file.read_text(encoding="utf-8") == base


def test_resolve_session_root_skips_empty_dirs(
    tmp_path: Path, monkeypatch,
) -> None:
    """Empty/uninitialized session dirs are skipped with fallback to main checkout."""
    from app.services.session_tools.sandbox import resolve_repo_dir

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    session_id = "sess-empty-1"
    empty_session = repo_dir / ".haunter_sessions" / session_id
    empty_session.mkdir(parents=True)
    monkeypatch.setenv("LOCAL_REPOS_DIR", str(tmp_path))

    root, err = resolve_repo_dir(repo_name="repo", session_id=session_id)
    assert err is None
    assert os.path.realpath(root or "") == os.path.realpath(str(repo_dir))

    (empty_session / "file.txt").write_text("x\n", encoding="utf-8")
    root2, err2 = resolve_repo_dir(repo_name="repo", session_id=session_id)
    assert err2 is None
    assert os.path.realpath(root2 or "") == os.path.realpath(str(empty_session))
