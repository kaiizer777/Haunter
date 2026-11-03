import React, { useState, useMemo } from "react";
import {
  FolderGit2,
  AlertTriangle,
  RefreshCw,
  ChevronDown,
  ChevronUp,
  ArrowUpRight,
  GitCommit,
  Check,
  Copy,
  ShieldCheck,
  Tag,
} from "lucide-react";
import { cn, formatRelativeTime } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import type { CodeReviewOut } from "../types";
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
  pendingOutcome,
  SUPPRESSED_UNSCORED_OUTCOME,
} from "../review-triage";
import { ConfidenceGauge } from "./confidence-gauge";
import {
  IdentityPill,
  BranchFlow,
  CommitShaBadge,
} from "./review-identity";
import { FindingItem } from "./finding-item";
import { useIsPrAssociated } from "../hooks/use-associated-prs";

export const ROW_SURFACE =
  "rounded-xl border-t border-t-zinc-700/60 border-x border-x-zinc-800/80 border-b border-b-zinc-900 bg-gradient-to-b from-[#141418] via-[#101013] to-[#0a0a0d] shadow-[inset_0_1px_0_rgba(255,255,255,0.05),0_6px_20px_rgba(0,0,0,0.4)]";

export interface RowIdentityProps {
  review: CodeReviewOut;
  chip?: React.ReactNode;
  compact?: boolean;
}

export function RowIdentity({ review, chip, compact = false }: RowIdentityProps) {
  const repoName =
    review.repo_owner && review.repo_name
      ? `${review.repo_owner}/${review.repo_name}`
      : "Repository";
  const repoUrl =
    review.repo_owner && review.repo_name
      ? `https://github.com/${review.repo_owner}/${review.repo_name}`
      : undefined;

  const nameClass = compact
    ? "inline-flex items-center gap-1.5 font-mono text-[13px] font-bold text-zinc-100 hover:text-white transition-colors"
    : "inline-flex items-center gap-1.5 font-mono text-sm font-bold text-zinc-100 hover:text-white transition-colors";

  return (
    <div className="flex flex-wrap items-center gap-x-2.5 gap-y-2">
      {repoUrl ? (
        <a
          href={repoUrl}
          target="_blank"
          rel="noreferrer"
          className={nameClass}
          title={`View repository ${repoName} on GitHub`}
        >
          <FolderGit2 className="h-3.5 w-3.5 text-zinc-400 shrink-0" />
          <span>{repoName}</span>
        </a>
      ) : (
        <span className={nameClass}>
          <FolderGit2 className="h-3.5 w-3.5 text-zinc-400 shrink-0" />
          <span>{repoName}</span>
        </span>
      )}

      <IdentityPill review={review} />
      {chip}

      {review.pr_author && (
        <span className="inline-flex items-center gap-1.5 rounded-full bg-zinc-900 border border-zinc-800 px-2 py-0.5 text-[11px] text-zinc-300">
          {review.pr_author_avatar ? (
            <img
              src={review.pr_author_avatar}
              alt=""
              className="h-3.5 w-3.5 rounded-full object-cover"
            />
          ) : (
            <span className="flex h-3.5 w-3.5 items-center justify-center rounded-full bg-zinc-800 text-[9px] font-bold text-zinc-300">
              {review.pr_author[0]?.toUpperCase()}
            </span>
          )}
          <span className="font-mono">@{review.pr_author}</span>
        </span>
      )}

      <span className="text-xs font-mono text-zinc-400">
        {formatRelativeTime(review.created_at)}
      </span>
    </div>
  );
}

export interface MetaFactProps {
  label: string;
  value: React.ReactNode;
  tone?: string;
  title?: string;
}

export function MetaFact({ label, value, tone, title }: MetaFactProps) {
  return (
    <span className="inline-flex items-baseline gap-1.5" title={title}>
      <span className="text-[10px] font-mono uppercase tracking-wider text-zinc-400 font-medium">
        {label}
      </span>
      <span className={cn("font-mono text-xs", tone ?? "text-zinc-300")}>
        {value}
      </span>
    </span>
  );
}

