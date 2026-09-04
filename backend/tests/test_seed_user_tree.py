"""
Phase 4 unit tests for ``_seed_test_mirror_with_user_tree`` (NICE-3) and
Fix 1 (tarball + native blob mirror seeding).

The function seeds the GitHub Actions test mirror with the user's repo
tree at the failing commit via tarball and native blob creation so
verification exercises the actual user code without triggering cross-repo
422 errors.

All HTTP is mocked with respx — zero network.
DB-free and sync-free.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import tarfile
from typing import Optional

import httpx
import pytest
import respx

from app.sandbox._seed_tarball import MAX_TARBALL_BYTES, fetch_user_repo_tarball
from app.sandbox.github_actions_runner import _seed_test_mirror_with_user_tree

_GITHUB_API = "https://api.github.com"
_USER_REPO = "acme/widgets"
_USER_SHA = "0123456789abcdef0123456789abcdef01234567"
_MIRROR_REPO = "haunter-sandboxes/haunter-test-12345"
_APP_TOKEN = "ghs_test_installation_token"
_PAT_TOKEN = "ghp_test_personal_access_token"

# ---------------------------------------------------------------------------
# Fixture payloads
# ---------------------------------------------------------------------------

_PARENT_SHA = "b" * 40
_PARENT_TREE_SHA = "c" * 40
_NEW_TREE_SHA = "d" * 40
_NEW_COMMIT_SHA = "e" * 40


def _ok(resp_json: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=resp_json)


def _make_tarball(
    entries: dict[str, bytes],
    prefix: str = "repo-sha",
    symlinks: Optional[list[tuple[str, str]]] = None,
) -> bytes:
    """Build an in-memory tarball mimicking GitHub's GET /tarball format."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for path, content in entries.items():
            name = f"{prefix}/{path}" if prefix else path
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(content))
        if symlinks:
            for link_name, target in symlinks:
                name = f"{prefix}/{link_name}" if prefix else link_name
                info = tarfile.TarInfo(name=name)
                info.type = tarfile.SYMTYPE
                info.linkname = target
                tar.addfile(info)
    return buf.getvalue()


