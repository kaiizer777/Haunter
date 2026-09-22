"""
Tests for AST and Symbol Call-Graph Analyzer Subagent Helper.
"""

from __future__ import annotations

import pytest

from app.subagents.ast_analyzer import (
    StackFrame,
    extract_generic_symbol_context,
    extract_python_ast_context,
    extract_stack_frames,
    format_ast_context,
)


def test_extract_python_traceback_frames():
    logs = """
    Traceback (most recent call last):
      File "/home/runner/work/my-repo/my-repo/app/routers/auth.py", line 120, in login
        token = create_access_token(user_id=user.id)
      File "/home/runner/work/my-repo/my-repo/app/services/token.py", line 45, in create_access_token
        raise ExpiredSignatureError("Token signing failed")
    ExpiredSignatureError: Token signing failed
    """
    repo_paths = [
        "app/routers/auth.py",
        "app/services/token.py",
        "app/main.py",
    ]
    frames = extract_stack_frames(logs, repo_paths)
    assert len(frames) == 2
    assert frames[0].file_path == "app/routers/auth.py"
    assert frames[0].line_number == 120
    assert frames[0].symbol_name == "login"
    assert frames[1].file_path == "app/services/token.py"
    assert frames[1].line_number == 45
    assert frames[1].symbol_name == "create_access_token"


def test_extract_pytest_traceback_frames():
    logs = """
    ________________________ test_calculate_metrics ________________________
    tests/test_metrics.py:28: in test_calculate_metrics
        res = compute_percentile(data, 95)
    app/analytics/stats.py:54: in compute_percentile
        return sorted_data[idx]
    E   IndexError: list index out of range
    """
    repo_paths = [
        "tests/test_metrics.py",
        "app/analytics/stats.py",
    ]
    frames = extract_stack_frames(logs, repo_paths)
    assert len(frames) == 2
    assert frames[0].file_path == "tests/test_metrics.py"
    assert frames[0].line_number == 28
    assert frames[0].symbol_name == "test_calculate_metrics"
    assert frames[1].file_path == "app/analytics/stats.py"
    assert frames[1].line_number == 54
    assert frames[1].symbol_name == "compute_percentile"


def test_extract_node_traceback_frames():
    logs = """
    Error: Unhandled rejection
        at AuthService.validateToken (/workspace/src/services/auth.ts:89:12)
        at async handleRequest (/workspace/src/api/handler.ts:34:5)
        at /workspace/src/server.ts:102:3
    """
    repo_paths = [
        "src/services/auth.ts",
        "src/api/handler.ts",
        "src/server.ts",
    ]
    frames = extract_stack_frames(logs, repo_paths)
    assert len(frames) == 3
    assert frames[0].file_path == "src/services/auth.ts"
    assert frames[0].line_number == 89
    assert frames[0].symbol_name == "AuthService.validateToken"
    assert frames[1].file_path == "src/api/handler.ts"
    assert frames[1].line_number == 34
    assert frames[1].symbol_name == "handleRequest"
    assert frames[2].file_path == "src/server.ts"
    assert frames[2].line_number == 102


def test_extract_stack_frames_ignores_third_party_and_stdlib():
    logs = """
    Traceback (most recent call last):
      File "/opt/hostedtoolcache/Python/3.11.0/x64/lib/python3.11/asyncio/runners.py", line 190, in run
        return runner.run(main)
      File "/home/runner/.local/lib/python3.11/site-packages/fastapi/routing.py", line 250, in app
        raw_response = await run_endpoint_function(...)
      File "/app/services/core.py", line 33, in execute
        do_work()
    """
    repo_paths = [
        "app/services/core.py",
    ]
    frames = extract_stack_frames(logs, repo_paths)
    assert len(frames) == 1
    assert frames[0].file_path == "app/services/core.py"
    assert frames[0].line_number == 33


def test_extract_stack_frames_rate_limit_guard():
    # Verify capping at max_frames = 5
    logs = "\n".join(f'  File "app/file_{i}.py", line {i*10}, in func_{i}' for i in range(10))
    repo_paths = [f"app/file_{i}.py" for i in range(10)]
    frames = extract_stack_frames(logs, repo_paths, max_frames=5)
    assert len(frames) == 5


def test_extract_python_ast_context_function():
    source = """import os
from typing import Optional

def calculate_fee(base_amount: float, rate: float = 0.05, currency: str = "USD") -> float:
    \"\"\"Calculate processing fee.\"\"\"
    tax = base_amount * 0.01
    total_fee = base_amount * rate + tax
    return round(total_fee, 2)
"""
    # Line 7 is "total_fee = base_amount * rate + tax"
    context = extract_python_ast_context(source, line_number=7)
    assert context is not None
    assert context["enclosing_symbol"] == "calculate_fee"
    assert context["enclosing_class"] is None
    assert "def calculate_fee(base_amount: float, rate: float = 0.05, currency: str = 'USD') -> float:" in context["signature"]
    assert "import os" in context["imports"]
    assert "from typing import Optional" in context["imports"]
    assert context["start_line"] == 4
    assert context["end_line"] == 8
    assert "total_fee = base_amount * rate + tax" in context["snippet"]


def test_extract_python_ast_context_class_method():
    source = """from dataclasses import dataclass

@dataclass
class PaymentEngine:
    gateway: str

    async def charge(self, user_id: str, amount_cents: int) -> bool:
        if amount_cents <= 0:
            raise ValueError("Invalid amount")
        return True
"""
    # Line 9 is "raise ValueError('Invalid amount')"
    context = extract_python_ast_context(source, line_number=9)
    assert context is not None
    assert context["enclosing_class"] == "PaymentEngine"
    assert context["enclosing_symbol"] == "charge"
    assert "async def charge(self, user_id: str, amount_cents: int) -> bool:" in context["signature"]
    assert context["start_line"] == 7
    assert context["end_line"] == 10


def test_extract_python_ast_context_syntax_error():
    source = "def broken(:"
    context = extract_python_ast_context(source, line_number=1)
    assert context is None


def test_extract_generic_symbol_context_typescript():
    ts_source = """import { Client } from 'pg';

export async function queryUser(userId: string, active: boolean = true): Promise<User> {
    const query = 'SELECT * FROM users WHERE id = $1';
    const result = await db.execute(query, [userId]);
    return result.rows[0];
}
"""
    context = extract_generic_symbol_context(ts_source, line_number=5, symbol_name="queryUser")
    assert context is not None
    assert context["enclosing_symbol"] == "queryUser"
    assert "queryUser" in (context["signature"] or "")
    assert "const result = await db.execute" in context["snippet"]


def test_format_ast_context_markdown():
    frame = StackFrame(file_path="backend/app/auth.py", line_number=45, symbol_name="verify_token")
    context = {
        "enclosing_class": "SecurityManager",
        "enclosing_symbol": "verify_token",
        "signature": "def verify_token(token: str, secret: str = 'default') -> bool:",
        "imports": ["import jwt", "from datetime import datetime"],
        "start_line": 40,
        "end_line": 50,
        "snippet": "    payload = jwt.decode(token, secret)\n    return bool(payload)",
        "language": "python",
    }
    formatted = format_ast_context(frame, context)
    assert "### `backend/app/auth.py` (Line 45, in `verify_token`)" in formatted
    assert "**Enclosing Class:** `SecurityManager`" in formatted
    assert "**Signature:** `def verify_token(token: str, secret: str = 'default') -> bool:`" in formatted
    assert "**Module Imports:**" in formatted
    assert "import jwt" in formatted
    assert "payload = jwt.decode(token, secret)" in formatted
