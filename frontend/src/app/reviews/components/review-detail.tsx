import React, { useState, useMemo } from "react";
import {
  ArrowLeft,
  ShieldCheck,
  ShieldAlert,
  AlertTriangle,
  FolderGit2,
  CheckCircle2,
  Zap,
  Boxes,
  Compass,
  Layers,
  Search,
  X,
  Check,
  GitCommit,
  GitBranch,
  Copy,
} from "lucide-react";
import { cn, formatRelativeTime } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type {
  CodeReviewOut,
  PriorityBucket,
  ReviewPerspective,
  ReviewFindingOut,
} from "../types";
import {
  isAnalysisFailed,
  isPublishFailed,
  isReviewPending,
  isSuppressed,
  isSuppressedUnscored,
  pendingLabelFor,
  getBlastStats,
  blastRadiusSummary,
} from "../lib/review-helpers";
import {
  blockerCounts,
  scoredOutcome,
  confidenceFromRisk,
  RISK_CRITICAL,
  RISK_MODERATE,
} from "../review-triage";
import { ConfidenceGauge } from "./confidence-gauge";
import { IdentityPill, BranchFlow } from "./review-identity";
import { FindingItem } from "./finding-item";
import { AppliedPrefsCard } from "./applied-prefs-card";
import { useIsPrAssociated } from "../hooks/use-associated-prs";

export interface ReviewDetailProps {
  review: CodeReviewOut;
  onClose?: () => void;
  onRetry?: () => void;
  className?: string;
}

const PERSPECTIVE_CONFIG: {
  id: ReviewPerspective;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
}[] = [
  { id: "all", label: "All", icon: Layers },
  { id: "security", label: "Security", icon: ShieldAlert },
  { id: "correctness", label: "Correctness", icon: CheckCircle2 },
  { id: "performance", label: "Performance", icon: Zap },
  { id: "architecture", label: "Architecture", icon: Boxes },
  { id: "holistic", label: "Holistic", icon: Compass },
];

function matchesPerspective(
  finding: ReviewFindingOut,
  perspective: ReviewPerspective
): boolean {
  if (perspective === "all") return true;
  const p = (finding.perspective || "").toLowerCase();
  const c = (finding.category || "").toLowerCase();
  if (p === perspective) return true;
  if (perspective === "correctness") {
    return c === "correctness" || c === "logic";
  }
  return c === perspective;
}

function resolveFindingPriority(finding: ReviewFindingOut): PriorityBucket {
  if (finding.priority) return finding.priority;
  const sev = (finding.severity || "").toLowerCase();
  if (sev === "critical" || sev === "high") return "must_fix";
  if (sev === "medium") return "should_fix";
  return "suggestion";
}

/**
 * Comprehensive Code Review Deep-Dive Inspector.
 *
 * Provides a high-density audit inspection surface including:
 * - PR/Push identity header with GitHub deep-links and navigation Back control
 * - Merge recommendation decision panel with risk score and blockers
 * - Structural and blast radius assessment
 * - Applied repository review preferences integration
 * - Multi-perspective analytical lenses (Security, Correctness, Performance, Architecture, Holistic)
 * - Priority-bucket triage filters and in-findings search
 * - Interactive FindingItem list with patch viewers and staged-fix persistence
 */