export interface FailedReviewCardProps {
  review: CodeReviewOut;
  onOpen: (id: string) => void;
  onRetry?: () => void;
  className?: string;
}

/**
 * Honest error card for analyses that failed to complete.
 * Renders neutral confidence gauge and unredacted error details.
 */
export function FailedReviewCard({
  review,
  onOpen,
  onRetry,
  className,
}: FailedReviewCardProps) {
  const [expanded, setExpanded] = useState(false);
  const [copiedError, setCopiedError] = useState(false);
  const [copyError, setCopyError] = useState<string | null>(null);

  const errorText = review.failure_reason ?? review.summary;
  const panelId = `failure-detail-${review.id}`;
  const commitUrl =
    review.repo_owner && review.repo_name
      ? `https://github.com/${review.repo_owner}/${review.repo_name}/commit/${review.commit_sha}`
      : undefined;
  const commitShort = review.commit_sha
    ? review.commit_sha.substring(0, 7)
    : "unknown";
  const identityLabel = `${review.repo_owner ?? "Repository"}/${
    review.repo_name ?? ""
  }${review.pr_number ? ` PR #${review.pr_number}` : ` Push @ ${commitShort}`}`;

  const handleCopyError = async () => {
    setCopyError(null);
    try {
      await navigator.clipboard.writeText(errorText);
      setCopiedError(true);
      setTimeout(() => setCopiedError(false), 2000);
    } catch {
      setCopyError("Copy failed — clipboard unavailable.");
    }
  };

  const errorChip = (
    <span className="inline-flex items-center gap-1 rounded-md border border-rose-500/40 bg-rose-500/10 px-2 py-0.5 text-[10px] font-mono font-bold uppercase tracking-wider text-rose-200">
      <AlertTriangle className="h-2.5 w-2.5 text-rose-400" aria-hidden="true" />
      Analysis failed
    </span>
  );

  return (
    <div
      className={cn(ROW_SURFACE, "border-rose-500/25", className)}
      data-testid="failed-review-card"
    >
      <div className="flex flex-col gap-4 p-5">
        <div className="flex flex-col gap-5 md:flex-row md:items-start">
          <div className="flex items-center gap-4 md:min-w-[84px] md:flex-col md:justify-center">
            <ConfidenceGauge riskScore={review.risk_score} size={64} neutral />
          </div>

          <div className="min-w-0 flex-1 space-y-3">
            <RowIdentity review={review} chip={errorChip} compact />

            {review.pr_title && (
              <h3 className="text-[15px] font-bold leading-snug tracking-tight text-zinc-50">
                {review.pr_title}
              </h3>
            )}

            <div className="rounded-lg border border-rose-500/30 bg-rose-950/20 px-3 py-2.5">
              <div className="flex items-start gap-2">
                <AlertTriangle
                  className="mt-0.5 h-3.5 w-3.5 shrink-0 text-rose-400"
                  aria-hidden="true"
                />
                <p className="max-h-32 min-w-0 flex-1 overflow-y-auto font-mono text-[11px] leading-relaxed break-words text-rose-100">
                  {errorText}
                </p>
              </div>
              {copyError && (
                <p className="mt-1.5 text-[11px] font-mono text-rose-300">
                  {copyError}
                </p>
              )}
            </div>

            <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
              <MetaFact
                label="Status"
                value={<span className="text-rose-300">Failed</span>}
              />
              <MetaFact
                label="Risk"
                value={<span>Not scored</span>}
                tone="text-zinc-500"
                title="No score — the analysis never produced a risk assessment"
              />
              <a
                href={
                  commitUrl ||
                  `https://github.com/${review.repo_owner}/${review.repo_name}/commit/${review.commit_sha}`
                }
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 font-mono text-xs text-zinc-400 hover:text-zinc-100 transition-colors"
                title="View commit on GitHub"
              >
                <GitCommit className="h-3 w-3 text-zinc-400" aria-hidden="true" />
                {commitShort}
              </a>
            </div>
          </div>

          <div className="flex shrink-0 items-start gap-2 md:w-[164px] md:justify-end">
            {onRetry && (
              <Button
                variant="outline"
                size="sm"
                onClick={onRetry}
                aria-label={`Retry loading reviews (failed: ${identityLabel})`}
                className="h-7 shrink-0 gap-1.5 border-rose-500/40 bg-rose-950/20 px-2.5 text-xs text-rose-200 hover:bg-rose-900/40 active:translate-y-[0.5px]"
              >
                <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
                Retry
              </Button>
            )}
            <Button
              variant="outline"
              size="sm"
              onClick={() => setExpanded((v) => !v)}
              aria-expanded={expanded}
              aria-controls={panelId}
              aria-label={
                expanded
                  ? `Collapse error details for ${identityLabel}`
                  : `Expand error details for ${identityLabel}`
              }
              className="h-7 shrink-0 gap-1.5 border-zinc-700 bg-zinc-900/80 px-2.5 text-xs text-zinc-200 hover:bg-zinc-800 active:translate-y-[0.5px]"
            >
              <span>{expanded ? "Collapse" : "Details"}</span>
              {expanded ? (
                <ChevronUp className="h-3.5 w-3.5" aria-hidden="true" />
              ) : (
                <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />
              )}
            </Button>
          </div>
        </div>

        {expanded && (
          <div
            id={panelId}
            className="space-y-4 border-t border-zinc-800/80 pt-4"
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h4 className="text-[11px] font-mono font-semibold uppercase tracking-[0.16em] text-zinc-400">
                Failure details
              </h4>
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleCopyError}
                  className="h-6 gap-1 border-zinc-700 bg-zinc-900 px-2 text-[11px] text-zinc-300 hover:text-white"
                >
                  {copiedError ? (
                    <>
                      <Check className="h-3 w-3 text-emerald-400" />
                      Copied!
                    </>
                  ) : (
                    <>
                      <Copy className="h-3 w-3 text-zinc-400" />
                      Copy error
                    </>
                  )}
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => onOpen(review.id)}
                  className="h-6 gap-1 border-zinc-700 bg-zinc-900 px-2 text-[11px] text-zinc-300 hover:text-white"
                >
                  <span>Full audit</span>
                  <ArrowUpRight className="h-3 w-3" />
                </Button>
              </div>
            </div>

            <pre className="max-h-64 overflow-auto rounded-lg border border-zinc-800 bg-zinc-950 p-3 font-mono text-xs text-zinc-300 whitespace-pre-wrap break-words">
              {review.failure_reason || review.summary}
            </pre>
          </div>
        )}
      </div>
    </div>
  );
}

