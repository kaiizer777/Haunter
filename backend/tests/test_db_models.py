"""
Database schema, model invariants, migrations, and connection pool tests (test_db_models.py).
"""

import hashlib

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import NullPool

from app.config import _to_asyncpg_url
from app.db import engine, engine_unpooled
from app.models import (
    Attempt,
    AuditJob,
    ModelConfig,
    Repo,
    RepoSettings,
    Run,
    RunStep,
    User,
)
from tests.conftest import truncate_all


@pytest.mark.asyncio
async def test_alembic_head_and_migration_check():
    """Verify Alembic migration script directory has a valid head revision."""
    cfg = Config("alembic.ini")
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1, f"Expected exactly 1 migration head, found {heads}"
    assert heads[0] == "a1b2c3d4e5f7"
    assert script.get_revision("a4b7c9d2e6f1").down_revision == "f2a9c4e7b1d3"
    assert script.get_revision("b6d8f0a2c4e6").down_revision == "a4b7c9d2e6f1"
    assert script.get_revision("c7a1b2c3d4e5").down_revision == "b6d8f0a2c4e6"
    assert script.get_revision("d8e9f0a1b2c3").down_revision == "c7a1b2c3d4e5"
    assert script.get_revision("e9d1c2b3a405").down_revision == "d8e9f0a1b2c3"
    assert script.get_revision("f3a4b5c6d7e8").down_revision == "e9d1c2b3a405"
    assert script.get_revision("a1b2c3d4e5f7").down_revision == "f3a4b5c6d7e8"


def test_to_asyncpg_url_variants():
    """Test URL parsing and driver/parameter transformation for asyncpg."""
    # Case 1: Standard postgresql URL with sslmode stripped
    url1 = "postgresql://user:pass@ep-cool.neon.tech/neondb?sslmode=require"
    converted1 = _to_asyncpg_url(url1)
    assert converted1.startswith("postgresql+asyncpg://")
    assert "sslmode" not in converted1

    # Case 2: Neon pooled URL with sslmode and channel_binding stripped
    url2 = "postgresql://user:pass@ep-cool-pooler.neon.tech/neondb?sslmode=require&channel_binding=require&client_encoding=utf8"
    converted2 = _to_asyncpg_url(url2)
    assert converted2.startswith("postgresql+asyncpg://")
    assert "sslmode" not in converted2
    assert "channel_binding" not in converted2
    assert "client_encoding=utf8" in converted2

    # Case 3: Already asyncpg url
    url3 = "postgresql+asyncpg://localhost:5432/haunter_db"
    converted3 = _to_asyncpg_url(url3)
    assert converted3 == url3


def test_nullpool_on_both_engines():
    """Assert both pooled and unpooled engines use NullPool for Neon compatibility."""
    assert isinstance(engine.pool, NullPool), "Runtime engine must use NullPool"
    assert isinstance(
        engine_unpooled.pool, NullPool
    ), "Unpooled migration engine must use NullPool"


@pytest.mark.asyncio
async def test_users_github_id_unique_constraint(db: AsyncSession):
    """Assert duplicate users.github_id raises IntegrityError."""
    await truncate_all(db)
    gh_id = 9988776655

    u1 = User(github_id=gh_id, github_username="user_alpha", access_token="tok_1")
    db.add(u1)
    await db.commit()

    u2 = User(github_id=gh_id, github_username="user_beta", access_token="tok_2")
    db.add(u2)
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


@pytest.mark.asyncio
async def test_repos_user_owner_name_unique_per_user_cross_user_allowed(
    db: AsyncSession,
):
    """
    Assert (user_id, owner, name) uniqueness:
    - Same user duplicate repo fails with IntegrityError.
    - Two different users can track the same owner/name independently.
    """
    await truncate_all(db)

    u1 = User(github_id=901, github_username="tenant_a", access_token="t1")
    u2 = User(github_id=902, github_username="tenant_b", access_token="t2")
    db.add_all([u1, u2])
    await db.commit()
    await db.refresh(u1)
    await db.refresh(u2)
    u1_id = u1.id
    u2_id = u2.id

    # 1. User 1 adds repo
    r1 = Repo(user_id=u1_id, owner="octocat", name="spoon-knife")
    db.add(r1)
    await db.commit()
    await db.refresh(r1)
    r1_id = r1.id

    # 2. User 1 adds same repo -> IntegrityError
    r1_dup = Repo(user_id=u1_id, owner="octocat", name="spoon-knife")
    db.add(r1_dup)
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()

    # 3. User 2 adds identical owner/name -> Success
    r2 = Repo(user_id=u2_id, owner="octocat", name="spoon-knife")
    db.add(r2)
    await db.commit()
    await db.refresh(r2)
    assert r2.id != r1_id


