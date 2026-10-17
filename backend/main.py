import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy import select

from app.auth import router as auth_router
from app.config import settings
from app.db import async_session_maker
from app.models import Run
from app.limiter import limiter
from app.repos import router as repos_router
from app.routers.eval import router as eval_router
from app.routers.github import router as github_router
from app.routers.hosting_config import router as hosting_config_router
from app.routers.model_config import router as model_config_router
from app.routers.reviews import router as reviews_router
from app.routers.sessions import router as sessions_router
from app.routers.settings import router as settings_router
from app.routers.traces import router as traces_router
from app.services.audit_pipeline import (
    start_audit_dispatch_workers,
    stop_audit_dispatch_workers,
)
from app.webhooks import router as webhooks_router

# Configure root logger to INFO so all logger.info() calls in submodules
# (orchestrator, subagents, sandbox) are visible in local dev and CloudWatch.
logging.basicConfig(level=logging.INFO, force=True)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Issue #158 — stranded-run signal on /health.
# Runs sitting in a non-terminal pipeline state are "active". Past
# _HEALTH_STUCK_AFTER_SECONDS they are reported as "stuck" so dashboards and
# alerts can detect a wedged pipeline without manual DB queries.
# Mirrors app.orchestrator.RunStatus minus its terminal states (pr_opened,
# fallback_commented, flaky_detected, completed, error).
# ---------------------------------------------------------------------------
_ACTIVE_RUN_STATUSES: frozenset = frozenset(
    {
        "pending",
        "context_gathering",
        "flake_verification",
        "fix_generation",
        "verification",
        "pending_pr",
        "fallback",
    }
)

# An active run older than this is counted as stuck. Pipeline runs normally
# settle in minutes; 30 minutes is a conservative wedge signal.
_HEALTH_STUCK_AFTER_SECONDS = 1800

# Upper bound on the runs-metrics lookup so a slow database can never make
# the liveness probe hang or flap. On timeout the probe still returns 200
# with "runs": null (see health_check).
_HEALTH_DB_TIMEOUT_SECONDS = 3.0


def _summarize_active_runs(
    ages_seconds: list, stuck_after_seconds: int = _HEALTH_STUCK_AFTER_SECONDS
) -> dict:
    """Build the runs-metrics payload from active-run ages. Pure: no I/O."""
    stuck_count = sum(1 for age in ages_seconds if age >= stuck_after_seconds)
    return {
        "active_count": len(ages_seconds),
        "oldest_active_age_seconds": max(ages_seconds) if ages_seconds else None,
        "stuck_count": stuck_count,
        "stuck_threshold_seconds": stuck_after_seconds,
        "degraded": stuck_count > 0,
    }


# ---------------------------------------------------------------------------
# Rate limiter (slowapi = express-rate-limit equivalent for FastAPI)
# Per-IP limiting applied to auth endpoints in auth.py via @limiter.limit().
# The limiter instance lives in app.limiter to avoid circular imports.
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    worker_count = start_audit_dispatch_workers()
    logger.info("audit dispatch workers started count=%s", worker_count)
    try:
        yield
    finally:
        await stop_audit_dispatch_workers()


app = FastAPI(title="Haunter Backend", lifespan=lifespan)


# Attach limiter to app state — slowapi reads it from here
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# ---------------------------------------------------------------------------
# CORS — allow configured frontend origin (+ localhost/127.0.0.1 variants)
# allow_credentials=True requires explicit origins (RFC 6454), wildcard "*" origin disallowed.
# Wildcard headers ["*"] allows modern browser fetch preflight headers (Cache-Control, Authorization, etc.)
# ---------------------------------------------------------------------------
cors_origins = [settings.frontend_url.rstrip("/")]
if "://localhost" in settings.frontend_url:
    cors_origins.append(
        settings.frontend_url.replace("://localhost", "://127.0.0.1").rstrip("/")
    )
elif "://127.0.0.1" in settings.frontend_url:
    cors_origins.append(
        settings.frontend_url.replace("://127.0.0.1", "://localhost").rstrip("/")
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH", "HEAD"],
    allow_headers=["*"],
)

app.include_router(auth_router, prefix="/auth")
app.include_router(repos_router)
app.include_router(github_router)
app.include_router(model_config_router)
app.include_router(hosting_config_router)
app.include_router(traces_router)
app.include_router(reviews_router)
app.include_router(sessions_router)
app.include_router(settings_router)
app.include_router(webhooks_router)
app.include_router(eval_router)


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/sandbox")
async def sandbox_health() -> dict:
    """
    Return the active sandbox provider configuration.

    Read-only, no auth required. Used by the dashboard's model/provider
    switcher to surface the current sandbox backend and — for the
    github_actions provider — the configured org and App ID.

    Response shape:
        {
          "provider": "github_actions" | "aws" | "gcp",
          "ok": true,
          "detail": {
            "org": "kaiizer777",          # github_actions only
            "app_id": "4772354",          # github_actions only
            "installation_id": "157771121"  # github_actions only
          }
        }
    """
    provider: str = (
        getattr(settings, "sandbox_provider", "github_actions").lower().strip()
    )
    detail: dict = {}

    if provider == "github_actions":
        detail["org"] = getattr(settings, "github_sandbox_org", None)
        detail["app_id"] = getattr(settings, "github_sandbox_app_id", None)
        detail["installation_id"] = getattr(
            settings, "github_sandbox_installation_id", None
        )

    return {"provider": provider, "ok": True, "detail": detail}
