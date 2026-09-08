"use client";

import { useEffect, useState, useCallback, useRef, useMemo } from "react";
import { useRouter } from "next/navigation";
import { AppLayout } from "@/components/layout/app-layout";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusBadge } from "@/components/runs/status-badge";
import { SelectDropdown } from "@/components/ui/select-dropdown";
import { api, RepoOut, RunOut } from "@/lib/api";
import { formatRelativeTime } from "@/lib/utils";
import {
  Activity,
  ChevronLeft,
  ChevronRight,
  GitCommit,
  GitBranch,
  Clock,
  ArrowUpRight,
  Trash2,
  Filter,
  Search,
  RotateCcw,
  X,
} from "lucide-react";

const PAGE_SIZE = 20;

/**
 * Format duration between two ISO timestamps.
 * Returns formatted string: "< 1s", "48s", "1m 12s", "12m 4s", "1h 5m", etc.
 * Returns "-" if missing or invalid.
 */
function formatDuration(createdAt?: string, updatedAt?: string): string {
  if (!createdAt || !updatedAt) return "-";
  const start = new Date(createdAt).getTime();
  const end = new Date(updatedAt).getTime();
  if (isNaN(start) || isNaN(end) || end < start) return "-";

  const totalSeconds = Math.floor((end - start) / 1000);
  if (totalSeconds < 1) return "< 1s";
  if (totalSeconds < 60) return `${totalSeconds}s`;

  const totalMinutes = Math.floor(totalSeconds / 60);
  const remainingSeconds = totalSeconds % 60;

  if (totalMinutes < 60) {
    return remainingSeconds > 0
      ? `${totalMinutes}m ${remainingSeconds}s`
      : `${totalMinutes}m`;
  }

  const hours = Math.floor(totalMinutes / 60);
  const remainingMinutes = totalMinutes % 60;

  return remainingMinutes > 0
    ? `${hours}h ${remainingMinutes}m`
    : `${hours}h`;
}

