"""
Repository management and model config routing tests (test_repos.py).

Covers:
1. POST /repos valid -> 201 + persisted in DB.
2. POST /repos duplicate own -> 409 Conflict.
3. POST /repos same (owner, name) for different user -> 201 Created.
4. POST /repos missing owner or name -> 422 Unprocessable Entity.
5. GET /repos multi-tenant isolation (User A sees 2, User B sees 1).
6. GET /repos empty -> 200 [].
7. DELETE /repos/{id} own repo -> 204 No Content.
8. DELETE /repos/{id} non-owned repo -> 404 Not Found (prevents existence oracle).
9. DELETE /repos/{id} non-existent repo -> 404 Not Found.
10. Concurrent POST /repos (4x same repo) -> exactly 1 row in DB (1x 201, 3x 409).
11. GET /config/model -> 200 fallback/active; GET /config/model?repo_id={non_owned} -> 404.
12. PUT /config/model valid -> 200 with server-derived base_url.
13. PUT /config/model evil_provider -> 422 Unprocessable Entity.
14. PUT /config/model evil_model -> 422 Unprocessable Entity.
15. PUT /config/model client-supplied base_url ignored / not injectable.
16. PUT /config/model/{repo_id} non-owned repo -> 404 Not Found.
17. PUT /config/model/{repo_id} per-repo scope isolated from global model config.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import uuid
import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ModelConfig, Repo, User
from app.repos import _validate_repo_ident, _verify_user_repo_permission, add_repo
from app.schemas import RepoCreate
from fastapi import HTTPException
from tests.conftest import truncate_all


@pytest.mark.parametrize(
    ("bad_owner", "bad_name"),
    [
        ("../evil", "repo"),
        ("owner", "../evil"),
        ("owner/sub", "repo"),
        ("owner", "repo/sub"),
        ("owner\x00", "repo"),
        ("owner", "repo\n"),
        ("..", "repo"),
        ("owner", ".."),
        (".", "repo"),
        ("owner", "."),
    ],
)
def test_validate_repo_ident_rejects_path_traversal_and_invalid_chars(bad_owner: str, bad_name: str) -> None:
    """_validate_repo_ident raises HTTPException 400 for path traversal, slashes, and control chars."""
    with pytest.raises(HTTPException) as exc_info:
        _validate_repo_ident(bad_owner, "owner")
        _validate_repo_ident(bad_name, "name")
    assert exc_info.value.status_code == 400


def test_validate_repo_ident_accepts_names_with_double_dots() -> None:
    """_validate_repo_ident accepts valid repository names containing '..' like 'foo..bar'."""
    _validate_repo_ident("foo..bar", "owner")
    _validate_repo_ident("my..repo", "name")


@pytest.mark.anyio
async def test_verify_user_repo_permission_rejects_invalid_identifiers() -> None:
    """_verify_user_repo_permission validates identifiers before constructing GitHub URLs."""
    user = User(id=uuid.uuid4(), github_id=123, github_username="test", access_token="enc_token")
    with pytest.raises(HTTPException) as exc_info:
        await _verify_user_repo_permission(user, "../traversal", "repo")
    assert exc_info.value.status_code == 400


@pytest.fixture(autouse=True)
def mock_github_repo_permission(respx_mock):
    """Default mock for GitHub repo permission check so existing tests pass."""
    route = respx_mock.get(
        url__regex=r"^https://api\.github\.com/repos/.*$",
        name="default_github_permission",
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "permissions": {"admin": True, "push": True, "pull": True},
                "default_branch": "main",
            },
        )
    )
    yield route


@pytest.mark.asyncio
async def test_post_repo_valid(db: AsyncSession, user_factory, make_auth_client):
    """POST /repos creates a new repo scoped to authenticated user."""
    await truncate_all(db)
    user = await user_factory(github_id=701, username="repo_owner")
    user_id = user.id
    client = make_auth_client(user_id)

    async with client:
        resp = await client.post(
            "/repos",
            json={
                "owner": "fastapi",
                "name": "fastapi",
                "default_branch": "master",
                "language_hint": "python",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["owner"] == "fastapi"
        assert data["name"] == "fastapi"
        assert data["default_branch"] == "master"
        assert data["language_hint"] == "python"
        assert "id" in data

    # Verify in DB
    res = await db.execute(select(Repo).where(Repo.id == uuid.UUID(data["id"])))
    repo_row = res.scalar_one_or_none()
    assert repo_row is not None
    assert repo_row.user_id == user_id


@pytest.mark.asyncio
async def test_post_repo_forbidden_without_access_token(
    db: AsyncSession, user_factory, make_auth_client
):
    """POST /repos without access token returns 403 Forbidden."""
    await truncate_all(db)
    user = await user_factory(github_id=740, username="no_token_user")
    user.access_token = None
    await db.commit()
    await db.refresh(user)
    client = make_auth_client(user.id)

    async with client:
        resp = await client.post(
            "/repos",
            json={"owner": "org", "name": "repo1"},
        )
        assert resp.status_code == 403
        assert "Push or admin permissions" in resp.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bad_owner", "bad_name"),
    [
        ("../evil_org", "repo1"),
        ("evil_org", "../evil_repo"),
        ("evil/org", "repo1"),
        ("org", "repo/sub"),
        ("org\x00", "repo1"),
        ("org", "repo1\n"),
        ("..", "repo1"),
        ("org", ".."),
    ],
)
async def test_post_repo_rejects_path_traversal_and_invalid_ident(
    db: AsyncSession, user_factory, make_auth_client, bad_owner: str, bad_name: str
):
    """POST /repos rejects path traversal, slashes, control chars, and invalid ident chars."""
    await truncate_all(db)
    user = await user_factory(github_id=746, username="traversal_user")
    client = make_auth_client(user.id)

    async with client:
        resp = await client.post(
            "/repos",
            json={"owner": bad_owner, "name": bad_name},
        )
        assert resp.status_code in (400, 422)


@pytest.mark.asyncio
async def test_post_repo_forbidden_when_lacking_push_admin(
    db: AsyncSession, user_factory, make_auth_client, respx_mock
):
    """POST /repos when user lacks push/admin permissions on GitHub returns 403 Forbidden."""
    await truncate_all(db)
    user = await user_factory(github_id=741, username="read_only_user")
    client = make_auth_client(user.id)

    respx_mock.routes.pop("default_github_permission")
    respx_mock.get("https://api.github.com/repos/org/read-only-repo").mock(
        return_value=httpx.Response(
            200,
            json={"permissions": {"admin": False, "push": False, "pull": True}},
        )
    )

    async with client:
        resp = await client.post(
            "/repos",
            json={"owner": "org", "name": "read-only-repo"},
        )
        assert resp.status_code == 403


@pytest.mark.asyncio
async def test_post_repo_forbidden_when_github_returns_404(
    db: AsyncSession, user_factory, make_auth_client, respx_mock
):
    """POST /repos when GitHub returns 404 for repo returns 403 Forbidden."""
    await truncate_all(db)
    user = await user_factory(github_id=742, username="non_existent_repo_user")
    client = make_auth_client(user.id)

    respx_mock.routes.pop("default_github_permission")
    respx_mock.get("https://api.github.com/repos/org/ghost-repo").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )

    async with client:
        resp = await client.post(
            "/repos",
            json={"owner": "org", "name": "ghost-repo"},
        )
        assert resp.status_code == 403


@pytest.mark.asyncio
async def test_post_repo_validates_and_sets_github_install_id(
    db: AsyncSession, user_factory, make_auth_client, monkeypatch: pytest.MonkeyPatch
):
    """POST /repos validates and persists matched github_install_id."""
    await truncate_all(db)
    user = await user_factory(github_id=743, username="install_user")
    client = make_auth_client(user.id)

    async def mock_resolve(owner: str, repo: str):
        return 998877

    monkeypatch.setattr("app.repos.resolve_installation_id", mock_resolve)

    async with client:
        resp = await client.post(
            "/repos",
            json={"owner": "org", "name": "installed-repo", "github_install_id": 998877},
        )
        assert resp.status_code == 201
        assert resp.json()["github_install_id"] == 998877


@pytest.mark.asyncio
async def test_post_repo_rejects_spoofed_github_install_id(
    db: AsyncSession, user_factory, make_auth_client, monkeypatch: pytest.MonkeyPatch
):
    """POST /repos with mismatched client github_install_id returns 400 Bad Request."""
    await truncate_all(db)
    user = await user_factory(github_id=744, username="spoof_user")
    client = make_auth_client(user.id)

    async def mock_resolve(owner: str, repo: str):
        return 998877

    monkeypatch.setattr("app.repos.resolve_installation_id", mock_resolve)

    async with client:
        resp = await client.post(
            "/repos",
            json={"owner": "org", "name": "installed-repo", "github_install_id": 112233},
        )
        assert resp.status_code == 400
        assert "Supplied github_install_id does not match" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_post_repo_drops_unverified_client_install_id(
    db: AsyncSession, user_factory, make_auth_client, monkeypatch: pytest.MonkeyPatch
):
    """POST /repos drops unverified client github_install_id when App is not installed."""
    await truncate_all(db)
    user = await user_factory(github_id=745, username="unverified_install_user")
    client = make_auth_client(user.id)

    async def mock_resolve_fail(owner: str, repo: str):
        raise RuntimeError("App not installed")

    monkeypatch.setattr("app.repos.resolve_installation_id", mock_resolve_fail)

    async with client:
        resp = await client.post(
            "/repos",
            json={"owner": "org", "name": "uninstalled-repo", "github_install_id": 112233},
        )
        assert resp.status_code == 201
        assert resp.json()["github_install_id"] is None


@pytest.mark.asyncio
async def test_post_repo_duplicate_own(
    db: AsyncSession, user_factory, make_auth_client
):
    """POST /repos for same repo twice by same user returns 409."""
    await truncate_all(db)
    user = await user_factory(github_id=702, username="dup_user")
    user_id = user.id
    client = make_auth_client(user_id)

    async with client:
        resp1 = await client.post("/repos", json={"owner": "org", "name": "repo1"})
        assert resp1.status_code == 201

        resp2 = await client.post("/repos", json={"owner": "org", "name": "repo1"})
        assert resp2.status_code == 409
        assert resp2.json() == {"detail": "Repo already connected"}


@pytest.mark.asyncio
async def test_post_repo_ignores_active_model_config_id(
    db: AsyncSession, user_factory, make_auth_client
):
    """POST /repos ignores client-supplied active_model_config_id to prevent BOLA (#205)."""
    await truncate_all(db)
    user = await user_factory(github_id=708, username="bola_user")
    client = make_auth_client(user.id)
    random_config_id = str(uuid.uuid4())

    async with client:
        resp = await client.post(
            "/repos",
            json={
                "owner": "org",
                "name": "repo_bola",
                "active_model_config_id": random_config_id,
            },
        )
        assert resp.status_code == 201
        assert resp.json()["active_model_config_id"] is None


@pytest.mark.asyncio
async def test_post_repo_fk_error_not_masked_as_409(
    db: AsyncSession, user_factory, make_auth_client
):
    """POST /repos returns 400 when an IntegrityError originates from a foreign key failure."""
    from unittest.mock import patch
    from sqlalchemy.exc import IntegrityError

    await truncate_all(db)
    user = await user_factory(github_id=709, username="fk_user")
    client = make_auth_client(user.id)

    # Simulate an IntegrityError with foreign key error signature
    fk_exc = IntegrityError(
        "insert or update on table 'repos' violates foreign key constraint",
        params={},
        orig=Exception("violates foreign key constraint"),
    )
    with patch("sqlalchemy.ext.asyncio.AsyncSession.commit", side_effect=fk_exc):
        async with client:
            resp = await client.post(
                "/repos",
                json={"owner": "org", "name": "repo_fk"},
            )
            assert resp.status_code == 400
            assert resp.json() == {"detail": "Invalid reference: referenced entity does not exist."}


@pytest.mark.asyncio
async def test_post_repo_same_owner_name_different_user(
    db: AsyncSession, user_factory, make_auth_client
):
    """POST /repos with same (owner, name) by two distinct users succeeds (multi-tenancy)."""
    await truncate_all(db)
    user_a = await user_factory(github_id=703, username="user_a")
    user_a_id = user_a.id
    user_b = await user_factory(github_id=704, username="user_b")
    user_b_id = user_b.id

    client_a = make_auth_client(user_a_id)
    client_b = make_auth_client(user_b_id)

    async with client_a:
        resp_a = await client_a.post(
            "/repos", json={"owner": "shared-org", "name": "shared-repo"}
        )
        assert resp_a.status_code == 201

    async with client_b:
        resp_b = await client_b.post(
            "/repos", json={"owner": "shared-org", "name": "shared-repo"}
        )
        assert resp_b.status_code == 201
        assert resp_b.json()["id"] != resp_a.json()["id"]


@pytest.mark.asyncio
async def test_registration_conflict_visible_on_post_and_list(
    db: AsyncSession, user_factory, make_auth_client
):
    """
    A cross-tenant duplicate flags both tenants while staying tenant-scoped:
    the first registration is clean, the second reports the conflict, and each
    GET /repos sees only its own row — now flagged true.
    """
    await truncate_all(db)
    user_a = await user_factory(github_id=720, username="conflict_user_a")
    user_b = await user_factory(github_id=721, username="conflict_user_b")
    client_a = make_auth_client(user_a.id)
    client_b = make_auth_client(user_b.id)

    async with client_a:
        resp_a = await client_a.post(
            "/repos", json={"owner": "shared-org", "name": "shared-repo"}
        )
        assert resp_a.status_code == 201
        assert resp_a.json()["registration_conflict"] is False

        list_a_before = (await client_a.get("/repos")).json()
        assert len(list_a_before) == 1
        assert list_a_before[0]["registration_conflict"] is False

        async with client_b:
            resp_b = await client_b.post(
                "/repos", json={"owner": "shared-org", "name": "shared-repo"}
            )
            assert resp_b.status_code == 201
            assert resp_b.json()["registration_conflict"] is True

            list_b = (await client_b.get("/repos")).json()
            assert len(list_b) == 1
            assert list_b[0]["registration_conflict"] is True

        list_a_after = (await client_a.get("/repos")).json()
    assert len(list_a_after) == 1
    assert list_a_after[0]["registration_conflict"] is True

    # Both registrations survive: this is visibility, not a new uniqueness rule.
    assert list_a_after[0]["id"] != list_b[0]["id"]
    assert list_a_after[0]["owner"] == list_b[0]["owner"] == "shared-org"
    assert list_a_after[0]["name"] == list_b[0]["name"] == "shared-repo"


@pytest.mark.asyncio
async def test_registration_conflict_false_for_sole_registration(
    db: AsyncSession, user_factory, make_auth_client
):
    """A repo tracked by only one tenant is never flagged, on POST or GET."""
    await truncate_all(db)
    user = await user_factory(github_id=722, username="sole_registration_user")
    client = make_auth_client(user.id)

    async with client:
        resp = await client.post(
            "/repos", json={"owner": "private-org", "name": "only-tenant"}
        )
        assert resp.status_code == 201
        assert resp.json()["registration_conflict"] is False

        listed = (await client.get("/repos")).json()
        assert len(listed) == 1
        assert listed[0]["registration_conflict"] is False


@pytest.mark.asyncio
async def test_registration_conflict_is_exact_case(
    db: AsyncSession, user_factory, make_auth_client
):
    """
    Case variants are distinct to the webhook resolver (exact equality), so
    they must not be reported as a conflict.
    """
    await truncate_all(db)
    user_a = await user_factory(github_id=723, username="case_user_a")
    user_b = await user_factory(github_id=724, username="case_user_b")
    client_a = make_auth_client(user_a.id)
    client_b = make_auth_client(user_b.id)

    async with client_a:
        resp_a = await client_a.post(
            "/repos", json={"owner": "Case-Org", "name": "Case-Repo"}
        )
        assert resp_a.status_code == 201
        assert resp_a.json()["registration_conflict"] is False

        async with client_b:
            resp_b = await client_b.post(
                "/repos", json={"owner": "case-org", "name": "case-repo"}
            )
            assert resp_b.status_code == 201
            assert resp_b.json()["registration_conflict"] is False

            list_b = (await client_b.get("/repos")).json()
            assert len(list_b) == 1
            assert list_b[0]["registration_conflict"] is False

        list_a = (await client_a.get("/repos")).json()
    assert len(list_a) == 1
    assert list_a[0]["registration_conflict"] is False


@pytest.mark.asyncio
async def test_patch_repo_auditor_install_returns_registration_conflict(
    db: AsyncSession, user_factory, make_auth_client
):
    """
    PATCH /repos/{id}/auditor-install carries the derived conflict flag too:
    true for a cross-tenant duplicate, false for a sole registration, and the
    existing 404-on-non-owner contract still holds.
    """
    await truncate_all(db)
    user_a = await user_factory(github_id=725, username="patch_user_a")
    user_b = await user_factory(github_id=726, username="patch_user_b")

    conflicted_repo = Repo(
        user_id=user_a.id, owner="patch-org", name="conflicted-repo"
    )
    unique_repo = Repo(user_id=user_a.id, owner="patch-org", name="unique-repo")
    other_tenant_repo = Repo(
        user_id=user_b.id, owner="patch-org", name="conflicted-repo"
    )
    db.add_all([conflicted_repo, unique_repo, other_tenant_repo])
    await db.commit()
    await db.refresh(conflicted_repo)
    await db.refresh(unique_repo)
    await db.refresh(other_tenant_repo)

    client_a = make_auth_client(user_a.id)

    async with client_a:
        resp_conflicted = await client_a.patch(
            f"/repos/{conflicted_repo.id}/auditor-install",
            json={"auditor_github_install_id": 4242},
        )
        assert resp_conflicted.status_code == 200
        body = resp_conflicted.json()
        assert body["auditor_github_install_id"] == 4242
        assert body["registration_conflict"] is True

        resp_unique = await client_a.patch(
            f"/repos/{unique_repo.id}/auditor-install",
            json={"auditor_github_install_id": 4343},
        )
        assert resp_unique.status_code == 200
        assert resp_unique.json()["registration_conflict"] is False

        resp_non_owned = await client_a.patch(
            f"/repos/{other_tenant_repo.id}/auditor-install",
            json={"auditor_github_install_id": 4444},
        )
        assert resp_non_owned.status_code == 404
        assert resp_non_owned.json() == {"detail": "Repo not found"}


@pytest.mark.asyncio
async def test_post_repo_missing_required_fields(
    db: AsyncSession, user_factory, make_auth_client
):
    """POST /repos missing required owner or name returns 422."""
    await truncate_all(db)
    user = await user_factory(github_id=705, username="validation_user")
    user_id = user.id
    client = make_auth_client(user_id)

    async with client:
        # Missing name
        resp1 = await client.post("/repos", json={"owner": "only-owner"})
        assert resp1.status_code == 422

        # Missing owner
        resp2 = await client.post("/repos", json={"name": "only-name"})
        assert resp2.status_code == 422


@pytest.mark.asyncio
async def test_get_repos_tenant_isolation(
    db: AsyncSession, user_factory, make_auth_client
):
    """GET /repos returns only repos owned by the calling tenant (User A sees 2, B sees 1)."""
    await truncate_all(db)
    user_a = await user_factory(github_id=706, username="user_iso_a")
    user_a_id = user_a.id
    user_b = await user_factory(github_id=707, username="user_iso_b")
    user_b_id = user_b.id

    # Seed User A repos
    r_a1 = Repo(user_id=user_a_id, owner="org-a", name="repo-1")
    r_a2 = Repo(user_id=user_a_id, owner="org-a", name="repo-2")
    # Seed User B repos
    r_b1 = Repo(user_id=user_b_id, owner="org-b", name="repo-b1")
    db.add_all([r_a1, r_a2, r_b1])
    await db.commit()

    client_a = make_auth_client(user_a_id)
    client_b = make_auth_client(user_b_id)

    async with client_a:
        resp_a = await client_a.get("/repos")
        assert resp_a.status_code == 200
        data_a = resp_a.json()
        assert len(data_a) == 2
        names_a = [r["name"] for r in data_a]
        assert "repo-1" in names_a and "repo-2" in names_a

    async with client_b:
        resp_b = await client_b.get("/repos")
        assert resp_b.status_code == 200
        data_b = resp_b.json()
        assert len(data_b) == 1
        assert data_b[0]["name"] == "repo-b1"


@pytest.mark.asyncio
async def test_get_repos_empty(db: AsyncSession, user_factory, make_auth_client):
    """GET /repos for a tenant with no repos returns 200 []."""
    await truncate_all(db)
    user = await user_factory(github_id=708, username="empty_user")
    user_id = user.id
    client = make_auth_client(user_id)

    async with client:
        resp = await client.get("/repos")
        assert resp.status_code == 200
        assert resp.json() == []


@pytest.mark.asyncio
async def test_delete_repo_own(db: AsyncSession, user_factory, make_auth_client):
    """DELETE /repos/{id} for own repo returns 204 and deletes from DB."""
    await truncate_all(db)
    user = await user_factory(github_id=709, username="del_user")
    user_id = user.id
    repo = Repo(user_id=user_id, owner="org", name="to-delete")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)
    repo_id = repo.id

    client = make_auth_client(user_id)
    async with client:
        resp = await client.delete(f"/repos/{repo_id}")
        assert resp.status_code == 204

    # Verify deleted
    res = await db.execute(select(Repo).where(Repo.id == repo_id))
    assert res.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_delete_repo_non_owned(db: AsyncSession, user_factory, make_auth_client):
    """DELETE /repos/{id} on another user's repo returns 404 (not 403, preventing existence oracle)."""
    await truncate_all(db)
    user_victim = await user_factory(github_id=710, username="victim_user")
    user_victim_id = user_victim.id
    user_attacker = await user_factory(github_id=711, username="attacker_user")
    user_attacker_id = user_attacker.id

    victim_repo = Repo(user_id=user_victim_id, owner="victim-org", name="secret-repo")
    db.add(victim_repo)
    await db.commit()
    await db.refresh(victim_repo)
    victim_repo_id = victim_repo.id

    attacker_client = make_auth_client(user_attacker_id)
    async with attacker_client:
        resp = await attacker_client.delete(f"/repos/{victim_repo_id}")
        assert resp.status_code == 404
        assert resp.json() == {"detail": "Repo not found"}

    # Assert repo is still in DB
    res = await db.execute(select(Repo).where(Repo.id == victim_repo_id))
    assert res.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_delete_repo_not_found(db: AsyncSession, user_factory, make_auth_client):
    """DELETE /repos/{random_uuid} returns 404."""
    await truncate_all(db)
    user = await user_factory(github_id=712, username="del_404_user")
    user_id = user.id
    client = make_auth_client(user_id)

    async with client:
        resp = await client.delete(f"/repos/{uuid.uuid4()}")
        assert resp.status_code == 404
        assert resp.json() == {"detail": "Repo not found"}


