import { TraceOut } from "@/lib/api";
import { formatCost, formatLatency, formatNumber } from "@/lib/utils";
import { DollarSign, Clock, Layers, Cpu, AlertTriangle } from "lucide-react";
import { Badge } from "@/components/ui/badge";

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

      {/* Attempts & Classification */}
      <div className="rounded-[6px] border border-zinc-800 bg-[#121215] p-3.5">
        <div className="flex items-center justify-between text-zinc-400">
          <span className="text-[11px] font-medium uppercase tracking-wider">
            Attempts
          </span>
          <Layers className="h-3.5 w-3.5 text-zinc-400" />
        </div>
        <div className="mt-1.5 flex items-center justify-between">
          <span className="font-mono text-lg font-bold text-zinc-100">
            {trace.attempts.length}
          </span>
          {trace.failure_classification && (
            <Badge variant="destructive" className="text-[10px] font-mono">
              <AlertTriangle className="h-2.5 w-2.5 mr-1" />
              {trace.failure_classification}
            </Badge>
          )}
        </div>
      </div>
    </div>
  );
}
