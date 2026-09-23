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

import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import dynamic from "next/dynamic";
import Link from "next/link";
import {
  Send,
  FlaskConical,
  GitPullRequest,
  X,
  CheckCircle2,
  XCircle,
  Circle,
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
} from "lucide-react";
import { api, SessionOut, ApiError, AvailableModelItem } from "@/lib/api";
import { useSessionStream, ChatMessage, ToolCallChip } from "@/hooks/useSessionStream";
import { AppLayout } from "@/components/layout/app-layout";

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
    { id: "nemotron-3.5-lightning-free", name: "Nemotron 3.5 Lightning", tag: "Free" },
    { id: "laguna-s-2.1-free", name: "Laguna S 2.1", tag: "Free" },
    { id: "deepseek-r1-0528-free", name: "DeepSeek R1 0528", tag: "Free" },
  ],
  groq: [
    { id: "llama-3.3-70b-versatile", name: "Llama 3.3 70B Versatile", tag: "Fast" },
    { id: "llama-3.1-8b-instant", name: "Llama 3.1 8B Instant", tag: "Fast" },
    { id: "openai/gpt-oss-120b", name: "GPT OSS 120B (Groq)", tag: "Fast" },
    { id: "deepseek-r1-distill-llama-70b", name: "DeepSeek R1 Llama 70B", tag: "Fast" },
    { id: "gemma2-9b-it", name: "Gemma 2 9B", tag: "Fast" },
  ],
  anthropic: [
    { id: "claude-sonnet-4-5", name: "Claude Sonnet 4.5", tag: "SOTA" },
    { id: "claude-haiku-3-5", name: "Claude Haiku 3.5", tag: "SOTA" },
  ],
  openai: [
    { id: "gpt-4o", name: "GPT-4o", tag: "GPT" },
    { id: "gpt-4o-mini", name: "GPT-4o Mini", tag: "GPT" },
  ],
} as const satisfies Record<string, { id: string; name: string; tag: string }[]>;

// ---------------------------------------------------------------------------
// Helpers & Sub-components
// ---------------------------------------------------------------------------

function StatusChip({ status }: { status: string }) {
  if (status === "active") {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2.5 py-0.5 text-[11px] font-mono font-medium text-emerald-300">
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
      <span className="inline-flex items-center gap-1.5 rounded-full border border-violet-500/30 bg-violet-500/10 px-2.5 py-0.5 text-[11px] font-mono font-medium text-violet-300">
        <CheckCircle2 className="h-3 w-3" />
        completed
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-zinc-700/60 bg-zinc-800/60 px-2.5 py-0.5 text-[11px] font-mono font-medium text-zinc-400">
      <Circle className="h-3 w-3" />
      {status}
    </span>
  );
}

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
// Markdown text renderer with inline code, bold, lists, and line breaks
// ---------------------------------------------------------------------------

