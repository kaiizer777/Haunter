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
    <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
      {/* Total Cost */}
      <div className="rounded-[6px] border border-zinc-800 bg-[#121215] p-3.5">
        <div className="flex items-center justify-between text-zinc-400">
          <span className="text-[11px] font-medium uppercase tracking-wider">
            Total Cost
          </span>
          <DollarSign className="h-3.5 w-3.5 text-amber-400" />
        </div>
        <div className="mt-1.5 flex items-baseline gap-1">
          <span className="font-mono text-lg font-bold text-zinc-100">
            {formatCost(trace.total_cost)}
          </span>
          <span className="text-[10px] text-zinc-500 font-mono">USD</span>
        </div>
      </div>

      {/* Total Latency */}
      <div className="rounded-[6px] border border-zinc-800 bg-[#121215] p-3.5">
        <div className="flex items-center justify-between text-zinc-400">
          <span className="text-[11px] font-medium uppercase tracking-wider">
            Total Latency
          </span>
          <Clock className="h-3.5 w-3.5 text-zinc-400" />
        </div>
        <div className="mt-1.5 flex items-baseline gap-1">
          <span className="font-mono text-lg font-bold text-zinc-100">
            {formatLatency(trace.total_latency_ms)}
          </span>
        </div>
      </div>

      {/* Token Volume */}
      <div className="rounded-[6px] border border-zinc-800 bg-[#121215] p-3.5">
        <div className="flex items-center justify-between text-zinc-400">
          <span className="text-[11px] font-medium uppercase tracking-wider">
            Total Tokens
          </span>
          <Cpu className="h-3.5 w-3.5 text-zinc-400" />
        </div>
        <div className="mt-1.5 flex items-baseline gap-1">
          <span className="font-mono text-lg font-bold text-zinc-100">
            {formatNumber(totalTokens)}
          </span>
          <span className="text-[10px] text-zinc-500 font-mono">tok</span>
        </div>
      </div>

      {/* Attempts */}
      <div className="rounded-[6px] border border-zinc-800 bg-[#121215] p-3.5">
        <div className="flex items-center justify-between text-zinc-400">
          <span className="text-[11px] font-medium uppercase tracking-wider">
            Attempts
          </span>
          <Layers className="h-3.5 w-3.5 text-zinc-400" />
        </div>
        <div className="mt-1.5 flex items-baseline gap-1">
          <span className="font-mono text-lg font-bold text-zinc-100">
            {trace.attempts.length}
          </span>
        </div>
        {/*
          NOTE: failure_classification is intentionally NOT rendered here.
          For older runs without failure_reason, the classifier label is shown
          on the run header card (next to the status badge) instead — placing
          a coarse label like "wrong_diagnosis" next to the attempts count
          read as "0 attempts → wrong diagnosis", which was misleading.
        */}
      </div>
    </div>
  );
}
