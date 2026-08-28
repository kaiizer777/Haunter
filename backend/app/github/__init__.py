"""GitHub App integration package — Phase 8.

Exposes:
  get_installation_token  — exchange JWT for scoped installation token (50-min cache)
  create_branch           — POST /git/refs
  commit_patch            — Git Data API: blob → tree → commit → update ref
  open_pr                 — POST /pulls
  post_commit_comment     — re-exported from github_client for convenience

All functions validate owner/repo/branch against _REPO_IDENT_RE / _BRANCH_RE before
making any HTTP call. force=false is always enforced. No token is ever logged or stored.
"""
from app.github.pr import (
    GitHubPRError,
    commit_patch,
    create_branch,
    get_installation_token,
    open_pr,
)

__all__ = [
    "GitHubPRError",
    "commit_patch",
    "create_branch",
    "get_installation_token",
    "open_pr",
]
