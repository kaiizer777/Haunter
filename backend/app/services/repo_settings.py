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
import time
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
        "file_issue_on_fallback": True,
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
        "file_issue_on_fallback": True,
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
        "file_issue_on_fallback": True,
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
        "file_issue_on_fallback": True,
        "enable_webcontainer_preview": False,
        "audit_trigger_on_pr": True,
        "audit_trigger_on_ci_failure": True,
        "audit_trigger_on_ci_success": False,
        "audit_trigger_on_manual_mention": True,
        "ignore_draft_prs": True,
        "max_cost_per_run_cents": 50,
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


# Mirrors RepoSettings.working_dir (String(255)).
_MAX_WORKING_DIR_CHARS: int = 255

# Bounds the test command that gets embedded into a sandbox workflow template.
# 1000 characters is far above any real `pytest`/`npm test`/`go test` invocation
# and keeps the rendered workflow small.
_MAX_TEST_COMMAND_CHARS: int = 1000

# Conservative allowlist for a repo-relative path: no shell metacharacters, no
# spaces, no globs. Anything outside this is rejected rather than escaped,
# because the value is interpolated into a shell `cd` in the sandbox workflow.
_WORKING_DIR_RE: re.Pattern[str] = re.compile(r"[A-Za-z0-9_./\-]+")


def validate_working_dir(value: str | None) -> str | None:
    """
    Validate the monorepo ``working_dir`` override.

    Returns ``None`` for ``None``/empty (no override), ``"."`` for a
    repo-root request, or a normalized repo-relative POSIX path. Raises
    ``ValueError`` on absolute paths, ``..`` traversal, unsupported
    characters, or over-length input.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("working_dir must be a string.")
    clean = value.strip().replace("\\", "/")
    if not clean or clean in (".", "./"):
        return "."
    if clean.startswith("/"):
        raise ValueError(
            "working_dir must be relative to the repository root, not absolute."
        )
    clean = clean.rstrip("/")
    if len(clean) > _MAX_WORKING_DIR_CHARS:
        raise ValueError(
            f"working_dir exceeds maximum length of {_MAX_WORKING_DIR_CHARS} characters."
        )
    segments = clean.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise ValueError(
            "working_dir must be a relative path without empty, '.' or '..' segments."
        )
    if not _WORKING_DIR_RE.fullmatch(clean):
        raise ValueError(
            "working_dir may only contain letters, digits, '_', '-', '.' and '/'."
        )
    return clean


def validate_test_command(value: str | None) -> str | None:
    """
    Validate the monorepo ``test_command`` override.

    Returns ``None`` for ``None``/blank (clears the override) or the stripped
    command. Raises ``ValueError`` on over-length input or NUL bytes, which
    would otherwise truncate or corrupt the rendered workflow document.

    The command is intentionally *not* sanitised against shell syntax: it is
    run by the sandbox as the repository owner configured it, and escaping it
    would break legitimate invocations (``npm test -- --run``, ``go test -race``).
    The blast radius is the repository's own private CI mirror.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("test_command must be a string.")
    clean = value.strip()
    if not clean:
        return None
    if len(clean) > _MAX_TEST_COMMAND_CHARS:
        raise ValueError(
            f"test_command exceeds maximum length of {_MAX_TEST_COMMAND_CHARS} characters."
        )
    if "\x00" in clean:
        raise ValueError("test_command must not contain NUL bytes.")
    return clean


# Issue #131: bounds for team review preferences (R10).
# review_guidelines_text rides the 512-entry settings cache as TEXT, so it
# is capped at 4000 chars; focus/checklist lists are capped at 20 items of
# 200 chars each. Enforced on every write path via the normalizers below.
REVIEW_STRICTNESS_LEVELS: tuple[str, ...] = ("low", "medium", "high")
_MAX_REVIEW_GUIDELINES_CHARS: int = 4000
_MAX_REVIEW_LIST_ITEMS: int = 20
_MAX_REVIEW_LIST_ITEM_CHARS: int = 200


def normalize_review_strictness(value: object) -> str:
    """Normalize a review strictness level to its canonical form."""
    if not isinstance(value, str):
        raise ValueError("review_strictness must be one of low, medium, high.")
    clean = value.strip().lower()
    if clean not in REVIEW_STRICTNESS_LEVELS:
        raise ValueError("review_strictness must be one of low, medium, high.")
    return clean