function FormattedText({ text }: { text: string }) {
  if (!text) return null;

  // Split into paragraphs / lines
  const lines = text.split("\n");

  return (
    <div className="space-y-1.5">
      {lines.map((line, lineIdx) => {
        // Empty lines create a subtle break
        if (!line.trim()) {
          return <div key={lineIdx} className="h-1.5" />;
        }

        // Bullet point detection
        const isBullet = /^\s*[-*]\s+/.test(line);
        const cleanLine = isBullet ? line.replace(/^\s*[-*]\s+/, "") : line;

        // Inline formatting parse: `code` and **bold**
        const tokens = cleanLine.split(/(`[^`]+`|\*\*[^*]+\*\*)/g);

        const renderedTokens = tokens.map((tok, tokIdx) => {
          if (tok.startsWith("`") && tok.endsWith("`")) {
            return (
              <code
                key={tokIdx}
                className="mx-0.5 rounded-[4px] border border-zinc-700/60 bg-zinc-800/80 px-1.5 py-0.5 text-[11px] font-mono text-amber-200"
              >
                {tok.slice(1, -1)}
              </code>
            );
          }
          if (tok.startsWith("**") && tok.endsWith("**")) {
            return (
              <strong key={tokIdx} className="font-semibold text-zinc-100">
                {tok.slice(2, -2)}
              </strong>
            );
          }
          return tok;
        });

        if (isBullet) {
          return (
            <div key={lineIdx} className="flex items-start gap-2 pl-2">
              <span className="text-zinc-500 mt-1 select-none">•</span>
              <p className="flex-1 text-[13.5px] leading-relaxed text-zinc-200">{renderedTokens}</p>
            </div>
          );
        }

        return (
          <p key={lineIdx} className="text-[13.5px] leading-relaxed text-zinc-200">
            {renderedTokens}
          </p>
        );
      })}
    </div>
  );
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

function ToolExecutionAccordion({
  toolCalls,
  thoughts,
  thoughtDuration,
  isStreamingTurn,
  onViewDiff,
}: {
  toolCalls: ToolCallChip[];
  thoughts?: string[];
  thoughtDuration?: number;
  isStreamingTurn?: boolean;
  onViewDiff?: (filePath: string) => void;
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

  let summaryTitle = `Executed ${toolCalls.length} tool${toolCalls.length !== 1 ? "s" : ""}`;
  if (editCount > 0 && fileCount === 0 && folderCount === 0 && searchCount === 0 && symbolCount === 0) {
    summaryTitle = `Edited ${editCount} file${editCount > 1 ? "s" : ""}`;
  } else if (symbolCount > 0 && editCount === 0 && fileCount === 0 && folderCount === 0 && searchCount === 0) {
    summaryTitle = `Analyzing symbols (${symbolCount} lookup${symbolCount > 1 ? "s" : ""})`;
  } else if (sandboxCount > 0 && editCount === 0 && fileCount === 0 && folderCount === 0 && searchCount === 0 && symbolCount === 0) {
    summaryTitle = `Running sandbox (${sandboxCount} command${sandboxCount > 1 ? "s" : ""})`;
  } else if (webCount > 0 && editCount === 0 && fileCount === 0 && folderCount === 0 && searchCount === 0 && symbolCount === 0 && sandboxCount === 0) {
    summaryTitle = `Searching web (${webCount} request${webCount > 1 ? "s" : ""})`;
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
// Chat Bubble (Matches SS2 user pill + clean assistant presentation)
// ---------------------------------------------------------------------------

function ChatBubble({
  message,
  isLatestStreaming,
  onViewDiff,
}: {
  message: ChatMessage;
  isLatestStreaming?: boolean;
  onViewDiff?: (filePath: string) => void;
}) {
  const [copied, setCopied] = useState(false);
  const [reaction, setReaction] = useState<"up" | "down" | null>(null);

  const isUser = message.role === "user";
  const isSystem = message.role === "system";

  if (isSystem) {
    return (
      <div className="my-2 flex items-center justify-center">
        <div className="rounded-full border border-zinc-800 bg-zinc-900/60 px-3.5 py-1 text-[11px] font-mono text-zinc-500">
          {message.content}
        </div>
      </div>
    );
  }

  if (isUser) {
    return (
      <div className="flex justify-end my-5">
        <div className="max-w-[85%] rounded-2xl border border-zinc-800/80 bg-[#18181c] px-4 py-3 shadow-sm">
          <p className="text-sm font-sans text-zinc-100 leading-relaxed whitespace-pre-wrap">
            {message.content}
          </p>
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

  const hasToolCalls = message.toolCalls && message.toolCalls.length > 0;
  const hasThoughts = message.thoughts && message.thoughts.length > 0;

  return (
    <div className="my-5 space-y-2">
      {/* If tools were executed, render tool accordion (with nested thoughts if any) */}
      {hasToolCalls ? (
        <ToolExecutionAccordion
          toolCalls={message.toolCalls!}
          thoughts={message.thoughts}
          thoughtDuration={message.thoughtDurationSeconds}
          isStreamingTurn={isLatestStreaming}
          onViewDiff={onViewDiff}
        />
      ) : hasThoughts ? (
        /* Otherwise render thought accordion by itself */
        <ThoughtAccordion
          thoughts={message.thoughts!}
          durationSeconds={message.thoughtDurationSeconds}
          isStreaming={isLatestStreaming && !message.content}
        />
      ) : null}

      {/* Main text content */}
      {message.content && (
        <div className="pt-1">
          <MarkdownContent content={message.content} />
        </div>
      )}

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

    </div>
  );
}

// ---------------------------------------------------------------------------
// Terminal Drawer — collapsible live terminal output panel
// ---------------------------------------------------------------------------

function TerminalDrawer({ logs }: { logs: string[] }) {
  const [open, setOpen] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to bottom when new chunks arrive.
  useEffect(() => {
    if (open) {
      bottomRef.current?.scrollIntoView({ behavior: "smooth" });
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
      >
        <div className="flex items-center gap-2">
          <Terminal className="h-3.5 w-3.5 text-emerald-400/80" />
          <span className="text-zinc-300 font-medium">Terminal</span>
          <span className="rounded-full bg-emerald-500/10 border border-emerald-500/20 px-1.5 py-px text-[10px] font-mono text-emerald-400">
            {logs.length} chunk{logs.length !== 1 ? "s" : ""}
          </span>
        </div>
        <ChevronUp
          className={`h-3.5 w-3.5 text-zinc-500 transition-transform duration-200 ${
            open ? "" : "rotate-180"
          }`}
        />
      </button>

      {/* Output pane */}
      {open && (
        <div className="max-h-52 overflow-y-auto px-4 py-3 bg-[#060608]">
          <pre
            id="terminal-output-content"
            className="text-[11px] font-mono text-emerald-200/80 leading-relaxed whitespace-pre-wrap break-words"
          >
            {fullOutput}
          </pre>
          <div ref={bottomRef} />
        </div>
      )}
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
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="relative w-full max-w-lg rounded-2xl border border-zinc-800 bg-gradient-to-b from-[#14141a] to-[#0d0d11] p-6 shadow-[0_24px_64px_rgba(0,0,0,0.8)]">
        <div className="flex items-center justify-between mb-5">
          <div className="flex items-center gap-2.5">
            <div className="flex h-8 w-8 items-center justify-center rounded-xl border border-violet-500/30 bg-violet-500/10 text-violet-400">
              <GitPullRequest className="h-4 w-4" />
            </div>
            <h2 className="text-sm font-semibold text-zinc-100">Commit & Open PR</h2>
          </div>
          <button
            onClick={onClose}
            className="flex h-7 w-7 items-center justify-center rounded-lg text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800/60 transition-colors"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-1.5">
            <label className="block text-[11px] font-medium text-zinc-400 uppercase tracking-wide font-mono">
              PR Title *
            </label>
            <input
              id="commit-title-input"
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="fix: resolve null check in auth middleware"
              maxLength={255}
              className="w-full rounded-xl border border-zinc-700/60 bg-zinc-900/90 px-3.5 py-2.5 text-sm text-zinc-100 placeholder-zinc-500 focus:outline-none focus:ring-1 focus:ring-violet-500/50 transition-all font-sans"
              autoFocus
            />
          </div>

          <div className="space-y-1.5">
            <label className="block text-[11px] font-medium text-zinc-400 uppercase tracking-wide font-mono">
              Description <span className="text-zinc-600 normal-case">(optional)</span>
            </label>
            <textarea
              id="commit-body-input"
              value={body}
              onChange={(e) => setBody(e.target.value)}
              placeholder="Summary of changes and rationale…"
              rows={4}
              maxLength={65535}
              className="w-full resize-y rounded-xl border border-zinc-700/60 bg-zinc-900/90 px-3.5 py-2.5 text-sm text-zinc-100 placeholder-zinc-500 focus:outline-none focus:ring-1 focus:ring-violet-500/50 transition-all font-mono"
            />
          </div>

          {error && (
            <div className="flex items-center gap-2 rounded-xl border border-red-500/30 bg-red-500/10 px-3.5 py-2 text-xs text-red-300">
              <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
              {error}
            </div>
          )}

          <div className="flex items-center gap-3 pt-3">
            <button
              type="button"
              onClick={onClose}
              className="flex-1 rounded-xl border border-zinc-700/60 bg-zinc-800/60 px-4 py-2.5 text-xs font-medium text-zinc-300 hover:bg-zinc-700/60 hover:text-zinc-100 transition-colors"
            >
              Cancel
            </button>
            <button
              id="commit-submit-btn"
              type="submit"
              disabled={loading}
              className="flex-1 flex items-center justify-center gap-2 rounded-xl border border-violet-500/40 bg-gradient-to-b from-violet-600 to-violet-700 px-4 py-2.5 text-xs font-semibold text-white shadow-lg hover:from-violet-500 hover:to-violet-600 active:translate-y-px transition-all disabled:opacity-50 disabled:cursor-not-allowed"
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
// Client Component
// ---------------------------------------------------------------------------

export default function SessionWorkspaceClient({ sessionId: propSessionId }: { sessionId?: string } = {}) {
  const searchParams = useSearchParams();
  const sessionId = propSessionId || searchParams.get("id") || "";

  const [session, setSession] = useState<SessionOut | null>(null);
  const [pageLoading, setPageLoading] = useState(true);
  const [pageError, setPageError] = useState<string | null>(null);

  // View mode switcher: "chat" (default - Screenshot 2 style), "diffs", "split"
  const [viewMode, setViewMode] = useState<"chat" | "diffs" | "split">("chat");

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

  // Chat & Stream
  const {
    messages,
    stagedPatches,
    isStreaming,
    terminalLogs,
    sendChatMessage,
    stopStreaming,
    setStagedPatches,
    setMessages,
  } = useSessionStream(sessionId);

  const [chatInput, setChatInput] = useState("");
  const chatBottomRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // -------------------------------------------------------------------------
  // Load session on mount
  // -------------------------------------------------------------------------

  useEffect(() => {
    if (!sessionId) {
      setPageLoading(false);
      setPageError("No session ID specified.");
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
        // Hydrate chat history from DB — skip tool messages (internal LLM plumbing).
        if (s.conversation_history && s.conversation_history.length > 0) {
          const hydrated: import("@/hooks/useSessionStream").ChatMessage[] = s.conversation_history
            .filter((m) => m.role === "user" || m.role === "assistant")
            .map((m) => ({
              role: m.role as "user" | "assistant",
              content: typeof m.content === "string" ? m.content : "",
            }));
          if (hydrated.length > 0) setMessages(hydrated);
        }
      })
      .catch((err) => {
        const msg = err instanceof ApiError ? err.message : "Failed to load session.";
        setPageError(msg);
      })
      .finally(() => setPageLoading(false));
  }, [sessionId, setStagedPatches, setMessages]);

  // Auto-scroll chat to bottom on new messages
  useEffect(() => {
    chatBottomRef.current?.scrollIntoView({ behavior: "smooth" });
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

  const handleSendChat = useCallback(
    async (textToSend?: string) => {
      const msg = (textToSend ?? chatInput).trim();
      if (!msg || isStreaming) return;
      setChatInput("");
      if (textareaRef.current) {
        textareaRef.current.style.height = "auto";
      }
      await sendChatMessage(msg, {
        model: selectedModelId || undefined,
        provider: selectedProvider || undefined,
      });
    },
    [chatInput, isStreaming, sendChatMessage, selectedModelId, selectedProvider]
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

  // -------------------------------------------------------------------------
  // Rendering helpers
  // -------------------------------------------------------------------------

  const patchFiles = Object.keys(stagedPatches);
  const activePatch = activeFile ? stagedPatches[activeFile] ?? "" : "";
  const { original: monacoOriginal, modified: monacoModified } = parseDiffForMonaco(activePatch);

  // -------------------------------------------------------------------------
  // Loading / error states
  // -------------------------------------------------------------------------

  if (pageLoading) {
    return (
      <AppLayout title="Session" subtitle="Loading workspace…">
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
          <Link href="/sessions" className="text-xs font-mono text-amber-400 hover:underline">
            Back to sessions
          </Link>
        </div>
      </AppLayout>
    );
  }

  const isActive = session.status === "active";

  // Topbar actions: View switchers, Verify, Commit PR, Close
  const topbarActions = (
    <div className="flex items-center gap-2">
      {/* View Switcher: Chat (Screenshot 2 style) | Diffs | Split */}
      <div className="flex items-center rounded-xl border border-zinc-800 bg-zinc-900/80 p-0.5">
        <button
          onClick={() => setViewMode("chat")}
          className={`flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-medium transition-all ${
            viewMode === "chat"
              ? "bg-zinc-800 text-zinc-100 shadow-sm"
              : "text-zinc-400 hover:text-zinc-200"
          }`}
          title="Chat view"
        >
          <MessageSquare className="h-3.5 w-3.5" />
          <span>Chat</span>
        </button>

        <button
          onClick={() => setViewMode("diffs")}
          className={`flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-medium transition-all ${
            viewMode === "diffs"
              ? "bg-zinc-800 text-zinc-100 shadow-sm"
              : "text-zinc-400 hover:text-zinc-200"
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
          className={`flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-medium transition-all ${
            viewMode === "split"
              ? "bg-zinc-800 text-zinc-100 shadow-sm"
              : "text-zinc-400 hover:text-zinc-200"
          }`}
          title="Side-by-side split view"
        >
          <Columns className="h-3.5 w-3.5" />
          <span>Split</span>
        </button>
      </div>

      <div className="h-4 w-px bg-zinc-800 mx-1" />

      {/* Status chip */}
      <StatusChip status={session.status} />

      {/* Verify sandbox button */}
      {isActive && (
        <button
          id="verify-sandbox-btn"
          onClick={handleVerify}
          disabled={sandboxLoading || patchFiles.length === 0}
          className="flex items-center gap-1.5 rounded-xl border border-zinc-700/60 bg-zinc-800/70 px-3 py-1.5 text-xs font-medium text-zinc-300 hover:text-zinc-100 hover:bg-zinc-700/60 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          title={patchFiles.length === 0 ? "Stage a patch to run verification" : "Run tests in sandbox"}
        >
          {sandboxLoading ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin text-amber-400" />
          ) : (
            <FlaskConical className="h-3.5 w-3.5 text-amber-400" />
          )}
          <span>{sandboxLoading ? "Running…" : "Run Tests"}</span>
        </button>
      )}

      {/* Commit & PR button */}
      {isActive && (
        <button
          id="commit-pr-btn"
          onClick={() => setShowCommitModal(true)}
          disabled={patchFiles.length === 0}
          className="flex items-center gap-1.5 rounded-xl border border-violet-500/40 bg-gradient-to-b from-violet-600 to-violet-700 px-3 py-1.5 text-xs font-semibold text-white shadow-sm hover:from-violet-500 hover:to-violet-600 active:translate-y-px transition-all disabled:opacity-40 disabled:cursor-not-allowed"
        >
          <GitPullRequest className="h-3.5 w-3.5" />
          <span>Commit & PR</span>
        </button>
      )}

      {/* Close session button */}
      {isActive && (
        <button
          id="close-session-btn"
          onClick={handleClose}
          className="flex items-center gap-1 rounded-xl border border-zinc-700/60 bg-zinc-800/60 px-2.5 py-1.5 text-xs font-medium text-zinc-400 hover:text-red-300 hover:border-red-500/30 hover:bg-red-500/10 transition-colors"
          title="Close session"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      )}
    </div>
  );

  return (
    <AppLayout
      title={session.title}
      subtitle={`${session.repo_owner}/${session.repo_name} · ${session.branch_name}`}
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
                {sandboxResult.run_url && (
                  <a
                    href={sandboxResult.run_url}
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
              className={`flex flex-col h-full bg-[#09090b] ${
                viewMode === "split"
                  ? "w-[45%] border-r border-zinc-800"
                  : "w-full"
              }`}
            >
              {/* Scrollable Conversation Stream */}
              <div className="flex-1 overflow-y-auto px-4 md:px-8 py-6">
                <div className="mx-auto max-w-3xl w-full">
                  {/* Empty state / Welcome */}
                  {messages.length === 0 && (
                    <div className="flex flex-col items-center justify-center min-h-[50vh] text-center gap-5 py-12">
                      <div className="flex h-14 w-14 items-center justify-center rounded-2xl border border-zinc-800 bg-[#121216] text-amber-400 shadow-inner">
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
                            className="rounded-xl border border-zinc-800/80 bg-[#121216]/60 p-3 hover:bg-[#18181f] hover:border-zinc-700 transition-all text-left group"
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
                      isLatestStreaming={isStreaming && i === messages.length - 1}
                      onViewDiff={handleViewDiffForFile}
                    />
                  ))}

                  <div ref={chatBottomRef} className="h-4" />
                </div>
              </div>

              {/* ============================================================ */}
              {/* TERMINAL DRAWER — live streaming command output               */}
              {/* ============================================================ */}
              <TerminalDrawer logs={terminalLogs} />

              {/* ============================================================ */}
              {/* SINGLE INPUT BAR (Matches Screenshot 2)                     */}
              {/* ============================================================ */}
              <div className="flex-none w-full bg-gradient-to-t from-[#09090b] via-[#09090b]/95 to-transparent pt-3 pb-5 px-4 md:px-8">
                <div className="mx-auto max-w-3xl w-full relative">
                  {/* Floating Action Menu Popover (when "+" is clicked) */}
                  {actionMenuOpen && (
                    <div className="absolute bottom-[calc(100%+8px)] left-3 z-30 w-56 rounded-2xl border border-zinc-800 bg-[#14141a] p-1.5 shadow-2xl backdrop-blur-xl">
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
                        disabled={patchFiles.length === 0}
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
                    </div>
                  )}

                  {/* Model Selector Popover */}
                  {modelPickerOpen && (
                    <div className="absolute bottom-[calc(100%+10px)] left-12 z-30 w-80 rounded-2xl border border-zinc-800/80 bg-[#111115] shadow-[0_24px_64px_rgba(0,0,0,0.8)] backdrop-blur-xl overflow-hidden">
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
                  <div className="relative rounded-2xl border border-zinc-700/60 bg-[#121216]/95 backdrop-blur-xl p-3 shadow-2xl transition-all focus-within:border-zinc-500/80 focus-within:ring-1 focus-within:ring-zinc-600/30">
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
                          className={`flex h-7 w-7 items-center justify-center rounded-lg border transition-all ${
                            actionMenuOpen
                              ? "border-amber-500/40 bg-amber-500/15 text-amber-300"
                              : "border-zinc-700/60 bg-zinc-800/60 text-zinc-400 hover:bg-zinc-700/60 hover:text-zinc-200"
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
                          className={`flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-mono transition-all ${
                            modelPickerOpen
                              ? "bg-zinc-800 text-zinc-100 border border-zinc-700"
                              : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/60"
                          }`}
                        >
                          {/* Provider indicator dot */}
                          <span
                            className={`inline-block h-1.5 w-1.5 rounded-full shrink-0 ${
                              selectedProvider === "groq"
                                ? "bg-orange-400"
                                : selectedProvider === "anthropic"
                                ? "bg-violet-400"
                                : selectedProvider === "openai"
                                ? "bg-emerald-400"
                                : "bg-amber-400"
                            }`}
                          />
                          <span className="truncate max-w-[140px]">{selectedModelId}</span>
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
                            onClick={stopStreaming}
                            className="flex h-8 w-8 items-center justify-center rounded-lg border border-red-500/40 bg-red-500/20 text-red-300 hover:bg-red-500/30 transition-all shadow-sm"
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
                                ? "bg-amber-500 text-zinc-950 font-bold shadow-md hover:bg-amber-400 active:translate-y-px"
                                : "bg-zinc-800/70 text-zinc-500 cursor-not-allowed"
                            }`}
                            title="Send message"
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
          {/* ============================================================== */}
          {(viewMode === "diffs" || viewMode === "split") && (
            <div
              className={`flex flex-col h-full bg-[#0d0d0f] ${
                viewMode === "split" ? "flex-1" : "w-full"
              }`}
            >
              {patchFiles.length > 0 ? (
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
              )}
            </div>
          )}
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
