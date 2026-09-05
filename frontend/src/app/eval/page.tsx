"use client";

import { useEffect, useState, useCallback, useTransition } from "react";
import { useRouter } from "next/navigation";
import { AppLayout } from "@/components/layout/app-layout";
import { ConfidenceOutcomeChart, AttemptDataPoint } from "@/components/eval/confidence-chart";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Modal } from "@/components/ui/modal";
import { Switch } from "@/components/ui/switch";
import { useAuth } from "@/lib/auth-context";
import { api, EvalResultOut, ApiError } from "@/lib/api";
import { formatRelativeTime } from "@/lib/utils";
import {
  Sparkles,
  Play,
  CheckCircle2,
  XCircle,
  Clock,
  Cpu,
  AlertCircle,
  History,
  TerminalSquare,
  FlaskConical
} from "lucide-react";

// localStorage key for the demo-mode toggle. Persists across page reloads
// so the operator doesn't have to re-enable it every time.
const DEMO_MODE_STORAGE_KEY = "haunter.eval.demoMode";

function readDemoModeFromStorage(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(DEMO_MODE_STORAGE_KEY) === "true";
  } catch {
    return false;
  }
}

function writeDemoModeToStorage(value: boolean): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(DEMO_MODE_STORAGE_KEY, value ? "true" : "false");
  } catch {
    // localStorage may be unavailable (private mode, quota). Swallow.
  }
}

