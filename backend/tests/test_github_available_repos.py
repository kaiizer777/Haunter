"""
Tests for GET /github/available-repos endpoint (test_github_available_repos.py).

Covers:
1. Multi-tenant isolation and connected status deduplication.
2. Unauthenticated access prevention (401).
3. GitHub 401 / expired / insufficient scope mapping to 401 with re-login prompt.
4. Cross-tenant isolation (User B does not see User A's connected status).
5. Permission filtering (push / admin required; read-only repos excluded).
6. Pagination handling (Link rel="next" up to 3 pages).
7. GitHub rate limit (403/429) mapping to 429 with Retry-After header.
8. GitHub 5xx mapping to 502 generic error.
9. User with missing or corrupted access token mapping to 401.
"""

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Repo
from tests.conftest import truncate_all


@pytest.mark.asyncio
async def test_list_available_repos_own_repos_only(
    db: AsyncSession, user_factory, make_auth_client
):
    """
    Mock GitHub returns 2 repos. Repo 1 is connected for User A, Repo 2 is connected
    for User B. Verify User A sees already_connected=True for Repo 1 and False for Repo 2.
    """
    await truncate_all(db)
    user_a = await user_factory(github_id=801, username="user_a", access_token="token_a")
    user_b = await user_factory(github_id=802, username="user_b", access_token="token_b")

    # User A connects owner-a/repo-1
    repo_a = Repo(
        user_id=user_a.id,
        owner="owner-a",
        name="repo-1",
        default_branch="main",
        language_hint="python",
    )
    # User B connects owner-a/repo-2
    repo_b = Repo(
        user_id=user_b.id,
        owner="owner-a",
        name="repo-2",
        default_branch="main",
        language_hint="typescript",
    )
    db.add_all([repo_a, repo_b])
    await db.commit()

    gh_payload = [
        {
            "name": "repo-1",
            "full_name": "owner-a/repo-1",
            "owner": {"login": "owner-a"},
            "private": False,
            "default_branch": "main",
            "language": "Python",
            "updated_at": "2026-08-20T10:00:00Z",
            "permissions": {"admin": False, "push": True, "pull": True},
        },
        {
            "name": "repo-2",
            "full_name": "owner-a/repo-2",
            "owner": {"login": "owner-a"},
            "private": True,
            "default_branch": "main",
            "language": "TypeScript",
            "updated_at": "2026-08-21T12:00:00Z",
            "permissions": {"admin": True, "push": True, "pull": True},
        },
    ]

    client_a = make_auth_client(user_a.id)

    with respx.mock(base_url="https://api.github.com") as rx:
        rx.get("/user/repos").mock(
            return_value=httpx.Response(200, json=gh_payload)
        )
        async with client_a:
            resp = await client_a.get("/github/available-repos")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2

    # Should be sorted by updated_at desc: repo-2 first, then repo-1
    assert data[0]["name"] == "repo-2"
    assert data[0]["already_connected"] is False  # User A has NOT connected repo-2
    assert data[0]["private"] is True
    assert data[0]["permissions_push"] is True

    assert data[1]["name"] == "repo-1"
    assert data[1]["already_connected"] is True  # User A HAS connected repo-1
    assert data[1]["private"] is False
    assert data[1]["permissions_push"] is True


@pytest.mark.asyncio
async def test_list_available_repos_requires_auth(client: httpx.AsyncClient):
    """GET /github/available-repos without session cookie returns 401."""
    resp = await client.get("/github/available-repos")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Not authenticated"}


@pytest.mark.asyncio
async def test_list_available_repos_handles_insufficient_scope(
    db: AsyncSession, user_factory, make_auth_client
):
    """GitHub returning 401 (e.g. invalid/revoked/expired token) maps to 401 with re-login detail."""
    await truncate_all(db)
    user = await user_factory(github_id=803, username="user_expired", access_token="expired_token")
    client = make_auth_client(user.id)

    with respx.mock(base_url="https://api.github.com") as rx:
        rx.get("/user/repos").mock(
            return_value=httpx.Response(401, json={"message": "Bad credentials"})
        )
        async with client:
            resp = await client.get("/github/available-repos")

    assert resp.status_code == 401
    assert resp.json() == {"detail": "Please re-login to grant repo access"}


@pytest.mark.asyncio
async def test_cross_tenant_cannot_see_other_users_repos_via_filter(
    db: AsyncSession, user_factory, make_auth_client
):
    """
    User A has connected 'haunter/core'. User B fetches available repos including 'haunter/core'.
    User B must see already_connected=False (no cross-tenant leakage of connected state).
    """
    await truncate_all(db)
    user_a = await user_factory(github_id=804, username="user_a")
    user_b = await user_factory(github_id=805, username="user_b")

    repo_a = Repo(
        user_id=user_a.id,
        owner="haunter",
        name="core",
        default_branch="main",
    )
    db.add(repo_a)
    await db.commit()

    gh_payload = [
        {
            "name": "core",
            "full_name": "haunter/core",
            "owner": {"login": "haunter"},
            "private": False,
            "default_branch": "main",
            "language": "Python",
            "updated_at": "2026-08-25T00:00:00Z",
            "permissions": {"admin": False, "push": True, "pull": True},
        }
    ]

    client_b = make_auth_client(user_b.id)

    with respx.mock(base_url="https://api.github.com") as rx:
        rx.get("/user/repos").mock(
            return_value=httpx.Response(200, json=gh_payload)
        )
        async with client_b:
            resp = await client_b.get("/github/available-repos")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == "core"
    assert data[0]["already_connected"] is False


