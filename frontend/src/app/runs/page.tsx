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
  CheckCircle2,
  Check,
  Coins,
  FolderGit2,
  Sparkles,
  RefreshCw,
  Layers,
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
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Selection & Deletion state
  const [selectedRunIds, setSelectedRunIds] = useState<Set<string>>(new Set());
  const [isBatchDeleting, setIsBatchDeleting] = useState(false);
  const [copiedSha, setCopiedSha] = useState<string | null>(null);

  const headerCheckboxRef = useRef<HTMLInputElement | null>(null);
  const searchInputRef = useRef<HTMLInputElement | null>(null);

  // Filters
  const [searchQuery, setSearchQuery] = useState("");
  const [selectedRepoId, setSelectedRepoId] = useState("");
  const [selectedStatus, setSelectedStatus] = useState("");
  const [page, setPage] = useState(0);

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

  const fetchRuns = useCallback(async (isManualRefresh = false) => {
    if (isManualRefresh) {
      setIsRefreshing(true);
    } else {
      setLoading(true);
    }
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
      setIsRefreshing(false);
    }
  }, [selectedRepoId, selectedStatus, page]);

  useEffect(() => {
    fetchRuns();
  }, [fetchRuns]);

  // Keyboard shortcut handlers: '/' to search, 'r' to refresh, 'Escape' to blur or clear
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (["INPUT", "TEXTAREA", "SELECT"].includes((e.target as HTMLElement)?.tagName)) {
        if (e.key === "Escape") {
          (e.target as HTMLElement).blur();
        }
        return;
      }
      if (e.key === "/") {
        e.preventDefault();
        searchInputRef.current?.focus();
      } else if (e.key === "r" || e.key === "R") {
        e.preventDefault();
        fetchRuns(true);
      } else if (e.key === "Escape") {
        setSelectedRunIds(new Set());
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [fetchRuns]);

  // Copy commit SHA with micro-feedback
  const handleCopySha = (e: React.MouseEvent, sha: string) => {
    e.stopPropagation();
    navigator.clipboard.writeText(sha);
    setCopiedSha(sha);
    setTimeout(() => setCopiedSha(null), 1500);
  };

  // Telemetry metrics calculation
  const metrics = useMemo(() => {
    const totalRuns = total || runs.length;
    const healedRuns = runs.filter(
      (r) => ["completed", "pr_opened", "passed"].includes(r.status.toLowerCase())
    ).length;
    const failedRuns = runs.filter(
      (r) => ["error", "failed"].includes(r.status.toLowerCase())
    ).length;
    const inProgressRuns = runs.filter((r) =>
      ["fix_generation", "verification", "context_gathering", "pending"].includes(r.status.toLowerCase())
    ).length;

    const totalCost = runs.reduce((acc, r) => acc + (r.cost || 0), 0);
    const totalTokens = runs.reduce((acc, r) => acc + (r.tokens || 0), 0);

    const durations = runs
      .map((r) => {
        if (!r.created_at || !r.updated_at) return null;
        const diff = new Date(r.updated_at).getTime() - new Date(r.created_at).getTime();
        return isNaN(diff) || diff <= 0 ? null : diff;
      })
      .filter((d): d is number => d !== null);

    const avgDurationMs =
      durations.length > 0
        ? durations.reduce((a, b) => a + b, 0) / durations.length
        : null;

    const avgDurationStr = avgDurationMs
      ? avgDurationMs < 60000
        ? `${Math.round(avgDurationMs / 1000)}s`
        : `${Math.floor(avgDurationMs / 60000)}m ${Math.round((avgDurationMs % 60000) / 1000)}s`
      : "-";

    const healRate = runs.length > 0 ? Math.round((healedRuns / runs.length) * 100) : 0;

    return {
      totalRuns,
      healedRuns,
      failedRuns,
      inProgressRuns,
      totalCost,
      totalTokens,
      avgDurationStr,
      healRate,
    };
  }, [runs, total]);

  // Client-side search filtering
  const filteredRuns = useMemo(() => {
    if (!searchQuery.trim()) {
      return runs;
    }
    const q = searchQuery.toLowerCase().trim();
    return runs.filter((run) => {
      const repo = reposMap[run.repo_id];
      const repoLabel = repo ? `${repo.owner}/${repo.name}` : `repo-${run.repo_id.slice(0, 8)}`;
      const branchName = run.head_branch || "";
      const commitSha = run.head_sha || "";

      const matchRepo = repoLabel.toLowerCase().includes(q);
      const matchBranch = branchName.toLowerCase().includes(q);
      const matchSha = commitSha.toLowerCase().includes(q);

      return matchRepo || matchBranch || matchSha;
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

  // Quick filter tab options (Linear / GitHub style)
  const quickFilterTabs = [
    {
      id: "",
      label: "All Runs",
      count: total,
    },
    {
      id: "completed",
      label: "PR Opened",
      count: metrics.healedRuns,
      dot: "bg-emerald-400",
    },
    {
      id: "fix_generation",
      label: "In Progress",
      count: metrics.inProgressRuns,
      dot: "bg-amber-400 animate-pulse",
    },
    {
      id: "error",
      label: "Failed",
      count: metrics.failedRuns,
      dot: "bg-rose-400",
    },
  ];

  return (
    <AppLayout
      title="CI Runs & Diagnoses"
      subtitle="Autonomous pipeline runs, sandbox verifications, and PR traces"
      actions={
        <div className="flex items-center gap-2.5">
          <Button
            variant="outline"
            size="sm"
            onClick={() => fetchRuns(true)}
            disabled={loading || isRefreshing}
            className="group h-8 px-3 text-xs font-mono rounded-[6px] text-zinc-300 hover:text-white bg-gradient-to-b from-zinc-800/90 via-zinc-850 to-zinc-900/90 border-t border-t-zinc-600/70 border-x border-x-zinc-700/60 border-b border-b-zinc-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.12),0_1px_3px_rgba(0,0,0,0.35),0_1px_2px_rgba(0,0,0,0.2)] hover:border-t-zinc-500 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.2),0_2px_6px_rgba(0,0,0,0.4)] active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.4)] flex items-center gap-1.5 transition-all"
            title="Refresh runs list (Hot-key: R)"
          >
            <RefreshCw className={`h-3.5 w-3.5 transition-colors ${isRefreshing ? "animate-spin text-amber-400" : "text-zinc-400 group-hover:text-amber-400"}`} />
            <span className="hidden sm:inline">Refresh</span>
          </Button>

          {selectedRunIds.size > 0 && (
            <Button
              variant="destructive"
              size="sm"
              onClick={handleBatchDelete}
              disabled={isBatchDeleting}
              className="h-8 px-3 text-xs font-mono font-medium rounded-[6px] bg-gradient-to-b from-red-600 via-red-650 to-red-800 hover:from-red-500 hover:via-red-600 hover:to-red-750 text-white border-t border-t-red-400/60 border-x border-x-red-600/70 border-b border-b-red-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.25),0_2px_6px_rgba(220,38,38,0.35)] active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.6)] flex items-center gap-1.5 transition-all"
            >
              <Trash2 className="h-3.5 w-3.5" />
              {isBatchDeleting ? "Deleting..." : `Delete (${selectedRunIds.size})`}
            </Button>
          )}
        </div>
      }
    >
      <div className="space-y-5 min-w-0">
        {/* Telemetry Metric Cards */}
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3.5">
          {/* Total Runs */}
          <div className="rounded-lg border border-zinc-800/80 bg-gradient-to-b from-[#121216]/90 to-[#0c0c0e]/90 backdrop-blur p-4 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.03)] flex flex-col justify-between">
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-mono uppercase tracking-wider text-zinc-400">Total CI Runs</span>
              <Activity className="h-4 w-4 text-zinc-500" />
            </div>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="text-2xl font-bold font-mono text-zinc-100 tabular-nums">{metrics.totalRuns}</span>
              <span className="text-[11px] font-mono text-zinc-500">runs</span>
            </div>
            <div className="mt-1 text-[11px] text-zinc-500 truncate">Autonomous pipelines tracked</div>
          </div>

          {/* Auto-Heal Rate */}
          <div className="rounded-lg border border-zinc-800/80 bg-gradient-to-b from-[#121216]/90 to-[#0c0c0e]/90 backdrop-blur p-4 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.03)] flex flex-col justify-between">
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-mono uppercase tracking-wider text-zinc-400">Heal Rate</span>
              <CheckCircle2 className="h-4 w-4 text-emerald-400" />
            </div>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="text-2xl font-bold font-mono text-emerald-400 tabular-nums">
                {runs.length > 0 ? `${metrics.healRate}%` : "-"}
              </span>
              <span className="text-[11px] font-mono text-zinc-500">PRs opened</span>
            </div>
            <div className="mt-1 text-[11px] text-zinc-500 truncate">Verified sandbox repairs</div>
          </div>

          {/* Avg MTTR */}
          <div className="rounded-lg border border-zinc-800/80 bg-gradient-to-b from-[#121216]/90 to-[#0c0c0e]/90 backdrop-blur p-4 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.03)] flex flex-col justify-between">
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-mono uppercase tracking-wider text-zinc-400">Avg MTTR</span>
              <Clock className="h-4 w-4 text-amber-400" />
            </div>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="text-2xl font-bold font-mono text-zinc-100 tabular-nums">{metrics.avgDurationStr}</span>
              <span className="text-[11px] font-mono text-zinc-500">per fix</span>
            </div>
            <div className="mt-1 text-[11px] text-zinc-500 truncate">Failure diagnosis to PR</div>
          </div>

          {/* Total Compute Cost */}
          <div className="rounded-lg border border-zinc-800/80 bg-gradient-to-b from-[#121216]/90 to-[#0c0c0e]/90 backdrop-blur p-4 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.03)] flex flex-col justify-between">
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-mono uppercase tracking-wider text-zinc-400">Total Compute</span>
              <Coins className="h-4 w-4 text-amber-400" />
            </div>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="text-2xl font-bold font-mono text-zinc-100 tabular-nums">
                ${metrics.totalCost.toFixed(4)}
              </span>
              {metrics.totalTokens > 0 && (
                <span className="text-[11px] font-mono text-zinc-500">
                  ({(metrics.totalTokens / 1000).toFixed(1)}k tok)
                </span>
              )}
            </div>
            <div className="mt-1 text-[11px] text-zinc-500 truncate">Inference & sandbox tokens</div>
          </div>
        </div>

        {/* Quick Status Filter Tabs & Control Bar */}
        <div className="space-y-3">
          {/* Quick Segmented Tabs */}
          <div className="flex items-center justify-between gap-3 overflow-x-auto pb-0.5">
            <div className="flex items-center gap-1.5 p-1 rounded-lg border border-zinc-800/80 bg-[#0d0d10]/90 backdrop-blur-sm">
              {quickFilterTabs.map((tab) => {
                const isActive = selectedStatus === tab.id;
                return (
                  <button
                    key={tab.id}
                    type="button"
                    onClick={() => {
                      setSelectedStatus(tab.id);
                      setPage(0);
                    }}
                    className={`flex items-center gap-2 px-3 py-1.5 rounded-[5px] text-xs font-mono transition-all ${
                      isActive
                        ? "bg-zinc-800 text-zinc-100 font-semibold border border-zinc-700/60 shadow-sm"
                        : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/40"
                    }`}
                  >
                    {tab.dot && <span className={`h-1.5 w-1.5 rounded-full ${tab.dot}`} />}
                    <span>{tab.label}</span>
                    <span
                      className={`text-[10px] px-1.5 py-0.2 rounded-full font-mono tabular-nums ${
                        isActive ? "bg-zinc-700 text-zinc-200" : "bg-zinc-900 text-zinc-500 border border-zinc-800"
                      }`}
                    >
                      {tab.count}
                    </span>
                  </button>
                );
              })}
            </div>

            <div className="hidden sm:flex items-center gap-2 text-xs font-mono text-zinc-500">
              <span className="tabular-nums">
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
                  Reset
                </Button>
              )}
            </div>
          </div>

          {/* Detailed Filters Bar */}
          <div className="relative z-20 flex flex-wrap items-center justify-between gap-3.5 rounded-lg border border-zinc-800/80 bg-[#0d0d10]/90 backdrop-blur-sm shadow-[inset_0_1px_0_0_rgba(255,255,255,0.03)] p-3">
            <div className="flex flex-wrap items-center gap-3 flex-1 min-w-[300px]">
              <div className="flex items-center gap-1.5 text-xs text-zinc-400 font-medium pl-1">
                <Filter className="h-3.5 w-3.5 text-zinc-500" />
                <span className="font-mono text-[11px] tracking-wide text-zinc-400 uppercase">Filters</span>
              </div>

              {/* Search input */}
              <div className="relative flex-1 min-w-[220px] max-w-sm">
                <Search className="absolute left-3 top-2.5 h-3.5 w-3.5 text-zinc-500 pointer-events-none" />
                <Input
                  ref={searchInputRef}
                  type="text"
                  placeholder="Search branch, commit sha, repo..."
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  className="h-8.5 pl-8.5 pr-8 text-xs font-mono rounded-[6px] bg-zinc-900/60 border-zinc-800/80 focus:outline-none focus:border-amber-400/80 focus:ring-1 focus:ring-amber-400/25 placeholder:text-zinc-500 placeholder:font-sans transition-all"
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
                    icon: <FolderGit2 className="h-3.5 w-3.5 text-amber-400/80" />,
                  },
                  ...repos.map((r) => ({
                    value: r.id,
                    label: `${r.owner}/${r.name}`,
                    icon: <FolderGit2 className="h-3.5 w-3.5 text-amber-400/80" />,
                  })),
                ]}
                buttonClassName="min-w-[190px] h-8.5"
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
                buttonClassName="min-w-[165px] h-8.5"
              />
            </div>

            <div className="flex sm:hidden items-center justify-between w-full pt-2 border-t border-zinc-800/60">
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
                  Reset
                </Button>
              )}
            </div>
          </div>
        </div>

        {error && (
          <div className="rounded-[7px] border border-red-900/60 bg-red-950/30 p-3.5 text-[13px] text-red-300">
            {error}
          </div>
        )}

        {/* Floating Batch Selection Toolbar */}
        {selectedRunIds.size > 0 && (
          <div className="relative z-10 overflow-hidden rounded-xl border-t border-t-zinc-600/70 border-x border-x-zinc-700/60 border-b border-b-zinc-950 bg-gradient-to-b from-[#18181f]/95 via-[#131317]/95 to-[#0d0d11]/95 backdrop-blur-md px-4 py-2.5 flex items-center justify-between text-xs shadow-[inset_0_1px_0_rgba(255,255,255,0.14),inset_0_-1px_0_rgba(0,0,0,0.5),0_12px_32px_rgba(0,0,0,0.65),0_2px_6px_rgba(0,0,0,0.4)] animate-in fade-in slide-in-from-bottom-2 duration-200">
            {/* Subtle light-from-above ambient gradient sheen */}
            <span
              aria-hidden="true"
              className="pointer-events-none absolute inset-x-0 top-0 h-4 bg-gradient-to-b from-white/[0.06] to-transparent rounded-t-xl"
            />
            <div className="flex items-center gap-2.5 relative z-10">
              <div className="flex items-center gap-2 px-2.5 py-1 rounded-[6px] bg-gradient-to-b from-amber-500/15 via-amber-500/10 to-amber-500/5 border-t border-t-amber-400/40 border-x border-x-amber-500/30 border-b border-b-amber-600/20 shadow-[inset_0_1px_0_rgba(255,255,255,0.15),0_1px_2px_rgba(0,0,0,0.3)]">
                <span className="relative flex h-2 w-2">
                  <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-60" />
                  <span className="relative inline-flex rounded-full h-2 w-2 bg-amber-400 shadow-[0_0_6px_rgba(251,191,36,0.8)]" />
                </span>
                <span className="font-mono text-amber-200 text-[11px] font-medium tracking-tight">
                  <span className="font-bold text-amber-100 tabular-nums">{selectedRunIds.size}</span> run
                  {selectedRunIds.size > 1 ? "s" : ""} selected
                </span>
              </div>
            </div>
            <div className="flex items-center gap-2.5 relative z-10">
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setSelectedRunIds(new Set())}
                disabled={isBatchDeleting}
                className="h-7 px-2.5 text-[11px] font-mono rounded-[5px] text-zinc-300 hover:text-white bg-gradient-to-b from-zinc-800/90 via-zinc-850 to-zinc-900/90 border-t border-t-zinc-600/60 border-x border-x-zinc-700/60 border-b border-b-zinc-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_1px_2px_rgba(0,0,0,0.3)] hover:border-t-zinc-500 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.15),0_1px_3px_rgba(0,0,0,0.4)] active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.4)] transition-all"
              >
                Clear selection
              </Button>
              <Button
                variant="destructive"
                size="sm"
                onClick={handleBatchDelete}
                disabled={isBatchDeleting}
                className="flex items-center gap-1.5 h-7 px-3 text-[11px] font-mono font-medium rounded-[5px] bg-gradient-to-b from-red-600 via-red-650 to-red-800 hover:from-red-500 hover:via-red-600 hover:to-red-750 text-white border-t border-t-red-400/60 border-x border-x-red-600/70 border-b border-b-red-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.25),0_2px_6px_rgba(220,38,38,0.35),0_1px_2px_rgba(0,0,0,0.4)] active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.6)] transition-all disabled:opacity-50"
              >
                <Trash2 className="h-3.5 w-3.5" />
                {isBatchDeleting ? "Deleting..." : `Delete Selected (${selectedRunIds.size})`}
              </Button>
            </div>
          </div>
        )}

        {/* High-Precision CI Runs Table */}
        <div className="relative z-0 overflow-hidden rounded-xl border-t border-t-zinc-600/60 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-gradient-to-b from-[#111115]/95 via-[#0d0d10]/95 to-[#09090c]/95 backdrop-blur-sm shadow-[inset_0_1px_0_rgba(255,255,255,0.12),inset_0_-1px_0_rgba(0,0,0,0.4),0_8px_32px_rgba(0,0,0,0.5),0_2px_4px_rgba(0,0,0,0.3)]">
          {/* Subtle light-from-above ambient gradient sheen */}
          <span
            aria-hidden="true"
            className="pointer-events-none absolute inset-x-0 top-0 h-6 bg-gradient-to-b from-white/[0.04] to-transparent z-10"
          />

          {loading ? (
            <div className="p-4 space-y-3">
              <Skeleton className="h-10 w-full bg-zinc-900/60" />
              <Skeleton className="h-10 w-full bg-zinc-900/60" />
              <Skeleton className="h-10 w-full bg-zinc-900/60" />
              <Skeleton className="h-10 w-full bg-zinc-900/60" />
            </div>
          ) : filteredRuns.length === 0 ? (
            <div className="p-12 text-center border border-dashed border-zinc-800/80 m-4 rounded-[6px]">
              <Layers className="h-8 w-8 mx-auto text-zinc-600 mb-2.5" />
              <h3 className="text-sm font-medium text-zinc-300 font-mono">
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
                  className="mt-4 text-xs font-mono rounded-[5px]"
                >
                  <RotateCcw className="h-3.5 w-3.5 mr-1.5" />
                  Reset Filters
                </Button>
              )}
            </div>
          ) : (
            <div className="overflow-x-auto">
              <Table>
                <TableHeader className="border-b border-zinc-800/90 bg-gradient-to-b from-[#141418] to-[#0c0c10] shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_1px_3px_rgba(0,0,0,0.35)]">
                  <TableRow className="border-b border-zinc-800/90 hover:bg-transparent">
                    {/* Column 0: CHECKBOX (Standard UX Left Position) */}
                    <TableHead className="w-10 pl-4 py-3 text-center">
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

                    {/* Column 1: STATUS */}
                    <TableHead className="w-[140px] px-3 text-[11px] font-mono tracking-wider uppercase text-zinc-500 font-medium select-none py-3">
                      Status
                    </TableHead>

                    {/* Column 2: REPOSITORY */}
                    <TableHead className="w-[200px] px-3 text-[11px] font-mono tracking-wider uppercase text-zinc-500 font-medium select-none py-3">
                      Repository
                    </TableHead>

                    {/* Column 3: BRANCH / COMMIT */}
                    <TableHead className="px-3 text-[11px] font-mono tracking-wider uppercase text-zinc-500 font-medium select-none py-3">
                      Branch / Commit
                    </TableHead>

                    {/* Column 4: TRIGGERED */}
                    <TableHead className="w-[110px] px-3 text-[11px] font-mono tracking-wider uppercase text-zinc-500 font-medium select-none py-3">
                      Triggered
                    </TableHead>

                    {/* Column 5: DURATION */}
                    <TableHead className="w-[110px] px-3 text-[11px] font-mono tracking-wider uppercase text-zinc-500 font-medium select-none py-3">
                      Duration
                    </TableHead>

                    {/* Column 6: COST */}
                    <TableHead className="w-[110px] px-3 text-[11px] font-mono tracking-wider uppercase text-zinc-500 font-medium select-none py-3">
                      Compute
                    </TableHead>

                    {/* Column 7: ACTIONS */}
                    <TableHead className="w-[90px] text-right pr-4 text-[11px] font-mono tracking-wider uppercase text-zinc-500 font-medium select-none py-3">
                      Actions
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody className="divide-y divide-zinc-800/40">
                  {filteredRuns.map((run) => {
                    const repo = reposMap[run.repo_id];
                    const repoLabel = repo ? `${repo.owner}/${repo.name}` : `repo-${run.repo_id.slice(0, 8)}`;
                    const [repoOwner, repoName] = repoLabel.includes("/") ? repoLabel.split("/") : ["", repoLabel];
                    const isSelected = selectedRunIds.has(run.id);
                    const branchName = run.head_branch || "unknown";
                    const commitSha = run.head_sha.slice(0, 7);
                    const isFixBranch =
                      branchName.startsWith("haunter/") ||
                      branchName.startsWith("fix-") ||
                      branchName.startsWith("fix/");

                    return (
                      <TableRow
                        key={run.id}
                        onClick={() => router.push(`/runs/detail?id=${run.id}`)}
                        className={`cursor-pointer group transition-all duration-150 border-b border-zinc-800/40 hover:bg-gradient-to-r hover:from-zinc-800/50 hover:via-zinc-800/30 hover:to-zinc-800/10 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.05),inset_0_-1px_0_rgba(0,0,0,0.2)] active:translate-y-[0.5px] ${
                          isSelected
                            ? "bg-amber-500/[0.05] border-l-[3px] border-l-amber-400 shadow-[inset_0_0_16px_rgba(245,158,11,0.04)] hover:bg-amber-500/[0.08]"
                            : "border-l-[3px] border-l-transparent"
                        } ${isBatchDeleting ? "opacity-50 pointer-events-none" : ""}`}
                      >
                        {/* Column 0: CHECKBOX */}
                        <TableCell className="w-10 pl-4 py-3 text-center align-middle" onClick={(e) => e.stopPropagation()}>
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

                        {/* Column 1: STATUS */}
                        <TableCell className="px-3 py-3 align-middle whitespace-nowrap">
                          <StatusBadge status={run.status} />
                        </TableCell>

                        {/* Column 2: REPOSITORY */}
                        <TableCell className="px-3 py-3 align-middle whitespace-nowrap">
                          <div className="flex items-center gap-2 max-w-[210px] min-w-0" title={repoLabel}>
                            <FolderGit2 className="h-3.5 w-3.5 text-zinc-500 shrink-0 group-hover:text-amber-400/80 transition-colors" />
                            <span className="font-mono text-xs truncate">
                              <span className="text-zinc-500">{repoOwner ? `${repoOwner}/` : ""}</span>
                              <span className="font-semibold text-zinc-200 group-hover:text-zinc-100 transition-colors">
                                {repoName}
                              </span>
                            </span>
                          </div>
                        </TableCell>

                        {/* Column 3: BRANCH / COMMIT */}
                        <TableCell className="px-3 py-3 align-middle">
                          <div className="flex items-center gap-2 font-mono text-xs text-zinc-400 min-w-0">
                            {isFixBranch ? (
                              <span
                                className="inline-flex items-center gap-1 px-2 py-0.5 rounded-[5px] bg-gradient-to-b from-amber-500/15 via-amber-500/10 to-amber-500/5 border-t border-t-amber-400/40 border-x border-x-amber-500/30 border-b border-b-amber-600/20 text-amber-300 text-[11px] font-mono shadow-[inset_0_1px_0_rgba(255,255,255,0.15),0_1px_2px_rgba(0,0,0,0.3)] truncate max-w-[170px]"
                                title={branchName}
                              >
                                <Sparkles className="h-3 w-3 text-amber-400 shrink-0" />
                                <span className="truncate">{branchName}</span>
                              </span>
                            ) : (
                              <span
                                className="inline-flex items-center gap-1.5 truncate max-w-[160px] text-zinc-300"
                                title={branchName}
                              >
                                <GitBranch className="h-3.5 w-3.5 text-zinc-500 shrink-0" />
                                <span className="truncate">{branchName}</span>
                              </span>
                            )}
                            <span className="text-zinc-600 shrink-0 select-none">•</span>
                            <button
                              type="button"
                              onClick={(e) => handleCopySha(e, run.head_sha)}
                              title="Click to copy full commit SHA"
                              className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-[5px] bg-gradient-to-b from-zinc-800/90 via-zinc-850 to-zinc-900 border-t border-t-zinc-600/60 border-x border-x-zinc-700/60 border-b border-b-zinc-850 text-[11px] text-zinc-400 hover:text-zinc-200 font-mono shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_1px_2px_rgba(0,0,0,0.3)] hover:border-zinc-500 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.15),0_1px_3px_rgba(0,0,0,0.4)] active:translate-y-[0.5px] transition-all shrink-0 group/commit"
                            >
                              <GitCommit className="h-3 w-3 text-zinc-500 group-hover/commit:text-amber-400 shrink-0" />
                              <span>{commitSha}</span>
                              {copiedSha === run.head_sha ? (
                                <Check className="h-2.5 w-2.5 text-emerald-400 ml-0.5" />
                              ) : null}
                            </button>
                          </div>
                        </TableCell>

                        {/* Column 4: TRIGGERED */}
                        <TableCell
                          className="px-3 py-3 text-zinc-400 font-mono text-xs tabular-nums align-middle whitespace-nowrap"
                          title={new Date(run.created_at).toLocaleString()}
                        >
                          {formatRelativeTime(run.created_at)}
                        </TableCell>

                        {/* Column 5: DURATION */}
                        <TableCell className="px-3 py-3 align-middle whitespace-nowrap">
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
                        <TableCell className="px-3 py-3 align-middle whitespace-nowrap">
                          <div className="flex flex-col font-mono leading-tight">
                            {run.cost && run.cost > 0 ? (
                              <span className="text-xs font-mono font-medium text-emerald-400 tabular-nums">
                                ${run.cost.toFixed(4)}
                              </span>
                            ) : (
                              <span className="text-xs font-mono font-medium text-zinc-500 tabular-nums">-</span>
                            )}
                            {run.tokens && run.tokens > 0 ? (
                              <span className="text-[10px] font-mono text-zinc-500 tabular-nums mt-0.5">
                                {((run.tokens || 0) / 1000).toFixed(1)}k tok
                              </span>
                            ) : null}
                          </div>
                        </TableCell>

                        {/* Column 7: ACTIONS */}
                        <TableCell className="text-right pr-4 py-3 align-middle whitespace-nowrap" onClick={(e) => e.stopPropagation()}>
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={(e) => {
                              e.stopPropagation();
                              router.push(`/runs/detail?id=${run.id}`);
                            }}
                            className="group/btn h-7 px-2.5 text-xs font-mono rounded-[5px] text-zinc-300 hover:text-white bg-gradient-to-b from-zinc-800/90 via-zinc-800/80 to-zinc-900/90 border-t border-t-zinc-600/70 border-x border-x-zinc-700/60 border-b border-b-zinc-900 shadow-[inset_0_1px_0_rgba(255,255,255,0.1),0_1px_3px_rgba(0,0,0,0.35)] hover:border-t-zinc-500 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.2),0_2px_6px_rgba(0,0,0,0.4)] active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.4)] transition-all inline-flex items-center gap-1.5"
                          >
                            <span>Trace</span>
                            <ArrowUpRight className="h-3.5 w-3.5 text-zinc-500 group-hover/btn:text-amber-400 group-hover/btn:translate-x-0.5 group-hover/btn:-translate-y-0.5 transition-all" />
                          </Button>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
          )}

          {/* Pagination Footer */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between border-t border-zinc-800/80 bg-gradient-to-b from-[#0e0e12] to-[#09090c] px-5 py-3 text-xs text-zinc-400 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]">
              <span className="font-mono text-[11px] text-zinc-500 tabular-nums">
                Showing {page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, total)} of {total} runs
              </span>
              <div className="flex items-center gap-2.5">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setPage((p) => Math.max(0, p - 1))}
                  disabled={page === 0 || loading}
                  className="h-7 px-2.5 text-[11px] font-mono rounded-[5px] bg-gradient-to-b from-zinc-800/80 to-zinc-900/80 border-t border-t-zinc-600/60 border-x border-x-zinc-700/60 border-b border-b-zinc-800/90 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_1px_2px_rgba(0,0,0,0.3)] active:translate-y-[0.5px] text-zinc-300 hover:text-zinc-100 disabled:opacity-40"
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
                  className="h-7 px-2.5 text-[11px] font-mono rounded-[5px] bg-gradient-to-b from-zinc-800/80 to-zinc-900/80 border-t border-t-zinc-600/60 border-x border-x-zinc-700/60 border-b border-b-zinc-800/90 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_1px_2px_rgba(0,0,0,0.3)] active:translate-y-[0.5px] text-zinc-300 hover:text-zinc-100 disabled:opacity-40"
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
