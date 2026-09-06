"""
Unit tests for app.sandbox._seed_tarball (Phase 5).

Covers in-memory tar parsing, skip prefixes (.git/, .github/workflows/),
MAX_FILE_BYTES filtering, priority tier truncation, compression types
(plain, gzip, bz2), and fetch_user_repo_tarball HTTP handling (success,
403 PAT fallback, MAX_TARBALL_BYTES cap).
"""

from __future__ import annotations

import bz2
import gzip
import io
import os
import tarfile
import pytest
import httpx
import respx

from app.sandbox._seed_tarball import (
    MAX_FILE_BYTES,
    MAX_TARBALL_BYTES,
    _file_priority_tier,
    _is_plain_tar,
    fetch_user_repo_tarball,
    parse_tar_to_files,
)


def _build_tar(files: dict[str, bytes | int], *, prefix: str = "repo-sha/") -> bytes:
    """Helper to build an uncompressed in-memory tarball from {rel_path: data_or_size}."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for rel_path, data_or_size in files.items():
            full_name = f"{prefix}{rel_path}"
            ti = tarfile.TarInfo(name=full_name)
            if isinstance(data_or_size, int):
                ti.size = data_or_size
                tar.addfile(ti, io.BytesIO(b"x" * data_or_size))
            else:
                ti.size = len(data_or_size)
                tar.addfile(ti, io.BytesIO(data_or_size))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# parse_tar_to_files filtering & size caps
# ---------------------------------------------------------------------------


def test_parse_tar_filters_skip_prefixes_and_max_file_bytes() -> None:
    """
    Build an in-memory tar with 3 files:
      1. small text
      2. binary (random 100 bytes)
      3. MAX_FILE_BYTES + 1 size
    Plus:
      4. .git/config
      5. .github/workflows/foo.yml

    Only the first two make it through parse_tar_to_files.
    """
    small_text = b"def test_example(): pass\n"
    random_binary = os.urandom(100)
    oversized_size = MAX_FILE_BYTES + 1

    files_input: dict[str, bytes | int] = {
        "tests/test_small.py": small_text,
        "assets/random.bin": random_binary,
        "large_data.bin": oversized_size,
        ".git/config": b"[core]\n\trepositoryformatversion = 0\n",
        ".github/workflows/foo.yml": b"name: CI\non: push\njobs: {}\n",
    }

    tar_bytes = _build_tar(files_input)
    result = parse_tar_to_files(tar_bytes, max_files=50)

    # Only small text and random binary make it through
    assert "tests/test_small.py" in result
    assert result["tests/test_small.py"] == small_text

    assert "assets/random.bin" in result
    assert result["assets/random.bin"] == random_binary

    assert "large_data.bin" not in result
    assert ".git/config" not in result
    assert ".github/workflows/foo.yml" not in result
    assert len(result) == 2


def test_parse_tar_priority_tier_ordering_and_cap() -> None:
    """Tier 0, 1, 2 files are prioritized over general source files when capped."""
    files_input: dict[str, bytes | int] = {
        "src/util_a.py": b"# util a",          # Tier 3
        "pytest.ini": b"[pytest]\n",            # Tier 0
        "src/util_b.py": b"# util b",          # Tier 3
        "requirements.txt": b"fastapi\n",      # Tier 1
        "tests/test_main.py": b"def test():pass",  # Tier 2
        "src/util_c.py": b"# util c",          # Tier 3
    }
    tar_bytes = _build_tar(files_input)

    # Cap at 3 files: should keep Tier 0 (pytest.ini), Tier 1 (requirements.txt), Tier 2 (tests/test_main.py)
    result = parse_tar_to_files(tar_bytes, max_files=3)
    assert len(result) == 3
    assert "pytest.ini" in result
    assert "requirements.txt" in result
    assert "tests/test_main.py" in result
    assert "src/util_a.py" not in result
    assert "src/util_b.py" not in result
    assert "src/util_c.py" not in result


def test_parse_tar_compression_support_gzip_and_bz2() -> None:
    """Tarballs compressed with gzip or bz2 are transparently extracted."""
    sample_files = {"app.py": b"print('hello')\n"}
    raw_tar = _build_tar(sample_files)

    # Gzip
    gz_tar = gzip.compress(raw_tar)
    gz_result = parse_tar_to_files(gz_tar, max_files=10)
    assert gz_result == sample_files

    # Bzip2
    bz2_tar = bz2.compress(raw_tar)
    bz2_result = parse_tar_to_files(bz2_tar, max_files=10)
    assert bz2_result == sample_files


def test_parse_tar_unrecognized_compression_raises() -> None:
    """Unrecognized non-tar payload raises ValueError."""
    with pytest.raises(ValueError, match="unrecognized compression"):
        parse_tar_to_files(b"INVALID_HEADER_DATA_12345", max_files=10)


def test_parse_tar_skips_non_files_and_root_entries() -> None:
    """Directories, symlinks, and root entries without slash are ignored."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        # Directory
        di = tarfile.TarInfo(name="repo-sha/subfolder")
        di.type = tarfile.DIRTYPE
        tar.addfile(di)

        # Symlink
        si = tarfile.TarInfo(name="repo-sha/link.txt")
        si.type = tarfile.SYMTYPE
        si.linkname = "regular.txt"
        tar.addfile(si)

        # Root entry (no subpath)
        root_entry = tarfile.TarInfo(name="repo-sha")
        root_entry.type = tarfile.REGTYPE
        root_entry.size = 0
        tar.addfile(root_entry, io.BytesIO(b""))

        # Regular file
        fi = tarfile.TarInfo(name="repo-sha/regular.txt")
        fi.type = tarfile.REGTYPE
        data = b"content"
        fi.size = len(data)
        tar.addfile(fi, io.BytesIO(data))

    result = parse_tar_to_files(buf.getvalue(), max_files=10)
    assert result == {"regular.txt": b"content"}


