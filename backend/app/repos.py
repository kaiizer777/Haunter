"""
Repo CRUD endpoints for Haunter.

All endpoints are gated by get_current_user. Every query and mutation is scoped
to the authenticated user's user.id — no endpoint trusts a client-supplied repo_id
without an ownership check against the current user.

Security invariant (multi-tenant isolation):
- DELETE/mutating endpoints return 404 (not 403) when a repo exists but isn't owned
  by the caller — prevents existence oracle leakage to non-owners.
- GET /repos lists ONLY repos owned by the current user (WHERE user_id = current_user.id).
- All SQL uses parameterised ORM constructs — no raw string interpolation.
"""

import logging
import re
import uuid
from typing import Annotated, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import _decrypt_token, get_current_user
from app.db import get_db
from app.github.pr import resolve_installation_id
from app.models import Repo, User
from app.schemas import (
    RepoAuditorInstallUpdate,
    RepoCreate,
    RepoOut,
    validate_repo_ident,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["repos"])

_GITHUB_API_BASE = "https://api.github.com"
_DEFAULT_TIMEOUT_SECONDS = 15.0

_REPO_IDENT_RE: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9_.\-]+$")


def _validate_repo_ident(value: str, label: str) -> None:
    """Validate owner or repo name to prevent SSRF / path traversal."""
    try:
        validate_repo_ident(value, label)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid repository {label}: {value!r}. Only alphanumeric characters, '.', '_', and '-' are allowed without path traversal segments.",
        ) from exc


async def _verify_user_repo_permission(
    current_user: User, owner: str, name: str
) -> None:
    """
    Verify that the authenticated user has push or admin permissions on GitHub for owner/name.
    Raises HTTPException(400) if owner or name is invalid.
    Raises HTTPException(403) if access is unauthorized or token is missing/invalid.
    """
    _validate_repo_ident(owner, "owner")
    _validate_repo_ident(name, "name")
    if not current_user.access_token:
        logger.warning(
            "repos: user %s has no access token to verify repo %s/%s",
            current_user.id,
            owner,
            name,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Push or admin permissions on GitHub repository required",
        )

    try:
        token = _decrypt_token(current_user.access_token)
    except Exception:
        logger.error(
            "repos: failed to decrypt access token for user %s", current_user.id
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Push or admin permissions on GitHub repository required",
        )

    if not token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Push or admin permissions on GitHub repository required",
        )

    url = f"{_GITHUB_API_BASE}/repos/{owner}/{name}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "Haunter-Autonomous-Agent/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_SECONDS) as client:
        try:
            resp = await client.get(url, headers=headers)
        except httpx.RequestError as exc:
            logger.error(
                "Network error verifying repo %s/%s for user %s: %s",
                owner,
                name,
                current_user.id,
                exc.__class__.__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to connect to GitHub to verify repository permissions",
            ) from exc

    if resp.status_code in (401, 403, 404):
        logger.warning(
            "GitHub rejected permission check (%d) for repo %s/%s user %s",
            resp.status_code,
            owner,
            name,
            current_user.id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Push or admin permissions on GitHub repository required",
        )

    if resp.is_error:
        logger.error(
            "GitHub returned %d during repo permission check for %s/%s",
            resp.status_code,
            owner,
            name,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to verify repository permissions with GitHub",
        )

    try:
        data = resp.json()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Invalid response from GitHub",
        ) from exc

    perms = data.get("permissions") or {}
    has_push = bool(perms.get("push"))
    has_admin = bool(perms.get("admin"))
    if not (has_push or has_admin):
        logger.warning(
            "User %s lacks push/admin permissions on %s/%s (permissions=%r)",
            current_user.id,
            owner,
            name,
            perms,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Push or admin permissions on GitHub repository required",
        )


# ---------------------------------------------------------------------------
# Repo CRUD
# ---------------------------------------------------------------------------


