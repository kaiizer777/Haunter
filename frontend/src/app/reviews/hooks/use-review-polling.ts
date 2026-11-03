"use client";

import { useEffect, useRef, useState } from "react";
import type { CodeReviewOut } from "../types";

export interface UseReviewPollingOptions {
  /** The current array of review items */
  reviews: CodeReviewOut[];
  /** Callback to trigger background refresh */
  onRefresh: () => Promise<void> | void;
  /** Polling interval in milliseconds (default: 4500ms) */
  intervalMs?: number;
  /** Whether polling is globally enabled (default: true) */
  enabled?: boolean;
}

export interface UseReviewPollingResult {
  /** True while a background polling request is in flight */
  isLiveSyncing: boolean;
  /** True if any reviews are currently pending or in-progress */
  hasActiveReviews: boolean;
}

/**
 * Adaptive live-polling hook for code reviews.
 *
 * When any review has status "pending" or "in_progress", sets up a background
 * polling interval (default 4.5s) to refresh the review list until all items
 * reach a terminal state.
 *
 * Automatically pauses when document.visibilityState is "hidden" and resumes
 * immediately when the user focuses the tab.
 */
export function useReviewPolling({
  reviews,
  onRefresh,
  intervalMs = 4500,
  enabled = true,
}: UseReviewPollingOptions): UseReviewPollingResult {
  const [isLiveSyncing, setIsLiveSyncing] = useState(false);
  const onRefreshRef = useRef(onRefresh);
  useEffect(() => {
    onRefreshRef.current = onRefresh;
  }, [onRefresh]);

  const inFlightRef = useRef(false);

  // Check if any review is currently active
  const hasActiveReviews = reviews.some((r) => {
    const s = r.status?.toLowerCase();
    return s === "pending" || s === "in_progress";
  });

  useEffect(() => {
    if (!enabled || !hasActiveReviews) {
      return;
    }

    let timerId: NodeJS.Timeout | null = null;
    let isCancelled = false;

    const executePoll = async () => {
      if (inFlightRef.current || isCancelled) return;
      if (typeof document !== "undefined" && document.visibilityState !== "visible") {
        return;
      }

      inFlightRef.current = true;
      setIsLiveSyncing(true);
      try {
        await onRefreshRef.current();
      } catch {
        // Polling errors are silent in the background
      } finally {
        if (!isCancelled) {
          inFlightRef.current = false;
          setIsLiveSyncing(false);
        }
      }
    };

    const startTimer = () => {
      if (timerId) clearInterval(timerId);
      timerId = setInterval(executePoll, intervalMs);
    };

    const stopTimer = () => {
      if (timerId) {
        clearInterval(timerId);
        timerId = null;
      }
    };

    const handleVisibilityChange = () => {
      if (document.visibilityState === "visible") {
        // Run an immediate poll upon returning to the tab if active reviews exist
        executePoll();
        startTimer();
      } else {
        stopTimer();
      }
    };

    // If visible on mount, start polling interval
    if (typeof document === "undefined" || document.visibilityState === "visible") {
      startTimer();
    }

    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", handleVisibilityChange);
    }

    return () => {
      isCancelled = true;
      stopTimer();
      if (typeof document !== "undefined") {
        document.removeEventListener("visibilitychange", handleVisibilityChange);
      }
    };
  }, [enabled, hasActiveReviews, intervalMs]);

  return {
    isLiveSyncing,
    hasActiveReviews,
  };
}
