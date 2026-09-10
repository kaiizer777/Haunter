"use client";

import { useEffect, useState, useCallback, useMemo, useRef } from "react";
import Link from "next/link";
import { AppLayout } from "@/components/layout/app-layout";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { SelectDropdown } from "@/components/ui/select-dropdown";
import { AddRepoModal } from "@/components/repos/add-repo-modal";
import { api, RepoOut } from "@/lib/api";
import { formatRelativeTime, cn } from "@/lib/utils";
import {
  GitBranch,
  Trash2,
  Plus,
  AlertCircle,
  ExternalLink,
  Search,
  X,
  ShieldCheck,
  Sliders,
  FolderGit2,
  ArrowUpRight,
  RefreshCw,
  Clock,
  Filter,
  Activity,
  Layers,
  RotateCcw,
} from "lucide-react";

/**
 * Color mapping for standard programming languages in repository badges.
 */
function getLanguageColor(lang?: string | null): string {
  if (!lang) return "bg-zinc-500";
  const l = lang.toLowerCase();
  if (l.includes("typescript") || l === "ts") return "bg-blue-400";
  if (l.includes("javascript") || l === "js") return "bg-yellow-400";
  if (l.includes("python") || l === "py") return "bg-sky-400";
  if (l.includes("rust") || l === "rs") return "bg-orange-400";
  if (l.includes("go") || l === "golang") return "bg-cyan-400";
  if (l.includes("java") || l === "kotlin") return "bg-red-400";
  if (l.includes("ruby")) return "bg-rose-400";
  return "bg-emerald-400";
}

