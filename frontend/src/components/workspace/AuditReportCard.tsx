"use client";

/**
 * AuditReportCard — Studio-grade live & post-audit report card component.
 *
 * Implements Phase 7.2 & 7.3 specifications:
 * - Live scanning progress state with dynamic bar and perspective/file counters.
 * - Health score radial ring / badge (0-100) with color grading.
 * - Confidence metric and target label chips.
 * - Executive summary with typography hierarchy.
 * - Filterable findings list (Blocker/Critical, Warning/High, Note/Medium/Low).
 * - Expandable details with diff-syntax highlighted patch previews.
 * - 1-Click "Stage Surgical Fix" button with painted-light depth and loading/staged transitions.
 */

import React, { useState, useMemo } from "react";
import {
  ShieldAlert,
  ShieldCheck,
  AlertTriangle,
  Info,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  ExternalLink,
  Copy,
  Check,
  Loader2,
  Sparkles,
  Wrench,
  FileCode2,
  Filter,
} from "lucide-react";
import type {
  AuditCardState,
  AuditFinding,
  AuditReportEvent,
} from "@/hooks/useSessionStream";

export interface AuditReportCardProps {
  scan: AuditCardState;
  onStageFix?: (finding: AuditFinding, diff?: string) => Promise<void> | void;
  onViewDiff?: (filePath: string) => void;
  className?: string;
}

type SeverityFilter = "all" | "blocker" | "warning" | "note";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function normalizeSeverity(sev?: string): "BLOCKER" | "WARNING" | "NOTE" {
  if (!sev) return "NOTE";
  const upper = sev.toUpperCase();
  if (upper === "BLOCKER" || upper === "CRITICAL" || upper === "FATAL") return "BLOCKER";
  if (upper === "WARNING" || upper === "HIGH" || upper === "WARN") return "WARNING";
  return "NOTE";
}

function getHealthScoreColor(score: number): {
  stroke: string;
  badgeBg: string;
  badgeText: string;
  badgeBorder: string;
  label: string;
} {
  if (score >= 85) {
    return {
      stroke: "#10b981", // emerald-500
      badgeBg: "bg-emerald-500/10",
      badgeText: "text-emerald-400",
      badgeBorder: "border-emerald-500/30",
      label: "Excellent",
    };
  }
  if (score >= 65) {
    return {
      stroke: "#f59e0b", // amber-500
      badgeBg: "bg-amber-500/10",
      badgeText: "text-amber-400",
      badgeBorder: "border-amber-500/30",
      label: "Action Recommended",
    };
  }
  return {
    stroke: "#f43f5e", // rose-500
    badgeBg: "bg-rose-500/10",
    badgeText: "text-rose-400",
    badgeBorder: "border-rose-500/30",
    label: "Critical Attention",
  };
}

// ---------------------------------------------------------------------------
// Health Score Radial Ring
// ---------------------------------------------------------------------------

