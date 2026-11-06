/**
 * Core analytical and status predicates, blast radius calculations,
 * and staged-fix persistence helpers for Code Reviews.
 */

import { RISK_CRITICAL, RISK_MODERATE } from "../review-triage";
export { RISK_CRITICAL, RISK_MODERATE };
import type { CodeReviewOut, ReviewFindingOut, StagedFixScope } from "../types";

/**
 * Enqueue placeholder written before analysis runs — the only summary a row
 * can carry when the orchestrator never persisted a result in webhooks handling.
 * A non-empty summary alone therefore proves nothing; it must differ from this.
 */
export const QUEUED_SUMMARY = "Autonomous code review queued.";

/**
 * A review whose analysis never completed — never render verdict telemetry for it.
 * Failed iff the backend reports status "error". A stale `failure_reason` on a
 * completed review must NOT hide its real verdict (the API type allows the
 * combination), so `failure_reason` alone is intentionally ignored here.
 */
export function isReviewFailed(review: CodeReviewOut): boolean {
  return review.status?.toLowerCase() === "error";
}

/**
 * status=error that still carries a completed analysis — publish failure.
 *
 * Strict positive-evidence gate. The orchestrator persists risk_score +
 * findings + token telemetry TOGETHER before it attempts publication, and NO
 * analysis-failure path writes any of them: diff-fetch failures, LLM failures,
 * and repo-not-found guards write only status + failure_reason + summary, and
 * timeout/crash backstops only touch status + failure_reason. So tokens, findings,
 * or a nonzero risk on an error row prove the persist step ran. (Enqueue
 * defaults are risk 0 + findings [] + zero tokens.)
 *
 * The 0/0-tokens + empty-findings + risk-0 corner (a clean or empty-diff
 * audit whose publish failed) is structurally identical to an LLM analysis
 * failure, so it falls through to the failure_reason prefixes below: the
 * post-persist error writes are "Failed to publish PR review:",
 * "Failed to publish commit comment:" and "Failed to read repository review settings:".
 * Anything else — including a custom analysis-failure summary — is fail-closed
 * to analysis-failed, which renders the neutral error card and never a fake
 * "Great to Merge" verdict.
 */
export function isPublishFailed(review: CodeReviewOut): boolean {
  if (!isReviewFailed(review)) return false;
  if (review.input_tokens > 0 || review.output_tokens > 0) return true;
  if (review.findings && review.findings.length > 0) return true;
  if (review.risk_score > 0) return true;
  const reason = (review.failure_reason ?? "").toLowerCase();
  return (
    reason.startsWith("failed to publish pr review:") ||
    reason.startsWith("failed to publish commit comment:") ||
    reason.startsWith("failed to read repository review settings:")
  );
}

/**
 * A row whose analysis FAILED, as opposed to one whose analysis succeeded and
 * whose PUBLICATION failed.
 *
 * The backend uses `status="error"` for both, but keeps `findings`, `summary`
 * and token telemetry on the row for the publish-failure case — deliberately,
 * so the dashboard stays truthful about an audit it did compute.
 */
export function isAnalysisFailed(review: CodeReviewOut): boolean {
  return isReviewFailed(review) && !isPublishFailed(review);
}

/**
 * A review with no finished analysis to report — never render verdict telemetry for it.
 * Fail-closed: only "completed" and "suppressed" carry a real score.
 * "suppressed" is terminal WITH a saved audit. Everything else — "pending"
 * or "in_progress" renders neutrally.
 */
export function isReviewPending(review: CodeReviewOut): boolean {
  const s = review.status?.toLowerCase();
  return s !== "completed" && s !== "error" && s !== "suppressed";
}

/**
 * Publication was skipped by repo settings, but the saved audit is real.
 */
export function isSuppressed(review: CodeReviewOut): boolean {
  return review.status?.toLowerCase() === "suppressed";
}

/**
 * Explicit completion signal for rows whose analysis output was persisted.
 */
export function hasCompletedAnalysis(review: CodeReviewOut): boolean {
  if (review.input_tokens > 0 || review.output_tokens > 0) return true;
  const summary = (review.summary ?? "").trim();
  return (
    summary.length > 0 && summary.toLowerCase() !== QUEUED_SUMMARY.toLowerCase()
  );
}

/**
 * Suppressed row with no saved audit — legacy row or suppress-before-save
 * race: analysis never persisted, so neither token telemetry nor a real
 * summary exists. Fail-closed: renders neutrally like pending.
 */
export function isSuppressedUnscored(review: CodeReviewOut): boolean {
  return isSuppressed(review) && !hasCompletedAnalysis(review);
}

/**
 * Neutral status-pill label for unscored rows.
 */
export function pendingLabelFor(review: CodeReviewOut): string {
  const s = review.status?.toLowerCase();
  if (s === "pending") return "Queued";
  return "Analyzing";
}

/**
 * Blast-radius telemetry statistics indicating change volume, lines delta, and impact scope.
 */
export interface BlastStats {
  filesCount: number;
  additions: number;
  deletions: number;
  /**
   * Impact scope classification:
   * - "Targeted": Localized diff with <= 3 files and <= 60 modifications under moderate risk.
   * - "Moderate": Medium diff surface (4-8 files or 61-250 modifications) or elevated risk.
   * - "Extensive": Wide impact (> 8 files, > 250 modifications, or critical risk >= 71).
   */
  scope: "Targeted" | "Moderate" | "Extensive";
  scopeColor: string;
}

/**
 * Blast-radius telemetry shared by the overview strip and the detail report.
 */
