"use client";

import { useState } from "react";
import {
  FileText,
  AlertTriangle,
  Bug,
  FileCode,
  Check,
  Copy,
  FolderGit2,
  Terminal,
  ChevronDown,
  ChevronUp,
  Sparkles,
} from "lucide-react";
import { Button } from "@/components/ui/button";

interface DiagnosisViewProps {
  summary: string;
}

/**
 * Parses markdown inline formatting: `code`, **bold**, etc.
 */
function renderInlineText(text: string) {
  if (!text) return null;

  // Split by code ticks and bold markers
  const parts = text.split(/(`[^`]+`|\*\*[^*]+\*\*)/g);

  return parts.map((part, idx) => {
    if (part.startsWith("`") && part.endsWith("`") && part.length >= 2) {
      const codeContent = part.slice(1, -1);
      return (
        <code
          key={idx}
          className="rounded-[4px] bg-zinc-900 border border-zinc-700/70 px-1.5 py-0.5 font-mono text-[11px] text-amber-300 font-semibold shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]"
        >
          {codeContent}
        </code>
      );
    }
    if (part.startsWith("**") && part.endsWith("**") && part.length >= 4) {
      const boldContent = part.slice(2, -2);
      return (
        <strong key={idx} className="font-semibold text-zinc-100">
          {boldContent}
        </strong>
      );
    }
    return <span key={idx}>{part}</span>;
  });
}

interface StructuredDiagnosis {
  errorType?: { name: string; details: string };
  fileLine?: { location: string; details: string };
  whyFailed?: string;
  files?: string[];
  otherContent: string[];
}

function parseStructuredDiagnosis(raw: string): StructuredDiagnosis | null {
  const lines = raw.split("\n");
  let foundStructuredKeys = false;

  const result: StructuredDiagnosis = {
    otherContent: [],
  };

  let currentSection = "";
  const failingFiles: string[] = [];

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (!line) continue;

    // Check for "Files in the failing commit" section or "Repository Files"
    if (line.startsWith("## Files in the failing commit") || line.startsWith("## Repository Files")) {
      currentSection = "files";
      foundStructuredKeys = true;
      continue;
    }

    if (currentSection === "files") {
      if (line.startsWith("- ") || line.startsWith("* ")) {
        failingFiles.push(line.slice(2).trim());
        continue;
      } else if (line.startsWith("## ")) {
        currentSection = "";
      }
    }

    // Match "- **Error Type:** `...` - details" or "- **Error Type:** ..."
    const errorMatch = line.match(/^[-*]?\s*\*\*Error Type:\*\*\s*(.*)$/i);
    if (errorMatch) {
      foundStructuredKeys = true;
      const rest = errorMatch[1].trim();
      // Extract code inside backticks if present
      const codeMatch = rest.match(/^`([^`]+)`\s*(?:-\s*)?(.*)$/);
      if (codeMatch) {
        result.errorType = {
          name: codeMatch[1],
          details: codeMatch[2],
        };
      } else {
        result.errorType = {
          name: rest,
          details: "",
        };
      }
      continue;
    }

    // Match "- **File/Line:** `...` - details" or "- **File/Line:** ..."
    const fileLineMatch = line.match(/^[-*]?\s*\*\*File(?:\/Line)?:\*\*\s*(.*)$/i);
    if (fileLineMatch) {
      foundStructuredKeys = true;
      const rest = fileLineMatch[1].trim();
      const codeMatch = rest.match(/^`([^`]+)`\s*(?:-\s*)?(.*)$/);
      if (codeMatch) {
        result.fileLine = {
          location: codeMatch[1],
          details: codeMatch[2],
        };
      } else {
        result.fileLine = {
          location: rest,
          details: "",
        };
      }
      continue;
    }

    // Match "- **Why CI Failed:** details"
    const whyMatch = line.match(/^[-*]?\s*\*\*Why CI Failed:\*\*\s*(.*)$/i);
    if (whyMatch) {
      foundStructuredKeys = true;
      result.whyFailed = whyMatch[1].trim();
      continue;
    }

    // Ignore root-cause summary title if it matches standard header
    if (line.match(/^\*\*Root-Cause Summary:\*\*$/i) || line.match(/^#+\s*Root-Cause Summary/i)) {
      continue;
    }

    // Anything else goes into other content
    result.otherContent.push(line);
  }

  if (failingFiles.length > 0) {
    result.files = failingFiles;
  }

  return foundStructuredKeys ? result : null;
}

export function DiagnosisView({ summary }: DiagnosisViewProps) {
  const [copied, setCopied] = useState(false);
  const [showRaw, setShowRaw] = useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(summary);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const structured = parseStructuredDiagnosis(summary);

  return (
    <div className="relative overflow-hidden rounded-[8px] border-t border-t-amber-500/50 border-x border-x-amber-900/30 border-b border-b-zinc-950 bg-gradient-to-b from-amber-950/25 via-[#101014] to-[#0a0a0d] p-4 sm:p-5 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_4px_16px_rgba(0,0,0,0.4)]">
      {/* Top ambient highlight line */}
      <span
        aria-hidden="true"
        className="pointer-events-none absolute inset-x-0 top-0 h-[1px] bg-gradient-to-r from-transparent via-amber-400/30 to-transparent"
      />

      {/* Header Bar */}
      <div className="flex items-center justify-between gap-3 border-b border-zinc-800/80 pb-3">
        <div className="flex items-center gap-2.5">
          <div className="flex h-7 w-7 items-center justify-center rounded-[6px] bg-amber-500/10 border border-amber-500/30 text-amber-400 shadow-[inset_0_1px_0_rgba(255,255,255,0.08)]">
            <Bug className="h-4 w-4" />
          </div>
          <div>
            <h4 className="text-xs font-bold uppercase tracking-wider text-amber-400 font-mono flex items-center gap-2">
              <span>Root Cause Diagnosis</span>
              <span className="h-1.5 w-1.5 rounded-full bg-amber-400 animate-pulse" />
            </h4>
            <p className="text-[11px] text-zinc-400 font-mono">
              Autonomous subagent failure localization & root cause isolation
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2">
          {structured && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => setShowRaw(!showRaw)}
              className="h-7 px-2 text-[11px] font-mono text-zinc-400 hover:text-zinc-200 bg-zinc-900/80 border border-zinc-800 hover:border-zinc-700 rounded-[5px]"
            >
              {showRaw ? (
                <>
                  <ChevronUp className="h-3 w-3 mr-1" /> Structured
                </>
              ) : (
                <>
                  <ChevronDown className="h-3 w-3 mr-1" /> Raw View
                </>
              )}
            </Button>
          )}

          <Button
            variant="outline"
            size="sm"
            onClick={handleCopy}
            className="h-7 px-2.5 text-[11px] font-mono rounded-[5px] bg-gradient-to-b from-zinc-800/90 via-zinc-850 to-zinc-900/90 border-t border-t-zinc-600/60 border-x border-x-zinc-700/60 border-b border-b-zinc-950 text-zinc-200 shadow-[inset_0_1px_0_rgba(255,255,255,0.1),0_1px_3px_rgba(0,0,0,0.3)] active:translate-y-[0.5px] transition-all"
            title="Copy full diagnosis summary"
          >
            {copied ? (
              <>
                <Check className="h-3 w-3 text-emerald-400 mr-1" />
                <span className="text-emerald-300">Copied</span>
              </>
            ) : (
              <>
                <Copy className="h-3 w-3 text-zinc-400 mr-1" />
                <span>Copy</span>
              </>
            )}
          </Button>
        </div>
      </div>

      {/* Content Area */}
      <div className="mt-4 space-y-3.5">
        {showRaw || !structured ? (
          /* Raw / Fallback view: supports standard text formatting with high contrast */
          <div className="rounded-[6px] border border-zinc-800/80 bg-[#08080a] p-3.5 shadow-[inset_0_1px_2px_rgba(0,0,0,0.5)]">
            <div className="text-xs text-zinc-300 leading-relaxed font-mono whitespace-pre-wrap select-text">
              {summary}
            </div>
          </div>
        ) : (
          /* Structured Elevated Telemetry View */
          <div className="space-y-3">
            {/* Error Type Callout */}
            {structured.errorType && (
              <div className="rounded-[6px] border-t border-t-rose-700/40 border-x border-x-rose-900/30 border-b border-b-zinc-950 bg-gradient-to-b from-rose-950/20 via-[#120d0f]/60 to-[#0a0809] p-3 shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]">
                <div className="flex items-start gap-2.5">
                  <div className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-[4px] bg-rose-500/15 border border-rose-500/30 text-rose-400">
                    <AlertTriangle className="h-3 w-3" />
                  </div>
                  <div className="flex-1 min-w-0 space-y-1.5">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-[10.5px] font-mono font-bold uppercase tracking-wider text-rose-400">
                        Error Type
                      </span>
                      <code className="rounded-[4px] bg-rose-950/50 border border-rose-800/60 px-2 py-0.5 font-mono text-xs font-semibold text-rose-200 select-text">
                        {structured.errorType.name}
                      </code>
                    </div>
                    {structured.errorType.details && (
                      <p className="text-xs text-zinc-300 leading-relaxed font-mono">
                        {renderInlineText(structured.errorType.details)}
                      </p>
                    )}
                  </div>
                </div>
              </div>
            )}

            {/* Location Pill / File & Line */}
            {structured.fileLine && (
              <div className="rounded-[6px] border border-zinc-800/80 bg-zinc-900/60 p-3 shadow-[inset_0_1px_0_rgba(255,255,255,0.02)]">
                <div className="flex items-start gap-2.5">
                  <div className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-[4px] bg-cyan-500/10 border border-cyan-500/30 text-cyan-400">
                    <FileCode className="h-3 w-3" />
                  </div>
                  <div className="flex-1 min-w-0 space-y-1">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-[10.5px] font-mono font-bold uppercase tracking-wider text-cyan-400">
                        Failure Location
                      </span>
                      <span className="inline-flex items-center gap-1 rounded-[4px] bg-zinc-900 border border-zinc-700/80 px-2 py-0.5 font-mono text-xs text-cyan-200 font-semibold select-text">
                        {structured.fileLine.location}
                      </span>
                    </div>
                    {structured.fileLine.details && (
                      <p className="text-xs text-zinc-400 font-mono">
                        {renderInlineText(structured.fileLine.details)}
                      </p>
                    )}
                  </div>
                </div>
              </div>
            )}

            {/* Why CI Failed Explanatory Card */}
            {structured.whyFailed && (
              <div className="rounded-[6px] border border-amber-500/20 bg-gradient-to-b from-amber-950/15 via-[#131215] to-[#0c0c0e] p-3.5 shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]">
                <div className="flex items-start gap-2.5">
                  <div className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-[4px] bg-amber-500/10 border border-amber-500/30 text-amber-400">
                    <Sparkles className="h-3 w-3" />
                  </div>
                  <div className="flex-1 min-w-0 space-y-1.5">
                    <span className="text-[10.5px] font-mono font-bold uppercase tracking-wider text-amber-400">
                      Why CI Failed
                    </span>
                    <p className="text-xs text-zinc-200 leading-relaxed font-mono select-text">
                      {renderInlineText(structured.whyFailed)}
                    </p>
                  </div>
                </div>
              </div>
            )}

            {/* Files in the Failing Commit */}
            {structured.files && structured.files.length > 0 && (
              <div className="rounded-[6px] border border-zinc-800/80 bg-zinc-950/60 p-3 space-y-2">
                <div className="flex items-center justify-between">
                  <span className="text-[10.5px] font-mono font-semibold uppercase tracking-wider text-zinc-400 flex items-center gap-1.5">
                    <FolderGit2 className="h-3 w-3 text-zinc-500" />
                    Files in Failing Commit ({structured.files.length})
                  </span>
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {structured.files.map((file, fIdx) => (
                    <span
                      key={fIdx}
                      className="inline-flex items-center gap-1.5 rounded-[4px] border border-zinc-800 bg-zinc-900/90 px-2 py-1 font-mono text-[11px] text-zinc-300 hover:border-zinc-700 hover:text-zinc-100 transition-colors select-text"
                    >
                      <FileCode className="h-3 w-3 text-zinc-500" />
                      {file}
                    </span>
                  ))}
                </div>
              </div>
            )}

            {/* Any remaining unparsed content */}
            {structured.otherContent.length > 0 && (
              <div className="rounded-[6px] border border-zinc-800/60 bg-[#09090b] p-3 text-xs text-zinc-400 font-mono space-y-1 select-text">
                {structured.otherContent.map((line, lIdx) => (
                  <div key={lIdx}>{renderInlineText(line)}</div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
