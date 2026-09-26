"""
Phase 4.3 (future.md §4.4 Phase 3) — Autonomous Self-Correction Eval Harness.

Golden broken-repo cases where the agent iteratively fixes failures across
2–3 CI runs before marking complete. Fully deterministic: no live GitHub,
no live LLM. ``verify_in_ci_sandbox`` is mocked with a fail→fail→pass
sequence and the LLM fix loop is scripted as ``str_replace`` patches
applied through the real orchestrator dispatch path.

Loop under test (per system prompt self-healing guidance):
  verify → on FAILED inspect logs → str_replace fix → re-verify,
  up to 3 verify attempts, then complete (pass) or exhaust (still failing).
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.session_streamer import SseQueue

MAX_SELF_HEAL_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# Golden cases (eval-harness fixture pattern: id + failure_type + trace
# keywords + deterministic fix plan). Each case needs two fixes because the
# mocked CI sequence is fail → fail → pass.
# ---------------------------------------------------------------------------

GOLDEN_CASES: list[dict[str, Any]] = [
    {
        "id": "selfheal-syntax-001",
        "category": "syntax_error",
        "file": "src/app.py",
        "broken_content": "def parse_payload(:\n    return {}\n",
        "fail_logs": [
            "E SyntaxError: invalid syntax (src/app.py, line 1)\n"
            " def parse_payload(:\n ^",
            "FAILED tests/test_app.py::test_parse - AssertionError: expected key 'id'",
        ],
        "trace_keywords": [["SyntaxError"], ["AssertionError"]],
        "fixes": [
            {
                "path": "src/app.py",
                "old_str": "def parse_payload(:",
                "new_str": "def parse_payload():",
            },
            {
                "path": "src/app.py",
                "old_str": "    return {}",
                "new_str": '    return {"id": 1}',
            },
        ],
        "pass_log": "All tests passed (src/app.py)",
    },
    {
        "id": "selfheal-assert-002",
        "category": "failing_assertion",
        "file": "src/calc.py",
        "broken_content": "def add(a, b):\n    return a - b\n",
        "fail_logs": [
            "FAILED tests/test_calc.py::test_add - AssertionError: 2 + 2 == 5",
            "FAILED tests/test_calc.py::test_add_none - TypeError: unsupported operand",
        ],
        "trace_keywords": [["AssertionError"], ["TypeError"]],
        "fixes": [
            {
                "path": "src/calc.py",
                "old_str": "    return a - b",
                "new_str": "    return a + b",
            },
            {
                "path": "src/calc.py",
                "old_str": "def add(a, b):\n    return a + b",
                "new_str": 'def add(a, b):\n    if a is None or b is None:\n        raise ValueError("None")\n    return a + b',
            },
        ],
        "pass_log": "All tests passed (src/calc.py)",
    },
    {
        "id": "selfheal-import-003",
        "category": "missing_import",
        "file": "src/main.py",
        "broken_content": "def fetch(url):\n    return requests.get(url).json()\n",
        "fail_logs": [
            "ModuleNotFoundError: No module named 'requests' (src/main.py)",
            "FAILED tests/test_fetch.py::test_timeout - AssertionError: missing timeout",
        ],
        "trace_keywords": [["ModuleNotFoundError"], ["AssertionError"]],
        "fixes": [
            {
                "path": "src/main.py",
                "old_str": "def fetch(url):",
                "new_str": "import requests\n\n\ndef fetch(url):",
            },
            {
                "path": "src/main.py",
                "old_str": "    return requests.get(url).json()",
                "new_str": "    return requests.get(url, timeout=10).json()",
            },
        ],
        "pass_log": "All tests passed (src/main.py)",
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_orchestrator() -> Any:
    from app.services.session_orchestrator import SessionOrchestrator

    orch = SessionOrchestrator(session_id=uuid.uuid4(), db=MagicMock(), gh_token="tok")
    orch._llm = AsyncMock()
    return orch


def _make_session() -> MagicMock:
    session = MagicMock()
    session.id = uuid.uuid4()
    session.base_sha = "a" * 40
    session.staged_patches = {}
    repo = MagicMock()
    repo.owner = "test-org"
    repo.name = "test-repo"
    session.repo = repo
    return session


def _fail_result(logs: str, run_id: int = 1) -> str:
    return (
        "CI sandbox verification: FAILED\n"
        "Workflow: auto\n"
        "Status: failed\n"
        f"Exit code: 1 (Duration: 0.01s)\n"
        f"Run URL: https://github.com/o/r/actions/runs/{run_id}\n"
        f"LOGS:\n{logs}"
    )


def _pass_result(logs: str, run_id: int = 99) -> str:
    return (
        "CI sandbox verification: PASSED\n"
        "Workflow: auto\n"
        "Status: passed\n"
        f"Exit code: 0 (Duration: 0.01s)\n"
        f"Run URL: https://github.com/o/r/actions/runs/{run_id}\n"
        f"LOGS:\n{logs}"
    )


async def _run_self_healing_loop(
    orch: Any,
    session: MagicMock,
    staged_patches: dict[str, str],
    queue: SseQueue,
    case: dict[str, Any],
    verify_results: list[str],
    max_attempts: int = MAX_SELF_HEAL_ATTEMPTS,
) -> dict[str, Any]:
    """Drive the agent self-healing loop deterministically.

    Mirrors the system-prompt instruction: verify → on FAILED inspect the
    traceback → str_replace fix → re-verify, up to ``max_attempts`` verifies.
    ``verify_results`` is the scripted fail→fail→pass (or always-fail)
    sequence returned by the mocked ``verify_in_ci_sandbox``.
    """
    fixes: list[dict[str, str]] = list(case["fixes"])
    trace_keywords: list[list[str]] = list(case["trace_keywords"])
    verify_calls = 0
    fix_calls = 0
    inspected: list[bool] = []
    result = ""

    with patch(
        "app.services.session_orchestrator.tool_verify_ci_sandbox",
        new_callable=AsyncMock,
        side_effect=list(verify_results),
    ) as mock_verify:
        for attempt in range(1, max_attempts + 1):
            result = await orch._dispatch_tool(
                tool_name="verify_in_ci_sandbox",
                args={},
                repo_owner="test-org",
                repo_name="test-repo",
                base_sha="a" * 40,
                staged_patches=staged_patches,
                queue=queue,
                session=session,
            )
            verify_calls += 1

            if "PASSED" in result:
                return {
                    "complete": True,
                    "attempts": attempt,
                    "verify_calls": verify_calls,
                    "fix_calls": fix_calls,
                    "inspected": inspected,
                    "final_result": result,
                }
            assert (
                "FAILED" in result
            ), f"attempt {attempt}: expected FAILED, got: {result[:200]}"

            # Agent inspects the traceback before fixing: the expected
            # keyword for this failure must be present in the tool result.
            keywords = trace_keywords[min(attempt - 1, len(trace_keywords) - 1)]
            assert any(
                kw in result for kw in keywords
            ), f"attempt {attempt}: expected one of {keywords} in CI logs, got: {result[:300]}"
            inspected.append(True)

            if attempt == max_attempts:
                break  # exhausted — do not fix beyond the attempt budget

            fix = fixes[fix_calls]
            fix_result = await orch._dispatch_tool(
                tool_name="str_replace",
                args={
                    "path": fix["path"],
                    "old_str": fix["old_str"],
                    "new_str": fix["new_str"],
                },
                repo_owner="test-org",
                repo_name="test-repo",
                base_sha="a" * 40,
                staged_patches=staged_patches,
                queue=queue,
                session=session,
            )
            fix_calls += 1
            assert fix_result.startswith(
                "Successfully"
            ), f"attempt {attempt}: str_replace failed: {fix_result}"

    return {
        "complete": False,
        "attempts": max_attempts,
        "verify_calls": verify_calls,
        "fix_calls": fix_calls,
        "inspected": inspected,
        "final_result": result,
    }


def _case_by_id(case_id: str) -> dict[str, Any]:
    for case in GOLDEN_CASES:
        if case["id"] == case_id:
            return case
    raise KeyError(f"unknown golden case {case_id!r}")


# ---------------------------------------------------------------------------
# Prompt guidance (Phase 4.1 system-prompt loop instruction)
# ---------------------------------------------------------------------------


def test_system_prompt_contains_self_healing_loop() -> None:
    from app.services.session_orchestrator import _build_system_prompt

    prompt = _build_system_prompt(
        repo_owner="o",
        repo_name="r",
        branch_name="b",
        base_sha="a" * 40,
        staged_patches={},
    )
    assert "verify_in_ci_sandbox" in prompt
    assert "FAILED" in prompt
    assert "str_replace" in prompt
    assert "re-run `verify_in_ci_sandbox`" in prompt
    assert "up to 3 attempts" in prompt


def test_golden_cases_shape() -> None:
    """Golden-case pattern reuse from test_eval_harness.py: unique ids, required keys."""
    ids = [c["id"] for c in GOLDEN_CASES]
    assert len(ids) == len(set(ids)), "duplicate golden case ids"
    assert len(GOLDEN_CASES) >= 3
    for case in GOLDEN_CASES:
        for key in (
            "id",
            "category",
            "file",
            "broken_content",
            "fail_logs",
            "fixes",
            "pass_log",
        ):
            assert key in case, f"case {case.get('id')} missing {key!r}"
        assert case["category"] in {
            "syntax_error",
            "failing_assertion",
            "missing_import",
        }
        assert len(case["fail_logs"]) == 2
        assert len(case["fixes"]) == 2
        for fix in case["fixes"]:
            assert set(fix) == {"path", "old_str", "new_str"}
            assert fix["old_str"]  # str_replace rejects empty old_str


# ---------------------------------------------------------------------------
# Core loop: fail → fail → pass across 2–3 CI runs, then complete
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case_id", [c["id"] for c in GOLDEN_CASES])
async def test_self_healing_fail_fail_pass(case_id: str) -> None:
    case = _case_by_id(case_id)
    orch = _make_orchestrator()
    session = _make_session()
    staged_patches: dict[str, str] = {}
    queue = SseQueue()

    verify_results = [
        _fail_result(case["fail_logs"][0], run_id=101),
        _fail_result(case["fail_logs"][1], run_id=102),
        _pass_result(case["pass_log"], run_id=103),
    ]

    with patch(
        "app.services.session_tools.editor.fetch_file_content",
        new_callable=AsyncMock,
        return_value=case["broken_content"],
    ):
        outcome = await _run_self_healing_loop(
            orch, session, staged_patches, queue, case, verify_results
        )

    assert outcome["complete"] is True
    assert (
        outcome["verify_calls"] == 3
    ), "agent must verify, fix, re-verify up to 3 CI runs"
    assert outcome["fix_calls"] == 2
    assert outcome["attempts"] == 3
    assert outcome["inspected"] == [True, True], "traceback inspected before every fix"
    assert "PASSED" in outcome["final_result"]
    # Both str_replace patches landed in staged state for the file under repair.
    assert case["file"] in staged_patches
    assert staged_patches[case["file"]].startswith("--- ")


async def test_self_healing_exhausts_after_three_attempts() -> None:
    """Always-failing CI must stop after 3 verifies — no unbounded retry loop."""
    case = _case_by_id("selfheal-syntax-001")
    orch = _make_orchestrator()
    session = _make_session()
    staged_patches: dict[str, str] = {}
    queue = SseQueue()

    verify_results = [
        _fail_result(case["fail_logs"][0], run_id=201),
        _fail_result(case["fail_logs"][1], run_id=202),
        _fail_result(case["fail_logs"][1], run_id=203),
    ]

    with patch(
        "app.services.session_tools.editor.fetch_file_content",
        new_callable=AsyncMock,
        return_value=case["broken_content"],
    ):
        outcome = await _run_self_healing_loop(
            orch, session, staged_patches, queue, case, verify_results
        )

    assert outcome["complete"] is False
    assert outcome["verify_calls"] == MAX_SELF_HEAL_ATTEMPTS
    assert outcome["fix_calls"] == MAX_SELF_HEAL_ATTEMPTS - 1
    assert "FAILED" in outcome["final_result"]


async def test_self_healing_pass_first_try_no_fix_needed() -> None:
    """Already-green CI completes immediately without any str_replace call."""
    case = _case_by_id("selfheal-assert-002")
    orch = _make_orchestrator()
    session = _make_session()
    staged_patches: dict[str, str] = {"src/calc.py": "@@ seeded\n"}
    staged_before = dict(staged_patches)
    queue = SseQueue()

    with (
        patch(
            "app.services.session_orchestrator.tool_verify_ci_sandbox",
            new_callable=AsyncMock,
            return_value=_pass_result(case["pass_log"]),
        ) as mock_verify,
        patch(
            "app.services.session_orchestrator.tool_str_replace",
            new_callable=AsyncMock,
        ) as mock_str_replace,
        patch(
            "app.services.session_tools.editor.fetch_file_content",
            new_callable=AsyncMock,
        ) as mock_fetch,
    ):
        result = await orch._dispatch_tool(
            tool_name="verify_in_ci_sandbox",
            args={},
            repo_owner="test-org",
            repo_name="test-repo",
            base_sha="a" * 40,
            staged_patches=staged_patches,
            queue=queue,
            session=session,
        )

    mock_verify.assert_awaited_once()
    mock_str_replace.assert_not_awaited()
    mock_fetch.assert_not_awaited()
    assert staged_patches == staged_before
    assert "PASSED" in result
