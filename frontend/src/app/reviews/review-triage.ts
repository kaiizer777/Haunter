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
 * Contrast: the band text/chip colours below are chosen to read clearly on the
 * card surface (#0c0c0e - #111115). This module does NOT claim a measured
 * contrast ratio — none has been verified here, so no number is asserted.
 * Note that the surrounding page still uses zinc-500 for de-emphasized 10px
 * chrome (see MetaFact in page.tsx), which is below the AA 4.5:1 threshold for
 * small text; migrating that is a page-wide visual change, not a token edit.
 */

/** Same high-risk boundary the gauge, the API min_risk param and the
 *  verdict merge rule have always used. Never fork this number. */
export const RISK_CRITICAL = 71;
export const RISK_MODERATE = 31;

type RiskBandKey = "critical" | "elevated" | "low" | "none";

export interface RiskBand {
  key: RiskBandKey;
  /** Sentence-case name. Used in prose and in the gauge's text alternative. */
  label: string;
  /** Uppercase literal for the chip. Kept separate from `label` so the chip
   *  text is real DOM text rather than a CSS-only transform — assistive tech
   *  and the test suite both read the literal. */
  chipLabel: string;
  /** Ring stroke for the gauge. */
  stroke: string;
  /** Text colour for the band's value text on the card surface. No contrast
   *  ratio is claimed here — see the module header. */
  text: string;
  /** Translucent chip: border + background, per the existing badge vocabulary. */
  chip: string;
  /** Solid dot used in inline legends and the triage chips. */
  dot: string;
}

/** Module-level and frozen: riskBand() hands these objects straight to the
 *  page, which reads `.chip` / `.dot` / `.text` into className strings. Frozen
 *  so one careless consumer cannot corrupt another row's band. */
const BAND_NONE: RiskBand = Object.freeze({
  key: "none",
  label: "No score",
  chipLabel: "NO SCORE",
  stroke: "#52525b",
  text: "text-zinc-400",
  chip: "border-zinc-700 bg-zinc-900 text-zinc-300",
  dot: "bg-zinc-500",
});

const BAND_LOW: RiskBand = Object.freeze({
  key: "low",
  label: "Low",
  chipLabel: "LOW RISK",
  stroke: "#10b981",
  text: "text-emerald-400",
  chip: "border-emerald-500/40 bg-emerald-500/10 text-emerald-200",
  dot: "bg-emerald-400",
});

const BAND_ELEVATED: RiskBand = Object.freeze({
  key: "elevated",
  label: "Elevated",
  chipLabel: "ELEVATED",
  stroke: "#f59e0b",
  text: "text-amber-400",
  chip: "border-amber-500/40 bg-amber-500/10 text-amber-200",
  dot: "bg-amber-400",
});

const BAND_CRITICAL: RiskBand = Object.freeze({
  key: "critical",
  label: "Critical",
  chipLabel: "CRITICAL",
  stroke: "#f43f5e",
  text: "text-rose-400",
  chip: "border-rose-500/50 bg-rose-500/15 text-rose-200",
  dot: "bg-rose-400",
});

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
  | "suppressed"
  | "failed";

export interface Outcome {
  key: OutcomeKey;
  /** Merge advice, phrased as the decision the user has to make. */
  label: string;
  band: RiskBand;
  /** Sort rank in the default "Attention" order. Lower sorts first. */
  rank: number;
  /** One-line explanation shown in the triage legend / chip tooltip. */
  meaning: string;
}

/**
 * The canonical one-line explanation per outcome. Single source for the
 * outcome builders below AND the page's triage chips, so the chip tooltip and
 * the detail panel can never drift apart. Every outcome carries the meaning of
 * its key — not a per-row string, which is what made the two drift.
 */
export function outcomeMeaning(key: OutcomeKey): string {
  switch (key) {
    case "changes_requested":
      return "Must-fix findings or a critical risk score. Address before merge.";
    case "advisory":
      return "No must-fix findings. Medium findings still need a focused pass before merge.";
    case "in_progress":
      return "Still running. A verdict appears here once the analysis lands.";
    case "failed":
      return "The analysis never completed. Retry, or inspect the failure reason.";
    case "suppressed":
      return "Skipped by repository settings, so no analysis was stored. Nothing is running.";
    case "clean":
      return "No blockers and a low risk score. Routine review is enough.";
  }
}

export interface TriageInput {
  risk_score: number;
  findings: { severity: string }[];
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
 *
 * Merge semantics are unchanged from the pre-redesign verdictFor(): must-fix
 * findings or a critical score request changes; a LOW-risk review is safe to
 * merge only when it is also free of should-fix (medium) findings; everything
 * else is advisory. Dropping the `shouldFix === 0` half of that condition is
 * how a review carrying real medium findings came to be labelled "Great to
 * Merge", so it is asserted directly in review-triage.test.ts.
 */
export function scoredOutcome(input: TriageInput): Outcome {
  const { mustFix, shouldFix } = blockerCounts(input.findings);
  const risk = Math.max(0, Math.min(100, input.risk_score));
  const band = riskBand(risk, true);

  if (mustFix > 0 || risk >= RISK_CRITICAL) {
    return {
      key: "changes_requested",
      label: "Changes Requested",
      band,
      rank: 0,
      meaning: outcomeMeaning("changes_requested"),
    };
  }
  if (risk < RISK_MODERATE && shouldFix === 0) {
    return {
      key: "clean",
      label: "Great to Merge",
      band,
      rank: 3,
      meaning: outcomeMeaning("clean"),
    };
  }
  return {
    key: "advisory",
    label: "Review Advised",
    band,
    rank: 1,
    meaning: outcomeMeaning("advisory"),
  };
}

/** Neutral outcome for an analysis that has not finished. No band, no score. */
export function pendingOutcome(status?: string | null): Outcome {
  const s = status?.toLowerCase();
  return {
    key: "in_progress",
    label: s === "pending" ? "Queued" : "Analyzing",
    band: BAND_NONE,
    rank: 2,
    meaning: outcomeMeaning("in_progress"),
  };
}

export const FAILED_OUTCOME: Outcome = Object.freeze({
  key: "failed",
  label: "Analysis failed",
  band: BAND_NONE,
  rank: 4,
  meaning: outcomeMeaning("failed"),
});

/**
 * Terminal outcome for a run the orchestrator deliberately skipped: repo
 * settings suppressed publication, so no analysis was ever stored.
 *
 * It must not reuse pendingOutcome(). Suppression is terminal — nothing is
 * running and nothing will ever arrive — so routing these rows through
 * `in_progress` counted a finished run as work in flight, sorted it above
 * rows that actually need a decision (rank 2 vs 4), and rendered the chip
 * tooltip "Still running. A verdict appears here once the analysis lands."
 * on a row that will never receive one.
 */
export const SUPPRESSED_UNSCORED_OUTCOME: Outcome = Object.freeze({
  key: "suppressed",
  label: "Suppressed",
  band: BAND_NONE,
  rank: 4,
  meaning: outcomeMeaning("suppressed"),
});

/** Order the triage chips render in — highest attention first. */
export const OUTCOME_ORDER: readonly OutcomeKey[] = Object.freeze([
  "changes_requested",
  "advisory",
  "in_progress",
  "failed",
  "suppressed",
  "clean",
] as OutcomeKey[]);

/**
 * Collapse repeated failure text into a cause signature so a page full of
 * "All 9 free-tier models exhausted (18 total attempts)" states the cause
 * once instead of nine times. Parenthesised detail (attempt counts, HTTP
 * bodies) is dropped from the signature only; the unredacted text stays on
 * the individual row, where the caller renders it, and never leaves the row
 * through a cluster.
 */
function failureSignature(reason: string | null | undefined): string {
  const text = (reason ?? "").trim();
  if (!text) return "No failure reason recorded";
  const head = text.split("(")[0].trim();
  const base = (head || text).replace(/[.\s]+$/, "");
  return base.length > 0 ? base : text;
}

export interface FailureCluster {
  signature: string;
  count: number;
}

/**
 * Group failed rows by root cause, most frequent first.
 *
 * The raw `failure_reason` is deliberately not part of the result. It is
 * unredacted backend text of unbounded length, and a cluster renders as a bare
 * badge — shipping it here served no consumer.
 */
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
      bySignature.set(signature, { signature, count: 1 });
    }
  }
  return [...bySignature.values()].sort(
    (a, b) => b.count - a.count || a.signature.localeCompare(b.signature)
  );
}

