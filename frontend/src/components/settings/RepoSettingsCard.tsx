"use client";

import React, { useState, useEffect, useMemo, useCallback, useRef } from "react";
import {
  Zap,
  ShieldAlert,
  ShieldCheck,
  Sliders,
  TerminalSquare,
  Wrench,
  FlaskConical,
  MessageSquareQuote,
  Globe,
  Bot,
  CheckCircle2,
  AlertCircle,
  RotateCcw,
  Save,
  Check,
  GitBranch,
  DollarSign,
  Gauge,
  SlidersHorizontal,
  Workflow,
  Sparkles,
  Info,
} from "lucide-react";
import { Switch } from "@/components/ui/switch";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { SelectDropdown, SelectOption } from "@/components/ui/select-dropdown";
import {
  RepoSettingsOut,
  RepoSettingsUpdate,
} from "@/lib/api";
import { cn } from "@/lib/utils";

export interface RepoSettingsCardProps {
  settings: RepoSettingsOut;
  repoFullName: string;
  repoId: string;
  onSave: (data: RepoSettingsUpdate) => Promise<RepoSettingsOut>;
  onApplyPreset: (presetName: string) => Promise<RepoSettingsOut>;
  isSaving?: boolean;
}

export interface PresetMeta {
  key: string;
  title: string;
  badge: string;
  description: string;
  icon: React.ComponentType<{ className?: string }>;
  accentColor: string;
}

export const PRESET_OPTIONS: PresetMeta[] = [
  {
    key: "autonomous",
    title: "Autonomous DevOps",
    badge: "Recommended",
    description: "Self-healing CI pipeline. Synthesizes patches, verifies in sandbox runners, and opens surgical PRs.",
    icon: Zap,
    accentColor: "text-amber-400 border-amber-500/30 bg-amber-500/10",
  },
  {
    key: "conservative",
    title: "Conservative Guardian",
    badge: "High Confidence",
    description: "Multi-perspective audit reports with high 90% confidence threshold. Code changes require human sign-off.",
    icon: ShieldAlert,
    accentColor: "text-sky-400 border-sky-500/30 bg-sky-500/10",
  },
  {
    key: "standard",
    title: "Standard Dual-Engine",
    badge: "Balanced",
    description: "Automated CI patch synthesis alongside parallel security, correctness, and performance reviews.",
    icon: Sliders,
    accentColor: "text-indigo-400 border-indigo-500/30 bg-indigo-500/10",
  },
  {
    key: "audit_only",
    title: "Read-Only Auditor",
    badge: "Non-Invasive",
    description: "Zero git modifications or branch creations. Posts in-depth diagnostic annotations on PRs and commits.",
    icon: ShieldCheck,
    accentColor: "text-emerald-400 border-emerald-500/30 bg-emerald-500/10",
  },
  {
    key: "live_studio_only",
    title: "Live Studio Only",
    badge: "Interactive",
    description: "Focused exclusively on in-browser WebContainer live preview and subagent cloud terminal sessions.",
    icon: TerminalSquare,
    accentColor: "text-violet-400 border-violet-500/30 bg-violet-500/10",
  },
  {
    key: "custom",
    title: "Custom Policy",
    badge: "Manual",
    description: "Granular control over all agent triggers, sandbox verification policies, and budget safety bounds.",
    icon: Wrench,
    accentColor: "text-zinc-300 border-zinc-700/50 bg-zinc-800/40",
  },
];

const SCOPE_OPTIONS: SelectOption[] = [
  {
    value: "inherit",
    label: "Global Cluster Default (Inherit)",
    description: "Uses the active LLM engine configured in Model Config dashboard.",
  },
  {
    value: "repo_pinned",
    label: "Repository Pinned",
    description: "Locks this repository to its explicitly pinned model configuration.",
  },
  {
    value: "user_preferred",
    label: "User Preferred",
    description: "Prioritizes user session preference across all diagnostic tasks.",
  },
];

