"use client";

import { useEffect, useState, useCallback } from "react";
import { AppLayout } from "@/components/layout/app-layout";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { AddRepoModal } from "@/components/repos/add-repo-modal";
import { api, RepoOut } from "@/lib/api";
import { formatRelativeTime } from "@/lib/utils";
import { GitBranch, Trash2, Plus, AlertCircle, ExternalLink, Code } from "lucide-react";

export default function ReposPage() {
  const [repos, setRepos] = useState<RepoOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [isAddModalOpen, setIsAddModalOpen] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);

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
    } catch (err: unknown) {
      alert("Failed to disconnect repository.");
    } finally {
      setDeletingId(null);
    }
  };

  const handleRepoAdded = (newRepo: RepoOut) => {
    setRepos((prev) => [newRepo, ...prev]);
  };

  return (
    <AppLayout
      title="Connected Repositories"
      subtitle="Manage tracked repositories and autonomous fix pipelines"
      actions={
        <Button
          onClick={() => setIsAddModalOpen(true)}
          className="flex items-center gap-1.5 bg-amber-400 text-zinc-950 hover:bg-amber-300 font-semibold"
          size="sm"
        >
          <Plus className="h-3.5 w-3.5" />
          Connect Repository
        </Button>
      }
    >
      <div className="space-y-4">
        {error && (
          <div className="flex items-center gap-2 rounded-[6px] border border-red-900/60 bg-red-950/30 p-3 text-xs text-red-300">
            <AlertCircle className="h-4 w-4 shrink-0 text-red-400" />
            <span>{error}</span>
          </div>
        )}

        {/* Repos Table / Skeletons / Empty state */}
        <div className="rounded-[6px] border border-zinc-800 bg-[#121215] overflow-hidden">
          {loading ? (
            <div className="p-4 space-y-3">
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full" />
            </div>
          ) : repos.length === 0 ? (
            <div className="p-12 text-center border border-dashed border-zinc-800/80 m-4 rounded-[6px]">
              <GitBranch className="h-6 w-6 mx-auto text-zinc-600 mb-2" />
              <h3 className="text-sm font-medium text-zinc-300">No repositories connected</h3>
              <p className="text-xs text-zinc-500 mt-1 max-w-sm mx-auto">
                Connect a GitHub repository to monitor CI failure workflows and trigger autonomous fixes.
              </p>
              <Button
                onClick={() => setIsAddModalOpen(true)}
                variant="outline"
                size="sm"
                className="mt-4"
              >
                <Plus className="h-3.5 w-3.5 mr-1" />
                Connect First Repository
              </Button>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-[35%]">Repository</TableHead>
                  <TableHead className="w-[20%]">Default Branch</TableHead>
                  <TableHead className="w-[20%]">Language Hint</TableHead>
                  <TableHead className="w-[15%]">Connected</TableHead>
                  <TableHead className="w-[10%] text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {repos.map((repo) => (
                  <TableRow key={repo.id}>
                    <TableCell>
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-xs font-semibold text-zinc-100">
                          {repo.owner}/{repo.name}
                        </span>
                        <a
                          href={`https://github.com/${repo.owner}/${repo.name}`}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-zinc-500 hover:text-zinc-300 transition-colors"
                        >
                          <ExternalLink className="h-3 w-3" />
                        </a>
                      </div>
                    </TableCell>

                    <TableCell>
                      <span className="rounded-[4px] border border-zinc-800 bg-[#0c0c0e] px-2 py-0.5 font-mono text-[11px] text-zinc-300">
                        {repo.default_branch || "main"}
                      </span>
                    </TableCell>

                    <TableCell>
                      {repo.language_hint ? (
                        <span className="inline-flex items-center gap-1 font-mono text-xs text-zinc-300">
                          <Code className="h-3 w-3 text-zinc-500" />
                          {repo.language_hint}
                        </span>
                      ) : (
                        <span className="text-zinc-600 font-mono text-xs">auto-detect</span>
                      )}
                    </TableCell>

                    <TableCell className="text-zinc-400 font-mono text-xs">
                      {formatRelativeTime(repo.created_at)}
                    </TableCell>

                    <TableCell className="text-right">
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() => handleDeleteRepo(repo.id, `${repo.owner}/${repo.name}`)}
                        disabled={deletingId === repo.id}
                        className="h-7 w-7 text-zinc-500 hover:text-red-400 hover:bg-red-950/30"
                        title="Disconnect repo"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
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
