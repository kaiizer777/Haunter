"use client";

import { useEffect, useState, useCallback, useMemo } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { AppLayout } from "@/components/layout/app-layout";
import { StatusBadge } from "@/components/runs/status-badge";
import { CostBreakdown } from "@/components/trace/cost-breakdown";
import { TraceTimeline } from "@/components/trace/trace-timeline";
import { DiagnosisView } from "@/components/trace/diagnosis-view";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import RetryRunButton from "@/components/runs/retry-run-button";
import RunLineage from "@/components/runs/run-lineage";
import { api, TraceOut, RepoOut } from "@/lib/api";
import { formatRelativeTime } from "@/lib/utils";
import {
  ArrowLeft,
  ExternalLink,
  GitPullRequest,
  GitBranch,
  FolderGit2,
  AlertCircle,
  Clock,
  AlertTriangle,
  Copy,
  Check,
  Activity,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";

export default function RunDetailClient() {
  // Run id is carried in the `?id=<uuid>` query string so this can be a
  // plain static page under `output: "export"`.
  const router = useRouter();
  const searchParams = useSearchParams();
  const runId = useMemo(() => searchParams.get("id") ?? "", [searchParams]);

  const [trace, setTrace] = useState<TraceOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [copiedId, setCopiedId] = useState(false);
  const [repos, setRepos] = useState<RepoOut[]>([]);

  useEffect(() => {
    if (typeof api.getRepos === "function") {
      api.getRepos()
        .then((data) => setRepos(data || []))
        .catch((err) => console.error("Failed to load repos:", err));
    }
  }, []);

  const currentRepo = useMemo(() => {
    if (!trace?.run?.repo_id || !repos.length) return null;
    return repos.find((r) => r.id === trace.run.repo_id) || null;
  }, [trace?.run?.repo_id, repos]);

  const repoIdentity = useMemo(() => {
    if (currentRepo) {
      return {
        fullName: `${currentRepo.owner}/${currentRepo.name}`,
        url: `https://github.com/${currentRepo.owner}/${currentRepo.name}`,
      };
    }
    if (trace?.run?.pr_url) {
      const match = trace.run.pr_url.match(/github\.com\/([^/]+)\/([^/]+)/);
      if (match) {
        return {
          fullName: `${match[1]}/${match[2]}`,
          url: `https://github.com/${match[1]}/${match[2]}`,
        };
      }
    }
    return null;
  }, [currentRepo, trace?.run?.pr_url]);

  const fetchTrace = useCallback(async () => {
    if (!runId) {
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const data = await api.getRunTrace(runId);
      setTrace(data);
    } catch (err: unknown) {
      if (err instanceof Error) {
        setError(err.message);
      } else {
        setError("Failed to fetch run trace timeline from server.");
      }
    } finally {
      setLoading(false);
    }
  }, [runId]);

  useEffect(() => {
    fetchTrace();
  }, [fetchTrace]);

  const handleCopyRunId = () => {
    if (!runId) return;
    navigator.clipboard.writeText(runId);
    setCopiedId(true);
    setTimeout(() => setCopiedId(false), 2000);
  };

  return (
    <AppLayout
      title="Run Trace & Observability"
      subtitle={runId ? `Run ID: ${runId}` : "Run Trace"}
      actions={
        <div className="flex items-center gap-2">
          {runId && trace && (
            <RetryRunButton
              runId={trace.run.id}
              status={trace.run.status}
              // Navigate to the newly dispatched child so the user lands on the
              // run that is actually progressing instead of a settled trace.
              onRetried={(child) => router.push(`/runs/detail?id=${child.id}`)}
              aria-label="Retry this run"
            />
          )}

          {runId && (
            <Button
              variant="outline"
              size="sm"
              onClick={handleCopyRunId}
              className="flex items-center gap-1.5 text-xs font-mono h-8 rounded-[6px] bg-gradient-to-b from-zinc-800/90 via-zinc-850 to-zinc-900/90 border-t border-t-zinc-600/60 border-x border-x-zinc-700/60 border-b border-b-zinc-950 text-zinc-200 shadow-[inset_0_1px_0_rgba(255,255,255,0.1),0_1px_3px_rgba(0,0,0,0.3)] active:translate-y-[0.5px] transition-all"
              title="Copy full Run UUID"
            >
              {copiedId ? (
                <>
                  <Check className="h-3.5 w-3.5 text-emerald-400" />
                  <span className="text-emerald-300">Copied ID</span>
                </>
              ) : (
                <>
                  <Copy className="h-3.5 w-3.5 text-zinc-400" />
                  <span>Copy Run ID</span>
                </>
              )}
            </Button>
          )}

          <Link href="/runs">
            <Button
              variant="outline"
              size="sm"
              className="flex items-center gap-1.5 text-xs font-mono h-8 rounded-[6px] bg-gradient-to-b from-zinc-800/90 via-zinc-850 to-zinc-900/90 border-t border-t-zinc-600/60 border-x border-x-zinc-700/60 border-b border-b-zinc-950 text-zinc-200 shadow-[inset_0_1px_0_rgba(255,255,255,0.1),0_1px_3px_rgba(0,0,0,0.3)] active:translate-y-[0.5px] transition-all"
            >
              <ArrowLeft className="h-3.5 w-3.5 text-zinc-400" />
              <span>Back to Runs</span>
            </Button>
          </Link>
        </div>
      }
    >
      <div className="space-y-6">
        {!runId && !loading && (
          <div className="rounded-[8px] border-t border-t-amber-700/60 border-x border-x-amber-900/50 border-b border-b-zinc-950 bg-gradient-to-b from-amber-950/40 via-amber-950/20 to-[#0c0a06] p-4 text-xs text-amber-300 font-mono shadow-[inset_0_1px_0_rgba(255,255,255,0.06)]">
            Missing <code className="font-mono bg-amber-900/40 px-1.5 py-0.5 rounded text-amber-200">id</code> query parameter. Open this page from a run row.
          </div>
        )}

        {error && (
          <div className="flex items-center gap-2.5 rounded-[8px] border-t border-t-rose-800/60 border-x border-x-rose-950/50 border-b border-b-zinc-950 bg-gradient-to-b from-rose-950/40 via-rose-950/20 to-[#0d0708] p-4 text-xs text-rose-300 font-mono shadow-[inset_0_1px_0_rgba(255,255,255,0.06)]">
            <AlertCircle className="h-4 w-4 shrink-0 text-rose-400" />
            <span>{error}</span>
          </div>
        )}

        {loading ? (
          <div className="space-y-4">
            <Skeleton className="h-32 w-full rounded-[8px]" />
            <Skeleton className="h-24 w-full rounded-[8px]" />
            <Skeleton className="h-72 w-full rounded-[8px]" />
          </div>
        ) : !trace ? (
          <div className="p-12 text-center border border-dashed border-zinc-800/80 rounded-[8px] bg-[#0c0c0f]/50">
            <AlertCircle className="h-8 w-8 mx-auto text-zinc-600 mb-2.5" />
            <h3 className="text-sm font-semibold text-zinc-200">Run not found</h3>
            <p className="text-xs text-zinc-500 mt-1 max-w-sm mx-auto">
              The requested run trace does not exist or you do not have permission to view it.
            </p>
          </div>
        ) : (
          <>
            {/* Header Meta Card */}
            <div className="relative overflow-hidden rounded-[8px] border-t border-t-zinc-700/60 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-gradient-to-b from-[#141418]/95 via-[#101013]/95 to-[#0a0a0d]/95 p-4 sm:p-5 space-y-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_4px_16px_rgba(0,0,0,0.35)]">
              <span
                aria-hidden="true"
                className="pointer-events-none absolute inset-x-0 top-0 h-[1px] bg-gradient-to-r from-transparent via-white/10 to-transparent"
              />

              {/* Repository Identity & PR Context Banner */}
              {repoIdentity && (
                <div className="flex flex-wrap items-center justify-between gap-3 pb-3.5 border-b border-zinc-800/80">
                  <div className="flex items-center gap-3 flex-wrap">
                    <a
                      href={repoIdentity.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-2 text-sm sm:text-base font-bold font-mono text-zinc-100 hover:text-white transition-colors"
                      title="View repository on GitHub"
                    >
                      <FolderGit2 className="h-4 w-4 text-zinc-400" />
                      <span>{repoIdentity.fullName}</span>
                      <ExternalLink className="h-3 w-3 text-zinc-500" />
                    </a>

                    {trace.run.pr_number && (
                      <span className="inline-flex items-center gap-1 rounded bg-zinc-800/80 px-2 py-0.5 text-xs font-mono font-medium text-amber-400 border border-zinc-700/60">
                        <GitPullRequest className="h-3 w-3" />
                        PR #{trace.run.pr_number}
                      </span>
                    )}

                    {trace.run.pr_author && (
                      <div className="inline-flex items-center gap-1.5 rounded-full bg-zinc-900 border border-zinc-800 px-2 py-0.5 text-xs font-mono text-zinc-300">
                        {trace.run.pr_author_avatar ? (
                          <img
                            src={trace.run.pr_author_avatar}
                            alt={trace.run.pr_author}
                            className="h-3.5 w-3.5 rounded-full object-cover"
                          />
                        ) : (
                          <span className="flex h-3.5 w-3.5 items-center justify-center rounded-full bg-zinc-800 text-[9px] font-bold text-zinc-300">
                            {trace.run.pr_author[0]?.toUpperCase()}
                          </span>
                        )}
                        <span>@{trace.run.pr_author}</span>
                      </div>
                    )}

                    {(trace.run.pr_branch || trace.run.head_branch) && (
                      <span className="inline-flex items-center gap-1.5 rounded bg-zinc-900/90 border border-zinc-800 px-2 py-0.5 text-[11px] font-mono text-zinc-300">
                        <GitBranch className="h-3 w-3 text-zinc-500" />
                        <span className="text-zinc-400">{trace.run.base_branch || currentRepo?.default_branch || "main"}</span>
                        <span className="text-zinc-500">←</span>
                        <span className="text-amber-400 font-medium">{trace.run.pr_branch || trace.run.head_branch}</span>
                      </span>
                    )}
                  </div>

                  {trace.run.pr_title && (
                    <div className="text-xs font-medium text-zinc-200 max-w-md truncate" title={trace.run.pr_title}>
                      {trace.run.pr_title}
                    </div>
                  )}
                </div>
              )}

              {/* Status & Actions Header */}
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-zinc-800/80 pb-4">
                <div className="flex items-center gap-3 flex-wrap">
                  <StatusBadge status={trace.run.status} />

                  {(trace.run.status === "flaky_detected" || trace.run.conclusion === "flaky_test") && (
                    <Badge variant="warning" className="text-[10px] font-mono px-2 py-0.5 rounded-[4px] border-amber-500/30 bg-amber-500/10 text-amber-300">
                      Passed 2/2 clean runs (quarantined)
                    </Badge>
                  )}

                  {/* Coarse failure classification */}
                  {trace.failure_classification && !trace.run.failure_reason && (
                    <Badge variant="destructive" className="text-[10px] font-mono px-2 py-0.5 rounded-[4px]">
                      <AlertTriangle className="h-2.5 w-2.5 mr-1" />
                      {trace.failure_classification}
                    </Badge>
                  )}

                  <span className="font-mono text-xs text-zinc-400 flex items-center gap-1.5 bg-zinc-900/80 border border-zinc-800 px-2.5 py-1 rounded-[5px]">
                    <Clock className="h-3.5 w-3.5 text-zinc-500" />
                    {formatRelativeTime(trace.run.created_at)}
                  </span>

                  {trace.run.pr_branch && (
                    <span className="font-mono text-xs text-zinc-400 flex items-center gap-1.5 bg-zinc-900/80 border border-zinc-800 px-2.5 py-1 rounded-[5px]">
                      <GitBranch className="h-3.5 w-3.5 text-zinc-500" />
                      <span className="text-zinc-300">{trace.run.pr_branch}</span>
                    </span>
                  )}
                </div>

                {/* PR Link (if opened) */}
                {trace.run.pr_url && (
                  <a
                    href={trace.run.pr_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1.5 rounded-[6px] border-t border-t-emerald-300/70 border-x border-x-emerald-600/70 border-b border-b-emerald-950 bg-gradient-to-b from-emerald-600/90 via-emerald-700 to-emerald-800 px-3.5 py-1.5 text-xs font-semibold text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.25),0_2px_8px_rgba(16,185,129,0.25)] hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.3),0_3px_12px_rgba(16,185,129,0.35)] active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.4)] transition-all"
                  >
                    <GitPullRequest className="h-3.5 w-3.5 text-emerald-100" />
                    <span>View Pull Request #{trace.run.pr_number || ""}</span>
                    <ExternalLink className="h-3 w-3 ml-0.5 text-emerald-200" />
                  </a>
                )}

                {/* Fallback Issue Link (if filed on exhaust) */}
                {trace.run.fallback_issue_url && (
                  <a
                    href={trace.run.fallback_issue_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1.5 rounded-[6px] border-t border-t-amber-300/60 border-x border-x-amber-600/60 border-b border-b-amber-950 bg-gradient-to-b from-amber-600/90 via-amber-700 to-amber-800 px-3.5 py-1.5 text-xs font-semibold text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.25),0_2px_8px_rgba(245,158,11,0.25)] hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.3),0_3px_12px_rgba(245,158,11,0.35)] active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.4)] transition-all"
                  >
                    <AlertTriangle className="h-3.5 w-3.5 text-amber-100" />
                    <span>View Fallback Issue #{trace.run.fallback_issue_number ?? ""}</span>
                    <ExternalLink className="h-3 w-3 ml-0.5 text-amber-200" />
                  </a>
                )}
              </div>

              {/* Failure Reason */}
              {trace.run.failure_reason && (
                <div className="rounded-[6px] border border-rose-800/60 bg-gradient-to-b from-rose-950/40 via-rose-950/20 to-[#0c0809] p-3.5 space-y-1.5 shadow-[inset_0_1px_0_rgba(255,255,255,0.06)]">
                  <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-rose-300 font-mono">
                    <AlertTriangle className="h-3.5 w-3.5 text-rose-400" />
                    <span>Failure Reason</span>
                  </div>
                  <p className="text-xs text-rose-200/90 leading-relaxed font-mono whitespace-pre-wrap select-text">
                    {trace.run.failure_reason}
                  </p>
                </div>
              )}

              {/* Elevated Root Cause Diagnosis Component */}
              {trace.run.diagnosis_summary && (
                <DiagnosisView summary={trace.run.diagnosis_summary} />
              )}
            </div>

            {/* Retry / PR-refinement thread */}
            <RunLineage
              runId={trace.run.id}
              parent={trace.parent}
              childRuns={trace.children}
            />

            {/* Stat Overview Cards */}
            <CostBreakdown trace={trace} />

            {/* Chronological Step & Attempt Timeline */}
            <div className="relative overflow-hidden rounded-[8px] border-t border-t-zinc-700/60 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-gradient-to-b from-[#141418]/95 via-[#101013]/95 to-[#0a0a0d]/95 p-5 space-y-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_4px_16px_rgba(0,0,0,0.35)]">
              <span
                aria-hidden="true"
                className="pointer-events-none absolute inset-x-0 top-0 h-[1px] bg-gradient-to-r from-transparent via-white/10 to-transparent"
              />

              <div className="border-b border-zinc-800/80 pb-3.5 flex items-center justify-between">
                <div>
                  <h3 className="text-xs font-semibold uppercase tracking-wider text-zinc-200 font-mono flex items-center gap-2">
                    <Activity className="h-3.5 w-3.5 text-amber-400" />
                    <span>Autonomous Execution Timeline</span>
                  </h3>
                  <p className="text-[11px] text-zinc-400 mt-0.5 font-mono">
                    Step-by-step trace from context gathering to sandbox verification and PR dispatch
                  </p>
                </div>
                <span className="text-[11px] font-mono text-zinc-500 bg-zinc-900/80 border border-zinc-800 px-2 py-0.5 rounded-[4px]">
                  {trace.steps.length} {trace.steps.length === 1 ? "step" : "steps"} • {trace.attempts.length} {trace.attempts.length === 1 ? "attempt" : "attempts"}
                </span>
              </div>

              <TraceTimeline trace={trace} />
            </div>
          </>
        )}
      </div>
    </AppLayout>
  );
}
