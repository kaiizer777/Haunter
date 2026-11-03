import React from "react";
import { ShieldCheck, ShieldAlert, Sparkles, Zap } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ReviewRibbonTab } from "../types";

export interface TelemetryRibbonStats {
  totalCount: number;
  highRiskCount: number;
  avgScore: number;
  scoredCount: number;
  totalPatches: number;
}

export interface TelemetryRibbonProps {
  activeTab: ReviewRibbonTab;
  onTabChange: (tab: ReviewRibbonTab) => void;
  stats: TelemetryRibbonStats;
  highRiskOnly?: boolean;
  className?: string;
}

/**
 * High-density telemetry ribbon and filter tabs.
 *
 * Provides instant macro awareness across 4 operational dimensions:
 * 1. Scanned reviews (total throughput)
 * 2. High-Risk flagged (risk >= 71 changes requested)
 * 3. Avg Risk score (honest mean across scored reviews)
 * 4. Actionable Patches (suggested code remediations)
 */
export function TelemetryRibbon({
  activeTab,
  onTabChange,
  stats,
  highRiskOnly = false,
  className,
}: TelemetryRibbonProps) {
  const isAllActive = activeTab === "all" && !highRiskOnly;
  const isHighRiskActive = highRiskOnly || activeTab === "high_risk";
  const isAvgRiskActive = activeTab === "avg_risk";
  const isPatchesActive = activeTab === "patches";

  return (
    <div
      role="region"
      aria-label="Code reviews telemetry ribbon"
      className={cn(
        "rounded-xl border-t border-t-zinc-700/60 border-x border-x-zinc-800/80 border-b border-b-zinc-900 bg-gradient-to-b from-[#16161a] via-[#111114] to-[#0a0a0d] p-1.5 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_4px_16px_rgba(0,0,0,0.35)]",
        className
      )}
    >
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-1.5">
        {/* Tab 1: All Reviews */}
        <button
          type="button"
          aria-pressed={isAllActive}
          onClick={() => onTabChange("all")}
          className={cn(
            "group flex items-baseline justify-between gap-2 rounded-lg px-3 py-2 text-left transition-all outline-none focus-visible:ring-2 focus-visible:ring-amber-400 active:translate-y-[0.5px]",
            isAllActive
              ? "border-t border-t-zinc-600/80 border-x border-x-zinc-700/60 border-b border-b-zinc-900 bg-zinc-800/90 shadow-[inset_0_1px_0_rgba(255,255,255,0.1),0_2px_6px_rgba(0,0,0,0.4)]"
              : "border border-transparent bg-transparent hover:border-zinc-800/80 hover:bg-zinc-900/60"
          )}
        >
          <div className="min-w-0">
            <div className="flex items-center gap-1.5 text-[11px] font-mono font-semibold uppercase tracking-wider text-zinc-300">
              <ShieldCheck className="h-3.5 w-3.5 text-emerald-400 shrink-0" />
              <span>Scanned</span>
            </div>
            <div className="mt-0.5 text-[11px] text-zinc-400 font-sans truncate">
              Push &amp; PR scans · server total
            </div>
          </div>
          <div className="text-xl font-bold font-mono text-zinc-100 tabular-nums">
            {stats.totalCount}
          </div>
        </button>

        {/* Tab 2: High Risk Flagged */}
        <button
          type="button"
          aria-pressed={isHighRiskActive}
          onClick={() => onTabChange("high_risk")}
          className={cn(
            "group flex items-baseline justify-between gap-2 rounded-lg px-3 py-2 text-left transition-all outline-none focus-visible:ring-2 focus-visible:ring-rose-400 active:translate-y-[0.5px]",
            isHighRiskActive
              ? "border-t border-t-rose-500/60 border-x border-x-rose-600/40 border-b border-b-rose-950 bg-rose-950/40 shadow-[inset_0_1px_0_rgba(255,255,255,0.1),0_2px_6px_rgba(0,0,0,0.4)]"
              : "border border-transparent bg-transparent hover:border-zinc-800/80 hover:bg-zinc-900/60"
          )}
        >
          <div className="min-w-0">
            <div className="flex items-center gap-1.5 text-[11px] font-mono font-semibold uppercase tracking-wider text-rose-300">
              <ShieldAlert className="h-3.5 w-3.5 text-rose-400 shrink-0" />
              <span>High-Risk ≥ 71</span>
            </div>
            <div
              className="mt-0.5 text-[11px] text-zinc-400 font-sans truncate"
              title="Scores >= 71 request changes · counted on this page only"
            >
              Changes requested · this page
            </div>
          </div>
          <div className="text-xl font-bold font-mono text-rose-400 tabular-nums">
            {stats.highRiskCount}
          </div>
        </button>

        {/* Tab 3: Avg Risk Score */}
        <button
          type="button"
          aria-pressed={isAvgRiskActive}
          onClick={() => onTabChange("avg_risk")}
          className={cn(
            "group flex items-baseline justify-between gap-2 rounded-lg px-3 py-2 text-left transition-all outline-none focus-visible:ring-2 focus-visible:ring-amber-400 active:translate-y-[0.5px]",
            isAvgRiskActive
              ? "border-t border-t-zinc-600/80 border-x border-x-zinc-700/60 border-b border-b-zinc-900 bg-zinc-800/90 shadow-[inset_0_1px_0_rgba(255,255,255,0.1),0_2px_6px_rgba(0,0,0,0.4)]"
              : "border border-transparent bg-transparent hover:border-zinc-800/80 hover:bg-zinc-900/60"
          )}
        >
          <div className="min-w-0">
            <div className="flex items-center gap-1.5 text-[11px] font-mono font-semibold uppercase tracking-wider text-amber-300">
              <Sparkles className="h-3.5 w-3.5 text-amber-400 shrink-0" />
              <span>Avg Risk</span>
            </div>
            <div className="mt-0.5 text-[11px] text-zinc-400 font-sans truncate">
              {stats.scoredCount === 0
                ? "No scored reviews"
                : `Mean score · this page · ${stats.scoredCount} scored`}
            </div>
          </div>
          <div
            className="text-xl font-bold font-mono text-zinc-100 tabular-nums"
            title={
              stats.scoredCount === 0
                ? "No scored reviews on this page — only completed + publication-skipped analyses score"
                : `Mean of ${stats.scoredCount} scored ${
                    stats.scoredCount === 1 ? "review" : "reviews"
                  } · only completed + publication-skipped analyses score`
            }
          >
            {stats.scoredCount === 0 ? (
              <span className="text-zinc-500">—</span>
            ) : (
              <>
                {stats.avgScore}{" "}
                <span className="text-[11px] text-zinc-400 font-normal">
                  / 100
                </span>
              </>
            )}
          </div>
        </button>

        {/* Tab 4: Actionable Patches */}
        <button
          type="button"
          aria-pressed={isPatchesActive}
          onClick={() => onTabChange("patches")}
          className={cn(
            "group flex items-baseline justify-between gap-2 rounded-lg px-3 py-2 text-left transition-all outline-none focus-visible:ring-2 focus-visible:ring-cyan-400 active:translate-y-[0.5px]",
            isPatchesActive
              ? "border-t border-t-cyan-500/60 border-x border-x-cyan-600/40 border-b border-b-cyan-950 bg-cyan-950/40 shadow-[inset_0_1px_0_rgba(255,255,255,0.1),0_2px_6px_rgba(0,0,0,0.4)]"
              : "border border-transparent bg-transparent hover:border-zinc-800/80 hover:bg-zinc-900/60"
          )}
        >
          <div className="min-w-0">
            <div className="flex items-center gap-1.5 text-[11px] font-mono font-semibold uppercase tracking-wider text-cyan-300">
              <Zap className="h-3.5 w-3.5 text-cyan-400 shrink-0" />
              <span>Patches</span>
            </div>
            <div className="mt-0.5 text-[11px] text-zinc-400 font-sans truncate">
              With suggested fix · this page
            </div>
          </div>
          <div className="text-xl font-bold font-mono text-cyan-400 tabular-nums">
            {stats.totalPatches}
          </div>
        </button>
      </div>
    </div>
  );
}
