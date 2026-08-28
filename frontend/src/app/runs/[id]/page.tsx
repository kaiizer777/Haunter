"use client";

import { useEffect, useState, useCallback, use } from "react";
import Link from "next/link";
import { AppLayout } from "@/components/layout/app-layout";
import { StatusBadge } from "@/components/runs/status-badge";
import { CostBreakdown } from "@/components/trace/cost-breakdown";
import { TraceTimeline } from "@/components/trace/trace-timeline";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import { api, TraceOut } from "@/lib/api";
import { formatRelativeTime } from "@/lib/utils";
import { 
  ArrowLeft, 
  GitBranch, 
  GitCommit, 
  ExternalLink, 
  GitPullRequest, 
  AlertCircle, 
  FileText,
  Clock
} from "lucide-react";

export default function RunDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const resolvedParams = use(params);
  const runId = resolvedParams.id;

  const [trace, setTrace] = useState<TraceOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchTrace = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.getRunTrace(runId);
      setTrace(data);
    } catch (err: unknown) {
      if (err instanceof Error) {
        setError(err.message);
      } else {
        setError("Failed to fetch run trace timeline.");
      }
    } finally {
      setLoading(false);
    }
  }, [runId]);

  useEffect(() => {
    fetchTrace();
  }, [fetchTrace]);

  return (
    <AppLayout
      title="Run Trace & Observability"
      subtitle={`Run ID: ${runId}`}
      actions={
        <Link href="/runs">
          <Button variant="outline" size="sm" className="flex items-center gap-1.5 text-xs">
            <ArrowLeft className="h-3.5 w-3.5" />
            Back to Runs
          </Button>
        </Link>
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
            <Skeleton className="h-28 w-full" />
            <Skeleton className="h-20 w-full" />
            <Skeleton className="h-64 w-full" />
          </div>
        ) : !trace ? (
          <div className="p-12 text-center border border-dashed border-zinc-800/80 rounded-[6px]">
            <AlertCircle className="h-6 w-6 mx-auto text-zinc-600 mb-2" />
            <h3 className="text-sm font-medium text-zinc-300">Run not found</h3>
            <p className="text-xs text-zinc-500 mt-1">
              The requested run trace does not exist or you do not have permission to view it.
            </p>
          </div>
        ) : (
          <>
            {/* Header Meta Card */}
            <div className="rounded-[6px] border border-zinc-800 bg-[#121215] p-4 sm:p-5 space-y-4">
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-zinc-800/80 pb-4">
                <div className="flex items-center gap-3">
                  <StatusBadge status={trace.run.status} />
                  <span className="font-mono text-xs text-zinc-400 flex items-center gap-1.5">
                    <Clock className="h-3.5 w-3.5 text-zinc-500" />
                    {formatRelativeTime(trace.run.created_at)}
                  </span>
                </div>

                {/* PR Link (if opened) */}
                {trace.run.pr_url && (
                  <a
                    href={trace.run.pr_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1.5 rounded-[5px] border border-emerald-800/80 bg-emerald-950/40 px-2.5 py-1 text-xs font-medium text-emerald-300 hover:bg-emerald-900/50 transition-colors"
                  >
                    <GitPullRequest className="h-3.5 w-3.5 text-emerald-400" />
                    <span>View Pull Request #{trace.run.pr_number || ""}</span>
                    <ExternalLink className="h-3 w-3 ml-0.5 text-emerald-400/80" />
                  </a>
                )}
              </div>

              {/* Diagnosis Summary (Plain text safe rendering) */}
              {trace.run.diagnosis_summary && (
                <div className="rounded-[5px] border border-zinc-800/90 bg-[#09090b] p-3.5 space-y-1.5">
                  <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-amber-400">
                    <FileText className="h-3.5 w-3.5" />
                    <span>Root Cause Diagnosis</span>
                  </div>
                  <p className="text-xs text-zinc-300 leading-relaxed font-mono whitespace-pre-wrap select-text">
                    {trace.run.diagnosis_summary}
                  </p>
                </div>
              )}
            </div>

            {/* Stat Overview Cards */}
            <CostBreakdown trace={trace} />

            {/* Chronological Step & Attempt Timeline */}
            <div className="rounded-[6px] border border-zinc-800 bg-[#121215] p-5 space-y-4">
              <div className="border-b border-zinc-800/80 pb-3">
                <h3 className="text-xs font-semibold uppercase tracking-wider text-zinc-300 font-mono">
                  Autonomous Execution Timeline
                </h3>
                <p className="text-[11px] text-zinc-500 mt-0.5 font-mono">
                  Step-by-step trace from context gathering to sandbox verification and PR dispatch
                </p>
              </div>

              <TraceTimeline trace={trace} />
            </div>
          </>
        )}
      </div>
    </AppLayout>
  );
}
