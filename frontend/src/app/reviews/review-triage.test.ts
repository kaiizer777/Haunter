import { describe, it, expect } from "vitest";
import {
  blockerCounts,
  clusterFailures,
  confidenceFromRisk,
  FAILED_OUTCOME,
  outcomeMeaning,
  pendingOutcome,
  riskBand,
  scoredOutcome,
  sortByAttention,
  RISK_CRITICAL,
  RISK_MODERATE,
  type Outcome,
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
      // label feeds the gauge's text alternative, chipLabel the rendered chip.
      // Both must be real text so severity is never carried by colour alone.
      expect(band.label.length).toBeGreaterThan(0);
      expect(band.chipLabel.length).toBeGreaterThan(0);
    }
  });

  it("returns the same frozen band object, so a consumer cannot corrupt other rows", () => {
    const a = riskBand(10, true);
    const b = riskBand(50, true);
    expect(a).not.toBe(b);
    // Both draws of the low band (risk 10 and risk 11) are the one
    // module-level object, so a consumer cannot swap one row's band out
    // from under another's.
    expect(riskBand(11, true)).toBe(a);
    expect(Object.isFrozen(a)).toBe(true);
    expect(Object.isFrozen(b)).toBe(true);
  });
});

describe("outcomeMeaning", () => {
  it("gives every outcome key a non-empty, distinct explanation", () => {
    const keys = ["changes_requested", "advisory", "in_progress", "failed", "clean"] as const;
    const meanings = keys.map(outcomeMeaning);
    for (const meaning of meanings) {
      expect(meaning.length).toBeGreaterThan(0);
    }
    expect(new Set(meanings).size).toBe(keys.length);
  });
});