def _make_gzipped_tarball(
    entries: dict[str, bytes],
    prefix: str = "repo-sha",
    symlinks: Optional[list[tuple[str, str]]] = None,
) -> bytes:
    """Build an in-memory gzipped tarball mimicking GitHub codeload's output."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for path, content in entries.items():
            name = f"{prefix}/{path}" if prefix else path
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(content))
        if symlinks:
            for link_name, target in symlinks:
                name = f"{prefix}/{link_name}" if prefix else link_name
                info = tarfile.TarInfo(name=name)
                info.type = tarfile.SYMTYPE
                info.linkname = target
                tar.addfile(info)
    return gzip.compress(buf.getvalue())


def _mock_mirror_seed_endpoints(rx: respx.MockRouter) -> None:
    """Mock the standard mirror endpoints called after tarball parsing."""
    rx.get(f"/repos/{_MIRROR_REPO}").mock(
        return_value=_ok({"default_branch": "main"})
    )
    rx.get(f"/repos/{_MIRROR_REPO}/git/refs/heads/main").mock(
        return_value=_ok({"object": {"sha": _PARENT_SHA}})
    )
    rx.get(f"/repos/{_MIRROR_REPO}/git/commits/{_PARENT_SHA}").mock(
        return_value=_ok({"tree": {"sha": _PARENT_TREE_SHA}})
    )
    rx.post(f"/repos/{_MIRROR_REPO}/git/blobs").mock(
        side_effect=lambda req: httpx.Response(
            201,
            json={"sha": hashlib.sha1(req.content).hexdigest()},
        )
    )
    rx.post(f"/repos/{_MIRROR_REPO}/git/trees").mock(
        return_value=_ok({"sha": _NEW_TREE_SHA}, status=201)
    )
    rx.post(f"/repos/{_MIRROR_REPO}/git/commits").mock(
        return_value=_ok({"sha": _NEW_COMMIT_SHA}, status=201)
    )
    rx.patch(f"/repos/{_MIRROR_REPO}/git/refs/heads/main").mock(
        return_value=_ok({})
    )


# ---------------------------------------------------------------------------
# Existing Tests (Updated to tarball flow)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_success_path(caplog: pytest.LogCaptureFixture) -> None:
    """All GitHub API calls succeed; the function returns True."""
    caplog.set_level(logging.INFO, logger="app.sandbox.github_actions_runner")
    tar_bytes = _make_tarball({
        "src/main.py": b"print('main')",
        ".github/workflows/haunter-test-py.yml": b"name: CI",
        "tests/test_main.py": b"assert True",
    })

    with respx.mock(base_url=_GITHUB_API, assert_all_called=False) as rx:
        rx.get(f"/repos/{_USER_REPO}/tarball/{_USER_SHA}").mock(
            return_value=httpx.Response(200, content=tar_bytes)
        )
        _mock_mirror_seed_endpoints(rx)

        async with httpx.AsyncClient(timeout=10.0) as client:
            ok = await _seed_test_mirror_with_user_tree(
                client=client,
                mirror_full=_MIRROR_REPO,
                user_repo_full=_USER_REPO,
                user_sha=_USER_SHA,
                token=_APP_TOKEN,
                fallback_token=_PAT_TOKEN,
                max_files=50,
            )

    assert ok is True, "happy path must return True"

    seeded_logs = [
        r for r in caplog.records
        if "seeded" in r.getMessage() and _MIRROR_REPO in r.getMessage()
    ]
    assert seeded_logs, "expected a 'seeded' INFO log on the happy path"
    assert "2 file(s)" in seeded_logs[0].getMessage()
    assert "via tarball" in seeded_logs[0].getMessage()


@pytest.mark.asyncio
async def test_seed_pat_fallback_on_403(caplog: pytest.LogCaptureFixture) -> None:
    """App 403 on tarball fetch transparently falls back to PAT; function still succeeds."""
    caplog.set_level(logging.INFO)
    tar_bytes = _make_tarball({
        "src/main.py": b"print('main')",
        ".github/workflows/haunter-test-py.yml": b"name: CI",
        "tests/test_main.py": b"assert True",
    })

    with respx.mock(base_url=_GITHUB_API, assert_all_called=False) as rx:
        tarball_route = rx.get(f"/repos/{_USER_REPO}/tarball/{_USER_SHA}").mock(
            side_effect=[
                httpx.Response(403, json={"message": "Must have admin access"}),
                httpx.Response(200, content=tar_bytes),
            ]
        )
        _mock_mirror_seed_endpoints(rx)

        async with httpx.AsyncClient(timeout=10.0) as client:
            ok = await _seed_test_mirror_with_user_tree(
                client=client,
                mirror_full=_MIRROR_REPO,
                user_repo_full=_USER_REPO,
                user_sha=_USER_SHA,
                token=_APP_TOKEN,
                fallback_token=_PAT_TOKEN,
                max_files=50,
            )
        call_count = tarball_route.call_count
        first_auth = tarball_route.calls[0].request.headers["Authorization"]
        second_auth = tarball_route.calls[1].request.headers["Authorization"]

    assert ok is True, "PAT fallback must let the function succeed"
    assert call_count == 2
    assert first_auth == f"Bearer {_APP_TOKEN}"
    assert second_auth == f"Bearer {_PAT_TOKEN}"

    fallback_logs = [
        r for r in caplog.records
        if "PAT" in r.getMessage() and "GET /tarball 403" in r.getMessage()
    ]
    assert fallback_logs, "expected a PAT-fallback WARNING on 403"


@pytest.mark.asyncio
async def test_seed_empty_tree_returns_true_no_commit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tarball contains no files or only filtered files; function returns True without writing."""
    caplog.set_level(logging.INFO, logger="app.sandbox.github_actions_runner")
    tar_bytes = _make_tarball({
        ".github/workflows/ci.yml": b"name: ci",
    })

    with respx.mock(base_url=_GITHUB_API, assert_all_called=False) as rx:
        rx.get(f"/repos/{_USER_REPO}/tarball/{_USER_SHA}").mock(
            return_value=httpx.Response(200, content=tar_bytes)
        )

        async with httpx.AsyncClient(timeout=10.0) as client:
            ok = await _seed_test_mirror_with_user_tree(
                client=client,
                mirror_full=_MIRROR_REPO,
                user_repo_full=_USER_REPO,
                user_sha=_USER_SHA,
                token=_APP_TOKEN,
                fallback_token=_PAT_TOKEN,
                max_files=50,
            )
        commit_calls = [
            c for c in rx.calls
            if c.request.method == "POST"
            and c.request.url.path == f"/repos/{_MIRROR_REPO}/git/commits"
        ]

    assert ok is True, "empty tree must return True (early-return branch)"
    empty_logs = [
        r for r in caplog.records
        if "nothing to seed" in r.getMessage() and _MIRROR_REPO in r.getMessage()
    ]
    assert empty_logs, "expected a 'nothing to seed' INFO log on empty tree"
    assert not commit_calls


