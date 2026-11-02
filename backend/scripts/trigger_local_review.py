"""
Trigger the real PR review pipeline against a running dev server.

Builds a realistic GitHub ``pull_request`` payload, signs the exact raw bytes
with HMAC-SHA256, and POSTs them to the real ``POST /webhooks/github`` ingress
(``app/webhooks.py``). Nothing about the review pipeline is stubbed or
bypassed: the request travels the same production path GitHub's webhooks do.

The dev server schedules the review as a FastAPI ``BackgroundTask`` after the
HTTP response is sent (no Lambda function name configured locally), so a
``queued`` response means "accepted", not "reviewed". Watch the uvicorn log
for the pipeline run.

Usage:
    cd backend
    python scripts/trigger_local_review.py --repo saif-org/haunter-demo --pr 7
    python scripts/trigger_local_review.py --repo saif-org/haunter-demo --pr 7 --seed-repo
    python scripts/trigger_local_review.py --repo saif-org/haunter-demo --pr 7 --dry-run --secret devsecret

Run from ``backend/``: ``Settings`` resolves ``env_file=".env"`` against the
current working directory.

Exit codes:
    0  -- queued (review_id, or an auditor-owned delivery with audit_id)
    1  -- request failed, non-200, or unexpected response body
    2  -- invalid CLI arguments
    3  -- duplicate delivery (same repo/commit/PR already handled)
    4  -- ignored or skipped by an ingress guard (reason printed verbatim)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import shlex
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import httpx

# ---------------------------------------------------------------------------
# Bootstrap path so imports resolve correctly when run from backend/scripts/
# ---------------------------------------------------------------------------

_BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND_DIR))

# ---------------------------------------------------------------------------
# Configure logging before any app imports
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("trigger_local_review")

# Suppress noisy library loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Mirrors ``app.github.pr.REVIEWABLE_PR_ACTIONS``. Kept local so importing this
#: module never pulls in app settings (which fire hard startup guards).
_REVIEWABLE_ACTIONS: tuple[str, ...] = (
    "opened",
    "synchronize",
    "ready_for_review",
    "reopened",
)

_AUDITOR_OWNED_REASON = "auditor pr audit owns this delivery"
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$", re.ASCII)
_DEFAULT_BASE_URL = "http://127.0.0.1:7555"
_DEFAULT_USERNAME = "saif-dev"
#: Distinct from ``scripts/create_dev_session.py`` (99887766) so seeding here
#: never collides with the dev-session fixture user.
_SEED_GITHUB_ID = 99887767


@dataclass(frozen=True)
class Outcome:
    """Exit code plus the human-readable lines describing the response."""

    exit_code: int
    lines: tuple[str, ...]


# ---------------------------------------------------------------------------
# Payload building and signing
# ---------------------------------------------------------------------------


def _build_payload(
    *,
    repo: str,
    pr_number: int,
    sha: str,
    base_ref: str,
    head_ref: str,
    action: str,
    sender: str,
    sender_type: str,
    base_sha: str | None = None,
) -> dict[str, Any]:
    """Build the minimal realistic ``pull_request`` payload the ingress reads."""
    owner, name = repo.split("/", 1)

    if base_sha is None:
        base_sha = hashlib.sha256(
            f"base:{repo}:{pr_number}:{base_ref}".encode("utf-8")
        ).hexdigest()[:40]
        if base_sha == sha:
            base_sha = hashlib.sha256(
                f"base-alt:{repo}:{pr_number}:{base_ref}".encode("utf-8")
            ).hexdigest()[:40]

    repo_block = {"name": name, "owner": {"login": owner}}

    return {
        "action": action,
        "number": pr_number,
        "pull_request": {
            "number": pr_number,
            "state": "open",
            "draft": False,
            "title": f"Local dev review trigger #{pr_number}",
            "user": {"login": sender, "type": sender_type},
            "head": {"ref": head_ref, "sha": sha, "repo": repo_block},
            "base": {"ref": base_ref, "sha": base_sha, "repo": repo_block},
        },
        "repository": {
            "name": name,
            "full_name": f"{owner}/{name}",
            "owner": {"login": owner},
            "default_branch": base_ref,
        },
        "sender": {"login": sender, "type": sender_type},
    }


def _sign_payload(body: bytes, secret: str) -> str:
    """Return the ``X-Hub-Signature-256`` value for the exact bytes being sent."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _resolve_secret(cli_secret: str | None) -> str | None:
    """
    Resolve the webhook secret: CLI override first, then app settings.

    ``app.config`` is imported only when ``--secret`` is absent so a dry run
    with an explicit secret needs no ``.env`` at all.
    """
    if cli_secret:
        return cli_secret

    from app.config import settings

    secret = settings.github_webhook_secret
    if not secret:
        logger.error(
            "GITHUB_WEBHOOK_SECRET is not set; pass --secret or configure backend/.env"
        )
        print(
            "ERROR: no webhook secret available.\n"
            "  Set GITHUB_WEBHOOK_SECRET in backend/.env (the dev server must use\n"
            "  the same value), or pass --secret <value>."
        )
        return None
    return secret


