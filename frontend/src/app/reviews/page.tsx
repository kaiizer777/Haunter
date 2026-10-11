"use client";

import { useEffect, useState, useCallback, useMemo, useRef } from "react";
import { AppLayout } from "@/components/layout/app-layout";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { SelectDropdown } from "@/components/ui/select-dropdown";
import {
  api,
  CodeReviewOut,
  RepoOut,
  ReviewFindingOut,
} from "@/lib/api";
import { formatRelativeTime, cn } from "@/lib/utils";
import {
  ShieldCheck,
  ShieldAlert,
  AlertTriangle,
  GitCommit,
  GitPullRequest,
  GitBranch,
  FolderGit2,
  Search,
  RefreshCw,
  Copy,
  Check,
  ChevronDown,
  ChevronUp,
  Bug,
  Zap,
  Lock,
  Boxes,
  Sparkles,
  ArrowUpRight,
  ArrowLeft,
  ExternalLink,
  EyeOff,
  FileDiff,
} from "lucide-react";

const PAGE_SIZE = 15;

/**
 * Single source of truth for the high-risk boundary. Ribbon counts,
 * the Risk toggle + `min_risk` API param, page stats, verdicts, gauges and
 * blast-scope heuristics all derive from these — previously the ribbon said
 * >=71 while the toggle sent min_risk=50 and the gauge used >70.
 */
const RISK_CRITICAL = 71;
const RISK_MODERATE = 31;

/** A review whose analysis never completed — never render verdict telemetry for it.
 * Failed iff the backend reports status "error". A stale `failure_reason` on a
 * completed review must NOT hide its real verdict (the API type allows the
 * combination), so `failure_reason` alone is intentionally ignored here. */
function isReviewFailed(review: CodeReviewOut): boolean {
  return review.status?.toLowerCase() === "error";
}

/** A review with no finished analysis to report — never render verdict telemetry for it.
 * Fail-closed: only "completed" and "suppressed" carry a real score.
 * "suppressed" is terminal WITH a saved audit: the orchestrator persists
 * risk_score + summary + findings BEFORE the publish guard
 * (backend/app/services/review_orchestrator.py:1233-1239), then records
 * status="suppressed" when enable_pr_comments is false (:1287) keeping the
 * saved data. Everything else — "pending" (row created at enqueue with
 * risk_score 0 + findings [], backend/app/webhooks.py:2271 + :2455),
 * "in_progress" (orchestrator running, risk_score still the 0 default until
 * post-analysis) or anything unknown — renders neutrally. Without this gate
 * a queued row reads as Great-to-Merge / SAFE with 10/10 confidence.
 * Checked after isReviewFailed; "error" rows keep the honest error panel. */
function isReviewPending(review: CodeReviewOut): boolean {
  const s = review.status?.toLowerCase();
  return s !== "completed" && s !== "error" && s !== "suppressed";
}

/** Publication was skipped by repo settings, but the saved audit is real.
 * Zinc/neutral indicator only — never hides verdict telemetry. Scored
 * suppressed rows render the saved audit exactly like completed (same card +
 * detail branches); suppressed is terminal so no Retry, like completed.
 * Unscored suppressed rows (see isSuppressedUnscored) render neutrally. */
function isSuppressed(review: CodeReviewOut): boolean {
  return review.status?.toLowerCase() === "suppressed";
}

/** Suppressed row with no saved audit — legacy row or suppress-before-save
 * race: risk_score still the 0 column default AND findings empty. Fail-closed:
 * renders neutrally like pending, never as Great-to-Merge / SAFE. Scored
 * suppressed rows (real audit) are unaffected. */
function isSuppressedUnscored(review: CodeReviewOut): boolean {
  return isSuppressed(review) && review.risk_score === 0 && review.findings.length === 0;
}

/** Neutral status-pill label for unscored rows. Reuses existing zinc pill chrome.
 * Only non-terminal rows reach this via isReviewPending; unscored suppressed
 * rows label themselves "Suppressed" at the PendingReviewCard gate below. */
function pendingLabelFor(review: CodeReviewOut): string {
  const s = review.status?.toLowerCase();
  if (s === "pending") return "Queued";
  return "Analyzing";
}

/**
 * Calibrated tactile physical gauge for Risk Score (0-100).
 * Replaces unearned neon glow with a clean circular bezel,
 * neutral dark contact shadow, and high-contrast score.
 *
 * `neutral` renders the honest no-score state for failed analyses:
 * zinc track, em-dash, NO SCORE badge — never a fake 0/SAFE.
 */
function RiskGauge({ score, size = 68, neutral = false }: { score: number; size?: number; neutral?: boolean }) {
  const strokeWidth = 5;
  const radius = (size - strokeWidth - 8) / 2;
  const circumference = 2 * Math.PI * radius;
  const clampedScore = Math.max(0, Math.min(100, score));
  const offset = neutral ? circumference : circumference - (clampedScore / 100) * circumference;

  let strokeColor = "#10b981"; // emerald
  let textClass = "text-emerald-400";
  let label = "SAFE";
  let badgeColor = "bg-emerald-950/60 text-emerald-300 border-emerald-500/30";

  if (neutral) {
    strokeColor = "#52525b"; // zinc — no verdict implied
    textClass = "text-zinc-500";
    label = "NO SCORE";
    badgeColor = "bg-zinc-900 text-zinc-500 border-zinc-700/60";
  } else if (clampedScore >= RISK_CRITICAL) {
    strokeColor = "#f43f5e"; // rose
    textClass = "text-rose-400";
    label = "CRITICAL";
    badgeColor = "bg-rose-950/60 text-rose-300 border-rose-500/30";
  } else if (clampedScore >= RISK_MODERATE) {
    strokeColor = "#f59e0b"; // amber
    textClass = "text-amber-400";
    label = "MODERATE";
    badgeColor = "bg-amber-950/60 text-amber-300 border-amber-500/30";
  }

  return (
    <div className="flex flex-col items-center justify-center">
      {/* Tactile circular bezel housing */}
      <div
        className="relative flex items-center justify-center rounded-full border border-zinc-800/90 bg-gradient-to-b from-[#18181b] via-[#111114] to-[#09090b] shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_2px_8px_rgba(0,0,0,0.5)]"
        style={{ width: size, height: size }}
      >
        <svg className="rotate-[-90deg]" width={size} height={size}>
          <circle
            cx={size / 2}
            cy={size / 2}
            r={radius}
            stroke="#27272a"
            strokeWidth={strokeWidth}
            fill="transparent"
          />
          <circle
            cx={size / 2}
            cy={size / 2}
            r={radius}
            stroke={strokeColor}
            strokeWidth={strokeWidth}
            fill="transparent"
            strokeDasharray={circumference}
            strokeDashoffset={offset}
            strokeLinecap="round"
            style={{
              transition: "stroke-dashoffset 0.6s cubic-bezier(0.16, 1, 0.3, 1)",
            }}
          />
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center">
          <span className={cn("font-mono text-base font-extrabold tabular-nums tracking-tight", textClass)}>
            {neutral ? "—" : clampedScore}
          </span>
        </div>
      </div>
      <span className={cn("mt-1.5 rounded-[4px] border px-2 py-0.5 text-[10px] font-mono font-bold tracking-wider uppercase", badgeColor)}>
        {label}
      </span>
    </div>
  );
}

/**
 * Finding Category Icon & Color Helper
 */
function getCategoryMeta(category: string) {
  const cat = category.toLowerCase();
  switch (cat) {
    case "security":
      return {
        icon: Lock,
        color: "text-rose-400 border-rose-500/30 bg-rose-950/40",
        label: "Security",
      };
    case "logic":
      return {
        icon: Bug,
        color: "text-amber-400 border-amber-500/30 bg-amber-950/40",
        label: "Logic & Edge Cases",
      };
    case "performance":
      return {
        icon: Zap,
        color: "text-cyan-400 border-cyan-500/30 bg-cyan-950/40",
        label: "Performance",
      };
    case "api_compatibility":
      return {
        icon: Boxes,
        color: "text-purple-400 border-purple-500/30 bg-purple-950/40",
        label: "API Compatibility",
      };
    default:
      return {
        icon: AlertTriangle,
        color: "text-zinc-400 border-zinc-700 bg-zinc-900/60",
        label: category,
      };
  }
}

function getSeverityBadge(severity: string) {
  const sev = severity.toLowerCase();
  switch (sev) {
    case "critical":
      return (
        <span className="rounded-[4px] border border-rose-500/40 bg-rose-950/60 px-1.5 py-0.5 text-[10px] font-mono font-bold uppercase tracking-wider text-rose-400 shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_1px_2px_rgba(0,0,0,0.3)]">
          CRITICAL
        </span>
      );
    case "high":
      return (
        <span className="rounded-[4px] border border-amber-500/40 bg-amber-950/60 px-1.5 py-0.5 text-[10px] font-mono font-bold uppercase tracking-wider text-amber-400">
          HIGH
        </span>
      );
    case "medium":
      return (
        <span className="rounded-[4px] border border-yellow-500/30 bg-yellow-950/40 px-1.5 py-0.5 text-[10px] font-mono font-medium uppercase tracking-wider text-yellow-300">
          MEDIUM
        </span>
      );
    default:
      return (
        <span className="rounded-[4px] border border-zinc-700 bg-zinc-900/80 px-1.5 py-0.5 text-[10px] font-mono font-medium uppercase tracking-wider text-zinc-400">
          LOW
        </span>
      );
  }
}

/**
 * Derived audit-report metrics. All client-side derivations from the GET-only
 * review payload — no backend writes involved.
 */

/** PR Confidence x/10 derived from risk score: low risk ⇒ high confidence. */
function confidenceFromRisk(riskScore: number): number {
  const clamped = Math.max(0, Math.min(100, riskScore));
  return Math.max(1, Math.min(10, Math.round((100 - clamped) / 10)));
}

/** Blocker counts: Must-Fix = critical+high findings, Should-Fix = medium. */
function blockerCounts(review: CodeReviewOut): { mustFix: number; shouldFix: number } {
  let mustFix = 0;
  let shouldFix = 0;
  for (const f of review.findings) {
    const sev = f.severity.toLowerCase();
    if (sev === "critical" || sev === "high") mustFix++;
    else if (sev === "medium") shouldFix++;
  }
  return { mustFix, shouldFix };
}

/** Blast-radius telemetry shared by the overview strip and the detail report. */
function getBlastStats(review: CodeReviewOut): {
  filesCount: number;
  additions: number;
  deletions: number;
  scope: "Targeted" | "Moderate" | "Extensive";
  scopeColor: string;
} {
  const filesCount = review.diff_stats?.files_changed ?? new Set(review.findings.map((f) => f.file_path)).size;
  const additions = review.diff_stats?.additions ?? review.findings.reduce((acc, f) => {
    if (!f.suggested_patch) return acc;
    const added = (f.suggested_patch.match(/^\+[^+]/gm) || []).length;
    return acc + (added || 1);
  }, 0);
  const deletions = review.diff_stats?.deletions ?? review.findings.reduce((acc, f) => {
    if (!f.suggested_patch) return acc;
    const removed = (f.suggested_patch.match(/^-[^-]/gm) || []).length;
    return acc + (removed || 0);
  }, 0);

  const totalModifications = additions + deletions;
  let scope: "Targeted" | "Moderate" | "Extensive" = "Targeted";
  let scopeColor = "border-emerald-500/30 bg-emerald-950/40 text-emerald-300";

  if (filesCount > 8 || totalModifications > 250 || review.risk_score >= RISK_CRITICAL) {
    scope = "Extensive";
    scopeColor = "border-rose-500/30 bg-rose-950/40 text-rose-300";
  } else if (filesCount > 3 || totalModifications > 60 || review.risk_score >= RISK_MODERATE) {
    scope = "Moderate";
    scopeColor = "border-amber-500/30 bg-amber-950/40 text-amber-300";
  }
  return { filesCount, additions, deletions, scope, scopeColor };
}

/** Merge verdict for the detail report header grid. Fills are deliberately
 * saturated (500/20 + 200-text) so the verdict wins the eye as the single
 * Tier-3 status moment per card — semantics unchanged. */
