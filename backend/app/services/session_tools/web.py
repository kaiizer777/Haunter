"""
Session web tools — Phase 3: TinyFish Live Web & Docs Intelligence.

Provides:
  - search_web_docs: Live web/docs search via TinyFish Search API.
  - fetch_web_content: Scrape & render docs/issues via TinyFish Fetch API.
  - fetch_package_metadata: PyPI/npm version and release checker.

Security:
  - All outbound URLs validated against SSRF vectors (loopback, link-local,
    private IP ranges) before any HTTP request is made.
  - TinyFish API key is read from settings; if unset, tools fail closed with a
    descriptive error — they never crash the agent loop.
  - httpx.AsyncClient with 15s timeout; 429 / 5xx handled gracefully.
  - Large markdown responses truncated at 40,000 characters.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_MAX_CONTENT_CHARS = 40_000

# ---------------------------------------------------------------------------
# SSRF protection — private / internal IP ranges
# ---------------------------------------------------------------------------

# RFC 1918, loopback, link-local (including AWS instance metadata 169.254.169.254)
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),       # loopback
    ipaddress.ip_network("::1/128"),            # IPv6 loopback
    ipaddress.ip_network("0.0.0.0/8"),          # unspecified
    ipaddress.ip_network("10.0.0.0/8"),         # RFC 1918 class A
    ipaddress.ip_network("172.16.0.0/12"),      # RFC 1918 class B (172.16-172.31)
    ipaddress.ip_network("192.168.0.0/16"),     # RFC 1918 class C
    ipaddress.ip_network("169.254.0.0/16"),     # link-local / AWS metadata
    ipaddress.ip_network("fc00::/7"),           # IPv6 unique local
    ipaddress.ip_network("fe80::/10"),          # IPv6 link-local
]

# Hostname-level blocklist (no DNS resolution needed for these well-known names).
_BLOCKED_HOSTNAMES = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})


def _validate_external_url(url: str) -> str:
    """
    Validate that *url* is safe to fetch externally.

    Enforces:
    - Scheme must be http or https.
    - Host must not be a known internal hostname or private IP range.

    Returns the original URL string if valid.
    Raises ValueError with a descriptive message on any violation.
    """
    if not url or not url.strip():
        raise ValueError("URL must not be empty.")

    parsed = urlparse(url)

    if parsed.scheme not in {"http", "https"}:
        raise ValueError(
            f"URL scheme {parsed.scheme!r} is not allowed — only 'http' and 'https' are permitted."
        )

    host = parsed.hostname or ""
    if not host:
        raise ValueError("URL must include a valid host.")

    # Reject well-known internal hostnames without DNS resolution.
    if host.lower() in _BLOCKED_HOSTNAMES:
        raise ValueError(f"URL host {host!r} is not allowed (internal/loopback hostname).")

    # Attempt IP parse — reject private/internal ranges directly.
    try:
        ip = ipaddress.ip_address(host)
        for network in _BLOCKED_NETWORKS:
            if ip in network:
                raise ValueError(
                    f"URL host {host!r} resolves to a private/internal IP address and is blocked (SSRF protection)."
                )
    except ValueError as exc:
        # If it's our explicit SSRF rejection, re-raise; otherwise it was a
        # domain name (not an IP literal) — that's fine, pass through.
        if "SSRF protection" in str(exc) or "internal/loopback" in str(exc) or "not allowed" in str(exc):
            raise

    return url


# ---------------------------------------------------------------------------
# Tool: search_web_docs
# ---------------------------------------------------------------------------


async def tool_search_web_docs(
    query: str,
    domain: str | None = None,
    max_results: int = 5,
) -> str:
    """
    Search live web/docs via TinyFish Search API.

    Returns a Markdown-formatted list of results with titles, URLs, and snippets.
    Returns a descriptive error string on auth failure, network error, or rate limit.
    """
    if not settings.tinyfish_api_key:
        return "Error: TINYFISH_API_KEY is not configured."

    if not query.strip():
        return "Error: query must not be empty."

    # Clamp max_results to a sane range.
    max_results = max(1, min(max_results, 20))

    payload: dict[str, Any] = {"query": query, "limit": max_results}
    if domain:
        payload["domain"] = domain

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{settings.tinyfish_base_url}/search",
                json=payload,
                headers={
                    "Authorization": f"Bearer {settings.tinyfish_api_key}",
                    "Content-Type": "application/json",
                },
            )
    except httpx.HTTPError as exc:
        logger.warning("tool_search_web_docs: HTTP error: %s", exc)
        return f"Error: web search request failed — {exc}"

    if response.status_code == 429:
        return "Error: TinyFish rate limit exceeded. Please retry in a moment."
    if response.status_code >= 500:
        return f"Error: TinyFish search service returned {response.status_code}. Try again later."
    if not response.is_success:
        return f"Error: TinyFish search returned HTTP {response.status_code}."

    try:
        data = response.json()
    except Exception:
        return "Error: could not parse TinyFish search response."

    # TinyFish returns {"results": [...]} where each item has title, url, snippet.
    results: list[dict[str, Any]] = data.get("results", [])
    if not results:
        return f"No relevant documentation found for '{query}'."

    lines: list[str] = []
    for item in results:
        title = item.get("title") or item.get("url", "")
        url = item.get("url", "")
        snippet = item.get("snippet") or item.get("description") or ""
        if title and url:
            lines.append(f"### [{title}]({url})")
        elif url:
            lines.append(f"### {url}")
        if snippet:
            lines.append(snippet)
        lines.append("")  # blank line between entries

    return "\n".join(lines).strip() or f"No relevant documentation found for '{query}'."


# ---------------------------------------------------------------------------
# Tool: fetch_web_content
# ---------------------------------------------------------------------------


async def tool_fetch_web_content(
    url: str,
    format: str = "markdown",
) -> str:
    """
    Fetch and render a web page / GitHub issue / documentation page via TinyFish Fetch API.

    The URL is validated against SSRF vectors before the request is made.
    Returns rendered Markdown content (truncated at 40,000 chars).
    Returns a descriptive error string on validation failure, network error, or rate limit.
    """
    if not settings.tinyfish_api_key:
        return "Error: TINYFISH_API_KEY is not configured."

    try:
        validated_url = _validate_external_url(url)
    except ValueError as exc:
        return f"Error: {exc}"

    payload: dict[str, Any] = {"url": validated_url, "format": format}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{settings.tinyfish_base_url}/fetch",
                json=payload,
                headers={
                    "Authorization": f"Bearer {settings.tinyfish_api_key}",
                    "Content-Type": "application/json",
                },
            )
    except httpx.HTTPError as exc:
        logger.warning("tool_fetch_web_content: HTTP error for url=%s: %s", url, exc)
        return f"Error: fetch request failed — {exc}"

    if response.status_code == 429:
        return "Error: TinyFish rate limit exceeded. Please retry in a moment."
    if response.status_code >= 500:
        return f"Error: TinyFish fetch service returned {response.status_code}. Try again later."
    if not response.is_success:
        return f"Error: TinyFish fetch returned HTTP {response.status_code}."

    try:
        data = response.json()
    except Exception:
        return "Error: could not parse TinyFish fetch response."

    content: str = data.get("content") or data.get("markdown") or data.get("text") or ""
    if not content:
        return f"No content returned for URL: {url}"

    if len(content) > _MAX_CONTENT_CHARS:
        content = content[:_MAX_CONTENT_CHARS] + f"\n\n[...truncated at {_MAX_CONTENT_CHARS} chars]"

    return content


# ---------------------------------------------------------------------------
# Tool: fetch_package_metadata
# ---------------------------------------------------------------------------


async def tool_fetch_package_metadata(
    ecosystem: str,
    package_name: str,
) -> str:
    """
    Fetch package metadata from PyPI or npm.

    Returns latest version, license, summary/description, and key metadata.
    Returns a descriptive error string on 404, invalid ecosystem, or network failure.
    """
    ecosystem = ecosystem.strip().lower()
    if ecosystem not in {"pypi", "npm"}:
        return (
            f"Error: unsupported ecosystem {ecosystem!r}. "
            "Supported ecosystems are 'pypi' and 'npm'."
        )

    if not package_name.strip():
        return "Error: package_name must not be empty."

    if ecosystem == "pypi":
        return await _fetch_pypi_metadata(package_name)
    else:
        return await _fetch_npm_metadata(package_name)


async def _fetch_pypi_metadata(package_name: str) -> str:
    """Fetch and format PyPI package metadata."""
    url = f"https://pypi.org/pypi/{package_name}/json"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        logger.warning("tool_fetch_package_metadata: PyPI HTTP error for %s: %s", package_name, exc)
        return f"Error: PyPI request failed — {exc}"

    if response.status_code == 404:
        return f"Package '{package_name}' not found on pypi."
    if response.status_code == 429:
        return "Error: PyPI rate limit exceeded. Please retry in a moment."
    if response.status_code >= 500:
        return f"Error: PyPI returned {response.status_code}. Try again later."
    if not response.is_success:
        return f"Error: PyPI returned HTTP {response.status_code}."

    try:
        data = response.json()
    except Exception:
        return "Error: could not parse PyPI response."

    info: dict[str, Any] = data.get("info", {})
    latest_version = info.get("version", "unknown")
    license_name = info.get("license") or "unknown"
    summary = info.get("summary") or "No summary available."
    requires_python = info.get("requires_python") or "any"
    home_page = info.get("home_page") or info.get("project_url") or ""

    lines = [
        f"**Package**: {package_name} (PyPI)",
        f"**Latest version**: {latest_version}",
        f"**License**: {license_name}",
        f"**Python requires**: {requires_python}",
        f"**Summary**: {summary}",
    ]
    if home_page:
        lines.append(f"**Homepage**: {home_page}")

    return "\n".join(lines)


async def _fetch_npm_metadata(package_name: str) -> str:
    """Fetch and format npm registry package metadata."""
    url = f"https://registry.npmjs.org/{package_name}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        logger.warning("tool_fetch_package_metadata: npm HTTP error for %s: %s", package_name, exc)
        return f"Error: npm registry request failed — {exc}"

    if response.status_code == 404:
        return f"Package '{package_name}' not found on npm."
    if response.status_code == 429:
        return "Error: npm registry rate limit exceeded. Please retry in a moment."
    if response.status_code >= 500:
        return f"Error: npm registry returned {response.status_code}. Try again later."
    if not response.is_success:
        return f"Error: npm registry returned HTTP {response.status_code}."

    try:
        data = response.json()
    except Exception:
        return "Error: could not parse npm registry response."

    dist_tags: dict[str, Any] = data.get("dist-tags", {})
    latest_version = dist_tags.get("latest", "unknown")
    description = data.get("description") or "No description available."

    # License can be a string or object.
    license_raw = data.get("license")
    if isinstance(license_raw, dict):
        license_name = license_raw.get("type") or "unknown"
    else:
        license_name = license_raw or "unknown"

    # Dependencies are per-version; pull from the latest version entry.
    versions: dict[str, Any] = data.get("versions", {})
    latest_version_data: dict[str, Any] = versions.get(latest_version, {})
    deps: dict[str, str] = latest_version_data.get("dependencies", {})
    homepage = data.get("homepage") or ""

    lines = [
        f"**Package**: {package_name} (npm)",
        f"**Latest version**: {latest_version}",
        f"**License**: {license_name}",
        f"**Description**: {description}",
    ]
    if deps:
        dep_list = ", ".join(f"`{k}@{v}`" for k, v in list(deps.items())[:10])
        if len(deps) > 10:
            dep_list += f" (and {len(deps) - 10} more)"
        lines.append(f"**Dependencies**: {dep_list}")
    else:
        lines.append("**Dependencies**: none")
    if homepage:
        lines.append(f"**Homepage**: {homepage}")

    return "\n".join(lines)