# ---------------------------------------------------------------------------
# Helper functions coverage: _is_plain_tar & _file_priority_tier
# ---------------------------------------------------------------------------


def test_is_plain_tar_detection() -> None:
    """Test _is_plain_tar on empty, zero-block, ustar magic, and invalid bytes."""
    assert _is_plain_tar(b"") is True
    assert _is_plain_tar(b"\x00" * 512) is True

    # Header with ustar magic at offset 257
    ustar_header = b"\x00" * 257 + b"ustar" + b"\x00" * 250
    assert _is_plain_tar(ustar_header) is True

    # Invalid bytes
    assert _is_plain_tar(b"NOT_A_TAR" * 10) is False


def test_file_priority_tier() -> None:
    """Verify tier assignment across config, dependency, test, and source files."""
    # Tier 0
    assert _file_priority_tier("pytest.ini") == 0
    assert _file_priority_tier("sub/pyproject.toml") == 0
    assert _file_priority_tier("conftest.py") == 0

    # Tier 1
    assert _file_priority_tier("requirements.txt") == 1
    assert _file_priority_tier("requirements-dev.txt") == 1
    assert _file_priority_tier("poetry.lock") == 1
    assert _file_priority_tier("Pipfile.lock") == 1

    # Tier 2
    assert _file_priority_tier("tests/test_unit.py") == 2
    assert _file_priority_tier("test/suite.py") == 2
    assert _file_priority_tier("src/foo_test.py") == 2
    assert _file_priority_tier("src/test_bar.py") == 2

    # Tier 3
    assert _file_priority_tier("src/main.py") == 3
    assert _file_priority_tier("docs/readme.md") == 3


# ---------------------------------------------------------------------------
# fetch_user_repo_tarball HTTP tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_user_repo_tarball_success() -> None:
    """fetch_user_repo_tarball returns tar bytes and token on 200 OK."""
    expected_bytes = b"fake-tar-bytes"
    with respx.mock(base_url="https://api.github.com") as respx_mock:
        route = respx_mock.get("/repos/owner/repo/tarball/sha123").respond(
            status_code=200,
            content=expected_bytes,
        )
        async with httpx.AsyncClient() as client:
            content, used_token = await fetch_user_repo_tarball(
                client=client,
                user_repo="owner/repo",
                sha="sha123",
                token="app_token",
            )

        assert route.called
        assert content == expected_bytes
        assert used_token == "app_token"
        req = route.calls.last.request
        assert req.headers["Authorization"] == "Bearer app_token"


@pytest.mark.asyncio
async def test_fetch_user_repo_tarball_403_fallback_pat() -> None:
    """fetch_user_repo_tarball retries with fallback_token on 403."""
    expected_bytes = b"tar-bytes-pat"
    with respx.mock(base_url="https://api.github.com") as respx_mock:
        respx_mock.get("/repos/owner/repo/tarball/sha123").side_effect = [
            httpx.Response(403, json={"message": "Forbidden"}),
            httpx.Response(200, content=expected_bytes),
        ]
        async with httpx.AsyncClient() as client:
            content, used_token = await fetch_user_repo_tarball(
                client=client,
                user_repo="owner/repo",
                sha="sha123",
                token="app_token",
                fallback_token="pat_token",
            )

        assert content == expected_bytes
        assert used_token == "pat_token"


@pytest.mark.asyncio
async def test_fetch_user_repo_tarball_max_tarball_bytes_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock streaming response exceeding MAX_TARBALL_BYTES raises HTTPStatusError."""
    # Monkeypatch cap to small value to avoid memory allocation
    monkeypatch.setattr("app.sandbox._seed_tarball.MAX_TARBALL_BYTES", 50)

    oversized_content = b"x" * 51
    with respx.mock(base_url="https://api.github.com") as respx_mock:
        respx_mock.get("/repos/owner/repo/tarball/sha123").respond(
            status_code=200,
            content=oversized_content,
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.HTTPStatusError, match="tarball too large"):
                await fetch_user_repo_tarball(
                    client=client,
                    user_repo="owner/repo",
                    sha="sha123",
                    token="app_token",
                )
