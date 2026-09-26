"""
Hermetic test suite for Repository Operational Governance & Feature Settings (Phase 6.1 & 6.2).

Covers:
1. GET /api/repos/{repo_id}/settings default fallback when no row exists in DB
2. GET /api/repos/{repo_id}/settings with existing persisted row
3. GET /api/repos/{repo_id}/settings on non-existent repo (404)
4. GET /api/repos/{repo_id}/settings multi-tenant isolation / IDOR prevention (404 for other user's repo)
5. GET /api/repos/{repo_id}/settings unauthenticated (401)
6. PATCH /api/repos/{repo_id}/settings partial update with flat fields
7. PATCH /api/repos/{repo_id}/settings partial update with nested features and audit_triggers (future02.md)
8. PATCH /api/repos/{repo_id}/settings validation failures:
   - negative max_cost_per_run_cents (422)
   - min_confidence_threshold < 0 or > 100 (422)
   - invalid preset name (422)
   - invalid git branch names (422)
9. PATCH /api/repos/{repo_id}/settings cross-user repo access (404)
10. POST /api/repos/{repo_id}/settings/preset applying governance presets:
    - autonomous / full_autonomous
    - conservative
    - standard
    - audit_only / auditor_only
    - live_studio_only
    - custom
11. POST /api/repos/{repo_id}/settings/preset/{preset_name} path param route
12. POST /api/repos/{repo_id}/settings/preset invalid preset (422)
13. POST /api/repos/{repo_id}/settings/preset cross-user repo access (404)
14. GET /api/settings/repos list all user repos with governance features and triggers
15. Service layer unit tests for branch validation, bounds checking, preset application, and version bumping
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.auth import _sign_user_id
from app.models import Repo, RepoSettings, User
from app.services.repo_settings import (
    ALLOWED_PRESETS,
    apply_preset,
    create_default_repo_settings,
    get_repo_settings,
    normalize_preset_name,
    update_repo_settings,
    validate_branch_name,
    validate_branches,
    validate_confidence_threshold,
    validate_cost_bounds,
)
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
    """Seed two users and a repo for User A."""
    user_a: User = await fake_audit_user_factory(github_id=5001, username="alpha_owner")
    user_b: User = await fake_audit_user_factory(github_id=5002, username="beta_owner")

    repo_a = Repo(
        id=uuid.uuid4(),
        user_id=user_a.id,
        owner="alpha_owner",
        name="alpha_repo",
        default_branch="main",
    )
    fake_audit_db.add(repo_a)

    repo_b = Repo(
        id=uuid.uuid4(),
        user_id=user_b.id,
        owner="beta_owner",
        name="beta_repo",
        default_branch="main",
    )
    fake_audit_db.add(repo_b)

    await fake_audit_db.commit()
    return user_a, user_b, repo_a, repo_b


# ===========================================================================
# Endpoint Tests: GET /repos/{repo_id}/settings
# ===========================================================================


@pytest.mark.asyncio
async def test_get_settings_default_fallback(seeded_env, make_fake_auth_client):
    """When repo has no settings row, GET returns default fallback configuration."""
    user_a, _, repo_a, _ = seeded_env
    client = make_fake_auth_client(user_a.id)

    async with client:
        resp = await client.get(f"/api/repos/{repo_a.id}/settings")
        assert resp.status_code == 200, resp.text
        data = resp.json()

        assert data["repo_id"] == str(repo_a.id)
        assert data["preset"] == "autonomous"
        assert data["preset_profile"] == "autonomous"
        assert data["enable_auto_fix"] is True
        assert data["enable_auto_fixer"] is True
        assert data["enable_auditor_mode"] is False
        assert data["enable_sandbox_verification"] is True
        assert data["enable_ci_sandbox"] is True
        assert data["enable_pr_comments"] is True
        assert data["enable_live_sessions"] is True
        assert data["enable_webcontainer_preview"] is True
        assert data["enable_subagents"] is True
        assert data["audit_trigger_on_pr"] is True
        assert data["audit_trigger_on_ci_failure"] is True
        assert data["audit_trigger_on_ci_success"] is False
        assert data["audit_trigger_on_manual_mention"] is True
        assert data["allowed_branches"] == ["main", "master"]
        assert data["monitored_branches"] == ["main", "master"]
        assert data["min_confidence_threshold"] == 80
        assert data["max_cost_per_run_cents"] == 100
        assert data["model_override_scope"] == "inherit"
        assert data["settings_version"] == 1


@pytest.mark.asyncio
async def test_get_settings_without_api_prefix(seeded_env, make_fake_auth_client):
    """GET /repos/{repo_id}/settings works identically to /api/repos/{repo_id}/settings."""
    user_a, _, repo_a, _ = seeded_env
    client = make_fake_auth_client(user_a.id)

    async with client:
        resp = await client.get(f"/repos/{repo_a.id}/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert data["repo_id"] == str(repo_a.id)
        assert data["preset"] == "autonomous"


@pytest.mark.asyncio
async def test_get_settings_persisted_row(fake_audit_db, seeded_env, make_fake_auth_client):
    """When repo has a persisted settings row, GET returns the stored values."""
    user_a, _, repo_a, _ = seeded_env

    persisted = RepoSettings(
        id=uuid.uuid4(),
        repo_id=repo_a.id,
        preset="conservative",
        enable_auto_fix=False,
        enable_auditor_mode=True,
        enable_sandbox_verification=True,
        enable_pr_comments=True,
        enable_live_sessions=False,
        enable_webcontainer_preview=False,
        enable_subagents=False,
        audit_trigger_on_pr=True,
        audit_trigger_on_ci_failure=True,
        audit_trigger_on_ci_success=False,
        audit_trigger_on_manual_mention=True,
        allowed_branches=["release", "prod"],
        min_confidence_threshold=92,
        max_cost_per_run_cents=45,
        model_override_scope="repo",
        settings_version=3,
    )
    fake_audit_db.add(persisted)
    await fake_audit_db.commit()

    client = make_fake_auth_client(user_a.id)
    async with client:
        resp = await client.get(f"/api/repos/{repo_a.id}/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert data["preset"] == "conservative"
        assert data["enable_auto_fix"] is False
        assert data["enable_auditor_mode"] is True
        assert data["allowed_branches"] == ["release", "prod"]
        assert data["min_confidence_threshold"] == 92
        assert data["max_cost_per_run_cents"] == 45
        assert data["model_override_scope"] == "repo"
        assert data["settings_version"] == 3


@pytest.mark.asyncio
async def test_get_settings_repo_not_found(seeded_env, make_fake_auth_client):
    """GET settings for non-existent repo returns 404."""
    user_a, _, _, _ = seeded_env
    client = make_fake_auth_client(user_a.id)

    async with client:
        resp = await client.get(f"/api/repos/{uuid.uuid4()}/settings")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Repo not found"


@pytest.mark.asyncio
async def test_get_settings_cross_user_isolation(seeded_env, make_fake_auth_client):
    """User B cannot see settings for User A's repo (returns 404, not 403)."""
    _, user_b, repo_a, _ = seeded_env
    client_b = make_fake_auth_client(user_b.id)

    async with client_b:
        resp = await client_b.get(f"/api/repos/{repo_a.id}/settings")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Repo not found"


