"use client";

import { useEffect, useState } from "react";
import { api, AssociatedPROut } from "@/lib/api";
import type { CodeReviewOut } from "../types";

/**
 * On-demand associated-PR lookup for push rows (`pr_number` NULL at rest).
 * Rows that belong to a PR (squash-merged, fast-forwarded) resolve to the PR
 * so the card can show `PR #n` instead of `Push @ sha`.
 *
 * Module-level cache keyed by review id: back/forward + re-renders never
 * refetch, and one page render resolves every row with a SINGLE
 * `POST /reviews/associated-prs` call (issue #116) — never one GET
 * per row. Row hooks queue into `batchQueue` and one microtask flush sends
 * every queued id at once (see `flushBatchPrLookups`). Keyed by id — NOT by
 * commit sha — because the lookup itself is review-scoped: the same sha can
 * appear in two repos/forks and must never share one entry. Resolves null
 * (no PR / token-missing / error) silently — the row keeps rendering
 * `Push @ sha` with no skeleton and no layout shift beyond the pill text
 * swap. Only rendered rows fetch; rows that already carry `pr_number` never
 * fetch. A failed batch leaves the cache untouched so a remount retries;
 * list Refresh / detail Retry evict only the failed (null) entries (see
 * clearFailedAssociatedPrCache) so misses genuinely retry while resolved
 * PRs are kept, not refetched. A per-id request generation keeps a stale
 * (pre-Refresh) completion from overwriting a newer result.
 */
export const associatedPrCache = new Map<string, AssociatedPROut | null>();
export const associatedPrInflight = new Map<
  string,
  Promise<AssociatedPROut | null>
>();

let batchQueue: Array<{
  id: string;
  resolve: (pr: AssociatedPROut | null) => void;
  generation: number;
}> = [];
let batchScheduled = false;
let currentGeneration = 0;
const prRequestGeneration = new Map<string, number>();

/**
 * Drop every cached + in-flight associated-PR entry. Only the test reset
 * uses the full clear (suite isolation); product paths use
 * clearFailedAssociatedPrCache below so resolved PRs survive a Refresh.
 */
export function clearAssociatedPrCache(): void {
  associatedPrCache.clear();
  associatedPrInflight.clear();
  prRequestGeneration.clear();
  batchQueue = [];
  batchScheduled = false;
}

/**
 * Drop only failed (null) cached entries + in-flight lookups. Called by the
 * list Refresh and detail Retry paths: null misses genuinely retry on the
 * fresh rows' batch fetch, while success entries are kept so resolved
 * `PR #n` pills never refetch.
 */
export function clearFailedAssociatedPrCache(): void {
  for (const [key, value] of associatedPrCache) {
    if (value == null) {
      associatedPrCache.delete(key);
      prRequestGeneration.delete(key);
    }
  }
  associatedPrInflight.clear();
  batchQueue = [];
  batchScheduled = false;
}

/** Test-only reset for the module-level associated-PR cache (vitest). */
export function __resetAssociatedPrCacheForTests(): void {
  clearAssociatedPrCache();
}

async function flushBatchPrLookups(): Promise<void> {
  const queue = batchQueue;
  batchQueue = [];
  batchScheduled = false;

  if (queue.length === 0) return;

  const ids = Array.from(new Set(queue.map((item) => item.id)));

  let batchFailed = false;
  const batchMap = new Map<string, AssociatedPROut | null>();
  const hasBatchFn = typeof api.getAssociatedPrsBatch === "function";

  if (hasBatchFn) {
    try {
      const res = await api.getAssociatedPrsBatch(ids);
      if (res && Array.isArray(res.items)) {
        for (const item of res.items) {
          batchMap.set(item.review_id, item.associated_pr ?? null);
        }
      }
    } catch {
      batchFailed = true;
    }
  }

  if (batchFailed) {
    // When batch fails, do NOT fan out to individual requests (avoid amplifying backend load).
    // Mark queued items as null without caching the transient failure so remount/revisit retries.
    for (const item of queue) {
      item.resolve(null);
    }
    return;
  }

  for (const item of queue) {
    if (batchMap.has(item.id)) {
      const pr = batchMap.get(item.id) ?? null;
      if (prRequestGeneration.get(item.id) === item.generation) {
        associatedPrCache.set(item.id, pr);
      }
      item.resolve(pr);
    } else if (typeof api.getAssociatedPr === "function") {
      api.getAssociatedPr(item.id).then(
        (res) => {
          const pr = res?.associated_pr ?? null;
          if (prRequestGeneration.get(item.id) === item.generation) {
            associatedPrCache.set(item.id, pr);
          }
          item.resolve(pr);
        },
        () => {
          // Transient failure: leave uncached so revisiting or remounting can retry
          item.resolve(null);
        }
      );
    } else {
      if (prRequestGeneration.get(item.id) === item.generation) {
        associatedPrCache.set(item.id, null);
      }
      item.resolve(null);
    }
  }
}

function requestAssociatedPr(id: string): Promise<AssociatedPROut | null> {
  const gen = ++currentGeneration;
  prRequestGeneration.set(id, gen);
  return new Promise((resolve) => {
    batchQueue.push({ id, resolve, generation: gen });
    if (!batchScheduled) {
      batchScheduled = true;
      queueMicrotask(flushBatchPrLookups);
    }
  });
}

/**
 * Hook to retrieve the associated pull request for a given review.
 */
export function useAssociatedPr(review: Partial<CodeReviewOut>): AssociatedPROut | null {
  const [, setResolved] = useState<AssociatedPROut | null>(
    () => (review?.id ? associatedPrCache.get(review.id) ?? null : null)
  );

  useEffect(() => {
    if (!review?.id) return;
    if (review.pr_number != null) return;
    const sha = review.commit_sha;
    if (!sha) return;
    const key = review.id;
    const cached = associatedPrCache.get(key);
    if (cached !== undefined) {
      setResolved(cached);
      return;
    }
    let cancelled = false;
    let pending = associatedPrInflight.get(key);
    if (!pending) {
      const task = requestAssociatedPr(review.id);
      pending = task.finally(() => {
        // Only clear our own entry: a Refresh/retry may have replaced it.
        if (associatedPrInflight.get(key) === pending) {
          associatedPrInflight.delete(key);
        }
      });
      associatedPrInflight.set(key, pending);
    }
    pending.then((pr) => {
      if (!cancelled) setResolved(pr);
    });
    return () => {
      cancelled = true;
    };
  }, [review]);

  if (!review?.id || review.pr_number != null) return null;
  return associatedPrCache.get(review.id) ?? null;
}

/**
 * Hook to check whether a review is PR-associated (either directly via pr_number
 * or asynchronously resolved via associated-PR lookup).
 */
export function useIsPrAssociated(review: CodeReviewOut): boolean {
  const associated = useAssociatedPr(review);
  return review.pr_number != null || associated != null;
}
