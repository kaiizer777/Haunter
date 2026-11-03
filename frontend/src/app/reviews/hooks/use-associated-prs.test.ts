import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import {
  useAssociatedPr,
  useIsPrAssociated,
  clearAssociatedPrCache,
  associatedPrCache,
} from "./use-associated-prs";
import { api } from "@/lib/api";
import type { CodeReviewOut } from "../types";

function mockReview(overrides: Partial<CodeReviewOut> = {}): CodeReviewOut {
  return {
    id: "review-abc",
    repo_id: "repo-xyz",
    commit_sha: "789def",
    risk_score: 10,
    summary: "Mock summary",
    findings: [],
    status: "completed",
    input_tokens: 100,
    output_tokens: 50,
    created_at: new Date().toISOString(),
    ...overrides,
  };
}

describe("useAssociatedPr hook", () => {
  beforeEach(() => {
    clearAssociatedPrCache();
    vi.restoreAllMocks();
  });

  it("returns null immediately if review already has pr_number", () => {
    const review = mockReview({ pr_number: 42 });
    const { result } = renderHook(() => useAssociatedPr(review));
    expect(result.current).toBe(null);
  });

  it("batches and resolves associated PR for push reviews", async () => {
    const review = mockReview({ id: "rev-push-1", commit_sha: "sha-123", pr_number: null });

    vi.spyOn(api, "getAssociatedPrsBatch").mockResolvedValueOnce({
      items: [
        {
          review_id: "rev-push-1",
          associated_pr: {
            number: 99,
            url: "https://github.com/org/repo/pull/99",
            title: "Batched PR Title",
          },
        },
      ],
    });

    const { result } = renderHook(() => useAssociatedPr(review));

    await waitFor(() => {
      expect(result.current).not.toBeNull();
    });

    expect(result.current?.number).toBe(99);
    expect(result.current?.title).toBe("Batched PR Title");
    expect(associatedPrCache.get("rev-push-1")?.number).toBe(99);
  });

  it("indicates isPrAssociated correctly", async () => {
    const directPrReview = mockReview({ pr_number: 101 });
    const { result: directResult } = renderHook(() => useIsPrAssociated(directPrReview));
    expect(directResult.current).toBe(true);
  });
});
