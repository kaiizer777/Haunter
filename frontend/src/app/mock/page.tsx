"use client";

import { useState, useMemo, useRef, useEffect } from "react";
import Link from "next/link";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { StatusBadge } from "@/components/runs/status-badge";
import { cn } from "@/lib/utils";
import {
  Activity,
  GitBranch,
  GitCommit,
  GitPullRequest,
  Sparkles,
  Sliders,
  ShieldCheck,
  TerminalSquare,
  Clock,
  Search,
  Filter,
  RotateCcw,
  ArrowUpRight,
  Trash2,
  User as UserIcon,
  X,
  PlayCircle,
  Workflow,
  ChevronLeft,
  ChevronRight,
} from "lucide-react";

export interface MockRun {
  id: string;
  repo: string;
  head_branch: string;
  head_sha: string;
  status: "completed" | "fix_generation" | "pr_opened" | "error" | "fallback";
  trigger: "workflow_run" | "manual";
  model: string;
  created_at: string;
  duration: string;
  tokens: number;
  cost: number;
  pr_number?: number;
  pr_url?: string;
  diagnosis: string;
  error_message?: string;
  sandbox_status?: "passed" | "failed" | "running";
}

const INITIAL_MOCK_RUNS: MockRun[] = [
  {
    id: "run-h841a10",
    repo: "kaiizer777/UpGrade",
    head_branch: "haunter/fix-d741607b-1",
    head_sha: "d16631d",
    status: "completed",
    trigger: "workflow_run",
    model: "nemotron-3.5-lightning-free",
    created_at: "12m ago",
    duration: "48s",
    tokens: 4120,
    cost: 0.0028,
    pr_number: 14,
    pr_url: "https://github.com/kaiizer777/UpGrade/pull/14",
    diagnosis: "Fixed off-by-one pagination offset calculation in workout session history endpoint.",
    sandbox_status: "passed",
  },
  {
    id: "run-h841a09",
    repo: "kaiizer777/Haunter",
    head_branch: "feature/auth-guard",
    head_sha: "9af42b8",
    status: "fix_generation",
    trigger: "workflow_run",
    model: "nemotron-3.5-lightning-free",
    created_at: "24m ago",
    duration: "29s",
    tokens: 2850,
    cost: 0.0019,
    diagnosis: "Synthesizing minimal async JWT verification callback patch for AWS Lambda entrypoint...",
    sandbox_status: "running",
  },
  {
    id: "run-h841a08",
    repo: "kaiizer777/UpGrade",
    head_branch: "main",
    head_sha: "c4b120f",
    status: "pr_opened",
    trigger: "workflow_run",
    model: "nemotron-3.5-lightning-free",
    created_at: "45m ago",
    duration: "1m 12s",
    tokens: 6340,
    cost: 0.0042,
    pr_number: 13,
    pr_url: "https://github.com/kaiizer777/UpGrade/pull/13",
    diagnosis: "Resolved concurrent Alembic schema migration deadlocks during pytest teardown.",
    sandbox_status: "passed",
  },
  {
    id: "run-h841a07",
    repo: "kaiizer777/Haunter",
    head_branch: "fix/worker-payload-timeout",
    head_sha: "7e2a91c",
    status: "error",
    trigger: "manual",
    model: "nemotron-3.5-lightning-free",
    created_at: "1h ago",
    duration: "34s",
    tokens: 3100,
    cost: 0.0021,
    diagnosis: "CodeBuild sandbox runner exceeded execution timeout limit (30s). Verification aborted.",
    error_message: "Sandbox verification error: CodeBuild runner timed out during pytest collection phase.",
    sandbox_status: "failed",
  },
  {
    id: "run-h841a06",
    repo: "kaiizer777/UpGrade",
    head_branch: "haunter/fix-8c2910fa-2",
    head_sha: "e883a45",
    status: "completed",
    trigger: "workflow_run",
    model: "nemotron-3.5-lightning-free",
    created_at: "3h ago",
    duration: "52s",
    tokens: 4890,
    cost: 0.0033,
    pr_number: 12,
    pr_url: "https://github.com/kaiizer777/UpGrade/pull/12",
    diagnosis: "Patched SQLAlchemy 2.0 async session context manager connection leak under load.",
    sandbox_status: "passed",
  },
  {
    id: "run-h841a05",
    repo: "kaiizer777/Haunter",
    head_branch: "refactor/session-store",
    head_sha: "a310dc4",
    status: "fallback",
    trigger: "workflow_run",
    model: "nemotron-3.5-lightning-free",
    created_at: "5h ago",
    duration: "1m 40s",
    tokens: 7420,
    cost: 0.0051,
    diagnosis: "Model confidence threshold unmet (0.68 < 0.85). Posted comprehensive diagnosis comment to commit.",
    sandbox_status: "failed",
  },
  {
    id: "run-h841a04",
    repo: "kaiizer777/UpGrade",
    head_branch: "hotfix/cors-preflight",
    head_sha: "3fb8901",
    status: "completed",
    trigger: "manual",
    model: "nemotron-3.5-lightning-free",
    created_at: "1d ago",
    duration: "41s",
    tokens: 3600,
    cost: 0.0024,
    pr_number: 11,
    pr_url: "https://github.com/kaiizer777/UpGrade/pull/11",
    diagnosis: "Fixed CORS preflight 403 by configuring explicit allowed headers in FastAPI middleware.",
    sandbox_status: "passed",
  },
  {
    id: "run-h841a03",
    repo: "kaiizer777/Haunter",
    head_branch: "main",
    head_sha: "2cd81f5",
    status: "pr_opened",
    trigger: "workflow_run",
    model: "nemotron-3.5-lightning-free",
    created_at: "1d ago",
    duration: "1m 05s",
    tokens: 5210,
    cost: 0.0035,
    pr_number: 8,
    pr_url: "https://github.com/kaiizer777/Haunter/pull/8",
    diagnosis: "Corrected GitHub webhook HMAC SHA-256 signature verification byte decoding.",
    sandbox_status: "passed",
  },
  {
    id: "run-h841a02",
    repo: "kaiizer777/UpGrade",
    head_branch: "feat/model-eval-metrics",
    head_sha: "6b7d142",
    status: "error",
    trigger: "workflow_run",
    model: "nemotron-3.5-lightning-free",
    created_at: "2d ago",
    duration: "18s",
    tokens: 1950,
    cost: 0.0013,
    diagnosis: "Syntax error in CI workflow matrix configuration file: invalid YAML indentation.",
    error_message: "Workflow parse error: scanner found character that cannot start any token.",
    sandbox_status: "failed",
  },
  {
    id: "run-h841a01",
    repo: "kaiizer777/Haunter",
    head_branch: "haunter/fix-19cb4812-4",
    head_sha: "f4209ea",
    status: "completed",
    trigger: "workflow_run",
    model: "nemotron-3.5-lightning-free",
    created_at: "3d ago",
    duration: "57s",
    tokens: 4620,
    cost: 0.0031,
    pr_number: 7,
    pr_url: "https://github.com/kaiizer777/Haunter/pull/7",
    diagnosis: "Configured NullPool engine configuration for Neon pooled connection strings in Lambda.",
    sandbox_status: "passed",
  },
];