export function ReviewDetail({
  review,
  onClose,
  onRetry,
  className,
}: ReviewDetailProps) {
  const [copiedSha, setCopiedSha] = useState(false);
  const [copyShaError, setCopyShaError] = useState<string | null>(null);
  const [activePerspective, setActivePerspective] =
    useState<ReviewPerspective>("all");
  const [activePriority, setActivePriority] = useState<
    PriorityBucket | "all"
  >("all");
  const [findingsSearch, setFindingsSearch] = useState("");

  const isPrAssociated = useIsPrAssociated(review);
  const failed = isAnalysisFailed(review);
  const publishFailed = isPublishFailed(review);
  const pending =
    !failed &&
    !publishFailed &&
    (isReviewPending(review) || isSuppressedUnscored(review));
  const suppressed =
    !failed && !publishFailed && !pending && isSuppressed(review);
  const pendingLabel = isSuppressedUnscored(review)
    ? "Suppressed"
    : pendingLabelFor(review);
  const errorText = review.failure_reason ?? review.summary;

  const blast = getBlastStats(review);
  const confidence = confidenceFromRisk(review.risk_score);
  const blockers = blockerCounts(review.findings || []);
  const verdict = scoredOutcome(review);
  const radius = blastRadiusSummary(review);

  const repoName =
    review.repo_owner && review.repo_name
      ? `${review.repo_owner}/${review.repo_name}`
      : "Repository";
  const repoUrl =
    review.repo_owner && review.repo_name
      ? `https://github.com/${review.repo_owner}/${review.repo_name}`
      : undefined;
  const commitUrl =
    review.repo_owner && review.repo_name
      ? `https://github.com/${review.repo_owner}/${review.repo_name}/commit/${review.commit_sha}`
      : undefined;
  const commitShort = review.commit_sha
    ? review.commit_sha.substring(0, 7)
    : "unknown";

  const touchedFiles = useMemo(
    () => [...new Set((review.findings || []).map((f) => f.file_path))],
    [review.findings]
  );
  const shownFiles = touchedFiles.slice(0, 8);
  const hiddenFiles = touchedFiles.length - shownFiles.length;

  const suggestionsCount = useMemo(() => {
    return (review.findings || []).filter(
      (f) => resolveFindingPriority(f) === "suggestion"
    ).length;
  }, [review.findings]);

  const handleCopySha = async () => {
    if (!review.commit_sha) return;
    setCopyShaError(null);
    try {
      await navigator.clipboard.writeText(review.commit_sha);
      setCopiedSha(true);
      setTimeout(() => setCopiedSha(false), 2000);
    } catch {
      setCopyShaError("Copy failed — clipboard unavailable.");
    }
  };

  // Filter findings by perspective, priority, and search query
  const filteredFindings = useMemo(() => {
    return (review.findings || []).filter((f) => {
      if (!matchesPerspective(f, activePerspective)) return false;
      if (
        activePriority !== "all" &&
        resolveFindingPriority(f) !== activePriority
      ) {
        return false;
      }
      if (findingsSearch.trim()) {
        const query = findingsSearch.toLowerCase();
        const path = (f.file_path || "").toLowerCase();
        const critique = (f.critique || "").toLowerCase();
        const symbol = (f.symbol_name || "").toLowerCase();
        return (
          path.includes(query) ||
          critique.includes(query) ||
          symbol.includes(query)
        );
      }
      return true;
    });
  }, [review.findings, activePerspective, activePriority, findingsSearch]);

  return (
    <div
      role="region"
      aria-label="Code review detail audit report"
      className={cn("space-y-6", className)}
    >
      {/* Header identity card */}
      <div className="rounded-xl border-t border-t-zinc-700/60 border-x border-x-zinc-800/80 border-b border-b-zinc-900 bg-gradient-to-b from-[#141418] via-[#101013] to-[#0a0a0d] p-5 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_8px_20px_rgba(0,0,0,0.4)]">
        <div className="flex items-center justify-between gap-3 border-b border-zinc-800/70 pb-3">
          <div className="text-[11px] font-mono font-semibold uppercase tracking-[0.16em] text-zinc-400">
            Haunter Autonomous Audit Report
          </div>
          {onClose && (
            <Button
              variant="outline"
              size="sm"
              onClick={onClose}
              className="h-7 gap-1.5 border-zinc-700 bg-zinc-900 px-2.5 text-xs text-zinc-200 hover:bg-zinc-800 hover:text-white active:translate-y-[0.5px]"
              aria-label="Back to review list"
            >
              <ArrowLeft className="h-3.5 w-3.5" />
              <span>Back to Reviews</span>
            </Button>
          )}
        </div>

        <div className="mt-3 flex flex-col lg:flex-row lg:items-start justify-between gap-5">
          <div className="flex-1 min-w-0 space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              {repoUrl ? (
                <a
                  href={repoUrl}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1.5 font-mono text-sm font-bold text-zinc-100 hover:text-white transition-colors"
                  title={`View repository ${repoName} on GitHub`}
                >
                  <FolderGit2 className="h-4 w-4 text-zinc-400 shrink-0" />
                  <span>{repoName}</span>
                </a>
              ) : (
                <span className="inline-flex items-center gap-1.5 font-mono text-sm font-bold text-zinc-100">
                  <FolderGit2 className="h-4 w-4 text-zinc-400 shrink-0" />
                  <span>{repoName}</span>
                </span>
              )}

              <IdentityPill review={review} />

              {failed && (
                <span className="inline-flex items-center rounded-md border border-rose-500/40 bg-rose-950/60 px-2 py-0.5 text-[10px] font-mono font-bold uppercase tracking-wider text-rose-300">
                  Error
                </span>
              )}

              {pending && (
                <span className="inline-flex items-center rounded-md border border-zinc-700/60 bg-zinc-900 px-2 py-0.5 text-[10px] font-mono font-bold uppercase tracking-wider text-zinc-300">
                  {pendingLabel}
                </span>
              )}

              {suppressed && (
                <span
                  className="inline-flex items-center rounded-md border border-zinc-700/60 bg-zinc-900 px-2 py-0.5 text-[10px] font-mono font-bold uppercase tracking-wider text-zinc-300"
                  title="Analysis finished — publishing to GitHub was skipped by repo settings"
                >
                  Publication skipped
                </span>
              )}

              {publishFailed && (
                <span
                  className="inline-flex items-center rounded-md border border-rose-500/40 bg-rose-950/60 px-2 py-0.5 text-[10px] font-mono font-bold uppercase tracking-wider text-rose-300"
                  title={
                    review.failure_reason
                      ? `Analysis finished and audit saved, but publishing to GitHub failed: ${review.failure_reason}`
                      : "Analysis finished and audit saved, but publishing to GitHub failed."
                  }
                >
                  Publication failed
                </span>
              )}

              {review.pr_author && (
                <div className="inline-flex items-center gap-1.5 rounded-full bg-zinc-900 border border-zinc-800 px-2 py-0.5 text-xs text-zinc-300">
                  {review.pr_author_avatar ? (
                    <img
                      src={review.pr_author_avatar}
                      alt={review.pr_author}
                      className="h-3.5 w-3.5 rounded-full object-cover"
                    />
                  ) : (
                    <span className="flex h-3.5 w-3.5 items-center justify-center rounded-full bg-zinc-800 text-[9px] font-bold text-zinc-300">
                      {review.pr_author[0]?.toUpperCase()}
                    </span>
                  )}
                  <span className="font-mono text-xs text-zinc-300">
                    @{review.pr_author}
                  </span>
                </div>
              )}
            </div>

            {review.pr_title && (
              <h2 className="text-xl font-bold tracking-tight text-zinc-50 leading-snug">
                {review.pr_title}
              </h2>
            )}

            <div className="flex flex-wrap items-center gap-2 text-[11px] font-mono">
              <BranchFlow
                headBranch={review.head_branch}
                baseBranch={review.base_branch}
              />

              {isPrAssociated ? (
                <div className="inline-flex items-center rounded bg-zinc-900/80 border border-zinc-800 px-1.5 py-0.5 text-xs text-zinc-400">
                  <a
                    href={
                      commitUrl ||
                      `https://github.com/${repoName}/commit/${review.commit_sha}`
                    }
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-1 hover:text-zinc-200 transition-colors pr-1.5 border-r border-zinc-800"
                    title="View commit on GitHub"
                  >
                    <GitCommit className="h-3 w-3 text-zinc-500" />
                    <span>{commitShort}</span>
                  </a>
                  <button
                    type="button"
                    onClick={handleCopySha}
                    className="rounded-sm pl-1.5 hover:text-zinc-200 transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-amber-400/70"
                    title="Copy commit SHA"
                    aria-label="Copy commit SHA"
                  >
                    {copiedSha ? (
                      <Check className="h-3 w-3 text-emerald-400" />
                    ) : (
                      <Copy className="h-3 w-3 text-zinc-400 hover:text-zinc-200" />
                    )}
                  </button>
                </div>
              ) : (
                <button
                  type="button"
                  onClick={handleCopySha}
                  className="rounded-sm text-zinc-400 hover:text-zinc-200 transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-amber-400/70"
                  title="Copy commit SHA"
                  aria-label="Copy commit SHA"
                >
                  {copiedSha ? (
                    <Check className="h-3.5 w-3.5 text-emerald-400" />
                  ) : (
                    <Copy className="h-3.5 w-3.5" />
                  )}
                </button>
              )}

              {copyShaError && (
                <p role="alert" className="text-[11px] font-mono text-rose-400">
                  {copyShaError}
                </p>
              )}

              <span className="text-zinc-400">
                {formatRelativeTime(review.created_at)}
              </span>
            </div>
          </div>

          <div className="flex items-start justify-start lg:justify-center">
            <ConfidenceGauge
              riskScore={review.risk_score}
              size={84}
              neutral={failed || pending}
              ariaLabel={
                failed || pending
                  ? "No confidence score — the analysis did not complete"
                  : undefined
              }
            />
          </div>
        </div>
      </div>

      {/* Honest states branching */}
      {failed ? (
        <>
          <div className="flex items-start justify-between gap-3 rounded-xl border border-rose-500/40 bg-rose-950/30 px-4 py-3 text-xs leading-relaxed text-rose-200 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]">
            <span role="alert" className="flex items-start gap-2">
              <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0 text-rose-400" />
              <span className="font-mono">{errorText}</span>
            </span>
            {onRetry && (
              <Button
                variant="outline"
                size="sm"
                onClick={onRetry}
                className="h-7 shrink-0 border-rose-500/40 text-xs text-rose-200 hover:bg-rose-900/40 active:translate-y-[0.5px]"
              >
                Retry
              </Button>
            )}
          </div>

          <section className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
            <div className="rounded-xl border border-rose-500/30 bg-[#0f0f12] p-4 space-y-1">
              <div className="text-[11px] font-mono font-semibold uppercase tracking-wider text-zinc-400">
                Status
              </div>
              <span className="inline-flex items-center rounded-md border border-rose-500/40 bg-rose-950/60 px-2.5 py-1 text-xs font-mono font-semibold text-rose-300">
                Error
              </span>
            </div>
            <div className="rounded-xl border border-zinc-800/80 bg-[#0f0f12] p-4 space-y-1">
              <div className="text-[11px] font-mono font-semibold uppercase tracking-wider text-zinc-400">
                PR Confidence
              </div>
              <div
                className="font-mono text-xl font-bold text-zinc-500"
                title="No score — analysis did not complete"
              >
                —
              </div>
              <div className="text-[11px] text-zinc-400">
                No score — analysis did not complete
              </div>
            </div>
            <div className="rounded-xl border border-zinc-800/80 bg-[#0f0f12] p-4 space-y-1">
              <div className="text-[11px] font-mono font-semibold uppercase tracking-wider text-zinc-400">
                Blockers
              </div>
              <div className="font-mono text-sm text-zinc-500">—</div>
            </div>
            <div className="rounded-xl border border-zinc-800/80 bg-[#0f0f12] p-4 space-y-1">
              <div className="text-[11px] font-mono font-semibold uppercase tracking-wider text-zinc-400">
                Blast Radius
              </div>
              <div className="font-mono text-sm text-zinc-500">—</div>
            </div>
          </section>

          <section className="rounded-xl border border-zinc-800/80 bg-[#0f0f12] p-5">
            <h3 className="text-xs font-mono font-semibold uppercase tracking-[0.16em] text-zinc-300">
              Structural &amp; Blast Radius Analysis
            </h3>
            <p className="mt-2 text-xs text-zinc-400 leading-relaxed">
              Unavailable — the analysis did not complete, so there is no
              structural telemetry to report.
            </p>
          </section>

          <section className="space-y-3">
            <div className="flex items-center justify-between text-xs font-semibold text-zinc-400 uppercase tracking-wider font-mono">
              <span>Findings Summary (0)</span>
            </div>
            <div className="flex items-center gap-2 rounded-xl border border-zinc-800 bg-zinc-950/60 px-4 py-3 text-xs font-mono text-zinc-500">
              No findings — the analysis did not complete.
            </div>
          </section>
        </>
      ) : pending ? (
        <>
          <section className="rounded-xl border border-zinc-800/80 bg-[#0f0f12] p-5">
            <h3 className="text-xs font-mono font-semibold uppercase tracking-[0.16em] text-zinc-300">
              PR Summary
            </h3>
            <p className="mt-2 text-sm text-zinc-300 leading-relaxed">
              {review.summary}
            </p>
          </section>

          <section className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
            <div className="rounded-xl border border-zinc-800/80 bg-[#0f0f12] p-4 space-y-1">
              <div className="text-[11px] font-mono font-semibold uppercase tracking-wider text-zinc-400">
                Status
              </div>
              <span className="inline-flex items-center rounded-md border border-zinc-700/60 bg-zinc-900 px-2.5 py-1 text-xs font-mono font-semibold text-zinc-300">
                {pendingLabel}
              </span>
            </div>
            <div className="rounded-xl border border-zinc-800/80 bg-[#0f0f12] p-4 space-y-1">
              <div className="text-[11px] font-mono font-semibold uppercase tracking-wider text-zinc-400">
                PR Confidence
              </div>
              <div
                className="font-mono text-xl font-bold text-zinc-500"
                title="No score — analysis has not completed"
              >
                —
              </div>
              <div className="text-[11px] text-zinc-400">
                No score — analysis has not completed
              </div>
            </div>
            <div className="rounded-xl border border-zinc-800/80 bg-[#0f0f12] p-4 space-y-1">
              <div className="text-[11px] font-mono font-semibold uppercase tracking-wider text-zinc-400">
                Blockers
              </div>
              <div className="font-mono text-sm text-zinc-500">—</div>
            </div>
            <div className="rounded-xl border border-zinc-800/80 bg-[#0f0f12] p-4 space-y-1">
              <div className="text-[11px] font-mono font-semibold uppercase tracking-wider text-zinc-400">
                Blast Radius
              </div>
              <div className="font-mono text-sm text-zinc-500">—</div>
            </div>
          </section>

          <section className="rounded-xl border border-zinc-800/80 bg-[#0f0f12] p-5">
            <h3 className="text-xs font-mono font-semibold uppercase tracking-[0.16em] text-zinc-300">
              Structural &amp; Blast Radius Analysis
            </h3>
            <p className="mt-2 text-xs text-zinc-400 leading-relaxed">
              Unavailable — the analysis has not completed yet, so there is no
              structural telemetry to report.
            </p>
          </section>

          <section className="space-y-3">
            <div className="flex items-center justify-between text-xs font-semibold text-zinc-400 uppercase tracking-wider font-mono">
              <span>Findings Summary (0)</span>
            </div>
            <div className="flex items-center gap-2 rounded-xl border border-zinc-800 bg-zinc-950/60 px-4 py-3 text-xs font-mono text-zinc-500">
              No findings yet — analysis in progress.
            </div>
          </section>
        </>
      ) : (
        <>
          {/* PR Summary card */}
          <section className="rounded-xl border border-zinc-800/80 bg-[#0f0f12] p-5">
            <h3 className="text-[10px] font-mono font-semibold uppercase tracking-[0.16em] text-zinc-400">
              PR Summary
            </h3>
            <p className="mt-2 max-w-[75ch] text-sm leading-relaxed text-zinc-200">
              {review.summary}
            </p>
          </section>

          {/* Decision panel: verdict, risk score, blockers */}
          <section className="overflow-hidden rounded-xl border border-zinc-800/80 bg-[#0f0f12]">
            <div className="flex flex-col gap-4 border-b border-zinc-800/80 p-5 sm:flex-row sm:items-center sm:justify-between">
              <div className="min-w-0 space-y-1.5">
                <div className="text-[10px] font-mono font-semibold uppercase tracking-[0.16em] text-zinc-400">
                  Merge recommendation
                </div>
                <div
                  className={cn(
                    "flex flex-wrap items-center gap-2 text-lg font-bold tracking-tight",
                    verdict.band.text
                  )}
                >
                  <ShieldCheck className="h-5 w-5 shrink-0" aria-hidden="true" />
                  <span>{verdict.label}</span>
                </div>
                <p className="text-xs leading-relaxed text-zinc-300 max-w-[65ch]">
                  {verdict.meaning}
                </p>
              </div>

              <dl className="grid shrink-0 grid-cols-4 gap-px overflow-hidden rounded-lg border border-zinc-800/80 bg-zinc-800/80 font-mono sm:w-auto">
                <div className="bg-zinc-950/90 px-3.5 py-2.5">
                  <dt className="text-[10px] uppercase tracking-wider text-zinc-400">
                    Risk
                  </dt>
                  <dd
                    className={cn(
                      "text-base font-bold tabular-nums",
                      verdict.band.text
                    )}
                  >
                    {review.risk_score}
                    <span className="text-[10px] text-zinc-500">/100</span>
                  </dd>
                </div>
                <div className="bg-zinc-950/90 px-3.5 py-2.5">
                  <dt className="text-[10px] uppercase tracking-wider text-zinc-400">
                    Must-Fix
                  </dt>
                  <dd
                    className={cn(
                      "text-base font-bold tabular-nums",
                      blockers.mustFix > 0 ? "text-rose-400" : "text-emerald-400"
                    )}
                  >
                    {blockers.mustFix}
                  </dd>
                </div>
                <div className="bg-zinc-950/90 px-3.5 py-2.5">
                  <dt className="text-[10px] uppercase tracking-wider text-zinc-400">
                    Should-Fix
                  </dt>
                  <dd
                    className={cn(
                      "text-base font-bold tabular-nums",
                      blockers.shouldFix > 0
                        ? "text-amber-400"
                        : "text-emerald-400"
                    )}
                  >
                    {blockers.shouldFix}
                  </dd>
                </div>
                <div className="bg-zinc-950/90 px-3.5 py-2.5">
                  <dt className="text-[10px] uppercase tracking-wider text-zinc-400">
                    Notes
                  </dt>
                  <dd className="text-base font-bold tabular-nums text-zinc-300">
                    {suggestionsCount}
                  </dd>
                </div>
              </dl>
            </div>

            {/* Supporting telemetry facts */}
            <dl className="grid grid-cols-1 gap-px bg-zinc-800/80 sm:grid-cols-2 lg:grid-cols-4">
              <div className="bg-zinc-950/90 px-4 py-3 space-y-0.5">
                <dt className="text-[10px] uppercase tracking-wider text-zinc-400 font-semibold">
                  Confidence
                </dt>
                <dd className="text-sm font-bold text-zinc-100">
                  {confidence}/10
                  <span className="ml-1.5 text-[10px] font-normal text-zinc-500">
                    in gauge ring
                  </span>
                </dd>
              </div>
              <div className="bg-zinc-950/90 px-4 py-3 space-y-0.5">
                <dt className="text-[10px] uppercase tracking-wider text-zinc-400 font-semibold">
                  Diff
                </dt>
                <dd className="font-mono text-sm text-zinc-100">
                  {blast.filesCount}{" "}
                  {blast.filesCount === 1 ? "file" : "files"}{" "}
                  <span className="text-emerald-400">+{blast.additions}</span>{" "}
                  <span className="text-rose-400">-{blast.deletions}</span>
                </dd>
              </div>
              <div className="bg-zinc-950/90 px-4 py-3 space-y-0.5">
                <dt className="text-[10px] uppercase tracking-wider text-zinc-400 font-semibold">
                  Blast Radius
                </dt>
                <dd className="font-mono text-sm font-bold text-zinc-100">
                  {radius.label}
                </dd>
                <dd className="text-[10px] text-zinc-400 font-sans">
                  {radius.explanation}
                </dd>
              </div>
              <div className="bg-zinc-950/90 px-4 py-3 space-y-0.5">
                <dt className="text-[10px] uppercase tracking-wider text-zinc-400 font-semibold">
                  Analysis Cost
                </dt>
                <dd className="font-mono text-sm text-zinc-100">
                  {review.input_tokens + review.output_tokens} tok
                </dd>
                <dd className="text-[10px] text-zinc-400 uppercase">
                  {review.status}
                </dd>
              </div>
            </dl>
          </section>

          {/* Structural & Blast Radius Analysis */}
          <section className="rounded-xl border border-zinc-800/80 bg-[#0f0f12] p-5 space-y-3">
            <h3 className="text-xs font-mono font-semibold uppercase tracking-[0.16em] text-zinc-300">
              Structural &amp; Blast Radius Analysis
            </h3>
            <ul className="space-y-2.5 text-xs text-zinc-300 leading-relaxed list-disc pl-5">
              <li>
                <span className="font-semibold text-zinc-100">
                  Impact Surface:
                </span>{" "}
                {radius.label} — {radius.explanation}.
              </li>
              <li>
                <span className="font-semibold text-zinc-100">
                  Files Modified:
                </span>{" "}
                <span className="font-mono">
                  {blast.filesCount} {blast.filesCount === 1 ? "file" : "files"}
                </span>{" "}
                <span className="font-mono text-emerald-400">
                  +{blast.additions}
                </span>{" "}
                <span className="font-mono text-zinc-500">/</span>{" "}
                <span className="font-mono text-rose-400">
                  -{blast.deletions}
                </span>{" "}
                <span
                  className={cn(
                    "ml-1 px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wider border",
                    blast.scopeColor
                  )}
                >
                  {blast.scope} mutation scope
                </span>
              </li>
              {touchedFiles.length > 0 && (
                <li>
                  <span className="font-semibold text-zinc-100">
                    Touched Components:
                  </span>{" "}
                  <span className="inline-flex flex-wrap gap-1.5 align-middle">
                    {shownFiles.map((f) => (
                      <span
                        key={f}
                        className="font-mono text-[10px] text-zinc-300 bg-zinc-900 px-1.5 py-0.5 rounded border border-zinc-800"
                      >
                        {f}
                      </span>
                    ))}
                    {hiddenFiles > 0 && (
                      <span className="font-mono text-[10px] text-zinc-400">
                        +{hiddenFiles} more
                      </span>
                    )}
                  </span>
                </li>
              )}
              <li>
                <span className="font-semibold text-zinc-100">
                  Risk Assessment:
                </span>{" "}
                {blockers.mustFix > 0
                  ? `Risk ${review.risk_score}/100 with ${blockers.mustFix} must-fix ${
                      blockers.mustFix === 1 ? "finding" : "findings"
                    } — address blockers before merge.`
                  : review.risk_score >= RISK_CRITICAL
                    ? `Risk ${review.risk_score}/100 is at or above the critical bar and no must-fix finding is named — the score alone requests changes; review before merging.`
                    : review.risk_score < RISK_MODERATE
                      ? `Risk ${review.risk_score}/100 with no must-fix findings — safe to merge pending routine review.`
                      : `Risk ${review.risk_score}/100 with no must-fix findings — merge after a focused review pass.`}
              </li>
            </ul>
          </section>

          {/* Applied Review Preferences */}
          {review.applied_review_prefs && (
            <AppliedPrefsCard
              appliedPrefs={review.applied_review_prefs}
              defaultExpanded={false}
            />
          )}

          {/* Analytical perspectives & findings section */}
          <section className="space-y-4">
            <div className="flex items-center justify-between text-xs font-semibold text-zinc-400 uppercase tracking-wider font-mono">
              <span>Findings Summary ({(review.findings || []).length})</span>
              {(review.findings || []).length > 0 && (
                <span className="text-[10px] text-zinc-500 font-normal">
                  Pre-verified candidate patches
                </span>
              )}
            </div>

            <div className="flex flex-col gap-3 rounded-xl border border-zinc-800/80 bg-[#0f0f12] p-3 md:flex-row md:items-center md:justify-between">
              {/* Multi-perspective tabs */}
              <div
                className="flex flex-wrap items-center gap-1"
                role="tablist"
                aria-label="Filter findings by analytical lens"
              >
                {PERSPECTIVE_CONFIG.map((persp) => {
                  const Icon = persp.icon;
                  const isSelected = activePerspective === persp.id;
                  const count = (review.findings || []).filter((f) =>
                    matchesPerspective(f, persp.id)
                  ).length;

                  return (
                    <button
                      key={persp.id}
                      type="button"
                      role="tab"
                      aria-selected={isSelected}
                      onClick={() => setActivePerspective(persp.id)}
                      className={cn(
                        "inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-mono transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400 active:translate-y-[0.5px]",
                        isSelected
                          ? "bg-zinc-800 font-semibold text-zinc-100 shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_1px_3px_rgba(0,0,0,0.3)] border border-zinc-700/80"
                          : "text-zinc-400 hover:text-zinc-200 border border-transparent hover:bg-zinc-900/60"
                      )}
                    >
                      <Icon className="h-3.5 w-3.5 text-zinc-400" />
                      <span>{persp.label}</span>
                      <span className="text-[10px] text-zinc-500 font-normal">
                        ({count})
                      </span>
                    </button>
                  );
                })}
              </div>

              {/* Priority bucket pills */}
              <div
                className="flex items-center rounded-lg border border-zinc-800 bg-zinc-950 p-1 text-[11px]"
                role="group"
                aria-label="Filter by priority bucket"
              >
                {(
                  [
                    { id: "all", label: "All" },
                    { id: "must_fix", label: "Must Fix" },
                    { id: "should_fix", label: "Should Fix" },
                    { id: "suggestion", label: "Notes" },
                  ] as const
                ).map((p) => {
                  const isSelected = activePriority === p.id;
                  return (
                    <button
                      key={p.id}
                      type="button"
                      onClick={() => setActivePriority(p.id)}
                      aria-pressed={isSelected}
                      className={cn(
                        "rounded-md px-2 py-0.5 font-mono uppercase text-[10px] tracking-wider transition-all focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-amber-400 active:translate-y-[0.5px]",
                        isSelected
                          ? "bg-zinc-800 font-semibold text-zinc-100 shadow-[0_1px_3px_rgba(0,0,0,0.3)]"
                          : "text-zinc-400 hover:text-zinc-200"
                      )}
                    >
                      {p.label}
                    </button>
                  );
                })}
              </div>
            </div>

            {/* Findings search bar */}
            <div className="relative">
              <Search
                className="absolute left-3 top-2.5 h-3.5 w-3.5 text-zinc-400 pointer-events-none"
                aria-hidden="true"
              />
              <Input
                placeholder="Search findings by symbol, critique or file..."
                value={findingsSearch}
                onChange={(e) => setFindingsSearch(e.target.value)}
                className="pl-9 pr-8 h-8 text-xs bg-[#0f0f12] border-zinc-800 text-zinc-100 placeholder:text-zinc-500 focus:border-amber-400/80"
                aria-label="Filter findings"
              />
              {findingsSearch && (
                <button
                  type="button"
                  onClick={() => setFindingsSearch("")}
                  aria-label="Clear findings search"
                  className="absolute right-2.5 top-2 rounded p-0.5 text-zinc-400 hover:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-amber-400"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              )}
            </div>

            {/* Findings List or Empty states */}
            {(review.findings || []).length === 0 ? (
              <div className="flex items-center gap-2.5 rounded-xl border border-emerald-500/30 bg-emerald-950/20 px-4 py-3.5 text-xs font-mono text-emerald-300">
                <Check className="h-4 w-4 text-emerald-400 shrink-0" />
                <span>
                  Clean audit — zero vulnerabilities or blockers detected. No regressions detected — no actionable findings on this review.
                </span>
              </div>
            ) : filteredFindings.length === 0 ? (
              <div className="flex flex-col items-center justify-center rounded-xl border border-zinc-800 bg-zinc-950/60 p-8 text-center">
                <p className="text-xs font-mono text-zinc-400">
                  No findings match the selected lens or search criteria.
                </p>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    setActivePerspective("all");
                    setActivePriority("all");
                    setFindingsSearch("");
                  }}
                  className="mt-3 text-xs border-zinc-700 bg-zinc-900 text-zinc-300 hover:text-white"
                >
                  Reset finding filters
                </Button>
              </div>
            ) : (
              <div className="space-y-3">
                {filteredFindings.map((finding, idx) => (
                  <FindingItem
                    key={idx}
                    finding={finding}
                    repoOwner={review.repo_owner}
                    repoName={review.repo_name}
                    commitSha={review.commit_sha}
                  />
                ))}
              </div>
            )}
          </section>

          {/* Footer Metadata Fact Bar */}
          <section className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-zinc-800/80 bg-zinc-950/80 px-4 py-2.5 text-[11px] font-mono text-zinc-400">
            <div className="flex flex-wrap items-center gap-4">
              <span>
                Engine:{" "}
                <span className="text-zinc-300">Haunter Reviewer v2.0</span>
              </span>
              <span>
                Tokens:{" "}
                <span className="text-zinc-300">
                  {review.input_tokens} in / {review.output_tokens} out
                </span>
              </span>
              <span>
                Findings:{" "}
                <span className="text-zinc-300">
                  {review.findings?.length || 0}
                </span>
              </span>
            </div>
            <div className="text-zinc-500">ID: {review.id}</div>
          </section>
        </>
      )}
    </div>
  );
}