@pytest.mark.asyncio
async def test_concurrent_post_same_repo(
    db: AsyncSession, user_factory, make_auth_client
):
    """4 concurrent POSTs for same repo result in exactly 1 DB row and 1x 201 + 3x 409."""
    await truncate_all(db)
    user = await user_factory(github_id=713, username="concurrent_user")
    user_id = user.id

    async def _send_post():
        client = make_auth_client(user_id)
        async with client:
            return await client.post(
                "/repos", json={"owner": "race-org", "name": "race-repo"}
            )

    results = await asyncio.gather(*[_send_post() for _ in range(4)])
    status_codes = [r.status_code for r in results]

    assert (
        status_codes.count(201) == 1
    ), f"Expected exactly 1 201 Created, got {status_codes}"
    assert status_codes.count(409) == 3, f"Expected 3 409 Conflicts, got {status_codes}"

    # Verify exactly 1 row in DB
    res = await db.execute(
        select(Repo).where(Repo.user_id == user_id, Repo.name == "race-repo")
    )
    rows = res.scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_model_config_get_global_and_repo(
    db: AsyncSession, user_factory, make_auth_client
):
    """GET /config/model global read is admin-only; GET with non-owned repo_id returns 404."""
    await truncate_all(db)
    user = await user_factory(github_id=714, username="cfg_user")
    user_id = user.id
    client = make_auth_client(user_id)
    admin = await user_factory(github_id=715, username="cfg_admin", role="admin")
    admin_client = make_auth_client(admin.id)

    async with client:
        # 1a. Non-admin global read -> 403
        resp_forbidden = await client.get("/config/model")
        assert resp_forbidden.status_code == 403

    async with admin_client:
        # 1b. Admin global config
        resp_global = await admin_client.get("/config/model")
        assert resp_global.status_code == 200
        data_global = resp_global.json()
        assert "model_name" in data_global
        assert "provider" in data_global
        assert "base_url" in data_global

        # 2. Non-existent / non-owned repo config -> 404
        resp_repo_404 = await client.get(f"/config/model?repo_id={uuid.uuid4()}")
        assert resp_repo_404.status_code == 404


