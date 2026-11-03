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

  it("resets in-flight status in finally block even if cancelled mid-refresh", async () => {
    let resolveRefresh!: () => void;
    const slowRefresh = vi.fn().mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          resolveRefresh = resolve;
        })
    );
    const reviews = [mockReview("pending")];

    const { unmount } = renderHook(() =>
      useReviewPolling({ reviews, onRefresh: slowRefresh, intervalMs: 1000 })
    );

    // Trigger poll tick
    await act(async () => {
      vi.advanceTimersByTime(1000);
    });
    expect(slowRefresh).toHaveBeenCalledTimes(1);

    // Unmount while refresh is still in flight (isCancelled becomes true)
    unmount();

    // Now resolve the in-flight promise
    await act(async () => {
      resolveRefresh();
    });

    // Remount or mount new instance with pending reviews
    const nextRefresh = vi.fn().mockResolvedValue(undefined);
    renderHook(() =>
      useReviewPolling({ reviews, onRefresh: nextRefresh, intervalMs: 1000 })
    );

    // Advance timer: nextRefresh should execute because inFlightRef was reset to false
    await act(async () => {
      vi.advanceTimersByTime(1000);
    });
    expect(nextRefresh).toHaveBeenCalledTimes(1);
  });
});
