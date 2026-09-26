"""
Feature Enforcement & Governance Interceptor Service Layer (Phase 6.3).

Provides centralized, deterministic policy interceptors for:
1. Branch allowance (exact match, glob wildcard patterns e.g. 'release/*', empty list allows all)
2. Draft PR filtering (governed by repo_settings.ignore_draft_prs)
3. Autonomous fix enablement (enable_auto_fix guard on CI failure)
4. Sandbox CI verification bypass (enable_sandbox_verification)
5. PR/commit comment suppression (enable_pr_comments)
6. Cost ceiling / budget bounds enforcement (max_cost_per_run_cents)
7. Structured skip/allow decisions with explicit reasons for audit trails & logging
"""

from __future__ import annotations

import fnmatch
import logging
import uuid
from dataclasses import dataclass
from typing import Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RepoSettings
from app.services.repo_settings import get_repo_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EnforcementDecision:
    """Structured decision returned by feature enforcement interceptors."""

    allowed: bool
    reason: str
    feature: str

    def __bool__(self) -> bool:
        return self.allowed


def match_branch(branch: Optional[str], pattern: str) -> bool:
    """
    Match a branch name against a pattern (exact match or glob wildcard).
    Strips git 'refs/heads/' prefix for resilient matching.
    """
    if not branch or not pattern:
        return False
    clean_branch = branch.strip().removeprefix("refs/heads/")
    clean_pattern = pattern.strip().removeprefix("refs/heads/")
    if clean_branch == clean_pattern:
        return True
    return fnmatch.fnmatchcase(clean_branch, clean_pattern) or fnmatch.fnmatch(
        clean_branch, clean_pattern
    )


def is_branch_allowed(
    branch: Optional[str],
    allowed_branches: Optional[Sequence[str]],
) -> EnforcementDecision:
    """
    Check if a branch is allowed by allowed_branches.

    - If allowed_branches is empty or None: allows all branches.
    - If branch matches any pattern (exact or wildcard glob): allowed.
    - Otherwise: rejected with explicit reason.
    """
    if not allowed_branches:
        return EnforcementDecision(
            allowed=True,
            reason="all branches allowed (empty allowed_branches)",
            feature="branch",
        )

    if not branch or not branch.strip():
        return EnforcementDecision(
            allowed=False,
            reason="branch name is empty or missing",
            feature="branch",
        )

    clean_branch = branch.strip().removeprefix("refs/heads/")
    for pat in allowed_branches:
        if match_branch(clean_branch, pat):
            return EnforcementDecision(
                allowed=True,
                reason=f"branch '{clean_branch}' matches allowed pattern '{pat}'",
                feature="branch",
            )

    return EnforcementDecision(
        allowed=False,
        reason=f"branch '{clean_branch}' is not in allowed branches {list(allowed_branches)}",
        feature="branch",
    )


def is_pr_branch_allowed(
    target_branch: Optional[str],
    head_branch: Optional[str],
    allowed_branches: Optional[Sequence[str]],
) -> EnforcementDecision:
    """
    Check if either target branch (base ref) or head branch of a PR matches allowed_branches.
    - If allowed_branches is empty or None: allows all branches.
    - If target_branch matches: allowed.
    - If head_branch matches: allowed.
    - Otherwise: rejected.
    """
    if not allowed_branches:
        return EnforcementDecision(
            allowed=True,
            reason="all branches allowed (empty allowed_branches)",
            feature="branch",
        )

    clean_target = (
        target_branch.strip().removeprefix("refs/heads/")
        if target_branch and target_branch.strip()
        else None
    )
    clean_head = (
        head_branch.strip().removeprefix("refs/heads/")
        if head_branch and head_branch.strip()
        else None
    )

    if clean_target:
        for pat in allowed_branches:
            if match_branch(clean_target, pat):
                return EnforcementDecision(
                    allowed=True,
                    reason=f"target branch '{clean_target}' matches allowed pattern '{pat}'",
                    feature="branch",
                )

    if clean_head:
        for pat in allowed_branches:
            if match_branch(clean_head, pat):
                return EnforcementDecision(
                    allowed=True,
                    reason=f"head branch '{clean_head}' matches allowed pattern '{pat}'",
                    feature="branch",
                )

    return EnforcementDecision(
        allowed=False,
        reason=(
            f"neither target branch '{clean_target}' nor head branch '{clean_head}' "
            f"matches allowed branches {list(allowed_branches)}"
        ),
        feature="branch",
    )