export default function RunsPage() {
  const router = useRouter();

  const [runs, setRuns] = useState<RunOut[]>([]);
  const [total, setTotal] = useState(0);
  const [repos, setRepos] = useState<RepoOut[]>([]);
  const [reposMap, setReposMap] = useState<Record<string, RepoOut>>({});

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Selection & Deletion state
  const [selectedRunIds, setSelectedRunIds] = useState<Set<string>>(new Set());
  const [isBatchDeleting, setIsBatchDeleting] = useState(false);

  const headerCheckboxRef = useRef<HTMLInputElement | null>(null);
  const searchInputRef = useRef<HTMLInputElement | null>(null);

  // Filters
  const [searchQuery, setSearchQuery] = useState("");
  const [selectedRepoId, setSelectedRepoId] = useState("");
  const [selectedStatus, setSelectedStatus] = useState("");
  const [page, setPage] = useState(0);

  // Keyboard shortcut: '/' to focus search input
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (
        e.key === "/" &&
        !["INPUT", "TEXTAREA", "SELECT"].includes((e.target as HTMLElement)?.tagName)
      ) {
        e.preventDefault();
        searchInputRef.current?.focus();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  // Fetch Repos list for dropdown and name lookup
  useEffect(() => {
    api.getRepos().then((data) => {
      setRepos(data);
      const map: Record<string, RepoOut> = {};
      data.forEach((r) => {
        map[r.id] = r;
      });
      setReposMap(map);
    }).catch(() => {});
  }, []);

  const fetchRuns = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.getRuns({
        repo_id: selectedRepoId || undefined,
        status: selectedStatus || undefined,
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      });
      setRuns(data.runs);
      setTotal(data.total);
    } catch (err: unknown) {
      if (err instanceof Error) {
        setError(err.message);
      } else {
        setError("Failed to fetch runs.");
      }
    } finally {
      setLoading(false);
    }
  }, [selectedRepoId, selectedStatus, page]);

  useEffect(() => {
    fetchRuns();
  }, [fetchRuns]);

  // Client-side filtering
  const filteredRuns = useMemo(() => {
    if (!searchQuery.trim()) {
      return runs;
    }
    const q = searchQuery.toLowerCase().trim();
    return runs.filter((run) => {
      const repo = reposMap[run.repo_id];
      const repoLabel = repo ? `${repo.owner}/${repo.name}` : `repo-${run.repo_id.slice(0, 8)}`;
      const branchName = run.head_branch;
      const commitSha = run.head_sha;
      const diagnosis = "";

      const matchRepo = repoLabel.toLowerCase().includes(q);
      const matchBranch = branchName.toLowerCase().includes(q);
      const matchSha = commitSha.toLowerCase().includes(q);
      const matchDiag = diagnosis.toLowerCase().includes(q);

      return matchRepo || matchBranch || matchSha || matchDiag;
    });
  }, [runs, reposMap, searchQuery]);

  const hasActiveFilters = Boolean(searchQuery || selectedRepoId || selectedStatus);

  const handleResetFilters = () => {
    setSearchQuery("");
    setSelectedRepoId("");
    setSelectedStatus("");
    setPage(0);
    setSelectedRunIds(new Set());
  };

  // Selection helpers
  const displayedRunIds = filteredRuns.map((r) => r.id);
  const selectedDisplayedCount = filteredRuns.filter((r) => selectedRunIds.has(r.id)).length;
  const isAllSelected = filteredRuns.length > 0 && selectedDisplayedCount === filteredRuns.length;
  const isIndeterminate = selectedDisplayedCount > 0 && selectedDisplayedCount < filteredRuns.length;

  useEffect(() => {
    if (headerCheckboxRef.current) {
      headerCheckboxRef.current.indeterminate = isIndeterminate;
    }
  }, [isIndeterminate]);

  // Prune selectedRunIds when runs change
  useEffect(() => {
    setSelectedRunIds((prev) => {
      if (prev.size === 0) return prev;
      const currentRunIds = new Set(runs.map((r) => r.id));
      let hasChanged = false;
      const next = new Set<string>();
      for (const id of prev) {
        if (currentRunIds.has(id)) {
          next.add(id);
        } else {
          hasChanged = true;
        }
      }
      return hasChanged ? next : prev;
    });
  }, [runs]);

  const handleToggleSelectAll = () => {
    if (isAllSelected) {
      setSelectedRunIds((prev) => {
        const next = new Set(prev);
        displayedRunIds.forEach((id) => next.delete(id));
        return next;
      });
    } else {
      setSelectedRunIds((prev) => {
        const next = new Set(prev);
        displayedRunIds.forEach((id) => next.add(id));
        return next;
      });
    }
  };

  const handleToggleRun = (runId: string) => {
    setSelectedRunIds((prev) => {
      const next = new Set(prev);
      if (next.has(runId)) {
        next.delete(runId);
      } else {
        next.add(runId);
      }
      return next;
    });
  };

  // Reset selection when page or filters change
  useEffect(() => {
    setSelectedRunIds(new Set());
  }, [page, selectedRepoId, selectedStatus, searchQuery]);

  const handleBatchDelete = async () => {
    if (selectedRunIds.size === 0) return;

    setIsBatchDeleting(true);
    setError(null);
    const idsToDelete = Array.from(selectedRunIds);
    try {
      await api.deleteRuns(idsToDelete);
      setSelectedRunIds(new Set());
      if (runs.length <= idsToDelete.length && page > 0) {
        setPage((p) => Math.max(0, p - 1));
      } else {
        await fetchRuns();
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Failed to delete selected runs.";
      setError(msg);
    } finally {
      setIsBatchDeleting(false);
    }
  };

  const totalPages = Math.ceil(total / PAGE_SIZE);

  return (
    <AppLayout
      title="CI Runs & Diagnoses"
      subtitle="Autonomous pipeline runs, sandbox verifications, and PR traces"
      actions={
        selectedRunIds.size > 0 ? (
          <Button
            variant="destructive"
            size="sm"
            onClick={handleBatchDelete}
            disabled={isBatchDeleting}
            className="h-8 px-3 text-xs font-mono font-medium rounded-[5px] flex items-center gap-1.5 shadow-sm"
          >
            <Trash2 className="h-3.5 w-3.5" />
            {isBatchDeleting ? "Deleting..." : `Delete Selected (${selectedRunIds.size})`}
          </Button>
        ) : undefined
      }
    >
      <div className="space-y-6 min-w-0">
        {/* Modern Filter Bar */}
        <div className="relative z-20 flex flex-wrap items-center justify-between gap-4 rounded-lg border border-zinc-800/80 bg-[#0d0d10]/90 backdrop-blur-sm shadow-[inset_0_1px_0_0_rgba(255,255,255,0.03)] p-3.5">
          <div className="flex flex-wrap items-center gap-3.5 flex-1 min-w-[300px]">
            <div className="flex items-center gap-1.5 text-xs text-zinc-400 font-medium pl-1">
              <Filter className="h-3.5 w-3.5 text-zinc-500" />
              <span className="font-mono text-[11px] tracking-wide text-zinc-400 uppercase">Filters</span>
            </div>

            {/* Search input */}
            <div className="relative w-64">
              <Search className="absolute left-3 top-2.5 h-4 w-4 text-zinc-500 pointer-events-none" />
              <Input
                ref={searchInputRef}
                type="text"
                placeholder="Search branch, sha, diagnosis..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="h-9 pl-9 pr-8 text-xs font-mono rounded-[6px] bg-zinc-900/50 border-zinc-800/80 focus:outline-none focus:border-amber-400/80 focus:ring-1 focus:ring-amber-400/25 placeholder:text-zinc-500 placeholder:font-sans transition-all"
              />
              {!searchQuery ? (
                <div className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 flex items-center">
                  <kbd className="inline-flex h-4 min-w-4 items-center justify-center rounded border border-zinc-700/60 bg-zinc-800/60 px-1 font-mono text-[10px] text-zinc-400 select-none">
                    /
                  </kbd>
                </div>
              ) : (
                <button
                  type="button"
                  onClick={() => {
                    setSearchQuery("");
                    searchInputRef.current?.focus();
                  }}
                  className="absolute right-2.5 top-1/2 -translate-y-1/2 text-zinc-500 hover:text-zinc-200 p-0.5 rounded transition-colors"
                  aria-label="Clear search"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              )}
            </div>

            {/* Repo select */}
            <SelectDropdown
              value={selectedRepoId}
              onChange={(val) => {
                setSelectedRepoId(val);
                setPage(0);
              }}
              options={[
                {
                  value: "",
                  label: `All Repositories (${repos.length})`,
                  icon: <GitBranch className="h-3.5 w-3.5 text-violet-400" />,
                },
                ...repos.map((r) => ({
                  value: r.id,
                  label: `${r.owner}/${r.name}`,
                  icon: <GitBranch className="h-3.5 w-3.5 text-violet-400" />,
                })),
              ]}
              buttonClassName="min-w-[190px]"
            />

            {/* Status select */}
            <SelectDropdown
              value={selectedStatus}
              onChange={(val) => {
                setSelectedStatus(val);
                setPage(0);
              }}
              options={[
                {
                  value: "",
                  label: "All Statuses",
                  badge: (
                    <span className="h-2 w-2 rounded-full bg-cyan-400 shadow-[0_0_8px_rgba(34,211,238,0.7)]" />
                  ),
                },
                {
                  value: "completed",
                  label: "Completed / PR Opened",
                  badge: (
                    <span className="h-2 w-2 rounded-full bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.6)]" />
                  ),
                },
                {
                  value: "fix_generation",
                  label: "Generating Fix",
                  badge: (
                    <span className="h-2 w-2 rounded-full bg-amber-400 animate-pulse shadow-[0_0_8px_rgba(251,191,36,0.6)]" />
                  ),
                },
                {
                  value: "error",
                  label: "Error / Failed",
                  badge: (
                    <span className="h-2 w-2 rounded-full bg-rose-400 shadow-[0_0_8px_rgba(251,113,133,0.6)]" />
                  ),
                },
                {
                  value: "fallback",
                  label: "Fallback Comment",
                  badge: (
                    <span className="h-2 w-2 rounded-full bg-violet-400 shadow-[0_0_8px_rgba(167,139,250,0.6)]" />
                  ),
                },
              ]}
              buttonClassName="min-w-[160px]"
            />
          </div>

          <div className="flex items-center gap-3.5">
            <span className="tabular-nums text-zinc-500 font-mono text-[11px]">
              Showing {filteredRuns.length} of {total} runs
            </span>

            {hasActiveFilters && (
              <Button
                variant="ghost"
                size="sm"
                onClick={handleResetFilters}
                className="h-6 px-2 text-[11px] font-mono rounded-[4px] text-zinc-400 hover:text-zinc-200 bg-zinc-800/40 hover:bg-zinc-800/80 border border-zinc-800 hover:border-zinc-700 transition-all flex items-center gap-1.5"
              >
                <RotateCcw className="h-3 w-3" />
                Clear Filters
              </Button>
            )}
          </div>
        </div>

        {error && (
          <div className="rounded-[7px] border border-red-900/60 bg-red-950/30 p-3.5 text-[13px] text-red-300">
            {error}
          </div>
        )}

        {/* Selection Toolbar */}
        {selectedRunIds.size > 0 && (
          <div className="relative z-10 border border-zinc-700/80 bg-zinc-900/95 backdrop-blur shadow-xl rounded-lg px-4 py-2.5 flex items-center justify-between text-xs animate-in fade-in slide-in-from-bottom-2 duration-200">
            <div className="flex items-center gap-2.5">
              <span className="inline-flex h-2 w-2 rounded-full bg-amber-400 shadow-[0_0_6px_rgba(251,191,36,0.6)]" />
              <span className="font-mono text-zinc-300 text-[11px]">
                <span className="font-semibold text-zinc-100 tabular-nums">{selectedRunIds.size}</span> run
                {selectedRunIds.size > 1 ? "s" : ""} selected
              </span>
            </div>
            <div className="flex items-center gap-2.5">
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setSelectedRunIds(new Set())}
                disabled={isBatchDeleting}
                className="h-7 px-2.5 text-[11px] font-mono text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/60 rounded-[4px] transition-colors"
              >
                Clear selection
              </Button>
              <Button
                variant="destructive"
                size="sm"
                onClick={handleBatchDelete}
                disabled={isBatchDeleting}
                className="flex items-center gap-1.5 h-7 px-3 text-[11px] font-mono font-medium rounded-[4px] shadow-sm transition-all"
              >
                <Trash2 className="h-3.5 w-3.5" />
                {isBatchDeleting ? "Deleting..." : `Delete Selected (${selectedRunIds.size})`}
              </Button>
            </div>
          </div>
        )}

        {/* Dense Table */}
        <div className="relative z-0 rounded-lg border border-zinc-800/80 bg-[#0d0d10]/90 backdrop-blur-sm overflow-hidden shadow-[inset_0_1px_0_0_rgba(255,255,255,0.02)]">
          {loading ? (
            <div className="p-4 space-y-3">
              <Skeleton className="h-9 w-full" />
              <Skeleton className="h-9 w-full" />
              <Skeleton className="h-9 w-full" />
              <Skeleton className="h-9 w-full" />
            </div>
          ) : filteredRuns.length === 0 ? (
            <div className="p-12 text-center border border-dashed border-zinc-800/80 m-4 rounded-[6px]">
              <Activity className="h-6 w-6 mx-auto text-zinc-600 mb-2" />
              <h3 className="text-sm font-medium text-zinc-300">
                {runs.length === 0 ? "No CI runs found" : "No matching CI runs"}
              </h3>
              <p className="text-xs text-zinc-500 mt-1 max-w-sm mx-auto">
                {runs.length === 0
                  ? "Trigger a failing GitHub Actions workflow on a connected repo to see autonomous diagnosis."
                  : "Try clearing your search query or adjusting the repository/status filter."}
              </p>
              {hasActiveFilters && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleResetFilters}
                  className="mt-4 text-xs font-mono"
                >
                  <RotateCcw className="h-3.5 w-3.5 mr-1.5" />
                  Reset Filters
                </Button>
              )}
            </div>
          ) : (
            <Table>
              <TableHeader className="border-b border-zinc-800/80 bg-[#09090b]">
                <TableRow className="border-b border-zinc-800/80 hover:bg-transparent">
                  {/* Column 1: STATUS */}
                  <TableHead className="w-[14%] pl-4 text-[11px] font-mono tracking-wider uppercase text-zinc-500 font-medium select-none py-3">
                    Status
                  </TableHead>

                  {/* Column 2: REPOSITORY */}
                  <TableHead className="w-[21%] text-[11px] font-mono tracking-wider uppercase text-zinc-500 font-medium select-none py-3">
                    Repository
                  </TableHead>

                  {/* Column 3: BRANCH / COMMIT */}
                  <TableHead className="w-[22%] text-[11px] font-mono tracking-wider uppercase text-zinc-500 font-medium select-none py-3">
                    Branch / Commit
                  </TableHead>

                  {/* Column 4: TRIGGERED */}
                  <TableHead className="w-[12%] text-[11px] font-mono tracking-wider uppercase text-zinc-500 font-medium select-none py-3">
                    Triggered
                  </TableHead>

                  {/* Column 5: DURATION */}
                  <TableHead className="w-[11%] text-[11px] font-mono tracking-wider uppercase text-zinc-500 font-medium select-none py-3">
                    Duration
                  </TableHead>

                  {/* Column 6: COST */}
                  <TableHead className="w-[10%] text-[11px] font-mono tracking-wider uppercase text-zinc-500 font-medium select-none py-3">
                    Cost
                  </TableHead>

                  {/* Column 7: ACTIONS */}
                  <TableHead className="w-[10%] text-right pr-4 text-[11px] font-mono tracking-wider uppercase text-zinc-500 font-medium select-none py-3">
                    Actions
                  </TableHead>

                  {/* Column 8: [CHECKBOX] */}
                  <TableHead className="w-10 px-3 text-center py-3">
                    <div className="flex items-center justify-center">
                      <input
                        ref={(el) => {
                          headerCheckboxRef.current = el;
                          if (el) {
                            el.indeterminate = isIndeterminate;
                          }
                        }}
                        type="checkbox"
                        checked={isAllSelected}
                        onChange={handleToggleSelectAll}
                        disabled={loading || filteredRuns.length === 0 || isBatchDeleting}
                        aria-label="Select all runs"
                        className="h-3.5 w-3.5 accent-amber-400 rounded-[3px] border-zinc-700 bg-zinc-900 focus:ring-0 cursor-pointer transition-opacity disabled:opacity-40 disabled:cursor-not-allowed"
                      />
                    </div>
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody className="divide-y divide-zinc-800/40">
                {filteredRuns.map((run) => {
                  const repo = reposMap[run.repo_id];
                  const repoLabel = repo ? `${repo.owner}/${repo.name}` : `repo-${run.repo_id.slice(0, 8)}`;
                  const isSelected = selectedRunIds.has(run.id);
                  const branchName = run.head_branch || "unknown";
                  const commitSha = run.head_sha.slice(0, 7);

                  return (
                    <TableRow
                      key={run.id}
                      onClick={() => router.push(`/runs/detail?id=${run.id}`)}
                      className={`cursor-pointer group transition-colors duration-150 hover:bg-zinc-800/30 border-b border-zinc-800/50 ${
                        isSelected ? "bg-amber-500/[0.04] hover:bg-amber-500/[0.07]" : ""
                      } ${isBatchDeleting ? "opacity-50 pointer-events-none" : ""}`}
                    >
                      {/* Column 1: STATUS */}
                      <TableCell className="pl-4 py-3 align-middle">
                        <StatusBadge status={run.status} />
                      </TableCell>

                      {/* Column 2: REPOSITORY */}
                      <TableCell className="py-3 align-middle">
                        <span
                          className="font-mono text-xs font-semibold text-zinc-200 group-hover:text-zinc-100 transition-colors truncate block max-w-[200px]"
                          title={repoLabel}
                        >
                          {repoLabel}
                        </span>
                      </TableCell>

                      {/* Column 3: BRANCH / COMMIT */}
                      <TableCell className="py-3 align-middle">
                        <div className="flex items-center gap-2 font-mono text-xs text-zinc-400 min-w-0">
                          <span className="inline-flex items-center gap-1.5 truncate max-w-[140px] text-zinc-400" title={branchName}>
                            <GitBranch className="h-3.5 w-3.5 text-zinc-500 shrink-0" />
                            <span className="truncate">{branchName}</span>
                          </span>
                          <span className="text-zinc-600 shrink-0 select-none">•</span>
                          <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-[4px] bg-zinc-800/60 border border-zinc-700/40 text-[11px] text-zinc-400 font-mono select-all shrink-0">
                            <GitCommit className="h-3 w-3 text-zinc-500 shrink-0" />
                            {commitSha}
                          </span>
                        </div>
                      </TableCell>

                      {/* Column 4: TRIGGERED */}
                      <TableCell
                        className="text-zinc-400 font-mono text-xs tabular-nums py-3 align-middle whitespace-nowrap"
                        title={new Date(run.created_at).toLocaleString()}
                      >
                        {formatRelativeTime(run.created_at)}
                      </TableCell>

                      {/* Column 5: DURATION */}
                      <TableCell className="py-3 align-middle whitespace-nowrap">
                        <span
                          className="inline-flex items-center gap-1.5 font-mono text-xs text-zinc-300 tabular-nums"
                          title={`Started: ${new Date(run.created_at).toLocaleString()}${
                            run.updated_at ? `\nUpdated: ${new Date(run.updated_at).toLocaleString()}` : ""
                          }`}
                        >
                          <Clock className="h-3.5 w-3.5 text-zinc-500 shrink-0" />
                          {formatDuration(run.created_at, run.updated_at)}
                        </span>
                      </TableCell>

                      {/* Column 6: COST */}
                      <TableCell className="py-3 align-middle">
                        <div className="flex flex-col font-mono">
                          {run.cost && run.cost > 0 ? (
                            <span className="text-xs font-mono font-medium text-emerald-400 tabular-nums">
                              ${run.cost.toFixed(4)}
                            </span>
                          ) : (
                            <span className="text-xs font-mono font-medium text-zinc-500 tabular-nums">-</span>
                          )}
                          {run.tokens && run.tokens > 0 ? (
                            <span className="text-[11px] font-mono text-zinc-500 tabular-nums">
                              {((run.tokens || 0) / 1000).toFixed(1)}k tok
                            </span>
                          ) : null}
                        </div>
                      </TableCell>

                      {/* Column 7: ACTIONS */}
                      <TableCell className="text-right pr-4 py-3 align-middle" onClick={(e) => e.stopPropagation()}>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={(e) => {
                            e.stopPropagation();
                            router.push(`/runs/detail?id=${run.id}`);
                          }}
                          className="group/btn h-6 px-2 text-[11px] font-mono rounded-[4px] text-zinc-400 hover:text-zinc-100 hover:bg-zinc-800/70 border border-transparent hover:border-zinc-700/50 transition-all inline-flex items-center gap-1"
                        >
                          <span>Trace</span>
                          <ArrowUpRight className="h-3 w-3 text-zinc-500 group-hover/btn:text-zinc-300 group-hover/btn:translate-x-0.5 group-hover/btn:-translate-y-0.5 transition-transform" />
                        </Button>
                      </TableCell>

                      {/* Column 8: [CHECKBOX] */}
                      <TableCell className="w-10 px-3 text-center py-3 align-middle" onClick={(e) => e.stopPropagation()}>
                        <div className="flex items-center justify-center">
                          <input
                            type="checkbox"
                            checked={isSelected}
                            disabled={isBatchDeleting}
                            onChange={(e) => {
                              e.stopPropagation();
                              handleToggleRun(run.id);
                            }}
                            onClick={(e) => e.stopPropagation()}
                            aria-label={`Select run ${run.id}`}
                            className="h-3.5 w-3.5 accent-amber-400 rounded-[3px] border-zinc-700 bg-zinc-900 focus:ring-0 cursor-pointer transition-opacity disabled:opacity-40 disabled:cursor-not-allowed"
                          />
                        </div>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          )}

          {/* Pagination Footer */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between border-t border-zinc-800/60 bg-[#09090b] px-5 py-3 text-xs text-zinc-400">
              <span className="font-mono text-[11px] text-zinc-500 tabular-nums">
                Showing {page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, total)} of {total} runs
              </span>
              <div className="flex items-center gap-2.5">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setPage((p) => Math.max(0, p - 1))}
                  disabled={page === 0 || loading}
                  className="h-7 px-2.5 text-[11px] font-mono rounded-[4px] border-zinc-800 bg-zinc-900/60 hover:bg-zinc-800/80 text-zinc-300 hover:text-zinc-100 disabled:opacity-40"
                >
                  <ChevronLeft className="h-3.5 w-3.5 mr-1 text-zinc-500" />
                  Prev
                </Button>
                <span className="font-mono text-[11px] px-1 text-zinc-400 tabular-nums">
                  {page + 1} / {totalPages}
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
                  disabled={page >= totalPages - 1 || loading}
                  className="h-7 px-2.5 text-[11px] font-mono rounded-[4px] border-zinc-800 bg-zinc-900/60 hover:bg-zinc-800/80 text-zinc-300 hover:text-zinc-100 disabled:opacity-40"
                >
                  Next
                  <ChevronRight className="h-3.5 w-3.5 ml-1 text-zinc-500" />
                </Button>
              </div>
            </div>
          )}
        </div>
      </div>
    </AppLayout>
  );
}
