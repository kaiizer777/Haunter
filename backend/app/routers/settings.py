"""
Repository operational governance & feature settings API router.

Endpoints:
- GET /api/repos/{repo_id}/settings: Fetch current governance settings or default fallback.
- PATCH /api/repos/{repo_id}/settings: Partial update of settings with schema validation.
- POST /api/repos/{repo_id}/settings/preset: Apply a governance preset (body or path param).
- GET /api/settings/repos: List all connected repos with their governance toggles (future02.md).
- PATCH /api/settings/repos/{repo_id}: Alias for settings patch.
- POST /api/settings/repos/{repo_id}/preset/{preset_name}: Alias for preset application.

Security invariants:
- All endpoints require authentication via get_current_user.
- Strict multi-tenant isolation: every query is scoped to repos owned by current_user.id.
- Returns 404 for unowned repos to prevent existence oracle leaks (OWASP API1).
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import get_current_user
from app.db import get_db
from app.models import Repo, User
from app.schemas import (
    PresetApplyIn,
    RepoSettingsOut,
    RepoSettingsUpdate,
    RepoWithSettingsOut,
)
from app.services.repo_settings import (
    apply_preset,
    create_default_repo_settings,
    get_repo_settings,
    normalize_preset_name,
    update_repo_settings,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["settings"])


async def _get_user_repo_or_404(
    db: AsyncSession,
    repo_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Repo:
    """Verify repo exists and is owned by the authenticated caller."""
    repo = await db.scalar(
        select(Repo).where(Repo.id == repo_id, Repo.user_id == user_id)
    )
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repo not found",
        )
    return repo


# ---------------------------------------------------------------------------
# Primary Repo Settings Endpoints
# ---------------------------------------------------------------------------


@router.get("/repos/{repo_id}/settings", response_model=RepoSettingsOut)
@router.get("/api/repos/{repo_id}/settings", response_model=RepoSettingsOut, include_in_schema=False)
async def get_settings(
    repo_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RepoSettingsOut:
    """
    Get repository governance settings.
    If no settings row exists yet, returns fallback defaults.
    """
    await _get_user_repo_or_404(db, repo_id, current_user.id)
    settings = await get_repo_settings(db, repo_id, auto_create=False)
    return RepoSettingsOut.model_validate(settings)


@router.patch("/repos/{repo_id}/settings", response_model=RepoSettingsOut)
@router.patch("/api/repos/{repo_id}/settings", response_model=RepoSettingsOut, include_in_schema=False)
async def patch_settings(
    repo_id: uuid.UUID,
    body: RepoSettingsUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RepoSettingsOut:
    """
    Partially update repository governance settings and feature switches.
    """
    await _get_user_repo_or_404(db, repo_id, current_user.id)
    updates = body.model_dump(exclude_unset=True)
    # Remove nested helper keys that were already flattened by schema
    updates.pop("features", None)
    updates.pop("audit_triggers", None)

    updated = await update_repo_settings(db, repo_id, updates)
    logger.info("Updated repo settings: repo_id=%s user=%s version=%d", repo_id, current_user.id, updated.settings_version)
    return RepoSettingsOut.model_validate(updated)


@router.post("/repos/{repo_id}/settings/preset", response_model=RepoSettingsOut)
@router.post("/api/repos/{repo_id}/settings/preset", response_model=RepoSettingsOut, include_in_schema=False)
async def post_preset(
    repo_id: uuid.UUID,
    body: PresetApplyIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RepoSettingsOut:
    """
    Apply a named governance preset to the repository.
    """
    await _get_user_repo_or_404(db, repo_id, current_user.id)
    settings = await get_repo_settings(db, repo_id, auto_create=True)
    apply_preset(settings, body.preset)
    await db.commit()
    await db.refresh(settings)
    logger.info("Applied preset '%s' to repo_id=%s by user=%s", body.preset, repo_id, current_user.id)
    return RepoSettingsOut.model_validate(settings)


@router.post("/repos/{repo_id}/settings/preset/{preset_name}", response_model=RepoSettingsOut)
@router.post("/api/repos/{repo_id}/settings/preset/{preset_name}", response_model=RepoSettingsOut, include_in_schema=False)
async def post_preset_path(
    repo_id: uuid.UUID,
    preset_name: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RepoSettingsOut:
    """
    1-click shortcut to apply a named governance preset via path parameter.
    """
    try:
        canonical = normalize_preset_name(preset_name)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    await _get_user_repo_or_404(db, repo_id, current_user.id)
    settings = await get_repo_settings(db, repo_id, auto_create=True)
    apply_preset(settings, canonical)
    await db.commit()
    await db.refresh(settings)
    logger.info("Applied preset '%s' via path to repo_id=%s by user=%s", canonical, repo_id, current_user.id)
    return RepoSettingsOut.model_validate(settings)


# ---------------------------------------------------------------------------
# FUTURE02.md Section 2.3 Compatibility Endpoints
# ---------------------------------------------------------------------------


@router.get("/api/settings/repos", response_model=list[RepoWithSettingsOut])
@router.get("/settings/repos", response_model=list[RepoWithSettingsOut], include_in_schema=False)
async def list_repos_with_settings(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[RepoWithSettingsOut]:
    """
    Return all linked repositories owned by current user with active settings.
    Conforms to FUTURE02.md 2.3 GET /api/settings/repos contract.
    """
    result = await db.execute(
        select(Repo)
        .options(selectinload(Repo.settings))
        .where(Repo.user_id == current_user.id)
        .order_by(Repo.created_at.desc())
    )
    repos = result.scalars().all()

    items: list[RepoWithSettingsOut] = []
    for repo in repos:
        settings = repo.settings
        if settings is None:
            settings = create_default_repo_settings(repo.id)
        items.append(
            RepoWithSettingsOut(
                repo_id=repo.id,
                repo_full_name=f"{repo.owner}/{repo.name}",
                preset_profile=settings.preset,
                features={
                    "auto_fixer": settings.enable_auto_fix,
                    "auditor_mode": settings.enable_auditor_mode,
                    "live_sessions": settings.enable_live_sessions,
                    "webcontainer_preview": settings.enable_webcontainer_preview,
                    "ci_sandbox": settings.enable_sandbox_verification,
                    "subagents": settings.enable_subagents,
                },
                audit_triggers={
                    "on_pr": settings.audit_trigger_on_pr,
                    "on_ci_failure": settings.audit_trigger_on_ci_failure,
                    "on_ci_success": settings.audit_trigger_on_ci_success,
                    "on_manual_mention": settings.audit_trigger_on_manual_mention,
                },
                monitored_branches=settings.allowed_branches,
            )
        )
    return items


@router.patch("/api/settings/repos/{repo_id}", response_model=RepoSettingsOut)
@router.patch("/settings/repos/{repo_id}", response_model=RepoSettingsOut, include_in_schema=False)
async def patch_settings_legacy_path(
    repo_id: uuid.UUID,
    body: RepoSettingsUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RepoSettingsOut:
    """Alias for PATCH /api/repos/{repo_id}/settings."""
    return await patch_settings(repo_id, body, current_user, db)


@router.post("/api/settings/repos/{repo_id}/preset/{preset_name}", response_model=RepoSettingsOut)
@router.post("/settings/repos/{repo_id}/preset/{preset_name}", response_model=RepoSettingsOut, include_in_schema=False)
async def post_preset_legacy_path(
    repo_id: uuid.UUID,
    preset_name: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RepoSettingsOut:
    """Alias for POST /api/repos/{repo_id}/settings/preset/{preset_name}."""
    return await post_preset_path(repo_id, preset_name, current_user, db)
