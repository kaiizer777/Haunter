"use client";

/**
 * /sessions/[id] -- Cloud Agentic Live Session workspace.
 *
 * Layout:
 *   Top action bar    -- title, repo badge, branch, status chip, action buttons
 *   2-col split       -- 37% Chat Dock | 63% Monaco DiffEditor workspace
 *
 * Features:
 *   - Real-time SSE agent chat via useSessionStream
 *   - Monaco DiffEditor (loaded dynamically, ssr:false)
 *   - Multi-file tab bar for staged patches
 *   - Run sandbox verification with inline result panel
 *   - Commit & Open PR modal with title + optional body
 *   - Close session button
 */

import { use, useCallback, useEffect, useRef, useState } from "react";
import dynamic from "next/dynamic";
import Link from "next/link";
import {
  GitBranch,
  Zap,
  Terminal,
  Send,
  FlaskConical,
  GitPullRequest,
  X,
  CheckCircle2,
  XCircle,
  Circle,
  Loader2,
  ChevronLeft,
  FileCode2,
  ChevronDown,
  ChevronUp,
  AlertTriangle,
  ExternalLink,
  WrapText,
} from "lucide-react";
import { api, SessionOut, SessionCommitIn, ApiError } from "@/lib/api";
import { useSessionStream, ChatMessage, ToolCallChip } from "@/hooks/useSessionStream";
import { AppLayout } from "@/components/layout/app-layout";

// ---------------------------------------------------------------------------
// Monaco DiffEditor -- loaded dynamically (ssr:false, browser APIs required)
// ---------------------------------------------------------------------------

