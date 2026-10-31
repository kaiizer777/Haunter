"""Custom provider endpoints for Haunter (issue #205).

Admin-only CRUD plus compat-mode discovery refresh. Secrets are accepted
once on write, encrypted at rest, and redacted in every response.
"""

import logging
import uuid
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, is_admin_user
from app.db import get_db
from app.llm.custom_provider import (
    CustomProvider,
    refresh_custom_provider_models,
    to_public_dict,
    upsert_custom_provider,
)
from app.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/config/custom-providers", tags=["custom_providers"])


def _require_admin(current_user: User) -> None:
    if not is_admin_user(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin permissions required",
        )


class CustomProviderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    compat: str = Field(pattern="^(openai|anthropic)$")
    base_url: str = Field(min_length=1, max_length=1024)
    api_key: str = Field(min_length=1, max_length=4096)


class CustomProviderUpdate(BaseModel):
    compat: Optional[str] = Field(default=None, pattern="^(openai|anthropic)$")
    base_url: Optional[str] = Field(default=None, min_length=1, max_length=1024)
    api_key: Optional[str] = Field(default=None, min_length=1, max_length=4096)
    models: Optional[list[str]] = Field(default=None, max_length=500)


@router.get("")
async def list_custom_providers(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict]:
    _require_admin(current_user)
    rows = (await db.execute(select(CustomProvider))).scalars().all()
    return [to_public_dict(p) for p in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_custom_provider(
    payload: CustomProviderCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    _require_admin(current_user)
    try:
        provider, created = await upsert_custom_provider(
            db,
            name=payload.name,
            compat=payload.compat,
            base_url=payload.base_url,
            api_key=payload.api_key,
            created_by=current_user.id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from None
    await db.commit()
    return {"created": created, "provider": to_public_dict(provider)}


@router.get("/{provider_id}")
async def get_custom_provider(
    provider_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    _require_admin(current_user)
    provider = (
        await db.execute(
            select(CustomProvider).where(CustomProvider.id == provider_id)
        )
    ).scalar_one_or_none()
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Provider not found"
        )
    return to_public_dict(provider)


@router.patch("/{provider_id}")
async def update_custom_provider(
    provider_id: uuid.UUID,
    payload: CustomProviderUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    _require_admin(current_user)
    provider = (
        await db.execute(
            select(CustomProvider).where(CustomProvider.id == provider_id)
        )
    ).scalar_one_or_none()
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Provider not found"
        )
    from app.auth import _encrypt_token
    from app.llm.discovery import validate_discovery_url

    try:
        if payload.compat is not None:
            from app.llm.custom_provider import CustomProviderCompat

            try:
                provider.compat = CustomProviderCompat(payload.compat).value
            except ValueError:
                raise ValueError(
                    "compat must be 'openai' or 'anthropic'"
                ) from None
        if payload.base_url is not None:
            provider.base_url = validate_discovery_url(payload.base_url)
        if payload.api_key is not None:
            if not payload.api_key.strip():
                raise ValueError("api_key must not be blank")
            provider.api_key_encrypted = _encrypt_token(payload.api_key.strip())
        if payload.models is not None:
            provider.models_json = list(payload.models)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from None
    await db.flush()
    await db.commit()
    return to_public_dict(provider)


@router.delete("/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_custom_provider(
    provider_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    _require_admin(current_user)
    await db.execute(
        delete(CustomProvider).where(CustomProvider.id == provider_id)
    )
    await db.commit()


@router.post("/{provider_id}/discover")
async def discover_custom_provider(
    provider_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Refresh the provider model list via compat-mode discovery.

    Failures degrade to the stored manual-entry list without raising.
    """
    _require_admin(current_user)
    provider = (
        await db.execute(
            select(CustomProvider).where(CustomProvider.id == provider_id)
        )
    ).scalar_one_or_none()
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Provider not found"
        )
    models = await refresh_custom_provider_models(db, provider)
    await db.commit()
    return {"provider_id": str(provider.id), "models": models}
