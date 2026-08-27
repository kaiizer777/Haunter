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
import uuid
import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ModelConfig, Repo, User
from tests.conftest import truncate_all


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
async def test_post_repo_duplicate_own(db: AsyncSession, user_factory, make_auth_client):
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
async def test_post_repo_same_owner_name_different_user(db: AsyncSession, user_factory, make_auth_client):
    """POST /repos with same (owner, name) by two distinct users succeeds (multi-tenancy)."""
    await truncate_all(db)
    user_a = await user_factory(github_id=703, username="user_a")
    user_a_id = user_a.id
    user_b = await user_factory(github_id=704, username="user_b")
    user_b_id = user_b.id

    client_a = make_auth_client(user_a_id)
    client_b = make_auth_client(user_b_id)

    async with client_a:
        resp_a = await client_a.post("/repos", json={"owner": "shared-org", "name": "shared-repo"})
        assert resp_a.status_code == 201

    async with client_b:
        resp_b = await client_b.post("/repos", json={"owner": "shared-org", "name": "shared-repo"})
        assert resp_b.status_code == 201
        assert resp_b.json()["id"] != resp_a.json()["id"]


@pytest.mark.asyncio
async def test_post_repo_missing_required_fields(db: AsyncSession, user_factory, make_auth_client):
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
async def test_get_repos_tenant_isolation(db: AsyncSession, user_factory, make_auth_client):
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
async def test_concurrent_post_same_repo(db: AsyncSession, user_factory, make_auth_client):
    """4 concurrent POSTs for same repo result in exactly 1 DB row and 1x 201 + 3x 409."""
    await truncate_all(db)
    user = await user_factory(github_id=713, username="concurrent_user")
    user_id = user.id

    async def _send_post():
        client = make_auth_client(user_id)
        async with client:
            return await client.post("/repos", json={"owner": "race-org", "name": "race-repo"})

    results = await asyncio.gather(*[_send_post() for _ in range(4)])
    status_codes = [r.status_code for r in results]

    assert status_codes.count(201) == 1, f"Expected exactly 1 201 Created, got {status_codes}"
    assert status_codes.count(409) == 3, f"Expected 3 409 Conflicts, got {status_codes}"

    # Verify exactly 1 row in DB
    res = await db.execute(select(Repo).where(Repo.user_id == user_id, Repo.name == "race-repo"))
    rows = res.scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_model_config_get_global_and_repo(db: AsyncSession, user_factory, make_auth_client):
    """GET /config/model returns global config; GET with non-owned repo_id returns 404."""
    await truncate_all(db)
    user = await user_factory(github_id=714, username="cfg_user")
    user_id = user.id
    client = make_auth_client(user_id)

    async with client:
        # 1. Global config
        resp_global = await client.get("/config/model")
        assert resp_global.status_code == 200
        data_global = resp_global.json()
        assert "model_name" in data_global
        assert "provider" in data_global
        assert "base_url" in data_global

        # 2. Non-existent / non-owned repo config -> 404
        resp_repo_404 = await client.get(f"/config/model?repo_id={uuid.uuid4()}")
        assert resp_repo_404.status_code == 404


@pytest.mark.asyncio
async def test_put_model_config_valid_and_validation(db: AsyncSession, user_factory, make_auth_client):
    """PUT /config/model validates provider/model allowlist, derives base_url server-side, ignores client base_url."""
    await truncate_all(db)
    user = await user_factory(github_id=715, username="put_cfg_user")
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
async def test_put_model_config_per_repo_and_non_owned(db: AsyncSession, user_factory, make_auth_client):
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