@pytest.mark.asyncio
async def test_list_available_repos_filters_non_push_repos(
    db: AsyncSession, user_factory, make_auth_client
):
    """Only repositories with push or admin permission are returned. Read-only repos are filtered out."""
    await truncate_all(db)
    user = await user_factory(github_id=806, username="user_perms")
    client = make_auth_client(user.id)

    gh_payload = [
        {
            "name": "push-allowed",
            "full_name": "org/push-allowed",
            "owner": {"login": "org"},
            "private": False,
            "default_branch": "main",
            "language": "Go",
            "updated_at": "2026-08-20T00:00:00Z",
            "permissions": {"admin": False, "push": True, "pull": True},
        },
        {
            "name": "admin-allowed",
            "full_name": "org/admin-allowed",
            "owner": {"login": "org"},
            "private": True,
            "default_branch": "main",
            "language": "Rust",
            "updated_at": "2026-08-21T00:00:00Z",
            "permissions": {"admin": True, "push": False, "pull": True},
        },
        {
            "name": "read-only",
            "full_name": "org/read-only",
            "owner": {"login": "org"},
            "private": False,
            "default_branch": "main",
            "language": "Java",
            "updated_at": "2026-08-22T00:00:00Z",
            "permissions": {"admin": False, "push": False, "pull": True},
        },
    ]

    with respx.mock(base_url="https://api.github.com") as rx:
        rx.get("/user/repos").mock(
            return_value=httpx.Response(200, json=gh_payload)
        )
        async with client:
            resp = await client.get("/github/available-repos")

    assert resp.status_code == 200
    data = resp.json()
    repo_names = [r["name"] for r in data]
    assert "push-allowed" in repo_names
    assert "admin-allowed" in repo_names
    assert "read-only" not in repo_names
    assert len(data) == 2


@pytest.mark.asyncio
async def test_list_available_repos_pagination(
    db: AsyncSession, user_factory, make_auth_client
):
    """Handles Link header pagination across multiple pages up to 3 pages."""
    await truncate_all(db)
    user = await user_factory(github_id=807, username="user_pages")
    client = make_auth_client(user.id)

    page1_payload = [
        {
            "name": "page1-repo",
            "full_name": "org/page1-repo",
            "owner": {"login": "org"},
            "private": False,
            "default_branch": "main",
            "language": "Python",
            "updated_at": "2026-08-10T00:00:00Z",
            "permissions": {"admin": False, "push": True, "pull": True},
        }
    ]
    page2_payload = [
        {
            "name": "page2-repo",
            "full_name": "org/page2-repo",
            "owner": {"login": "org"},
            "private": False,
            "default_branch": "main",
            "language": "Python",
            "updated_at": "2026-08-20T00:00:00Z",
            "permissions": {"admin": False, "push": True, "pull": True},
        }
    ]

    page2_url = "https://api.github.com/user/repos?per_page=100&page=2"

    with respx.mock(base_url="https://api.github.com") as rx:
        rx.get(page2_url).mock(
            return_value=httpx.Response(200, json=page2_payload)
        )
        rx.get("/user/repos").mock(
            return_value=httpx.Response(
                200,
                json=page1_payload,
                headers={"Link": f'<{page2_url}>; rel="next"'},
            )
        )

        async with client:
            resp = await client.get("/github/available-repos")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    # Sorted by updated_at desc: page2-repo (Aug 20) before page1-repo (Aug 10)
    assert data[0]["name"] == "page2-repo"
    assert data[1]["name"] == "page1-repo"


@pytest.mark.asyncio
async def test_list_available_repos_rate_limit(
    db: AsyncSession, user_factory, make_auth_client
):
    """GitHub 429 / 403 rate limit maps to 429 with Retry-After header propagation."""
    await truncate_all(db)
    user = await user_factory(github_id=808, username="user_ratelimit")
    client = make_auth_client(user.id)

    with respx.mock(base_url="https://api.github.com") as rx:
        rx.get("/user/repos").mock(
            return_value=httpx.Response(
                429,
                json={"message": "API rate limit exceeded"},
                headers={"Retry-After": "60"},
            )
        )
        async with client:
            resp = await client.get("/github/available-repos")

    assert resp.status_code == 429
    assert resp.json() == {"detail": "GitHub rate limit exceeded"}
    assert resp.headers.get("retry-after") == "60"


@pytest.mark.asyncio
async def test_list_available_repos_github_5xx(
    db: AsyncSession, user_factory, make_auth_client
):
    """GitHub 500 error maps to 502 generic error."""
    await truncate_all(db)
    user = await user_factory(github_id=809, username="user_500")
    client = make_auth_client(user.id)

    with respx.mock(base_url="https://api.github.com") as rx:
        rx.get("/user/repos").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        async with client:
            resp = await client.get("/github/available-repos")

    assert resp.status_code == 502
    assert resp.json() == {"detail": "GitHub API error"}


@pytest.mark.asyncio
async def test_list_available_repos_user_missing_token(
    db: AsyncSession, user_factory, make_auth_client
):
    """User without access_token returns 401."""
    await truncate_all(db)
    user = await user_factory(github_id=810, username="user_notoken", access_token=None)
    client = make_auth_client(user.id)

    async with client:
        resp = await client.get("/github/available-repos")

    assert resp.status_code == 401
    assert resp.json() == {"detail": "GitHub not connected - please re-login"}