function HealthScoreRing({ score }: { score: number }) {
  const clampedScore = Math.max(0, Math.min(100, Math.round(score)));
  const theme = getHealthScoreColor(clampedScore);
  const radius = 22;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference - (clampedScore / 100) * circumference;

  return (
    <div
      className="flex items-center gap-3 rounded-lg border border-zinc-800 bg-[#121217] px-3 py-2 shadow-inner"
      role="group"
      aria-label={`Health Score ${clampedScore} out of 100 (${theme.label})`}
    >
      <div className="relative flex h-14 w-14 items-center justify-center shrink-0">
        <svg className="h-14 w-14 -rotate-90" viewBox="0 0 54 54">
          <circle
            cx="27"
            cy="27"
            r={radius}
            strokeWidth="4"
            className="stroke-zinc-800 fill-transparent"
          />
          <circle
            cx="27"
            cy="27"
            r={radius}
            strokeWidth="4"
            stroke={theme.stroke}
            fill="transparent"
            strokeDasharray={circumference}
            strokeDashoffset={offset}
            strokeLinecap="round"
            className="transition-all duration-700 ease-out"
          />
        </svg>
        <span className="absolute font-mono text-sm font-bold text-zinc-100">
          {clampedScore}
        </span>
      </div>
      <div className="flex flex-col">
        <span className="text-[11px] font-mono uppercase tracking-wider text-zinc-400">
          Health Score
        </span>
        <span className={`text-xs font-semibold ${theme.badgeText}`}>
          {theme.label}
        </span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Diff Snippet Box with Syntax Highlight & Copy
// ---------------------------------------------------------------------------

function DiffSnippetBox({ diff }: { diff: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(diff);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const lines = diff.split("\n");

  return (
    <div className="my-2.5 overflow-hidden rounded-lg border border-zinc-800/90 bg-[#09090c] text-xs font-mono">
      <div className="flex items-center justify-between border-b border-zinc-800/70 bg-[#131318] px-3 py-1.5 text-zinc-400">
        <span className="text-[11px] text-zinc-400">Remediation Patch</span>
        <button
          onClick={handleCopy}
          aria-label="Copy remediation diff"
          className="flex items-center gap-1 rounded px-2 py-0.5 text-[10px] text-zinc-400 transition-colors hover:bg-zinc-800 hover:text-zinc-200"
        >
          {copied ? (
            <Check className="h-3 w-3 text-emerald-400" />
          ) : (
            <Copy className="h-3 w-3" />
          )}
          <span>{copied ? "Copied" : "Copy"}</span>
        </button>
      </div>
      <div className="max-h-60 overflow-x-auto p-2.5 font-mono text-[11.5px] leading-relaxed">
        {lines.map((line, idx) => {
          let lineClass = "text-zinc-300";
          if (line.startsWith("+") && !line.startsWith("+++")) {
            lineClass = "bg-emerald-950/40 text-emerald-300 font-medium px-1 rounded-sm";
          } else if (line.startsWith("-") && !line.startsWith("---")) {
            lineClass = "bg-rose-950/40 text-rose-300 font-medium px-1 rounded-sm";
          } else if (line.startsWith("@@")) {
            lineClass = "text-cyan-400/90";
          }
          return (
            <div key={idx} className={`${lineClass} whitespace-pre`}>
              {line || " "}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Finding Item Component
// ---------------------------------------------------------------------------

function FindingCard({
  finding,
  onStageFix,
  onViewDiff,
}: {
  finding: AuditFinding;
  onStageFix?: (finding: AuditFinding, diff?: string) => Promise<void> | void;
  onViewDiff?: (filePath: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [stageState, setStageState] = useState<"idle" | "loading" | "staged" | "error">("idle");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const normSev = normalizeSeverity(finding.severity);

  let sevBadgeClass = "border-zinc-700/60 bg-zinc-800/80 text-zinc-300";
  let SevIcon = Info;
  if (normSev === "BLOCKER") {
    sevBadgeClass = "border-rose-500/40 bg-rose-500/10 text-rose-300";
    SevIcon = ShieldAlert;
  } else if (normSev === "WARNING") {
    sevBadgeClass = "border-amber-500/40 bg-amber-500/10 text-amber-300";
    SevIcon = AlertTriangle;
  }

  const patchText = finding.remediation_diff || finding.suggested_fix || "";

  const handleStage = async () => {
    if (stageState === "loading" || stageState === "staged") return;
    setStageState("loading");
    setErrorMessage(null);
    try {
      await onStageFix?.(finding, patchText);
      setStageState("staged");
    } catch (err) {
      setStageState("error");
      setErrorMessage(err instanceof Error ? err.message : "Failed to stage fix");
    }
  };

  const lineRangeText = useMemo(() => {
    if (typeof finding.line_start === "number") {
      if (typeof finding.line_end === "number" && finding.line_end !== finding.line_start) {
        return `:${finding.line_start}-${finding.line_end}`;
      }
      return `:${finding.line_start}`;
    }
    if (typeof finding.line_number === "number") {
      return `:${finding.line_number}`;
    }
    return "";
  }, [finding.line_start, finding.line_end, finding.line_number]);

  return (
    <div
      className="rounded-lg border border-zinc-800/90 bg-[#101015] p-3.5 transition-colors hover:border-zinc-750"
      data-testid={`finding-${finding.id || finding.file_path}`}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          {/* Severity Badge */}
          <span
            className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-mono font-medium ${sevBadgeClass}`}
          >
            <SevIcon className="h-3 w-3 shrink-0" />
            {finding.severity?.toUpperCase() || normSev}
          </span>

          {/* Finding ID / Category */}
          {finding.id && (
            <span className="rounded bg-zinc-800/90 px-1.5 py-0.5 font-mono text-[11px] text-zinc-300">
              {finding.id}
            </span>
          )}
          {finding.category && (
            <span className="text-[11px] font-mono text-zinc-400">
              {finding.category}
            </span>
          )}
        </div>

        {/* File Path & Line Info */}
        <div className="flex items-center gap-2">
          <span
            className="font-mono text-xs text-zinc-300 hover:text-zinc-100"
            title={`${finding.file_path}${lineRangeText}`}
          >
            {finding.file_path}
            <span className="text-zinc-500">{lineRangeText}</span>
          </span>
          {onViewDiff && (
            <button
              onClick={() => onViewDiff(finding.file_path)}
              aria-label={`Open ${finding.file_path} in editor`}
              title="Open file in editor"
              className="rounded p-1 text-zinc-400 transition-colors hover:bg-zinc-800 hover:text-zinc-200"
            >
              <ExternalLink className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
      </div>

      {/* Title */}
      {finding.title && (
        <h4 className="mt-2 text-sm font-medium text-zinc-100">
          {finding.title}
        </h4>
      )}

      {/* Description */}
      <p className="mt-1.5 text-xs text-zinc-300 leading-relaxed">
        {finding.description}
      </p>

      {/* Expand / View Fix Action */}
      {patchText && (
        <div className="mt-3">
          <button
            onClick={() => setExpanded((prev) => !prev)}
            aria-expanded={expanded}
            className="inline-flex items-center gap-1 text-xs font-mono text-violet-400 hover:text-violet-300 transition-colors"
          >
            {expanded ? (
              <ChevronDown className="h-3.5 w-3.5" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5" />
            )}
            <span>{expanded ? "Hide suggested fix" : "View suggested fix"}</span>
          </button>

          {expanded && <DiffSnippetBox diff={patchText} />}
        </div>
      )}

      {/* Actions Bar */}
      <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-zinc-800/80 pt-2.5">
        <div className="text-[11px] text-zinc-400 font-mono">
          {finding.perspective && <span>Perspective: {finding.perspective}</span>}
        </div>

        <div className="flex items-center gap-2">
          {/* 1-Click Stage Surgical Fix button with painted-light depth */}
          <button
            onClick={handleStage}
            disabled={stageState === "loading" || stageState === "staged"}
            aria-label={`Stage surgical fix for finding ${finding.id || finding.file_path}`}
            className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium font-sans transition-all duration-150 ${
              stageState === "staged"
                ? "border border-emerald-500/40 bg-emerald-500/15 text-emerald-300 cursor-default"
                : stageState === "loading"
                  ? "border border-violet-500/40 bg-violet-600/40 text-violet-200 cursor-wait"
                  : "border-t border-t-violet-400/40 border-x border-x-violet-500/30 border-b border-b-violet-700/50 bg-gradient-to-b from-violet-600 to-violet-700 text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.25),0_2px_4px_rgba(0,0,0,0.3)] hover:brightness-110 active:translate-y-[1px]"
            }`}
          >
            {stageState === "loading" ? (
              <>
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                <span>Staging Fix…</span>
              </>
            ) : stageState === "staged" ? (
              <>
                <Check className="h-3.5 w-3.5 text-emerald-400" />
                <span>Staged ✓</span>
              </>
            ) : (
              <>
                <Wrench className="h-3.5 w-3.5" />
                <span>Stage Surgical Fix</span>
              </>
            )}
          </button>
        </div>
      </div>

      {errorMessage && (
        <p className="mt-2 text-[11px] text-rose-400 font-mono" role="alert">
          {errorMessage}
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main AuditReportCard Component
// ---------------------------------------------------------------------------

export function AuditReportCard({
  scan,
  onStageFix,
  onViewDiff,
  className = "",
}: AuditReportCardProps) {
  const [filter, setFilter] = useState<SeverityFilter>("all");
  const [globalStageState, setGlobalStageState] = useState<
    "idle" | "loading" | "staged" | "error"
  >("idle");

  const isScanning = scan.status === "scanning";
  const report = scan.report;

  // Calculate counts
  const findings = report?.findings || [];
  const blockerCount = findings.filter(
    (f) => normalizeSeverity(f.severity) === "BLOCKER"
  ).length;
  const warningCount = findings.filter(
    (f) => normalizeSeverity(f.severity) === "WARNING"
  ).length;
  const noteCount = findings.filter(
    (f) => normalizeSeverity(f.severity) === "NOTE"
  ).length;

  const filteredFindings = useMemo(() => {
    if (filter === "all") return findings;
    if (filter === "blocker") {
      return findings.filter((f) => normalizeSeverity(f.severity) === "BLOCKER");
    }
    if (filter === "warning") {
      return findings.filter((f) => normalizeSeverity(f.severity) === "WARNING");
    }
    return findings.filter((f) => normalizeSeverity(f.severity) === "NOTE");
  }, [findings, filter]);

  // Handle Global Remediation Diff staging if present
  const handleStageGlobalFix = async () => {
    if (!report?.remediation_diff) return;
    setGlobalStageState("loading");
    try {
      const topFinding = findings[0] || {
        id: "GLOBAL",
        severity: "BLOCKER",
        file_path: "multi-file",
        description: "Global remediation patch",
      };
      await onStageFix?.(topFinding, report.remediation_diff);
      setGlobalStageState("staged");
    } catch {
      setGlobalStageState("error");
    }
  };

  // -------------------------------------------------------------------------
  // Progress State View
  // -------------------------------------------------------------------------

  if (isScanning) {
    const progress = scan.progress || {
      completed: 0,
      total: 1,
      statusText: "Analyzing repository…",
    };
    const completed = progress.completed;
    const total = Math.max(progress.total, 1);
    const percent = Math.min(100, Math.round((completed / total) * 100));

    return (
      <div
        className={`my-3 overflow-hidden rounded-xl border border-zinc-800 bg-[#0e0e13] p-4 shadow-md ${className}`}
        role="region"
        aria-label="Audit Scan in Progress"
        data-testid="audit-scanning-view"
      >
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2.5">
            <span className="relative flex h-3 w-3 shrink-0">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-cyan-400 opacity-60" />
              <span className="relative inline-flex rounded-full h-3 w-3 bg-cyan-400 shadow-[0_0_8px_rgba(34,211,238,0.8)]" />
            </span>
            <h3 className="font-mono text-sm font-semibold text-zinc-100">
              {scan.target_label || "Repository Audit"}
            </h3>
            <span className="rounded-full border border-cyan-500/30 bg-cyan-500/10 px-2 py-0.5 font-mono text-[10px] text-cyan-300">
              Scanning…
            </span>
          </div>
          <span className="font-mono text-xs font-semibold text-zinc-300">
            {percent}%
          </span>
        </div>

        {/* Progress Bar */}
        <div className="mt-3 h-2 w-full overflow-hidden rounded-full bg-zinc-800/80">
          <div
            className="h-full bg-gradient-to-r from-cyan-500 to-violet-500 transition-all duration-300 ease-out"
            style={{ width: `${Math.max(percent, 5)}%` }}
            role="progressbar"
            aria-valuenow={percent}
            aria-valuemin={0}
            aria-valuemax={100}
          />
        </div>

        {/* Status text */}
        <div className="mt-2.5 flex items-center justify-between text-xs font-mono text-zinc-400">
          <span className="truncate">
            {progress.current ? (
              <>Current: <strong className="text-zinc-200">{progress.current}</strong></>
            ) : (
              progress.statusText || "Scanning files…"
            )}
          </span>
          <span className="shrink-0 text-zinc-400">
            {completed} / {total}
          </span>
        </div>

        {/* Perspectives chips if present */}
        {scan.perspectives && scan.perspectives.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-1.5 border-t border-zinc-800/70 pt-2.5">
            {scan.perspectives.map((p) => (
              <span
                key={p}
                className="rounded border border-zinc-700/60 bg-zinc-800/60 px-2 py-0.5 font-mono text-[10px] text-zinc-400"
              >
                {p}
              </span>
            ))}
          </div>
        )}
      </div>
    );
  }

  // -------------------------------------------------------------------------
  // Report View (Completed)
  // -------------------------------------------------------------------------

  const healthScore = report?.health_score ?? (findings.length === 0 ? 100 : Math.max(20, 100 - blockerCount * 30 - warningCount * 10));
  const confidence = report?.confidence;
  const summaryText = report?.executive_summary || report?.summary || "";

  return (
    <div
      className={`my-3 overflow-hidden rounded-xl border border-zinc-800 bg-[#0e0e13] p-4.5 shadow-lg ${className}`}
      role="region"
      aria-label="Audit Scan Report"
      data-testid="audit-report-view"
    >
      {/* Top Header */}
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-zinc-800/80 pb-3.5">
        <div className="space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <span aria-hidden="true" className="text-lg">🛡️</span>
            <h3 className="font-sans text-sm font-semibold tracking-tight text-zinc-100">
              {scan.target_label || "Repository Audit Report"}
            </h3>
            {confidence !== undefined && (
              <span className="rounded-full border border-violet-500/30 bg-violet-500/10 px-2 py-0.5 font-mono text-[11px] text-violet-300">
                {confidence}% Confidence
              </span>
            )}
            <span className="rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 font-mono text-[10px] text-emerald-300">
              Done
            </span>
          </div>

          {/* Quick Counter Pills */}
          <div className="flex flex-wrap items-center gap-2 pt-1 font-mono text-xs">
            <span
              className={`rounded px-2 py-0.5 ${
                blockerCount > 0
                  ? "bg-rose-500/15 text-rose-300 font-medium"
                  : "bg-zinc-800/80 text-zinc-400"
              }`}
            >
              {blockerCount} Blocker{blockerCount !== 1 ? "s" : ""}
            </span>
            <span
              className={`rounded px-2 py-0.5 ${
                warningCount > 0
                  ? "bg-amber-500/15 text-amber-300 font-medium"
                  : "bg-zinc-800/80 text-zinc-400"
              }`}
            >
              {warningCount} Warning{warningCount !== 1 ? "s" : ""}
            </span>
            <span className="rounded bg-zinc-800/80 px-2 py-0.5 text-zinc-400">
              {noteCount} Note{noteCount !== 1 ? "s" : ""}
            </span>
          </div>
        </div>

        {/* Health Score Ring */}
        <HealthScoreRing score={healthScore} />
      </div>

      {/* Executive Summary */}
      {summaryText && (
        <div className="my-3 rounded-lg border border-zinc-800/60 bg-[#121218] p-3 text-xs text-zinc-300 leading-relaxed font-sans">
          <div className="flex items-center gap-1.5 font-mono text-[11px] uppercase tracking-wider text-zinc-400 mb-1">
            <Sparkles className="h-3 w-3 text-amber-400" />
            <span>Executive Summary</span>
          </div>
          <p>{summaryText}</p>
        </div>
      )}

      {/* Global Remediation Diff CTA if present */}
      {report?.remediation_diff && (
        <div className="my-3 flex items-center justify-between rounded-lg border border-violet-500/25 bg-violet-950/20 px-3.5 py-2.5">
          <div className="flex items-center gap-2">
            <FileCode2 className="h-4 w-4 text-violet-400" />
            <div className="flex flex-col">
              <span className="text-xs font-medium text-zinc-200">
                Complete Remediation Patch Available
              </span>
              <span className="text-[11px] font-mono text-zinc-400">
                Addresses all detected issues across target files
              </span>
            </div>
          </div>
          <button
            onClick={handleStageGlobalFix}
            disabled={globalStageState === "loading" || globalStageState === "staged"}
            aria-label="Stage Global Remediation Patch"
            className="inline-flex items-center gap-1.5 rounded-lg border border-violet-400/40 bg-gradient-to-b from-violet-600 to-violet-700 px-3 py-1.5 text-xs font-medium text-white shadow-sm transition-all hover:brightness-110 active:translate-y-[1px]"
          >
            {globalStageState === "loading" ? (
              <>
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                <span>Staging All…</span>
              </>
            ) : globalStageState === "staged" ? (
              <>
                <Check className="h-3.5 w-3.5 text-emerald-400" />
                <span>All Staged ✓</span>
              </>
            ) : (
              <>
                <Wrench className="h-3.5 w-3.5" />
                <span>Stage All Fixes</span>
              </>
            )}
          </button>
        </div>
      )}

      {/* Findings Section */}
      <div className="mt-4">
        {/* Filters Header */}
        <div className="flex flex-wrap items-center justify-between gap-2 pb-2">
          <span className="flex items-center gap-1.5 font-mono text-xs font-semibold text-zinc-300">
            <Filter className="h-3.5 w-3.5 text-zinc-400" />
            Findings & Recommendations ({findings.length})
          </span>

          <div className="flex items-center gap-1 rounded-lg border border-zinc-800 bg-[#121218] p-0.5">
            <button
              onClick={() => setFilter("all")}
              className={`rounded-md px-2.5 py-1 text-[11px] font-mono transition-colors ${
                filter === "all"
                  ? "bg-zinc-800 text-zinc-100 font-semibold shadow-xs"
                  : "text-zinc-400 hover:text-zinc-200"
              }`}
            >
              All ({findings.length})
            </button>
            <button
              onClick={() => setFilter("blocker")}
              className={`rounded-md px-2.5 py-1 text-[11px] font-mono transition-colors ${
                filter === "blocker"
                  ? "bg-rose-500/20 text-rose-300 font-semibold shadow-xs"
                  : "text-zinc-400 hover:text-zinc-200"
              }`}
            >
              Blockers ({blockerCount})
            </button>
            <button
              onClick={() => setFilter("warning")}
              className={`rounded-md px-2.5 py-1 text-[11px] font-mono transition-colors ${
                filter === "warning"
                  ? "bg-amber-500/20 text-amber-300 font-semibold shadow-xs"
                  : "text-zinc-400 hover:text-zinc-200"
              }`}
            >
              Warnings ({warningCount})
            </button>
            <button
              onClick={() => setFilter("note")}
              className={`rounded-md px-2.5 py-1 text-[11px] font-mono transition-colors ${
                filter === "note"
                  ? "bg-sky-500/20 text-sky-300 font-semibold shadow-xs"
                  : "text-zinc-400 hover:text-zinc-200"
              }`}
            >
              Notes ({noteCount})
            </button>
          </div>
        </div>

        {/* Findings List */}
        <div className="mt-2.5 space-y-2.5">
          {filteredFindings.length > 0 ? (
            filteredFindings.map((finding, idx) => (
              <FindingCard
                key={finding.id || `${finding.file_path}-${idx}`}
                finding={finding}
                onStageFix={onStageFix}
                onViewDiff={onViewDiff}
              />
            ))
          ) : (
            <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-zinc-800 py-8 text-center">
              <CheckCircle2 className="h-8 w-8 text-emerald-400/90 mb-2" />
              <p className="text-xs font-medium text-zinc-200">
                {findings.length === 0
                  ? "Zero security vulnerabilities or regressions detected! Clean bill of health."
                  : `No findings matching the "${filter}" filter.`}
              </p>
              <p className="text-[11px] font-mono text-zinc-500 mt-1">
                All checks passed the production readiness threshold.
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
