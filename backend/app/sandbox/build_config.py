"""
Cloud Build configuration generator — Phase 7.

Produces a Cloud Build `Build` dict that:
  1. Clones the target repo (shallow) using GITHUB_TOKEN via Secret Manager.
  2. Detects language/runtime (Dockerfile → docker build, else Python/Node).
  3. Applies the patch (echo + git apply, never stored in substitution).
  4. Runs the test suite (pytest / npm test).

Security invariants:
  - repo owner/name validated against a strict allowlist regex before
    being interpolated into any build step command.
  - patch_text is never passed as a Cloud Build substitution (no `${}` injection).
    Instead it is written via a shell heredoc in the apply step.
  - GITHUB_TOKEN is fetched via Secret Manager binding — never logged or
    passed as a substitution value.
  - No Docker-in-Docker, no privileged mode, no shared persistent volumes.
  - Build timeout capped at 600s; queueTtl at 120s to bound cost.

Image supply-chain pinning (NICE-3 / Phase 4):
  - All ``gcr.io/cloud-builders/git`` references are pinned to a SHA digest
    (@sha256:...) so a future upstream re-tag of ``latest`` cannot silently
    change what code runs in the sandbox. Digest verified against the
    registry's ``Docker-Content-Digest`` header on the ``latest`` manifest
    at the time of the Phase 4 change; bump explicitly when the base image
    needs to advance.
"""

from __future__ import annotations

import re
import uuid

from app.models import Repo

# Allowlist: only alphanumeric, dash, dot, underscore — no shell metacharacters.
_REPO_IDENT_RE: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9_.\-]+$")

# Secret Manager resource name template — substituted at build runtime.
# The Cloud Build SA must have `secretmanager.secretAccessor` on this secret only.
_GITHUB_TOKEN_SECRET = (
    "projects/{project_id}/secrets/GITHUB_TOKEN/versions/latest"
)

# Pinned digest of gcr.io/cloud-builders/git (verified against gcr.io's
# Docker-Content-Digest header on the ``latest`` manifest at the time of
# the Phase 4 change). Pinning to a digest (not a tag) prevents silent
# upstream re-tags from changing what runs in the sandbox. See module
# docstring, "Image supply-chain pinning".
_GIT_IMAGE: str = (
    "gcr.io/cloud-builders/git@sha256:"
    "bfcbd8719280b196bd860e89531c3c9b598daab4a07aef1d17a163c822d569bd"
)


def _validate_repo_ident(value: str, field: str) -> None:
    """Raise ValueError if *value* contains characters outside the allowlist."""
    if not _REPO_IDENT_RE.fullmatch(value):
        raise ValueError(
            f"repo.{field}={value!r} contains characters not allowed in a "
            f"Cloud Build step command. Only [a-zA-Z0-9_.-] are permitted."
        )


