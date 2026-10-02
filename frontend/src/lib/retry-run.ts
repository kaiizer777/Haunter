/**
 * Run statuses that have settled. Only these may be retried — cloning a run
 * the orchestrator is still mutating would fork the pipeline and race the
 * in-flight transitions on the source row.
 *
 * Mirrors the backend `_TERMINAL_STATUSES` set in `app/orchestrator.py`, which
 * `POST /runs/{id}/retry` reads to decide retryability. The server is the
 * authority (it 409s a non-settled run); this copy only decides whether to
 * render the button, so a run that is somehow not in the set is simply not
 * offered the action.
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
