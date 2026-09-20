import { TraceOut } from "@/lib/api";
import { formatCost, formatLatency, formatNumber } from "@/lib/utils";
import { DollarSign, Clock, Layers, Cpu } from "lucide-react";

interface CostBreakdownProps {
  trace: TraceOut;
}

export function CostBreakdown({ trace }: CostBreakdownProps) {
  const totalTokens = trace.steps.reduce(
    (acc, s) => acc + (s.input_tokens || 0) + (s.output_tokens || 0),
    0
  );

  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-3.5">
      {/* Total Cost */}
      <div className="group relative overflow-hidden rounded-[8px] border-t border-t-zinc-700/60 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-gradient-to-b from-[#141418]/95 via-[#101013]/95 to-[#0a0a0d]/95 p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_4px_16px_rgba(0,0,0,0.35)] transition-all duration-150 hover:border-t-zinc-600/80 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.1),0_6px_20px_rgba(0,0,0,0.45)]">
        <span
          aria-hidden="true"
          className="pointer-events-none absolute inset-x-0 top-0 h-[1px] bg-gradient-to-r from-transparent via-white/10 to-transparent"
        />
        <div className="flex items-center justify-between text-zinc-400">
          <span className="text-[10.5px] font-mono font-semibold uppercase tracking-wider text-zinc-400">
            Total Cost
          </span>
          <div className="flex h-6 w-6 items-center justify-center rounded-[5px] bg-amber-500/10 border border-amber-500/20 text-amber-400 shadow-[inset_0_1px_0_rgba(255,255,255,0.08)]">
            <DollarSign className="h-3.5 w-3.5" />
          </div>
        </div>
        <div className="mt-2.5 flex items-baseline gap-1.5">
          <span className="font-mono text-xl font-bold tracking-tight text-zinc-100">
            {formatCost(trace.total_cost)}
          </span>
          <span className="text-[10px] text-zinc-500 font-mono">USD</span>
        </div>
        <p className="mt-1 text-[10.5px] text-zinc-500 font-mono">
          Across pipeline steps
        </p>
      </div>

      {/* Total Latency */}
      <div className="group relative overflow-hidden rounded-[8px] border-t border-t-zinc-700/60 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-gradient-to-b from-[#141418]/95 via-[#101013]/95 to-[#0a0a0d]/95 p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_4px_16px_rgba(0,0,0,0.35)] transition-all duration-150 hover:border-t-zinc-600/80 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.1),0_6px_20px_rgba(0,0,0,0.45)]">
        <span
          aria-hidden="true"
          className="pointer-events-none absolute inset-x-0 top-0 h-[1px] bg-gradient-to-r from-transparent via-white/10 to-transparent"
        />
        <div className="flex items-center justify-between text-zinc-400">
          <span className="text-[10.5px] font-mono font-semibold uppercase tracking-wider text-zinc-400">
            Total Latency
          </span>
          <div className="flex h-6 w-6 items-center justify-center rounded-[5px] bg-zinc-900/90 border border-zinc-800 text-zinc-400 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]">
            <Clock className="h-3.5 w-3.5" />
          </div>
        </div>
        <div className="mt-2.5 flex items-baseline gap-1.5">
          <span className="font-mono text-xl font-bold tracking-tight text-zinc-100">
            {formatLatency(trace.total_latency_ms)}
          </span>
        </div>
        <p className="mt-1 text-[10.5px] text-zinc-500 font-mono">
          End-to-end wall clock
        </p>
      </div>

      {/* Token Volume */}
      <div className="group relative overflow-hidden rounded-[8px] border-t border-t-zinc-700/60 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-gradient-to-b from-[#141418]/95 via-[#101013]/95 to-[#0a0a0d]/95 p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_4px_16px_rgba(0,0,0,0.35)] transition-all duration-150 hover:border-t-zinc-600/80 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.1),0_6px_20px_rgba(0,0,0,0.45)]">
        <span
          aria-hidden="true"
          className="pointer-events-none absolute inset-x-0 top-0 h-[1px] bg-gradient-to-r from-transparent via-white/10 to-transparent"
        />
        <div className="flex items-center justify-between text-zinc-400">
          <span className="text-[10.5px] font-mono font-semibold uppercase tracking-wider text-zinc-400">
            Total Tokens
          </span>
          <div className="flex h-6 w-6 items-center justify-center rounded-[5px] bg-zinc-900/90 border border-zinc-800 text-zinc-400 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]">
            <Cpu className="h-3.5 w-3.5" />
          </div>
        </div>
        <div className="mt-2.5 flex items-baseline gap-1.5">
          <span className="font-mono text-xl font-bold tracking-tight text-zinc-100">
            {formatNumber(totalTokens)}
          </span>
          <span className="text-[10px] text-zinc-500 font-mono">tok</span>
        </div>
        <p className="mt-1 text-[10.5px] text-zinc-500 font-mono">
          Inference token volume
        </p>
      </div>

      {/* Attempts */}
      <div className="group relative overflow-hidden rounded-[8px] border-t border-t-zinc-700/60 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-gradient-to-b from-[#141418]/95 via-[#101013]/95 to-[#0a0a0d]/95 p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_4px_16px_rgba(0,0,0,0.35)] transition-all duration-150 hover:border-t-zinc-600/80 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.1),0_6px_20px_rgba(0,0,0,0.45)]">
        <span
          aria-hidden="true"
          className="pointer-events-none absolute inset-x-0 top-0 h-[1px] bg-gradient-to-r from-transparent via-white/10 to-transparent"
        />
        <div className="flex items-center justify-between text-zinc-400">
          <span className="text-[10.5px] font-mono font-semibold uppercase tracking-wider text-zinc-400">
            Attempts
          </span>
          <div className="flex h-6 w-6 items-center justify-center rounded-[5px] bg-zinc-900/90 border border-zinc-800 text-zinc-400 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]">
            <Layers className="h-3.5 w-3.5" />
          </div>
        </div>
        <div className="mt-2.5 flex items-baseline gap-1.5">
          <span className="font-mono text-xl font-bold tracking-tight text-zinc-100">
            {trace.attempts.length}
          </span>
        </div>
        <p className="mt-1 text-[10.5px] text-zinc-500 font-mono">
          Sandbox verify cycles
        </p>
      </div>
    </div>
  );
}
