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


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}
