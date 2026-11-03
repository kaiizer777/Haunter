import React from "react";
import { cn } from "@/lib/utils";
import { FileCode, Plus, Minus } from "lucide-react";

export interface DiffViewerProps {
  /** Raw unified diff patch string */
  patch: string;
  /** Optional file path or name header */
  filename?: string;
  /** Optional CSS class name */
  className?: string;
  /** Optional max height for vertical scrolling (e.g. '400px' or 400) */
  maxHeight?: string | number;
  /** Whether to show line number gutters (default: true) */
  showLineNumbers?: boolean;
}

/**
 * Parses simple additions and deletions count from a unified diff patch string.
 */
function getPatchStats(patch: string) {
  const additions = (patch.match(/^\+[^+]/gm) || []).length;
  const deletions = (patch.match(/^-[^-]/gm) || []).length;
  return { additions, deletions };
}

/**
 * Studio-grade, line-numbered syntax-highlighted diff viewer.
 *
 * Designed with a deep #070709 background, emerald additions, rose deletions,
 * and cyan chunk headers. Code never wraps, ensuring alignment and readability.
 */
export function DiffViewer({
  patch,
  filename,
  className,
  maxHeight,
  showLineNumbers = true,
}: DiffViewerProps) {
  if (!patch || patch.trim().length === 0) {
    return (
      <div
        className={cn(
          "rounded-md border border-zinc-800/80 bg-[#070709] px-4 py-3 text-xs font-mono text-zinc-500",
          className
        )}
      >
        No patch content available
      </div>
    );
  }

  const lines = patch.trim().split("\n");
  const stats = getPatchStats(patch);

  return (
    <div
      className={cn(
        "rounded-md border border-zinc-800/90 bg-[#070709] overflow-hidden shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]",
        className
      )}
    >
      {/* Optional tactile filename / diff header */}
      {filename && (
        <div
          data-testid="diff-header"
          className="flex items-center justify-between border-b border-zinc-800/80 bg-zinc-900/60 px-3 py-1.5 text-[11px] font-mono"
        >
          <div className="flex items-center gap-1.5 text-zinc-300 truncate">
            <FileCode className="h-3.5 w-3.5 shrink-0 text-zinc-400" />
            <span className="truncate font-medium">{filename}</span>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {stats.additions > 0 && (
              <span
                data-testid="diff-additions"
                className="inline-flex items-center gap-0.5 text-emerald-400 font-semibold text-[10px]"
              >
                <Plus className="h-2.5 w-2.5" />
                {stats.additions}
              </span>
            )}
            {stats.deletions > 0 && (
              <span
                data-testid="diff-deletions"
                className="inline-flex items-center gap-0.5 text-rose-400 font-semibold text-[10px]"
              >
                <Minus className="h-2.5 w-2.5" />
                {stats.deletions}
              </span>
            )}
          </div>
        </div>
      )}

      {/* Code diff lines viewport */}
      <div
        className="overflow-x-auto text-[11px] font-mono leading-relaxed select-text"
        style={maxHeight ? { maxHeight, overflowY: "auto" } : undefined}
      >
        {lines.map((line, idx) => {
          let lineStyle =
            "text-zinc-300 px-3 py-0.5 border-l-2 border-transparent hover:bg-zinc-900/30";

          if (line.startsWith("@@")) {
            lineStyle =
              "text-cyan-400 bg-cyan-950/25 px-3 py-0.5 border-l-2 border-cyan-500 font-semibold";
          } else if (line.startsWith("+")) {
            lineStyle =
              "bg-emerald-950/40 text-emerald-300 border-l-2 border-emerald-500 px-3 py-0.5";
          } else if (line.startsWith("-")) {
            lineStyle =
              "bg-rose-950/40 text-rose-300 border-l-2 border-rose-500 px-3 py-0.5";
          }

          return (
            <div
              key={idx}
              className={cn(
                "flex items-start min-w-full font-mono whitespace-pre transition-colors",
                lineStyle
              )}
            >
              {showLineNumbers && (
                <span className="select-none text-zinc-600 mr-2.5 w-7 text-right text-[10px] inline-block shrink-0 pt-px font-mono">
                  {idx + 1}
                </span>
              )}
              <span className="flex-1 font-mono tracking-tight">{line}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
