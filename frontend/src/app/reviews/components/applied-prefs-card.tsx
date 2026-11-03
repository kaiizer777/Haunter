import React, { useState } from "react";
import {
  ShieldAlert,
  Sliders,
  CheckCircle2,
  FileText,
  ChevronDown,
  ChevronRight,
  AlertCircle,
  Tag,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type { AppliedReviewPrefs } from "../types";

export interface AppliedPrefsCardProps {
  /** Applied repository review preferences captured at review analysis time */
  appliedPrefs?: AppliedReviewPrefs | null;
  /** Optional container class name */
  className?: string;
  /** Whether the collapsible sections should be expanded by default (default: false) */
  defaultExpanded?: boolean;
}

/**
 * Strictness badge metadata.
 */
function getStrictnessBadge(strictness?: string | null) {
  const norm = (strictness ?? "").toLowerCase().trim();
  switch (norm) {
    case "high":
    case "strict":
      return {
        label: "Strict",
        className:
          "border-rose-500/40 bg-rose-950/50 text-rose-300 shadow-[inset_0_1px_0_rgba(255,255,255,0.08)]",
        description: "Enforces zero tolerance on security & architecture bugs",
      };
    case "low":
    case "lenient":
      return {
        label: "Lenient",
        className:
          "border-emerald-500/40 bg-emerald-950/50 text-emerald-300 shadow-[inset_0_1px_0_rgba(255,255,255,0.06)]",
        description: "Only flags critical vulnerabilities and breaking flaws",
      };
    case "medium":
    case "balanced":
    default:
      return {
        label: "Balanced",
        className:
          "border-amber-500/40 bg-amber-950/50 text-amber-300 shadow-[inset_0_1px_0_rgba(255,255,255,0.06)]",
        description: "Standard balanced triage of critical and medium issues",
      };
  }
}

/**
 * Repository Review Preferences Card.
 *
 * Displays the strictness tier, tailored focus areas, automated verification
 * checklist, and repository-specific custom review guidelines.
 */
export function AppliedPrefsCard({
  appliedPrefs,
  className,
  defaultExpanded = false,
}: AppliedPrefsCardProps) {
  const [showGuidelines, setShowGuidelines] = useState(defaultExpanded);
  const [showChecklist, setShowChecklist] = useState(defaultExpanded);

  const prefs = appliedPrefs?.prefs;
  const hasPrefs =
    prefs &&
    (prefs.review_strictness ||
      (prefs.review_focus_areas && prefs.review_focus_areas.length > 0) ||
      (prefs.review_checklist && prefs.review_checklist.length > 0) ||
      Boolean(prefs.review_guidelines_text));

  if (!appliedPrefs || !hasPrefs) {
    return (
      <div
        className={cn(
          "rounded-lg border border-zinc-800/80 bg-zinc-950/40 p-4 text-xs text-zinc-400 font-mono shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]",
          className
        )}
      >
        <div className="flex items-center gap-2 text-zinc-400">
          <Sliders className="h-4 w-4 text-zinc-500" />
          <span>Default repository review profile (no custom guidelines applied)</span>
        </div>
      </div>
    );
  }

  const strictness = getStrictnessBadge(prefs.review_strictness);
  const focusAreas = prefs.review_focus_areas ?? [];
  const checklist = prefs.review_checklist ?? [];
  const guidelinesText = prefs.review_guidelines_text?.trim() ?? "";

  return (
    <div
      className={cn(
        "rounded-lg border border-zinc-800/80 bg-zinc-950/60 p-4 space-y-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]",
        className
      )}
    >
      {/* Header bar: Strictness badge + Settings Version + Truncation warning */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800/60 pb-3">
        <div className="flex items-center gap-2.5 flex-wrap">
          <div className="flex items-center gap-1.5 text-xs font-mono font-medium text-zinc-300">
            <Sliders className="h-3.5 w-3.5 text-zinc-400" />
            <span>Repository Review Profile</span>
          </div>

          {/* Strictness badge */}
          <span
            className={cn(
              "inline-flex items-center gap-1 rounded-[4px] border px-2 py-0.5 text-[11px] font-mono font-bold tracking-wide uppercase",
              strictness.className
            )}
            title={strictness.description}
          >
            <ShieldAlert className="h-3 w-3 shrink-0" />
            {strictness.label}
          </span>

          {/* Settings version tag */}
          {appliedPrefs.settings_version != null && (
            <span className="rounded-[4px] border border-zinc-800 bg-zinc-900/90 px-1.5 py-0.5 text-[10px] font-mono text-zinc-400">
              v{appliedPrefs.settings_version}
            </span>
          )}
        </div>

        {/* Truncated notice */}
        {appliedPrefs.truncated && (
          <span
            className="inline-flex items-center gap-1 text-[11px] font-mono text-amber-400 bg-amber-950/30 border border-amber-500/30 px-2 py-0.5 rounded"
            title="Custom guidelines were truncated to fit the LLM context window"
          >
            <AlertCircle className="h-3 w-3" />
            <span>Guidelines Truncated</span>
          </span>
        )}
      </div>

      {/* Focus Areas Tags */}
      {focusAreas.length > 0 && (
        <div className="space-y-1.5">
          <div className="flex items-center gap-1 text-[11px] font-mono font-semibold uppercase tracking-wider text-zinc-400">
            <Tag className="h-3 w-3 text-zinc-500" />
            <span>Targeted Focus Areas</span>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {focusAreas.map((area, idx) => (
              <span
                key={idx}
                className="inline-flex items-center rounded-[5px] border border-zinc-800/90 bg-zinc-900/80 px-2 py-0.5 text-[11px] font-mono text-zinc-200 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]"
              >
                {area}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Review Checklist */}
      {checklist.length > 0 && (
        <div className="space-y-2 border-t border-zinc-800/50 pt-3">
          <button
            type="button"
            onClick={() => setShowChecklist(!showChecklist)}
            aria-expanded={showChecklist}
            className="flex items-center gap-1.5 text-[11px] font-mono font-semibold uppercase tracking-wider text-zinc-400 hover:text-zinc-200 transition-colors focus:outline-none"
          >
            {showChecklist ? (
              <ChevronDown className="h-3.5 w-3.5 text-zinc-500" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5 text-zinc-500" />
            )}
            <span>Custom Review Checklist ({checklist.length})</span>
          </button>

          {showChecklist && (
            <ul className="space-y-1.5 pl-2 animate-in fade-in duration-150">
              {checklist.map((item, idx) => (
                <li
                  key={idx}
                  className="flex items-start gap-2 text-xs font-mono text-zinc-300"
                >
                  <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400 shrink-0 mt-0.5" />
                  <span>{item}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* Custom Guidelines Text Preview */}
      {guidelinesText.length > 0 && (
        <div className="space-y-2 border-t border-zinc-800/50 pt-3">
          <button
            type="button"
            onClick={() => setShowGuidelines(!showGuidelines)}
            aria-expanded={showGuidelines}
            className="flex items-center gap-1.5 text-[11px] font-mono font-semibold uppercase tracking-wider text-zinc-400 hover:text-zinc-200 transition-colors focus:outline-none"
          >
            {showGuidelines ? (
              <ChevronDown className="h-3.5 w-3.5 text-zinc-500" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5 text-zinc-500" />
            )}
            <FileText className="h-3.5 w-3.5 text-zinc-500" />
            <span>Repository Review Guidelines Preview</span>
          </button>

          {showGuidelines && (
            <div className="rounded-md border border-zinc-800/90 bg-[#070709] p-3 text-[11px] font-mono leading-relaxed text-zinc-300 whitespace-pre-wrap select-text max-h-64 overflow-y-auto animate-in fade-in duration-150">
              {guidelinesText}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