export function getBlastStats(review: CodeReviewOut): BlastStats {
  const findings = review.findings || [];
  const filesCount =
    review.diff_stats?.files_changed ??
    new Set(findings.map((f) => f.file_path)).size;

  const additions =
    review.diff_stats?.additions ??
    findings.reduce((acc, f) => {
      if (!f.suggested_patch) return acc;
      const added = (f.suggested_patch.match(/^\+[^+]/gm) || []).length;
      return acc + (added || 1);
    }, 0);

  const deletions =
    review.diff_stats?.deletions ??
    findings.reduce((acc, f) => {
      if (!f.suggested_patch) return acc;
      const removed = (f.suggested_patch.match(/^-[^-]/gm) || []).length;
      return acc + (removed || 0);
    }, 0);

  const totalModifications = additions + deletions;
  let scope: "Targeted" | "Moderate" | "Extensive" = "Targeted";
  let scopeColor = "border-emerald-500/30 bg-emerald-950/40 text-emerald-300";

  if (
    filesCount > 8 ||
    totalModifications > 250 ||
    review.risk_score >= RISK_CRITICAL
  ) {
    scope = "Extensive";
    scopeColor = "border-rose-500/30 bg-rose-950/40 text-rose-300";
  } else if (
    filesCount > 3 ||
    totalModifications > 60 ||
    review.risk_score >= RISK_MODERATE
  ) {
    scope = "Moderate";
    scopeColor = "border-amber-500/30 bg-amber-950/40 text-amber-300";
  }
  return { filesCount, additions, deletions, scope, scopeColor };
}

/**
 * Human-readable blast-radius scope label + one-line explanation.
 * Maps "Targeted" internal scope to user-facing "Isolated" surface terminology.
 */
export function blastRadiusSummary(review: CodeReviewOut): {
  label: string;
  explanation: string;
} {
  const blast = getBlastStats(review);
  const label = blast.scope === "Targeted" ? "Isolated" : blast.scope;
  const touched = [...new Set((review.findings || []).map((f) => f.file_path))];
  const area =
    touched.length > 0
      ? touched.length === 1
        ? touched[0].split("/").slice(-2).join("/")
        : `${touched.length} components`
      : "no finding locations";
  return {
    label,
    explanation: `${blast.filesCount} ${blast.filesCount === 1 ? "file" : "files"}, +${blast.additions}/-${blast.deletions} across ${area}`,
  };
}

/**
 * 32-bit FNV-1a non-cryptographic hash for stable string identity.
 */
export function fnv1a32(input: string): number {
  let hash = 0x811c9dc5;
  for (let i = 0; i < input.length; i++) {
    hash ^= input.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash;
}

/**
 * Computes a deterministic, stable key for a review finding within a review.
 * Avoids index-based React keys when mapping findings across rerenders and lens filters.
 */
export function computeFindingStableKey(
  finding: ReviewFindingOut,
  reviewId?: string | number
): string {
  const prefix = reviewId ? `${reviewId}:` : "";
  const path = finding.file_path || "general";
  const start = finding.line_start ?? 0;
  const end = finding.line_end ?? 0;
  const cat = finding.category || "finding";
  const sym = finding.symbol_name ? `:${finding.symbol_name}` : "";
  const critiqueHash = `:${fnv1a32(finding.critique || "").toString(16)}`;
  let patchSuffix = "";
  if (finding.suggested_patch) {
    patchSuffix = `:${fnv1a32(finding.suggested_patch).toString(16)}`;
  }
  return `${prefix}${cat}:${path}:${start}-${end}${sym}${critiqueHash}${patchSuffix}`;
}

/**
 * Local staged-fix storage key in localStorage.
 */
export const STAGED_FIXES_KEY = "haunter-staged-fixes";

/**
 * Identity of one staged fix, used as the localStorage key.
 * Uses 32-bit FNV-1a hash of the full suggested patch body.
 */
export function stagedFixKey(
  finding: ReviewFindingOut,
  scope: StagedFixScope = {}
): string {
  const patch = finding.suggested_patch ?? "";
  const hash = fnv1a32(patch);
  const repo =
    scope.repoOwner && scope.repoName
      ? `${scope.repoOwner}/${scope.repoName}`
      : "";
  return `${repo}@${scope.commitSha ?? ""}:${finding.file_path}:${finding.line_start}-${finding.line_end}:${patch.length}:${hash.toString(16)}`;
}

/**
 * Read all currently staged fixes from localStorage.
 */
export function readStagedFixes(): Record<string, string> {
  if (typeof window === "undefined" || !window.localStorage) {
    return {};
  }
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

/**
 * Test whether a particular finding's candidate remediation patch is currently staged.
 */
export function isFixStaged(
  finding: ReviewFindingOut,
  scope?: StagedFixScope
): boolean {
  try {
    return stagedFixKey(finding, scope) in readStagedFixes();
  } catch {
    return false;
  }
}

/**
 * Toggle the staged status of a candidate remediation patch in localStorage.
 * Throws if localStorage.setItem fails (quota exceeded / storage disabled).
 * Returns `true` if now staged, `false` if un-staged.
 */
export function toggleStagedFix(
  finding: ReviewFindingOut,
  scope?: StagedFixScope
): boolean {
  if (!finding.suggested_patch) return false;
  const stored = readStagedFixes();
  const key = stagedFixKey(finding, scope);
  const nextStaged = !(key in stored);

  if (nextStaged) {
    stored[key] = finding.suggested_patch;
  } else {
    delete stored[key];
  }

  // May throw in quota-exceeded or private-browsing modes
  localStorage.setItem(STAGED_FIXES_KEY, JSON.stringify(stored));
  return nextStaged;
}

/**
 * Clear all staged fixes from localStorage (helper for testing or reset).
 */
export function clearStagedFixes(): void {
  if (typeof window !== "undefined" && window.localStorage) {
    try {
      localStorage.removeItem(STAGED_FIXES_KEY);
    } catch {
      // Ignore
    }
  }
}
