"use client";

/**
 * SessionWorkspaceClient — Cloud Agentic Live Session interactive workspace client.
 *
 * Modern AI Chat Workspace:
 *   - Conversational stream matching Gemini / Cursor / Claude style.
 *   - Sleek thought accordion ("Thought for 7s >").
 *   - Tool call execution grouping ("Exploring 1 file, 1 folder v").
 *   - Rich markdown and code block rendering with copy buttons.
 *   - Single floating bottom input bar with "+" actions, model selector pill, and send/stop button.
 *   - Seamless view switching: Chat (SS2 style), Staged Diffs (Monaco), or Split view.
 *   - Verification runner and PR commit workflow.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import dynamic from "next/dynamic";
import Link from "next/link";
import {
  Send,
  FlaskConical,
  GitPullRequest,
  X,
  CheckCircle2,
  XCircle,
  Loader2,
  FileCode2,
  ChevronDown,
  ChevronRight,
  ChevronUp,
  AlertTriangle,
  ExternalLink,
  WrapText,
  Copy,
  Check,
  ThumbsUp,
  ThumbsDown,
  Plus,
  Square,
  Mic,
  MessageSquare,
  Columns,
  Sparkles,
  FileText,
  Folder,
  Code2,
  Search,
  Zap,
  Compass,
  FolderTree,
  FilePlus,
  FileMinus,
  Layers,
  Terminal,
  ShieldCheck,
  TestTube2,
  Globe,
  Package,
  ListTodo,
  Clock,
  Undo2,
  ShieldAlert,
  GitBranch,
} from "lucide-react";
import { api, SessionOut, CheckpointOut, ApiError, AvailableModelItem } from "@/lib/api";
import {
  useSessionStream,
  ChatMessage,
  ToolCallChip,
  SubagentCardState,
  AuditFinding,
} from "@/hooks/useSessionStream";
import { AuditReportCard } from "@/components/workspace/AuditReportCard";
import { ClarificationPromptCard } from "@/components/workspace/ClarificationPromptCard";
import { AppLayout } from "@/components/layout/app-layout";
import { WebPreviewPanel } from "@/components/workspace/WebPreviewPanel";
import { useWebContainer } from "@/hooks/useWebContainer";
import type { FileSystemTree } from "@/hooks/useWebContainer";

// ---------------------------------------------------------------------------
// Monaco DiffEditor — loaded dynamically (ssr:false, browser APIs required)
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
// Offline fallback model presets — used when GET /config/model/available fails.
// These are known-stable models; the API response will override these on success.
// ---------------------------------------------------------------------------

const FALLBACK_MODELS = {
  opencode_zen: [
    { id: "nemotron-3.5-lightning-free", name: "Nemotron 3.5 Lightning", tag: "Free", context_window: 131072 },
    { id: "space-bunny-free", name: "Space Bunny", tag: "1M · Free", context_window: 1048576 },
    { id: "longcat-2.5-preview-free", name: "Longcat 2.5 Preview", tag: "1M · Free", context_window: 1048576 },
    { id: "ling-3.0-flash-fin-free", name: "Ling 3.0 Flash", tag: "1M · Free", context_window: 1048576 },
    { id: "mimo-v2.6-flash-free", name: "MiMo 2.6 Flash", tag: "256k · Free", context_window: 262144 },
    { id: "laguna-s-2.1-free", name: "Laguna S 2.1", tag: "Free", context_window: 131072 },
    { id: "deepseek-r1-0528-free", name: "DeepSeek R1 0528", tag: "Free", context_window: 65536 },
  ],
  groq: [
    { id: "llama-3.3-70b-versatile", name: "Llama 3.3 70B Versatile", tag: "Fast", context_window: 131072 },
    { id: "llama-3.1-8b-instant", name: "Llama 3.1 8B Instant", tag: "Fast", context_window: 131072 },
    { id: "openai/gpt-oss-120b", name: "GPT OSS 120B (Groq)", tag: "Fast", context_window: 131072 },
    { id: "deepseek-r1-distill-llama-70b", name: "DeepSeek R1 Llama 70B", tag: "Fast", context_window: 131072 },
    { id: "gemma2-9b-it", name: "Gemma 2 9B", tag: "Fast", context_window: 8192 },
  ],
  anthropic: [
    { id: "claude-sonnet-4-5", name: "Claude Sonnet 4.5", tag: "SOTA", context_window: 200000 },
    { id: "claude-haiku-3-5", name: "Claude Haiku 3.5", tag: "SOTA", context_window: 200000 },
  ],
  openai: [
    { id: "gpt-4o", name: "GPT-4o", tag: "GPT", context_window: 128000 },
    { id: "gpt-4o-mini", name: "GPT-4o Mini", tag: "GPT", context_window: 128000 },
  ],
} as const satisfies Record<string, { id: string; name: string; tag: string; context_window?: number }[]>;

const MODEL_CONTEXT_WINDOWS: Record<string, number> = {
  "space-bunny-free": 1048576,
  "longcat-2.5-preview-free": 1048576,
  "ling-3.0-flash-fin-free": 1048576,
  "mimo-v2.5-free": 262144,
  "mimo-v2.6-flash-free": 262144,
  "muse-spark-1.2-contributor-free": 131072,
  "muse-spark-1.3-contributor-free": 131072,
  "nemotron-3-ultra-free": 131072,
  "nemotron-3.5-lightning-free": 131072,
  "jev-1.13-free": 131072,
  "laguna-s-2.1-free": 131072,
  "deepseek-r1-0528-free": 65536,
  "llama-3.3-70b-versatile": 131072,
  "llama-3.1-8b-instant": 131072,
  "openai/gpt-oss-120b": 131072,
  "deepseek-r1-distill-llama-70b": 131072,
  "gemma2-9b-it": 8192,
  "claude-sonnet-4-5": 200000,
  "claude-haiku-3-5": 200000,
  "gpt-4o": 128000,
  "gpt-4o-mini": 128000,
};

const MODEL_SHORT_NAMES: Record<string, string> = {
  "nemotron-3.5-lightning-free": "Nemotron 3.5",
  "laguna-s-2.1-free": "Laguna 2.1",
  "space-bunny-free": "Space Bunny",
  "longcat-2.5-preview-free": "Longcat 2.5",
  "ling-3.0-flash-fin-free": "Ling 3.0",
  "mimo-v2.6-flash-free": "MiMo 2.6",
  "deepseek-r1-0528-free": "DeepSeek R1",
  "llama-3.3-70b-versatile": "Llama 3.3 70B",
  "llama-3.1-8b-instant": "Llama 3.1 8B",
  "openai/gpt-oss-120b": "GPT OSS 120B",
  "deepseek-r1-distill-llama-70b": "DeepSeek R1 70B",
  "gemma2-9b-it": "Gemma 2 9B",
  "claude-sonnet-4-5": "Claude 3.5 Sonnet",
  "claude-haiku-3-5": "Claude 3.5 Haiku",
  "gpt-4o": "GPT-4o",
  "gpt-4o-mini": "GPT-4o Mini",
};

function formatTokens(count: number): string {
  if (count >= 1000000) {
    const m = count / 1000000;
    return m >= 1 && m < 1.05 ? "1M" : `${m.toFixed(1)}M`;
  }
  if (count >= 1000) return `${(count / 1000).toFixed(count >= 10000 ? 0 : 1)}k`;
  return `${count}`;
}

// ---------------------------------------------------------------------------
// Helpers & Sub-components
// ---------------------------------------------------------------------------
// Code Block with Copy Button
// ---------------------------------------------------------------------------

function CodeBlock({ code, lang }: { code: string; lang: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(code);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="my-3 overflow-hidden rounded-xl border border-zinc-800 bg-[#0d0d11] text-xs font-mono shadow-sm">
      <div className="flex items-center justify-between border-b border-zinc-800/80 bg-[#141419] px-3.5 py-1.5 text-zinc-400">
        <span className="text-[11px] font-mono text-zinc-400 lowercase">{lang || "code"}</span>
        <button
          onClick={handleCopy}
          className="flex items-center gap-1 rounded px-2 py-0.5 text-[10px] text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200 transition-colors"
        >
          {copied ? <Check className="h-3 w-3 text-emerald-400" /> : <Copy className="h-3 w-3" />}
          <span>{copied ? "Copied" : "Copy"}</span>
        </button>
      </div>
      <div className="overflow-x-auto p-3.5 text-zinc-200 leading-relaxed font-mono">
        <pre>{code}</pre>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Markdown text renderer with tables, headings, lists, blockquotes, inline code
// ---------------------------------------------------------------------------

function renderInlineTokens(text: string): React.ReactNode[] {
  if (!text) return [];

  // Match: `code`, **bold**, [link](url)
  const regex = /(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\([^)]+\))/g;
  const parts = text.split(regex);

  return parts.map((part, idx) => {
    if (part.startsWith("`") && part.endsWith("`") && part.length >= 2) {
      return (
        <code
          key={idx}
          className="mx-0.5 rounded-[5px] border border-zinc-700/60 bg-zinc-800/90 px-1.5 py-0.5 text-[11px] font-mono text-amber-200 shadow-[inset_0_1px_0_rgba(255,255,255,0.06)]"
        >
          {part.slice(1, -1)}
        </code>
      );
    }
    if (part.startsWith("**") && part.endsWith("**") && part.length >= 4) {
      return (
        <strong key={idx} className="font-semibold text-zinc-100">
          {renderInlineTokens(part.slice(2, -2))}
        </strong>
      );
    }
    const linkMatch = part.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
    if (linkMatch) {
      return (
        <a
          key={idx}
          href={linkMatch[2]}
          target="_blank"
          rel="noopener noreferrer"
          className="text-violet-400 hover:text-violet-300 underline underline-offset-2 transition-colors"
        >
          {linkMatch[1]}
        </a>
      );
    }
    return <span key={idx}>{part}</span>;
  });
}

function parseTableRow(row: string): string[] {
  let clean = row.trim();
  if (clean.startsWith("|")) clean = clean.slice(1);
  if (clean.endsWith("|")) clean = clean.slice(0, -1);
  return clean.split("|").map((cell) => cell.trim());
}

function isTableSeparator(line: string): boolean {
  const clean = line.trim();
  if (!clean.includes("-")) return false;
  const cells = parseTableRow(clean);
  return cells.length > 0 && cells.every((c) => /^:?-+:?$/.test(c));
}

function isTableRow(line: string): boolean {
  const clean = line.trim();
  return clean.startsWith("|") || (clean.includes("|") && clean.endsWith("|"));
}

function parseAlignments(separatorLine: string): ("left" | "center" | "right")[] {
  const cells = parseTableRow(separatorLine);
  return cells.map((cell) => {
    const trimmed = cell.trim();
    const starts = trimmed.startsWith(":");
    const ends = trimmed.endsWith(":");
    if (starts && ends) return "center";
    if (ends) return "right";
    return "left";
  });
}

function FormattedText({ text }: { text: string }) {
  if (!text) return null;

  const lines = text.split("\n");
  const elements: React.ReactNode[] = [];
  let i = 0;
  let keyIndex = 0;

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    // 1. Empty lines
    if (!trimmed) {
      elements.push(<div key={keyIndex++} className="h-1.5" />);
      i++;
      continue;
    }

    // 2. GFM Markdown Table Detection (Current line is table row + next line is separator)
    if (i + 1 < lines.length && isTableRow(line) && isTableSeparator(lines[i + 1])) {
      const headers = parseTableRow(line);
      const alignments = parseAlignments(lines[i + 1]);
      i += 2;

      const rows: string[][] = [];
      while (i < lines.length && isTableRow(lines[i]) && lines[i].trim() !== "" && !isTableSeparator(lines[i])) {
        rows.push(parseTableRow(lines[i]));
        i++;
      }

      elements.push(
        <div
          key={keyIndex++}
          className="my-3 overflow-hidden rounded-xl border border-zinc-800 bg-[#0c0c10] shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_2px_8px_rgba(0,0,0,0.4)]"
        >
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs border-collapse">
              <thead>
                <tr className="border-b border-zinc-800 bg-[#14141a]">
                  {headers.map((h, hIdx) => {
                    const align = alignments[hIdx] || "left";
                    return (
                      <th
                        key={hIdx}
                        className={`px-3.5 py-2.5 font-mono text-[11px] font-semibold text-zinc-300 uppercase tracking-wider ${
                          align === "center"
                            ? "text-center"
                            : align === "right"
                            ? "text-right"
                            : "text-left"
                        }`}
                      >
                        {renderInlineTokens(h)}
                      </th>
                    );
                  })}
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-850/80 font-sans">
                {rows.map((row, rIdx) => (
                  <tr
                    key={rIdx}
                    className="hover:bg-zinc-800/30 transition-colors duration-100 odd:bg-transparent even:bg-[#111116]/50"
                  >
                    {row.map((cell, cIdx) => {
                      const align = alignments[cIdx] || "left";
                      return (
                        <td
                          key={cIdx}
                          className={`px-3.5 py-2.5 text-zinc-200 leading-relaxed text-xs ${
                            align === "center"
                              ? "text-center"
                              : align === "right"
                              ? "text-right"
                              : "text-left"
                          }`}
                        >
                          {renderInlineTokens(cell)}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      );
      continue;
    }

    // 3. Headings (#, ##, ###, ####)
    const headingMatch = line.match(/^(#{1,4})\s+(.+)$/);
    if (headingMatch) {
      const level = headingMatch[1].length;
      const content = headingMatch[2];
      if (level === 1) {
        elements.push(
          <h1 key={keyIndex++} className="text-base font-semibold text-zinc-100 mt-4 mb-1.5 font-sans">
            {renderInlineTokens(content)}
          </h1>
        );
      } else if (level === 2) {
        elements.push(
          <h2 key={keyIndex++} className="text-sm font-semibold text-zinc-100 mt-3 mb-1 font-sans">
            {renderInlineTokens(content)}
          </h2>
        );
      } else {
        elements.push(
          <h3 key={keyIndex++} className="text-xs font-semibold text-zinc-200 mt-2.5 mb-1 font-sans uppercase tracking-wide">
            {renderInlineTokens(content)}
          </h3>
        );
      }
      i++;
      continue;
    }

    // 4. Blockquote (> quote)
    if (line.startsWith("> ") || line === ">") {
      const quoteLines: string[] = [];
      while (i < lines.length && (lines[i].startsWith("> ") || lines[i] === ">")) {
        quoteLines.push(lines[i].replace(/^>\s?/, ""));
        i++;
      }
      elements.push(
        <div
          key={keyIndex++}
          className="my-2 border-l-2 border-violet-500/60 bg-violet-500/5 px-3 py-1.5 rounded-r-lg text-xs text-zinc-300 font-sans italic"
        >
          {quoteLines.map((ql, qlIdx) => (
            <p key={qlIdx} className="leading-relaxed">{renderInlineTokens(ql)}</p>
          ))}
        </div>
      );
      continue;
    }

    // 5. Unordered List Items (- or * or •)
    if (/^\s*[-*•]\s+/.test(line)) {
      const cleanLine = line.replace(/^\s*[-*•]\s+/, "");
      elements.push(
        <div key={keyIndex++} className="flex items-start gap-2 pl-2 my-0.5">
          <span className="text-zinc-500 mt-1 select-none text-[10px]">•</span>
          <p className="flex-1 text-[13.5px] leading-relaxed text-zinc-200">
            {renderInlineTokens(cleanLine)}
          </p>
        </div>
      );
      i++;
      continue;
    }

    // 6. Ordered List Items (1. 2.)
    const numListMatch = line.match(/^\s*(\d+)\.\s+(.+)$/);
    if (numListMatch) {
      const num = numListMatch[1];
      const cleanLine = numListMatch[2];
      elements.push(
        <div key={keyIndex++} className="flex items-start gap-2 pl-2 my-0.5">
          <span className="text-zinc-400 font-mono text-xs mt-0.5 select-none">{num}.</span>
          <p className="flex-1 text-[13.5px] leading-relaxed text-zinc-200">
            {renderInlineTokens(cleanLine)}
          </p>
        </div>
      );
      i++;
      continue;
    }

    // 7. Regular paragraph
    elements.push(
      <p key={keyIndex++} className="text-[13.5px] leading-relaxed text-zinc-200">
        {renderInlineTokens(line)}
      </p>
    );
    i++;
  }

  return <div className="space-y-1.5">{elements}</div>;
}

function MarkdownContent({ content }: { content: string }) {
  if (!content) return null;

  // Split by fenced code blocks
  const parts = content.split(/(```[\s\S]*?```)/g);

  return (
    <div className="space-y-2">
      {parts.map((part, index) => {
        if (part.startsWith("```") && part.endsWith("```")) {
          const lines = part.slice(3, -3).trim().split("\n");
          const firstLine = lines[0].trim();
          const hasLang = /^[a-zA-Z0-9_-]+$/.test(firstLine);
          const lang = hasLang ? firstLine : "";
          const code = (hasLang ? lines.slice(1) : lines).join("\n");

          return <CodeBlock key={index} code={code} lang={lang} />;
        }
        return <FormattedText key={index} text={part} />;
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Thought Accordion ("Thought for 7s >" matching Screenshot 2)
// ---------------------------------------------------------------------------

function ThoughtAccordion({
  thoughts,
  durationSeconds,
  isStreaming,
}: {
  thoughts: string[];
  durationSeconds?: number;
  isStreaming?: boolean;
}) {
  const [open, setOpen] = useState(false);
  if (!thoughts || thoughts.length === 0) return null;

  const seconds = durationSeconds ?? Math.max(3, Math.min(12, thoughts.length * 2 + 1));

  return (
    <div className="my-1.5 select-none">
      <button
        onClick={() => setOpen((prev) => !prev)}
        className="group inline-flex items-center gap-1.5 text-xs font-sans text-zinc-400 hover:text-zinc-200 transition-colors py-1 cursor-pointer"
      >
        <span className="font-medium text-zinc-400 group-hover:text-zinc-300">
          {isStreaming ? "Thinking..." : `Thought for ${seconds}s`}
        </span>
        <ChevronRight
          className={`h-3 w-3 text-zinc-500 transition-transform duration-200 ${
            open ? "rotate-90 text-zinc-300" : ""
          }`}
        />
      </button>

      {open && (
        <div className="mt-1.5 mb-2 pl-3 border-l-2 border-zinc-800 space-y-1.5 py-1">
          {thoughts.map((t, idx) => (
            <p key={idx} className="text-xs font-mono text-zinc-400 leading-relaxed">
              {t}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tool Execution Group Accordion ("Exploring 1 file, 1 folder v")
// ---------------------------------------------------------------------------

/**
 * Collapsible accordion grouping completed tool execution chips for a chat turn.
 * Displays summary title (e.g. "Exploring 1 file, 1 folder") and individual chips.
 */