@pytest.mark.asyncio
async def test_get_settings_unauthenticated(seeded_env, client):
    """Unauthenticated access to settings returns 401."""
    _, _, repo_a, _ = seeded_env

    resp = await client.get(f"/api/repos/{repo_a.id}/settings")
    assert resp.status_code == 401


# ===========================================================================
# Endpoint Tests: PATCH /repos/{repo_id}/settings
# ===========================================================================


@pytest.mark.asyncio
async def test_patch_settings_flat_fields(seeded_env, make_fake_auth_client):
    """PATCH updates individual fields and bumps settings_version when trigger semantics change."""
    user_a, _, repo_a, _ = seeded_env
    client = make_fake_auth_client(user_a.id)

    async with client:
        patch_payload = {
            "enable_auto_fix": False,
            "enable_auditor_mode": True,
            "max_cost_per_run_cents": 250,
            "min_confidence_threshold": 88,
            "allowed_branches": ["main", "staging"],
        }
        resp = await client.patch(f"/api/repos/{repo_a.id}/settings", json=patch_payload)
        assert resp.status_code == 200, resp.text
        data = resp.json()

        assert data["enable_auto_fix"] is False
        assert data["enable_auditor_mode"] is True
        assert data["max_cost_per_run_cents"] == 250
        assert data["min_confidence_threshold"] == 88
        assert data["allowed_branches"] == ["main", "staging"]
        assert data["settings_version"] == 2  # bumped due to auditor toggle change


