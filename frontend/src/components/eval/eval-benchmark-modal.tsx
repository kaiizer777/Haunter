"use client";

import { useState } from "react";
import { Modal } from "@/components/ui/modal";
import { Button } from "@/components/ui/button";
import { api, EvalResultOut, ApiError } from "@/lib/api";
import {
  Zap,
  Play,
  Loader2,
  CheckCircle2,
  AlertCircle,
  ShieldCheck,
} from "lucide-react";

interface EvalBenchmarkModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSuccess: () => void;
}

export function EvalBenchmarkModal({
  isOpen,
  onClose,
  onSuccess,
}: EvalBenchmarkModalProps) {
  const [benchmarkMode, setBenchmarkMode] = useState<"demo" | "dry_run">("demo");
  const [isRunning, setIsRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<EvalResultOut | null>(null);

  const handleRun = async () => {
    setIsRunning(true);
    setError(null);
    setResult(null);

    try {
      const payload =
        benchmarkMode === "demo"
          ? { demo_mode: true }
          : { dry_run: true };

      const res = await api.runEval(payload);
      setResult(res);
      onSuccess();
    } catch (err: unknown) {
      if (err instanceof ApiError) {
        if (err.status === 403) {
          setError("Admin privileges required to trigger server-side eval benchmarks.");
        } else {
          setError(err.message);
        }
      } else if (err instanceof Error) {
        setError(err.message);
      } else {
        setError("Failed to execute benchmark run.");
      }
    } finally {
      setIsRunning(false);
    }
  };

  const handleClose = () => {
    if (isRunning) return;
    setError(null);
    setResult(null);
    onClose();
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={handleClose}
      title="Autonomous Eval Benchmark Runner"
      description="Execute the standardized evaluation harness against server-side golden CI failure fixtures."
      className="max-w-lg"
    >
      <div className="space-y-4 text-xs">
        {/* Mode Selector */}
        <div className="space-y-2">
          <label className="text-[11px] font-mono uppercase tracking-wider text-zinc-400">
            Select Evaluation Strategy
          </label>

          <div className="grid grid-cols-1 gap-2.5">
            {/* Option 1: Demo Smoke */}
            <div
              onClick={() => !isRunning && setBenchmarkMode("demo")}
              className={`cursor-pointer rounded-[6px] border p-3 transition-all ${
                benchmarkMode === "demo"
                  ? "border-amber-500/80 bg-amber-950/20 shadow-[0_0_12px_rgba(245,158,11,0.12)]"
                  : "border-zinc-800 bg-[#0d0d10] hover:border-zinc-700"
              }`}
            >
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <Zap className="h-4 w-4 text-amber-400" />
                  <span className="font-semibold text-zinc-100 font-mono">
                    Canonical Demo Smoke
                  </span>
                </div>
                <span className="rounded-full border border-amber-500/30 bg-amber-500/10 px-2 py-0.5 text-[10px] font-mono text-amber-400">
                  Live LLM
                </span>
              </div>
              <p className="mt-1 text-zinc-400 text-[11px] leading-relaxed">
                Pins evaluation to canonical fixture (psf/requests import error).
                Exercises the autonomous diagnosis and patch generator end-to-end.
              </p>
            </div>

            {/* Option 2: Dry Run Suite */}
            <div
              onClick={() => !isRunning && setBenchmarkMode("dry_run")}
              className={`cursor-pointer rounded-[6px] border p-3 transition-all ${
                benchmarkMode === "dry_run"
                  ? "border-emerald-500/80 bg-emerald-950/20 shadow-[0_0_12px_rgba(16,185,129,0.12)]"
                  : "border-zinc-800 bg-[#0d0d10] hover:border-zinc-700"
              }`}
            >
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <ShieldCheck className="h-4 w-4 text-emerald-400" />
                  <span className="font-semibold text-zinc-100 font-mono">
                    Full Golden Fixture Suite (Dry Run)
                  </span>
                </div>
                <span className="rounded-full border border-zinc-700 bg-zinc-800/60 px-2 py-0.5 text-[10px] font-mono text-zinc-300">
                  Zero Cost
                </span>
              </div>
              <p className="mt-1 text-zinc-400 text-[11px] leading-relaxed">
                Evaluates all golden failure fixtures with deterministic stubs.
                Verifies harness contracts, scoring aggregators, and calibration.
              </p>
            </div>
          </div>
        </div>

        {/* Status / Error / Success Messages */}
        {error && (
          <div className="rounded-[6px] border border-red-900/60 bg-red-950/30 p-3 text-red-300 flex items-start gap-2">
            <AlertCircle className="h-4 w-4 text-red-400 shrink-0 mt-0.5" />
            <div className="space-y-1">
              <span className="font-semibold font-mono text-[11px]">Benchmark Error</span>
              <p className="text-[11px] leading-tight">{error}</p>
            </div>
          </div>
        )}

        {result && (
          <div className="rounded-[6px] border border-emerald-900/60 bg-emerald-950/30 p-3 text-emerald-300 flex items-start gap-2">
            <CheckCircle2 className="h-4 w-4 text-emerald-400 shrink-0 mt-0.5" />
            <div className="space-y-1">
              <span className="font-semibold font-mono text-[11px]">Benchmark Completed!</span>
              <p className="text-[11px]">
                Accuracy:{" "}
                <strong className="text-emerald-400 font-mono">
                  {result.overall_accuracy !== null && result.overall_accuracy !== undefined
                    ? `${(result.overall_accuracy * 100).toFixed(1)}%`
                    : "100%"}
                </strong>
                {result.passed_fixtures !== undefined && result.total_fixtures !== undefined && (
                  <span className="ml-1 text-zinc-400">
                    ({result.passed_fixtures}/{result.total_fixtures} passed)
                  </span>
                )}
              </p>
              <p className="text-[10px] font-mono text-zinc-500">Run ID: {result.id}</p>
            </div>
          </div>
        )}

        {/* Footer Actions */}
        <div className="flex items-center justify-end gap-2.5 pt-2 border-t border-zinc-800/80">
          <Button
            variant="ghost"
            size="sm"
            onClick={handleClose}
            disabled={isRunning}
            className="text-zinc-400 hover:text-zinc-200"
          >
            {result ? "Close" : "Cancel"}
          </Button>

          <Button
            size="sm"
            onClick={handleRun}
            disabled={isRunning}
            className="bg-amber-500 hover:bg-amber-400 text-zinc-950 font-medium font-mono text-xs px-4"
          >
            {isRunning ? (
              <>
                <Loader2 className="h-3.5 w-3.5 animate-spin mr-1.5" />
                Executing Pipeline...
              </>
            ) : (
              <>
                <Play className="h-3.5 w-3.5 fill-current mr-1.5" />
                Trigger Benchmark
              </>
            )}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
