"use client";

import { useEffect, useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import { AppLayout } from "@/components/layout/app-layout";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { RunsFilter } from "@/components/runs/runs-filter";
import { StatusBadge } from "@/components/runs/status-badge";
import { api, RepoOut, RunOut } from "@/lib/api";
import { formatRelativeTime } from "@/lib/utils";
import { Activity, ChevronLeft, ChevronRight, GitCommit, GitBranch, ArrowUpRight } from "lucide-react";

const PAGE_SIZE = 20;

export default function RunsPage() {
  const router = useRouter();

  const [runs, setRuns] = useState<RunOut[]>([]);
  const [total, setTotal] = useState(0);
  const [repos, setRepos] = useState<RepoOut[]>([]);
  const [reposMap, setReposMap] = useState<Record<string, RepoOut>>({});

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

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

  const handleResetFilters = () => {
    setSelectedRepoId("");
    setSelectedStatus("");
    setFrom("");
    setTo("");
    setPage(0);
  };

  const totalPages = Math.ceil(total / PAGE_SIZE);

  return (
    <AppLayout
      title="CI Runs & Diagnoses"
      subtitle="Autonomous pipeline runs, sandbox verifications, and PR traces"
    >
      <div className="space-y-4">
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
          <div className="rounded-[6px] border border-red-900/60 bg-red-950/30 p-3 text-xs text-red-300">
            {error}
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
                  <TableHead className="w-[18%]">Status</TableHead>
                  <TableHead className="w-[28%]">Repository</TableHead>
                  <TableHead className="w-[24%]">Branch / Commit</TableHead>
                  <TableHead className="w-[15%]">Triggered</TableHead>
                  <TableHead className="w-[15%] text-right">Action</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {runs.map((run) => {
                  const repo = reposMap[run.repo_id];
                  const repoLabel = repo ? `${repo.owner}/${repo.name}` : `repo-${run.repo_id.slice(0, 8)}`;

                  return (
                    <TableRow
                      key={run.id}
                      onClick={() => router.push(`/runs/${run.id}`)}
                      className="cursor-pointer group"
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

                      {/* Drilldown Link */}
                      <TableCell className="text-right">
                        <span className="inline-flex items-center gap-1 font-mono text-xs text-zinc-500 group-hover:text-zinc-200 transition-colors">
                          Trace
                          <ArrowUpRight className="h-3 w-3" />
                        </span>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          )}

          {/* Pagination Footer */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between border-t border-zinc-800 bg-[#0c0c0e] px-4 py-2.5 text-xs text-zinc-400">
              <span className="font-mono text-[11px]">
                Showing {page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, total)} of {total} runs
              </span>
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setPage((p) => Math.max(0, p - 1))}
                  disabled={page === 0 || loading}
                  className="h-7 px-2"
                >
                  <ChevronLeft className="h-3.5 w-3.5" />
                </Button>
                <span className="font-mono text-[11px] px-1 text-zinc-300">
                  {page + 1} / {totalPages}
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
                  disabled={page >= totalPages - 1 || loading}
                  className="h-7 px-2"
                >
                  <ChevronRight className="h-3.5 w-3.5" />
                </Button>
              </div>
            </div>
          )}
        </div>
      </div>
    </AppLayout>
  );
}