@pytest.mark.asyncio
async def test_put_model_config_valid_and_validation(
    db: AsyncSession, user_factory, make_auth_client
):
    """PUT /config/model validates provider/model allowlist, derives base_url server-side, ignores client base_url."""
    await truncate_all(db)
    user = await user_factory(github_id=715, username="put_cfg_user", role="admin")
    user_id = user.id
    client = make_auth_client(user_id)

    async with client:
        # 1. Valid update with openai provider
        resp_valid = await client.put(
            "/config/model",
            json={"provider": "openai", "model_name": "gpt-4o"},
        )
        assert resp_valid.status_code == 200
        data_valid = resp_valid.json()
        assert data_valid["provider"] == "openai"
        assert data_valid["model_name"] == "gpt-4o"
        assert data_valid["base_url"] == "https://api.openai.com/v1"

        # 2. Invalid provider -> 422
        resp_bad_provider = await client.put(
            "/config/model",
            json={"provider": "evil_proxy", "model_name": "gpt-4o"},
        )
        assert resp_bad_provider.status_code == 422

        # 3. Invalid model -> 422
        resp_bad_model = await client.put(
            "/config/model",
            json={"provider": "openai", "model_name": "evil-llm-injection"},
        )
        assert resp_bad_model.status_code == 422

        # 4. Client-supplied base_url is ignored / stripped by schema
        resp_override = await client.put(
            "/config/model",
            json={
                "provider": "opencode_zen",
                "model_name": "nemotron-3.5-lightning-free",
                "base_url": "https://evil-attacker.com/v1",
            },
        )
        assert resp_override.status_code == 200
        assert resp_override.json()["base_url"] == "https://opencode.ai/zen/v1"


