"use client";

import { useCallback, useState } from "react";
import { RotateCcw, Loader2, AlertTriangle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { api, RunOut } from "@/lib/api";
import { isRetryableStatus } from "@/lib/retry-run";
import { cn } from "@/lib/utils";

/**
 * Inline one-click retry error banner. Rendered by the caller's error region
 * so screen readers announce the failure via the surrounding `role="alert"`.
 */
export function RetryRunError({ message }: { message: string }) {
  return (
    <div
      role="alert"
      className="flex items-start gap-2.5 rounded-[6px] border border-rose-900/60 bg-rose-950/30 px-3 py-2.5 text-xs text-rose-300"
    >
      <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-rose-400 mt-px" />
      <span className="font-mono">{message}</span>
    </div>
  );
}

export interface RetryRunButtonProps {
  runId: string;
  status: string;
  /** Called with the newly created child run once the retry is accepted. */
  onRetried?: (child: RunOut) => void;
  /** Hides the control (e.g. row actions that already surface it elsewhere). */
  className?: string;
  label?: string;
  "aria-label"?: string;
}

/**
 * One-click retry control.
 *
 * POSTs to /runs/{id}/retry, which clones the settled run into a fresh pending
 * child and re-dispatches the orchestrator. Renders nothing for a run that has
 * not settled — the backend would reject it with 409, so offering the control
 * there would be a dead affordance.
 */
export default function RetryRunButton({
  runId,
  status,
  onRetried,
  className,
  label = "Retry",
  "aria-label": ariaLabel,
}: RetryRunButtonProps) {
  const [isRetrying, setIsRetrying] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleRetry = useCallback(async () => {
    setIsRetrying(true);
    setError(null);
    try {
      const child = await api.retryRun(runId);
      onRetried?.(child);
    } catch (err: unknown) {
      setError(
        err instanceof Error ? err.message : "Failed to dispatch a retry for this run."
      );
    } finally {
      setIsRetrying(false);
    }
  }, [runId, onRetried]);

  if (!isRetryableStatus(status)) {
    return null;
  }

  return (
    <div className={cn("flex flex-col items-end gap-1.5", className)}>
      {error && <RetryRunError message={error} />}
      <Button
        variant="outline"
        size="sm"
        onClick={handleRetry}
        disabled={isRetrying}
        aria-busy={isRetrying}
        aria-label={ariaLabel ?? `Retry run ${runId}`}
        title="Re-diagnose this failure and open a fresh fix PR"
        className="inline-flex items-center gap-1.5 h-7 px-2.5 text-[11px] font-mono rounded-[5px] text-zinc-300 hover:text-white bg-gradient-to-b from-zinc-800/90 via-zinc-800/80 to-zinc-900/90 border-t border-t-zinc-600/70 border-x border-x-zinc-700/60 border-b border-b-zinc-900 shadow-[inset_0_1px_0_rgba(255,255,255,0.1),0_1px_3px_rgba(0,0,0,0.35)] hover:border-t-zinc-500 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.2),0_2px_6px_rgba(0,0,0,0.4)] active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.4)] transition-all disabled:opacity-50 disabled:cursor-not-allowed"
      >
        {isRetrying ? (
          <Loader2 className="h-3 w-3 animate-spin text-amber-400" />
        ) : (
          <RotateCcw className="h-3 w-3 text-zinc-500" />
        )}
        <span>{isRetrying ? "Retrying..." : label}</span>
      </Button>
    </div>
  );
}