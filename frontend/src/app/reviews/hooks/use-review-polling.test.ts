import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useReviewPolling } from "./use-review-polling";
import type { CodeReviewOut } from "../types";

function mockReview(status: string): CodeReviewOut {
  return {
    id: "r-" + Math.random(),
    repo_id: "repo-1",
    commit_sha: "abcdef",
    risk_score: 10,
    summary: "Mock",
    findings: [],
    status,
    input_tokens: 0,
    output_tokens: 0,
    created_at: new Date().toISOString(),
  };
}

describe("useReviewPolling hook", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("does not poll when all reviews are terminal", () => {
    const onRefresh = vi.fn();
    const reviews = [mockReview("completed"), mockReview("error")];

    const { result } = renderHook(() =>
      useReviewPolling({ reviews, onRefresh, intervalMs: 2000 })
    );

    expect(result.current.hasActiveReviews).toBe(false);
    expect(result.current.isLiveSyncing).toBe(false);

    act(() => {
      vi.advanceTimersByTime(5000);
    });

    expect(onRefresh).not.toHaveBeenCalled();
  });

  it("polls periodically when pending or in_progress reviews exist", async () => {
    const onRefresh = vi.fn().mockResolvedValue(undefined);
    const reviews = [mockReview("completed"), mockReview("pending")];

    const { result } = renderHook(() =>
      useReviewPolling({ reviews, onRefresh, intervalMs: 3000 })
    );

    expect(result.current.hasActiveReviews).toBe(true);

    await act(async () => {
      vi.advanceTimersByTime(3000);
    });

    expect(onRefresh).toHaveBeenCalledTimes(1);

    await act(async () => {
      vi.advanceTimersByTime(3000);
    });

    expect(onRefresh).toHaveBeenCalledTimes(2);
  });
});