@pytest.mark.asyncio
async def test_put_model_config_per_repo_and_non_owned(
    db: AsyncSession, user_factory, make_auth_client
):
    """PUT /config/model/{repo_id} updates per-repo config; returns 404 on unowned repo."""
    await truncate_all(db)
    user_a = await user_factory(github_id=716, username="user_repo_cfg_a")
    user_a_id = user_a.id
    user_b = await user_factory(github_id=717, username="user_repo_cfg_b")
    user_b_id = user_b.id

    repo_a = Repo(user_id=user_a_id, owner="org-a", name="repo-cfg-a")
    db.add(repo_a)
    await db.commit()
    await db.refresh(repo_a)
    repo_a_id = repo_a.id

    client_a = make_auth_client(user_a_id)
    client_b = make_auth_client(user_b_id)

    async with client_a:
        # User A updates their own repo's model config
        resp_a = await client_a.put(
            f"/config/model/{repo_a_id}",
            json={"provider": "anthropic", "model_name": "claude-sonnet-4-5"},
        )
        assert resp_a.status_code == 200
        assert resp_a.json()["model_name"] == "claude-sonnet-4-5"

    async with client_b:
        # User B attempts to update User A's repo model config -> 404
        resp_b = await client_b.put(
            f"/config/model/{repo_a_id}",
            json={"provider": "anthropic", "model_name": "claude-sonnet-4-5"},
        )
        assert resp_b.status_code == 404
        assert resp_b.json() == {"detail": "Repo not found"}


