import { useState, useMemo } from "react";
import { TraceOut } from "@/lib/api";
import { formatCost, formatLatency, formatRelativeTime } from "@/lib/utils";
import { 
  Bot, 
  Terminal, 
  GitPullRequest, 
  Code2, 
  AlertCircle, 
  CheckCircle2, 
  XCircle,
  Copy,
  Check,
  Clock,
  Sparkles,
  ChevronDown,
  ChevronUp,
  FileCode2,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { ConfidenceBar } from "@/components/runs/confidence-bar";
import { Button } from "@/components/ui/button";

interface TraceTimelineProps {
  trace: TraceOut;
}

function getStepMetadata(stepName: string) {
  const normalized = stepName.toLowerCase();
  if (normalized.includes("context") || normalized.includes("gather")) {
    return {
      icon: Bot,
      role: "Context Gatherer",
      desc: "Scans repository context, extracts CI logs, and isolates failure site",
      badgeColor: "border-amber-500/30 bg-amber-500/10 text-amber-300",
      accentDot: "bg-amber-400 border-amber-500/40 shadow-[0_0_8px_rgba(245,158,11,0.3)]",
    };
  }
  if (normalized.includes("fix") || normalized.includes("generator")) {
    return {
      icon: Code2,
      role: "Fix Generator",
      desc: "Synthesizes minimal diff patch targeted directly at root cause",
      badgeColor: "border-cyan-500/30 bg-cyan-500/10 text-cyan-300",
      accentDot: "bg-cyan-400 border-cyan-500/40 shadow-[0_0_8px_rgba(6,182,212,0.3)]",
    };
  }
  if (normalized.includes("sandbox") || normalized.includes("verify")) {
    return {
      icon: Terminal,
      role: "Sandbox Verifier",
      desc: "Runs verification pipeline in isolated ephemeral mirror repository",
      badgeColor: "border-purple-500/30 bg-purple-500/10 text-purple-300",
      accentDot: "bg-purple-400 border-purple-500/40 shadow-[0_0_8px_rgba(168,85,247,0.3)]",
    };
  }
  if (normalized.includes("pr") || normalized.includes("writer")) {
    return {
      icon: GitPullRequest,
      role: "PR Writer",
      desc: "Composes pull request branch, title, and detailed diagnostic summary",
      badgeColor: "border-emerald-500/30 bg-emerald-500/10 text-emerald-300",
      accentDot: "bg-emerald-400 border-emerald-500/40 shadow-[0_0_8px_rgba(52,211,153,0.3)]",
    };
  }
  return {
    icon: Sparkles,
    role: "Pipeline Step",
    desc: "Autonomous workflow subagent task execution",
    badgeColor: "border-zinc-700 bg-zinc-800 text-zinc-300",
    accentDot: "bg-zinc-400 border-zinc-500/40 shadow-[0_0_8px_rgba(161,161,170,0.3)]",
  };
}

function DiffViewer({ patchText }: { patchText: string }) {
  const [expanded, setExpanded] = useState(false);

  const lines = useMemo(() => patchText.split("\n"), [patchText]);

  const { addCount, delCount, filePath } = useMemo(() => {
    let adds = 0;
    let dels = 0;
    let path = "";
    for (const l of lines) {
      if (l.startsWith("+++ b/")) {
        path = l.slice(6);
      } else if (l.startsWith("+") && !l.startsWith("+++")) {
        adds++;
      } else if (l.startsWith("-") && !l.startsWith("---")) {
        dels++;
      }
    }
    return { addCount: adds, delCount: dels, filePath: path };
  }, [lines]);

  const shouldTruncate = lines.length > 18;
  const displayedLines = shouldTruncate && !expanded ? lines.slice(0, 16) : lines;

  return (
    <div className="space-y-1.5">
      <div className="rounded-[6px] border border-zinc-800/90 bg-[#070709] overflow-hidden shadow-[inset_0_1px_2px_rgba(0,0,0,0.6)]">
        {/* Diff File Header Bar */}
        {filePath && (
          <div className="flex items-center justify-between border-b border-zinc-800/80 bg-zinc-900/60 px-3 py-1.5 text-[11px] font-mono text-zinc-300">
            <span className="flex items-center gap-1.5 text-zinc-300 font-semibold">
              <FileCode2 className="h-3 w-3 text-zinc-500" />
              {filePath}
            </span>
            <div className="flex items-center gap-2 font-mono text-[10px]">
              {addCount > 0 && <span className="text-emerald-400 font-semibold">+{addCount}</span>}
              {delCount > 0 && <span className="text-rose-400 font-semibold">-{delCount}</span>}
            </div>
          </div>
        )}

        {/* Diff Body */}
        <pre className="overflow-x-auto p-3 text-[11.5px] font-mono leading-relaxed select-text">
          {displayedLines.map((line, lIdx) => {
            const isAdd = line.startsWith("+") && !line.startsWith("+++");
            const isDel = line.startsWith("-") && !line.startsWith("---");
            const isHunk = line.startsWith("@@");
            const isHeader = line.startsWith("---") || line.startsWith("+++");

            let lineClass = "text-zinc-300";
            if (isAdd) lineClass = "bg-emerald-950/30 text-emerald-300 border-l-2 border-emerald-500/80 pl-2 -ml-2";
            else if (isDel) lineClass = "bg-rose-950/30 text-rose-300 border-l-2 border-rose-500/80 pl-2 -ml-2";
            else if (isHunk) lineClass = "bg-cyan-950/20 text-cyan-400 font-semibold";
            else if (isHeader) lineClass = "text-zinc-400 font-medium";

            return (
              <div key={lIdx} className={`${lineClass} font-mono py-[1px]`}>
                {line || " "}
              </div>
            );
          })}
        </pre>

        {shouldTruncate && (
          <div className="border-t border-zinc-800/70 bg-zinc-900/40 p-1 text-center">
            <button
              type="button"
              onClick={() => setExpanded(!expanded)}
              className="inline-flex items-center gap-1 text-[11px] font-mono text-zinc-400 hover:text-zinc-200 transition-colors py-0.5 px-2"
            >
              {expanded ? (
                <>
                  <ChevronUp className="h-3 w-3" /> Show less
                </>
              ) : (
                <>
                  <ChevronDown className="h-3 w-3" /> Show all {lines.length} lines (+{lines.length - 16} more)
                </>
              )}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

export function TraceTimeline({ trace }: TraceTimelineProps) {
  const { steps, attempts } = trace;
  const [copiedPatchAttempt, setCopiedPatchAttempt] = useState<number | null>(null);

  const handleCopyPatch = (patchText: string, attemptNumber: number) => {
    navigator.clipboard.writeText(patchText);
    setCopiedPatchAttempt(attemptNumber);
    setTimeout(() => {
      setCopiedPatchAttempt(null);
    }, 2000);
  };

  return (
    <div className="space-y-6">
      {/* Timeline Spine */}
      <div className="relative pl-8 space-y-6">
        {/* Subagent Steps */}
        {steps.map((step, idx) => {
          const meta = getStepMetadata(step.step_name);
          const Icon = meta.icon;
          const isLastStep = idx === steps.length - 1;
          const hasAttempts = attempts.length > 0;
          const isLastItem = isLastStep && !hasAttempts;

          return (
            <div key={`${step.step_name}-${idx}`} className="relative group">
              {/* Connector segment to next node */}
              {!isLastItem && (
                <span
                  aria-hidden="true"
                  className="pointer-events-none absolute w-[2px] z-0"
                  style={{
                    left: "-17px",
                    top: "26px",
                    bottom: "-50px",
                    background: isLastStep
                      ? "linear-gradient(to bottom, rgba(245, 158, 11, 0.4), rgba(113, 113, 122, 0.4), rgba(52, 211, 153, 0.4))"
                      : "linear-gradient(to bottom, rgba(245, 158, 11, 0.4), rgba(245, 158, 11, 0.25), rgba(113, 113, 122, 0.4))",
                  }}
                />
              )}

              {/* Illuminated Timeline Node Dot */}
              <div className="absolute -left-[26px] top-4 z-10 flex h-5 w-5 items-center justify-center rounded-full bg-[#0d0d10] border border-amber-500/40 shadow-[0_0_8px_rgba(245,158,11,0.25)]">
                <span className="h-1.5 w-1.5 rounded-full bg-amber-400 shadow-[0_0_6px_rgba(245,158,11,1)]" />
              </div>

              {/* Step Card */}
              <div className="group relative overflow-hidden rounded-[8px] border-t border-t-zinc-700/60 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-gradient-to-b from-[#141418]/95 via-[#101013]/95 to-[#0a0a0d]/95 p-4 sm:p-4.5 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_3px_12px_rgba(0,0,0,0.35)] transition-all duration-150 hover:border-t-zinc-600/80 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.1),0_6px_20px_rgba(0,0,0,0.45)]">
                <span
                  aria-hidden="true"
                  className="pointer-events-none absolute inset-x-0 top-0 h-[1px] bg-gradient-to-r from-transparent via-white/10 to-transparent"
                />

                <div className="flex flex-wrap items-center justify-between gap-3 border-b border-zinc-800/70 pb-3">
                  <div className="flex items-center gap-3">
                    <div className="flex h-8 w-8 items-center justify-center rounded-[6px] bg-zinc-900/90 border border-zinc-700/60 text-amber-400 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_1px_3px_rgba(0,0,0,0.3)]">
                      <Icon className="h-4 w-4" />
                    </div>
                    <div>
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-[10px] font-mono px-1.5 py-0.5 rounded-[4px] bg-zinc-900 border border-zinc-800 text-zinc-400 font-semibold">
                          Step 0{idx + 1}
                        </span>
                        <h4 className="text-[13px] font-bold text-zinc-100 font-mono tracking-tight">
                          {step.step_name}
                        </h4>
                        <span className={`text-[10px] font-mono px-2 py-0.5 rounded-[4px] border ${meta.badgeColor}`}>
                          {meta.role}
                        </span>
                      </div>
                      <p className="text-[11px] text-zinc-400 mt-0.5 hidden sm:block font-mono">
                        {meta.desc}
                      </p>
                    </div>
                  </div>

                  {/* Numerics: Tokens, Latency, Cost */}
                  <div className="flex items-center gap-2 sm:gap-2.5 text-xs font-mono">
                    <div className="flex items-center gap-1 rounded-[5px] border border-zinc-800 bg-zinc-900/80 px-2.5 py-1 text-zinc-300" title="Input / Output Tokens">
                      <span className="text-zinc-500 text-[10px] font-mono">In:</span>
                      <span className="text-zinc-200 font-semibold">{step.input_tokens || 0}</span>
                      <span className="text-zinc-600 mx-0.5">/</span>
                      <span className="text-zinc-500 text-[10px] font-mono">Out:</span>
                      <span className="text-zinc-200 font-semibold">{step.output_tokens || 0}</span>
                      <span className="text-zinc-500 text-[10px] ml-0.5">tok</span>
                    </div>

                    <div className="flex items-center gap-1.5 rounded-[5px] border border-zinc-800 bg-zinc-900/80 px-2.5 py-1 text-zinc-300">
                      <Clock className="h-3 w-3 text-zinc-500" />
                      <span>{formatLatency(step.latency_ms)}</span>
                    </div>

                    <div className="flex items-center rounded-[5px] border border-amber-500/20 bg-amber-500/[0.08] px-2.5 py-1 font-semibold text-amber-300 shadow-[inset_0_1px_0_rgba(255,255,255,0.06)]">
                      <span>{formatCost(step.cost_estimate)}</span>
                    </div>
                  </div>
                </div>

                <div className="mt-2.5 flex items-center justify-between text-[11px] text-zinc-500 font-mono">
                  <span className="flex items-center gap-1 text-zinc-400">
                    <Clock className="h-3 w-3 text-zinc-500" />
                    {formatRelativeTime(step.created_at)}
                  </span>
                  <span className="text-emerald-400/90 flex items-center gap-1 text-[10.5px]">
                    <CheckCircle2 className="h-3 w-3 text-emerald-400" />
                    Completed
                  </span>
                </div>
              </div>
            </div>
          );
        })}

        {/* Sandbox Verification Attempts Section */}
        {attempts.map((attempt, aIdx) => {
          const isPass = attempt.verification_status === "pass" || attempt.verification_status === "completed";
          const isFail = attempt.verification_status === "fail" || attempt.verification_status === "failed";
          const isLastAttempt = aIdx === attempts.length - 1;

          return (
            <div key={`attempt-${attempt.attempt_number}`} className="relative group">
              {/* Connector segment to next attempt */}
              {!isLastAttempt && (
                <span
                  aria-hidden="true"
                  className="pointer-events-none absolute w-[2px] z-0"
                  style={{
                    left: "-17px",
                    top: "26px",
                    bottom: "-50px",
                    background: "linear-gradient(to bottom, rgba(52, 211, 153, 0.4), rgba(113, 113, 122, 0.4))",
                  }}
                />
              )}

              {/* Illuminated Attempt Timeline Dot */}
              <div className={`absolute -left-[26px] top-4 z-10 flex h-5 w-5 items-center justify-center rounded-full bg-[#0d0d10] border ${
                isPass
                  ? "border-emerald-500/50 shadow-[0_0_8px_rgba(52,211,153,0.3)]"
                  : isFail
                  ? "border-rose-500/50 shadow-[0_0_8px_rgba(244,63,94,0.3)]"
                  : "border-amber-500/50 shadow-[0_0_8px_rgba(245,158,11,0.3)]"
              }`}>
                <span
                  className={
                    isPass
                      ? "h-1.5 w-1.5 rounded-full bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,1)]"
                      : isFail
                      ? "h-1.5 w-1.5 rounded-full bg-red-400 shadow-[0_0_6px_rgba(248,113,113,1)]"
                      : "h-1.5 w-1.5 rounded-full bg-amber-400 shadow-[0_0_6px_rgba(245,158,11,1)]"
                  }
                />
              </div>

              {/* Attempt Card */}
              <div className={`group relative overflow-hidden rounded-[8px] border-t border-x border-b p-4 sm:p-5 space-y-3.5 bg-gradient-to-b from-[#141418]/95 via-[#101013]/95 to-[#0a0a0d]/95 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_4px_16px_rgba(0,0,0,0.35)] transition-all duration-150 ${
                isPass
                  ? "border-t-emerald-700/60 border-x-emerald-900/40 border-b-zinc-950"
                  : isFail
                  ? "border-t-rose-800/60 border-x-rose-950/40 border-b-zinc-950"
                  : "border-t-zinc-700/60 border-x-zinc-800/80 border-b-zinc-950"
              }`}>
                <span
                  aria-hidden="true"
                  className="pointer-events-none absolute inset-x-0 top-0 h-[1px] bg-gradient-to-r from-transparent via-white/10 to-transparent"
                />

                <div className="flex flex-wrap items-center justify-between gap-3 border-b border-zinc-800/70 pb-3">
                  <div className="flex items-center gap-2.5">
                    <span className="rounded-[5px] border border-zinc-700/80 bg-zinc-900/90 px-2.5 py-1 font-mono text-xs font-bold text-zinc-100 shadow-[inset_0_1px_0_rgba(255,255,255,0.08)]">
                      Attempt #{attempt.attempt_number}
                    </span>

                    {attempt.verification_status && (
                      <Badge
                        variant={isPass ? "success" : isFail ? "destructive" : "warning"}
                        className="text-[11px] font-mono px-2.5 py-0.5 rounded-[5px]"
                      >
                        {isPass ? (
                          <CheckCircle2 className="h-3.5 w-3.5 mr-1 text-emerald-400" />
                        ) : isFail ? (
                          <XCircle className="h-3.5 w-3.5 mr-1 text-rose-400" />
                        ) : (
                          <AlertCircle className="h-3.5 w-3.5 mr-1 text-amber-400" />
                        )}
                        Sandbox {attempt.verification_status}
                      </Badge>
                    )}
                  </div>

                  <div className="flex items-center gap-4 flex-wrap">
                    {attempt.confidence_score !== null && (
                      <div className="flex items-center gap-2 bg-zinc-900/80 border border-zinc-800 px-2.5 py-1 rounded-[5px]">
                        <span className="text-[11px] text-zinc-400 font-mono">Confidence:</span>
                        <ConfidenceBar score={attempt.confidence_score} />
                      </div>
                    )}

                    {attempt.build_duration_ms && (
                      <div className="flex items-center gap-1.5 font-mono text-xs text-zinc-300 bg-zinc-900/80 border border-zinc-800 px-2.5 py-1 rounded-[5px]">
                        <Clock className="h-3 w-3 text-zinc-500" />
                        <span>{formatLatency(attempt.build_duration_ms)}</span>
                      </div>
                    )}
                  </div>
                </div>

                {/* Failure Reason */}
                {attempt.failure_reason && (
                  <div className="rounded-[6px] border border-rose-800/60 bg-gradient-to-b from-rose-950/40 via-rose-950/20 to-[#0c0809] p-3 text-xs text-rose-200 font-mono whitespace-pre-wrap shadow-[inset_0_1px_0_rgba(255,255,255,0.06)]">
                    <span className="text-rose-400 font-semibold block text-[10.5px] uppercase tracking-wider mb-1 flex items-center gap-1">
                      <AlertCircle className="h-3.5 w-3.5" />
                      Failure Reason
                    </span>
                    {attempt.failure_reason}
                  </div>
                )}

                {/* Patch Unified Diff Viewer */}
                {attempt.patch_text && (
                  <div className="space-y-2">
                    <div className="flex items-center justify-between text-xs font-mono text-zinc-400 pt-1">
                      <span className="font-semibold text-zinc-300 flex items-center gap-1.5">
                        <Code2 className="h-3.5 w-3.5 text-amber-400" />
                        Generated Patch (Unified Diff)
                      </span>

                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => handleCopyPatch(attempt.patch_text!, attempt.attempt_number)}
                        className="h-7 px-2.5 text-xs font-mono rounded-[5px] bg-gradient-to-b from-zinc-800/90 via-zinc-850 to-zinc-900/90 border-t border-t-zinc-600/60 border-x border-x-zinc-700/60 border-b border-b-zinc-950 text-zinc-200 shadow-[inset_0_1px_0_rgba(255,255,255,0.1),0_1px_3px_rgba(0,0,0,0.3)] active:translate-y-[0.5px] transition-all"
                      >
                        {copiedPatchAttempt === attempt.attempt_number ? (
                          <>
                            <Check className="h-3 w-3 text-emerald-400 mr-1" />
                            <span className="text-emerald-300">Copied!</span>
                          </>
                        ) : (
                          <>
                            <Copy className="h-3 w-3 text-zinc-400 mr-1" />
                            <span>Copy Patch</span>
                          </>
                        )}
                      </Button>
                    </div>

                    <DiffViewer patchText={attempt.patch_text} />
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