export default function ReposPage() {
  const [repos, setRepos] = useState<RepoOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isAddModalOpen, setIsAddModalOpen] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const [searchQuery, setSearchQuery] = useState("");
  const [selectedLanguage, setSelectedLanguage] = useState<string>("all");

  const searchInputRef = useRef<HTMLInputElement | null>(null);

  const fetchRepos = useCallback(async (isManualRefresh = false) => {
    if (isManualRefresh) {
      setIsRefreshing(true);
    } else {
      setLoading(true);
    }
    setError(null);
    try {
      const data = await api.getRepos();
      setRepos(data);
    } catch (err: unknown) {
      if (err instanceof Error) {
        setError(err.message);
      } else {
        setError("Failed to load repositories.");
      }
    } finally {
      setLoading(false);
      setIsRefreshing(false);
    }
  }, []);

  useEffect(() => {
    fetchRepos();
  }, [fetchRepos]);

  // Keyboard shortcut handlers: '/' to search, 'r' to refresh, 'Escape' to blur
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (["INPUT", "TEXTAREA", "SELECT"].includes((e.target as HTMLElement)?.tagName)) {
        if (e.key === "Escape") {
          (e.target as HTMLElement).blur();
        }
        return;
      }
      if (e.key === "/") {
        e.preventDefault();
        searchInputRef.current?.focus();
      } else if (e.key === "r" || e.key === "R") {
        e.preventDefault();
        fetchRepos(true);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [fetchRepos]);

  const handleDeleteRepo = async (id: string, repoName: string) => {
    if (!confirm(`Are you sure you want to disconnect ${repoName}?`)) {
      return;
    }

    setDeletingId(id);
    try {
      await api.removeRepo(id);
      setRepos((prev) => prev.filter((r) => r.id !== id));
    } catch {
      alert("Failed to disconnect repository.");
    } finally {
      setDeletingId(null);
    }
  };

  const handleRepoAdded = (newRepo: RepoOut) => {
    setRepos((prev) => [newRepo, ...prev]);
  };

  // Language hints and counts
  const languageStats = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const r of repos) {
      const lang = r.language_hint ? r.language_hint.toLowerCase() : "auto-detect";
      counts[lang] = (counts[lang] || 0) + 1;
    }
    return counts;
  }, [repos]);

  const availableLanguages = useMemo(() => {
    const set = new Set<string>();
    for (const r of repos) {
      if (r.language_hint) set.add(r.language_hint.toLowerCase());
    }
    return Array.from(set).sort();
  }, [repos]);

  // Filtered repositories
  const filteredRepos = useMemo(() => {
    return repos.filter((repo) => {
      const q = searchQuery.toLowerCase().trim();
      const matchesSearch =
        !q ||
        repo.owner.toLowerCase().includes(q) ||
        repo.name.toLowerCase().includes(q) ||
        `${repo.owner}/${repo.name}`.toLowerCase().includes(q) ||
        (repo.default_branch && repo.default_branch.toLowerCase().includes(q));

      const matchesLang =
        selectedLanguage === "all" ||
        (repo.language_hint && repo.language_hint.toLowerCase() === selectedLanguage.toLowerCase()) ||
        (!repo.language_hint && selectedLanguage === "auto-detect");

      return matchesSearch && matchesLang;
    });
  }, [repos, searchQuery, selectedLanguage]);

  const hasActiveFilters = Boolean(searchQuery || selectedLanguage !== "all");

  const handleResetFilters = () => {
    setSearchQuery("");
    setSelectedLanguage("all");
  };

  return (
    <AppLayout
      title="Connected Repositories"
      subtitle="Manage tracked repositories and autonomous fix pipelines"
      actions={
        <div className="flex items-center gap-2.5">
          <Button
            variant="outline"
            size="sm"
            onClick={() => fetchRepos(true)}
            disabled={loading || isRefreshing}
            className="group h-8 px-3 text-xs font-mono rounded-[6px] text-zinc-300 hover:text-white bg-gradient-to-b from-zinc-800/90 via-zinc-850 to-zinc-900/90 border-t border-t-zinc-600/70 border-x border-x-zinc-700/60 border-b border-b-zinc-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.12),0_1px_3px_rgba(0,0,0,0.35),0_1px_2px_rgba(0,0,0,0.2)] hover:border-t-zinc-500 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.2),0_2px_6px_rgba(0,0,0,0.4)] active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.4)] flex items-center gap-1.5 transition-all cursor-pointer"
            title="Refresh repositories (Hot-key: R)"
          >
            <RefreshCw
              className={cn(
                "h-3.5 w-3.5 transition-colors",
                isRefreshing ? "animate-spin text-amber-400" : "text-zinc-400 group-hover:text-amber-400"
              )}
            />
            <span className="hidden sm:inline">Refresh</span>
          </Button>

          <Button
            onClick={() => setIsAddModalOpen(true)}
            className="flex items-center gap-1.5 bg-gradient-to-b from-amber-400 via-amber-450 to-amber-500 hover:from-amber-300 hover:to-amber-400 text-zinc-950 font-bold text-xs h-8 px-3.5 rounded-[6px] border-t border-t-amber-200/50 border-x border-x-amber-400/60 border-b border-b-amber-600/40 shadow-[inset_0_1px_0_rgba(255,255,255,0.35),0_2px_8px_rgba(245,158,11,0.25)] active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.3)] cursor-pointer transition-all"
            size="sm"
          >
            <Plus className="h-3.5 w-3.5" />
            <span>Connect Repository</span>
          </Button>
        </div>
      }
    >
      <div className="space-y-5 min-w-0 pb-16">
        {/* Error Alert */}
        {error && (
          <div className="flex items-start gap-3 rounded-[7px] border border-red-900/60 bg-red-950/30 p-3.5 text-[13px] text-red-300 shadow-md animate-in fade-in">
            <AlertCircle className="h-4.5 w-4.5 shrink-0 text-red-400 mt-0.5" />
            <div className="space-y-0.5">
              <p className="font-semibold text-red-200">Repository Pipeline Error</p>
              <p className="text-red-300/90 text-xs font-mono">{error}</p>
            </div>
          </div>
        )}

        {/* Telemetry Metric Cards */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3.5">
          {/* Stat 1: Total Monitored Repos */}
          <div className="rounded-lg border border-zinc-800/80 bg-gradient-to-b from-[#121216]/90 to-[#0c0c0e]/90 backdrop-blur p-4 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.03)] flex flex-col justify-between">
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-mono uppercase tracking-wider text-zinc-400">Monitored Repos</span>
              <FolderGit2 className="h-4 w-4 text-amber-400" />
            </div>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="text-2xl font-bold font-mono text-zinc-100 tabular-nums">{repos.length}</span>
              <span className="text-[11px] font-mono text-zinc-500">
                active {repos.length === 1 ? "repo" : "repos"}
              </span>
            </div>
            <div className="mt-1 text-[11px] text-zinc-500 truncate">Webhook triggers configured for automated triage</div>
          </div>

          {/* Stat 2: Webhook Gateway */}
          <div className="rounded-lg border border-zinc-800/80 bg-gradient-to-b from-[#121216]/90 to-[#0c0c0e]/90 backdrop-blur p-4 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.03)] flex flex-col justify-between">
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-mono uppercase tracking-wider text-zinc-400">CI Webhook Gateway</span>
              <span className="flex items-center gap-1.5 text-[10px] font-mono text-emerald-400 bg-emerald-950/40 border border-emerald-800/40 px-2 py-0.5 rounded-[4px]">
                <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse" />
                Live
              </span>
            </div>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="text-base font-bold font-mono text-zinc-100">workflow_run.completed</span>
            </div>
            <div className="mt-1 text-[11px] text-zinc-500 truncate">Instant wakeup on GitHub Actions failure events</div>
          </div>

          {/* Stat 3: Sandbox Isolation */}
          <div className="rounded-lg border border-zinc-800/80 bg-gradient-to-b from-[#121216]/90 to-[#0c0c0e]/90 backdrop-blur p-4 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.03)] flex flex-col justify-between">
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-mono uppercase tracking-wider text-zinc-400">Sandbox Isolation</span>
              <ShieldCheck className="h-4 w-4 text-emerald-400" />
            </div>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="text-base font-bold font-mono text-zinc-100">Verified CI Sandboxes</span>
            </div>
            <div className="mt-1 text-[11px] text-zinc-500 truncate">Patches verified in isolated mirror before PR creation</div>
          </div>
        </div>

        {/* Quick Filter Tabs & Control Bar */}
        <div className="space-y-3">
          {/* Quick Segmented Language Tabs */}
          {repos.length > 0 && (
            <div className="flex items-center justify-between gap-3 overflow-x-auto pb-0.5">
              <div className="flex items-center gap-1.5 p-1 rounded-lg border border-zinc-800/80 bg-[#0d0d10]/90 backdrop-blur-sm">
                <button
                  type="button"
                  onClick={() => setSelectedLanguage("all")}
                  className={cn(
                    "flex items-center gap-2 px-3 py-1.5 rounded-[5px] text-xs font-mono transition-all cursor-pointer",
                    selectedLanguage === "all"
                      ? "bg-zinc-800 text-zinc-100 font-semibold border border-zinc-700/60 shadow-sm"
                      : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/40"
                  )}
                >
                  <span>All Repos</span>
                  <span
                    className={cn(
                      "text-[10px] px-1.5 py-0.2 rounded-full font-mono tabular-nums",
                      selectedLanguage === "all"
                        ? "bg-zinc-700 text-zinc-200"
                        : "bg-zinc-900 text-zinc-500 border border-zinc-800"
                    )}
                  >
                    {repos.length}
                  </span>
                </button>

                {availableLanguages.map((lang) => {
                  const isActive = selectedLanguage.toLowerCase() === lang.toLowerCase();
                  const count = languageStats[lang] || 0;
                  return (
                    <button
                      key={lang}
                      type="button"
                      onClick={() => setSelectedLanguage(lang)}
                      className={cn(
                        "flex items-center gap-2 px-3 py-1.5 rounded-[5px] text-xs font-mono transition-all cursor-pointer",
                        isActive
                          ? "bg-zinc-800 text-zinc-100 font-semibold border border-zinc-700/60 shadow-sm"
                          : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/40"
                      )}
                    >
                      <span className={cn("h-1.5 w-1.5 rounded-full", getLanguageColor(lang))} />
                      <span className="capitalize">{lang}</span>
                      <span
                        className={cn(
                          "text-[10px] px-1.5 py-0.2 rounded-full font-mono tabular-nums",
                          isActive
                            ? "bg-zinc-700 text-zinc-200"
                            : "bg-zinc-900 text-zinc-500 border border-zinc-800"
                        )}
                      >
                        {count}
                      </span>
                    </button>
                  );
                })}
              </div>

              <div className="hidden sm:flex items-center gap-2 text-xs font-mono text-zinc-500">
                <span className="tabular-nums">
                  Showing {filteredRepos.length} of {repos.length} repos
                </span>
                {hasActiveFilters && (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={handleResetFilters}
                    className="h-6 px-2 text-[11px] font-mono rounded-[4px] text-zinc-400 hover:text-zinc-200 bg-zinc-800/40 hover:bg-zinc-800/80 border border-zinc-800 hover:border-zinc-700 transition-all flex items-center gap-1.5 cursor-pointer"
                  >
                    <RotateCcw className="h-3 w-3" />
                    Reset
                  </Button>
                )}
              </div>
            </div>
          )}

          {/* Detailed Filters Bar */}
          <div className="relative z-20 flex flex-wrap items-center justify-between gap-3.5 rounded-lg border border-zinc-800/80 bg-[#0d0d10]/90 backdrop-blur-sm shadow-[inset_0_1px_0_0_rgba(255,255,255,0.03)] p-3">
            <div className="flex flex-wrap items-center gap-3 flex-1 min-w-[300px]">
              <div className="flex items-center gap-1.5 text-xs text-zinc-400 font-medium pl-1">
                <Filter className="h-3.5 w-3.5 text-zinc-500" />
                <span className="font-mono text-[11px] tracking-wide text-zinc-400 uppercase">Filters</span>
              </div>

              {/* Search input with '/' hotkey */}
              <div className="relative flex-1 min-w-[240px] max-w-sm">
                <Search className="absolute left-3 top-2.5 h-3.5 w-3.5 text-zinc-500 pointer-events-none" />
                <Input
                  ref={searchInputRef}
                  type="text"
                  placeholder="Search owner, repo name, default branch..."
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  className="h-8.5 pl-8.5 pr-8 text-xs font-mono rounded-[6px] bg-zinc-900/60 border-zinc-800/80 focus:outline-none focus:border-amber-400/80 focus:ring-1 focus:ring-amber-400/25 placeholder:text-zinc-500 placeholder:font-sans transition-all"
                />
                {!searchQuery ? (
                  <div className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 flex items-center">
                    <kbd className="inline-flex h-4 min-w-4 items-center justify-center rounded border border-zinc-700/60 bg-zinc-800/60 px-1 font-mono text-[10px] text-zinc-400 select-none">
                      /
                    </kbd>
                  </div>
                ) : (
                  <button
                    type="button"
                    onClick={() => {
                      setSearchQuery("");
                      searchInputRef.current?.focus();
                    }}
                    className="absolute right-2.5 top-1/2 -translate-y-1/2 text-zinc-500 hover:text-zinc-200 p-0.5 rounded transition-colors cursor-pointer"
                    aria-label="Clear search"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                )}
              </div>

              {/* Language dropdown */}
              <SelectDropdown
                value={selectedLanguage}
                onChange={(val) => setSelectedLanguage(val)}
                options={[
                  {
                    value: "all",
                    label: `All Languages (${repos.length})`,
                    icon: <Layers className="h-3.5 w-3.5 text-zinc-400" />,
                  },
                  ...availableLanguages.map((lang) => ({
                    value: lang,
                    label: lang.charAt(0).toUpperCase() + lang.slice(1),
                    badge: (
                      <span className={cn("h-2 w-2 rounded-full", getLanguageColor(lang))} />
                    ),
                  })),
                ]}
                buttonClassName="min-w-[170px] h-8.5"
              />
            </div>

            <div className="flex sm:hidden items-center justify-between w-full pt-2 border-t border-zinc-800/60">
              <span className="tabular-nums text-zinc-500 font-mono text-[11px]">
                Showing {filteredRepos.length} of {repos.length} repos
              </span>
              {hasActiveFilters && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={handleResetFilters}
                  className="h-6 px-2 text-[11px] font-mono rounded-[4px] text-zinc-400 hover:text-zinc-200 bg-zinc-800/40 hover:bg-zinc-800/80 border border-zinc-800 hover:border-zinc-700 transition-all flex items-center gap-1.5 cursor-pointer"
                >
                  <RotateCcw className="h-3 w-3" />
                  Reset
                </Button>
              )}
            </div>
          </div>
        </div>

        {/* High-Precision Repos Table */}
        <div className="relative z-0 overflow-hidden rounded-xl border-t border-t-zinc-600/60 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-gradient-to-b from-[#111115]/95 via-[#0d0d10]/95 to-[#09090c]/95 backdrop-blur-sm shadow-[inset_0_1px_0_rgba(255,255,255,0.12),inset_0_-1px_0_rgba(0,0,0,0.4),0_8px_32px_rgba(0,0,0,0.5),0_2px_4px_rgba(0,0,0,0.3)]">
          {/* Subtle light-from-above ambient gradient sheen */}
          <span
            aria-hidden="true"
            className="pointer-events-none absolute inset-x-0 top-0 h-6 bg-gradient-to-b from-white/[0.04] to-transparent z-10"
          />

          {loading ? (
            <div className="p-4 space-y-3">
              <Skeleton className="h-10 w-full bg-zinc-900/60" />
              <Skeleton className="h-10 w-full bg-zinc-900/60" />
              <Skeleton className="h-10 w-full bg-zinc-900/60" />
            </div>
          ) : repos.length === 0 ? (
            <div className="p-12 sm:p-16 text-center border border-dashed border-zinc-800/80 m-4 rounded-xl bg-[#09090b]/40">
              <div className="h-12 w-12 rounded-xl bg-amber-400/10 border border-amber-500/30 text-amber-400 flex items-center justify-center mx-auto mb-4 shadow-[0_0_20px_rgba(245,158,11,0.15)]">
                <GitBranch className="h-6 w-6" />
              </div>
              <h3 className="text-base font-bold text-zinc-100 font-mono">No repositories connected</h3>
              <p className="text-xs sm:text-sm text-zinc-400 mt-1 max-w-md mx-auto leading-relaxed">
                Connect a GitHub repository to monitor CI failure workflows and trigger autonomous fixes.
              </p>

              <div className="mt-6 grid grid-cols-1 sm:grid-cols-3 gap-3 max-w-lg mx-auto text-left">
                <div className="rounded-lg border border-zinc-800/80 bg-zinc-900/50 p-3 text-xs text-zinc-400 shadow-sm">
                  <span className="font-semibold text-zinc-200 block mb-0.5">⚡ Zero Overhead</span>
                  Passes run untouched; fixes trigger only on CI failure.
                </div>
                <div className="rounded-lg border border-zinc-800/80 bg-zinc-900/50 p-3 text-xs text-zinc-400 shadow-sm">
                  <span className="font-semibold text-zinc-200 block mb-0.5">🛡️ Sandbox Verified</span>
                  Patches verified in isolated mirrors before PRs open.
                </div>
                <div className="rounded-lg border border-zinc-800/80 bg-zinc-900/50 p-3 text-xs text-zinc-400 shadow-sm">
                  <span className="font-semibold text-zinc-200 block mb-0.5">⚙️ Custom Routing</span>
                  Override inference models per repository.
                </div>
              </div>

              <Button
                onClick={() => setIsAddModalOpen(true)}
                className="mt-6 bg-gradient-to-b from-amber-400 to-amber-500 text-zinc-950 hover:from-amber-300 hover:to-amber-400 font-bold text-xs h-9 px-4 rounded-[6px] shadow-[0_1px_0_inset_rgba(255,255,255,0.35),0_2px_8px_rgba(245,158,11,0.25)] cursor-pointer transition-all"
                size="sm"
              >
                <Plus className="h-3.5 w-3.5 mr-1.5" />
                <span>Connect First Repository</span>
              </Button>
            </div>
          ) : filteredRepos.length === 0 ? (
            <div className="p-12 text-center border border-dashed border-zinc-800/80 m-4 rounded-[6px]">
              <Search className="h-8 w-8 mx-auto text-zinc-600 mb-2.5" />
              <h3 className="text-sm font-medium text-zinc-300 font-mono">No matching repositories</h3>
              <p className="text-xs text-zinc-500 mt-1 max-w-sm mx-auto">
                Try clearing your search query or adjusting the language filter.
              </p>
              <Button
                variant="outline"
                size="sm"
                onClick={handleResetFilters}
                className="mt-4 text-xs font-mono rounded-[5px] cursor-pointer"
              >
                <RotateCcw className="h-3.5 w-3.5 mr-1.5" />
                Reset Filters
              </Button>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <Table>
                <TableHeader className="border-b border-zinc-800/90 bg-gradient-to-b from-[#141418] to-[#0c0c10] shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_1px_3px_rgba(0,0,0,0.35)]">
                  <TableRow className="border-b border-zinc-800/90 hover:bg-transparent">
                    <TableHead className="w-[38%] pl-4 py-3 text-[11px] font-mono tracking-wider uppercase text-zinc-500 font-medium select-none">
                      Repository
                    </TableHead>
                    <TableHead className="w-[18%] px-3 py-3 text-[11px] font-mono tracking-wider uppercase text-zinc-500 font-medium select-none">
                      Default Branch
                    </TableHead>
                    <TableHead className="w-[18%] px-3 py-3 text-[11px] font-mono tracking-wider uppercase text-zinc-500 font-medium select-none">
                      Language Hint
                    </TableHead>
                    <TableHead className="w-[14%] px-3 py-3 text-[11px] font-mono tracking-wider uppercase text-zinc-500 font-medium select-none">
                      Connected
                    </TableHead>
                    <TableHead className="w-[12%] pr-4 py-3 text-[11px] font-mono tracking-wider uppercase text-zinc-500 font-medium select-none text-right">
                      Actions
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody className="divide-y divide-zinc-800/40">
                  {filteredRepos.map((repo) => (
                    <TableRow
                      key={repo.id}
                      className="border-b border-zinc-800/40 hover:bg-zinc-800/20 transition-colors group"
                    >
                      {/* Repository Name & Links */}
                      <TableCell className="pl-4 py-3.5">
                        <div className="flex flex-col gap-1.5">
                          <div className="flex items-center gap-2">
                            <FolderGit2 className="h-4 w-4 text-amber-400/80 shrink-0" />
                            <div className="flex items-center gap-1 font-mono text-xs">
                              <span className="text-zinc-400">{repo.owner}/</span>
                              <span className="font-bold text-zinc-100 group-hover:text-amber-300 transition-colors">
                                {repo.name}
                              </span>
                            </div>
                            <a
                              href={`https://github.com/${repo.owner}/${repo.name}`}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="text-zinc-500 hover:text-zinc-200 transition-colors p-0.5 rounded hover:bg-zinc-800/60"
                              title="Open repository on GitHub"
                            >
                              <ExternalLink className="h-3 w-3" />
                            </a>
                          </div>

                          <div className="flex items-center gap-2 text-[11px] pl-6">
                            <Link
                              href={`/runs?repo_id=${repo.id}`}
                              className="text-zinc-400 hover:text-amber-400 transition-colors font-mono flex items-center gap-0.5"
                            >
                              <span>View runs</span>
                              <ArrowUpRight className="h-2.5 w-2.5" />
                            </Link>
                            <span className="text-zinc-600">·</span>
                            <Link
                              href="/config"
                              className="text-zinc-400 hover:text-cyan-400 transition-colors font-mono flex items-center gap-0.5"
                            >
                              <span>Model routing</span>
                              <ArrowUpRight className="h-2.5 w-2.5" />
                            </Link>
                          </div>
                        </div>
                      </TableCell>

                      {/* Default Branch */}
                      <TableCell className="px-3 py-3.5">
                        <span className="inline-flex items-center gap-1.5 rounded-[5px] border border-zinc-800/80 bg-[#0c0c0e]/90 px-2.5 py-1 font-mono text-[11px] text-zinc-300 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.03)] font-medium">
                          <GitBranch className="h-3 w-3 text-zinc-500" />
                          <span>{repo.default_branch || "main"}</span>
                        </span>
                      </TableCell>

                      {/* Language Hint */}
                      <TableCell className="px-3 py-3.5">
                        {repo.language_hint ? (
                          <span className="inline-flex items-center gap-1.5 font-mono text-[11px] text-zinc-300 bg-zinc-900/70 border border-zinc-800/80 px-2.5 py-1 rounded-[5px] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.02)]">
                            <span className={cn("h-1.5 w-1.5 rounded-full", getLanguageColor(repo.language_hint))} />
                            <span className="capitalize">{repo.language_hint}</span>
                          </span>
                        ) : (
                          <span className="text-zinc-500 font-mono text-[11px] bg-zinc-900/40 px-2 py-0.5 rounded-[4px] border border-zinc-800/50">
                            auto-detect
                          </span>
                        )}
                      </TableCell>

                      {/* Connected Timestamp */}
                      <TableCell className="px-3 py-3.5 font-mono text-xs text-zinc-400">
                        <div className="flex items-center gap-1.5">
                          <Clock className="h-3 w-3 text-zinc-500 shrink-0" />
                          <span>{formatRelativeTime(repo.created_at)}</span>
                        </div>
                      </TableCell>

                      {/* Actions */}
                      <TableCell className="pr-4 py-3.5 text-right">
                        <div className="flex items-center justify-end gap-1.5">
                          <Link
                            href="/config"
                            className="h-7 w-7 rounded-[5px] flex items-center justify-center text-zinc-400 hover:text-cyan-300 bg-gradient-to-b from-zinc-800/70 via-zinc-850/80 to-zinc-900/90 border border-zinc-700/50 hover:border-cyan-500/40 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_1px_2px_rgba(0,0,0,0.3)] transition-all cursor-pointer"
                            title="Configure model for this repository"
                          >
                            <Sliders className="h-3.5 w-3.5" />
                          </Link>
                          <Button
                            variant="ghost"
                            size="icon"
                            onClick={() => handleDeleteRepo(repo.id, `${repo.owner}/${repo.name}`)}
                            disabled={deletingId === repo.id}
                            className="h-7 w-7 rounded-[5px] text-zinc-400 hover:text-red-400 bg-gradient-to-b from-zinc-800/70 via-zinc-850/80 to-zinc-900/90 border border-zinc-700/50 hover:border-red-500/40 hover:bg-red-950/30 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_1px_2px_rgba(0,0,0,0.3)] transition-all cursor-pointer"
                            title="Disconnect repository"
                          >
                            {deletingId === repo.id ? (
                              <RefreshCw className="h-3.5 w-3.5 animate-spin text-red-400" />
                            ) : (
                              <Trash2 className="h-3.5 w-3.5" />
                            )}
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </div>
      </div>

      {/* Add Repo Modal */}
      <AddRepoModal
        isOpen={isAddModalOpen}
        onClose={() => setIsAddModalOpen(false)}
        onSuccess={handleRepoAdded}
      />
    </AppLayout>
  );
}

