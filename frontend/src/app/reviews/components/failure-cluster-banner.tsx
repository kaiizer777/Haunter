import React, { useState } from "react";
import { AlertTriangle, Copy, Check, X, ChevronDown, ChevronUp } from "lucide-react";
import { cn } from "@/lib/utils";
import type { FailureCluster } from "../review-triage";

export interface FailureClusterBannerProps {
  clusters: readonly FailureCluster[];
  onDismiss?: () => void;
  className?: string;
}

/**
 * Aggregated root-cause failure cluster banner.
 *
 * Groups repeated failure signatures so identical infrastructure timeouts
 * or free-tier model exhaustions appear once as actionable insights rather than
 * flooding the review triage queue.
 */
export function FailureClusterBanner({
  clusters,
  onDismiss,
  className,
}: FailureClusterBannerProps) {
  const [copiedSignature, setCopiedSignature] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<boolean>(true);

  const total = clusters.reduce((sum, c) => sum + c.count, 0);

  if (clusters.length === 0 || total < 2) {
    return null;
  }

  const handleCopy = async (sig: string) => {
    try {
      await navigator.clipboard.writeText(sig);
      setCopiedSignature(sig);
      setTimeout(() => setCopiedSignature(null), 2000);
    } catch {
      // Fallback
    }
  };

  const displayedClusters = expanded ? clusters : clusters.slice(0, 2);

  return (
    <div
      role="status"
      aria-label="Failed analyses root cause summary"
      className={cn(
        "rounded-xl border-t border-t-rose-500/50 border-x border-x-rose-600/30 border-b border-b-rose-950 bg-gradient-to-b from-rose-950/40 via-rose-950/25 to-[#0e0c0d] px-4 py-3 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_4px_12px_rgba(0,0,0,0.35)]",
        className
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-start gap-2.5 min-w-0 flex-1">
          <AlertTriangle
            className="mt-0.5 h-4 w-4 shrink-0 text-rose-400"
            aria-hidden="true"
          />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <h3 className="text-xs font-semibold text-rose-100">
                {total} failed analyses
                {clusters.length > 1 && ` across ${clusters.length} causes`}
              </h3>
              {clusters.length > 2 && (
                <button
                  type="button"
                  onClick={() => setExpanded((v) => !v)}
                  className="inline-flex items-center gap-0.5 text-[11px] font-mono text-rose-300/80 hover:text-rose-100 transition-colors focus:outline-none focus:ring-1 focus:ring-rose-400"
                >
                  <span>{expanded ? "Show less" : `+${clusters.length - 2} more`}</span>
                  {expanded ? (
                    <ChevronUp className="h-3 w-3" />
                  ) : (
                    <ChevronDown className="h-3 w-3" />
                  )}
                </button>
              )}
            </div>

            <ul className="mt-2 space-y-1.5">
              {displayedClusters.map((cluster) => {
                const isCopied = copiedSignature === cluster.signature;
                return (
                  <li
                    key={cluster.signature}
                    className="group flex items-center justify-between gap-2 rounded-md border border-rose-500/20 bg-rose-950/30 px-2.5 py-1 text-[11px]"
                  >
                    <div className="flex items-baseline gap-2 min-w-0 flex-1">
                      <span className="shrink-0 rounded border border-rose-500/40 bg-rose-900/60 px-1.5 py-0.2 font-mono text-[10px] font-bold tabular-nums text-rose-200">
                        {cluster.count}
                      </span>
                      <span className="min-w-0 flex-1 font-mono text-rose-100/90 break-words line-clamp-2">
                        {cluster.signature}
                      </span>
                    </div>

                    <button
                      type="button"
                      onClick={() => handleCopy(cluster.signature)}
                      aria-label={
                        isCopied
                          ? `Copied signature for ${cluster.signature}`
                          : `Copy failure signature: ${cluster.signature}`
                      }
                      title={isCopied ? "Copied!" : "Copy failure signature"}
                      className="shrink-0 rounded p-1 text-rose-300/70 hover:text-rose-100 hover:bg-rose-900/50 transition-colors focus:outline-none focus:ring-1 focus:ring-rose-400"
                    >
                      {isCopied ? (
                        <Check className="h-3 w-3 text-emerald-400" />
                      ) : (
                        <Copy className="h-3 w-3" />
                      )}
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        </div>

        {/* Optional dismiss button */}
        {onDismiss && (
          <button
            type="button"
            onClick={onDismiss}
            aria-label="Dismiss failure clusters banner"
            className="shrink-0 rounded p-1 text-rose-400/80 hover:text-rose-100 hover:bg-rose-900/40 transition-colors focus:outline-none focus:ring-1 focus:ring-rose-400"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        )}
      </div>
    </div>
  );
}
