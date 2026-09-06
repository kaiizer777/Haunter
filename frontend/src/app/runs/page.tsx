"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import { useRouter } from "next/navigation";
import { AppLayout } from "@/components/layout/app-layout";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { RunsFilter } from "@/components/runs/runs-filter";
import { StatusBadge } from "@/components/runs/status-badge";
import { api, RepoOut, RunOut } from "@/lib/api";
import { formatRelativeTime } from "@/lib/utils";
import { Activity, ChevronLeft, ChevronRight, GitCommit, GitBranch, ArrowUpRight, Trash2 } from "lucide-react";

const PAGE_SIZE = 20;

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
  const [deletingIds, setDeletingIds] = useState<Set<string>>(new Set());
  const [isBatchDeleting, setIsBatchDeleting] = useState(false);

  const headerCheckboxRef = useRef<HTMLInputElement | null>(null);

  // Filters
  const [selectedRepoId, setSelectedRepoId] = useState("");
  const [selectedStatus, setSelectedStatus] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
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
        from: from ? new Date(from).toISOString() : undefined,
        to: to ? new Date(to).toISOString() : undefined,
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
  }, [selectedRepoId, selectedStatus, from, to, page]);

  useEffect(() => {
    fetchRuns();
  }, [fetchRuns]);

  // Selection helpers
  const displayedRunIds = runs.map((r) => r.id);
  const selectedDisplayedCount = runs.filter((r) => selectedRunIds.has(r.id)).length;
  const isAllSelected = runs.length > 0 && selectedDisplayedCount === runs.length;
  const isIndeterminate = selectedDisplayedCount > 0 && selectedDisplayedCount < runs.length;

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

  const handleDeleteSingleRun = async (runId: string) => {
    if (!window.confirm("Are you sure you want to delete this run?")) {
      return;
    }

    setDeletingIds((prev) => new Set(prev).add(runId));
    setError(null);
    try {
      await api.deleteRun(runId);
      setSelectedRunIds((prev) => {
        if (!prev.has(runId)) return prev;
        const next = new Set(prev);
        next.delete(runId);
        return next;
      });
      if (runs.length === 1 && page > 0) {
        setPage((p) => Math.max(0, p - 1));
      } else {
        await fetchRuns();
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Failed to delete run.";
      setError(msg);
      alert(`Failed to delete run: ${msg}`);
    } finally {
      setDeletingIds((prev) => {
        const next = new Set(prev);
        next.delete(runId);
        return next;
      });
    }
  };

  const handleBatchDelete = async () => {
    if (selectedRunIds.size === 0) return;

    const count = selectedRunIds.size;
    if (
      !window.confirm(
        `Are you sure you want to delete ${count} selected run${count > 1 ? "s" : ""}? This action cannot be undone.`
      )
    ) {
      return;
    }

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
      alert(`Failed to delete selected runs: ${msg}`);
    } finally {
      setIsBatchDeleting(false);
    }
  };

  const handleResetFilters = () => {
    setSelectedRepoId("");
    setSelectedStatus("");
    setFrom("");
    setTo("");
    setPage(0);
    setSelectedRunIds(new Set());
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
            className="h-9 px-3.5 text-[13px] flex items-center gap-2"
          >
            <Trash2 className="h-4 w-4" />
            {isBatchDeleting ? "Deleting..." : `Delete Selected (${selectedRunIds.size})`}
          </Button>
        ) : undefined
      }
    >
      <div className="space-y-5">
        {/* Server-side Filters */}
        <RunsFilter
          repos={repos}
          selectedRepoId={selectedRepoId}
          selectedStatus={selectedStatus}
          from={from}
          to={to}
          onRepoChange={(id) => {
            setSelectedRepoId(id);
            setPage(0);
          }}
          onStatusChange={(st) => {
            setSelectedStatus(st);
            setPage(0);
          }}
          onFromChange={(f) => {
            setFrom(f);
            setPage(0);
          }}
          onToChange={(t) => {
            setTo(t);
            setPage(0);
          }}
          onReset={handleResetFilters}
        />

        {error && (
          <div className="rounded-[7px] border border-red-900/60 bg-red-950/30 p-3.5 text-[13px] text-red-300">
            {error}
          </div>
        )}

        {/* Selection Toolbar */}
        {selectedRunIds.size > 0 && (
          <div className="flex items-center justify-between rounded-[7px] border border-red-900/60 bg-red-950/20 px-4 py-2.5 text-[13px]">
            <span className="font-mono text-zinc-300">
              <span className="font-semibold text-zinc-100">{selectedRunIds.size}</span> run{selectedRunIds.size > 1 ? "s" : ""} selected
            </span>
            <div className="flex items-center gap-2.5">
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setSelectedRunIds(new Set())}
                disabled={isBatchDeleting}
                className="h-8 px-3 text-[13px] text-zinc-400 hover:text-zinc-200"
              >
                Clear selection
              </Button>
              <Button
                variant="destructive"
                size="sm"
                onClick={handleBatchDelete}
                disabled={isBatchDeleting}
                className="flex items-center gap-2 h-8 px-3 text-[13px]"
              >
                <Trash2 className="h-4 w-4" />
                {isBatchDeleting ? "Deleting..." : `Delete Selected (${selectedRunIds.size})`}
              </Button>
            </div>
          </div>
        )}

        {/* Dense Table */}
        <div className="rounded-[6px] border border-zinc-800 bg-[#121215] overflow-hidden">
          {loading ? (
            <div className="p-4 space-y-3">
              <Skeleton className="h-9 w-full" />
              <Skeleton className="h-9 w-full" />
              <Skeleton className="h-9 w-full" />
              <Skeleton className="h-9 w-full" />
            </div>
          ) : runs.length === 0 ? (
            <div className="p-12 text-center border border-dashed border-zinc-800/80 m-4 rounded-[6px]">
              <Activity className="h-6 w-6 mx-auto text-zinc-600 mb-2" />
              <h3 className="text-sm font-medium text-zinc-300">No CI runs found</h3>
              <p className="text-xs text-zinc-500 mt-1 max-w-sm mx-auto">
                Trigger a failing GitHub Actions workflow on a connected repo to see autonomous diagnosis.
              </p>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-[16%]">Status</TableHead>
                  <TableHead className="w-[26%]">Repository</TableHead>
                  <TableHead className="w-[22%]">Branch / Commit</TableHead>
                  <TableHead className="w-[16%]">Triggered</TableHead>
                  <TableHead className="w-[16%] text-right">Action</TableHead>
                  {/* Selection Checkbox */}
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
                      disabled={loading || runs.length === 0 || isBatchDeleting}
                      aria-label="Select all runs"
                      className="h-4 w-4 rounded bg-zinc-900 border-zinc-700 text-amber-400 accent-amber-400 focus:ring-0 cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
                    />
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {runs.map((run) => {
                  const repo = reposMap[run.repo_id];
                  const repoLabel = repo ? `${repo.owner}/${repo.name}` : `repo-${run.repo_id.slice(0, 8)}`;
                  const isSelected = selectedRunIds.has(run.id);
                  const isRowDeleting = deletingIds.has(run.id);

                  return (
                    <TableRow
                      key={run.id}
                      onClick={() => router.push(`/runs/detail?id=${run.id}`)}
                      className={`cursor-pointer group ${isSelected ? "bg-zinc-800/30" : ""} ${
                        isRowDeleting || isBatchDeleting ? "opacity-50 pointer-events-none" : ""
                      }`}
                    >
                      {/* Status */}
                      <TableCell>
                        <StatusBadge status={run.status} />
                      </TableCell>

                      {/* Repository */}
                      <TableCell>
                        <span className="font-mono text-xs font-semibold text-zinc-200 group-hover:text-amber-400 transition-colors">
                          {repoLabel}
                        </span>
                      </TableCell>

                      {/* Branch & Commit SHA */}
                      <TableCell>
                        <div className="flex items-center gap-2 font-mono text-[11px] text-zinc-400">
                          <span className="inline-flex items-center gap-1">
                            <GitBranch className="h-3 w-3 text-zinc-500" />
                            {run.head_branch}
                          </span>
                          <span className="text-zinc-600">•</span>
                          <span className="inline-flex items-center gap-1 text-zinc-400">
                            <GitCommit className="h-3 w-3 text-zinc-500" />
                            {run.head_sha.slice(0, 7)}
                          </span>
                        </div>
                      </TableCell>

                      {/* Time ago */}
                      <TableCell className="text-zinc-400 font-mono text-xs">
                        {formatRelativeTime(run.created_at)}
                      </TableCell>

                      {/* Action */}
                      <TableCell className="text-right">
                        <div className="inline-flex items-center justify-end gap-1.5">
                          <span className="inline-flex items-center gap-1 font-mono text-xs text-zinc-500 group-hover:text-zinc-200 transition-colors mr-1">
                            Trace
                            <ArrowUpRight className="h-3 w-3" />
                          </span>
                          <Button
                            variant="ghost"
                            size="icon"
                            onClick={(e) => {
                              e.stopPropagation();
                              handleDeleteSingleRun(run.id);
                            }}
                            disabled={isRowDeleting || isBatchDeleting}
                            className="h-7 w-7 text-zinc-500 hover:text-red-400 hover:bg-red-950/30"
                            title="Delete run"
                            aria-label={`Delete run ${run.id}`}
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </Button>
                        </div>
                      </TableCell>

                      {/* Selection Checkbox */}
                      <TableCell className="w-10 px-3 text-center" onClick={(e) => e.stopPropagation()}>
                        <input
                          type="checkbox"
                          checked={isSelected}
                          disabled={isRowDeleting || isBatchDeleting}
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
            <div className="flex items-center justify-between border-t border-zinc-800 bg-[#0c0c0e] px-5 py-3 text-[13px] text-zinc-400">
              <span className="font-mono text-xs">
                Showing {page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, total)} of {total} runs
              </span>
              <div className="flex items-center gap-2.5">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setPage((p) => Math.max(0, p - 1))}
                  disabled={page === 0 || loading}
                  className="h-8 px-2.5"
                >
                  <ChevronLeft className="h-4 w-4" />
                </Button>
                <span className="font-mono text-xs px-1.5 text-zinc-300">
                  {page + 1} / {totalPages}
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
                  disabled={page >= totalPages - 1 || loading}
                  className="h-8 px-2.5"
                >
                  <ChevronRight className="h-4 w-4" />
                </Button>
              </div>
            </div>
          )}
        </div>
      </div>
    </AppLayout>
  );
}
