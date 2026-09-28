"""
Write path tests for repos.auditor_github_install_id.

Covers PATCH /repos/{repo_id}/auditor-install:
1. Owner sets own repo install id -> 200 + persisted in store.
2. Owner clears install id with null -> 200 + None persisted.
3. Zero / negative install id -> 422.
4. Cross-user set on another user's repo -> 404 and value unchanged.
5. Non-existent repo -> 404.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.auth import _sign_user_id
from app.models import Repo, User
from main import app
from tests.fake_audit_db import (  # noqa: F401
    FakeAsyncSession,
    audit_store,
    fake_audit_db,
    fake_audit_user_factory,
)


@pytest.fixture
def make_fake_auth_client():
    """Factory to create an authenticated AsyncClient with cookie for fake user."""

    def _make(user_id: uuid.UUID) -> AsyncClient:
        transport = ASGITransport(app=app)
        return AsyncClient(
            transport=transport,
            base_url="http://testserver",
            cookies={"haunter_session": _sign_user_id(user_id)},
        )

    return _make


@pytest_asyncio.fixture
async def seeded_env(fake_audit_db: FakeAsyncSession, fake_audit_user_factory):
    """Seed two users; user A owns one repo with no auditor install id."""
    user_a: User = await fake_audit_user_factory(github_id=9001, username="auditor_a")
    user_b: User = await fake_audit_user_factory(github_id=9002, username="auditor_b")

    repo_a = Repo(
        id=uuid.uuid4(),
        user_id=user_a.id,
        owner="auditor_a",
        name="audited_repo",
        default_branch="main",
    )
    fake_audit_db.add(repo_a)
    await fake_audit_db.commit()
    return user_a, user_b, repo_a


@pytest.mark.asyncio
async def test_owner_sets_auditor_install_id(seeded_env, make_fake_auth_client):
    """Owner A sets own repo auditor install id -> 200 and value persisted."""
    user_a, _, repo_a = seeded_env
    client = make_fake_auth_client(user_a.id)

    async with client:
        resp = await client.patch(
            f"/repos/{repo_a.id}/auditor-install",
            json={"auditor_github_install_id": 123456},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["auditor_github_install_id"] == 123456

    assert repo_a.auditor_github_install_id == 123456


@pytest.mark.asyncio
async def test_owner_clears_auditor_install_id(seeded_env, make_fake_auth_client):
    """Owner A clears install id with null -> 200 and None persisted."""
    user_a, _, repo_a = seeded_env
    repo_a.auditor_github_install_id = 777

    client = make_fake_auth_client(user_a.id)
    async with client:
        resp = await client.patch(
            f"/repos/{repo_a.id}/auditor-install",
            json={"auditor_github_install_id": None},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["auditor_github_install_id"] is None

    assert repo_a.auditor_github_install_id is None


@pytest.mark.asyncio
async def test_auditor_install_id_rejects_non_positive(
    seeded_env, make_fake_auth_client
):
    """Zero and negative install ids fail validation with 422."""
    user_a, _, repo_a = seeded_env
    client = make_fake_auth_client(user_a.id)

    async with client:
        resp_zero = await client.patch(
            f"/repos/{repo_a.id}/auditor-install",
            json={"auditor_github_install_id": 0},
        )
        assert resp_zero.status_code == 422

        resp_neg = await client.patch(
            f"/repos/{repo_a.id}/auditor-install",
            json={"auditor_github_install_id": -5},
        )
        assert resp_neg.status_code == 422

    assert repo_a.auditor_github_install_id is None


@pytest.mark.asyncio
async def test_cross_user_set_returns_404_and_value_unchanged(
    seeded_env, make_fake_auth_client
):
    """User B cannot set user A's repo install id -> 404, value unchanged."""
    user_a, user_b, repo_a = seeded_env
    repo_a.auditor_github_install_id = 111

    client_b = make_fake_auth_client(user_b.id)
    async with client_b:
        resp = await client_b.patch(
            f"/repos/{repo_a.id}/auditor-install",
            json={"auditor_github_install_id": 999999},
        )
        assert resp.status_code == 404
        assert resp.json() == {"detail": "Repo not found"}

    assert repo_a.auditor_github_install_id == 111


@pytest.mark.asyncio
async def test_set_nonexistent_repo_returns_404(seeded_env, make_fake_auth_client):
    """PATCH on an unknown repo id returns 404."""
    user_a, _, _ = seeded_env
    client = make_fake_auth_client(user_a.id)

    async with client:
        resp = await client.patch(
            f"/repos/{uuid.uuid4()}/auditor-install",
            json={"auditor_github_install_id": 42},
        )
        assert resp.status_code == 404
        assert resp.json() == {"detail": "Repo not found"}
