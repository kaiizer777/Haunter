"""
Integration test for AST & Symbol Call-Graph Context Expansion in Context Gatherer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Repo, Run, User
from app.subagents.context_gatherer import gather_context
from tests.conftest import truncate_all


@pytest.mark.asyncio
async def test_gather_context_enriches_summary_with_ast_context(db: AsyncSession):
    await truncate_all(db)

    user = User(
        github_id=987654321,
        github_username="ast_tester",
        access_token="test_token",
    )
    db.add(user)
    await db.commit()

    repo = Repo(
        user_id=user.id,
        owner="haunter-org",
        name="haunter-repo",
        default_branch="main",
    )
    db.add(repo)
    await db.commit()

    run = Run(
        repo_id=repo.id,
        github_run_id=55556666,
        github_delivery_id="deliv-ast-123",
        head_sha="0123456789abcdef0123456789abcdef01234567",
        head_branch="main",
        status="pending",
        conclusion="failure",
    )
    db.add(run)
    await db.commit()

    logs_with_traceback = """
    Traceback (most recent call last):
      File "backend/app/routers/payment.py", line 1, in checkout
        result = process_payment(amount=100)
      File "backend/app/services/processor.py", line 12, in process_payment
        raise ValueError("Unsupported currency")
    ValueError: Unsupported currency
    """

    diff_content = """diff --git a/backend/app/routers/payment.py b/backend/app/routers/payment.py
--- a/backend/app/routers/payment.py
+++ b/backend/app/routers/payment.py
@@ -39,2 +39,2 @@
-    result = process_payment(amount=50)
+    result = process_payment(amount=100)
"""

    processor_code = """from typing import Optional

class PaymentProcessor:
    def __init__(self, mode: str = "live"):
        self.mode = mode

def process_payment(amount: float, currency: str = "USD") -> bool:
    \"\"\"Process payment for user transaction.\"\"\"
    if amount <= 0:
        raise ValueError("Invalid amount")
    if currency != "USD":
        raise ValueError("Unsupported currency")
    return True
"""

    mock_llm_response = {
        "content": "ValueError: Unsupported currency raised in process_payment",
        "model": "test-model",
        "latency_ms": 150,
        "usage": {"input_tokens": 500, "output_tokens": 50},
    }

    async def mock_fetch_file_content(owner, repo, path, sha, token=None):
        if path == "backend/app/services/processor.py":
            return processor_code
        if path == "backend/app/routers/payment.py":
            return "def checkout(): pass"
        return None

    async def mock_fetch_repo_tree(owner, repo, sha, token=None, max_paths=60):
        return [
            "backend/app/routers/payment.py",
            "backend/app/services/processor.py",
        ]

    with (
        patch("app.github_client.fetch_workflow_run_logs", AsyncMock(return_value=logs_with_traceback)),
        patch("app.github_client.fetch_diff", AsyncMock(return_value=diff_content)),
        patch("app.github_client.fetch_commit_metadata", AsyncMock(return_value={"commit": {"message": "update payment"}})),
        patch("app.github_client.fetch_file_content", AsyncMock(side_effect=mock_fetch_file_content)),
        patch("app.github_client.fetch_repo_tree_paths", AsyncMock(side_effect=mock_fetch_repo_tree)),
        patch("app.llm.LLMClient.complete", AsyncMock(return_value=mock_llm_response)),
    ):
        summary = await gather_context(run=run, repo=repo, db=db)

        # Assert basic diagnosis exists
        assert "ValueError: Unsupported currency" in summary

        # Assert Section 8 AST & Symbol Context is present
        assert "## Enclosing Scope & Symbol Context" in summary
        assert "backend/app/services/processor.py" in summary
        assert "process_payment" in summary
        assert "def process_payment(amount: float, currency: str = 'USD') -> bool:" in summary