def is_draft_pr_allowed(
    is_draft: bool,
    ignore_draft_prs: bool,
) -> EnforcementDecision:
    """Check if a PR is allowed based on its draft status and ignore_draft_prs setting."""
    if is_draft and ignore_draft_prs:
        return EnforcementDecision(
            allowed=False,
            reason="draft PR ignored by repository policy (ignore_draft_prs=True)",
            feature="draft_pr",
        )
    return EnforcementDecision(
        allowed=True,
        reason="PR is not draft or draft PRs are allowed",
        feature="draft_pr",
    )


def is_auto_fix_allowed(enable_auto_fix: bool) -> EnforcementDecision:
    """Check if autonomous fix is enabled."""
    if not enable_auto_fix:
        return EnforcementDecision(
            allowed=False,
            reason="auto_fix disabled by repository settings (enable_auto_fix=False)",
            feature="auto_fix",
        )
    return EnforcementDecision(
        allowed=True,
        reason="auto_fix is enabled",
        feature="auto_fix",
    )


def is_sandbox_verification_allowed(enable_sandbox_verification: bool) -> EnforcementDecision:
    """Check if CI sandbox verification is enabled."""
    if not enable_sandbox_verification:
        return EnforcementDecision(
            allowed=False,
            reason="sandbox verification disabled by repository settings (enable_sandbox_verification=False)",
            feature="sandbox_verification",
        )
    return EnforcementDecision(
        allowed=True,
        reason="sandbox verification is enabled",
        feature="sandbox_verification",
    )


def is_pr_comments_allowed(enable_pr_comments: bool) -> EnforcementDecision:
    """Check if PR comments are enabled."""
    if not enable_pr_comments:
        return EnforcementDecision(
            allowed=False,
            reason="PR comments disabled by repository settings (enable_pr_comments=False)",
            feature="pr_comments",
        )
    return EnforcementDecision(
        allowed=True,
        reason="PR comments are enabled",
        feature="pr_comments",
    )


def check_cost_ceiling(
    current_cost_cents: float,
    max_cost_per_run_cents: int,
) -> EnforcementDecision:
    """Check if the accumulated run cost exceeds the max cost ceiling in cents."""
    if max_cost_per_run_cents > 0 and current_cost_cents >= max_cost_per_run_cents:
        return EnforcementDecision(
            allowed=False,
            reason=f"Run cost {current_cost_cents:.2f}¢ exceeds maximum cost limit of {max_cost_per_run_cents}¢",
            feature="cost_budget",
        )
    return EnforcementDecision(
        allowed=True,
        reason=f"Run cost {current_cost_cents:.2f}¢ within budget limit of {max_cost_per_run_cents}¢",
        feature="cost_budget",
    )


async def check_feature_enforcement(
    db: AsyncSession,
    repo_id: uuid.UUID,
    *,
    event_type: Optional[str] = None,
    branch: Optional[str] = None,
    target_branch: Optional[str] = None,
    head_branch: Optional[str] = None,
    is_draft: bool = False,
    current_cost_cents: Optional[float] = None,
    settings: Optional[RepoSettings] = None,
) -> EnforcementDecision:
    """
    Composite evaluation checking all applicable repository governance rules.
    Loads repo settings if not provided, then executes checks in sequence.
    """
    if settings is None:
        settings = await get_repo_settings(db, repo_id)

    # 1. Draft PR check
    if event_type == "pull_request" or is_draft:
        draft_dec = is_draft_pr_allowed(is_draft, settings.ignore_draft_prs)
        if not draft_dec.allowed:
            return draft_dec

    # 2. Branch check
    if target_branch is not None or head_branch is not None:
        branch_dec = is_pr_branch_allowed(target_branch, head_branch, settings.allowed_branches)
        if not branch_dec.allowed:
            return branch_dec
    elif branch is not None:
        branch_dec = is_branch_allowed(branch, settings.allowed_branches)
        if not branch_dec.allowed:
            return branch_dec

    # 3. Auto-fix check (for workflow_run CI failures)
    if event_type == "workflow_run":
        fix_dec = is_auto_fix_allowed(settings.enable_auto_fix)
        if not fix_dec.allowed:
            return fix_dec

    # 4. Cost ceiling check
    if current_cost_cents is not None:
        cost_dec = check_cost_ceiling(current_cost_cents, settings.max_cost_per_run_cents)
        if not cost_dec.allowed:
            return cost_dec

    return EnforcementDecision(
        allowed=True,
        reason="all feature enforcement checks passed",
        feature="general",
    )
