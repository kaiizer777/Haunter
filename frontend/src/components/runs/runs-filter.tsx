"use client";

import { RepoOut } from "@/lib/api";
import { Filter, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";

interface RunsFilterProps {
  repos: RepoOut[];
  selectedRepoId: string;
  selectedStatus: string;
  from: string;
  to: string;
  onRepoChange: (repoId: string) => void;
  onStatusChange: (status: string) => void;
  onFromChange: (from: string) => void;
  onToChange: (to: string) => void;
  onReset: () => void;
}

const STATUS_OPTIONS = [
  { value: "", label: "All Statuses" },
  { value: "completed", label: "Completed (PR Opened)" },
  { value: "fallback", label: "Fallback (Commented)" },
  { value: "error", label: "Error" },
  { value: "pending", label: "Pending" },
  { value: "context_gathering", label: "Context Gathering" },
  { value: "fix_generation", label: "Fix Generation" },
  { value: "verification", label: "Verification" },
];

export function RunsFilter({
  repos,
  selectedRepoId,
  selectedStatus,
  from,
  to,
  onRepoChange,
  onStatusChange,
  onFromChange,
  onToChange,
  onReset,
}: RunsFilterProps) {
  const hasActiveFilters = Boolean(selectedRepoId || selectedStatus || from || to);

  return (
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-[6px] border border-zinc-800 bg-[#121215] p-3">
      <div className="flex flex-wrap items-center gap-2.5">
        <div className="flex items-center gap-1.5 text-xs text-zinc-400 font-medium pl-1">
          <Filter className="h-3.5 w-3.5 text-amber-400" />
          <span>Filters:</span>
        </div>

        {/* Repo Select */}
        <select
          value={selectedRepoId}
          onChange={(e) => onRepoChange(e.target.value)}
          className="h-8 rounded-[5px] border border-zinc-800 bg-[#0c0c0e] px-2.5 text-xs text-zinc-200 focus:border-amber-400 focus:outline-none"
        >
          <option value="">All Repositories</option>
          {repos.map((r) => (
            <option key={r.id} value={r.id}>
              {r.owner}/{r.name}
            </option>
          ))}
        </select>

        {/* Status Select */}
        <select
          value={selectedStatus}
          onChange={(e) => onStatusChange(e.target.value)}
          className="h-8 rounded-[5px] border border-zinc-800 bg-[#0c0c0e] px-2.5 text-xs text-zinc-200 focus:border-amber-400 focus:outline-none"
        >
          {STATUS_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>

        {/* Date From */}
        <input
          type="date"
          value={from}
          onChange={(e) => onFromChange(e.target.value)}
          placeholder="From"
          className="h-8 rounded-[5px] border border-zinc-800 bg-[#0c0c0e] px-2 text-xs text-zinc-300 focus:border-amber-400 focus:outline-none"
        />

        {/* Date To */}
        <input
          type="date"
          value={to}
          onChange={(e) => onToChange(e.target.value)}
          placeholder="To"
          className="h-8 rounded-[5px] border border-zinc-800 bg-[#0c0c0e] px-2 text-xs text-zinc-300 focus:border-amber-400 focus:outline-none"
        />
      </div>

      {hasActiveFilters && (
        <Button
          variant="ghost"
          size="sm"
          onClick={onReset}
          className="h-7 text-xs text-zinc-400 hover:text-zinc-100 flex items-center gap-1.5"
        >
          <RotateCcw className="h-3 w-3" />
          Clear
        </Button>
      )}
    </div>
  );
}