const ILLEGAL_BRANCH_REGEX = /[\s~^:\\]|@{|\.\./;

export function RepoSettingsCard({
  settings: initialSettings,
  repoFullName,
  repoId,
  onSave,
  onApplyPreset,
  isSaving = false,
}: RepoSettingsCardProps) {
  // Local form state
  const [preset, setPreset] = useState<string>(initialSettings.preset || "autonomous");
  const [enableAutoFix, setEnableAutoFix] = useState<boolean>(initialSettings.enable_auto_fix);
  const [enableAuditorMode, setEnableAuditorMode] = useState<boolean>(initialSettings.enable_auditor_mode);
  const [enableSandbox, setEnableSandbox] = useState<boolean>(initialSettings.enable_sandbox_verification);
  const [enablePrComments, setEnablePrComments] = useState<boolean>(initialSettings.enable_pr_comments);
  const [enableLiveSessions, setEnableLiveSessions] = useState<boolean>(initialSettings.enable_live_sessions);
  const [enableWebcontainer, setEnableWebcontainer] = useState<boolean>(initialSettings.enable_webcontainer_preview);
  const [enableSubagents, setEnableSubagents] = useState<boolean>(initialSettings.enable_subagents);

  // Auditor triggers
  const [triggerOnPr, setTriggerOnPr] = useState<boolean>(initialSettings.audit_trigger_on_pr);
  const [triggerOnCiFailure, setTriggerOnCiFailure] = useState<boolean>(initialSettings.audit_trigger_on_ci_failure);
  const [triggerOnCiSuccess, setTriggerOnCiSuccess] = useState<boolean>(initialSettings.audit_trigger_on_ci_success);
  const [triggerOnManualMention, setTriggerOnManualMention] = useState<boolean>(initialSettings.audit_trigger_on_manual_mention);

  // Operational bounds
  const [branchesStr, setBranchesStr] = useState<string>(
    (initialSettings.allowed_branches || ["main", "master"]).join(", ")
  );
  const [ignoreDraftPrs, setIgnoreDraftPrs] = useState<boolean>(initialSettings.ignore_draft_prs);
  const [minConfidence, setMinConfidence] = useState<number>(initialSettings.min_confidence_threshold ?? 80);
  const [maxCostCents, setMaxCostCents] = useState<number>(initialSettings.max_cost_per_run_cents ?? 100);
  const [modelScope, setModelScope] = useState<string>(initialSettings.model_override_scope || "inherit");

  // Feedback banner state
  const [statusMessage, setStatusMessage] = useState<{ type: "success" | "error"; text: string } | null>(null);
  const [isApplyingPreset, setIsApplyingPreset] = useState<string | null>(null);

  // Track previous repo_id to prevent clearing success messages on same-repo settings updates
  const prevRepoId = useRef(initialSettings.repo_id);

  // Sync state when initialSettings changes from upstream (e.g. repo switch)
  useEffect(() => {
    setPreset(initialSettings.preset || "autonomous");
    setEnableAutoFix(initialSettings.enable_auto_fix);
    setEnableAuditorMode(initialSettings.enable_auditor_mode);
    setEnableSandbox(initialSettings.enable_sandbox_verification);
    setEnablePrComments(initialSettings.enable_pr_comments);
    setEnableLiveSessions(initialSettings.enable_live_sessions);
    setEnableWebcontainer(initialSettings.enable_webcontainer_preview);
    setEnableSubagents(initialSettings.enable_subagents);

    setTriggerOnPr(initialSettings.audit_trigger_on_pr);
    setTriggerOnCiFailure(initialSettings.audit_trigger_on_ci_failure);
    setTriggerOnCiSuccess(initialSettings.audit_trigger_on_ci_success);
    setTriggerOnManualMention(initialSettings.audit_trigger_on_manual_mention);

    setBranchesStr((initialSettings.allowed_branches || ["main", "master"]).join(", "));
    setIgnoreDraftPrs(initialSettings.ignore_draft_prs);
    setMinConfidence(initialSettings.min_confidence_threshold ?? 80);
    setMaxCostCents(initialSettings.max_cost_per_run_cents ?? 100);
    setModelScope(initialSettings.model_override_scope || "inherit");

    if (prevRepoId.current !== initialSettings.repo_id) {
      setStatusMessage(null);
      prevRepoId.current = initialSettings.repo_id;
    }
  }, [initialSettings]);

  // Parse branches
  const parsedBranches = useMemo(() => {
    return branchesStr
      .split(",")
      .map((b) => b.trim())
      .filter(Boolean);
  }, [branchesStr]);

  // Branch validation
  const branchValidationError = useMemo(() => {
    if (parsedBranches.length === 0) {
      return "At least one monitored branch is required (e.g. 'main').";
    }
    for (const b of parsedBranches) {
      if (b.length > 255) return `Branch name '${b}' exceeds 255 characters.`;
      if (b.startsWith("/") || b.endsWith("/")) return `Branch '${b}' cannot begin or end with '/'.`;
      if (b.startsWith(".") || b.endsWith(".")) return `Branch '${b}' cannot begin or end with '.'.`;
      if (b.endsWith(".lock")) return `Branch '${b}' cannot end with '.lock'.`;
      if (ILLEGAL_BRANCH_REGEX.test(b)) {
        return `Branch '${b}' contains invalid characters or sequences (~, ^, :, \\, .., spaces).`;
      }
    }
    return null;
  }, [parsedBranches]);

  // Budget validation
  const costValidationError = useMemo(() => {
    if (isNaN(maxCostCents) || maxCostCents < 0) {
      return "Budget cap must be a non-negative number in cents.";
    }
    if (maxCostCents > 10_000_000) {
      return "Budget cap exceeds maximum permitted limit ($100,000 USD).";
    }
    return null;
  }, [maxCostCents]);

  // Check dirty state
  const isDirty = useMemo(() => {
    const initialBranches = (initialSettings.allowed_branches || ["main", "master"]).join(", ");
    return (
      preset !== (initialSettings.preset || "autonomous") ||
      enableAutoFix !== initialSettings.enable_auto_fix ||
      enableAuditorMode !== initialSettings.enable_auditor_mode ||
      enableSandbox !== initialSettings.enable_sandbox_verification ||
      enablePrComments !== initialSettings.enable_pr_comments ||
      enableLiveSessions !== initialSettings.enable_live_sessions ||
      enableWebcontainer !== initialSettings.enable_webcontainer_preview ||
      enableSubagents !== initialSettings.enable_subagents ||
      triggerOnPr !== initialSettings.audit_trigger_on_pr ||
      triggerOnCiFailure !== initialSettings.audit_trigger_on_ci_failure ||
      triggerOnCiSuccess !== initialSettings.audit_trigger_on_ci_success ||
      triggerOnManualMention !== initialSettings.audit_trigger_on_manual_mention ||
      branchesStr !== initialBranches ||
      ignoreDraftPrs !== initialSettings.ignore_draft_prs ||
      minConfidence !== (initialSettings.min_confidence_threshold ?? 80) ||
      maxCostCents !== (initialSettings.max_cost_per_run_cents ?? 100) ||
      modelScope !== (initialSettings.model_override_scope || "inherit")
    );
  }, [
    preset,
    enableAutoFix,
    enableAuditorMode,
    enableSandbox,
    enablePrComments,
    enableLiveSessions,
    enableWebcontainer,
    enableSubagents,
    triggerOnPr,
    triggerOnCiFailure,
    triggerOnCiSuccess,
    triggerOnManualMention,
    branchesStr,
    ignoreDraftPrs,
    minConfidence,
    maxCostCents,
    modelScope,
    initialSettings,
  ]);

  // Handle Preset Click
  const handlePresetSelect = useCallback(
    async (presetKey: string) => {
      if (isSaving || isApplyingPreset) return;
      setIsApplyingPreset(presetKey);
      setStatusMessage(null);

      try {
        const updated = await onApplyPreset(presetKey);
        setPreset(updated.preset || presetKey);
        setEnableAutoFix(updated.enable_auto_fix);
        setEnableAuditorMode(updated.enable_auditor_mode);
        setEnableSandbox(updated.enable_sandbox_verification);
        setEnablePrComments(updated.enable_pr_comments);
        setEnableLiveSessions(updated.enable_live_sessions);
        setEnableWebcontainer(updated.enable_webcontainer_preview);
        setEnableSubagents(updated.enable_subagents);

        setTriggerOnPr(updated.audit_trigger_on_pr);
        setTriggerOnCiFailure(updated.audit_trigger_on_ci_failure);
        setTriggerOnCiSuccess(updated.audit_trigger_on_ci_success);
        setTriggerOnManualMention(updated.audit_trigger_on_manual_mention);

        setMinConfidence(updated.min_confidence_threshold ?? 80);
        setMaxCostCents(updated.max_cost_per_run_cents ?? 100);
        setStatusMessage({
          type: "success",
          text: `Applied '${presetKey}' governance preset profile successfully.`,
        });
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : "Failed to apply preset.";
        setStatusMessage({ type: "error", text: msg });
      } finally {
        setIsApplyingPreset(null);
      }
    },
    [isSaving, isApplyingPreset, onApplyPreset]
  );

  // Handle Save
  const handleSave = useCallback(async () => {
    if (branchValidationError || costValidationError) return;
    setStatusMessage(null);

    const payload: RepoSettingsUpdate = {
      preset,
      enable_auto_fix: enableAutoFix,
      enable_auditor_mode: enableAuditorMode,
      enable_sandbox_verification: enableSandbox,
      enable_pr_comments: enablePrComments,
      enable_live_sessions: enableLiveSessions,
      enable_webcontainer_preview: enableWebcontainer,
      enable_subagents: enableSubagents,
      audit_trigger_on_pr: triggerOnPr,
      audit_trigger_on_ci_failure: triggerOnCiFailure,
      audit_trigger_on_ci_success: triggerOnCiSuccess,
      audit_trigger_on_manual_mention: triggerOnManualMention,
      allowed_branches: parsedBranches,
      ignore_draft_prs: ignoreDraftPrs,
      min_confidence_threshold: minConfidence,
      max_cost_per_run_cents: maxCostCents,
      model_override_scope: modelScope,
    };

    try {
      const res = await onSave(payload);
      setStatusMessage({
        type: "success",
        text: `Repository governance settings saved (v${res.settings_version}).`,
      });
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Failed to save settings.";
      setStatusMessage({ type: "error", text: msg });
    }
  }, [
    branchValidationError,
    costValidationError,
    preset,
    enableAutoFix,
    enableAuditorMode,
    enableSandbox,
    enablePrComments,
    enableLiveSessions,
    enableWebcontainer,
    enableSubagents,
    triggerOnPr,
    triggerOnCiFailure,
    triggerOnCiSuccess,
    triggerOnManualMention,
    parsedBranches,
    ignoreDraftPrs,
    minConfidence,
    maxCostCents,
    modelScope,
    onSave,
  ]);

  // Handle Reset / Discard
  const handleReset = useCallback(() => {
    setPreset(initialSettings.preset || "autonomous");
    setEnableAutoFix(initialSettings.enable_auto_fix);
    setEnableAuditorMode(initialSettings.enable_auditor_mode);
    setEnableSandbox(initialSettings.enable_sandbox_verification);
    setEnablePrComments(initialSettings.enable_pr_comments);
    setEnableLiveSessions(initialSettings.enable_live_sessions);
    setEnableWebcontainer(initialSettings.enable_webcontainer_preview);
    setEnableSubagents(initialSettings.enable_subagents);

    setTriggerOnPr(initialSettings.audit_trigger_on_pr);
    setTriggerOnCiFailure(initialSettings.audit_trigger_on_ci_failure);
    setTriggerOnCiSuccess(initialSettings.audit_trigger_on_ci_success);
    setTriggerOnManualMention(initialSettings.audit_trigger_on_manual_mention);

    setBranchesStr((initialSettings.allowed_branches || ["main", "master"]).join(", "));
    setIgnoreDraftPrs(initialSettings.ignore_draft_prs);
    setMinConfidence(initialSettings.min_confidence_threshold ?? 80);
    setMaxCostCents(initialSettings.max_cost_per_run_cents ?? 100);
    setModelScope(initialSettings.model_override_scope || "inherit");
    setStatusMessage(null);
  }, [initialSettings]);

  // Auto-switch to "custom" if a toggle is modified that differs from named preset
  const handleCustomToggle = useCallback(
    (setter: (val: boolean) => void, nextVal: boolean) => {
      setter(nextVal);
      if (preset !== "custom") {
        setPreset("custom");
      }
    },
    [preset]
  );

  return (
    <div className="space-y-6">
      {/* Status Banner */}
      {statusMessage && (
        <div
          role="alert"
          className={cn(
            "flex items-center justify-between rounded-lg p-3.5 text-xs font-medium border shadow-sm transition-all duration-200",
            statusMessage.type === "success"
              ? "bg-emerald-950/40 border-emerald-500/40 text-emerald-200"
              : "bg-red-950/40 border-red-500/40 text-red-200"
          )}
        >
          <div className="flex items-center gap-2.5">
            {statusMessage.type === "success" ? (
              <CheckCircle2 className="h-4 w-4 text-emerald-400 shrink-0" />
            ) : (
              <AlertCircle className="h-4 w-4 text-red-400 shrink-0" />
            )}
            <span>{statusMessage.text}</span>
          </div>
          <button
            type="button"
            onClick={() => setStatusMessage(null)}
            className="text-zinc-400 hover:text-zinc-100 text-xs ml-4"
          >
            Dismiss
          </button>
        </div>
      )}

      {/* SECTION 1: Governance Preset Cards */}
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-xs font-mono font-semibold uppercase tracking-wider text-zinc-300">
              Governance Presets
            </h3>
            <p className="text-xs text-zinc-500 mt-0.5">
              1-click configuration templates balancing autonomy, auditor inspection, and developer control.
            </p>
          </div>
          <span className="text-[11px] font-mono text-zinc-400">
            Active: <strong className="text-amber-400 uppercase font-bold">{preset}</strong>
          </span>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {PRESET_OPTIONS.map((opt) => {
            const isSelected = preset === opt.key;
            const Icon = opt.icon;
            const isLoading = isApplyingPreset === opt.key;

            return (
              <button
                key={opt.key}
                type="button"
                onClick={() => handlePresetSelect(opt.key)}
                disabled={isSaving || isApplyingPreset !== null}
                className={cn(
                  "relative flex flex-col justify-between text-left p-4 rounded-xl border transition-all duration-150 select-none group",
                  isSelected
                    ? "border-t border-t-amber-400/80 border-x border-x-amber-500/40 border-b border-b-amber-600/30 bg-gradient-to-b from-[#181820] via-[#121217] to-[#0c0c10] shadow-[inset_0_1px_0_rgba(255,255,255,0.15),0_4px_20px_rgba(245,158,11,0.12)] -translate-y-0.5"
                    : "border-zinc-800/80 bg-[#101014]/90 hover:bg-[#14141a] hover:border-zinc-700/80 text-zinc-400 hover:text-zinc-200 hover:-translate-y-0.5 shadow-sm"
                )}
              >
                <div>
                  <div className="flex items-center justify-between mb-2">
                    <div className="flex items-center gap-2">
                      <div
                        className={cn(
                          "flex h-7 w-7 items-center justify-center rounded-lg border",
                          isSelected
                            ? "border-amber-400/50 bg-amber-400/10 text-amber-300 shadow-[0_0_10px_rgba(251,191,36,0.3)]"
                            : "border-zinc-800 bg-zinc-900 text-zinc-400 group-hover:text-zinc-200"
                        )}
                      >
                        <Icon className="h-4 w-4" />
                      </div>
                      <span
                        className={cn(
                          "text-xs font-semibold tracking-tight",
                          isSelected ? "text-zinc-100" : "text-zinc-300 group-hover:text-zinc-100"
                        )}
                      >
                        {opt.title}
                      </span>
                    </div>

                    <span
                      className={cn(
                        "rounded-[4px] px-1.5 py-0.5 text-[10px] font-mono font-medium border",
                        isSelected
                          ? "border-amber-500/40 bg-amber-500/10 text-amber-300 shadow-[0_0_8px_rgba(245,158,11,0.2)]"
                          : "border-zinc-800 bg-zinc-900/80 text-zinc-500"
                      )}
                    >
                      {opt.badge}
                    </span>
                  </div>

                  <p className="text-[11px] text-zinc-400 leading-relaxed min-h-[34px]">
                    {opt.description}
                  </p>
                </div>

                <div className="mt-3 pt-2.5 border-t border-zinc-800/60 flex items-center justify-between text-[10px] font-mono">
                  <span className={isSelected ? "text-amber-400 font-semibold" : "text-zinc-500"}>
                    {isLoading ? "Applying..." : isSelected ? "✓ Active Profile" : "Click to apply"}
                  </span>
                  {isSelected && (
                    <span className="relative flex h-2 w-2">
                      <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-60" />
                      <span className="relative inline-flex rounded-full h-2 w-2 bg-amber-400" />
                    </span>
                  )}
                </div>
              </button>
            );
          })}
        </div>
      </div>

      {/* SECTION 2: Core Capabilities & Feature Toggles */}
      <div className="rounded-xl border border-zinc-800/80 bg-gradient-to-b from-[#121216]/95 via-[#0e0e12]/95 to-[#0a0a0d]/95 p-4 sm:p-5 shadow-sm space-y-4">
        <div>
          <h3 className="text-xs font-mono font-semibold uppercase tracking-wider text-zinc-300">
            Core Autonomous Capabilities
          </h3>
          <p className="text-xs text-zinc-500 mt-0.5">
            Enable or restrict specific agent roles and runtime execution environments.
          </p>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-3.5 pt-1">
          {/* Autonomous Fixer */}
          <div className="flex items-start justify-between gap-4 p-3.5 rounded-lg border border-zinc-800/70 bg-[#121216]/80 hover:border-zinc-700/80 transition-all">
            <div className="flex items-start gap-3">
              <div className="flex h-8 w-8 items-center justify-center rounded-lg border border-amber-500/30 bg-amber-500/10 text-amber-400 shrink-0 mt-0.5">
                <Zap className="h-4 w-4" />
              </div>
              <div>
                <label
                  htmlFor="toggle-auto-fix"
                  className="text-xs font-semibold text-zinc-200 block cursor-pointer select-none"
                >
                  Autonomous Fix Generator & Auto-PR
                </label>
                <p className="text-[11px] text-zinc-400 leading-relaxed mt-0.5">
                  Diagnoses CI workflow failures, generates surgical code patches, and opens verified pull requests.
                </p>
              </div>
            </div>
            <Switch
              id="toggle-auto-fix"
              checked={enableAutoFix}
              onCheckedChange={(val) => handleCustomToggle(setEnableAutoFix, val)}
              aria-label="Autonomous Fix Generator & Auto-PR"
            />
          </div>

          {/* Multi-Perspective Auditor */}
          <div className="flex items-start justify-between gap-4 p-3.5 rounded-lg border border-zinc-800/70 bg-[#121216]/80 hover:border-zinc-700/80 transition-all">
            <div className="flex items-start gap-3">
              <div className="flex h-8 w-8 items-center justify-center rounded-lg border border-sky-500/30 bg-sky-500/10 text-sky-400 shrink-0 mt-0.5">
                <ShieldCheck className="h-4 w-4" />
              </div>
              <div>
                <label
                  htmlFor="toggle-auditor-mode"
                  className="text-xs font-semibold text-zinc-200 block cursor-pointer select-none"
                >
                  Multi-Perspective Auditor Bot
                </label>
                <p className="text-[11px] text-zinc-400 leading-relaxed mt-0.5">
                  Conducts parallel security, correctness, and architecture reviews without modifying source branches.
                </p>
              </div>
            </div>
            <Switch
              id="toggle-auditor-mode"
              checked={enableAuditorMode}
              onCheckedChange={(val) => handleCustomToggle(setEnableAuditorMode, val)}
              aria-label="Multi-Perspective Auditor Bot"
            />
          </div>

          {/* CI Mirror Sandbox */}
          <div className="flex items-start justify-between gap-4 p-3.5 rounded-lg border border-zinc-800/70 bg-[#121216]/80 hover:border-zinc-700/80 transition-all">
            <div className="flex items-start gap-3">
              <div className="flex h-8 w-8 items-center justify-center rounded-lg border border-emerald-500/30 bg-emerald-500/10 text-emerald-400 shrink-0 mt-0.5">
                <FlaskConical className="h-4 w-4" />
              </div>
              <div>
                <label
                  htmlFor="toggle-sandbox"
                  className="text-xs font-semibold text-zinc-200 block cursor-pointer select-none"
                >
                  CI Mirror Sandbox Runner
                </label>
                <p className="text-[11px] text-zinc-400 leading-relaxed mt-0.5">
                  Validates patches inside ephemeral GitHub Actions runners before committing or publishing PRs.
                </p>
              </div>
            </div>
            <Switch
              id="toggle-sandbox"
              checked={enableSandbox}
              onCheckedChange={(val) => handleCustomToggle(setEnableSandbox, val)}
              aria-label="CI Mirror Sandbox Runner"
            />
          </div>

          {/* PR & Commit Comments */}
          <div className="flex items-start justify-between gap-4 p-3.5 rounded-lg border border-zinc-800/70 bg-[#121216]/80 hover:border-zinc-700/80 transition-all">
            <div className="flex items-start gap-3">
              <div className="flex h-8 w-8 items-center justify-center rounded-lg border border-indigo-500/30 bg-indigo-500/10 text-indigo-400 shrink-0 mt-0.5">
                <MessageSquareQuote className="h-4 w-4" />
              </div>
              <div>
                <label
                  htmlFor="toggle-pr-comments"
                  className="text-xs font-semibold text-zinc-200 block cursor-pointer select-none"
                >
                  PR & Commit Annotations
                </label>
                <p className="text-[11px] text-zinc-400 leading-relaxed mt-0.5">
                  Publishes structured diagnostic root-cause summaries and line-by-line review comments on GitHub.
                </p>
              </div>
            </div>
            <Switch
              id="toggle-pr-comments"
              checked={enablePrComments}
              onCheckedChange={(val) => handleCustomToggle(setEnablePrComments, val)}
              aria-label="PR & Commit Annotations"
            />
          </div>

          {/* Interactive Live Sessions */}
          <div className="flex items-start justify-between gap-4 p-3.5 rounded-lg border border-zinc-800/70 bg-[#121216]/80 hover:border-zinc-700/80 transition-all">
            <div className="flex items-start gap-3">
              <div className="flex h-8 w-8 items-center justify-center rounded-lg border border-violet-500/30 bg-violet-500/10 text-violet-400 shrink-0 mt-0.5">
                <TerminalSquare className="h-4 w-4" />
              </div>
              <div>
                <label
                  htmlFor="toggle-live-sessions"
                  className="text-xs font-semibold text-zinc-200 block cursor-pointer select-none"
                >
                  Interactive Live Cloud Sessions
                </label>
                <p className="text-[11px] text-zinc-400 leading-relaxed mt-0.5">
                  Allows interactive developer sessions with bidirectional streaming and slash command execution.
                </p>
              </div>
            </div>
            <Switch
              id="toggle-live-sessions"
              checked={enableLiveSessions}
              onCheckedChange={(val) => handleCustomToggle(setEnableLiveSessions, val)}
              aria-label="Interactive Live Cloud Sessions"
            />
          </div>

          {/* WebContainer In-Browser Dev Server */}
          <div className="flex items-start justify-between gap-4 p-3.5 rounded-lg border border-zinc-800/70 bg-[#121216]/80 hover:border-zinc-700/80 transition-all">
            <div className="flex items-start gap-3">
              <div className="flex h-8 w-8 items-center justify-center rounded-lg border border-teal-500/30 bg-teal-500/10 text-teal-400 shrink-0 mt-0.5">
                <Globe className="h-4 w-4" />
              </div>
              <div>
                <label
                  htmlFor="toggle-webcontainer"
                  className="text-xs font-semibold text-zinc-200 block cursor-pointer select-none"
                >
                  In-Browser WebContainer Preview
                </label>
                <p className="text-[11px] text-zinc-400 leading-relaxed mt-0.5">
                  Boots real Node.js dev servers in-browser with live hot reload for immediate UI validation.
                </p>
              </div>
            </div>
            <Switch
              id="toggle-webcontainer"
              checked={enableWebcontainer}
              onCheckedChange={(val) => handleCustomToggle(setEnableWebcontainer, val)}
              aria-label="In-Browser WebContainer Preview"
            />
          </div>

          {/* Subagent Task Delegation */}
          <div className="flex items-start justify-between gap-4 p-3.5 rounded-lg border border-zinc-800/70 bg-[#121216]/80 hover:border-zinc-700/80 transition-all md:col-span-2">
            <div className="flex items-start gap-3">
              <div className="flex h-8 w-8 items-center justify-center rounded-lg border border-amber-500/30 bg-amber-500/10 text-amber-400 shrink-0 mt-0.5">
                <Bot className="h-4 w-4" />
              </div>
              <div>
                <label
                  htmlFor="toggle-subagents"
                  className="text-xs font-semibold text-zinc-200 block cursor-pointer select-none"
                >
                  Subagent Task Delegation Engine
                </label>
                <p className="text-[11px] text-zinc-400 leading-relaxed mt-0.5">
                  Empowers orchestrators to spawn autonomous specialized subagents for security scans, diff analysis, and unit test generation.
                </p>
              </div>
            </div>
            <Switch
              id="toggle-subagents"
              checked={enableSubagents}
              onCheckedChange={(val) => handleCustomToggle(setEnableSubagents, val)}
              aria-label="Subagent Task Delegation Engine"
            />
          </div>
        </div>
      </div>

      {/* SECTION 3: Auditor Bot Triggers */}
      <div className="rounded-xl border border-zinc-800/80 bg-gradient-to-b from-[#121216]/95 via-[#0e0e12]/95 to-[#0a0a0d]/95 p-4 sm:p-5 shadow-sm space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-xs font-mono font-semibold uppercase tracking-wider text-zinc-300">
              Auditor Bot Activation Triggers
            </h3>
            <p className="text-xs text-zinc-500 mt-0.5">
              Specify GitHub webhook events that trigger autonomous code audits and reviews.
            </p>
          </div>
          {!enableAuditorMode && (
            <span className="text-[10px] font-mono text-zinc-500 bg-zinc-900 border border-zinc-800 px-2 py-0.5 rounded">
              Auditor Bot is currently OFF
            </span>
          )}
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 pt-1">
          {/* PR Trigger */}
          <div className="flex items-center justify-between p-3 rounded-lg border border-zinc-800/60 bg-[#121216]/60">
            <div>
              <span className="text-xs font-medium text-zinc-200 block">Trigger on Pull Requests</span>
              <span className="text-[10px] font-mono text-zinc-500">PR opened or commits synchronized</span>
            </div>
            <Switch
              checked={triggerOnPr}
              onCheckedChange={(val) => handleCustomToggle(setTriggerOnPr, val)}
              aria-label="Trigger on Pull Requests"
            />
          </div>

          {/* CI Failure Trigger */}
          <div className="flex items-center justify-between p-3 rounded-lg border border-zinc-800/60 bg-[#121216]/60">
            <div>
              <span className="text-xs font-medium text-zinc-200 block">Trigger on CI Failures</span>
              <span className="text-[10px] font-mono text-zinc-500">GitHub Actions workflow_run failure</span>
            </div>
            <Switch
              checked={triggerOnCiFailure}
              onCheckedChange={(val) => handleCustomToggle(setTriggerOnCiFailure, val)}
              aria-label="Trigger on CI Failures"
            />
          </div>

          {/* CI Success Trigger */}
          <div className="flex items-center justify-between p-3 rounded-lg border border-zinc-800/60 bg-[#121216]/60">
            <div>
              <span className="text-xs font-medium text-zinc-200 block">Trigger on Successful CI Runs</span>
              <span className="text-[10px] font-mono text-zinc-500">Verification scan on green builds</span>
            </div>
            <Switch
              checked={triggerOnCiSuccess}
              onCheckedChange={(val) => handleCustomToggle(setTriggerOnCiSuccess, val)}
              aria-label="Trigger on Successful CI Runs"
            />
          </div>

          {/* Manual Mention Trigger */}
          <div className="flex items-center justify-between p-3 rounded-lg border border-zinc-800/60 bg-[#121216]/60">
            <div>
              <span className="text-xs font-medium text-zinc-200 block">Trigger on @haunter Mention</span>
              <span className="text-[10px] font-mono text-zinc-500">On-demand review comment trigger</span>
            </div>
            <Switch
              checked={triggerOnManualMention}
              onCheckedChange={(val) => handleCustomToggle(setTriggerOnManualMention, val)}
              aria-label="Trigger on @haunter Mention"
            />
          </div>
        </div>
      </div>

      {/* SECTION 4: Operational Governance & Policy Bounds */}
      <div className="rounded-xl border border-zinc-800/80 bg-gradient-to-b from-[#121216]/95 via-[#0e0e12]/95 to-[#0a0a0d]/95 p-4 sm:p-5 shadow-sm space-y-4">
        <div>
          <h3 className="text-xs font-mono font-semibold uppercase tracking-wider text-zinc-300">
            Operational Governance & Safety Bounds
          </h3>
          <p className="text-xs text-zinc-500 mt-0.5">
            Strict policy constraints preventing runaway token usage and limiting execution to permitted branches.
          </p>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 pt-1">
          {/* Monitored Branches */}
          <div className="space-y-1.5 md:col-span-2">
            <label htmlFor="input-branches" className="text-xs font-medium text-zinc-300 flex items-center gap-1.5">
              <GitBranch className="h-3.5 w-3.5 text-zinc-400" />
              Monitored Branches (Comma-separated)
            </label>
            <Input
              id="input-branches"
              value={branchesStr}
              onChange={(e) => {
                setBranchesStr(e.target.value);
                if (preset !== "custom") setPreset("custom");
              }}
              placeholder="main, master, release/*"
              className={cn(
                "font-mono text-xs",
                branchValidationError && "border-red-500/80 focus-visible:ring-red-400"
              )}
            />
            {branchValidationError ? (
              <p role="alert" className="text-[11px] text-red-400 font-mono flex items-center gap-1">
                <AlertCircle className="h-3 w-3 shrink-0" />
                {branchValidationError}
              </p>
            ) : (
              <p className="text-[10px] font-mono text-zinc-500">
                Only events affecting these branches trigger auto-fixers or audits. Supports wildcards (e.g. <code className="text-zinc-400">release/*</code>).
              </p>
            )}
          </div>

          {/* Ignore Draft PRs */}
          <div className="flex items-center justify-between p-3.5 rounded-lg border border-zinc-800/60 bg-[#121216]/60">
            <div>
              <span className="text-xs font-medium text-zinc-200 block">Ignore Draft Pull Requests</span>
              <span className="text-[10px] font-mono text-zinc-500">Suppress agent actions while PR is in draft</span>
            </div>
            <Switch
              checked={ignoreDraftPrs}
              onCheckedChange={(val) => handleCustomToggle(setIgnoreDraftPrs, val)}
              aria-label="Ignore Draft Pull Requests"
            />
          </div>

          {/* Model Scope */}
          <div className="space-y-1.5">
            <label className="text-xs font-medium text-zinc-300 flex items-center gap-1.5">
              <Workflow className="h-3.5 w-3.5 text-zinc-400" />
              Model Override Scope
            </label>
            <SelectDropdown
              value={modelScope}
              onChange={(val) => {
                setModelScope(val);
                if (preset !== "custom") setPreset("custom");
              }}
              options={SCOPE_OPTIONS}
              aria-label="Model Override Scope"
            />
          </div>

          {/* Confidence Threshold */}
          <div className="space-y-2 p-3.5 rounded-lg border border-zinc-800/60 bg-[#121216]/60">
            <div className="flex items-center justify-between">
              <label htmlFor="slider-confidence" className="text-xs font-medium text-zinc-300 flex items-center gap-1.5">
                <Gauge className="h-3.5 w-3.5 text-zinc-400" />
                Min Confidence Threshold
              </label>
              <div className="flex items-center gap-1.5">
                <span className="font-mono text-xs font-bold text-amber-400">{minConfidence}%</span>
                <span
                  className={cn(
                    "text-[9px] font-mono px-1 py-0.2 rounded border",
                    minConfidence >= 85
                      ? "border-emerald-500/40 text-emerald-300 bg-emerald-500/10"
                      : minConfidence >= 75
                      ? "border-sky-500/40 text-sky-300 bg-sky-500/10"
                      : "border-amber-500/40 text-amber-300 bg-amber-500/10"
                  )}
                >
                  {minConfidence >= 85 ? "Strict" : minConfidence >= 75 ? "Balanced" : "Permissive"}
                </span>
              </div>
            </div>
            <input
              id="slider-confidence"
              type="range"
              min={0}
              max={100}
              step={5}
              value={minConfidence}
              onChange={(e) => {
                setMinConfidence(parseInt(e.target.value, 10));
                if (preset !== "custom") setPreset("custom");
              }}
              className="w-full h-1.5 bg-zinc-800 rounded-lg appearance-none cursor-pointer accent-amber-400"
            />
            <p className="text-[10px] font-mono text-zinc-500">
              Patches with confidence scores below this threshold are suppressed or logged as comments only.
            </p>
          </div>

          {/* Max Cost per Run */}
          <div className="space-y-2 p-3.5 rounded-lg border border-zinc-800/60 bg-[#121216]/60">
            <div className="flex items-center justify-between">
              <label htmlFor="input-cost-cents" className="text-xs font-medium text-zinc-300 flex items-center gap-1.5">
                <DollarSign className="h-3.5 w-3.5 text-zinc-400" />
                Max Budget Cap per Run
              </label>
              <span className="font-mono text-xs font-bold text-emerald-400">
                ${(maxCostCents / 100).toFixed(2)} USD
              </span>
            </div>
            <Input
              id="input-cost-cents"
              type="number"
              min={0}
              max={10000000}
              step={10}
              value={maxCostCents}
              onChange={(e) => {
                setMaxCostCents(parseInt(e.target.value || "0", 10));
                if (preset !== "custom") setPreset("custom");
              }}
              className="font-mono text-xs"
            />
            {costValidationError ? (
              <p role="alert" className="text-[11px] text-red-400 font-mono">
                {costValidationError}
              </p>
            ) : (
              <p className="text-[10px] font-mono text-zinc-500">
                Hard ceiling in cents. Runs hitting this cap abort before spawning further reasoning calls.
              </p>
            )}
          </div>
        </div>
      </div>

      {/* ACTION BAR: Save & Feedback */}
      <div className="sticky bottom-4 z-20 flex items-center justify-between rounded-xl border border-zinc-700/60 bg-[#0d0d10]/95 backdrop-blur-md p-3.5 shadow-[0_8px_32px_rgba(0,0,0,0.6)]">
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2">
            <span
              className={cn(
                "h-2 w-2 rounded-full",
                isDirty ? "bg-amber-400 animate-pulse" : "bg-emerald-400"
              )}
            />
            <span className="text-xs font-mono text-zinc-300">
              {isDirty ? "Unsaved policy changes" : "All policies in sync"}
            </span>
          </div>
          <span className="hidden sm:inline text-zinc-600">|</span>
          <span className="hidden sm:inline text-[11px] font-mono text-zinc-500">
            Version v{initialSettings.settings_version || 1}
          </span>
        </div>

        <div className="flex items-center gap-2.5">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={handleReset}
            disabled={!isDirty || isSaving}
            className="text-xs text-zinc-400 hover:text-zinc-200"
          >
            <RotateCcw className="h-3.5 w-3.5 mr-1.5" />
            Reset
          </Button>

          <Button
            type="button"
            size="sm"
            onClick={handleSave}
            disabled={!isDirty || isSaving || !!branchValidationError || !!costValidationError}
            className={cn(
              "text-xs font-semibold px-4 transition-all duration-150",
              "bg-gradient-to-b from-amber-400 via-amber-500 to-amber-600 text-zinc-950",
              "border-t border-t-amber-200/40 border-x border-x-amber-500/40 border-b border-b-amber-700/40",
              "shadow-[inset_0_1px_0_rgba(255,255,255,0.4),0_2px_8px_rgba(245,158,11,0.25)]",
              "hover:from-amber-300 hover:via-amber-400 hover:to-amber-500 active:translate-y-[0.5px]",
              "disabled:opacity-40 disabled:cursor-not-allowed"
            )}
          >
            {isSaving ? (
              <>
                <RotateCcw className="h-3.5 w-3.5 mr-1.5 animate-spin" />
                Saving...
              </>
            ) : (
              <>
                <Save className="h-3.5 w-3.5 mr-1.5" />
                Save Governance Policy
              </>
            )}
          </Button>
        </div>
      </div>
    </div>
  );
}
