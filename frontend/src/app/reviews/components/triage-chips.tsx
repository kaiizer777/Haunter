import React from "react";
import { cn } from "@/lib/utils";
import {
  OUTCOME_ORDER,
  outcomeMeaning,
  type OutcomeKey,
} from "../review-triage";

export interface TriageChipStyle {
  label: string;
  dot: string;
  active: string;
}

export const TRIAGE_CHIP_STYLES: Readonly<Record<OutcomeKey, TriageChipStyle>> =
  Object.freeze({
    changes_requested: {
      label: "Need changes",
      dot: "bg-rose-400",
      active: "border-rose-500/60 bg-rose-950/40 text-rose-200",
    },
    advisory: {
      label: "Review advised",
      dot: "bg-amber-400",
      active: "border-amber-500/60 bg-amber-950/40 text-amber-200",
    },
    in_progress: {
      label: "In progress",
      dot: "bg-zinc-400",
      active: "border-zinc-500/60 bg-zinc-800 text-zinc-100",
    },
    failed: {
      label: "Failed",
      dot: "bg-rose-300",
      active: "border-rose-500/60 bg-rose-950/40 text-rose-100",
    },
    suppressed: {
      label: "Suppressed",
      dot: "bg-zinc-500",
      active: "border-zinc-500/60 bg-zinc-800 text-zinc-100",
    },
    clean: {
      label: "Great to merge",
      dot: "bg-emerald-400",
      active: "border-emerald-500/60 bg-emerald-950/40 text-emerald-200",
    },
  });

export interface TriageBucket {
  key: OutcomeKey;
  label: string;
  dot: string;
  active: string;
  meaning: string;
}

export const TRIAGE_BUCKETS: readonly TriageBucket[] = Object.freeze(
  OUTCOME_ORDER.map((key) => ({
    key,
    ...TRIAGE_CHIP_STYLES[key],
    meaning: outcomeMeaning(key),
  }))
);

export interface TriageChipsProps {
  counts: Record<OutcomeKey, number>;
  activeOutcome: OutcomeKey | null;
  onToggle: (key: OutcomeKey | null) => void;
  className?: string;
}

/**
 * Computes the lead sentence for the triage summary strip.
 */
export function getLeadSentence(counts: Record<OutcomeKey, number>): React.ReactNode {
  const actionable = (counts.changes_requested ?? 0) + (counts.advisory ?? 0);
  if (actionable > 0) {
    return (
      <>
        <span className="font-semibold text-zinc-100">{actionable}</span>{" "}
        {actionable === 1 ? "review needs" : "reviews need"} a decision before merge
        {(counts.failed ?? 0) > 0 && (
          <>
            {" · "}
            <span className="font-semibold text-rose-300">{counts.failed}</span>{" "}
            failed to analyse
          </>
        )}
      </>
    );
  }

  if ((counts.in_progress ?? 0) > 0) {
    return (
      <>
        <span className="font-semibold text-zinc-100">{counts.in_progress}</span>{" "}
        {counts.in_progress === 1 ? "analysis is" : "analyses are"} still running
        {(counts.failed ?? 0) > 0 && (
          <>
            {" ("}
            <span className="font-semibold text-rose-300">{counts.failed}</span>{" "}
            {counts.failed === 1
              ? "failed analysis requires inspection"
              : "failed analyses require inspection"}
            {")"}
          </>
        )}
      </>
    );
  }

  if ((counts.failed ?? 0) > 0) {
    return (
      <>
        <span className="font-semibold text-rose-300">{counts.failed}</span>{" "}
        {counts.failed === 1
          ? "failed analysis requires"
          : "failed analyses require"}{" "}
        retry or inspection
      </>
    );
  }

  return <>Nothing needs a decision right now</>;
}

/**
 * Triage outcome strip and summary.
 *
 * Provides a high-density orientation strip showing actionable merge decisions
 * and review states at a glance, with interactive toggle chips.
 */
export function TriageChips({
  counts,
  activeOutcome,
  onToggle,
  className,
}: TriageChipsProps) {
  const total = Object.values(counts).reduce((sum, n) => sum + n, 0);

  return (
    <section
      aria-label="Review triage summary for this page"
      className={cn(
        "rounded-xl border border-zinc-800/80 bg-gradient-to-b from-[#111115] to-[#0c0c0e] p-3 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]",
        className
      )}
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        {/* Lead sentence — the triage verdict in words */}
        <p
          data-testid="triage-lead-sentence"
          className="text-xs font-medium tracking-tight text-zinc-200"
        >
          {getLeadSentence(counts)}
        </p>

        {/* Counter pills */}
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="font-mono text-[10px] uppercase tracking-wider text-zinc-400 font-medium">
            In view
          </span>

          {/* All button */}
          <button
            type="button"
            onClick={() => onToggle(null)}
            aria-pressed={activeOutcome === null}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 font-mono text-[10px] uppercase tracking-wider transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400 active:translate-y-[0.5px]",
              activeOutcome === null
                ? "border-zinc-500/60 bg-zinc-800 font-semibold text-zinc-100 shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_1px_3px_rgba(0,0,0,0.3)]"
                : "border-zinc-800 bg-zinc-950 text-zinc-400 hover:border-zinc-700 hover:text-zinc-200"
            )}
          >
            <span>All</span>
            <span className={activeOutcome === null ? "text-zinc-300" : "text-zinc-500"}>
              {total}
            </span>
          </button>

          {/* Bucket buttons */}
          {TRIAGE_BUCKETS.map((bucket) => {
            const count = counts[bucket.key] ?? 0;
            const isActive = activeOutcome === bucket.key;
            const isZero = count === 0;
            return (
              <button
                key={bucket.key}
                type="button"
                onClick={() => onToggle(isActive ? null : bucket.key)}
                aria-pressed={isActive}
                title={bucket.meaning}
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 font-mono text-[10px] uppercase tracking-wider transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400 active:translate-y-[0.5px]",
                  isActive
                    ? cn("font-semibold shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_1px_3px_rgba(0,0,0,0.3)]", bucket.active)
                    : isZero
                      ? "border-zinc-800/60 bg-zinc-950/40 text-zinc-500 hover:border-zinc-800 hover:text-zinc-300"
                      : "border-zinc-800 bg-zinc-950 text-zinc-300 hover:border-zinc-700 hover:text-zinc-100"
                )}
              >
                <span
                  className={cn(
                    "h-1.5 w-1.5 shrink-0 rounded-full transition-opacity",
                    bucket.dot,
                    isZero && !isActive ? "opacity-30" : "opacity-100"
                  )}
                  aria-hidden="true"
                />
                <span>{bucket.label}</span>
                <span
                  className={
                    isActive
                      ? "opacity-90 font-bold"
                      : isZero
                        ? "text-zinc-600 font-normal"
                        : "text-zinc-400 font-medium"
                  }
                >
                  {count}
                </span>
              </button>
            );
          })}
        </div>
      </div>
    </section>
  );
}
