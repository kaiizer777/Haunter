import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.auth import router as auth_router
from app.config import settings
from app.limiter import limiter
from app.repos import router as repos_router
from app.routers.eval import router as eval_router
from app.routers.github import router as github_router
from app.routers.hosting_config import router as hosting_config_router
from app.routers.model_config import router as model_config_router
from app.routers.traces import router as traces_router
from app.webhooks import router as webhooks_router

# Configure root logger to INFO so all logger.info() calls in submodules
# (orchestrator, subagents, sandbox) are visible in local dev and CloudWatch.
logging.basicConfig(level=logging.INFO, force=True)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate limiter (slowapi = express-rate-limit equivalent for FastAPI)
# Per-IP limiting applied to auth endpoints in auth.py via @limiter.limit().
# The limiter instance lives in app.limiter to avoid circular imports.
# ---------------------------------------------------------------------------
app = FastAPI(title="Haunter Backend")


# Attach limiter to app state — slowapi reads it from here
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# ---------------------------------------------------------------------------
# CORS — exact origin match only, never wildcard with credentials
# allow_origins=[FRONTEND_URL] + allow_credentials=True is correct.
# Wildcard "*" with credentials is both insecure and browser-rejected (RFC).
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Cookie"],
)

app.include_router(auth_router, prefix="/auth")
app.include_router(repos_router)
app.include_router(github_router)
app.include_router(model_config_router)
app.include_router(hosting_config_router)
app.include_router(traces_router)
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
    provider: str = getattr(settings, "sandbox_provider", "github_actions").lower().strip()
    detail: dict = {}

    if provider == "github_actions":
        detail["org"] = getattr(settings, "github_sandbox_org", None)
        detail["app_id"] = getattr(settings, "github_sandbox_app_id", None)
        detail["installation_id"] = getattr(
            settings, "github_sandbox_installation_id", None
        )

    return {"provider": provider, "ok": True, "detail": detail}
