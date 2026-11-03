import React, { useState } from "react";
import {
  ExternalLink,
  Copy,
  Check,
  FileDiff,
  Boxes,
  ChevronDown,
  ChevronRight,
  Code2,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type { ReviewFindingOut, PriorityBucket } from "../types";
import {
  getCategoryMeta,
  PriorityBadge,
  SeverityBadge,
} from "../lib/category-meta";
import {
  isFixStaged,
  toggleStagedFix,
} from "../lib/review-helpers";
import { DiffViewer } from "./diff-viewer";

export interface FindingItemProps {
  /** The finding data from review analysis */
  finding: ReviewFindingOut & { non_commentable?: boolean };
  /** Repository owner (e.g. 'acme') */
  repoOwner?: string | null;
  /** Repository name (e.g. 'sentinel') */
  repoName?: string | null;
  /** Commit SHA to anchor file links */
  commitSha?: string;
  /** Optional override for staged state */
  isStaged?: boolean;
  /** Optional callback when fix staging state toggles */
  onToggleStage?: (finding: ReviewFindingOut, staged: boolean) => void;
  /** Optional container class name */
  className?: string;
}

/**
 * Resolves priority bucket from explicit priority or severity fallback.
 */
function resolvePriority(finding: ReviewFindingOut): PriorityBucket {
  if (finding.priority) return finding.priority;
  const sev = (finding.severity ?? "").toLowerCase();
  if (sev === "critical" || sev === "high") return "must_fix";
  if (sev === "medium") return "should_fix";
  return "suggestion";
}

/**
 * Finding Item component with direct GitHub file link, architectural callout mode,
 * syntax-highlighted diff viewer, and dual tactile actions (Copy Patch & Stage Fix).
 */
export function FindingItem({
  finding,
  repoOwner,
  repoName,
  commitSha,
  isStaged: isStagedProp,
  onToggleStage,
  className,
}: FindingItemProps) {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState<string | null>(null);

  const scope = { repoOwner, repoName, commitSha };
  const [localStaged, setLocalStaged] = useState<boolean>(() =>
    isFixStaged(finding, scope)
  );
  const [stageError, setStageError] = useState<string | null>(null);
  const [patchOpen, setPatchOpen] = useState(true);

  const isStaged = isStagedProp ?? localStaged;
  const priority = resolvePriority(finding);
  const catMeta = getCategoryMeta(finding.category);
  const CatIcon = catMeta.icon;

  // Architectural / Non-commentable finding predicate
  const isArchitectural = Boolean(
    finding.non_commentable === true ||
      finding.line_start === 0 ||
      finding.file_path === "(general)"
  );

  const handleCopy = async () => {
    if (!finding.suggested_patch) return;
    setCopyError(null);
    try {
      await navigator.clipboard.writeText(finding.suggested_patch);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopyError("Copy failed — clipboard unavailable.");
    }
  };

  const handleStage = () => {
    if (!finding.suggested_patch) return;
    setStageError(null);
    try {
      const nextStaged = toggleStagedFix(finding, scope);
      setLocalStaged(nextStaged);
      onToggleStage?.(finding, nextStaged);
    } catch {
      setStageError("Couldn't stage fix in this browser.");
    }
  };

  const lineRangeSuffix =
    finding.line_start > 0
      ? `#L${finding.line_start}${
          finding.line_end && finding.line_end !== finding.line_start
            ? `-L${finding.line_end}`
            : ""
        }`
      : "";

  const githubFileUrl =
    !isArchitectural && repoOwner && repoName && commitSha && finding.line_start > 0
      ? `https://github.com/${repoOwner}/${repoName}/blob/${commitSha}/${finding.file_path}${lineRangeSuffix}`
      : undefined;

  return (
    <div
      className={cn(
        "rounded-lg border p-4 space-y-3 transition-colors",
        isArchitectural
          ? "border-indigo-500/30 bg-gradient-to-b from-indigo-950/20 via-zinc-950/70 to-zinc-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]"
          : "border-zinc-800/80 bg-zinc-950/60 shadow-[inset_0_1px_0_rgba(255,255,255,0.03)] hover:border-zinc-700/80",
        className
      )}
    >
      {/* Header bar: category + priority + severity + file/architectural badge */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 flex-wrap">
          {/* Category pill */}
          <span className={catMeta.badgeClass}>
            <CatIcon className="h-3 w-3 shrink-0" />
            {catMeta.label}
          </span>

          {/* Priority bucket badge */}
          <PriorityBadge priority={priority} />

          {/* Severity badge */}
          <SeverityBadge severity={finding.severity} />

          {/* AST / Symbol badge if present */}
          {finding.symbol_name && (
            <span className="inline-flex items-center gap-1 font-mono text-[10px] text-zinc-300 bg-zinc-900 px-1.5 py-0.5 rounded border border-zinc-800 shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]">
              <Code2 className="h-2.5 w-2.5 text-zinc-500" />
              <span className="text-zinc-500 font-sans">fn:</span>{" "}
              {finding.symbol_name}
            </span>
          )}
        </div>

        {/* File Anchor or Architectural Callout */}
        {isArchitectural ? (
          <div
            data-testid="architectural-tag"
            className="inline-flex items-center gap-1.5 font-mono text-[11px] font-semibold text-indigo-300 bg-indigo-950/40 px-2 py-0.5 rounded-[4px] border border-indigo-500/30 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]"
            title="System architecture or repository-level critique without line-anchored attachment"
          >
            <Boxes className="h-3.5 w-3.5 text-indigo-400" />
            <span>SYSTEM ARCHITECTURE</span>
          </div>
        ) : githubFileUrl ? (
          <a
            href={githubFileUrl}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1.5 font-mono text-xs text-zinc-300 hover:text-white bg-zinc-900/90 hover:bg-zinc-850 px-2 py-0.5 rounded border border-zinc-800 hover:border-zinc-700 transition-colors shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]"
            title="Open in GitHub at exact line number"
          >
            <span>
              {finding.file_path}:{finding.line_start}
              {finding.line_end !== finding.line_start
                ? `-${finding.line_end}`
                : ""}
            </span>
            <ExternalLink className="h-3 w-3 text-zinc-400 shrink-0" />
          </a>
        ) : (
          <div className="font-mono text-xs text-zinc-400 bg-zinc-900/80 px-2 py-0.5 rounded border border-zinc-800">
            {finding.file_path}:{finding.line_start}
            {finding.line_end !== finding.line_start
              ? `-${finding.line_end}`
              : ""}
          </div>
        )}
      </div>

      {/* Critique prose */}
      <p className="text-xs text-zinc-200 leading-relaxed font-sans">
        {finding.critique}
      </p>

      {/* Candidate Remediation Patch Section */}
      {finding.suggested_patch && (
        <div className="mt-2 space-y-2 border-t border-zinc-800/60 pt-2.5">
          <div className="flex items-center justify-between text-[11px] text-zinc-400 font-mono">
            <button
              type="button"
              onClick={() => setPatchOpen(!patchOpen)}
              aria-expanded={patchOpen}
              className="inline-flex items-center gap-1 text-zinc-400 hover:text-zinc-200 uppercase tracking-wider text-[10px] font-semibold transition-colors focus:outline-none"
            >
              {patchOpen ? (
                <ChevronDown className="h-3 w-3" />
              ) : (
                <ChevronRight className="h-3 w-3" />
              )}
              <span>Candidate Remediation Patch</span>
            </button>

            <div className="flex items-center gap-2">
              {/* Secondary tactile action: Copy Patch */}
              <button
                type="button"
                onClick={handleCopy}
                className="inline-flex items-center gap-1.5 h-6 px-2.5 rounded-[5px] text-[11px] font-mono font-medium bg-gradient-to-b from-zinc-800 via-zinc-850 to-zinc-900 border-t border-t-zinc-700/60 border-x border-x-zinc-800/60 border-b border-b-zinc-950 text-zinc-200 shadow-[inset_0_1px_0_rgba(255,255,255,0.1),0_1px_3px_rgba(0,0,0,0.3)] hover:text-white active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.4)] transition-all"
              >
                {copied ? (
                  <>
                    <Check className="h-3 w-3 text-emerald-400" />
                    <span className="text-emerald-300">Copied</span>
                  </>
                ) : (
                  <>
                    <Copy className="h-3 w-3 text-zinc-400" />
                    <span>Copy Patch</span>
                  </>
                )}
              </button>

              {/* Primary/Toggle tactile action: Stage Fix */}
              <button
                type="button"
                onClick={handleStage}
                title={
                  isStaged
                    ? "Staged in this browser — click to unstage"
                    : "Stage this fix in this browser (server-side apply pending)"
                }
                className={cn(
                  "inline-flex items-center gap-1.5 h-6 px-2.5 rounded-[5px] text-[11px] font-mono font-medium transition-all active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.4)]",
                  isStaged
                    ? "bg-gradient-to-b from-emerald-600/90 via-emerald-700 to-emerald-800 border-t border-t-emerald-300/60 border-x border-x-emerald-600/60 border-b border-b-emerald-950 text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.2),0_1px_3px_rgba(0,0,0,0.3)]"
                    : "bg-gradient-to-b from-blue-600/90 via-blue-700 to-blue-800 border-t border-t-blue-300/60 border-x border-x-blue-600/60 border-b border-b-blue-950 text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.2),0_1px_3px_rgba(0,0,0,0.3)] hover:brightness-105"
                )}
              >
                {isStaged ? (
                  <>
                    <Check className="h-3 w-3 text-emerald-200" />
                    <span>Staged Fix</span>
                  </>
                ) : (
                  <>
                    <FileDiff className="h-3 w-3 text-blue-200" />
                    <span>Stage Fix</span>
                  </>
                )}
              </button>
            </div>
          </div>

          {(copyError || stageError) && (
            <p role="alert" className="text-[11px] font-mono text-rose-400">
              {copyError ?? stageError}
            </p>
          )}

          {patchOpen && <DiffViewer patch={finding.suggested_patch} />}
        </div>
      )}
    </div>
  );
}