@pytest.mark.asyncio
async def test_fk_cascade_delete_user_repos_runs(db: AsyncSession):
    """Assert deleting a User cascades to delete Repos, Runs, RunSteps, and Attempts."""
    await truncate_all(db)

    user = User(github_id=903, github_username="cascade_user", access_token="t3")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    user_id = user.id

    repo = Repo(user_id=user_id, owner="org", name="repo")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)
    repo_id = repo.id

    run = Run(
        repo_id=repo_id,
        github_run_id=554433,
        github_delivery_id="deliv-cascade-1",
        head_sha="0123456789abcdef0123456789abcdef01234567",
        head_branch="main",
        status="pending",
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    run_id = run.id

    step = RunStep(
        run_id=run_id, step_name="context_gatherer", input_tokens=10, output_tokens=20
    )
    attempt = Attempt(run_id=run_id, attempt_number=1, patch_text="diff --git ...")
    db.add_all([step, attempt])
    await db.commit()

    # Delete the root user via SQL-level delete to test DB ON DELETE CASCADE
    from sqlalchemy import delete as sa_delete

    await db.execute(sa_delete(User).where(User.id == user_id))
    await db.commit()

    # Verify all children were deleted
    assert (
        await db.execute(select(Repo).where(Repo.id == repo_id))
    ).scalar_one_or_none() is None
    assert (
        await db.execute(select(Run).where(Run.id == run_id))
    ).scalar_one_or_none() is None
    assert (
        await db.execute(select(RunStep).where(RunStep.run_id == run_id))
    ).scalars().all() == []
    assert (
        await db.execute(select(Attempt).where(Attempt.run_id == run_id))
    ).scalars().all() == []


@pytest.mark.asyncio
async def test_runs_github_delivery_id_unique(db: AsyncSession):
    """Assert github_delivery_id uniqueness constraint and nullable behavior."""
    await truncate_all(db)

    user = User(github_id=904, github_username="run_user", access_token="t4")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    user_id = user.id

    repo = Repo(user_id=user_id, owner="org", name="repo")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)
    repo_id = repo.id

    # 1. Unique delivery ID
    run1 = Run(
        repo_id=repo_id,
        github_run_id=1001,
        github_delivery_id="deliv-unique-999",
        head_sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        head_branch="main",
        status="pending",
    )
    db.add(run1)
    await db.commit()

    run2 = Run(
        repo_id=repo_id,
        github_run_id=1002,
        github_delivery_id="deliv-unique-999",  # duplicate delivery id
        head_sha="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        head_branch="main",
        status="pending",
    )
    db.add(run2)
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()

    # 2. Null delivery ID allowed multiple times
    run_null_1 = Run(
        repo_id=repo_id,
        github_run_id=1003,
        github_delivery_id=None,
        head_sha="cccccccccccccccccccccccccccccccccccccccc",
        head_branch="main",
        status="pending",
    )
    run_null_2 = Run(
        repo_id=repo_id,
        github_run_id=1004,
        github_delivery_id=None,
        head_sha="dddddddddddddddddddddddddddddddddddddddd",
        head_branch="main",
        status="pending",
    )
    db.add_all([run_null_1, run_null_2])
    await db.commit()


@pytest.mark.asyncio
async def test_timestamps_timezone_aware(db: AsyncSession):
    """Assert created_at and updated_at timestamps on models are timezone-aware."""
    await truncate_all(db)

    user = User(github_id=905, github_username="tz_user", access_token="t5")
    db.add(user)
    await db.commit()
    await db.refresh(user)

    assert user.created_at is not None
    assert user.created_at.tzinfo is not None
    assert user.updated_at is not None
    assert user.updated_at.tzinfo is not None


@pytest.mark.asyncio
async def test_model_configs_defaults(db: AsyncSession):
    """Assert default values on ModelConfig model."""
    await truncate_all(db)

    cfg = ModelConfig(provider="opencode_zen")
    db.add(cfg)
    await db.commit()
    await db.refresh(cfg)

    assert cfg.model_name == "nemotron-3.5-lightning-free"
    assert cfg.base_url == "https://opencode.ai/zen/v1"
    assert cfg.is_active is True
    assert cfg.scope == "global"
    assert cfg.created_at.tzinfo is not None


@pytest.mark.asyncio
async def test_repo_settings_safe_defaults_and_unique_repo(
    db: AsyncSession,
):
    await truncate_all(db)
    user = User(github_id=906, github_username="auditor_settings", access_token="t6")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    repo = Repo(
        user_id=user.id,
        owner="settings-org",
        name="settings-repo",
        auditor_github_install_id=987654,
    )
    db.add(repo)
    await db.commit()
    await db.refresh(repo)

    settings_row = RepoSettings(repo_id=repo.id)
    db.add(settings_row)
    await db.commit()
    await db.refresh(settings_row)

    assert settings_row.enable_auditor_mode is False
    assert settings_row.audit_trigger_on_pr is True
    assert settings_row.audit_trigger_on_ci_failure is True
    assert settings_row.audit_trigger_on_ci_success is False
    assert settings_row.audit_trigger_on_manual_mention is True
    assert repo.auditor_github_install_id == 987654

    db.add(RepoSettings(repo_id=repo.id))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


@pytest.mark.asyncio
async def test_audit_job_has_recoverable_dispatch_defaults(
    db: AsyncSession,
):
    await truncate_all(db)
    user = User(github_id=907, github_username="audit_outbox", access_token="t7")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    repo = Repo(user_id=user.id, owner="outbox-org", name="outbox-repo")
    db.add(repo)
    await db.commit()
    await db.refresh(repo)

    job = AuditJob(
        audit_id="audit-dddddddddddd",
        repo_id=repo.id,
        delivery_id="delivery-outbox-defaults",
        # `delivery_fingerprint` is NOT NULL by schema: a delivery id alone is
        # not an idempotency key, so the payload digest is part of the row's
        # identity and has no default to fall back on.
        delivery_fingerprint=hashlib.sha256(b"outbox-defaults-payload").hexdigest(),
        audit_type="pr_audit",
        base_sha="b" * 40,
        status="queued",
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    assert job.attempts == 0
    assert job.dispatch_attempts == 0
    assert job.base_sha == "b" * 40
    assert len(job.delivery_fingerprint) == 64
    assert job.next_attempt_at is not None
    assert job.next_attempt_at.tzinfo is not None
    assert job.lease_expires_at is None
    assert job.last_error is None

    job.status = "skipped_no_diff"
    await db.commit()
    await db.refresh(job)
    assert job.status == "skipped_no_diff"
