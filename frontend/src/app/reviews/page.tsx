"use client";

import React, { useState, useEffect, useCallback, useMemo, useRef } from "react";
import {
  ArrowLeft,
  RefreshCw,
  Inbox,
  ShieldCheck,
  X,
} from "lucide-react";
import { AppLayout } from "@/components/layout/app-layout";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { api, RepoOut } from "@/lib/api";

import type { CodeReviewOut, ReviewRibbonTab } from "./types";
import { PAGE_SIZE } from "./types";
import {
  isAnalysisFailed,
  isPublishFailed,
  isReviewPending,
  isSuppressedUnscored,
  isReviewFailed,
  RISK_CRITICAL,
} from "./lib/review-helpers";
import {
  Outcome,
  OutcomeKey,
  SortKey,
  clusterFailures,
  sortByAttention,
  scoredOutcome,
  pendingOutcome,
  FAILED_OUTCOME,
  SUPPRESSED_UNSCORED_OUTCOME,
} from "./review-triage";
import { useReviewPolling } from "./hooks/use-review-polling";
import {
  clearAssociatedPrCache,
  clearFailedAssociatedPrCache,
  refreshAssociatedPrs,
  __resetAssociatedPrCacheForTests,
} from "./hooks/use-associated-prs";

// Modular Presentation Components
import {
  TelemetryRibbon,
  FilterToolbar,
  TriageChips,
  FailureClusterBanner,
  ReviewCard,
  ReviewDetail,
  ROW_SURFACE,
} from "./components";

// Re-export cache helpers so unit tests maintain 100% backward compatibility
export {
  clearAssociatedPrCache,
  clearFailedAssociatedPrCache,
  refreshAssociatedPrs,
  __resetAssociatedPrCacheForTests,
};

function readReviewParam(): string | null {
  if (typeof window === "undefined") return null;
  const params = new URLSearchParams(window.location.search);
  const id = params.get("review");
  return id && id.trim() !== "" ? id.trim() : null;
}

/**
 * Responsive desktop breakpoint check.
 * Defaults to false (mobile/tablet fallback) during SSR and in standard JSDOM test runs.
 */
function useIsDesktop(): boolean {
  const [isDesktop, setIsDesktop] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const check = () => {
      if (window.matchMedia) {
        setIsDesktop(window.matchMedia("(min-width: 1280px)").matches);
      } else {
        setIsDesktop(window.innerWidth >= 1280);
      }
    };
    check();
    window.addEventListener("resize", check);
    return () => window.removeEventListener("resize", check);
  }, []);

  return isDesktop;
}

