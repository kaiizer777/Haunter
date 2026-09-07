"use client";

import { useEffect, useState, useCallback } from "react";
import { AppLayout } from "@/components/layout/app-layout";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusBadge } from "@/components/runs/status-badge";
import {
  api,
  UserEvalMetricsOut,
  RunOut,
  RepoOut,
  ApiError,
} from "@/lib/api";
import { formatRelativeTime } from "@/lib/utils";
import {
  Inbox,
  TrendingUp,
  Activity,
  Clock,
  DollarSign,
  AlertCircle,
  GitBranch,
  Sparkles,
  Target,
} from "lucide-react";

/**
 * Format a duration in seconds as "Xm Ys" (e.g. "1m 52s").
 * - If `totalSeconds` is null/undefined/zero/negative, returns "—".
 * - Seconds-only durations (e.g. 42s) render as "<sec>s".
 * - Minute-only durations (e.g. 2m 0s) render as "<min>m" (matches the
 *   project pattern in `runs/page.tsx:formatDuration`).
 */
function formatMinutesSeconds(totalSeconds: number | null | undefined): string {
  if (totalSeconds === null || totalSeconds === undefined || totalSeconds <= 0) {
    return "—";
  }
  const secs = Math.floor(totalSeconds);
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  if (m === 0) return `${s}s`;
  if (s === 0) return `${m}m`;
  return `${m}m ${s}s`;
}

/**
 * Format the duration between two ISO timestamps using the same rules as
 * `formatMinutesSeconds`. Returns "—" if either timestamp is missing or the
 * range is invalid.
 */
function formatRangeDuration(
  createdAt: string | null | undefined,
  updatedAt: string | null | undefined
): string {
  if (!createdAt || !updatedAt) return "—";
  const start = new Date(createdAt).getTime();
  const end = new Date(updatedAt).getTime();
  if (isNaN(start) || isNaN(end) || end < start) return "—";
  return formatMinutesSeconds((end - start) / 1000);
}

