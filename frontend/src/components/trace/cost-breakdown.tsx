import { TraceOut } from "@/lib/api";
import { formatCost, formatLatency, formatNumber } from "@/lib/utils";
import { DollarSign, Clock, Layers, Cpu, ArrowUpRight, CheckCircle2 } from "lucide-react";

interface CostBreakdownProps {
  trace: TraceOut;
}

export function CostBreakdown({ trace }: CostBreakdownProps) {
  const totalInputTokens = trace.steps.reduce(
    (acc, s) => acc + (s.input_tokens || 0),
    0
  );
  const totalOutputTokens = trace.steps.reduce(
    (acc, s) => acc + (s.output_tokens || 0),
    0
  );
  const totalTokens = totalInputTokens + totalOutputTokens;

  const hasPassedAttempt = trace.attempts.some(
    (a) => a.verification_status === "pass" || a.verification_status === "completed"
  );

  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-3.5">
      {/* Total Cost */}
      <div className="group relative overflow-hidden rounded-[8px] border-t border-t-zinc-700/60 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-gradient-to-b from-[#141418]/95 via-[#101013]/95 to-[#0a0a0d]/95 p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_3px_12px_rgba(0,0,0,0.35)] transition-all duration-150 hover:border-t-zinc-600/80 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.1),0_4px_16px_rgba(0,0,0,0.45)]">
        <span
          aria-hidden="true"
          className="pointer-events-none absolute inset-x-0 top-0 h-[1px] bg-gradient-to-r from-transparent via-amber-400/20 to-transparent"
        />
        <div className="flex items-center justify-between text-zinc-400">
          <span className="text-[10.5px] font-mono font-semibold uppercase tracking-wider text-zinc-400">
            Total Cost
          </span>
          <div className="flex h-6 w-6 items-center justify-center rounded-[5px] bg-amber-500/10 border border-amber-500/30 text-amber-400 shadow-[inset_0_1px_0_rgba(255,255,255,0.08)]">
            <DollarSign className="h-3.5 w-3.5" />
          </div>
        </div>
        <div className="mt-2.5 flex items-baseline gap-1.5">
          <span className="font-mono text-2xl font-bold tracking-tight text-zinc-100">
            {formatCost(trace.total_cost)}
          </span>
          <span className="text-[10px] text-zinc-500 font-mono">USD</span>
        </div>
        <div className="mt-1.5 flex items-center justify-between text-[10.5px] text-zinc-500 font-mono">
          <span>Across pipeline steps</span>
          {trace.steps.length > 0 && (
            <span className="text-zinc-400 text-[10px] bg-zinc-900/80 border border-zinc-800 px-1.5 py-0.2 rounded-[4px]">
              {formatCost(trace.total_cost / trace.steps.length)}/step
            </span>
          )}
        </div>
      </div>

      {/* Total Latency */}
      <div className="group relative overflow-hidden rounded-[8px] border-t border-t-zinc-700/60 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-gradient-to-b from-[#141418]/95 via-[#101013]/95 to-[#0a0a0d]/95 p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_3px_12px_rgba(0,0,0,0.35)] transition-all duration-150 hover:border-t-zinc-600/80 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.1),0_4px_16px_rgba(0,0,0,0.45)]">
        <span
          aria-hidden="true"
          className="pointer-events-none absolute inset-x-0 top-0 h-[1px] bg-gradient-to-r from-transparent via-cyan-400/20 to-transparent"
        />
        <div className="flex items-center justify-between text-zinc-400">
          <span className="text-[10.5px] font-mono font-semibold uppercase tracking-wider text-zinc-400">
            Total Latency
          </span>
          <div className="flex h-6 w-6 items-center justify-center rounded-[5px] bg-cyan-500/10 border border-cyan-500/30 text-cyan-400 shadow-[inset_0_1px_0_rgba(255,255,255,0.06)]">
            <Clock className="h-3.5 w-3.5" />
          </div>
        </div>
        <div className="mt-2.5 flex items-baseline gap-1.5">
          <span className="font-mono text-2xl font-bold tracking-tight text-zinc-100">
            {formatLatency(trace.total_latency_ms)}
          </span>
        </div>
        <div className="mt-1.5 flex items-center justify-between text-[10.5px] text-zinc-500 font-mono">
          <span>End-to-end wall clock</span>
          <span className="text-zinc-400 text-[10px] bg-zinc-900/80 border border-zinc-800 px-1.5 py-0.2 rounded-[4px]">
            {trace.steps.length} {trace.steps.length === 1 ? "step" : "steps"}
          </span>
        </div>
      </div>

      {/* Token Volume */}
      <div className="group relative overflow-hidden rounded-[8px] border-t border-t-zinc-700/60 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-gradient-to-b from-[#141418]/95 via-[#101013]/95 to-[#0a0a0d]/95 p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_3px_12px_rgba(0,0,0,0.35)] transition-all duration-150 hover:border-t-zinc-600/80 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.1),0_4px_16px_rgba(0,0,0,0.45)]">
        <span
          aria-hidden="true"
          className="pointer-events-none absolute inset-x-0 top-0 h-[1px] bg-gradient-to-r from-transparent via-purple-400/20 to-transparent"
        />
        <div className="flex items-center justify-between text-zinc-400">
          <span className="text-[10.5px] font-mono font-semibold uppercase tracking-wider text-zinc-400">
            Total Tokens
          </span>
          <div className="flex h-6 w-6 items-center justify-center rounded-[5px] bg-purple-500/10 border border-purple-500/30 text-purple-400 shadow-[inset_0_1px_0_rgba(255,255,255,0.06)]">
            <Cpu className="h-3.5 w-3.5" />
          </div>
        </div>
        <div className="mt-2.5 flex items-baseline gap-1.5">
          <span className="font-mono text-2xl font-bold tracking-tight text-zinc-100">
            {formatNumber(totalTokens)}
          </span>
          <span className="text-[10px] text-zinc-500 font-mono">tok</span>
        </div>
        <div className="mt-1.5 flex items-center justify-between text-[10.5px] text-zinc-500 font-mono">
          <span>Inference token volume</span>
          {totalTokens > 0 && (
            <span className="text-zinc-400 text-[10px] bg-zinc-900/80 border border-zinc-800 px-1.5 py-0.2 rounded-[4px]">
              {formatNumber(totalInputTokens)} / {formatNumber(totalOutputTokens)}
            </span>
          )}
        </div>
      </div>

      {/* Attempts */}
      <div className="group relative overflow-hidden rounded-[8px] border-t border-t-zinc-700/60 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-gradient-to-b from-[#141418]/95 via-[#101013]/95 to-[#0a0a0d]/95 p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_3px_12px_rgba(0,0,0,0.35)] transition-all duration-150 hover:border-t-zinc-600/80 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.1),0_4px_16px_rgba(0,0,0,0.45)]">
        <span
          aria-hidden="true"
          className="pointer-events-none absolute inset-x-0 top-0 h-[1px] bg-gradient-to-r from-transparent via-emerald-400/20 to-transparent"
        />
        <div className="flex items-center justify-between text-zinc-400">
          <span className="text-[10.5px] font-mono font-semibold uppercase tracking-wider text-zinc-400">
            Attempts
          </span>
          <div className="flex h-6 w-6 items-center justify-center rounded-[5px] bg-emerald-500/10 border border-emerald-500/30 text-emerald-400 shadow-[inset_0_1px_0_rgba(255,255,255,0.06)]">
            <Layers className="h-3.5 w-3.5" />
          </div>
        </div>
        <div className="mt-2.5 flex items-baseline gap-1.5">
          <span className="font-mono text-2xl font-bold tracking-tight text-zinc-100">
            {trace.attempts.length}
          </span>
        </div>
        <div className="mt-1.5 flex items-center justify-between text-[10.5px] text-zinc-500 font-mono">
          <span>Sandbox verify cycles</span>
          {hasPassedAttempt ? (
            <span className="inline-flex items-center gap-1 text-emerald-400 text-[10px] bg-emerald-950/40 border border-emerald-800/50 px-1.5 py-0.2 rounded-[4px]">
              <CheckCircle2 className="h-2.5 w-2.5" />
              Verified
            </span>
          ) : (
            <span className="text-zinc-500 text-[10px] bg-zinc-900/80 border border-zinc-800 px-1.5 py-0.2 rounded-[4px]">
              {trace.attempts.length === 1 ? "1 cycle" : `${trace.attempts.length} cycles`}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