function verdictFor(review: CodeReviewOut): { label: string; color: string } {
  const { mustFix, shouldFix } = blockerCounts(review);
  if (mustFix > 0 || review.risk_score >= RISK_CRITICAL) {
    return { label: "Changes Requested", color: "bg-rose-500/20 text-rose-200 border-rose-400/50 shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_1px_2px_rgba(0,0,0,0.3)]" };
  }
  if (review.risk_score < RISK_MODERATE && shouldFix === 0) {
    return { label: "Great to Merge", color: "bg-emerald-500/20 text-emerald-200 border-emerald-400/50 shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_1px_2px_rgba(0,0,0,0.3)]" };
  }
  return { label: "Review Advised", color: "bg-amber-500/20 text-amber-200 border-amber-400/50 shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_1px_2px_rgba(0,0,0,0.3)]" };
}

/** Human-readable blast-radius scope label + one-line explanation. */
function blastRadiusSummary(review: CodeReviewOut): { label: string; explanation: string } {
  const blast = getBlastStats(review);
  const label = blast.scope === "Targeted" ? "Isolated" : blast.scope;
  const touched = [...new Set(review.findings.map((f) => f.file_path))];
  const area = touched.length > 0
    ? touched.length === 1 ? touched[0].split("/").slice(-2).join("/") : `${touched.length} components`
    : "no finding locations";
  return {
    label,
    explanation: `${blast.filesCount} ${blast.filesCount === 1 ? "file" : "files"}, +${blast.additions}/-${blast.deletions} across ${area}`,
  };
}

/** Read a deep-linked review id from the URL (`?review=<id>`). */
function readReviewParam(): string | null {
  if (typeof window === "undefined") return null;
  return new URLSearchParams(window.location.search).get("review");
}

/**
 * Studio-grade syntax-highlighted diff viewer
 */
function DiffViewer({ patch }: { patch: string }) {
  const lines = patch.trim().split("\n");

  return (
    <div className="overflow-x-auto rounded-md border border-zinc-800/90 bg-[#070709] text-[11px] font-mono leading-relaxed select-text font-mono">
      {lines.map((line, idx) => {
        let lineStyle = "text-zinc-300 px-3 py-0.5 border-l-2 border-transparent";

        if (line.startsWith("@@")) {
          lineStyle = "text-cyan-400 bg-cyan-950/20 px-3 py-0.5 border-l-2 border-cyan-500 font-semibold";
        } else if (line.startsWith("+")) {
          lineStyle = "bg-emerald-950/40 text-emerald-300 border-l-2 border-emerald-500 px-3 py-0.5";
        } else if (line.startsWith("-")) {
          lineStyle = "bg-rose-950/40 text-rose-300 border-l-2 border-rose-500 px-3 py-0.5";
        }

        return (
          <div key={idx} className={cn("flex items-start min-w-full font-mono whitespace-pre", lineStyle)}>
            <span className="select-none text-zinc-600 mr-2.5 w-6 text-right text-[10px] inline-block shrink-0 pt-px font-mono">
              {idx + 1}
            </span>
            <span className="flex-1 font-mono">{line}</span>
          </div>
        );
      })}
    </div>
  );
}

/**
 * Local staged-fix store. There is no backend stage/apply endpoint for code
 * reviews (backend/app/routers/reviews.py is GET-only; the staged_patches
 * flow in sessions.py belongs to chat sessions and is not wired to this UI),
 * so Stage Fix persists the patch in this browser until a server-side apply
 * endpoint exists. Deliberately distinct from Copy Patch, which only touches
 * the clipboard and never persists anything.
 */
const STAGED_FIXES_KEY = "haunter-staged-fixes";

function stagedFixKey(finding: ReviewFindingOut): string {
  const patch = finding.suggested_patch ?? "";
  return `${finding.file_path}:${finding.line_start}-${finding.line_end}:${patch.length}:${patch.slice(0, 64)}`;
}

function readStagedFixes(): Record<string, string> {
  try {
    const raw = localStorage.getItem(STAGED_FIXES_KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as Record<string, string>;
    }
    return {};
  } catch {
    return {};
  }
}

function isFixStaged(finding: ReviewFindingOut): boolean {
  try {
    return stagedFixKey(finding) in readStagedFixes();
  } catch {
    return false;
  }
}

/**
 * Finding Card component with direct GitHub file link and dual tactile actions
 */