# ---------------------------------------------------------------------------
# Response classification
# ---------------------------------------------------------------------------


def _reason_hint(reason: str) -> str:
    """One-line actionable hint for an ingress guard reason."""
    if reason == "draft PR":
        return "Hint: the payload marked the PR as a draft; this script sends draft=false."
    if reason == "closed PR":
        return "Hint: the payload marked the PR closed; this script sends state=open."
    if reason == "bot PR":
        return "Hint: --sender / --sender-type must not be a bot (default: local-dev / User)."
    if reason == "haunter fix branch":
        return "Hint: --head-ref must not start with 'haunter/' (feedback-loop guard)."
    if reason == "missing head sha":
        return "Hint: pass --sha with a 40-character hex commit SHA."
    if reason == "invalid repository payload":
        return "Hint: --repo must be owner/name, e.g. saif-org/haunter-demo."
    if reason == "unregistered repository":
        return "Hint: no repos row matches this owner/name; rerun with --seed-repo."
    if reason == "ambiguous repository registration":
        return (
            "Hint: more than one repos row matches this owner/name; remove or "
            "scope the duplicate registration."
        )
    if reason.startswith("unsupported PR action"):
        return f"Hint: --action must be one of {', '.join(_REVIEWABLE_ACTIONS)}."
    if "branch" in reason:
        return (
            "Hint: the base branch must be in the repo's allowed_branches "
            "(default: main, master)."
        )
    return "Hint: check the uvicorn log for the guard that rejected this delivery."


def _body_text(raw_text: str) -> str:
    return raw_text.strip() or "<empty body>"


def _classify_response(
    status_code: int,
    body: Any,
    *,
    raw_text: str = "",
    retry_after: str | None = None,
) -> Outcome:
    """Map an ingress response to the script's exit code and summary lines."""
    if status_code == 200 and isinstance(body, Mapping):
        status = body.get("status")

        if status == "queued":
            if body.get("reason") == _AUDITOR_OWNED_REASON:
                audit_id = body.get("audit_id")
                if not audit_id:
                    return Outcome(
                        1,
                        (
                            "HTTP 200 queued (auditor) but no audit_id in the response.",
                            f"Body: {_body_text(raw_text)}",
                        ),
                    )
                return Outcome(
                    0,
                    (
                        f"Queued as an auditor PR audit: audit_id={audit_id}",
                        "No CodeReview row was created — the auditor owns this delivery.",
                        "The audit runs in the background after this response.",
                    ),
                )

            review_id = body.get("review_id")
            if not review_id:
                return Outcome(
                    1,
                    (
                        "HTTP 200 queued but no review_id in the response.",
                        f"Body: {_body_text(raw_text)}",
                    ),
                )
            return Outcome(
                0,
                (
                    f"Queued: review_id={review_id}",
                    "queued != reviewed: the pipeline runs as a BackgroundTask in the",
                    "uvicorn process after this response. Watch the server log.",
                ),
            )

        if status == "duplicate":
            review_id = body.get("review_id")
            return Outcome(
                3,
                (
                    f"Duplicate: this repo/commit/PR was already handled (review_id={review_id}).",
                    "Pass --sha with a fresh 40-character hex SHA to force a new review.",
                ),
            )

        if status in ("ignored", "skipped"):
            reason = str(body.get("reason") or "<no reason given>")
            return Outcome(
                4,
                (
                    f"{str(status).capitalize()} by ingress guard: {reason}",
                    _reason_hint(reason),
                ),
            )

        return Outcome(
            1,
            (
                "Unexpected 200 response body.",
                f"Body: {_body_text(raw_text)}",
            ),
        )

    if status_code == 401:
        return Outcome(
            1,
            (
                "HTTP 401 Invalid or missing signature.",
                "Secret mismatch or GITHUB_WEBHOOK_SECRET unset on the server.",
                "Use the same value for backend/.env and --secret (if you pass one).",
            ),
        )

    if status_code == 429:
        retry_hint = (
            f"Retry after {retry_after}s."
            if retry_after
            else "No Retry-After header was returned."
        )
        return Outcome(
            1,
            (
                "HTTP 429 webhook rate limit exceeded for this repo/sender.",
                retry_hint,
                "Wait for the 60s window to drain, then rerun.",
            ),
        )

    if status_code == 503:
        return Outcome(
            1,
            (
                "HTTP 503 the server failed to dispatch the review.",
                "The CodeReview row may have been committed but scheduling failed.",
                "Check the uvicorn log; GitHub would normally redeliver this.",
            ),
        )

    return Outcome(
        1,
        (
            f"HTTP {status_code} from the webhook ingress.",
            f"Body: {_body_text(raw_text)}",
        ),
    )


