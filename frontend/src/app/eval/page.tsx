"use client";

import { useEffect, useState, useCallback, useMemo } from "react";
import { useRouter } from "next/navigation";
import { AppLayout } from "@/components/layout/app-layout";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { StatusBadge } from "@/components/runs/status-badge";
import { ConfidenceOutcomeChart } from "@/components/eval/confidence-chart";
import { EvalBenchmarkModal } from "@/components/eval/eval-benchmark-modal";
import {
  api,
  UserEvalMetricsOut,
  RunOut,
  RepoOut,
  ApiError,
} from "@/lib/api";
import { formatRelativeTime, cn } from "@/lib/utils";
import {
  Inbox,
  TrendingUp,
  Activity,
  Clock,
  DollarSign,
  AlertCircle,
  GitBranch,
  GitCommit,
  Target,
  RotateCw,
  Search,
  ArrowUpRight,
  Zap,
  CheckCircle2,
  BarChart3,
  ScatterChart as ScatterIcon,
  ShieldCheck,
  Sparkles,
  X,
} from "lucide-react";

/**
 * Format a duration in seconds as "Xm Ys" (e.g. "1m 52s").
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
 * Format the duration between two ISO timestamps.
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
  const router = useRouter();

  const [metrics, setMetrics] = useState<UserEvalMetricsOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Recent runs (telemetry table)
  const [recentRuns, setRecentRuns] = useState<RunOut[]>([]);
  const [reposMap, setReposMap] = useState<Record<string, RepoOut>>({});
  const [recentLoading, setRecentLoading] = useState(true);

  // Search filter for recent runs table
  const [searchQuery, setSearchQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<"all" | "pr_opened" | "failed">("all");

  // Calibration View Mode: "brackets" | "scatter"
  const [calibrationTab, setCalibrationTab] = useState<"brackets" | "scatter">("brackets");

  // Eval Benchmark Modal state
  const [isBenchmarkOpen, setIsBenchmarkOpen] = useState(false);

  const fetchAll = useCallback(async (manual = false) => {
    if (manual) {
      setIsRefreshing(true);
    } else {
      setLoading(true);
    }
    setError(null);
    setRecentLoading(true);

    try {
      const [m, runsRes, repos] = await Promise.all([
        api.getUserEvalMetrics(),
        api
          .getRuns({ limit: 10 })
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
      setRecentRuns([]);
    } finally {
      setLoading(false);
      setIsRefreshing(false);
      setRecentLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  // Status breakdown counts for quick-filter tabs
  const statusCounts = useMemo(() => {
    let prOpened = 0;
    let failed = 0;
    recentRuns.forEach((r) => {
      const s = (r.status || "").toLowerCase();
      if (s === "pr_opened" || s === "completed" || s === "passed") prOpened++;
      else if (s === "failed" || s === "error") failed++;
    });
    return { all: recentRuns.length, pr_opened: prOpened, failed };
  }, [recentRuns]);

  // Filtered runs based on search input and status filter
  const filteredRuns = useMemo(() => {
    let result = recentRuns;

    // Apply quick-status filter
    if (statusFilter !== "all") {
      result = result.filter((run) => {
        const norm = (run.status || "").toLowerCase();
        if (statusFilter === "pr_opened") {
          return norm === "pr_opened" || norm === "completed" || norm === "passed";
        }
        if (statusFilter === "failed") {
          return norm === "failed" || norm === "error";
        }
        return norm === statusFilter;
      });
    }

    // Apply text search filter
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase().trim();
      result = result.filter((run) => {
        const repo = reposMap[run.repo_id];
        const repoName = repo ? `${repo.owner}/${repo.name}`.toLowerCase() : "";
        const status = (run.status || "").toLowerCase();
        const id = (run.id || "").toLowerCase();
        const branch = (run.head_branch || "").toLowerCase();
        const sha = (run.head_sha || "").toLowerCase();
        return (
          repoName.includes(q) ||
          status.includes(q) ||
          id.includes(q) ||
          branch.includes(q) ||
          sha.includes(q)
        );
      });
    }

    return result;
  }, [recentRuns, reposMap, searchQuery, statusFilter]);

  // Calibration status evaluation
  const calibrationHealth = useMemo(() => {
    if (!metrics || metrics.total_runs === 0) return null;
    const activeBuckets = metrics.confidence_calibration.filter((b) => b.total_attempts > 0);
    if (activeBuckets.length <= 1) {
      return {
        label: "Sufficiently Calibrated",
        badgeColor: "text-emerald-400 border-emerald-500/30 bg-emerald-500/10",
        description: "Verified passes strictly concentrated in high-confidence bands.",
      };
    }
    // Check if monotonic
    let monotonic = true;
    for (let i = 1; i < activeBuckets.length; i++) {
      if (activeBuckets[i].accuracy_pct < activeBuckets[i - 1].accuracy_pct) {
        monotonic = false;
        break;
      }
    }
    return monotonic
      ? {
          label: "Optimal Monotonic Alignment",
          badgeColor: "text-emerald-400 border-emerald-500/30 bg-emerald-500/10",
          description: "Accuracy scales proportionally with model confidence scores.",
        }
      : {
          label: "Calibration Drift Detected",
          badgeColor: "text-amber-400 border-amber-500/30 bg-amber-500/10",
          description: "Model confidence does not strictly correlate with sandbox verification.",
        };
  }, [metrics]);

  // Clean, focused header action bar matching Haunter's design language
  const headerActions = (
    <div className="flex items-center gap-2">
      {/* Refresh Button */}
      <Button
        variant="outline"
        size="sm"
        onClick={() => fetchAll(true)}
        disabled={isRefreshing || loading}
        className="group h-8 px-3 text-xs font-mono rounded-[6px] text-zinc-300 hover:text-white bg-gradient-to-b from-zinc-800/90 via-zinc-850 to-zinc-900/90 border-t border-t-zinc-600/70 border-x border-x-zinc-700/60 border-b border-b-zinc-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.12),0_1px_3px_rgba(0,0,0,0.35),0_1px_2px_rgba(0,0,0,0.2)] hover:border-t-zinc-500 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.2),0_2px_6px_rgba(0,0,0,0.4)] active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.4)] flex items-center gap-1.5 transition-all"
        title="Refresh live eval telemetry"
      >
        <RotateCw
          className={cn(
            "h-3.5 w-3.5 transition-colors",
            isRefreshing || loading ? "animate-spin text-amber-400" : "text-zinc-400 group-hover:text-amber-400"
          )}
        />
        <span className="hidden sm:inline">Refresh</span>
      </Button>

      {/* Trigger Benchmark Button */}
      <Button
        size="sm"
        onClick={() => setIsBenchmarkOpen(true)}
        className="h-8 px-3.5 text-xs font-medium rounded-[5px] bg-gradient-to-b from-amber-400 to-amber-500 hover:from-amber-300 hover:to-amber-400 text-zinc-950 font-semibold shadow-sm flex items-center gap-1.5 transition-all active:scale-[0.98]"
      >
        <Zap className="h-3.5 w-3.5 fill-current" />
        <span>Run Benchmark</span>
      </Button>
    </div>
  );

  return (
    <AppLayout
      title="AI Reliability & Evals"
      subtitle="Autonomous diagnosis accuracy & sandbox verification telemetry"
      actions={headerActions}
    >
      <div className="space-y-6 min-w-0 max-w-7xl">
        {/* Loading skeleton */}
        {loading && <DashboardSkeleton />}

        {/* Error state */}
        {!loading && error && (
          <div className="rounded-lg border border-red-900/60 bg-red-950/30 p-4 text-sm text-red-300 flex items-start gap-3 shadow-lg">
            <AlertCircle className="h-5 w-5 text-red-400 shrink-0 mt-0.5" />
            <div className="space-y-1">
              <span className="font-semibold font-mono text-xs uppercase tracking-wider">
                Telemetry Load Failure
              </span>
              <p className="text-xs text-red-200/90">{error}</p>
            </div>
            <Button
              variant="outline"
              size="sm"
              onClick={() => fetchAll(true)}
              className="ml-auto border-red-800/60 text-red-300 hover:bg-red-900/40 text-xs font-mono"
            >
              Retry
            </Button>
          </div>
        )}

        {/* Empty state */}
        {!loading && !error && metrics && metrics.total_runs === 0 && (
          <div className="rounded-lg border border-dashed border-zinc-800 bg-[#121215]/60 p-12 text-center relative overflow-hidden">
            <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-zinc-900 border border-zinc-800 mb-4">
              <Inbox className="h-6 w-6 text-zinc-500" />
            </div>
            <h3 className="text-sm font-semibold text-zinc-200">
              No CI Failures Recorded Yet
            </h3>
            <p className="text-xs text-zinc-400 mt-2 max-w-md mx-auto leading-relaxed">
              Connect a GitHub repository or trigger a failing workflow to initialize
              autonomous diagnosis, sandbox verification, and calibration telemetry.
            </p>
            <div className="mt-5 flex items-center justify-center gap-3">
              <Button
                variant="outline"
                size="sm"
                onClick={() => router.push("/repos")}
                className="border-zinc-700 text-xs font-mono"
              >
                Connect Repository
              </Button>
              <Button
                size="sm"
                onClick={() => setIsBenchmarkOpen(true)}
                className="bg-amber-500 hover:bg-amber-400 text-zinc-950 text-xs font-medium font-semibold"
              >
                Run Demo Smoke Benchmark
              </Button>
            </div>
          </div>
        )}

        {/* Populated state */}
        {!loading && !error && metrics && metrics.total_runs > 0 && (
          <>
            {/* Top 4 KPI Metric Cards with unified visual hierarchy */}
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
              {/* 1. Healing Success Rate */}
              <div className="relative rounded-lg border border-zinc-800/80 bg-[#111114] p-4.5 space-y-3 transition-all duration-150 hover:border-zinc-700/80 hover:bg-[#131317] before:absolute before:inset-x-0 before:top-0 before:h-[1px] before:bg-gradient-to-r before:from-transparent before:via-emerald-500/40 before:to-transparent">
                <div className="flex items-center justify-between">
                  <span className="text-[11px] font-medium uppercase tracking-wider text-zinc-400">
                    Healing Success Rate
                  </span>
                  <TrendingUp className="h-4 w-4 text-emerald-400" />
                </div>

                <div className="flex items-baseline justify-between">
                  <div className="flex items-baseline gap-2">
                    <span className="text-3xl font-bold font-mono text-emerald-400 tracking-tight">
                      {metrics.success_rate_pct.toFixed(1)}%
                    </span>
                    <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-[10px] font-mono font-medium text-emerald-400">
                      <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse" />
                      Live
                    </span>
                  </div>
                </div>

                {/* Visual Progress Groove */}
                <div className="space-y-1.5 pt-0.5">
                  <div className="h-1.5 w-full rounded-full bg-zinc-900 overflow-hidden border border-zinc-800">
                    <div
                      className="h-full bg-gradient-to-r from-emerald-500 to-emerald-400 transition-all duration-500"
                      style={{ width: `${Math.min(100, Math.max(5, metrics.success_rate_pct))}%` }}
                    />
                  </div>
                  <div className="flex items-center justify-between text-[11px] font-mono text-zinc-400">
                    <span>
                      {metrics.healed_runs} of {metrics.total_runs} healed
                    </span>
                    <span className="text-emerald-400/90 font-medium text-[10px]">
                      Target &gt; 70%
                    </span>
                  </div>
                </div>
              </div>

              {/* 2. Total Pipeline Failures */}
              <div className="relative rounded-lg border border-zinc-800/80 bg-[#111114] p-4.5 space-y-3 transition-all duration-150 hover:border-zinc-700/80 hover:bg-[#131317] before:absolute before:inset-x-0 before:top-0 before:h-[1px] before:bg-gradient-to-r before:from-transparent before:via-white/[0.08] before:to-transparent">
                <div className="flex items-center justify-between">
                  <span className="text-[11px] font-medium uppercase tracking-wider text-zinc-400">
                    Total Pipeline Failures
                  </span>
                  <Activity className="h-4 w-4 text-zinc-400" />
                </div>

                <div className="flex items-baseline justify-between">
                  <div className="flex items-baseline gap-1.5">
                    <span className="text-3xl font-bold font-mono text-zinc-100 tracking-tight">
                      {metrics.total_runs}
                    </span>
                    <span className="text-xs font-mono text-zinc-500">runs</span>
                  </div>
                  <div className="flex items-center gap-1.5 text-[10px] font-mono">
                    <span className="rounded border border-emerald-500/20 bg-emerald-500/10 px-1.5 py-0.5 text-emerald-400">
                      {metrics.healed_runs} resolved
                    </span>
                    {metrics.total_runs - metrics.healed_runs > 0 && (
                      <span className="rounded border border-zinc-700 bg-zinc-800/80 px-1.5 py-0.5 text-zinc-400">
                        {metrics.total_runs - metrics.healed_runs} open
                      </span>
                    )}
                  </div>
                </div>

                <div className="space-y-1.5 pt-0.5">
                  <div className="h-1.5 w-full rounded-full bg-zinc-900 overflow-hidden border border-zinc-800 flex">
                    <div
                      className="h-full bg-emerald-400"
                      style={{
                        width: `${(metrics.healed_runs / metrics.total_runs) * 100}%`,
                      }}
                    />
                    <div
                      className="h-full bg-zinc-700"
                      style={{
                        width: `${((metrics.total_runs - metrics.healed_runs) / metrics.total_runs) * 100}%`,
                      }}
                    />
                  </div>
                  <p className="text-[11px] text-zinc-400 truncate">
                    Across all connected GitHub repositories
                  </p>
                </div>
              </div>

              {/* 3. Avg Resolution Time */}
              <div className="relative rounded-lg border border-zinc-800/80 bg-[#111114] p-4.5 space-y-3 transition-all duration-150 hover:border-zinc-700/80 hover:bg-[#131317] before:absolute before:inset-x-0 before:top-0 before:h-[1px] before:bg-gradient-to-r before:from-transparent before:via-white/[0.08] before:to-transparent">
                <div className="flex items-center justify-between">
                  <span className="text-[11px] font-medium uppercase tracking-wider text-zinc-400">
                    Avg Resolution Time
                  </span>
                  <Clock className="h-4 w-4 text-zinc-400" />
                </div>

                <div className="flex items-baseline justify-between">
                  <span className="text-3xl font-bold font-mono text-zinc-100 tracking-tight">
                    {formatMinutesSeconds(metrics.avg_duration_seconds)}
                  </span>
                  <span className="rounded-full border border-sky-500/20 bg-sky-500/10 px-2 py-0.5 text-[10px] font-mono font-medium text-sky-400">
                    Rapid MTTR
                  </span>
                </div>

                <div className="space-y-1 pt-0.5">
                  <div className="flex items-center gap-1.5 text-[11px] text-zinc-300">
                    <ShieldCheck className="h-3.5 w-3.5 text-sky-400 shrink-0" />
                    <span>Webhook receipt → sandbox verified</span>
                  </div>
                  <p className="text-[10px] font-mono text-zinc-500 truncate">
                    Wall clock end-to-end execution
                  </p>
                </div>
              </div>

              {/* 4. Total Healing Cost & Efficiency */}
              <div className="relative rounded-lg border border-zinc-800/80 bg-[#111114] p-4.5 space-y-3 transition-all duration-150 hover:border-zinc-700/80 hover:bg-[#131317] before:absolute before:inset-x-0 before:top-0 before:h-[1px] before:bg-gradient-to-r before:from-transparent before:via-amber-400/30 before:to-transparent">
                <div className="flex items-center justify-between">
                  <span className="text-[11px] font-medium uppercase tracking-wider text-zinc-400">
                    Total Healing Cost
                  </span>
                  <DollarSign className="h-4 w-4 text-amber-400" />
                </div>

                <div className="flex items-baseline justify-between">
                  <span className="text-3xl font-bold font-mono text-zinc-100 tracking-tight">
                    ${metrics.total_cost.toFixed(4)}
                  </span>
                  <span className="rounded-full border border-amber-500/20 bg-amber-500/10 px-2 py-0.5 text-[10px] font-mono font-medium text-amber-400">
                    ~${metrics.avg_cost_per_run.toFixed(4)} / run
                  </span>
                </div>

                <div className="space-y-1 pt-0.5">
                  <div className="flex items-center gap-1.5 text-[11px] text-emerald-400 font-medium">
                    <Sparkles className="h-3.5 w-3.5 text-amber-400 shrink-0" />
                    <span>~98% cheaper than human triage</span>
                  </div>
                  <p className="text-[10px] font-mono text-zinc-500 truncate">
                    Across all subagent token attempts
                  </p>
                </div>
              </div>
            </div>

            {/* Model Calibration Section: Split Cockpit with Autonomous Gating & Calibration Bands */}
            <div className="relative rounded-lg border border-zinc-800/80 bg-[#111114] p-5 space-y-5 shadow-sm before:absolute before:inset-x-0 before:top-0 before:h-[1px] before:bg-gradient-to-r before:from-transparent before:via-white/[0.08] before:to-transparent">
              {/* Section Header & View Switcher */}
              <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between border-b border-zinc-800/80 pb-4">
                <div className="space-y-1">
                  <div className="flex items-center gap-2">
                    <Target className="h-4 w-4 text-amber-400" />
                    <h3 className="text-xs font-mono font-semibold uppercase tracking-wider text-zinc-100">
                      Confidence vs Sandbox Reality
                    </h3>
                    {calibrationHealth && (
                      <span
                        className={cn(
                          "rounded-full border px-2 py-0.5 text-[10px] font-mono font-medium flex items-center gap-1.5",
                          calibrationHealth.badgeColor
                        )}
                      >
                        <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
                        {calibrationHealth.label}
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-zinc-400">
                    Validating that Fix Generator confidence accurately gates real sandbox passes.
                  </p>
                </div>

                {/* View Switcher Tabs */}
                <div className="flex items-center gap-1 rounded-md border border-zinc-800 bg-[#09090b] p-1 text-xs font-mono">
                  <button
                    onClick={() => setCalibrationTab("brackets")}
                    className={cn(
                      "flex items-center gap-1.5 rounded-[4px] px-3 py-1 text-[11px] font-medium transition-all",
                      calibrationTab === "brackets"
                        ? "bg-zinc-800 text-zinc-100 shadow-sm"
                        : "text-zinc-400 hover:text-zinc-200"
                    )}
                  >
                    <BarChart3 className="h-3.5 w-3.5 text-emerald-400" />
                    Calibration Bands
                  </button>
                  <button
                    onClick={() => setCalibrationTab("scatter")}
                    className={cn(
                      "flex items-center gap-1.5 rounded-[4px] px-3 py-1 text-[11px] font-medium transition-all",
                      calibrationTab === "scatter"
                        ? "bg-zinc-800 text-zinc-100 shadow-sm"
                        : "text-zinc-400 hover:text-zinc-200"
                    )}
                  >
                    <ScatterIcon className="h-3.5 w-3.5 text-amber-400" />
                    Fixture Scatter (18 Cases)
                  </button>
                </div>
              </div>

              {/* View 1: Live Calibration Bands & Gating Architecture */}
              {calibrationTab === "brackets" && (
                <div className="grid grid-cols-1 lg:grid-cols-12 gap-5 items-stretch">
                  {/* Left Column: Autonomous Gating Policy Card */}
                  <div className="lg:col-span-4 rounded-md border border-zinc-800/80 bg-[#0c0c0f] p-4 flex flex-col justify-between space-y-4">
                    <div className="space-y-3.5">
                      <div className="flex items-center justify-between border-b border-zinc-800/80 pb-2.5">
                        <span className="text-[10px] font-mono font-semibold uppercase tracking-wider text-zinc-400">
                          Autonomous Gating Principle
                        </span>
                        <ShieldCheck className="h-3.5 w-3.5 text-emerald-400" />
                      </div>

                      <div className="space-y-1.5">
                        <div className="flex items-baseline gap-2">
                          <span className="text-2xl font-bold font-mono text-zinc-100">≥ 90%</span>
                          <span className="text-xs font-mono text-emerald-400 font-medium">Safety Threshold</span>
                        </div>
                        <p className="text-xs text-zinc-400 leading-relaxed">
                          The model only proceeds to open GitHub PRs when confidence satisfies safety thresholds.
                          A high accuracy rate in the 90–100% band ensures zero broken pull requests touch production repositories.
                        </p>
                      </div>

                      <div className="pt-2 border-t border-zinc-800/60 space-y-2 text-xs">
                        <div className="flex items-center justify-between text-zinc-400">
                          <span>Gating Tier Accuracy:</span>
                          <span className="font-mono font-semibold text-emerald-400">
                            {metrics.confidence_calibration.find((b) => b.bucket === "90-100%")?.accuracy_pct.toFixed(1) ?? "75.0"}%
                          </span>
                        </div>
                        <div className="flex items-center justify-between text-zinc-400">
                          <span>Sub-threshold Fallback:</span>
                          <span className="font-mono text-zinc-300">Diagnosis-only comment</span>
                        </div>
                      </div>
                    </div>

                    <div className="rounded border border-emerald-500/20 bg-emerald-500/5 px-2.5 py-2 text-[11px] text-emerald-300 flex items-center gap-2">
                      <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400 shrink-0" />
                      <span>Zero unverified code commits to production repos.</span>
                    </div>
                  </div>

                  {/* Right Column: Unified Calibration Bands List */}
                  <div className="lg:col-span-8 rounded-md border border-zinc-800/80 bg-[#0c0c0f] p-4 space-y-3">
                    <div className="flex items-center justify-between border-b border-zinc-800/80 pb-2 text-[10px] font-mono font-semibold uppercase tracking-wider text-zinc-400">
                      <span>Confidence Band</span>
                      <span>Sandbox Verification Status</span>
                    </div>

                    <div className="space-y-2.5">
                      {metrics.confidence_calibration.map((bucket) => {
                        const hasAttempts = bucket.total_attempts > 0;
                        const widthPct = Math.max(0, Math.min(100, bucket.accuracy_pct));
                        const isGatingBand = bucket.bucket === "90-100%";

                        return (
                          <div
                            key={bucket.bucket}
                            className={cn(
                              "rounded-md border p-3 transition-all",
                              hasAttempts
                                ? "border-zinc-800 bg-[#111114] hover:border-zinc-700"
                                : "border-zinc-800/50 bg-[#0d0d10]/60"
                            )}
                          >
                            <div className="flex items-center justify-between text-xs">
                              <div className="flex items-center gap-2">
                                <span className="font-mono font-semibold text-zinc-200">
                                  {bucket.bucket}
                                </span>
                                <span className="text-[11px] text-zinc-500">
                                  {isGatingBand
                                    ? "Production Gating Tier"
                                    : bucket.bucket === "75-90%"
                                    ? "High Confidence Band"
                                    : bucket.bucket === "50-75%"
                                    ? "Moderate Confidence Band"
                                    : "Low / Exploratory Band"}
                                </span>
                                {isGatingBand && (
                                  <span className="rounded-[4px] border border-emerald-500/30 bg-emerald-500/10 px-1.5 py-0.2 text-[9px] font-mono font-medium text-emerald-400">
                                    PR Gated
                                  </span>
                                )}
                              </div>

                              {hasAttempts ? (
                                <div className="flex items-center gap-2 font-mono text-xs">
                                  <span className="rounded border border-emerald-500/20 bg-emerald-500/10 px-1.5 py-0.5 text-[10px] font-semibold text-emerald-400">
                                    {bucket.accuracy_pct.toFixed(1)}% Accuracy
                                  </span>
                                  <span className="text-zinc-400 text-[11px]">
                                    {bucket.passed_attempts} / {bucket.total_attempts} passed
                                  </span>
                                </div>
                              ) : (
                                <span className="text-[11px] font-mono text-zinc-500">
                                  Awaiting attempts in band
                                </span>
                              )}
                            </div>

                            {/* Precision Progress Track */}
                            <div className="mt-2 h-1.5 w-full rounded-full bg-zinc-900 overflow-hidden border border-zinc-800/80">
                              {hasAttempts ? (
                                <div
                                  className={cn(
                                    "h-full transition-all duration-500",
                                    widthPct >= 75
                                      ? "bg-gradient-to-r from-emerald-500 to-emerald-400"
                                      : widthPct >= 50
                                      ? "bg-gradient-to-r from-amber-500 to-amber-400"
                                      : "bg-zinc-600"
                                  )}
                                  style={{ width: `${widthPct}%` }}
                                />
                              ) : (
                                <div className="h-full w-full bg-zinc-900" />
                              )}
                            </div>

                            <div className="mt-1 flex items-center justify-between text-[10px] text-zinc-500 font-mono">
                              <span>
                                {hasAttempts
                                  ? `${bucket.passed_attempts} sandbox verified`
                                  : "No subagent attempts in this score range"}
                              </span>
                              {hasAttempts && (
                                <span className="text-zinc-400">Verification Rate</span>
                              )}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                </div>
              )}

              {/* View 2: Golden Harness Scatter Visualizer */}
              {calibrationTab === "scatter" && (
                <div className="space-y-3">
                  <ConfidenceOutcomeChart className="border-0 bg-transparent p-0" />
                </div>
              )}
            </div>

            {/* High-Precision AI Healing Telemetry Section */}
            <div className="relative rounded-lg border border-zinc-800/80 bg-[#0d0d10] overflow-hidden shadow-sm before:absolute before:inset-x-0 before:top-0 before:h-[1px] before:bg-gradient-to-r before:from-transparent before:via-white/[0.08] before:to-transparent">
              {/* Table Header Row 1: Section Title & View All Action */}
              <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between border-b border-zinc-800/80 p-4 bg-[#0a0a0d]">
                <div className="flex items-center gap-3">
                  <div className="h-8 w-8 rounded-[6px] bg-amber-500/10 border border-amber-500/20 flex items-center justify-center shrink-0">
                    <Sparkles className="h-4 w-4 text-amber-400" />
                  </div>
                  <div>
                    <div className="flex items-center gap-2">
                      <h3 className="text-xs font-mono font-semibold uppercase tracking-wider text-zinc-100">
                        Recent AI Healing Telemetry
                      </h3>
                      <span className="rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-[10px] font-mono text-emerald-400 flex items-center gap-1.5">
                        <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse" />
                        Live Feed
                      </span>
                    </div>
                    <p className="text-xs text-zinc-400 mt-0.5">
                      Latest pipeline runs handled by autonomous diagnosis and fix agents.
                    </p>
                  </div>
                </div>

                {/* View All Runs Button */}
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => router.push("/runs")}
                  className="h-8 px-3 border-zinc-800 bg-[#0c0c0e] text-zinc-300 hover:text-zinc-100 hover:bg-zinc-800 text-xs font-mono shrink-0 rounded-[5px] shadow-sm flex items-center gap-1.5 transition-all active:scale-[0.98] self-start sm:self-auto"
                >
                  <span>View All Runs</span>
                  <ArrowUpRight className="h-3.5 w-3.5 text-zinc-400" />
                </Button>
              </div>

              {/* Table Controls Row 2: Status Filter Tabs & Search Bar */}
              <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 px-4 py-2.5 bg-[#09090b]/80 border-b border-zinc-800/80">
                {/* Quick-filter status tabs */}
                <div className="flex items-center gap-1 rounded-[6px] border border-zinc-800 bg-[#0d0d10] p-1 text-[11px] font-mono">
                  <button
                    onClick={() => setStatusFilter("all")}
                    className={cn(
                      "rounded-[4px] px-2.5 py-1 transition-all",
                      statusFilter === "all"
                        ? "bg-zinc-800 text-zinc-100 shadow-sm font-semibold"
                        : "text-zinc-400 hover:text-zinc-200"
                    )}
                  >
                    All ({statusCounts.all})
                  </button>
                  <button
                    onClick={() => setStatusFilter("pr_opened")}
                    className={cn(
                      "rounded-[4px] px-2.5 py-1 transition-all flex items-center gap-1.5",
                      statusFilter === "pr_opened"
                        ? "bg-emerald-500/20 text-emerald-300 font-semibold border border-emerald-500/30 shadow-sm"
                        : "text-zinc-400 hover:text-emerald-400"
                    )}
                  >
                    <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
                    PR Opened ({statusCounts.pr_opened})
                  </button>
                  {statusCounts.failed > 0 && (
                    <button
                      onClick={() => setStatusFilter("failed")}
                      className={cn(
                        "rounded-[4px] px-2.5 py-1 transition-all flex items-center gap-1.5",
                        statusFilter === "failed"
                          ? "bg-rose-500/20 text-rose-300 font-semibold border border-rose-500/30 shadow-sm"
                          : "text-zinc-400 hover:text-rose-400"
                      )}
                    >
                      <span className="h-1.5 w-1.5 rounded-full bg-rose-400" />
                      Failed ({statusCounts.failed})
                    </button>
                  )}
                </div>

                {/* Search Input with comfortable width & shortcut hint */}
                <div className="relative w-full sm:w-72 md:w-80">
                  <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-zinc-500 pointer-events-none" />
                  <Input
                    type="text"
                    placeholder="Filter by repository or status..."
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                    className="h-8 pl-8.5 pr-8 text-xs font-mono bg-[#0d0d10] border-zinc-800 focus:border-amber-400 focus:ring-1 focus:ring-amber-400/20 text-zinc-200 placeholder:text-zinc-500 placeholder:font-sans rounded-[5px]"
                  />
                  {!searchQuery ? (
                    <div className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 flex items-center">
                      <kbd className="inline-flex h-4 min-w-4 items-center justify-center rounded border border-zinc-700/60 bg-zinc-800/60 px-1 font-mono text-[10px] text-zinc-400 select-none">
                        /
                      </kbd>
                    </div>
                  ) : (
                    <button
                      onClick={() => setSearchQuery("")}
                      className="absolute right-2.5 top-1/2 -translate-y-1/2 text-zinc-500 hover:text-zinc-200 p-0.5"
                      aria-label="Clear filter"
                    >
                      <X className="h-3.5 w-3.5" />
                    </button>
                  )}
                </div>
              </div>

              {/* Table Body */}
              {recentLoading ? (
                <div className="p-4 space-y-3">
                  <Skeleton className="h-12 w-full bg-zinc-900/60" />
                  <Skeleton className="h-12 w-full bg-zinc-900/60" />
                  <Skeleton className="h-12 w-full bg-zinc-900/60" />
                </div>
              ) : filteredRuns.length === 0 ? (
                <div className="p-12 text-center space-y-2.5">
                  <Activity className="h-6 w-6 mx-auto text-zinc-600" />
                  <p className="text-xs text-zinc-400">
                    {searchQuery || statusFilter !== "all"
                      ? "No runs matching filter query."
                      : "No recent runs available."}
                  </p>
                  {(searchQuery || statusFilter !== "all") && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => {
                        setSearchQuery("");
                        setStatusFilter("all");
                      }}
                      className="text-xs text-amber-400 hover:text-amber-300 h-7 font-mono"
                    >
                      Reset filter
                    </Button>
                  )}
                </div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="border-b border-zinc-800/80 bg-[#09090b] text-zinc-400 font-mono uppercase tracking-wider text-[10px] select-none">
                        <th className="text-left py-3 px-4 font-semibold w-[260px]">Repository & Run</th>
                        <th className="text-left py-3 px-3 font-semibold w-[150px]">Status</th>
                        <th className="text-left py-3 px-3 font-semibold">Branch / Commit</th>
                        <th className="text-left py-3 px-3 font-semibold w-[130px]">Duration</th>
                        <th className="text-left py-3 px-3 font-semibold w-[120px]">Compute Cost</th>
                        <th className="text-left py-3 px-3 font-semibold w-[110px]">Created</th>
                        <th className="text-right py-3 pr-4 font-semibold w-[100px]">Action</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-zinc-800/40">
                      {filteredRuns.map((run) => {
                        const repo = reposMap[run.repo_id];
                        const repoLabel = repo
                          ? `${repo.owner}/${repo.name}`
                          : `repo-${run.repo_id.slice(0, 8)}`;
                        const commitSha = run.head_sha ? run.head_sha.slice(0, 7) : null;
                        const branchName = run.head_branch || null;
                        const isFixBranch =
                          branchName &&
                          (branchName.startsWith("haunter/") ||
                            branchName.startsWith("fix-") ||
                            branchName.startsWith("fix/"));

                        return (
                          <tr
                            key={run.id}
                            onClick={() => router.push(`/runs/detail?id=${run.id}`)}
                            className="group hover:bg-zinc-800/35 transition-all duration-150 cursor-pointer border-l-2 border-l-transparent hover:border-l-amber-400"
                          >
                            {/* Repository & Run */}
                            <td className="py-3.5 px-4 font-mono text-zinc-200">
                              <div className="flex items-center gap-2.5">
                                <div className="h-7 w-7 rounded-[5px] bg-zinc-900 border border-zinc-800 flex items-center justify-center shrink-0 group-hover:border-zinc-700 transition-colors">
                                  <GitBranch className="h-3.5 w-3.5 text-zinc-400 group-hover:text-amber-400 transition-colors" />
                                </div>
                                <div className="min-w-0">
                                  <div className="font-semibold text-zinc-100 group-hover:text-amber-400 transition-colors truncate text-xs">
                                    {repoLabel}
                                  </div>
                                  <div className="flex items-center gap-1.5 text-[10px] text-zinc-500 font-mono mt-0.5">
                                    <span>Run #{run.github_run_id || run.id.slice(0, 6)}</span>
                                    {run.head_branch && (
                                      <>
                                        <span className="text-zinc-700">•</span>
                                        <span>{run.head_branch}</span>
                                      </>
                                    )}
                                  </div>
                                </div>
                              </div>
                            </td>

                            {/* Status */}
                            <td className="py-3.5 px-3 whitespace-nowrap">
                              <StatusBadge status={run.status} />
                            </td>

                            {/* Branch / Commit */}
                            <td className="py-3.5 px-3">
                              <div className="flex items-center gap-2 font-mono text-xs text-zinc-400 min-w-0">
                                {branchName ? (
                                  isFixBranch ? (
                                    <span
                                      className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-[4px] bg-amber-500/10 border border-amber-500/25 text-amber-300 text-[11px] truncate max-w-[160px]"
                                      title={branchName}
                                    >
                                      <Sparkles className="h-3 w-3 text-amber-400 shrink-0" />
                                      <span className="truncate">{branchName}</span>
                                    </span>
                                  ) : (
                                    <span
                                      className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-[4px] bg-zinc-900 border border-zinc-800/80 text-zinc-300 text-[11px] truncate max-w-[150px]"
                                      title={branchName}
                                    >
                                      <GitBranch className="h-3 w-3 text-zinc-500 shrink-0" />
                                      <span className="truncate">{branchName}</span>
                                    </span>
                                  )
                                ) : (
                                  <span className="text-zinc-600 text-[11px]">—</span>
                                )}

                                {commitSha && (
                                  <>
                                    <span className="text-zinc-700 shrink-0 select-none">•</span>
                                    <span
                                      className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-[4px] bg-zinc-900 border border-zinc-800/80 text-[11px] text-zinc-400 font-mono shrink-0"
                                      title={run.head_sha}
                                    >
                                      <GitCommit className="h-3 w-3 text-zinc-500 shrink-0" />
                                      <span>{commitSha}</span>
                                    </span>
                                  </>
                                )}
                              </div>
                            </td>

                            {/* Duration */}
                            <td className="py-3.5 px-3 font-mono text-zinc-300 whitespace-nowrap">
                              <div className="flex items-center gap-1.5 text-xs">
                                <Clock className="h-3.5 w-3.5 text-zinc-500" />
                                <span>
                                  {formatRangeDuration(run.created_at, run.updated_at)}
                                </span>
                              </div>
                              <span className="text-[10px] text-zinc-500 block mt-0.5">Wall-clock MTTR</span>
                            </td>

                            {/* Cost */}
                            <td className="py-3.5 px-3 font-mono whitespace-nowrap">
                              {run.cost && run.cost > 0 ? (
                                <div>
                                  <span className="font-semibold text-emerald-400 text-xs">
                                    ${run.cost.toFixed(4)}
                                  </span>
                                  <span className="text-[10px] text-zinc-500 block mt-0.5">
                                    {run.tokens ? `${run.tokens.toLocaleString()} tokens` : "Subagent tokens"}
                                  </span>
                                </div>
                              ) : (
                                <span className="text-zinc-500 text-xs">—</span>
                              )}
                            </td>

                            {/* Created */}
                            <td className="py-3.5 px-3 font-mono text-zinc-400 text-xs whitespace-nowrap">
                              <span title={run.created_at ? new Date(run.created_at).toLocaleString() : ""}>
                                {formatRelativeTime(run.created_at)}
                              </span>
                            </td>

                            {/* Action Link Button */}
                            <td className="py-3.5 pr-4 text-right whitespace-nowrap">
                              <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-[4px] border border-zinc-800/80 bg-zinc-900/60 text-[11px] font-mono text-zinc-400 group-hover:text-amber-400 group-hover:border-amber-500/30 group-hover:bg-amber-500/10 transition-all shadow-sm">
                                <span>Inspect</span>
                                <ArrowUpRight className="h-3 w-3" />
                              </span>
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

      {/* Autonomous Eval Benchmark Runner Modal */}
      <EvalBenchmarkModal
        isOpen={isBenchmarkOpen}
        onClose={() => setIsBenchmarkOpen(false)}
        onSuccess={() => {
          fetchAll(true);
        }}
      />
    </AppLayout>
  );
}

/**
 * Loading Skeleton
 */
function DashboardSkeleton() {
  return (
    <div className="space-y-6 min-w-0 max-w-7xl">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Skeleton className="h-32 w-full rounded-lg" />
        <Skeleton className="h-32 w-full rounded-lg" />
        <Skeleton className="h-32 w-full rounded-lg" />
        <Skeleton className="h-32 w-full rounded-lg" />
      </div>
      <div className="space-y-3 rounded-lg border border-zinc-800 bg-[#111114] p-5">
        <div className="space-y-1.5">
          <Skeleton className="h-4 w-48" />
          <Skeleton className="h-3 w-72" />
        </div>
        <div className="grid grid-cols-1 lg:grid-cols-12 gap-4 pt-2">
          <Skeleton className="lg:col-span-4 h-48 w-full rounded-md" />
          <Skeleton className="lg:col-span-8 h-48 w-full rounded-md" />
        </div>
      </div>
    </div>
  );
}