@pytest.mark.asyncio
async def test_patch_settings_nested_future02_payload(seeded_env, make_fake_auth_client):
    """PATCH accepts future02.md Section 2.3 nested payload structure."""
    user_a, _, repo_a, _ = seeded_env
    client = make_fake_auth_client(user_a.id)

    async with client:
        nested_payload = {
            "preset_profile": "custom",
            "features": {
                "auto_fixer": False,
                "auditor_mode": True,
                "ci_sandbox": False,
            },
            "audit_triggers": {
                "on_pr": True,
                "on_ci_failure": False,
            },
            "monitored_branches": ["develop"],
        }
        resp = await client.patch(f"/api/repos/{repo_a.id}/settings", json=nested_payload)
        assert resp.status_code == 200, resp.text
        data = resp.json()

        assert data["preset"] == "custom"
        assert data["enable_auto_fix"] is False
        assert data["enable_auditor_mode"] is True
        assert data["enable_sandbox_verification"] is False
        assert data["audit_trigger_on_pr"] is True
        assert data["audit_trigger_on_ci_failure"] is False
        assert data["allowed_branches"] == ["develop"]


@pytest.mark.asyncio
async def test_patch_settings_with_preset_profile(seeded_env, make_fake_auth_client):
    """PATCH with preset profile applies preset configuration without contradictory flags."""
    user_a, _, repo_a, _ = seeded_env
    client = make_fake_auth_client(user_a.id)

    async with client:
        resp = await client.patch(
            f"/api/repos/{repo_a.id}/settings",
            json={"preset": "conservative"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["preset"] == "conservative"
        assert data["enable_auto_fix"] is False
        assert data["enable_auditor_mode"] is True
        assert data["min_confidence_threshold"] == 90
        assert data["settings_version"] >= 2


@pytest.mark.asyncio
async def test_patch_settings_with_preset_and_explicit_override(seeded_env, make_fake_auth_client):
    """PATCH with preset profile honours explicit granular overrides specified alongside it."""
    user_a, _, repo_a, _ = seeded_env
    client = make_fake_auth_client(user_a.id)

    async with client:
        resp = await client.patch(
            f"/api/repos/{repo_a.id}/settings",
            json={
                "preset": "conservative",
                "enable_auto_fix": True,
                "min_confidence_threshold": 95,
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["preset"] == "conservative"
        assert data["enable_auto_fix"] is True
        assert data["enable_auditor_mode"] is True
        assert data["min_confidence_threshold"] == 95


@pytest.mark.asyncio
async def test_patch_settings_negative_cost_fails(seeded_env, make_fake_auth_client):
    """PATCH with negative cost returns 422 validation error."""
    user_a, _, repo_a, _ = seeded_env
    client = make_fake_auth_client(user_a.id)

    async with client:
        resp = await client.patch(
            f"/api/repos/{repo_a.id}/settings",
            json={"max_cost_per_run_cents": -10},
        )
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_settings_confidence_bounds_fails(seeded_env, make_fake_auth_client):
    """PATCH with out-of-bounds confidence threshold returns 422."""
    user_a, _, repo_a, _ = seeded_env
    client = make_fake_auth_client(user_a.id)

    async with client:
        resp1 = await client.patch(
            f"/api/repos/{repo_a.id}/settings",
            json={"min_confidence_threshold": 105},
        )
        assert resp1.status_code == 422

        resp2 = await client.patch(
            f"/api/repos/{repo_a.id}/settings",
            json={"min_confidence_threshold": -1},
        )
        assert resp2.status_code == 422


@pytest.mark.asyncio
async def test_patch_settings_invalid_preset_fails(seeded_env, make_fake_auth_client):
    """PATCH with unknown preset returns 422."""
    user_a, _, repo_a, _ = seeded_env
    client = make_fake_auth_client(user_a.id)

    async with client:
        resp = await client.patch(
            f"/api/repos/{repo_a.id}/settings",
            json={"preset": "unknown_preset_x"},
        )
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_settings_invalid_branch_name_fails(seeded_env, make_fake_auth_client):
    """PATCH with illegal git branch names returns 422."""
    user_a, _, repo_a, _ = seeded_env
    client = make_fake_auth_client(user_a.id)

    async with client:
        # Cannot contain double-dots or spaces
        resp1 = await client.patch(
            f"/api/repos/{repo_a.id}/settings",
            json={"allowed_branches": ["main", "feature/..branch"]},
        )
        assert resp1.status_code == 422

        resp2 = await client.patch(
            f"/api/repos/{repo_a.id}/settings",
            json={"allowed_branches": ["feat with space"]},
        )
        assert resp2.status_code == 422

        resp3 = await client.patch(
            f"/api/repos/{repo_a.id}/settings",
            json={"allowed_branches": ["feat~1"]},
        )
        assert resp3.status_code == 422


@pytest.mark.asyncio
async def test_patch_settings_cross_user_isolation(seeded_env, make_fake_auth_client):
    """User B cannot patch User A's repo settings (returns 404)."""
    _, user_b, repo_a, _ = seeded_env
    client_b = make_fake_auth_client(user_b.id)

    async with client_b:
        resp = await client_b.patch(
            f"/api/repos/{repo_a.id}/settings",
            json={"enable_auto_fix": False},
        )
        assert resp.status_code == 404


# ===========================================================================
# Endpoint Tests: POST /repos/{repo_id}/settings/preset
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "preset_name,expected_auto_fix,expected_auditor,expected_sandbox,expected_conf",
    [
        ("autonomous", True, False, True, 80),
        ("full_autonomous", True, False, True, 80),
        ("conservative", False, True, True, 90),
        ("standard", True, True, True, 85),
        ("audit_only", False, True, False, 80),
        ("auditor_only", False, True, False, 80),
        ("live_studio_only", False, False, True, 80),
    ],
)
async def test_post_preset_application(
    seeded_env,
    make_fake_auth_client,
    preset_name,
    expected_auto_fix,
    expected_auditor,
    expected_sandbox,
    expected_conf,
):
    """Applying any standard preset sets the exact expected operational flags."""
    user_a, _, repo_a, _ = seeded_env
    client = make_fake_auth_client(user_a.id)

    async with client:
        resp = await client.post(
            f"/api/repos/{repo_a.id}/settings/preset",
            json={"preset": preset_name},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()

        assert data["enable_auto_fix"] == expected_auto_fix
        assert data["enable_auditor_mode"] == expected_auditor
        assert data["enable_sandbox_verification"] == expected_sandbox
        assert data["min_confidence_threshold"] == expected_conf
        assert data["settings_version"] >= 2


@pytest.mark.asyncio
async def test_post_preset_path_parameter(seeded_env, make_fake_auth_client):
    """POST /api/repos/{repo_id}/settings/preset/{preset_name} applies preset via path."""
    user_a, _, repo_a, _ = seeded_env
    client = make_fake_auth_client(user_a.id)

    async with client:
        resp = await client.post(f"/api/repos/{repo_a.id}/settings/preset/conservative")
        assert resp.status_code == 200
        data = resp.json()
        assert data["preset"] == "conservative"
        assert data["enable_auditor_mode"] is True
        assert data["enable_auto_fix"] is False


@pytest.mark.asyncio
async def test_post_preset_invalid_name(seeded_env, make_fake_auth_client):
    """POST preset with invalid preset name returns 422."""
    user_a, _, repo_a, _ = seeded_env
    client = make_fake_auth_client(user_a.id)

    async with client:
        resp = await client.post(
            f"/api/repos/{repo_a.id}/settings/preset",
            json={"preset": "not_a_preset"},
        )
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_preset_cross_user_isolation(seeded_env, make_fake_auth_client):
    """User B cannot apply preset to User A's repo (returns 404)."""
    _, user_b, repo_a, _ = seeded_env
    client_b = make_fake_auth_client(user_b.id)

    async with client_b:
        resp = await client_b.post(
            f"/api/repos/{repo_a.id}/settings/preset",
            json={"preset": "conservative"},
        )
        assert resp.status_code == 404


# ===========================================================================
# Endpoint Tests: GET /api/settings/repos (FUTURE02.md Collection View)
# ===========================================================================


@pytest.mark.asyncio
async def test_list_repos_with_settings(seeded_env, make_fake_auth_client):
    """GET /api/settings/repos returns user's repos with governance feature maps."""
    user_a, _, repo_a, _ = seeded_env
    client = make_fake_auth_client(user_a.id)

    async with client:
        resp = await client.get("/api/settings/repos")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 1
        item = data[0]
        assert item["repo_id"] == str(repo_a.id)
        assert item["repo_full_name"] == "alpha_owner/alpha_repo"
        assert "features" in item
        assert "audit_triggers" in item
        assert "monitored_branches" in item
        assert item["features"]["auto_fixer"] is True
        assert item["features"]["auditor_mode"] is False
        assert "on_manual_mention" in item["audit_triggers"]
        assert item["audit_triggers"]["on_manual_mention"] is True


@pytest.mark.asyncio
async def test_list_repos_with_settings_eager_load_persisted(seeded_env, make_fake_auth_client):
    """GET /api/settings/repos eagerly loads updated persisted settings."""
    user_a, _, repo_a, _ = seeded_env
    client = make_fake_auth_client(user_a.id)

    async with client:
        # Patch repo_a settings to conservative
        patch_resp = await client.patch(
            f"/api/repos/{repo_a.id}/settings",
            json={"preset": "conservative"},
        )
        assert patch_resp.status_code == 200

        # Query collection endpoint
        resp = await client.get("/api/settings/repos")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        item = data[0]
        assert item["preset_profile"] == "conservative"
        assert item["features"]["auto_fixer"] is False
        assert item["features"]["auditor_mode"] is True
        assert item["audit_triggers"]["on_manual_mention"] is True


# ===========================================================================
# Service Layer Direct Unit Tests
# ===========================================================================


def test_service_branch_validation_rules():
    """Branch validation permits standard git branches and wildcard glob patterns, rejecting illegal syntax."""
    # Valid branches (including wildcard patterns like release/* and feature-*)
    assert validate_branch_name("main") == "main"
    assert validate_branch_name("master") == "master"
    assert validate_branch_name("feature/login-oauth") == "feature/login-oauth"
    assert validate_branch_name("v1.2.3") == "v1.2.3"
    assert validate_branch_name("bugfix/issue-42") == "bugfix/issue-42"
    assert validate_branch_name("release/*") == "release/*"
    assert validate_branch_name("feature-*") == "feature-*"
    assert validate_branch_name("release/?") == "release/?"
    assert validate_branch_name("v[0-9]") == "v[0-9]"
    assert validate_branches(["main", "release/v2", "release/*", "feature-*"]) == [
        "main",
        "release/v2",
        "release/*",
        "feature-*",
    ]

    # Invalid branches
    invalid_branches = [
        "",
        "   ",
        "feature/..secret",
        "branch with space",
        "starts/with/slash/",
        "/ends/with/slash",
        "name.lock",
        "branch~1",
        "branch^2",
        "branch:colon",
        "branch@{1}",
        "branch\\backslash",
        "branch\x00null",
        "branch\x1fctrl",
        "branch\x7fdel",
        "\x01leading",
        "a" * 256,
    ]
    for bad in invalid_branches:
        with pytest.raises(ValueError):
            validate_branch_name(bad)


def test_service_bounds_validation():
    """Validates cost bounds and confidence thresholds."""
    assert validate_cost_bounds(0) == 0
    assert validate_cost_bounds(100) == 100
    assert validate_cost_bounds(10_000_000) == 10_000_000

    with pytest.raises(ValueError):
        validate_cost_bounds(-1)
    with pytest.raises(ValueError):
        validate_cost_bounds(10_000_001)

    assert validate_confidence_threshold(0) == 0
    assert validate_confidence_threshold(50) == 50
    assert validate_confidence_threshold(100) == 100

    with pytest.raises(ValueError):
        validate_confidence_threshold(-1)
    with pytest.raises(ValueError):
        validate_confidence_threshold(101)


def test_service_preset_normalization():
    """Preset normalization maps aliases to canonical names and raises on invalid ones."""
    for p in ALLOWED_PRESETS:
        canonical = normalize_preset_name(p)
        assert canonical in {"autonomous", "conservative", "standard", "audit_only", "live_studio_only", "custom"}

    assert normalize_preset_name("full_autonomous") == "autonomous"
    assert normalize_preset_name("auditor_only") == "audit_only"
    assert normalize_preset_name("AUTONOMOUS") == "autonomous"
    assert normalize_preset_name("  Conservative  ") == "conservative"

    with pytest.raises(ValueError, match="Invalid governance preset"):
        normalize_preset_name("god_mode")


def test_service_apply_preset_logic():
    """Applying preset modifies settings and increments settings_version."""
    settings = create_default_repo_settings(uuid.uuid4())
    assert settings.preset == "autonomous"
    assert settings.settings_version == 1

    apply_preset(settings, "conservative")
    assert settings.preset == "conservative"
    assert settings.enable_auto_fix is False
    assert settings.enable_auditor_mode is True
    assert settings.min_confidence_threshold == 90
    assert settings.settings_version == 2

    apply_preset(settings, "audit_only")
    assert settings.preset == "audit_only"
    assert settings.enable_sandbox_verification is False
    assert settings.settings_version == 3


@pytest.mark.asyncio
async def test_service_get_and_update(fake_audit_db):
    """Service layer get_or_create and update_repo_settings work transactionally."""
    repo_id = uuid.uuid4()

    # Get without row returns in-memory default
    s1 = await get_repo_settings(fake_audit_db, repo_id, auto_create=False)
    assert s1.repo_id == repo_id
    assert s1.preset == "autonomous"

    # Update persists to DB
    updated = await update_repo_settings(
        fake_audit_db,
        repo_id,
        {
            "preset": "standard",
            "max_cost_per_run_cents": 500,
            "allowed_branches": ["main", "beta"],
        },
    )
    assert updated.preset == "standard"
    assert updated.max_cost_per_run_cents == 500
    assert updated.allowed_branches == ["main", "beta"]

    # Subsequent fetch finds the row
    fetched = await get_repo_settings(fake_audit_db, repo_id, auto_create=False)
    assert fetched.preset == "standard"
    assert fetched.max_cost_per_run_cents == 500