function FindingItem({
  finding,
  repoOwner,
  repoName,
  commitSha,
}: {
  finding: ReviewFindingOut;
  repoOwner?: string | null;
  repoName?: string | null;
  commitSha?: string;
}) {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState<string | null>(null);
  const [staged, setStaged] = useState<boolean>(() => isFixStaged(finding));
  const [staging, setStaging] = useState(false);
  const [stageError, setStageError] = useState<string | null>(null);
  const meta = getCategoryMeta(finding.category);
  const Icon = meta.icon;

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

  // Toggle local staging. Never touches the clipboard: staging persists the
  // patch in this browser (see STAGED_FIXES_KEY), copying does not. Staged
  // state persists across reloads; copied state is transient.
  const handleStage = () => {
    if (!finding.suggested_patch || staging) return;
    setStageError(null);
    setStaging(true);
    try {
      const stored = readStagedFixes();
      const key = stagedFixKey(finding);
      if (key in stored) {
        delete stored[key];
        setStaged(false);
      } else {
        stored[key] = finding.suggested_patch;
        setStaged(true);
      }
      localStorage.setItem(STAGED_FIXES_KEY, JSON.stringify(stored));
    } catch {
      setStageError("Couldn't stage fix in this browser.");
    } finally {
      setStaging(false);
    }
  };

  const lineRangeSuffix = finding.line_start
    ? `#L${finding.line_start}${finding.line_end && finding.line_end !== finding.line_start ? `-L${finding.line_end}` : ""}`
    : "";

  const githubFileUrl = repoOwner && repoName && commitSha
    ? `https://github.com/${repoOwner}/${repoName}/blob/${commitSha}/${finding.file_path}${lineRangeSuffix}`
    : undefined;

  return (
    <div className="rounded-lg border border-zinc-800/80 bg-zinc-950/60 p-4 space-y-3 transition-colors hover:border-zinc-700/80">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 flex-wrap">
          <span className={cn("inline-flex items-center gap-1 rounded-[5px] border px-2 py-0.5 text-[11px] font-medium font-mono", meta.color)}>
            <Icon className="h-3 w-3" />
            {meta.label}
          </span>
          {getSeverityBadge(finding.severity)}
          {finding.symbol_name && (
            <span className="font-mono text-[10px] text-zinc-300 bg-zinc-900 px-1.5 py-0.5 rounded border border-zinc-800">
              <span className="text-zinc-500 font-sans">fn:</span> {finding.symbol_name}
            </span>
          )}
        </div>

        {/* Direct file link to GitHub line numbers */}
        {githubFileUrl ? (
          <a
            href={githubFileUrl}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1.5 font-mono text-xs text-zinc-300 hover:text-white bg-zinc-900/90 hover:bg-zinc-850 px-2 py-0.5 rounded border border-zinc-800 hover:border-zinc-700 transition-colors"
            title="Open in GitHub at exact line number"
          >
            <span>{finding.file_path}:{finding.line_start}{finding.line_end !== finding.line_start ? `-${finding.line_end}` : ""}</span>
            <ExternalLink className="h-3 w-3 text-zinc-400" />
          </a>
        ) : (
          <div className="font-mono text-xs text-zinc-400 bg-zinc-900/80 px-2 py-0.5 rounded border border-zinc-800">
            {finding.file_path}:{finding.line_start}{finding.line_end !== finding.line_start ? `-${finding.line_end}` : ""}
          </div>
        )}
      </div>

      <p className="text-xs text-zinc-200 leading-relaxed font-sans">
        {finding.critique}
      </p>

      {finding.suggested_patch && (
        <div className="mt-2 space-y-2">
          <div className="flex items-center justify-between text-[11px] text-zinc-400 font-mono">
            <span className="text-zinc-400 uppercase tracking-wider text-[10px] font-semibold">Candidate Remediation Patch</span>
            <div className="flex items-center gap-2">
              {/* Dual tactile action: Copy Patch */}
              <button
                type="button"
                onClick={handleCopy}
                className="inline-flex items-center gap-1.5 h-6 px-2.5 rounded-[5px] text-[11px] font-mono font-medium bg-gradient-to-b from-zinc-800 via-zinc-850 to-zinc-900 border-t border-t-zinc-600/60 border-x border-x-zinc-700/60 border-b border-b-zinc-950 text-zinc-200 shadow-[inset_0_1px_0_rgba(255,255,255,0.1),0_1px_3px_rgba(0,0,0,0.3)] hover:text-white active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.4)] transition-all"
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

              {/* Dual tactile action: Stage Fix (local staging, see STAGED_FIXES_KEY) */}
              <button
                type="button"
                onClick={handleStage}
                disabled={staging}
                title={
                  staged
                    ? "Staged in this browser — click to unstage"
                    : "Stage this fix in this browser (server-side apply pending)"
                }
                className={cn(
                  "inline-flex items-center gap-1.5 h-6 px-2.5 rounded-[5px] text-[11px] font-mono font-medium transition-all active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.4)]",
                  staged
                    ? "bg-gradient-to-b from-emerald-600/90 via-emerald-700 to-emerald-800 border-t border-t-emerald-300/60 border-x border-x-emerald-600/60 border-b border-b-emerald-950 text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.2),0_1px_3px_rgba(0,0,0,0.3)]"
                    : "bg-gradient-to-b from-blue-600/90 via-blue-700 to-blue-800 border-t border-t-blue-300/60 border-x border-x-blue-600/60 border-b border-b-blue-950 text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.2),0_1px_3px_rgba(0,0,0,0.3)] hover:brightness-105"
                )}
              >
                {staged ? (
                  <>
                    <Check className="h-3 w-3 text-emerald-200" />
                    <span>Staged Fix</span>
                  </>
                ) : staging ? (
                  <>
                    <FileDiff className="h-3 w-3 text-blue-200" />
                    <span>Staging…</span>
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

          <DiffViewer patch={finding.suggested_patch} />
        </div>
      )}
    </div>
  );
}

/**
 * Identity pills — PR# vs Push parity. Push rows arrive with `pr_number` NULL
 * (direct pushes, never PR-associated), so the Push pill carries the SAME
 * size/weight/position as the amber PR# pill: `Push @ <short-sha>` linking to
 * the commit. Never render a fake PR#.
 */
const IDENTITY_PILL_BASE =
  "inline-flex items-center gap-1 rounded px-2 py-0.5 text-sm font-mono font-bold border transition-colors";

function PrPill({ prNumber, prUrl, repoName }: { prNumber: number; prUrl?: string; repoName: string }) {
  return (
    <a
      href={prUrl || `https://github.com/${repoName}/pull/${prNumber}`}
      target="_blank"
      rel="noreferrer"
      className={cn(IDENTITY_PILL_BASE, "bg-amber-500/15 text-amber-300 border-amber-400/40 hover:text-amber-200 hover:bg-amber-500/25")}
    >
      <GitPullRequest className="h-3.5 w-3.5" />
      <span>PR #{prNumber}</span>
      <ArrowUpRight className="h-2.5 w-2.5 opacity-70" />
    </a>
  );
}

function PushPill({ commitSha, commitUrl, repoName }: { commitSha: string; commitUrl?: string; repoName: string }) {
  const short = commitSha ? commitSha.substring(0, 7) : "unknown";
  return (
    <a
      href={commitUrl || `https://github.com/${repoName}/commit/${commitSha}`}
      target="_blank"
      rel="noreferrer"
      title="View push commit on GitHub"
      className={cn(IDENTITY_PILL_BASE, "bg-zinc-800 text-zinc-100 border-zinc-600/60 hover:bg-zinc-700 hover:text-white")}
    >
      <GitCommit className="h-3.5 w-3.5 text-zinc-300" />
      <span>Push @ {short}</span>
      <ArrowUpRight className="h-2.5 w-2.5 opacity-70" />
    </a>
  );
}

/**
 * Honest error row for analyses that never completed (status "error" or a
 * `failure_reason` such as "All 9 free-tier models exhausted"). Renders NO
 * verdict telemetry — ERROR pill, em-dash confidence, neutral gauge, the raw
 * error text, and a Retry that refetches the list. Showing 10/10 + SAFE here
 * (the risk_score column defaults to 0) would be lying UI.
 */
function FailedReviewCard({
  review,
  onOpen,
  onRetry,
}: {
  review: CodeReviewOut;
  onOpen: (id: string) => void;
  onRetry: () => void;
}) {
  const repoName = review.repo_owner && review.repo_name ? `${review.repo_owner}/${review.repo_name}` : "Repository";
  const repoUrl = review.repo_owner && review.repo_name ? `https://github.com/${review.repo_owner}/${review.repo_name}` : undefined;
  const prUrl = review.pr_url || (review.repo_owner && review.repo_name && review.pr_number ? `https://github.com/${review.repo_owner}/${review.repo_name}/pull/${review.pr_number}` : undefined);
  const commitUrl = review.repo_owner && review.repo_name ? `https://github.com/${review.repo_owner}/${review.repo_name}/commit/${review.commit_sha}` : undefined;
  const commitShort = review.commit_sha ? review.commit_sha.substring(0, 7) : "unknown";
  const errorText = review.failure_reason ?? review.summary;
  const [expanded, setExpanded] = useState(false);
  const errorExcerpt = errorText.length > 140 ? `${errorText.slice(0, 140).trimEnd()}…` : errorText;
  const identityLabel = `${repoName}${review.pr_number ? ` PR #${review.pr_number}` : ` Push @ ${commitShort}`}`;

  // Collapsed default: dimmed single-line row so failed analyses don't
  // dominate the stream. role=alert lives on the expanded panel only, and
  // Retry stays outside it — same pattern as the detail error banner.
  if (!expanded) {
    return (
      <div className="rounded-xl border border-rose-500/20 bg-[#0d0d10] px-4 py-3 opacity-90 transition-opacity hover:opacity-100 focus-within:opacity-100">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          {repoUrl ? (
            <a
              href={repoUrl}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1.5 font-mono text-sm font-bold text-zinc-300 hover:text-white transition-colors"
            >
              <FolderGit2 className="h-3.5 w-3.5 text-zinc-500" />
              <span>{repoName}</span>
            </a>
          ) : (
            <span className="inline-flex items-center gap-1.5 font-mono text-sm font-bold text-zinc-300">
              <FolderGit2 className="h-3.5 w-3.5 text-zinc-500" />
              <span>{repoName}</span>
            </span>
          )}
          {review.pr_number ? (
            <PrPill prNumber={review.pr_number} prUrl={prUrl} repoName={repoName} />
          ) : (
            <PushPill commitSha={review.commit_sha} commitUrl={commitUrl} repoName={repoName} />
          )}
          <span className="inline-flex items-center rounded-full border border-rose-500/25 bg-rose-500/10 px-2 py-0.5 text-[10px] font-mono font-bold uppercase tracking-wider text-rose-300">
            Error
          </span>
          <span className="min-w-0 flex-1 basis-40 truncate text-xs font-mono text-zinc-400" title={errorText}>
            {errorExcerpt}
          </span>
          <span className="text-xs text-zinc-500 font-mono">
            {formatRelativeTime(review.created_at)}
          </span>
          <span className="ml-auto inline-flex items-center gap-1.5">
            <Button
              variant="outline"
              size="sm"
              onClick={onRetry}
              aria-label={`Retry loading reviews (failed: ${identityLabel})`}
              className="h-7 px-2.5 text-xs gap-1.5 border-rose-500/30 bg-transparent hover:bg-rose-900/30 text-rose-200/90"
            >
              <RefreshCw className="h-3.5 w-3.5" />
              <span>Retry</span>
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setExpanded(true)}
              aria-expanded={false}
              aria-label={`Expand error details for ${identityLabel}`}
              className="h-7 w-7 p-0 border-zinc-800 bg-transparent hover:bg-zinc-800 text-zinc-400"
            >
              <ChevronDown className="h-3.5 w-3.5" />
            </Button>
          </span>
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-xl border border-rose-500/30 bg-gradient-to-b from-[#141114] to-[#0c0c0e] p-5 shadow-[inset_0_1px_0_rgba(255,255,255,0.04),0_8px_20px_rgba(0,0,0,0.4)]">
      <div className="flex flex-col md:flex-row md:items-start justify-between gap-5">
        {/* Left: neutral gauge — no score implied */}
        <div className="flex items-center md:flex-col justify-start md:justify-center gap-4 md:min-w-[80px]">
          <RiskGauge score={review.risk_score} size={64} neutral />
        </div>

        {/* Center: identity + raw error */}
        <div className="flex-1 min-w-0 space-y-4">
          <div className="flex flex-wrap items-center gap-2">
            {repoUrl ? (
              <a
                href={repoUrl}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1.5 font-mono text-sm font-bold text-zinc-100 hover:text-white transition-colors"
              >
                <FolderGit2 className="h-3.5 w-3.5 text-zinc-400" />
                <span>{repoName}</span>
              </a>
            ) : (
              <span className="inline-flex items-center gap-1.5 font-mono text-sm font-bold text-zinc-100">
                <FolderGit2 className="h-3.5 w-3.5 text-zinc-400" />
                <span>{repoName}</span>
              </span>
            )}
            {review.pr_number ? (
              <PrPill prNumber={review.pr_number} prUrl={prUrl} repoName={repoName} />
            ) : (
              <PushPill commitSha={review.commit_sha} commitUrl={commitUrl} repoName={repoName} />
            )}
            <span className="inline-flex items-center rounded-full border border-rose-500/40 bg-rose-950/60 px-2 py-0.5 text-[10px] font-mono font-bold uppercase tracking-wider text-rose-300">
              Error
            </span>
            <span className="text-xs text-zinc-400 font-mono">
              {formatRelativeTime(review.created_at)}
            </span>
          </div>

          {review.pr_title && (
            <h3 className="text-base font-bold tracking-tight text-zinc-50 leading-snug">
              {review.pr_title}
            </h3>
          )}

          <div role="alert" className="flex items-start gap-2 rounded-lg border border-rose-500/30 bg-rose-950/20 px-3 py-2 text-xs text-rose-200 leading-relaxed">
            <AlertTriangle className="h-3.5 w-3.5 mt-0.5 shrink-0 text-rose-400" />
            <span>{errorText}</span>
          </div>

          {/* No telemetry: every verdict cell is an em-dash */}
          <dl className="grid grid-cols-2 lg:grid-cols-4 gap-px overflow-hidden rounded-lg border border-zinc-800/80 bg-zinc-800/80 text-xs font-mono">
            <div className="bg-zinc-950/90 px-3 py-2 space-y-0.5">
              <dt className="text-[10px] uppercase tracking-wider text-zinc-500 font-semibold">Status</dt>
              <dd className="text-rose-300 font-semibold">Error</dd>
            </div>
            <div className="bg-zinc-950/90 px-3 py-2 space-y-0.5">
              <dt className="text-[10px] uppercase tracking-wider text-zinc-500 font-semibold">PR Confidence</dt>
              <dd className="text-lg font-bold text-zinc-500" title="No score — analysis did not complete">—</dd>
            </div>
            <div className="bg-zinc-950/90 px-3 py-2 space-y-0.5">
              <dt className="text-[10px] uppercase tracking-wider text-zinc-500 font-semibold">Blockers</dt>
              <dd className="text-zinc-500">—</dd>
            </div>
            <div className="bg-zinc-950/90 px-3 py-2 space-y-0.5">
              <dt className="text-[10px] uppercase tracking-wider text-zinc-500 font-semibold">Blast Radius</dt>
              <dd className="text-zinc-500">—</dd>
            </div>
          </dl>

          {/* Compact factual meta: author / branch / SHA only */}
          <div className="flex flex-wrap items-center gap-2 text-[11px] font-mono text-zinc-400">
            {review.pr_author && (
              <span className="inline-flex items-center gap-1">
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
                <span>@{review.pr_author}</span>
              </span>
            )}
            {(review.head_branch || review.base_branch) && (
              <span className="inline-flex items-center gap-1">
                <GitBranch className="h-3 w-3 text-zinc-500" />
                <span>{review.base_branch || "main"}</span>
                <span className="text-zinc-600">←</span>
                <span className="text-zinc-300">{review.head_branch || "head"}</span>
              </span>
            )}
            <a
              href={commitUrl || `https://github.com/${repoName}/commit/${review.commit_sha}`}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 hover:text-zinc-200 transition-colors"
              title="View commit on GitHub"
            >
              <GitCommit className="h-3 w-3 text-zinc-500" />
              <span>{commitShort}</span>
            </a>
          </div>
        </div>

        {/* Right: retry + audit */}
        <div className="flex flex-row md:flex-col items-end justify-between gap-3 min-w-[120px]">
          <div className="flex flex-col items-end gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={onRetry}
              aria-label={`Retry loading reviews (failed: ${identityLabel})`}
              className="h-7 px-2.5 text-xs gap-1.5 border-rose-500/40 bg-rose-950/30 hover:bg-rose-900/40 text-rose-200"
            >
              <RefreshCw className="h-3.5 w-3.5" />
              <span>Retry</span>
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => onOpen(review.id)}
              aria-label={`Full audit for ${repoName}${review.pr_number ? ` PR #${review.pr_number}` : ""}${review.pr_title ? ` — ${review.pr_title}` : ""} (${review.id})`}
              className="h-7 px-2.5 text-xs gap-1.5 border-amber-500/40 bg-amber-950/30 hover:bg-amber-900/40 text-amber-200"
            >
              <span>Full audit</span>
              <ArrowUpRight className="h-3.5 w-3.5" />
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setExpanded(false)}
              aria-expanded={true}
              aria-label={`Collapse error details for ${identityLabel}`}
              className="h-7 px-2.5 text-xs gap-1.5 border-zinc-700 bg-zinc-900/80 hover:bg-zinc-800 text-zinc-300"
            >
              <span>Collapse</span>
              <ChevronUp className="h-3.5 w-3.5" />
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}

/**
 * Honest neutral row for analyses that have not finished (status "pending" /
 * "in_progress" — or anything but completed/error/suppressed, fail-closed).
 * Scored "suppressed" is terminal WITH a saved audit so it renders the full
 * completed branches, never this card; unscored suppressed rows (risk 0 +
 * no findings, no saved audit) render this card with a "Suppressed" pill.
 * Renders NO
 * verdict telemetry — Queued/Analyzing pill, em-dash confidence, neutral
 * gauge, the queued summary verbatim. A pending row carries the column
 * defaults (risk_score 0, findings []) which would otherwise read as
 * Great-to-Merge / SAFE with 10/10 confidence. No Retry: nothing failed —
 * Refresh re-polls the GET-only list.
 */
function PendingReviewCard({
  review,
  onOpen,
}: {
  review: CodeReviewOut;
  onOpen: (id: string) => void;
}) {
  const repoName = review.repo_owner && review.repo_name ? `${review.repo_owner}/${review.repo_name}` : "Repository";
  const repoUrl = review.repo_owner && review.repo_name ? `https://github.com/${review.repo_owner}/${review.repo_name}` : undefined;
  const prUrl = review.pr_url || (review.repo_owner && review.repo_name && review.pr_number ? `https://github.com/${review.repo_owner}/${review.repo_name}/pull/${review.pr_number}` : undefined);
  const commitUrl = review.repo_owner && review.repo_name ? `https://github.com/${review.repo_owner}/${review.repo_name}/commit/${review.commit_sha}` : undefined;
  const label = isSuppressedUnscored(review) ? "Suppressed" : pendingLabelFor(review);

  return (
    <div className="rounded-xl border border-zinc-800/90 bg-gradient-to-b from-[#111115] to-[#0c0c0e] p-5 shadow-[inset_0_1px_0_rgba(255,255,255,0.04),0_8px_20px_rgba(0,0,0,0.4)]">
      <div className="flex flex-col md:flex-row md:items-start justify-between gap-5">
        {/* Left: neutral gauge — no score implied */}
        <div className="flex items-center md:flex-col justify-start md:justify-center gap-4 md:min-w-[80px]">
          <RiskGauge score={review.risk_score} size={64} neutral />
        </div>

        {/* Center: identity + queued summary + em-dash telemetry */}
        <div className="flex-1 min-w-0 space-y-4">
          <div className="flex flex-wrap items-center gap-2">
            {repoUrl ? (
              <a
                href={repoUrl}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1.5 font-mono text-sm font-bold text-zinc-100 hover:text-white transition-colors"
              >
                <FolderGit2 className="h-3.5 w-3.5 text-zinc-400" />
                <span>{repoName}</span>
              </a>
            ) : (
              <span className="inline-flex items-center gap-1.5 font-mono text-sm font-bold text-zinc-100">
                <FolderGit2 className="h-3.5 w-3.5 text-zinc-400" />
                <span>{repoName}</span>
              </span>
            )}
            {review.pr_number ? (
              <PrPill prNumber={review.pr_number} prUrl={prUrl} repoName={repoName} />
            ) : (
              <PushPill commitSha={review.commit_sha} commitUrl={commitUrl} repoName={repoName} />
            )}
            <span className="inline-flex items-center rounded-full border border-zinc-700/60 bg-zinc-900 px-2 py-0.5 text-[10px] font-mono font-bold uppercase tracking-wider text-zinc-400">
              {label}
            </span>
            <span className="text-xs text-zinc-400 font-mono">
              {formatRelativeTime(review.created_at)}
            </span>
          </div>

          {review.pr_title && (
            <h3 className="text-base font-bold tracking-tight text-zinc-50 leading-snug">
              {review.pr_title}
            </h3>
          )}

          <p className="text-xs text-zinc-400 leading-relaxed font-sans">
            {review.summary}
          </p>

          {/* No telemetry: every verdict cell is an em-dash */}
          <dl className="grid grid-cols-2 lg:grid-cols-4 gap-px overflow-hidden rounded-lg border border-zinc-800/80 bg-zinc-800/80 text-xs font-mono">
            <div className="bg-zinc-950/90 px-3 py-2 space-y-0.5">
              <dt className="text-[10px] uppercase tracking-wider text-zinc-500 font-semibold">Status</dt>
              <dd><span className="inline-flex items-center rounded-full border border-zinc-700/60 bg-zinc-900 px-2 py-0.5 text-[10px] font-semibold text-zinc-400">{label}</span></dd>
            </div>
            <div className="bg-zinc-950/90 px-3 py-2 space-y-0.5">
              <dt className="text-[10px] uppercase tracking-wider text-zinc-500 font-semibold">PR Confidence</dt>
              <dd className="text-lg font-bold text-zinc-500" title="No score — analysis has not completed">—</dd>
            </div>
            <div className="bg-zinc-950/90 px-3 py-2 space-y-0.5">
              <dt className="text-[10px] uppercase tracking-wider text-zinc-500 font-semibold">Blockers</dt>
              <dd className="text-zinc-500">—</dd>
            </div>
            <div className="bg-zinc-950/90 px-3 py-2 space-y-0.5">
              <dt className="text-[10px] uppercase tracking-wider text-zinc-500 font-semibold">Blast Radius</dt>
              <dd className="text-zinc-500">—</dd>
            </div>
          </dl>
        </div>

        {/* Right: audit only — no retry, nothing failed */}
        <div className="flex flex-row md:flex-col items-end justify-between gap-3 min-w-[120px]">
          <div className="flex flex-col items-end gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => onOpen(review.id)}
              aria-label={`Full audit for ${repoName}${review.pr_number ? ` PR #${review.pr_number}` : ""}${review.pr_title ? ` — ${review.pr_title}` : ""} (${review.id})`}
              className="h-7 px-2.5 text-xs gap-1.5 border-amber-500/40 bg-amber-950/30 hover:bg-amber-900/40 text-amber-200"
            >
              <span>Full audit</span>
              <ArrowUpRight className="h-3.5 w-3.5" />
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}

/**
 * Main Review Card (overview row). Compact by design: identity, confidence,
 * blast-radius strip, category pills. Full audit opens via `onOpen`.
 */
function ReviewCard({ review, onOpen, onRetry }: { review: CodeReviewOut; onOpen: (id: string) => void; onRetry: () => void }) {
  const [expanded, setExpanded] = useState(false);
  const [copiedSha, setCopiedSha] = useState(false);
  const [copyShaError, setCopyShaError] = useState<string | null>(null);
  const repoName = review.repo_owner && review.repo_name ? `${review.repo_owner}/${review.repo_name}` : "Repository";
  const repoUrl = review.repo_owner && review.repo_name ? `https://github.com/${review.repo_owner}/${review.repo_name}` : undefined;
  const prUrl = review.pr_url || (review.repo_owner && review.repo_name && review.pr_number ? `https://github.com/${review.repo_owner}/${review.repo_name}/pull/${review.pr_number}` : undefined);
  const commitUrl = review.repo_owner && review.repo_name ? `https://github.com/${review.repo_owner}/${review.repo_name}/commit/${review.commit_sha}` : undefined;

  // Category counts
  const categoryCounts = useMemo(() => {
    const counts = { security: 0, logic: 0, performance: 0, api_compatibility: 0 };
    for (const f of review.findings) {
      const cat = f.category.toLowerCase() as keyof typeof counts;
      if (counts[cat] !== undefined) {
        counts[cat]++;
      }
    }
    return counts;
  }, [review.findings]);

  const commitShort = review.commit_sha ? review.commit_sha.substring(0, 7) : "unknown";

  const handleCopySha = async (e: React.MouseEvent) => {
    e.stopPropagation();
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

  const failed = isReviewFailed(review);
  const suppressed = isSuppressed(review);
  if (failed) {
    return <FailedReviewCard review={review} onOpen={onOpen} onRetry={onRetry} />;
  }

  // Non-terminal rows — plus unscored suppressed rows with no saved audit —
  // never reach verdict telemetry: neutral pending card.
  if (isReviewPending(review) || isSuppressedUnscored(review)) {
    return <PendingReviewCard review={review} onOpen={onOpen} />;
  }

  // Blast Radius Telemetry (shared helper — overview strip + detail report agree)
  const blast = getBlastStats(review);
  const filesCount = blast.filesCount;
  const additions = blast.additions;
  const deletions = blast.deletions;
  const confidence = confidenceFromRisk(review.risk_score);
  const blockers = blockerCounts(review);
  const verdict = verdictFor(review);
  const radius = blastRadiusSummary(review);

  return (
    <div className="rounded-xl border border-zinc-800/90 bg-gradient-to-b from-[#111115] to-[#0c0c0e] p-5 shadow-[inset_0_1px_0_rgba(255,255,255,0.04),0_8px_20px_rgba(0,0,0,0.4)] transition-all hover:border-zinc-700/90">
      <div className="flex flex-col md:flex-row md:items-start justify-between gap-5">
        {/* Left: Gauge + risk score */}
        <div className="flex items-center md:flex-col justify-start md:justify-center gap-1.5 md:min-w-[80px]">
          <RiskGauge score={review.risk_score} size={64} />
          <span className="md:text-center text-[10px] font-mono text-zinc-500">
            risk <span className="text-zinc-300 font-semibold">{review.risk_score}/100</span>
          </span>
        </div>

        {/* Center: report identity, summary, audit strip */}
        <div className="flex-1 min-w-0 space-y-4">
          <div className="flex flex-wrap items-center gap-2">
            {/* Repo link — prominent report identity */}
            {repoUrl ? (
              <a
                href={repoUrl}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1.5 font-mono text-sm font-bold text-zinc-100 hover:text-white transition-colors"
              >
                <FolderGit2 className="h-3.5 w-3.5 text-zinc-400" />
                <span>{repoName}</span>
              </a>
            ) : (
              <span className="inline-flex items-center gap-1.5 font-mono text-sm font-bold text-zinc-100">
                <FolderGit2 className="h-3.5 w-3.5 text-zinc-400" />
                <span>{repoName}</span>
              </span>
            )}

            {/* Identity pill — amber PR# when PR-associated, equal-weight
                Push @ sha for direct pushes (pr_number NULL), verdict pill
                alongside in both cases */}
            <div className="inline-flex items-center gap-1.5">
              {review.pr_number ? (
                <PrPill prNumber={review.pr_number} prUrl={prUrl} repoName={repoName} />
              ) : (
                <PushPill commitSha={review.commit_sha} commitUrl={commitUrl} repoName={repoName} />
              )}
              <span className={cn("inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-mono font-semibold", verdict.color)}>
                {verdict.label}
              </span>
              {suppressed && (
                <span
                  className="inline-flex items-center rounded-full border border-zinc-700/60 bg-zinc-900 px-2 py-0.5 text-[10px] font-mono font-semibold text-zinc-400"
                  title="Analysis finished — publishing to GitHub was skipped by repo settings"
                >
                  Publication skipped
                </span>
              )}
              {!review.pr_number && (
                <button
                  type="button"
                  onClick={handleCopySha}
                  className="rounded-sm text-zinc-500 hover:text-zinc-200 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400/70"
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
            </div>

            {/* Author Badge */}
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
                <span className="font-mono text-xs text-zinc-300">@{review.pr_author}</span>
              </div>
            )}

            {/* Branch Flow Pill */}
            {(review.head_branch || review.base_branch) && (
              <div className="inline-flex items-center gap-1.5 rounded bg-zinc-900/90 border border-zinc-800 px-2 py-0.5 text-[11px] font-mono">
                <GitBranch className="h-3 w-3 text-zinc-500" />
                <span className="text-zinc-400">{review.base_branch || "main"}</span>
                <span className="text-zinc-500">←</span>
                <span className="text-amber-400 font-medium">{review.head_branch || "head"}</span>
              </div>
            )}

            {/* Commit SHA with 1-click copy — PR rows only. Push rows carry
                the sha in the Push pill with its own copy affordance above. */}
            {review.pr_number ? (
            <div className="inline-flex items-center rounded bg-zinc-900/80 border border-zinc-800 px-1.5 py-0.5 text-xs font-mono text-zinc-400">
              <a
                href={commitUrl || `https://github.com/${repoName}/commit/${review.commit_sha}`}
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
                className="rounded-sm pl-1.5 hover:text-zinc-200 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400/70"
                title="Copy commit SHA"
                aria-label="Copy commit SHA"
              >
                {copiedSha ? (
                  <Check className="h-3 w-3 text-emerald-400" />
                ) : (
                  <Copy className="h-3 w-3 text-zinc-500 hover:text-zinc-300" />
                )}
              </button>
            </div>
            ) : null}

            <span className="text-zinc-500">•</span>
            <span className="text-xs text-zinc-400 font-mono">
              {formatRelativeTime(review.created_at)}
            </span>
          </div>

          {copyShaError && (
            <p role="alert" className="text-[11px] font-mono text-rose-400">
              {copyShaError}
            </p>
          )}

          {/* Hero PR Title */}
          {review.pr_title && (
            <h3 className="text-base font-bold tracking-tight text-zinc-50 leading-snug">
              {review.pr_title}
            </h3>
          )}

          <p className="text-xs text-zinc-300 leading-relaxed font-sans">
            {review.summary}
          </p>

          {/* Audit strip: mirrors the shipped audit comment's status table */}
          <dl className="grid grid-cols-2 lg:grid-cols-4 gap-px overflow-hidden rounded-lg border border-zinc-800/80 bg-zinc-800/80 text-xs font-mono shadow-[inset_0_1px_0_rgba(255,255,255,0.02)]">
            <div className="bg-zinc-950/90 px-3 py-2 space-y-0.5">
              <dt className="text-[10px] uppercase tracking-wider text-zinc-500 font-semibold">Status</dt>
              <dd><span className={cn("inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-semibold", verdict.color)}>{verdict.label}</span></dd>
            </div>
            <div className="bg-zinc-950/90 px-3 py-2 space-y-0.5">
              <dt className="text-[10px] uppercase tracking-wider text-zinc-500 font-semibold">PR Confidence</dt>
              <dd className="text-lg font-bold text-zinc-100" title={`PR Confidence ${confidence}/10 derived from risk score ${review.risk_score}`}>{confidence}/10</dd>
            </div>
            <div className="bg-zinc-950/90 px-3 py-2 space-y-0.5">
              <dt className="text-[10px] uppercase tracking-wider text-zinc-500 font-semibold">Blockers</dt>
              <dd className="text-zinc-200">
                Must-Fix <span className={cn("font-bold", blockers.mustFix > 0 ? "text-rose-400" : "text-emerald-400")}>{blockers.mustFix}</span>
                <span className="text-zinc-600"> · </span>
                Should-Fix <span className={cn("font-bold", blockers.shouldFix > 0 ? "text-amber-400" : "text-emerald-400")}>{blockers.shouldFix}</span>
              </dd>
            </div>
            <div className="bg-zinc-950/90 px-3 py-2 space-y-0.5">
              <dt className="text-[10px] uppercase tracking-wider text-zinc-500 font-semibold">Blast Radius</dt>
              <dd className="font-bold text-zinc-100">{radius.label}</dd>
              <dd className="text-[10px] text-zinc-500 font-sans">{radius.explanation}</dd>
            </div>
          </dl>

          {/* Compact diff telemetry + findings count */}
          <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1 text-xs font-mono text-zinc-400">
            <span className="text-zinc-100 font-semibold">{filesCount} {filesCount === 1 ? "file" : "files"}</span>
            <span className="text-emerald-400 font-semibold">+{additions}</span>
            <span className="text-rose-400 font-semibold">-{deletions}</span>
            <span className="text-zinc-700">|</span>
            <span>{review.findings.length} {review.findings.length === 1 ? "finding" : "findings"}</span>
            <span className="text-zinc-700">|</span>
            <span>{review.input_tokens + review.output_tokens} tok</span>
          </div>

          {/* Finding Category Badges */}
          <div className="flex flex-wrap items-center gap-2 pt-1">
            {categoryCounts.security > 0 && (
              <span className="inline-flex items-center gap-1 rounded-full border border-rose-500/30 bg-rose-950/40 px-2 py-0.5 text-[10px] font-mono text-rose-300">
                <Lock className="h-2.5 w-2.5 text-rose-400" />
                {categoryCounts.security} Security
              </span>
            )}
            {categoryCounts.logic > 0 && (
              <span className="inline-flex items-center gap-1 rounded-full border border-amber-500/30 bg-amber-950/40 px-2 py-0.5 text-[10px] font-mono text-amber-300">
                <Bug className="h-2.5 w-2.5 text-amber-400" />
                {categoryCounts.logic} Logic
              </span>
            )}
            {categoryCounts.performance > 0 && (
              <span className="inline-flex items-center gap-1 rounded-full border border-cyan-500/30 bg-cyan-950/40 px-2 py-0.5 text-[10px] font-mono text-cyan-300">
                <Zap className="h-2.5 w-2.5 text-cyan-400" />
                {categoryCounts.performance} Perf
              </span>
            )}
            {categoryCounts.api_compatibility > 0 && (
              <span className="inline-flex items-center gap-1 rounded-full border border-purple-500/30 bg-purple-950/40 px-2 py-0.5 text-[10px] font-mono text-purple-300">
                <Boxes className="h-2.5 w-2.5 text-purple-400" />
                {categoryCounts.api_compatibility} API
              </span>
            )}
            {review.findings.length === 0 && (
              <span className="inline-flex items-center gap-1 rounded-full border border-emerald-500/30 bg-emerald-950/30 px-2 py-0.5 text-[10px] font-mono text-emerald-300">
                <Check className="h-2.5 w-2.5 text-emerald-400" />
                No regressions detected
              </span>
            )}
          </div>
        </div>

        {/* Right: Actions & Telemetry */}
        <div className="flex flex-row md:flex-col items-end justify-between gap-3 min-w-[120px]">
          <div className="text-right text-[11px] font-mono text-zinc-400 space-y-0.5">
            <div>{review.findings.length} {review.findings.length === 1 ? "finding" : "findings"}</div>
            <div>{review.input_tokens + review.output_tokens} tok</div>
          </div>

          <div className="flex flex-col items-end gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => onOpen(review.id)}
              aria-label={`Full audit for ${repoName}${review.pr_number ? ` PR #${review.pr_number}` : ""}${review.pr_title ? ` — ${review.pr_title}` : ""} (${review.id})`}
              className="h-7 px-2.5 text-xs gap-1.5 border-amber-500/40 bg-amber-950/30 hover:bg-amber-900/40 text-amber-200"
            >
              <span>Full audit</span>
              <ArrowUpRight className="h-3.5 w-3.5" />
            </Button>
            {review.findings.length > 0 && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => setExpanded(!expanded)}
                className="h-7 px-2.5 text-xs gap-1.5 border-zinc-700 bg-zinc-900/80 hover:bg-zinc-800 text-zinc-200"
              >
                <span>{expanded ? "Collapse" : `Review (${review.findings.length})`}</span>
                {expanded ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
              </Button>
            )}
          </div>
        </div>
      </div>

      {/* Accordion Findings */}
      {expanded && review.findings.length > 0 && (
        <div className="mt-5 pt-4 border-t border-zinc-800/80 space-y-3">
          <div className="flex items-center justify-between text-xs font-semibold text-zinc-400 uppercase tracking-wider font-mono">
            <span>Actionable Findings ({review.findings.length})</span>
            <span className="text-[10px] text-zinc-500 font-normal">Pre-verified candidate patches</span>
          </div>
          <div className="space-y-3">
            {review.findings.map((f, i) => (
              <FindingItem
                key={i}
                finding={f}
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

/**
 * Full professional audit report (detail view). Mirrors the shipped audit
 * comment format: header identity, PR summary, status/confidence/blockers/
 * blast-radius grid, structural analysis, findings, metadata.
 */
function ReviewDetail({ review, onRetry }: { review: CodeReviewOut; onRetry?: () => void }) {
  const [copiedSha, setCopiedSha] = useState(false);
  const [copyShaError, setCopyShaError] = useState<string | null>(null);
  const failed = isReviewFailed(review);
  const pending = !failed && (isReviewPending(review) || isSuppressedUnscored(review));
  const suppressed = !failed && !pending && isSuppressed(review);
  const pendingLabel = isSuppressedUnscored(review) ? "Suppressed" : pendingLabelFor(review);
  const errorText = review.failure_reason ?? review.summary;
  const blast = getBlastStats(review);
  const confidence = confidenceFromRisk(review.risk_score);
  const blockers = blockerCounts(review);
  const verdict = verdictFor(review);
  const radius = blastRadiusSummary(review);
  const touchedFiles = useMemo(
    () => [...new Set(review.findings.map((f) => f.file_path))],
    [review.findings]
  );

  const repoName = review.repo_owner && review.repo_name ? `${review.repo_owner}/${review.repo_name}` : "Repository";
  const repoUrl = review.repo_owner && review.repo_name ? `https://github.com/${review.repo_owner}/${review.repo_name}` : undefined;
  const prUrl = review.pr_url || (review.repo_owner && review.repo_name && review.pr_number ? `https://github.com/${review.repo_owner}/${review.repo_name}/pull/${review.pr_number}` : undefined);
  const commitUrl = review.repo_owner && review.repo_name ? `https://github.com/${review.repo_owner}/${review.repo_name}/commit/${review.commit_sha}` : undefined;
  const commitShort = review.commit_sha ? review.commit_sha.substring(0, 7) : "unknown";

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

  const shownFiles = touchedFiles.slice(0, 8);
  const hiddenFiles = touchedFiles.length - shownFiles.length;

  return (
    <div className="space-y-6">
      {/* Header: identity */}
      <div className="rounded-xl border border-zinc-800/90 bg-gradient-to-b from-[#111115] to-[#0c0c0e] p-5 shadow-[inset_0_1px_0_rgba(255,255,255,0.04),0_8px_20px_rgba(0,0,0,0.4)]">
        <div className="text-[11px] font-mono font-semibold uppercase tracking-[0.16em] text-zinc-400">
          Haunter Autonomous Audit Report
        </div>
        <div className="mt-2.5 flex flex-col lg:flex-row lg:items-start justify-between gap-5">
          <div className="flex-1 min-w-0 space-y-2.5">
            <div className="flex flex-wrap items-center gap-2">
              {repoUrl ? (
                <a href={repoUrl} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1.5 font-mono text-sm font-semibold text-zinc-100 hover:text-white transition-colors">
                  <FolderGit2 className="h-4 w-4 text-zinc-400" />
                  <span>{repoName}</span>
                </a>
              ) : (
                <span className="inline-flex items-center gap-1.5 font-mono text-sm font-semibold text-zinc-100">
                  <FolderGit2 className="h-4 w-4 text-zinc-400" />
                  <span>{repoName}</span>
                </span>
              )}
              {review.pr_number ? (
                <PrPill prNumber={review.pr_number} prUrl={prUrl} repoName={repoName} />
              ) : (
                <PushPill commitSha={review.commit_sha} commitUrl={commitUrl} repoName={repoName} />
              )}
              {failed && (
                <span className="inline-flex items-center rounded-full border border-rose-500/40 bg-rose-950/60 px-2 py-0.5 text-[10px] font-mono font-bold uppercase tracking-wider text-rose-300">
                  Error
                </span>
              )}
              {pending && (
                <span className="inline-flex items-center rounded-full border border-zinc-700/60 bg-zinc-900 px-2 py-0.5 text-[10px] font-mono font-bold uppercase tracking-wider text-zinc-400">
                  {pendingLabel}
                </span>
              )}
              {suppressed && (
                <span
                  className="inline-flex items-center rounded-full border border-zinc-700/60 bg-zinc-900 px-2 py-0.5 text-[10px] font-mono font-bold uppercase tracking-wider text-zinc-400"
                  title="Analysis finished — publishing to GitHub was skipped by repo settings"
                >
                  Publication skipped
                </span>
              )}
              {review.pr_author && (
                <div className="inline-flex items-center gap-1.5 rounded-full bg-zinc-900 border border-zinc-800 px-2 py-0.5 text-xs text-zinc-300">
                  {review.pr_author_avatar ? (
                    <img src={review.pr_author_avatar} alt={review.pr_author} className="h-3.5 w-3.5 rounded-full object-cover" />
                  ) : (
                    <span className="flex h-3.5 w-3.5 items-center justify-center rounded-full bg-zinc-800 text-[9px] font-bold text-zinc-300">
                      {review.pr_author[0]?.toUpperCase()}
                    </span>
                  )}
                  <span className="font-mono text-xs text-zinc-300">@{review.pr_author}</span>
                </div>
              )}
            </div>

            {review.pr_title && (
              <h2 className="text-xl font-bold tracking-tight text-zinc-50 leading-snug">{review.pr_title}</h2>
            )}

            <div className="flex flex-wrap items-center gap-2 text-[11px] font-mono">
              {(review.head_branch || review.base_branch) && (
                <div className="inline-flex items-center gap-1.5 rounded bg-zinc-900/90 border border-zinc-800 px-2 py-0.5">
                  <GitBranch className="h-3 w-3 text-zinc-500" />
                  <span className="text-zinc-400">{review.base_branch || "main"}</span>
                  <span className="text-zinc-500">←</span>
                  <span className="text-amber-400 font-medium">{review.head_branch || "head"}</span>
                </div>
              )}
              {/* Commit SHA chip — PR rows only, mirroring the overview card.
                  Push rows carry the sha in the Push pill with their own copy
                  affordance alongside (same pattern as the overview row). */}
              {review.pr_number ? (
              <div className="inline-flex items-center rounded bg-zinc-900/80 border border-zinc-800 px-1.5 py-0.5 text-xs text-zinc-400">
                <a href={commitUrl || `https://github.com/${repoName}/commit/${review.commit_sha}`} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 hover:text-zinc-200 transition-colors pr-1.5 border-r border-zinc-800" title="View commit on GitHub">
                  <GitCommit className="h-3 w-3 text-zinc-500" />
                  <span>{commitShort}</span>
                </a>
                <button type="button" onClick={handleCopySha} className="rounded-sm pl-1.5 hover:text-zinc-200 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400/70" title="Copy commit SHA" aria-label="Copy commit SHA">
                  {copiedSha ? <Check className="h-3 w-3 text-emerald-400" /> : <Copy className="h-3 w-3 text-zinc-500 hover:text-zinc-300" />}
                </button>
              </div>
              ) : (
                <button type="button" onClick={handleCopySha} className="rounded-sm text-zinc-500 hover:text-zinc-200 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400/70" title="Copy commit SHA" aria-label="Copy commit SHA">
                  {copiedSha ? <Check className="h-3.5 w-3.5 text-emerald-400" /> : <Copy className="h-3.5 w-3.5" />}
                </button>
              )}
              {copyShaError && (
                <p role="alert" className="text-[11px] font-mono text-rose-400">
                  {copyShaError}
                </p>
              )}
              <span className="text-zinc-500">{formatRelativeTime(review.created_at)}</span>
            </div>
          </div>

          <div className="flex items-start justify-start lg:justify-center">
            <RiskGauge score={review.risk_score} size={84} neutral={failed || pending} />
          </div>
        </div>
      </div>

      {failed ? (
        <>
          {/* Honest failure panel: raw error, no verdict telemetry.
              The alert covers the error text only — the Retry button stays
              outside role=alert so assistive tech doesn't trap interaction. */}
          <div className="flex items-start justify-between gap-3 rounded-xl border border-rose-500/40 bg-rose-950/30 px-4 py-3 text-xs leading-relaxed text-rose-200">
            <span role="alert" className="flex items-start gap-2">
              <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0 text-rose-400" />
              <span>{errorText}</span>
            </span>
            {onRetry && (
              <Button
                variant="outline"
                size="sm"
                onClick={onRetry}
                className="h-7 shrink-0 border-rose-500/40 text-xs text-rose-200 hover:bg-rose-900/40"
              >
                Retry
              </Button>
            )}
          </div>

          <section className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
            <div className="rounded-xl border border-rose-500/30 bg-[#0f0f12] p-4 space-y-1">
              <div className="text-[11px] font-mono font-semibold uppercase tracking-wider text-zinc-500">Status</div>
              <span className="inline-flex items-center rounded-full border border-rose-500/40 bg-rose-950/60 px-2.5 py-1 text-xs font-mono font-semibold text-rose-300">
                Error
              </span>
            </div>
            <div className="rounded-xl border border-zinc-800/80 bg-[#0f0f12] p-4 space-y-1">
              <div className="text-[11px] font-mono font-semibold uppercase tracking-wider text-zinc-500">PR Confidence</div>
              <div className="font-mono text-xl font-bold text-zinc-500" title="No score — analysis did not complete">—</div>
              <div className="text-[11px] text-zinc-500">No score — analysis did not complete</div>
            </div>
            <div className="rounded-xl border border-zinc-800/80 bg-[#0f0f12] p-4 space-y-1">
              <div className="text-[11px] font-mono font-semibold uppercase tracking-wider text-zinc-500">Blockers</div>
              <div className="font-mono text-sm text-zinc-500">—</div>
            </div>
            <div className="rounded-xl border border-zinc-800/80 bg-[#0f0f12] p-4 space-y-1">
              <div className="text-[11px] font-mono font-semibold uppercase tracking-wider text-zinc-500">Blast Radius</div>
              <div className="font-mono text-sm text-zinc-500">—</div>
            </div>
          </section>

          <section className="rounded-xl border border-zinc-800/80 bg-[#0f0f12] p-5">
            <h3 className="text-xs font-mono font-semibold uppercase tracking-[0.16em] text-zinc-300">Structural &amp; Blast Radius Analysis</h3>
            <p className="mt-2 text-xs text-zinc-500 leading-relaxed">Unavailable — the analysis did not complete, so there is no structural telemetry to report.</p>
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
          {/* Honest pending panel: queued summary verbatim, no verdict telemetry. */}
          <section className="rounded-xl border border-zinc-800/80 bg-[#0f0f12] p-5">
            <h3 className="text-xs font-mono font-semibold uppercase tracking-[0.16em] text-zinc-300">PR Summary</h3>
            <p className="mt-2 text-sm text-zinc-400 leading-relaxed">{review.summary}</p>
          </section>

          <section className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
            <div className="rounded-xl border border-zinc-800/80 bg-[#0f0f12] p-4 space-y-1">
              <div className="text-[11px] font-mono font-semibold uppercase tracking-wider text-zinc-500">Status</div>
              <span className="inline-flex items-center rounded-full border border-zinc-700/60 bg-zinc-900 px-2.5 py-1 text-xs font-mono font-semibold text-zinc-400">
                {pendingLabel}
              </span>
            </div>
            <div className="rounded-xl border border-zinc-800/80 bg-[#0f0f12] p-4 space-y-1">
              <div className="text-[11px] font-mono font-semibold uppercase tracking-wider text-zinc-500">PR Confidence</div>
              <div className="font-mono text-xl font-bold text-zinc-500" title="No score — analysis has not completed">—</div>
              <div className="text-[11px] text-zinc-500">No score — analysis has not completed</div>
            </div>
            <div className="rounded-xl border border-zinc-800/80 bg-[#0f0f12] p-4 space-y-1">
              <div className="text-[11px] font-mono font-semibold uppercase tracking-wider text-zinc-500">Blockers</div>
              <div className="font-mono text-sm text-zinc-500">—</div>
            </div>
            <div className="rounded-xl border border-zinc-800/80 bg-[#0f0f12] p-4 space-y-1">
              <div className="text-[11px] font-mono font-semibold uppercase tracking-wider text-zinc-500">Blast Radius</div>
              <div className="font-mono text-sm text-zinc-500">—</div>
            </div>
          </section>

          <section className="rounded-xl border border-zinc-800/80 bg-[#0f0f12] p-5">
            <h3 className="text-xs font-mono font-semibold uppercase tracking-[0.16em] text-zinc-300">Structural &amp; Blast Radius Analysis</h3>
            <p className="mt-2 text-xs text-zinc-500 leading-relaxed">Unavailable — the analysis has not completed yet, so there is no structural telemetry to report.</p>
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
      {/* PR Summary */}
      <section className="rounded-xl border border-zinc-800/80 bg-[#0f0f12] p-5">
        <h3 className="text-xs font-mono font-semibold uppercase tracking-[0.16em] text-zinc-300">PR Summary</h3>
        <p className="mt-2 text-sm text-zinc-200 leading-relaxed">{review.summary}</p>
      </section>

      {/* Status grid */}
      <section className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
        <div className="rounded-xl border border-zinc-800/80 bg-[#0f0f12] p-4 space-y-1">
          <div className="text-[11px] font-mono font-semibold uppercase tracking-wider text-zinc-500">Status</div>
          <span className={cn("inline-flex items-center rounded-full border px-2.5 py-1 text-xs font-mono font-semibold", verdict.color)}>
            {verdict.label}
          </span>
        </div>
        <div className="rounded-xl border border-zinc-800/80 bg-[#0f0f12] p-4 space-y-1">
          <div className="text-[11px] font-mono font-semibold uppercase tracking-wider text-zinc-500">PR Confidence</div>
          <div className="font-mono text-xl font-bold text-zinc-100">{confidence}/10</div>
          <div className="text-[11px] text-zinc-500">Derived from risk score {review.risk_score}</div>
        </div>
        <div className="rounded-xl border border-zinc-800/80 bg-[#0f0f12] p-4 space-y-1">
          <div className="text-[11px] font-mono font-semibold uppercase tracking-wider text-zinc-500">Blockers</div>
          <div className="font-mono text-sm text-zinc-200">
            Must-Fix <span className={cn("font-bold", blockers.mustFix > 0 ? "text-rose-400" : "text-emerald-400")}>{blockers.mustFix}</span>
            <span className="text-zinc-600"> · </span>
            Should-Fix <span className={cn("font-bold", blockers.shouldFix > 0 ? "text-amber-400" : "text-emerald-400")}>{blockers.shouldFix}</span>
          </div>
          <div className="text-[11px] text-zinc-500">Critical/high vs medium findings</div>
        </div>
        <div className="rounded-xl border border-zinc-800/80 bg-[#0f0f12] p-4 space-y-1">
          <div className="text-[11px] font-mono font-semibold uppercase tracking-wider text-zinc-500">Blast Radius</div>
          <div className="font-mono text-sm font-bold text-zinc-100">{radius.label}</div>
          <div className="text-[11px] text-zinc-500">{radius.explanation}</div>
        </div>
      </section>

      {/* Structural & Blast Radius Analysis */}
      <section className="rounded-xl border border-zinc-800/80 bg-[#0f0f12] p-5 space-y-3">
        <h3 className="text-xs font-mono font-semibold uppercase tracking-[0.16em] text-zinc-300">Structural &amp; Blast Radius Analysis</h3>
        <ul className="space-y-2.5 text-xs text-zinc-300 leading-relaxed list-disc pl-5">
          <li>
            <span className="font-semibold text-zinc-100">Impact Surface:</span> {radius.label} — {radius.explanation}.
          </li>
          <li>
            <span className="font-semibold text-zinc-100">Files Modified:</span>{" "}
            <span className="font-mono">{blast.filesCount} {blast.filesCount === 1 ? "file" : "files"}</span>{" "}
            <span className="font-mono text-emerald-400">+{blast.additions}</span>
            <span className="font-mono text-zinc-500">/</span>
            <span className="font-mono text-rose-400">-{blast.deletions}</span>{" "}
            <span className={cn("ml-1 px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wider border", blast.scopeColor)}>
              {blast.scope} mutation scope
            </span>
          </li>
          {touchedFiles.length > 0 && (
            <li>
              <span className="font-semibold text-zinc-100">Touched Components:</span>{" "}
              <span className="inline-flex flex-wrap gap-1.5 align-middle">
                {shownFiles.map((f) => (
                  <span key={f} className="font-mono text-[10px] text-zinc-300 bg-zinc-900 px-1.5 py-0.5 rounded border border-zinc-800">{f}</span>
                ))}
                {hiddenFiles > 0 && (
                  <span className="font-mono text-[10px] text-zinc-500">+{hiddenFiles} more</span>
                )}
              </span>
            </li>
          )}
          <li>
            <span className="font-semibold text-zinc-100">Risk Assessment:</span>{" "}
            {blockers.mustFix > 0
              ? `Risk ${review.risk_score}/100 with ${blockers.mustFix} must-fix ${blockers.mustFix === 1 ? "finding" : "findings"} — address blockers before merge.`
              : review.risk_score < RISK_MODERATE
                ? `Risk ${review.risk_score}/100 with no must-fix findings — safe to merge pending routine review.`
                : `Risk ${review.risk_score}/100 with no must-fix findings — merge after a focused review pass.`}
          </li>
        </ul>
      </section>

      {/* Findings Summary */}
      <section className="space-y-3">
        <div className="flex items-center justify-between text-xs font-semibold text-zinc-400 uppercase tracking-wider font-mono">
          <span>Findings Summary ({review.findings.length})</span>
          {review.findings.length > 0 && (
            <span className="text-[10px] text-zinc-500 font-normal">Pre-verified candidate patches</span>
          )}
        </div>
        {review.findings.length === 0 ? (
          <div className="flex items-center gap-2 rounded-xl border border-emerald-500/30 bg-emerald-950/20 px-4 py-3 text-xs font-mono text-emerald-300">
            <Check className="h-4 w-4 text-emerald-400" />
            No regressions detected — no actionable findings on this review.
          </div>
        ) : (
          <div className="space-y-3">
            {review.findings.map((f, i) => (
              <FindingItem
                key={i}
                finding={f}
                repoOwner={review.repo_owner}
                repoName={review.repo_name}
                commitSha={review.commit_sha}
              />
            ))}
          </div>
        )}
      </section>

      {/* Metadata footer */}
      <section className="flex flex-wrap items-center gap-2.5 rounded-lg border border-zinc-800/80 bg-zinc-950/70 px-3 py-2 text-[11px] font-mono text-zinc-400">
        <span>{review.input_tokens + review.output_tokens} tokens</span>
        <span className="text-zinc-700">|</span>
        <span className="uppercase tracking-wider text-[10px]">Mutation scope:</span>
        <span className={cn("px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wider border", blast.scopeColor)}>
          {blast.scope}
        </span>
        <span className="text-zinc-700">|</span>
        <span className="uppercase text-[10px] text-zinc-500">{review.status}</span>
        {review.findings.length === 0 && (
          <span className="inline-flex items-center gap-1 rounded-full border border-emerald-500/30 bg-emerald-950/30 px-2 py-0.5 text-[10px] text-emerald-300">
            <Check className="h-2.5 w-2.5 text-emerald-400" />
            No regressions
          </span>
        )}
      </section>
        </>
      )}
    </div>
  );
}

export default function ReviewsPage() {
  const [reviews, setReviews] = useState<CodeReviewOut[]>([]);
  const [repos, setRepos] = useState<RepoOut[]>([]);
  const [selectedRepoId, setSelectedRepoId] = useState<string>("all");
  const [selectedSeverity, setSelectedSeverity] = useState<string>("all");
  const [highRiskOnly, setHighRiskOnly] = useState<boolean>(false);
  const [hideFailed, setHideFailed] = useState<boolean>(false);
  const [activeTab, setActiveTab] = useState<"all" | "high_risk" | "avg_risk" | "patches">("all");
  const [searchQuery, setSearchQuery] = useState<string>("");
  const [loading, setLoading] = useState<boolean>(true);
  const [refreshing, setRefreshing] = useState<boolean>(false);
  const [page, setPage] = useState<number>(0);
  const [totalCount, setTotalCount] = useState<number>(0);
  const [fetchError, setFetchError] = useState<string | null>(null);

  // Detail view: selected review id, deep-linkable via `?review=<id>`.
  const [selectedId, setSelectedId] = useState<string | null>(() => readReviewParam());
  const [detailReview, setDetailReview] = useState<CodeReviewOut | null>(null);
  const [detailLoading, setDetailLoading] = useState<boolean>(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailRetry, setDetailRetry] = useState<number>(0);
  const [listLoaded, setListLoaded] = useState<boolean>(false);

  // Load repos for filter
  useEffect(() => {
    api.getRepos()
      .then((data) => setRepos(data || []))
      .catch((err) => console.error("Failed to load repos:", err));
  }, []);

  const fetchReviews = useCallback(async () => {
    try {
      setLoading(true);
      const params: {
        limit: number;
        offset: number;
        repo_id?: string;
        min_risk?: number;
        severity?: string;
      } = {
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      };

      if (selectedRepoId !== "all") {
        params.repo_id = selectedRepoId;
      }
      if (highRiskOnly) {
        params.min_risk = RISK_CRITICAL;
      }
      if (selectedSeverity !== "all") {
        params.severity = selectedSeverity;
      }

      const res = await api.getCodeReviews(params);
      setReviews(res.reviews || []);
      setTotalCount(res.total || 0);
      setFetchError(null);
    } catch (err) {
      console.error("Failed to fetch code reviews:", err);
      setFetchError("Couldn't load code reviews. Check your connection and retry.");
    } finally {
      setLoading(false);
      setRefreshing(false);
      setListLoaded(true);
    }
  }, [selectedRepoId, highRiskOnly, selectedSeverity, page]);

  useEffect(() => {
    fetchReviews();
  }, [fetchReviews]);

  const openReview = useCallback((id: string) => {
    window.history.pushState({ review: id }, "", `${window.location.pathname}?review=${encodeURIComponent(id)}`);
    setDetailError(null);
    setSelectedId(id);
  }, []);

  const closeReview = useCallback(() => {
    window.history.pushState({}, "", window.location.pathname);
    setSelectedId(null);
  }, []);

  // Back/forward buttons stay in sync with the deep link.
  useEffect(() => {
    const onPopState = () => setSelectedId(readReviewParam());
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  // Keep keyboard focus on a live element across detail open/close: opening
  // the detail unmounts the triggering "Full audit" button, closing unmounts
  // Back — both would otherwise drop focus to <body>.
  const backButtonRef = useRef<HTMLButtonElement>(null);
  const listTopRef = useRef<HTMLDivElement>(null);
  const prevSelectedRef = useRef<string | null>(null);
  useEffect(() => {
    if (selectedId) {
      backButtonRef.current?.focus();
    } else if (prevSelectedRef.current) {
      listTopRef.current?.focus();
    }
    prevSelectedRef.current = selectedId;
    // Intentionally transition-only: re-focusing on detail payload arrival
    // would yank focus away from wherever the user moved it.
  }, [selectedId]);

  // Escape closes the detail view.
  useEffect(() => {
    if (!selectedId) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeReview();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectedId, closeReview]);

  // Resolve the detail payload: prefer the already-fetched page row, fall
  // back to the existing GET detail endpoint for deep links / other pages.
  useEffect(() => {
    if (!selectedId) {
      setDetailReview(null);
      setDetailError(null);
      setDetailLoading(false);
      return;
    }
    const found = reviews.find((r) => r.id === selectedId);
    if (found) {
      setDetailReview(found);
      setDetailError(null);
      setDetailLoading(false);
      return;
    }
    if (!listLoaded) return;
    let cancelled = false;
    setDetailLoading(true);
    setDetailError(null);
    api.getReviewDetail(selectedId).then(
      (r) => {
        if (!cancelled) {
          setDetailReview(r);
          setDetailLoading(false);
        }
      },
      () => {
        if (!cancelled) {
          setDetailReview(null);
          setDetailError("Couldn't load that review. It may have moved pages — go back and retry.");
          setDetailLoading(false);
        }
      }
    );
    return () => {
      cancelled = true;
    };
  }, [selectedId, reviews, listLoaded, detailRetry]);

  const handleRefresh = () => {
    setRefreshing(true);
    fetchReviews();
  };

  // Detail Retry honestly re-polls the GET-only list (there is no analysis
  // re-run endpoint) AND bumps detailRetry so off-page deep links refetch
  // the detail endpoint too. Bumping detailRetry alone is a no-op for cached
  // rows because the resolver above returns early on cache hit.
  const handleDetailRetry = useCallback(() => {
    setRefreshing(true);
    fetchReviews();
    setDetailRetry((n) => n + 1);
  }, [fetchReviews]);

  // Client-side text search & ribbon tab filtering
  const filteredReviews = useMemo(() => {
    let list = reviews;
    // Explicit opt-in only — default OFF so failed rows are never hidden silently.
    if (hideFailed) {
      list = list.filter((r) => !isReviewFailed(r));
    }
    if (activeTab === "patches") {
      list = list.filter((r) => r.findings.some((f) => !!f.suggested_patch));
    }
    if (!searchQuery.trim()) return list;
    const q = searchQuery.toLowerCase();
    return list.filter((r) => {
      const matchSummary = r.summary?.toLowerCase().includes(q);
      const matchFailure = r.failure_reason?.toLowerCase().includes(q);
      const matchCommit = r.commit_sha?.toLowerCase().includes(q);
      const matchRepo = (r.repo_owner && r.repo_name) ? `${r.repo_owner}/${r.repo_name}`.toLowerCase().includes(q) : false;
      const matchPr = r.pr_title?.toLowerCase().includes(q) || (r.pr_author && r.pr_author.toLowerCase().includes(q));
      const matchFindings = r.findings.some(
        (f) =>
          f.file_path.toLowerCase().includes(q) ||
          f.critique.toLowerCase().includes(q) ||
          (f.symbol_name && f.symbol_name.toLowerCase().includes(q))
      );
      return matchSummary || matchFailure || matchCommit || matchRepo || matchPr || matchFindings;
    });
  }, [reviews, searchQuery, activeTab, hideFailed]);

  // Metric rollups — computed from the current page only (`reviews` holds a
  // single PAGE_SIZE window). Reviews Scanned uses the server-side
  // `totalCount` instead, so every tile derived here is explicitly labeled
  // "this page" in the ribbon below. Avg Risk scores finished analyses only:
  // failed rows default risk_score to 0 (averaging it in would lie, e.g.
  // 85+0→43), and pending / in_progress rows are still at the 0
  // default because analysis never ran — only "completed" and scored
  // "suppressed" carry a real score ("suppressed" is terminal WITH a saved audit:
  // backend/app/services/review_orchestrator.py:1233-1239 + :1287; unscored
  // suppressed rows with risk 0 + no findings stay out like pending).
  // Reports how many rows actually scored.
  const pageStats = useMemo(() => {
    const total = reviews.length;
    const highRisk = reviews.filter((r) => r.risk_score >= RISK_CRITICAL).length;
    const scored = reviews.filter((r) => {
      const s = r.status?.toLowerCase();
      if (s === "completed") return true;
      if (s === "suppressed") return !isSuppressedUnscored(r);
      return false;
    });
    const avgScore = scored.length > 0 ? Math.round(scored.reduce((acc, r) => acc + r.risk_score, 0) / scored.length) : 0;
    const totalPatches = reviews.reduce((acc, r) => acc + r.findings.filter((f) => !!f.suggested_patch).length, 0);
    return { total, highRisk, avgScore, totalPatches, scoredCount: scored.length };
  }, [reviews]);

  const repoOptions = [
    { value: "all", label: "All Repositories" },
    ...repos.map((r) => ({
      value: r.id,
      label: `${r.owner}/${r.name}`,
    })),
  ];

  const failedCount = useMemo(() => reviews.filter((r) => isReviewFailed(r)).length, [reviews]);

  return (
    <AppLayout
      title="Code Reviews"
      subtitle="Autonomous push-level code review & actionable remediation sentinel"
      actions={
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={handleRefresh}
            disabled={refreshing || loading}
            className="h-8 gap-1.5 border-zinc-700 bg-zinc-900 text-zinc-300 hover:bg-zinc-800"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", (refreshing || loading) && "animate-spin")} />
            <span>Refresh</span>
          </Button>
        </div>
      }
    >
      <div className="space-y-6 pb-12">
        {selectedId ? (
          <div aria-live="polite">
            <Button
              variant="outline"
              size="sm"
              onClick={closeReview}
              ref={backButtonRef}
              className="h-8 gap-1.5 border-zinc-700 bg-zinc-900 text-zinc-300 hover:bg-zinc-800"
            >
              <ArrowLeft className="h-3.5 w-3.5" />
              <span>Back to reviews</span>
            </Button>

            {detailLoading || (!detailReview && !detailError) ? (
              <div className="space-y-4" role="status" aria-label="Loading review detail">
                <Skeleton className="h-8 w-64" />
                <div className="rounded-xl border border-zinc-800/80 bg-[#111114] p-5 space-y-3">
                  <Skeleton className="h-5 w-48" />
                  <Skeleton className="h-4 w-full" />
                  <Skeleton className="h-4 w-3/4" />
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
                  {[1, 2, 3, 4].map((n) => (
                    <Skeleton key={n} className="h-20 w-full" />
                  ))}
                </div>
              </div>
            ) : detailError || !detailReview ? (
              <div
                className="flex items-center justify-between gap-3 rounded-xl border border-rose-500/40 bg-rose-950/30 px-4 py-2.5 text-xs font-mono text-rose-300"
              >
                <span role="alert">{detailError ?? "Review not found."}</span>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleDetailRetry}
                  className="h-7 border-rose-500/40 text-xs text-rose-200 hover:bg-rose-900/40"
                >
                  Retry
                </Button>
              </div>
            ) : (
              <ReviewDetail review={detailReview} onRetry={handleDetailRetry} />
            )}
          </div>
        ) : (
          <>
        {/* Report header: slim telemetry strip doubling as filter tabs.
            Definitions: Reviews Scanned is the server-side total across all
            pages; every other figure is counted on this page only. High-risk
            means risk >= 71 — the same bar as the Risk toggle, stats, gauge
            and merge verdict. */}
        <div ref={listTopRef} tabIndex={-1} className="rounded-xl border border-zinc-800/90 bg-gradient-to-b from-[#141418] via-[#101013] to-[#0a0a0d] p-1.5 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_4px_16px_rgba(0,0,0,0.35)] outline-none">
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-1.5">
            {/* Tab 1: All Reviews */}
            <button
              type="button"
              aria-pressed={activeTab === "all" && !highRiskOnly}
              onClick={() => {
                setActiveTab("all");
                setHighRiskOnly(false);
                setPage(0);
              }}
              className={cn(
                "flex items-baseline justify-between gap-2 rounded-lg px-3 py-2 text-left transition-all",
                activeTab === "all" && !highRiskOnly
                  ? "bg-zinc-800/90 border border-zinc-700/80 shadow-[inset_0_1px_0_rgba(255,255,255,0.1),0_2px_6px_rgba(0,0,0,0.4)]"
                  : "bg-transparent border border-transparent hover:bg-zinc-900/60 hover:border-zinc-800/60"
              )}
            >
              <div className="min-w-0">
                <div className="flex items-center gap-1.5 text-[11px] font-mono font-semibold uppercase tracking-wider text-zinc-300">
                  <ShieldCheck className="h-3.5 w-3.5 text-emerald-400" />
                  <span>Scanned</span>
                </div>
                <div className="mt-0.5 text-[11px] text-zinc-500 font-sans">Push &amp; PR scans · server total</div>
              </div>
              <div className="text-xl font-bold font-mono text-zinc-100">{totalCount}</div>
            </button>

            {/* Tab 2: High Risk Flagged */}
            <button
              type="button"
              aria-pressed={highRiskOnly || activeTab === "high_risk"}
              onClick={() => {
                setActiveTab("high_risk");
                setHighRiskOnly(true);
                setPage(0);
              }}
              className={cn(
                "flex items-baseline justify-between gap-2 rounded-lg px-3 py-2 text-left transition-all",
                highRiskOnly || activeTab === "high_risk"
                  ? "bg-rose-950/30 border border-rose-500/40 shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_2px_6px_rgba(0,0,0,0.4)]"
                  : "bg-transparent border border-transparent hover:bg-zinc-900/60 hover:border-zinc-800/60"
              )}
            >
              <div className="min-w-0">
                <div className="flex items-center gap-1.5 text-[11px] font-mono font-semibold uppercase tracking-wider text-rose-300">
                  <ShieldAlert className="h-3.5 w-3.5 text-rose-400" />
                  <span>High-Risk ≥ 71</span>
                </div>
                <div className="mt-0.5 text-[11px] text-zinc-500 font-sans" title="Scores >= 71 request changes · counted on this page only">Changes requested · this page</div>
              </div>
              <div className="text-xl font-bold font-mono text-rose-400">{pageStats.highRisk}</div>
            </button>

            {/* Tab 3: Avg Risk Score */}
            <button
              type="button"
              aria-pressed={activeTab === "avg_risk"}
              onClick={() => {
                setActiveTab("avg_risk");
                setHighRiskOnly(false);
                setPage(0);
              }}
              className={cn(
                "flex items-baseline justify-between gap-2 rounded-lg px-3 py-2 text-left transition-all",
                activeTab === "avg_risk"
                  ? "bg-zinc-800/90 border border-zinc-700/80 shadow-[inset_0_1px_0_rgba(255,255,255,0.1),0_2px_6px_rgba(0,0,0,0.4)]"
                  : "bg-transparent border border-transparent hover:bg-zinc-900/60 hover:border-zinc-800/60"
              )}
            >
              <div className="min-w-0">
                <div className="flex items-center gap-1.5 text-[11px] font-mono font-semibold uppercase tracking-wider text-amber-300">
                  <Sparkles className="h-3.5 w-3.5 text-amber-400" />
                  <span>Avg Risk</span>
                </div>
                <div className="mt-0.5 text-[11px] text-zinc-500 font-sans">Mean score · this page · {pageStats.scoredCount === 0 ? "no scored reviews" : `${pageStats.scoredCount} scored`}</div>
              </div>
              <div
                className="text-xl font-bold font-mono text-zinc-100"
                title={pageStats.scoredCount === 0 ? "No scored reviews on this page — only completed + publication-skipped analyses score" : `Mean of ${pageStats.scoredCount} scored ${pageStats.scoredCount === 1 ? "review" : "reviews"} · only completed + publication-skipped analyses score`}
              >
                {pageStats.scoredCount === 0 ? (
                  <span className="text-zinc-500" title="No score — no completed or publication-skipped analyses on this page">—</span>
                ) : (
                  <>{pageStats.avgScore} <span className="text-[11px] text-zinc-400 font-normal">/ 100</span></>
                )}
              </div>
            </button>

            {/* Tab 4: Actionable Patches */}
            <button
              type="button"
              aria-pressed={activeTab === "patches"}
              onClick={() => {
                setActiveTab("patches");
                setPage(0);
              }}
              className={cn(
                "flex items-baseline justify-between gap-2 rounded-lg px-3 py-2 text-left transition-all",
                activeTab === "patches"
                  ? "bg-cyan-950/30 border border-cyan-500/40 shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_2px_6px_rgba(0,0,0,0.4)]"
                  : "bg-transparent border border-transparent hover:bg-zinc-900/60 hover:border-zinc-800/60"
              )}
            >
              <div className="min-w-0">
                <div className="flex items-center gap-1.5 text-[11px] font-mono font-semibold uppercase tracking-wider text-cyan-300">
                  <Zap className="h-3.5 w-3.5 text-cyan-400" />
                  <span>Patches</span>
                </div>
                <div className="mt-0.5 text-[11px] text-zinc-500 font-sans">With suggested fix · this page</div>
              </div>
              <div className="text-xl font-bold font-mono text-cyan-400">{pageStats.totalPatches}</div>
            </button>
          </div>
        </div>

        {/* Filter Controls Bar */}
        <div className="flex flex-col md:flex-row items-stretch md:items-center justify-between gap-3 rounded-xl border border-zinc-800/80 bg-[#0f0f12] p-3">
          <div className="flex flex-wrap items-center gap-2.5">
            <div className="w-[200px]">
              <SelectDropdown
                value={selectedRepoId}
                onChange={(val) => {
                  setSelectedRepoId(val);
                  setPage(0);
                }}
                options={repoOptions}
                placeholder="All Repositories"
              />
            </div>

            {/* Severity filter pills */}
            <div className="flex items-center rounded-lg border border-zinc-800 bg-zinc-950 p-1 text-xs">
              {["all", "critical", "high", "medium", "low"].map((sev) => (
                <button
                  key={sev}
                  onClick={() => {
                    setSelectedSeverity(sev);
                    setPage(0);
                  }}
                  className={cn(
                    "rounded-md px-2.5 py-1 font-mono uppercase text-[10px] tracking-wider transition-all",
                    selectedSeverity === sev
                      ? "bg-zinc-800 text-zinc-100 font-semibold shadow-[0_1px_3px_rgba(0,0,0,0.3)]"
                      : "text-zinc-500 hover:text-zinc-300"
                  )}
                >
                  {sev}
                </button>
              ))}
            </div>

            {/* High Risk score filter toggle (risk >= 71 — same bar as the ribbon, stats and verdict) */}
            <button
              onClick={() => {
                const nextVal = !highRiskOnly;
                setHighRiskOnly(nextVal);
                setActiveTab(nextVal ? "high_risk" : "all");
                setPage(0);
              }}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-mono transition-all",
                highRiskOnly
                  ? "border-rose-500/50 bg-rose-950/40 text-rose-300 shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_2px_4px_rgba(0,0,0,0.3)]"
                  : "border-zinc-800 bg-zinc-950 text-zinc-400 hover:border-zinc-700 hover:text-zinc-200"
              )}
            >
              <ShieldAlert className="h-3.5 w-3.5 text-rose-400" />
              <span>Risk ≥ 71</span>
            </button>

            {/* Hide-failed toggle — default OFF, session state only. Explicit
                opt-in: failed rows are never hidden silently. */}
            {failedCount > 0 && (
              <button
                onClick={() => setHideFailed((v) => !v)}
                aria-pressed={hideFailed}
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-mono transition-all",
                  hideFailed
                    ? "border-zinc-500/60 bg-zinc-800 text-zinc-100 shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_2px_4px_rgba(0,0,0,0.3)]"
                    : "border-zinc-800 bg-zinc-950 text-zinc-400 hover:border-zinc-700 hover:text-zinc-200"
                )}
              >
                <EyeOff className="h-3.5 w-3.5 text-zinc-500" />
                <span>{hideFailed ? `Show failed (${failedCount})` : `Hide failed (${failedCount})`}</span>
              </button>
            )}
          </div>

          <div className="flex items-center gap-2">
            <div className="relative flex-1 md:w-64">
              <Search className="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-zinc-500" />
              <Input
                placeholder="Search findings, commits, files..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="pl-8 h-8 text-xs bg-zinc-950 border-zinc-800 focus:border-amber-400"
              />
            </div>
          </div>
        </div>

        {/* Fetch error banner (replaces silent console-only failure).
            Alert covers the message only; Retry stays outside role=alert. */}
        {fetchError && (
          <div
            className="flex items-center justify-between gap-3 rounded-xl border border-rose-500/40 bg-rose-950/30 px-4 py-2.5 text-xs font-mono text-rose-300"
          >
            <span role="alert">{fetchError}</span>
            <Button
              variant="outline"
              size="sm"
              onClick={handleRefresh}
              className="h-7 border-rose-500/40 text-xs text-rose-200 hover:bg-rose-900/40"
            >
              Retry
            </Button>
          </div>
        )}

        {/* Review Cards Stream */}
        {loading && reviews.length === 0 ? (
          <div className="space-y-4">
            {[1, 2, 3].map((n) => (
              <div key={n} className="rounded-xl border border-zinc-800/80 bg-[#111114] p-5 space-y-3">
                <div className="flex items-center justify-between">
                  <Skeleton className="h-5 w-48" />
                  <Skeleton className="h-5 w-24" />
                </div>
                <Skeleton className="h-4 w-full" />
                <Skeleton className="h-4 w-3/4" />
              </div>
            ))}
          </div>
        ) : filteredReviews.length === 0 ? (
          <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-zinc-800 bg-[#0c0c0e] py-16 text-center space-y-3">
            <div className="flex h-12 w-12 items-center justify-center rounded-full bg-zinc-900 border border-zinc-800 text-zinc-500">
              <ShieldCheck className="h-6 w-6 text-zinc-500" />
            </div>
            <div className="space-y-1">
              <h3 className="text-sm font-semibold text-zinc-200">No Code Reviews Found</h3>
              <p className="text-xs text-zinc-400 max-w-sm">
                Push commits or open a pull request on your connected repositories to trigger Haunter&apos;s Sentinel.
              </p>
            </div>
            {(selectedRepoId !== "all" || highRiskOnly || hideFailed || activeTab !== "all" || selectedSeverity !== "all" || searchQuery) && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  setSelectedRepoId("all");
                  setSelectedSeverity("all");
                  setHighRiskOnly(false);
                  setHideFailed(false);
                  setActiveTab("all");
                  setSearchQuery("");
                  setPage(0);
                }}
                className="mt-2 text-xs border-zinc-700 hover:bg-zinc-800 text-zinc-300"
              >
                Clear Filters
              </Button>
            )}
          </div>
        ) : (
          <div className="space-y-5">
            {filteredReviews.map((review) => (
              <ReviewCard key={review.id} review={review} onOpen={openReview} onRetry={handleRefresh} />
            ))}
          </div>
        )}

        {/* Pagination controls */}
        {totalCount > PAGE_SIZE && (
          <div className="flex items-center justify-between pt-4 border-t border-zinc-800 text-xs font-mono text-zinc-500">
            <div>
              Showing {page * PAGE_SIZE + 1} to {Math.min((page + 1) * PAGE_SIZE, totalCount)} of {totalCount} reviews
            </div>
            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={page === 0}
                onClick={() => setPage(page - 1)}
                className="h-8 border-zinc-800 text-xs"
              >
                Previous
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={(page + 1) * PAGE_SIZE >= totalCount}
                onClick={() => setPage(page + 1)}
                className="h-8 border-zinc-800 text-xs"
              >
                Next
              </Button>
            </div>
          </div>
        )}
          </>
        )}
      </div>
    </AppLayout>
  );
}
