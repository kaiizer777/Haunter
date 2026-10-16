"use client";

import { useEffect, useState, useCallback, useMemo, useRef } from "react";
import { AppLayout } from "@/components/layout/app-layout";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { SelectDropdown } from "@/components/ui/select-dropdown";
import {
  api,
  AssociatedPROut,
  CodeReviewOut,
  RepoOut,
  ReviewFindingOut,
} from "@/lib/api";
import { formatRelativeTime, cn } from "@/lib/utils";
import {
  RISK_CRITICAL,
  RISK_MODERATE,
  blockerCounts,
  clusterFailures,
  confidenceFromRisk,
  pendingOutcome,
  riskBand,
  scoredOutcome,
  sortByAttention,
  outcomeMeaning,
  FAILED_OUTCOME,
  SUPPRESSED_UNSCORED_OUTCOME,
  OUTCOME_ORDER,
  SORT_OPTIONS,
  type FailureCluster,
  type Outcome,
  type OutcomeKey,
  type SortKey,
} from "./review-triage";
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
  SlidersHorizontal,
  Inbox,
} from "lucide-react";

const PAGE_SIZE = 15;

/** A review whose analysis never completed — never render verdict telemetry for it.
 * Failed iff the backend reports status "error". A stale `failure_reason` on a
 * completed review must NOT hide its real verdict (the API type allows the
 * combination), so `failure_reason` alone is intentionally ignored here. */
function isReviewFailed(review: CodeReviewOut): boolean {
  return review.status?.toLowerCase() === "error";
}

/**
 * A row whose analysis FAILED, as opposed to one whose analysis succeeded and
 * whose PUBLICATION failed.
 *
 * The backend uses `status="error"` for both, but keeps `findings`, `summary`
 * and token telemetry on the row for the publish-failure case — deliberately,
 * so the dashboard stays truthful about an audit it did compute
 * (backend/app/services/review_orchestrator.py: "Publish failures set
 * status=error + failure_reason while keeping findings/summary so the
 * dashboard stays truthful").
 *
 * Collapsing the two means `isReviewFailed` routes a perfectly good saved
 * audit into FailedReviewCard, hiding every finding the orchestrator paid for.
 * The saved audit is still a scored audit and is rendered as one; only the
 * genuine analysis failures get the neutral error card.
 *
 * Complement of isPublishFailed below: every error row lands on exactly one
 * side. A custom analysis-failure summary ("Code review analysis failed:
 * ...", backend/app/services/review_orchestrator.py:1223) must NOT flip a
 * row to the scored branch — hence the strict positive-evidence gate there
 * instead of the summary check hasCompletedAnalysis performs.
 */
function isAnalysisFailed(review: CodeReviewOut): boolean {
  return isReviewFailed(review) && !isPublishFailed(review);
}

/** status=error that still carries a completed analysis — publish failure.
 *
 * Strict positive-evidence gate. The orchestrator persists risk_score +
 * findings + token telemetry TOGETHER before it attempts publication
 * (backend/app/services/review_orchestrator.py:1233-1239), and NO
 * analysis-failure path writes any of them: the diff-fetch failure (:1193),
 * the LLM failure (:1222) and the repo-not-found guard (:1101) write only
 * status + failure_reason + summary, and the timeout/crash backstop
 * (:1034-1061) only touches status + failure_reason. So tokens, findings or
 * a nonzero risk on an error row prove the persist step ran. (Enqueue
 * defaults are risk 0 + findings [] + zero tokens —
 * backend/app/webhooks.py:2271-2279.)
 *
 * The 0/0-tokens + empty-findings + risk-0 corner (a clean or empty-diff
 * audit whose publish failed) is structurally identical to an LLM analysis
 * failure, so it falls through to the failure_reason prefixes below: the
 * three post-persist error writes are "Failed to publish PR review:"
 * (:1411/:1517), "Failed to publish commit comment:" (:1690) and "Failed to
 * read repository review settings:" (:1267). Anything else — including a
 * custom analysis-failure summary — is fail-closed to analysis-failed, which
 * renders the neutral error card and never a fake "Great to Merge" verdict.
 */