export interface PendingReviewCardProps {
  review: CodeReviewOut;
  onOpen: (id: string) => void;
  className?: string;
}

/**
 * Honest pending card for in-flight or unscored suppressed reviews.
 */
export function PendingReviewCard({
  review,
  onOpen,
  className,
}: PendingReviewCardProps) {
  const label = isSuppressedUnscored(review)
    ? "Suppressed"
    : pendingLabelFor(review);
  const outcome = isSuppressedUnscored(review)
    ? SUPPRESSED_UNSCORED_OUTCOME
    : pendingOutcome(review.status);

  const statusChip = (
    <span className="inline-flex items-center gap-1.5 rounded-md border border-zinc-700 bg-zinc-900 px-2 py-0.5 text-[10px] font-mono font-semibold uppercase tracking-wider text-zinc-300">
      <span className="relative flex h-1.5 w-1.5" aria-hidden="true">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-zinc-400 opacity-60" />
        <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-zinc-400" />
      </span>
      {label}
    </span>
  );

  return (
    <div
      className={cn(ROW_SURFACE, className)}
      data-testid="pending-review-card"
    >
      <div className="flex flex-col gap-5 p-5 md:flex-row md:items-start">
        <div className="flex items-center gap-4 md:min-w-[84px] md:flex-col md:justify-center">
          <ConfidenceGauge riskScore={review.risk_score} size={64} neutral />
        </div>

        <div className="min-w-0 flex-1 space-y-3">
          <RowIdentity review={review} chip={statusChip} compact />

          {review.pr_title && (
            <h3 className="text-[15px] font-bold leading-snug tracking-tight text-zinc-50">
              {review.pr_title}
            </h3>
          )}

          <p className="text-xs leading-relaxed text-zinc-400">
            {review.summary}
          </p>

          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <MetaFact
              label="Status"
              value={<span className="text-zinc-300">{label}</span>}
            />
            <MetaFact
              label="Risk"
              value={<span className="text-zinc-500">Not scored</span>}
              tone="text-zinc-500"
            />
          </div>
        </div>

        <div className="flex shrink-0 items-start gap-2 md:w-[164px] md:justify-end">
          <Button
            variant="outline"
            size="sm"
            onClick={() => onOpen(review.id)}
            aria-label={`Full audit for ${review.repo_owner ?? "Repository"}/${
              review.repo_name ?? ""
            }${review.pr_number ? ` PR #${review.pr_number}` : ""}${
              review.pr_title ? ` — ${review.pr_title}` : ""
            } (${review.id})`}
            className="h-7 shrink-0 gap-1.5 border-zinc-700 bg-zinc-900/80 px-2.5 text-xs text-zinc-200 hover:bg-zinc-800 active:translate-y-[0.5px]"
          >
            <span>Full audit</span>
            <ArrowUpRight className="h-3.5 w-3.5" aria-hidden="true" />
          </Button>
        </div>
      </div>
      <span className="sr-only">{outcome.meaning}</span>
    </div>
  );
}

export interface ReviewCardProps {
  review: CodeReviewOut;
  onOpen: (id: string) => void;
  onRetry?: () => void;
  className?: string;
}

/**
 * Main High-Density Code Review Card.
 *
 * Renders completed, pending, or failed reviews with calibrated tactile depth,
 * multi-tier telemetry facts, category breakdown chips, and expandable findings accordion.
 */
export function ReviewCard({
  review,
  onOpen,
  onRetry,
  className,
}: ReviewCardProps) {
  const [expanded, setExpanded] = useState(false);
  const findingsPanelId = `findings-${review.id}`;
  const isPrAssociated = useIsPrAssociated(review);

  // Category counts
  const categoryCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const f of review.findings || []) {
      const cat = f.category?.toLowerCase() || "general";
      counts[cat] = (counts[cat] || 0) + 1;
    }
    return counts;
  }, [review.findings]);

  // Fail-closed routing to dedicated honest card components
  if (isAnalysisFailed(review)) {
    return (
      <FailedReviewCard
        review={review}
        onOpen={onOpen}
        onRetry={onRetry}
        className={className}
      />
    );
  }

  if (isReviewPending(review) || isSuppressedUnscored(review)) {
    return (
      <PendingReviewCard
        review={review}
        onOpen={onOpen}
        className={className}
      />
    );
  }

  // Scored review telemetry
  const blast = getBlastStats(review);
  const blockers = blockerCounts(review.findings || []);
  const verdict = scoredOutcome(review);
  const radius = blastRadiusSummary(review);
  const suppressed = isSuppressed(review);
  const publishFailed = isPublishFailed(review);
  const findingCount = (review.findings || []).length;

  const verdictChip = (
    <span
      className={cn(
        "inline-flex items-center whitespace-nowrap rounded-md border px-2 py-0.5 text-[10px] font-mono font-semibold shadow-[inset_0_1px_0_rgba(255,255,255,0.08)]",
        verdict.band.chip
      )}
    >
      <span
        className={cn(
          "mr-1.5 h-1.5 w-1.5 shrink-0 rounded-full",
          verdict.band.dot
        )}
        aria-hidden="true"
      />
      {verdict.label}
    </span>
  );

  return (
    <div
      className={cn(
        ROW_SURFACE,
        "transition-all duration-150 hover:border-zinc-700/80 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_8px_24px_rgba(0,0,0,0.5)]",
        className
      )}
    >
      <div className="flex flex-col gap-5 p-5 md:flex-row md:items-start">
        {/* Left: Tactical Confidence Gauge */}
        <div className="flex items-center gap-4 md:min-w-[84px] md:flex-col md:justify-center">
          <ConfidenceGauge riskScore={review.risk_score} size={64} />
        </div>

        {/* Center: Identity, PR Title, summary, decision line, facts */}
        <div className="min-w-0 flex-1 space-y-3">
          <RowIdentity review={review} chip={verdictChip} />

          {/* Secondary identity badges */}
          <div className="flex flex-wrap items-center gap-2 text-[11px] font-mono">
            {suppressed && (
              <span
                className="inline-flex items-center rounded-md border border-zinc-700 bg-zinc-900 px-2 py-0.5 text-[10px] text-zinc-300"
                title="Analysis finished — publishing to GitHub was skipped by repo settings"
              >
                Publication skipped
              </span>
            )}

            {publishFailed && (
              <span
                className="inline-flex items-center rounded-md border border-rose-500/30 bg-rose-950/40 px-2 py-0.5 text-[10px] text-rose-300"
                title={
                  review.failure_reason
                    ? `Analysis finished and audit saved, but publishing failed: ${review.failure_reason}`
                    : "Analysis finished and audit saved, but publishing to GitHub failed."
                }
              >
                Publication failed
              </span>
            )}

            <BranchFlow
              headBranch={review.head_branch}
              baseBranch={review.base_branch}
            />

            <CommitShaBadge
              commitSha={review.commit_sha}
              repoOwner={review.repo_owner}
              repoName={review.repo_name}
            />
          </div>

          {/* PR Title */}
          {review.pr_title && (
            <h3 className="text-[15px] font-bold leading-snug tracking-tight text-zinc-50">
              {review.pr_title}
            </h3>
          )}

          {/* Summary */}
          <p className="max-w-[75ch] text-xs leading-relaxed text-zinc-300">
            {review.summary}
          </p>

          {/* Decision line: Blocker counts & Category breakdown tags */}
          <div className="flex flex-wrap items-center gap-2 pt-1">
            {blockers.mustFix > 0 ? (
              <span className="inline-flex items-center gap-1 rounded-md border border-rose-500/50 bg-rose-950/50 px-2 py-0.5 text-[11px] font-mono font-bold text-rose-300 shadow-[inset_0_1px_0_rgba(255,255,255,0.06)]">
                {blockers.mustFix} must fix
              </span>
            ) : null}

            {blockers.shouldFix > 0 ? (
              <span className="inline-flex items-center gap-1 rounded-md border border-amber-500/50 bg-amber-950/50 px-2 py-0.5 text-[11px] font-mono font-bold text-amber-300 shadow-[inset_0_1px_0_rgba(255,255,255,0.06)]">
                {blockers.shouldFix} should fix
              </span>
            ) : null}

            {blockers.mustFix === 0 && blockers.shouldFix === 0 ? (
              <span className="inline-flex items-center gap-1 rounded-md border border-emerald-500/40 bg-emerald-950/40 px-2 py-0.5 text-[11px] font-mono font-medium text-emerald-300">
                <ShieldCheck className="h-3 w-3 text-emerald-400" />
                0 blockers
              </span>
            ) : null}

            {/* Category breakdown chips */}
            {Object.entries(categoryCounts).map(([cat, count]) => (
              <span
                key={cat}
                className="inline-flex items-center gap-1 rounded-md border border-zinc-800 bg-zinc-900/80 px-2 py-0.5 font-mono text-[10px] text-zinc-400"
              >
                <Tag className="h-2.5 w-2.5 text-zinc-500" />
                <span className="uppercase">{cat}</span>
                <span className="text-zinc-200 font-bold">{count}</span>
              </span>
            ))}
          </div>

          {/* Telemetry Fact Line */}
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 font-mono text-[11px] pt-1">
            <MetaFact
              label="Risk"
              value={`${review.risk_score}/100`}
              tone={cn("tabular-nums", verdict.band.text)}
            />
            <MetaFact
              label="Diff"
              value={
                <span>
                  {blast.filesCount}{" "}
                  {blast.filesCount === 1 ? "file" : "files"}{" "}
                  <span className="text-emerald-400">+{blast.additions}</span>
                  <span className="text-zinc-500">/</span>
                  <span className="text-rose-400">-{blast.deletions}</span>
                </span>
              }
            />
            <MetaFact
              label="Blast"
              value={radius.label}
              tone="text-[10px] font-semibold uppercase tracking-wider text-zinc-300"
              title={radius.explanation}
            />
            <MetaFact
              label="Tokens"
              value={`${review.input_tokens + review.output_tokens} tok`}
              tone="text-zinc-400"
            />
          </div>
        </div>

        {/* Right column: Action CTAs */}
        <div className="flex shrink-0 items-start gap-2 md:w-[170px] md:flex-col md:items-end">
          <Button
            variant="default"
            size="sm"
            onClick={() => onOpen(review.id)}
            aria-label={`Inspect audit for ${
              review.repo_owner && review.repo_name
                ? `${review.repo_owner}/${review.repo_name}`
                : "repository"
            }${review.pr_number ? ` PR #${review.pr_number}` : ""}`}
            className="h-8 gap-1.5 border-t border-t-amber-400/60 border-x border-x-amber-600/50 border-b border-b-amber-900 bg-gradient-to-b from-amber-500 via-amber-600 to-amber-700 px-3 text-xs font-semibold text-zinc-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.3),0_2px_6px_rgba(0,0,0,0.3)] hover:brightness-105 active:translate-y-[0.5px]"
          >
            <span>Inspect Audit</span>
            <ArrowUpRight className="h-3.5 w-3.5 shrink-0" />
          </Button>

          {findingCount > 0 && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => setExpanded((v) => !v)}
              aria-expanded={expanded}
              aria-controls={findingsPanelId}
              className="h-7 gap-1 border-zinc-700 bg-zinc-900/80 px-2.5 text-xs text-zinc-200 hover:bg-zinc-800 active:translate-y-[0.5px]"
            >
              <span>{expanded ? "Collapse" : `Findings (${findingCount})`}</span>
              {expanded ? (
                <ChevronUp className="h-3.5 w-3.5" />
              ) : (
                <ChevronDown className="h-3.5 w-3.5" />
              )}
            </Button>
          )}
        </div>
      </div>

      {/* Expandable Findings Accordion */}
      {expanded && findingCount > 0 && (
        <div
          id={findingsPanelId}
          className="border-t border-zinc-800/80 bg-zinc-950/40 p-5 space-y-3"
        >
          <div className="flex items-center justify-between text-xs font-mono font-semibold uppercase tracking-wider text-zinc-400">
            <span>Inline Findings ({findingCount})</span>
            <span className="text-[10px] text-zinc-500 font-normal">
              Pre-verified candidate patches
            </span>
          </div>

          <div className="space-y-3">
            {review.findings.map((finding, idx) => (
              <FindingItem
                key={idx}
                finding={finding}
                repoOwner={review.repo_owner}
                repoName={review.repo_name}
                commitSha={review.commit_sha}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
