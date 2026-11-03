import React, { useState } from "react";
import {
  GitPullRequest,
  GitCommit,
  GitBranch,
  ArrowUpRight,
  ArrowRight,
  Copy,
  Check,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type { AssociatedPROut, CodeReviewOut } from "../types";
import { useAssociatedPr } from "../hooks/use-associated-prs";

const IDENTITY_PILL_BASE =
  "inline-flex items-center gap-1.5 rounded-[5px] px-2 py-0.5 text-sm font-mono font-bold border transition-colors";

export interface PrPillProps {
  prNumber: number;
  prUrl?: string;
  repoName?: string;
  repoOwner?: string;
  className?: string;
}

/**
 * Amber PR identity badge linking to the pull request on GitHub.
 */
export function PrPill({
  prNumber,
  prUrl,
  repoName,
  repoOwner,
  className,
}: PrPillProps) {
  const url =
    prUrl ||
    (repoOwner && repoName
      ? `https://github.com/${repoOwner}/${repoName}/pull/${prNumber}`
      : repoName
        ? `https://github.com/${repoName}/pull/${prNumber}`
        : undefined);

  const content = (
    <>
      <GitPullRequest className="h-3.5 w-3.5 shrink-0" />
      <span>PR #{prNumber}</span>
      {url && <ArrowUpRight className="h-2.5 w-2.5 opacity-70 shrink-0" />}
    </>
  );

  if (url) {
    return (
      <a
        href={url}
        target="_blank"
        rel="noreferrer"
        title={`View PR #${prNumber} on GitHub`}
        className={cn(
          IDENTITY_PILL_BASE,
          "bg-amber-500/15 text-amber-300 border-amber-400/40 hover:bg-amber-500/25 hover:text-amber-200 shadow-[inset_0_1px_0_rgba(255,255,255,0.06)]",
          className
        )}
      >
        {content}
      </a>
    );
  }

  return (
    <span
      className={cn(
        IDENTITY_PILL_BASE,
        "bg-amber-500/15 text-amber-300 border-amber-400/40 shadow-[inset_0_1px_0_rgba(255,255,255,0.06)]",
        className
      )}
    >
      {content}
    </span>
  );
}

export interface PushPillProps {
  commitSha: string;
  commitUrl?: string;
  repoName?: string;
  repoOwner?: string;
  className?: string;
}

/**
 * Zinc Push identity badge matching PR pill weight/dimensions for non-PR push runs.
 */
export function PushPill({
  commitSha,
  commitUrl,
  repoName,
  repoOwner,
  className,
}: PushPillProps) {
  const shortSha = commitSha ? commitSha.substring(0, 7) : "unknown";
  const url =
    commitUrl ||
    (repoOwner && repoName
      ? `https://github.com/${repoOwner}/${repoName}/commit/${commitSha}`
      : repoName
        ? `https://github.com/${repoName}/commit/${commitSha}`
        : undefined);

  const content = (
    <>
      <GitCommit className="h-3.5 w-3.5 text-zinc-300 shrink-0" />
      <span>Push @ {shortSha}</span>
      {url && <ArrowUpRight className="h-2.5 w-2.5 opacity-70 shrink-0" />}
    </>
  );

  if (url) {
    return (
      <a
        href={url}
        target="_blank"
        rel="noreferrer"
        title={`View commit ${shortSha} on GitHub`}
        className={cn(
          IDENTITY_PILL_BASE,
          "bg-zinc-800 text-zinc-100 border-zinc-600/60 hover:bg-zinc-750 hover:text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.08)]",
          className
        )}
      >
        {content}
      </a>
    );
  }

  return (
    <span
      className={cn(
        IDENTITY_PILL_BASE,
        "bg-zinc-800 text-zinc-100 border-zinc-600/60 shadow-[inset_0_1px_0_rgba(255,255,255,0.08)]",
        className
      )}
    >
      {content}
    </span>
  );
}

export interface IdentityPillProps {
  review: Pick<
    CodeReviewOut,
    "pr_number" | "pr_url" | "commit_sha" | "repo_name" | "repo_owner"
  > & { id?: string };
  associatedPr?: AssociatedPROut | null;
  className?: string;
}

/**
 * Polymorphic identity pill: resolves directly to PR pill if review has pr_number
 * or associated PR, otherwise cleanly renders Push pill.
 */
export function IdentityPill({
  review,
  associatedPr: propAssociatedPr,
  className,
}: IdentityPillProps) {
  const hookAssociatedPr = useAssociatedPr(review as CodeReviewOut);
  const associatedPr =
    propAssociatedPr !== undefined ? propAssociatedPr : hookAssociatedPr;

  if (review.pr_number != null) {
    return (
      <PrPill
        prNumber={review.pr_number}
        prUrl={review.pr_url ?? undefined}
        repoName={review.repo_name ?? undefined}
        repoOwner={review.repo_owner ?? undefined}
        className={className}
      />
    );
  }

  if (associatedPr && associatedPr.number != null) {
    return (
      <PrPill
        prNumber={associatedPr.number}
        prUrl={associatedPr.url}
        repoName={review.repo_name ?? undefined}
        repoOwner={review.repo_owner ?? undefined}
        className={className}
      />
    );
  }

  return (
    <PushPill
      commitSha={review.commit_sha}
      repoName={review.repo_name ?? undefined}
      repoOwner={review.repo_owner ?? undefined}
      className={className}
    />
  );
}

export interface CommitShaBadgeProps {
  commitSha: string;
  repoOwner?: string | null;
  repoName?: string | null;
  className?: string;
  showCopy?: boolean;
}

/**
 * Commit SHA badge with one-click copy and tactile feedback tooltip.
 */
export function CommitShaBadge({
  commitSha,
  repoOwner,
  repoName,
  className,
  showCopy = true,
}: CommitShaBadgeProps) {
  const [copied, setCopied] = useState(false);
  const shortSha = commitSha ? commitSha.substring(0, 7) : "unknown";

  const handleCopy = async (e: React.MouseEvent) => {
    e.stopPropagation();
    e.preventDefault();
    if (!commitSha) return;
    try {
      await navigator.clipboard.writeText(commitSha);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Fallback
    }
  };

  const commitUrl =
    repoOwner && repoName
      ? `https://github.com/${repoOwner}/${repoName}/commit/${commitSha}`
      : undefined;

  return (
    <div
      className={cn(
        "relative inline-flex items-center gap-1 rounded-[5px] border border-zinc-800 bg-zinc-900/90 px-1.5 py-0.5 font-mono text-[11px] text-zinc-300 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]",
        className
      )}
    >
      <GitCommit className="h-3 w-3 text-zinc-400 shrink-0" />
      {commitUrl ? (
        <a
          href={commitUrl}
          target="_blank"
          rel="noreferrer"
          className="hover:text-white hover:underline transition-colors"
          title={`View commit ${commitSha} on GitHub`}
        >
          {shortSha}
        </a>
      ) : (
        <span>{shortSha}</span>
      )}

      {showCopy && (
        <button
          type="button"
          onClick={handleCopy}
          aria-label={copied ? "Copied SHA" : "Copy full commit SHA"}
          title={copied ? "Copied SHA!" : "Copy full SHA"}
          className="ml-1 rounded p-0.5 text-zinc-400 hover:text-white hover:bg-zinc-800 transition-colors focus:outline-none focus:ring-1 focus:ring-zinc-600"
        >
          {copied ? (
            <Check className="h-2.5 w-2.5 text-emerald-400" />
          ) : (
            <Copy className="h-2.5 w-2.5" />
          )}
        </button>
      )}

      {/* Floating feedback tooltip */}
      {copied && (
        <div
          role="status"
          className="absolute -top-7 left-1/2 -translate-x-1/2 whitespace-nowrap rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] font-mono text-emerald-300 shadow-md border border-zinc-700/80 animate-in fade-in zoom-in-95 duration-100"
        >
          Copied SHA!
        </div>
      )}
    </div>
  );
}

export interface BranchFlowProps {
  headBranch?: string | null;
  baseBranch?: string | null;
  className?: string;
}

/**
 * Branch flow indicator showing source branch flowing into target base branch.
 */
export function BranchFlow({
  headBranch,
  baseBranch,
  className,
}: BranchFlowProps) {
  if (!headBranch && !baseBranch) return null;

  return (
    <div
      className={cn(
        "inline-flex items-center gap-1.5 text-[11px] font-mono",
        className
      )}
    >
      {headBranch && (
        <span className="inline-flex items-center gap-1 rounded-[4px] border border-zinc-800 bg-zinc-900/90 px-1.5 py-0.5 text-zinc-300 shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]">
          <GitBranch className="h-3 w-3 text-zinc-400 shrink-0" />
          <span className="truncate max-w-[140px]">{headBranch}</span>
        </span>
      )}

      {headBranch && baseBranch && (
        <ArrowRight className="h-3 w-3 text-zinc-500 shrink-0" />
      )}

      {baseBranch && (
        <span className="inline-flex items-center gap-1 rounded-[4px] border border-zinc-800/80 bg-zinc-950/60 px-1.5 py-0.5 text-zinc-400">
          <span className="truncate max-w-[140px]">{baseBranch}</span>
        </span>
      )}
    </div>
  );
}

export interface PrTitleProps {
  title?: string | null;
  prNumber?: number | null;
  prUrl?: string;
  className?: string;
}

/**
 * Clean PR title typography with optional link.
 */
export function PrTitle({
  title,
  prNumber,
  prUrl,
  className,
}: PrTitleProps) {
  if (!title) return null;

  if (prUrl) {
    return (
      <a
        href={prUrl}
        target="_blank"
        rel="noreferrer"
        className={cn(
          "font-medium text-zinc-100 hover:text-white hover:underline transition-colors line-clamp-1",
          className
        )}
      >
        {title}
      </a>
    );
  }

  return (
    <span
      className={cn(
        "font-medium text-zinc-100 line-clamp-1",
        className
      )}
    >
      {title}
    </span>
  );
}