export default function EvalPage() {
  const [metrics, setMetrics] = useState<UserEvalMetricsOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Recent runs (small "Recent AI Healing Telemetry" table).
  // The /eval/user-metrics response does NOT include a per-run list, so we
  // call /runs?limit=5 in parallel. The RunOut shape exposes cost/tokens but
  // not per-run attempt counts or confidence — those live under /runs/{id}/trace.
  // We display what RunOut actually exposes and document the omission.
  const [recentRuns, setRecentRuns] = useState<RunOut[]>([]);
  const [reposMap, setReposMap] = useState<Record<string, RepoOut>>({});
  const [recentLoading, setRecentLoading] = useState(true);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    setError(null);
    setRecentLoading(true);
    try {
      const [m, runsRes, repos] = await Promise.all([
        api.getUserEvalMetrics(),
        api
          .getRuns({ limit: 5 })
          .catch(() => ({ runs: [] as RunOut[], total: 0 })),
        api.getRepos().catch(() => [] as RepoOut[]),
      ]);
      setMetrics(m);
      setRecentRuns(runsRes.runs || []);
      const map: Record<string, RepoOut> = {};
      repos.forEach((r) => {
        map[r.id] = r;
      });
      setReposMap(map);
    } catch (err: unknown) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else if (err instanceof Error) {
        setError(err.message);
      } else {
        setError("Failed to load AI reliability metrics.");
      }
      // Clear recent runs on the primary error — the user is shown a single
      // error state and we don't want a half-loaded table below it.
      setRecentRuns([]);
    } finally {
      setLoading(false);
      setRecentLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  return (
    <AppLayout
      title="AI Reliability & Evals"
      subtitle="Real-time diagnosis accuracy, sandbox verification rates, and cost telemetry across your repositories."
    >
      <div className="space-y-6 min-w-0">
        {/* Loading state — mirrors the populated layout to avoid layout shift. */}
        {loading && <DashboardSkeleton />}

        {/* Error state — small, non-blocking card. */}
        {!loading && error && (
          <div className="rounded-[6px] border border-red-900/60 bg-red-950/30 p-4 text-sm text-red-300">
            <div className="flex items-center gap-2">
              <AlertCircle className="h-4 w-4 text-red-400 shrink-0" />
              <span>{error}</span>
            </div>
          </div>
        )}

        {/* Empty state — no CI failures yet, only the centered card. */}
        {!loading && !error && metrics && metrics.total_runs === 0 && (
          <div className="rounded-[6px] border border-dashed border-zinc-800/80 bg-[#121215]/50 p-12 text-center">
            <Inbox className="h-8 w-8 mx-auto text-zinc-600 mb-3" />
            <h3 className="text-sm font-semibold text-zinc-200">
              No CI failures recorded yet
            </h3>
            <p className="text-xs text-zinc-500 mt-2 max-w-md mx-auto leading-relaxed">
              Connect a repository and trigger a failing workflow to see
              autonomous evaluation telemetry.
            </p>
          </div>
        )}

        {/* Populated state — header (AppLayout), 4 metric cards, calibration chart, recent table. */}
        {!loading && !error && metrics && metrics.total_runs > 0 && (
          <>
            {/* Top metric cards — exact order: success rate, total runs, avg time, total cost. */}
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
              {/* 1. Healing Success Rate */}
              <div className="rounded-[6px] border border-zinc-800 bg-[#121215] p-4 space-y-2">
                <div className="flex items-center justify-between text-[11px] font-mono text-zinc-400">
                  <span className="uppercase tracking-wider">
                    Healing Success Rate
                  </span>
                  <TrendingUp className="h-3.5 w-3.5 text-emerald-400" />
                </div>
                <div className="flex items-baseline gap-2">
                  <span className="text-2xl font-bold font-mono text-emerald-400 tracking-tight">
                    {metrics.success_rate_pct.toFixed(1)}%
                  </span>
                  <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-500/20 bg-emerald-500/10 px-2 py-0.5 text-[10px] font-mono font-medium text-emerald-400">
                    <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse" />
                    Live
                  </span>
                </div>
                <p className="text-[10px] font-mono text-zinc-500">
                  {metrics.healed_runs} of {metrics.total_runs} runs healed
                </p>
              </div>

              {/* 2. Total Pipeline Failures Handled */}
              <div className="rounded-[6px] border border-zinc-800 bg-[#121215] p-4 space-y-2">
                <div className="flex items-center justify-between text-[11px] font-mono text-zinc-400">
                  <span className="uppercase tracking-wider">
                    Total Pipeline Failures
                  </span>
                  <Activity className="h-3.5 w-3.5 text-zinc-400" />
                </div>
                <div className="flex items-baseline gap-2">
                  <span className="text-2xl font-bold font-mono text-zinc-100 tracking-tight">
                    {metrics.total_runs}
                  </span>
                  <span className="text-[10px] font-mono text-zinc-500">
                    runs
                  </span>
                </div>
                <p className="text-[10px] font-mono text-zinc-500">
                  Across all connected repositories
                </p>
              </div>

              {/* 3. Avg Resolution Time */}
              <div className="rounded-[6px] border border-zinc-800 bg-[#121215] p-4 space-y-2">
                <div className="flex items-center justify-between text-[11px] font-mono text-zinc-400">
                  <span className="uppercase tracking-wider">
                    Avg Resolution Time
                  </span>
                  <Clock className="h-3.5 w-3.5 text-zinc-400" />
                </div>
                <div className="flex items-baseline gap-2">
                  <span className="text-2xl font-bold font-mono text-zinc-100 tracking-tight">
                    {formatMinutesSeconds(metrics.avg_duration_seconds)}
                  </span>
                </div>
                <p className="text-[10px] font-mono text-zinc-500">
                  Wall-clock from webhook to terminal
                </p>
              </div>

              {/* 4. Total Healing Cost
                  The /eval/user-metrics response does NOT expose a token count
                  (only total_cost). The brief explicitly forbids fabricating a
                  number, so we render the dollar amount only with an
                  "across all attempts" footnote. */}
              <div className="rounded-[6px] border border-zinc-800 bg-[#121215] p-4 space-y-2">
                <div className="flex items-center justify-between text-[11px] font-mono text-zinc-400">
                  <span className="uppercase tracking-wider">
                    Total Healing Cost
                  </span>
                  <DollarSign className="h-3.5 w-3.5 text-zinc-400" />
                </div>
                <div className="flex items-baseline gap-2">
                  <span className="text-2xl font-bold font-mono text-zinc-100 tracking-tight">
                    ${metrics.total_cost.toFixed(4)}
                  </span>
                </div>
                <p className="text-[10px] font-mono text-zinc-500">
                  Across all attempts
                </p>
              </div>
            </div>

            {/* Confidence vs Sandbox Reality — pure CSS horizontal bars. */}
            <div className="rounded-[6px] border border-zinc-800 bg-[#121215] p-5 space-y-4">
              <div className="flex items-start justify-between gap-4">
                <div>
                  <h3 className="text-sm font-mono font-semibold uppercase tracking-wider text-zinc-200">
                    Confidence vs Sandbox Reality
                  </h3>
                  <p className="text-[11px] font-mono text-zinc-500 mt-0.5">
                    How often each confidence band actually passed in the
                    sandbox.
                  </p>
                </div>
                <Target className="h-4 w-4 text-zinc-500 shrink-0" />
              </div>

              <div className="space-y-3">
                {/* Rendered in the API order: 0-50%, 50-75%, 75-90%, 90-100%. */}
                {metrics.confidence_calibration.map((bucket) => {
                  const widthPct = Math.max(
                    0,
                    Math.min(100, bucket.accuracy_pct)
                  );
                  // Match the existing chart color thresholds in
                  // frontend/src/components/eval/confidence-chart.tsx:192-203.
                  const barColor =
                    widthPct >= 75
                      ? "bg-emerald-400"
                      : widthPct >= 50
                      ? "bg-amber-400"
                      : widthPct > 0
                      ? "bg-zinc-300"
                      : "bg-zinc-500";
                  const textColor =
                    widthPct >= 75
                      ? "text-emerald-400"
                      : widthPct >= 50
                      ? "text-amber-400"
                      : widthPct > 0
                      ? "text-zinc-300"
                      : "text-zinc-600";
                  return (
                    <div key={bucket.bucket} className="space-y-1.5">
                      <div className="flex items-center justify-between gap-3 text-[11px] font-mono">
                        <span className="text-zinc-300 w-20 shrink-0">
                          {bucket.bucket}
                        </span>
                        {bucket.total_attempts === 0 ? (
                          <span className="text-zinc-600 text-[10px]">
                            No attempts in this range
                          </span>
                        ) : (
                          <>
                            <span
                              className={`font-semibold ${textColor} shrink-0`}
                            >
                              {bucket.accuracy_pct.toFixed(1)}%
                            </span>
                            <span className="text-zinc-500 ml-auto shrink-0">
                              {bucket.passed_attempts} /{" "}
                              {bucket.total_attempts}
                            </span>
                          </>
                        )}
                      </div>
                      <div className="h-1.5 w-full rounded-full bg-zinc-900 overflow-hidden border border-zinc-800">
                        <div
                          className={`h-full ${barColor} transition-all`}
                          style={{
                            width: `${
                              bucket.total_attempts === 0 ? 0 : widthPct
                            }%`,
                          }}
                        />
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>

            {/* Recent AI Healing Telemetry — uses /runs?limit=5. */}
            <div className="rounded-[6px] border border-zinc-800 bg-[#121215] overflow-hidden">
              <div className="flex items-center justify-between gap-4 border-b border-zinc-800/80 p-4">
                <div>
                  <h3 className="text-sm font-mono font-semibold uppercase tracking-wider text-zinc-200">
                    Recent AI Healing Telemetry
                  </h3>
                  <p className="text-[11px] font-mono text-zinc-500 mt-0.5">
                    Latest pipeline runs handled by autonomous agents.
                  </p>
                </div>
                <Sparkles className="h-4 w-4 text-amber-400 shrink-0" />
              </div>
              {recentLoading ? (
                <div className="p-4 space-y-3">
                  <Skeleton className="h-9 w-full" />
                  <Skeleton className="h-9 w-full" />
                  <Skeleton className="h-9 w-full" />
                </div>
              ) : recentRuns.length === 0 ? (
                <div className="p-10 text-center">
                  <Activity className="h-5 w-5 mx-auto text-zinc-600 mb-2" />
                  <p className="text-xs text-zinc-500">No recent runs.</p>
                </div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="text-zinc-400 font-mono uppercase tracking-wider text-[10px]">
                        <th className="text-left p-3 font-medium">
                          Repository
                        </th>
                        <th className="text-left p-3 font-medium">Status</th>
                        <th className="text-left p-3 font-medium">
                          Duration
                        </th>
                        <th className="text-left p-3 font-medium">Cost</th>
                        <th className="text-left p-3 font-medium">
                          Created
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {recentRuns.map((run) => {
                        const repo = reposMap[run.repo_id];
                        const repoLabel = repo
                          ? `${repo.owner}/${repo.name}`
                          : `repo-${run.repo_id.slice(0, 8)}`;
                        return (
                          <tr
                            key={run.id}
                            className="border-t border-zinc-800/60 hover:bg-zinc-900/30"
                          >
                            <td className="p-3 font-mono text-zinc-200">
                              <div className="flex items-center gap-1.5">
                                <GitBranch className="h-3 w-3 text-zinc-500" />
                                <span className="font-semibold">
                                  {repoLabel}
                                </span>
                              </div>
                            </td>
                            <td className="p-3">
                              <StatusBadge status={run.status} />
                            </td>
                            <td className="p-3 font-mono text-zinc-300">
                              {formatRangeDuration(
                                run.created_at,
                                run.updated_at
                              )}
                            </td>
                            <td className="p-3 font-mono">
                              {run.cost && run.cost > 0 ? (
                                <span className="text-emerald-400">
                                  ${run.cost.toFixed(4)}
                                </span>
                              ) : (
                                <span className="text-zinc-500">—</span>
                              )}
                            </td>
                            <td className="p-3 font-mono text-zinc-400">
                              {formatRelativeTime(run.created_at)}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </AppLayout>
  );
}

/**
 * Loading skeleton — matches the populated layout's structure (header line,
 * 4 metric cards, calibration chart with 4 bars) so loading → populated has
 * no layout shift.
 */
function DashboardSkeleton() {
  return (
    <div className="space-y-6 min-w-0">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Skeleton className="h-28 w-full" />
        <Skeleton className="h-28 w-full" />
        <Skeleton className="h-28 w-full" />
        <Skeleton className="h-28 w-full" />
      </div>
      <div className="space-y-3 rounded-[6px] border border-zinc-800 bg-[#121215] p-5">
        <div className="space-y-1.5">
          <Skeleton className="h-4 w-48" />
          <Skeleton className="h-3 w-72" />
        </div>
        <div className="space-y-3 pt-2">
          <Skeleton className="h-3 w-full" />
          <Skeleton className="h-3 w-full" />
          <Skeleton className="h-3 w-full" />
          <Skeleton className="h-3 w-full" />
        </div>
      </div>
    </div>
  );
}