function isPublishFailed(review: CodeReviewOut): boolean {
  if (!isReviewFailed(review)) return false;
  if (review.input_tokens > 0 || review.output_tokens > 0) return true;
  if (review.findings.length > 0) return true;
  if (review.risk_score > 0) return true;
  const reason = (review.failure_reason ?? "").toLowerCase();
  return (
    reason.startsWith("failed to publish pr review:") ||
    reason.startsWith("failed to publish commit comment:") ||
    reason.startsWith("failed to read repository review settings:")
  );
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

/** Enqueue placeholder written before analysis runs — the only summary a row
 * can carry when the orchestrator never persisted a result
 * (backend/app/webhooks.py:2276 + :2460). A non-empty summary alone therefore
 * proves nothing; it must differ from this. */
const QUEUED_SUMMARY = "Autonomous code review queued.";

/** Explicit completion signal for rows whose analysis output was persisted.
 * The orchestrator persists risk_score + summary + findings + token telemetry
 * TOGETHER before the publish guard
 * (backend/app/services/review_orchestrator.py:1233-1239), and the suppressed
 * path (:1287) only flips status, keeping the saved data. So a suppressed
 * row whose tokens are nonzero ran the LLM, and a row whose summary differs
 * from the enqueue placeholder got a persisted result — including the
 * empty-diff short-circuit, which returns 0/0 tokens with its own real
 * summary (backend/app/subagents/code_reviewer.py:691-702). Score + findings
 * alone cannot signal this: a genuine CLEAN audit is also 0 risk + empty.
 * (CodeReviewOut carries no updated_at — backend/app/routers/reviews.py — so
 * a timestamp comparison is unavailable.)
 *
 * Summary-only evidence is valid for suppressed rows (no failure path writes
 * a suppressed status) but NOT for error rows: analysis failures write their
 * own non-queued summaries ("Code review analysis failed: ...", "Failed to
 * fetch git diff: ...", "Repository record not found."). Error rows must go
 * through isPublishFailed's strict gate instead — never this function alone. */
function hasCompletedAnalysis(review: CodeReviewOut): boolean {
  if (review.input_tokens > 0 || review.output_tokens > 0) return true;
  const summary = (review.summary ?? "").trim();
  return summary.length > 0 && summary.toLowerCase() !== QUEUED_SUMMARY.toLowerCase();
}

/** Suppressed row with no saved audit — legacy row or suppress-before-save
 * race: analysis never persisted, so neither token telemetry nor a real
 * summary exists. Fail-closed: renders neutrally like pending, never as
 * Great-to-Merge / SAFE. Scored suppressed rows (real audit, even a clean
 * 0-risk/empty one) are unaffected. */
function isSuppressedUnscored(review: CodeReviewOut): boolean {
  return isSuppressed(review) && !hasCompletedAnalysis(review);
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
 * Confidence ring (0-10) for a completed review.
 *
 * The ring shows CONFIDENCE on a 0-10 scale, not the raw 0-100 risk score:
 * risk 45 → "6/10". A fuller arc means "more trustworthy", which is the
 * inverse of the old risk ring and reads correctly at a glance — a nearly
 * full ring next to a must-fix badge tells the user the model was sure of
 * itself, not that the change is safe.
 *
 * The COLOUR still comes from the single risk band (./review-triage), so
 * severity keeps exactly one colour language across the gauge, the verdict
 * chip and the blocker counts. The chip below states that band in words, so
 * severity is never carried by colour alone.
 *
 * Accessibility: the ring SVG is decorative (`aria-hidden`); the group is
 * announced once as "Confidence N of 10 — <band>". The 0-100 risk score stays
 * visible as text in the row so nothing is hidden by this choice.
 *
 * `neutral` renders the honest no-score state for analyses that never
 * finished: zinc track, em-dash, NO SCORE badge — never a fake 10/10.
 *
 * Named `ConfidenceGauge` because it renders confidence. It still *takes* a
 * risk score, because confidence is derived from risk (see confidenceFromRisk).
 */
function ConfidenceGauge({ riskScore, size = 68, neutral = false }: { riskScore: number; size?: number; neutral?: boolean }) {
  const strokeWidth = 5;
  const radius = (size - strokeWidth - 8) / 2;
  const circumference = 2 * Math.PI * radius;
  const clampedRisk = Math.max(0, Math.min(100, riskScore));
  const confidence = confidenceFromRisk(clampedRisk);
  const fraction = confidence / 10;
  const offset = neutral ? circumference : circumference * (1 - fraction);

  const band = riskBand(clampedRisk, !neutral);
  const compact = size <= 72;

  return (
    <div
      className="flex flex-col items-center justify-center"
      role="img"
      aria-label={
        neutral
          ? "No confidence score — the analysis did not complete"
          : `Confidence ${confidence} of 10 — ${band.label.toLowerCase()} risk`
      }
    >
      {/* Tactile circular bezel housing */}
      <div
        className="relative flex items-center justify-center rounded-full border border-zinc-800/90 bg-gradient-to-b from-[#18181b] via-[#111114] to-[#09090b] shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_2px_8px_rgba(0,0,0,0.5)]"
        style={{ width: size, height: size }}
      >
        <svg className="rotate-[-90deg]" width={size} height={size} aria-hidden="true">
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
            stroke={band.stroke}
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
        <div className="absolute inset-0 flex items-baseline justify-center">
          <span
            className={cn(
              "font-mono font-extrabold tabular-nums tracking-tight",
              compact ? "text-base" : "text-2xl",
              band.text
            )}
          >
            {neutral ? "—" : confidence}
          </span>
          {!neutral && (
            <span className={cn("ml-0.5 font-mono font-bold text-zinc-500", compact ? "text-[9px]" : "text-xs")}>
              /10
            </span>
          )}
        </div>
      </div>
      <span className={cn("mt-1.5 rounded-[4px] border px-2 py-0.5 text-[10px] font-mono font-bold tracking-wider uppercase", band.chip)}>
        {band.chipLabel}
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
 * review payload — no backend writes involved. Confidence and blocker counts
 * now come from ./review-triage so the row, the detail grid and the gauge can
 * never disagree about what a score means.
 */

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

/**
 * Merge verdict for the detail report header grid. Delegates to the shared
 * outcome vocabulary so the chip, the gauge ring and the blocker colours are
 * all driven by one severity band instead of three independent palettes.
 * Terminal-only: callers gate pending/failed rows out first.
 */
function verdictFor(review: CodeReviewOut): Outcome {
  return scoredOutcome({
    risk_score: review.risk_score,
    findings: review.findings,
  });
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

/**
 * Identity of one staged fix, used as the localStorage key.
 *
 * Every field here must discriminate between two DIFFERENT fixes, or staging
 * one silently toggles the other:
 *   - repo owner/name + commit sha: the same file/line/patch shape recurs
 *     across repos and across commits (the audit re-runs on every push to a
 *     branch), so without them a fix staged on `main` reads as staged on the
 *     next commit, and an identical finding in another repo collides.
 *   - the FULL patch: length + first 64 chars still collide for two patches
 *     that differ only after char 64, or that differ in length while sharing
 *     a 64-char prefix. Hashing the whole patch is what makes the key unique.
 *
 * The patch body is hashed rather than concatenated so the key stays a fixed
 * size for a multi-KB diff.
 */
function stagedFixKey(
  finding: ReviewFindingOut,
  scope: { repoOwner?: string | null; repoName?: string | null; commitSha?: string | null } = {}
): string {
  const patch = finding.suggested_patch ?? "";
  // FNV-1a, 32-bit — collision-resistant enough to separate two diffs in one
  // browser's storage, and stable across sessions (no crypto dependency).
  let hash = 0x811c9dc5;
  for (let i = 0; i < patch.length; i++) {
    hash ^= patch.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  const repo = scope.repoOwner && scope.repoName ? `${scope.repoOwner}/${scope.repoName}` : "";
  return `${repo}@${scope.commitSha ?? ""}:${finding.file_path}:${finding.line_start}-${finding.line_end}:${patch.length}:${hash.toString(16)}`;
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

function isFixStaged(
  finding: ReviewFindingOut,
  scope?: { repoOwner?: string | null; repoName?: string | null; commitSha?: string | null }
): boolean {
  try {
    return stagedFixKey(finding, scope) in readStagedFixes();
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
  const [staged, setStaged] = useState<boolean>(() => isFixStaged(finding, { repoOwner, repoName, commitSha }));
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
  //
  // Order matters: `localStorage.setItem` throws on a quota-exceeded or
  // private-mode browser, and it must run BEFORE any setState. Updating state
  // first and persisting second renders "Staged Fix" for a patch that was
  // never written — the badge then survives a reload as "not staged", which
  // is the worst possible failure for a control whose entire job is to
  // promise persistence.
  const handleStage = () => {
    if (!finding.suggested_patch || staging) return;
    setStageError(null);
    setStaging(true);
    try {
      const stored = readStagedFixes();
      const key = stagedFixKey(finding, { repoOwner, repoName, commitSha });
      const nextStaged = !(key in stored);
      if (nextStaged) {
        stored[key] = finding.suggested_patch;
      } else {
        delete stored[key];
      }
      // May throw — nothing above this line has touched React state yet, so a
      // persistence failure leaves the UI describing exactly what is stored.
      localStorage.setItem(STAGED_FIXES_KEY, JSON.stringify(stored));
      setStaged(nextStaged);
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
      title="View pull request on GitHub"
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
 * On-demand associated-PR lookup for push rows (`pr_number` NULL at rest).
 * Rows that belong to a PR (squash-merged, fast-forwarded) resolve to the PR
 * so the card can show `PR #n` instead of `Push @ sha`.
 *
 * Module-level cache keyed by review id: back/forward + re-renders never
 * refetch, and simultaneous mounts for one review share a single in-flight
 * request. Keyed by id — NOT by commit sha — because the lookup itself is
 * review-scoped (`GET /reviews/{id}/associated-pr`): the same sha can appear
 * in two repos/forks and must never share one entry. Resolves null (no PR /
 * token-missing / error) silently — the row keeps rendering `Push @ sha`
 * with no skeleton and no layout shift beyond the pill text swap. Only the
 * rendered page window fetches (~PAGE_SIZE calls max); rows that already
 * carry `pr_number` never fetch. List Refresh / detail Retry evict only the
 * failed (null) entries (see clearFailedAssociatedPrCache) so misses
 * genuinely retry while resolved PRs are kept, not refetched.
 */
const associatedPrCache = new Map<string, AssociatedPROut | null>();
const associatedPrInflight = new Map<string, Promise<AssociatedPROut | null>>();

/**
 * Drop every cached + in-flight associated-PR entry. Only the test reset
 * uses the full clear (suite isolation); product paths use
 * clearFailedAssociatedPrCache below so resolved PRs survive a Refresh.
 */
export function clearAssociatedPrCache(): void {
  associatedPrCache.clear();
  associatedPrInflight.clear();
  prRequestGeneration.clear();
  batchQueue = [];
  batchScheduled = false;
}

/**
 * Drop only failed (null) cached entries + in-flight lookups. Called by the
 * list Refresh and detail Retry paths: null misses genuinely retry on the
 * fresh row objects, while success entries are kept so resolved `PR #n`
 * pills never refetch (a full clear would re-fire ~PAGE_SIZE GitHub-backed
 * lookups on every Refresh, defeating the cache above).
 */
export function clearFailedAssociatedPrCache(): void {
  for (const [key, value] of associatedPrCache) {
    if (value == null) {
      associatedPrCache.delete(key);
      prRequestGeneration.delete(key);
    }
  }
  associatedPrInflight.clear();
  batchQueue = [];
  batchScheduled = false;
}

/** Test-only reset for the module-level associated-PR cache (vitest). */
export function __resetAssociatedPrCacheForTests(): void {
  clearAssociatedPrCache();
}

let batchQueue: Array<{
  id: string;
  resolve: (pr: AssociatedPROut | null) => void;
  generation: number;
}> = [];
let batchScheduled = false;
let currentGeneration = 0;
const prRequestGeneration = new Map<string, number>();

async function flushBatchPrLookups() {
  const queue = batchQueue;
  batchQueue = [];
  batchScheduled = false;

  if (queue.length === 0) return;

  const ids = Array.from(new Set(queue.map((item) => item.id)));

  let batchFailed = false;
  const batchMap = new Map<string, AssociatedPROut | null>();
  const hasBatchFn = typeof api.getAssociatedPrsBatch === "function";

  if (hasBatchFn) {
    try {
      const res = await api.getAssociatedPrsBatch(ids);
      if (res && Array.isArray(res.items)) {
        for (const item of res.items) {
          batchMap.set(item.review_id, item.associated_pr ?? null);
        }
      }
    } catch {
      batchFailed = true;
    }
  }

  if (batchFailed) {
    // When batch fails, do NOT fan out to individual requests (avoid amplifying backend load).
    // Mark queued items as null without caching the transient failure so remount/revisit retries.
    for (const item of queue) {
      item.resolve(null);
    }
    return;
  }

  for (const item of queue) {
    if (batchMap.has(item.id)) {
      const pr = batchMap.get(item.id) ?? null;
      if (prRequestGeneration.get(item.id) === item.generation) {
        associatedPrCache.set(item.id, pr);
      }
      item.resolve(pr);
    } else if (typeof api.getAssociatedPr === "function") {
      api.getAssociatedPr(item.id).then(
        (res) => {
          const pr = res?.associated_pr ?? null;
          if (prRequestGeneration.get(item.id) === item.generation) {
            associatedPrCache.set(item.id, pr);
          }
          item.resolve(pr);
        },
        () => {
          // Transient failure: leave uncached so revisiting or remounting can retry
          item.resolve(null);
        }
      );
    } else {
      if (prRequestGeneration.get(item.id) === item.generation) {
        associatedPrCache.set(item.id, null);
      }
      item.resolve(null);
    }
  }
}

function requestAssociatedPr(id: string): Promise<AssociatedPROut | null> {
  const gen = ++currentGeneration;
  prRequestGeneration.set(id, gen);
  return new Promise((resolve) => {
    batchQueue.push({ id, resolve, generation: gen });
    if (!batchScheduled) {
      batchScheduled = true;
      queueMicrotask(flushBatchPrLookups);
    }
  });
}

function useAssociatedPr(review: CodeReviewOut): AssociatedPROut | null {
  const [resolved, setResolved] = useState<AssociatedPROut | null>(
    () => associatedPrCache.get(review.id) ?? null
  );
  useEffect(() => {
    if (review.pr_number != null) return;
    const sha = review.commit_sha;
    if (!sha) return;
    const key = review.id;
    const cached = associatedPrCache.get(key);
    if (cached !== undefined) {
      setResolved(cached);
      return;
    }
    let cancelled = false;
    let pending = associatedPrInflight.get(key);
    if (!pending) {
      pending = requestAssociatedPr(review.id).finally(() => {
        associatedPrInflight.delete(key);
      });
      associatedPrInflight.set(key, pending);
    }
    pending.then((pr) => {
      if (!cancelled) setResolved(pr);
    });
    return () => {
      cancelled = true;
    };
    // Identity dep on purpose: a list Refresh / detail Retry delivers fresh
    // row objects, which re-runs the lookup (cache cleared by the handler →
    // genuine retry; cache intact → hit, no refetch). Plain re-renders and
    // back-nav reuse the same refs, so success caching still holds.
  }, [review]);
  if (review.pr_number != null) return null;
  return resolved;
}

/**
 * Whether this row is PR-associated for chrome purposes — either the row
 * carries a PR number directly, or the associated-PR lookup resolved one for
 * a push row. The SHA chip and the copy affordance must key off THIS, not
 * raw `pr_number`: a push row that resolved to PR #91 renders the PR pill,
 * so keying the chip off `pr_number` alone gave it the push-row "Copy SHA"
 * button and hid its commit link.
 */
function useIsPrAssociated(review: CodeReviewOut): boolean {
  const associated = useAssociatedPr(review);
  return review.pr_number != null || associated != null;
}

/**
 * Single identity-pill call site for every card + the detail header: amber
 * `PR #n` when PR-associated (directly or via associated-PR resolution),
 * equal-weight `Push @ sha` otherwise. Same PrPill component either way, so
 * no visual drift between direct-PR rows and resolved push rows.
 */
function IdentityPill({ review }: { review: CodeReviewOut }) {
  const associated = useAssociatedPr(review);
  const repoName = review.repo_owner && review.repo_name ? `${review.repo_owner}/${review.repo_name}` : "Repository";
  if (review.pr_number != null) {
    const prUrl = review.pr_url || (review.repo_owner && review.repo_name ? `https://github.com/${review.repo_owner}/${review.repo_name}/pull/${review.pr_number}` : undefined);
    return <PrPill prNumber={review.pr_number} prUrl={prUrl} repoName={repoName} />;
  }
  if (associated) {
    return <PrPill prNumber={associated.number} prUrl={associated.url} repoName={repoName} />;
  }
  const commitUrl = review.repo_owner && review.repo_name ? `https://github.com/${review.repo_owner}/${review.repo_name}/commit/${review.commit_sha}` : undefined;
  return <PushPill commitSha={review.commit_sha} commitUrl={commitUrl} repoName={repoName} />;
}

/**
 * Shared row skeleton.
 *
 * Every state of every card — collapsed failure, expanded failure, pending,
 * scored-collapsed, scored-expanded — renders through these two primitives, so
 * the repo name, PR pill, verdict chip and time stamp always sit on the same
 * baseline and the right-hand action column always ends on the same right
 * edge. The page previously had two unrelated card layouts (a one-line
 * failure strip and a three-column detail card), which is why the right edge
 * of the list was ragged row to row.
 *
 * `RowIdentity` below is the identity line; the `md:w-[164px]` column is the
 * right-hand action column.
 */
const ROW_SURFACE =
  "rounded-xl border border-zinc-800/90 bg-gradient-to-b from-[#111115] to-[#0c0c0e] shadow-[inset_0_1px_0_rgba(255,255,255,0.04),0_8px_20px_rgba(0,0,0,0.4)]";

/**
 * Identity line: repo → PR/Push pill → verdict chip → author → branch → sha →
 * time. Shared by every card state so the leading edge never shifts.
 */
function RowIdentity({
  review,
  chip,
  compact = false,
}: {
  review: CodeReviewOut;
  /** Verdict / status chip, or null when the caller renders it elsewhere. */
  chip?: React.ReactNode;
  compact?: boolean;
}) {
  const repoName = review.repo_owner && review.repo_name ? `${review.repo_owner}/${review.repo_name}` : "Repository";
  const repoUrl = review.repo_owner && review.repo_name ? `https://github.com/${review.repo_owner}/${review.repo_name}` : undefined;

  const nameClass = compact
    ? "inline-flex items-center gap-1.5 font-mono text-[13px] font-bold text-zinc-100 hover:text-white transition-colors"
    : "inline-flex items-center gap-1.5 font-mono text-sm font-bold text-zinc-100 hover:text-white transition-colors";

  return (
    <div className="flex flex-wrap items-center gap-x-2.5 gap-y-2">
      {repoUrl ? (
        <a href={repoUrl} target="_blank" rel="noreferrer" className={nameClass}>
          <FolderGit2 className="h-3.5 w-3.5 text-zinc-400" />
          <span>{repoName}</span>
        </a>
      ) : (
        <span className={nameClass}>
          <FolderGit2 className="h-3.5 w-3.5 text-zinc-400" />
          <span>{repoName}</span>
        </span>
      )}
      <IdentityPill review={review} />
      {chip}
      {review.pr_author && (
        <span className="inline-flex items-center gap-1.5 rounded-full bg-zinc-900 border border-zinc-800 px-2 py-0.5 text-[11px] text-zinc-300">
          {review.pr_author_avatar ? (
            <img src={review.pr_author_avatar} alt="" className="h-3.5 w-3.5 rounded-full object-cover" />
          ) : (
            <span className="flex h-3.5 w-3.5 items-center justify-center rounded-full bg-zinc-800 text-[9px] font-bold text-zinc-300">
              {review.pr_author[0]?.toUpperCase()}
            </span>
          )}
          <span className="font-mono">@{review.pr_author}</span>
        </span>
      )}
      <span className="text-xs font-mono text-zinc-400">{formatRelativeTime(review.created_at)}</span>
    </div>
  );
}

/**
 * Small labelled fact used in the row footer of every list card — scored,
 * pending and failed. `title` puts the explanation on the wrapper, so both the
 * label and the value are part of the hover target — that is what removes the
 * need for the hand-rolled copies of this markup that used to exist for "Risk"
 * and "Blast".
 */
function MetaFact({
  label,
  value,
  tone,
  title,
}: {
  label: string;
  value: React.ReactNode;
  tone?: string;
  title?: string;
}) {
  return (
    <span className="inline-flex items-baseline gap-1.5" title={title}>
      <span className="text-[10px] font-mono uppercase tracking-wider text-zinc-500">{label}</span>
      <span className={cn("font-mono text-xs", tone ?? "text-zinc-300")}>{value}</span>
    </span>
  );
}

/**
 * Honest error row for analyses that never completed (status "error").
 * Renders NO verdict telemetry — the neutral gauge, the raw error text with no
 * truncation, and a Retry that re-polls the list. Showing 10/10 + SAFE here
 * (the risk_score column defaults to 0) would be lying UI.
 *
 * The failure text is shown in full inside a scroll region. The previous
 * collapsed row clipped it at 140 characters with a CSS `truncate` and a
 * `title` tooltip, which was unreadable on touch and gave keyboard users no
 * path to the remainder at all.
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
  const [expanded, setExpanded] = useState(false);
  const [copiedError, setCopiedError] = useState(false);
  const [copyError, setCopyError] = useState<string | null>(null);
  const errorText = review.failure_reason ?? review.summary;
  const panelId = `failure-detail-${review.id}`;
  const commitUrl = review.repo_owner && review.repo_name
    ? `https://github.com/${review.repo_owner}/${review.repo_name}/commit/${review.commit_sha}`
    : undefined;
  const commitShort = review.commit_sha ? review.commit_sha.substring(0, 7) : "unknown";
  const identityLabel = `${review.repo_owner ?? "Repository"}/${review.repo_name ?? ""}${
    review.pr_number ? ` PR #${review.pr_number}` : ` Push @ ${commitShort}`
  }`;

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
    <span className="inline-flex items-center gap-1 rounded-full border border-rose-500/40 bg-rose-500/10 px-2 py-0.5 text-[10px] font-mono font-bold uppercase tracking-wider text-rose-200">
      <AlertTriangle className="h-2.5 w-2.5" aria-hidden="true" />
      Analysis failed
    </span>
  );

  return (
    <div className={cn(ROW_SURFACE, "border-rose-500/25")} data-testid="failed-review-card">
      <div className="flex flex-col gap-4 p-5">
        {/* One skeleton, same as a scored row: gauge | content | actions */}
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

            {/* Full text, never truncated. Scrolls instead of clipping so a
                long "Last errors: ..." tail is always reachable.

                No role="alert" here, expanded or not. The reason text is static
                content that is mounted with the collapsed row, and WAI-ARIA
                forbids a live region around content present at load. Toggling
                the role attribute onto an already-mounted node is also not a
                reliable way to announce anything: a live region speaks when its
                *content* changes or when the node is newly inserted, neither of
                which happens when this row expands. This box is deliberately
                NOT inside the Expand button's disclosure: aria-controls points
                at the "Failure details" panel below (line 1039), while this
                reason text is always visible, collapsed or not. It is static
                text a screen reader reaches by reading the row, so it needs no
                live-region announcement. */}
            <div className="rounded-lg border border-rose-500/30 bg-rose-950/20 px-3 py-2.5">
              <div className="flex items-start gap-2">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-rose-400" aria-hidden="true" />
                <p className="max-h-32 min-w-0 flex-1 overflow-y-auto font-mono text-[11px] leading-relaxed break-words text-rose-100">
                  {errorText}
                </p>
              </div>
              {copyError && <p className="mt-1.5 text-[11px] font-mono text-rose-300">{copyError}</p>}
            </div>

            <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
              <MetaFact label="Status" value={<span className="text-rose-300">Failed</span>} />
              <MetaFact
                label="Risk"
                value={<span>Not scored</span>}
                tone="text-zinc-500"
                title="No score — the analysis never produced a risk assessment"
              />
              <a
                href={commitUrl || `https://github.com/${review.repo_owner}/${review.repo_name}/commit/${review.commit_sha}`}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 font-mono text-xs text-zinc-400 hover:text-zinc-100 transition-colors"
                title="View commit on GitHub"
              >
                <GitCommit className="h-3 w-3 text-zinc-500" aria-hidden="true" />
                {commitShort}
              </a>
            </div>
          </div>

          {/* Right column: fixed width + right-aligned so every row's actions
              end on the same edge regardless of expand state. */}
          <div className="flex shrink-0 items-start gap-2 md:w-[164px] md:justify-end">
            <Button
              variant="outline"
              size="sm"
              onClick={onRetry}
              aria-label={`Retry loading reviews (failed: ${identityLabel})`}
              className="h-7 shrink-0 gap-1.5 border-rose-500/40 bg-rose-950/20 px-2.5 text-xs text-rose-200 hover:bg-rose-900/40"
            >
              <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
              Retry
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setExpanded((v) => !v)}
              aria-expanded={expanded}
              aria-controls={panelId}
              aria-label={expanded ? `Collapse error details for ${identityLabel}` : `Expand error details for ${identityLabel}`}
              className="h-7 shrink-0 gap-1.5 border-zinc-700 bg-zinc-900/80 px-2.5 text-xs text-zinc-200 hover:bg-zinc-800"
            >
              <span>{expanded ? "Collapse" : "Details"}</span>
              {expanded ? <ChevronUp className="h-3.5 w-3.5" aria-hidden="true" /> : <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />}
            </Button>
          </div>
        </div>

        {/* Progressive disclosure: identity, failure text and the commit are
            always visible; everything below is on demand. */}
        {expanded && (
          <div id={panelId} className="space-y-4 border-t border-zinc-800/80 pt-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h4 className="text-[11px] font-mono font-semibold uppercase tracking-[0.16em] text-zinc-400">
                Failure details
              </h4>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={handleCopyError}
                  className="inline-flex items-center gap-1.5 rounded-[5px] border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-[11px] text-zinc-300 transition-colors hover:bg-zinc-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400"
                >
                  {copiedError ? (
                    <>
                      <Check className="h-3 w-3 text-emerald-400" aria-hidden="true" />
                      <span className="text-emerald-300">Copied</span>
                    </>
                  ) : (
                    <>
                      <Copy className="h-3 w-3 text-zinc-400" aria-hidden="true" />
                      Copy reason
                    </>
                  )}
                </button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => onOpen(review.id)}
                  aria-label={`Full audit for ${identityLabel} (${review.id})`}
                  className="h-7 gap-1.5 border-zinc-700 bg-zinc-900/80 px-2.5 text-xs text-zinc-200 hover:bg-zinc-800"
                >
                  <span>Full audit</span>
                  <ArrowUpRight className="h-3.5 w-3.5" aria-hidden="true" />
                </Button>
              </div>
            </div>

            <p className="text-[11px] leading-relaxed text-zinc-400">
              No risk score, confidence or blocker telemetry exists for this review — the analysis
              never completed. Retry re-polls the review list; it does not re-run the model, because
              the reviews API is read-only.
            </p>

            <div className="flex flex-wrap items-center gap-2 text-[11px] font-mono text-zinc-400">
              {(review.head_branch || review.base_branch) && (
                <span className="inline-flex items-center gap-1">
                  <GitBranch className="h-3 w-3 text-zinc-500" aria-hidden="true" />
                  <span>{review.base_branch || "main"}</span>
                  <span className="text-zinc-600">←</span>
                  <span className="text-zinc-300">{review.head_branch || "head"}</span>
                </span>
              )}
              {review.pr_author && <span>opened by @{review.pr_author}</span>}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * Honest neutral row for analyses that have not finished (status "pending" /
 * "in_progress" — or anything but completed/error/suppressed, fail-closed).
 * Scored "suppressed" is terminal WITH a saved audit so it renders the full
 * completed branches, never this card; unscored suppressed rows (no token
 * telemetry + no real summary, see hasCompletedAnalysis) render this card
 * with a "Suppressed" pill.
 *
 * Renders NO verdict telemetry — the queued summary verbatim, a "not scored"
 * label instead of a fabricated 10/10. No Retry: nothing failed. Renders
 * through the same RowIdentity skeleton as every other state so an in-flight
 * row keeps the same baseline as a scored one.
 */
function PendingReviewCard({
  review,
  onOpen,
}: {
  review: CodeReviewOut;
  onOpen: (id: string) => void;
}) {
  const label = isSuppressedUnscored(review) ? "Suppressed" : pendingLabelFor(review);
  // Suppressed-unscored is terminal, so it carries its own outcome: reusing
  // pendingOutcome() would announce "Still running. A verdict appears here
  // once the analysis lands." on a row that will never receive one.
  const outcome = isSuppressedUnscored(review)
    ? SUPPRESSED_UNSCORED_OUTCOME
    : pendingOutcome(review.status);

  const statusChip = (
    <span className="inline-flex items-center gap-1 rounded-full border border-zinc-700 bg-zinc-900 px-2 py-0.5 text-[10px] font-mono font-semibold uppercase tracking-wider text-zinc-300">
      <span className="relative flex h-1.5 w-1.5" aria-hidden="true">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-zinc-400 opacity-60" />
        <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-zinc-400" />
      </span>
      {label}
    </span>
  );

  return (
    <div className={ROW_SURFACE}>
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

          <p className="text-xs leading-relaxed text-zinc-400">{review.summary}</p>

          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <MetaFact label="Status" value={<span className="text-zinc-300">{label}</span>} />
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
            aria-label={`Full audit for ${review.repo_owner ?? "Repository"}/${review.repo_name ?? ""}${
              review.pr_number ? ` PR #${review.pr_number}` : ""
            }${review.pr_title ? ` — ${review.pr_title}` : ""} (${review.id})`}
            className="h-7 shrink-0 gap-1.5 border-zinc-700 bg-zinc-900/80 px-2.5 text-xs text-zinc-200 hover:bg-zinc-800"
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

/**
 * Presentation for each triage chip. Module-scope and frozen: this is
 * invariant data, and rebuilding a 5-element array on every render of a
 * component that re-renders on every filter change is pure waste.
 *
 * Only the *visual* language lives here. The chip tooltip is NOT copied in
 * from review-triage — it comes from outcomeMeaning(), the same source the
 * verdict panel reads, so the strip and the row can never describe the same
 * outcome differently.
 */
const TRIAGE_CHIP_STYLES: Readonly<
  Record<OutcomeKey, { label: string; dot: string; active: string }>
> = Object.freeze({
  changes_requested: {
    label: "Need changes",
    dot: "bg-rose-400",
    active: "border-rose-500/60 bg-rose-950/40 text-rose-200",
  },
  advisory: {
    label: "Review advised",
    dot: "bg-amber-400",
    active: "border-amber-500/60 bg-amber-950/40 text-amber-200",
  },
  in_progress: {
    label: "In progress",
    dot: "bg-zinc-400",
    active: "border-zinc-500/60 bg-zinc-800 text-zinc-100",
  },
  failed: {
    label: "Failed",
    dot: "bg-rose-300",
    active: "border-rose-500/60 bg-rose-950/40 text-rose-100",
  },
  suppressed: {
    label: "Suppressed",
    dot: "bg-zinc-500",
    active: "border-zinc-500/60 bg-zinc-800 text-zinc-100",
  },
  clean: {
    label: "Great to merge",
    dot: "bg-emerald-400",
    active: "border-emerald-500/60 bg-emerald-950/40 text-emerald-200",
  },
});

/** Chip rows in attention order, driven by the shared OUTCOME_ORDER. */
const TRIAGE_BUCKETS: readonly {
  key: OutcomeKey;
  label: string;
  dot: string;
  active: string;
  meaning: string;
}[] = Object.freeze(
  OUTCOME_ORDER.map((key) => ({
    key,
    ...TRIAGE_CHIP_STYLES[key],
    meaning: outcomeMeaning(key),
  }))
);

/**
 * Triage Summary — the one-glance answer to "what needs my attention?".
 *
 * Five outcome counters, ordered by how much they demand a human decision.
 * Each is a real toggle filter with `aria-pressed`, so the strip is both
 * orientation and control. Counts are honest: a review contributes to exactly
 * one bucket, and failed / in-progress rows never inflate the scored numbers.
 *
 * This sits directly under the header and directly above the list, so the
 * eye lands on "3 need changes" before it lands on nine identical failure
 * rows. The dot + label pairing means every counter is readable without
 * colour.
 */
function TriageSummary({
  counts,
  activeOutcome,
  onToggle,
}: {
  counts: Record<OutcomeKey, number>;
  activeOutcome: OutcomeKey | null;
  onToggle: (key: OutcomeKey | null) => void;
}) {
  const total = Object.values(counts).reduce((sum, n) => sum + n, 0);
  const actionable = counts.changes_requested + counts.advisory;

  return (
    <section
      aria-label="Review triage summary for this page"
      className="rounded-xl border border-zinc-800/80 bg-gradient-to-b from-[#111115] to-[#0c0c0e] p-3 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]"
    >
      <div className="flex flex-wrap items-center gap-3">
        {/* Lead sentence — the triage verdict in words. */}
        <p className="mr-1 text-xs text-zinc-400">
          {actionable > 0 ? (
            <>
              <span className="font-semibold text-zinc-100">{actionable}</span>{" "}
              {actionable === 1 ? "review needs" : "reviews need"} a decision before merge
              {counts.failed > 0 && (
                <>
                  {" · "}
                  <span className="font-semibold text-rose-300">{counts.failed}</span> failed to analyse
                </>
              )}
            </>
          ) : counts.in_progress > 0 ? (
            <>
              <span className="font-semibold text-zinc-100">{counts.in_progress}</span>{" "}
              {counts.in_progress === 1 ? "analysis is" : "analyses are"} still running
            </>
          ) : (
            <>Nothing needs a decision right now</>
          )}
        </p>

        {/* Counters act as filters, and every other number on this page is
            scoped ("this page", "of N reviews"), so the strip says so too.
            "In view", not "This page": these counts are computed over
            `filteredReviews`, which is the loaded window minus the search box,
            the Patches tab and the hide-failed toggle — strictly narrower than
            the page whenever any of those are active. */}
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="font-mono text-[10px] uppercase tracking-wider text-zinc-500">
            In view
          </span>
          <button
            type="button"
            onClick={() => onToggle(null)}
            aria-pressed={activeOutcome === null}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 font-mono text-[10px] uppercase tracking-wider transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400",
              activeOutcome === null
                ? "border-zinc-500/60 bg-zinc-800 font-semibold text-zinc-100"
                : "border-zinc-800 bg-zinc-950 text-zinc-400 hover:border-zinc-700 hover:text-zinc-200"
            )}
          >
            All
            <span className="text-zinc-500">{total}</span>
          </button>
          {TRIAGE_BUCKETS.map((bucket) => {
            const count = counts[bucket.key];
            const isActive = activeOutcome === bucket.key;
            return (
              <button
                key={bucket.key}
                type="button"
                onClick={() => onToggle(isActive ? null : bucket.key)}
                aria-pressed={isActive}
                title={bucket.meaning}
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 font-mono text-[10px] uppercase tracking-wider transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400",
                  isActive ? cn("font-semibold", bucket.active) : "border-zinc-800 bg-zinc-950 text-zinc-400 hover:border-zinc-700 hover:text-zinc-200"
                )}
              >
                <span className={cn("h-1.5 w-1.5 shrink-0 rounded-full", bucket.dot)} aria-hidden="true" />
                <span>{bucket.label}</span>
                <span className={isActive ? "opacity-80" : "text-zinc-500"}>{count}</span>
              </button>
            );
          })}
        </div>
      </div>
    </section>
  );
}

/**
 * Failure cluster banner.
 *
 * Rendered once per distinct root cause instead of repeating the same sentence
 * on every failed row. This is the difference between "the free tier is
 * exhausted, nine times" as a fact the user can act on, and nine cards of
 * noise that bury the reviews they actually need to read.
 *
 * Only rendered when there are two or more failures. With a single failure the
 * row itself already states the cause in full, so a banner would just print
 * the same sentence twice.
 *
 * Typed with the clusterFailures() return type rather than a hand-copied
 * structural shape, so a change to the cluster contract fails here instead of
 * silently drifting.
 */
function FailureClusterBanner({ clusters }: { clusters: readonly FailureCluster[] }) {
  const total = clusters.reduce((sum, c) => sum + c.count, 0);
  if (clusters.length === 0 || total < 2) return null;
  return (
    <div
      role="status"
      className="rounded-xl border border-rose-500/30 bg-rose-950/20 px-4 py-3 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]"
    >
      <div className="flex items-start gap-2.5">
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-rose-400" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <h3 className="text-xs font-semibold text-rose-100">
            {/* Pluralised on `total` (the failure count), not on the number of
                clusters: nine rows sharing one cause is "9 failed analyses",
                never "9 failed analysis". The singular is unreachable — the
                guard above returns null below two failures — so it is not
                spelled out here. */}
            {total} failed analyses
            {clusters.length > 1 && ` across ${clusters.length} causes`}
          </h3>
          <ul className="mt-1.5 space-y-1">
            {clusters.map((cluster) => (
              <li key={cluster.signature} className="flex items-baseline gap-2 text-[11px]">
                <span className="shrink-0 rounded border border-rose-500/30 bg-rose-950/40 px-1.5 py-0.5 font-mono text-[10px] font-bold tabular-nums text-rose-300">
                  {cluster.count}
                </span>
                <span className="min-w-0 flex-1 font-mono text-rose-100/90">{cluster.signature}</span>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}

/**
 * Main Review Card (overview row).
 *
 * Hierarchy, in the order the eye should move:
 *   1. Verdict chip — the merge decision, on the identity line.
 *   2. Must-fix blocker count — the only number that means "act on this".
 *   3. Title + summary.
 *   4. Confidence / diff / blast radius / tokens — one quiet line.
 *
 * The previous layout gave `risk 45/100`, `6/10` confidence, blocker counts
 * and blast radius four equal-weight bordered cells, so nothing said where to
 * look first; and it printed the finding count three times (blocker cell,
 * footer meta, right-hand telemetry). Collapsed and expanded rows now share
 * one structure, so the right edge no longer shifts when a row expands.
 */
function ReviewCard({ review, onOpen, onRetry }: { review: CodeReviewOut; onOpen: (id: string) => void; onRetry: () => void }) {
  const [expanded, setExpanded] = useState(false);
  const [copiedSha, setCopiedSha] = useState(false);
  const [copyShaError, setCopyShaError] = useState<string | null>(null);
  const findingsPanelId = `findings-${review.id}`;
  const repoName = review.repo_owner && review.repo_name ? `${review.repo_owner}/${review.repo_name}` : "Repository";
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

  // Must run before the early returns below: React requires hooks in a stable
  // order across renders, and a failed/pending row would otherwise skip it.
  const isPrAssociated = useIsPrAssociated(review);

  if (isAnalysisFailed(review)) {
    return <FailedReviewCard review={review} onOpen={onOpen} onRetry={onRetry} />;
  }

  // Non-terminal rows — plus unscored suppressed rows with no completion
  // signal (see hasCompletedAnalysis) —
  // never reach verdict telemetry: neutral pending card.
  if (isReviewPending(review) || isSuppressedUnscored(review)) {
    return <PendingReviewCard review={review} onOpen={onOpen} />;
  }

  // Blast Radius Telemetry (shared helper — overview strip + detail report agree)
  const blast = getBlastStats(review);
  const filesCount = blast.filesCount;
  const additions = blast.additions;
  const deletions = blast.deletions;
  const blockers = blockerCounts(review.findings);
  const verdict = verdictFor(review);
  const radius = blastRadiusSummary(review);
  const suppressed = isSuppressed(review);
  const findingCount = review.findings.length;

  const verdictChip = (
    <span
      className={cn(
        "inline-flex items-center whitespace-nowrap rounded-full border px-2 py-0.5 text-[10px] font-mono font-semibold shadow-[inset_0_1px_0_rgba(255,255,255,0.08)]",
        verdict.band.chip
      )}
    >
      <span className={cn("mr-1.5 h-1.5 w-1.5 shrink-0 rounded-full", verdict.band.dot)} aria-hidden="true" />
      {verdict.label}
    </span>
  );

  return (
    <div className={cn(ROW_SURFACE, "transition-colors hover:border-zinc-700/80")}>
      <div className="flex flex-col gap-5 p-5 md:flex-row md:items-start">
        {/* Gauge — same footprint in every state, so the identity line always
            starts at the same horizontal offset. */}
        <div className="flex items-center gap-4 md:min-w-[84px] md:flex-col md:justify-center">
          <ConfidenceGauge riskScore={review.risk_score} size={64} />
        </div>

        {/* Center: report identity, summary, decision line */}
        <div className="min-w-0 flex-1 space-y-3">
          {/* RowIdentity renders repo -> PR/Push pill -> verdict chip ->
              author -> time. Shared with the failure and pending rows so the
              leading edge of every card is identical. */}
          <RowIdentity review={review} chip={verdictChip} />

          {/* Secondary identity facts, one step down from the repo line. */}
          <div className="flex flex-wrap items-center gap-2 text-[11px] font-mono">
            {suppressed && (
              <span
                className="inline-flex items-center rounded-full border border-zinc-700 bg-zinc-900 px-2 py-0.5 text-[10px] text-zinc-400"
                title="Analysis finished - publishing to GitHub was skipped by repo settings"
              >
                Publication skipped
              </span>
            )}
            {isPublishFailed(review) && (
              <span
                className="inline-flex items-center rounded-full border border-rose-500/30 bg-rose-950/40 px-2 py-0.5 text-[10px] text-rose-300"
                title={
                  review.failure_reason
                    ? `Analysis finished and this audit is saved below, but publishing it to GitHub failed: ${review.failure_reason}`
                    : "Analysis finished and this audit is saved below, but publishing it to GitHub failed."
                }
              >
                Publication failed
              </span>
            )}
            {(review.head_branch || review.base_branch) && (
              <span className="inline-flex items-center gap-1 rounded border border-zinc-800 bg-zinc-900/60 px-1.5 py-0.5">
                <GitBranch className="h-3 w-3 text-zinc-500" aria-hidden="true" />
                <span className="text-zinc-400">{review.base_branch || "main"}</span>
                <span className="text-zinc-600" aria-hidden="true">&#8592;</span>
                <span className="text-amber-400">{review.head_branch || "head"}</span>
              </span>
            )}
            {isPrAssociated ? (
              <span className="inline-flex items-center rounded border border-zinc-800 bg-zinc-900/60">
                <a
                  href={commitUrl || `https://github.com/${repoName}/commit/${review.commit_sha}`}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 border-r border-zinc-800 py-0.5 pl-1.5 pr-1.5 text-zinc-400 transition-colors hover:text-zinc-100"
                  title="View commit on GitHub"
                >
                  <GitCommit className="h-3 w-3 text-zinc-500" aria-hidden="true" />
                  {commitShort}
                </a>
                <button
                  type="button"
                  onClick={handleCopySha}
                  className="rounded-sm py-0.5 pl-1.5 pr-1 text-zinc-500 transition-colors hover:text-zinc-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400"
                  title="Copy commit SHA"
                  aria-label="Copy commit SHA"
                >
                  {copiedSha ? (
                    <Check className="h-3 w-3 text-emerald-400" aria-hidden="true" />
                  ) : (
                    <Copy className="h-3 w-3" aria-hidden="true" />
                  )}
                </button>
              </span>
            ) : (
              /* Push rows carry the sha inside the Push pill; give them the
                 same one-click copy affordance PR rows get. */
              <button
                type="button"
                onClick={handleCopySha}
                className="inline-flex items-center gap-1 rounded border border-zinc-800 bg-zinc-900/60 py-0.5 pl-1.5 pr-1.5 text-zinc-500 transition-colors hover:text-zinc-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400"
                title="Copy commit SHA"
                aria-label="Copy commit SHA"
              >
                {copiedSha ? (
                  <Check className="h-3 w-3 text-emerald-400" aria-hidden="true" />
                ) : (
                  <Copy className="h-3 w-3" aria-hidden="true" />
                )}
                Copy SHA
              </button>
            )}
          </div>

          {copyShaError && (
            <p role="alert" className="font-mono text-[11px] text-rose-400">{copyShaError}</p>
          )}

          {/* Hero PR Title */}
          {review.pr_title && (
            <h3 className="text-[15px] font-bold leading-snug tracking-tight text-zinc-50">
              {review.pr_title}
            </h3>
          )}

          <p className="text-xs leading-relaxed text-zinc-300">{review.summary}</p>

          {/* Tier 1 - the decision. The verdict is restated as a word plus
              the blocker split, which is the only number on this row that
              means "act on this". The finding total is deliberately NOT
              repeated here; it appears once, in the findings toggle. */}
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 border-l-2 border-zinc-800 pl-3">
            <span className={cn("inline-flex items-center gap-1.5 text-sm font-semibold", verdict.band.text)}>
              <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
              {verdict.label}
            </span>
            <span className="inline-flex items-center gap-1.5 text-xs text-zinc-400">
              Must-Fix
              <span className={cn("font-mono text-sm font-bold tabular-nums", blockers.mustFix > 0 ? "text-rose-400" : "text-emerald-400")}>
                {blockers.mustFix}
              </span>
              <span className="text-zinc-700" aria-hidden="true">&#183;</span>
              Should-Fix
              <span className={cn("font-mono text-sm font-bold tabular-nums", blockers.shouldFix > 0 ? "text-amber-400" : "text-emerald-400")}>
                {blockers.shouldFix}
              </span>
            </span>
          </div>

          {/* Tier 2 - supporting telemetry on one quiet line, no boxes.
              Confidence lives in the ring, so this line carries the raw
              0-100 risk score the ring no longer shows. */}
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 font-mono text-[11px]">
            <MetaFact
              label="Risk"
              value={`${review.risk_score}/100`}
              tone={cn("tabular-nums", verdict.band.text)}
            />
            <MetaFact
              label="Diff"
              value={
                <span>
                  {filesCount} {filesCount === 1 ? "file" : "files"}{" "}
                  <span className="text-emerald-400">+{additions}</span>
                  <span className="text-rose-400">-{deletions}</span>
                </span>
              }
            />
            <MetaFact
              label="Blast"
              value={radius.label}
              tone="text-[10px] font-semibold uppercase tracking-wider text-zinc-400"
              title={radius.explanation}
            />
            <span className="text-zinc-600">{review.input_tokens + review.output_tokens} tok</span>
          </div>

          {/* Tier 3 - finding categories. Last thing read, never competing
              with the decision line above. */}
          <div className="flex flex-wrap items-center gap-1.5 pt-0.5">
            {categoryCounts.security > 0 && (
              <span className="inline-flex items-center gap-1 rounded-full border border-rose-500/30 bg-rose-950/40 px-2 py-0.5 text-[10px] font-mono text-rose-300">
                <Lock className="h-2.5 w-2.5 text-rose-400" aria-hidden="true" />
                {categoryCounts.security} Security
              </span>
            )}
            {categoryCounts.logic > 0 && (
              <span className="inline-flex items-center gap-1 rounded-full border border-amber-500/30 bg-amber-950/40 px-2 py-0.5 text-[10px] font-mono text-amber-300">
                <Bug className="h-2.5 w-2.5 text-amber-400" aria-hidden="true" />
                {categoryCounts.logic} Logic
              </span>
            )}
            {categoryCounts.performance > 0 && (
              <span className="inline-flex items-center gap-1 rounded-full border border-cyan-500/30 bg-cyan-950/40 px-2 py-0.5 text-[10px] font-mono text-cyan-300">
                <Zap className="h-2.5 w-2.5 text-cyan-400" aria-hidden="true" />
                {categoryCounts.performance} Perf
              </span>
            )}
            {categoryCounts.api_compatibility > 0 && (
              <span className="inline-flex items-center gap-1 rounded-full border border-zinc-600/50 bg-zinc-900/60 px-2 py-0.5 text-[10px] font-mono text-zinc-300">
                <Boxes className="h-2.5 w-2.5 text-zinc-400" aria-hidden="true" />
                {categoryCounts.api_compatibility} API
              </span>
            )}
            {findingCount === 0 && (
              <span className="inline-flex items-center gap-1 rounded-full border border-emerald-500/30 bg-emerald-950/30 px-2 py-0.5 text-[10px] font-mono text-emerald-300">
                <Check className="h-2.5 w-2.5 text-emerald-400" aria-hidden="true" />
                No regressions detected
              </span>
            )}
          </div>
        </div>

        {/* Right column - fixed width so every row's actions end on the same
            edge whether or not the findings panel is open. */}
        <div className="flex shrink-0 flex-col items-stretch gap-2 md:w-[164px] md:items-end">
          <Button
            variant="outline"
            size="sm"
            onClick={() => onOpen(review.id)}
            aria-label={`Full audit for ${repoName}${review.pr_number ? ` PR #${review.pr_number}` : ""}${review.pr_title ? ` - ${review.pr_title}` : ""} (${review.id})`}
            className="h-7 shrink-0 gap-1.5 border-zinc-700 bg-zinc-900/80 px-2.5 text-xs text-zinc-200 hover:bg-zinc-800"
          >
            <span>Full audit</span>
            <ArrowUpRight className="h-3.5 w-3.5" aria-hidden="true" />
          </Button>
          {findingCount > 0 && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => setExpanded(!expanded)}
              aria-expanded={expanded}
              aria-controls={findingsPanelId}
              className="h-7 shrink-0 gap-1.5 border-zinc-700 bg-zinc-900/80 px-2.5 text-xs text-zinc-200 hover:bg-zinc-800"
            >
              <span>{expanded ? "Hide findings" : `Review (${findingCount})`}</span>
              {expanded ? <ChevronUp className="h-3.5 w-3.5" aria-hidden="true" /> : <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />}
            </Button>
          )}
        </div>
      </div>

      {/* Accordion Findings. `id` matches the Review button's aria-controls so the
          control points at a real element in the document — without it the
          reference dangles and assistive tech has nothing to move focus to. */}
      {expanded && review.findings.length > 0 && (
        <div id={findingsPanelId} className="mt-5 pt-4 border-t border-zinc-800/80 space-y-3">
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
  const failed = isAnalysisFailed(review);
  const publishFailed = isPublishFailed(review);
  const pending = !failed && !publishFailed && (isReviewPending(review) || isSuppressedUnscored(review));
  const suppressed = !failed && !publishFailed && !pending && isSuppressed(review);
  const pendingLabel = isSuppressedUnscored(review) ? "Suppressed" : pendingLabelFor(review);
  const errorText = review.failure_reason ?? review.summary;
  const blast = getBlastStats(review);
  const confidence = confidenceFromRisk(review.risk_score);
  const blockers = blockerCounts(review.findings);
  const verdict = verdictFor(review);
  const radius = blastRadiusSummary(review);
  const touchedFiles = useMemo(
    () => [...new Set(review.findings.map((f) => f.file_path))],
    [review.findings]
  );

  const repoName = review.repo_owner && review.repo_name ? `${review.repo_owner}/${review.repo_name}` : "Repository";
  const repoUrl = review.repo_owner && review.repo_name ? `https://github.com/${review.repo_owner}/${review.repo_name}` : undefined;
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
  const isPrAssociated = useIsPrAssociated(review);

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
              <IdentityPill review={review} />
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
              {publishFailed && (
                <span
                  className="inline-flex items-center rounded-full border border-rose-500/40 bg-rose-950/60 px-2 py-0.5 text-[10px] font-mono font-bold uppercase tracking-wider text-rose-300"
                  title={
                    review.failure_reason
                      ? `Analysis finished and this audit is saved below, but publishing it to GitHub failed: ${review.failure_reason}`
                      : "Analysis finished and this audit is saved below, but publishing it to GitHub failed."
                  }
                >
                  Publication failed
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
              {/* Commit SHA chip — shown for PR-associated rows (direct or resolved),
                  mirroring the overview card. Push rows carry the sha in the
                  Push pill with their own copy affordance alongside. */}
              {isPrAssociated ? (
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
            <ConfidenceGauge riskScore={review.risk_score} size={84} neutral={failed || pending} />
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
      {/* PR Summary — the verdict rationale, so it reads as prose at the size a
          human actually scans, not as a small mono fragment. */}
      <section className="rounded-xl border border-zinc-800/80 bg-[#0f0f12] p-5">
        <h3 className="text-[10px] font-mono font-semibold uppercase tracking-[0.16em] text-zinc-500">PR Summary</h3>
        <p className="mt-2 max-w-[68ch] text-sm leading-relaxed text-zinc-200">{review.summary}</p>
      </section>

      {/* Decision panel. The verdict leads because it is the only thing that
          requires a human decision; confidence lives in the ring, so this
          states the raw risk score instead of repeating confidence/10. */}
      <section className="overflow-hidden rounded-xl border border-zinc-800/80 bg-[#0f0f12]">
        <div className="flex flex-col gap-4 border-b border-zinc-800/80 p-5 sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0 space-y-1.5">
            <div className="text-[10px] font-mono font-semibold uppercase tracking-[0.16em] text-zinc-500">
              Merge recommendation
            </div>
            <div className={cn("flex flex-wrap items-center gap-2 text-lg font-bold tracking-tight", verdict.band.text)}>
              <ShieldCheck className="h-5 w-5 shrink-0" aria-hidden="true" />
              {verdict.label}
            </div>
            <p className="text-xs leading-relaxed text-zinc-400">{verdict.meaning}</p>
          </div>
          <dl className="grid shrink-0 grid-cols-3 gap-px overflow-hidden rounded-lg border border-zinc-800/80 bg-zinc-800/80 font-mono sm:w-auto">
            <div className="bg-zinc-950/90 px-3.5 py-2.5">
              <dt className="text-[10px] uppercase tracking-wider text-zinc-500">Risk</dt>
              <dd className={cn("text-base font-bold tabular-nums", verdict.band.text)}>
                {review.risk_score}
                <span className="text-[10px] text-zinc-500">/100</span>
              </dd>
            </div>
            <div className="bg-zinc-950/90 px-3.5 py-2.5">
              <dt className="text-[10px] uppercase tracking-wider text-zinc-500">Must-Fix</dt>
              <dd className={cn("text-base font-bold tabular-nums", blockers.mustFix > 0 ? "text-rose-400" : "text-emerald-400")}>
                {blockers.mustFix}
              </dd>
            </div>
            <div className="bg-zinc-950/90 px-3.5 py-2.5">
              <dt className="text-[10px] uppercase tracking-wider text-zinc-500">Should-Fix</dt>
              <dd className={cn("text-base font-bold tabular-nums", blockers.shouldFix > 0 ? "text-amber-400" : "text-emerald-400")}>
                {blockers.shouldFix}
              </dd>
            </div>
          </dl>
        </div>

        {/* Supporting facts, quieter than the decision above. */}
        <dl className="grid grid-cols-1 gap-px bg-zinc-800/80 sm:grid-cols-2 lg:grid-cols-4">
          <div className="bg-zinc-950/90 px-4 py-3 space-y-0.5">
            <dt className="text-[10px] uppercase tracking-wider text-zinc-500 font-semibold">Confidence</dt>
            <dd className="text-sm font-bold text-zinc-100">
              {confidence}/10
              <span className="ml-1.5 text-[10px] font-normal text-zinc-500">in the ring</span>
            </dd>
          </div>
          <div className="bg-zinc-950/90 px-4 py-3 space-y-0.5">
            <dt className="text-[10px] uppercase tracking-wider text-zinc-500 font-semibold">Diff</dt>
            <dd className="font-mono text-sm text-zinc-100">
              {blast.filesCount} {blast.filesCount === 1 ? "file" : "files"}{" "}
              <span className="text-emerald-400">+{blast.additions}</span>
              <span className="text-rose-400">-{blast.deletions}</span>
            </dd>
          </div>
          <div className="bg-zinc-950/90 px-4 py-3 space-y-0.5">
            <dt className="text-[10px] uppercase tracking-wider text-zinc-500 font-semibold">Blast Radius</dt>
            <dd className="font-mono text-sm font-bold text-zinc-100">{radius.label}</dd>
            <dd className="text-[10px] text-zinc-500 font-sans">{radius.explanation}</dd>
          </div>
          <div className="bg-zinc-950/90 px-4 py-3 space-y-0.5">
            <dt className="text-[10px] uppercase tracking-wider text-zinc-500 font-semibold">Analysis cost</dt>
            <dd className="font-mono text-sm text-zinc-100">{review.input_tokens + review.output_tokens} tok</dd>
            <dd className="text-[10px] text-zinc-500 uppercase">{review.status}</dd>
          </div>
        </dl>
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
              : // The three branches below must mirror scoredOutcome()'s
                // thresholds exactly (risk-triage review-triage.ts:188-196).
                // It returned "merge after a focused review pass" for every
                // risk >= 31 with no must-fix findings, while scoredOutcome()
                // had already returned changes_requested from risk >= 71 — so a
                // critical review displayed "Changes Requested" in the verdict
                // panel and "merge after a focused review pass" one screen
                // below it, on the same page, at the same moment.
                review.risk_score >= RISK_CRITICAL
                  ? `Risk ${review.risk_score}/100 is at or above the critical bar and no must-fix finding is named — the score alone requests changes; review before merging.`
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
  const [sortKey, setSortKey] = useState<SortKey>("attention");
  const [activeOutcome, setActiveOutcome] = useState<OutcomeKey | null>(null);
  const [activeTab, setActiveTab] = useState<"all" | "high_risk" | "avg_risk" | "patches">("all");
  const [searchQuery, setSearchQuery] = useState<string>("");
  const [loading, setLoading] = useState<boolean>(true);
  const [refreshing, setRefreshing] = useState<boolean>(false);
  const [page, setPage] = useState<number>(0);
  const [totalCount, setTotalCount] = useState<number>(0);
  const [fetchError, setFetchError] = useState<string | null>(null);

  // Detail view: selected review id, deep-linkable via `?review=<id>`.
  //
  // Starts null on BOTH the server and the client's first render, then adopts
  // the URL's id in a mount effect. Seeding it with `useState(() =>
  // readReviewParam())` made the two trees disagree: readReviewParam() returns
  // null when `window` is undefined, so a cold-loaded `?review=<id>` rendered
  // the overview on the server and the detail on the client — React discards
  // the mismatched tree and re-renders, and the deep link visibly flickers.
  // Reading after mount keeps the first client tree identical to the server's.
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detailReview, setDetailReview] = useState<CodeReviewOut | null>(null);
  const [detailLoading, setDetailLoading] = useState<boolean>(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailRetry, setDetailRetry] = useState<number>(0);
  const [listLoaded, setListLoaded] = useState<boolean>(false);

  // Single source for "the user is filtering", used by both the empty-state
  // copy and the Clear Filters button. Any one control being active is enough;
  // it deliberately does not look at `reviews.length`, because repo/risk/
  // severity are applied server-side and a no-match filter legitimately
  // returns zero rows.
  const filtersActive =
    selectedRepoId !== "all" ||
    highRiskOnly ||
    hideFailed ||
    activeTab !== "all" ||
    selectedSeverity !== "all" ||
    searchQuery !== "" ||
    activeOutcome !== null;

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

  // Clamp the page index when the server-side result set shrinks beneath it.
  // The server filters by repo / high-risk / severity, and a Refresh can also
  // come back with fewer rows after rows disappear upstream, so the user can
  // be parked on a page that no longer exists — the footer would claim
  // "Showing 46 to 60 of 10" and Next would dead-end.
  //
  // This clamps AFTER the response rather than before the request, and that is
  // deliberate: the shrink is only knowable from the response that discovers
  // it, so no pre-flight check could have skipped that call. Hoisting the clamp
  // into fetchReviews() would require `totalCount` in its useCallback deps,
  // which re-creates the callback on every total change and makes the fetch
  // effect above fire a second, redundant request on mount — trading one
  // unavoidable round trip for a permanent extra one.
  const maxPage = Math.max(0, Math.ceil(totalCount / PAGE_SIZE) - 1);
  useEffect(() => {
    if (page > maxPage) setPage(maxPage);
  }, [page, maxPage]);

  // Adopt the deep-linked id AFTER the first client render (see selectedId).
  // Guarded so it can only run once: without the guard this effect depends on
  // nothing and would not re-fire, but keeping the intent explicit prevents a
  // later dep addition from silently turning it into a re-assert loop.
  useEffect(() => {
    setSelectedId(readReviewParam());
  }, []);

  const openReview = useCallback((id: string) => {
    window.history.pushState({ review: id }, "", `${window.location.pathname}?review=${encodeURIComponent(id)}`);
    setDetailError(null);
    setSelectedId(id);
  }, []);

  // Back/forward buttons stay in sync with the deep link.
  useEffect(() => {
    const onPopState = () => setSelectedId(readReviewParam());
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  const closeReview = useCallback(() => {
    // replaceState, NOT pushState.
    //
    // openReview pushed an entry carrying `?review=<id>`; pushing a second one
    // on close leaves the review entry still in the stack behind us, so the
    // next Back navigates INTO it and `onPopState` reads the id straight back
    // out of the URL — reopening the review the user just closed. Replacing
    // the current entry rewrites it to the overview URL, so Back steps past
    // the detail to whatever preceded it.
    //
    // `history.back()` would also solve it and additionally preserve a
    // same-origin referrer, but it is wrong here: when the page was COLD
    // loaded at `?review=<id>` (shared link, refresh) openReview never pushed
    // anything, so back() would navigate off the site entirely — from a
    // "Back to reviews" control that reads as going to the review list.
    window.history.replaceState({}, "", window.location.pathname);
    setDetailError(null);
    setSelectedId(null);
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
  //
  // `detailFetchKeyRef` guards the endpoint call. The effect's deps include
  // `reviews`, so when a Retry's list request lands it re-runs this effect; for
  // a review outside the current page that meant discarding the in-flight
  // detail result and immediately issuing a second identical GET, so one click
  // fetched the same detail twice. Keyed on (id, retry) — the two things that
  // legitimately demand a refetch — the re-run is a no-op.
  const detailFetchKeyRef = useRef<string | null>(null);
  useEffect(() => {
    if (!selectedId) {
      setDetailReview(null);
      setDetailError(null);
      setDetailLoading(false);
      detailFetchKeyRef.current = null;
      return;
    }
    const found = reviews.find((r) => r.id === selectedId);
    if (found) {
      setDetailReview(found);
      setDetailError(null);
      setDetailLoading(false);
      detailFetchKeyRef.current = null;
      return;
    }
    if (!listLoaded) return;
    const fetchKey = `${selectedId}::${detailRetry}`;
    if (detailFetchKeyRef.current === fetchKey) return;
    detailFetchKeyRef.current = fetchKey;
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
    // Evict only failed (null) entries so misses genuinely retry when the
    // fresh rows arrive (the hook re-runs on the new row objects), while
    // resolved PRs are kept and never refetch.
    clearFailedAssociatedPrCache();
    setRefreshing(true);
    fetchReviews();
  };

  // Detail Retry honestly re-polls the GET-only list (there is no analysis
  // re-run endpoint) AND bumps detailRetry so off-page deep links refetch
  // the detail endpoint too. Bumping detailRetry alone is a no-op for cached
  // rows because the resolver above returns early on cache hit.
  const handleDetailRetry = useCallback(() => {
    clearFailedAssociatedPrCache();
    setRefreshing(true);
    fetchReviews();
    setDetailRetry((n) => n + 1);
  }, [fetchReviews]);

  // Client-side text search & ribbon tab filtering
  const filteredReviews = useMemo(() => {
    let list = reviews;
    // Explicit opt-in only — default OFF so failed rows are never hidden silently.
    // Publish failures stay hidden under this control too: the row is still an
    // error row the user asked to see less of, even though its saved audit is
    // rendered when the control is off.
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

  /**
   * Outcome for a row, using the same honesty gates as the cards: a failed
   * analysis is a failure, an unfinished one is in-progress, and only a
   * genuinely scored row gets a verdict. Each row lands in exactly one
   * bucket, so the triage counters always sum to the row count.
   */
  const outcomeOf = useCallback(
    (review: CodeReviewOut): Outcome => {
      if (isAnalysisFailed(review)) return FAILED_OUTCOME;
      // Suppressed-but-unscored is TERMINAL: nothing is running and no verdict
      // will ever arrive. Reporting it through pendingOutcome() counted a
      // finished run as work in flight and rendered the chip tooltip "Still
      // running" on a row that is never going to produce one.
      if (isSuppressedUnscored(review)) return SUPPRESSED_UNSCORED_OUTCOME;
      if (isReviewPending(review)) return pendingOutcome(review.status);
      return scoredOutcome({ risk_score: review.risk_score, findings: review.findings });
    },
    []
  );

  // Counts drive the triage summary, so they are computed over the same
  // population the outcome chips filter within: `filteredReviews`. Deriving
  // them from the raw `reviews` window let a chip advertise "Need changes 3"
  // while the search box or the failed-rows toggle had already excluded every
  // one of those three — clicking it then showed an empty list. The chips are
  // navigation, so their numbers must match what they reveal.
  const triageCounts = useMemo(() => {
    const counts: Record<OutcomeKey, number> = {
      changes_requested: 0,
      advisory: 0,
      clean: 0,
      in_progress: 0,
      failed: 0,
      suppressed: 0,
    };
    for (const review of filteredReviews) {
      counts[outcomeOf(review).key] += 1;
    }
    return counts;
  }, [filteredReviews, outcomeOf]);

  // Triage filter + sort. Applied after the existing repo/severity/search
  // filters so the new controls compose rather than replace them.
  const sortedReviews = useMemo(() => {
    const filtered = activeOutcome
      ? filteredReviews.filter((r) => outcomeOf(r).key === activeOutcome)
      : filteredReviews;
    return sortByAttention(filtered, outcomeOf, sortKey);
  }, [filteredReviews, activeOutcome, outcomeOf, sortKey]);

  // Distinct root causes behind the failures on this page — stated once in
  // the cluster banner instead of repeated on every failed row.
  //
  // Derived from `sortedReviews`, not from the raw window, so the banner can
  // never announce "3 failed analyses" while zero failed rows are on screen
  // (which is exactly what happens with a non-failure outcome chip active).
  //
  // Analysis failures only: the banner reads "N failed analyses", and a
  // publish failure is the opposite — the analysis completed and its audit is
  // rendered below the row. Counting it here would label a saved audit as a
  // failed analysis.
  const failureClusters = useMemo(
    () => clusterFailures(sortedReviews.filter((r) => isAnalysisFailed(r))),
    [sortedReviews]
  );

  // Metric rollups — computed from the current page only (`reviews` holds a
  // single PAGE_SIZE window). Reviews Scanned uses the server-side
  // `totalCount` instead, so every tile derived here is explicitly labeled
  // "this page" in the ribbon below. Avg Risk scores finished analyses only:
  // failed rows default risk_score to 0 (averaging it in would lie, e.g.
  // 85+0→43), and pending / in_progress rows are still at the 0
  // default because analysis never ran — only "completed" and scored
  // "suppressed" carry a real score ("suppressed" is terminal WITH a saved audit:
  // backend/app/services/review_orchestrator.py:1233-1239 + :1287; suppressed
  // rows WITHOUT a completion signal (see hasCompletedAnalysis) stay out
  // like pending — a clean 0-risk/empty audit still scores because its
  // summary + token telemetry prove analysis ran).
  // "error" rows count ONLY when the analysis actually ran: a publish
  // failure carries the same real score as the completed audit it saved, so
  // excluding it would drop a scored audit out of "Scored this page" and out
  // of Avg Risk.
  // Reports how many rows actually scored.
  const pageStats = useMemo(() => {
    const total = reviews.length;
    const highRisk = reviews.filter((r) => r.risk_score >= RISK_CRITICAL).length;
    const scored = reviews.filter((r) => {
      const s = r.status?.toLowerCase();
      if (s === "completed") return true;
      if (s === "suppressed") return !isSuppressedUnscored(r);
      if (s === "error") return isPublishFailed(r);
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

            {/* A Retry on a CACHED row re-polls the list; the detail itself
                only changes when that request returns. If it fails, the
                overview banner is not on screen — the user is looking at the
                detail — so the failure was invisible while the detail kept
                showing the stale row. Surfaced here for the same reason. */}
            {fetchError && (
              <div className="flex items-center justify-between gap-3 rounded-xl border border-rose-500/40 bg-rose-950/30 px-4 py-2.5 text-xs font-mono text-rose-300">
                <span role="alert">Refresh failed: {fetchError}</span>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleDetailRetry}
                  className="h-7 border-rose-500/40 text-xs text-rose-200 hover:bg-rose-900/40"
                >
                  Retry
                </Button>
              </div>
            )}

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
              /* Keyed by review id: ReviewDetail holds local state (copied, copiedSha,
                copyShaError, and FindingItem's per-row staged/copied flags).
                React reconciles by position, so opening review B after review
                A REUSED A's component instance and carried A's staged/copied
                badges onto B's findings until the user clicked each one. A
                remount on id change gives every review its own state. */
              <ReviewDetail key={detailReview.id} review={detailReview} onRetry={handleDetailRetry} />
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
                opt-in: failed rows are never hidden silently.

                Stays visible once switched on, even on a page that happens to
                hold no failed rows. `failedCount > 0` alone removes the only
                control that can undo the filter, trapping the user on a page
                that will never show a failed row again. */}
            {(failedCount > 0 || hideFailed) && (
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
            {/* Sort — default "Attention" floats merge-blocking reviews above
                clean ones and sinks failures to the bottom, so a page full of
                identical infrastructure errors cannot bury a real finding.

                Scope, stated plainly because it is genuinely limited: the list
                is one server page of PAGE_SIZE rows. The backend already
                returns created_at DESC for every page, so "Newest" matches the
                incoming order and changes nothing in practice; "Attention"
                can only reorder the window in front of the user, not pull an
                actionable review off another page. Client-side sorting across
                pages would need a server sort parameter, which is out of scope
                here — so the labels say what they actually do rather than
                implying a global reorder. */}
            <div
              className="flex items-center rounded-lg border border-zinc-800 bg-zinc-950 p-1"
              role="group"
              aria-label="Sort reviews on this page"
            >
              <SlidersHorizontal className="ml-1.5 h-3.5 w-3.5 shrink-0 text-zinc-500" aria-hidden="true" />
              {SORT_OPTIONS.map((opt) => (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => {
                    setSortKey(opt.value);
                    setPage(0);
                  }}
                  aria-pressed={sortKey === opt.value}
                  title={
                    opt.value === "attention"
                      ? "Reorders the reviews on this page: blockers first, clean last, failures at the bottom."
                      : "Newest first. The server already returns this page newest-first, so this normally changes nothing."
                  }
                  className={cn(
                    "rounded-md px-2.5 py-1 font-mono text-[10px] uppercase tracking-wider transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400",
                    sortKey === opt.value
                      ? "bg-zinc-800 font-semibold text-zinc-100 shadow-[0_1px_3px_rgba(0,0,0,0.3)]"
                      : "text-zinc-500 hover:text-zinc-300"
                  )}
                >
                  {opt.label}
                </button>
              ))}
            </div>

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

        {/* Triage summary — "what needs attention" in one glance. Hidden
            while the first load is in flight so it cannot flash empty
            counters before any data exists. */}
        {!loading && reviews.length > 0 && (
          <TriageSummary
            counts={triageCounts}
            activeOutcome={activeOutcome}
            // Deliberately does NOT reset to page 0.
            //
            // The counts are page-scoped: they are computed over
            // `filteredReviews`, the SAME 15-row window the chip filter runs
            // over. Resetting to page 0 would make the two disagree — click
            // "Need changes 3" while reading page 2 and the filter would be
            // applied to the rows of page 1, which is how a visible "3" lands
            // the user on an empty page. Keeping the page holds the scope
            // constant: the number you clicked and the rows you get come from
            // one window, which is exactly what the strip's own wording claims
            // ("Review triage summary for this page", and the explicit
            // in-view / on-this-page labels on the chips).
            onToggle={(key) => setActiveOutcome(key)}
          />
        )}

        {/* Distinct root causes behind the failures, stated once. No
            activeOutcome guard is needed any more: failureClusters is derived
            from the rendered rows, so when the active chip excludes failures
            the list is simply empty and the banner hides itself. */}
        {failureClusters.length > 0 && <FailureClusterBanner clusters={failureClusters} />}

        {/* Review Cards Stream */}
        {loading && reviews.length === 0 ? (
          <div className="space-y-4">
            {[1, 2, 3].map((n) => (
              <div key={n} className={cn(ROW_SURFACE, "space-y-4 p-5")}>
                <div className="flex flex-col gap-5 md:flex-row md:items-start">
                  <Skeleton className="h-16 w-16 rounded-full md:min-w-[84px]" />
                  <div className="flex-1 space-y-3">
                    <div className="flex items-center gap-2.5">
                      <Skeleton className="h-4 w-48" />
                      <Skeleton className="h-4 w-24 rounded-full" />
                      <Skeleton className="h-4 w-16 rounded-full" />
                    </div>
                    <Skeleton className="h-4 w-3/5" />
                    <Skeleton className="h-3 w-full" />
                    <Skeleton className="h-3 w-2/3" />
                    <Skeleton className="h-3 w-1/3" />
                  </div>
                  <div className="space-y-2 md:w-[164px]">
                    <Skeleton className="h-7 w-full" />
                    <Skeleton className="h-7 w-full" />
                  </div>
                </div>
              </div>
            ))}
          </div>
        ) : sortedReviews.length === 0 ? (
          <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-zinc-800 bg-[#0c0c0e] py-16 text-center space-y-3">
            <div className="flex h-12 w-12 items-center justify-center rounded-full bg-zinc-900 border border-zinc-800 text-zinc-500">
              {filtersActive ? (
                <Inbox className="h-6 w-6 text-zinc-500" />
              ) : (
                <ShieldCheck className="h-6 w-6 text-zinc-500" />
              )}
            </div>
            <div className="space-y-1">
              <h3 className="text-sm font-semibold text-zinc-200">
                {filtersActive ? "Nothing matches these filters" : "No Code Reviews Found"}
              </h3>
              <p className="text-xs text-zinc-400 max-w-sm">
                {filtersActive
                  ? "No review matches the current filter combination. Widen or clear the filters to see the rest."
                  : "Push commits or open a pull request on your connected repositories to trigger Haunter's Sentinel."}
              </p>
            </div>
            {/* Shown whenever a filter is active, NOT gated on `reviews.length`.
                repo_id / min_risk / severity are applied SERVER-side, so a
                filter with no matches returns reviews=[] — and gating on that
                hid the only control that can undo the filter, leaving the user
                on a permanently empty page with no way out. The copy above keys
                off the same flag for the same reason: "No Code Reviews Found /
                push some commits" is wrong when the user filtered their own
                existing reviews down to nothing. */}
            {filtersActive && (
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
                  setActiveOutcome(null);
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
            {sortedReviews.map((review) => (
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