export type SortKey = "attention" | "newest";

export const SORT_OPTIONS: readonly { readonly value: SortKey; readonly label: string }[] =
  Object.freeze([
    { value: "attention", label: "Attention" },
    { value: "newest", label: "Newest" },
  ] as const);

/**
 * Parse a backend timestamp. An unparseable value sorts to -Infinity rather
 * than NaN: NaN would survive the arithmetic below and make the comparator
 * return NaN, which Array.sort coerces to 0 — i.e. a malformed timestamp would
 * silently make the whole "Newest" control a no-op instead of visibly sinking
 * the rows it cannot date.
 */
function timestampOf(value: string | null | undefined): number {
  const parsed = Date.parse(value ?? "");
  return Number.isNaN(parsed) ? Number.NEGATIVE_INFINITY : parsed;
}

/** Stable final tiebreak so the comparator is a total order on distinct ids. */
function byId(a: { id: string }, b: { id: string }): number {
  return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
}

/**
 * Default sort is by attention: things that block a merge float to the top,
 * finished-and-clean rows sink, and failures sink to the very bottom so a
 * page full of identical infrastructure errors cannot bury a real finding.
 */
export function sortByAttention<T extends TriageInput & { created_at: string; id: string }>(
  rows: T[],
  outcomeOf: (row: T) => Outcome,
  sort: SortKey,
): T[] {
  const copy = [...rows];
  if (sort === "newest") {
    return copy.sort((a, b) => {
      const ta = timestampOf(a.created_at);
      const tb = timestampOf(b.created_at);
      return ta === tb ? byId(a, b) : tb - ta;
    });
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
    const ta = timestampOf(a.created_at);
    const tb = timestampOf(b.created_at);
    return ta === tb ? byId(a, b) : tb - ta;
  });
}
