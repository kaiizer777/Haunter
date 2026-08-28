"""
One-off script to restore kaiizer777's repo connection after test suite
wiped prod data with truncate_all(). Run from backend/ with:
    python -m scripts.restore_repos
"""
import asyncio
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select, delete
from app.db import async_session_maker
from app.models import User, Repo, Run


async def main() -> None:
    async with async_session_maker() as s:
        # 1. Nuke leftover test dummy rows (user_a/user_b created by truncate-less tests)
        dummy_usernames = ["user_a", "user_b", "test-user"]
        for uname in dummy_usernames:
            u = (await s.execute(select(User).where(User.github_username == uname))).scalar_one_or_none()
            if u:
                await s.delete(u)
                print(f"Deleted dummy user: {uname}")

        # Dummy repos with nonsense owner/name
        for owner, name in [("a", "repo"), ("b", "repo")]:
            r = (await s.execute(
                select(Repo).where(Repo.owner == owner, Repo.name == name)
            )).scalar_one_or_none()
            if r:
                await s.delete(r)
                print(f"Deleted dummy repo: {owner}/{name}")

        await s.commit()

        # 2. Find real user
        user = (await s.execute(
            select(User).where(User.github_username == "kaiizer777")
        )).scalar_one_or_none()

        if not user:
            print("ERROR: kaiizer777 user not found in DB — log in via the dashboard to re-create it.")
            return

        print(f"Found user: {user.github_username} (id={user.id})")

        # 3. Reconnect UpGrade repo
        existing = (await s.execute(
            select(Repo).where(Repo.user_id == user.id, Repo.owner == "kaiizer777", Repo.name == "UpGrade")
        )).scalar_one_or_none()

        if existing:
            print(f"UpGrade repo already connected (id={existing.id})")
        else:
            repo = Repo(
                user_id=user.id,
                owner="kaiizer777",
                name="UpGrade",
                default_branch="main",
            )
            s.add(repo)
            await s.commit()
            print(f"Reconnected UpGrade repo: kaiizer777/UpGrade (id={repo.id})")

        # 4. Summary
        repos = (await s.execute(select(Repo).where(Repo.user_id == user.id))).scalars().all()
        runs = (await s.execute(select(Run))).scalars().all()
        print(f"\nFinal state: {len(repos)} repo(s), {len(runs)} run(s) in DB")
        for r in repos:
            print(f"  Repo: {r.owner}/{r.name} (id={r.id})")


if __name__ == "__main__":
    asyncio.run(main())
