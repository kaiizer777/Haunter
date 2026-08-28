"""
Creates rich test data in Neon DB including multiple repos, runs, traces, attempts,
and XSS payload spot-checks to verify frontend anti-slop dashboard views and security invariants.
"""

import asyncio
import uuid
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from app.db import async_session_maker
from app.models import User, Repo, Run, RunStep, Attempt
from app.auth import _sign_user_id

async def main():
    async with async_session_maker() as db:
        # 1. User
        user_id = uuid.uuid4()
        user_res = await db.execute(select(User).where(User.github_username == "saif-dev"))
        user = user_res.scalar_one_or_none()
        if not user:
            user = User(
                id=user_id,
                github_id=99887766,
                github_username="saif-dev",
                avatar_url="https://avatars.githubusercontent.com/u/583231?v=4",
            )
            db.add(user)
            await db.commit()
            print(f"Created user: {user.github_username} (ID: {user.id})")
        else:
            print(f"Using user: {user.github_username} (ID: {user.id})")

        # 2. Repos
        repo1_id = uuid.uuid4()
        repo1_res = await db.execute(select(Repo).where(Repo.user_id == user.id, Repo.owner == "saif-org", Repo.name == "haunter-demo"))
        repo1 = repo1_res.scalar_one_or_none()
        if not repo1:
            repo1 = Repo(
                id=repo1_id,
                user_id=user.id,
                owner="saif-org",
                name="haunter-demo",
                default_branch="main",
                language_hint="python",
            )
            db.add(repo1)

        repo2_id = uuid.uuid4()
        repo2_res = await db.execute(select(Repo).where(Repo.user_id == user.id, Repo.owner == "saif-org", Repo.name == "api-gateway"))
        repo2 = repo2_res.scalar_one_or_none()
        if not repo2:
            repo2 = Repo(
                id=repo2_id,
                user_id=user.id,
                owner="saif-org",
                name="api-gateway",
                default_branch="main",
                language_hint="typescript",
            )
            db.add(repo2)

        await db.commit()
        if not repo1:
            repo1_res = await db.execute(select(Repo).where(Repo.user_id == user.id, Repo.owner == "saif-org", Repo.name == "haunter-demo"))
            repo1 = repo1_res.scalar_one()
        if not repo2:
            repo2_res = await db.execute(select(Repo).where(Repo.user_id == user.id, Repo.owner == "saif-org", Repo.name == "api-gateway"))
            repo2 = repo2_res.scalar_one()

        print(f"Repos ready: {repo1.owner}/{repo1.name} and {repo2.owner}/{repo2.name}")

        # 3. Create Sample Runs

        # Run 1: Completed / PR Opened
        run1_id = uuid.uuid4()
        run1 = Run(
            id=run1_id,
            repo_id=repo1.id,
            github_run_id=int(uuid.uuid4().int % 1_000_000_000),
            github_delivery_id=str(uuid.uuid4()),
            head_sha="7f8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b",
            head_branch="main",
            status="completed",
            conclusion="success",
            diagnosis_summary="ModuleNotFoundError: No module named 'pydantic_settings' in backend/app/config.py:12. Root cause is a missing requirement in requirements.txt during workflow execution.",
            pr_url="https://github.com/saif-org/haunter-demo/pull/42",
            pr_number=42,
            pr_branch="haunter/fix-pydantic-settings",
        )
        db.add(run1)

        # Run 1 Steps
        s1 = RunStep(id=uuid.uuid4(), run_id=run1_id, step_name="context_gatherer", input_tokens=1420, output_tokens=310, latency_ms=1240, cost_estimate=0.00034)
        s2 = RunStep(id=uuid.uuid4(), run_id=run1_id, step_name="fix_generator", input_tokens=2180, output_tokens=490, latency_ms=1890, cost_estimate=0.00078)
        s3 = RunStep(id=uuid.uuid4(), run_id=run1_id, step_name="sandbox_verifier", input_tokens=0, output_tokens=0, latency_ms=14200, cost_estimate=0.00000)
        s4 = RunStep(id=uuid.uuid4(), run_id=run1_id, step_name="pr_writer", input_tokens=850, output_tokens=220, latency_ms=980, cost_estimate=0.00021)
        db.add_all([s1, s2, s3, s4])

        # Run 1 Attempt
        att1 = Attempt(
            id=uuid.uuid4(),
            run_id=run1_id,
            attempt_number=1,
            patch_text="""--- a/requirements.txt
+++ b/requirements.txt
@@ -3,3 +3,4 @@
 fastapi>=0.110.0
 uvicorn>=0.28.0
 sqlalchemy>=2.0.0
+pydantic-settings>=2.2.0""",
            confidence_score=94,
            verification_status="pass",
            build_duration_ms=14200,
        )
        db.add(att1)

        # Run 2: Fallback Run with 2 failed attempts
        run2_id = uuid.uuid4()
        run2 = Run(
            id=run2_id,
            repo_id=repo2.id,
            github_run_id=int(uuid.uuid4().int % 1_000_000_000),
            github_delivery_id=str(uuid.uuid4()),
            head_sha="9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a0b",
            head_branch="feature/auth-refactor",
            status="fallback",
            conclusion="failure",
            diagnosis_summary="TypeError: Expected AsyncSession but received Session. Database driver mismatch in async handler.",
            final_summary="Attempted 2 fix iterations but unit tests in sandbox still failed due to schema constraint violations.",
        )
        db.add(run2)

        s2_1 = RunStep(id=uuid.uuid4(), run_id=run2_id, step_name="context_gatherer", input_tokens=1800, output_tokens=420, latency_ms=1600, cost_estimate=0.00045)
        s2_2 = RunStep(id=uuid.uuid4(), run_id=run2_id, step_name="fix_generator", input_tokens=3100, output_tokens=650, latency_ms=2400, cost_estimate=0.00110)
        db.add_all([s2_1, s2_2])

        att2_1 = Attempt(
            id=uuid.uuid4(),
            run_id=run2_id,
            attempt_number=1,
            patch_text="""--- a/src/db.ts
+++ b/src/db.ts
@@ -10,3 +10,3 @@
-export const session = createSession();
+export const session = createAsyncSession();""",
            confidence_score=68,
            verification_status="fail",
            failure_reason="AssertionError: 3 test cases failed in tests/db.test.ts:44",
            build_duration_ms=11500,
        )
        att2_2 = Attempt(
            id=uuid.uuid4(),
            run_id=run2_id,
            attempt_number=2,
            patch_text="""--- a/src/db.ts
+++ b/src/db.ts
@@ -10,3 +10,5 @@
-export const session = createSession();
+export const session = createAsyncSession({
+  poolSize: 10
+});""",
            confidence_score=52,
            verification_status="fail",
            failure_reason="AssertionError: Connection timeout in tests/db.test.ts:88",
            build_duration_ms=12100,
        )
        db.add_all([att2_1, att2_2])

        # Run 3: XSS Injection Spot-Check Fixture
        run3_id = uuid.uuid4()
        run3 = Run(
            id=run3_id,
            repo_id=repo1.id,
            github_run_id=int(uuid.uuid4().int % 1_000_000_000),
            github_delivery_id=str(uuid.uuid4()),
            head_sha="00112233445566778899aabbccddeeff00112233",
            head_branch="fix/security-check",
            status="completed",
            conclusion="success",
            diagnosis_summary="<script>alert('XSS-TEST-DIAGNOSIS')</script><img src=x onerror=alert('XSS-IMG')>",
            pr_url="https://github.com/saif-org/haunter-demo/pull/99",
            pr_number=99,
            pr_branch="haunter/xss-test",
        )
        db.add(run3)

        s3_1 = RunStep(id=uuid.uuid4(), run_id=run3_id, step_name="context_gatherer", input_tokens=1000, output_tokens=200, latency_ms=900, cost_estimate=0.00020)
        db.add(s3_1)

        att3_1 = Attempt(
            id=uuid.uuid4(),
            run_id=run3_id,
            attempt_number=1,
            patch_text="""--- a/test.py
+++ b/test.py
@@ -1,2 +1,3 @@
+# <script>alert("XSS-IN-PATCH")</script>
+print("Safe plain-text rendering")""",
            confidence_score=99,
            verification_status="pass",
            build_duration_ms=8000,
        )
        db.add(att3_1)

        await db.commit()

        # Generate signed session cookie
        signed_cookie = _sign_user_id(user.id)
        print(f"\n==========================================")
        print(f"DEV_SESSION_COOKIE: {signed_cookie}")
        print(f"RUN_1 (Completed): {run1_id}")
        print(f"RUN_2 (Fallback):  {run2_id}")
        print(f"RUN_3 (XSS Spot):  {run3_id}")
        print(f"==========================================\n")

if __name__ == "__main__":
    asyncio.run(main())