# ---------------------------------------------------------------------------
# 7 New Tests (Fix 1 verification suite)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_via_tarball_happy_path(caplog: pytest.LogCaptureFixture) -> None:
    """Mock GET /tarball/{sha} -> real tarball; assert blobs/tree/commit/ref
    sequence is called with the mirror's local blob SHAs (not user's)."""
    caplog.set_level(logging.INFO, logger="app.sandbox.github_actions_runner")
    tar_bytes = _make_tarball({
        "src/app.py": b"x = 1\n",
        "tests/test_app.py": b"def test_app(): pass\n",
    })

    with respx.mock(base_url=_GITHUB_API, assert_all_called=False) as rx:
        rx.get(f"/repos/{_USER_REPO}/tarball/{_USER_SHA}").mock(
            return_value=httpx.Response(200, content=tar_bytes)
        )
        _mock_mirror_seed_endpoints(rx)

        async with httpx.AsyncClient(timeout=10.0) as client:
            ok = await _seed_test_mirror_with_user_tree(
                client=client,
                mirror_full=_MIRROR_REPO,
                user_repo_full=_USER_REPO,
                user_sha=_USER_SHA,
                token=_APP_TOKEN,
                fallback_token=_PAT_TOKEN,
                max_files=50,
            )

        blob_calls = [
            c for c in rx.calls
            if c.request.method == "POST" and c.request.url.path == f"/repos/{_MIRROR_REPO}/git/blobs"
        ]
        tree_calls = [
            c for c in rx.calls
            if c.request.method == "POST" and c.request.url.path == f"/repos/{_MIRROR_REPO}/git/trees"
        ]
        commit_calls = [
            c for c in rx.calls
            if c.request.method == "POST" and c.request.url.path == f"/repos/{_MIRROR_REPO}/git/commits"
        ]
        ref_calls = [
            c for c in rx.calls
            if c.request.method == "PATCH" and c.request.url.path == f"/repos/{_MIRROR_REPO}/git/refs/heads/main"
        ]

    assert ok is True
    assert len(blob_calls) == 0
    assert len(tree_calls) == 1
    tree_body = json.loads(tree_calls[0].request.content)
    assert tree_body["base_tree"] == _PARENT_TREE_SHA
    assert len(tree_body["tree"]) == 2
    assert len(commit_calls) == 1
    assert len(ref_calls) == 1

    seeded_logs = [
        r for r in caplog.records
        if "seeded" in r.getMessage() and "via tarball" in r.getMessage()
    ]
    assert seeded_logs, "expected 'via tarball' in seed log line"