describe("scoredOutcome", () => {
  const base: TriageInput = { risk_score: 10, findings: [] };

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

  /**
   * The `shouldFix === 0` half of the clean condition. Without it, risk 20
   * with three medium findings sorted into the LOW band and was labelled
   * "Great to Merge" — the redesign shipped that for a day, and no other test
   * caught it because no fixture had low risk *with* findings. This is the
   * regression test for that exact shape.
   */
  it("never calls a low-risk review clean while it still carries medium findings", () => {
    const outcome = scoredOutcome({
      risk_score: 20,
      findings: [finding("medium"), finding("medium"), finding("medium")],
    });
    expect(outcome.key).toBe("advisory");
    expect(outcome.label).toBe("Review Advised");
    expect(outcome.label).not.toBe("Great to Merge");
  });

  it("treats low-only and nit findings as non-blocking for the clean verdict", () => {
    expect(
      scoredOutcome({
        risk_score: RISK_MODERATE - 1,
        findings: [finding("low"), finding("nit")],
      }).key
    ).toBe("clean");
  });

  it("treats mixed-case severities as blockers, not unknown ones", () => {
    expect(blockerCounts([finding("Critical"), finding("HIGH"), finding("Medium")])).toEqual({
      mustFix: 2,
      shouldFix: 1,
    });
    expect(scoredOutcome({ risk_score: 5, findings: [finding("HIGH")] }).key).toBe(
      "changes_requested"
    );
    expect(scoredOutcome({ risk_score: 5, findings: [finding("Medium")] }).key).toBe("advisory");
  });

  it("clamps out-of-range risk before classifying", () => {
    expect(scoredOutcome({ risk_score: -40, findings: [] }).key).toBe("clean");
    expect(scoredOutcome({ risk_score: -40, findings: [finding("medium")] }).key).toBe(
      "advisory"
    );
    expect(scoredOutcome({ risk_score: 400, findings: [] }).key).toBe("changes_requested");
    expect(scoredOutcome({ risk_score: 400, findings: [] }).band.key).toBe("critical");
  });

  it("carries the vocabulary meaning, not a per-row copy of it", () => {
    expect(scoredOutcome({ risk_score: 90, findings: [] }).meaning).toBe(
      outcomeMeaning("changes_requested")
    );
    expect(pendingOutcome().meaning).toBe(outcomeMeaning("in_progress"));
    expect(FAILED_OUTCOME.meaning).toBe(outcomeMeaning("failed"));
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

describe("clusterFailures", () => {
  /**
   * `failureSignature` is module-private now — it has no production consumer,
   * so these exercise it through the only entry point that uses it.
   */
  it("drops the parenthesised tail so repeated failures collapse to one cause", () => {
    const clusters = clusterFailures([
      { failure_reason: "All 9 free-tier models exhausted (18 total attempts)" },
      { failure_reason: "All 9 free-tier models exhausted (11 total attempts)" },
    ]);
    expect(clusters).toHaveLength(1);
    expect(clusters[0].signature).toBe("All 9 free-tier models exhausted");
    expect(clusters[0].count).toBe(2);
  });

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
  });

  it("keeps distinct causes distinct", () => {
    const clusters = clusterFailures([
      { failure_reason: "GitHub 404: repository not found" },
      { failure_reason: "All 9 free-tier models exhausted" },
    ]);
    expect(clusters).toHaveLength(2);
    expect(clusters[0].signature).not.toBe(clusters[1].signature);
  });

  it("never returns an empty signature for a blank or missing reason", () => {
    const clusters = clusterFailures([
      { failure_reason: "" },
      { failure_reason: null },
      { failure_reason: undefined },
    ]);
    for (const cluster of clusters) {
      expect(cluster.signature.length).toBeGreaterThan(0);
    }
    expect(clusters).toHaveLength(1);
    expect(clusters[0].signature).toBe("No failure reason recorded");
  });

  it("handles unicode and very long reason text without throwing", () => {
    const clusters = clusterFailures([
      { failure_reason: "\u00e9chec d\u00e9pendance \u2014 \u4e2d\u6587\u9519\u8bef (attempt 1)" },
      { failure_reason: "\u00e9chec d\u00e9pendance \u2014 \u4e2d\u6587\u9519\u8bef (attempt 2)" },
      { failure_reason: "x".repeat(5000) },
    ]);
    expect(clusters).toHaveLength(2);
    expect(clusters[0].count).toBe(2);
    expect(clusters[0].signature).toContain("\u4e2d\u6587\u9519\u8bef");
  });

  it("emits a count-1 cluster; suppressing single-cause banners is the banner's job", () => {
    const clusters = clusterFailures([{ failure_reason: "one-off flake (retry 1)" }]);
    expect(clusters).toEqual([{ signature: "one-off flake", count: 1 }]);
  });

  it("returns nothing for an empty list", () => {
    expect(clusterFailures([])).toEqual([]);
  });
});

describe("sortByAttention", () => {
  interface Row extends TriageInput {
    id: string;
    status: string;
    created_at: string;
  }

  const row = (
    id: string,
    risk_score: number,
    status: string,
    findings: { severity: string }[] = [],
    created_at = "2026-01-01T00:00:00.000Z"
  ): Row => ({ id, risk_score, status, findings, created_at });

  // Returns a full Outcome, so the sort contract is type-checked here instead
  // of being silenced with `as never`.
  const outcomeOf = (r: Row): Outcome =>
    r.status === "error"
      ? FAILED_OUTCOME
      : r.status === "pending"
        ? pendingOutcome(r.status)
        : scoredOutcome(r);

  it("sinks failures below real findings so repeated errors cannot bury signal", () => {
    const sorted = sortByAttention(
      [
        row("fail", 0, "error"),
        row("clean", 5, "completed"),
        row("blocker", 90, "completed", [finding("critical")]),
      ],
      outcomeOf,
      "attention"
    );
    expect(sorted.map((r) => r.id)).toEqual(["blocker", "clean", "fail"]);
  });

  /**
   * Both rows must land in the SAME band for the risk tiebreak to run at all.
   * The old fixture used risk 40 (advisory) and risk 95 (changes_requested),
   * so the rank check short-circuited first and the tiebreak was dead code
   * under test. Risk 40 vs 45 are both advisory, which forces it to execute.
   *
   * The ids sort AGAINST the expected order on purpose: "worst-risk" (45) is
   * the row that must come first but sorts last by id, so deleting the risk
   * tiebreak would produce ["best-risk", "worst-risk"] and fail here on its
   * own, rather than only failing in the paired test below.
   */
  it("orders by risk within one band, not just by band", () => {
    const rows = [row("worst-risk", 45, "completed"), row("best-risk", 40, "completed")];
    expect(outcomeOf(rows[0]).rank).toBe(outcomeOf(rows[1]).rank);
    expect(sortByAttention(rows, outcomeOf, "attention").map((r) => r.id)).toEqual([
      "worst-risk",
      "best-risk",
    ]);
  });

  it("orders the changes-requested band by risk too, when ranks tie", () => {
    const rows = [
      row("milder", 71, "completed"),
      row("worst", 99, "completed"),
    ];
    expect(outcomeOf(rows[0]).rank).toBe(outcomeOf(rows[1]).rank);
    expect(sortByAttention(rows, outcomeOf, "attention").map((r) => r.id)).toEqual([
      "worst",
      "milder",
    ]);
  });

  it("falls back to newest-first when sorted by newest", () => {
    const sorted = sortByAttention(
      [
        row("old", 5, "completed", [], "2026-01-01T00:00:00.000Z"),
        row("new", 90, "completed", [], "2026-06-01T00:00:00.000Z"),
      ],
      outcomeOf,
      "newest"
    );
    expect(sorted.map((r) => r.id)).toEqual(["new", "old"]);
  });

  /**
   * Every fixture used to share one created_at, so the date tiebreak was never
   * observed. Distinct timestamps within one band must sort newest first.
   */
  it("breaks rank ties by recency, so the date tiebreak actually runs", () => {
    const sorted = sortByAttention(
      [
        row("older", 40, "completed", [], "2026-01-01T00:00:00.000Z"),
        row("newer", 40, "completed", [], "2026-05-01T00:00:00.000Z"),
      ],
      outcomeOf,
      "attention"
    );
    expect(sorted.map((r) => r.id)).toEqual(["newer", "older"]);
  });

  it("sinks an unparseable timestamp instead of turning the comparator into NaN", () => {
    // Built literally, not through row(): row()'s default parameter would
    // substitute a valid date for an explicit `undefined` and hide the case.
    const unparseable: Row = {
      id: "broken",
      risk_score: 40,
      status: "completed",
      findings: [],
      created_at: "not-a-date",
    };
    const missing: Row = {
      id: "missing",
      risk_score: 40,
      status: "completed",
      findings: [],
      created_at: undefined as unknown as string,
    };
    const good = row("good", 40, "completed", [], "2026-01-01T00:00:00.000Z");

    const sorted = sortByAttention([unparseable, good, missing], outcomeOf, "newest");
    // NaN would coerce to 0 and leave the order untouched; -Infinity sinks
    // both undated rows below the one dateable review.
    expect(sorted.map((r) => r.id)).toEqual(["good", "broken", "missing"]);
  });

  it("is a total order: equal rank and equal time still sort by id", () => {
    const rows = [row("b", 40, "completed"), row("a", 40, "completed")];
    const forward = sortByAttention(rows, outcomeOf, "attention").map((r) => r.id);
    const reversed = sortByAttention([...rows].reverse(), outcomeOf, "attention").map((r) => r.id);
    expect(forward).toEqual(["a", "b"]);
    expect(reversed).toEqual(forward);
  });

  it("does not mutate its input", () => {
    const input = [row("a", 5, "completed"), row("b", 90, "completed")];
    const copy = [...input];
    sortByAttention(input, outcomeOf, "attention");
    expect(input).toEqual(copy);
  });
});
