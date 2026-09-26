"""IAM-only AWS Lambda entry point for the durable audit outbox poller.

This function is deployed separately from the public API function
(infra/aws/lambda.tf: `aws_lambda_function.audit_dispatcher`):

  - it has **no** Lambda Function URL, so it is not reachable from the internet
  - its only resource policy grant is `lambda:InvokeFunction` for
    `events.amazonaws.com` scoped to the audit-dispatch EventBridge rule
  - it never handles a public HTTP event and refuses one if it receives one

That is why this handler may accept an unsigned event: the payload is not a
trust boundary, the IAM invoke permission is. Audit work is still dispatched to
the public function as a *signed* child invocation carrying the per-attempt
dispatch fence, so the unauthenticated-invocation protection is unchanged — it
just lives in the function that is actually reachable from outside AWS.

Its own IAM role may only invoke the main function and write its log group; it
carries no SSM parameter access and no Secrets Manager access.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logging.basicConfig(level=logging.INFO, force=True)

logger = logging.getLogger(__name__)

_DISPATCH_OPERATION = "dispatch_audits"


def handler(event: Any, context: Any = None) -> dict:
    if not isinstance(event, dict):
        return {"error": "invalid invocation"}
    if "requestContext" in event:
        logger.warning("audit_dispatcher: rejected public HTTP invocation")
        return {"error": "unsupported invocation"}
    if event.get("operation") != _DISPATCH_OPERATION:
        logger.warning("audit_dispatcher: rejected unknown operation")
        return {"error": "unsupported invocation"}
    try:
        from app.services.audit_pipeline import dispatch_audit_jobs

        summary = asyncio.run(dispatch_audit_jobs())
    except Exception as exc:
        logger.warning(
            "audit_dispatcher: dispatch poll failed error_type=%s",
            type(exc).__name__,
        )
        return {"error": "audit dispatch poll failed"}
    return {
        "status": "completed",
        "recovered": summary.recovered,
        "claimed": summary.claimed,
        "scheduled": summary.scheduled,
        "released": summary.released,
        "terminal": summary.terminal,
    }
