"""
Tests for GitHub integration router (backend/app/routers/github.py).

Covers:
1. 401 when no session cookie.
2. User with missing or un-decryptable access token returns 401.
3. List user's repos that are both private=True and permissions.push=True -> returned
   with already_connected=True if the user already added them.
4. Pagination cursor passed through via Link rel="next" header across multiple pages.
5. Network error connecting to GitHub (httpx.RequestError) is mapped to 502 with redacted body.
6. GitHub 401 / expired / insufficient scope mapping to 401 with re-login prompt.
7. GitHub 403 / 429 rate limit mapping to 429 with Retry-After header.
8. GitHub 5xx mapping to 502 generic error.
9. Permission filtering: repositories without push or admin permissions are filtered out.
10. Multi-tenant isolation: User A connected repo is not marked connected for User B.
11. _parse_next_link helper unit tests for RFC5988 Link headers and httpx .links dictionary.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Repo
from app.routers.github import _parse_next_link
from tests.conftest import truncate_all


# ---------------------------------------------------------------------------
# Unit tests for _parse_next_link
# ---------------------------------------------------------------------------


def test_parse_next_link_rfc5988():
    """Extract next URL from standard RFC5988 Link header."""
    url = "https://api.github.com/user/repos?page=2&per_page=100"
    resp = httpx.Response(200, headers={"link": f'<{url}>; rel="next"'})
    assert _parse_next_link(resp) == url


def test_parse_next_link_none_when_no_header():
    """Return None when no Link header is present."""
    resp = httpx.Response(200, headers={})
    assert _parse_next_link(resp) is None


def test_parse_next_link_with_cursor():
    """Extract next URL with cursor parameter from Link header."""
    cursor_url = "https://api.github.com/user/repos?per_page=100&after=cursor_xyz123"
    resp = httpx.Response(
        200,
        headers={"link": f'<{cursor_url}>; rel="next", <https://api.github.com/user/repos?page=5>; rel="last"'},
    )
    assert _parse_next_link(resp) == cursor_url


# ---------------------------------------------------------------------------
# Router integration tests for GET /github/available-repos
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_available_repos_requires_auth(client: httpx.AsyncClient):
    """GET /github/available-repos without session cookie returns 401."""
    resp = await client.get("/github/available-repos")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Not authenticated"}


@pytest.mark.asyncio
async def test_list_available_repos_user_missing_token(
    db: AsyncSession, user_factory, make_auth_client
):
    """User without access_token returns 401."""
    await truncate_all(db)
    user = await user_factory(github_id=901, username="user_notoken", access_token=None)
    client = make_auth_client(user.id)

    async with client:
        resp = await client.get("/github/available-repos")

    assert resp.status_code == 401
    assert resp.json() == {"detail": "GitHub not connected - please re-login"}


@pytest.mark.asyncio
async def test_list_available_repos_user_corrupted_token(
    db: AsyncSession, user_factory, make_auth_client
):
    """User with un-decryptable access_token returns 401."""
    await truncate_all(db)
    user = await user_factory(github_id=902, username="user_badtoken", access_token="initial")
    # Manually corrupt encrypted token in DB
    user.access_token = "corrupted_non_fernet_token"
    await db.commit()

    client = make_auth_client(user.id)
    async with client:
        resp = await client.get("/github/available-repos")

    assert resp.status_code == 401
    assert resp.json() == {"detail": "GitHub not connected - please re-login"}


@pytest.mark.asyncio
async def test_list_available_repos_private_push_already_connected(
    db: AsyncSession, user_factory, make_auth_client
):
    """
    List user's repos that are both private=True and permissions.push=True ->
    returned with already_connected=True if the user already added them.
    """
    await truncate_all(db)
    user = await user_factory(github_id=903, username="user_private")
    client = make_auth_client(user.id)

    # Pre-connect the repo in DB for this user
    connected_repo = Repo(
        user_id=user.id,
        owner="my-org",
        name="private-core",
        default_branch="main",
        language_hint="python",
    )
    db.add(connected_repo)
    await db.commit()

    gh_payload = [
        {
            "name": "private-core",
            "full_name": "my-org/private-core",
            "owner": {"login": "my-org"},
            "private": True,
            "default_branch": "main",
            "language": "Python",
            "updated_at": "2026-08-30T12:00:00Z",
            "permissions": {"admin": False, "push": True, "pull": True},
        },
        {
            "name": "private-unconnected",
            "full_name": "my-org/private-unconnected",
            "owner": {"login": "my-org"},
            "private": True,
            "default_branch": "main",
            "language": "Go",
            "updated_at": "2026-08-29T10:00:00Z",
            "permissions": {"admin": False, "push": True, "pull": True},
        },
    ]

    with respx.mock(base_url="https://api.github.com") as rx:
        rx.get("/user/repos").mock(return_value=httpx.Response(200, json=gh_payload))
        async with client:
            resp = await client.get("/github/available-repos")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2

    # private-core: private=True, permissions_push=True, already_connected=True
    core = next(r for r in data if r["name"] == "private-core")
    assert core["private"] is True
    assert core["permissions_push"] is True
    assert core["already_connected"] is True

    # private-unconnected: private=True, permissions_push=True, already_connected=False
    unconn = next(r for r in data if r["name"] == "private-unconnected")
    assert unconn["private"] is True
    assert unconn["permissions_push"] is True
    assert unconn["already_connected"] is False


@pytest.mark.asyncio
async def test_list_available_repos_pagination_cursor_passed_through(
    db: AsyncSession, user_factory, make_auth_client
):
    """
    Pagination cursor from Link rel="next" is passed through to the subsequent GitHub request.
    """
    await truncate_all(db)
    user = await user_factory(github_id=904, username="user_pagination")
    client = make_auth_client(user.id)

    cursor_url = "https://api.github.com/user/repos?per_page=100&after=cursor_page_2"

    page1_payload = [
        {
            "name": "repo-page-1",
            "full_name": "my-org/repo-page-1",
            "owner": {"login": "my-org"},
            "private": False,
            "default_branch": "main",
            "language": "Python",
            "updated_at": "2026-08-10T00:00:00Z",
            "permissions": {"admin": False, "push": True, "pull": True},
        }
    ]
    page2_payload = [
        {
            "name": "repo-page-2",
            "full_name": "my-org/repo-page-2",
            "owner": {"login": "my-org"},
            "private": True,
            "default_branch": "main",
            "language": "TypeScript",
            "updated_at": "2026-08-20T00:00:00Z",
            "permissions": {"admin": True, "push": True, "pull": True},
        }
    ]

    with respx.mock(base_url="https://api.github.com") as rx:
        page2_route = rx.get(cursor_url).mock(
            return_value=httpx.Response(200, json=page2_payload)
        )
        rx.get("/user/repos").mock(
            return_value=httpx.Response(
                200,
                json=page1_payload,
                headers={"Link": f'<{cursor_url}>; rel="next"'},
            )
        )

        async with client:
            resp = await client.get("/github/available-repos")

    assert resp.status_code == 200
    assert page2_route.called is True
    data = resp.json()
    assert len(data) == 2
    # Sorted by updated_at desc
    assert data[0]["name"] == "repo-page-2"
    assert data[1]["name"] == "repo-page-1"


@pytest.mark.asyncio
async def test_list_available_repos_network_error_mapped_to_502(
    db: AsyncSession, user_factory, make_auth_client
):
    """
    Network error connecting to GitHub (httpx.ConnectError) is mapped to 502
    with a redacted body (no secrets or internal exception details leaked).
    """
    await truncate_all(db)
    user = await user_factory(github_id=905, username="user_neterr")
    client = make_auth_client(user.id)

    with respx.mock(base_url="https://api.github.com") as rx:
        rx.get("/user/repos").mock(
            side_effect=httpx.ConnectError("Connection refused to api.github.com:443")
        )
        async with client:
            resp = await client.get("/github/available-repos")

    assert resp.status_code == 502
    assert resp.json() == {"detail": "Failed to connect to GitHub"}


@pytest.mark.asyncio
async def test_list_available_repos_handles_insufficient_scope(
    db: AsyncSession, user_factory, make_auth_client
):
    """GitHub returning 401 maps to 401 with re-login prompt."""
    await truncate_all(db)
    user = await user_factory(github_id=906, username="user_expired", access_token="expired_token")
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
    User B must see already_connected=False (no cross-tenant leakage).
    """
    await truncate_all(db)
    user_a = await user_factory(github_id=907, username="user_a")
    user_b = await user_factory(github_id=908, username="user_b")

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
        rx.get("/user/repos").mock(return_value=httpx.Response(200, json=gh_payload))
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
    user = await user_factory(github_id=909, username="user_perms")
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
        rx.get("/user/repos").mock(return_value=httpx.Response(200, json=gh_payload))
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
async def test_list_available_repos_rate_limit(
    db: AsyncSession, user_factory, make_auth_client
):
    """GitHub 429 / 403 rate limit maps to 429 with Retry-After header propagation."""
    await truncate_all(db)
    user = await user_factory(github_id=910, username="user_ratelimit")
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
    user = await user_factory(github_id=911, username="user_500")
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
async def test_list_available_repos_invalid_json(
    db: AsyncSession, user_factory, make_auth_client
):
    """Invalid JSON from GitHub maps to 502 Invalid response from GitHub."""
    await truncate_all(db)
    user = await user_factory(github_id=912, username="user_invalid_json")
    client = make_auth_client(user.id)

    with respx.mock(base_url="https://api.github.com") as rx:
        rx.get("/user/repos").mock(
            return_value=httpx.Response(200, text="Not a JSON payload")
        )
        async with client:
            resp = await client.get("/github/available-repos")

    assert resp.status_code == 502
    assert resp.json() == {"detail": "Invalid response from GitHub"}
