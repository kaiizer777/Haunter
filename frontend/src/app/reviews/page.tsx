"use client";

import { useEffect, useState, useCallback, useMemo } from "react";
import { AppLayout } from "@/components/layout/app-layout";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { SelectDropdown } from "@/components/ui/select-dropdown";
import {
  api,
  CodeReviewOut,
  RepoOut,
  ReviewFindingOut,
} from "@/lib/api";
import { formatRelativeTime, cn } from "@/lib/utils";
import {
  ShieldCheck,
  ShieldAlert,
  AlertTriangle,
  GitCommit,
  GitPullRequest,
  Search,
  RefreshCw,
  Copy,
  Check,
  ChevronDown,
  ChevronUp,
  Bug,
  Zap,
  Lock,
  Boxes,
  Sparkles,
  ArrowUpRight,
} from "lucide-react";

const PAGE_SIZE = 15;

/**
 * Radial tactile gauge for Risk Score (0-100).
 */
function RiskGauge({ score, size = 64 }: { score: number; size?: number }) {
  const strokeWidth = 5;
  const radius = (size - strokeWidth) / 2;
  const circumference = 2 * Math.PI * radius;
  const clampedScore = Math.max(0, Math.min(100, score));
  const offset = circumference - (clampedScore / 100) * circumference;

  let strokeColor = "#10b981"; // emerald
  let glowColor = "rgba(16, 185, 129, 0.3)";
  let textClass = "text-emerald-400";
  let label = "SAFE";

  if (clampedScore > 70) {
    strokeColor = "#f43f5e"; // rose
    glowColor = "rgba(244, 63, 94, 0.4)";
    textClass = "text-rose-400";
    label = "CRITICAL";
  } else if (clampedScore > 30) {
    strokeColor = "#f59e0b"; // amber
    glowColor = "rgba(245, 158, 11, 0.3)";
    textClass = "text-amber-400";
    label = "MODERATE";
  }

  return (
    <div className="flex flex-col items-center justify-center">
      <div className="relative flex items-center justify-center" style={{ width: size, height: size }}>
        <svg className="rotate-[-90deg]" width={size} height={size}>
          <circle
            cx={size / 2}
            cy={size / 2}
            r={radius}
            stroke="#27272a"
            strokeWidth={strokeWidth}
            fill="transparent"
          />
          <circle
            cx={size / 2}
            cy={size / 2}
            r={radius}
            stroke={strokeColor}
            strokeWidth={strokeWidth}
            fill="transparent"
            strokeDasharray={circumference}
            strokeDashoffset={offset}
            strokeLinecap="round"
            style={{
              transition: "stroke-dashoffset 0.6s cubic-bezier(0.16, 1, 0.3, 1)",
              filter: `drop-shadow(0 0 6px ${glowColor})`,
            }}
          />
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center">
          <span className={cn("font-mono text-sm font-bold tracking-tight", textClass)}>
            {clampedScore}
          </span>
        </div>
      </div>
      <span className={cn("mt-1.5 text-[10px] font-mono font-semibold tracking-wider uppercase", textClass)}>
        {label}
      </span>
    </div>
  );
}

/**
 * Finding Category Icon & Color Helper
 */
function getCategoryMeta(category: string) {
  const cat = category.toLowerCase();
  switch (cat) {
    case "security":
      return {
        icon: Lock,
        color: "text-rose-400 border-rose-500/30 bg-rose-950/40",
        label: "Security",
      };
    case "logic":
      return {
        icon: Bug,
        color: "text-amber-400 border-amber-500/30 bg-amber-950/40",
        label: "Logic & Edge Cases",
      };
    case "performance":
      return {
        icon: Zap,
        color: "text-cyan-400 border-cyan-500/30 bg-cyan-950/40",
        label: "Performance",
      };
    case "api_compatibility":
      return {
        icon: Boxes,
        color: "text-purple-400 border-purple-500/30 bg-purple-950/40",
        label: "API Compatibility",
      };
    default:
      return {
        icon: AlertTriangle,
        color: "text-zinc-400 border-zinc-700 bg-zinc-900/60",
        label: category,
      };
  }
}

