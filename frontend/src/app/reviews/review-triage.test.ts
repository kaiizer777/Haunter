import { describe, it, expect } from "vitest";
import {
  blockerCounts,
  clusterFailures,
  confidenceFromRisk,
  failureSignature,
  pendingOutcome,
  riskBand,
  scoredOutcome,
  sortByAttention,
  RISK_CRITICAL,
  RISK_MODERATE,
  type TriageInput,
} from "./review-triage";

/**
 * Unit coverage for the severity/outcome vocabulary that the /reviews cards,
 * gauge, chips and summary all read from. These are the honesty rules: a row
 * with no real analysis must never be described as safe, and "Attention"
 * ordering must sink failures below real findings.
 */
const finding = (severity: string) => ({ severity } as { severity: string });

describe("riskBand", () => {
  it("maps risk to exactly one band using the shared boundaries", () => {
    expect(riskBand(0, true).key).toBe("low");
    expect(riskBand(RISK_MODERATE - 1, true).key).toBe("low");
    expect(riskBand(RISK_MODERATE, true).key).toBe("elevated");
    expect(riskBand(RISK_CRITICAL - 1, true).key).toBe("elevated");
    expect(riskBand(RISK_CRITICAL, true).key).toBe("critical");
    expect(riskBand(100, true).key).toBe("critical");
  });

  it("returns the neutral band for unscored rows, never a low-risk verdict", () => {
    expect(riskBand(0, false).key).toBe("none");
    expect(riskBand(0, false).label).toBe("No score");
    expect(riskBand(0, false).chipLabel).toBe("NO SCORE");
    // An unscored row must not inherit the "low risk" green.
    expect(riskBand(0, false).stroke).not.toBe(riskBand(0, true).stroke);
  });

  it("clamps out-of-range scores instead of producing an undefined band", () => {
    expect(riskBand(-20, true).key).toBe("low");
    expect(riskBand(180, true).key).toBe("critical");
  });

  it("always pairs colour with a spoken label", () => {
    for (const score of [0, 31, 71, 100]) {
      const band = riskBand(score, true);
      expect(band.label.length).toBeGreaterThan(0);
      expect(band.chipLabel.length).toBeGreaterThan(0);
      expect(band.description.length).toBeGreaterThan(0);
    }
  });
});

describe("scoredOutcome", () => {
  const base: TriageInput = { risk_score: 10, status: "completed", findings: [] };

  it("requests changes when a must-fix finding exists regardless of score", () => {
    const outcome = scoredOutcome({ ...base, risk_score: 5, findings: [finding("critical")] });
    expect(outcome.key).toBe("changes_requested");
    expect(outcome.label).toBe("Changes Requested");
  });

  it("treats high severity as must-fix and medium as should-fix", () => {
    expect(blockerCounts([finding("critical"), finding("high"), finding("medium")])).toEqual({
      mustFix: 2,
      shouldFix: 1,
    });
    // Low/nit findings are neither; they must not inflate blocker counts.
    expect(blockerCounts([finding("low"), finding("nit")])).toEqual({ mustFix: 0, shouldFix: 0 });
  });

  it("requests changes on a critical score even with zero findings", () => {
    expect(scoredOutcome({ ...base, risk_score: RISK_CRITICAL, findings: [] }).key).toBe(
      "changes_requested"
    );
  });

  it("calls a low-risk, blocker-free audit safe to merge", () => {
    expect(scoredOutcome({ ...base, risk_score: 0, findings: [] }).key).toBe("clean");
    expect(scoredOutcome({ ...base, risk_score: RISK_MODERATE - 1, findings: [] }).key).toBe("clean");
  });

  it("keeps a moderate score advisory", () => {
    const outcome = scoredOutcome({ ...base, risk_score: RISK_MODERATE, findings: [] });
    expect(outcome.key).toBe("advisory");
    expect(outcome.label).toBe("Review Advised");
  });

  it("ranks merge-blocking above advisory above clean", () => {
    const ranks = [
      scoredOutcome({ ...base, risk_score: 90, findings: [] }).rank,
      scoredOutcome({ ...base, risk_score: 50, findings: [] }).rank,
      scoredOutcome({ ...base, risk_score: 5, findings: [] }).rank,
    ];
    expect(ranks).toEqual([...ranks].sort((a, b) => a - b));
  });
});

