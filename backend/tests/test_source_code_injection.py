import pytest
import respx
import httpx
from app.github_client import fetch_file_content
from app.subagents.context_gatherer import _discover_candidate_files_to_inspect


@pytest.mark.asyncio
async def test_fetch_file_content_success():
    with respx.mock(base_url="https://api.github.com") as rx:
        rx.get("/repos/owner/repo/contents/backend/app/core/analytics.py?ref=sha123").respond(
            200, text="def get_tool_stats(): pass"
        )
        content = await fetch_file_content("owner", "repo", "backend/app/core/analytics.py", "sha123")
        assert content == "def get_tool_stats(): pass"


@pytest.mark.asyncio
async def test_fetch_file_content_not_found():
    with respx.mock(base_url="https://api.github.com") as rx:
        rx.get("/repos/owner/repo/contents/unknown.py?ref=sha123").respond(404)
        content = await fetch_file_content("owner", "repo", "unknown.py", "sha123")
        assert content is None


def test_discover_candidate_files_from_test_stem():
    logs = """
    FAILED tests/test_analytics.py::test_analytics_tool_stats_latency - AssertionError: Expected 150.0, got 100.0
    tests/test_analytics.py:28: in test_analytics_tool_stats_latency
    """
    repo_paths = [
        "backend/app/core/analytics.py",
        "backend/tests/test_analytics.py",
        "backend/app/main.py",
        "pytest.ini",
    ]
    candidates, test_files = _discover_candidate_files_to_inspect(logs, repo_paths)
    assert "backend/app/core/analytics.py" in candidates
    # Should not include test file itself as the implementation candidate
    assert "backend/tests/test_analytics.py" not in candidates
    assert "backend/tests/test_analytics.py" in test_files


def test_discover_candidate_files_prioritizes_core_over_router():
    logs = "FAILED tests/test_analytics.py::test_analytics_tool_stats_latency"
    repo_paths = [
        "backend/app/api/routers/analytics.py",
        "backend/app/core/analytics.py",
    ]
    candidates, _ = _discover_candidate_files_to_inspect(logs, repo_paths)
    assert candidates[0] == "backend/app/core/analytics.py"



def test_discover_candidate_files_from_traceback_lines():
    logs = """
    Traceback (most recent call last):
      File "backend/app/services/ai.py", line 45, in generate
        raise ValueError("model error")
    ValueError: model error
    """
    repo_paths = [
        "backend/app/services/ai.py",
        "backend/app/core/analytics.py",
    ]
    candidates, _ = _discover_candidate_files_to_inspect(logs, repo_paths)
    assert candidates == ["backend/app/services/ai.py"]
