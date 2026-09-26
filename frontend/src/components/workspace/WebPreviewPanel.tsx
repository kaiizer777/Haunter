"use client";

import { useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  ExternalLink,
  Eye,
  EyeOff,
  Globe,
  KeyRound,
  Loader2,
  Monitor,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Settings2,
  Smartphone,
  Tablet,
  Terminal,
  Trash2,
  X,
} from "lucide-react";
import type { WebContainerStatus } from "@/hooks/useWebContainer";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface WebPreviewPanelProps {
  status: WebContainerStatus;
  previewUrl: string | null;
  onBoot: () => void;
  onRefresh: () => void;
  onRestartServer: () => void;
  className?: string;
  error?: string | null;
  onSaveEnv?: (vars: Record<string, string>) => void | Promise<void>;
  initialEnvVars?: Record<string, string>;
}

export interface EnvDrawerProps {
  vars: Record<string, string>;
  onSave: (vars: Record<string, string>) => void;
  onClose: () => void;
}

type PreviewViewport = "desktop" | "tablet" | "mobile";

const VIEWPORT_MAX_WIDTH: Record<PreviewViewport, string> = {
  desktop: "100%",
  tablet: "768px",
  mobile: "390px",
};

function isSecretKey(key: string): boolean {
  return /(_KEY|_SECRET|_TOKEN)$/i.test(key.trim());
}