def _outcome_from_response(response: httpx.Response) -> Outcome:
    """Parse the response body and classify it. Non-JSON bodies are reported raw."""
    try:
        body: Any = response.json()
    except ValueError:
        body = None
    return _classify_response(
        response.status_code,
        body,
        raw_text=response.text,
        retry_after=response.headers.get("Retry-After"),
    )


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


async def _post_webhook(
    client: httpx.AsyncClient,
    *,
    url: str,
    body: bytes,
    headers: Mapping[str, str],
    timeout: float,
) -> httpx.Response:
    """POST the already-signed raw bytes. Never re-serialize after signing."""
    return await client.post(url, content=body, headers=dict(headers), timeout=timeout)


# ---------------------------------------------------------------------------
# Optional DB seeding
# ---------------------------------------------------------------------------


async def _seed_repo(repo: str, base_ref: str) -> bool:
    """Create the ``users`` + ``repos`` rows the ingress resolves, if missing."""
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from app.db import async_session_maker
    from app.models import Repo, User

    owner, name = repo.split("/", 1)

    try:
        async with async_session_maker() as db:
            user = (
                await db.execute(
                    select(User).where(User.github_username == _DEFAULT_USERNAME)
                )
            ).scalar_one_or_none()
            if user is None:
                user = User(
                    github_id=_SEED_GITHUB_ID,
                    github_username=_DEFAULT_USERNAME,
                )
                db.add(user)
                await db.flush()
                print(
                    f"Created user: {user.github_username} "
                    f"(github_id={user.github_id}, id={user.id})"
                )
            else:
                print(f"Using existing user: {user.github_username} (id={user.id})")

            repo_row = (
                await db.execute(
                    select(Repo).where(
                        Repo.user_id == user.id,
                        Repo.owner == owner,
                        Repo.name == name,
                    )
                )
            ).scalar_one_or_none()
            if repo_row is None:
                repo_row = Repo(
                    user_id=user.id,
                    owner=owner,
                    name=name,
                    default_branch=base_ref,
                )
                db.add(repo_row)
                print(f"Created repo: {owner}/{name} (default_branch={base_ref})")
            else:
                print(
                    f"Using existing repo: {owner}/{name} "
                    f"(default_branch={repo_row.default_branch})"
                )

            await db.commit()
    except IntegrityError as exc:
        logger.error("Seeding failed: %s", type(exc).__name__)
        print(
            "ERROR: seeding violated a uniqueness constraint. Another user may "
            f"already own github_id={_SEED_GITHUB_ID}."
        )
        return False

    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "POST a signed pull_request webhook to a running dev server's real "
            "POST /webhooks/github ingress."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="Repository in owner/name format, e.g. saif-org/haunter-demo",
    )
    parser.add_argument(
        "--pr",
        required=True,
        type=int,
        help="Pull request number (must be > 0)",
    )
    parser.add_argument(
        "--sha",
        default=None,
        help=(
            "Head commit SHA (40 hex chars). Defaults to a fresh random SHA so "
            "repeat runs dodge the dedup guard."
        ),
    )
    parser.add_argument(
        "--base-sha",
        default=None,
        help=(
            "Base commit SHA (40 hex chars). Defaults to a synthetic SHA derived "
            "from repo/PR/base-ref. Pass the real base SHA when triggering an "
            "auditor PR audit so intake pins match live GitHub."
        ),
    )
    parser.add_argument("--base-ref", default="main", help="Base branch (default: main)")
    parser.add_argument(
        "--head-ref",
        default="feature/local-dev",
        help="Head branch (default: feature/local-dev)",
    )
    parser.add_argument(
        "--action",
        default="opened",
        choices=list(_REVIEWABLE_ACTIONS),
        help="pull_request action (default: opened)",
    )
    parser.add_argument(
        "--base-url",
        default=_DEFAULT_BASE_URL,
        help=f"Dev server base URL (default: {_DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--secret",
        default=None,
        help="Webhook secret override (default: settings.github_webhook_secret)",
    )
    parser.add_argument(
        "--sender", default="local-dev", help="Sender login (default: local-dev)"
    )
    parser.add_argument(
        "--sender-type", default="User", help="Sender type (default: User)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and sign the payload, print it with a curl equivalent, send nothing.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds (default: 30.0)",
    )
    parser.add_argument(
        "--seed-repo",
        action="store_true",
        help="Create the missing users/repos rows before POSTing (needs DB access).",
    )

    args = parser.parse_args(argv)

    if args.repo.count("/") != 1:
        parser.error("--repo must be in owner/name format, e.g. 'saif-org/haunter-demo'")
    owner, name = args.repo.split("/", 1)
    if not owner or not name:
        parser.error("--repo must be in owner/name format, e.g. 'saif-org/haunter-demo'")

    if args.pr <= 0:
        parser.error("--pr must be a positive integer")

    if args.sha is not None:
        if not _SHA_RE.fullmatch(args.sha):
            parser.error("--sha must be exactly 40 hexadecimal characters")
        args.sha = args.sha.lower()

    if args.base_sha is not None:
        if not _SHA_RE.fullmatch(args.base_sha):
            parser.error("--base-sha must be exactly 40 hexadecimal characters")
        args.base_sha = args.base_sha.lower()

    if args.timeout <= 0:
        parser.error("--timeout must be greater than 0")

    if not args.base_url.startswith(("http://", "https://")):
        parser.error("--base-url must start with http:// or https://")
    args.base_url = args.base_url.rstrip("/")

    return args


