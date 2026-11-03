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

const HUNK_REGEX = /@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

/**
 * Parses simple additions and deletions count from a unified diff patch string.
 */
function getPatchStats(patch: string) {
  const additions = (patch.match(/^\+[^+]/gm) || []).length;
  const deletions = (patch.match(/^-[^-]/gm) || []).length;
  return { additions, deletions };
}

/**
 * Clean replacement snippets or patches, stripping enclosing markdown code fences.
 */
function cleanPatchString(raw: string): string {
  if (!raw) return "";
  let cleaned = raw.replace(/\r\n/g, "\n").trim();
  // Strip outer markdown code blocks if present: ```diff ... ``` or ```python ... ``` or ```suggestion ... ```
  const fenceMatch = cleaned.match(/^```(?:[a-zA-Z0-9_-]+)?\n([\s\S]*?)\n```$/);
  if (fenceMatch) {
    cleaned = fenceMatch[1].trim();
  }
  return cleaned;
}

interface FormattedLine {
  raw: string;
  lineStyle: string;
  lineNumber: string;
}

function parseDiffLines(patch: string): FormattedLine[] {
  const lines = patch.split("\n");
  let currentOldLine = 0;
  let currentNewLine = 0;
  let inHunk = false;

  return lines.map((line, idx) => {
    const hunkMatch = line.match(HUNK_REGEX);
    if (hunkMatch) {
      currentOldLine = parseInt(hunkMatch[1], 10);
      currentNewLine = parseInt(hunkMatch[2], 10);
      inHunk = true;
      return {
        raw: line,
        lineStyle:
          "text-cyan-400 bg-cyan-950/25 px-3 py-0.5 border-l-2 border-cyan-500 font-semibold",
        lineNumber: "",
      };
    }

    if (line.startsWith("---") || line.startsWith("+++")) {
      return {
        raw: line,
        lineStyle:
          "text-zinc-400 bg-zinc-900/40 px-3 py-0.5 border-l-2 border-zinc-600 font-medium",
        lineNumber: "",
      };
    }

    if (line.startsWith("+")) {
      const num = inHunk ? currentNewLine++ : idx + 1;
      return {
        raw: line,
        lineStyle:
          "bg-emerald-950/40 text-emerald-300 border-l-2 border-emerald-500 px-3 py-0.5",
        lineNumber: String(num),
      };
    }

    if (line.startsWith("-")) {
      const num = inHunk ? currentOldLine++ : idx + 1;
      return {
        raw: line,
        lineStyle:
          "bg-rose-950/40 text-rose-300 border-l-2 border-rose-500 px-3 py-0.5",
        lineNumber: String(num),
      };
    }

    // Context line or normal snippet line
    let numStr = "";
    if (inHunk) {
      numStr = String(currentNewLine);
      currentOldLine++;
      currentNewLine++;
    } else {
      numStr = String(idx + 1);
    }

    return {
      raw: line,
      lineStyle:
        "text-zinc-300 px-3 py-0.5 border-l-2 border-transparent hover:bg-zinc-900/30",
      lineNumber: numStr,
    };
  });
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
  const cleanedPatch = cleanPatchString(patch);
  if (!cleanedPatch || cleanedPatch.length === 0) {
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

  const lines = parseDiffLines(cleanedPatch);
  const stats = getPatchStats(cleanedPatch);

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
        {lines.map((line, idx) => (
          <div
            key={idx}
            className={cn(
              "flex items-start min-w-full font-mono whitespace-pre transition-colors",
              line.lineStyle
            )}
          >
            {showLineNumbers && (
              <span className="select-none text-zinc-600 mr-2.5 w-7 text-right text-[10px] inline-block shrink-0 pt-px font-mono">
                {line.lineNumber || "\u00A0"}
              </span>
            )}
            <span className="flex-1 font-mono tracking-tight">{line.raw || " "}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
