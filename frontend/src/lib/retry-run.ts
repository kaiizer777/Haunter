/**
 * Run statuses that have settled. Only these may be retried — cloning a run
 * the orchestrator is still mutating would fork the pipeline.
 * Mirrors backend `_RETRYABLE_STATUSES` in app/routers/traces.py.
 */
export const RETRYABLE_RUN_STATUSES: ReadonlySet<string> = new Set([
  "pr_opened",
  "fallback_commented",
  "flaky_detected",
  "completed",
  "error",
]);

/** True when a run in this status can be re-dispatched via POST /runs/{id}/retry. */
export function isRetryableStatus(status: string): boolean {
  return RETRYABLE_RUN_STATUSES.has(status);
}