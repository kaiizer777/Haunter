import React from "react";
import {
  Lock,
  Bug,
  Zap,
  Boxes,
  Layers,
  Sparkles,
  AlertTriangle,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type { PriorityBucket } from "../types";

export interface CategoryMeta {
  icon: LucideIcon;
  color: string;
  badgeClass: string;
  label: string;
}

export interface PriorityMeta {
  key: PriorityBucket;
  label: string;
  badgeClass: string;
  text: string;
  dot: string;
  description: string;
}

export interface SeverityMeta {
  label: string;
  badgeClass: string;
  text: string;
  dot: string;
}

/**
 * Finding Category Icon & Color Helper
 * Maps category names (e.g. security, logic/correctness, performance, api_compatibility, architecture, holistic)
 * to designated tactile iconography and theme tokens.
 */
export function getCategoryMeta(category: string): CategoryMeta {
  const cat = (category ?? "").toLowerCase().trim();
  switch (cat) {
    case "security":
      return {
        icon: Lock,
        color: "text-rose-400 border-rose-500/30 bg-rose-950/40",
        badgeClass:
          "inline-flex items-center gap-1 rounded-[5px] border border-rose-500/30 bg-rose-950/40 px-2 py-0.5 text-[11px] font-mono font-medium text-rose-400 shadow-[inset_0_1px_0_rgba(255,255,255,0.05)]",
        label: "Security",
      };
    case "logic":
    case "correctness":
      return {
        icon: Bug,
        color: "text-amber-400 border-amber-500/30 bg-amber-950/40",
        badgeClass:
          "inline-flex items-center gap-1 rounded-[5px] border border-amber-500/30 bg-amber-950/40 px-2 py-0.5 text-[11px] font-mono font-medium text-amber-400 shadow-[inset_0_1px_0_rgba(255,255,255,0.05)]",
        label: cat === "correctness" ? "Correctness" : "Logic & Edge Cases",
      };
    case "performance":
      return {
        icon: Zap,
        color: "text-cyan-400 border-cyan-500/30 bg-cyan-950/40",
        badgeClass:
          "inline-flex items-center gap-1 rounded-[5px] border border-cyan-500/30 bg-cyan-950/40 px-2 py-0.5 text-[11px] font-mono font-medium text-cyan-400 shadow-[inset_0_1px_0_rgba(255,255,255,0.05)]",
        label: "Performance",
      };
    case "api_compatibility":
      return {
        icon: Boxes,
        color: "text-purple-400 border-purple-500/30 bg-purple-950/40",
        badgeClass:
          "inline-flex items-center gap-1 rounded-[5px] border border-purple-500/30 bg-purple-950/40 px-2 py-0.5 text-[11px] font-mono font-medium text-purple-400 shadow-[inset_0_1px_0_rgba(255,255,255,0.05)]",
        label: "API Compatibility",
      };
    case "architecture":
      return {
        icon: Layers,
        color: "text-indigo-400 border-indigo-500/30 bg-indigo-950/40",
        badgeClass:
          "inline-flex items-center gap-1 rounded-[5px] border border-indigo-500/30 bg-indigo-950/40 px-2 py-0.5 text-[11px] font-mono font-medium text-indigo-400 shadow-[inset_0_1px_0_rgba(255,255,255,0.05)]",
        label: "Architecture",
      };
    case "holistic":
      return {
        icon: Sparkles,
        color: "text-emerald-400 border-emerald-500/30 bg-emerald-950/40",
        badgeClass:
          "inline-flex items-center gap-1 rounded-[5px] border border-emerald-500/30 bg-emerald-950/40 px-2 py-0.5 text-[11px] font-mono font-medium text-emerald-400 shadow-[inset_0_1px_0_rgba(255,255,255,0.05)]",
        label: "Holistic Quality",
      };
    default:
      return {
        icon: AlertTriangle,
        color: "text-zinc-400 border-zinc-700 bg-zinc-900/60",
        badgeClass:
          "inline-flex items-center gap-1 rounded-[5px] border border-zinc-700 bg-zinc-900/60 px-2 py-0.5 text-[11px] font-mono font-medium text-zinc-400",
        label: category || "General",
      };
  }
}

/**
 * Priority Bucket Badge Metadata
 * Must Fix (critical/high blockers), Should Fix (medium), Suggestion (low/style)
 */
export function getPriorityMeta(priority: PriorityBucket): PriorityMeta {
  switch (priority) {
    case "must_fix":
      return {
        key: "must_fix",
        label: "Must Fix",
        badgeClass:
          "rounded-[4px] border border-rose-500/40 bg-rose-950/60 px-1.5 py-0.5 text-[10px] font-mono font-bold uppercase tracking-wider text-rose-400 shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_1px_2px_rgba(0,0,0,0.3)]",
        text: "text-rose-400",
        dot: "bg-rose-400",
        description: "Must-fix blockers or critical defects. Address before merge.",
      };
    case "should_fix":
      return {
        key: "should_fix",
        label: "Should Fix",
        badgeClass:
          "rounded-[4px] border border-amber-500/40 bg-amber-950/60 px-1.5 py-0.5 text-[10px] font-mono font-bold uppercase tracking-wider text-amber-400 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_1px_2px_rgba(0,0,0,0.25)]",
        text: "text-amber-400",
        dot: "bg-amber-400",
        description: "Important improvements or edge cases requiring focused attention.",
      };
    case "suggestion":
      return {
        key: "suggestion",
        label: "Suggestion",
        badgeClass:
          "rounded-[4px] border border-cyan-500/30 bg-cyan-950/40 px-1.5 py-0.5 text-[10px] font-mono font-medium uppercase tracking-wider text-cyan-300 shadow-[inset_0_1px_0_rgba(255,255,255,0.05),0_1px_2px_rgba(0,0,0,0.2)]",
        text: "text-cyan-300",
        dot: "bg-cyan-400",
        description: "Optional polish, non-blocking advice, or minor style suggestions.",
      };
  }
}

/**
 * Severity Badge Metadata
 */
export function getSeverityMeta(severity: string): SeverityMeta {
  const sev = (severity ?? "").toLowerCase().trim();
  switch (sev) {
    case "critical":
      return {
        label: "CRITICAL",
        badgeClass:
          "rounded-[4px] border border-rose-500/40 bg-rose-950/60 px-1.5 py-0.5 text-[10px] font-mono font-bold uppercase tracking-wider text-rose-400 shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_1px_2px_rgba(0,0,0,0.3)]",
        text: "text-rose-400",
        dot: "bg-rose-400",
      };
    case "high":
      return {
        label: "HIGH",
        badgeClass:
          "rounded-[4px] border border-amber-500/40 bg-amber-950/60 px-1.5 py-0.5 text-[10px] font-mono font-bold uppercase tracking-wider text-amber-400 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_1px_2px_rgba(0,0,0,0.25)]",
        text: "text-amber-400",
        dot: "bg-amber-400",
      };
    case "medium":
      return {
        label: "MEDIUM",
        badgeClass:
          "rounded-[4px] border border-yellow-500/30 bg-yellow-950/40 px-1.5 py-0.5 text-[10px] font-mono font-medium uppercase tracking-wider text-yellow-300 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]",
        text: "text-yellow-300",
        dot: "bg-yellow-400",
      };
    default:
      return {
        label: "LOW",
        badgeClass:
          "rounded-[4px] border border-zinc-700 bg-zinc-900/80 px-1.5 py-0.5 text-[10px] font-mono font-medium uppercase tracking-wider text-zinc-400",
        text: "text-zinc-400",
        dot: "bg-zinc-500",
      };
  }
}

/**
 * Presentational Severity Badge Component
 */
export function SeverityBadge({
  severity,
  className,
}: {
  severity: string;
  className?: string;
}) {
  const meta = getSeverityMeta(severity);
  return React.createElement(
    "span",
    { className: cn(meta.badgeClass, className) },
    meta.label
  );
}

/**
 * Presentational Priority Bucket Badge Component
 */
export function PriorityBadge({
  priority,
  className,
}: {
  priority: PriorityBucket;
  className?: string;
}) {
  const meta = getPriorityMeta(priority);
  return React.createElement(
    "span",
    {
      className: cn(meta.badgeClass, className),
      title: meta.description,
    },
    meta.label
  );
}
