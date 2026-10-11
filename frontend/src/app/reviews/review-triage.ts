/**
 * Triage + severity tokens for the Code Reviews surface.
 *
 * Single source of truth for three things that used to disagree with each
 * other across the page (the donut was amber, the ERROR badge rose, the
 * confidence numeral plain white):
 *
 *  1. The risk band (RiskBand) — drives the gauge, the verdict chip, the
 *     blocker colouring and the stat labels from ONE function.
 *  2. The review outcome (Outcome) — failed / in-progress / scored, derived
 *     with the same honesty gates the page has always used (a failed or
 *     unfinished analysis never renders verdict telemetry).
 *  3. The triage order + counts — what the user should look at first, and
 *     the filter chips that let them act on it.
 *
 * Page-owned: only app/reviews/page.tsx imports this. Nothing else in the app
 * depends on it, so the blast radius of changing it is exactly one page.
 *
 * Contrast: every tone below meets WCAG 2.2 AA (>=4.5:1) for small text on the
 * card surface (#0c0c0e - #111115). Muted labels deliberately use zinc-400
 * (~7.1:1) rather than zinc-500 (~3.9:1, which fails AA for 10px labels).
 */

/** Same high-risk boundary the gauge, the API min_risk param and the
 *  verdict merge rule have always used. Never fork this number. */
export const RISK_CRITICAL = 71;
export const RISK_MODERATE = 31;

export type RiskBandKey = "critical" | "elevated" | "low" | "none";

export interface RiskBand {
  key: RiskBandKey;
  /** Sentence-case name. Used in prose and in the gauge's text alternative. */
  label: string;
  /** Uppercase literal for the chip. Kept separate from `label` so the chip
   *  text is real DOM text rather than a CSS-only transform — assistive tech
   *  and the test suite both read the literal. */
  chipLabel: string;
  /** Full sentence alternative for assistive tech on the gauge. */
  description: string;
  /** Ring stroke for the gauge. */
  stroke: string;
  /** Text colour that clears 4.5:1 on the card surface. */
  text: string;
  /** Translucent chip: border + background, per the existing badge vocabulary. */
  chip: string;
  /** Solid dot used in inline legends and the triage chips. */
  dot: string;
}

const BAND_NONE: RiskBand = {
  key: "none",
  label: "No score",
  chipLabel: "NO SCORE",
  description: "No risk score — the analysis did not complete",
  stroke: "#52525b",
  text: "text-zinc-400",
  chip: "border-zinc-700 bg-zinc-900 text-zinc-300",
  dot: "bg-zinc-500",
};

const BAND_LOW: RiskBand = {
  key: "low",
  label: "Low",
  chipLabel: "LOW RISK",
  description: "Low risk",
  stroke: "#10b981",
  text: "text-emerald-400",
  chip: "border-emerald-500/40 bg-emerald-500/10 text-emerald-200",
  dot: "bg-emerald-400",
};

const BAND_ELEVATED: RiskBand = {
  key: "elevated",
  label: "Elevated",
  chipLabel: "ELEVATED",
  description: "Elevated risk — review before merging",
  stroke: "#f59e0b",
  text: "text-amber-400",
  chip: "border-amber-500/40 bg-amber-500/10 text-amber-200",
  dot: "bg-amber-400",
};

const BAND_CRITICAL: RiskBand = {
  key: "critical",
  label: "Critical",
  chipLabel: "CRITICAL",
  description: "Critical risk — changes requested",
  stroke: "#f43f5e",
  text: "text-rose-400",
  chip: "border-rose-500/50 bg-rose-500/15 text-rose-200",
  dot: "bg-rose-400",
};

/**
 * The one severity mapping. `scored=false` is the honest no-score state for
 * analyses that never finished — a neutral band, never a fake 0 / "Safe".
 */
export function riskBand(riskScore: number, scored: boolean): RiskBand {
  if (!scored) return BAND_NONE;
  const clamped = Math.max(0, Math.min(100, riskScore));
  if (clamped >= RISK_CRITICAL) return BAND_CRITICAL;
  if (clamped >= RISK_MODERATE) return BAND_ELEVATED;
  return BAND_LOW;
}

export type OutcomeKey =
  | "changes_requested"
  | "advisory"
  | "clean"
  | "in_progress"
  | "failed";

export interface Outcome {
  key: OutcomeKey;
  /** Merge advice, phrased as the decision the user has to make. */
  label: string;
  /** Screen-reader context for the chip. */
  description: string;
  band: RiskBand;
  /** Sort rank in the default "Attention" order. Lower sorts first. */
  rank: number;
  /** One-line explanation shown in the triage legend / chip tooltip. */
  meaning: string;
}

export interface TriageInput {
  risk_score: number;
  status?: string | null;
  findings: { severity: string }[];
  failure_reason?: string | null;
}

/** Must-fix = critical + high findings. Should-fix = medium. */
export function blockerCounts(findings: { severity: string }[]): {
  mustFix: number;
  shouldFix: number;
} {
  let mustFix = 0;
  let shouldFix = 0;
  for (const f of findings) {
    const sev = f.severity.toLowerCase();
    if (sev === "critical" || sev === "high") mustFix++;
    else if (sev === "medium") shouldFix++;
  }
  return { mustFix, shouldFix };
}

