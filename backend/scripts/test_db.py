import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import asyncio
import uuid
from app.db import get_db
from app.models import Repo, Run, RunStep

async def main():
    print("Testing DB insert and query...")
    async for session in get_db():
        try:
            # 1. Create a repo
            repo = Repo(
                owner="test-owner",
                name="test-repo",
                default_branch="main",
            )
            session.add(repo)
            await session.commit()
            print(f"Created Repo: {repo.id}")
            
            # 2. Create a run
            run = Run(
                repo_id=repo.id,
                github_run_id=123456,
                head_sha="abcdef",
                head_branch="main",
                status="in_progress",
            )
            session.add(run)
            await session.commit()
            print(f"Created Run: {run.id}")
            
            # 3. Create a run step
            step = RunStep(
                run_id=run.id,
                step_name="test_step",
                input_tokens=100,
                output_tokens=50,
            )
            session.add(step)
            await session.commit()
            print(f"Created RunStep: {step.id}")
            
            # 4. Query back
            from sqlalchemy import select
            stmt = select(Repo).where(Repo.owner == "test-owner")
            result = await session.execute(stmt)
            fetched_repo = result.scalars().first()
            print(f"Fetched Repo: {fetched_repo.owner}/{fetched_repo.name}")
            
            stmt2 = select(Run).where(Run.repo_id == fetched_repo.id)
            result2 = await session.execute(stmt2)
            fetched_run = result2.scalars().first()
            print(f"Fetched Run: github_run_id={fetched_run.github_run_id}, status={fetched_run.status}")
            
            stmt3 = select(RunStep).where(RunStep.run_id == fetched_run.id)
            result3 = await session.execute(stmt3)
            fetched_step = result3.scalars().first()
            print(f"Fetched RunStep: {fetched_step.step_name}, in={fetched_step.input_tokens}, out={fetched_step.output_tokens}")
            
            # Cleanup
            print("Cleaning up...")
            await session.delete(fetched_repo)
            await session.commit()
            print("Cleanup done.")
            
        except Exception as e:
            print(f"Error: {e}")
            await session.rollback()
            raise

if __name__ == "__main__":
    asyncio.run(main())
