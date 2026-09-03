"""Tarball-based mirror seeding — production fix for the cross-repo 422.

Reuses the constants and helpers already defined in
backend/app/sandbox/github_actions_runner.py and mirror.py
(_GITHUB_API_BASE, _auth_headers, _SEED_SKIP_PREFIXES). Do not redefine
them here — that would create drift.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import tarfile
from typing import Optional

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
      - Beyond max_files (deterministic ordering by sorted path)

    The first path component is stripped because GitHub tarballs are
    prefixed with a directory like "repo-name-sha/". The rest of the
    path is preserved as the file's location in the mirror.
    """
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tar:
        members = sorted(tar.getmembers(), key=lambda m: m.name)
        for member in members:
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
            f = tar.extractfile(member)
            if f is None:
                continue
            files[rel_path] = f.read()
            if len(files) >= max_files:
                break
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

    blob_entries: list[tuple[str, str]] = []
    file_items = list(files.items())
    for i in range(0, len(file_items), BLOB_BATCH_SIZE):
        batch = file_items[i:i + BLOB_BATCH_SIZE]
        results = await asyncio.gather(*(create_blob(p, c) for p, c in batch))
        blob_entries.extend(results)

    # Tree on the mirror, using mirror-local blob SHAs.
    tree_resp = await client.post(
        f"{_GITHUB_API_BASE}/repos/{mirror}/git/trees",
        headers=headers,
        json={
            "base_tree": parent_tree_sha,
            "tree": [
                {"path": p, "mode": "100644", "type": "blob", "sha": s}
                for p, s in blob_entries
            ],
        },
    )
    tree_resp.raise_for_status()
    new_tree_sha = tree_resp.json()["sha"]

    commit_resp = await client.post(
        f"{_GITHUB_API_BASE}/repos/{mirror}/git/commits",
        headers=headers,
        json={
            "message": f"haunter: seed mirror with {len(blob_entries)} file(s)",
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