export default function EvalPage() {
  const router = useRouter();
  const { user, loading: authLoading } = useAuth();

  const [evalResults, setEvalResults] = useState<EvalResultOut[]>([]);
  const [selectedEval, setSelectedEval] = useState<EvalResultOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Trigger modal state
  const [isRunModalOpen, setIsRunModalOpen] = useState(false);
  const [isDryRun, setIsDryRun] = useState(true);
  // Demo mode: pinned to a known-fixable canonical fixture + default model.
  // Persisted to localStorage so it survives page reloads.
  const [isDemoMode, setIsDemoMode] = useState<boolean>(false);
  const [isPending, startTransition] = useTransition();
  const [runError, setRunError] = useState<string | null>(null);

  // Hydrate demo-mode toggle from localStorage after mount.
  useEffect(() => {
    setIsDemoMode(readDemoModeFromStorage());
  }, []);

  const handleDemoModeChange = useCallback((next: boolean) => {
    setIsDemoMode(next);
    writeDemoModeToStorage(next);
    // Demo mode forces live LLM execution — keep the dry-run checkbox in sync
    // so the operator sees the effective mode in the modal.
    if (next) setIsDryRun(false);
  }, []);

  // Security gate: non-admin users must NEVER see this view (WORK.md:251)
  useEffect(() => {
    if (!authLoading && (!user || !user.is_admin)) {
      router.replace("/runs");
    }
  }, [user, authLoading, router]);

  const fetchEvalResults = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.getEvalResults();
      setEvalResults(data);
      if (data.length > 0) {
        setSelectedEval(data[0]);
      }
    } catch (err: unknown) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Failed to load evaluation harness benchmark data.");
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (user?.is_admin) {
      fetchEvalResults();
    }
  }, [user, fetchEvalResults]);

  const handleTriggerRun = () => {
    setRunError(null);
    startTransition(async () => {
      try {
        const newResult = await api.runEval({
          dry_run: isDryRun,
          demo_mode: isDemoMode,
        });
        setEvalResults((prev) => [newResult, ...prev]);
        setSelectedEval(newResult);
        setIsRunModalOpen(false);
      } catch (err: unknown) {
        if (err instanceof ApiError) {
          setRunError(err.message);
        } else {
          setRunError("Failed to trigger evaluation harness run.");
        }
      }
    });
  };

  // Convert selected eval fixture scores to attempt data points for chart
  const chartPoints: AttemptDataPoint[] | undefined = selectedEval?.fixture_scores?.map(
    (fs) => ({
      confidence: Math.round(fs.fix_score * 100),
      passed: fs.context_score >= 0.5 && fs.fix_score >= 0.7,
      attempt_number: 1,
      label: `${fs.fixture_id}`,
    })
  );

  // While checking auth, display skeleton
  if (authLoading || !user?.is_admin) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-[#09090b]">
        <div className="flex flex-col items-center gap-3">
          <div className="h-7 w-7 rounded-[5px] bg-zinc-900 border border-zinc-800 animate-pulse" />
          <Skeleton className="h-4 w-32" />
        </div>
      </div>
    );
  }

  return (
    <AppLayout
      title="Golden Eval Harness"
      subtitle="Benchmark autonomous failure diagnosis & fix accuracy against golden test cases"
      actions={
        <div className="flex items-center gap-4">
          <Switch
            checked={isDemoMode}
            onCheckedChange={handleDemoModeChange}
            label={
              <span className="inline-flex items-center gap-1">
                <FlaskConical className="h-3 w-3 text-amber-400" />
                Demo mode
              </span>
            }
            description={isDemoMode ? "Pinned" : "Off"}
            tooltip="Pins the eval to a known-fixable canonical failure. Use for demos and CI."
          />
          <Button
            onClick={() => setIsRunModalOpen(true)}
            className="flex items-center gap-1.5 bg-amber-400 text-zinc-950 hover:bg-amber-300 font-semibold text-xs"
            size="sm"
          >
            <Play className="h-3.5 w-3.5 fill-current" />
            Run Eval Harness
          </Button>
        </div>
      }
    >
      <div className="space-y-6">
        {error && (
          <div className="flex items-center gap-2 rounded-[6px] border border-red-900/60 bg-red-950/30 p-3.5 text-xs text-red-300">
            <AlertCircle className="h-4 w-4 shrink-0 text-red-400" />
            <span>{error}</span>
          </div>
        )}

        {loading ? (
          <div className="space-y-4">
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <Skeleton className="h-28 w-full" />
              <Skeleton className="h-28 w-full" />
              <Skeleton className="h-28 w-full" />
              <Skeleton className="h-28 w-full" />
            </div>
            <Skeleton className="h-72 w-full" />
            <Skeleton className="h-64 w-full" />
          </div>
        ) : evalResults.length === 0 ? (
          <div className="p-12 text-center border border-dashed border-zinc-800/80 rounded-[6px] bg-[#121215]/50">
            <Sparkles className="h-8 w-8 mx-auto text-amber-400/80 mb-3" />
            <h3 className="text-sm font-semibold text-zinc-200">No evaluation runs recorded</h3>
            <p className="text-xs text-zinc-500 mt-1 max-w-md mx-auto">
              Run the golden benchmark harness to evaluate subagent root-cause matching, fix generator accuracy, and confidence calibration.
            </p>
            <Button
              onClick={() => setIsRunModalOpen(true)}
              variant="outline"
              size="sm"
              className="mt-4"
            >
              <Play className="h-3.5 w-3.5 mr-1.5 fill-current text-amber-400" />
              Trigger First Benchmark Run
            </Button>
          </div>
        ) : (
          <>
            {/* Top Run Switcher Bar */}
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-[6px] border border-zinc-800 bg-[#121215] px-4 py-3">
              <div className="flex items-center gap-3">
                <History className="h-4 w-4 text-zinc-400" />
                <span className="text-xs font-mono font-semibold uppercase tracking-wider text-zinc-400">
                  Select Benchmark Run:
                </span>
                <select
                  value={selectedEval?.id || ""}
                  onChange={(e) => {
                    const found = evalResults.find((r) => r.id === e.target.value);
                    if (found) setSelectedEval(found);
                  }}
                  className="rounded-[4px] border border-zinc-700 bg-zinc-900 px-3 py-1 text-xs font-mono text-zinc-200 focus:outline-none focus:border-amber-400 cursor-pointer"
                >
                  {evalResults.map((r) => (
                    <option key={r.id} value={r.id}>
                      {new Date(r.created_at).toLocaleDateString()} — {r.mode || "BENCHMARK"} ({(r.overall_accuracy ? (r.overall_accuracy * 100).toFixed(1) : "0.0")}% acc)
                    </option>
                  ))}
                </select>
              </div>

              {selectedEval && (
                <div className="flex items-center gap-2 font-mono text-[11px] text-zinc-400">
                  <span className="rounded-[3px] border border-zinc-800 bg-zinc-900 px-2 py-0.5 text-zinc-300">
                    Mode: {selectedEval.mode || "DRY-RUN"}
                  </span>
                  <span className="flex items-center gap-1">
                    <Clock className="h-3 w-3 text-zinc-500" />
                    {formatRelativeTime(selectedEval.created_at)}
                  </span>
                </div>
              )}
            </div>

            {/* Scorecard KPI Cards */}
            {selectedEval && (
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
                {/* 1. Overall Accuracy */}
                <div className="rounded-[6px] border border-zinc-800 bg-[#121215] p-4 space-y-2">
                  <div className="flex items-center justify-between text-[11px] font-mono text-zinc-400">
                    <span className="uppercase tracking-wider">Overall Accuracy</span>
                    <Sparkles className="h-3.5 w-3.5 text-amber-400" />
                  </div>
                  <div className="flex items-baseline gap-2">
                    <span className="text-2xl font-bold font-mono text-amber-400 tracking-tight">
                      {selectedEval.overall_accuracy !== null
                        ? `${(selectedEval.overall_accuracy * 100).toFixed(1)}%`
                        : "—"}
                    </span>
                    <span className="text-[10px] font-mono text-zinc-500">
                      Harmonic Mean
                    </span>
                  </div>
                  <div className="h-1.5 w-full rounded-full bg-zinc-900 overflow-hidden border border-zinc-800">
                    <div
                      className="h-full bg-amber-400"
                      style={{
                        width: `${Math.min(
                          100,
                          (selectedEval.overall_accuracy || 0) * 100
                        )}%`,
                      }}
                    />
                  </div>
                </div>

                {/* 2. Golden Fixtures Pass Rate */}
                <div className="rounded-[6px] border border-zinc-800 bg-[#121215] p-4 space-y-2">
                  <div className="flex items-center justify-between text-[11px] font-mono text-zinc-400">
                    <span className="uppercase tracking-wider">Golden Set Pass Rate</span>
                    <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400" />
                  </div>
                  <div className="flex items-baseline gap-2">
                    <span className="text-2xl font-bold font-mono text-emerald-400 tracking-tight">
                      {selectedEval.overall_pass_rate !== null
                        ? `${(selectedEval.overall_pass_rate * 100).toFixed(1)}%`
                        : "—"}
                    </span>
                    <span className="text-[10px] font-mono text-zinc-400">
                      ({selectedEval.passed_fixtures ?? "?"}/{selectedEval.total_fixtures ?? "?"} fixtures)
                    </span>
                  </div>
                  <div className="h-1.5 w-full rounded-full bg-zinc-900 overflow-hidden border border-zinc-800">
                    <div
                      className="h-full bg-emerald-400"
                      style={{
                        width: `${Math.min(
                          100,
                          (selectedEval.overall_pass_rate || 0) * 100
                        )}%`,
                      }}
                    />
                  </div>
                </div>

                {/* 3. Context Gatherer Root-Cause Match */}
                <div className="rounded-[6px] border border-zinc-800 bg-[#121215] p-4 space-y-2">
                  <div className="flex items-center justify-between text-[11px] font-mono text-zinc-400">
                    <span className="uppercase tracking-wider">Context Gatherer</span>
                    <TerminalSquare className="h-3.5 w-3.5 text-zinc-400" />
                  </div>
                  <div className="flex items-baseline gap-2">
                    <span className="text-2xl font-bold font-mono text-zinc-100 tracking-tight">
                      {selectedEval.context_gatherer_avg !== null
                        ? `${(selectedEval.context_gatherer_avg * 100).toFixed(1)}%`
                        : "—"}
                    </span>
                    <span className="text-[10px] font-mono text-zinc-500">
                      Keyword Match
                    </span>
                  </div>
                  <div className="h-1.5 w-full rounded-full bg-zinc-900 overflow-hidden border border-zinc-800">
                    <div
                      className="h-full bg-zinc-300"
                      style={{
                        width: `${Math.min(
                          100,
                          (selectedEval.context_gatherer_avg || 0) * 100
                        )}%`,
                      }}
                    />
                  </div>
                </div>

                {/* 4. Fix Generator Confidence */}
                <div className="rounded-[6px] border border-zinc-800 bg-[#121215] p-4 space-y-2">
                  <div className="flex items-center justify-between text-[11px] font-mono text-zinc-400">
                    <span className="uppercase tracking-wider">Fix Generator</span>
                    <Cpu className="h-3.5 w-3.5 text-zinc-400" />
                  </div>
                  <div className="flex items-baseline gap-2">
                    <span className="text-2xl font-bold font-mono text-zinc-100 tracking-tight">
                      {selectedEval.fix_generator_avg !== null
                        ? `${(selectedEval.fix_generator_avg * 100).toFixed(1)}%`
                        : "—"}
                    </span>
                    <span className="text-[10px] font-mono text-zinc-500">
                      Mean Confidence
                    </span>
                  </div>
                  <div className="h-1.5 w-full rounded-full bg-zinc-900 overflow-hidden border border-zinc-800">
                    <div
                      className="h-full bg-zinc-300"
                      style={{
                        width: `${Math.min(
                          100,
                          (selectedEval.fix_generator_avg || 0) * 100
                        )}%`,
                      }}
                    />
                  </div>
                </div>
              </div>
            )}

            {/* Confidence vs. Sandbox Outcome Chart */}
            <ConfidenceOutcomeChart data={chartPoints} />

            {/* Per-Fixture Breakdown Table */}
            {selectedEval?.fixture_scores && selectedEval.fixture_scores.length > 0 && (
              <div className="rounded-[6px] border border-zinc-800 bg-[#121215] overflow-hidden space-y-3 p-4">
                <div className="flex items-center justify-between border-b border-zinc-800 pb-3">
                  <div>
                    <h3 className="text-xs font-mono font-semibold uppercase tracking-wider text-zinc-200">
                      Per-Fixture Benchmark Breakdown
                    </h3>
                    <p className="text-[11px] font-mono text-zinc-500 mt-0.5">
                      Subagent evaluation metrics across individual golden cases
                    </p>
                  </div>
                </div>

                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="w-[40%]">Fixture ID / Case</TableHead>
                      <TableHead className="w-[20%]">Context Match %</TableHead>
                      <TableHead className="w-[20%]">Fix Confidence %</TableHead>
                      <TableHead className="w-[20%] text-right">Result</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {selectedEval.fixture_scores.map((fs) => {
                      const isPassed = fs.context_score >= 0.5 && fs.fix_score >= 0.7;
                      return (
                        <TableRow key={fs.fixture_id}>
                          <TableCell className="font-mono text-xs font-semibold text-zinc-200">
                            {fs.fixture_id}
                          </TableCell>
                          <TableCell className="font-mono text-xs text-zinc-300">
                            {(fs.context_score * 100).toFixed(1)}%
                          </TableCell>
                          <TableCell className="font-mono text-xs text-zinc-300">
                            {(fs.fix_score * 100).toFixed(1)}%
                          </TableCell>
                          <TableCell className="text-right">
                            {isPassed ? (
                              <span className="inline-flex items-center gap-1 rounded-[4px] border border-emerald-800/80 bg-emerald-950/40 px-2 py-0.5 text-[11px] font-mono font-medium text-emerald-400">
                                <CheckCircle2 className="h-3 w-3" /> PASS
                              </span>
                            ) : (
                              <span className="inline-flex items-center gap-1 rounded-[4px] border border-zinc-700 bg-zinc-800/60 px-2 py-0.5 text-[11px] font-mono font-medium text-zinc-400">
                                <XCircle className="h-3 w-3" /> FAIL
                              </span>
                            )}
                          </TableCell>
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
              </div>
            )}
          </>
        )}
      </div>

      {/* Trigger Eval Run Modal */}
      <Modal
        isOpen={isRunModalOpen}
        onClose={() => setIsRunModalOpen(false)}
        title="Execute Golden Eval Harness"
      >
        <div className="space-y-4 text-xs">
          <p className="text-zinc-400 leading-relaxed font-mono">
            Execute the automated evaluation runner against the curated server allowlist of golden test fixtures.
          </p>

          {runError && (
            <div className="rounded-[4px] border border-red-900/80 bg-red-950/40 p-2.5 text-xs text-red-300">
              {runError}
            </div>
          )}

          {isDemoMode && (
            <div className="rounded-[5px] border border-amber-500/30 bg-amber-950/20 p-2.5 text-[11px] font-mono text-amber-200">
              <div className="flex items-center gap-1.5 text-amber-300 font-semibold">
                <FlaskConical className="h-3 w-3" />
                Demo mode active
              </div>
              <p className="mt-1 leading-relaxed text-amber-200/80">
                Pinned to <span className="text-amber-100">fixture-001</span> and the
                default model. Dry Run is disabled so the LLM is exercised.
              </p>
            </div>
          )}

          <div className="space-y-3 rounded-[5px] border border-zinc-800 bg-[#09090b] p-3 font-mono">
            <div className="flex items-center justify-between">
              <div>
                <span className="font-medium text-zinc-200 block">Dry Run Execution</span>
                <span className="text-[11px] text-zinc-500">
                  Use deterministic fixture stubs (fast, zero LLM tokens used)
                </span>
              </div>
              <input
                type="checkbox"
                checked={isDryRun}
                disabled={isDemoMode}
                onChange={(e) => setIsDryRun(e.target.checked)}
                className="h-4 w-4 rounded bg-zinc-900 border-zinc-700 text-amber-400 focus:ring-0 cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
              />
            </div>
          </div>

          <div className="flex items-center justify-end gap-2 pt-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => setIsRunModalOpen(false)}
              disabled={isPending}
            >
              Cancel
            </Button>
            <Button
              size="sm"
              onClick={handleTriggerRun}
              disabled={isPending}
              className="bg-amber-400 text-zinc-950 hover:bg-amber-300 font-semibold"
            >
              {isPending ? "Executing Harness..." : "Start Benchmark"}
            </Button>
          </div>
        </div>
      </Modal>
    </AppLayout>
  );
}