describe("pendingOutcome", () => {
  it("never carries a score or a merge verdict", () => {
    const outcome = pendingOutcome("pending");
    expect(outcome.key).toBe("in_progress");
    expect(outcome.band.key).toBe("none");
    expect(outcome.label).not.toBe("Great to Merge");
    expect(outcome.label).not.toBe("Changes Requested");
  });

  it("distinguishes queued from running in the label only", () => {
    expect(pendingOutcome("pending").label).toBe("Queued");
    expect(pendingOutcome("in_progress").label).toBe("Analyzing");
  });
});

describe("confidenceFromRisk", () => {
  it("stays inside 1..10 for any input", () => {
    expect(confidenceFromRisk(0)).toBe(10);
    expect(confidenceFromRisk(100)).toBe(1);
    expect(confidenceFromRisk(-50)).toBe(10);
    expect(confidenceFromRisk(500)).toBe(1);
  });
});

describe("failureSignature", () => {
  it("drops the parenthesised tail so repeated failures collapse to one cause", () => {
    expect(failureSignature("All 9 free-tier models exhausted (18 total attempts)")).toBe(
      "All 9 free-tier models exhausted"
    );
  });

  it("keeps distinct causes distinct", () => {
    expect(failureSignature("GitHub 404: repository not found")).not.toBe(
      failureSignature("All 9 free-tier models exhausted")
    );
  });

  it("never returns an empty signature for a blank reason", () => {
    expect(failureSignature("")).toBe("No failure reason recorded");
    expect(failureSignature(null)).toBe("No failure reason recorded");
  });
});

describe("clusterFailures", () => {
  it("groups identical causes and counts them, most frequent first", () => {
    const clusters = clusterFailures([
      { failure_reason: "Provider quota exhausted (9 attempts)" },
      { failure_reason: "Provider quota exhausted (12 attempts)" },
      { failure_reason: "GitHub 404 (not found)" },
    ]);
    expect(clusters).toHaveLength(2);
    expect(clusters[0]).toMatchObject({
      signature: "Provider quota exhausted",
      count: 2,
    });
    // The verbatim reason is preserved for the row, not just the signature.
    expect(clusters[0].sample).toContain("9 attempts");
  });

  it("returns nothing for an empty list", () => {
    expect(clusterFailures([])).toEqual([]);
  });
});

describe("sortByAttention", () => {
  const row = (id: string, risk_score: number, status: string, findings: { severity: string }[] = []) => ({
    id,
    risk_score,
    status,
    findings,
    created_at: "2026-01-01T00:00:00.000Z",
  });

  const outcomeOf = (r: { risk_score: number; status: string; findings: { severity: string }[] }) =>
    r.status === "error"
      ? { rank: 4 }
      : r.status === "pending"
        ? { rank: 2 }
        : scoredOutcome(r);

  it("sinks failures below real findings so repeated errors cannot bury signal", () => {
    const sorted = sortByAttention(
      [
        row("fail", 0, "error"),
        row("clean", 5, "completed"),
        row("blocker", 90, "completed", [finding("critical")]),
      ],
      outcomeOf as never,
      "attention"
    );
    expect(sorted.map((r) => r.id)).toEqual(["blocker", "clean", "fail"]);
  });

  it("puts the riskier actionable review first within the actionable band", () => {
    const sorted = sortByAttention(
      [row("low", 40, "completed"), row("high", 95, "completed")],
      outcomeOf as never,
      "attention"
    );
    expect(sorted.map((r) => r.id)).toEqual(["high", "low"]);
  });

  it("falls back to newest-first when sorted by newest", () => {
    const sorted = sortByAttention(
      [
        { ...row("old", 5, "completed"), created_at: "2026-01-01T00:00:00.000Z" },
        { ...row("new", 90, "completed"), created_at: "2026-06-01T00:00:00.000Z" },
      ],
      outcomeOf as never,
      "newest"
    );
    expect(sorted.map((r) => r.id)).toEqual(["new", "old"]);
  });

  it("does not mutate its input", () => {
    const input = [row("a", 5, "completed"), row("b", 90, "completed")];
    const copy = [...input];
    sortByAttention(input, outcomeOf as never, "attention");
    expect(input).toEqual(copy);
  });
});