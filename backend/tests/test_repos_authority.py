"""
Issue #42 — POST /repos must verify GitHub authority before inserting.

Attack: any authenticated tenant POSTs {"owner": victim-org, "name": victim-repo}
without GitHub access. A second `repos` row for that owner/name makes
`_resolve_repo_for_delivery` (app/webhooks.py) match N>1 rows and refuse EVERY
delivery for the victim repo — a pure availability kill against one repository.

Fix (option 1): `add_repo` verifies the caller's push/admin permission via
`GET /repos/{owner}/{name}` with the caller's own OAuth token BEFORE insert
(403 otherwise), and never stores a client-supplied `github_install_id`.

Hermetic DB tests (respx-mocked GitHub, real TEST_DATABASE_URL postgres):
- attacker without access is blocked: 403, no second row, and the victim's
  delivery still resolves to exactly one repo (no ambiguity created).
- a spoofed `github_install_id` does not bypass the authority check.
- a second tenant WITH push access can still register (multi-tenancy
  preserved); the resolver then refuses ambiguous deliveries fail-closed.
"""

import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import _encrypt_token
from app.models import Repo, User
from app.repos import _verify_user_repo_permission
from app.webhooks import _resolve_repo_for_delivery
from fastapi import HTTPException
from tests.conftest import truncate_all

VICTIM_OWNER = "victim-org"
VICTIM_NAME = "victim-repo"
VICTIM_URL = f"https://api.github.com/repos/{VICTIM_OWNER}/{VICTIM_NAME}"


async def _raise_app_not_installed(owner: str, repo: str) -> int:
    """Stand-in for resolve_installation_id: write App not installed here."""
    raise RuntimeError("App not installed")

_PUSH_RESPONSE = httpx.Response(
    200,
    json={"permissions": {"push": True, "admin": False, "pull": True}},
)
_NO_ACCESS_RESPONSE = httpx.Response(404, json={"message": "Not Found"})


async def _repo_row_count(
    db: AsyncSession, owner: str, name: str
) -> int:
    """Number of `repos` rows for one owner/name across ALL tenants."""
    result = await db.execute(
        select(Repo).where(Repo.owner == owner, Repo.name == name)
    )
    return len(result.scalars().all())


