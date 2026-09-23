"use client";

/**
 * /sessions — Live Sessions dashboard.
 *
 * Lists all pairing sessions for the authenticated user.
 * Provides "Resume Session", "Close" actions, and a "Start New Session" modal.
 *
 * Design: obsidian dark theme consistent with the Haunter sidebar aesthetic.
 * Amber accent color, zinc-800 borders, painted-light primary CTAs.
 */

import { useEffect, useState, useCallback } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  Zap,
  GitBranch,
  Plus,
  X,
  ExternalLink,
  ChevronRight,
  Circle,
  CheckCircle2,
  XCircle,
  Loader2,
  Terminal,
  FileCode2,
  AlertTriangle,
} from "lucide-react";
import {
  api,
  SessionOut,
  SessionCreateIn,
  RepoOut,
  ApiError,
} from "@/lib/api";
import { AppLayout } from "@/components/layout/app-layout";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function StatusChip({ status }: { status: string }) {
  if (status === "active") {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-[5px] border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-[11px] font-mono font-medium text-emerald-300">
        <span className="relative flex h-1.5 w-1.5">
          <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-60" />
          <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-emerald-400" />
        </span>
        active
      </span>
    );
  }
  if (status === "completed") {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-[5px] border border-violet-500/30 bg-violet-500/10 px-2 py-0.5 text-[11px] font-mono font-medium text-violet-300">
        <CheckCircle2 className="h-3 w-3" />
        completed
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5 rounded-[5px] border border-zinc-700/60 bg-zinc-800/60 px-2 py-0.5 text-[11px] font-mono font-medium text-zinc-400">
      <Circle className="h-3 w-3" />
      {status}
    </span>
  );
}