export default function ReviewsPage() {
  const [reviews, setReviews] = useState<CodeReviewOut[]>([]);
  const [repos, setRepos] = useState<RepoOut[]>([]);
  const [selectedRepoId, setSelectedRepoId] = useState<string>("all");
  const [selectedSeverity, setSelectedSeverity] = useState<string>("all");
  const [highRiskOnly, setHighRiskOnly] = useState<boolean>(false);
  const [hideFailed, setHideFailed] = useState<boolean>(false);
  const [sortKey, setSortKey] = useState<SortKey>("attention");
  const [activeOutcome, setActiveOutcome] = useState<OutcomeKey | null>(null);
  const [activeTab, setActiveTab] = useState<ReviewRibbonTab>("all");
  const [searchQuery, setSearchQuery] = useState<string>("");
  const [loading, setLoading] = useState<boolean>(true);
  const [refreshing, setRefreshing] = useState<boolean>(false);
  const [page, setPage] = useState<number>(0);
  const [totalCount, setTotalCount] = useState<number>(0);
  const [fetchError, setFetchError] = useState<string | null>(null);

  // Detail view state
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detailReview, setDetailReview] = useState<CodeReviewOut | null>(null);
  const [detailLoading, setDetailLoading] = useState<boolean>(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailRetry, setDetailRetry] = useState<number>(0);
  const [listLoaded, setListLoaded] = useState<boolean>(false);

  // Desktop split layout detection
  const isDesktop = useIsDesktop();

  // Load repositories for filter selector
  useEffect(() => {
    api.getRepos()
      .then((data) => setRepos(data || []))
      .catch((err) => console.error("Failed to load repos:", err));
  }, []);

  const fetchReviews = useCallback(async () => {
    try {
      setLoading(true);
      const params: {
        limit: number;
        offset: number;
        repo_id?: string;
        min_risk?: number;
        severity?: string;
      } = {
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      };

      if (selectedRepoId !== "all") {
        params.repo_id = selectedRepoId;
      }
      if (highRiskOnly) {
        params.min_risk = RISK_CRITICAL;
      }
      if (selectedSeverity !== "all") {
        params.severity = selectedSeverity;
      }

      const res = await api.getCodeReviews(params);
      setReviews(res.reviews || []);
      setTotalCount(res.total || 0);
      setFetchError(null);
    } catch (err) {
      console.error("Failed to fetch code reviews:", err);
      setFetchError("Couldn't load code reviews. Check your connection and retry.");
    } finally {
      setLoading(false);
      setRefreshing(false);
      setListLoaded(true);
    }
  }, [selectedRepoId, highRiskOnly, selectedSeverity, page]);

  useEffect(() => {
    fetchReviews();
  }, [fetchReviews]);

  // Adaptive live polling (4.5s) for any pending or in-progress analysis
  const { isLiveSyncing } = useReviewPolling({
    reviews,
    onRefresh: fetchReviews,
    intervalMs: 4500,
  });

  // Clamp page index when total count shrinks
  const maxPage = Math.max(0, Math.ceil(totalCount / PAGE_SIZE) - 1);
  useEffect(() => {
    if (page > maxPage) setPage(maxPage);
  }, [page, maxPage]);

  // Adopt deep-linked review parameter from URL on client mount
  useEffect(() => {
    setSelectedId(readReviewParam());
  }, []);

  const openReview = useCallback((id: string) => {
    window.history.pushState({ review: id }, "", `${window.location.pathname}?review=${encodeURIComponent(id)}`);
    setDetailError(null);
    setSelectedId(id);
  }, []);

  const closeReview = useCallback(() => {
    window.history.replaceState({}, "", window.location.pathname);
    setDetailError(null);
    setSelectedId(null);
  }, []);

  // Back/forward browser history sync
  useEffect(() => {
    const onPopState = () => setSelectedId(readReviewParam());
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  // Keyboard accessibility: Escape key closes the detail view
  useEffect(() => {
    if (!selectedId) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeReview();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectedId, closeReview]);

  // Focus management: move focus to Back when detail opens, back to list on close
  const backButtonRef = useRef<HTMLButtonElement>(null);
  const listTopRef = useRef<HTMLDivElement>(null);
  const prevSelectedRef = useRef<string | null>(null);
  useEffect(() => {
    if (selectedId) {
      backButtonRef.current?.focus();
    } else if (prevSelectedRef.current) {
      listTopRef.current?.focus();
    }
    prevSelectedRef.current = selectedId;
  }, [selectedId]);

  // Resolve detail review payload
  const detailFetchKeyRef = useRef<string | null>(null);
  useEffect(() => {
    if (!selectedId) {
      setDetailReview(null);
      setDetailError(null);
      setDetailLoading(false);
      detailFetchKeyRef.current = null;
      return;
    }
    const found = reviews.find((r) => r.id === selectedId);
    if (found) {
      setDetailReview(found);
      setDetailError(null);
      setDetailLoading(false);
      detailFetchKeyRef.current = null;
      return;
    }
    if (!listLoaded) return;
    const fetchKey = `${selectedId}::${detailRetry}`;
    if (detailFetchKeyRef.current === fetchKey) return;
    detailFetchKeyRef.current = fetchKey;
    let cancelled = false;
    setDetailLoading(true);
    setDetailError(null);
    api.getReviewDetail(selectedId).then(
      (r) => {
        if (!cancelled) {
          setDetailReview(r);
          setDetailLoading(false);
        }
      },
      () => {
        if (!cancelled) {
          setDetailReview(null);
          setDetailError("Couldn't load that review. It may have moved pages — go back and retry.");
          setDetailLoading(false);
        }
      }
    );
    return () => {
      cancelled = true;
    };
  }, [selectedId, reviews, listLoaded, detailRetry]);

  const handleRefresh = useCallback(() => {
    clearFailedAssociatedPrCache();
    setRefreshing(true);
    fetchReviews();
  }, [fetchReviews]);

  const handleDetailRetry = useCallback(() => {
    clearFailedAssociatedPrCache();
    setRefreshing(true);
    fetchReviews();
    setDetailRetry((n) => n + 1);
  }, [fetchReviews]);

  // Client-side text search & ribbon tab filtering
  const filteredReviews = useMemo(() => {
    let list = reviews;
    if (hideFailed) {
      list = list.filter((r) => !isReviewFailed(r));
    }
    if (activeTab === "patches") {
      list = list.filter((r) => r.findings.some((f) => !!f.suggested_patch));
    }
    if (!searchQuery.trim()) return list;
    const q = searchQuery.toLowerCase();
    return list.filter((r) => {
      const matchSummary = r.summary?.toLowerCase().includes(q);
      const matchFailure = r.failure_reason?.toLowerCase().includes(q);
      const matchCommit = r.commit_sha?.toLowerCase().includes(q);
      const matchRepo = (r.repo_owner && r.repo_name) ? `${r.repo_owner}/${r.repo_name}`.toLowerCase().includes(q) : false;
      const matchPr = r.pr_title?.toLowerCase().includes(q) || (r.pr_author && r.pr_author.toLowerCase().includes(q));
      const matchFindings = r.findings.some(
        (f) =>
          f.file_path.toLowerCase().includes(q) ||
          f.critique.toLowerCase().includes(q) ||
          (f.symbol_name && f.symbol_name.toLowerCase().includes(q))
      );
      return matchSummary || matchFailure || matchCommit || matchRepo || matchPr || matchFindings;
    });
  }, [reviews, searchQuery, activeTab, hideFailed]);

  const outcomeOf = useCallback(
    (review: CodeReviewOut): Outcome => {
      if (isAnalysisFailed(review)) return FAILED_OUTCOME;
      if (isSuppressedUnscored(review)) return SUPPRESSED_UNSCORED_OUTCOME;
      if (isReviewPending(review)) return pendingOutcome(review.status);
      return scoredOutcome({ risk_score: review.risk_score, findings: review.findings });
    },
    []
  );

  const triageCounts = useMemo(() => {
    const counts: Record<OutcomeKey, number> = {
      changes_requested: 0,
      advisory: 0,
      clean: 0,
      in_progress: 0,
      failed: 0,
      suppressed: 0,
    };
    for (const review of filteredReviews) {
      counts[outcomeOf(review).key] += 1;
    }
    return counts;
  }, [filteredReviews, outcomeOf]);

  const sortedReviews = useMemo(() => {
    const filtered = activeOutcome
      ? filteredReviews.filter((r) => outcomeOf(r).key === activeOutcome)
      : filteredReviews;
    return sortByAttention(filtered, outcomeOf, sortKey);
  }, [filteredReviews, activeOutcome, outcomeOf, sortKey]);

  const failureClusters = useMemo(
    () => clusterFailures(sortedReviews.filter((r) => isAnalysisFailed(r))),
    [sortedReviews]
  );

  const pageStats = useMemo(() => {
    const highRisk = reviews.filter((r) => r.risk_score >= RISK_CRITICAL).length;
    const scored = reviews.filter((r) => {
      const s = r.status?.toLowerCase();
      if (s === "completed") return true;
      if (s === "suppressed") return !isSuppressedUnscored(r);
      if (s === "error") return isPublishFailed(r);
      return false;
    });
    const avgScore = scored.length > 0 ? Math.round(scored.reduce((acc, r) => acc + r.risk_score, 0) / scored.length) : 0;
    const totalPatches = reviews.reduce((acc, r) => acc + r.findings.filter((f) => !!f.suggested_patch).length, 0);
    return {
      totalCount,
      highRiskCount: highRisk,
      avgScore,
      scoredCount: scored.length,
      totalPatches,
    };
  }, [reviews, totalCount]);

  const repoOptions = useMemo(
    () => [
      { value: "all", label: "All Repositories" },
      ...repos.map((r) => ({
        value: r.id,
        label: `${r.owner}/${r.name}`,
      })),
    ],
    [repos]
  );

  const failedCount = useMemo(() => reviews.filter((r) => isReviewFailed(r)).length, [reviews]);

  const filtersActive =
    selectedRepoId !== "all" ||
    highRiskOnly ||
    hideFailed ||
    activeTab !== "all" ||
    selectedSeverity !== "all" ||
    searchQuery !== "" ||
    activeOutcome !== null;

  const handleClearFilters = useCallback(() => {
    setSelectedRepoId("all");
    setSelectedSeverity("all");
    setHighRiskOnly(false);
    setHideFailed(false);
    setActiveTab("all");
    setSearchQuery("");
    setActiveOutcome(null);
    setPage(0);
  }, []);

  const handleTabChange = useCallback((tab: ReviewRibbonTab) => {
    setActiveTab(tab);
    if (tab === "high_risk") {
      setHighRiskOnly(true);
    } else {
      setHighRiskOnly(false);
    }
    setPage(0);
  }, []);

  // Shared detail pane renderer
  const renderDetailPane = (isSplitDesktop = false) => (
    <div aria-live="polite" className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <Button
          variant="outline"
          size="sm"
          onClick={closeReview}
          ref={backButtonRef}
          aria-label={isSplitDesktop ? "Close inspector" : "Back to reviews"}
          className="h-8 gap-1.5 border-zinc-700 bg-zinc-900 text-zinc-300 hover:bg-zinc-800 active:translate-y-[0.5px]"
        >
          {isSplitDesktop ? (
            <>
              <X className="h-3.5 w-3.5" />
              <span>Close inspector</span>
            </>
          ) : (
            <>
              <ArrowLeft className="h-3.5 w-3.5" />
              <span>Back to reviews</span>
            </>
          )}
        </Button>
        {isSplitDesktop && (
          <div className="border border-zinc-800 bg-zinc-900/80 px-2.5 py-1 text-[11px] font-mono text-zinc-300 rounded-md flex items-center gap-1.5 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]">
            <span className="h-1.5 w-1.5 rounded-full bg-amber-400" aria-hidden="true" />
            <span>Detail Inspector</span>
          </div>
        )}
      </div>

      {fetchError && (
        <div className="flex items-center justify-between gap-3 rounded-xl border border-rose-500/40 bg-rose-950/30 px-4 py-2.5 text-xs font-mono text-rose-300">
          <span role="alert">Refresh failed: {fetchError}</span>
          <Button
            variant="outline"
            size="sm"
            onClick={handleDetailRetry}
            className="h-7 border-rose-500/40 text-xs text-rose-200 hover:bg-rose-900/40"
          >
            Retry
          </Button>
        </div>
      )}

      {detailLoading || (!detailReview && !detailError) ? (
        <div className="space-y-4" role="status" aria-label="Loading review detail">
          <Skeleton className="h-8 w-64" />
          <div className="rounded-xl border border-zinc-800/80 bg-[#111114] p-5 space-y-3">
            <Skeleton className="h-5 w-48" />
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-3/4" />
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
            {[1, 2, 3, 4].map((n) => (
              <Skeleton key={n} className="h-20 w-full" />
            ))}
          </div>
        </div>
      ) : detailError || !detailReview ? (
        <div className="flex items-center justify-between gap-3 rounded-xl border border-rose-500/40 bg-rose-950/30 px-4 py-2.5 text-xs font-mono text-rose-300">
          <span role="alert">{detailError ?? "Review not found."}</span>
          <Button
            variant="outline"
            size="sm"
            onClick={handleDetailRetry}
            className="h-7 border-rose-500/40 text-xs text-rose-200 hover:bg-rose-900/40"
          >
            Retry
          </Button>
        </div>
      ) : (
        <ReviewDetail
          key={detailReview.id}
          review={detailReview}
          onRetry={handleDetailRetry}
        />
      )}
    </div>
  );

  // Shared reviews list stream renderer
  const renderReviewsStream = () => (
    <>
      {loading && reviews.length === 0 ? (
        <div className="space-y-4">
          {[1, 2, 3].map((n) => (
            <div key={n} className={cn(ROW_SURFACE, "space-y-4 p-5")}>
              <div className="flex flex-col gap-5 md:flex-row md:items-start">
                <Skeleton className="h-16 w-16 rounded-full md:min-w-[84px]" />
                <div className="flex-1 space-y-3">
                  <div className="flex items-center gap-2.5">
                    <Skeleton className="h-4 w-48" />
                    <Skeleton className="h-4 w-24 rounded-full" />
                    <Skeleton className="h-4 w-16 rounded-full" />
                  </div>
                  <Skeleton className="h-4 w-3/5" />
                  <Skeleton className="h-3 w-full" />
                  <Skeleton className="h-3 w-2/3" />
                  <Skeleton className="h-3 w-1/3" />
                </div>
                <div className="space-y-2 md:w-[164px]">
                  <Skeleton className="h-7 w-full" />
                  <Skeleton className="h-7 w-full" />
                </div>
              </div>
            </div>
          ))}
        </div>
      ) : sortedReviews.length === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-zinc-800 bg-[#0c0c0e] py-16 text-center space-y-3">
          <div className="flex h-12 w-12 items-center justify-center rounded-full bg-zinc-900 border border-zinc-800 text-zinc-500">
            {filtersActive ? (
              <Inbox className="h-6 w-6 text-zinc-500" />
            ) : (
              <ShieldCheck className="h-6 w-6 text-zinc-500" />
            )}
          </div>
          <div className="space-y-1">
            <h3 className="text-sm font-semibold text-zinc-200">
              {filtersActive ? "Nothing matches these filters" : "No Code Reviews Found"}
            </h3>
            <p className="text-xs text-zinc-400 max-w-sm">
              {filtersActive
                ? "No review matches the current filter combination. Widen or clear the filters to see the rest."
                : "Push commits or open a pull request on your connected repositories to trigger Haunter's Sentinel."}
            </p>
          </div>
          {filtersActive && (
            <Button
              variant="outline"
              size="sm"
              onClick={handleClearFilters}
              className="mt-2 text-xs border-zinc-700 hover:bg-zinc-800 text-zinc-300"
            >
              Clear Filters
            </Button>
          )}
        </div>
      ) : (
        <div className="space-y-5">
          {sortedReviews.map((review) => (
            <ReviewCard
              key={review.id}
              review={review}
              onOpen={openReview}
              onRetry={handleRefresh}
              isSelected={selectedId === review.id}
              className={
                selectedId === review.id
                  ? "border-l-4 border-l-amber-500 bg-zinc-900/60 shadow-[0_4px_24px_rgba(0,0,0,0.5)]"
                  : undefined
              }
            />
          ))}
        </div>
      )}

      {/* Pagination controls */}
      {totalCount > PAGE_SIZE && (
        <div className="flex items-center justify-between pt-4 border-t border-zinc-800 text-xs font-mono text-zinc-500">
          <div>
            Showing {page * PAGE_SIZE + 1} to {Math.min((page + 1) * PAGE_SIZE, totalCount)} of {totalCount} reviews
          </div>
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={page === 0}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              className="h-8 border-zinc-800 text-xs"
            >
              Previous
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={(page + 1) * PAGE_SIZE >= totalCount}
              onClick={() => setPage((p) => p + 1)}
              className="h-8 border-zinc-800 text-xs"
            >
              Next
            </Button>
          </div>
        </div>
      )}
    </>
  );

  return (
    <AppLayout
      title="Code Reviews"
      subtitle="Autonomous push-level code review & actionable remediation sentinel"
      actions={
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={handleRefresh}
            disabled={refreshing || loading}
            className="h-8 gap-1.5 border-zinc-700 bg-zinc-900 text-zinc-300 hover:bg-zinc-800"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", (refreshing || loading) && "animate-spin")} />
            <span>Refresh</span>
          </Button>
        </div>
      }
    >
      <div className="space-y-6 pb-12">
        {/* Selected review active: render desktop split pane on large screens, mobile fallback on narrow */}
        {selectedId ? (
          isDesktop ? (
            /* Master-Detail Split Pane on Desktop */
            <div className="xl:grid xl:grid-cols-12 xl:gap-6 xl:h-[calc(100vh-140px)] xl:overflow-hidden items-start">
              {/* Left Pane: Master List */}
              <div className="xl:col-span-5 xl:h-full xl:overflow-y-auto xl:pr-2 space-y-4">
                <FilterToolbar
                  searchQuery={searchQuery}
                  onSearchChange={setSearchQuery}
                  selectedRepoId={selectedRepoId}
                  onRepoChange={(repoId) => {
                    setSelectedRepoId(repoId);
                    setPage(0);
                  }}
                  repoOptions={repoOptions}
                  selectedSeverity={selectedSeverity}
                  onSeverityChange={(sev) => {
                    setSelectedSeverity(sev);
                    setPage(0);
                  }}
                  highRiskOnly={highRiskOnly}
                  onToggleHighRisk={() => {
                    const nextVal = !highRiskOnly;
                    setHighRiskOnly(nextVal);
                    setActiveTab(nextVal ? "high_risk" : "all");
                    setPage(0);
                  }}
                  hideFailed={hideFailed}
                  onToggleHideFailed={() => setHideFailed((v) => !v)}
                  failedCount={failedCount}
                  sortKey={sortKey}
                  onSortChange={(k) => {
                    setSortKey(k);
                    setPage(0);
                  }}
                  isLiveSyncing={isLiveSyncing}
                />
                {!loading && reviews.length > 0 && (
                  <TriageChips
                    counts={triageCounts}
                    activeOutcome={activeOutcome}
                    onToggle={(key) => setActiveOutcome(key)}
                  />
                )}
                {renderReviewsStream()}
              </div>

              {/* Right Pane: Detail Inspector */}
              <div className="xl:col-span-7 xl:h-full xl:overflow-y-auto xl:pl-1">
                {renderDetailPane(true)}
              </div>
            </div>
          ) : (
            /* Mobile / Tablet Responsive Fallback */
            renderDetailPane(false)
          )
        ) : (
          /* Normal List View (No Review Selected) */
          <>
            {/* Telemetry Ribbon */}
            <div ref={listTopRef} tabIndex={-1} className="outline-none">
              <TelemetryRibbon
                activeTab={activeTab}
                onTabChange={handleTabChange}
                stats={pageStats}
                highRiskOnly={highRiskOnly}
              />
            </div>

            {/* Filter Toolbar */}
            <FilterToolbar
              searchQuery={searchQuery}
              onSearchChange={setSearchQuery}
              selectedRepoId={selectedRepoId}
              onRepoChange={(repoId) => {
                setSelectedRepoId(repoId);
                setPage(0);
              }}
              repoOptions={repoOptions}
              selectedSeverity={selectedSeverity}
              onSeverityChange={(sev) => {
                setSelectedSeverity(sev);
                setPage(0);
              }}
              highRiskOnly={highRiskOnly}
              onToggleHighRisk={() => {
                const nextVal = !highRiskOnly;
                setHighRiskOnly(nextVal);
                setActiveTab(nextVal ? "high_risk" : "all");
                setPage(0);
              }}
              hideFailed={hideFailed}
              onToggleHideFailed={() => setHideFailed((v) => !v)}
              failedCount={failedCount}
              sortKey={sortKey}
              onSortChange={(k) => {
                setSortKey(k);
                setPage(0);
              }}
              isLiveSyncing={isLiveSyncing}
            />

            {/* Fetch error banner */}
            {fetchError && (
              <div className="flex items-center justify-between gap-3 rounded-xl border border-rose-500/40 bg-rose-950/30 px-4 py-2.5 text-xs font-mono text-rose-300">
                <span role="alert">{fetchError}</span>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleRefresh}
                  className="h-7 border-rose-500/40 text-xs text-rose-200 hover:bg-rose-900/40"
                >
                  Retry
                </Button>
              </div>
            )}

            {/* Triage summary chips */}
            {!loading && reviews.length > 0 && (
              <TriageChips
                counts={triageCounts}
                activeOutcome={activeOutcome}
                onToggle={(key) => setActiveOutcome(key)}
              />
            )}

            {/* Failure Cluster Banner */}
            {failureClusters.length > 0 && (
              <FailureClusterBanner clusters={failureClusters} />
            )}

            {/* Reviews stream + pagination */}
            {renderReviewsStream()}
          </>
        )}
      </div>
    </AppLayout>
  );
}
