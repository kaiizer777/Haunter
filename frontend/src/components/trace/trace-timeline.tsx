import { TraceOut, RunStepOut, AttemptOut } from "@/lib/api";
import { formatCost, formatLatency, formatRelativeTime } from "@/lib/utils";
import { 
  Bot, 
  Terminal, 
  ShieldCheck, 
  GitPullRequest, 
  Code2, 
  AlertCircle, 
  CheckCircle2, 
  XCircle 
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { ConfidenceBar } from "@/components/runs/confidence-bar";

interface TraceTimelineProps {
  trace: TraceOut;
}

function getStepIcon(stepName: string) {
  const normalized = stepName.toLowerCase();
  if (normalized.includes("context") || normalized.includes("gather")) {
    return Bot;
  }
  if (normalized.includes("fix") || normalized.includes("generator")) {
    return Code2;
  }
  if (normalized.includes("sandbox") || normalized.includes("verify")) {
    return Terminal;
  }
  if (normalized.includes("pr") || normalized.includes("writer")) {
    return GitPullRequest;
  }
  return Bot;
}

export function TraceTimeline({ trace }: TraceTimelineProps) {
  const { steps, attempts } = trace;

  return (
    <div className="space-y-6">
      {/* Timeline Pipeline */}
      <div className="relative pl-6 border-l border-zinc-800 space-y-8">
        {/* Step List */}
        {steps.map((step, idx) => {
          const Icon = getStepIcon(step.step_name);
          const isLast = idx === steps.length - 1 && attempts.length === 0;

          return (
            <div key={`${step.step_name}-${idx}`} className="relative group">
              {/* Timeline Dot */}
              <div className="absolute -left-[31px] top-1 flex h-4 w-4 items-center justify-center rounded-full bg-[#121215] border border-zinc-700">
                <span className="h-1.5 w-1.5 rounded-full bg-amber-400" />
              </div>

              {/* Step Card */}
              <div className="rounded-[6px] border border-zinc-800/90 bg-[#121215] p-4 transition-colors hover:border-zinc-700/80">
                <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800/60 pb-2.5">
                  <div className="flex items-center gap-2">
                    <div className="flex h-6 w-6 items-center justify-center rounded-[4px] bg-zinc-900 border border-zinc-800 text-zinc-300">
                      <Icon className="h-3.5 w-3.5 text-amber-400" />
                    </div>
                    <div>
                      <h4 className="text-xs font-semibold text-zinc-100 font-mono">
                        {step.step_name}
                      </h4>
                      <span className="text-[10px] text-zinc-500 font-mono">
                        {formatRelativeTime(step.created_at)}
                      </span>
                    </div>
                  </div>

                  {/* Numerics: Tokens, Latency, Cost */}
                  <div className="flex items-center gap-3 text-xs font-mono">
                    <span className="text-zinc-400" title="Input / Output Tokens">
                      <span className="text-zinc-300">{step.input_tokens || 0}</span>
                      <span className="text-zinc-600">/</span>
                      <span className="text-zinc-300">{step.output_tokens || 0}</span>
                      <span className="text-zinc-500 text-[10px] ml-1">tok</span>
                    </span>

                    <span className="text-zinc-400 border-l border-zinc-800 pl-3">
                      {formatLatency(step.latency_ms)}
                    </span>

                    <span className="text-amber-400/90 font-semibold border-l border-zinc-800 pl-3">
                      {formatCost(step.cost_estimate)}
                    </span>
                  </div>
                </div>
              </div>
            </div>
          );
        })}

        {/* Attempts Section */}
        {attempts.map((attempt) => {
          const isPass = attempt.verification_status === "pass" || attempt.verification_status === "completed";
          const isFail = attempt.verification_status === "fail" || attempt.verification_status === "failed";

          return (
            <div key={`attempt-${attempt.attempt_number}`} className="relative group">
              {/* Timeline Dot */}
              <div className="absolute -left-[31px] top-1 flex h-4 w-4 items-center justify-center rounded-full bg-[#121215] border border-zinc-700">
                <span
                  className={
                    isPass
                      ? "h-1.5 w-1.5 rounded-full bg-emerald-400"
                      : isFail
                      ? "h-1.5 w-1.5 rounded-full bg-red-400"
                      : "h-1.5 w-1.5 rounded-full bg-amber-400"
                  }
                />
              </div>

              {/* Attempt Card */}
              <div className="rounded-[6px] border border-zinc-800/90 bg-[#121215] p-4 space-y-3">
                <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800/60 pb-2.5">
                  <div className="flex items-center gap-2">
                    <span className="rounded-[4px] border border-zinc-700 bg-zinc-900 px-1.5 py-0.5 font-mono text-[11px] font-bold text-zinc-200">
                      Attempt #{attempt.attempt_number}
                    </span>

                    {attempt.verification_status && (
                      <Badge
                        variant={isPass ? "success" : isFail ? "destructive" : "warning"}
                        className="text-[10px] font-mono"
                      >
                        {isPass ? (
                          <CheckCircle2 className="h-3 w-3 mr-1" />
                        ) : isFail ? (
                          <XCircle className="h-3 w-3 mr-1" />
                        ) : (
                          <AlertCircle className="h-3 w-3 mr-1" />
                        )}
                        Sandbox {attempt.verification_status}
                      </Badge>
                    )}
                  </div>

                  <div className="flex items-center gap-4">
                    {attempt.confidence_score !== null && (
                      <div className="flex items-center gap-1.5">
                        <span className="text-[10px] text-zinc-500 font-mono">Confidence:</span>
                        <ConfidenceBar score={attempt.confidence_score} />
                      </div>
                    )}

                    {attempt.build_duration_ms && (
                      <span className="font-mono text-xs text-zinc-400 border-l border-zinc-800 pl-3">
                        {formatLatency(attempt.build_duration_ms)}
                      </span>
                    )}
                  </div>
                </div>

                {/* Failure Reason */}
                {attempt.failure_reason && (
                  <div className="rounded-[5px] border border-red-900/50 bg-red-950/20 p-2.5 text-xs text-red-300 font-mono whitespace-pre-wrap">
                    <span className="text-red-400 font-semibold block text-[10px] uppercase mb-1">
                      Failure Reason
                    </span>
                    {attempt.failure_reason}
                  </div>
                )}

                {/* Patch Diff Box (Escaped plain text) */}
                {attempt.patch_text && (
                  <div className="space-y-1">
                    <div className="flex items-center justify-between text-[11px] font-mono text-zinc-400">
                      <span>Generated Patch (Unified Diff)</span>
                    </div>
                    <pre className="max-h-72 overflow-x-auto rounded-[5px] border border-zinc-800/80 bg-[#09090b] p-3 text-[11px] font-mono text-zinc-300 leading-relaxed whitespace-pre select-text">
                      {attempt.patch_text}
                    </pre>
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