@pytest.mark.asyncio
async def test_delete_model_config_per_repo_resets_to_global(
    db: AsyncSession, user_factory, make_auth_client
):
    """DELETE /config/model/{repo_id} drops the repo override; unowned -> 404."""
    await truncate_all(db)
    user_a = await user_factory(github_id=718, username="user_repo_reset_a")
    user_a_id = user_a.id
    user_b = await user_factory(github_id=719, username="user_repo_reset_b")
    user_b_id = user_b.id

    repo_a = Repo(user_id=user_a_id, owner="org-a", name="repo-reset-a")
    db.add(repo_a)
    await db.commit()
    await db.refresh(repo_a)
    repo_a_id = repo_a.id

    client_a = make_auth_client(user_a_id)
    client_b = make_auth_client(user_b_id)

    async with client_a:
        resp_put = await client_a.put(
            f"/config/model/{repo_a_id}",
            json={"provider": "anthropic", "model_name": "claude-sonnet-4-5"},
        )
        assert resp_put.status_code == 200
        assert resp_put.json()["scope"] == "repo"

        resp_get = await client_a.get(f"/config/model?repo_id={repo_a_id}")
        assert resp_get.status_code == 200
        assert resp_get.json()["scope"] == "repo"

        resp_del = await client_a.delete(f"/config/model/{repo_a_id}")
        assert resp_del.status_code == 204

        # After reset the repo inherits the platform default again.
        resp_after = await client_a.get(f"/config/model?repo_id={repo_a_id}")
        assert resp_after.status_code == 200
        assert resp_after.json()["scope"] == "global"

    async with client_b:
        resp_b = await client_b.delete(f"/config/model/{repo_a_id}")
        assert resp_b.status_code == 404
        assert resp_b.json() == {"detail": "Repo not found"}