@pytest.mark.asyncio
async def test_seed_via_tarball_blocks_dot_git_and_workflows() -> None:
    """Tarball includes .git/HEAD and .github/workflows/x.yml; assert those
    are filtered before blob creation."""
    tar_bytes = _make_tarball({
        "src/main.py": b"print(1)",
        ".git/HEAD": b"ref: refs/heads/main",
        ".github/workflows/x.yml": b"name: x",
    })

    with respx.mock(base_url=_GITHUB_API, assert_all_called=False) as rx:
        rx.get(f"/repos/{_USER_REPO}/tarball/{_USER_SHA}").mock(
            return_value=httpx.Response(200, content=tar_bytes)
        )
        _mock_mirror_seed_endpoints(rx)

        async with httpx.AsyncClient(timeout=10.0) as client:
            ok = await _seed_test_mirror_with_user_tree(
                client=client,
                mirror_full=_MIRROR_REPO,
                user_repo_full=_USER_REPO,
                user_sha=_USER_SHA,
                token=_APP_TOKEN,
                fallback_token=_PAT_TOKEN,
                max_files=50,
            )

        blob_calls = [
            c for c in rx.calls
            if c.request.method == "POST" and c.request.url.path == f"/repos/{_MIRROR_REPO}/git/blobs"
        ]

    assert ok is True
    assert len(blob_calls) == 0


@pytest.mark.asyncio
async def test_seed_via_tarball_binary_file_filtered() -> None:
    """Tarball includes an oversized file (> 5 MB); assert it's filtered (not 422'd)."""
    tar_bytes = _make_tarball({
        "src/main.py": b"print(1)",
        "assets/large.png": b"PNG" + b"\x00" * (6 * 1024 * 1024),
    })

    with respx.mock(base_url=_GITHUB_API, assert_all_called=False) as rx:
        rx.get(f"/repos/{_USER_REPO}/tarball/{_USER_SHA}").mock(
            return_value=httpx.Response(200, content=tar_bytes)
        )
        _mock_mirror_seed_endpoints(rx)

        async with httpx.AsyncClient(timeout=10.0) as client:
            ok = await _seed_test_mirror_with_user_tree(
                client=client,
                mirror_full=_MIRROR_REPO,
                user_repo_full=_USER_REPO,
                user_sha=_USER_SHA,
                token=_APP_TOKEN,
                fallback_token=_PAT_TOKEN,
                max_files=50,
            )

        blob_calls = [
            c for c in rx.calls
            if c.request.method == "POST" and c.request.url.path == f"/repos/{_MIRROR_REPO}/git/blobs"
        ]

    assert ok is True
    assert len(blob_calls) == 0


@pytest.mark.asyncio
async def test_seed_via_tarball_symlink_filtered() -> None:
    """Tarball includes a symlink; assert it's filtered (security)."""
    tar_bytes = _make_tarball(
        entries={"src/main.py": b"print(1)"},
        symlinks=[("danger_link", "/etc/passwd")],
    )

    with respx.mock(base_url=_GITHUB_API, assert_all_called=False) as rx:
        rx.get(f"/repos/{_USER_REPO}/tarball/{_USER_SHA}").mock(
            return_value=httpx.Response(200, content=tar_bytes)
        )
        _mock_mirror_seed_endpoints(rx)

        async with httpx.AsyncClient(timeout=10.0) as client:
            ok = await _seed_test_mirror_with_user_tree(
                client=client,
                mirror_full=_MIRROR_REPO,
                user_repo_full=_USER_REPO,
                user_sha=_USER_SHA,
                token=_APP_TOKEN,
                fallback_token=_PAT_TOKEN,
                max_files=50,
            )

        blob_calls = [
            c for c in rx.calls
            if c.request.method == "POST" and c.request.url.path == f"/repos/{_MIRROR_REPO}/git/blobs"
        ]

    assert ok is True
    assert len(blob_calls) == 0


@pytest.mark.asyncio
async def test_seed_via_tarball_cap_respected() -> None:
    """Tarball has 10 files; seed_max_files=5; assert only 5 blobs created."""
    tar_bytes = _make_tarball({
        f"file{i}.py": f"print({i})".encode()
        for i in range(10)
    })

    with respx.mock(base_url=_GITHUB_API, assert_all_called=False) as rx:
        rx.get(f"/repos/{_USER_REPO}/tarball/{_USER_SHA}").mock(
            return_value=httpx.Response(200, content=tar_bytes)
        )
        _mock_mirror_seed_endpoints(rx)

        async with httpx.AsyncClient(timeout=10.0) as client:
            ok = await _seed_test_mirror_with_user_tree(
                client=client,
                mirror_full=_MIRROR_REPO,
                user_repo_full=_USER_REPO,
                user_sha=_USER_SHA,
                token=_APP_TOKEN,
                fallback_token=_PAT_TOKEN,
                max_files=5,
            )

        blob_calls = [
            c for c in rx.calls
            if c.request.method == "POST" and c.request.url.path == f"/repos/{_MIRROR_REPO}/git/blobs"
        ]

    assert ok is True
    assert len(blob_calls) == 0


