"""
Repository operational governance & feature settings service layer.

Provides:
- Loading repo settings with fallback to default preset profile
- Preset application (autonomous, conservative, standard, audit_only, live_studio_only, custom)
- Updating settings with validation (branch names, cost bounds)
- Multi-tenant repo access and ownership verification
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RepoSettings

logger = logging.getLogger(__name__)

# Canonical preset names and alias mapping
CANONICAL_PRESET_MAP: dict[str, str] = {
    "autonomous": "autonomous",
    "full_autonomous": "autonomous",
    "conservative": "conservative",
    "standard": "standard",
    "audit_only": "audit_only",
    "auditor_only": "audit_only",
    "live_studio_only": "live_studio_only",
    "custom": "custom",
}

ALLOWED_PRESETS: set[str] = set(CANONICAL_PRESET_MAP.keys())

DEFAULT_PRESET = "autonomous"

PRESET_CONFIGURATIONS: dict[str, dict[str, Any]] = {
    "autonomous": {
        "preset": "autonomous",
        "enable_auto_fix": True,
        "enable_auditor_mode": False,
        "enable_sandbox_verification": True,
        "enable_pr_comments": True,
        "enable_live_sessions": True,
        "enable_webcontainer_preview": True,
        "audit_trigger_on_pr": True,
        "audit_trigger_on_ci_failure": True,
        "audit_trigger_on_ci_success": False,
        "audit_trigger_on_manual_mention": True,
        "ignore_draft_prs": True,
        "max_cost_per_run_cents": 100,
        "model_override_scope": "inherit",
    },
    "conservative": {
        "preset": "conservative",
        "enable_auto_fix": False,
        "enable_auditor_mode": True,
        "enable_sandbox_verification": True,
        "enable_pr_comments": True,
        "enable_live_sessions": False,
        "enable_webcontainer_preview": False,
        "audit_trigger_on_pr": True,
        "audit_trigger_on_ci_failure": True,
        "audit_trigger_on_ci_success": False,
        "audit_trigger_on_manual_mention": True,
        "ignore_draft_prs": True,
        "max_cost_per_run_cents": 50,
        "model_override_scope": "inherit",
    },
    "standard": {
        "preset": "standard",
        "enable_auto_fix": True,
        "enable_auditor_mode": True,
        "enable_sandbox_verification": True,
        "enable_pr_comments": True,
        "enable_live_sessions": True,
        "enable_webcontainer_preview": True,
        "audit_trigger_on_pr": True,
        "audit_trigger_on_ci_failure": True,
        "audit_trigger_on_ci_success": False,
        "audit_trigger_on_manual_mention": True,
        "ignore_draft_prs": True,
        "max_cost_per_run_cents": 100,
        "model_override_scope": "inherit",
    },
    "audit_only": {
        "preset": "audit_only",
        "enable_auto_fix": False,
        "enable_auditor_mode": True,
        "enable_sandbox_verification": False,
        "enable_pr_comments": True,
        "enable_live_sessions": False,
        "enable_webcontainer_preview": False,
        "audit_trigger_on_pr": True,
        "audit_trigger_on_ci_failure": True,
        "audit_trigger_on_ci_success": False,
        "audit_trigger_on_manual_mention": True,
        "ignore_draft_prs": True,
        "max_cost_per_run_cents": 50,
        "model_override_scope": "inherit",
    },
    "live_studio_only": {
        "preset": "live_studio_only",
        "enable_auto_fix": False,
        "enable_auditor_mode": False,
        "enable_sandbox_verification": True,
        "enable_pr_comments": False,
        "enable_live_sessions": True,
        "enable_webcontainer_preview": True,
        "audit_trigger_on_pr": False,
        "audit_trigger_on_ci_failure": False,
        "audit_trigger_on_ci_success": False,
        "audit_trigger_on_manual_mention": False,
        "ignore_draft_prs": True,
        "max_cost_per_run_cents": 100,
        "model_override_scope": "inherit",
    },
    "custom": {
        "preset": "custom",
    },
}

# Git branch ref rules (subset conforming to git-check-ref-format, permitting glob wildcards *, ?, [)
_ILLEGAL_BRANCH_CHARS = re.compile(r"[\s~^:\\]|@{|\.\.")


def normalize_preset_name(preset_name: str) -> str:
    """Normalize and validate preset name. Raises ValueError on unknown presets."""
    clean = preset_name.strip().lower()
    canonical = CANONICAL_PRESET_MAP.get(clean)
    if canonical is None:
        valid_presets = sorted(ALLOWED_PRESETS)
        raise ValueError(
            f"Invalid governance preset: '{preset_name}'. Allowed presets: {valid_presets}"
        )
    return canonical


def validate_branch_name(branch: str) -> str:
    """Validate a single git branch name string."""
    if not isinstance(branch, str) or not branch.strip():
        raise ValueError("Branch name must be a non-empty string.")
    if any(ord(c) < 32 or ord(c) == 127 for c in branch):
        raise ValueError(f"Branch name '{branch}' contains ASCII control characters.")
    clean = branch.strip()
    if len(clean) > 255:
        raise ValueError(
            f"Branch name '{clean}' exceeds maximum length of 255 characters."
        )
    if clean.startswith("/") or clean.endswith("/"):
        raise ValueError(f"Branch name '{clean}' cannot begin or end with '/'.")
    if clean.startswith(".") or clean.endswith("."):
        raise ValueError(f"Branch name '{clean}' cannot begin or end with '.'.")
    if clean.endswith(".lock"):
        raise ValueError(f"Branch name '{clean}' cannot end with '.lock'.")
    if _ILLEGAL_BRANCH_CHARS.search(clean):
        raise ValueError(
            f"Branch name '{clean}' contains invalid characters or sequences (whitespace, ~, ^, :, \\, .., @{{)."
        )
    return clean


def validate_branches(branches: Sequence[str]) -> list[str]:
    """Validate and normalize a sequence of branch names."""
    if not isinstance(branches, (list, tuple)):
        raise ValueError("Allowed branches must be a list of strings.")
    validated = [validate_branch_name(b) for b in branches]
    return validated


def validate_cost_bounds(cost_cents: int) -> int:
    """Validate max cost per run in cents. Must be non-negative integer."""
    if not isinstance(cost_cents, int) or isinstance(cost_cents, bool):
        raise ValueError("Max cost per run must be an integer.")
    if cost_cents < 0:
        raise ValueError(f"Max cost per run cannot be negative (got {cost_cents}).")
    if cost_cents > 10_000_000:
        raise ValueError(
            f"Max cost per run exceeds maximum ceiling of 10,000,000 cents (got {cost_cents})."
        )
    return cost_cents


def create_default_repo_settings(
    repo_id: uuid.UUID,
    preset: str = DEFAULT_PRESET,
) -> RepoSettings:
    """Instantiate in-memory default RepoSettings for a repo without saving to DB."""
    canonical = normalize_preset_name(preset)
    cfg = PRESET_CONFIGURATIONS[canonical]
    now = datetime.now(timezone.utc)
    return RepoSettings(
        id=uuid.uuid4(),
        repo_id=repo_id,
        preset=canonical,
        enable_auto_fix=cfg.get("enable_auto_fix", True),
        enable_auditor_mode=cfg.get("enable_auditor_mode", False),
        enable_sandbox_verification=cfg.get("enable_sandbox_verification", True),
        enable_pr_comments=cfg.get("enable_pr_comments", True),
        enable_live_sessions=cfg.get("enable_live_sessions", True),
        enable_webcontainer_preview=cfg.get("enable_webcontainer_preview", True),
        audit_trigger_on_pr=cfg.get("audit_trigger_on_pr", True),
        audit_trigger_on_ci_failure=cfg.get("audit_trigger_on_ci_failure", True),
        audit_trigger_on_ci_success=cfg.get("audit_trigger_on_ci_success", False),
        audit_trigger_on_manual_mention=cfg.get(
            "audit_trigger_on_manual_mention", True
        ),
        allowed_branches=["main", "master"],
        ignore_draft_prs=cfg.get("ignore_draft_prs", True),
        max_cost_per_run_cents=cfg.get("max_cost_per_run_cents", 100),
        model_override_scope=cfg.get("model_override_scope", "inherit"),
        settings_version=1,
        created_at=now,
        updated_at=now,
    )


async def get_repo_settings(
    db: AsyncSession,
    repo_id: uuid.UUID,
    *,
    auto_create: bool = False,
) -> RepoSettings:
    """
    Fetch repository settings for repo_id.

    If not found in DB:
    - If auto_create=True, instantiates default settings, persists to DB, and returns it.
    - If auto_create=False, returns an in-memory default fallback instance.
    """
    row = await db.scalar(select(RepoSettings).where(RepoSettings.repo_id == repo_id))
    if row is not None:
        return row

    default_settings = create_default_repo_settings(repo_id)
    if auto_create:
        db.add(default_settings)
        await db.commit()
        await db.refresh(default_settings)
        logger.info(
            "Created default repo settings: repo_id=%s preset=%s",
            repo_id,
            default_settings.preset,
        )
        return default_settings

    return default_settings


def apply_preset(
    settings: RepoSettings,
    preset_name: str,
) -> RepoSettings:
    """
    Apply governance preset configuration to a RepoSettings instance.

    Bumps settings_version so replayed deliveries are detected.
    """
    canonical = normalize_preset_name(preset_name)
    cfg = PRESET_CONFIGURATIONS[canonical]

    settings.preset = canonical
    if canonical != "custom":
        for field, value in cfg.items():
            setattr(settings, field, value)

    settings.settings_version += 1
    settings.updated_at = datetime.now(timezone.utc)
    logger.info(
        "Applied preset '%s' to settings for repo_id=%s version=%d",
        canonical,
        settings.repo_id,
        settings.settings_version,
    )
    return settings


async def update_repo_settings(
    db: AsyncSession,
    repo_id: uuid.UUID,
    updates: Mapping[str, Any],
) -> RepoSettings:
    """
    Update repository settings with validation.
    Persists changes to the database and returns the refreshed row.
    """
    settings = await db.scalar(
        select(RepoSettings).where(RepoSettings.repo_id == repo_id)
    )
    if settings is None:
        settings = create_default_repo_settings(repo_id)
        db.add(settings)

    # Track if trigger semantics or governance changed to bump settings_version
    trigger_fields = {
        "enable_auditor_mode",
        "audit_trigger_on_pr",
        "audit_trigger_on_ci_failure",
        "audit_trigger_on_ci_success",
        "audit_trigger_on_manual_mention",
        "preset",
    }
    bump_version = False
    preset_applied = False

    # If preset is updated and not "custom", apply preset configuration first
    preset_val = (
        updates.get("preset") if "preset" in updates else updates.get("preset_profile")
    )
    if preset_val is not None:
        canonical = normalize_preset_name(preset_val)
        if canonical != "custom":
            apply_preset(settings, canonical)
            preset_applied = True
        else:
            if settings.preset != "custom":
                settings.preset = "custom"
                bump_version = True

    for key, value in updates.items():
        if value is None or key in ("preset_profile", "preset"):
            continue

        # Handle aliases
        if key in ("enable_auto_fix", "enable_auto_fixer"):
            val = bool(value)
            if settings.enable_auto_fix != val:
                settings.enable_auto_fix = val
        elif key in ("enable_sandbox_verification", "enable_ci_sandbox"):
            val = bool(value)
            if settings.enable_sandbox_verification != val:
                settings.enable_sandbox_verification = val
        elif key in ("allowed_branches", "monitored_branches"):
            settings.allowed_branches = validate_branches(value)
        elif key == "max_cost_per_run_cents":
            settings.max_cost_per_run_cents = validate_cost_bounds(value)
        elif hasattr(settings, key):
            if key in trigger_fields and getattr(settings, key) != value:
                bump_version = True
            setattr(settings, key, value)
        else:
            logger.debug("Ignoring unrecognized settings field: %s", key)

    if bump_version and not preset_applied:
        settings.settings_version += 1

    settings.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(settings)
    return settings


# Re-export feature enforcement helpers (Phase 6.3)
from app.services.feature_enforcement import (  # noqa: E402
    EnforcementDecision,
    check_cost_ceiling,
    check_feature_enforcement,
    is_auto_fix_allowed,
    is_branch_allowed,
    is_draft_pr_allowed,
    is_pr_branch_allowed,
    is_pr_comments_allowed,
    is_sandbox_verification_allowed,
    match_branch,
)

__all__ = [
    "ALLOWED_PRESETS",
    "CANONICAL_PRESET_MAP",
    "DEFAULT_PRESET",
    "EnforcementDecision",
    "PRESET_CONFIGURATIONS",
    "apply_preset",
    "check_cost_ceiling",
    "check_feature_enforcement",
    "create_default_repo_settings",
    "get_repo_settings",
    "is_auto_fix_allowed",
    "is_branch_allowed",
    "is_draft_pr_allowed",
    "is_pr_branch_allowed",
    "is_pr_comments_allowed",
    "is_sandbox_verification_allowed",
    "match_branch",
    "normalize_preset_name",
    "update_repo_settings",
    "validate_branch_name",
    "validate_branches",
    "validate_cost_bounds",
]