def build_cloud_build_config(
    repo: Repo,
    patch_text: str,
    run_id: uuid.UUID,
    project_id: str,
) -> dict:
    """
    Return a Cloud Build `Build` dict for sandbox patch verification.

    Steps:
      clone   — shallow clone target repo using GITHUB_TOKEN from Secret Manager
      apply   — apply patch_text via `git apply` (heredoc, never a substitution)
      install — install project dependencies (pip or npm)
      test    — run the test suite (pytest or npm test)

    Args:
        repo:       Repo ORM object (owner + name validated against allowlist).
        patch_text: Unified diff to apply. Never logged, never a substitution.
        run_id:     Haunter run UUID — attached as build tag for correlation.
        project_id: GCP project ID for Secret Manager binding.

    Returns:
        dict compatible with google.cloud.devtools.cloudbuild_v1.Build.

    Raises:
        ValueError: if repo.owner or repo.name contain disallowed characters.
    """
    _validate_repo_ident(repo.owner, "owner")
    _validate_repo_ident(repo.name, "name")

    # Escape any single-quotes in patch_text so the heredoc shell command
    # cannot be broken out of. Cloud Build steps run as /bin/sh -c.
    # We use a null-delimited printf + base64 to completely avoid injection.
    # patch_text is base64-encoded in Python then decoded in the shell step,
    # which eliminates ALL shell-metacharacter risk.
    import base64

    patch_b64: str = base64.b64encode(patch_text.encode()).decode()

    # Secret Manager binding — SA needs secretAccessor on this secret only.
    secret_resource = _GITHUB_TOKEN_SECRET.format(project_id=project_id)

    # Pinned digest (see _GIT_IMAGE at module level for the source).

    steps: list[dict] = [
        # ----------------------------------------------------------------
        # Step 1: clone — authenticated shallow clone via Secret Manager token
        # ----------------------------------------------------------------
        {
            "id": "clone",
            "name": _GIT_IMAGE,
            "entrypoint": "sh",
            "args": [
                "-c",
                (
                    "git clone --depth 1 "
                    f"https://x-access-token:$$GITHUB_TOKEN@github.com/{repo.owner}/{repo.name}.git ."
                ),
            ],
            "secret_env": ["GITHUB_TOKEN"],
        },
        # ----------------------------------------------------------------
        # Step 2: apply patch — base64-decoded heredoc, zero substitution risk
        # ----------------------------------------------------------------
        {
            "id": "apply",
            "name": _GIT_IMAGE,
            "entrypoint": "sh",
            "args": [
                "-c",
                (
                    f"echo '{patch_b64}' | base64 -d > /tmp/haunter.patch && "
                    "git apply --check /tmp/haunter.patch && "
                    "git apply /tmp/haunter.patch"
                ),
            ],
        },
        # ----------------------------------------------------------------
        # Step 3: install dependencies — detect Dockerfile / Python / Node
        # ----------------------------------------------------------------
        {
            "id": "install",
            "name": "gcr.io/cloud-builders/docker",
            "entrypoint": "sh",
            "args": [
                "-c",
                (
                    "if [ -f Dockerfile ]; then "
                    "  docker build -t haunter-sandbox-image . ; "
                    "elif [ -f requirements.txt ]; then "
                    "  pip install --quiet -r requirements.txt ; "
                    "elif [ -f package.json ]; then "
                    "  npm ci --prefer-offline ; "
                    "else "
                    "  echo 'No recognised dependency manifest found — skipping install.' ; "
                    "fi"
                ),
            ],
        },
        # ----------------------------------------------------------------
        # Step 4: run tests — pytest or npm test
        # ----------------------------------------------------------------
        {
            "id": "test",
            "name": "python:3.12-slim",
            "entrypoint": "sh",
            "args": [
                "-c",
                (
                    "if [ -f Dockerfile ]; then "
                    "  docker run --rm haunter-sandbox-image pytest -q || "
                    "  docker run --rm haunter-sandbox-image python -m pytest -q ; "
                    "elif [ -f pytest.ini ] || [ -f pyproject.toml ] || [ -f setup.cfg ]; then "
                    "  pip install --quiet pytest && pytest -q ; "
                    "elif [ -f requirements.txt ]; then "
                    "  pip install --quiet pytest && python -m pytest -q ; "
                    "elif [ -f package.json ]; then "
                    "  npm test ; "
                    "else "
                    "  python -m pytest -q ; "
                    "fi"
                ),
            ],
        },
    ]

    build: dict = {
        "steps": steps,
        "timeout": "600s",
        "queue_ttl": "120s",
        "tags": [f"haunter-run-{run_id}", "haunter-sandbox"],
        "options": {
            "machine_type": "E2_HIGHCPU_8",
            "logging": "GCS_ONLY",
        },
        # Service account with minimal roles (storage.objectViewer + secretAccessor
        # on GITHUB_TOKEN only). No owner, no cloudsql, no secretmanager wildcard.
        # Configured in GCP IAM out-of-band; referenced here for documentation.
        # "service_account": f"projects/{project_id}/serviceAccounts/haunter-sandbox@{project_id}.iam.gserviceaccount.com",
        "available_secrets": {
            "secret_manager": [
                {
                    "version_name": secret_resource,
                    "env": "GITHUB_TOKEN",
                }
            ]
        },
    }

    return build
