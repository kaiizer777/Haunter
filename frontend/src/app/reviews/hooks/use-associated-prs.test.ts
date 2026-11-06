import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";
import {
  useAssociatedPr,
  useIsPrAssociated,
  clearAssociatedPrCache,
  refreshAssociatedPrs,
  clearFailedAssociatedPrCache,
  associatedPrCache,
  associatedPrInflight,
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

  it("resolves push review to associated PR", async () => {
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
    expect(result.current?.url).toBe("https://github.com/org/repo/pull/99");
    expect(associatedPrCache.get("rev-push-1")?.number).toBe(99);
  });

  it("resolves push reviews with pr_number: null to null when no associated PR is linked", async () => {
    const review = mockReview({ id: "rev-standalone-push", commit_sha: "sha-solo", pr_number: null });

    vi.spyOn(api, "getAssociatedPrsBatch").mockResolvedValueOnce({
      items: [
        {
          review_id: "rev-standalone-push",
          associated_pr: null,
        },
      ],
    });

    const { result } = renderHook(() => useAssociatedPr(review));

    await waitFor(() => {
      expect(associatedPrCache.has("rev-standalone-push")).toBe(true);
    });

    expect(result.current).toBeNull();
    expect(associatedPrCache.get("rev-standalone-push")).toBeNull();
  });

  it("indicates isPrAssociated correctly for direct PR and resolved associated PR", async () => {
    const directPrReview = mockReview({ pr_number: 101 });
    const { result: directResult } = renderHook(() => useIsPrAssociated(directPrReview));
    expect(directResult.current).toBe(true);

    vi.spyOn(api, "getAssociatedPrsBatch").mockResolvedValueOnce({
      items: [
        {
          review_id: "rev-push-linked",
          associated_pr: {
            number: 55,
            url: "https://github.com/org/repo/pull/55",
            title: "Linked PR",
          },
        },
      ],
    });

    const pushReview = mockReview({ id: "rev-push-linked", commit_sha: "sha-55", pr_number: null });
    const { result: pushResult } = renderHook(() => useIsPrAssociated(pushReview));

    await waitFor(() => {
      expect(pushResult.current).toBe(true);
    });
  });

  it("handles failed batch request without poisoning cache so remount retries", async () => {
    const review = mockReview({ id: "rev-transient-fail", commit_sha: "sha-err", pr_number: null });
    const batchSpy = vi.spyOn(api, "getAssociatedPrsBatch").mockRejectedValueOnce(
      new Error("503 Service Unavailable")
    );

    const { result, unmount } = renderHook(() => useAssociatedPr(review));

    await waitFor(() => {
      expect(batchSpy).toHaveBeenCalledWith(["rev-transient-fail"]);
    });

    // Returned null without caching the transient failure
    expect(result.current).toBeNull();
    expect(associatedPrCache.has("rev-transient-fail")).toBe(false);

    unmount();

    // Now on retry / remount, provide successful response
    batchSpy.mockResolvedValueOnce({
      items: [
        {
          review_id: "rev-transient-fail",
          associated_pr: {
            number: 404,
            url: "https://github.com/org/repo/pull/404",
            title: "Retried PR",
          },
        },
      ],
    });

    const { result: retryResult } = renderHook(() => useAssociatedPr(review));

    await waitFor(() => {
      expect(retryResult.current?.number).toBe(404);
    });
    expect(associatedPrCache.get("rev-transient-fail")?.number).toBe(404);
  });

  it("clears only failed (null) cache entries on clearFailedAssociatedPrCache while preserving resolved PRs", () => {
    associatedPrCache.set("rev-success", {
      number: 12,
      url: "https://github.com/org/repo/pull/12",
      title: "Success PR",
    });
    associatedPrCache.set("rev-failed-miss", null);

    clearFailedAssociatedPrCache();

    expect(associatedPrCache.has("rev-success")).toBe(true);
    expect(associatedPrCache.get("rev-success")?.number).toBe(12);
    expect(associatedPrCache.has("rev-failed-miss")).toBe(false);
  });

  it("directly caches null for omitted review IDs without individual GET fan-out", async () => {
    const review = mockReview({ id: "rev-omitted-1", commit_sha: "sha-omitted", pr_number: null });
    const getAssociatedPrSpy = vi.spyOn(api, "getAssociatedPr");
    const getBatchSpy = vi.spyOn(api, "getAssociatedPrsBatch").mockResolvedValueOnce({
      items: [], // review ID is omitted from batch
    });

    const { result } = renderHook(() => useAssociatedPr(review));

    await waitFor(() => {
      expect(getBatchSpy).toHaveBeenCalledWith(["rev-omitted-1"]);
    });

    // Wait microtask flush
    await waitFor(() => {
      expect(associatedPrCache.has("rev-omitted-1")).toBe(true);
    });

    expect(associatedPrCache.get("rev-omitted-1")).toBeNull();
    expect(result.current).toBeNull();
    // Crucial: no individual GET calls fired
    expect(getAssociatedPrSpy).not.toHaveBeenCalled();
  });

  it("bumps generation during refreshAssociatedPrs so late response from prior run does not poison cache", async () => {
    let resolveBatch!: (value: any) => void;
    const batchPromise = new Promise((resolve) => {
      resolveBatch = resolve;
    });

    vi.spyOn(api, "getAssociatedPrsBatch").mockImplementation(() => batchPromise as any);

    const review = mockReview({ id: "rev-late-1", commit_sha: "sha-late", pr_number: null });
    renderHook(() => useAssociatedPr(review));

    // Wait until in flight
    await waitFor(() => {
      expect(associatedPrInflight.has("rev-late-1")).toBe(true);
    });

    // Invalidate / refresh: bumps generation for in-flight keys before clearing
    act(() => {
      refreshAssociatedPrs();
    });

    // Now resolve late batch response
    await act(async () => {
      resolveBatch({
        items: [
          {
            review_id: "rev-late-1",
            associated_pr: {
              number: 777,
              url: "https://github.com/org/repo/pull/777",
              title: "Late PR",
            },
          },
        ],
      });
      await batchPromise;
    });

    // The late response must NOT have poisoned the cache because its generation was invalidated
    expect(associatedPrCache.get("rev-late-1")).toBeUndefined();
  });
});