function getSeverityBadge(severity: string) {
  const sev = severity.toLowerCase();
  switch (sev) {
    case "critical":
      return (
        <span className="rounded-[4px] border border-red-500/40 bg-red-950/60 px-1.5 py-0.5 text-[10px] font-mono font-bold uppercase tracking-wider text-red-400 shadow-[0_0_8px_rgba(239,68,68,0.3)]">
          CRITICAL
        </span>
      );
    case "high":
      return (
        <span className="rounded-[4px] border border-amber-500/40 bg-amber-950/60 px-1.5 py-0.5 text-[10px] font-mono font-bold uppercase tracking-wider text-amber-400">
          HIGH
        </span>
      );
    case "medium":
      return (
        <span className="rounded-[4px] border border-yellow-500/30 bg-yellow-950/40 px-1.5 py-0.5 text-[10px] font-mono font-medium uppercase tracking-wider text-yellow-300">
          MEDIUM
        </span>
      );
    default:
      return (
        <span className="rounded-[4px] border border-zinc-700 bg-zinc-900/80 px-1.5 py-0.5 text-[10px] font-mono font-medium uppercase tracking-wider text-zinc-400">
          LOW
        </span>
      );
  }
}

/**
 * Finding Card component with GitHub suggestion block copy action
 */
function FindingItem({ finding }: { finding: ReviewFindingOut }) {
  const [copied, setCopied] = useState(false);
  const meta = getCategoryMeta(finding.category);
  const Icon = meta.icon;

  const handleCopy = () => {
    if (!finding.suggested_patch) return;
    navigator.clipboard.writeText(finding.suggested_patch);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="rounded-lg border border-zinc-800/80 bg-zinc-950/60 p-4 space-y-3 transition-colors hover:border-zinc-700/80">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className={cn("inline-flex items-center gap-1 rounded-[5px] border px-2 py-0.5 text-[11px] font-medium font-mono", meta.color)}>
            <Icon className="h-3 w-3" />
            {meta.label}
          </span>
          {getSeverityBadge(finding.severity)}
        </div>
        <div className="font-mono text-xs text-zinc-400 bg-zinc-900/80 px-2 py-0.5 rounded border border-zinc-800">
          {finding.file_path}:{finding.line_start}{finding.line_end !== finding.line_start ? `-${finding.line_end}` : ""}
        </div>
      </div>

      <p className="text-xs text-zinc-200 leading-relaxed">
        {finding.critique}
      </p>

      {finding.suggested_patch && (
        <div className="mt-2 space-y-1.5">
          <div className="flex items-center justify-between text-[11px] text-zinc-400 font-mono">
            <span className="text-zinc-500 uppercase tracking-wider text-[10px]">Candidate Remediation Patch</span>
            <Button
              variant="outline"
              size="sm"
              onClick={handleCopy}
              className="h-6 px-2 text-[11px] gap-1 font-mono border-zinc-700 hover:bg-zinc-800 text-zinc-300"
            >
              {copied ? (
                <>
                  <Check className="h-3 w-3 text-emerald-400" />
                  <span className="text-emerald-400">Copied</span>
                </>
              ) : (
                <>
                  <Copy className="h-3 w-3" />
                  <span>Copy Suggestion</span>
                </>
              )}
            </Button>
          </div>
          <pre className="overflow-x-auto rounded-md border border-zinc-800 bg-[#070709] p-3 text-[11px] font-mono text-emerald-300/90 leading-relaxed selection:bg-emerald-950">
            <code>{finding.suggested_patch.trim()}</code>
          </pre>
        </div>
      )}
    </div>
  );
}

/**
 * Main Review Card
 */