/** PR confidence x/10 derived from risk score: low risk => high confidence. */
export function confidenceFromRisk(riskScore: number): number {
  const clamped = Math.max(0, Math.min(100, riskScore));
  return Math.max(1, Math.min(10, Math.round((100 - clamped) / 10)));
}

/**
 * Outcome for a review that actually produced a verdict.
 * Merge semantics unchanged: must-fix findings or a critical score request
 * changes; a clean low-risk audit is safe; everything else is advisory.
 */
export function scoredOutcome(input: TriageInput): Outcome {
  const { mustFix } = blockerCounts(input.findings);
  const risk = Math.max(0, Math.min(100, input.risk_score));
  const band = riskBand(risk, true);

  if (mustFix > 0 || risk >= RISK_CRITICAL) {
    return {
      key: "changes_requested",
      label: "Changes Requested",
      description:
        mustFix > 0
          ? `Changes requested — ${mustFix} must-fix ${mustFix === 1 ? "finding" : "findings"}`
          : "Changes requested — critical risk score",
      band,
      rank: 0,
      meaning: "Must-fix findings or a critical risk score. Address before merge.",
    };
  }
  if (risk < RISK_MODERATE) {
    return {
      key: "clean",
      label: "Great to Merge",
      description: "Great to merge — no blockers and a low risk score",
      band,
      rank: 3,
      meaning: "No blockers and a low risk score. Routine review is enough.",
    };
  }
  return {
    key: "advisory",
    label: "Review Advised",
    description: "Review advised — worth a focused pass before merge",
    band,
    rank: 1,
    meaning: "No must-fix findings, but the change carries real risk.",
  };
}

/** Neutral outcome for an analysis that has not finished. No band, no score. */
export function pendingOutcome(status?: string | null): Outcome {
  const s = status?.toLowerCase();
  return {
    key: "in_progress",
    label: s === "pending" ? "Queued" : "Analyzing",
    description: "Analysis has not completed yet — no verdict available",
    band: BAND_NONE,
    rank: 2,
    meaning: "Still running. A verdict appears here once the analysis lands.",
  };
}

export const FAILED_OUTCOME: Outcome = {
  key: "failed",
  label: "Analysis failed",
  description: "Analysis failed — no verdict was produced",
  band: BAND_NONE,
  rank: 4,
  meaning: "The analysis never completed. Retry, or inspect the failure reason.",
};

/** Order the triage chips render in — highest attention first. */
export const OUTCOME_ORDER: OutcomeKey[] = [
  "changes_requested",
  "advisory",
  "in_progress",
  "failed",
  "clean",
];

/**
 * Collapse repeated failure text into a cause signature so a page full of
 * "All 9 free-tier models exhausted (18 total attempts)" states the cause
 * once instead of nine times. Parenthesised detail (attempt counts, HTTP
 * bodies) is dropped from the signature but kept verbatim on the row.
 */
export function failureSignature(reason: string | null | undefined): string {
  const text = (reason ?? "").trim();
  if (!text) return "No failure reason recorded";
  const head = text.split("(")[0].trim();
  const base = (head || text).replace(/[.\s]+$/, "");
  return base.length > 0 ? base : text;
}

export interface FailureCluster {
  signature: string;
  count: number;
  /** The raw reason text of the first row in the cluster, shown verbatim. */
  sample: string;
}

/** Group failed rows by root cause, most frequent first. */
export function clusterFailures<T extends { failure_reason?: string | null }>(
  failed: T[],
): FailureCluster[] {
  const bySignature = new Map<string, FailureCluster>();
  for (const row of failed) {
    const signature = failureSignature(row.failure_reason);
    const existing = bySignature.get(signature);
    if (existing) {
      existing.count += 1;
    } else {
      bySignature.set(signature, {
        signature,
        count: 1,
        sample: row.failure_reason ?? signature,
      });
    }
  }
  return [...bySignature.values()].sort(
    (a, b) => b.count - a.count || a.signature.localeCompare(b.signature)
  );
}

export type SortKey = "attention" | "newest";

export const SORT_OPTIONS: { value: SortKey; label: string }[] = [
  { value: "attention", label: "Attention" },
  { value: "newest", label: "Newest" },
];

/**
 * Default sort is by attention: things that block a merge float to the top,
 * finished-and-clean rows sink, and failures sink to the very bottom so a
 * page full of identical infrastructure errors cannot bury a real finding.
 */
export function sortByAttention<T extends TriageInput & { created_at: string }>(
  rows: T[],
  outcomeOf: (row: T) => Outcome,
  sort: SortKey,
): T[] {
  const copy = [...rows];
  if (sort === "newest") {
    return copy.sort(
      (a, b) => Date.parse(b.created_at) - Date.parse(a.created_at)
    );
  }
  return copy.sort((a, b) => {
    const ra = outcomeOf(a).rank;
    const rb = outcomeOf(b).rank;
    if (ra !== rb) return ra - rb;
    if (ra === 0 || ra === 1) {
      // Within the actionable bands the riskier review comes first.
      const risk = b.risk_score - a.risk_score;
      if (risk !== 0) return risk;
    }
    return Date.parse(b.created_at) - Date.parse(a.created_at);
  });
}