@pytest.mark.asyncio
async def test_attacker_without_github_access_cannot_register_victim_repo(
    db: AsyncSession, user_factory, make_auth_client,
    respx_mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The issue #42 attack: victim registers, attacker re-registers -> 403.

    GitHub shows the repo to the victim (200 + push) but not to the attacker
    (404). The attacker's POST must fail closed: 403, no second row, and the
    victim's delivery still resolves to exactly one repo.
    """
    await truncate_all(db)
    monkeypatch.setattr(
        "app.repos.resolve_installation_id",
        _raise_app_not_installed,
    )
    respx_mock.get(VICTIM_URL).mock(
        side_effect=[_PUSH_RESPONSE, _NO_ACCESS_RESPONSE]
    )

    victim = await user_factory(github_id=801, username="victim_user")
    attacker = await user_factory(github_id=802, username="attacker_user")

    async with make_auth_client(victim.id) as victim_client:
        victim_resp = await victim_client.post(
            "/repos", json={"owner": VICTIM_OWNER, "name": VICTIM_NAME}
        )
        assert victim_resp.status_code == 201

    async with make_auth_client(attacker.id) as attacker_client:
        attacker_resp = await attacker_client.post(
            "/repos", json={"owner": VICTIM_OWNER, "name": VICTIM_NAME}
        )
        assert attacker_resp.status_code == 403
        assert "Push or admin permissions" in attacker_resp.json()["detail"]

    assert await _repo_row_count(db, VICTIM_OWNER, VICTIM_NAME) == 1

    repo, reason = await _resolve_repo_for_delivery(
        db,
        event="workflow_run",
        delivery_id="dlv-issue-42",
        owner=VICTIM_OWNER,
        name=VICTIM_NAME,
        replay_repo_id=None,
    )
    assert reason is None
    assert repo is not None
    assert repo.user_id == victim.id


@pytest.mark.asyncio
async def test_attacker_spoofed_install_id_does_not_bypass_authority(
    db: AsyncSession, user_factory, make_auth_client,
    respx_mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client-supplied `github_install_id` must not bypass the 403.

    The attacker guesses the victim's real installation id, but GitHub still
    shows them nothing (404). Authority is checked before the install id is
    even compared, so the POST fails closed with no row written.
    """
    await truncate_all(db)

    async def _resolve_real_install_id(owner: str, repo: str) -> int:
        return 998877

    monkeypatch.setattr(
        "app.repos.resolve_installation_id", _resolve_real_install_id
    )
    respx_mock.get(VICTIM_URL).mock(return_value=_NO_ACCESS_RESPONSE)

    await user_factory(github_id=803, username="victim_install_user")
    attacker = await user_factory(github_id=804, username="spoof_attacker")
    async with make_auth_client(attacker.id) as attacker_client:
        resp = await attacker_client.post(
            "/repos",
            json={
                "owner": VICTIM_OWNER,
                "name": VICTIM_NAME,
                "github_install_id": 998877,
            },
        )
        assert resp.status_code == 403

    assert await _repo_row_count(db, VICTIM_OWNER, VICTIM_NAME) == 0


@pytest.mark.asyncio
async def test_second_tenant_with_push_access_can_still_register(
    db: AsyncSession, user_factory, make_auth_client,
    respx_mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two tenants that both hold push access may track the same repo.

    Deliberate multi-tenancy (models.Repo docstring, migration b4e2f1a9c3d7):
    both POSTs succeed. The webhook resolver then refuses ambiguous deliveries
    fail-closed instead of mis-routing one tenant's delivery to the other.
    """
    await truncate_all(db)
    monkeypatch.setattr(
        "app.repos.resolve_installation_id",
        _raise_app_not_installed,
    )
    respx_mock.get(VICTIM_URL).mock(
        side_effect=[_PUSH_RESPONSE, _PUSH_RESPONSE]
    )

    tenant_a = await user_factory(github_id=805, username="tenant_a")
    tenant_b = await user_factory(github_id=806, username="tenant_b")

    async with make_auth_client(tenant_a.id) as client_a:
        resp_a = await client_a.post(
            "/repos", json={"owner": VICTIM_OWNER, "name": VICTIM_NAME}
        )
        assert resp_a.status_code == 201

    async with make_auth_client(tenant_b.id) as client_b:
        resp_b = await client_b.post(
            "/repos", json={"owner": VICTIM_OWNER, "name": VICTIM_NAME}
        )
        assert resp_b.status_code == 201

    assert await _repo_row_count(db, VICTIM_OWNER, VICTIM_NAME) == 2

    repo, reason = await _resolve_repo_for_delivery(
        db,
        event="workflow_run",
        delivery_id="dlv-issue-42-ambiguous",
        owner=VICTIM_OWNER,
        name=VICTIM_NAME,
        replay_repo_id=None,
    )
    assert repo is None
    assert reason == "ambiguous repository registration"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        [1, 2, 3],
        "just-a-string",
        {"permissions": "admin"},
        {"permissions": ["push"]},
        {"permissions": 1},
    ],
)
async def test_verify_rejects_malformed_github_payload_shapes(
    respx_mock, payload: object,
) -> None:
    """Non-object JSON or non-object `permissions` must fail closed, never 500.

    A MITM, proxy, or future GitHub shape change must not turn the authority
    check into an unhandled AttributeError: every shape violation raises an
    HTTPException (fail-closed, no row) with an explicit edge-handling branch.
    """
    user = User(
        id=uuid.uuid4(),
        github_id=900,
        github_username="shape_user",
        access_token=_encrypt_token("shape_token"),
    )
    respx_mock.get(
        "https://api.github.com/repos/org/shaped-repo"
    ).mock(return_value=httpx.Response(200, json=payload))
    with pytest.raises(HTTPException) as exc_info:
        await _verify_user_repo_permission(user, "org", "shaped-repo")
    assert exc_info.value.status_code == 502
