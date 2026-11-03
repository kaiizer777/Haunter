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
    Object.defineProperty(document, "visibilityState", {
      value: "visible",
      writable: true,
      configurable: true,
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
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

  it("polls with default 4500ms interval timing when intervalMs is omitted", async () => {
    const onRefresh = vi.fn().mockResolvedValue(undefined);
    const reviews = [mockReview("pending")];

    const { result } = renderHook(() =>
      useReviewPolling({ reviews, onRefresh })
    );

    expect(result.current.hasActiveReviews).toBe(true);

    // Just before default interval
    await act(async () => {
      vi.advanceTimersByTime(4499);
    });
    expect(onRefresh).not.toHaveBeenCalled();

    // At 4500ms mark
    await act(async () => {
      vi.advanceTimersByTime(1);
    });
    expect(onRefresh).toHaveBeenCalledTimes(1);

    // Next tick at 9000ms
    await act(async () => {
      vi.advanceTimersByTime(4500);
    });
    expect(onRefresh).toHaveBeenCalledTimes(2);
  });

  it("prevents interval creation when enabled is false", async () => {
    const onRefresh = vi.fn().mockResolvedValue(undefined);
    const reviews = [mockReview("pending")];

    const { result } = renderHook(() =>
      useReviewPolling({ reviews, onRefresh, enabled: false, intervalMs: 2000 })
    );

    expect(result.current.hasActiveReviews).toBe(true);
    expect(result.current.isLiveSyncing).toBe(false);

    await act(async () => {
      vi.advanceTimersByTime(10000);
    });

    expect(onRefresh).not.toHaveBeenCalled();
  });

  it("blocks overlapping interval ticks during in-flight refresh", async () => {
    let resolveRefresh!: () => void;
    const slowRefresh = vi.fn().mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          resolveRefresh = resolve;
        })
    );
    const reviews = [mockReview("in_progress")];

    renderHook(() =>
      useReviewPolling({ reviews, onRefresh: slowRefresh, intervalMs: 1000 })
    );

    // First interval tick starts refresh
    await act(async () => {
      vi.advanceTimersByTime(1000);
    });
    expect(slowRefresh).toHaveBeenCalledTimes(1);

    // Advance through multiple intervals while slowRefresh is still pending
    await act(async () => {
      vi.advanceTimersByTime(3000);
    });
    // Should still be 1 call because in-flight refresh blocks overlapping ticks
    expect(slowRefresh).toHaveBeenCalledTimes(1);

    // Resolve in-flight request
    await act(async () => {
      resolveRefresh();
    });

    // Next tick after completion proceeds normally
    await act(async () => {
      vi.advanceTimersByTime(1000);
    });
    expect(slowRefresh).toHaveBeenCalledTimes(2);
  });

  it("reflects isLiveSyncing status during async refresh", async () => {
    let resolveRefresh!: () => void;
    const asyncRefresh = vi.fn().mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          resolveRefresh = resolve;
        })
    );
    const reviews = [mockReview("pending")];

    const { result } = renderHook(() =>
      useReviewPolling({ reviews, onRefresh: asyncRefresh, intervalMs: 1000 })
    );

    expect(result.current.isLiveSyncing).toBe(false);

    // Trigger tick
    await act(async () => {
      vi.advanceTimersByTime(1000);
    });

    expect(result.current.isLiveSyncing).toBe(true);

    // Resolve the async call
    await act(async () => {
      resolveRefresh();
    });

    expect(result.current.isLiveSyncing).toBe(false);
  });

  it("pauses polling when document.visibilityState is hidden and resumes on focus", async () => {
    const onRefresh = vi.fn().mockResolvedValue(undefined);
    const reviews = [mockReview("pending")];

    renderHook(() =>
      useReviewPolling({ reviews, onRefresh, intervalMs: 1000 })
    );

    // Initial tick runs
    await act(async () => {
      vi.advanceTimersByTime(1000);
    });
    expect(onRefresh).toHaveBeenCalledTimes(1);

    // Tab becomes hidden
    act(() => {
      Object.defineProperty(document, "visibilityState", {
        value: "hidden",
        writable: true,
        configurable: true,
      });
      document.dispatchEvent(new Event("visibilitychange"));
    });

    // Advance time while hidden: no polling should occur
    await act(async () => {
      vi.advanceTimersByTime(5000);
    });
    expect(onRefresh).toHaveBeenCalledTimes(1);

    // Tab becomes visible again
    await act(async () => {
      Object.defineProperty(document, "visibilityState", {
        value: "visible",
        writable: true,
        configurable: true,
      });
      document.dispatchEvent(new Event("visibilitychange"));
    });

    // Immediately executes poll upon focus
    expect(onRefresh).toHaveBeenCalledTimes(2);

    // Continues interval ticks
    await act(async () => {
      vi.advanceTimersByTime(1000);
    });
    expect(onRefresh).toHaveBeenCalledTimes(3);
  });

  it("cleans up polling interval and event listeners on unmount", async () => {
    const onRefresh = vi.fn().mockResolvedValue(undefined);
    const reviews = [mockReview("pending")];

    const { unmount } = renderHook(() =>
      useReviewPolling({ reviews, onRefresh, intervalMs: 1000 })
    );

    await act(async () => {
      vi.advanceTimersByTime(1000);
    });
    expect(onRefresh).toHaveBeenCalledTimes(1);

    unmount();

    // Advance time after unmount
    await act(async () => {
      vi.advanceTimersByTime(5000);
    });
    expect(onRefresh).toHaveBeenCalledTimes(1);

    // Visibility change after unmount should not trigger refresh
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
      vi.advanceTimersByTime(1000);
    });
    expect(onRefresh).toHaveBeenCalledTimes(1);
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