const HARDCODED_USER = {
  username: "kaiizer777",
  avatarUrl: "https://avatars.githubusercontent.com/u/108753243?v=4",
  role: "Lead Maintainer",
  isAdmin: true,
};

const HARDCODED_MODEL = "nemotron-3.5-lightning-free";

export default function MockRunsPage() {
  const [runs, setRuns] = useState<MockRun[]>(INITIAL_MOCK_RUNS);
  const [searchQuery, setSearchQuery] = useState("");
  const [selectedRepo, setSelectedRepo] = useState("");
  const [selectedStatus, setSelectedStatus] = useState("");
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [activeDetailRun, setActiveDetailRun] = useState<MockRun | null>(null);

  const headerCheckboxRef = useRef<HTMLInputElement | null>(null);

  // Available unique repos
  const repos = useMemo(() => {
    return Array.from(new Set(INITIAL_MOCK_RUNS.map((r) => r.repo)));
  }, []);

  // Filtered runs
  const filteredRuns = useMemo(() => {
    return runs.filter((run) => {
      if (selectedRepo && run.repo !== selectedRepo) return false;
      if (selectedStatus) {
        if (selectedStatus === "completed" && !(run.status === "completed" || run.status === "pr_opened")) {
          return false;
        }
        if (selectedStatus === "fix_generation" && run.status !== "fix_generation") return false;
        if (selectedStatus === "error" && run.status !== "error") return false;
        if (selectedStatus === "fallback" && run.status !== "fallback") return false;
      }
      if (searchQuery.trim()) {
        const q = searchQuery.toLowerCase();
        const matchBranch = run.head_branch.toLowerCase().includes(q);
        const matchSha = run.head_sha.toLowerCase().includes(q);
        const matchRepo = run.repo.toLowerCase().includes(q);
        const matchDiag = run.diagnosis.toLowerCase().includes(q);
        if (!matchBranch && !matchSha && !matchRepo && !matchDiag) return false;
      }
      return true;
    });
  }, [runs, selectedRepo, selectedStatus, searchQuery]);

  const hasActiveFilters = Boolean(searchQuery || selectedRepo || selectedStatus);

  const handleResetFilters = () => {
    setSearchQuery("");
    setSelectedRepo("");
    setSelectedStatus("");
  };

  // Selection handlers
  const displayedRunIds = filteredRuns.map((r) => r.id);
  const selectedDisplayedCount = filteredRuns.filter((r) => selectedIds.has(r.id)).length;
  const isAllSelected = filteredRuns.length > 0 && selectedDisplayedCount === filteredRuns.length;
  const isIndeterminate = selectedDisplayedCount > 0 && selectedDisplayedCount < filteredRuns.length;

  useEffect(() => {
    if (headerCheckboxRef.current) {
      headerCheckboxRef.current.indeterminate = isIndeterminate;
    }
  }, [isIndeterminate]);

  const handleToggleSelectAll = () => {
    if (isAllSelected) {
      setSelectedIds((prev) => {
        const next = new Set(prev);
        displayedRunIds.forEach((id) => next.delete(id));
        return next;
      });
    } else {
      setSelectedIds((prev) => {
        const next = new Set(prev);
        displayedRunIds.forEach((id) => next.add(id));
        return next;
      });
    }
  };

  const handleToggleRun = (runId: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(runId)) {
        next.delete(runId);
      } else {
        next.add(runId);
      }
      return next;
    });
  };

  const handleBatchDelete = () => {
    setRuns((prev) => prev.filter((r) => !selectedIds.has(r.id)));
    if (activeDetailRun && selectedIds.has(activeDetailRun.id)) {
      setActiveDetailRun(null);
    }
    setSelectedIds(new Set());
  };

  const handleResetMockData = () => {
    setRuns(INITIAL_MOCK_RUNS);
    setSelectedIds(new Set());
    handleResetFilters();
  };

  return (
    <div className="min-h-screen bg-[#09090b] text-zinc-100 flex flex-row">
      {/* ========================================================================= */}
      {/* 1. SIDEBAR SHELL                                                          */}
      {/* ========================================================================= */}
      <aside className="fixed inset-y-0 left-0 z-30 flex w-[264px] flex-col border-r border-zinc-800 bg-[#0c0c0e] select-none">
        {/* Brand Header */}
        <div className="flex h-16 items-center justify-between px-5 border-b border-zinc-800/80">
          <Link href="/mock" className="flex items-center gap-3 group">
            <div className="flex h-8 w-8 items-center justify-center rounded-[6px] bg-zinc-900 border border-zinc-700/70 text-amber-400 group-hover:border-amber-400/80 transition-colors">
              <TerminalSquare className="h-4.5 w-4.5" />
            </div>
            <div className="flex flex-col">
              <span className="text-sm font-bold tracking-wider text-zinc-100 uppercase">
                Haunter
              </span>
              <span className="text-[11px] font-mono text-zinc-500">
                Autonomous CI
              </span>
            </div>
          </Link>
          <span className="rounded-[5px] border border-zinc-800 bg-zinc-900 px-2 py-0.5 text-[11px] font-mono text-zinc-400">
            v1.0
          </span>
        </div>

        {/* Navigation Links */}
        <div className="flex-1 overflow-y-auto px-3.5 py-4 space-y-1.5">
          <div className="px-2.5 pb-2 text-[11px] font-semibold uppercase tracking-wider text-zinc-500 font-mono">
            Navigation
          </div>

          {/* Runs (Active in Preview) */}
          <Link
            href="/mock"
            className="flex items-center justify-between rounded-[6px] px-3 py-2 text-[13px] font-semibold transition-colors bg-zinc-800/90 text-zinc-100 border border-zinc-700/60"
          >
            <div className="flex items-center gap-3">
              <Activity className="h-4 w-4 text-amber-400" />
              <span>Runs</span>
            </div>
            <span className="rounded-[4px] border border-amber-500/30 bg-amber-950/40 px-1.5 py-0.5 text-[10px] font-mono text-amber-400">
              Mock
            </span>
          </Link>

          {/* Repositories */}
          <Link
            href="/repos"
            className="flex items-center justify-between rounded-[6px] px-3 py-2 text-[13px] font-medium transition-colors text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200"
          >
            <div className="flex items-center gap-3">
              <GitBranch className="h-4 w-4 text-zinc-400" />
              <span>Repositories</span>
            </div>
          </Link>

          {/* Eval Harness */}
          <Link
            href="/eval"
            className="flex items-center justify-between rounded-[6px] px-3 py-2 text-[13px] font-medium transition-colors text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200"
          >
            <div className="flex items-center gap-3">
              <Sparkles className="h-4 w-4 text-zinc-400" />
              <span>Eval Harness</span>
            </div>
            <span className="rounded-[4px] border border-zinc-800 bg-zinc-900/90 px-1.5 py-0.5 text-[10px] font-mono text-zinc-500">
              Admin
            </span>
          </Link>

          {/* Model Config */}
          <Link
            href="/config"
            className="flex items-center justify-between rounded-[6px] px-3 py-2 text-[13px] font-medium transition-colors text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200"
          >
            <div className="flex items-center gap-3">
              <Sliders className="h-4 w-4 text-zinc-400" />
              <span>Model Config</span>
            </div>
            <span className="rounded-[4px] border border-zinc-800 bg-zinc-900/90 px-1.5 py-0.5 text-[10px] font-mono text-zinc-500">
              Live
            </span>
          </Link>
        </div>

        {/* Footer Pipeline & Model Status */}
        <div className="p-3.5 border-t border-zinc-800/80 bg-[#09090b] space-y-2.5">
          <div className="rounded-[7px] border border-zinc-800/80 bg-[#121215] p-3.5">
            <div className="flex items-center justify-between text-xs">
              <div className="flex items-center gap-2 text-zinc-300">
                <span className="h-2 w-2 rounded-full bg-emerald-400 animate-pulse" />
                <span className="font-medium">Pipeline Live</span>
              </div>
              <ShieldCheck className="h-3.5 w-3.5 text-zinc-500" />
            </div>
            <div className="mt-2 flex items-center justify-between text-[11px] font-mono text-zinc-500">
              <span>Active Model</span>
              <span className="text-zinc-300 truncate max-w-[125px]" title={HARDCODED_MODEL}>
                nemotron-3.5
              </span>
            </div>
          </div>

          {/* Mock User Pill in Sidebar */}
          <div className="flex items-center gap-2.5 px-3 py-2.5 rounded-[7px] border border-zinc-800/60 bg-[#0e0e11]">
            <div className="relative h-7 w-7 rounded-full overflow-hidden border border-zinc-700 shrink-0 bg-zinc-800 flex items-center justify-center">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={HARDCODED_USER.avatarUrl}
                alt={HARDCODED_USER.username}
                className="h-full w-full object-cover"
                onError={(e) => {
                  (e.target as HTMLElement).style.display = "none";
                }}
              />
              <UserIcon className="h-4 w-4 text-zinc-400" />
            </div>
            <div className="flex flex-col min-w-0 flex-1">
              <span className="text-xs font-mono font-medium text-zinc-200 truncate">
                {HARDCODED_USER.username}
              </span>
              <span className="text-[10px] text-amber-400/90 font-mono">
                Hardcoded Dev
              </span>
            </div>
          </div>
        </div>
      </aside>

      {/* ========================================================================= */}
      {/* 2. MAIN CONTENT AREA (OFFSET BY SIDEBAR WIDTH 264px)                      */}
      {/* ========================================================================= */}
      <div className="flex-1 flex flex-col pl-[264px] min-h-screen">
        {/* Topbar */}
        <header className="sticky top-0 z-20 flex h-16 w-full items-center justify-between border-b border-zinc-800 bg-[#09090b]/90 px-6 backdrop-blur-sm">
          <div className="flex items-center gap-3.5">
            <div>
              <div className="flex items-center gap-2.5">
                <h1 className="text-base font-semibold tracking-tight text-zinc-100">
                  CI Runs (Preview)
                </h1>
                <span className="inline-flex items-center gap-1.5 rounded-full border border-amber-500/30 bg-amber-950/40 px-2.5 py-1 text-xs font-mono font-medium text-amber-400">
                  <span className="h-1.5 w-1.5 rounded-full bg-amber-400 animate-pulse" />
                  Mock Sandbox
                </span>
              </div>
              <p className="text-xs text-zinc-400 font-mono mt-0.5">
                Standalone preview with realistic test runs &amp; zero auth gating
              </p>
            </div>
          </div>

          <div className="flex items-center gap-3.5">
            {/* Reset mock data button */}
            <Button
              variant="outline"
              size="sm"
              onClick={handleResetMockData}
              className="h-8 px-3 text-xs text-zinc-400 hover:text-zinc-200 border-zinc-800 rounded-[7px] font-mono"
              title="Reset mock data to initial 10 runs"
            >
              <RotateCcw className="h-3.5 w-3.5 mr-1.5" />
              Reset Mock Data
            </Button>

            {/* Mock User profile pill */}
            <div className="flex items-center gap-3 pl-3 border-l border-zinc-800">
              <div className="flex items-center gap-2.5">
                <div className="relative h-8 w-8 rounded-full overflow-hidden border border-zinc-700 shrink-0 bg-zinc-800 flex items-center justify-center">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={HARDCODED_USER.avatarUrl}
                    alt={HARDCODED_USER.username}
                    className="h-full w-full object-cover"
                    onError={(e) => {
                      (e.target as HTMLElement).style.display = "none";
                    }}
                  />
                  <UserIcon className="h-4 w-4 text-zinc-400" />
                </div>
                <div className="flex flex-col text-left">
                  <span className="text-[13px] font-mono text-zinc-200 leading-none">
                    {HARDCODED_USER.username}
                  </span>
                  <span className="text-[10px] font-mono text-zinc-500 leading-none mt-1">
                    admin: true
                  </span>
                </div>
              </div>
            </div>
          </div>
        </header>

        {/* Main Body */}
        <main className="flex-1 p-6 overflow-y-auto space-y-5">
          {/* ===================================================================== */}
          {/* 3. FILTER BAR CONTAINER                                               */}
          {/* ===================================================================== */}
          <div className="flex flex-wrap items-center justify-between gap-4 rounded-[7px] border border-zinc-800 bg-[#121215] p-3.5">
            <div className="flex flex-wrap items-center gap-3.5 flex-1 min-w-[300px]">
              <div className="flex items-center gap-2 text-[13px] text-zinc-400 font-medium pl-1">
                <Filter className="h-4 w-4 text-amber-400" />
                <span>Filters:</span>
              </div>

              {/* Search input */}
              <div className="relative w-64">
                <Search className="absolute left-3 top-3 h-4 w-4 text-zinc-500" />
                <Input
                  type="text"
                  placeholder="Search branch, sha, diagnosis..."
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  className="h-10 pl-9.5 pr-3.5 text-[13px] rounded-[7px]"
                />
              </div>

              {/* Repo Select */}
              <select
                value={selectedRepo}
                onChange={(e) => setSelectedRepo(e.target.value)}
                className="h-10 rounded-[7px] border border-zinc-800 bg-[#0c0c0e] px-3.5 text-[13px] text-zinc-200 focus:border-amber-400 focus:outline-none"
              >
                <option value="">All Repositories ({repos.length})</option>
                {repos.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </select>

              {/* Status Select */}
              <select
                value={selectedStatus}
                onChange={(e) => setSelectedStatus(e.target.value)}
                className="h-10 rounded-[7px] border border-zinc-800 bg-[#0c0c0e] px-3.5 text-[13px] text-zinc-200 focus:border-amber-400 focus:outline-none"
              >
                <option value="">All Statuses</option>
                <option value="completed">Completed / PR Opened</option>
                <option value="fix_generation">Generating Fix</option>
                <option value="error">Error / Failed</option>
                <option value="fallback">Fallback Comment</option>
              </select>
            </div>

            <div className="flex items-center gap-3.5">
              <span className="font-mono text-xs text-zinc-500">
                Showing {filteredRuns.length} of {runs.length} runs
              </span>

              {runs.length < INITIAL_MOCK_RUNS.length && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleResetMockData}
                  className="h-8 px-2.5 text-xs text-amber-400 border-amber-500/30 bg-amber-950/20 hover:bg-amber-950/40 rounded-[7px] flex items-center gap-1.5 font-mono"
                  title="Restore all initial mock runs"
                >
                  <RotateCcw className="h-3 w-3" />
                  Reset Mock Data
                </Button>
              )}

              {hasActiveFilters && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={handleResetFilters}
                  className="h-8 px-2.5 text-xs rounded-[7px] text-zinc-400 hover:text-zinc-100 flex items-center gap-1.5"
                >
                  <RotateCcw className="h-3.5 w-3.5" />
                  Clear Filters
                </Button>
              )}
            </div>
          </div>

          {/* ===================================================================== */}
          {/* SELECTION TOOLBAR (when rows are selected)                            */}
          {/* ===================================================================== */}
          {selectedIds.size > 0 && (
            <div className="flex items-center justify-between rounded-[7px] border border-red-500/30 bg-red-500/10 px-4 py-2.5 text-[13px]">
              <span className="font-mono text-zinc-300">
                <span className="font-semibold text-zinc-100">{selectedIds.size}</span> run
                {selectedIds.size > 1 ? "s" : ""} selected
              </span>
              <div className="flex items-center gap-2.5">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setSelectedIds(new Set())}
                  className="h-8 px-3 text-[13px] text-zinc-400 hover:text-zinc-200"
                >
                  Clear selection
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={handleBatchDelete}
                  className="flex items-center gap-2 h-8 px-3 text-[13px] font-medium rounded-[6px] bg-red-500/10 hover:bg-red-500/20 text-red-400 border border-red-500/30 transition-colors"
                >
                  <Trash2 className="h-4 w-4" />
                  Delete Selected ({selectedIds.size})
                </Button>
              </div>
            </div>
          )}

          {/* ===================================================================== */}
          {/* 4. RUNS TABLE SHELL                                                   */}
          {/* ===================================================================== */}
          <div className="rounded-[6px] border border-zinc-800 bg-[#121215] overflow-hidden shadow-sm">
            {filteredRuns.length === 0 ? (
              <div className="p-12 text-center border border-dashed border-zinc-800/80 m-4 rounded-[6px]">
                <Activity className="h-6 w-6 mx-auto text-zinc-600 mb-2" />
                <h3 className="text-sm font-medium text-zinc-300">
                  {runs.length === 0 ? "All mock runs deleted" : "No matching CI runs"}
                </h3>
                <p className="text-xs text-zinc-500 mt-1 max-w-sm mx-auto">
                  {runs.length === 0
                    ? "All mock CI runs have been removed. Restore initial mock data to continue testing."
                    : "Try clearing your search query or adjusting the repository/status filter."}
                </p>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={runs.length === 0 ? handleResetMockData : handleResetFilters}
                  className="mt-4 text-xs font-mono"
                >
                  <RotateCcw className="h-3.5 w-3.5 mr-1.5" />
                  {runs.length === 0 ? "Reset Mock Data" : "Reset Filters"}
                </Button>
              </div>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    {/* Checkbox Column */}
                    <TableHead className="w-10 px-3">
                      <input
                        ref={(el) => {
                          headerCheckboxRef.current = el;
                          if (el) {
                            el.indeterminate = isIndeterminate;
                          }
                        }}
                        type="checkbox"
                        checked={isAllSelected}
                        onChange={handleToggleSelectAll}
                        disabled={filteredRuns.length === 0}
                        aria-label="Select all runs"
                        className="h-4 w-4 rounded bg-zinc-900 border-zinc-700 text-amber-400 accent-amber-400 focus:ring-0 cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
                      />
                    </TableHead>
                    <TableHead className="w-[14%]">Status</TableHead>
                    <TableHead className="w-[22%]">Repository</TableHead>
                    <TableHead className="w-[23%]">Branch / Commit</TableHead>
                    <TableHead className="w-[13%]">Trigger</TableHead>
                    <TableHead className="w-[9%]">Duration</TableHead>
                    <TableHead className="w-[9%]">Cost</TableHead>
                    <TableHead className="w-[10%] text-right">Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filteredRuns.map((run) => {
                    const isSelected = selectedIds.has(run.id);

                    return (
                      <TableRow
                        key={run.id}
                        onClick={() => setActiveDetailRun(run)}
                        className={`cursor-pointer group ${isSelected ? "bg-zinc-800/30" : ""}`}
                      >
                        {/* Checkbox */}
                        <TableCell className="w-10 px-3" onClick={(e) => e.stopPropagation()}>
                          <input
                            type="checkbox"
                            checked={isSelected}
                            onClick={(e) => e.stopPropagation()}
                            onChange={(e) => {
                              e.stopPropagation();
                              handleToggleRun(run.id);
                            }}
                            aria-label={`Select run ${run.id}`}
                            className="h-4 w-4 rounded bg-zinc-900 border-zinc-700 text-amber-400 accent-amber-400 focus:ring-0 cursor-pointer"
                          />
                        </TableCell>

                        {/* Status */}
                        <TableCell>
                          <StatusBadge status={run.status} />
                        </TableCell>

                        {/* Repository */}
                        <TableCell>
                          <span className="font-mono text-xs font-semibold text-zinc-200 group-hover:text-amber-400 transition-colors">
                            {run.repo}
                          </span>
                        </TableCell>

                        {/* Branch / Commit */}
                        <TableCell>
                          <div className="flex items-center gap-2 font-mono text-[11px] text-zinc-400">
                            <span className="inline-flex items-center gap-1 truncate max-w-[130px]" title={run.head_branch}>
                              <GitBranch className="h-3 w-3 text-zinc-500 shrink-0" />
                              <span className="truncate">{run.head_branch}</span>
                            </span>
                            <span className="text-zinc-600 shrink-0">•</span>
                            <span className="inline-flex items-center gap-1 text-zinc-300 shrink-0">
                              <GitCommit className="h-3 w-3 text-zinc-500" />
                              {run.head_sha}
                            </span>
                          </div>
                        </TableCell>

                        {/* Trigger */}
                        <TableCell>
                          <div className="flex flex-col text-[11px] font-mono">
                            <span className="inline-flex items-center gap-1 text-zinc-300">
                              {run.trigger === "workflow_run" ? (
                                <Workflow className="h-3 w-3 text-blue-400 shrink-0" />
                              ) : (
                                <PlayCircle className="h-3 w-3 text-emerald-400 shrink-0" />
                              )}
                              {run.trigger}
                            </span>
                            <span className="text-zinc-500 text-[10px] mt-0.5">
                              {run.created_at}
                            </span>
                          </div>
                        </TableCell>

                        {/* Duration */}
                        <TableCell>
                          <span className="inline-flex items-center gap-1 font-mono text-xs text-zinc-300">
                            <Clock className="h-3 w-3 text-zinc-500" />
                            {run.duration}
                          </span>
                        </TableCell>

                        {/* Cost & Tokens */}
                        <TableCell>
                          <div className="flex flex-col font-mono text-xs">
                            <span className="text-emerald-400 font-medium">
                              ${run.cost.toFixed(4)}
                            </span>
                            <span className="text-[10px] text-zinc-500">
                              {(run.tokens / 1000).toFixed(1)}k tok
                            </span>
                          </div>
                        </TableCell>

                        {/* Actions */}
                        <TableCell className="text-right" onClick={(e) => e.stopPropagation()}>
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={(e) => {
                              e.stopPropagation();
                              setActiveDetailRun(run);
                            }}
                            className="h-7 px-2 text-xs font-mono text-zinc-400 hover:text-amber-400 hover:bg-zinc-800/80"
                          >
                            Trace
                            <ArrowUpRight className="h-3 w-3 ml-1" />
                          </Button>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            )}

            {/* Table Footer */}
            <div className="flex items-center justify-between border-t border-zinc-800 bg-[#0c0c0e] px-5 py-3 text-[13px] text-zinc-400">
              <span className="font-mono text-xs">
                Showing {filteredRuns.length} of {runs.length} mock runs
              </span>
              <div className="flex items-center gap-2">
                <span className="font-mono text-[11px] text-zinc-500 mr-2">
                  Page 1 of 1 (Mock Preview)
                </span>
                <Button variant="outline" size="sm" disabled className="h-7 px-2">
                  <ChevronLeft className="h-3.5 w-3.5" />
                </Button>
                <Button variant="outline" size="sm" disabled className="h-7 px-2">
                  <ChevronRight className="h-3.5 w-3.5" />
                </Button>
              </div>
            </div>
          </div>

          {/* ===================================================================== */}
          {/* DETAIL TRACE MODAL                                                    */}
          {/* ===================================================================== */}
          {activeDetailRun && (
            <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-xs p-4">
              <div className="relative w-full max-w-2xl rounded-[8px] border border-zinc-700/80 bg-[#121215] p-6 shadow-2xl space-y-4">
                {/* Modal Header */}
                <div className="flex items-start justify-between border-b border-zinc-800 pb-3.5">
                  <div className="space-y-1">
                    <div className="flex items-center gap-2.5">
                      <span className="font-mono text-sm font-bold text-zinc-100">
                        {activeDetailRun.id}
                      </span>
                      <StatusBadge status={activeDetailRun.status} />
                    </div>
                    <div className="flex items-center gap-2 font-mono text-xs text-zinc-400">
                      <span>{activeDetailRun.repo}</span>
                      <span>•</span>
                      <span>{activeDetailRun.head_branch}</span>
                      <span>•</span>
                      <span>{activeDetailRun.head_sha}</span>
                    </div>
                  </div>
                  <Button
                    variant="ghost"
                    size="icon"
                    onClick={() => setActiveDetailRun(null)}
                    className="h-8 w-8 text-zinc-400 hover:text-zinc-100 hover:bg-zinc-800"
                  >
                    <X className="h-4 w-4" />
                  </Button>
                </div>

                {/* Metrics row */}
                <div className="grid grid-cols-4 gap-3 font-mono text-xs">
                  <div className="rounded-[6px] border border-zinc-800 bg-[#0c0c0e] p-2.5">
                    <span className="text-[10px] text-zinc-500 uppercase block">Model</span>
                    <span className="text-zinc-200 font-medium truncate block mt-0.5">
                      {activeDetailRun.model.replace("-free", "")}
                    </span>
                  </div>
                  <div className="rounded-[6px] border border-zinc-800 bg-[#0c0c0e] p-2.5">
                    <span className="text-[10px] text-zinc-500 uppercase block">Duration</span>
                    <span className="text-zinc-200 font-medium block mt-0.5">
                      {activeDetailRun.duration}
                    </span>
                  </div>
                  <div className="rounded-[6px] border border-zinc-800 bg-[#0c0c0e] p-2.5">
                    <span className="text-[10px] text-zinc-500 uppercase block">Tokens</span>
                    <span className="text-zinc-200 font-medium block mt-0.5">
                      {activeDetailRun.tokens.toLocaleString()}
                    </span>
                  </div>
                  <div className="rounded-[6px] border border-zinc-800 bg-[#0c0c0e] p-2.5">
                    <span className="text-[10px] text-zinc-500 uppercase block">Total Cost</span>
                    <span className="text-emerald-400 font-medium block mt-0.5">
                      ${activeDetailRun.cost.toFixed(4)}
                    </span>
                  </div>
                </div>

                {/* Diagnosis Details */}
                <div className="space-y-1.5">
                  <span className="text-xs font-semibold text-zinc-300 uppercase tracking-wider font-mono">
                    Autonomous Diagnosis
                  </span>
                  <div className="rounded-[6px] border border-zinc-800 bg-[#09090b] p-3 text-xs text-zinc-300 font-mono leading-relaxed">
                    {activeDetailRun.diagnosis}
                  </div>
                </div>

                {/* Sandbox Verification Result */}
                <div className="space-y-1.5">
                  <span className="text-xs font-semibold text-zinc-300 uppercase tracking-wider font-mono">
                    CodeBuild Sandbox Verification
                  </span>
                  <div className="rounded-[6px] border border-zinc-800 bg-[#09090b] p-3 text-xs font-mono space-y-1">
                    <div className="flex items-center justify-between">
                      <span className="text-zinc-400">Status:</span>
                      <span
                        className={cn(
                          "font-semibold",
                          activeDetailRun.sandbox_status === "passed"
                            ? "text-emerald-400"
                            : activeDetailRun.sandbox_status === "failed"
                            ? "text-red-400"
                            : "text-amber-400"
                        )}
                      >
                        {activeDetailRun.sandbox_status?.toUpperCase() || "PENDING"}
                      </span>
                    </div>
                    {activeDetailRun.error_message && (
                      <div className="mt-2 text-red-300 bg-red-950/30 border border-red-900/40 p-2 rounded text-[11px]">
                        {activeDetailRun.error_message}
                      </div>
                    )}
                  </div>
                </div>

                {/* PR Link if available */}
                {activeDetailRun.pr_url && (
                  <div className="flex items-center justify-between rounded-[6px] border border-emerald-900/50 bg-emerald-950/20 p-3 text-xs">
                    <div className="flex items-center gap-2 text-emerald-300">
                      <GitPullRequest className="h-4 w-4" />
                      <span>PR #{activeDetailRun.pr_number} opened successfully on GitHub</span>
                    </div>
                    <a
                      href={activeDetailRun.pr_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1 font-mono text-xs text-emerald-400 hover:underline"
                    >
                      View on GitHub
                      <ArrowUpRight className="h-3 w-3" />
                    </a>
                  </div>
                )}

                {/* Footer */}
                <div className="flex justify-end pt-2">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setActiveDetailRun(null)}
                    className="text-xs font-mono"
                  >
                    Close Trace
                  </Button>
                </div>
              </div>
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
