"""Tarball-based mirror seeding — production fix for the cross-repo 422.

Reuses the constants and helpers already defined in
backend/app/sandbox/github_actions_runner.py and mirror.py
(_GITHUB_API_BASE, _auth_headers, _SEED_SKIP_PREFIXES). Do not redefine
them here — that would create drift.
"""

from __future__ import annotations

import asyncio
import base64
import bz2
import gzip
import io
import logging
import tarfile
from typing import Optional, Any

import httpx

from app.sandbox.github_actions_runner import _SEED_SKIP_PREFIXES
from app.sandbox.github_actions_runner import _auth_headers as _mirror_auth_headers
from app.sandbox.github_actions_runner import _GITHUB_API_BASE

logger = logging.getLogger(__name__)

# Public header used by GET /repos/{owner}/{repo}/tarball/{sha}
# Returns a deterministic, plain tarball (not gzip) for simpler parsing.
GITHUB_TARBALL_ACCEPT = "application/vnd.github.tarball"

# Hard cap on the tarball response size. Anything larger than this is treated
# as a seed failure (the GitHub API can return up to ~500 MB for a monorepo;
# Lambda's 3 GB memory budget could not hold that AND the parsed tree).
# Sized for the worst realistic Python project tree: a 100 MB tarball at
# gzip-equivalent decoding expands ~3x in memory, so 100 MB raw ≈ 300 MB
# in-process — within budget.
MAX_TARBALL_BYTES: int = 100 * 1024 * 1024  # 100 MB

# Maximum size of a single file we will seed. Files larger than this are
# filtered out (binary blobs, vendored data, large fixtures) to keep Lambda
# memory bounded and blob-creation latency predictable.
MAX_FILE_BYTES: int = 5 * 1024 * 1024  # 5 MB

# Batch size for concurrent blob creation. 16 keeps wall-clock low while
# staying well under GitHub's 5000-req/h installation-token rate limit.
BLOB_BATCH_SIZE: int = 16


