"use client";

import { useState, useEffect, useCallback, useMemo } from "react";
import { Modal } from "@/components/ui/modal";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { api, AvailableRepoOut, RepoOut, ApiError } from "@/lib/api";
import { cn } from "@/lib/utils";
import {
  AlertCircle,
  Loader2,
  Search,
  Lock,
  GitBranch,
  CheckCircle2,
  Plus,
  RefreshCw,
  FolderGit2,
  X,
  Globe2,
} from "lucide-react";

interface AddRepoModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSuccess: (repo: RepoOut) => void;
}

function relativeTime(iso: string | null): string {
  if (!iso) return "—";
  const diff = Date.now() - new Date(iso).getTime();
  const days = Math.floor(diff / 86_400_000);
  if (days === 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 30) return `${days}d ago`;
  const months = Math.floor(days / 30);
  if (months < 12) return `${months}mo ago`;
  return `${Math.floor(months / 12)}y ago`;
}

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

export function AddRepoModal({ isOpen, onClose, onSuccess }: AddRepoModalProps) {
  const [repos, setRepos] = useState<AvailableRepoOut[]>([]);
  const [loading, setLoading] = useState(false);
  const [fetchError, setFetchError] = useState<{ message: string; status: number } | null>(null);
  const [retryAfter, setRetryAfter] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [visibilityFilter, setVisibilityFilter] = useState<"all" | "public" | "private">("all");
  const [connecting, setConnecting] = useState<string | null>(null);
  const [connectError, setConnectError] = useState<string | null>(null);

  const fetchRepos = useCallback(async () => {
    setLoading(true);
    setFetchError(null);
    setRetryAfter(null);
    try {
      const data = await api.getAvailableRepos();
      setRepos(data);
    } catch (err) {
      if (err instanceof ApiError) {
        setFetchError({ message: err.message, status: err.status });
        if (err.status === 429) setRetryAfter("60");
      } else {
        setFetchError({ message: "Failed to load repositories.", status: 0 });
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (isOpen) {
      setSearch("");
      setVisibilityFilter("all");
      setConnectError(null);
      fetchRepos();
    }
  }, [isOpen, fetchRepos]);

  const handleConnect = async (repo: AvailableRepoOut) => {
    setConnecting(repo.full_name);
    setConnectError(null);
    try {
      const newRepo = await api.addRepo({
        owner: repo.owner,
        name: repo.name,
        default_branch: repo.default_branch ?? "main",
        language_hint: repo.language?.toLowerCase() ?? null,
      });
      onSuccess(newRepo);
      onClose();
    } catch (err) {
      setConnectError(err instanceof Error ? err.message : "Failed to connect repository.");
    } finally {
      setConnecting(null);
    }
  };

  const filtered = useMemo(() => {
    return repos.filter((r) => {
      const matchesSearch = r.full_name.toLowerCase().includes(search.toLowerCase());
      if (!matchesSearch) return false;
      if (visibilityFilter === "public") return !r.private;
      if (visibilityFilter === "private") return r.private;
      return true;
    });
  }, [repos, search, visibilityFilter]);

  const API_LOGIN_URL = `${process.env.NEXT_PUBLIC_API_URL}/auth/login`;

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title="Connect Repository"
      description="Select a repository with push or admin access to track CI failures."
      className="max-w-2xl bg-[#0f0f13] border-zinc-800 shadow-[0_16px_48px_rgba(0,0,0,0.8),inset_0_1px_0_0_rgba(255,255,255,0.05)]"
    >
      <div className="space-y-3.5">
        {/* Row-level connect error */}
        {connectError && (
          <div className="flex items-center gap-2 rounded-lg border border-red-900/80 bg-red-950/40 p-3 text-xs text-red-300 shadow-md">
            <AlertCircle className="h-4 w-4 shrink-0 text-red-400" />
            <span>{connectError}</span>
          </div>
        )}

        {/* Fetch error (auth / rate limit / network) */}
        {fetchError && (
          <div className="flex items-start gap-2.5 rounded-lg border border-amber-900/80 bg-amber-950/30 p-3 text-xs text-amber-300 shadow-md">
            <AlertCircle className="h-4 w-4 shrink-0 text-amber-400 mt-0.5" />
            <div className="space-y-1">
              {fetchError.status === 401 ? (
                <span>
                  Please re-login to grant repo access.
                  <a
                    href={API_LOGIN_URL}
                    className="ml-1 underline underline-offset-2 text-amber-200 hover:text-white font-medium"
                  >
                    Re-login →
                  </a>
                </span>
              ) : fetchError.status === 429 ? (
                <span>
                  GitHub rate limit exceeded.{retryAfter ? ` Retry in ${retryAfter}s.` : ""}{" "}
                  <button
                    onClick={fetchRepos}
                    className="underline underline-offset-2 text-amber-200 hover:text-white font-semibold cursor-pointer"
                  >
                    Try again
                  </button>
                </span>
              ) : (
                <span>{fetchError.message}</span>
              )}
            </div>
          </div>
        )}

        {/* Search bar & Visibility filter */}
        {!fetchError && (
          <div className="flex flex-col sm:flex-row sm:items-center gap-2.5">
            <div className="relative flex-1">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-zinc-500 pointer-events-none" />
              <Input
                id="repo-picker-search"
                placeholder="Search repos..."
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                disabled={loading}
                className="pl-8 pr-7 h-9 border-zinc-800 bg-[#07070a] font-mono text-xs text-zinc-200 placeholder:text-zinc-500 focus:border-amber-400/80 focus:ring-1 focus:ring-amber-400/30 rounded-lg shadow-inner"
                autoFocus
              />
              {search && (
                <button
                  type="button"
                  onClick={() => setSearch("")}
                  className="absolute right-2.5 top-1/2 -translate-y-1/2 text-zinc-500 hover:text-zinc-300 cursor-pointer"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              )}
            </div>

            <div className="inline-flex rounded-lg border border-zinc-800 bg-[#07070a] p-0.5 text-xs self-start sm:self-auto shrink-0 shadow-inner">
              <button
                type="button"
                onClick={() => setVisibilityFilter("all")}
                className={cn(
                  "px-2.5 py-1 rounded-md text-[11px] font-mono font-medium transition-colors cursor-pointer",
                  visibilityFilter === "all"
                    ? "bg-zinc-800 text-zinc-100 font-semibold"
                    : "text-zinc-400 hover:text-zinc-200"
                )}
              >
                All
              </button>
              <button
                type="button"
                onClick={() => setVisibilityFilter("public")}
                className={cn(
                  "px-2.5 py-1 rounded-md text-[11px] font-mono font-medium transition-colors cursor-pointer",
                  visibilityFilter === "public"
                    ? "bg-zinc-800 text-zinc-100 font-semibold"
                    : "text-zinc-400 hover:text-zinc-200"
                )}
              >
                Public
              </button>
              <button
                type="button"
                onClick={() => setVisibilityFilter("private")}
                className={cn(
                  "px-2.5 py-1 rounded-md text-[11px] font-mono font-medium transition-colors cursor-pointer",
                  visibilityFilter === "private"
                    ? "bg-zinc-800 text-zinc-100 font-semibold"
                    : "text-zinc-400 hover:text-zinc-200"
                )}
              >
                Private
              </button>
            </div>
          </div>
        )}

        {/* Repo list */}
        <div className="max-h-[380px] overflow-y-auto space-y-1.5 pr-0.5">
          {loading ? (
            Array.from({ length: 5 }).map((_, i) => (
              <div
                key={i}
                className="flex items-center gap-3 px-3.5 py-3 rounded-lg border border-zinc-800/60 bg-[#0a0a0d]"
              >
                <div className="flex-1 space-y-2">
                  <Skeleton className="h-3.5 w-48 rounded" />
                  <Skeleton className="h-2.5 w-28 rounded" />
                </div>
                <Skeleton className="h-7 w-20 rounded-md" />
              </div>
            ))
          ) : !fetchError && filtered.length === 0 ? (
            <div className="text-center py-10 text-zinc-500 text-xs border border-dashed border-zinc-800 rounded-lg p-6 bg-[#07070a]/40">
              <FolderGit2 className="h-6 w-6 text-zinc-600 mx-auto mb-2" />
              <span className="text-zinc-300 font-medium block">
                {repos.length === 0
                  ? "No repositories with push access found."
                  : "No repos match your search."}
              </span>
              <span className="text-zinc-500 text-[11px] mt-0.5 block">
                Verify that your GitHub account or Organization has push permissions.
              </span>
            </div>
          ) : !fetchError ? (
            filtered.map((repo) => {
              const isConnecting = connecting === repo.full_name;
              return (
                <div
                  key={repo.full_name}
                  className={cn(
                    "flex items-center gap-3 px-3.5 py-2.5 rounded-lg border transition-all duration-150 group",
                    repo.already_connected
                      ? "border-zinc-800/40 bg-[#070709]/60 opacity-60"
                      : "border-zinc-800/80 bg-[#0a0a0d] hover:bg-[#121217] hover:border-zinc-700 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.02)]"
                  )}
                >
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-xs font-mono font-bold text-zinc-100 truncate group-hover:text-amber-300 transition-colors">
                        {repo.full_name}
                      </span>
                      {repo.private ? (
                        <span className="inline-flex items-center gap-1 rounded px-1.5 py-0.2 text-[10px] font-mono font-medium bg-zinc-900 text-zinc-400 border border-zinc-700/60 shrink-0">
                          <Lock className="h-2.5 w-2.5" />
                          private
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-1 rounded px-1.5 py-0.2 text-[10px] font-mono font-medium bg-zinc-900/60 text-zinc-400 border border-zinc-800 shrink-0">
                          <Globe2 className="h-2.5 w-2.5 text-zinc-400" />
                          public
                        </span>
                      )}
                    </div>
                    <div className="flex items-center gap-3 mt-1 text-[11px] text-zinc-400 font-mono">
                      {repo.default_branch && (
                        <span className="flex items-center gap-1 text-zinc-400">
                          <GitBranch className="h-3 w-3 text-zinc-400" />
                          <span>{repo.default_branch}</span>
                        </span>
                      )}
                      {repo.language && (
                        <span className="flex items-center gap-1.5">
                          <span className={cn("h-1.5 w-1.5 rounded-full", getLanguageColor(repo.language))} />
                          <span className="text-zinc-300">{repo.language}</span>
                        </span>
                      )}
                      <span className="text-zinc-400">{relativeTime(repo.updated_at)}</span>
                    </div>
                  </div>

                  {repo.already_connected ? (
                    <span className="inline-flex items-center gap-1.5 text-[11px] font-mono font-medium text-zinc-400 bg-zinc-900 border border-zinc-800 px-2.5 py-1 rounded-md shrink-0">
                      <CheckCircle2 className="h-3 w-3 text-emerald-400" />
                      Connected
                    </span>
                  ) : (
                    <Button
                      id={`connect-${repo.full_name.replace("/", "-")}`}
                      size="sm"
                      disabled={!!connecting}
                      onClick={() => handleConnect(repo)}
                      className="h-7 px-3 text-xs font-bold shrink-0 bg-gradient-to-b from-amber-400 to-amber-500 text-zinc-950 hover:from-amber-300 hover:to-amber-400 shadow-[0_1px_0_inset_rgba(255,255,255,0.35),0_2px_6px_rgba(245,158,11,0.2)] disabled:opacity-40 cursor-pointer"
                    >
                      {isConnecting ? (
                        <Loader2 className="h-3 w-3 animate-spin" />
                      ) : (
                        <>
                          <Plus className="h-3 w-3 mr-1" />
                          Connect
                        </>
                      )}
                    </Button>
                  )}
                </div>
              );
            })
          ) : null}
        </div>

        {/* Footer: cap note + refresh */}
        {!loading && !fetchError && repos.length > 0 && (
          <div className="flex items-center justify-between pt-2.5 border-t border-zinc-800/80 text-[11px] text-zinc-400 font-mono">
            <span>Showing up to 300 repos with push or admin access.</span>
            <button
              onClick={fetchRepos}
              className="flex items-center gap-1.5 text-zinc-400 hover:text-amber-400 transition-colors cursor-pointer"
            >
              <RefreshCw className="h-3 w-3" />
              <span>Refresh</span>
            </button>
          </div>
        )}
      </div>
    </Modal>
  );
}