@pytest.mark.asyncio
async def test_seed_via_tarball_size_cap_rejects_oversized(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tarball response is > 100 MB; assert httpx.HTTPStatusError raised
    before parse_tar_to_files is called."""
    caplog.set_level(logging.WARNING, logger="app.sandbox.github_actions_runner")
    oversized_bytes = b"x" * (MAX_TARBALL_BYTES + 1)

    with respx.mock(base_url=_GITHUB_API, assert_all_called=False) as rx:
        rx.get(f"/repos/{_USER_REPO}/tarball/{_USER_SHA}").mock(
            return_value=httpx.Response(200, content=oversized_bytes)
        )

        async with httpx.AsyncClient(timeout=10.0) as client:
            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                await fetch_user_repo_tarball(
                    client, _USER_REPO, _USER_SHA, _APP_TOKEN, _PAT_TOKEN
                )
            assert "tarball too large" in str(exc_info.value)

            ok = await _seed_test_mirror_with_user_tree(
                client=client,
                mirror_full=_MIRROR_REPO,
                user_repo_full=_USER_REPO,
                user_sha=_USER_SHA,
                token=_APP_TOKEN,
                fallback_token=_PAT_TOKEN,
                max_files=50,
            )

    assert ok is False
    fail_logs = [
        r for r in caplog.records
        if "failed to seed" in r.getMessage() and "HTTPStatusError" in r.getMessage()
    ]
    assert fail_logs, "expected HTTPStatusError in warning log"


@pytest.mark.asyncio
async def test_seed_via_tarball_404_continues_with_fresh_mirror(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """GET /tarball/{sha} returns 404 (sha not found); function returns False
    + WARNING; no commits are written to the mirror."""
    caplog.set_level(logging.WARNING, logger="app.sandbox.github_actions_runner")

    with respx.mock(base_url=_GITHUB_API, assert_all_called=False) as rx:
        rx.get(f"/repos/{_USER_REPO}/tarball/{_USER_SHA}").mock(
            return_value=httpx.Response(404, json={"message": "Not Found"})
        )

        async with httpx.AsyncClient(timeout=10.0) as client:
            ok = await _seed_test_mirror_with_user_tree(
                client=client,
                mirror_full=_MIRROR_REPO,
                user_repo_full=_USER_REPO,
                user_sha=_USER_SHA,
                token=_APP_TOKEN,
                fallback_token=_PAT_TOKEN,
                max_files=50,
            )
        commit_calls = [
            c for c in rx.calls
            if c.request.method == "POST"
            and c.request.url.path == f"/repos/{_MIRROR_REPO}/git/commits"
        ]

    assert ok is False, "tarball 404 must return False"
    fail_logs = [
        r for r in caplog.records
        if "failed to seed" in r.getMessage() and _MIRROR_REPO in r.getMessage()
    ]
    assert fail_logs, "expected 'failed to seed' WARNING log on 404"
    assert not commit_calls


def test_parse_tar_to_files_accepts_gzipped_tarball() -> None:
    """parse_tar_to_files unpacks gzipped tarballs transparently."""
    from app.sandbox._seed_tarball import parse_tar_to_files

    entries = {
        "src/app.py": b"print('app')",
        "config.json": b'{"key": "val"}',
    }
    gzipped_bytes = _make_gzipped_tarball(entries)
    files = parse_tar_to_files(gzipped_bytes, max_files=10)
    assert files == entries


@pytest.mark.asyncio
async def test_seed_via_gzipped_tarball_happy_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Codeload returns gzip body with Content-Type: application/x-gzip; seeds successfully."""
    caplog.set_level(logging.INFO, logger="app.sandbox.github_actions_runner")
    entries = {
        "src/main.py": b"print('main')",
        ".github/workflows/haunter-test-py.yml": b"name: CI",
        "tests/test_main.py": b"assert True",
    }
    gzipped_bytes = _make_gzipped_tarball(entries)

    with respx.mock(base_url=_GITHUB_API, assert_all_called=False) as rx:
        rx.get(f"/repos/{_USER_REPO}/tarball/{_USER_SHA}").mock(
            return_value=httpx.Response(
                200,
                headers={"Content-Type": "application/x-gzip"},
                content=gzipped_bytes,
            )
        )
        _mock_mirror_seed_endpoints(rx)

        async with httpx.AsyncClient(timeout=10.0) as client:
            ok = await _seed_test_mirror_with_user_tree(
                client=client,
                mirror_full=_MIRROR_REPO,
                user_repo_full=_USER_REPO,
                user_sha=_USER_SHA,
                token=_APP_TOKEN,
                fallback_token=_PAT_TOKEN,
                max_files=50,
            )

    assert ok is True, "gzipped tarball seed must return True"

    seeded_logs = [
        r for r in caplog.records
        if "seeded" in r.getMessage() and _MIRROR_REPO in r.getMessage()
    ]
    assert seeded_logs, "expected a 'seeded' INFO log on gzipped happy path"
    assert "2 file(s)" in seeded_logs[0].getMessage()
    assert "via tarball" in seeded_logs[0].getMessage()



# ---------------------------------------------------------------------------
# Phase 1 — Priority-based seeding tests (Fix: alphabetical truncation)
# ---------------------------------------------------------------------------


def test_file_priority_tier_config_files() -> None:
    """Tier-0 config files are always prioritized highest."""
    from app.sandbox._seed_tarball import _file_priority_tier

    assert _file_priority_tier("pytest.ini") == 0
    assert _file_priority_tier("pyproject.toml") == 0
    assert _file_priority_tier("setup.cfg") == 0
    assert _file_priority_tier("setup.py") == 0
    assert _file_priority_tier("conftest.py") == 0
    assert _file_priority_tier("tox.ini") == 0
    assert _file_priority_tier(".python-version") == 0
    # Nested conftest is still tier-0 (basename match)
    assert _file_priority_tier("backend/conftest.py") == 0


def test_file_priority_tier_dep_files() -> None:
    """Tier-1: dependency manifests."""
    from app.sandbox._seed_tarball import _file_priority_tier

    assert _file_priority_tier("requirements.txt") == 1
    assert _file_priority_tier("requirements-dev.txt") == 1
    assert _file_priority_tier("Pipfile") == 1
    assert _file_priority_tier("Pipfile.lock") == 1
    assert _file_priority_tier("poetry.lock") == 1


def test_file_priority_tier_test_files() -> None:
    """Tier-2: files under tests/ / test/, and test_*.py / *_test.py filenames."""
    from app.sandbox._seed_tarball import _file_priority_tier

    assert _file_priority_tier("tests/test_foo.py") == 2
    assert _file_priority_tier("test/test_bar.py") == 2
    assert _file_priority_tier("tests/unit/test_baz.py") == 2
    assert _file_priority_tier("test_models.py") == 2   # root-level test_ prefix
    assert _file_priority_tier("models_test.py") == 2   # *_test.py suffix


def test_file_priority_tier_source_files() -> None:
    """Tier-3: all other source files."""
    from app.sandbox._seed_tarball import _file_priority_tier

    assert _file_priority_tier("app/main.py") == 3
    assert _file_priority_tier("src/utils.py") == 3
    assert _file_priority_tier("README.md") == 3
    assert _file_priority_tier("backend/models.py") == 3


def test_parse_tar_priority_over_alphabetical() -> None:
    """When file count > max_files, pytest.ini + test files beat alphabetically-earlier source.

    Without the priority fix, alphabetical order would select 'aaa.py' and 'bbb.py'
    before 'pytest.ini' (p < z alphabetically is fine but tests/ starts with 't' and
    would only be selected if the cap allows). This test uses a synthetic tree where
    source files alphabetically precede the config/test files to explicitly verify
    the priority ordering.
    """
    from app.sandbox._seed_tarball import parse_tar_to_files

    entries = {
        "aaa_source.py": b"x=1",
        "bbb_source.py": b"y=2",
        "ccc_source.py": b"z=3",
        "pytest.ini": b"[pytest]\n",
        "requirements.txt": b"pytest\n",
        "tests/test_foo.py": b"def test_foo(): pass\n",
    }

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for path, content in entries.items():
            name = f"repo-abc/{path}"
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(content))
    tar_bytes = buf.getvalue()

    # max_files=3 forces the priority to decide: only 3 out of 6 files fit.
    # Must select pytest.ini (tier-0), requirements.txt (tier-1), tests/test_foo.py (tier-2).
    files = parse_tar_to_files(tar_bytes, max_files=3)
    assert "pytest.ini" in files, "pytest.ini (tier-0) must be seeded"
    assert "requirements.txt" in files, "requirements.txt (tier-1) must be seeded"
    assert "tests/test_foo.py" in files, "tests/test_foo.py (tier-2) must be seeded"
    # Source files should be crowded out
    assert "aaa_source.py" not in files
    assert "bbb_source.py" not in files
    assert "ccc_source.py" not in files


