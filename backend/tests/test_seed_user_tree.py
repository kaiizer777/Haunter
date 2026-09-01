"""
Phase 4 unit tests for ``_seed_test_mirror_with_user_tree`` (NICE-3).

The function seeds the GitHub Actions test mirror with the user's repo
tree at the failing commit so verification can actually exercise the
failing test (not just the patch in isolation). These tests verify the
end-to-end happy path, the App→PAT fallback on 403, the
best-effort-failure sentinel, and the early-return on an empty tree.

All HTTP is mocked with respx — zero network. The mocks pin the exact
endpoint sequence the function calls so a future refactor that changes
the call order will fail loudly here rather than silently break in
production.

DB-free and sync-free. No conftest fixtures are used except the
implicit pytest-asyncio ``async def`` test runner (asyncio_mode = auto).
"""

from __future__ import annotations

import logging

import httpx
import pytest
import respx

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

_USER_TREE_SHA = "a" * 40  # 40 hex chars; GitHub SHA length
_PARENT_SHA = "b" * 40
_PARENT_TREE_SHA = "c" * 40
_NEW_TREE_SHA = "d" * 40
_NEW_COMMIT_SHA = "e" * 40


def _user_tree_entries() -> list[dict]:
    # 3 blobs: one regular file, one .github/workflows/ (skipped), one nested.
    # The function filters out the .github/workflows/ path internally.
    return [
        {"path": "src/main.py", "mode": "100644", "type": "blob", "sha": "1" * 40},
        {"path": ".github/workflows/haunter-test-py.yml", "mode": "100644", "type": "blob", "sha": "2" * 40},
        {"path": "tests/test_main.py", "mode": "100644", "type": "blob", "sha": "3" * 40},
    ]


def _ok(resp_json: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=resp_json)