function ToolExecutionAccordion({
  toolCalls,
  thoughts,
  thoughtDuration,
  isStreamingTurn,
  onViewDiff,
  onClarificationSelect,
}: {
  toolCalls: ToolCallChip[];
  thoughts?: string[];
  thoughtDuration?: number;
  isStreamingTurn?: boolean;
  onViewDiff?: (filePath: string) => void;
  onClarificationSelect?: (choice: string) => Promise<void> | void;
}) {
  const [open, setOpen] = useState(true);

  if ((!toolCalls || toolCalls.length === 0) && !isStreamingTurn) {
    return null;
  }

  // Generate clean summary title like Screenshot 2: "Exploring 1 file, 1 folder"
  const fileCount = toolCalls.filter(
    (c) => c.name === "read_file" || c.name === "stage_patch" || c.name === "read_file_slice" || c.name === "get_file_outline"
  ).length;
  const folderCount = toolCalls.filter(
    (c) => c.name === "list_files" || c.name === "list_directory"
  ).length;
  const searchCount = toolCalls.filter(
    (c) => c.name === "grep_search" || c.name === "glob_files"
  ).length;
  const editCount = toolCalls.filter(
    (c) =>
      c.name === "str_replace" ||
      c.name === "create_file" ||
      c.name === "delete_file" ||
      c.name === "apply_multi_patch"
  ).length;
  const symbolCount = toolCalls.filter(
    (c) => c.name === "find_symbol" || c.name === "find_references"
  ).length;
  const sandboxCount = toolCalls.filter(
    (c) =>
      c.name === "run_terminal_command" ||
      c.name === "run_linter" ||
      c.name === "run_targeted_tests"
  ).length;
  const webCount = toolCalls.filter(
    (c) =>
      c.name === "search_web_docs" ||
      c.name === "fetch_web_content" ||
      c.name === "fetch_package_metadata"
  ).length;
  const planCount = toolCalls.filter((c) => c.name === "update_plan").length;
  const clarificationCalls = toolCalls.filter((c) => c.name === "ask_user_clarification");
  const clarificationCount = clarificationCalls.length;

  let summaryTitle = `Executed ${toolCalls.length} tool${toolCalls.length !== 1 ? "s" : ""}`;
  if (editCount > 0 && fileCount === 0 && folderCount === 0 && searchCount === 0 && symbolCount === 0) {
    summaryTitle = `Edited ${editCount} file${editCount > 1 ? "s" : ""}`;
  } else if (symbolCount > 0 && editCount === 0 && fileCount === 0 && folderCount === 0 && searchCount === 0) {
    summaryTitle = `Analyzing symbols (${symbolCount} lookup${symbolCount > 1 ? "s" : ""})`;
  } else if (sandboxCount > 0 && editCount === 0 && fileCount === 0 && folderCount === 0 && searchCount === 0 && symbolCount === 0) {
    summaryTitle = `Running sandbox (${sandboxCount} command${sandboxCount > 1 ? "s" : ""})`;
  } else if (webCount > 0 && editCount === 0 && fileCount === 0 && folderCount === 0 && searchCount === 0 && symbolCount === 0 && sandboxCount === 0) {
    summaryTitle = `Searching web (${webCount} request${webCount > 1 ? "s" : ""})`;
  } else if (planCount > 0 && editCount === 0 && fileCount === 0 && folderCount === 0 && searchCount === 0 && symbolCount === 0 && sandboxCount === 0 && webCount === 0 && clarificationCount === 0) {
    summaryTitle = "Planning execution";
  } else if (clarificationCount > 0 && editCount === 0 && fileCount === 0 && folderCount === 0 && searchCount === 0 && symbolCount === 0 && sandboxCount === 0 && webCount === 0 && planCount === 0) {
    summaryTitle = clarificationCount === 1 ? "Waiting for user clarification" : `Waiting for clarification (${clarificationCount} questions)`;
  } else if (fileCount > 0 && folderCount > 0) {
    summaryTitle = `Exploring ${fileCount} file${fileCount > 1 ? "s" : ""}, ${folderCount} folder${folderCount > 1 ? "s" : ""}`;
  } else if (searchCount > 0 && fileCount === 0 && folderCount === 0) {
    summaryTitle = `Searching codebase (${searchCount} quer${searchCount > 1 ? "ies" : "y"})`;
  } else if (fileCount > 0) {
    summaryTitle = `Analyzing ${fileCount} file${fileCount > 1 ? "s" : ""}`;
  } else if (folderCount > 0) {
    summaryTitle = `Exploring ${folderCount} folder${folderCount > 1 ? "s" : ""}`;
  } else if (isStreamingTurn && toolCalls.length === 0) {
    summaryTitle = "Exploring repository…";
  }

  return (
    <div className="my-2 select-none">
      <button
        onClick={() => setOpen((prev) => !prev)}
        className="flex items-center gap-1.5 text-xs font-sans text-zinc-300 hover:text-zinc-100 transition-colors py-1 cursor-pointer"
      >
        <span className="font-medium text-zinc-300">{summaryTitle}</span>
        {clarificationCount > 0 && (
          <span className="ml-1.5 inline-flex items-center gap-1 rounded-full bg-amber-500/20 border border-amber-500/35 px-2 py-0.5 text-[10px] font-mono text-amber-300 font-semibold">
            <span className="h-1.5 w-1.5 rounded-full bg-amber-400 animate-pulse" />
            Awaiting Input
          </span>
        )}
        <ChevronDown
          className={`h-3.5 w-3.5 text-zinc-400 transition-transform duration-200 ${
            open ? "" : "-rotate-90"
          }`}
        />
      </button>

      {open && (
        <div className="mt-1.5 pl-3 border-l-2 border-zinc-800/80 space-y-1.5">
          {/* Nested thought block if present */}
          {thoughts && thoughts.length > 0 && (
            <ThoughtAccordion
              thoughts={thoughts}
              durationSeconds={thoughtDuration}
              isStreaming={isStreamingTurn && toolCalls.length === 0}
            />
          )}

          {/* List of tool calls */}
          {toolCalls.map((chip, idx) => {
            const rawPath = (chip.args?.path as string) || "";
            const isStagePatch = chip.name === "stage_patch";
            const isListFiles = chip.name === "list_files";
            const isGrepSearch = chip.name === "grep_search";
            const isGlobFiles = chip.name === "glob_files";
            const isReadFileSlice = chip.name === "read_file_slice";
            const isListDirectory = chip.name === "list_directory";
            const isStrReplace = chip.name === "str_replace";
            const isCreateFile = chip.name === "create_file";
            const isDeleteFile = chip.name === "delete_file";
            const isMultiPatch = chip.name === "apply_multi_patch";
            const isGetFileOutline = chip.name === "get_file_outline";
            const isFindSymbol = chip.name === "find_symbol";
            const isFindReferences = chip.name === "find_references";
            const isRunTerminal = chip.name === "run_terminal_command";
            const isRunLinter = chip.name === "run_linter";
            const isRunTests = chip.name === "run_targeted_tests";

            let icon = <FileText className="h-3.5 w-3.5 text-blue-400/80 shrink-0" />;
            let label = rawPath || chip.name;
            let actionPrefix = isStagePatch ? "Staged" : "Analyzed";
            let showViewDiff = false;

            if (isGrepSearch) {
              const query = (chip.args?.query as string) || "";
              const matchCount = chip.args?.match_count as number | undefined;
              icon = <Search className="h-3.5 w-3.5 text-cyan-400/80 shrink-0" />;
              actionPrefix = "";
              label =
                matchCount !== undefined
                  ? `Searched '${query}' (${matchCount} match${matchCount !== 1 ? "es" : ""})`
                  : `Searched '${query}'`;
            } else if (isGlobFiles) {
              const pattern = (chip.args?.pattern as string) || "";
              const fileCount = chip.args?.file_count as number | undefined;
              icon = <Compass className="h-3.5 w-3.5 text-emerald-400/80 shrink-0" />;
              actionPrefix = "";
              label =
                fileCount !== undefined
                  ? `Found ${fileCount} files matching '${pattern}'`
                  : `Found files matching '${pattern}'`;
            } else if (isReadFileSlice) {
              const start = chip.args?.start_line ?? 1;
              const end = chip.args?.end_line ?? start;
              icon = <FileText className="h-3.5 w-3.5 text-sky-400/80 shrink-0" />;
              actionPrefix = "";
              label = `Read ${rawPath} (L${start}-L${end})`;
            } else if (isListDirectory) {
              const dirPath = rawPath || (chip.args?.path as string) || ".";
              icon = <FolderTree className="h-3.5 w-3.5 text-amber-400/80 shrink-0" />;
              actionPrefix = "";
              label = `Listed directory ${dirPath}`;
            } else if (isListFiles) {
              icon = <Folder className="h-3.5 w-3.5 text-amber-400/80 shrink-0" />;
            } else if (isStagePatch) {
              icon = <FileCode2 className="h-3.5 w-3.5 text-violet-400/80 shrink-0" />;
              showViewDiff = true;
            } else if (isStrReplace) {
              icon = <FileCode2 className="h-3.5 w-3.5 text-violet-400/80 shrink-0" />;
              actionPrefix = "";
              label = `Edited ${rawPath}`;
              showViewDiff = true;
            } else if (isCreateFile) {
              icon = <FilePlus className="h-3.5 w-3.5 text-emerald-400/80 shrink-0" />;
              actionPrefix = "";
              label = `Created ${rawPath}`;
              showViewDiff = true;
            } else if (isDeleteFile) {
              icon = <FileMinus className="h-3.5 w-3.5 text-rose-400/80 shrink-0" />;
              actionPrefix = "";
              label = `Deleted ${rawPath}`;
            } else if (isMultiPatch) {
              const patchList = (chip.args?.patches as unknown[]) || [];
              icon = <Layers className="h-3.5 w-3.5 text-indigo-400/80 shrink-0" />;
              actionPrefix = "";
              label = `Multi-file edit (${patchList.length} file${patchList.length !== 1 ? "s" : ""})`;
            } else if (isGetFileOutline) {
              icon = <FileCode2 className="h-3.5 w-3.5 text-sky-300/80 shrink-0" />;
              actionPrefix = "";
              label = `Outlined ${rawPath || "file"}`;
            } else if (isFindSymbol) {
              const symName = (chip.args?.name as string) || "";
              const symCount = chip.args?.symbol_count as number | undefined;
              icon = <Search className="h-3.5 w-3.5 text-violet-400/80 shrink-0" />;
              actionPrefix = "";
              label =
                symCount !== undefined
                  ? `Found symbol '${symName}' (${symCount} match${symCount !== 1 ? "es" : ""})`
                  : `Found symbol '${symName}'`;
            } else if (isFindReferences) {
              const symName = (chip.args?.symbol as string) || "";
              const refCount = chip.args?.match_count as number | undefined;
              icon = <Zap className="h-3.5 w-3.5 text-amber-300/80 shrink-0" />;
              actionPrefix = "";
              label =
                refCount !== undefined
                  ? `References for '${symName}' (${refCount} call site${refCount !== 1 ? "s" : ""})`
                  : `References for '${symName}'`;
            } else if (isRunTerminal) {
              const cmd = (chip.args?.command as string) || "";
              const exitCode = chip.args?.exit_code as number | undefined;
              const shortCmd = cmd.length > 40 ? cmd.slice(0, 37) + "…" : cmd;
              icon = <Terminal className="h-3.5 w-3.5 text-emerald-300/80 shrink-0" />;
              actionPrefix = "";
              label =
                exitCode !== undefined
                  ? `Ran '${shortCmd}' (exit: ${exitCode})`
                  : `Ran '${shortCmd}'`;
            } else if (isRunLinter) {
              const fCount = chip.args?.file_count as number | undefined;
              icon = <ShieldCheck className="h-3.5 w-3.5 text-sky-300/80 shrink-0" />;
              actionPrefix = "";
              label =
                fCount !== undefined
                  ? `Linted ${fCount} file${fCount !== 1 ? "s" : ""}`
                  : "Ran linter";
            } else if (isRunTests) {
              const tCount = chip.args?.target_count as number | undefined;
              icon = <TestTube2 className="h-3.5 w-3.5 text-violet-300/80 shrink-0" />;
              actionPrefix = "";
              label =
                tCount !== undefined
                  ? `Ran tests on ${tCount} target${tCount !== 1 ? "s" : ""}`
                  : "Ran targeted tests";
            } else if (chip.name === "search_web_docs") {
              const query = (chip.args?.query as string) || "";
              const domain = (chip.args?.domain as string) || "";
              icon = <Globe className="h-3.5 w-3.5 text-sky-400/80 shrink-0" />;
              actionPrefix = "";
              label = domain
                ? `Searched docs for '${query}' on ${domain}`
                : `Searched docs for '${query}'`;
            } else if (chip.name === "fetch_web_content") {
              const fetchUrl = (chip.args?.url as string) || "";
              const shortUrl =
                fetchUrl.length > 60 ? fetchUrl.slice(0, 57) + "…" : fetchUrl;
              icon = <ExternalLink className="h-3.5 w-3.5 text-cyan-400/80 shrink-0" />;
              actionPrefix = "";
              label = `Read external page ${shortUrl}`;
            } else if (chip.name === "fetch_package_metadata") {
              const pkgName = (chip.args?.package_name as string) || "";
              const eco = (chip.args?.ecosystem as string) || "";
              icon = <Package className="h-3.5 w-3.5 text-amber-400/80 shrink-0" />;
              actionPrefix = "";
              label = `Checked ${pkgName} (${eco})`;
            } else if (chip.name === "update_plan") {
              const comp = (chip.args?.completed_count as number) ?? 0;
              const tot = (chip.args?.total_count as number) ?? 0;
              icon = <ListTodo className="h-3.5 w-3.5 text-indigo-400/80 shrink-0" />;
              actionPrefix = "";
              label = `Updated task checklist (${comp}/${tot})`;
            } else if (chip.name === "ask_user_clarification") {
              const question =
                (chip.args?.question as string) ||
                (typeof chip.args === "string" ? chip.args : "") ||
                "Clarification requested";
              const rawOptions = (chip.args?.options as string[]) || [];
              const options = Array.isArray(rawOptions)
                ? rawOptions.filter((o) => typeof o === "string" && o.trim().length > 0)
                : [];
              return (
                <ClarificationPromptCard
                  key={idx}
                  question={question}
                  options={options}
                  index={
                    clarificationCount > 1
                      ? clarificationCalls.findIndex((c) => c === chip) + 1
                      : undefined
                  }
                  total={clarificationCount > 1 ? clarificationCount : undefined}
                  isPending={true}
                  onSelectOption={onClarificationSelect}
                  disabled={isStreamingTurn}
                />
              );
            } else if (chip.name === "checkpoint_restore") {
              const cpId = (chip.args?.checkpoint_id as string) || "";
              icon = <Undo2 className="h-3.5 w-3.5 text-violet-400/80 shrink-0" />;
              actionPrefix = "";
              label = `Restored to checkpoint '${cpId}'`;
            } else if (chip.name === "scan_security_vulnerabilities") {
              const scanPaths = (chip.args?.paths as string[]) || (chip.args?.file_paths as string[]) || [];
              const scanResult =
                (chip.args?.scan_result as string) ||
                (chip.args?.result as string) ||
                (chip.args?.output as string) ||
                (chip.args?.summary as string) ||
                "";
              const { isClean } = parseSecurityScanResult(scanResult);
              icon = isClean ? (
                <ShieldCheck className="h-3.5 w-3.5 text-emerald-400/80 shrink-0" />
              ) : (
                <ShieldAlert className="h-3.5 w-3.5 text-red-400/80 shrink-0" />
              );
              actionPrefix = "";
              label = isClean
                ? `Security scan passed (${scanPaths.length} file${scanPaths.length !== 1 ? "s" : ""})`
                : `Security violations found in ${scanPaths.length} file${scanPaths.length !== 1 ? "s" : ""}`;
            }

            return (
              <div
                key={idx}
                className="flex items-center justify-between gap-3 text-xs text-zinc-400 py-0.5 group"
              >
                <div className="flex items-center gap-2 font-sans truncate min-w-0">
                  {actionPrefix && (
                    <span className="text-zinc-500 font-normal">
                      {actionPrefix}
                    </span>
                  )}
                  {icon}
                  <span className="font-mono text-zinc-200 truncate" title={label}>
                    {label}
                  </span>
                </div>

                {showViewDiff && rawPath && onViewDiff && (
                  <button
                    onClick={() => onViewDiff(rawPath)}
                    className="shrink-0 rounded border border-violet-500/30 bg-violet-500/10 px-2 py-0.5 text-[10px] font-mono text-violet-300 hover:bg-violet-500/20 transition-colors"
                  >
                    View Diff
                  </button>
                )}
              </div>
            );
          })}

          {/* Working indicator if currently streaming */}
          {isStreamingTurn && (
            <div className="flex items-center gap-2 pt-1 text-xs text-zinc-400 font-sans">
              <span className="relative flex h-2 w-2">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-60" />
                <span className="relative inline-flex rounded-full h-2 w-2 bg-amber-400" />
              </span>
              <span>Working…</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// SubagentCard — live wrapper for invoke_subagent start/done SSE events
// ---------------------------------------------------------------------------

const ROLE_EMOJI: Record<string, string> = {
  repo_navigator: "🧭",
  feature_architect: "⚡",
  bug_hunter: "🔍",
  sandbox_verifier: "🧪",
  code_guardian: "🛡️",
};

function SubagentCard({
  role,
  task,
  summary,
  patchesModified,
  status,
  onViewDiff,
}: SubagentCardState & { onViewDiff?: (filePath: string) => void }) {
  const emoji = ROLE_EMOJI[role] ?? "🤖";
  if (status === "running") {
    return (
      <div className="my-2 rounded-xl border border-zinc-800 bg-[#0e0e12] px-3.5 py-2.5" role="status" aria-live="polite">
        <div className="flex items-center gap-2">
          <span aria-hidden="true" className="text-sm leading-none">{emoji}</span>
          <span className="rounded-full border border-zinc-700/60 bg-zinc-800/80 px-2 py-0.5 font-mono text-[11px] text-zinc-200">{role}</span>
          <span className="relative ml-auto flex h-2 w-2 shrink-0">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-amber-400 opacity-60" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-amber-400" />
          </span>
        </div>
        <p className="mt-1.5 truncate font-mono text-xs text-zinc-500" title={task}>{task.slice(0, 120)}</p>
      </div>
    );
  }
  return (
    <div className="my-2 rounded-xl border border-emerald-500/20 bg-[#0e0e12] px-3.5 py-2.5" aria-live="polite">
      <div className="flex items-center gap-2">
        <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-emerald-400" aria-label="Subagent complete" />
        <span aria-hidden="true" className="text-sm leading-none">{emoji}</span>
        <span className="rounded-full border border-zinc-700/60 bg-zinc-800/80 px-2 py-0.5 font-mono text-[11px] text-zinc-200">{role}</span>
      </div>
      {summary && <p className="mt-1.5 text-[13px] leading-relaxed text-zinc-200">{summary}</p>}
      {patchesModified && patchesModified.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {patchesModified.map((f) => (
            <button key={f} onClick={() => onViewDiff?.(f)} title={f} className="shrink-0 truncate rounded border border-violet-500/30 bg-violet-500/10 px-2 py-0.5 font-mono text-[10px] text-violet-300 transition-colors hover:bg-violet-500/20">
              {f}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Chat Bubble (Matches SS2 user pill + clean assistant presentation)
// ---------------------------------------------------------------------------

/**
 * Extract clean answer text from an answering user message in the conversation.
 * Handles both structured prefix headers and raw text replies.
 */
function extractClarificationAnswer(userMsg?: ChatMessage | null): string | null {
  if (!userMsg?.content) return null;
  const content = userMsg.content.trim();
  if (content.startsWith("[User Clarification Response]: ")) {
    return content.replace("[User Clarification Response]: ", "").trim();
  }
  if (content.startsWith("Proceed with: ")) {
    return content.replace("Proceed with: ", "").trim();
  }
  return content;
}

/**
 * Renders an individual chat bubble for system, user, or assistant turns.
 * Assistant turns render thoughts, tool executions, markdown content, and
 * active/resolved ClarificationPromptCards for `ask_user_clarification` calls.
 */
function ChatBubble({
  message,
  messageIndex,
  messages,
  isLatestStreaming,
  onViewDiff,
  onStageAuditFix,
  onClarificationSelect,
  isSessionBlocked,
}: {
  message: ChatMessage;
  messageIndex?: number;
  messages?: ChatMessage[];
  isLatestStreaming?: boolean;
  onViewDiff?: (filePath: string) => void;
  onStageAuditFix?: (finding: AuditFinding, diff?: string) => Promise<void> | void;
  onClarificationSelect?: (choice: string) => Promise<void> | void;
  isSessionBlocked?: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const [reaction, setReaction] = useState<"up" | "down" | null>(null);

  const isUser = message.role === "user";
  const isSystem = message.role === "system";

  if (isSystem) {
    return (
      <div className="my-3 flex items-center justify-center">
        <div className="rounded-full border-t border-t-zinc-750/50 border-x border-x-zinc-800/60 border-b border-b-zinc-900 bg-[#101014] px-3.5 py-1 text-[11px] font-mono text-zinc-400 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]">
          {message.content}
        </div>
      </div>
    );
  }

  if (isUser) {
    return (
      <div className="flex justify-end my-6">
        <div className="flex flex-col items-end max-w-[85%]">
          <div className="relative rounded-2xl border-t border-t-zinc-700/60 border-x border-x-zinc-800/70 border-b border-b-zinc-900 bg-gradient-to-b from-[#1c1c22] to-[#141418] px-4.5 py-3 shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_2px_8px_rgba(0,0,0,0.35)]">
            <p className="text-[13.5px] font-sans text-zinc-100 leading-relaxed whitespace-pre-wrap selection:bg-amber-500/30">
              {message.content}
            </p>
          </div>
        </div>
      </div>
    );
  }

  // Assistant turn
  const handleCopy = () => {
    if (!message.content) return;
    navigator.clipboard.writeText(message.content);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const toolCalls = message.toolCalls || [];
  const clarificationCalls = toolCalls.filter((c) => c.name === "ask_user_clarification");
  const executionToolCalls = toolCalls.filter((c) => c.name !== "ask_user_clarification");

  const hasExecutionToolCalls = executionToolCalls.length > 0;
  const hasThoughts = message.thoughts && message.thoughts.length > 0;

  // Track subsequent user messages to resolve clarification calls per question index
  const subsequentUserMsgs =
    messages && typeof messageIndex === "number"
      ? messages.slice(messageIndex + 1).filter((m) => m.role === "user")
      : [];

  return (
    <div className="my-5 space-y-2">
      {/* If tools were executed, render tool accordion (with nested thoughts if any) */}
      {hasExecutionToolCalls ? (
        <ToolExecutionAccordion
          toolCalls={executionToolCalls}
          thoughts={message.thoughts}
          thoughtDuration={message.thoughtDurationSeconds}
          isStreamingTurn={isLatestStreaming}
          onViewDiff={onViewDiff}
          onClarificationSelect={onClarificationSelect}
        />
      ) : hasThoughts ? (
        /* Otherwise render thought accordion by itself */
        <ThoughtAccordion
          thoughts={message.thoughts!}
          durationSeconds={message.thoughtDurationSeconds}
          isStreaming={isLatestStreaming && !message.content}
        />
      ) : null}

      {/* Subagent progress cards (chronological, tied to this assistant turn) */}
      {message.subagents && message.subagents.length > 0 && (
        <div className="space-y-2 my-2" aria-live="polite">
          {message.subagents.map((s, i) => (
            <SubagentCard
              key={`${s.role}-${s.startedAt}-${i}`}
              role={s.role}
              task={s.task}
              startedAt={s.startedAt}
              status={s.status}
              summary={s.summary}
              patchesModified={s.patchesModified}
              onViewDiff={onViewDiff}
            />
          ))}
        </div>
      )}

      {/* Main text content */}
      {message.content && (
        <div className="pt-1">
          <MarkdownContent content={message.content} />
        </div>
      )}

      {/* Clarification prompt cards rendered prominently right in transcript */}
      {clarificationCalls.map((chip, idx) => {
        const question =
          (chip.args?.question as string) ||
          (typeof chip.args === "string" ? chip.args : "") ||
          "Clarification requested";
        const rawOptions = (chip.args?.options as string[]) || [];
        const options = Array.isArray(rawOptions)
          ? rawOptions.filter((o) => typeof o === "string" && o.trim().length > 0)
          : [];

        // Match each question to its corresponding answering user turn
        const answeringUserMsg = subsequentUserMsgs[idx];
        const isAnswered = Boolean(answeringUserMsg);
        const answeredChoice = isAnswered ? extractClarificationAnswer(answeringUserMsg) : null;
        const isQuestionPending = isAnswered
          ? false
          : messages && typeof messageIndex === "number"
          ? true
          : Boolean(isSessionBlocked);

        return (
          <ClarificationPromptCard
            key={idx}
            question={question}
            options={options}
            index={idx + 1}
            total={clarificationCalls.length}
            isPending={isQuestionPending}
            selectedAnswer={answeredChoice}
            onSelectOption={onClarificationSelect}
            disabled={isLatestStreaming}
          />
        );
      })}

      {/* Action buttons bar underneath response (Copy, ThumbsUp, ThumbsDown) + model badge */}
      {message.content && (
        <div className="flex items-center justify-between pt-2">
          <div className="flex items-center gap-1 text-zinc-500">
            <button
              onClick={handleCopy}
              title="Copy response"
              className="rounded-md p-1.5 hover:bg-zinc-800/70 hover:text-zinc-300 transition-colors"
            >
              {copied ? (
                <Check className="h-3.5 w-3.5 text-emerald-400" />
              ) : (
                <Copy className="h-3.5 w-3.5" />
              )}
            </button>
            <button
              onClick={() => setReaction(reaction === "up" ? null : "up")}
              title="Helpful"
              className={`rounded-md p-1.5 hover:bg-zinc-800/70 transition-colors ${
                reaction === "up" ? "text-amber-400" : "hover:text-zinc-300"
              }`}
            >
              <ThumbsUp className="h-3.5 w-3.5" />
            </button>
            <button
              onClick={() => setReaction(reaction === "down" ? null : "down")}
              title="Not helpful"
              className={`rounded-md p-1.5 hover:bg-zinc-800/70 transition-colors ${
                reaction === "down" ? "text-rose-400" : "hover:text-zinc-300"
              }`}
            >
              <ThumbsDown className="h-3.5 w-3.5" />
            </button>
          </div>

          {/* Model badge — shows which model/provider handled this response */}
          {message.model && (
            <div className="flex items-center gap-1.5 text-[10px] font-mono text-zinc-600 select-none">
              <span className="h-1.5 w-1.5 rounded-full bg-amber-500/60 shrink-0" />
              <span className="truncate max-w-[160px]" title={`${message.provider ?? ""}/${message.model}`}>
                {message.model}
              </span>
            </div>
          )}
        </div>
      )}

      {/* Inline Audit Scan Report Card */}
      {message.auditScan && (
        <div className="mt-2.5 w-full">
          <AuditReportCard
            scan={message.auditScan}
            onStageFix={onStageAuditFix}
            onViewDiff={onViewDiff}
          />
        </div>
      )}

    </div>
  );
}

// ---------------------------------------------------------------------------
// ANSI → styled spans (no new deps). Preserves raw GitHub Actions lines while
// rendering SGR color codes instead of leaking raw escape sequences.
// ---------------------------------------------------------------------------

const ANSI_FG_COLORS: Record<number, string> = {
  30: "#71717a",
  31: "#f87171",
  32: "#34d399",
  33: "#fbbf24",
  34: "#60a5fa",
  35: "#c084fc",
  36: "#22d3ee",
  37: "#e4e4e7",
  90: "#71717a",
  91: "#fca5a5",
  92: "#6ee7b7",
  93: "#fcd34d",
  94: "#93c5fd",
  95: "#d8b4fe",
  96: "#67e8f9",
  97: "#fafafa",
};

interface AnsiSegment {
  text: string;
  color?: string;
  bold?: boolean;
  dim?: boolean;
}

function parseAnsiSegments(input: string): AnsiSegment[] {
  // Normalize progress-bar carriage returns and strip non-SGR escape sequences
  // (cursor moves, clear-line) so CI logs never break layout.
  const withoutOsc = input.replace(/\][^\u0007]*\u0007/g, "");
  const withoutCsi = withoutOsc.replace(/\[(?!([0-9;]*)m)[0-9;?]*[A-Za-z]/g, "");
  const normalized = withoutCsi.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  const sgrRe = /\[([0-9;]*)m/g;

  const segments: AnsiSegment[] = [];
  let lastIdx = 0;
  let color: string | undefined;
  let bold = false;
  let dim = false;

  const pushText = (text: string) => {
    if (!text) return;
    segments.push({ text, color, bold, dim });
  };

  let match: RegExpExecArray | null;
  while ((match = sgrRe.exec(normalized)) !== null) {
    pushText(normalized.slice(lastIdx, match.index));
    lastIdx = match.index + match[0].length;
    const codes = match[1] === "" ? [0] : match[1].split(";").map((n) => Number(n));
    for (const code of codes) {
      if (code === 0) {
        color = undefined;
        bold = false;
        dim = false;
      } else if (code === 1) {
        bold = true;
      } else if (code === 2) {
        dim = true;
      } else if (code === 22) {
        bold = false;
        dim = false;
      } else if (code === 39) {
        color = undefined;
      } else if (ANSI_FG_COLORS[code]) {
        color = ANSI_FG_COLORS[code];
      }
      // Background SGR codes (40-47, 100-107) are intentionally ignored —
      // translucent dark surfaces already provide contrast.
    }
  }
  pushText(normalized.slice(lastIdx));
  return segments;
}

function AnsiText({ text, idPrefix }: { text: string; idPrefix: string }) {
  const segments = parseAnsiSegments(text);
  return (
    <>
      {segments.map((seg, i) => {
        if (!seg.color && !seg.bold && !seg.dim) {
          return <span key={`${idPrefix}-${i}`}>{seg.text}</span>;
        }
        return (
          <span
            key={`${idPrefix}-${i}`}
            style={{
              ...(seg.color ? { color: seg.color } : {}),
              ...(seg.bold ? { fontWeight: 700 } : {}),
              ...(seg.dim ? { opacity: 0.65 } : {}),
            }}
          >
            {seg.text}
          </span>
        );
      })}
    </>
  );
}

// ---------------------------------------------------------------------------
// Terminal Drawer — collapsible live terminal output panel
// Raw GitHub Actions lines arrive via the same terminal_output SSE event as
// local sandbox output and merge into this single buffer with ANSI colors
// preserved. flex-none + capped height keeps split-view layout stable.
// ---------------------------------------------------------------------------

function TerminalDrawer({ logs, ciActive }: { logs: string[]; ciActive?: boolean }) {
  const [open, setOpen] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to bottom when new chunks arrive.
  // Honors prefers-reduced-motion: instant jump when the user prefers
  // reduced motion, smooth scroll otherwise.
  useEffect(() => {
    if (open) {
      const reduceMotion =
        typeof window !== "undefined" &&
        typeof window.matchMedia === "function" &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      bottomRef.current?.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth" });
    }
  }, [logs, open]);

  if (logs.length === 0) return null;

  // Combine all chunks into a single string for display.
  const fullOutput = logs.join("");

  return (
    <div className="flex-none border-t border-zinc-800/60 bg-[#0a0a0d]">
      {/* Header / toggle bar */}
      <button
        onClick={() => setOpen((p) => !p)}
        className="flex w-full items-center justify-between px-4 py-2 text-xs font-mono text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900/40 transition-colors select-none"
        id="terminal-drawer-toggle"
        aria-expanded={open}
      >
        <div className="flex items-center gap-2 min-w-0">
          <Terminal className="h-3.5 w-3.5 shrink-0 text-emerald-400/80" />
          <span className="text-zinc-300 font-medium">Terminal</span>
          <span className="rounded-full bg-emerald-500/10 border border-emerald-500/20 px-1.5 py-px text-[10px] font-mono text-emerald-400">
            {logs.length} chunk{logs.length !== 1 ? "s" : ""}
          </span>
          {ciActive && (
            <span className="inline-flex items-center gap-1 rounded-full border border-amber-500/30 bg-amber-500/10 px-1.5 py-px text-[10px] font-mono text-amber-300">
              <span className="relative flex h-1.5 w-1.5">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-60" />
                <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-amber-400" />
              </span>
              CI live
            </span>
          )}
        </div>
        <ChevronUp
          className={`h-3.5 w-3.5 shrink-0 text-zinc-500 transition-transform duration-200 ${
            open ? "" : "rotate-180"
          }`}
        />
      </button>

      {/* Output pane — capped height + overflow so CI bursts never shift layout */}
      {open && (
        <div className="max-h-52 overflow-y-auto px-4 py-3 bg-[#060608]">
          <pre
            id="terminal-output-content"
            className="text-[11px] font-mono text-emerald-200/80 leading-relaxed whitespace-pre-wrap break-words"
          >
            <AnsiText text={fullOutput} idPrefix="term" />
          </pre>
          <div ref={bottomRef} />
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Live CI Status Chip — queued → running → passed/failed with external link
// ---------------------------------------------------------------------------

type CiPhase = "queued" | "running" | "passed" | "failed";

function isExternalRunUrl(url: string | null | undefined): url is string {
  if (!url) return false;
  if (url === "pending" || url === "unknown") return false;
  return /^https?:\/\//.test(url);
}

function extractCiRunNumber(runUrl: string | null | undefined): string | null {
  if (!runUrl) return null;
  const runsMatch = runUrl.match(/\/runs\/(\d+)/);
  if (runsMatch) return runsMatch[1];
  const trailing = runUrl.match(/(\d{4,})(?:\/)?$/);
  return trailing ? trailing[1] : null;
}

function CiStatusChip({
  phase,
  runUrl,
  workflowName,
  stepName,
}: {
  phase: CiPhase;
  runUrl?: string | null;
  workflowName?: string | null;
  stepName?: string | null;
}) {
  const linkable = isExternalRunUrl(runUrl);
  const runNumber = extractCiRunNumber(runUrl ?? null);

  const base =
    "inline-flex shrink-0 items-center gap-1.5 rounded-full border-t border-x border-b px-2.5 py-0.5 text-[11px] font-mono font-medium transition-colors duration-200";

  if (phase === "passed") {
    return (
      <span
        data-testid="ci-status-chip"
        data-phase="passed"
        role="status"
        aria-live="polite"
        aria-label={`CI passed${runNumber ? `, run #${runNumber}` : ""}`}
        className={`${base} border-t-emerald-400/40 border-x-emerald-500/30 border-b-emerald-600/20 bg-gradient-to-b from-emerald-500/15 to-emerald-500/5 text-emerald-300 shadow-[inset_0_1px_0_rgba(255,255,255,0.1),0_1px_3px_rgba(0,0,0,0.3)]`}
      >
        <CheckCircle2 className="h-3 w-3" />
        <span>CI passed{runNumber ? ` · #${runNumber}` : ""}</span>
        {linkable && (
          <a
            href={runUrl as string}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(e) => e.stopPropagation()}
            title="Open GitHub Actions run"
            aria-label="Open GitHub Actions run"
            className="inline-flex min-h-[24px] min-w-[24px] items-center justify-center rounded p-1 text-emerald-400 hover:text-emerald-200"
          >
            <ExternalLink className="h-3 w-3" />
          </a>
        )}
      </span>
    );
  }

  if (phase === "failed") {
    return (
      <span
        data-testid="ci-status-chip"
        data-phase="failed"
        role="status"
        aria-live="polite"
        aria-label={`CI failed${runNumber ? `, run #${runNumber}` : ""}`}
        className={`${base} border-t-red-400/40 border-x-red-500/30 border-b-red-600/20 bg-gradient-to-b from-red-500/15 to-red-500/5 text-red-300 shadow-[inset_0_1px_0_rgba(255,255,255,0.1),0_1px_3px_rgba(0,0,0,0.3)]`}
      >
        <XCircle className="h-3 w-3" />
        <span>CI failed{runNumber ? ` · #${runNumber}` : ""}</span>
        {linkable && (
          <a
            href={runUrl as string}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(e) => e.stopPropagation()}
            title="Open GitHub Actions run"
            aria-label="Open GitHub Actions run"
            className="inline-flex min-h-[24px] min-w-[24px] items-center justify-center rounded p-1 text-red-400 hover:text-red-200"
          >
            <ExternalLink className="h-3 w-3" />
          </a>
        )}
      </span>
    );
  }

  if (phase === "running") {
    const runningLabel = `CI running${stepName ? `, ${stepName}` : workflowName ? `, ${workflowName}` : ""}`;
    return (
      <span
        data-testid="ci-status-chip"
        data-phase="running"
        role="status"
        aria-live="polite"
        aria-label={runningLabel}
        className={`${base} border-t-sky-400/40 border-x-sky-500/30 border-b-sky-600/20 bg-gradient-to-b from-sky-500/15 to-sky-500/5 text-sky-300 shadow-[inset_0_1px_0_rgba(255,255,255,0.1),0_1px_3px_rgba(0,0,0,0.3)]`}
      >
        <span className="relative flex h-1.5 w-1.5">
          <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-sky-400 opacity-60" />
          <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-sky-400 shadow-[0_0_6px_rgba(56,189,248,0.8)]" />
        </span>
        <span className="truncate max-w-[220px]">
          CI running{stepName ? ` · ${stepName}` : workflowName ? ` · ${workflowName}` : ""}
        </span>
        {linkable && (
          <a
            href={runUrl as string}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(e) => e.stopPropagation()}
            title="Open GitHub Actions run"
            aria-label="Open GitHub Actions run"
            className="inline-flex min-h-[24px] min-w-[24px] shrink-0 items-center justify-center rounded p-1 text-sky-400 hover:text-sky-200"
          >
            <ExternalLink className="h-3 w-3" />
          </a>
        )}
      </span>
    );
  }

  return (
    <span
      data-testid="ci-status-chip"
      data-phase="queued"
      role="status"
      aria-live="polite"
      aria-label={`CI queued${workflowName ? `, ${workflowName}` : ""}`}
      className={`${base} border-t-amber-400/50 border-x-amber-500/35 border-b-amber-600/25 bg-gradient-to-b from-amber-500/20 to-amber-500/5 text-amber-300 shadow-[inset_0_1px_0_rgba(255,255,255,0.12),0_1px_3px_rgba(0,0,0,0.3)]`}
    >
      <span className="relative flex h-1.5 w-1.5">
        <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-60" />
        <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-amber-400 shadow-[0_0_6px_rgba(245,158,11,0.8)]" />
      </span>
      <span className="truncate max-w-[220px]">CI queued{workflowName ? ` · ${workflowName}` : ""}</span>
      {linkable && (
        <a
          href={runUrl as string}
          target="_blank"
          rel="noopener noreferrer"
          onClick={(e) => e.stopPropagation()}
          title="Open GitHub Actions run"
          aria-label="Open GitHub Actions run"
          className="inline-flex min-h-[24px] min-w-[24px] shrink-0 items-center justify-center rounded p-1 text-amber-400 hover:text-amber-200"
        >
          <ExternalLink className="h-3 w-3" />
        </a>
      )}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Agent Action Badge — CI sandbox verdict rendered inline in the chat stream
// ---------------------------------------------------------------------------

function CiVerificationBadge({
  passed,
  runUrl,
  durationSeconds,
  workflowName,
}: {
  passed: boolean;
  runUrl?: string | null;
  durationSeconds?: number | null;
  workflowName?: string | null;
}) {
  const runNumber = extractCiRunNumber(runUrl ?? null);
  const linkable = isExternalRunUrl(runUrl);
  const durationLabel =
    typeof durationSeconds === "number" && Number.isFinite(durationSeconds)
      ? ` in ${Math.max(0, Math.round(durationSeconds))}s`
      : "";
  const runLabel = runNumber ? `Run #${runNumber}` : workflowName ? `${workflowName}` : "CI run";

  return (
    <div
      data-testid="ci-verification-badge"
      role="status"
      aria-live="polite"
      className={`my-2 flex items-center gap-2.5 rounded-xl border px-3.5 py-2.5 transition-colors duration-200 ${
        passed
          ? "border-emerald-500/25 bg-emerald-500/[0.07]"
          : "border-red-500/25 bg-red-500/[0.07]"
      }`}
    >
      <span
        className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border ${
          passed
            ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-300"
            : "border-red-500/30 bg-red-500/10 text-red-300"
        }`}
      >
        {passed ? <ShieldCheck className="h-4 w-4" /> : <TestTube2 className="h-4 w-4" />}
      </span>
      <p className="min-w-0 flex-1 text-[12.5px] leading-snug text-zinc-200">
        <span className="font-medium text-zinc-100">Agent verified changes</span>
        <span className="text-zinc-400"> in GitHub Actions CI Sandbox</span>
        <span className={`font-mono ${passed ? "text-emerald-300" : "text-red-300"}`}>
          {" "}
          ({runLabel} — {passed ? "Passed" : "Failed"}
          {durationLabel})
        </span>
      </p>
      {linkable && (
        <a
          href={runUrl as string}
          target="_blank"
          rel="noopener noreferrer"
          className={`flex shrink-0 items-center gap-1 rounded-lg border px-2 py-1 text-[11px] font-mono transition-colors ${
            passed
              ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-300 hover:bg-emerald-500/20"
              : "border-red-500/30 bg-red-500/10 text-red-300 hover:bg-red-500/20"
          }`}
        >
          <span>View run</span>
          <ExternalLink className="h-3 w-3" />
        </a>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Commit PR Modal
// ---------------------------------------------------------------------------

interface CommitModalProps {
  sessionId: string;
  repoOwner?: string;
  repoName?: string;
  branchName?: string;
  patchCount?: number;
  onClose: () => void;
  onSuccess: (prUrl: string, prNumber: number) => void;
}

function CommitModal({
  sessionId,
  repoOwner,
  repoName,
  branchName,
  patchCount = 0,
  onClose,
  onSuccess,
}: CommitModalProps) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
      } else if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
        e.preventDefault();
        const form = document.getElementById("commit-modal-form") as HTMLFormElement | null;
        if (form) form.requestSubmit();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

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
      className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-md transition-all animate-in fade-in duration-200"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="relative w-full max-w-lg overflow-hidden rounded-2xl border-t border-t-zinc-650/60 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-[#121216] shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_24px_64px_rgba(0,0,0,0.85)] animate-in fade-in zoom-in-95 duration-150 ease-out">
        {/* Modal Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-zinc-800/80 bg-[#15151c]/70">
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-xl border-t border-t-violet-400/40 border-x border-x-violet-500/30 border-b border-b-violet-800/40 bg-gradient-to-b from-violet-500/20 to-violet-600/5 text-violet-300 shadow-[inset_0_1px_0_rgba(255,255,255,0.1)]">
              <GitPullRequest className="h-4.5 w-4.5" />
            </div>
            <div>
              <h2 className="text-sm font-semibold text-zinc-100 tracking-tight">Commit & Open PR</h2>
              <p className="text-xs text-zinc-400 font-normal mt-0.5">Publish verified staged patches to GitHub</p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="flex h-7 w-7 items-center justify-center rounded-lg border border-transparent hover:border-zinc-750 hover:bg-zinc-800 text-zinc-400 hover:text-zinc-200 transition-all active:translate-y-[0.5px]"
            title="Close (Esc)"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Target Context Strip */}
        {(branchName || repoName || patchCount > 0) && (
          <div className="flex items-center justify-between px-6 py-2.5 bg-[#0d0d12] border-b border-zinc-800/60 text-xs font-mono">
            <div className="flex items-center gap-2 text-zinc-400">
              <GitBranch className="h-3.5 w-3.5 text-violet-400/80" />
              <span className="text-zinc-200 font-medium">{branchName || "main"}</span>
              {repoOwner && repoName && (
                <span className="text-zinc-500 text-[11px]">({repoOwner}/{repoName})</span>
              )}
            </div>
            {patchCount > 0 && (
              <span className="inline-flex items-center gap-1 rounded-md bg-amber-500/10 border border-amber-500/25 px-2 py-0.5 text-[11px] font-mono text-amber-300">
                <FileCode2 className="h-3 w-3" />
                {patchCount} file{patchCount !== 1 ? "s" : ""} staged
              </span>
            )}
          </div>
        )}

        {/* Form Content */}
        <form id="commit-modal-form" onSubmit={handleSubmit} className="p-6 space-y-4">
          {/* PR Title Field */}
          <div className="space-y-1.5">
            <div className="flex items-center justify-between">
              <label htmlFor="commit-title-input" className="text-xs font-medium text-zinc-200">
                PR Title <span className="text-amber-400 font-bold">*</span>
              </label>
              <span className="text-[11px] font-mono text-zinc-500">{title.length}/255</span>
            </div>
            <input
              id="commit-title-input"
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="e.g. fix(auth): resolve null check in token middleware"
              maxLength={255}
              className="w-full rounded-xl border border-zinc-700/80 bg-[#16161d] px-3.5 py-2.5 text-xs text-zinc-100 placeholder:text-zinc-500 focus:outline-none focus:border-violet-500/80 focus:ring-1 focus:ring-violet-500/30 focus:bg-[#181822] shadow-[inset_0_1px_2px_rgba(0,0,0,0.4)] transition-all font-mono"
              autoFocus
            />
          </div>

          {/* Description Field */}
          <div className="space-y-1.5">
            <div className="flex items-center justify-between">
              <label htmlFor="commit-body-input" className="text-xs font-medium text-zinc-200">
                Description <span className="text-zinc-500 font-normal">(optional)</span>
              </label>
              <span className="text-[11px] font-mono text-zinc-500">
                {body.length > 0 ? `${body.length} chars` : "Markdown supported"}
              </span>
            </div>
            <textarea
              id="commit-body-input"
              value={body}
              onChange={(e) => setBody(e.target.value)}
              placeholder="Summary of changes and rationale…"
              rows={4}
              maxLength={65535}
              className="w-full resize-y rounded-xl border border-zinc-700/80 bg-[#16161d] px-3.5 py-2.5 text-xs text-zinc-100 placeholder:text-zinc-500 focus:outline-none focus:border-violet-500/80 focus:ring-1 focus:ring-violet-500/30 focus:bg-[#181822] shadow-[inset_0_1px_2px_rgba(0,0,0,0.4)] transition-all font-mono leading-relaxed"
            />
          </div>

          {/* Error display */}
          {error && (
            <div className="flex items-center gap-2 rounded-xl border border-red-500/40 bg-red-500/10 px-3.5 py-2.5 text-xs font-mono text-red-300 animate-in fade-in duration-150">
              <AlertTriangle className="h-4 w-4 shrink-0 text-red-400" />
              <span>{error}</span>
            </div>
          )}

          {/* Footer Toolbar */}
          <div className="flex items-center justify-between pt-3 border-t border-zinc-800/80 -mx-6 -mb-6 px-6 py-3.5 bg-[#0e0e13]/90 rounded-b-2xl mt-4">
            <div className="flex items-center gap-1.5 text-[11px] font-mono text-zinc-400">
              <kbd className="rounded border border-zinc-700/70 bg-zinc-800/90 px-1.5 py-0.5 text-[10px] text-zinc-300 font-sans shadow-sm">
                ⌘ / Ctrl + ↵
              </kbd>
              <span>to submit</span>
            </div>
            <div className="flex items-center gap-2.5">
              <button
                type="button"
                onClick={onClose}
                className="rounded-xl border-t border-t-zinc-700/60 border-x border-x-zinc-800/60 border-b border-b-zinc-950 bg-gradient-to-b from-zinc-800/90 to-zinc-850 px-3.5 py-2 text-xs font-medium text-zinc-300 hover:text-white hover:border-t-zinc-600 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_1px_2px_rgba(0,0,0,0.3)] active:translate-y-[0.5px] transition-all"
              >
                Cancel
              </button>
              <button
                id="commit-submit-btn"
                type="submit"
                disabled={loading}
                className="flex items-center justify-center gap-2 rounded-xl border-t border-t-violet-400/60 border-x border-x-violet-600/60 border-b border-b-violet-950 bg-gradient-to-b from-violet-600 via-violet-650 to-violet-700 px-4 py-2 text-xs font-semibold text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.25),0_2px_8px_rgba(124,58,237,0.35)] hover:brightness-105 active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.35)] transition-all disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <GitPullRequest className="h-3.5 w-3.5" />}
                <span>{loading ? "Publishing…" : "Commit & Open PR"}</span>
              </button>
            </div>
          </div>
        </form>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Client Component
// ---------------------------------------------------------------------------

export default function SessionWorkspaceClient({ sessionId: propSessionId }: { sessionId?: string } = {}) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const sessionId = propSessionId || searchParams.get("id") || "";

  const [session, setSession] = useState<SessionOut | null>(null);
  const [pageLoading, setPageLoading] = useState(true);
  const [pageError, setPageError] = useState<string | null>(null);

  // View mode switcher: "chat" (default - Screenshot 2 style), "diffs", "split", "preview"
  const [viewMode, setViewMode] = useState<"chat" | "diffs" | "split" | "preview">("chat");

  // Split right-panel tab: "diffs" (Monaco) vs "preview" (live iframe). Pure UI state.
  const [splitRightPanel, setSplitRightPanel] = useState<"diffs" | "preview">("diffs");

  // Selected file tab in Monaco
  const [activeFile, setActiveFile] = useState<string | null>(null);

  // Modals & Action States
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

  // Active Model Selector State
  const [modelPickerOpen, setModelPickerOpen] = useState(false);
  const [modelSearch, setModelSearch] = useState("");
  const [customModelInput, setCustomModelInput] = useState("");
  const [customProviderInput, setCustomProviderInput] = useState<"auto" | "groq" | "opencode_zen">("auto");
  const [availableModels, setAvailableModels] = useState<{
    opencode_zen: AvailableModelItem[];
    groq: AvailableModelItem[];
    openai: AvailableModelItem[];
    anthropic: AvailableModelItem[];
  }>({
    opencode_zen: FALLBACK_MODELS.opencode_zen,
    groq: FALLBACK_MODELS.groq,
    openai: FALLBACK_MODELS.openai,
    anthropic: FALLBACK_MODELS.anthropic,
  });
  // Selected model — id (e.g. "nemotron-3.5-lightning-free") and inferred provider
  const [selectedModelId, setSelectedModelId] = useState<string>(() => {
    if (typeof window !== "undefined") {
      return localStorage.getItem("haunter_session_model") ?? "nemotron-3.5-lightning-free";
    }
    return "nemotron-3.5-lightning-free";
  });
  const [selectedProvider, setSelectedProvider] = useState<string>(() => {
    if (typeof window !== "undefined") {
      return localStorage.getItem("haunter_session_provider") ?? "opencode_zen";
    }
    return "opencode_zen";
  });
  const [actionMenuOpen, setActionMenuOpen] = useState(false);

  const allKnownModels = useMemo(() => {
    return [
      ...availableModels.opencode_zen,
      ...availableModels.groq,
      ...availableModels.openai,
      ...availableModels.anthropic,
    ];
  }, [availableModels]);

  // Chat & Stream (Phase 4.2: live CI sandbox events flow through the same SSE stream)
  const {
    messages,
    stagedPatches,
    sandboxStatus: liveSandboxStatus,
    sandboxQueued,
    sandboxProgress,
    sandboxResult: liveSandboxResult,
    ciStartedAt,
    ciFinishedAt,
    isStreaming,
    terminalLogs,
    plan,
    pendingClarification,
    activeAudit,
    sendChatMessage,
    stopStreaming,
    setStagedPatches,
    setMessages,
    setTerminalLogs,
    setPlan,
    setPendingClarification,
    setCheckpoints,
  } = useSessionStream(sessionId);

  // Live CI derivation — SSE (agent-driven verify_in_ci_sandbox) takes
  // precedence; the manual top-bar REST verify is the fallback source.
  // Terminal verdicts win; any live step activity promotes queued → running.
  const liveCiPhase: CiPhase | null =
    liveSandboxStatus?.status === "passed"
      ? "passed"
      : liveSandboxStatus?.status === "failed"
        ? "failed"
        : sandboxProgress || liveSandboxStatus?.status === "running"
          ? "running"
          : liveSandboxStatus?.status === "queued" || sandboxQueued
            ? "queued"
            : null;
  const ciRunUrl = liveSandboxStatus?.run_url ?? liveSandboxResult?.run_url ?? sandboxQueued?.run_url ?? null;
  const ciWorkflowName = sandboxQueued?.workflow_name ?? null;
  const ciStepName = sandboxProgress?.step_name ?? null;
  const ciActive = liveCiPhase === "queued" || liveCiPhase === "running" || sandboxLoading;
  const ciDurationSeconds =
    ciStartedAt != null && ciFinishedAt != null
      ? Math.max(0, Math.round((ciFinishedAt - ciStartedAt) / 1000))
      : liveSandboxResult?.duration_s ?? null;

  // WebContainer live preview (Phase 2.2) — boots a WASM Node runtime in-tab.
  const {
    status: wcStatus,
    previewUrl,
    terminalOutput: wcTerminalOutput,
    boot: bootWebContainer,
    writeFile: wcWriteFile,
    writeEnvFile,
    restartDevServer,
    teardown: wcTeardown,
    error: wcError,
  } = useWebContainer();

  // In-container env vars (never persisted).
  const [previewEnvVars, setPreviewEnvVars] = useState<Record<string, string>>({});
  // Tracks how many WebContainer chunks have already been merged into
  // terminalLogs so each chunk is appended exactly once.
  const wcMergedCountRef = useRef(0);

  // Merge WebContainer terminal output into the existing terminalLogs state
  // so it flows into the existing <TerminalDrawer logs={terminalLogs} />.
  // No duplicate TerminalDrawer — one terminal, all output sources.
  useEffect(() => {
    if (wcTerminalOutput.length > wcMergedCountRef.current) {
      const fresh = wcTerminalOutput.slice(wcMergedCountRef.current);
      wcMergedCountRef.current = wcTerminalOutput.length;
      setTerminalLogs((prev) => [...prev, ...fresh]);
    }
  }, [wcTerminalOutput, setTerminalLogs]);

  // Teardown the container when the workspace unmounts.
  useEffect(() => () => wcTeardown(), [wcTeardown]);

  // Sync agent-staged patches into the WebContainer filesystem on every change.
  // Only fires when the preview panel is active AND the container is ready.
  useEffect(() => {
    if (wcStatus !== "ready") return;
    if (viewMode !== "preview" && viewMode !== "split") return;

    const entries = Object.entries(stagedPatches);
    if (entries.length === 0) return;

    // Write the latest version of each staged file to the container.
    // parseDiffForMonaco is already available — use its `modified` output as
    // the complete post-patch file content to write.
    entries.forEach(([filePath, diff]) => {
      const { modified } = parseDiffForMonaco(diff);
      if (modified) {
        wcWriteFile(filePath, modified).catch((err) => {
          console.error("[WebContainer] writeFile failed:", filePath, err);
        });
      }
    });
  }, [stagedPatches, wcStatus, viewMode, wcWriteFile]);

  // Build a v1 synthetic file tree from staged patches + minimal scaffold.
  // Full repo clone via GitHub API file tree fetch is a v2 enhancement.
  const handleBootPreview = useCallback(async () => {
    const fileTree = buildPreviewFileTree(stagedPatches);
    await bootWebContainer(fileTree);
  }, [stagedPatches, bootWebContainer]);

  // Iframe remount is owned by WebPreviewPanel's inner refreshNonce —
  // no outer key here so refresh preserves panel state (viewport, env draft).
  const handleRefreshPreview = useCallback(() => {}, []);

  const handleSavePreviewEnv = useCallback(
    async (vars: Record<string, string>) => {
      setPreviewEnvVars(vars);
      await writeEnvFile(vars);
    },
    [writeEnvFile]
  );

  const [chatInput, setChatInput] = useState("");
  const [showPlanSidebar, setShowPlanSidebar] = useState(true);
  const [securityViolations, setSecurityViolations] = useState<string | null>(null);
  const chatBottomRef = useRef<HTMLDivElement>(null);
  const chatContainerRef = useRef<HTMLDivElement>(null);
  const initialScrolledRef = useRef(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Wire security violations detection from messages (tool calls & audit scans) — scan newest first
  useEffect(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      const msg = messages[i];
      if (msg.toolCalls && msg.toolCalls.length > 0) {
        for (let j = msg.toolCalls.length - 1; j >= 0; j--) {
          const tc = msg.toolCalls[j];
          if (tc.name === "scan_security_vulnerabilities") {
            const scanResult =
              (tc.args?.scan_result as string) ||
              (tc.args?.result as string) ||
              (tc.args?.output as string) ||
              (tc.args?.summary as string) ||
              "";
            if (scanResult && scanResult.trim().length > 0) {
              const { hasViolations, isClean } = parseSecurityScanResult(scanResult);
              if (hasViolations) {
                setSecurityViolations(
                  scanResult || "Security violations detected in repository scan."
                );
                return;
              } else if (isClean) {
                setSecurityViolations(null);
                return;
              }
            }
          }
        }
      }
      if (msg.auditScan?.report?.findings) {
        const blockers = msg.auditScan.report.findings.filter(
          (f) =>
            f.severity?.toUpperCase() === "BLOCKER" ||
            f.severity?.toUpperCase() === "CRITICAL"
        );
        if (blockers.length > 0) {
          setSecurityViolations(
            `${blockers.length} critical security blocker(s) detected.`
          );
          return;
        } else {
          setSecurityViolations(null);
          return;
        }
      }
    }
    setSecurityViolations(null);
  }, [messages]);

  // -------------------------------------------------------------------------
  // Clarification selection handler
  // -------------------------------------------------------------------------

  /**
   * Handle user selection of a clarification option or custom submission.
   * Unblocks the agent session, records the answer, and triggers the next agent turn.
   * Restores pending clarification and rolls back optimistic state on error.
   */
  const handleClarificationSelect = useCallback(
    async (choice: string) => {
      const prevPending = pendingClarification;
      setPendingClarification(null);

      // Optimistically append user's response in chat so UI gives immediate feedback
      const optimisticMsg = {
        role: "user" as const,
        content: `[User Clarification Response]: ${choice}`,
      };
      setMessages((prev) => [...prev, optimisticMsg]);

      try {
        await api.clarifySession(sessionId, { response: choice });
        if (session) {
          setSession({ ...session, status: "active", waiting_input: null });
        }
        // Remove optimistic clarification message right before sendChatMessage to avoid
        // duplicate responses in the transcript (sendChatMessage appends its own turn).
        setMessages((prev) => prev.filter((m) => m !== optimisticMsg));

        // Trigger the next agent chat turn automatically
        await sendChatMessage(`Proceed with: ${choice}`, {
          model: selectedModelId,
          provider: selectedProvider === "auto" ? undefined : selectedProvider,
        });
      } catch (err) {
        // Roll back optimistic state on error and rethrow so caller can reset button states
        setPendingClarification(prevPending);
        setMessages((prev) => prev.filter((m) => m !== optimisticMsg));
        const msg = err instanceof ApiError ? err.message : "Failed to submit clarification.";
        setActionError(msg);
        throw err;
      }
    },
    [sessionId, session, selectedModelId, selectedProvider, sendChatMessage, setMessages, pendingClarification, setPendingClarification, setActionError]
  );

  // -------------------------------------------------------------------------
  // Load session on mount
  // -------------------------------------------------------------------------

  useEffect(() => {
    initialScrolledRef.current = false;
    if (!sessionId) {
      router.replace("/sessions");
      return;
    }

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
        // Pre-populate plan and waiting_input if present.
        if (s.plan && s.plan.length > 0) {
          setPlan(s.plan);
        }
        if (s.status === "awaiting_clarification" && s.waiting_input) {
          setPendingClarification(s.waiting_input);
        }
        // Hydrate chat history from DB — preserve tool calls so transcript states persist.
        if (s.conversation_history && s.conversation_history.length > 0) {
          type RawHistoryMessage = {
            role?: string;
            content?: unknown;
            tool_calls?: Array<{
              name?: string;
              args?: Record<string, unknown>;
              function?: {
                name?: string;
                arguments?: string | Record<string, unknown>;
              };
            }>;
            thoughts?: string[];
          };
          const rawHistory = s.conversation_history as unknown as RawHistoryMessage[];
          const hydrated: import("@/hooks/useSessionStream").ChatMessage[] = rawHistory
            .filter((m) => m.role === "user" || m.role === "assistant")
            .map((m) => {
              const rawToolCalls = m.tool_calls;
              let toolCalls: ToolCallChip[] | undefined;
              if (Array.isArray(rawToolCalls)) {
                toolCalls = rawToolCalls.map((tc) => {
                  if (tc.name) return { name: tc.name, args: tc.args };
                  if (tc.function) {
                    let parsedArgs: Record<string, unknown> | undefined;
                    try {
                      parsedArgs =
                        typeof tc.function.arguments === "string"
                          ? (JSON.parse(tc.function.arguments) as Record<string, unknown>)
                          : tc.function.arguments;
                    } catch {
                      parsedArgs = undefined;
                    }
                    return { name: tc.function.name ?? "unknown", args: parsedArgs };
                  }
                  return { name: "unknown", args: undefined };
                });
              }
              return {
                role: m.role as "user" | "assistant",
                content: typeof m.content === "string" ? m.content : "",
                toolCalls,
                thoughts: Array.isArray(m.thoughts) ? m.thoughts : undefined,
              };
            });
          if (hydrated.length > 0) setMessages(hydrated);
        }
        // Seed checkpoints from DB.
        if (s.checkpoints && s.checkpoints.length > 0) {
          setCheckpoints(s.checkpoints as CheckpointOut[]);
        }
      })
      .catch((err) => {
        const msg = err instanceof ApiError ? err.message : "Failed to load session.";
        setPageError(msg);
      })
      .finally(() => setPageLoading(false));
  }, [sessionId, setStagedPatches, setMessages, setPlan, setPendingClarification, setCheckpoints, router]);

  // Auto-scroll chat to bottom on new messages.
  // Performs instant jump on initial page load / session hydration so user immediately
  // lands on the latest chat without an animated scroll down from top.
  useEffect(() => {
    if (messages.length === 0) return;
    if (!initialScrolledRef.current) {
      if (chatContainerRef.current) {
        chatContainerRef.current.scrollTop = chatContainerRef.current.scrollHeight;
      }
      chatBottomRef.current?.scrollIntoView({ behavior: "auto" });
      initialScrolledRef.current = true;
      requestAnimationFrame(() => {
        if (chatContainerRef.current) {
          chatContainerRef.current.scrollTop = chatContainerRef.current.scrollHeight;
        }
        chatBottomRef.current?.scrollIntoView({ behavior: "auto" });
      });
    } else {
      chatBottomRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages, isStreaming]);

  // When new patches arrive from SSE, select the first one if none selected
  useEffect(() => {
    const files = Object.keys(stagedPatches);
    if (files.length > 0 && !activeFile) {
      setActiveFile(files[0]);
    }
  }, [stagedPatches, activeFile]);

  // Fetch available models from API on mount — merge over offline fallbacks.
  useEffect(() => {
    api
      .getAvailableModels()
      .then((data) => {
        setAvailableModels({
          opencode_zen: data.opencode_zen.length ? data.opencode_zen : FALLBACK_MODELS.opencode_zen,
          groq: (data.groq ?? []).length ? (data.groq ?? []) : FALLBACK_MODELS.groq,
          openai: data.openai.length ? data.openai : FALLBACK_MODELS.openai,
          anthropic: data.anthropic.length ? data.anthropic : FALLBACK_MODELS.anthropic,
        });
      })
      .catch(() => {
        // Network unavailable — retain offline fallback presets; no error surfaced to user.
      });
  }, []);

  // Persist model + provider selection to localStorage whenever they change.
  useEffect(() => {
    if (typeof window !== "undefined") {
      localStorage.setItem("haunter_session_model", selectedModelId);
      localStorage.setItem("haunter_session_provider", selectedProvider);
    }
  }, [selectedModelId, selectedProvider]);

  // Auto-resize textarea
  const handleInputChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setChatInput(e.target.value);
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
      textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, 180)}px`;
    }
  };

  // -------------------------------------------------------------------------
  // Actions
  // -------------------------------------------------------------------------

  /**
   * Handle user submission of chat input textarea.
   * If session is awaiting clarification, validates string length (max 2000 chars)
   * and delegates to handleClarificationSelect; otherwise sends chat message.
   */
  const handleSendChat = useCallback(
    async (textToSend?: string) => {
      const msg = (textToSend ?? chatInput).trim();
      if (!msg || isStreaming) return;
      setChatInput("");
      if (textareaRef.current) {
        textareaRef.current.style.height = "auto";
      }

      // If the session is currently blocked waiting on clarification, submitting text
      // answers the clarification request to properly unblock the orchestrator lifecycle.
      if (pendingClarification || session?.status === "awaiting_clarification") {
        if (msg.length > 2000) {
          setActionError("Clarification response cannot exceed 2000 characters.");
          return;
        }
        await handleClarificationSelect(msg);
        return;
      }

      await sendChatMessage(msg, {
        model: selectedModelId || undefined,
        provider: selectedProvider || undefined,
      });
    },
    [chatInput, isStreaming, sendChatMessage, selectedModelId, selectedProvider, pendingClarification, session?.status, handleClarificationSelect, setActionError]
  );

  const handleStageAuditFix = useCallback(
    async (finding: AuditFinding, diff?: string) => {
      const cleanDiff = stripCodeFences(diff);
      const cleanRemediation = stripCodeFences(finding.remediation_diff);
      const cleanSuggested = stripCodeFences(finding.suggested_fix);

      const candidateDiff =
        (isUnifiedDiff(cleanDiff) ? cleanDiff : null) ||
        (isUnifiedDiff(cleanRemediation) ? cleanRemediation : null) ||
        (isUnifiedDiff(cleanSuggested) ? cleanSuggested : null);

      if (candidateDiff && finding.file_path) {
        setStagedPatches((prev) => ({
          ...prev,
          [finding.file_path]: candidateDiff,
        }));
        setActiveFile(finding.file_path);
      } else {
        const fixProse = cleanSuggested || finding.description || "";
        const prompt = `Apply surgical fix for finding ${finding.id || finding.title || "audit issue"} in ${finding.file_path}${finding.line_start ? `:${finding.line_start}` : ""}${finding.line_end ? `-${finding.line_end}` : ""}${fixProse ? `: ${fixProse}` : ""}`;
        await handleSendChat(prompt);
      }
    },
    [setStagedPatches, setActiveFile, handleSendChat]
  );

  const handleVerify = async () => {
    if (isStreaming || sandboxLoading || ciActive) return;
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
    try {
      const updated = await api.getSession(sessionId);
      setSession(updated);
    } catch {
      // Non-critical
    }
  };

  const handleViewDiffForFile = (filePath: string) => {
    setActiveFile(filePath);
    setViewMode("diffs");
  };

  // Calculate total session tokens from messages, tool calls, staged diffs, and context
  const calculatedTokens = useMemo(() => {
    let charCount = 0;
    for (const m of messages) {
      charCount += m.content?.length || 0;
      if (m.thoughts?.length) {
        for (const t of m.thoughts) charCount += t.length;
      }
      if (m.toolCalls?.length) {
        for (const tc of m.toolCalls) {
          charCount += tc.name.length + (tc.args ? JSON.stringify(tc.args).length : 0);
        }
      }
    }
    for (const patch of Object.values(stagedPatches)) {
      charCount += (patch?.length || 0);
    }
    if (charCount === 0 && messages.length === 0) return 0;
    const estimated = Math.round(charCount / 3.8);
    return estimated;
  }, [messages, stagedPatches]);

  // -------------------------------------------------------------------------
  // Rendering helpers
  // -------------------------------------------------------------------------

  const patchFiles = Object.keys(stagedPatches);
  const activePatch = activeFile ? stagedPatches[activeFile] ?? "" : "";
  const { original: monacoOriginal, modified: monacoModified } = parseDiffForMonaco(activePatch);
  // Split right-panel selector — hoisted to a boolean so JSX branches below
  // don't narrow the `splitRightPanel` union (avoids TS2367 in tab buttons).
  const showSplitPreview = viewMode === "split" && splitRightPanel === "preview";

  // -------------------------------------------------------------------------
  // Loading / error states
  // -------------------------------------------------------------------------

  if (pageLoading) {
    return (
      <AppLayout title="Workspace">
        <div className="flex h-[80vh] items-center justify-center bg-[#09090b]">
          <Loader2 className="h-7 w-7 animate-spin text-amber-400" />
        </div>
      </AppLayout>
    );
  }

  if (pageError || !session) {
    return (
      <AppLayout title="Session Not Found">
        <div className="flex h-[80vh] flex-col items-center justify-center bg-[#09090b] gap-4">
          <div className="flex h-12 w-12 items-center justify-center rounded-2xl border border-red-500/30 bg-red-500/10 text-red-400">
            <AlertTriangle className="h-6 w-6" />
          </div>
          <p className="text-sm text-zinc-400">{pageError ?? "Session not found."}</p>
          <Link href="/sessions?list=true" className="text-xs font-mono text-amber-400 hover:underline">
            Back to sessions
          </Link>
        </div>
      </AppLayout>
    );
  }

  const isActive = session.status === "active";

  const activeModelKey = selectedModelId || "nemotron-3.5-lightning-free";
  const matchedModel = allKnownModels.find((m) => m.id === activeModelKey);
  const contextWindow =
    matchedModel?.context_window ||
    MODEL_CONTEXT_WINDOWS[activeModelKey] ||
    (activeModelKey.includes("space-bunny") || activeModelKey.includes("longcat") || activeModelKey.includes("ling-3.0") || activeModelKey.includes("1m") ? 1048576 : 131072);
  const modelDisplayName =
    matchedModel?.name ||
    MODEL_SHORT_NAMES[activeModelKey] ||
    activeModelKey
      .replace(/-free$/, "")
      .split("-")
      .map((s) => s.charAt(0).toUpperCase() + s.slice(1))
      .join(" ");
  const tokenPercent = Math.min(100, Math.max(0, (calculatedTokens / contextWindow) * 100));

  // Topbar actions: View switchers, Verify, Commit PR, Close
  const topbarActions = (
    <div className="flex items-center gap-2">
      {/* View Switcher: Chat (Screenshot 2 style) | Diffs | Split */}
      <div className="flex items-center rounded-xl border-t border-t-zinc-700/60 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-[#0c0c10] p-1 shadow-[inset_0_1px_2px_rgba(0,0,0,0.5)]">
        <button
          onClick={() => setViewMode("chat")}
          className={`flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-medium transition-all active:translate-y-[0.5px] ${
            viewMode === "chat"
              ? "bg-gradient-to-b from-zinc-800 via-zinc-800 to-zinc-850 text-zinc-100 font-semibold border-t border-t-zinc-600/70 border-x border-x-zinc-700/50 border-b border-b-zinc-900 shadow-[inset_0_1px_0_rgba(255,255,255,0.12),0_1px_3px_rgba(0,0,0,0.3)]"
              : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900/60"
          }`}
          title="Chat view"
        >
          <MessageSquare className="h-3.5 w-3.5" />
          <span>Chat</span>
        </button>

        <button
          onClick={() => setViewMode("diffs")}
          className={`flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-medium transition-all active:translate-y-[0.5px] ${
            viewMode === "diffs"
              ? "bg-gradient-to-b from-zinc-800 via-zinc-800 to-zinc-850 text-zinc-100 font-semibold border-t border-t-zinc-600/70 border-x border-x-zinc-700/50 border-b border-b-zinc-900 shadow-[inset_0_1px_0_rgba(255,255,255,0.12),0_1px_3px_rgba(0,0,0,0.3)]"
              : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900/60"
          }`}
          title="Monaco Diff Editor"
        >
          <FileCode2 className="h-3.5 w-3.5" />
          <span>Diffs</span>
          {patchFiles.length > 0 && (
            <span className="rounded-full bg-amber-500/20 px-1.5 py-0.2 text-[10px] font-mono text-amber-300">
              {patchFiles.length}
            </span>
          )}
        </button>

        <button
          onClick={() => setViewMode("split")}
          className={`flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-medium transition-all active:translate-y-[0.5px] ${
            viewMode === "split"
              ? "bg-gradient-to-b from-zinc-800 via-zinc-800 to-zinc-850 text-zinc-100 font-semibold border-t border-t-zinc-600/70 border-x border-x-zinc-700/50 border-b border-b-zinc-900 shadow-[inset_0_1px_0_rgba(255,255,255,0.12),0_1px_3px_rgba(0,0,0,0.3)]"
              : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900/60"
          }`}
          title="Side-by-side split view"
        >
          <Columns className="h-3.5 w-3.5" />
          <span>Split</span>
        </button>

        <button
          onClick={() => setViewMode("preview")}
          className={`flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-medium transition-all active:translate-y-[0.5px] ${
            viewMode === "preview"
              ? "bg-gradient-to-b from-zinc-800 via-zinc-800 to-zinc-850 text-zinc-100 font-semibold border-t border-t-zinc-600/70 border-x border-x-zinc-700/50 border-b border-b-zinc-900 shadow-[inset_0_1px_0_rgba(255,255,255,0.12),0_1px_3px_rgba(0,0,0,0.3)]"
              : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900/60"
          }`}
          title="Live WebContainer preview"
        >
          <Globe className="h-3.5 w-3.5" />
          <span>Preview</span>
        </button>
      </div>

      {/* AI Model & Context Token Stats Block */}
      <div
        className="flex items-center gap-2.5 rounded-xl border-t border-t-zinc-700/60 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-[#0c0c10] px-3 py-1.5 shadow-[inset_0_1px_2px_rgba(0,0,0,0.5)] select-none"
        title={`Active Model: ${activeModelKey} (${selectedProvider})\nContext: ${calculatedTokens.toLocaleString()} / ${contextWindow.toLocaleString()} tokens (${tokenPercent.toFixed(1)}% used)`}
      >
        <div className="flex items-center gap-1.5">
          <span className="relative flex h-2 w-2">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-40" />
            <span className="relative inline-flex rounded-full h-2 w-2 bg-amber-400 shadow-[0_0_6px_rgba(251,191,36,0.6)]" />
          </span>
          <span className="text-xs font-medium text-zinc-200 font-sans whitespace-nowrap">
            {modelDisplayName}
          </span>
        </div>

        <div className="h-3 w-px bg-zinc-800" />

        <div className="flex items-center gap-2">
          <div className="flex items-center gap-1 font-mono text-xs whitespace-nowrap">
            <span className="text-zinc-200 font-semibold">{formatTokens(calculatedTokens)}</span>
            <span className="text-zinc-500">/</span>
            <span className="text-zinc-400">{formatTokens(contextWindow)}</span>
            <span className="text-zinc-500 text-[10px]">tok</span>
          </div>

          <div className="flex items-center gap-1.5 rounded-md bg-zinc-900 border border-zinc-800 px-1.5 py-0.5">
            <div className="w-8 h-1 rounded-full bg-zinc-800 overflow-hidden">
              <div
                className={`h-full transition-all duration-300 ${
                  tokenPercent > 85
                    ? "bg-rose-500"
                    : tokenPercent > 60
                    ? "bg-amber-400"
                    : "bg-emerald-400"
                }`}
                style={{ width: `${Math.max(4, tokenPercent)}%` }}
              />
            </div>
            <span
              className={`text-[10px] font-mono font-medium ${
                tokenPercent > 85
                  ? "text-rose-400"
                  : tokenPercent > 60
                  ? "text-amber-300"
                  : "text-emerald-400"
              }`}
            >
              {tokenPercent.toFixed(1)}%
            </span>
          </div>
        </div>
      </div>

      {/* Live CI status chip — queued → running → passed/failed with run link */}
      {liveCiPhase && (
        <CiStatusChip
          phase={liveCiPhase}
          runUrl={ciRunUrl}
          workflowName={ciWorkflowName}
          stepName={ciStepName}
        />
      )}
      {sandboxLoading && !liveCiPhase && (
        <CiStatusChip phase="running" stepName="verifying" />
      )}

      {/* Commit & PR button with Security Badge */}
      {isActive && (
        <div className="relative flex items-center gap-1.5">
          {securityViolations && (
            <button
              onClick={() => setSecurityViolations(null)}
              title="Security violations detected — click to dismiss"
              className="flex items-center gap-1 rounded-xl border border-red-500/40 bg-red-500/10 px-2 py-1.5 text-[10px] font-mono text-red-300 hover:bg-red-500/20 transition-colors"
            >
              <ShieldAlert className="h-3.5 w-3.5" />
              Security risk
            </button>
          )}
          <button
            id="commit-pr-btn"
            onClick={() => setShowCommitModal(true)}
            disabled={patchFiles.length === 0 || isStreaming || sandboxLoading || ciActive}
            className="flex items-center gap-1.5 rounded-xl border-t border-t-violet-400/60 border-x border-x-violet-600/60 border-b border-b-violet-950 bg-gradient-to-b from-violet-600 via-violet-650 to-violet-700 px-3.5 py-1.5 text-xs font-semibold text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.25),0_2px_8px_rgba(124,58,237,0.3)] hover:brightness-105 active:translate-y-[0.5px] transition-all disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <GitPullRequest className="h-3.5 w-3.5" />
            <span>Commit & PR</span>
          </button>
        </div>
      )}

      {/* Close session button */}
      {isActive && (
        <button
          id="close-session-btn"
          onClick={handleClose}
          className="flex items-center justify-center h-7 w-7 rounded-xl border-t border-t-zinc-700/60 border-x border-x-zinc-800/60 border-b border-b-zinc-950 bg-gradient-to-b from-zinc-800/80 to-zinc-900 text-zinc-400 hover:text-red-300 hover:border-t-red-500/50 hover:bg-red-500/10 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_1px_2px_rgba(0,0,0,0.3)] active:translate-y-[0.5px] transition-all"
          title="Close session"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      )}
    </div>
  );

  return (
    <AppLayout
      title={session.repo_name || session.title || "Workspace"}
      actions={topbarActions}
      noPadding
    >
      <div className="flex h-[calc(100vh-4rem)] flex-col overflow-hidden bg-[#09090b]">
        {/* Alerts / Success banners */}
        {actionError && (
          <div className="mx-6 mt-3 flex items-center justify-between rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-2.5 text-xs text-red-300">
            <div className="flex items-center gap-2">
              <AlertTriangle className="h-4 w-4 shrink-0" />
              <span>{actionError}</span>
            </div>
            <button onClick={() => setActionError(null)} className="text-red-400 hover:text-red-200">
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        )}

        {prSuccess && (
          <div className="mx-6 mt-3 flex items-center justify-between gap-3 rounded-xl border border-violet-500/30 bg-violet-500/10 px-4 py-2.5">
            <div className="flex items-center gap-2 text-xs text-violet-300">
              <CheckCircle2 className="h-4 w-4 shrink-0 text-violet-400" />
              <span>PR #{prSuccess.number} opened successfully!</span>
            </div>
            <a
              href={prSuccess.url}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-1 text-xs font-mono text-violet-400 hover:underline"
            >
              View on GitHub <ExternalLink className="h-3.5 w-3.5" />
            </a>
          </div>
        )}

        {sandboxResult && (
          <div
            className={`mx-6 mt-3 rounded-xl border px-4 py-2.5 ${
              sandboxResult.passed
                ? "border-emerald-500/30 bg-emerald-500/10"
                : "border-red-500/30 bg-red-500/10"
            }`}
          >
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2 text-xs">
                {sandboxResult.passed ? (
                  <CheckCircle2 className="h-4 w-4 text-emerald-400 shrink-0" />
                ) : (
                  <XCircle className="h-4 w-4 text-red-400 shrink-0" />
                )}
                <span className={sandboxResult.passed ? "text-emerald-300 font-medium" : "text-red-300 font-medium"}>
                  Sandbox {sandboxResult.status} — {sandboxResult.passed ? "all tests passed" : "tests failed"}
                </span>
              </div>
              <div className="flex items-center gap-3">
                {isExternalRunUrl(sandboxResult.run_url) && (
                  <a
                    href={sandboxResult.run_url as string}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="flex items-center gap-1 text-[11px] font-mono text-zinc-400 hover:text-zinc-200 hover:underline"
                  >
                    View run <ExternalLink className="h-3 w-3" />
                  </a>
                )}
                <button
                  onClick={() => setSandboxResult(null)}
                  className="text-zinc-500 hover:text-zinc-300"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
            {sandboxResult.logs && (
              <pre className="mt-2 max-h-24 overflow-y-auto rounded-lg bg-black/50 p-2.5 text-[10px] font-mono text-zinc-400 leading-relaxed">
                {sandboxResult.logs}
              </pre>
            )}
          </div>
        )}

        {/* ================================================================ */}
        {/* WORKSPACE BODY                                                   */}
        {/* ================================================================ */}
        <div className="flex-1 flex overflow-hidden">
          {/* ============================================================== */}
          {/* CHAT DISPLAY (Active in "chat" and "split" mode)                */}
          {/* ============================================================== */}
          {(viewMode === "chat" || viewMode === "split") && (
            <div
              className={`flex flex-col h-full bg-[#09090b] relative ${
                viewMode === "split"
                  ? "w-[45%] border-r border-zinc-800"
                  : "w-full"
              }`}
            >
              {/* Subtle ambient lighting */}
              <div
                aria-hidden="true"
                className="pointer-events-none absolute -top-24 left-1/2 -translate-x-1/2 h-72 w-full max-w-2xl rounded-full bg-amber-500/[0.025] blur-3xl"
              />

              {/* Scrollable Conversation Stream */}
              <div ref={chatContainerRef} className="flex-1 overflow-y-auto px-4 md:px-8 py-6 relative z-10">
                <div className="mx-auto max-w-3xl w-full">
                  {/* Empty state / Welcome */}
                  {messages.length === 0 && (
                    <div className="flex flex-col items-center justify-center min-h-[50vh] text-center gap-5 py-12">
                      <div className="flex h-14 w-14 items-center justify-center rounded-2xl border-t border-t-amber-400/40 border-x border-x-amber-500/30 border-b border-b-amber-600/20 bg-gradient-to-b from-amber-500/20 via-amber-500/10 to-amber-600/5 text-amber-400 shadow-[inset_0_1px_0_rgba(255,255,255,0.2),0_0_24px_rgba(245,158,11,0.15)]">
                        <Sparkles className="h-7 w-7" />
                      </div>
                      <div>
                        <h2 className="text-base font-semibold text-zinc-100">
                          Live Pairing Session
                        </h2>
                        <p className="text-xs font-mono text-zinc-500 mt-1">
                          {session.repo_owner}/{session.repo_name} · {session.branch_name}
                        </p>
                      </div>

                      {/* Quick Prompt Cards */}
                      <div className="grid grid-cols-1 sm:grid-cols-3 gap-2.5 w-full max-w-lg mt-4 text-left">
                        {[
                          {
                            title: "Read a file",
                            desc: 'read app/main.py',
                            prompt: "read app/main.py",
                          },
                          {
                            title: "Stage a fix",
                            desc: 'fix the null check in auth.py',
                            prompt: "fix the null check in auth.py",
                          },
                          {
                            title: "View staged",
                            desc: 'show what is staged',
                            prompt: "show what is staged",
                          },
                        ].map((item, idx) => (
                          <button
                            key={idx}
                            onClick={() => handleSendChat(item.prompt)}
                            className="rounded-xl border-t border-t-zinc-700/60 border-x border-x-zinc-800/70 border-b border-b-zinc-950 bg-gradient-to-b from-[#15151a] to-[#0f0f13] p-3.5 hover:border-t-amber-500/50 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_4px_16px_rgba(0,0,0,0.4)] transition-all text-left group active:translate-y-[0.5px]"
                          >
                            <span className="block text-[11px] font-mono text-zinc-400 group-hover:text-amber-300 transition-colors">
                              {item.title}
                            </span>
                            <span className="block text-xs text-zinc-500 truncate mt-0.5">
                              &ldquo;{item.desc}&rdquo;
                            </span>
                          </button>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* Messages Stream */}
                  {messages.map((msg, i) => (
                    <ChatBubble
                      key={i}
                      message={msg}
                      messageIndex={i}
                      messages={messages}
                      isLatestStreaming={isStreaming && i === messages.length - 1}
                      onViewDiff={handleViewDiffForFile}
                      onStageAuditFix={handleStageAuditFix}
                      onClarificationSelect={handleClarificationSelect}
                      isSessionBlocked={session?.status === "awaiting_clarification" || Boolean(pendingClarification)}
                    />
                  ))}

                  {/* Live audit scan progress card (if active and not yet attached to message) */}
                  {activeAudit &&
                    activeAudit.status === "scanning" &&
                    !messages.some((m) => m.auditScan?.scan_id === activeAudit.scan_id) && (
                      <AuditReportCard
                        scan={activeAudit}
                        onStageFix={handleStageAuditFix}
                        onViewDiff={handleViewDiffForFile}
                      />
                    )}


                  {/* Agent CI verification badge — sandbox_result verdict inline */}
                  {(liveCiPhase === "passed" || liveCiPhase === "failed") &&
                    liveSandboxStatus && (
                      <CiVerificationBadge
                        passed={liveCiPhase === "passed"}
                        runUrl={ciRunUrl}
                        durationSeconds={ciDurationSeconds}
                        workflowName={ciWorkflowName}
                      />
                    )}
                  {sandboxResult && !liveCiPhase && (
                    <CiVerificationBadge
                      passed={sandboxResult.passed}
                      runUrl={sandboxResult.run_url}
                      durationSeconds={null}
                      workflowName={null}
                    />
                  )}

                  {/* Fallback Clarification Request Card if not already rendered inline in transcript */}
                  {pendingClarification &&
                    !messages.some((m, idx) => {
                      const isLatestAssistant =
                        m.role === "assistant" &&
                        !messages.slice(idx + 1).some((sub) => sub.role === "user");
                      return (
                        isLatestAssistant &&
                        m.toolCalls?.some((c) => c.name === "ask_user_clarification")
                      );
                    }) && (
                      <ClarificationPromptCard
                        question={pendingClarification.question}
                        options={pendingClarification.options}
                        isPending={true}
                        onSelectOption={handleClarificationSelect}
                        disabled={isStreaming}
                      />
                    )}

                  <div ref={chatBottomRef} className="h-4" />
                </div>
              </div>

              {/* ============================================================ */}
              {/* TERMINAL DRAWER — live streaming command output               */}
              {/* ============================================================ */}
              <TerminalDrawer logs={terminalLogs} ciActive={ciActive} />

              {/* ============================================================ */}
              {/* SINGLE INPUT BAR (Matches Screenshot 2)                     */}
              {/* ============================================================ */}
              <div className="flex-none w-full bg-gradient-to-t from-[#09090b] via-[#09090b]/95 to-transparent pt-2 pb-5 px-4 md:px-8">
                <div className="mx-auto max-w-3xl w-full relative">
                  {/* Contextual Suggestion Pills when conversation is active */}
                  {messages.length > 0 && !isStreaming && (
                    <div className="flex items-center gap-2 mb-2.5 overflow-x-auto pb-1 scrollbar-none select-none">
                      <span className="text-[10px] font-mono text-zinc-500 uppercase tracking-wider shrink-0 mr-1">
                        Suggestions:
                      </span>
                      <button
                        onClick={() => handleSendChat("Run targeted tests in sandbox")}
                        className="shrink-0 flex items-center gap-1.5 rounded-full border-t border-t-zinc-750/70 border-x border-x-zinc-800 border-b border-b-zinc-950 bg-gradient-to-b from-zinc-850 to-zinc-900/90 px-3 py-1 text-xs font-mono text-zinc-400 hover:text-amber-300 hover:border-t-amber-500/50 hover:bg-amber-500/10 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_1px_2px_rgba(0,0,0,0.3)] transition-all cursor-pointer active:translate-y-[0.5px]"
                      >
                        <TestTube2 className="h-3 w-3 text-amber-400" />
                        <span>Run tests</span>
                      </button>
                      <button
                        onClick={() => handleSendChat("Show what files are currently staged")}
                        className="shrink-0 flex items-center gap-1.5 rounded-full border-t border-t-zinc-750/70 border-x border-x-zinc-800 border-b border-b-zinc-950 bg-gradient-to-b from-zinc-850 to-zinc-900/90 px-3 py-1 text-xs font-mono text-zinc-400 hover:text-violet-300 hover:border-t-violet-500/50 hover:bg-violet-500/10 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_1px_2px_rgba(0,0,0,0.3)] transition-all cursor-pointer active:translate-y-[0.5px]"
                      >
                        <FileCode2 className="h-3 w-3 text-violet-400" />
                        <span>Check staged</span>
                      </button>
                      <button
                        onClick={() => handleSendChat("Explain the CI failure and recommended fix")}
                        className="shrink-0 flex items-center gap-1.5 rounded-full border-t border-t-zinc-750/70 border-x border-x-zinc-800 border-b border-b-zinc-950 bg-gradient-to-b from-zinc-850 to-zinc-900/90 px-3 py-1 text-xs font-mono text-zinc-400 hover:text-emerald-300 hover:border-t-emerald-500/50 hover:bg-emerald-500/10 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_1px_2px_rgba(0,0,0,0.3)] transition-all cursor-pointer active:translate-y-[0.5px]"
                      >
                        <Sparkles className="h-3 w-3 text-emerald-400" />
                        <span>Explain fix</span>
                      </button>
                    </div>
                  )}

                  {/* Floating Action Menu Popover (when "+" is clicked) */}
                  {actionMenuOpen && (
                    <div className="absolute bottom-[calc(100%+8px)] left-3 z-30 w-56 rounded-2xl border-t border-t-zinc-700/80 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-[#121217]/98 p-1.5 shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_16px_40px_rgba(0,0,0,0.7)] backdrop-blur-2xl">
                      <div className="px-2.5 py-1.5 text-[10px] font-mono uppercase tracking-wider text-zinc-500">
                        Quick Actions
                      </div>
                      <button
                        onClick={() => {
                          setChatInput("read ");
                          setActionMenuOpen(false);
                          textareaRef.current?.focus();
                        }}
                        className="flex w-full items-center gap-2 rounded-xl px-2.5 py-2 text-xs text-zinc-300 hover:bg-zinc-800/80 hover:text-zinc-100 transition-colors"
                      >
                        <FileText className="h-3.5 w-3.5 text-blue-400" />
                        <span>Read file…</span>
                      </button>
                      <button
                        onClick={() => {
                          setChatInput("fix the null check in ");
                          setActionMenuOpen(false);
                          textareaRef.current?.focus();
                        }}
                        className="flex w-full items-center gap-2 rounded-xl px-2.5 py-2 text-xs text-zinc-300 hover:bg-zinc-800/80 hover:text-zinc-100 transition-colors"
                      >
                        <Code2 className="h-3.5 w-3.5 text-amber-400" />
                        <span>Stage patch…</span>
                      </button>
                      <button
                        onClick={() => {
                          handleVerify();
                          setActionMenuOpen(false);
                        }}
                        disabled={patchFiles.length === 0 || isStreaming || sandboxLoading || ciActive}
                        className="flex w-full items-center gap-2 rounded-xl px-2.5 py-2 text-xs text-zinc-300 hover:bg-zinc-800/80 hover:text-zinc-100 transition-colors disabled:opacity-40"
                      >
                        <FlaskConical className="h-3.5 w-3.5 text-emerald-400" />
                        <span>Run test sandbox</span>
                      </button>
                      <button
                        onClick={() => {
                          setViewMode("diffs");
                          setActionMenuOpen(false);
                        }}
                        className="flex w-full items-center gap-2 rounded-xl px-2.5 py-2 text-xs text-zinc-300 hover:bg-zinc-800/80 hover:text-zinc-100 transition-colors"
                      >
                        <FileCode2 className="h-3.5 w-3.5 text-violet-400" />
                        <span>View staged diffs</span>
                      </button>
                      <button
                        onClick={() => {
                          setChatInput("/security-scan");
                          setActionMenuOpen(false);
                          textareaRef.current?.focus();
                        }}
                        className="flex w-full items-center gap-2 rounded-xl px-2.5 py-2 text-xs text-zinc-300 hover:bg-zinc-800/80 hover:text-zinc-100 transition-colors"
                      >
                        <ShieldAlert className="h-3.5 w-3.5 text-rose-400" />
                        <span>Security scan (/security-scan)</span>
                      </button>
                      <button
                        onClick={() => {
                          setChatInput("/repo-audit");
                          setActionMenuOpen(false);
                          textareaRef.current?.focus();
                        }}
                        className="flex w-full items-center gap-2 rounded-xl px-2.5 py-2 text-xs text-zinc-300 hover:bg-zinc-800/80 hover:text-zinc-100 transition-colors"
                      >
                        <Sparkles className="h-3.5 w-3.5 text-violet-400" />
                        <span>Code audit (/repo-audit)</span>
                      </button>
                    </div>
                  )}

                  {/* Slash Command Autocomplete Popover */}
                  {chatInput.startsWith("/") && !chatInput.includes(" ") && (
                    <div className="absolute bottom-[calc(100%+8px)] left-0 right-0 z-30 mx-auto max-w-lg rounded-xl border border-zinc-800 bg-[#121217]/98 p-1.5 shadow-2xl backdrop-blur-xl">
                      <div className="px-2.5 py-1 text-[10px] font-mono uppercase tracking-wider text-zinc-500">
                        Slash Commands
                      </div>
                      <button
                        onClick={() => {
                          setChatInput("/security-scan");
                          textareaRef.current?.focus();
                        }}
                        className="flex w-full items-center justify-between rounded-lg px-2.5 py-2 text-xs text-zinc-200 hover:bg-zinc-800/80 transition-colors"
                      >
                        <div className="flex items-center gap-2">
                          <ShieldAlert className="h-4 w-4 text-rose-400" />
                          <span className="font-mono font-semibold text-rose-300">/security-scan</span>
                        </div>
                        <span className="text-[11px] text-zinc-400">Scan for OWASP & secrets</span>
                      </button>
                      <button
                        onClick={() => {
                          setChatInput("/repo-audit");
                          textareaRef.current?.focus();
                        }}
                        className="flex w-full items-center justify-between rounded-lg px-2.5 py-2 text-xs text-zinc-200 hover:bg-zinc-800/80 transition-colors"
                      >
                        <div className="flex items-center gap-2">
                          <Sparkles className="h-4 w-4 text-violet-400" />
                          <span className="font-mono font-semibold text-violet-300">/repo-audit</span>
                        </div>
                        <span className="text-[11px] text-zinc-400">Architecture & code quality</span>
                      </button>
                    </div>
                  )}

                  {/* Model Selector Popover */}
                  {modelPickerOpen && (
                    <div className="absolute bottom-[calc(100%+10px)] left-12 z-30 w-80 rounded-2xl border-t border-t-zinc-700/80 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-[#121217]/98 shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_24px_64px_rgba(0,0,0,0.8)] backdrop-blur-2xl overflow-hidden">
                      {/* Header */}
                      <div className="flex items-center justify-between px-3.5 pt-3 pb-2 border-b border-zinc-800/60">
                        <span className="text-[10px] font-mono uppercase tracking-wider text-zinc-500">
                          Select Model
                        </span>
                        <button
                          onClick={() => setModelPickerOpen(false)}
                          className="flex h-5 w-5 items-center justify-center rounded text-zinc-600 hover:text-zinc-300 transition-colors"
                        >
                          <X className="h-3.5 w-3.5" />
                        </button>
                      </div>

                      {/* Search */}
                      <div className="px-2.5 pt-2 pb-1">
                        <div className="flex items-center gap-2 rounded-xl border border-zinc-800 bg-zinc-900/80 px-2.5 py-1.5">
                          <Search className="h-3.5 w-3.5 text-zinc-500 shrink-0" />
                          <input
                            type="text"
                            value={modelSearch}
                            onChange={(e) => setModelSearch(e.target.value)}
                            placeholder="Filter models…"
                            className="flex-1 bg-transparent text-xs text-zinc-200 placeholder-zinc-600 focus:outline-none font-mono"
                          />
                          {modelSearch && (
                            <button onClick={() => setModelSearch("")} className="text-zinc-600 hover:text-zinc-300">
                              <X className="h-3 w-3" />
                            </button>
                          )}
                        </div>
                      </div>

                      {/* Model list */}
                      <div className="max-h-[360px] overflow-y-auto px-1.5 pb-1.5 space-y-0.5">
                        {(
                          [
                            {
                              label: "OpenCode Zen · Free Tier",
                              provider: "opencode_zen",
                              items: availableModels.opencode_zen,
                              accentClass: "text-amber-300",
                              dotClass: "bg-amber-400",
                              badgeClass: "bg-amber-500/15 text-amber-300 border-amber-500/30",
                              badgeLabel: "Free",
                            },
                            {
                              label: "Groq · Ultra Fast",
                              provider: "groq",
                              items: availableModels.groq,
                              accentClass: "text-orange-300",
                              dotClass: "bg-orange-400",
                              badgeClass: "bg-orange-500/15 text-orange-300 border-orange-500/30",
                              badgeLabel: "Fast",
                            },
                            {
                              label: "Anthropic",
                              provider: "anthropic",
                              items: availableModels.anthropic,
                              accentClass: "text-violet-300",
                              dotClass: "bg-violet-400",
                              badgeClass: "bg-violet-500/15 text-violet-300 border-violet-500/30",
                              badgeLabel: "SOTA",
                            },
                            {
                              label: "OpenAI",
                              provider: "openai",
                              items: availableModels.openai,
                              accentClass: "text-emerald-300",
                              dotClass: "bg-emerald-400",
                              badgeClass: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
                              badgeLabel: "GPT",
                            },
                          ] as const
                        ).map((group) => {
                          const filtered = group.items.filter(
                            (m) =>
                              !modelSearch ||
                              m.id.toLowerCase().includes(modelSearch.toLowerCase()) ||
                              m.name.toLowerCase().includes(modelSearch.toLowerCase()) ||
                              m.tag.toLowerCase().includes(modelSearch.toLowerCase()),
                          );
                          if (filtered.length === 0 && modelSearch) return null;
                          return (
                            <div key={group.provider}>
                              <div className="flex items-center gap-1.5 px-2.5 py-1.5">
                                <span className={`inline-block h-1.5 w-1.5 rounded-full ${group.dotClass}`} />
                                <span className="text-[10px] font-mono uppercase tracking-wide text-zinc-500">
                                  {group.label}
                                </span>
                              </div>
                              {filtered.map((m) => (
                                <button
                                  key={m.id}
                                  onClick={() => {
                                    setSelectedModelId(m.id);
                                    setSelectedProvider(group.provider);
                                    setModelPickerOpen(false);
                                    setModelSearch("");
                                  }}
                                  className={`flex w-full items-center justify-between rounded-xl px-2.5 py-2 text-xs font-mono transition-colors ${
                                    selectedModelId === m.id
                                      ? "bg-amber-500/10 border border-amber-500/20"
                                      : "hover:bg-zinc-800/70 border border-transparent"
                                  }`}
                                >
                                  <div className="flex items-center gap-2 min-w-0">
                                    {selectedModelId === m.id ? (
                                      <Check className="h-3 w-3 text-amber-400 shrink-0" />
                                    ) : (
                                      <span className="h-3 w-3 shrink-0" />
                                    )}
                                    <span className={`truncate ${selectedModelId === m.id ? "text-amber-200" : "text-zinc-300"}`}>
                                      {m.name || m.id}
                                    </span>
                                  </div>
                                  <span className={`ml-2 shrink-0 rounded border px-1.5 py-0.5 text-[9px] font-mono ${group.badgeClass}`}>
                                    {m.tag || group.badgeLabel}
                                  </span>
                                </button>
                              ))}
                            </div>
                          );
                        })}

                        {/* Custom model input */}
                        <div>
                          <div className="flex items-center gap-1.5 px-2.5 py-1.5">
                            <Zap className="h-3 w-3 text-zinc-500" />
                            <span className="text-[10px] font-mono uppercase tracking-wide text-zinc-500">
                              Custom Model ID
                            </span>
                          </div>
                          <div className="px-2 space-y-1.5">
                            <input
                              type="text"
                              value={customModelInput}
                              onChange={(e) => setCustomModelInput(e.target.value)}
                              placeholder="e.g. deepseek-r1-distill-llama-70b"
                              className="w-full rounded-xl border border-zinc-800 bg-zinc-900/80 px-2.5 py-1.5 text-xs font-mono text-zinc-200 placeholder-zinc-600 focus:outline-none focus:border-zinc-600"
                            />
                            <div className="flex items-center gap-1.5">
                              {(["auto", "groq", "opencode_zen"] as const).map((p) => (
                                <button
                                  key={p}
                                  onClick={() => setCustomProviderInput(p)}
                                  className={`rounded-lg border px-2 py-1 text-[10px] font-mono transition-colors ${
                                    customProviderInput === p
                                      ? "border-amber-500/40 bg-amber-500/15 text-amber-300"
                                      : "border-zinc-800 text-zinc-500 hover:border-zinc-700 hover:text-zinc-300"
                                  }`}
                                >
                                  {p === "auto" ? "auto-detect" : p}
                                </button>
                              ))}
                            </div>
                            <button
                              disabled={!customModelInput.trim()}
                              onClick={() => {
                                if (!customModelInput.trim()) return;
                                const resolvedProvider =
                                  customProviderInput === "auto"
                                    ? undefined
                                    : customProviderInput;
                                setSelectedModelId(customModelInput.trim());
                                setSelectedProvider(resolvedProvider ?? "opencode_zen");
                                setModelPickerOpen(false);
                                setModelSearch("");
                              }}
                              className="w-full rounded-xl border border-zinc-700/60 bg-zinc-800/60 py-1.5 text-xs font-mono text-zinc-300 hover:bg-zinc-700/60 hover:text-zinc-100 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                            >
                              Use this model
                            </button>
                          </div>
                        </div>
                      </div>
                    </div>
                  )}

                  {/* Input Card Container (Matches Screenshot 2) */}
                  <div className="relative rounded-2xl border-t border-t-zinc-700/70 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-gradient-to-b from-[#141419]/95 via-[#111115]/98 to-[#0b0b0e]/98 backdrop-blur-2xl p-3.5 shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_12px_40px_rgba(0,0,0,0.65)] transition-all focus-within:border-t-zinc-500 focus-within:ring-1 focus-within:ring-amber-500/25">
                    {/* Textarea */}
                    <textarea
                      ref={textareaRef}
                      value={chatInput}
                      onChange={handleInputChange}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && !e.shiftKey) {
                          e.preventDefault();
                          handleSendChat();
                        }
                      }}
                      disabled={!isActive || isStreaming}
                      placeholder={
                        !isActive
                          ? "Session is closed."
                          : isStreaming
                          ? "Agent is responding…"
                          : pendingClarification || session?.status === "awaiting_clarification"
                          ? "Agent is waiting for clarification above (select an option or type reply)…"
                          : "Ask anything, @ to mention, / for actions"
                      }
                      rows={1}
                      className="w-full resize-none bg-transparent px-2 py-1 text-sm text-zinc-100 placeholder-zinc-500 focus:outline-none font-sans min-h-[44px] max-h-[160px] leading-relaxed disabled:opacity-50"
                    />

                    {/* Bottom toolbar inside input card */}
                    <div className="flex items-center justify-between pt-2 px-1">
                      {/* Left: "+" button and Model Picker Pill */}
                      <div className="flex items-center gap-2">
                        {/* "+" button */}
                        <button
                          type="button"
                          onClick={() => {
                            setActionMenuOpen((prev) => !prev);
                            setModelPickerOpen(false);
                          }}
                          className={`flex h-7 w-7 items-center justify-center rounded-lg border-t border-x border-b transition-all active:translate-y-[0.5px] ${
                            actionMenuOpen
                              ? "border-t-amber-400/60 border-x-amber-500/40 border-b-amber-700 bg-amber-500/20 text-amber-300 shadow-[inset_0_1px_0_rgba(255,255,255,0.15)]"
                              : "border-t-zinc-650/70 border-x-zinc-750/60 border-b-zinc-900 bg-gradient-to-b from-zinc-800 to-zinc-850 text-zinc-300 hover:text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.1),0_1px_2px_rgba(0,0,0,0.3)]"
                          }`}
                          title="Actions & Prompts"
                        >
                          <Plus className="h-4 w-4" />
                        </button>

                        {/* Model pill with provider dot + Chevron */}
                        <button
                          type="button"
                          onClick={() => {
                            setModelPickerOpen((prev) => !prev);
                            setActionMenuOpen(false);
                          }}
                          className={`flex items-center gap-2 rounded-lg px-2.5 py-1 text-xs font-mono border-t border-x border-b transition-all active:translate-y-[0.5px] ${
                            modelPickerOpen
                              ? "border-t-zinc-600 border-x-zinc-700 border-b-zinc-900 bg-zinc-800 text-zinc-100 shadow-[inset_0_1px_0_rgba(255,255,255,0.1)]"
                              : "border-t-zinc-700/60 border-x-zinc-800/60 border-b-zinc-950 bg-gradient-to-b from-zinc-850 to-zinc-900/90 text-zinc-300 hover:text-white hover:border-t-zinc-600 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_1px_2px_rgba(0,0,0,0.3)]"
                          }`}
                        >
                          {/* Provider indicator dot */}
                          <span
                            className={`inline-block h-1.5 w-1.5 rounded-full shrink-0 ${
                              selectedProvider === "groq"
                                ? "bg-orange-400 shadow-[0_0_6px_rgba(251,146,60,0.8)]"
                                : selectedProvider === "anthropic"
                                ? "bg-violet-400 shadow-[0_0_6px_rgba(167,139,250,0.8)]"
                                : selectedProvider === "openai"
                                ? "bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,0.8)]"
                                : "bg-amber-400 shadow-[0_0_6px_rgba(251,191,36,0.8)]"
                            }`}
                          />
                          <span className="truncate max-w-[150px] font-medium">{selectedModelId}</span>
                          <ChevronUp className={`h-3 w-3 text-zinc-500 transition-transform ${modelPickerOpen ? "rotate-180" : ""}`} />
                        </button>
                      </div>

                      {/* Right: Mic/Action icon and Send/Stop button */}
                      <div className="flex items-center gap-2">
                        <button
                          type="button"
                          onClick={() => {
                            setChatInput("show what is staged");
                            textareaRef.current?.focus();
                          }}
                          className="flex h-7 w-7 items-center justify-center rounded-lg text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800/60 transition-colors"
                          title="Quick prompt"
                        >
                          <Mic className="h-4 w-4" />
                        </button>

                        {/* Send or Stop button */}
                        {isStreaming ? (
                          <button
                            type="button"
                            onClick={() => {
                              stopStreaming();
                            }}
                            className="flex h-8 w-8 items-center justify-center rounded-lg border-t border-t-red-400/60 border-x border-x-red-500/40 border-b border-b-red-800 bg-gradient-to-b from-red-500/30 to-red-600/20 text-red-300 hover:bg-red-500/40 transition-all shadow-[inset_0_1px_0_rgba(255,255,255,0.15),0_2px_6px_rgba(239,68,68,0.2)] active:translate-y-[0.5px]"
                            title="Stop generating"
                          >
                            <Square className="h-3.5 w-3.5 fill-current" />
                          </button>
                        ) : (
                          <button
                            type="button"
                            onClick={() => handleSendChat()}
                            disabled={!isActive || !chatInput.trim()}
                            className={`flex h-8 w-8 items-center justify-center rounded-lg transition-all ${
                              chatInput.trim()
                                ? "border-t border-t-amber-300/80 border-x border-x-amber-500/70 border-b border-b-amber-700 bg-gradient-to-b from-amber-400 via-amber-500 to-amber-600 text-zinc-950 font-bold shadow-[inset_0_1px_0_rgba(255,255,255,0.35),0_2px_8px_rgba(245,158,11,0.35)] hover:brightness-105 active:translate-y-[0.5px]"
                                : "border-t border-t-zinc-700/40 border-x border-x-zinc-800/40 border-b border-b-zinc-900 bg-zinc-850/80 text-zinc-600 cursor-not-allowed"
                            }`}
                            title="Send message (Enter)"
                          >
                            <Send className="h-3.5 w-3.5" />
                          </button>
                        )}
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* ============================================================== */}
          {/* MONACO DIFF EDITOR (Active in "diffs" and "split" mode)         */}
          {/* Split right panel toggles Diff editor vs Preview iframe via     */}
          {/* splitRightPanel segmented control (pure UI state).              */}
          {/* ============================================================== */}
          {(viewMode === "diffs" || viewMode === "split") && (
            <div
              className={`flex flex-col h-full bg-[#0d0d0f] ${
                viewMode === "split" ? "flex-1" : "w-full"
              }`}
            >
              {showSplitPreview ? (
                <>
                  {/* Right panel header — segmented control only. Active tab is
                      known here (preview), so selected states are literals to
                      avoid narrowing the splitRightPanel union (TS2367). */}
                  <div className="flex-none flex items-center justify-end border-b border-zinc-800 bg-[#0c0c0e] px-2 py-1.5">
                    <div
                      className="flex shrink-0 items-center rounded-lg border border-zinc-800 bg-[#0a0a0d] p-0.5"
                      role="tablist"
                      aria-label="Split right panel view"
                    >
                      <button
                        type="button"
                        role="tab"
                        aria-selected={false}
                        onClick={() => setSplitRightPanel("diffs")}
                        className="flex items-center gap-1.5 rounded-md px-2.5 py-1 text-[11px] font-medium transition-all text-zinc-500 hover:text-zinc-300"
                      >
                        <FileCode2 className="h-3 w-3" />
                        <span>Diffs</span>
                      </button>
                      <button
                        type="button"
                        role="tab"
                        aria-selected={true}
                        onClick={() => setSplitRightPanel("preview")}
                        className="flex items-center gap-1.5 rounded-md px-2.5 py-1 text-[11px] font-medium transition-all bg-gradient-to-b from-zinc-700 to-zinc-800 text-zinc-100 shadow-[inset_0_1px_0_rgba(255,255,255,0.12)]"
                      >
                        <Globe className="h-3 w-3" />
                        <span>Preview</span>
                      </button>
                    </div>
                  </div>

                  {/* Preview side — reuses WebPreviewPanel, no new data flows */}
                  <div className="flex-1 overflow-hidden">
                    <WebPreviewPanel
                      status={wcStatus}
                      previewUrl={previewUrl}
                      error={wcError}
                      onBoot={handleBootPreview}
                      onRefresh={handleRefreshPreview}
                      onRestartServer={restartDevServer}
                      onSaveEnv={handleSavePreviewEnv}
                      initialEnvVars={previewEnvVars}
                      className="h-full"
                    />
                  </div>
                </>
              ) : patchFiles.length > 0 ? (
                <>
                  {/* File tab bar */}
                  <div className="flex-none flex items-center border-b border-zinc-800 bg-[#0c0c0e] overflow-x-auto">
                    {patchFiles.map((file) => (
                      <button
                        key={file}
                        onClick={() => setActiveFile(file)}
                        className={`flex items-center gap-2 px-4 py-2.5 text-xs font-mono whitespace-nowrap border-r border-zinc-800 transition-all ${
                          activeFile === file
                            ? "bg-[#13131a] text-amber-300 border-t-2 border-t-amber-500"
                            : "text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800/30"
                        }`}
                      >
                        <FileCode2 className="h-3.5 w-3.5 shrink-0" />
                        <span className="truncate max-w-[200px]" title={file}>
                          {file.split("/").pop()}
                        </span>
                      </button>
                    ))}

                    {/* Switch back to chat shortcut */}
                    {viewMode === "diffs" && (
                      <button
                        onClick={() => setViewMode("chat")}
                        className="ml-auto mr-3 flex items-center gap-1 text-xs font-sans text-zinc-400 hover:text-zinc-200 transition-colors"
                      >
                        <MessageSquare className="h-3.5 w-3.5" />
                        <span>Back to Chat</span>
                      </button>
                    )}

                    {/* Split right-panel segmented control */}
                    {viewMode === "split" && (
                      <div
                        className="ml-auto mr-2 flex shrink-0 items-center rounded-lg border border-zinc-800 bg-[#0a0a0d] p-0.5"
                        role="tablist"
                        aria-label="Split right panel view"
                      >
                        <button
                          type="button"
                          role="tab"
                          aria-selected={splitRightPanel === "diffs"}
                          onClick={() => setSplitRightPanel("diffs")}
                          className={`flex items-center gap-1.5 rounded-md px-2.5 py-1 text-[11px] font-medium transition-all ${
                            splitRightPanel === "diffs"
                              ? "bg-gradient-to-b from-zinc-700 to-zinc-800 text-zinc-100 shadow-[inset_0_1px_0_rgba(255,255,255,0.12)]"
                              : "text-zinc-500 hover:text-zinc-300"
                          }`}
                        >
                          <FileCode2 className="h-3 w-3" />
                          <span>Diffs</span>
                        </button>
                        <button
                          type="button"
                          role="tab"
                          aria-selected={splitRightPanel === "preview"}
                          onClick={() => setSplitRightPanel("preview")}
                          className={`flex items-center gap-1.5 rounded-md px-2.5 py-1 text-[11px] font-medium transition-all ${
                            splitRightPanel === "preview"
                              ? "bg-gradient-to-b from-zinc-700 to-zinc-800 text-zinc-100 shadow-[inset_0_1px_0_rgba(255,255,255,0.12)]"
                              : "text-zinc-500 hover:text-zinc-300"
                          }`}
                        >
                          <Globe className="h-3 w-3" />
                          <span>Preview</span>
                        </button>
                      </div>
                    )}
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
                /* Empty state when no diffs staged */
                <>
                  {viewMode === "split" && (
                    <div className="flex-none flex items-center justify-end border-b border-zinc-800 bg-[#0c0c0e] px-2 py-1.5">
                      <div
                        className="flex shrink-0 items-center rounded-lg border border-zinc-800 bg-[#0a0a0d] p-0.5"
                        role="tablist"
                        aria-label="Split right panel view"
                      >
                        <button
                          type="button"
                          role="tab"
                          aria-selected={splitRightPanel === "diffs"}
                          onClick={() => setSplitRightPanel("diffs")}
                          className={`flex items-center gap-1.5 rounded-md px-2.5 py-1 text-[11px] font-medium transition-all ${
                            splitRightPanel === "diffs"
                              ? "bg-gradient-to-b from-zinc-700 to-zinc-800 text-zinc-100 shadow-[inset_0_1px_0_rgba(255,255,255,0.12)]"
                              : "text-zinc-500 hover:text-zinc-300"
                          }`}
                        >
                          <FileCode2 className="h-3 w-3" />
                          <span>Diffs</span>
                        </button>
                        <button
                          type="button"
                          role="tab"
                          aria-selected={splitRightPanel === "preview"}
                          onClick={() => setSplitRightPanel("preview")}
                          className={`flex items-center gap-1.5 rounded-md px-2.5 py-1 text-[11px] font-medium transition-all ${
                            splitRightPanel === "preview"
                              ? "bg-gradient-to-b from-zinc-700 to-zinc-800 text-zinc-100 shadow-[inset_0_1px_0_rgba(255,255,255,0.12)]"
                              : "text-zinc-500 hover:text-zinc-300"
                          }`}
                        >
                          <Globe className="h-3 w-3" />
                          <span>Preview</span>
                        </button>
                      </div>
                    </div>
                  )}
                  <div className="flex-1 flex flex-col items-center justify-center gap-5 px-8 text-center">
                    <div className="flex h-16 w-16 items-center justify-center rounded-2xl border border-zinc-800 bg-[#121216] text-zinc-600">
                      <WrapText className="h-8 w-8" />
                    </div>
                    <div>
                      <p className="text-sm font-medium text-zinc-300">No staged patches yet</p>
                      <p className="text-xs font-mono text-zinc-500 mt-1">
                        Ask the agent to inspect and modify files in the chat
                      </p>
                    </div>

                    <button
                      onClick={() => setViewMode("chat")}
                      className="flex items-center gap-2 rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-2 text-xs font-mono text-amber-300 hover:bg-amber-500/20 transition-colors"
                    >
                      <MessageSquare className="h-3.5 w-3.5" />
                      <span>Go to Chat</span>
                    </button>
                  </div>
                </>
              )}
            </div>
          )}

          {/* ============================================================== */}
          {/* LIVE PREVIEW (Active in "preview" mode)                         */}
          {/* ============================================================== */}
          {viewMode === "preview" && (
            <div className="flex h-full w-full flex-1 flex-col overflow-hidden bg-[#09090b] md:w-auto">
              <WebPreviewPanel
                status={wcStatus}
                previewUrl={previewUrl}
                error={wcError}
                onBoot={handleBootPreview}
                onRefresh={handleRefreshPreview}
                onRestartServer={restartDevServer}
                onSaveEnv={handleSavePreviewEnv}
                initialEnvVars={previewEnvVars}
                className="flex-1"
              />
            </div>
          )}

          {/* ============================================================== */}
          {/* PLAN CHECKLIST SIDEBAR (Collapsible)                            */}
          {/* ============================================================== */}
          {plan.length > 0 && (
            <div
              className={`flex flex-col border-l border-zinc-800 bg-[#0c0c0e] transition-all duration-200 shrink-0 ${
                showPlanSidebar ? "w-72" : "w-10"
              }`}
            >
              {/* Header */}
              <div className="flex items-center justify-between border-b border-zinc-800 px-3 py-2.5 bg-[#101014]">
                <button
                  onClick={() => setShowPlanSidebar((prev) => !prev)}
                  className="flex items-center gap-2 text-xs font-semibold text-zinc-200 hover:text-white transition-colors"
                  title="Toggle plan checklist"
                >
                  <ListTodo className="h-4 w-4 text-amber-400 shrink-0" />
                  {showPlanSidebar && <span>Execution Plan</span>}
                </button>
                {showPlanSidebar && (
                  <div className="flex items-center gap-1.5">
                    <span className="rounded-full bg-amber-500/20 px-2 py-0.5 text-[10px] font-mono text-amber-300">
                      {plan.filter((t) => t.status === "completed").length}/{plan.length}
                    </span>
                    <button
                      onClick={() => setShowPlanSidebar(false)}
                      className="text-zinc-500 hover:text-zinc-300"
                    >
                      <ChevronRight className="h-3.5 w-3.5" />
                    </button>
                  </div>
                )}
              </div>

              {showPlanSidebar && (
                <div className="flex-1 overflow-y-auto p-3 space-y-2">
                  {/* Progress bar */}
                  <div className="mb-3">
                    <div className="flex items-center justify-between text-[11px] font-mono text-zinc-400 mb-1">
                      <span>Progress</span>
                      <span>
                        {Math.round(
                          (plan.filter((t) => t.status === "completed").length / plan.length) * 100
                        )}
                        %
                      </span>
                    </div>
                    <div className="h-1.5 w-full rounded-full bg-zinc-800 overflow-hidden">
                      <div
                        className="h-full bg-gradient-to-r from-amber-500 to-emerald-500 transition-all duration-300"
                        style={{
                          width: `${(plan.filter((t) => t.status === "completed").length / plan.length) * 100}%`,
                        }}
                      />
                    </div>
                  </div>

                  {/* Task list */}
                  {plan.map((task) => {
                    let taskIcon = <Clock className="h-3.5 w-3.5 text-zinc-500 shrink-0" />;
                    let taskBg = "bg-zinc-900/40 border-zinc-800/60 text-zinc-400";
                    if (task.status === "completed") {
                      taskIcon = <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400 shrink-0" />;
                      taskBg = "bg-emerald-950/20 border-emerald-500/20 text-zinc-200 line-through opacity-80";
                    } else if (task.status === "in_progress") {
                      taskIcon = <Loader2 className="h-3.5 w-3.5 text-amber-400 shrink-0 animate-spin" />;
                      taskBg = "bg-amber-950/30 border-amber-500/40 text-amber-100 font-medium shadow-sm";
                    } else if (task.status === "failed") {
                      taskIcon = <XCircle className="h-3.5 w-3.5 text-rose-400 shrink-0" />;
                      taskBg = "bg-rose-950/20 border-rose-500/30 text-rose-200";
                    }

                    return (
                      <div
                        key={task.id}
                        className={`flex items-start gap-2.5 rounded-xl border p-2.5 text-xs transition-all ${taskBg}`}
                      >
                        <div className="mt-0.5">{taskIcon}</div>
                        <div className="flex-1 min-w-0">
                          <span className="block leading-relaxed break-words">{task.title}</span>
                          <span className="text-[10px] font-mono text-zinc-500 uppercase tracking-wider block mt-0.5">
                            {task.status.replace("_", " ")}
                          </span>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Modals */}
      {showCommitModal && (
        <CommitModal
          sessionId={sessionId}
          repoOwner={session?.repo_owner}
          repoName={session?.repo_name}
          branchName={session?.branch_name}
          patchCount={patchFiles.length}
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

function stripCodeFences(text?: string): string {
  if (!text || typeof text !== "string") return "";
  let clean = text.trim();
  if (clean.startsWith("```")) {
    clean = clean.replace(/^```[a-zA-Z0-9_-]*\r?\n?/, "").replace(/\r?\n?```$/, "");
  }
  return clean.trim();
}

function parseSecurityScanResult(scanResult: string): { hasViolations: boolean; isClean: boolean } {
  if (!scanResult) return { hasViolations: false, isClean: true };
  const lower = scanResult.toLowerCase().trim();

  // Explicit clean phrases / 0 violations
  const isCleanPhrase =
    lower === "" ||
    /\b(?:clean|passed|passed\s+cleanly|passed\s+all\s+checks|success(?:ful)?)\b/.test(lower) ||
    /(?:no|0|zero)\s+(?:security\s+)?(?:vulnerabilit\w*|violation\w*|flaws?|secrets?|issues?|findings?|alerts?)/.test(lower) ||
    /0\s+(?:secrets?|sql\s+injection\s+flaws?|flaws?|vulnerabilit\w*|violations?|critical|high)/.test(lower) ||
    lower.includes("no vulnerabilities found") ||
    lower.includes("no violations found") ||
    lower.includes("0 secrets or sql injection flaws detected");

  if (isCleanPhrase) {
    return { hasViolations: false, isClean: true };
  }

  const hasViolations =
    /(?:found|detected|\d+)\s+(?:security\s+)?(?:vulnerabilit\w*|violation\w*|flaws?|secrets?|issues?|findings?|alerts?)/.test(lower) ||
    /\b(?:failed|critical|high|blocker|violation|vulnerabilit\w*)\b/.test(lower);

  return { hasViolations, isClean: !hasViolations };
}

function isUnifiedDiff(text?: string): boolean {
  if (!text || typeof text !== "string") return false;
  const stripped = stripCodeFences(text);
  const trimmed = stripped.trim();
  return (
    trimmed.startsWith("diff --git") ||
    trimmed.startsWith("--- ") ||
    trimmed.startsWith("@@ ") ||
    (trimmed.includes("\n--- ") && trimmed.includes("\n+++ ")) ||
    (trimmed.includes("\n@@ ") && (trimmed.includes("\n+") || trimmed.includes("\n-")))
  );
}

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
  }

  return {
    original: originalLines.join("\n"),
    modified: modifiedLines.join("\n"),
  };
}

// ---------------------------------------------------------------------------
// Preview scaffold — v1 synthetic file tree for WebContainer boot.
// Only agent-staged files + a minimal Vite scaffold are mounted. Full repo
// clone via GitHub API file tree fetch is a v2 enhancement.
// ---------------------------------------------------------------------------

const PREVIEW_SCAFFOLD_PACKAGE_JSON = `{
  "name": "haunter-preview",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "vite --port 3000 --host --strictPort"
  },
  "devDependencies": {
    "vite": "^5.4.0"
  }
}
`;

const PREVIEW_SCAFFOLD_VITE_CONFIG = `import { defineConfig } from "vite";

export default defineConfig({
  server: { port: 3000, host: true, strictPort: true },
});
`;

const PREVIEW_SCAFFOLD_INDEX_HTML = `<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Haunter Preview</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.js"></script>
  </body>
</html>
`;

const PREVIEW_SCAFFOLD_MAIN_JS = `const root = document.getElementById("root");
if (root) {
  root.innerHTML =
    "<main style=\"font-family: ui-monospace, monospace; padding: 32px; color: #e4e4e7;\">" +
    "<h1 style=\"font-size: 18px; margin-bottom: 8px;\">Haunter Live Preview</h1>" +
    "<p style=\"font-size: 12px; color: #71717a;\">Container is running. Stage files with the agent to see them here.</p>" +
    "</main>";
  document.body.style.background = "#09090b";
  document.body.style.margin = "0";
}
`;

function insertPreviewFile(
  tree: FileSystemTree,
  filePath: string,
  contents: string
): void {
  const parts = filePath.replace(/^\/+/, "").split("/").filter(Boolean);
  if (parts.length === 0) return;
  let node = tree;
  for (let i = 0; i < parts.length - 1; i += 1) {
    const segment = parts[i];
    const existing = node[segment] as
      | { directory: FileSystemTree }
      | { file: { contents: string } }
      | undefined;
    if (!existing || !("directory" in existing)) {
      const child: FileSystemTree = {};
      node[segment] = { directory: child };
      node = child;
    } else {
      node = existing.directory;
    }
  }
  node[parts[parts.length - 1]] = { file: { contents } };
}

function buildPreviewFileTree(
  stagedPatches: Record<string, string>
): FileSystemTree {
  const tree: FileSystemTree = {
    "package.json": { file: { contents: PREVIEW_SCAFFOLD_PACKAGE_JSON } },
    "vite.config.js": { file: { contents: PREVIEW_SCAFFOLD_VITE_CONFIG } },
    "index.html": { file: { contents: PREVIEW_SCAFFOLD_INDEX_HTML } },
  };
  insertPreviewFile(tree, "src/main.js", PREVIEW_SCAFFOLD_MAIN_JS);

  for (const [filePath, diff] of Object.entries(stagedPatches)) {
    const { modified } = parseDiffForMonaco(diff);
    if (!modified) continue;
    // Never let a staged patch clobber the scaffold boot contract unless it
    // explicitly targets that path — last write wins, which is correct.
    insertPreviewFile(tree, filePath, modified.endsWith("\n") ? modified : `${modified}\n`);
  }

  return tree;
}
