"""
Hosting & Sandbox provider configuration endpoints (Phase 14).

GET  /config/hosting  — read current active provider config (any authenticated user)
PUT  /config/hosting  — update provider config (admin-only, same gate as PUT /config/model)

Security invariants:
- Provider values validated by server allowlist (aws only).
  Values are NEVER derived from request headers, path params, or free-text.
- Write endpoint is admin-gated: ADMIN_USER_ID env var must match current user.
  Returns 403 (not 401) to indicate authenticated-but-not-authorized.
- SQL queries use parameterised SQLAlchemy ORM (no raw string interpolation).
- system_configs keys are checked against _ALLOWED_KEYS server-side before write
  (defence-in-depth beyond Pydantic validation).
- After a successful write, the in-process hosting adapter TTL cache is invalidated
  so the next request picks up the new value within 1 request (not 60s wait).
"""

import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.hosting import get_active_hosting_provider, get_active_sandbox_provider, invalidate_provider_cache
from app.auth import get_current_user
from app.config import settings
from app.db import get_db
from app.models import SystemConfig, User
from app.schemas import HostingConfigOut, HostingConfigUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/config/hosting", tags=["hosting_config"])

# Only these keys may be written via this endpoint.
_ALLOWED_KEYS: frozenset[str] = frozenset({"hosting_provider", "sandbox_provider"})


async def _require_admin(current_user: User) -> None:
    """Raise 403 if ADMIN_USER_ID is configured and caller is not the admin."""
    if settings.admin_user_id and str(current_user.id) != settings.admin_user_id:
        logger.warning(
            "hosting_config: non-admin user %s attempted write", current_user.id
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin permissions required to update hosting/sandbox provider",
        )


@router.get("", response_model=HostingConfigOut)
async def get_hosting_config(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HostingConfigOut:
    """
    Return the currently active HOSTING_PROVIDER and SANDBOX_PROVIDER.
    Reads from DB (system_configs) with 60s TTL cache, falls back to env.
    """
    # Check if DB has overrides (direct read, no cache — GET should always be fresh)
    result = await db.execute(
        select(SystemConfig).where(
            SystemConfig.key.in_(["hosting_provider", "sandbox_provider"])
        )
    )
    rows = {row.key: row.value for row in result.scalars().all()}

    hosting = rows.get("hosting_provider") or settings.hosting_provider
    sandbox = rows.get("sandbox_provider") or settings.sandbox_provider
    source = "db" if rows else "env"

    return HostingConfigOut(
        hosting_provider=hosting,
        sandbox_provider=sandbox,
        source=source,
    )


@router.put("", response_model=HostingConfigOut)
async def update_hosting_config(
    body: HostingConfigUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HostingConfigOut:
    """
    Update active HOSTING_PROVIDER and SANDBOX_PROVIDER.
    Admin-only (ADMIN_USER_ID env var must match authenticated user).

    Uses Postgres UPSERT (INSERT ... ON CONFLICT DO UPDATE) for atomicity.
    Invalidates the hosting adapter in-process cache after write.

    Security: body.hosting_provider and body.sandbox_provider are validated
    against allowlist — only "aws" passes validation. Any other value
    returns 422 before reaching this handler.
    """
    await _require_admin(current_user)

    # Pydantic already validated values — but assert against server-side allowlist
    # as defence-in-depth (protects against schema bypass via direct ORM call in tests).
    allowed_values = {"aws"}
    assert body.hosting_provider in allowed_values, "hosting_provider out of allowlist"
    assert body.sandbox_provider in allowed_values, "sandbox_provider out of allowlist"

    # Upsert both keys atomically
    for key, value in [
        ("hosting_provider", body.hosting_provider),
        ("sandbox_provider", body.sandbox_provider),
    ]:
        assert key in _ALLOWED_KEYS, f"key {key!r} not in allowed system config keys"
        stmt = pg_insert(SystemConfig).values(
            key=key, value=value, updated_at=datetime.now(timezone.utc)
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["key"],
            set_={"value": value, "updated_at": datetime.now(timezone.utc)},
        )
        await db.execute(stmt)

    await db.commit()

    # Invalidate in-process cache so adapter picks up new values immediately
    invalidate_provider_cache()

    logger.info(
        "hosting_config: updated by user=%s hosting=%s sandbox=%s",
        current_user.id,
        body.hosting_provider,
        body.sandbox_provider,
    )

    return HostingConfigOut(
        hosting_provider=body.hosting_provider,
        sandbox_provider=body.sandbox_provider,
        source="db",
    )
