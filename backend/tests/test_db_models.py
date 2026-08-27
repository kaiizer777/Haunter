"""
Database schema, model invariants, migrations, and connection pool tests (test_db_models.py).
"""

from datetime import datetime, timezone
import uuid
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import NullPool

from app.config import _to_asyncpg_url
from app.db import engine, engine_unpooled
from app.models import Attempt, ModelConfig, Repo, Run, RunStep, User
from tests.conftest import truncate_all


@pytest.mark.asyncio
async def test_alembic_head_and_migration_check():
    """Verify Alembic migration script directory has a valid head revision."""
    cfg = Config("alembic.ini")
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1, f"Expected exactly 1 migration head, found {heads}"
    assert heads[0] is not None


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
    assert isinstance(engine_unpooled.pool, NullPool), "Unpooled migration engine must use NullPool"


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
async def test_repos_user_owner_name_unique_per_user_cross_user_allowed(db: AsyncSession):
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

    step = RunStep(run_id=run_id, step_name="context_gatherer", input_tokens=10, output_tokens=20)
    attempt = Attempt(run_id=run_id, attempt_number=1, patch_text="diff --git ...")
    db.add_all([step, attempt])
    await db.commit()

    # Delete the root user via SQL-level delete to test DB ON DELETE CASCADE
    from sqlalchemy import delete as sa_delete
    await db.execute(sa_delete(User).where(User.id == user_id))
    await db.commit()

    # Verify all children were deleted
    assert (await db.execute(select(Repo).where(Repo.id == repo_id))).scalar_one_or_none() is None
    assert (await db.execute(select(Run).where(Run.id == run_id))).scalar_one_or_none() is None
    assert (await db.execute(select(RunStep).where(RunStep.run_id == run_id))).scalars().all() == []
    assert (await db.execute(select(Attempt).where(Attempt.run_id == run_id))).scalars().all() == []


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
    assert cfg.created_at.tzinfo is not None