async def fetch_user_repo_tarball(
    client: httpx.AsyncClient,
    user_repo: str,
    sha: str,
    token: str,
    fallback_token: Optional[str] = None,
) -> tuple[bytes, str]:
    """GET /repos/{user_repo}/tarball/{sha} → (raw tarball bytes, token used).

    Uses the GitHub API (not codeload.github.com) so the same installation
    token used for write operations also works for read on private repos.

    Returns the raw tarball bytes and the token that ultimately succeeded
    (so the caller can keep using the same token for the rest of the seed
    flow). Raises httpx.HTTPStatusError on failure (caller decides whether
    to fall back to a fresh mirror).
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": GITHUB_TARBALL_ACCEPT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    used_token = token
    resp = await client.get(
        f"https://api.github.com/repos/{user_repo}/tarball/{sha}",
        headers=headers,
        follow_redirects=True,
    )
    if resp.status_code == 403 and fallback_token and fallback_token != token:
        logger.warning(
            "seed_tarball: GET /tarball 403 with App token — retrying with PAT"
        )
        headers["Authorization"] = f"Bearer {fallback_token}"
        used_token = fallback_token
        resp = await client.get(
            f"https://api.github.com/repos/{user_repo}/tarball/{sha}",
            headers=headers,
            follow_redirects=True,
        )

    # Enforce a hard size cap. A malicious or pathological tarball could
    # otherwise OOM the Lambda. Check after the request so the cap applies
    # regardless of which token succeeded.
    if len(resp.content) > MAX_TARBALL_BYTES:
        raise httpx.HTTPStatusError(
            f"tarball too large: {len(resp.content)} bytes > {MAX_TARBALL_BYTES}",
            request=resp.request,
            response=resp,
        )

    resp.raise_for_status()
    return resp.content, used_token


def _is_plain_tar(tar_bytes: bytes) -> bool:
    """Check if byte sequence represents an uncompressed tar archive.

    A valid uncompressed tarball either has the 'ustar' magic at offset 257,
    is completely empty (0 bytes), or starts with a 512-byte zero block
    (empty archive).
    """
    if not tar_bytes:
        return True
    if len(tar_bytes) >= 262 and tar_bytes[257:262] in (b"ustar", b"\x00\x00\x00\x00\x00"):
        return True
    if tar_bytes.startswith(b"\x00" * 512):
        return True
    return False


def _file_priority_tier(rel_path: str) -> int:
    """Return a priority tier for a repo file path (lower = higher priority).

    Tier 0 — project config files that pytest/build tools discover at startup.
              These MUST be in the sandbox or the test runner fails immediately.
    Tier 1 — dependency manifest files.  Without these the install step may be
              incomplete (wrong package versions).
    Tier 2 — test source files.  The primary artefacts being exercised.
    Tier 3 — all other source files.

    When the total file count exceeds max_files, files are selected in tier
    order so that tier-0 files are never crowded out by source code.
    """
    name = rel_path.split("/")[-1]  # basename only

    _TIER0_NAMES: frozenset[str] = frozenset({
        "pytest.ini", "pyproject.toml", "setup.cfg", "setup.py",
        "tox.ini", "conftest.py", ".python-version",
    })
    if name in _TIER0_NAMES:
        return 0

    _TIER1_GLOBS: tuple[str, ...] = (
        "requirements", "Pipfile", "poetry.lock",
    )
    if any(name.startswith(g) for g in _TIER1_GLOBS) or name in ("Pipfile.lock",):
        return 1

    # Tier 2: test files anywhere in the tree
    parts = rel_path.split("/")
    if (
        # top-level or nested tests/ / test/ directory
        any(p in ("tests", "test") for p in parts[:-1])
        # test_*.py or *_test.py filename pattern
        or name.startswith("test_")
        or name.endswith("_test.py")
    ):
        return 2

    return 3


def parse_tar_to_files(
    tar_bytes: bytes,
    max_files: int,
) -> dict[str, bytes]:
    """Parse a tarball into a {path: content_bytes} dict.

    Filters:
      - Directories (no payload)
      - Symlinks (security: prevents symlink injection into the mirror)
      - Files matching SEED_SKIP_PREFIXES
      - Files larger than MAX_FILE_BYTES
      - Beyond max_files (priority-ordered: config → deps → tests → source)

    The first path component is stripped because GitHub tarballs are
    prefixed with a directory like "repo-name-sha/". The rest of the
    path is preserved as the file's location in the mirror.

    Priority ordering ensures that pytest.ini, pyproject.toml, setup.cfg,
    requirements*.txt, and test directories are always seeded before general
    source files when the total file count exceeds max_files. This prevents
    large repos (>50 files) from having their test infrastructure silently
    dropped by the cap.
    """
    # --- Pass 1: collect all eligible (rel_path, member) pairs ----------
    # We need two passes: first collect everything that passes the skip/size
    # filters, then sort by priority and truncate. This avoids breaking out
    # of the tar iteration early (which would skip priority files that appear
    # later in the archive).
    fileobj: io.BufferedIOBase
    if tar_bytes.startswith(b"\x1f\x8b"):
        fileobj = gzip.GzipFile(fileobj=io.BytesIO(tar_bytes))
    elif tar_bytes.startswith(b"BZh"):
        fileobj = bz2.BZ2File(io.BytesIO(tar_bytes))
    elif _is_plain_tar(tar_bytes):
        fileobj = io.BytesIO(tar_bytes)
    else:
        raise ValueError(
            f"parse_tar_to_files: unrecognized compression; first bytes: {tar_bytes[:8]!r}"
        )

    eligible: list[tuple[int, str, "tarfile.TarInfo"]] = []  # (tier, rel_path, member)

    with tarfile.open(fileobj=fileobj, mode="r:") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            parts = member.name.split("/", 1)
            if len(parts) != 2:
                continue  # top-level entry, not a file
            rel_path = parts[1]
            if any(rel_path.startswith(p) for p in _SEED_SKIP_PREFIXES):
                continue
            if member.size > MAX_FILE_BYTES:
                logger.info(
                    "seed_tarball: skipping %s (%d bytes > %d)",
                    rel_path, member.size, MAX_FILE_BYTES,
                )
                continue
            tier = _file_priority_tier(rel_path)
            eligible.append((tier, rel_path, member))

    # --- Pass 2: sort by (tier, name) and apply cap ----------------------
    eligible.sort(key=lambda t: (t[0], t[1]))

    # Re-open to extract content for the selected members.
    # tarfile requires a seekable stream for random access; re-decompress.
    if tar_bytes.startswith(b"\x1f\x8b"):
        fileobj2: io.BufferedIOBase = gzip.GzipFile(fileobj=io.BytesIO(tar_bytes))
    elif tar_bytes.startswith(b"BZh"):
        fileobj2 = bz2.BZ2File(io.BytesIO(tar_bytes))
    else:
        fileobj2 = io.BytesIO(tar_bytes)

    # Build a name→member lookup from the full archive for targeted extraction.
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=fileobj2, mode="r:") as tar2:
        # Index all members by name for O(1) lookup.
        member_index: dict[str, "tarfile.TarInfo"] = {
            m.name: m for m in tar2.getmembers() if m.isfile()
        }
        for tier, rel_path, member in eligible[:max_files]:
            m = member_index.get(member.name)
            if m is None:
                continue
            f = tar2.extractfile(m)
            if f is None:
                continue
            files[rel_path] = f.read()

    return files




async def seed_mirror_via_content(
    client: httpx.AsyncClient,
    mirror: str,
    files: dict[str, bytes],
    parent_sha: str,
    parent_tree_sha: str,
    default_branch: str,
    token: str,
    fallback_token: Optional[str] = None,
) -> str:
    """Create blobs natively in the mirror, then tree → commit → ref.

    Returns the new HEAD commit SHA.

    Blobs are created in batches of BLOB_BATCH_SIZE via asyncio.gather to
    keep wall-clock low. The tree, commit, and ref-PATCH are sequential
    (GitHub's API requires the previous call's SHA as input).
    """
    headers = _mirror_auth_headers(token)

    async def create_blob(path: str, content: bytes) -> tuple[str, str]:
        encoded = base64.b64encode(content).decode("ascii")
        resp = await client.post(
            f"{_GITHUB_API_BASE}/repos/{mirror}/git/blobs",
            headers=headers,
            json={"content": encoded, "encoding": "base64"},
        )
        resp.raise_for_status()
        return path, resp.json()["sha"]

    tree_entries: list[dict[str, Any]] = []
    blobs_to_create: list[tuple[str, bytes]] = []

    for path, content in files.items():
        # Inline small text files directly into the tree to save API calls
        # and prevent GitHub secondary rate limits (403 Forbidden on blobs).
        if len(content) < 50_000:
            try:
                text_content = content.decode("utf-8")
                tree_entries.append({
                    "path": path,
                    "mode": "100644",
                    "type": "blob",
                    "content": text_content,
                })
                continue
            except UnicodeDecodeError:
                pass
        blobs_to_create.append((path, content))

    for i in range(0, len(blobs_to_create), BLOB_BATCH_SIZE):
        batch = blobs_to_create[i:i + BLOB_BATCH_SIZE]
        results = await asyncio.gather(*(create_blob(p, c) for p, c in batch))
        for p, s in results:
            tree_entries.append({"path": p, "mode": "100644", "type": "blob", "sha": s})
        if i + BLOB_BATCH_SIZE < len(blobs_to_create):
            await asyncio.sleep(0.5)  # Throttle to respect secondary rate limits

    # Tree on the mirror
    tree_resp = await client.post(
        f"{_GITHUB_API_BASE}/repos/{mirror}/git/trees",
        headers=headers,
        json={
            "base_tree": parent_tree_sha,
            "tree": tree_entries,
        },
    )
    tree_resp.raise_for_status()
    new_tree_sha = tree_resp.json()["sha"]

    commit_resp = await client.post(
        f"{_GITHUB_API_BASE}/repos/{mirror}/git/commits",
        headers=headers,
        json={
            "message": f"haunter: seed mirror with {len(tree_entries)} file(s)",
            "tree": new_tree_sha,
            "parents": [parent_sha],
        },
    )
    commit_resp.raise_for_status()
    new_commit_sha = commit_resp.json()["sha"]

    ref_resp = await client.patch(
        f"{_GITHUB_API_BASE}/repos/{mirror}/git/refs/heads/{default_branch}",
        headers=headers,
        json={"sha": new_commit_sha, "force": True},
    )
    ref_resp.raise_for_status()

    return new_commit_sha
