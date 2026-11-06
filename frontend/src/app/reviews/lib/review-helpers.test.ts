import { describe, it, expect, beforeEach, afterEach } from "vitest";
import {
  isReviewFailed,
  isAnalysisFailed,
  isPublishFailed,
  isReviewPending,
  isSuppressed,
  hasCompletedAnalysis,
  isSuppressedUnscored,
  pendingLabelFor,
  getBlastStats,
  blastRadiusSummary,
  stagedFixKey,
  readStagedFixes,
  isFixStaged,
  toggleStagedFix,
  clearStagedFixes,
  computeFindingStableKey,
  fnv1a32,
} from "./review-helpers";
import type { CodeReviewOut, ReviewFindingOut } from "../types";

function mockReview(overrides: Partial<CodeReviewOut> = {}): CodeReviewOut {
  return {
    id: "rev-1",
    repo_id: "repo-1",
    repo_owner: "octocat",
    repo_name: "spoon-knife",
    commit_sha: "abcdef123456",
    risk_score: 25,
    summary: "Routine refactor looks solid.",
    findings: [],
    status: "completed",
    input_tokens: 1200,
    output_tokens: 450,
    created_at: new Date().toISOString(),
    ...overrides,
  };
}

describe("review-helpers honesty predicates", () => {
  it("detects failed reviews", () => {
    expect(isReviewFailed(mockReview({ status: "error" }))).toBe(true);
    expect(isReviewFailed(mockReview({ status: "completed" }))).toBe(false);
  });

  it("distinguishes publish failures from true analysis failures", () => {
    // True analysis failure (no tokens, no findings, no score)
    const analysisFailure = mockReview({
      status: "error",
      risk_score: 0,
      findings: [],
      input_tokens: 0,
      output_tokens: 0,
      failure_reason: "Model rate limit exceeded",
    });
    expect(isPublishFailed(analysisFailure)).toBe(false);
    expect(isAnalysisFailed(analysisFailure)).toBe(true);

    // Publish failure with saved analysis findings & tokens
    const publishFailure = mockReview({
      status: "error",
      risk_score: 80,
      findings: [
        {
          file_path: "src/auth.ts",
          line_start: 10,
          line_end: 12,
          category: "security",
          severity: "critical",
          critique: "SQL injection flaw",
        },
      ],
      input_tokens: 500,
      output_tokens: 200,
      failure_reason: "Failed to publish PR review: GitHub 502 Bad Gateway",
    });
    expect(isPublishFailed(publishFailure)).toBe(true);
    expect(isAnalysisFailed(publishFailure)).toBe(false);
  });

  it("detects pending / in_progress reviews", () => {
    expect(isReviewPending(mockReview({ status: "pending" }))).toBe(true);
    expect(isReviewPending(mockReview({ status: "in_progress" }))).toBe(true);
    expect(isReviewPending(mockReview({ status: "completed" }))).toBe(false);
    expect(isReviewPending(mockReview({ status: "error" }))).toBe(false);
    expect(isReviewPending(mockReview({ status: "suppressed" }))).toBe(false);

    expect(pendingLabelFor(mockReview({ status: "pending" }))).toBe("Queued");
    expect(pendingLabelFor(mockReview({ status: "in_progress" }))).toBe("Analyzing");
  });

  it("handles suppressed status with and without saved audit", () => {
    const scoredSuppressed = mockReview({
      status: "suppressed",
      summary: "Completed audit with comments disabled.",
      input_tokens: 100,
      output_tokens: 50,
    });
    expect(isSuppressed(scoredSuppressed)).toBe(true);
    expect(hasCompletedAnalysis(scoredSuppressed)).toBe(true);
    expect(isSuppressedUnscored(scoredSuppressed)).toBe(false);

    const unscoredSuppressed = mockReview({
      status: "suppressed",
      summary: "Autonomous code review queued.",
      input_tokens: 0,
      output_tokens: 0,
    });
    expect(isSuppressed(unscoredSuppressed)).toBe(true);
    expect(hasCompletedAnalysis(unscoredSuppressed)).toBe(false);
    expect(isSuppressedUnscored(unscoredSuppressed)).toBe(true);
  });
});

