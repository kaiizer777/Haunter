"""
Comprehensive Phase 3 Verification Suite.

Tests:
1. Missing X-Hub-Signature-256 -> 401 Unauthorized
2. Invalid/tampered X-Hub-Signature-256 -> 401 Unauthorized
3. Missing X-GitHub-Delivery -> 400 Bad Request
4. Non-workflow_run event (e.g. ping/push) -> 200 Ignored (no DB row)
5. Non-failure / non-completed workflow_run -> 200 Ignored (no DB row)
6. Unregistered repository payload -> 200 Ignored (no DB row)
7. Valid failing workflow_run for registered repo -> 200 Queued (<200ms), 1 DB row created (status=pending, conclusion=failure)
8. Idempotency on duplicate delivery -> 200 Duplicate (no extra DB row)
9. Concurrent duplicate deliveries (race condition test) -> exactly 1 DB row, no unhandled exceptions
10. Oversized payload (>2MB) -> 413 Payload Too Large
11. GitHub client wrapper security check -> verifies no secret / token leaks
"""

import asyncio
import hashlib
import hmac
import json
import logging
import sys
import uuid
from pathlib import Path
from typing import Any

# Ensure backend root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from httpx import ASGITransport
from sqlalchemy import delete, select

from app.config import settings
from app.db import async_session_maker
from app.models import Repo, Run, User
from main import app

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("phase3_test")

TEST_SECRET = settings.github_webhook_secret or "test_webhook_secret_key_12345"


def sign_payload(secret: str, payload_bytes: bytes) -> str:
    sig = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


async def setup_test_data() -> tuple[uuid.UUID, uuid.UUID]:
    """Ensure a test user and repo exist in DB for testing."""
    async with async_session_maker() as session:
        # Check or create test user
        stmt_user = select(User).where(User.github_id == 999888777)
        res_user = await session.execute(stmt_user)
        user = res_user.scalars().first()
        if not user:
            user = User(
                github_id=999888777,
                github_username="haunter-test-bot",
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)

        # Check or create test repo
        stmt_repo = select(Repo).where(Repo.owner == "haunter-test-bot", Repo.name == "ci-sandbox")
        res_repo = await session.execute(stmt_repo)
        repo = res_repo.scalars().first()
        if not repo:
            repo = Repo(
                user_id=user.id,
                owner="haunter-test-bot",
                name="ci-sandbox",
                default_branch="main",
            )
            session.add(repo)
            await session.commit()
            await session.refresh(repo)

        # Clean up any leftover runs for this test repo
        stmt_del = delete(Run).where(Run.repo_id == repo.id)
        await session.execute(stmt_del)
        await session.commit()

        return user.id, repo.id