const MonacoDiffEditor = dynamic(
  () => import("@monaco-editor/react").then((m) => m.DiffEditor),
  {
    ssr: false,
    loading: () => (
      <div className="flex h-full items-center justify-center bg-[#0d0d0f] text-zinc-600 text-xs font-mono">
        Loading editor…
      </div>
    ),
  }
);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function StatusChip({ status }: { status: string }) {
  if (status === "active") {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-[5px] border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-[10px] font-mono font-medium text-emerald-300">
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
      <span className="inline-flex items-center gap-1.5 rounded-[5px] border border-violet-500/30 bg-violet-500/10 px-2 py-0.5 text-[10px] font-mono font-medium text-violet-300">
        <CheckCircle2 className="h-3 w-3" />
        completed
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5 rounded-[5px] border border-zinc-700/60 bg-zinc-800/60 px-2 py-0.5 text-[10px] font-mono font-medium text-zinc-400">
      <Circle className="h-3 w-3" />
      {status}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Chat message renderer
// ---------------------------------------------------------------------------

function ThoughtAccordion({ thoughts }: { thoughts: string[] }) {
  const [open, setOpen] = useState(false);
  if (!thoughts.length) return null;
  return (
    <div className="mt-2 rounded-lg border border-zinc-800/80 bg-zinc-900/40 overflow-hidden">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between px-3 py-1.5 text-[10px] font-mono text-zinc-500 hover:text-zinc-400 hover:bg-zinc-800/30 transition-colors"
      >
        <span>thoughts ({thoughts.length})</span>
        {open ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
      </button>
      {open && (
        <div className="px-3 pb-3 space-y-1.5 border-t border-zinc-800/60">
          {thoughts.map((t, i) => (
            <p key={i} className="text-[11px] font-mono text-zinc-500 leading-relaxed">
              {t}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}

function ToolChip({ chip }: { chip: ToolCallChip }) {
  const iconMap: Record<string, string> = {
    read_file: "📄",
    stage_patch: "📝",
    discard_patch: "🗑",
    list_files: "📂",
  };
  return (
    <span className="inline-flex items-center gap-1 rounded-[4px] border border-zinc-700/60 bg-zinc-800/60 px-1.5 py-0.5 text-[10px] font-mono text-zinc-400">
      {iconMap[chip.name] ?? "⚡"} {chip.name}
    </span>
  );
}

function ChatBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user";
  const isSystem = message.role === "system";

  if (isSystem) {
    return (
      <div className="flex items-start gap-2 py-1">
        <div className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-zinc-800 text-zinc-500 text-[9px] font-mono mt-0.5">
          SYS
        </div>
        <p className="text-[11px] font-mono text-zinc-500 leading-relaxed">{message.content}</p>
      </div>
    );
  }

  if (isUser) {
    return (
      <div className="flex justify-end py-1">
        <div className="max-w-[85%] rounded-xl rounded-br-sm border border-amber-500/20 bg-amber-500/10 px-3 py-2">
          <p className="text-xs text-amber-100 leading-relaxed">{message.content}</p>
        </div>
      </div>
    );
  }

  // assistant
  return (
    <div className="flex items-start gap-2.5 py-1">
      <div className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-zinc-700/60 bg-zinc-800/80 text-[10px] font-mono text-zinc-400 mt-0.5">
        AI
      </div>
      <div className="flex-1 min-w-0">
        {message.content && (
          <p className="text-xs text-zinc-300 leading-relaxed whitespace-pre-wrap">{message.content}</p>
        )}
        {message.toolCalls && message.toolCalls.length > 0 && (
          <div className="flex flex-wrap gap-1.5 mt-2">
            {message.toolCalls.map((chip, i) => (
              <ToolChip key={i} chip={chip} />
            ))}
          </div>
        )}
        {message.thoughts && message.thoughts.length > 0 && (
          <ThoughtAccordion thoughts={message.thoughts} />
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Commit PR Modal
// ---------------------------------------------------------------------------

interface CommitModalProps {
  sessionId: string;
  onClose: () => void;
  onSuccess: (prUrl: string, prNumber: number) => void;
}

function CommitModal({ sessionId, onClose, onSuccess }: CommitModalProps) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!title.trim()) {
      setError("PR title is required.");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const result = await api.commitSession(sessionId, {
        title: title.trim(),
        body: body.trim() || null,
      });
      onSuccess(result.pr_url, result.pr_number);
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : "Failed to commit.";
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
      <div className="relative w-full max-w-lg rounded-xl border-t border-t-zinc-600/50 border-x border-x-zinc-700/60 border-b border-b-zinc-900 bg-gradient-to-b from-[#14141a] to-[#0d0d11] p-6 shadow-[0_24px_64px_rgba(0,0,0,0.7),inset_0_1px_0_rgba(255,255,255,0.08)]">
        <div className="flex items-center justify-between mb-6">
          <div className="flex items-center gap-2.5">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg border border-violet-500/30 bg-violet-500/10 text-violet-400">
              <GitPullRequest className="h-4 w-4" />
            </div>
            <h2 className="text-sm font-semibold text-zinc-100">Commit & Open PR</h2>
          </div>
          <button onClick={onClose} className="flex h-7 w-7 items-center justify-center rounded-md text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800/60 transition-colors">
            <X className="h-4 w-4" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-1.5">
            <label className="block text-[11px] font-medium text-zinc-400 uppercase tracking-wide font-mono">PR Title *</label>
            <input
              id="commit-title-input"
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="fix: resolve null pointer in auth middleware"
              maxLength={255}
              className="w-full rounded-lg border border-zinc-700/60 bg-zinc-900/80 px-3 py-2 text-sm text-zinc-200 placeholder-zinc-600 focus:outline-none focus:ring-1 focus:ring-violet-500/50 transition-all"
              autoFocus
            />
          </div>

          <div className="space-y-1.5">
            <label className="block text-[11px] font-medium text-zinc-400 uppercase tracking-wide font-mono">
              Description{" "}
              <span className="text-zinc-600 normal-case tracking-normal">(optional)</span>
            </label>
            <textarea
              id="commit-body-input"
              value={body}
              onChange={(e) => setBody(e.target.value)}
              placeholder="What changed and why…"
              rows={4}
              maxLength={65535}
              className="w-full resize-y rounded-lg border border-zinc-700/60 bg-zinc-900/80 px-3 py-2 text-sm text-zinc-200 placeholder-zinc-600 focus:outline-none focus:ring-1 focus:ring-violet-500/50 transition-all font-mono"
            />
          </div>

          {error && (
            <div className="flex items-center gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300">
              <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
              {error}
            </div>
          )}

          <div className="flex items-center gap-3 pt-2">
            <button type="button" onClick={onClose} className="flex-1 rounded-lg border border-zinc-700/60 bg-zinc-800/60 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-700/60 hover:text-zinc-100 transition-colors">
              Cancel
            </button>
            <button
              id="commit-submit-btn"
              type="submit"
              disabled={loading}
              className="flex-1 flex items-center justify-center gap-2 rounded-lg border-t border-t-violet-400/40 border-x border-x-violet-500/30 border-b border-b-violet-600/20 bg-gradient-to-b from-violet-500/20 via-violet-500/15 to-violet-600/10 px-4 py-2 text-sm font-semibold text-violet-200 shadow-[inset_0_1px_0_rgba(255,255,255,0.15),0_2px_4px_rgba(0,0,0,0.3)] hover:from-violet-500/25 active:translate-y-px transition-all disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <GitPullRequest className="h-4 w-4" />}
              {loading ? "Publishing…" : "Commit & Open PR"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

interface Props {
  params: Promise<{ id: string }>;
}

export default function SessionWorkspacePage({ params }: Props) {
  const { id: sessionId } = use(params);
  const [session, setSession] = useState<SessionOut | null>(null);
  const [pageLoading, setPageLoading] = useState(true);
  const [pageError, setPageError] = useState<string | null>(null);

  // Monaco: selected file tab
  const [activeFile, setActiveFile] = useState<string | null>(null);
  const [baseFileContent, setBaseFileContent] = useState<string>("");

  // Modals / banners
  const [showCommitModal, setShowCommitModal] = useState(false);
  const [prSuccess, setPrSuccess] = useState<{ url: string; number: number } | null>(null);
  const [sandboxLoading, setSandboxLoading] = useState(false);
  const [sandboxResult, setSandboxResult] = useState<{
    status: string;
    passed: boolean;
    logs?: string | null;
    run_url?: string | null;
  } | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  // Chat
  const { messages, stagedPatches, isStreaming, sendChatMessage, setStagedPatches } =
    useSessionStream(sessionId);

  const [chatInput, setChatInput] = useState("");
  const chatBottomRef = useRef<HTMLDivElement>(null);

  // -------------------------------------------------------------------------
  // Load session on mount
  // -------------------------------------------------------------------------

  useEffect(() => {
    api
      .getSession(sessionId)
      .then((s) => {
        setSession(s);
        // Pre-populate staged patches from DB if any exist.
        if (s.staged_patches && Object.keys(s.staged_patches).length > 0) {
          setStagedPatches(s.staged_patches);
          const firstFile = Object.keys(s.staged_patches)[0];
          setActiveFile(firstFile);
        }
      })
      .catch((err) => {
        const msg = err instanceof ApiError ? err.message : "Failed to load session.";
        setPageError(msg);
      })
      .finally(() => setPageLoading(false));
  }, [sessionId, setStagedPatches]);

  // Auto-scroll chat to bottom on new messages
  useEffect(() => {
    chatBottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // When active file changes or staged patches update, fetch base content for diff
  useEffect(() => {
    if (!activeFile || !session) {
      setBaseFileContent("");
      return;
    }
    // Fetch base file content for the diff viewer
    const url = `${process.env.NEXT_PUBLIC_API_URL}/sessions/${sessionId}/tree`;
    // We use api.getSessionTree to check it exists but fetch raw file separately
    // For the diff viewer, base content will just be fetched via the backend
    // In the context of this workspace, we show the diff text directly
    setBaseFileContent(""); // base is shown via Monaco diff from patch text
  }, [activeFile, session, sessionId]);

  // When new patches arrive from SSE, auto-select the first one
  useEffect(() => {
    const files = Object.keys(stagedPatches);
    if (files.length > 0 && !activeFile) {
      setActiveFile(files[0]);
    }
  }, [stagedPatches, activeFile]);

  // -------------------------------------------------------------------------
  // Actions
  // -------------------------------------------------------------------------

  const handleSendChat = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      const msg = chatInput.trim();
      if (!msg || isStreaming) return;
      setChatInput("");
      await sendChatMessage(msg);
    },
    [chatInput, isStreaming, sendChatMessage]
  );

  const handleVerify = async () => {
    setActionError(null);
    setSandboxLoading(true);
    setSandboxResult(null);
    try {
      const result = await api.verifySession(sessionId);
      setSandboxResult({
        status: result.status,
        passed: result.passed,
        logs: result.logs,
        run_url: result.run_url,
      });
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : "Sandbox verification failed.";
      setActionError(msg);
    } finally {
      setSandboxLoading(false);
    }
  };

  const handleClose = async () => {
    setActionError(null);
    try {
      const updated = await api.closeSession(sessionId);
      setSession(updated);
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : "Failed to close session.";
      setActionError(msg);
    }
  };

  const handleCommitSuccess = async (prUrl: string, prNumber: number) => {
    setShowCommitModal(false);
    setPrSuccess({ url: prUrl, number: prNumber });
    // Refresh session state.
    try {
      const updated = await api.getSession(sessionId);
      setSession(updated);
    } catch {
      // Non-critical — session likely marked completed server-side.
    }
  };

  // -------------------------------------------------------------------------
  // Rendering helpers
  // -------------------------------------------------------------------------

  const patchFiles = Object.keys(stagedPatches);
  const activePatch = activeFile ? stagedPatches[activeFile] ?? "" : "";

  // Parse unified diff to extract original and modified lines for Monaco.
  // Monaco DiffEditor wants original + modified full strings.
  // We reconstruct them from hunk lines as a best-effort approximation.
  const { original: monacoOriginal, modified: monacoModified } = parseDiffForMonaco(activePatch);

  // -------------------------------------------------------------------------
  // Loading / error states
  // -------------------------------------------------------------------------

  if (pageLoading) {
    return (
      <AppLayout title="Session" subtitle="Loading workspace…">
        <div className="flex h-screen items-center justify-center bg-[#09090b]">
          <Loader2 className="h-7 w-7 animate-spin text-amber-400" />
        </div>
      </AppLayout>
    );
  }

  if (pageError || !session) {
    return (
      <AppLayout title="Session Not Found">
        <div className="flex h-screen flex-col items-center justify-center bg-[#09090b] gap-4">
          <div className="flex h-12 w-12 items-center justify-center rounded-xl border border-red-500/30 bg-red-500/10 text-red-400">
            <AlertTriangle className="h-6 w-6" />
          </div>
          <p className="text-sm text-zinc-400">{pageError ?? "Session not found."}</p>
          <Link href="/sessions" className="text-xs font-mono text-amber-400 hover:underline">
            Back to sessions
          </Link>
        </div>
      </AppLayout>
    );
  }

  const isActive = session.status === "active";

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------

  return (
    <AppLayout title={session.title} subtitle={`${session.repo_owner}/${session.repo_name} · ${session.branch_name}`}>
      <div className="flex h-screen flex-col overflow-hidden bg-[#09090b]">
        {/* ================================================================ */}
        {/* TOP ACTION BAR                                                    */}
        {/* ================================================================ */}
        <header className="flex-none border-b border-zinc-800/80 bg-gradient-to-r from-[#0c0c0f] to-[#0a0a0d] px-5 py-3 shadow-[0_1px_16px_rgba(0,0,0,0.4)]">
          <div className="flex items-center justify-between gap-4">
            {/* Left: back + title + meta */}
            <div className="flex items-center gap-3 min-w-0">
              <Link
                href="/sessions"
                className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-zinc-700/60 bg-zinc-800/60 text-zinc-500 hover:text-zinc-300 hover:bg-zinc-700/60 transition-colors"
              >
                <ChevronLeft className="h-4 w-4" />
              </Link>

              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <h1 className="text-sm font-semibold text-zinc-100 truncate max-w-[200px]">
                    {session.title}
                  </h1>
                  <StatusChip status={session.status} />
                </div>
                <div className="flex items-center gap-2 mt-0.5">
                  <span className="inline-flex items-center gap-1 text-[10px] font-mono text-zinc-500">
                    <GitBranch className="h-3 w-3" />
                    {session.repo_owner}/{session.repo_name}
                  </span>
                  <span className="text-zinc-700 text-[10px]">·</span>
                  <span className="text-[10px] font-mono text-zinc-600">
                    {session.branch_name}
                  </span>
                  <span className="text-zinc-700 text-[10px]">·</span>
                  <span className="text-[10px] font-mono text-zinc-600">
                    {session.base_sha.slice(0, 8)}
                  </span>
                </div>
              </div>
            </div>

            {/* Right: action buttons */}
            <div className="flex items-center gap-2 shrink-0">
              {isActive && (
                <>
                  <button
                    id="verify-sandbox-btn"
                    onClick={handleVerify}
                    disabled={sandboxLoading || patchFiles.length === 0}
                    className="flex items-center gap-1.5 rounded-lg border border-zinc-700/60 bg-zinc-800/60 px-3 py-1.5 text-xs font-medium text-zinc-300 hover:text-zinc-100 hover:bg-zinc-700/60 hover:border-zinc-600/60 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    {sandboxLoading ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <FlaskConical className="h-3.5 w-3.5" />
                    )}
                    {sandboxLoading ? "Running…" : "Run Tests"}
                  </button>

                  <button
                    id="commit-pr-btn"
                    onClick={() => setShowCommitModal(true)}
                    disabled={patchFiles.length === 0}
                    className="flex items-center gap-1.5 rounded-lg border-t border-t-amber-400/40 border-x border-x-amber-500/30 border-b border-b-amber-600/20 bg-gradient-to-b from-amber-500/20 via-amber-500/15 to-amber-600/10 px-3 py-1.5 text-xs font-semibold text-amber-200 shadow-[inset_0_1px_0_rgba(255,255,255,0.15),0_2px_4px_rgba(0,0,0,0.3)] hover:from-amber-500/25 active:translate-y-px transition-all disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    <GitPullRequest className="h-3.5 w-3.5" />
                    Commit & PR
                  </button>

                  <button
                    id="close-session-btn"
                    onClick={handleClose}
                    className="flex items-center gap-1.5 rounded-lg border border-zinc-700/60 bg-zinc-800/60 px-3 py-1.5 text-xs font-medium text-zinc-400 hover:text-red-300 hover:border-red-500/30 hover:bg-red-500/10 transition-colors"
                  >
                    <X className="h-3.5 w-3.5" />
                    Close
                  </button>
                </>
              )}
            </div>
          </div>

          {/* Inline status panels */}
          {actionError && (
            <div className="mt-2 flex items-center gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300">
              <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
              {actionError}
            </div>
          )}

          {prSuccess && (
            <div className="mt-2 flex items-center justify-between gap-3 rounded-lg border border-violet-500/30 bg-violet-500/10 px-3 py-2">
              <div className="flex items-center gap-2 text-xs text-violet-300">
                <CheckCircle2 className="h-3.5 w-3.5 shrink-0" />
                <span>PR #{prSuccess.number} opened successfully!</span>
              </div>
              <a
                href={prSuccess.url}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-1 text-[10px] font-mono text-violet-400 hover:underline"
              >
                View PR <ExternalLink className="h-3 w-3" />
              </a>
            </div>
          )}

          {sandboxResult && (
            <div
              className={`mt-2 rounded-lg border px-3 py-2 ${
                sandboxResult.passed
                  ? "border-emerald-500/30 bg-emerald-500/10"
                  : "border-red-500/30 bg-red-500/10"
              }`}
            >
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2 text-xs">
                  {sandboxResult.passed ? (
                    <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400 shrink-0" />
                  ) : (
                    <XCircle className="h-3.5 w-3.5 text-red-400 shrink-0" />
                  )}
                  <span className={sandboxResult.passed ? "text-emerald-300" : "text-red-300"}>
                    Sandbox {sandboxResult.status} —{" "}
                    {sandboxResult.passed ? "all tests passed" : "tests failed"}
                  </span>
                </div>
                {sandboxResult.run_url && (
                  <a
                    href={sandboxResult.run_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="flex items-center gap-1 text-[10px] font-mono text-zinc-500 hover:text-zinc-300 hover:underline"
                  >
                    View run <ExternalLink className="h-3 w-3" />
                  </a>
                )}
              </div>
              {sandboxResult.logs && (
                <pre className="mt-2 max-h-24 overflow-y-auto rounded-md bg-black/40 p-2 text-[10px] font-mono text-zinc-400 leading-relaxed">
                  {sandboxResult.logs}
                </pre>
              )}
            </div>
          )}
        </header>

        {/* ================================================================ */}
        {/* MAIN SPLIT                                                        */}
        {/* ================================================================ */}
        <div className="flex-1 flex overflow-hidden">
          {/* -------------------------------------------------------------- */}
          {/* LEFT PANE: Chat Dock (37%)                                      */}
          {/* -------------------------------------------------------------- */}
          <section
            className="flex flex-col border-r border-zinc-800/80 bg-gradient-to-b from-[#0d0d10] to-[#0a0a0d]"
            style={{ width: "37%", minWidth: 280 }}
          >
            {/* Chat header */}
            <div className="flex-none px-4 py-3 border-b border-zinc-800/60">
              <div className="flex items-center gap-2">
                <Terminal className="h-3.5 w-3.5 text-amber-400" />
                <span className="text-[11px] font-mono font-semibold text-zinc-400 uppercase tracking-widest">
                  Agent Chat
                </span>
                {isStreaming && (
                  <span className="ml-auto flex items-center gap-1 text-[10px] font-mono text-amber-400">
                    <span className="relative flex h-1.5 w-1.5">
                      <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-60" />
                      <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-amber-400" />
                    </span>
                    streaming
                  </span>
                )}
              </div>
            </div>

            {/* Messages */}
            <div className="flex-1 overflow-y-auto px-4 py-3 space-y-2">
              {messages.length === 0 && (
                <div className="flex flex-col items-center justify-center h-full gap-3 py-10 text-center">
                  <div className="flex h-10 w-10 items-center justify-center rounded-xl border border-zinc-700/60 bg-zinc-800/60 text-zinc-600">
                    <Zap className="h-5 w-5" />
                  </div>
                  <p className="text-[11px] font-mono text-zinc-600">
                    Start by sending a message to the agent
                  </p>
                </div>
              )}
              {messages.map((msg, i) => (
                <ChatBubble key={i} message={msg} />
              ))}
              <div ref={chatBottomRef} />
            </div>

            {/* Chat input */}
            <div className="flex-none p-3 border-t border-zinc-800/60">
              <form onSubmit={handleSendChat} className="flex items-end gap-2">
                <textarea
                  id="chat-input"
                  value={chatInput}
                  onChange={(e) => setChatInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      handleSendChat(e as unknown as React.FormEvent);
                    }
                  }}
                  disabled={!isActive || isStreaming}
                  placeholder={
                    !isActive
                      ? "Session is closed."
                      : isStreaming
                      ? "Agent is responding…"
                      : "Message the agent (Enter to send)"
                  }
                  rows={2}
                  className="flex-1 resize-none rounded-lg border border-zinc-700/60 bg-zinc-900/80 px-3 py-2 text-xs text-zinc-200 placeholder-zinc-600 focus:outline-none focus:ring-1 focus:ring-amber-500/40 transition-all font-mono disabled:opacity-50 disabled:cursor-not-allowed"
                />
                <button
                  type="submit"
                  disabled={!isActive || isStreaming || !chatInput.trim()}
                  id="chat-send-btn"
                  className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border-t border-t-amber-400/40 border-x border-x-amber-500/30 border-b border-b-amber-600/20 bg-gradient-to-b from-amber-500/20 via-amber-500/15 to-amber-600/10 text-amber-300 shadow-[inset_0_1px_0_rgba(255,255,255,0.15)] hover:from-amber-500/25 active:translate-y-px transition-all disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  {isStreaming ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <Send className="h-4 w-4" />
                  )}
                </button>
              </form>
            </div>
          </section>

          {/* -------------------------------------------------------------- */}
          {/* RIGHT PANE: Monaco Workspace (63%)                              */}
          {/* -------------------------------------------------------------- */}
          <section className="flex-1 flex flex-col overflow-hidden bg-[#0d0d0f]">
            {/* File tab bar */}
            {patchFiles.length > 0 ? (
              <>
                <div className="flex-none flex items-center gap-0 border-b border-zinc-800/80 bg-[#0c0c0e] overflow-x-auto">
                  {patchFiles.map((file) => (
                    <button
                      key={file}
                      onClick={() => setActiveFile(file)}
                      className={`flex items-center gap-2 px-4 py-2.5 text-[11px] font-mono whitespace-nowrap border-r border-zinc-800/60 transition-all ${
                        activeFile === file
                          ? "bg-[#13131a] text-amber-300 border-t-2 border-t-amber-500/60"
                          : "text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800/30"
                      }`}
                    >
                      <FileCode2 className="h-3 w-3 shrink-0" />
                      <span className="truncate max-w-[180px]" title={file}>
                        {file.split("/").pop()}
                      </span>
                    </button>
                  ))}
                </div>

                {/* Monaco DiffEditor */}
                <div className="flex-1 overflow-hidden">
                  <MonacoDiffEditor
                    height="100%"
                    theme="vs-dark"
                    language={guessLanguage(activeFile ?? "")}
                    original={monacoOriginal}
                    modified={monacoModified}
                    options={{
                      readOnly: true,
                      renderSideBySide: true,
                      minimap: { enabled: false },
                      fontSize: 12,
                      fontFamily: "'Geist Mono', 'JetBrains Mono', Menlo, monospace",
                      lineNumbers: "on",
                      wordWrap: "off",
                      scrollBeyondLastLine: false,
                      renderLineHighlight: "gutter",
                      diffWordWrap: "off",
                      hideUnchangedRegions: { enabled: true },
                    }}
                  />
                </div>
              </>
            ) : (
              /* Empty state */
              <div className="flex-1 flex flex-col items-center justify-center gap-5 px-8 text-center">
                <div className="flex h-16 w-16 items-center justify-center rounded-2xl border border-zinc-700/60 bg-zinc-900/40 text-zinc-700">
                  <WrapText className="h-8 w-8" />
                </div>
                <div>
                  <p className="text-sm font-medium text-zinc-500">No staged patches yet</p>
                  <p className="text-[11px] font-mono text-zinc-700 mt-1">
                    Ask the agent to read and modify files to see diffs here
                  </p>
                </div>
                <div className="grid grid-cols-1 gap-2 text-left w-full max-w-xs">
                  {[
                    ["Read a file", "read app/main.py"],
                    ["Stage a fix", "fix the null check in auth.py"],
                    ["View staged", "show what's staged"],
                  ].map(([label, example]) => (
                    <div
                      key={label}
                      className="rounded-lg border border-zinc-800/60 bg-zinc-900/40 px-3 py-2"
                    >
                      <p className="text-[10px] font-mono text-zinc-600 mb-0.5">{label}</p>
                      <p className="text-[11px] font-mono text-zinc-500">&ldquo;{example}&rdquo;</p>
                    </div>
                  ))}
                </div>

                {/* Session meta card */}
                <div className="mt-2 rounded-xl border border-zinc-800/60 bg-zinc-900/30 p-4 text-left w-full max-w-xs">
                  <p className="text-[10px] font-mono text-zinc-600 uppercase tracking-widest mb-2">Session</p>
                  <div className="space-y-1.5 text-[11px] font-mono">
                    <div className="flex items-center justify-between">
                      <span className="text-zinc-600">repo</span>
                      <span className="text-zinc-400">{session.repo_owner}/{session.repo_name}</span>
                    </div>
                    <div className="flex items-center justify-between">
                      <span className="text-zinc-600">branch</span>
                      <span className="text-zinc-400">{session.branch_name}</span>
                    </div>
                    <div className="flex items-center justify-between">
                      <span className="text-zinc-600">base</span>
                      <span className="text-zinc-400">{session.base_sha.slice(0, 12)}</span>
                    </div>
                  </div>
                </div>
              </div>
            )}
          </section>
        </div>
      </div>

      {/* Modals */}
      {showCommitModal && (
        <CommitModal
          sessionId={sessionId}
          onClose={() => setShowCommitModal(false)}
          onSuccess={handleCommitSuccess}
        />
      )}
    </AppLayout>
  );
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

/** Guess Monaco language identifier from file extension. */
function guessLanguage(filePath: string): string {
  const ext = filePath.split(".").pop()?.toLowerCase() ?? "";
  const map: Record<string, string> = {
    ts: "typescript",
    tsx: "typescript",
    js: "javascript",
    jsx: "javascript",
    py: "python",
    go: "go",
    rs: "rust",
    java: "java",
    rb: "ruby",
    json: "json",
    yaml: "yaml",
    yml: "yaml",
    toml: "toml",
    md: "markdown",
    sh: "shell",
    css: "css",
    html: "html",
  };
  return map[ext] ?? "plaintext";
}

/**
 * Parse a unified diff patch into original and modified full strings for Monaco DiffEditor.
 *
 * This is a best-effort reconstruction:
 * - Lines starting with ' ' or '-' contribute to original.
 * - Lines starting with ' ' or '+' contribute to modified.
 * Skips hunk headers and file headers.
 */
function parseDiffForMonaco(patch: string): { original: string; modified: string } {
  if (!patch) return { original: "", modified: "" };

  const originalLines: string[] = [];
  const modifiedLines: string[] = [];

  for (const rawLine of patch.split("\n")) {
    if (!rawLine) continue;
    const prefix = rawLine[0];
    const content = rawLine.slice(1);

    if (prefix === " ") {
      originalLines.push(content);
      modifiedLines.push(content);
    } else if (prefix === "-") {
      originalLines.push(content);
    } else if (prefix === "+") {
      modifiedLines.push(content);
    }
    // Skip @@ headers, --- / +++ filename lines, diff meta lines.
  }

  return {
    original: originalLines.join("\n"),
    modified: modifiedLines.join("\n"),
  };
}