// Stable row ids: crypto.randomUUID() when available (all modern browsers),
// Date.now()+random fallback for non-secure contexts where crypto is undefined.
function newEnvRowId(): string {
  if (
    typeof crypto !== "undefined" &&
    typeof crypto.randomUUID === "function"
  ) {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

// ---------------------------------------------------------------------------
// EnvDrawer — right-side slide-out for in-container .env management
// ---------------------------------------------------------------------------

export function EnvDrawer({ vars, onSave, onClose }: EnvDrawerProps) {
  const [rows, setRows] = useState<{ id: string; key: string; value: string }[]>(
    () =>
      Object.entries(vars).map(([key, value]) => ({
        id: newEnvRowId(),
        key,
        value,
      }))
  );
  const [revealed, setRevealed] = useState<Record<string, boolean>>({});
  const firstInputRef = useRef<HTMLInputElement>(null);

  // Sync rows when `vars` changes externally (e.g. parent save round-trip).
  // Identity-guarded via ref so in-progress user edits survive re-renders
  // where the vars object is stable.
  const varsRef = useRef(vars);
  useEffect(() => {
    if (varsRef.current !== vars) {
      varsRef.current = vars;
      setRows(
        Object.entries(vars).map(([key, value]) => ({
          id: newEnvRowId(),
          key,
          value,
        }))
      );
    }
  }, [vars]);

  // Autofocus the first key input on open.
  useEffect(() => {
    firstInputRef.current?.focus();
  }, []);

  const handleAdd = () => {
    setRows((prev) => [...prev, { id: newEnvRowId(), key: "", value: "" }]);
  };

  const handleRemove = (id: string) => {
    setRows((prev) => prev.filter((r) => r.id !== id));
  };

  const handleSave = () => {
    const merged: Record<string, string> = {};
    for (const row of rows) {
      const k = row.key.trim();
      if (!k) continue;
      merged[k] = row.value;
    }
    onSave(merged);
  };

  return (
    <div
      className="fixed inset-0 z-50"
      role="dialog"
      aria-modal="true"
      aria-label="Environment variables"
      onKeyDown={(e) => {
        if (e.key === "Escape") onClose();
      }}
    >
      <div
        className="absolute inset-0 bg-black/60 backdrop-blur-[2px]"
        onClick={onClose}
        aria-hidden="true"
      />
      <aside className="absolute right-0 top-0 flex h-full w-80 max-w-[90vw] flex-col border-l border-zinc-800 bg-[#0c0c0e] shadow-[-16px_0_48px_rgba(0,0,0,0.6)]">
        <div className="flex items-center justify-between border-b border-zinc-800 px-4 py-3">
          <div className="flex items-center gap-2">
            <span className="flex h-7 w-7 items-center justify-center rounded-lg border border-zinc-700/60 bg-zinc-800/60 text-zinc-300">
              <KeyRound className="h-3.5 w-3.5" />
            </span>
            <div>
              <p className="text-xs font-semibold text-zinc-100">
                Environment Variables
              </p>
              <p className="font-mono text-[10px] text-zinc-500">.env · this tab only</p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close environment drawer"
            className="flex h-7 w-7 items-center justify-center rounded-lg text-zinc-500 transition-colors hover:bg-zinc-800/60 hover:text-zinc-200"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="flex-1 space-y-2 overflow-y-auto px-4 py-3">
          {rows.length === 0 && (
            <p className="rounded-xl border border-dashed border-zinc-800 px-3 py-4 text-center font-mono text-[11px] text-zinc-500">
              No variables yet — add your first below.
            </p>
          )}
          {rows.map((row, idx) => {
            const secret = isSecretKey(row.key);
            const showPlaintext = revealed[row.id] ?? false;
            return (
              <div
                key={row.id}
                className="rounded-xl border border-zinc-800 bg-zinc-900/40 p-2"
              >
                <div className="flex items-center gap-1.5">
                  <input
                    type="text"
                    ref={idx === 0 ? firstInputRef : undefined}
                    value={row.key}
                    onChange={(e) =>
                      setRows((prev) =>
                        prev.map((r) =>
                          r.id === row.id ? { ...r, key: e.target.value } : r
                        )
                      )
                    }
                    placeholder="KEY"
                    aria-label="Variable key"
                    spellCheck={false}
                    autoComplete="off"
                    className="w-[38%] min-w-0 rounded-lg border border-zinc-700/60 bg-zinc-900/90 px-2 py-1.5 font-mono text-[11px] text-zinc-100 placeholder-zinc-600 focus:border-zinc-500 focus:outline-none"
                  />
                  <div className="relative min-w-0 flex-1">
                    <input
                      type={secret && !showPlaintext ? "password" : "text"}
                      value={row.value}
                      onChange={(e) =>
                        setRows((prev) =>
                          prev.map((r) =>
                            r.id === row.id ? { ...r, value: e.target.value } : r
                          )
                        )
                      }
                      placeholder="value"
                      aria-label={
                        row.key ? `Value for ${row.key}` : "Variable value"
                      }
                      spellCheck={false}
                      autoComplete="off"
                      className="w-full rounded-lg border border-zinc-700/60 bg-zinc-900/90 py-1.5 pl-2 pr-8 font-mono text-[11px] text-zinc-100 placeholder-zinc-600 focus:border-zinc-500 focus:outline-none"
                    />
                    {secret && (
                      <button
                        type="button"
                        onClick={() =>
                          setRevealed((prev) => ({
                            ...prev,
                            [row.id]: !prev[row.id],
                          }))
                        }
                        aria-label={
                          showPlaintext ? "Hide value" : "Show value"
                        }
                        className="absolute right-1 top-1/2 flex h-6 w-6 -translate-y-1/2 items-center justify-center rounded-md text-zinc-500 transition-colors hover:bg-zinc-800 hover:text-zinc-200"
                      >
                        {showPlaintext ? (
                          <EyeOff className="h-3.5 w-3.5" />
                        ) : (
                          <Eye className="h-3.5 w-3.5" />
                        )}
                      </button>
                    )}
                  </div>
                  <button
                    type="button"
                    onClick={() => handleRemove(row.id)}
                    aria-label={
                      row.key ? `Remove ${row.key}` : "Remove variable"
                    }
                    className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-zinc-600 transition-colors hover:bg-red-500/10 hover:text-red-300"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>
            );
          })}

          <button
            type="button"
            onClick={handleAdd}
            className="flex w-full items-center justify-center gap-1.5 rounded-xl border border-dashed border-zinc-700/70 px-3 py-2 font-mono text-[11px] text-zinc-400 transition-colors hover:border-zinc-500 hover:text-zinc-200"
          >
            <Plus className="h-3.5 w-3.5" />
            Add Variable
          </button>
        </div>

        <div className="border-t border-zinc-800 px-4 py-3">
          <button
            type="button"
            onClick={handleSave}
            className="flex w-full items-center justify-center gap-2 rounded-xl border border-amber-300/70 border-x-amber-500/60 border-b-amber-700 bg-gradient-to-b from-amber-400 via-amber-500 to-amber-600 px-4 py-2 text-xs font-semibold text-zinc-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.35),0_2px_8px_rgba(245,158,11,0.35)] transition-all hover:brightness-105 active:translate-y-[0.5px]"
          >
            Save &amp; Restart
          </button>
          <p className="mt-2 font-mono text-[10px] leading-relaxed text-zinc-600">
            Values live only in the container for this tab session — never
            persisted to the backend or localStorage.
          </p>
        </div>
      </aside>
    </div>
  );
}

// ---------------------------------------------------------------------------
// WebPreviewPanel — browser-framed live WebContainer preview
// ---------------------------------------------------------------------------

export function WebPreviewPanel({
  status,
  previewUrl,
  onBoot,
  onRefresh,
  onRestartServer,
  className = "",
  error = null,
  onSaveEnv,
  initialEnvVars = {},
}: WebPreviewPanelProps) {
  const [viewport, setViewport] = useState<PreviewViewport>("desktop");
  const [envOpen, setEnvOpen] = useState(false);
  const [envVars, setEnvVars] = useState<Record<string, string>>(initialEnvVars);
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [restartingBadge, setRestartingBadge] = useState(false);

  useEffect(() => {
    if (status === "ready") {
      setRestartingBadge(false);
    }
  }, [status]);

  const handleRefresh = () => {
    setRefreshNonce((n) => n + 1);
    onRefresh();
  };

  const handleSaveEnv = (vars: Record<string, string>) => {
    setEnvVars(vars);
    setEnvOpen(false);
    setRestartingBadge(true);
    void onSaveEnv?.(vars);
  };

  const isLoading =
    status === "booting" ||
    status === "mounting" ||
    status === "installing" ||
    status === "starting";
  const showRestarting = restartingBadge || status === "starting";
  const framed = viewport !== "desktop" && status === "ready" && previewUrl;

  const statusLabel: Record<string, string> = {
    booting: "Booting WebContainer…",
    mounting: "Mounting files…",
    installing: "Installing dependencies…",
    starting: "Starting dev server…",
  };

  return (
    <div className={`flex h-full flex-col bg-[#09090b] ${className}`}>
      {/* Browser bar */}
      <div className="flex flex-none items-center gap-2 border-b border-zinc-800 bg-[#0c0c0e] px-3 py-2">
        <button
          type="button"
          onClick={handleRefresh}
          disabled={status !== "ready" || !previewUrl}
          title="Reload preview"
          aria-label="Reload preview"
          className="flex h-7 w-7 items-center justify-center rounded-lg border border-zinc-700/60 bg-zinc-800/60 text-zinc-300 transition-all hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
        >
          <RefreshCw className="h-3.5 w-3.5" />
        </button>
        <button
          type="button"
          onClick={onRestartServer}
          disabled={status !== "ready"}
          title="Restart dev server"
          aria-label="Restart dev server"
          className="flex h-7 w-7 items-center justify-center rounded-lg border border-zinc-700/60 bg-zinc-800/60 text-zinc-300 transition-all hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
        >
          <RotateCcw className="h-3.5 w-3.5" />
        </button>

        <div className="flex min-w-0 flex-1 items-center gap-2 rounded-lg border border-zinc-800 bg-black/40 px-2.5 py-1.5">
          <Globe className="h-3.5 w-3.5 shrink-0 text-zinc-500" />
          <span
            className="truncate font-mono text-[11px] text-zinc-400"
            title={previewUrl ?? "Preview not running"}
          >
            {previewUrl ?? "Preview not running"}
          </span>
        </div>

        {status === "ready" && previewUrl && !showRestarting && (
          <span className="inline-flex shrink-0 items-center gap-1.5 rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 font-mono text-[10px] font-medium text-emerald-300">
            <span className="relative flex h-1.5 w-1.5">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-60" />
              <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-emerald-400" />
            </span>
            Live
          </span>
        )}
        {showRestarting && (
          <span className="inline-flex shrink-0 items-center gap-1.5 rounded-full border border-amber-500/30 bg-amber-500/10 px-2 py-0.5 font-mono text-[10px] font-medium text-amber-300">
            <Loader2 className="h-3 w-3 animate-spin" />
            Restarting…
          </span>
        )}

        <div
          className="flex shrink-0 items-center rounded-lg border border-zinc-800 bg-[#0a0a0d] p-0.5"
          role="group"
          aria-label="Preview viewport width"
        >
          {(
            [
              { key: "desktop", icon: Monitor, label: "Desktop width" },
              { key: "tablet", icon: Tablet, label: "Tablet width (768px)" },
              { key: "mobile", icon: Smartphone, label: "Mobile width (390px)" },
            ] as const
          ).map(({ key, icon: Icon, label }) => (
            <button
              key={key}
              type="button"
              onClick={() => setViewport(key)}
              title={label}
              aria-label={label}
              aria-pressed={viewport === key}
              className={`flex h-7 w-7 items-center justify-center rounded-md transition-all ${
                viewport === key
                  ? "border border-zinc-600/70 bg-gradient-to-b from-zinc-700 to-zinc-800 text-zinc-100 shadow-[inset_0_1px_0_rgba(255,255,255,0.12)]"
                  : "text-zinc-500 hover:bg-zinc-800/60 hover:text-zinc-300"
              }`}
            >
              <Icon className="h-3.5 w-3.5" />
            </button>
          ))}
        </div>

        <button
          type="button"
          onClick={() => setEnvOpen(true)}
          title="Manage .env variables"
          aria-label="Manage .env variables"
          className="flex shrink-0 items-center gap-1.5 rounded-lg border border-zinc-700/60 bg-zinc-800/60 px-2.5 py-1.5 font-mono text-[11px] text-zinc-300 transition-all hover:text-white"
        >
          <Settings2 className="h-3.5 w-3.5" />
          <span>Env</span>
          {Object.keys(envVars).length > 0 && (
            <span className="rounded-full bg-zinc-700 px-1.5 py-px font-mono text-[9px] text-zinc-200">
              {Object.keys(envVars).length}
            </span>
          )}
        </button>

        {status === "ready" && previewUrl && (
          <a
            href={previewUrl}
            target="_blank"
            rel="noopener noreferrer"
            title="Open preview in new tab"
            aria-label="Open preview in new tab"
            className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-zinc-700/60 bg-zinc-800/60 text-zinc-300 transition-all hover:text-white"
          >
            <ExternalLink className="h-3.5 w-3.5" />
          </a>
        )}
      </div>

      {/* Content */}
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
        {status === "idle" && (
          <div className="flex flex-1 items-center justify-center px-6 py-12">
            <div className="flex w-full max-w-sm flex-col items-center gap-4 rounded-2xl border border-zinc-800 bg-[#0e0e12] px-6 py-8 text-center">
              <span className="flex h-12 w-12 items-center justify-center rounded-2xl border border-zinc-700/60 bg-zinc-800/60 text-zinc-300">
                <Globe className="h-6 w-6" />
              </span>
              <div>
                <p className="text-sm font-semibold text-zinc-100">
                  Live Preview
                </p>
                <p className="mt-1 font-mono text-[11px] leading-relaxed text-zinc-500">
                  Boot a WebContainer in your browser to run the staged
                  changes with hot reload. No cloud cost.
                </p>
              </div>
              <button
                type="button"
                onClick={onBoot}
                className="flex items-center gap-2 rounded-xl border border-amber-300/70 border-x-amber-500/60 border-b-amber-700 bg-gradient-to-b from-amber-400 via-amber-500 to-amber-600 px-4 py-2 text-xs font-semibold text-zinc-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.35),0_2px_8px_rgba(245,158,11,0.35)] transition-all hover:brightness-105 active:translate-y-[0.5px]"
              >
                <Play className="h-3.5 w-3.5 fill-current" />
                Launch Preview
              </button>
              <p className="font-mono text-[10px] text-zinc-600">
                Boots with staged files only (v1 synthetic scaffold).
              </p>
            </div>
          </div>
        )}

        {isLoading && (
          <div className="flex flex-1 flex-col items-center justify-center gap-3 px-6 text-center">
            <Loader2 className="h-7 w-7 animate-spin text-amber-400" />
            <p className="text-sm font-medium text-zinc-200">
              {statusLabel[status] ?? "Working…"}
            </p>
            {status === "installing" && (
              <p className="flex max-w-sm items-center gap-1.5 font-mono text-[11px] leading-relaxed text-zinc-500">
                <Terminal className="h-3.5 w-3.5 shrink-0 text-emerald-400/80" />
                Streaming install logs to Terminal — open the Terminal drawer
                in Chat to follow along.
              </p>
            )}
          </div>
        )}

        {status === "ready" &&
          (previewUrl ? (
            <div className="flex min-h-0 flex-1 justify-center overflow-auto bg-[#09090b]">
              <div
                className={
                  framed
                    ? "my-3 h-[calc(100%-1.5rem)] overflow-hidden rounded-xl border border-zinc-700/70 shadow-[0_8px_32px_rgba(0,0,0,0.5)]"
                    : "h-full w-full"
                }
                style={{
                  width: VIEWPORT_MAX_WIDTH[viewport],
                  maxWidth: "100%",
                }}
              >
                <iframe
                  key={refreshNonce}
                  src={previewUrl}
                  title="Live preview"
                  sandbox="allow-scripts allow-same-origin allow-forms"
                  className="h-full w-full border-0 bg-white"
                />
              </div>
            </div>
          ) : (
            <div className="flex flex-1 flex-col items-center justify-center gap-3 px-6 text-center">
              <Loader2 className="h-7 w-7 animate-spin text-amber-400" />
              <p className="font-mono text-xs text-zinc-400">
                Waiting for server URL…
              </p>
            </div>
          ))}

        {status === "error" && (
          <div className="flex flex-1 items-center justify-center px-6 py-12">
            <div className="flex w-full max-w-sm flex-col items-center gap-3 rounded-2xl border border-red-500/30 bg-red-500/[0.06] px-6 py-8 text-center">
              <span className="flex h-12 w-12 items-center justify-center rounded-2xl border border-red-500/30 bg-red-500/10 text-red-400">
                <AlertTriangle className="h-6 w-6" />
              </span>
              <p className="text-sm font-semibold text-zinc-100">
                Preview failed to start
              </p>
              <p className="max-w-full break-words font-mono text-[11px] leading-relaxed text-zinc-400">
                {error ?? "Failed to boot WebContainer."}
              </p>
              <button
                type="button"
                onClick={onBoot}
                className="mt-1 flex items-center gap-2 rounded-xl border border-zinc-700/60 bg-zinc-800/60 px-4 py-2 font-mono text-xs text-zinc-200 transition-all hover:bg-zinc-700/60 hover:text-white active:translate-y-[0.5px]"
              >
                <RefreshCw className="h-3.5 w-3.5" />
                Retry
              </button>
            </div>
          </div>
        )}
      </div>

      {envOpen && (
        <EnvDrawer
          vars={envVars}
          onSave={handleSaveEnv}
          onClose={() => setEnvOpen(false)}
        />
      )}
    </div>
  );
}

export default WebPreviewPanel;