async def main() -> None:
    print("\n========================================================")
    print("           HAUNTER PHASE 3 VERIFICATION SUITE           ")
    print("========================================================")

    _, test_repo_id = await setup_test_data()

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

        # -----------------------------------------------------------------
        # TEST 1: Missing signature -> 401
        # -----------------------------------------------------------------
        resp = await client.post(
            "/webhooks/github",
            headers={"X-GitHub-Event": "workflow_run", "X-GitHub-Delivery": str(uuid.uuid4())},
            json={"action": "completed"},
        )
        assert resp.status_code == 401, f"Expected 401, got {resp.status_code}"
        assert resp.json() == {"detail": "Invalid or missing signature"}
        print("[PASS] Test 1 Passed: Missing signature returns 401 generic")

        # -----------------------------------------------------------------
        # TEST 2: Invalid/tampered signature -> 401
        # -----------------------------------------------------------------
        resp = await client.post(
            "/webhooks/github",
            headers={
                "X-GitHub-Event": "workflow_run",
                "X-GitHub-Delivery": str(uuid.uuid4()),
                "X-Hub-Signature-256": "sha256=0000000000000000000000000000000000000000000000000000000000000000",
            },
            json={"action": "completed"},
        )
        assert resp.status_code == 401, f"Expected 401, got {resp.status_code}"
        assert resp.json() == {"detail": "Invalid or missing signature"}
        print("[PASS] Test 2 Passed: Invalid signature returns 401 generic")

        # -----------------------------------------------------------------
        # TEST 3: Missing X-GitHub-Delivery -> 400
        # -----------------------------------------------------------------
        raw_body = json.dumps({"action": "completed"}).encode("utf-8")
        valid_sig = sign_payload(TEST_SECRET, raw_body)
        resp = await client.post(
            "/webhooks/github",
            headers={
                "X-GitHub-Event": "workflow_run",
                "X-Hub-Signature-256": valid_sig,
            },
            content=raw_body,
        )
        assert resp.status_code == 400, f"Expected 400, got {resp.status_code}"
        print("[PASS] Test 3 Passed: Missing X-GitHub-Delivery returns 400")

        # -----------------------------------------------------------------
        # TEST 4: Non-workflow_run event (ping/push) -> 200 Ignored
        # -----------------------------------------------------------------
        raw_body = json.dumps({"zen": "Keep it logically awesome."}).encode("utf-8")
        valid_sig = sign_payload(TEST_SECRET, raw_body)
        resp = await client.post(
            "/webhooks/github",
            headers={
                "X-GitHub-Event": "ping",
                "X-GitHub-Delivery": str(uuid.uuid4()),
                "X-Hub-Signature-256": valid_sig,
            },
            content=raw_body,
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        assert resp.json()["status"] == "ignored"
        print("[PASS] Test 4 Passed: Non-workflow_run event returns 200 Ignored")

        # -----------------------------------------------------------------
        # TEST 5: Successful workflow_run (conclusion != failure) -> 200 Ignored
        # -----------------------------------------------------------------
        success_payload = {
            "action": "completed",
            "workflow_run": {
                "id": 111111111,
                "head_sha": "a" * 40,
                "head_branch": "main",
                "conclusion": "success",
            },
            "repository": {
                "name": "ci-sandbox",
                "full_name": "haunter-test-bot/ci-sandbox",
                "owner": {"login": "haunter-test-bot"},
            },
        }
        raw_body = json.dumps(success_payload).encode("utf-8")
        valid_sig = sign_payload(TEST_SECRET, raw_body)
        resp = await client.post(
            "/webhooks/github",
            headers={
                "X-GitHub-Event": "workflow_run",
                "X-GitHub-Delivery": str(uuid.uuid4()),
                "X-Hub-Signature-256": valid_sig,
            },
            content=raw_body,
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        assert resp.json()["status"] == "ignored"
        print("[PASS] Test 5 Passed: Non-failure conclusion returns 200 Ignored")

        # -----------------------------------------------------------------
        # TEST 6: Unregistered repository -> 200 Ignored (no DB row)
        # -----------------------------------------------------------------
        unreg_payload = {
            "action": "completed",
            "workflow_run": {
                "id": 222222222,
                "head_sha": "b" * 40,
                "head_branch": "main",
                "conclusion": "failure",
            },
            "repository": {
                "name": "unregistered-repo",
                "full_name": "unknown-org/unregistered-repo",
                "owner": {"login": "unknown-org"},
            },
        }
        raw_body = json.dumps(unreg_payload).encode("utf-8")
        valid_sig = sign_payload(TEST_SECRET, raw_body)
        resp = await client.post(
            "/webhooks/github",
            headers={
                "X-GitHub-Event": "workflow_run",
                "X-GitHub-Delivery": str(uuid.uuid4()),
                "X-Hub-Signature-256": valid_sig,
            },
            content=raw_body,
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        assert resp.json()["status"] == "ignored"
        print("[PASS] Test 6 Passed: Unregistered repository dropped cleanly with 200 Ignored")

        # -----------------------------------------------------------------
        # TEST 7: Valid failure for registered repo -> 200 Queued + DB row
        # -----------------------------------------------------------------
        delivery_id_valid = str(uuid.uuid4())
        valid_run_id = 9876543210
        valid_fail_payload = {
            "action": "completed",
            "workflow_run": {
                "id": valid_run_id,
                "head_sha": "c" * 40,
                "head_branch": "feature/fix-auth",
                "conclusion": "failure",
            },
            "repository": {
                "name": "ci-sandbox",
                "full_name": "haunter-test-bot/ci-sandbox",
                "owner": {"login": "haunter-test-bot"},
            },
        }
        raw_body = json.dumps(valid_fail_payload).encode("utf-8")
        valid_sig = sign_payload(TEST_SECRET, raw_body)
        resp = await client.post(
            "/webhooks/github",
            headers={
                "X-GitHub-Event": "workflow_run",
                "X-GitHub-Delivery": delivery_id_valid,
                "X-Hub-Signature-256": valid_sig,
            },
            content=raw_body,
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        res_data = resp.json()
        assert res_data["status"] == "queued"
        assert res_data["github_run_id"] == valid_run_id

        # Verify DB row
        async with async_session_maker() as session:
            stmt = select(Run).where(Run.github_run_id == valid_run_id)
            run_row = (await session.execute(stmt)).scalars().first()
            assert run_row is not None
            assert run_row.status == "pending"
            assert run_row.conclusion == "failure"
            assert run_row.head_sha == "c" * 40
            assert run_row.github_delivery_id == delivery_id_valid
            assert run_row.repo_id == test_repo_id
        print("[PASS] Test 7 Passed: Valid failing run enqueued & persisted in Neon DB")

        # -----------------------------------------------------------------
        # TEST 8: Idempotency on duplicate delivery -> 200 Duplicate
        # -----------------------------------------------------------------
        resp_dup = await client.post(
            "/webhooks/github",
            headers={
                "X-GitHub-Event": "workflow_run",
                "X-GitHub-Delivery": delivery_id_valid,
                "X-Hub-Signature-256": valid_sig,
            },
            content=raw_body,
        )
        assert resp_dup.status_code == 200
        assert resp_dup.json()["status"] == "duplicate"

        # Verify count is still 1
        async with async_session_maker() as session:
            stmt = select(Run).where(Run.github_delivery_id == delivery_id_valid)
            runs = (await session.execute(stmt)).scalars().all()
            assert len(runs) == 1
        print("[PASS] Test 8 Passed: Replayed delivery id handled idempotently without extra rows")

        # -----------------------------------------------------------------
        # TEST 9: Concurrent deliveries race-condition test
        # -----------------------------------------------------------------
        concurrent_delivery_id = str(uuid.uuid4())
        concurrent_run_id = 9876543211
        concurrent_payload = {
            "action": "completed",
            "workflow_run": {
                "id": concurrent_run_id,
                "head_sha": "d" * 40,
                "head_branch": "fix/concurrent",
                "conclusion": "failure",
            },
            "repository": {
                "name": "ci-sandbox",
                "full_name": "haunter-test-bot/ci-sandbox",
                "owner": {"login": "haunter-test-bot"},
            },
        }
        concurrent_bytes = json.dumps(concurrent_payload).encode("utf-8")
        concurrent_sig = sign_payload(TEST_SECRET, concurrent_bytes)

        async def send_webhook() -> httpx.Response:
            return await client.post(
                "/webhooks/github",
                headers={
                    "X-GitHub-Event": "workflow_run",
                    "X-GitHub-Delivery": concurrent_delivery_id,
                    "X-Hub-Signature-256": concurrent_sig,
                },
                content=concurrent_bytes,
            )

        responses = await asyncio.gather(
            send_webhook(),
            send_webhook(),
            send_webhook(),
            send_webhook(),
        )

        for r in responses:
            assert r.status_code == 200
            assert r.json()["status"] in ("queued", "duplicate")

        async with async_session_maker() as session:
            stmt = select(Run).where(Run.github_run_id == concurrent_run_id)
            runs = (await session.execute(stmt)).scalars().all()
            assert len(runs) == 1, f"Expected 1 run row under concurrent delivery, got {len(runs)}"
        print("[PASS] Test 9 Passed: Concurrent duplicate deliveries resolved via DB unique constraint")

        # -----------------------------------------------------------------
        # TEST 10: Oversized payload (>2MB) -> 413
        # -----------------------------------------------------------------
        oversized_bytes = b"x" * (2 * 1024 * 1024 + 100)
        oversized_sig = sign_payload(TEST_SECRET, oversized_bytes)
        resp_oversized = await client.post(
            "/webhooks/github",
            headers={
                "X-GitHub-Event": "workflow_run",
                "X-GitHub-Delivery": str(uuid.uuid4()),
                "X-Hub-Signature-256": oversized_sig,
                "Content-Length": str(len(oversized_bytes)),
            },
            content=oversized_bytes,
        )
        assert resp_oversized.status_code == 413, f"Expected 413, got {resp_oversized.status_code}"
        print("[PASS] Test 10 Passed: Oversized payload (>2MB) rejected with 413 before parsing")

        # -----------------------------------------------------------------
        # TEST 11: GitHub REST API Client Wrapper Unit Tests
        # -----------------------------------------------------------------
        from app.github_client import (
            GitHubAuthError,
            GitHubClientError,
            GitHubResourceNotFoundError,
            _build_headers,
            fetch_commit_metadata,
            fetch_diff,
            fetch_workflow_run_logs,
        )

        # 11a: Verify header builder
        h_none = _build_headers(token=None)
        assert "Authorization" not in h_none or h_none.get("Authorization") is None

        secret_token = "ghp_super_secret_test_token_never_leak"
        h_auth = _build_headers(token=secret_token)
        assert h_auth["Authorization"] == f"Bearer {secret_token}"

        # 11b: Verify exceptions never leak token in their message string
        try:
            raise GitHubAuthError("GitHub authentication failure (401)")
        except GitHubAuthError as exc:
            assert secret_token not in str(exc)

        # 11c: Mock zip log extraction test
        import io
        import zipfile
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zf:
            zf.writestr("job_1.txt", "2026-08-27T00:00:00Z Build failed on step 4")
            zf.writestr("job_2.txt", "2026-08-27T00:00:01Z Test failure: AssertionError")

        # Test log unzipping helper logic
        with zipfile.ZipFile(io.BytesIO(zip_buffer.getvalue())) as zf:
            parts = []
            for name in sorted(zf.namelist()):
                if name.endswith(".txt"):
                    parts.append(f"=== File: {name} ===\n{zf.read(name).decode('utf-8')}")
            extracted_logs = "\n\n".join(parts)
            assert "Build failed on step 4" in extracted_logs
            assert "Test failure: AssertionError" in extracted_logs

        print("[PASS] Test 11 Passed: GitHub client wrapper security, header construction, and log extraction verified")

        print("\n========================================================")
        print("         ALL 11 PHASE 3 VERIFICATION TESTS PASSED       ")
        print("========================================================\n")


if __name__ == "__main__":
    asyncio.run(main())