@router.post("/repos", response_model=RepoOut, status_code=201)
async def add_repo(
    body: RepoCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RepoOut:
    """
    Add a repo to the current user's workspace.
    Enforces (user_id, owner, name) uniqueness — same public repo can be tracked
    by two different tenants independently.
    Verifies user push/admin permissions on GitHub and validates GitHub App installation.
    """
    # 1. Validate repository identifiers and verify user's push/admin access on GitHub
    _validate_repo_ident(body.owner, "owner")
    _validate_repo_ident(body.name, "name")
    await _verify_user_repo_permission(current_user, body.owner, body.name)

    # 2. Check for duplicate under this user before insert.
    existing = await db.execute(
        select(Repo).where(
            Repo.user_id == current_user.id,
            Repo.owner == body.owner,
            Repo.name == body.name,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="Repo already connected")

    # 3. Resolve / validate GitHub App installation ID
    validated_install_id: Optional[int] = None
    try:
        validated_install_id = await resolve_installation_id(body.owner, body.name)
    except Exception as exc:
        logger.debug(
            "GitHub App installation not resolved for %s/%s: %s",
            body.owner,
            body.name,
            exc,
        )
        validated_install_id = None

    if body.github_install_id is not None:
        if (
            validated_install_id is not None
            and body.github_install_id != validated_install_id
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Supplied github_install_id does not match the verified GitHub App installation for this repository.",
            )
        final_install_id = validated_install_id
    else:
        final_install_id = validated_install_id

    repo = Repo(
        user_id=current_user.id,
        owner=body.owner,
        name=body.name,
        default_branch=body.default_branch,
        language_hint=body.language_hint,
        active_model_config_id=body.active_model_config_id,
        github_install_id=final_install_id,
    )
    db.add(repo)
    try:
        await db.commit()
        await db.refresh(repo)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Repo already connected")

    logger.info(
        "Repo added: user=%s repo=%s/%s install_id=%s",
        current_user.id,
        body.owner,
        body.name,
        final_install_id,
    )
    return RepoOut.model_validate(repo)


@router.get("/repos", response_model=list[RepoOut])
async def list_repos(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[RepoOut]:
    """
    List all repos owned by the current user.
    Scoped strictly to WHERE user_id = current_user.id — no cross-tenant leakage.
    """
    result = await db.execute(
        select(Repo)
        .where(Repo.user_id == current_user.id)
        .order_by(Repo.created_at.desc())
    )
    repos = result.scalars().all()
    return [RepoOut.model_validate(r) for r in repos]


@router.delete("/repos/{repo_id}", status_code=204)
async def remove_repo(
    repo_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """
    Remove a repo. Returns 404 whether the repo doesn't exist OR isn't owned by
    the current user — prevents existence oracle leakage to non-owners.
    """
    result = await db.execute(
        select(Repo).where(Repo.id == repo_id, Repo.user_id == current_user.id)
    )
    repo = result.scalar_one_or_none()
    if repo is None:
        raise HTTPException(status_code=404, detail="Repo not found")

    await db.delete(repo)
    await db.commit()
    logger.info("Repo removed: user=%s repo_id=%s", current_user.id, repo_id)


@router.patch("/repos/{repo_id}/auditor-install", response_model=RepoOut)
async def set_repo_auditor_install(
    repo_id: uuid.UUID,
    body: RepoAuditorInstallUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RepoOut:
    """
    Set or clear the auditor GitHub App installation id for a repo owned by
    the current user. Returns 404 whether the repo doesn't exist OR isn't
    owned by the caller — prevents existence oracle leakage to non-owners.
    """
    result = await db.execute(
        select(Repo).where(Repo.id == repo_id, Repo.user_id == current_user.id)
    )
    repo = result.scalar_one_or_none()
    if repo is None:
        raise HTTPException(status_code=404, detail="Repo not found")

    repo.auditor_github_install_id = body.auditor_github_install_id
    await db.commit()
    await db.refresh(repo)
    logger.info(
        "Repo auditor install updated: user=%s repo_id=%s", current_user.id, repo_id
    )
    return RepoOut.model_validate(repo)