function PatchCountBadge({ count }: { count: number }) {
  if (count === 0) return null;
  return (
    <span className="inline-flex items-center gap-1 rounded-[4px] border border-amber-500/30 bg-amber-500/10 px-1.5 py-0.5 text-[10px] font-mono text-amber-300">
      <FileCode2 className="h-3 w-3" />
      {count} patch{count !== 1 ? "es" : ""}
    </span>
  );
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// ---------------------------------------------------------------------------
// New Session Modal
// ---------------------------------------------------------------------------

interface NewSessionModalProps {
  repos: RepoOut[];
  onClose: () => void;
  onCreated: (session: SessionOut) => void;
}

function NewSessionModal({ repos, onClose, onCreated }: NewSessionModalProps) {
  const [repoId, setRepoId] = useState(repos[0]?.id ?? "");
  const [branch, setBranch] = useState("");
  const [title, setTitle] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!repoId) {
      setError("Select a repository.");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const selectedRepo = repos.find((r) => r.id === repoId);
      const payload: SessionCreateIn = {
        repo_id: repoId,
        branch_name: branch.trim() || selectedRepo?.default_branch || null,
        title: title.trim() || null,
      };
      const session = await api.createSession(payload);
      onCreated(session);
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : "Failed to create session.";
      setError(msg);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="relative w-full max-w-md rounded-xl border-t border-t-zinc-600/50 border-x border-x-zinc-700/60 border-b border-b-zinc-900 bg-gradient-to-b from-[#14141a] to-[#0d0d11] p-6 shadow-[0_24px_64px_rgba(0,0,0,0.7),inset_0_1px_0_rgba(255,255,255,0.08)]">
        {/* Header */}
        <div className="flex items-center justify-between mb-6">
          <div className="flex items-center gap-2.5">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg border border-amber-500/30 bg-amber-500/10 text-amber-400">
              <Zap className="h-4 w-4" />
            </div>
            <h2 className="text-sm font-semibold text-zinc-100">Start Live Session</h2>
          </div>
          <button
            onClick={onClose}
            className="flex h-7 w-7 items-center justify-center rounded-md text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800/60 transition-colors"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          {/* Repo selector */}
          <div className="space-y-1.5">
            <label className="block text-[11px] font-medium text-zinc-400 uppercase tracking-wide font-mono">
              Repository
            </label>
            {repos.length === 0 ? (
              <p className="text-xs text-zinc-500 font-mono">
                No repositories connected. Add one in{" "}
                <Link href="/repos" className="text-amber-400 hover:underline">
                  Repositories
                </Link>
                .
              </p>
            ) : (
              <select
                id="session-repo-select"
                value={repoId}
                onChange={(e) => setRepoId(e.target.value)}
                className="w-full rounded-lg border border-zinc-700/60 bg-zinc-900/80 px-3 py-2 text-sm text-zinc-200 focus:outline-none focus:ring-1 focus:ring-amber-500/50 transition-all"
              >
                {repos.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.owner}/{r.name}
                  </option>
                ))}
              </select>
            )}
          </div>

          {/* Branch */}
          <div className="space-y-1.5">
            <label className="block text-[11px] font-medium text-zinc-400 uppercase tracking-wide font-mono">
              Branch{" "}
              <span className="text-zinc-600 normal-case tracking-normal">(leave blank for default)</span>
            </label>
            <input
              id="session-branch-input"
              type="text"
              value={branch}
              onChange={(e) => setBranch(e.target.value)}
              placeholder={
                repos.find((r) => r.id === repoId)?.default_branch ?? "main"
              }
              className="w-full rounded-lg border border-zinc-700/60 bg-zinc-900/80 px-3 py-2 text-sm text-zinc-200 placeholder-zinc-600 focus:outline-none focus:ring-1 focus:ring-amber-500/50 transition-all font-mono"
            />
          </div>

          {/* Title */}
          <div className="space-y-1.5">
            <label className="block text-[11px] font-medium text-zinc-400 uppercase tracking-wide font-mono">
              Session Title
            </label>
            <input
              id="session-title-input"
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Fix: broken auth middleware"
              maxLength={255}
              className="w-full rounded-lg border border-zinc-700/60 bg-zinc-900/80 px-3 py-2 text-sm text-zinc-200 placeholder-zinc-600 focus:outline-none focus:ring-1 focus:ring-amber-500/50 transition-all"
            />
          </div>

          {error && (
            <div className="flex items-center gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300">
              <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
              {error}
            </div>
          )}

          <div className="flex items-center gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="flex-1 rounded-lg border border-zinc-700/60 bg-zinc-800/60 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-700/60 hover:text-zinc-100 transition-colors"
            >
              Cancel
            </button>
            <button
              id="session-create-submit"
              type="submit"
              disabled={loading || repos.length === 0}
              className="flex-1 flex items-center justify-center gap-2 rounded-lg border-t border-t-amber-400/40 border-x border-x-amber-500/30 border-b border-b-amber-600/20 bg-gradient-to-b from-amber-500/20 via-amber-500/15 to-amber-600/10 px-4 py-2 text-sm font-semibold text-amber-200 shadow-[inset_0_1px_0_rgba(255,255,255,0.15),0_2px_4px_rgba(0,0,0,0.3)] hover:from-amber-500/25 hover:via-amber-500/20 hover:to-amber-600/15 active:translate-y-px active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.25)] transition-all disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {loading ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Zap className="h-4 w-4" />
              )}
              {loading ? "Starting…" : "Start Session"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Session Card
// ---------------------------------------------------------------------------

interface SessionCardProps {
  session: SessionOut;
  onClose: (id: string) => void;
}

function SessionCard({ session, onClose }: SessionCardProps) {
  const patchCount = Object.keys(session.staged_patches ?? {}).length;

  return (
    <div className="group relative overflow-hidden rounded-xl border-t border-t-zinc-600/50 border-x border-x-zinc-700/60 border-b border-b-zinc-900 bg-gradient-to-b from-[#13131a] to-[#0d0d11] p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_4px_12px_rgba(0,0,0,0.4)] transition-all duration-200 hover:border-t-zinc-500/60 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.09),0_4px_20px_rgba(0,0,0,0.5)]">
      {/* Ambient top glow on hover */}
      <span
        aria-hidden="true"
        className="pointer-events-none absolute inset-x-0 top-0 h-16 bg-gradient-to-b from-white/[0.03] to-transparent opacity-0 group-hover:opacity-100 transition-opacity duration-300"
      />

      <div className="relative">
        {/* Top row: title + status */}
        <div className="flex items-start justify-between gap-3 mb-3">
          <div className="min-w-0 flex-1">
            <h3 className="text-sm font-semibold text-zinc-100 truncate">{session.title}</h3>
            <div className="flex items-center gap-1.5 mt-0.5">
              <GitBranch className="h-3 w-3 text-zinc-500 shrink-0" />
              <span className="text-[11px] font-mono text-zinc-400 truncate">
                {session.repo_owner}/{session.repo_name}
              </span>
            </div>
          </div>
          <StatusChip status={session.status} />
        </div>

        {/* Meta row: branch, sha, patches */}
        <div className="flex flex-wrap items-center gap-2 mb-4">
          <span className="inline-flex items-center gap-1 rounded-[4px] border border-zinc-700/60 bg-zinc-900/60 px-1.5 py-0.5 text-[10px] font-mono text-zinc-400">
            <Terminal className="h-3 w-3" />
            {session.branch_name}
          </span>
          <span className="inline-flex items-center gap-1 rounded-[4px] border border-zinc-700/60 bg-zinc-900/60 px-1.5 py-0.5 text-[10px] font-mono text-zinc-500">
            {session.base_sha.slice(0, 8)}
          </span>
          <PatchCountBadge count={patchCount} />
        </div>

        {/* Timestamp */}
        <p className="text-[10px] font-mono text-zinc-600 mb-4">
          {formatDate(session.created_at)}
        </p>

        {/* Actions */}
        <div className="flex items-center gap-2">
          <Link
            href={`/sessions/workspace?id=${session.id}`}
            id={`session-resume-${session.id}`}
            className="flex flex-1 items-center justify-center gap-1.5 rounded-lg border-t border-t-amber-400/40 border-x border-x-amber-500/30 border-b border-b-amber-600/20 bg-gradient-to-b from-amber-500/20 via-amber-500/15 to-amber-600/10 px-3 py-1.5 text-xs font-semibold text-amber-200 shadow-[inset_0_1px_0_rgba(255,255,255,0.15),0_2px_4px_rgba(0,0,0,0.3)] hover:from-amber-500/25 active:translate-y-px transition-all"
          >
            <ChevronRight className="h-3.5 w-3.5" />
            {session.status === "active" ? "Resume" : "View"}
          </Link>

          {session.status === "active" && (
            <button
              onClick={() => onClose(session.id)}
              id={`session-close-${session.id}`}
              className="flex items-center justify-center gap-1 rounded-lg border border-zinc-700/60 bg-zinc-800/60 px-3 py-1.5 text-xs text-zinc-400 hover:text-red-300 hover:border-red-500/30 hover:bg-red-500/10 transition-colors"
            >
              <XCircle className="h-3.5 w-3.5" />
              Close
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function SessionsPage() {
  const router = useRouter();
  const [sessions, setSessions] = useState<SessionOut[]>([]);
  const [total, setTotal] = useState(0);
  const [repos, setRepos] = useState<RepoOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showModal, setShowModal] = useState(false);

  const loadSessions = useCallback(async () => {
    try {
      const data = await api.listSessions({ limit: 50 });
      setSessions(data.sessions);
      setTotal(data.total);
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : "Failed to load sessions.";
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadSessions();
    api.getRepos().then(setRepos).catch(() => {});
  }, [loadSessions]);

  const handleClose = async (sessionId: string) => {
    try {
      await api.closeSession(sessionId);
      await loadSessions();
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : "Failed to close session.";
      setError(msg);
    }
  };

  const handleCreated = (session: SessionOut) => {
    setShowModal(false);
    router.push(`/sessions/workspace?id=${session.id}`);
  };

  return (
    <AppLayout title="Live Sessions" subtitle="Cloud agentic pairing workspace">
      <div className="min-h-screen bg-[#09090b] px-6 py-8">
        {/* Page header */}
        <div className="mb-8 flex items-start justify-between gap-4">
          <div>
            <div className="flex items-center gap-2.5 mb-1">
              <div className="flex h-8 w-8 items-center justify-center rounded-lg border border-amber-500/30 bg-amber-500/10 text-amber-400">
                <Zap className="h-4.5 w-4.5" />
              </div>
              <h1 className="text-lg font-bold text-zinc-100 tracking-tight">Live Sessions</h1>
            </div>
            <p className="text-xs font-mono text-zinc-500 ml-10.5">
              {total > 0 ? `${total} session${total !== 1 ? "s" : ""}` : "No sessions yet"} — cloud agentic pairing workspace
            </p>
          </div>

          <button
            id="start-new-session-btn"
            onClick={() => setShowModal(true)}
            className="flex items-center gap-2 rounded-lg border-t border-t-amber-400/40 border-x border-x-amber-500/30 border-b border-b-amber-600/20 bg-gradient-to-b from-amber-500/20 via-amber-500/15 to-amber-600/10 px-4 py-2 text-sm font-semibold text-amber-200 shadow-[inset_0_1px_0_rgba(255,255,255,0.15),0_2px_4px_rgba(0,0,0,0.3)] hover:from-amber-500/25 active:translate-y-px transition-all"
          >
            <Plus className="h-4 w-4" />
            New Session
          </button>
        </div>

        {/* Error */}
        {error && (
          <div className="mb-6 flex items-center gap-2 rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-300">
            <AlertTriangle className="h-4 w-4 shrink-0" />
            {error}
          </div>
        )}

        {/* Loading */}
        {loading && (
          <div className="flex items-center justify-center py-20">
            <Loader2 className="h-6 w-6 animate-spin text-amber-400" />
          </div>
        )}

        {/* Empty state */}
        {!loading && sessions.length === 0 && !error && (
          <div className="flex flex-col items-center justify-center py-24 text-center">
            <div className="flex h-14 w-14 items-center justify-center rounded-2xl border border-zinc-700/60 bg-zinc-900/60 text-zinc-600 mb-4">
              <Zap className="h-7 w-7" />
            </div>
            <p className="text-sm font-medium text-zinc-400 mb-1">No live sessions</p>
            <p className="text-xs text-zinc-600 font-mono mb-6">
              Start a session to pair with the AI agent on a repository
            </p>
            <button
              onClick={() => setShowModal(true)}
              className="flex items-center gap-2 rounded-lg border-t border-t-amber-400/40 border-x border-x-amber-500/30 border-b border-b-amber-600/20 bg-gradient-to-b from-amber-500/20 via-amber-500/15 to-amber-600/10 px-5 py-2.5 text-sm font-semibold text-amber-200 shadow-[inset_0_1px_0_rgba(255,255,255,0.15),0_2px_4px_rgba(0,0,0,0.3)] transition-all"
            >
              <Plus className="h-4 w-4" />
              Start First Session
            </button>
          </div>
        )}

        {/* Session grid */}
        {!loading && sessions.length > 0 && (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
            {sessions.map((session) => (
              <SessionCard
                key={session.id}
                session={session}
                onClose={handleClose}
              />
            ))}
          </div>
        )}
      </div>

      {/* New Session Modal */}
      {showModal && (
        <NewSessionModal
          repos={repos}
          onClose={() => setShowModal(false)}
          onCreated={handleCreated}
        />
      )}
    </AppLayout>
  );
}
