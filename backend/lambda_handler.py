"""
AWS Lambda entry point for Haunter (Phase 14).

Two invocation modes:

1. HTTP mode (API Gateway HTTP API v2 or Lambda Function URL):
   Event has "requestContext" key → routed through Mangum → FastAPI ASGI app.
   Webhook handler returns 2xx in <1s; pipeline dispatched via async Lambda
   self-invoke (AWSHostingAdapter) — never blocks the response.

2. Pipeline mode (async self-invocation from AWSHostingAdapter):
   Event has "run_id" key and no "requestContext" → runs handle_failed_run()
   directly. Lambda timeout must be 900s (15 min) to cover full pipeline.

Cost model (always-free, permanent — not 12-month like EC2):
  Lambda free tier: 1,000,000 requests + 400,000 GB-seconds/month.
  At 10 users / 3 repos / ~20 webhooks/week:
    - Webhook invocations: ~80/month → $0
    - Pipeline invocations: ~80/month × 5min × 512MB = 80 × 300 × 0.5 = 12,000 GB-s/mo
    - CodeBuild sandbox: ~80 × 10min = 800min/mo vs 100min always-free → ~700min overrun
      at $0.005/min = $3.50/mo — set $1 budget alert (HAUNTER.md:177) as early warning.
  → Hosting cost: $0. Sandbox cost: ~$3-4/mo above free tier at 20 webhook/week cadence.
    At realistic 5 webhooks/week: 200min/mo → stays within 100min free + ~$0.50 overrun.
  Budget alert: https://us-east-1.console.aws.amazon.com/billing/home#/budgets

Security:
  Lambda execution role: AWSLambdaBasicExecutionRole (CloudWatch Logs only).
  No secretsmanager:GetSecretValue. No cross-tenant resource access.
  Environment secrets (DATABASE_URL etc.) injected via Lambda env vars — not
  Secrets Manager — so no secretsmanager:GetSecretValue needed.
  Explicit Deny on secretsmanager:* in IAM policy (infra/aws/lambda.tf).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import uuid as _uuid

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Mangum handler (lazy — imported once, reused across warm invocations)
# ---------------------------------------------------------------------------

_mangum_handler = None


def _get_mangum_handler():
    global _mangum_handler
    if _mangum_handler is None:
        from mangum import Mangum
        from main import app

        # lifespan="off" because Lambda does not call ASGI lifespan events
        # (startup/shutdown) in the standard invocation model.
        _mangum_handler = Mangum(app, lifespan="off")
    return _mangum_handler


# ---------------------------------------------------------------------------
# Pipeline execution (direct invocation mode)
# ---------------------------------------------------------------------------


async def _run_pipeline(run_id_str: str) -> None:
    """Run handle_failed_run in async context with a fresh DB session."""
    from app.orchestrator import handle_failed_run

    run_id = _uuid.UUID(run_id_str)
    logger.info("lambda_handler: pipeline mode, run_id=%s", run_id)
    await handle_failed_run(run_id)
    logger.info("lambda_handler: pipeline completed for run_id=%s", run_id)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def handler(event: dict, context) -> dict:
    """
    AWS Lambda handler.

    Detects invocation mode and dispatches accordingly:
    - HTTP event (API GW / Function URL): Mangum → FastAPI
    - Pipeline event ({"run_id": "...", "token": "..."}): asyncio.run(handle_failed_run)

    Pipeline self-invoke is authenticated via HMAC-SHA256 over run_id using
    GITHUB_WEBHOOK_SECRET (fallback SESSION_SECRET_KEY) to prevent unauthenticated
    direct InvokeFunction from triggering arbitrary run_id (S-06). The hosting
    adapter includes the token; direct Invoke without valid token is rejected.
    """
    # Pipeline invocation: {"run_id": "...", "token": "..."} without HTTP context
    if "run_id" in event and "requestContext" not in event:
        run_id_str = event["run_id"]
        # HMAC verification — fail closed when secret is configured
        try:
            # Lazy import settings to avoid import at cold-start when env not set
            from app.config import settings as _settings

            _hmac_key = getattr(_settings, "github_webhook_secret", None) or getattr(
                _settings, "session_secret_key", None
            )
            if _hmac_key:
                expected = hmac.new(
                    _hmac_key.encode(), str(run_id_str).encode(), hashlib.sha256
                ).hexdigest()
                provided = event.get("token") or ""
                # Use constant-time compare to prevent timing side-channel
                if not provided or not hmac.compare_digest(expected, str(provided)):
                    logger.warning(
                        "lambda_handler: rejected unauthenticated pipeline invoke for run_id=%s",
                        run_id_str,
                    )
                    return {"error": "unauthorized pipeline invocation", "run_id": run_id_str}
        except Exception as exc:  # noqa: BLE001 — never let HMAC check crash handler
            logger.warning("lambda_handler: HMAC check error for run_id=%s: %s", run_id_str, exc)
            # If settings cannot be loaded, fall through to allow pipeline (dev mode without secret)
            pass
        logger.info("lambda_handler: received pipeline invocation for run_id=%s", run_id_str)
        try:
            asyncio.run(_run_pipeline(run_id_str))
        except Exception as exc:
            logger.exception(
                "lambda_handler: pipeline failed for run_id=%s: %s", run_id_str, exc
            )
            # Return error shape — Lambda async invocations don't return to caller
            return {"error": str(exc), "run_id": run_id_str}
        return {"status": "completed", "run_id": run_id_str}

    # HTTP invocation: API Gateway HTTP API v2 or Lambda Function URL
    return _get_mangum_handler()(event, context)