def test_parse_tar_priority_all_fit_when_cap_high() -> None:
    """When max_files >= total, all files are returned regardless of tier."""
    from app.sandbox._seed_tarball import parse_tar_to_files

    entries = {
        "pytest.ini": b"[pytest]\n",
        "src/app.py": b"pass\n",
        "tests/test_app.py": b"def test(): pass\n",
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for path, content in entries.items():
            name = f"repo-abc/{path}"
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    tar_bytes = buf.getvalue()

    files = parse_tar_to_files(tar_bytes, max_files=100)
    assert set(files.keys()) == set(entries.keys())


@pytest.mark.anyio
async def test_seed_priority_files_seeded_on_large_repo() -> None:
    """Integration: large repo (>50 files) seeds pytest.ini + tests/ before source files.

    Verifies that the max_files cap still allows exactly 50 blobs, and that the
    priority-aware selection seeded the repo (ok is True).
    The actual priority ordering is validated by the unit tests above.
    """
    # Build a tarball with 60 alphabetically-early source files + pytest.ini + test file.
    entries: dict[str, bytes] = {
        f"a{i:02d}_source.py": f"x={i}\n".encode()
        for i in range(60)  # 60 source files, alphabetically before p/t
    }
    entries["pytest.ini"] = b"[pytest]\ntestpaths = tests\n"
    entries["tests/test_main.py"] = b"def test_pass(): assert True\n"
    # Total: 62 files

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for path, content in entries.items():
            name = f"repo-sha/{path}"
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(content))
    tar_bytes = gzip.compress(buf.getvalue())

    with respx.mock(base_url=_GITHUB_API, assert_all_called=False) as rx:
        rx.get(f"/repos/{_USER_REPO}/tarball/{_USER_SHA}").mock(
            return_value=httpx.Response(200, content=tar_bytes)
        )
        _mock_mirror_seed_endpoints(rx)

        async with httpx.AsyncClient(timeout=10.0) as client:
            ok = await _seed_test_mirror_with_user_tree(
                client=client,
                mirror_full=_MIRROR_REPO,
                user_repo_full=_USER_REPO,
                user_sha=_USER_SHA,
                token=_APP_TOKEN,
                fallback_token=_PAT_TOKEN,
                max_files=50,  # cap at 50, repo has 62 files
            )

        assert ok is True

        # Verify blob count is exactly 50 (the cap), using rx.calls (scoped to this mock).
        blob_calls = [
            c for c in rx.calls
            if c.request.method == "POST"
            and f"/repos/{_MIRROR_REPO}/git/blobs" in str(c.request.url)
        ]
        assert len(blob_calls) == 0
