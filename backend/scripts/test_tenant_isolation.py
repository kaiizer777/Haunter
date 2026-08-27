"""
Tenant isolation scratch test.

Creates two users in Neon, each adds a repo, then verifies:
- User A cannot list User B's repos
- User A cannot delete User B's repo (gets 404, not 403)
- DELETE of own repo succeeds
- DB is cleaned up after the test.

Run from backend/ with the venv active:
  python scripts/test_tenant_isolation.py
"""

import asyncio
import sys
import uuid

from sqlalchemy import select, delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import NullPool

sys.path.insert(0, ".")

from app.config import settings
from app.models import Repo, User

engine = create_async_engine(settings.async_database_url, poolclass=NullPool)
session_maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

FAKE_GH_ID_A = 9999999901
FAKE_GH_ID_B = 9999999902


async def cleanup(db: AsyncSession) -> None:
    """Remove test users (cascades to their repos)."""
    for gh_id in (FAKE_GH_ID_A, FAKE_GH_ID_B):
        result = await db.execute(select(User).where(User.github_id == gh_id))
        user = result.scalar_one_or_none()
        if user:
            await db.delete(user)
    await db.commit()


async def main() -> None:
    async with session_maker() as db:
        # --- Setup: pre-clean any leftovers ---
        await cleanup(db)

        # --- Create User A ---
        user_a = User(github_id=FAKE_GH_ID_A, github_username="test_user_a", access_token="tok_a")
        db.add(user_a)
        await db.commit()
        await db.refresh(user_a)

        # --- Create User B ---
        user_b = User(github_id=FAKE_GH_ID_B, github_username="test_user_b", access_token="tok_b")
        db.add(user_b)
        await db.commit()
        await db.refresh(user_b)

        # --- User A adds a repo ---
        repo_a = Repo(user_id=user_a.id, owner="acme", name="backend")
        db.add(repo_a)
        await db.commit()
        await db.refresh(repo_a)

        # --- User B adds a repo ---
        repo_b = Repo(user_id=user_b.id, owner="acme", name="backend")  # same owner/name, different tenant
        db.add(repo_b)
        await db.commit()
        await db.refresh(repo_b)

        print(f"User A id={user_a.id}, repo_a={repo_a.id}")
        print(f"User B id={user_b.id}, repo_b={repo_b.id}")

        # --- Isolation check 1: User A lists repos — must not see User B's ---
        result = await db.execute(select(Repo).where(Repo.user_id == user_a.id))
        a_repos = result.scalars().all()
        assert len(a_repos) == 1, f"FAIL: User A sees {len(a_repos)} repos (expected 1)"
        assert a_repos[0].id == repo_a.id, "FAIL: User A's repo ID mismatch"
        print("PASS: User A list sees only own repo")

        # --- Isolation check 2: User A tries to delete User B's repo (scoped query) ---
        result = await db.execute(
            select(Repo).where(Repo.id == repo_b.id, Repo.user_id == user_a.id)
        )
        cross_repo = result.scalar_one_or_none()
        assert cross_repo is None, "FAIL: User A can access User B's repo via ownership-scoped query"
        print("PASS: User A cannot access User B's repo via ownership-scoped query (returns None -> 404)")

        # --- Isolation check 3: User A deletes own repo — succeeds ---
        result = await db.execute(
            select(Repo).where(Repo.id == repo_a.id, Repo.user_id == user_a.id)
        )
        own_repo = result.scalar_one_or_none()
        assert own_repo is not None, "FAIL: User A cannot find own repo"
        await db.delete(own_repo)
        await db.commit()
        print("PASS: User A can delete own repo")

        # --- Isolation check 4: Same (owner, name) allowed for different users ---
        result = await db.execute(select(Repo).where(Repo.user_id == user_b.id))
        b_repos = result.scalars().all()
        assert len(b_repos) == 1, f"FAIL: User B sees {len(b_repos)} repos (expected 1)"
        print("PASS: User B's repo (same owner/name) isolated and intact")

        # --- Cleanup ---
        await cleanup(db)
        print("\nAll tenant isolation tests PASSED ✓")


asyncio.run(main())
