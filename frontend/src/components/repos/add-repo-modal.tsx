"use client";

import { useState, useEffect, useCallback } from "react";
import { Modal } from "@/components/ui/modal";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { api, AvailableRepoOut, RepoOut, ApiError } from "@/lib/api";
import {
  AlertCircle,
  Loader2,
  Search,
  Lock,
  GitBranch,
  CheckCircle2,
  Plus,
  RefreshCw,
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

export function AddRepoModal({ isOpen, onClose, onSuccess }: AddRepoModalProps) {
  const [repos, setRepos] = useState<AvailableRepoOut[]>([]);
  const [loading, setLoading] = useState(false);
  const [fetchError, setFetchError] = useState<{ message: string; status: number } | null>(null);
  const [retryAfter, setRetryAfter] = useState<string | null>(null);
  const [search, setSearch] = useState("");
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

  const filtered = repos.filter((r) =>
    r.full_name.toLowerCase().includes(search.toLowerCase())
  );

  const API_LOGIN_URL = `${process.env.NEXT_PUBLIC_API_URL}/auth/login`;

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title="Connect Repository"
      description="Select a repository with push or admin access to track CI failures."
      className="max-w-2xl"
    >
      <div className="space-y-3">
        {/* Row-level connect error */}
        {connectError && (
          <div className="flex items-center gap-2 rounded-[5px] border border-red-900/60 bg-red-950/30 p-2.5 text-xs text-red-300">
            <AlertCircle className="h-4 w-4 shrink-0 text-red-400" />
            <span>{connectError}</span>
          </div>
        )}

        {/* Fetch error (auth / rate limit / network) */}
        {fetchError && (
          <div className="flex items-start gap-2 rounded-[5px] border border-amber-900/60 bg-amber-950/20 p-2.5 text-xs text-amber-300">
            <AlertCircle className="h-4 w-4 shrink-0 text-amber-400 mt-0.5" />
            <div className="space-y-1">
              {fetchError.status === 401 ? (
                <span>
                  Please re-login to grant repo access.
                  <a
                    href={API_LOGIN_URL}
                    className="ml-1 underline underline-offset-2 text-amber-200 hover:text-white"
                  >
                    Re-login →
                  </a>
                </span>
              ) : fetchError.status === 429 ? (
                <span>
                  GitHub rate limit exceeded.{retryAfter ? ` Retry in ${retryAfter}s.` : ""}{" "}
                  <button
                    onClick={fetchRepos}
                    className="underline underline-offset-2 text-amber-200 hover:text-white"
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

        {/* Search bar */}
        {!fetchError && (
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-zinc-500 pointer-events-none" />
            <Input
              id="repo-picker-search"
              placeholder="Search repos..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              disabled={loading}
              className="pl-8"
              autoFocus
            />
          </div>
        )}

        {/* Repo list */}
        <div className="max-h-[360px] overflow-y-auto space-y-1 pr-0.5">
          {loading ? (
            Array.from({ length: 5 }).map((_, i) => (
              <div
                key={i}
                className="flex items-center gap-3 px-3 py-2.5 rounded-[6px] border border-zinc-800/60"
              >
                <div className="flex-1 space-y-1.5">
                  <Skeleton className="h-3 w-48" />
                  <Skeleton className="h-2.5 w-28" />
                </div>
                <Skeleton className="h-6 w-16 rounded-[4px]" />
              </div>
            ))
          ) : !fetchError && filtered.length === 0 ? (
            <div className="text-center py-8 text-zinc-500 text-xs">
              {repos.length === 0
                ? "No repositories with push access found."
                : "No repos match your search."}
            </div>
          ) : !fetchError ? (
            filtered.map((repo) => {
              const isConnecting = connecting === repo.full_name;
              return (
                <div
                  key={repo.full_name}
                  className={`flex items-center gap-3 px-3 py-2.5 rounded-[6px] border transition-colors ${
                    repo.already_connected
                      ? "border-zinc-800/40 bg-zinc-900/20 opacity-60"
                      : "border-zinc-800/60 bg-zinc-900/40 hover:bg-zinc-800/40 hover:border-zinc-700/60"
                  }`}
                >
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-1.5 flex-wrap">
                      <span className="text-xs font-medium text-zinc-100 truncate">
                        {repo.full_name}
                      </span>
                      {repo.private && (
                        <span className="inline-flex items-center gap-0.5 rounded px-1 py-0.5 text-[10px] font-medium bg-zinc-800 text-zinc-400 border border-zinc-700/60 shrink-0">
                          <Lock className="h-2.5 w-2.5" />
                          private
                        </span>
                      )}
                    </div>
                    <div className="flex items-center gap-2 mt-0.5 text-[10px] text-zinc-500">
                      {repo.default_branch && (
                        <span className="flex items-center gap-0.5">
                          <GitBranch className="h-2.5 w-2.5" />
                          {repo.default_branch}
                        </span>
                      )}
                      {repo.language && <span>{repo.language}</span>}
                      <span>{relativeTime(repo.updated_at)}</span>
                    </div>
                  </div>

                  {repo.already_connected ? (
                    <span className="inline-flex items-center gap-1 text-[10px] text-zinc-500 shrink-0">
                      <CheckCircle2 className="h-3 w-3" />
                      Connected
                    </span>
                  ) : (
                    <Button
                      id={`connect-${repo.full_name.replace("/", "-")}`}
                      size="sm"
                      disabled={!!connecting}
                      onClick={() => handleConnect(repo)}
                      className="h-6 px-2 text-[10px] shrink-0 bg-amber-400 text-zinc-950 hover:bg-amber-300 disabled:opacity-40"
                    >
                      {isConnecting ? (
                        <Loader2 className="h-3 w-3 animate-spin" />
                      ) : (
                        <>
                          <Plus className="h-3 w-3 mr-0.5" />
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
          <div className="flex items-center justify-between pt-2 border-t border-zinc-800/60 text-[10px] text-zinc-600">
            <span>Showing up to 300 repos with push or admin access.</span>
            <button
              onClick={fetchRepos}
              className="flex items-center gap-1 text-zinc-500 hover:text-zinc-300 transition-colors"
            >
              <RefreshCw className="h-3 w-3" />
              Refresh
            </button>
          </div>
        )}
      </div>
    </Modal>
  );
}