def normalize_review_guidelines(value: object) -> str | None:
    """Normalize guidelines text; blank clears the override (None)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("review_guidelines_text must be a string.")
    clean = value.strip()
    if not clean:
        return None
    if len(clean) > _MAX_REVIEW_GUIDELINES_CHARS:
        raise ValueError(
            f"review_guidelines_text exceeds maximum length of {_MAX_REVIEW_GUIDELINES_CHARS} characters."
        )
    return clean


def normalize_review_string_list(value: object, field_name: str) -> list[str]:
    """Normalize a focus-area/checklist list: strip, drop blanks, cap."""
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field_name} must be a list of strings.")
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{field_name} must be a list of strings.")
        stripped = item.strip()
        if stripped:
            cleaned.append(stripped)
    if len(cleaned) > _MAX_REVIEW_LIST_ITEMS:
        raise ValueError(
            f"{field_name} accepts at most {_MAX_REVIEW_LIST_ITEMS} items."
        )
    for item in cleaned:
        if len(item) > _MAX_REVIEW_LIST_ITEM_CHARS:
            raise ValueError(
                f"{field_name} items accept at most {_MAX_REVIEW_LIST_ITEM_CHARS} characters each."
            )
    return cleaned


# ---------------------------------------------------------------------------
# Short-TTL read cache for near-static governance config (db-perf #109,
# hardened by #134).
#
# The cache stores *detached snapshot copies* (transient ``RepoSettings``
# instances with no session attached), never the live session-bound row:
# caching the live row meant a later ``db.rollback()`` (e.g. duplicate
# webhook delivery) expired its attributes and the next cache hit raised
# ``MissingGreenlet``/``DetachedInstanceError`` (#134-1).
#
# Writes are guarded by an invalidation generation counter: the read path
# captures the generation before the DB await and only stores the result
# when it is unchanged, so an in-flight read can never restore stale data
# after a concurrent write invalidated the cache (#134-2).
#
# The cache is bounded: expired entries are pruned on every store and the
# size is capped, so distinct-repo reads cannot leak memory over a worker
# lifetime (#134-4).
# ---------------------------------------------------------------------------

_REPO_SETTINGS_TTL: float = 60.0
_REPO_SETTINGS_MAXSIZE: int = 512
_REPO_SETTINGS_CACHE: dict[uuid.UUID, tuple[RepoSettings, float]] = {}
_REPO_SETTINGS_GEN: int = 0


def invalidate_repo_settings_cache(repo_id: uuid.UUID | None = None) -> None:
    """Drop cached repo settings — all rows, or one repo.

    Call after every write path (``update_repo_settings``, preset POST
    handlers, ``auto_create`` row creation is handled inline) so the next
    read re-queries the DB instead of serving a stale row for up to TTL.
    Bumps the invalidation generation so in-flight reads that started
    before this call do not repopulate the cache with stale data.
    """
    global _REPO_SETTINGS_GEN
    _REPO_SETTINGS_GEN += 1
    if repo_id is None:
        _REPO_SETTINGS_CACHE.clear()
    else:
        _REPO_SETTINGS_CACHE.pop(repo_id, None)


def _prune_repo_settings_cache(now: float, stored_key: uuid.UUID) -> None:
    """Evict expired entries, then oldest-inserted ones past the cap.

    The size cap is only enforced when storing a new key: overwriting
    an existing entry must not evict an unrelated repo's live entry.
    """
    expired = [
        key
        for key, (_, cached_at) in _REPO_SETTINGS_CACHE.items()
        if now - cached_at >= _REPO_SETTINGS_TTL
    ]
    for key in expired:
        del _REPO_SETTINGS_CACHE[key]
    if stored_key not in _REPO_SETTINGS_CACHE:
        while len(_REPO_SETTINGS_CACHE) >= _REPO_SETTINGS_MAXSIZE:
            _REPO_SETTINGS_CACHE.pop(next(iter(_REPO_SETTINGS_CACHE)))


def _detached_settings_copy(row: RepoSettings) -> RepoSettings:
    """Build a session-independent snapshot copy of a settings row.

    Copies every mapped column into a transient instance (no session, so
    no rollback/close can ever expire it). Mutable column values are
    copied per-object so the cache entry cannot alias caller state. Each
    cache hit hands out a fresh copy, so a caller mutating its result
    can never poison the cached entry.
    """
    values = {
        col.name: getattr(row, col.name) for col in RepoSettings.__table__.columns
    }
    branches = values.get("allowed_branches")
    if isinstance(branches, list):
        values["allowed_branches"] = list(branches)
    for _list_key in ("review_focus_areas", "review_checklist"):
        _items = values.get(_list_key)
        if isinstance(_items, list):
            values[_list_key] = list(_items)
    return RepoSettings(**values)


def _store_repo_settings_snapshot(
    repo_id: uuid.UUID,
    row: RepoSettings,
    generation: int,
    now: float,
) -> None:
    """Cache a detached snapshot unless an invalidation landed mid-read."""
    if generation != _REPO_SETTINGS_GEN:
        return
    _prune_repo_settings_cache(now, repo_id)
    _REPO_SETTINGS_CACHE[repo_id] = (_detached_settings_copy(row), now)


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
        file_issue_on_fallback=cfg.get("file_issue_on_fallback", True),
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
        working_dir=None,
        test_command=None,
        review_guidelines_text=None,
        review_strictness="medium",
        review_focus_areas=[],
        review_checklist=[],
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

    Hot-read path (``auto_create=False``) is served from a 60s in-process
    TTL cache: near-static governance config must not cost a SELECT per
    request. Only persisted DB rows are cached — the in-memory default
    fallback is cheap to build and never cached, so a later row creation
    cannot be hidden behind a stale miss.

    Write paths (``auto_create=True`` creation, :func:`update_repo_settings`,
    preset POST handlers) always hit the DB for an attached instance and
    invalidate this cache, so a cached snapshot is never used as a write
    target for a different session.
    """
    if not auto_create:
        cached = _REPO_SETTINGS_CACHE.get(repo_id)
        if cached is not None:
            row_cached, cached_at = cached
            if time.monotonic() - cached_at < _REPO_SETTINGS_TTL:
                return _detached_settings_copy(row_cached)

    generation = _REPO_SETTINGS_GEN
    row = await db.scalar(select(RepoSettings).where(RepoSettings.repo_id == repo_id))
    if row is not None:
        _store_repo_settings_snapshot(repo_id, row, generation, time.monotonic())
        return row

    default_settings = create_default_repo_settings(repo_id)
    if auto_create:
        db.add(default_settings)
        await db.commit()
        # This commit is the newest write: drop anything a concurrent
        # writer cached, then publish our fresh row — capturing the
        # generation after commit so a writer that lands during the
        # refresh() await below wins over our store.
        invalidate_repo_settings_cache(repo_id)
        generation = _REPO_SETTINGS_GEN
        await db.refresh(default_settings)
        logger.info(
            "Created default repo settings: repo_id=%s preset=%s",
            repo_id,
            default_settings.preset,
        )
        _store_repo_settings_snapshot(
            repo_id, default_settings, generation, time.monotonic()
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
        "review_guidelines_text",
        "review_strictness",
        "review_focus_areas",
        "review_checklist",
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
        if key == "review_guidelines_text":
            normalized_guidelines = normalize_review_guidelines(value)
            if settings.review_guidelines_text != normalized_guidelines:
                settings.review_guidelines_text = normalized_guidelines
                bump_version = True
            continue
        if key == "review_strictness":
            normalized_strictness = (
                "medium" if value is None else normalize_review_strictness(value)
            )
            if settings.review_strictness != normalized_strictness:
                settings.review_strictness = normalized_strictness
                bump_version = True
            continue
        if key in ("review_focus_areas", "review_checklist"):
            normalized_items = (
                [] if value is None else normalize_review_string_list(value, key)
            )
            if list(getattr(settings, key) or []) != normalized_items:
                setattr(settings, key, normalized_items)
                bump_version = True
            continue
        if key in ("working_dir", "test_command"):
            # Handled before the None-skip below: these two are optional
            # overrides, so an explicit null is how a caller *clears* one.
            if key == "working_dir":
                settings.working_dir = validate_working_dir(value)
            else:
                settings.test_command = validate_test_command(value)
            continue

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
    # Invalidate before the refresh() await: the commit is already durable,
    # and a concurrent read during refresh must not see the stale cache.
    invalidate_repo_settings_cache(repo_id)
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
    "invalidate_repo_settings_cache",
    "is_auto_fix_allowed",
    "is_branch_allowed",
    "is_draft_pr_allowed",
    "is_pr_branch_allowed",
    "is_pr_comments_allowed",
    "is_sandbox_verification_allowed",
    "match_branch",
    "normalize_preset_name",
    "normalize_review_guidelines",
    "normalize_review_strictness",
    "normalize_review_string_list",
    "update_repo_settings",
    "validate_branch_name",
    "validate_branches",
    "validate_cost_bounds",
    "validate_test_command",
    "validate_working_dir",
]
