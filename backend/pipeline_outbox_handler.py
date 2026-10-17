"""IAM-only AWS Lambda entry point for the pipeline-outbox sweeper (#157).

Deployed like ``audit_dispatcher_handler`` (see ``infra/aws/lambda.tf``):

  - no Lambda Function URL, so not reachable from the internet;
  - the only resource-policy grant is ``lambda:InvokeFunction`` for
    ``events.amazonaws.com`` scoped to the pipeline-outbox EventBridge rule;
  - never handles a public HTTP event and refuses one if it receives one.

The event payload is not a trust boundary here — the IAM invoke permission
is. Redrives go through the hosting adapter as *signed* pipeline
self-invocations, so the unauthenticated-invocation protection is unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logging.basicConfig(level=logging.INFO, force=True)

logger = logging.getLogger(__name__)

_DISPATCH_OPERATION = "sweep_pipeline_outbox"


def handler(event: Any, context: Any = None) -> dict:
    if not isinstance(event, dict):
        return {"error": "invalid invocation"}
    if "requestContext" in event:
        logger.warning("pipeline_outbox: rejected public HTTP invocation")
        return {"error": "unsupported invocation"}
    if event.get("operation") != _DISPATCH_OPERATION:
        logger.warning("pipeline_outbox: rejected unknown operation")
        return {"error": "unsupported invocation"}
    try:
        from app.services.pipeline_outbox import sweep_stale_pending_runs

        summary = asyncio.run(sweep_stale_pending_runs())
    except Exception as exc:
        logger.warning(
            "pipeline_outbox: sweep failed error_type=%s",
            type(exc).__name__,
        )
        return {"error": "pipeline outbox sweep failed"}
    return {
        "status": "completed",
        "scanned": summary.scanned,
        "redriven": summary.redriven,
        "exhausted": summary.exhausted,
        "skipped": summary.skipped,
    }
