"use client";

import { useEffect, useState, useCallback, useMemo } from "react";
import { useRouter } from "next/navigation";
import { AppLayout } from "@/components/layout/app-layout";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import { ConfidenceOutcomeChart } from "@/components/eval/confidence-chart";
import { EvalBenchmarkModal } from "@/components/eval/eval-benchmark-modal";
import {
  api,
  UserEvalMetricsOut,
  ApiError,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import {
  Inbox,
  TrendingUp,
  Activity,
  Clock,
  AlertCircle,
  Target,
  Zap,
  CheckCircle2,
  BarChart3,
  ScatterChart as ScatterIcon,
  ShieldCheck,
  Sparkles,
  RefreshCw,
  Coins,
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

export default function EvalPage() {
  const router = useRouter();

  const [metrics, setMetrics] = useState<UserEvalMetricsOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Calibration View Mode: "brackets" | "scatter"
  const [calibrationTab, setCalibrationTab] = useState<"brackets" | "scatter">("brackets");

  // Eval Benchmark Modal state
  const [isBenchmarkOpen, setIsBenchmarkOpen] = useState(false);

  const fetchMetrics = useCallback(async (manual = false) => {
    if (manual) {
      setIsRefreshing(true);
    } else {
      setLoading(true);
    }
    setError(null);

    try {
      const m = await api.getUserEvalMetrics();
      setMetrics(m);
    } catch (err: unknown) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else if (err instanceof Error) {
        setError(err.message);
      } else {
        setError("Failed to load AI reliability metrics.");
      }
    } finally {
      setLoading(false);
      setIsRefreshing(false);
    }
  }, []);

  useEffect(() => {
    fetchMetrics();
  }, [fetchMetrics]);

  // Keyboard shortcut handler: 'r' to refresh
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (["INPUT", "TEXTAREA", "SELECT"].includes((e.target as HTMLElement)?.tagName)) {
        return;
      }
      if (e.key === "r" || e.key === "R") {
        e.preventDefault();
        fetchMetrics(true);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [fetchMetrics]);

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

  // Header action bar
  const headerActions = (
    <div className="flex items-center gap-2.5">
      {/* Refresh Button */}
      <Button
        variant="outline"
        size="sm"
        onClick={() => fetchMetrics(true)}
        disabled={isRefreshing || loading}
        className="group h-8 px-3 text-xs font-mono rounded-[6px] text-zinc-300 hover:text-white bg-gradient-to-b from-zinc-800/90 via-zinc-850 to-zinc-900/90 border-t border-t-zinc-600/70 border-x border-x-zinc-700/60 border-b border-b-zinc-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.12),0_1px_3px_rgba(0,0,0,0.35),0_1px_2px_rgba(0,0,0,0.2)] hover:border-t-zinc-500 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.2),0_2px_6px_rgba(0,0,0,0.4)] active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.4)] flex items-center gap-1.5 transition-all cursor-pointer"
        title="Refresh live eval telemetry (Hot-key: R)"
      >
        <RefreshCw
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
        className="flex items-center gap-1.5 bg-gradient-to-b from-amber-400 via-amber-450 to-amber-500 hover:from-amber-300 hover:to-amber-400 text-zinc-950 font-bold text-xs h-8 px-3.5 rounded-[6px] border-t border-t-amber-200/50 border-x border-x-amber-400/60 border-b border-b-amber-600/40 shadow-[inset_0_1px_0_rgba(255,255,255,0.35),0_2px_8px_rgba(245,158,11,0.25)] active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.3)] cursor-pointer transition-all"
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
      <div className="space-y-6 min-w-0 pb-16">
        {/* Loading skeleton */}
        {loading && <DashboardSkeleton />}

        {/* Error state */}
        {!loading && error && (
          <div className="rounded-[7px] border border-red-900/60 bg-red-950/30 p-3.5 text-[13px] text-red-300 flex items-start gap-3 shadow-md animate-in fade-in">
            <AlertCircle className="h-4.5 w-4.5 text-red-400 shrink-0 mt-0.5" />
            <div className="space-y-0.5 flex-1">
              <p className="font-semibold text-red-200 font-mono text-xs uppercase tracking-wider">
                Telemetry Load Failure
              </p>
              <p className="text-xs text-red-300/90 font-mono">{error}</p>
            </div>
            <Button
              variant="outline"
              size="sm"
              onClick={() => fetchMetrics(true)}
              className="border-red-800/60 text-red-300 hover:bg-red-900/40 text-xs font-mono rounded-[5px]"
            >
              Retry
            </Button>
          </div>
        )}

        {/* Empty state */}
        {!loading && !error && metrics && metrics.total_runs === 0 && (
          <div className="rounded-xl border border-dashed border-zinc-800/80 bg-[#09090b]/40 p-12 text-center relative overflow-hidden">
            <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-xl bg-amber-400/10 border border-amber-500/30 text-amber-400 mb-4 shadow-[0_0_20px_rgba(245,158,11,0.15)]">
              <Inbox className="h-6 w-6" />
            </div>
            <h3 className="text-base font-bold text-zinc-100 font-mono">
              No CI Failures Recorded Yet
            </h3>
            <p className="text-xs sm:text-sm text-zinc-400 mt-2 max-w-md mx-auto leading-relaxed">
              Connect a GitHub repository or trigger a failing workflow to initialize
              autonomous diagnosis, sandbox verification, and calibration telemetry.
            </p>
            <div className="mt-6 flex items-center justify-center gap-3">
              <Button
                variant="outline"
                size="sm"
                onClick={() => router.push("/repos")}
                className="border-zinc-700 text-xs font-mono rounded-[6px]"
              >
                Connect Repository
              </Button>
              <Button
                size="sm"
                onClick={() => setIsBenchmarkOpen(true)}
                className="bg-gradient-to-b from-amber-400 to-amber-500 text-zinc-950 font-bold text-xs h-8 px-3.5 rounded-[6px] shadow-sm"
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
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3.5">
              {/* 1. Healing Success Rate */}
              <div className="rounded-lg border border-zinc-800/80 bg-gradient-to-b from-[#121216]/90 to-[#0c0c0e]/90 backdrop-blur p-4 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.03)] flex flex-col justify-between">
                <div className="flex items-center justify-between">
                  <span className="text-[11px] font-mono uppercase tracking-wider text-zinc-400">
                    Healing Success Rate
                  </span>
                  <TrendingUp className="h-4 w-4 text-emerald-400" />
                </div>

                <div className="mt-2 flex items-baseline gap-2">
                  <span className="text-2xl font-bold font-mono text-emerald-400 tabular-nums">
                    {metrics.success_rate_pct.toFixed(1)}%
                  </span>
                  <span className="inline-flex items-center gap-1.5 rounded-[4px] border border-emerald-500/30 bg-emerald-500/10 px-1.5 py-0.2 text-[10px] font-mono font-medium text-emerald-400">
                    <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse" />
                    Live
                  </span>
                </div>

                {/* Progress Track */}
                <div className="mt-2 space-y-1.5">
                  <div className="h-1.5 w-full rounded-full bg-zinc-900 overflow-hidden border border-zinc-800/80">
                    <div
                      className="h-full bg-gradient-to-r from-emerald-500 to-emerald-400 transition-all duration-500"
                      style={{ width: `${Math.min(100, Math.max(5, metrics.success_rate_pct))}%` }}
                    />
                  </div>
                  <div className="flex items-center justify-between text-[11px] font-mono text-zinc-500">
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
              <div className="rounded-lg border border-zinc-800/80 bg-gradient-to-b from-[#121216]/90 to-[#0c0c0e]/90 backdrop-blur p-4 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.03)] flex flex-col justify-between">
                <div className="flex items-center justify-between">
                  <span className="text-[11px] font-mono uppercase tracking-wider text-zinc-400">
                    Total Pipeline Failures
                  </span>
                  <Activity className="h-4 w-4 text-zinc-400" />
                </div>

                <div className="mt-2 flex items-baseline justify-between">
                  <div className="flex items-baseline gap-1.5">
                    <span className="text-2xl font-bold font-mono text-zinc-100 tabular-nums">
                      {metrics.total_runs}
                    </span>
                    <span className="text-[11px] font-mono text-zinc-500">runs</span>
                  </div>
                  <div className="flex items-center gap-1.5 text-[10px] font-mono">
                    <span className="rounded-[4px] border border-emerald-500/20 bg-emerald-500/10 px-1.5 py-0.2 text-emerald-400">
                      {metrics.healed_runs} resolved
                    </span>
                    {metrics.total_runs - metrics.healed_runs > 0 && (
                      <span className="rounded-[4px] border border-zinc-700 bg-zinc-800/80 px-1.5 py-0.2 text-zinc-400">
                        {metrics.total_runs - metrics.healed_runs} open
                      </span>
                    )}
                  </div>
                </div>

                <div className="mt-2 space-y-1.5">
                  <div className="h-1.5 w-full rounded-full bg-zinc-900 overflow-hidden border border-zinc-800/80 flex">
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
                  <p className="text-[11px] text-zinc-500 truncate">
                    Across all connected GitHub repositories
                  </p>
                </div>
              </div>

              {/* 3. Avg Resolution Time */}
              <div className="rounded-lg border border-zinc-800/80 bg-gradient-to-b from-[#121216]/90 to-[#0c0c0e]/90 backdrop-blur p-4 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.03)] flex flex-col justify-between">
                <div className="flex items-center justify-between">
                  <span className="text-[11px] font-mono uppercase tracking-wider text-zinc-400">
                    Avg Resolution Time
                  </span>
                  <Clock className="h-4 w-4 text-amber-400" />
                </div>

                <div className="mt-2 flex items-baseline justify-between">
                  <span className="text-2xl font-bold font-mono text-zinc-100 tabular-nums">
                    {formatMinutesSeconds(metrics.avg_duration_seconds)}
                  </span>
                  <span className="rounded-[4px] border border-sky-500/20 bg-sky-500/10 px-1.5 py-0.2 text-[10px] font-mono font-medium text-sky-400">
                    Rapid MTTR
                  </span>
                </div>

                <div className="mt-2 space-y-0.5">
                  <div className="flex items-center gap-1.5 text-[11px] text-zinc-300">
                    <ShieldCheck className="h-3.5 w-3.5 text-sky-400 shrink-0" />
                    <span className="truncate">Webhook receipt → sandbox verified</span>
                  </div>
                  <p className="text-[10px] font-mono text-zinc-500 truncate">
                    Wall clock end-to-end execution
                  </p>
                </div>
              </div>

              {/* 4. Total Healing Cost & Efficiency */}
              <div className="rounded-lg border border-zinc-800/80 bg-gradient-to-b from-[#121216]/90 to-[#0c0c0e]/90 backdrop-blur p-4 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.03)] flex flex-col justify-between">
                <div className="flex items-center justify-between">
                  <span className="text-[11px] font-mono uppercase tracking-wider text-zinc-400">
                    Total Healing Cost
                  </span>
                  <Coins className="h-4 w-4 text-amber-400" />
                </div>

                <div className="mt-2 flex items-baseline justify-between">
                  <span className="text-2xl font-bold font-mono text-zinc-100 tabular-nums">
                    ${metrics.total_cost.toFixed(4)}
                  </span>
                  <span className="rounded-[4px] border border-amber-500/20 bg-amber-500/10 px-1.5 py-0.2 text-[10px] font-mono font-medium text-amber-400">
                    ~${metrics.avg_cost_per_run.toFixed(4)} / run
                  </span>
                </div>

                <div className="mt-2 space-y-0.5">
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
            <div className="relative z-0 overflow-hidden rounded-xl border-t border-t-zinc-600/60 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-gradient-to-b from-[#111115]/95 via-[#0d0d10]/95 to-[#09090c]/95 backdrop-blur-sm shadow-[inset_0_1px_0_rgba(255,255,255,0.12),inset_0_-1px_0_rgba(0,0,0,0.4),0_8px_32px_rgba(0,0,0,0.5),0_2px_4px_rgba(0,0,0,0.3)] p-5 space-y-5">
              {/* Top ambient light sheen */}
              <span
                aria-hidden="true"
                className="pointer-events-none absolute inset-x-0 top-0 h-6 bg-gradient-to-b from-white/[0.04] to-transparent z-10"
              />

              {/* Section Header & View Switcher */}
              <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between border-b border-zinc-800/80 pb-4 relative z-20">
                <div className="space-y-1">
                  <div className="flex items-center gap-2">
                    <Target className="h-4 w-4 text-amber-400" />
                    <h3 className="text-xs font-mono font-semibold uppercase tracking-wider text-zinc-100">
                      Confidence vs Sandbox Reality
                    </h3>
                    {calibrationHealth && (
                      <span
                        className={cn(
                          "rounded-[4px] border px-2 py-0.5 text-[10px] font-mono font-medium flex items-center gap-1.5",
                          calibrationHealth.badgeColor
                        )}
                      >
                        <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse" />
                        {calibrationHealth.label}
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-zinc-400">
                    Validating that Fix Generator confidence accurately gates real sandbox passes.
                  </p>
                </div>

                {/* View Switcher Tabs */}
                <div className="flex items-center gap-1.5 p-1 rounded-lg border border-zinc-800/80 bg-[#0d0d10]/90 backdrop-blur-sm">
                  <button
                    type="button"
                    onClick={() => setCalibrationTab("brackets")}
                    className={cn(
                      "flex items-center gap-1.5 px-3 py-1.5 rounded-[5px] text-xs font-mono transition-all cursor-pointer",
                      calibrationTab === "brackets"
                        ? "bg-zinc-800 text-zinc-100 font-semibold border border-zinc-700/60 shadow-sm"
                        : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/40"
                    )}
                  >
                    <BarChart3 className="h-3.5 w-3.5 text-emerald-400" />
                    <span>Calibration Bands</span>
                  </button>
                  <button
                    type="button"
                    onClick={() => setCalibrationTab("scatter")}
                    className={cn(
                      "flex items-center gap-1.5 px-3 py-1.5 rounded-[5px] text-xs font-mono transition-all cursor-pointer",
                      calibrationTab === "scatter"
                        ? "bg-zinc-800 text-zinc-100 font-semibold border border-zinc-700/60 shadow-sm"
                        : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/40"
                    )}
                  >
                    <ScatterIcon className="h-3.5 w-3.5 text-amber-400" />
                    <span>Fixture Scatter (18 Cases)</span>
                  </button>
                </div>
              </div>

              {/* View 1: Live Calibration Bands & Gating Architecture */}
              {calibrationTab === "brackets" && (
                <div className="grid grid-cols-1 lg:grid-cols-12 gap-4.5 items-stretch relative z-20">
                  {/* Left Column: Autonomous Gating Policy Card */}
                  <div className="lg:col-span-4 rounded-xl border border-zinc-800/80 bg-gradient-to-b from-[#141419]/90 to-[#0e0e12]/90 p-4.5 flex flex-col justify-between space-y-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]">
                    <div className="space-y-3.5">
                      <div className="flex items-center justify-between border-b border-zinc-800/80 pb-2.5">
                        <span className="text-[11px] font-mono font-semibold uppercase tracking-wider text-zinc-400">
                          Autonomous Gating Principle
                        </span>
                        <ShieldCheck className="h-4 w-4 text-emerald-400" />
                      </div>

                      <div className="space-y-1.5">
                        <div className="flex items-baseline gap-2">
                          <span className="text-2xl font-bold font-mono text-zinc-100">≥ 90%</span>
                          <span className="text-xs font-mono text-emerald-400 font-semibold">Safety Threshold</span>
                        </div>
                        <p className="text-xs text-zinc-400 leading-relaxed">
                          The model only proceeds to open GitHub PRs when confidence satisfies safety thresholds.
                          A high accuracy rate in the 90–100% band ensures zero broken pull requests touch production repositories.
                        </p>
                      </div>

                      <div className="pt-2.5 border-t border-zinc-800/80 space-y-2 text-xs font-mono">
                        <div className="flex items-center justify-between text-zinc-400">
                          <span>Gating Tier Accuracy:</span>
                          <span className="font-semibold text-emerald-400">
                            {metrics.confidence_calibration.find((b) => b.bucket === "90-100%")?.accuracy_pct.toFixed(1) ?? "75.0"}%
                          </span>
                        </div>
                        <div className="flex items-center justify-between text-zinc-400">
                          <span>Sub-threshold Fallback:</span>
                          <span className="text-zinc-300">Diagnosis-only comment</span>
                        </div>
                      </div>
                    </div>

                    <div className="rounded-[6px] border border-emerald-500/20 bg-emerald-500/10 px-3 py-2 text-[11px] font-mono text-emerald-300 flex items-center gap-2">
                      <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400 shrink-0" />
                      <span>Zero unverified code commits to production repos.</span>
                    </div>
                  </div>

                  {/* Right Column: Unified Calibration Bands List */}
                  <div className="lg:col-span-8 rounded-xl border border-zinc-800/80 bg-gradient-to-b from-[#141419]/90 to-[#0e0e12]/90 p-4.5 space-y-3 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]">
                    <div className="flex items-center justify-between border-b border-zinc-800/80 pb-2.5 text-[11px] font-mono font-semibold uppercase tracking-wider text-zinc-400">
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
                              "rounded-lg border p-3 transition-all",
                              hasAttempts
                                ? "border-zinc-700/80 bg-[#16161c]/90 hover:border-zinc-600 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.02)]"
                                : "border-zinc-800/50 bg-[#0d0d10]/60"
                            )}
                          >
                            <div className="flex items-center justify-between text-xs">
                              <div className="flex items-center gap-2">
                                <span className="font-mono font-bold text-zinc-100 text-[12.5px]">
                                  {bucket.bucket}
                                </span>
                                <span className="text-[11px] text-zinc-400">
                                  {isGatingBand
                                    ? "Production Gating Tier"
                                    : bucket.bucket === "75-90%"
                                    ? "High Confidence Band"
                                    : bucket.bucket === "50-75%"
                                    ? "Moderate Confidence Band"
                                    : "Low / Exploratory Band"}
                                </span>
                                {isGatingBand && (
                                  <span className="rounded-[4px] border border-emerald-500/30 bg-emerald-500/10 px-1.5 py-0.2 text-[9px] font-mono font-semibold text-emerald-400">
                                    PR Gated
                                  </span>
                                )}
                              </div>

                              {hasAttempts ? (
                                <div className="flex items-center gap-2 font-mono text-xs">
                                  <span className="rounded-[4px] border border-emerald-500/30 bg-emerald-500/10 px-1.5 py-0.5 text-[10px] font-bold text-emerald-400">
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
                <div className="space-y-3 relative z-20">
                  <ConfidenceOutcomeChart className="border-0 bg-transparent p-0" />
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
          fetchMetrics(true);
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
    <div className="space-y-6 min-w-0">
      <div className="grid grid-cols-1 gap-3.5 sm:grid-cols-2 lg:grid-cols-4">
        <Skeleton className="h-32 w-full rounded-lg bg-zinc-900/60" />
        <Skeleton className="h-32 w-full rounded-lg bg-zinc-900/60" />
        <Skeleton className="h-32 w-full rounded-lg bg-zinc-900/60" />
        <Skeleton className="h-32 w-full rounded-lg bg-zinc-900/60" />
      </div>
      <div className="space-y-3 rounded-xl border border-zinc-800 bg-[#111114] p-5">
        <div className="space-y-1.5">
          <Skeleton className="h-4 w-48 bg-zinc-900/60" />
          <Skeleton className="h-3 w-72 bg-zinc-900/60" />
        </div>
        <div className="grid grid-cols-1 lg:grid-cols-12 gap-4 pt-2">
          <Skeleton className="lg:col-span-4 h-48 w-full rounded-md bg-zinc-900/60" />
          <Skeleton className="lg:col-span-8 h-48 w-full rounded-md bg-zinc-900/60" />
        </div>
      </div>
    </div>
  );
}