@pytest.mark.asyncio
async def test_add_repo_integrity_error_23505_returns_409() -> None:
    """add_repo maps IntegrityError with pgcode '23505' to HTTP 409 Conflict."""
    user = User(id=uuid.uuid4(), github_id=999, github_username="testuser", access_token="enc")
    body = RepoCreate(owner="test-owner", name="test-repo", default_branch="main")

    mock_db = AsyncMock(spec=AsyncSession)
    mock_execute_res = MagicMock()
    mock_execute_res.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_execute_res

    class MockOrig23505:
        pgcode = "23505"
        sqlstate = "23505"

        def __str__(self) -> str:
            return "duplicate key value violates unique constraint"

    mock_db.commit.side_effect = IntegrityError(
        statement="INSERT INTO repos ...",
        params={"owner": "test-owner", "name": "test-repo"},
        orig=MockOrig23505(),
    )

    with patch("app.repos._verify_user_repo_permission", AsyncMock(return_value=None)), \
         patch("app.repos.resolve_installation_id", AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc_info:
            await add_repo(body=body, current_user=user, db=mock_db)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Repo already connected"
    mock_db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_add_repo_integrity_error_unrelated_returns_400() -> None:
    """add_repo maps unrelated DB constraint IntegrityError to HTTP 400 even if bound parameters contain 'unique'."""
    user = User(id=uuid.uuid4(), github_id=999, github_username="testuser", access_token="enc")
    body = RepoCreate(owner="unique-owner", name="unique-repo", default_branch="main")

    mock_db = AsyncMock(spec=AsyncSession)
    mock_execute_res = MagicMock()
    mock_execute_res.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_execute_res

    class MockOrigCheckConstraint:
        pgcode = "23514"  # check_violation
        sqlstate = "23514"

        def __str__(self) -> str:
            return "new row violates check constraint ck_repo_something"

    mock_db.commit.side_effect = IntegrityError(
        statement="INSERT INTO repos (owner, name) VALUES ('unique-owner', 'unique-repo')",
        params={"owner": "unique-owner", "name": "unique-repo"},
        orig=MockOrigCheckConstraint(),
    )

    with patch("app.repos._verify_user_repo_permission", AsyncMock(return_value=None)), \
         patch("app.repos.resolve_installation_id", AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc_info:
            await add_repo(body=body, current_user=user, db=mock_db)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Database integrity constraint violation."
    mock_db.rollback.assert_awaited_once()