# ---------------------------------------------------------------------------
# Test 1 — happy path: 200 on every call; the function returns True and
# the expected 8-call sequence lands (commit, tree, mirror, ref, parent,
# new-tree, new-commit, ref-PATCH).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_success_path(caplog: pytest.LogCaptureFixture) -> None:
    """All GitHub API calls succeed; the function returns True."""
    caplog.set_level(logging.INFO, logger="app.sandbox.github_actions_runner")

    with respx.mock(base_url=_GITHUB_API, assert_all_called=True) as rx:
        # 1. User commit fetch
        rx.get(f"/repos/{_USER_REPO}/git/commits/{_USER_SHA}").mock(
            return_value=_ok({"tree": {"sha": _USER_TREE_SHA}})
        )
        # 2. Recursive tree fetch (user repo)
        rx.get(f"/repos/{_USER_REPO}/git/trees/{_USER_TREE_SHA}").mock(
            return_value=_ok({"tree": _user_tree_entries()})
        )
        # 3. Mirror repo metadata
        rx.get(f"/repos/{_MIRROR_REPO}").mock(
            return_value=_ok({"default_branch": "main"})
        )
        # 4. Mirror default-branch ref
        rx.get(f"/repos/{_MIRROR_REPO}/git/refs/heads/main").mock(
            return_value=_ok({"object": {"sha": _PARENT_SHA}})
        )
        # 5. Parent commit (to extract parent tree SHA)
        rx.get(f"/repos/{_MIRROR_REPO}/git/commits/{_PARENT_SHA}").mock(
            return_value=_ok({"tree": {"sha": _PARENT_TREE_SHA}})
        )
        # 6. POST new tree (cross-repo blob refs)
        rx.post(f"/repos/{_MIRROR_REPO}/git/trees").mock(
            return_value=_ok({"sha": _NEW_TREE_SHA}, status=201)
        )
        # 7. POST new commit on mirror
        rx.post(f"/repos/{_MIRROR_REPO}/git/commits").mock(
            return_value=_ok({"sha": _NEW_COMMIT_SHA}, status=201)
        )
        # 8. PATCH mirror default-branch ref (fast-forward)
        rx.patch(f"/repos/{_MIRROR_REPO}/git/refs/heads/main").mock(
            return_value=_ok({})
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

    assert ok is True, "happy path must return True"

    # The function filters out .github/workflows/*, so the seeded log line
    # should report 2 files (src/main.py + tests/test_main.py).
    seeded_logs = [
        r for r in caplog.records
        if "seeded" in r.getMessage() and _MIRROR_REPO in r.getMessage()
    ]
    assert seeded_logs, "expected a 'seeded' INFO log on the happy path"
    assert "2 file(s)" in seeded_logs[0].getMessage()


# ---------------------------------------------------------------------------
# Test 2 — App token hits 403 on the user-commit fetch; PAT fallback
# retries and the function still returns True.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_pat_fallback_on_403(caplog: pytest.LogCaptureFixture) -> None:
    """App 403 on step 1 transparently falls back to PAT; function still succeeds."""
    caplog.set_level(logging.INFO, logger="app.sandbox.github_actions_runner")

    with respx.mock(base_url=_GITHUB_API, assert_all_called=True) as rx:
        # 1. App token → 403; PAT retry → 200 (use side_effect list)
        commit_route = rx.get(
            f"/repos/{_USER_REPO}/git/commits/{_USER_SHA}", name="user_commit"
        ).mock(
            side_effect=[
                httpx.Response(403, json={"message": "Must have admin access"}),
                _ok({"tree": {"sha": _USER_TREE_SHA}}),
            ]
        )
        # 2. Recursive tree fetch (now with PAT)
        rx.get(f"/repos/{_USER_REPO}/git/trees/{_USER_TREE_SHA}").mock(
            return_value=_ok({"tree": _user_tree_entries()})
        )
        # 3-5. Mirror side (now with PAT)
        rx.get(f"/repos/{_MIRROR_REPO}").mock(
            return_value=_ok({"default_branch": "main"})
        )
        rx.get(f"/repos/{_MIRROR_REPO}/git/refs/heads/main").mock(
            return_value=_ok({"object": {"sha": _PARENT_SHA}})
        )
        rx.get(f"/repos/{_MIRROR_REPO}/git/commits/{_PARENT_SHA}").mock(
            return_value=_ok({"tree": {"sha": _PARENT_TREE_SHA}})
        )
        # 6-8. Mirror writes (now with PAT)
        rx.post(f"/repos/{_MIRROR_REPO}/git/trees").mock(
            return_value=_ok({"sha": _NEW_TREE_SHA}, status=201)
        )
        rx.post(f"/repos/{_MIRROR_REPO}/git/commits").mock(
            return_value=_ok({"sha": _NEW_COMMIT_SHA}, status=201)
        )
        rx.patch(f"/repos/{_MIRROR_REPO}/git/refs/heads/main").mock(
            return_value=_ok({})
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

    assert ok is True, "PAT fallback must let the function succeed"

    # Confirm the retry actually fired (2 calls on the commit endpoint).
    assert commit_route.call_count == 2, (
        f"expected 2 calls (App 403 + PAT 200); got {commit_route.call_count}"
    )

    # And a warning about the PAT fallback must have been logged.
    fallback_logs = [
        r for r in caplog.records
        if "PAT" in r.getMessage() and "seed step 1" in r.getMessage()
    ]
    assert fallback_logs, "expected a PAT-fallback WARNING on step 1"


# ---------------------------------------------------------------------------
# Test 3 — best-effort failure: a 422 on the new-tree POST (the closest
# equivalent to "blob not found" — the function does cross-repo blob
# references, so a bad blob SHA surfaces as a 422 on the tree POST, not
# on an individual blob POST). The function must return False and emit
# a WARNING.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_blob_not_found_returns_false(caplog: pytest.LogCaptureFixture) -> None:
    """422 on the new-tree POST surfaces as False + WARNING (best-effort)."""
    caplog.set_level(logging.INFO, logger="app.sandbox.github_actions_runner")

    with respx.mock(base_url=_GITHUB_API, assert_all_called=True) as rx:
        rx.get(f"/repos/{_USER_REPO}/git/commits/{_USER_SHA}").mock(
            return_value=_ok({"tree": {"sha": _USER_TREE_SHA}})
        )
        rx.get(f"/repos/{_USER_REPO}/git/trees/{_USER_TREE_SHA}").mock(
            return_value=_ok({"tree": _user_tree_entries()})
        )
        rx.get(f"/repos/{_MIRROR_REPO}").mock(
            return_value=_ok({"default_branch": "main"})
        )
        rx.get(f"/repos/{_MIRROR_REPO}/git/refs/heads/main").mock(
            return_value=_ok({"object": {"sha": _PARENT_SHA}})
        )
        rx.get(f"/repos/{_MIRROR_REPO}/git/commits/{_PARENT_SHA}").mock(
            return_value=_ok({"tree": {"sha": _PARENT_TREE_SHA}})
        )
        # Cross-repo blob reference is invalid → GitHub returns 422.
        rx.post(f"/repos/{_MIRROR_REPO}/git/trees").mock(
            return_value=httpx.Response(
                422,
                json={"message": "Invalid blob object: not found in repository"},
            )
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

    assert ok is False, "422 on the new-tree POST must propagate as False"

    # A WARNING about the failure must have been logged.
    fail_logs = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "failed to seed" in r.getMessage()
        and _MIRROR_REPO in r.getMessage()
    ]
    assert fail_logs, "expected a 'failed to seed' WARNING log on 422"


# ---------------------------------------------------------------------------
# Test 4 — empty tree (after filter): the function returns True early
# without making any mirror-side write. This is the function's
# documented "nothing to seed" branch (line 612 of
# github_actions_runner.py); the runner continues with the old fresh
# mirror behaviour.
#
# NOTE: the Phase 4 spec asked for "a single README-only commit" in this
# case. The current implementation does not create a README commit on
# an empty tree — it returns True after logging "nothing to seed". That
# behaviour is the function's existing contract (NICE-3 wiring, not
# behaviour change), so the assertion is "returns True + early return
# + no commit POST". A future phase that wants a README commit would
# need to extend the function explicitly.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_empty_tree_returns_true_no_commit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Recursive tree response is empty; the function returns True without writing."""
    caplog.set_level(logging.INFO, logger="app.sandbox.github_actions_runner")

    with respx.mock(base_url=_GITHUB_API, assert_all_called=True) as rx:
        rx.get(f"/repos/{_USER_REPO}/git/commits/{_USER_SHA}").mock(
            return_value=_ok({"tree": {"sha": _USER_TREE_SHA}})
        )
        rx.get(f"/repos/{_USER_REPO}/git/trees/{_USER_TREE_SHA}").mock(
            return_value=_ok({"tree": []})  # truly empty
        )
        # No mirror-side mocks: the function must not reach them.

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

    assert ok is True, "empty tree must return True (early-return branch)"

    # The 'nothing to seed' INFO log must have been emitted.
    empty_logs = [
        r for r in caplog.records
        if "nothing to seed" in r.getMessage() and _MIRROR_REPO in r.getMessage()
    ]
    assert empty_logs, "expected a 'nothing to seed' INFO log on empty tree"

    # And the commit endpoint must NOT have been called (early return).
    # Count respx calls on the mirror commit POST endpoint via the calls
    # log; no named route was registered for it, so the global calls
    # counter is the right tool.
    commit_calls = [
        c for c in respx.calls
        if c.request.method == "POST"
        and c.request.url.path == f"/repos/{_MIRROR_REPO}/git/commits"
    ]
    assert not commit_calls, (
        f"mirror commit endpoint was hit on empty tree: {len(commit_calls)} call(s)"
    )