describe("blast stats calculations", () => {
  it("computes targeted blast stats correctly", () => {
    const review = mockReview({
      risk_score: 15,
      diff_stats: { files_changed: 2, additions: 15, deletions: 5 },
    });
    const stats = getBlastStats(review);
    expect(stats.scope).toBe("Targeted");
    expect(stats.filesCount).toBe(2);
    expect(stats.additions).toBe(15);
    expect(stats.deletions).toBe(5);

    const summary = blastRadiusSummary(review);
    expect(summary.label).toBe("Isolated");
    expect(summary.explanation).toContain("2 files, +15/-5");
  });

  it("computes extensive blast stats driven specifically by high risk score", () => {
    // Small diff volume, but critical risk score >= 71
    const review = mockReview({
      risk_score: 85,
      diff_stats: { files_changed: 1, additions: 5, deletions: 2 },
    });
    const stats = getBlastStats(review);
    expect(stats.scope).toBe("Extensive");
    expect(stats.filesCount).toBe(1);
    expect(stats.additions + stats.deletions).toBe(7);
  });

  it("computes extensive blast stats driven specifically by high volume modification lines", () => {
    // Low risk score (20), but total modifications > 250
    const review = mockReview({
      risk_score: 20,
      diff_stats: { files_changed: 3, additions: 200, deletions: 120 },
    });
    const stats = getBlastStats(review);
    expect(stats.scope).toBe("Extensive");
    expect(stats.filesCount).toBe(3);
    expect(stats.additions + stats.deletions).toBe(320);
  });

  it("computes extensive blast stats driven specifically by wide file count", () => {
    // Low risk score and low modifications, but files_changed > 8
    const review = mockReview({
      risk_score: 10,
      diff_stats: { files_changed: 10, additions: 10, deletions: 5 },
    });
    const stats = getBlastStats(review);
    expect(stats.scope).toBe("Extensive");
    expect(stats.filesCount).toBe(10);
  });
});

describe("staged fix localStorage persistence", () => {
  const sampleFinding: ReviewFindingOut = {
    file_path: "src/index.ts",
    line_start: 14,
    line_end: 20,
    category: "logic",
    severity: "high",
    critique: "Off-by-one error",
    suggested_patch: "@@ -14,7 +14,7 @@\n-for (let i = 0; i <= n; i++)\n+for (let i = 0; i < n; i++)",
  };

  const scope = {
    repoOwner: "test-owner",
    repoName: "test-repo",
    commitSha: "c0ffee123",
  };

  beforeEach(() => {
    clearStagedFixes();
  });

  afterEach(() => {
    clearStagedFixes();
  });

  it("generates deterministic FNV-1a hash key", () => {
    const key1 = stagedFixKey(sampleFinding, scope);
    const key2 = stagedFixKey(sampleFinding, scope);
    expect(key1).toBe(key2);
    expect(key1).toContain("test-owner/test-repo@c0ffee123:src/index.ts:14-20");
  });

  it("toggles staging state accurately", () => {
    expect(isFixStaged(sampleFinding, scope)).toBe(false);

    // Stage
    const next1 = toggleStagedFix(sampleFinding, scope);
    expect(next1).toBe(true);
    expect(isFixStaged(sampleFinding, scope)).toBe(true);
    const stored = readStagedFixes();
    const key = stagedFixKey(sampleFinding, scope);
    expect(stored[key]).toBe(sampleFinding.suggested_patch);

    // Unstage
    const next2 = toggleStagedFix(sampleFinding, scope);
    expect(next2).toBe(false);
    expect(isFixStaged(sampleFinding, scope)).toBe(false);
  });

  it("computes deterministic finding stable keys", () => {
    // Pin fnv1a32 with known literals to ensure hashing implementation remains invariant
    expect(fnv1a32("")).toBe(0x811c9dc5);
    expect(fnv1a32("Off-by-one error").toString(16)).toBe("4621ca93");

    const findingWithoutPatch: ReviewFindingOut = {
      file_path: "src/index.ts",
      line_start: 14,
      line_end: 20,
      category: "logic",
      severity: "high",
      critique: "Off-by-one error",
    };

    const key1 = computeFindingStableKey(findingWithoutPatch, "rev-123");
    expect(key1).toBe("rev-123:logic:src/index.ts:14-20:4621ca93");

    const key2 = computeFindingStableKey({
      ...findingWithoutPatch,
      symbol_name: "calculateTax",
    });
    expect(key2).toBe("logic:src/index.ts:14-20:calculateTax:4621ca93");

    // Distinguishes findings when suggested patch differs
    const f1: ReviewFindingOut = {
      ...findingWithoutPatch,
      suggested_patch: "diff1",
    };
    const f2: ReviewFindingOut = {
      ...findingWithoutPatch,
      suggested_patch: "diff2",
    };
    expect(computeFindingStableKey(f1)).not.toBe(computeFindingStableKey(f2));
    expect(computeFindingStableKey(f1)).not.toBe(computeFindingStableKey(findingWithoutPatch));

    // Distinguishes findings at same file and lines without patches when critiques differ
    const fNoPatchA: ReviewFindingOut = {
      file_path: "src/index.ts",
      line_start: 14,
      line_end: 20,
      category: "logic",
      severity: "high",
      critique: "First issue description",
    };
    const fNoPatchB: ReviewFindingOut = {
      file_path: "src/index.ts",
      line_start: 14,
      line_end: 20,
      category: "logic",
      severity: "high",
      critique: "Second distinct issue description",
    };
    expect(computeFindingStableKey(fNoPatchA)).not.toBe(
      computeFindingStableKey(fNoPatchB)
    );
  });
});
