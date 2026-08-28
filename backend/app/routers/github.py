"""
GitHub integration router for Haunter.

Provides GitHub API proxying and available repository discovery for the authenticated user.
Enforces multi-tenant isolation, token decryption at rest, rate limit handling,
pagination, and permission filtering.
"""

import logging
from typing import Annotated, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import _decrypt_token, get_current_user
from app.db import get_db
from app.models import Repo, User
from app.schemas import AvailableRepoOut

logger = logging.getLogger(__name__)

router = APIRouter(tags=["github"])

_GITHUB_API_BASE = "https://api.github.com"
_DEFAULT_TIMEOUT_SECONDS = 15.0
_MAX_PAGES = 3  # MVP limit: 3 pages @ 100 repos/page = 300 repos max


def _parse_next_link(response: httpx.Response) -> Optional[str]:
    """
    Extract the next page URL from GitHub API Link header if present.
    Supports httpx .links dictionary and standard RFC5988 header fallback.
    """
    if hasattr(response, "links") and "next" in response.links:
        return response.links["next"].get("url")

    link_header = response.headers.get("link")
    if not link_header:
        return None

    for part in link_header.split(","):
        sections = part.strip().split(";")
        if len(sections) >= 2:
            url_part = sections[0].strip()
            rel_part = sections[1].strip()
            if rel_part == 'rel="next"' and url_part.startswith("<") and url_part.endswith(">"):
                return url_part[1:-1]
    return None


@router.get("/github/available-repos", response_model=list[AvailableRepoOut])
async def list_available_repos(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[AvailableRepoOut]:
    """
    List GitHub repositories accessible to the authenticated user that have push
    or admin permissions.

    1. Decrypts user's stored GitHub access token (at rest protection).
    2. Fetches repos from GitHub REST API with pagination up to 300 repos max.
    3. Filters out repositories where the user lacks push/admin permissions.
    4. Compares with tenant's connected repos in DB (WHERE user_id = current_user.id)
       to set already_connected=True without leaking cross-tenant status.
    5. Returns repositories sorted by updated_at descending.
    """
    if not current_user.access_token:
        raise HTTPException(
            status_code=401,
            detail="GitHub not connected - please re-login",
        )

    try:
        token = _decrypt_token(current_user.access_token)
    except Exception:
        logger.error("Failed to decrypt access token for user %s", current_user.id)
        raise HTTPException(
            status_code=401,
            detail="GitHub not connected - please re-login",
        )

    if not token:
        raise HTTPException(
            status_code=401,
            detail="GitHub not connected - please re-login",
        )

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Haunter-Autonomous-Agent/1.0",
    }

    raw_repos: list[dict] = []
    next_url: Optional[str] = (
        f"{_GITHUB_API_BASE}/user/repos"
        "?per_page=100&sort=updated&affiliation=owner,collaborator,organization_member"
    )

    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_SECONDS) as client:
        for _ in range(_MAX_PAGES):
            if not next_url:
                break
            try:
                response = await client.get(next_url, headers=headers)
            except httpx.RequestError as exc:
                logger.error(
                    "Network error connecting to GitHub API for user %s: %s",
                    current_user.id,
                    exc.__class__.__name__,
                )
                raise HTTPException(status_code=502, detail="Failed to connect to GitHub") from exc

            if response.status_code == 401:
                logger.warning(
                    "GitHub returned 401 (token expired/revoked/insufficient scope) for user %s",
                    current_user.id,
                )
                raise HTTPException(
                    status_code=401,
                    detail="Please re-login to grant repo access",
                )

            if response.status_code in (403, 429):
                retry_after = response.headers.get("Retry-After")
                resp_headers = {"Retry-After": retry_after} if retry_after else None
                logger.warning("GitHub rate limit encountered for user %s", current_user.id)
                raise HTTPException(
                    status_code=429,
                    detail="GitHub rate limit exceeded",
                    headers=resp_headers,
                )

            if response.is_server_error or response.status_code >= 500:
                logger.error(
                    "GitHub API 5xx error (%d) for user %s",
                    response.status_code,
                    current_user.id,
                )
                raise HTTPException(status_code=502, detail="GitHub API error")

            if response.is_error:
                logger.error(
                    "GitHub API error (%d) for user %s",
                    response.status_code,
                    current_user.id,
                )
                raise HTTPException(status_code=502, detail="GitHub API error")

            try:
                page_data = response.json()
            except Exception as exc:
                logger.error("Failed to parse GitHub JSON response for user %s", current_user.id)
                raise HTTPException(status_code=502, detail="Invalid response from GitHub") from exc

            if not isinstance(page_data, list):
                break

            raw_repos.extend(page_data)
            next_url = _parse_next_link(response)

    # Multi-tenant isolation: find all connected repos strictly owned by current user
    result = await db.execute(
        select(Repo.owner, Repo.name).where(Repo.user_id == current_user.id)
    )
    connected_set = {(row[0].lower(), row[1].lower()) for row in result.all()}

    # Filter and map repos
    available: list[AvailableRepoOut] = []
    for r in raw_repos:
        if not isinstance(r, dict):
            continue

        perms = r.get("permissions") or {}
        has_push = bool(perms.get("push"))
        has_admin = bool(perms.get("admin"))
        if not (has_push or has_admin):
            continue

        owner_info = r.get("owner")
        owner_login = owner_info.get("login", "") if isinstance(owner_info, dict) else ""
        repo_name = r.get("name", "")
        if not owner_login or not repo_name:
            continue

        full_name = r.get("full_name") or f"{owner_login}/{repo_name}"
        is_connected = (owner_login.lower(), repo_name.lower()) in connected_set

        available.append(
            AvailableRepoOut(
                owner=owner_login,
                name=repo_name,
                full_name=full_name,
                default_branch=r.get("default_branch"),
                language=r.get("language"),
                private=bool(r.get("private", False)),
                updated_at=r.get("updated_at"),
                already_connected=is_connected,
                permissions_push=has_push or has_admin,
            )
        )

    # Sort descending by updated_at (None placed last)
    available.sort(key=lambda item: item.updated_at or "", reverse=True)
    return available
