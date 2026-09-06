"use client";

import { useEffect, useState, useCallback, useRef, useMemo } from "react";
import { useRouter } from "next/navigation";
import { AppLayout } from "@/components/layout/app-layout";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusBadge } from "@/components/runs/status-badge";
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
      const branchName = run.head_branch || (run as any).branch || "";
      const commitSha = (run as any).commit_sha || run.head_sha || "";
      const diagnosis = (run as any).diagnosis || (run as any).diagnosis_summary || "";

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
            className="h-10 px-4 text-xs font-medium flex items-center gap-2"
          >
            <Trash2 className="h-4 w-4" />
            {isBatchDeleting ? "Deleting..." : `Delete Selected (${selectedRunIds.size})`}
          </Button>
        ) : undefined
      }
    >
      <div className="space-y-6">
        {/* Modern Filter Bar ported from mock/page.tsx */}
        <div className="flex flex-wrap items-center justify-between gap-4 rounded-[7px] border border-zinc-800 bg-[#121215] p-3.5">
          <div className="flex flex-wrap items-center gap-3.5 flex-1 min-w-[300px]">
            <div className="flex items-center gap-2 text-[13px] text-zinc-400 font-medium pl-1">
              <Filter className="h-4 w-4 text-amber-400" />
              <span>Filters:</span>
            </div>

            {/* Search input */}
            <div className="relative w-64">
              <Search className="absolute left-3 top-3 h-4 w-4 text-zinc-500" />
              <Input
                type="text"
                placeholder="Search branch, sha, diagnosis..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="h-10 pl-9.5 pr-3.5 text-xs rounded-[7px]"
              />
            </div>

            {/* Repo select */}
            <select
              value={selectedRepoId}
              onChange={(e) => {
                setSelectedRepoId(e.target.value);
                setPage(0);
              }}
              className="h-10 rounded-[7px] border border-zinc-800 bg-[#0c0c0e] px-4 text-xs text-zinc-200 focus:border-amber-400 focus:outline-none"
            >
              <option value="">All Repositories ({repos.length})</option>
              {repos.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.owner}/{r.name}
                </option>
              ))}
            </select>

            {/* Status select */}
            <select
              value={selectedStatus}
              onChange={(e) => {
                setSelectedStatus(e.target.value);
                setPage(0);
              }}
              className="h-10 rounded-[7px] border border-zinc-800 bg-[#0c0c0e] px-4 text-xs text-zinc-200 focus:border-amber-400 focus:outline-none"
            >
              <option value="">All Statuses</option>
              <option value="completed">Completed / PR Opened</option>
              <option value="fix_generation">Generating Fix</option>
              <option value="error">Error / Failed</option>
              <option value="fallback">Fallback Comment</option>
            </select>
          </div>

          <div className="flex items-center gap-3.5">
            <span className="font-mono text-xs text-zinc-500">
              Showing {filteredRuns.length} of {total} runs
            </span>

            {hasActiveFilters && (
              <Button
                variant="ghost"
                size="sm"
                onClick={handleResetFilters}
                className="h-8 px-2.5 text-xs rounded-[7px] text-zinc-400 hover:text-zinc-100 flex items-center gap-1.5"
              >
                <RotateCcw className="h-3.5 w-3.5" />
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
          <div className="flex items-center justify-between rounded-[7px] border border-red-900/60 bg-red-950/20 px-5 py-3 text-sm">
            <span className="font-mono text-zinc-300">
              <span className="font-semibold text-zinc-100">{selectedRunIds.size}</span> run
              {selectedRunIds.size > 1 ? "s" : ""} selected
            </span>
            <div className="flex items-center gap-3">
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setSelectedRunIds(new Set())}
                disabled={isBatchDeleting}
                className="h-9 px-3.5 text-xs text-zinc-400 hover:text-zinc-200"
              >
                Clear selection
              </Button>
              <Button
                variant="destructive"
                size="sm"
                onClick={handleBatchDelete}
                disabled={isBatchDeleting}
                className="flex items-center gap-2 h-9 px-3.5 text-xs font-medium"
              >
                <Trash2 className="h-4 w-4" />
                {isBatchDeleting ? "Deleting..." : `Delete Selected (${selectedRunIds.size})`}
              </Button>
            </div>
          </div>
        )}

        {/* Dense Table */}
        <div className="rounded-[6px] border border-zinc-800 bg-[#121215] overflow-hidden shadow-sm">
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
              <TableHeader>
                <TableRow>
                  {/* Column 1: STATUS */}
                  <TableHead className="w-[13%] pl-4">Status</TableHead>

                  {/* Column 2: REPOSITORY */}
                  <TableHead className="w-[22%]">Repository</TableHead>

                  {/* Column 3: BRANCH / COMMIT */}
                  <TableHead className="w-[21%]">Branch / Commit</TableHead>

                  {/* Column 4: TRIGGERED */}
                  <TableHead className="w-[12%]">Triggered</TableHead>

                  {/* Column 5: DURATION */}
                  <TableHead className="w-[10%]">Duration</TableHead>

                  {/* Column 6: COST */}
                  <TableHead className="w-[10%]">Cost</TableHead>

                  {/* Column 7: ACTIONS */}
                  <TableHead className="w-[9%] text-right pr-4">Actions</TableHead>

                  {/* Column 8: [CHECKBOX] */}
                  <TableHead className="w-10 px-3 text-center">
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
                      className="h-4 w-4 rounded bg-zinc-900 border-zinc-700 text-amber-400 accent-amber-400 focus:ring-0 cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
                    />
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {filteredRuns.map((run) => {
                  const repo = reposMap[run.repo_id];
                  const repoLabel = repo ? `${repo.owner}/${repo.name}` : `repo-${run.repo_id.slice(0, 8)}`;
                  const isSelected = selectedRunIds.has(run.id);
                  const branchName = run.head_branch || (run as any).branch || "unknown";
                  const commitSha = ((run as any).commit_sha || run.head_sha || "").slice(0, 7);

                  return (
                    <TableRow
                      key={run.id}
                      onClick={() => router.push(`/runs/detail?id=${run.id}`)}
                      className={`cursor-pointer group ${isSelected ? "bg-zinc-800/30" : ""} ${
                        isBatchDeleting ? "opacity-50 pointer-events-none" : ""
                      }`}
                    >
                      {/* Column 1: STATUS */}
                      <TableCell className="pl-4">
                        <StatusBadge status={run.status} />
                      </TableCell>

                      {/* Column 2: REPOSITORY */}
                      <TableCell>
                        <span className="font-mono text-xs font-semibold text-zinc-200 group-hover:text-amber-400 transition-colors">
                          {repoLabel}
                        </span>
                      </TableCell>

                      {/* Column 3: BRANCH / COMMIT */}
                      <TableCell>
                        <div className="flex items-center gap-2 font-mono text-xs text-zinc-400">
                          <span className="inline-flex items-center gap-1 truncate max-w-[130px]" title={branchName}>
                            <GitBranch className="h-3.5 w-3.5 text-zinc-500 shrink-0" />
                            <span className="truncate">{branchName}</span>
                          </span>
                          <span className="text-zinc-600 shrink-0">•</span>
                          <span className="inline-flex items-center gap-1 text-zinc-400 shrink-0">
                            <GitCommit className="h-3.5 w-3.5 text-zinc-500" />
                            {commitSha}
                          </span>
                        </div>
                      </TableCell>

                      {/* Column 4: TRIGGERED */}
                      <TableCell className="text-zinc-400 font-mono text-xs">
                        {formatRelativeTime(run.created_at)}
                      </TableCell>

                      {/* Column 5: DURATION */}
                      <TableCell>
                        <span className="inline-flex items-center gap-1.5 font-mono text-xs text-zinc-300">
                          <Clock className="h-3.5 w-3.5 text-zinc-500 shrink-0" />
                          {formatDuration(run.created_at, run.updated_at)}
                        </span>
                      </TableCell>

                      {/* Column 6: COST */}
                      <TableCell>
                        <div className="flex flex-col font-mono text-xs">
                          {run.cost && run.cost > 0 ? (
                            <span className="text-emerald-400 font-medium">
                              ${run.cost.toFixed(4)}
                            </span>
                          ) : (
                            <span className="text-zinc-500 font-medium">-</span>
                          )}
                          {run.tokens && run.tokens > 0 ? (
                            <span className="text-[11px] text-zinc-500">
                              {((run.tokens || 0) / 1000).toFixed(1)}k tok
                            </span>
                          ) : null}
                        </div>
                      </TableCell>

                      {/* Column 7: ACTIONS */}
                      <TableCell className="text-right pr-4" onClick={(e) => e.stopPropagation()}>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={(e) => {
                            e.stopPropagation();
                            router.push(`/runs/detail?id=${run.id}`);
                          }}
                          className="h-7 px-2 text-xs font-mono text-zinc-400 hover:text-amber-400 hover:bg-zinc-800/80"
                        >
                          Trace
                          <ArrowUpRight className="h-3 w-3 ml-1" />
                        </Button>
                      </TableCell>

                      {/* Column 8: [CHECKBOX] */}
                      <TableCell className="w-10 px-3 text-center" onClick={(e) => e.stopPropagation()}>
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
                          className="h-4 w-4 rounded bg-zinc-900 border-zinc-700 text-amber-400 accent-amber-400 focus:ring-0 cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
                        />
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          )}

          {/* Pagination Footer */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between border-t border-zinc-800 bg-[#0c0c0e] px-6 py-3.5 text-sm text-zinc-400">
              <span className="font-mono text-xs text-zinc-400">
                Showing {page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, total)} of {total} runs
              </span>
              <div className="flex items-center gap-3">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setPage((p) => Math.max(0, p - 1))}
                  disabled={page === 0 || loading}
                  className="h-9 px-3 text-xs"
                >
                  <ChevronLeft className="h-4 w-4 mr-1" />
                  Prev
                </Button>
                <span className="font-mono text-xs px-2 text-zinc-300">
                  {page + 1} / {totalPages}
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
                  disabled={page >= totalPages - 1 || loading}
                  className="h-9 px-3 text-xs"
                >
                  Next
                  <ChevronRight className="h-4 w-4 ml-1" />
                </Button>
              </div>
            </div>
          )}
        </div>
      </div>
    </AppLayout>
  );
}
