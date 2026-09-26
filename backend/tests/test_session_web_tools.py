"""
Unit and integration tests for Haunter Session Web Tools (Phase 3: TinyFish Live Web & Docs).

Covers:
- SSRF URL validation (_validate_external_url).
- tool_search_web_docs: live web/docs search via TinyFish Search API.
- tool_fetch_web_content: live webpage/docs scraping via TinyFish Fetch API.
- tool_fetch_package_metadata: PyPI & npm registry checks.
- Orchestrator tool dispatch & error handling.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from app.config import settings
from app.services.session_tools.web import (
    _validate_external_url,
    tool_fetch_package_metadata,
    tool_fetch_web_content,
    tool_search_web_docs,
)


# ---------------------------------------------------------------------------
# SSRF Validation Tests
# ---------------------------------------------------------------------------


def test_validate_external_url_valid() -> None:
    """Valid public HTTP and HTTPS URLs must pass through unchanged."""
    assert (
        _validate_external_url("https://nextjs.org/docs") == "https://nextjs.org/docs"
    )
    assert _validate_external_url("http://python.org") == "http://python.org"
    assert (
        _validate_external_url("https://github.com/fastapi/fastapi/issues/123")
        == "https://github.com/fastapi/fastapi/issues/123"
    )


def test_validate_external_url_empty() -> None:
    """Empty or whitespace URL must raise ValueError."""
    with pytest.raises(ValueError, match="URL must not be empty"):
        _validate_external_url("")
    with pytest.raises(ValueError, match="URL must not be empty"):
        _validate_external_url("   ")


def test_validate_external_url_invalid_schemes() -> None:
    """Disallowed schemes must be rejected."""
    with pytest.raises(ValueError, match="only 'http' and 'https' are permitted"):
        _validate_external_url("ftp://example.com/file.txt")
    with pytest.raises(ValueError, match="only 'http' and 'https' are permitted"):
        _validate_external_url("file:///etc/passwd")
    with pytest.raises(ValueError, match="only 'http' and 'https' are permitted"):
        _validate_external_url("gopher://example.com")


def test_validate_external_url_blocked_hostnames() -> None:
    """Internal hostnames like localhost must be rejected."""
    with pytest.raises(ValueError, match="internal/loopback hostname"):
        _validate_external_url("http://localhost:8000/api")
    with pytest.raises(ValueError, match="internal/loopback hostname"):
        _validate_external_url("https://ip6-localhost/test")


def test_validate_external_url_ssrf_private_ips() -> None:
    """Private, link-local, and loopback IP addresses must be blocked."""
    blocked_ips = [
        "127.0.0.1",
        "10.0.0.5",
        "172.16.0.1",
        "192.168.1.100",
        "169.254.169.254",  # AWS metadata service
        "[::1]",
    ]
    for ip in blocked_ips:
        with pytest.raises(ValueError, match="SSRF protection"):
            _validate_external_url(f"http://{ip}/secret")


# ---------------------------------------------------------------------------
# tool_search_web_docs Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_web_docs_missing_api_key() -> None:
    """Tool returns descriptive error when TINYFISH_API_KEY is not configured."""
    with patch.object(settings, "tinyfish_api_key", None):
        result = await tool_search_web_docs("Next.js 15")
        assert "TINYFISH_API_KEY is not configured" in result


@pytest.mark.asyncio
async def test_search_web_docs_empty_query() -> None:
    """Tool returns error when query is empty."""
    with patch.object(settings, "tinyfish_api_key", "test-key"):
        result = await tool_search_web_docs("   ")
        assert "query must not be empty" in result


@pytest.mark.asyncio
@respx.mock
async def test_search_web_docs_success() -> None:
    """Successful search returns formatted Markdown with title, link, and snippet."""
    search_url = "https://api.search.tinyfish.ai"
    mock_results = {
        "results": [
            {
                "title": "Next.js by Vercel",
                "url": "https://nextjs.org",
                "snippet": "The React framework for the web.",
            },
            {
                "title": "Next.js 16 Blog",
                "url": "https://nextjs.org/blog/next-16",
                "snippet": "Next.js 16 is now available.",
            },
        ]
    }
    route = respx.get(search_url).respond(status_code=200, json=mock_results)

    with (
        patch.object(settings, "tinyfish_api_key", "test-key"),
        patch.object(settings, "tinyfish_search_url", search_url),
    ):
        output = await tool_search_web_docs(
            query="Next.js", domain="nextjs.org", max_results=5
        )

    assert route.called
    req = route.calls.last.request
    assert req.headers.get("x-api-key") == "test-key"
    assert "query=Next.js" in str(req.url)
    assert "include_domains=nextjs.org" in str(req.url)

    assert "### [Next.js by Vercel](https://nextjs.org)" in output
    assert "The React framework for the web." in output
    assert "### [Next.js 16 Blog](https://nextjs.org/blog/next-16)" in output


@pytest.mark.asyncio
@respx.mock
async def test_search_web_docs_domain_cleaning() -> None:
    """Domain parameter is sanitized to strip scheme, path, and trailing slashes."""
    search_url = "https://api.search.tinyfish.ai"
    route = respx.get(search_url).respond(status_code=200, json={"results": []})

    with (
        patch.object(settings, "tinyfish_api_key", "test-key"),
        patch.object(settings, "tinyfish_search_url", search_url),
    ):
        await tool_search_web_docs(query="test", domain="https://docs.python.org/3/")

    assert route.called
    req = route.calls.last.request
    assert "include_domains=docs.python.org" in str(req.url)


@pytest.mark.asyncio
@respx.mock
async def test_search_web_docs_max_results_slicing() -> None:
    """max_results limits the number of output items rendered."""
    search_url = "https://api.search.tinyfish.ai"
    mock_results = {
        "results": [
            {
                "title": f"Result {i}",
                "url": f"https://example.com/{i}",
                "snippet": f"Snippet {i}",
            }
            for i in range(10)
        ]
    }
    respx.get(search_url).respond(status_code=200, json=mock_results)

    with (
        patch.object(settings, "tinyfish_api_key", "test-key"),
        patch.object(settings, "tinyfish_search_url", search_url),
    ):
        output = await tool_search_web_docs(query="test", max_results=3)

    assert output.count("### [Result") == 3


@pytest.mark.asyncio
@respx.mock
async def test_search_web_docs_empty_results() -> None:
    """When no results are found, returns a helpful message."""
    search_url = "https://api.search.tinyfish.ai"
    respx.get(search_url).respond(status_code=200, json={"results": []})

    with (
        patch.object(settings, "tinyfish_api_key", "test-key"),
        patch.object(settings, "tinyfish_search_url", search_url),
    ):
        output = await tool_search_web_docs(query="nonexistent term 12345")

    assert "No relevant documentation found for 'nonexistent term 12345'" in output


@pytest.mark.asyncio
@respx.mock
async def test_search_web_docs_errors() -> None:
    """Handles rate limiting, server error, and network failures gracefully."""
    search_url = "https://api.search.tinyfish.ai"

    # 429
    respx.get(search_url).respond(status_code=429)
    with (
        patch.object(settings, "tinyfish_api_key", "test-key"),
        patch.object(settings, "tinyfish_search_url", search_url),
    ):
        r429 = await tool_search_web_docs("query")
    assert "rate limit exceeded" in r429

    # 500
    respx.get(search_url).respond(status_code=500)
    with (
        patch.object(settings, "tinyfish_api_key", "test-key"),
        patch.object(settings, "tinyfish_search_url", search_url),
    ):
        r500 = await tool_search_web_docs("query")
    assert "search service returned 500" in r500

    # Network exception
    respx.get(search_url).mock(side_effect=httpx.ConnectError("Connection refused"))
    with (
        patch.object(settings, "tinyfish_api_key", "test-key"),
        patch.object(settings, "tinyfish_search_url", search_url),
    ):
        r_err = await tool_search_web_docs("query")
    assert "web search request failed" in r_err


# ---------------------------------------------------------------------------
# tool_fetch_web_content Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_web_content_missing_api_key() -> None:
    """Tool returns descriptive error when TINYFISH_API_KEY is not configured."""
    with patch.object(settings, "tinyfish_api_key", None):
        result = await tool_fetch_web_content("https://example.com")
        assert "TINYFISH_API_KEY is not configured" in result


@pytest.mark.asyncio
async def test_fetch_web_content_ssrf_blocked() -> None:
    """SSRF-vulnerable target URLs fail closed immediately without making network call."""
    with patch.object(settings, "tinyfish_api_key", "test-key"):
        result = await tool_fetch_web_content("http://169.254.169.254/latest/meta-data")
        assert "Error:" in result
        assert "blocked" in result or "SSRF protection" in result


@pytest.mark.asyncio
@respx.mock
async def test_fetch_web_content_success() -> None:
    """Successful fetch extracts rendered text/markdown from TinyFish response."""
    fetch_url = "https://api.fetch.tinyfish.ai"
    mock_response = {
        "results": [
            {
                "url": "https://nextjs.org/docs",
                "text": "# Next.js Documentation\n\nWelcome to Next.js.",
            }
        ]
    }
    route = respx.post(fetch_url).respond(status_code=200, json=mock_response)

    with (
        patch.object(settings, "tinyfish_api_key", "test-key"),
        patch.object(settings, "tinyfish_fetch_url", fetch_url),
    ):
        content = await tool_fetch_web_content("https://nextjs.org/docs")

    assert route.called
    req = route.calls.last.request
    assert req.headers.get("x-api-key") == "test-key"
    assert "# Next.js Documentation" in content
    assert "Welcome to Next.js." in content


@pytest.mark.asyncio
@respx.mock
async def test_fetch_web_content_truncation() -> None:
    """Large content over 40,000 characters is truncated with notice."""
    fetch_url = "https://api.fetch.tinyfish.ai"
    huge_text = "A" * 50_000
    respx.post(fetch_url).respond(
        status_code=200, json={"results": [{"text": huge_text}]}
    )

    with (
        patch.object(settings, "tinyfish_api_key", "test-key"),
        patch.object(settings, "tinyfish_fetch_url", fetch_url),
    ):
        content = await tool_fetch_web_content("https://example.com/huge")

    assert len(content) > 40_000
    assert "[...truncated at 40000 chars]" in content


# ---------------------------------------------------------------------------
# tool_fetch_package_metadata Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_package_metadata_unsupported_ecosystem() -> None:
    """Unsupported ecosystem produces clear error string."""
    result = await tool_fetch_package_metadata("golang", "gin")
    assert "unsupported ecosystem 'golang'" in result


@pytest.mark.asyncio
async def test_fetch_package_metadata_empty_package() -> None:
    """Empty package name produces clear error string."""
    result = await tool_fetch_package_metadata("pypi", "   ")
    assert "package_name must not be empty" in result


@pytest.mark.asyncio
@respx.mock
async def test_fetch_package_metadata_pypi_success() -> None:
    """PyPI metadata is parsed and formatted as markdown."""
    pypi_url = "https://pypi.org/pypi/fastapi/json"
    respx.get(pypi_url).respond(
        status_code=200,
        json={
            "info": {
                "version": "0.115.0",
                "license": "MIT",
                "summary": "FastAPI framework, high performance",
                "requires_python": ">=3.8",
                "home_page": "https://fastapi.tiangolo.com",
            }
        },
    )

    output = await tool_fetch_package_metadata("pypi", "fastapi")
    assert "**Package**: fastapi (PyPI)" in output
    assert "**Latest version**: 0.115.0" in output
    assert "**License**: MIT" in output
    assert "**Python requires**: >=3.8" in output
    assert "https://fastapi.tiangolo.com" in output


@pytest.mark.asyncio
@respx.mock
async def test_fetch_package_metadata_npm_success() -> None:
    """npm metadata is parsed and formatted as markdown."""
    npm_url = "https://registry.npmjs.org/next"
    respx.get(npm_url).respond(
        status_code=200,
        json={
            "dist-tags": {"latest": "15.0.0"},
            "description": "The React Framework",
            "license": "MIT",
            "versions": {
                "15.0.0": {"dependencies": {"react": "^19.0.0", "react-dom": "^19.0.0"}}
            },
            "homepage": "https://nextjs.org",
        },
    )

    output = await tool_fetch_package_metadata("npm", "next")
    assert "**Package**: next (npm)" in output
    assert "**Latest version**: 15.0.0" in output
    assert "**License**: MIT" in output
    assert "`react@^19.0.0`" in output
    assert "https://nextjs.org" in output


@pytest.mark.asyncio
@respx.mock
async def test_fetch_package_metadata_not_found() -> None:
    """404 responses from PyPI or npm return clean 'not found' message."""
    respx.get("https://pypi.org/pypi/unknown-pkg-xyz/json").respond(status_code=404)
    respx.get("https://registry.npmjs.org/unknown-pkg-xyz").respond(status_code=404)

    pypi_res = await tool_fetch_package_metadata("pypi", "unknown-pkg-xyz")
    assert "Package 'unknown-pkg-xyz' not found on pypi." in pypi_res

    npm_res = await tool_fetch_package_metadata("npm", "unknown-pkg-xyz")
    assert "Package 'unknown-pkg-xyz' not found on npm." in npm_res


# ---------------------------------------------------------------------------
# Orchestrator Dispatch Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_web_tools_dispatch() -> None:
    """SessionOrchestrator._dispatch_tool correctly routes web intelligence tools."""
    from unittest.mock import MagicMock
    from app.services.session_orchestrator import SessionOrchestrator
    from app.services.session_streamer import SseQueue

    db_mock = MagicMock()
    orch = SessionOrchestrator(session_id="test-session-id", db=db_mock)
    queue = SseQueue()

    with (
        patch(
            "app.services.session_orchestrator.tool_search_web_docs",
            new_callable=AsyncMock,
        ) as mock_search,
        patch(
            "app.services.session_orchestrator.tool_fetch_web_content",
            new_callable=AsyncMock,
        ) as mock_fetch,
        patch(
            "app.services.session_orchestrator.tool_fetch_package_metadata",
            new_callable=AsyncMock,
        ) as mock_pkg,
    ):
        mock_search.return_value = "Search Results Mock"
        mock_fetch.return_value = "Fetch Content Mock"
        mock_pkg.return_value = "Package Metadata Mock"

        res_search = await orch._dispatch_tool(
            tool_name="search_web_docs",
            args={"query": "FastAPI", "domain": "tiangolo.com", "max_results": 3},
            repo_owner="test-owner",
            repo_name="test-repo",
            base_sha="sha123",
            staged_patches={},
            queue=queue,
        )
        assert res_search == "Search Results Mock"
        mock_search.assert_called_once_with(
            query="FastAPI", domain="tiangolo.com", max_results=3
        )

        res_fetch = await orch._dispatch_tool(
            tool_name="fetch_web_content",
            args={"url": "https://example.com/doc", "format": "markdown"},
            repo_owner="test-owner",
            repo_name="test-repo",
            base_sha="sha123",
            staged_patches={},
            queue=queue,
        )
        assert res_fetch == "Fetch Content Mock"
        mock_fetch.assert_called_once_with(
            url="https://example.com/doc", format="markdown"
        )

        res_pkg = await orch._dispatch_tool(
            tool_name="fetch_package_metadata",
            args={"ecosystem": "pypi", "package_name": "httpx"},
            repo_owner="test-owner",
            repo_name="test-repo",
            base_sha="sha123",
            staged_patches={},
            queue=queue,
        )
        assert res_pkg == "Package Metadata Mock"
        mock_pkg.assert_called_once_with(ecosystem="pypi", package_name="httpx")
