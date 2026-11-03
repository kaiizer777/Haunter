import React from "react";
import {
  Search,
  X,
  ShieldAlert,
  EyeOff,
  SlidersHorizontal,
  Radio,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { SelectDropdown } from "@/components/ui/select-dropdown";
import { Input } from "@/components/ui/input";
import { SORT_OPTIONS, RISK_CRITICAL, type SortKey } from "../review-triage";

export interface RepoOption {
  value: string;
  label: string;
}

export interface FilterToolbarProps {
  searchQuery: string;
  onSearchChange: (q: string) => void;
  selectedRepoId: string;
  onRepoChange: (repoId: string) => void;
  repoOptions: RepoOption[];
  selectedSeverity: string;
  onSeverityChange: (sev: string) => void;
  highRiskOnly: boolean;
  onToggleHighRisk: () => void;
  hideFailed: boolean;
  onToggleHideFailed: () => void;
  failedCount: number;
  sortKey: SortKey;
  onSortChange: (sort: SortKey) => void;
  isLiveSyncing?: boolean;
  className?: string;
}

const SEVERITIES = ["all", "critical", "high", "medium", "low"] as const;

/**
 * Filter toolbar for Code Reviews.
 *
 * Houses repository scope selector, severity filter pills, risk ≥ 71 toggle,
 * failed-row toggle, sort order switcher, live-sync radar indicator, and search.
 */
export function FilterToolbar({
  searchQuery,
  onSearchChange,
  selectedRepoId,
  onRepoChange,
  repoOptions,
  selectedSeverity,
  onSeverityChange,
  highRiskOnly,
  onToggleHighRisk,
  hideFailed,
  onToggleHideFailed,
  failedCount,
  sortKey,
  onSortChange,
  isLiveSyncing = false,
  className,
}: FilterToolbarProps) {
  return (
    <div
      role="region"
      aria-label="Filter and search reviews"
      className={cn(
        "flex flex-col gap-3 rounded-xl border border-zinc-800/80 bg-[#0f0f12] p-3 shadow-[inset_0_1px_0_rgba(255,255,255,0.03)] lg:flex-row lg:items-center lg:justify-between",
        className
      )}
    >
      {/* Left controls: Repo picker, severity pills, risk toggle, hide failed */}
      <div className="flex flex-wrap items-center gap-2.5">
        {/* Repo selector dropdown */}
        <div className="w-[200px] shrink-0">
          <SelectDropdown
            value={selectedRepoId}
            onChange={onRepoChange}
            options={repoOptions}
            placeholder="All Repositories"
          />
        </div>

        {/* Severity filter pills */}
        <div
          className="flex items-center rounded-lg border border-zinc-800 bg-zinc-950 p-1 text-xs"
          role="group"
          aria-label="Filter by finding severity"
        >
          {SEVERITIES.map((sev) => {
            const isSelected = selectedSeverity.toLowerCase() === sev;
            return (
              <button
                key={sev}
                type="button"
                onClick={() => onSeverityChange(sev)}
                aria-pressed={isSelected}
                className={cn(
                  "rounded-md px-2.5 py-1 font-mono uppercase text-[10px] tracking-wider transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400 active:translate-y-[0.5px]",
                  isSelected
                    ? "bg-zinc-800 font-semibold text-zinc-100 shadow-[0_1px_3px_rgba(0,0,0,0.3)]"
                    : "text-zinc-400 hover:text-zinc-200"
                )}
              >
                {sev}
              </button>
            );
          })}
        </div>

        {/* High Risk score filter toggle (risk >= 71) */}
        <button
          type="button"
          onClick={onToggleHighRisk}
          aria-pressed={highRiskOnly}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-mono transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-rose-400 active:translate-y-[0.5px]",
            highRiskOnly
              ? "border-rose-500/50 bg-rose-950/40 font-semibold text-rose-300 shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_2px_4px_rgba(0,0,0,0.3)]"
              : "border-zinc-800 bg-zinc-950 text-zinc-400 hover:border-zinc-700 hover:text-zinc-200"
          )}
        >
          <ShieldAlert className="h-3.5 w-3.5 text-rose-400 shrink-0" />
          <span>Risk ≥ {RISK_CRITICAL}</span>
        </button>

        {/* Hide-failed toggle */}
        {(failedCount > 0 || hideFailed) && (
          <button
            type="button"
            onClick={onToggleHideFailed}
            aria-pressed={hideFailed}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-mono transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400 active:translate-y-[0.5px]",
              hideFailed
                ? "border-zinc-500/60 bg-zinc-800 font-semibold text-zinc-100 shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_2px_4px_rgba(0,0,0,0.3)]"
                : "border-zinc-800 bg-zinc-950 text-zinc-400 hover:border-zinc-700 hover:text-zinc-200"
            )}
          >
            <EyeOff className="h-3.5 w-3.5 text-zinc-400 shrink-0" />
            <span>
              {hideFailed
                ? `Show failed (${failedCount})`
                : `Hide failed (${failedCount})`}
            </span>
          </button>
        )}

        {/* Live Syncing radar badge */}
        {isLiveSyncing && (
          <div
            role="status"
            aria-label="Live Syncing active"
            className="inline-flex items-center gap-1.5 rounded-md border border-amber-500/40 bg-gradient-to-b from-amber-500/15 via-amber-500/10 to-amber-500/5 px-2.5 py-1 font-mono text-[11px] font-medium text-amber-300 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_0_8px_rgba(245,158,11,0.15)]"
          >
            <span className="relative flex h-2 w-2 shrink-0" aria-hidden="true">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-amber-400 opacity-75" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-amber-400" />
            </span>
            <Radio className="h-3 w-3 text-amber-400" aria-hidden="true" />
            <span>Live Syncing</span>
          </div>
        )}
      </div>

      {/* Right controls: Sort pills and search input */}
      <div className="flex flex-wrap items-center gap-2.5">
        {/* Sort Group */}
        <div
          className="flex items-center rounded-lg border border-zinc-800 bg-zinc-950 p-1"
          role="group"
          aria-label="Sort reviews on this page"
        >
          <SlidersHorizontal
            className="ml-1.5 h-3.5 w-3.5 shrink-0 text-zinc-400"
            aria-hidden="true"
          />
          {SORT_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              type="button"
              onClick={() => onSortChange(opt.value)}
              aria-pressed={sortKey === opt.value}
              title={
                opt.value === "attention"
                  ? "Reorders reviews on this page: blockers first, clean last, failures at the bottom."
                  : "Newest first. Matches repository incoming sequence."
              }
              className={cn(
                "rounded-md px-2.5 py-1 font-mono text-[10px] uppercase tracking-wider transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400 active:translate-y-[0.5px]",
                sortKey === opt.value
                  ? "bg-zinc-800 font-semibold text-zinc-100 shadow-[0_1px_3px_rgba(0,0,0,0.3)]"
                  : "text-zinc-400 hover:text-zinc-200"
              )}
            >
              {opt.label}
            </button>
          ))}
        </div>

        {/* Search input with clear button */}
        <div className="relative flex-1 sm:w-64 sm:flex-initial">
          <Search
            className="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-zinc-400 pointer-events-none"
            aria-hidden="true"
          />
          <Input
            placeholder="Search findings, commits, files..."
            value={searchQuery}
            onChange={(e) => onSearchChange(e.target.value)}
            className="pl-8 pr-7 h-8 text-xs bg-zinc-950 border-zinc-800 text-zinc-100 placeholder:text-zinc-500 focus:border-amber-400/80 focus:ring-1 focus:ring-amber-400/50"
            aria-label="Search reviews"
          />
          {searchQuery && (
            <button
              type="button"
              onClick={() => onSearchChange("")}
              aria-label="Clear search text"
              className="absolute right-2 top-2 rounded p-0.5 text-zinc-400 hover:text-zinc-200 transition-colors focus:outline-none focus:ring-1 focus:ring-amber-400"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