def _build_headers(body: bytes, secret: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "User-Agent": "haunter-local-review-trigger",
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": str(uuid.uuid4()),
        "X-Hub-Signature-256": _sign_payload(body, secret),
    }


def _curl_equivalent(url: str, body: bytes, headers: Mapping[str, str]) -> str:
    lines = [f"curl -sS -X POST {shlex.quote(url)}"]
    for key, value in headers.items():
        lines.append(f"  -H {shlex.quote(f'{key}: {value}')}")
    lines.append(f"  --data-binary {shlex.quote(body.decode('utf-8'))}")
    return " \\\n".join(lines)


def _print_dry_run(
    *, url: str, payload: Mapping[str, Any], body: bytes, headers: Mapping[str, str]
) -> None:
    print()
    print("=" * 72)
    print("  DRY RUN -- nothing was sent")
    print("=" * 72)
    print(f"  POST {url}")
    print(f"  Delivery: {headers['X-GitHub-Delivery']}")
    print(f"  Signature: {headers['X-Hub-Signature-256']}")
    print()
    print(json.dumps(payload, indent=2, sort_keys=True))
    print()
    print("  curl equivalent (POSIX shell):")
    print(_curl_equivalent(url, body, headers))
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main() -> int:
    args = _parse_args()

    sha = args.sha or secrets.token_hex(20)
    payload = _build_payload(
        repo=args.repo,
        pr_number=args.pr,
        sha=sha,
        base_ref=args.base_ref,
        head_ref=args.head_ref,
        action=args.action,
        sender=args.sender,
        sender_type=args.sender_type,
        base_sha=args.base_sha,
    )
    body = json.dumps(payload).encode("utf-8")

    secret = _resolve_secret(args.secret)
    if secret is None:
        return 1

    headers = _build_headers(body, secret)
    url = f"{args.base_url}/webhooks/github"

    print()
    print("=" * 72)
    print("  Haunter local PR review trigger")
    print("=" * 72)
    print(f"  Repo:     {args.repo}")
    print(f"  PR:       #{args.pr}")
    print(f"  Action:   {args.action}")
    print(f"  Head SHA: {sha}")
    print(f"  Base SHA: {payload['pull_request']['base']['sha']}")
    print(f"  Base:     {args.base_ref} <- {args.head_ref}")
    print(f"  Ingress:  POST {url}")

    if args.seed_repo:
        print()
        if not await _seed_repo(args.repo, args.base_ref):
            return 1

    if args.dry_run:
        _print_dry_run(url=url, payload=payload, body=body, headers=headers)
        return 0

    print()
    async with httpx.AsyncClient() as client:
        try:
            response = await _post_webhook(
                client,
                url=url,
                body=body,
                headers=headers,
                timeout=args.timeout,
            )
        except httpx.RequestError as exc:
            logger.error("Webhook POST failed: %s", type(exc).__name__)
            print()
            print(f"ERROR: could not reach the dev server at {args.base_url}.")
            print("Is uvicorn running? Start it from backend/ with:")
            print("  python -m uvicorn main:app --reload --port 7555")
            return 1

    outcome = _outcome_from_response(response)
    print()
    for line in outcome.lines:
        print(f"  {line}")
    print()
    return outcome.exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