function ReviewCard({ review }: { review: CodeReviewOut }) {
  const [expanded, setExpanded] = useState(false);
  const repoName = review.repo_owner && review.repo_name ? `${review.repo_owner}/${review.repo_name}` : "Repository";

  // Category counts
  const categoryCounts = useMemo(() => {
    const counts = { security: 0, logic: 0, performance: 0, api_compatibility: 0 };
    for (const f of review.findings) {
      const cat = f.category.toLowerCase() as keyof typeof counts;
      if (counts[cat] !== undefined) {
        counts[cat]++;
      }
    }
    return counts;
  }, [review.findings]);

  const commitShort = review.commit_sha ? review.commit_sha.substring(0, 7) : "unknown";

  return (
    <div className="rounded-xl border border-zinc-800/90 bg-gradient-to-b from-[#111115] to-[#0c0c0e] p-5 shadow-[inset_0_1px_0_rgba(255,255,255,0.04),0_8px_20px_rgba(0,0,0,0.4)] transition-all hover:border-zinc-700/90">
      <div className="flex flex-col md:flex-row md:items-start justify-between gap-5">
        {/* Left: Gauge */}
        <div className="flex items-center md:flex-col justify-start md:justify-center gap-4 md:min-w-[80px]">
          <RiskGauge score={review.risk_score} size={64} />
        </div>

        {/* Center: Info & Findings overview */}
        <div className="flex-1 min-w-0 space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-semibold text-zinc-100 text-sm">{repoName}</span>
            <span className="text-zinc-600">•</span>
            {review.pr_number ? (
              <a
                href={`https://github.com/${repoName}/pull/${review.pr_number}`}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 rounded bg-zinc-800/80 px-2 py-0.5 text-xs font-mono font-medium text-amber-400 hover:text-amber-300 hover:bg-zinc-700/80 transition-colors"
              >
                <GitPullRequest className="h-3 w-3" />
                PR #{review.pr_number}
                <ArrowUpRight className="h-2.5 w-2.5 opacity-70" />
              </a>
            ) : (
              <span className="inline-flex items-center gap-1 rounded bg-zinc-800/80 px-2 py-0.5 text-xs font-mono text-zinc-300">
                <GitCommit className="h-3 w-3 text-zinc-400" />
                Push
              </span>
            )}

            <a
              href={`https://github.com/${repoName}/commit/${review.commit_sha}`}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 text-xs font-mono text-zinc-400 hover:text-zinc-200 transition-colors"
            >
              <GitCommit className="h-3 w-3" />
              {commitShort}
            </a>

            <span className="text-zinc-600">•</span>
            <span className="text-xs text-zinc-400 font-mono">
              {formatRelativeTime(review.created_at)}
            </span>
          </div>

          <p className="text-xs text-zinc-300 leading-relaxed font-sans">
            {review.summary}
          </p>

          {/* Finding Category Badges */}
          <div className="flex flex-wrap items-center gap-2 pt-1">
            {categoryCounts.security > 0 && (
              <span className="inline-flex items-center gap-1 rounded-full border border-rose-500/30 bg-rose-950/40 px-2 py-0.5 text-[10px] font-mono text-rose-300">
                <Lock className="h-2.5 w-2.5 text-rose-400" />
                {categoryCounts.security} Security
              </span>
            )}
            {categoryCounts.logic > 0 && (
              <span className="inline-flex items-center gap-1 rounded-full border border-amber-500/30 bg-amber-950/40 px-2 py-0.5 text-[10px] font-mono text-amber-300">
                <Bug className="h-2.5 w-2.5 text-amber-400" />
                {categoryCounts.logic} Logic
              </span>
            )}
            {categoryCounts.performance > 0 && (
              <span className="inline-flex items-center gap-1 rounded-full border border-cyan-500/30 bg-cyan-950/40 px-2 py-0.5 text-[10px] font-mono text-cyan-300">
                <Zap className="h-2.5 w-2.5 text-cyan-400" />
                {categoryCounts.performance} Perf
              </span>
            )}
            {categoryCounts.api_compatibility > 0 && (
              <span className="inline-flex items-center gap-1 rounded-full border border-purple-500/30 bg-purple-950/40 px-2 py-0.5 text-[10px] font-mono text-purple-300">
                <Boxes className="h-2.5 w-2.5 text-purple-400" />
                {categoryCounts.api_compatibility} API
              </span>
            )}
            {review.findings.length === 0 && (
              <span className="inline-flex items-center gap-1 rounded-full border border-emerald-500/30 bg-emerald-950/30 px-2 py-0.5 text-[10px] font-mono text-emerald-300">
                <Check className="h-2.5 w-2.5 text-emerald-400" />
                No regressions detected
              </span>
            )}
          </div>
        </div>

        {/* Right: Actions & Telemetry */}
        <div className="flex flex-row md:flex-col items-end justify-between gap-3 min-w-[120px]">
          <div className="text-right text-[11px] font-mono text-zinc-500 space-y-0.5">
            <div>{review.input_tokens + review.output_tokens} tok</div>
            <div className="text-zinc-600 text-[10px]">{review.status}</div>
          </div>

          {review.findings.length > 0 && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => setExpanded(!expanded)}
              className="h-7 px-2.5 text-xs gap-1.5 border-zinc-700 bg-zinc-900/80 hover:bg-zinc-800 text-zinc-200"
            >
              <span>{expanded ? "Collapse" : `Review (${review.findings.length})`}</span>
              {expanded ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
            </Button>
          )}
        </div>
      </div>

      {/* Accordion Findings */}
      {expanded && review.findings.length > 0 && (
        <div className="mt-5 pt-4 border-t border-zinc-800/80 space-y-3">
          <div className="flex items-center justify-between text-xs font-semibold text-zinc-400 uppercase tracking-wider font-mono">
            <span>Actionable Findings ({review.findings.length})</span>
            <span className="text-[10px] text-zinc-500 font-normal">Pre-verified candidate patches</span>
          </div>
          <div className="space-y-3">
            {review.findings.map((f, i) => (
              <FindingItem key={i} finding={f} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export default function ReviewsPage() {
  const [reviews, setReviews] = useState<CodeReviewOut[]>([]);
  const [repos, setRepos] = useState<RepoOut[]>([]);
  const [selectedRepoId, setSelectedRepoId] = useState<string>("all");
  const [selectedSeverity, setSelectedSeverity] = useState<string>("all");
  const [highRiskOnly, setHighRiskOnly] = useState<boolean>(false);
  const [searchQuery, setSearchQuery] = useState<string>("");
  const [loading, setLoading] = useState<boolean>(true);
  const [refreshing, setRefreshing] = useState<boolean>(false);
  const [page, setPage] = useState<number>(0);
  const [totalCount, setTotalCount] = useState<number>(0);

  // Load repos for filter
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
        params.min_risk = 50;
      }
      if (selectedSeverity !== "all") {
        params.severity = selectedSeverity;
      }

      const res = await api.getCodeReviews(params);
      setReviews(res.reviews || []);
      setTotalCount(res.total || 0);
    } catch (err) {
      console.error("Failed to fetch code reviews:", err);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [selectedRepoId, highRiskOnly, selectedSeverity, page]);

  useEffect(() => {
    fetchReviews();
  }, [fetchReviews]);

  const handleRefresh = () => {
    setRefreshing(true);
    fetchReviews();
  };

  // Client-side text search filtering
  const filteredReviews = useMemo(() => {
    if (!searchQuery.trim()) return reviews;
    const q = searchQuery.toLowerCase();
    return reviews.filter((r) => {
      const matchSummary = r.summary?.toLowerCase().includes(q);
      const matchCommit = r.commit_sha?.toLowerCase().includes(q);
      const matchRepo = (r.repo_owner && r.repo_name) ? `${r.repo_owner}/${r.repo_name}`.toLowerCase().includes(q) : false;
      const matchFindings = r.findings.some(
        (f) =>
          f.file_path.toLowerCase().includes(q) ||
          f.critique.toLowerCase().includes(q)
      );
      return matchSummary || matchCommit || matchRepo || matchFindings;
    });
  }, [reviews, searchQuery]);

  // Metric rollups
  const stats = useMemo(() => {
    const total = reviews.length;
    const highRisk = reviews.filter((r) => r.risk_score >= 71).length;
    const avgScore = total > 0 ? Math.round(reviews.reduce((acc, r) => acc + r.risk_score, 0) / total) : 0;
    const totalPatches = reviews.reduce((acc, r) => acc + r.findings.filter((f) => !!f.suggested_patch).length, 0);
    return { total, highRisk, avgScore, totalPatches };
  }, [reviews]);

  const repoOptions = [
    { value: "all", label: "All Repositories" },
    ...repos.map((r) => ({
      value: r.id,
      label: `${r.owner}/${r.name}`,
    })),
  ];

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
        {/* Metric Cards Row */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          <div className="rounded-xl border border-zinc-800 bg-[#111114] p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]">
            <div className="flex items-center justify-between text-zinc-400 text-xs font-mono">
              <span>REVIEWS SCANNED</span>
              <ShieldCheck className="h-4 w-4 text-emerald-400" />
            </div>
            <div className="mt-2 text-2xl font-bold font-mono text-zinc-100">{totalCount}</div>
            <div className="mt-1 text-[11px] text-zinc-500">Autonomous CI push & PR scans</div>
          </div>

          <div className="rounded-xl border border-zinc-800 bg-[#111114] p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]">
            <div className="flex items-center justify-between text-zinc-400 text-xs font-mono">
              <span>HIGH RISK FLAGGED</span>
              <ShieldAlert className="h-4 w-4 text-rose-400" />
            </div>
            <div className="mt-2 text-2xl font-bold font-mono text-rose-400">{stats.highRisk}</div>
            <div className="mt-1 text-[11px] text-zinc-500">Score &gt;= 71 (Changes Requested)</div>
          </div>

          <div className="rounded-xl border border-zinc-800 bg-[#111114] p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]">
            <div className="flex items-center justify-between text-zinc-400 text-xs font-mono">
              <span>AVG RISK SCORE</span>
              <Sparkles className="h-4 w-4 text-amber-400" />
            </div>
            <div className="mt-2 text-2xl font-bold font-mono text-zinc-100">{stats.avgScore} <span className="text-xs text-zinc-500 font-normal">/ 100</span></div>
            <div className="mt-1 text-[11px] text-zinc-500">Across current batch</div>
          </div>

          <div className="rounded-xl border border-zinc-800 bg-[#111114] p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]">
            <div className="flex items-center justify-between text-zinc-400 text-xs font-mono">
              <span>ACTIONABLE PATCHES</span>
              <Zap className="h-4 w-4 text-cyan-400" />
            </div>
            <div className="mt-2 text-2xl font-bold font-mono text-cyan-400">{stats.totalPatches}</div>
            <div className="mt-1 text-[11px] text-zinc-500">1-click GitHub suggestions ready</div>
          </div>
        </div>

        {/* Filter Controls Bar */}
        <div className="flex flex-col md:flex-row items-stretch md:items-center justify-between gap-3 rounded-xl border border-zinc-800/80 bg-[#0f0f12] p-3">
          <div className="flex flex-wrap items-center gap-2.5">
            <div className="w-[200px]">
              <SelectDropdown
                value={selectedRepoId}
                onChange={(val) => {
                  setSelectedRepoId(val);
                  setPage(0);
                }}
                options={repoOptions}
                placeholder="All Repositories"
              />
            </div>

            {/* Severity filter pills */}
            <div className="flex items-center rounded-lg border border-zinc-800 bg-zinc-950 p-1 text-xs">
              {["all", "critical", "high", "medium", "low"].map((sev) => (
                <button
                  key={sev}
                  onClick={() => {
                    setSelectedSeverity(sev);
                    setPage(0);
                  }}
                  className={cn(
                    "rounded-md px-2.5 py-1 font-mono uppercase text-[10px] tracking-wider transition-all",
                    selectedSeverity === sev
                      ? "bg-zinc-800 text-zinc-100 font-semibold shadow-[0_1px_3px_rgba(0,0,0,0.3)]"
                      : "text-zinc-500 hover:text-zinc-300"
                  )}
                >
                  {sev}
                </button>
              ))}
            </div>

            {/* High Risk score filter toggle (> 50) */}
            <button
              onClick={() => {
                setHighRiskOnly(!highRiskOnly);
                setPage(0);
              }}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-mono transition-all",
                highRiskOnly
                  ? "border-rose-500/50 bg-rose-950/40 text-rose-300 shadow-[0_0_12px_rgba(244,63,94,0.2)]"
                  : "border-zinc-800 bg-zinc-950 text-zinc-400 hover:border-zinc-700 hover:text-zinc-200"
              )}
            >
              <ShieldAlert className="h-3.5 w-3.5 text-rose-400" />
              <span>Risk &gt; 50</span>
            </button>
          </div>

          <div className="flex items-center gap-2">
            <div className="relative flex-1 md:w-64">
              <Search className="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-zinc-500" />
              <Input
                placeholder="Search findings, commits, files..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="pl-8 h-8 text-xs bg-zinc-950 border-zinc-800 focus:border-amber-400"
              />
            </div>
          </div>
        </div>

        {/* Review Cards Stream */}
        {loading && reviews.length === 0 ? (
          <div className="space-y-4">
            {[1, 2, 3].map((n) => (
              <div key={n} className="rounded-xl border border-zinc-800/80 bg-[#111114] p-5 space-y-3">
                <div className="flex items-center justify-between">
                  <Skeleton className="h-5 w-48" />
                  <Skeleton className="h-5 w-24" />
                </div>
                <Skeleton className="h-4 w-full" />
                <Skeleton className="h-4 w-3/4" />
              </div>
            ))}
          </div>
        ) : filteredReviews.length === 0 ? (
          <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-zinc-800 bg-[#0c0c0e] py-16 text-center space-y-3">
            <div className="flex h-12 w-12 items-center justify-center rounded-full bg-zinc-900 border border-zinc-800 text-zinc-500">
              <ShieldCheck className="h-6 w-6 text-zinc-600" />
            </div>
            <div className="space-y-1">
              <h3 className="text-sm font-semibold text-zinc-200">No Code Reviews Found</h3>
              <p className="text-xs text-zinc-500 max-w-sm">
                Push commits or open a pull request on your connected repositories to trigger Haunter&apos;s Sentinel.
              </p>
            </div>
            {(selectedRepoId !== "all" || highRiskOnly || selectedSeverity !== "all" || searchQuery) && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  setSelectedRepoId("all");
                  setSelectedSeverity("all");
                  setHighRiskOnly(false);
                  setSearchQuery("");
                  setPage(0);
                }}
                className="mt-2 text-xs border-zinc-700 hover:bg-zinc-800 text-zinc-300"
              >
                Clear Filters
              </Button>
            )}
          </div>
        ) : (
          <div className="space-y-4">
            {filteredReviews.map((review) => (
              <ReviewCard key={review.id} review={review} />
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
                onClick={() => setPage(page - 1)}
                className="h-8 border-zinc-800 text-xs"
              >
                Previous
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={(page + 1) * PAGE_SIZE >= totalCount}
                onClick={() => setPage(page + 1)}
                className="h-8 border-zinc-800 text-xs"
              >
                Next
              </Button>
            </div>
          </div>
        )}
      </div>
    </AppLayout>
  );
}
