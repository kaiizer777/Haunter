/**
 * Domain types for the modern Code Reviews surface and triage pipeline.
 *
 * Models review findings, priority buckets (must_fix, should_fix, suggestion),
 * multi-perspective lenses, repository review preferences, and telemetry filters.
 */

import type { OutcomeKey, SortKey } from "./review-triage";

export type { OutcomeKey, SortKey };

/**
 * Modern triage priority buckets.
 * - must_fix: Critical or high findings that block a clean merge.
 * - should_fix: Medium findings requiring a focused pass before merge.
 * - suggestion: Low or informational improvements / style notes.
 */
export type PriorityBucket = "must_fix" | "should_fix" | "suggestion";

/**
 * Multi-perspective analytical lenses applied during code review.
 */
export type ReviewPerspective =
  | "all"
  | "security"
  | "correctness"
  | "performance"
  | "architecture"
  | "holistic";

/**
 * Finding categories emitted by review agents or static analysis.
 */
export type FindingCategory =
  | "security"
  | "logic"
  | "correctness"
  | "performance"
  | "api_compatibility"
  | "architecture"
  | "holistic"
  | string;

/**
 * Finding severity levels.
 */
export type FindingSeverity = "critical" | "high" | "medium" | "low" | string;

/**
 * Repository review preferences captured at analysis time.
 */
export interface AppliedReviewPrefs {
  prefs?: {
    review_guidelines_text?: string | null;
    review_strictness?: "lenient" | "medium" | "strict" | string | null;
    review_focus_areas?: string[] | null;
    review_checklist?: string[] | null;
  } | null;
  settings_version?: number | null;
  truncated?: boolean;
}

/**
 * Single finding produced during an automated code review.
 */
export interface ReviewFindingOut {
  file_path: string;
  line_start: number;
  line_end: number;
  category: FindingCategory;
  severity: FindingSeverity;
  critique: string;
  suggested_patch?: string | null;
  symbol_name?: string | null;
  ast_type?: string | null;
  priority?: PriorityBucket;
  perspective?: ReviewPerspective;
}

/**
 * Git diff statistics associated with a review.
 */
export interface DiffStats {
  additions: number;
  deletions: number;
  files_changed: number;
}

/**
 * Full code review entity returned by the backend.
 */
export interface CodeReviewOut {
  id: string;
  repo_id: string;
  repo_owner?: string | null;
  repo_name?: string | null;
  commit_sha: string;
  pr_number?: number | null;
  pr_title?: string | null;
  pr_author?: string | null;
  pr_author_avatar?: string | null;
  head_branch?: string | null;
  base_branch?: string | null;
  pr_url?: string | null;
  failure_reason?: string | null;
  diff_stats?: DiffStats | null;
  risk_score: number;
  summary: string;
  findings: ReviewFindingOut[];
  status: string;
  input_tokens: number;
  output_tokens: number;
  applied_review_prefs?: AppliedReviewPrefs | null;
  created_at: string;
}

/**
 * Code review list envelope.
 */
export interface CodeReviewListOut {
  reviews: CodeReviewOut[];
  total: number;
}

/**
 * Single PR associated with a review, via GET /reviews/{id}/associated-pr.
 */
export interface AssociatedPROut {
  number: number;
  url: string;
  title: string | null;
}

/**
 * Tracked repository entity.
 */
export interface RepoOut {
  id: string;
  owner: string;
  name: string;
  default_branch: string | null;
  language_hint: string | null;
  created_at: string;
  registration_conflict?: boolean;
}

/**
 * Navigation / ribbon tabs for filtering the review list.
 */
export type ReviewRibbonTab = "all" | "high_risk" | "avg_risk" | "patches";

/**
 * Detail tabs for the deep-dive review view.
 */
export type DetailTab =
  | "findings"
  | "diff"
  | "architecture"
  | "preferences"
  | "telemetry";

/**
 * Generic telemetry tab representation.
 */
export type TelemetryTab = ReviewRibbonTab | DetailTab;

/**
 * Scope for staged fix persistence in localStorage.
 */
export interface StagedFixScope {
  repoOwner?: string | null;
  repoName?: string | null;
  commitSha?: string | null;
}
