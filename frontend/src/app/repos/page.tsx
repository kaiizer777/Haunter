"use client";

import { useEffect, useState, useCallback, useMemo } from "react";
import Link from "next/link";
import { AppLayout } from "@/components/layout/app-layout";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
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
  const [error, setError] = useState<string | null>(null);
  const [isAddModalOpen, setIsAddModalOpen] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const [searchQuery, setSearchQuery] = useState("");
  const [selectedLanguage, setSelectedLanguage] = useState<string>("all");

  const fetchRepos = useCallback(async () => {
    setLoading(true);
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
    }
  }, []);

  useEffect(() => {
    fetchRepos();
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

  // Distinct language hints
  const availableLanguages = useMemo(() => {
    const set = new Set<string>();
    for (const r of repos) {
      if (r.language_hint) set.add(r.language_hint);
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
        repo.language_hint?.toLowerCase() === selectedLanguage.toLowerCase();

      return matchesSearch && matchesLang;
    });
  }, [repos, searchQuery, selectedLanguage]);

  return (
    <AppLayout
      title="Connected Repositories"
      subtitle="Manage tracked repositories and autonomous fix pipelines"
      actions={
        <div className="flex items-center gap-3">
          <Button
            variant="outline"
            size="sm"
            onClick={fetchRepos}
            disabled={loading}
            className="group h-8 px-3 border-t border-t-zinc-600/70 border-x border-x-zinc-700/60 border-b border-b-zinc-950 bg-gradient-to-b from-zinc-800/90 via-zinc-850 to-zinc-900/90 shadow-[inset_0_1px_0_rgba(255,255,255,0.12),0_1px_3px_rgba(0,0,0,0.35),0_1px_2px_rgba(0,0,0,0.2)] hover:border-t-zinc-500 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.2),0_2px_6px_rgba(0,0,0,0.4)] active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.4)] text-zinc-300 hover:text-white gap-1.5 text-xs font-mono rounded-[6px] transition-all cursor-pointer"
          >
            <RefreshCw className={cn("h-3.5 w-3.5 transition-colors", loading ? "animate-spin text-amber-400" : "text-zinc-400 group-hover:text-amber-400")} />
            <span>Refresh</span>
          </Button>

          <Button
            onClick={() => setIsAddModalOpen(true)}
            className="flex items-center gap-1.5 bg-gradient-to-b from-amber-400 to-amber-500 text-zinc-950 hover:from-amber-300 hover:to-amber-400 font-bold text-xs h-8 px-3.5 shadow-[0_1px_0_inset_rgba(255,255,255,0.35),0_2px_8px_rgba(245,158,11,0.25)] ring-1 ring-amber-400/40 cursor-pointer"
            size="sm"
          >
            <Plus className="h-3.5 w-3.5" />
            <span>Connect Repository</span>
          </Button>
        </div>
      }
    >
      <div className="space-y-5 min-w-0 max-w-6xl pb-16">
        {/* Error Alert */}
        {error && (
          <div className="flex items-start gap-3 rounded-lg border border-red-800/80 bg-red-950/40 p-4 text-sm text-red-200 shadow-md animate-in fade-in">
            <AlertCircle className="h-5 w-5 shrink-0 text-red-400 mt-0.5" />
            <div className="space-y-0.5">
              <p className="font-semibold text-red-200">Repository Pipeline Error</p>
              <p className="text-red-300/90 text-xs font-mono">{error}</p>
            </div>
          </div>
        )}

        {/* TOP SUMMARY STATS STRIP */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3.5">
          {/* Stat 1: Total Connected */}
          <div className="rounded-xl border border-zinc-800 bg-[#121215] p-4 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.03),0_2px_8px_rgba(0,0,0,0.25)] space-y-1">
            <div className="flex items-center justify-between">
              <span className="text-xs font-mono font-medium text-zinc-400 uppercase tracking-wider">
                Monitored Repos
              </span>
              <FolderGit2 className="h-4 w-4 text-amber-400" />
            </div>
            <div className="flex items-baseline gap-2 pt-1">
              <span className="text-2xl font-bold font-mono text-zinc-100">{repos.length}</span>
              <span className="text-xs text-zinc-400">active {repos.length === 1 ? "repository" : "repositories"}</span>
            </div>
            <p className="text-[11px] text-zinc-500 pt-0.5">
              Webhook triggers configured for automated triage
            </p>
          </div>

          {/* Stat 2: Webhook Gateway */}
          <div className="rounded-xl border border-zinc-800 bg-[#121215] p-4 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.03),0_2px_8px_rgba(0,0,0,0.25)] space-y-1">
            <div className="flex items-center justify-between">
              <span className="text-xs font-mono font-medium text-zinc-400 uppercase tracking-wider">
                CI Webhook Gateway
              </span>
              <span className="flex items-center gap-1.5 text-[11px] font-mono text-emerald-400 bg-emerald-950/40 border border-emerald-800/40 px-2 py-0.5 rounded">
                <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse" />
                Live
              </span>
            </div>
            <div className="flex items-baseline gap-2 pt-1">
              <span className="text-sm font-bold text-zinc-200">workflow_run.completed</span>
            </div>
            <p className="text-[11px] text-zinc-500 pt-0.5">
              Instant wakeup on GitHub Actions failure events
            </p>
          </div>

          {/* Stat 3: Sandbox Verification */}
          <div className="rounded-xl border border-zinc-800 bg-[#121215] p-4 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.03),0_2px_8px_rgba(0,0,0,0.25)] space-y-1">
            <div className="flex items-center justify-between">
              <span className="text-xs font-mono font-medium text-zinc-400 uppercase tracking-wider">
                Sandbox Isolation
              </span>
              <ShieldCheck className="h-4 w-4 text-emerald-400" />
            </div>
            <div className="flex items-baseline gap-2 pt-1">
              <span className="text-sm font-bold text-zinc-200">Verified CI Sandboxes</span>
            </div>
            <p className="text-[11px] text-zinc-500 pt-0.5">
              Patches verified in isolated mirror before PR creation
            </p>
          </div>
        </div>

        {/* MAIN CONTAINER: Search + Table */}
        <div className="rounded-xl border border-zinc-800 bg-[#121215] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.03),0_4px_20px_rgba(0,0,0,0.35)] overflow-hidden">
          {/* Filter Bar */}
          {!loading && repos.length > 0 && (
            <div className="p-4 border-b border-zinc-800/80 bg-[#0e0e11] flex flex-col sm:flex-row sm:items-center justify-between gap-3">
              <div className="flex items-center gap-2 flex-1 max-w-md">
                <div className="relative flex-1">
                  <Search className="h-3.5 w-3.5 absolute left-3 top-1/2 -translate-y-1/2 text-zinc-500 pointer-events-none" />
                  <input
                    type="text"
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                    placeholder="Search connected repositories..."
                    className="w-full h-8 pl-8 pr-7 rounded-md border border-zinc-800 bg-[#070709] text-xs text-zinc-200 placeholder:text-zinc-500 focus:outline-none focus:border-amber-400/80 focus:ring-1 focus:ring-amber-400/30 transition-all font-mono"
                  />
                  {searchQuery && (
                    <button
                      type="button"
                      onClick={() => setSearchQuery("")}
                      className="absolute right-2 top-1/2 -translate-y-1/2 text-zinc-500 hover:text-zinc-300 cursor-pointer"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  )}
                </div>
              </div>

              {/* Language Filter Chips */}
              <div className="flex items-center gap-1.5 flex-wrap text-xs">
                <button
                  type="button"
                  onClick={() => setSelectedLanguage("all")}
                  className={cn(
                    "px-2.5 py-1 rounded-md text-[11px] font-mono font-medium transition-colors cursor-pointer",
                    selectedLanguage === "all"
                      ? "bg-zinc-800 text-zinc-100 font-bold border border-zinc-700"
                      : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900"
                  )}
                >
                  All ({repos.length})
                </button>
                {availableLanguages.map((lang) => (
                  <button
                    key={lang}
                    type="button"
                    onClick={() => setSelectedLanguage(lang)}
                    className={cn(
                      "px-2.5 py-1 rounded-md text-[11px] font-mono font-medium transition-colors cursor-pointer flex items-center gap-1.5",
                      selectedLanguage === lang
                        ? "bg-zinc-800 text-amber-300 font-bold border border-zinc-700"
                        : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900"
                    )}
                  >
                    <span className={cn("h-1.5 w-1.5 rounded-full", getLanguageColor(lang))} />
                    <span>{lang}</span>
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* Repos Table / Skeletons / Empty state */}
          {loading ? (
            <div className="p-5 space-y-3.5">
              <Skeleton className="h-10 w-full rounded-md" />
              <Skeleton className="h-10 w-full rounded-md" />
              <Skeleton className="h-10 w-full rounded-md" />
            </div>
          ) : repos.length === 0 ? (
            <div className="p-12 sm:p-16 text-center border border-dashed border-zinc-800/80 m-5 rounded-xl bg-[#09090b]/40">
              <div className="h-12 w-12 rounded-xl bg-amber-400/10 border border-amber-500/30 text-amber-400 flex items-center justify-center mx-auto mb-4 shadow-[0_0_20px_rgba(245,158,11,0.15)]">
                <GitBranch className="h-6 w-6" />
              </div>
              <h3 className="text-base font-bold text-zinc-100">No repositories connected</h3>
              <p className="text-xs sm:text-sm text-zinc-400 mt-1 max-w-md mx-auto leading-relaxed">
                Connect a GitHub repository to monitor CI failure workflows and trigger autonomous fixes.
              </p>

              <div className="mt-5 grid grid-cols-1 sm:grid-cols-3 gap-2.5 max-w-lg mx-auto text-left">
                <div className="rounded-lg border border-zinc-800/80 bg-[#0c0c0e] p-2.5 text-xs text-zinc-400">
                  <span className="font-semibold text-zinc-200 block">⚡ Zero Overhead</span>
                  Passes run untouched; fixes trigger only on CI failure.
                </div>
                <div className="rounded-lg border border-zinc-800/80 bg-[#0c0c0e] p-2.5 text-xs text-zinc-400">
                  <span className="font-semibold text-zinc-200 block">🛡️ Sandbox Verified</span>
                  Patches verified in isolated mirrors before PRs open.
                </div>
                <div className="rounded-lg border border-zinc-800/80 bg-[#0c0c0e] p-2.5 text-xs text-zinc-400">
                  <span className="font-semibold text-zinc-200 block">⚙️ Custom Routing</span>
                  Override inference models per repository.
                </div>
              </div>

              <Button
                onClick={() => setIsAddModalOpen(true)}
                className="mt-6 bg-gradient-to-b from-amber-400 to-amber-500 text-zinc-950 hover:from-amber-300 hover:to-amber-400 font-bold text-xs h-9 px-4 shadow-[0_1px_0_inset_rgba(255,255,255,0.35),0_2px_8px_rgba(245,158,11,0.25)] cursor-pointer"
                size="sm"
              >
                <Plus className="h-3.5 w-3.5 mr-1.5" />
                <span>Connect First Repository</span>
              </Button>
            </div>
          ) : filteredRepos.length === 0 ? (
            <div className="p-12 text-center text-zinc-500 text-xs space-y-2">
              <Search className="h-6 w-6 mx-auto text-zinc-600 mb-1" />
              <p className="text-zinc-300 font-medium text-sm">No repositories match your filter</p>
              <p>Try clearing your search query or choosing &quot;All&quot; languages.</p>
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  setSearchQuery("");
                  setSelectedLanguage("all");
                }}
                className="mt-2 text-xs border-zinc-700"
              >
                Reset Search Filters
              </Button>
            </div>
          ) : (
            <Table>
              <TableHeader className="bg-[#09090c] border-b border-zinc-800">
                <TableRow className="border-zinc-800/80 hover:bg-transparent">
                  <TableHead className="w-[34%] font-mono text-xs text-zinc-400 uppercase tracking-wider py-3.5">
                    Repository
                  </TableHead>
                  <TableHead className="w-[18%] font-mono text-xs text-zinc-400 uppercase tracking-wider py-3.5">
                    Default Branch
                  </TableHead>
                  <TableHead className="w-[18%] font-mono text-xs text-zinc-400 uppercase tracking-wider py-3.5">
                    Language Hint
                  </TableHead>
                  <TableHead className="w-[16%] font-mono text-xs text-zinc-400 uppercase tracking-wider py-3.5">
                    Connected
                  </TableHead>
                  <TableHead className="w-[14%] font-mono text-xs text-zinc-400 uppercase tracking-wider py-3.5 text-right">
                    Actions
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {filteredRepos.map((repo) => (
                  <TableRow
                    key={repo.id}
                    className="border-zinc-800/70 hover:bg-[#15151a] transition-colors group"
                  >
                    {/* Repository Name & External Links */}
                    <TableCell className="py-3.5">
                      <div className="flex flex-col gap-1">
                        <div className="flex items-center gap-2">
                          <span className="font-mono text-xs font-bold text-zinc-100 group-hover:text-amber-300 transition-colors">
                            {repo.owner}/{repo.name}
                          </span>
                          <a
                            href={`https://github.com/${repo.owner}/${repo.name}`}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-zinc-500 hover:text-zinc-200 transition-colors"
                            title="Open repository on GitHub"
                          >
                            <ExternalLink className="h-3 w-3" />
                          </a>
                        </div>
                        <div className="flex items-center gap-2 text-[11px]">
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
                    <TableCell className="py-3.5">
                      <span className="inline-flex items-center gap-1.5 rounded-md border border-zinc-800 bg-[#0c0c0e] px-2.5 py-1 font-mono text-[11px] text-zinc-300 shadow-inner font-medium">
                        <GitBranch className="h-3 w-3 text-zinc-500" />
                        <span>{repo.default_branch || "main"}</span>
                      </span>
                    </TableCell>

                    {/* Language Hint */}
                    <TableCell className="py-3.5">
                      {repo.language_hint ? (
                        <span className="inline-flex items-center gap-1.5 font-mono text-xs text-zinc-300 bg-zinc-900/60 border border-zinc-800/80 px-2.5 py-1 rounded-md">
                          <span className={cn("h-1.5 w-1.5 rounded-full", getLanguageColor(repo.language_hint))} />
                          <span className="capitalize">{repo.language_hint}</span>
                        </span>
                      ) : (
                        <span className="text-zinc-500 font-mono text-xs bg-zinc-900/40 px-2 py-0.5 rounded border border-zinc-800/50">
                          auto-detect
                        </span>
                      )}
                    </TableCell>

                    {/* Connected Timestamp */}
                    <TableCell className="text-zinc-400 font-mono text-xs py-3.5">
                      <div className="flex items-center gap-1.5">
                        <Clock className="h-3 w-3 text-zinc-600 shrink-0" />
                        <span>{formatRelativeTime(repo.created_at)}</span>
                      </div>
                    </TableCell>

                    {/* Actions */}
                    <TableCell className="text-right py-3.5">
                      <div className="flex items-center justify-end gap-1.5">
                        <Link
                          href="/config"
                          className="h-7 w-7 rounded flex items-center justify-center text-zinc-400 hover:text-cyan-300 hover:bg-zinc-800/80 transition-colors"
                          title="Configure model for this repository"
                        >
                          <Sliders className="h-3.5 w-3.5" />
                        </Link>
                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={() => handleDeleteRepo(repo.id, `${repo.owner}/${repo.name}`)}
                          disabled={deletingId === repo.id}
                          className="h-7 w-7 text-zinc-400 hover:text-red-400 hover:bg-red-950/40 transition-colors cursor-pointer"
                          title="Disconnect repo"
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
