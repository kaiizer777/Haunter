"use client";

import React, { Suspense, useEffect, useState, useCallback, useMemo } from "react";
import { useSearchParams } from "next/navigation";
import Link from "next/link";
import { AppLayout } from "@/components/layout/app-layout";
import { RepoSettingsCard } from "@/components/settings/RepoSettingsCard";
import { SelectDropdown, SelectOption } from "@/components/ui/select-dropdown";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import {
  api,
  RepoOut,
  RepoWithSettingsOut,
  RepoSettingsOut,
  RepoSettingsUpdate,
} from "@/lib/api";
import {
  GitBranch,
  ShieldCheck,
  Zap,
  DollarSign,
  Layers,
  AlertCircle,
  RefreshCw,
  FolderGit2,
  ExternalLink,
  Sliders,
  Sparkles,
  Bot,
  TerminalSquare,
  Wrench,
  CheckCircle2,
} from "lucide-react";
import { cn } from "@/lib/utils";

interface UnifiedRepoItem {
  id: string;
  fullName: string;
  presetProfile?: string;
  defaultBranch?: string | null;
}

function SettingsContent() {
  const searchParams = useSearchParams();
  const initialRepoIdFromUrl = searchParams.get("repo_id");

  // Repositories state
  const [repos, setRepos] = useState<UnifiedRepoItem[]>([]);
  const [selectedRepoId, setSelectedRepoId] = useState<string | null>(initialRepoIdFromUrl);
  const [loadingRepos, setLoadingRepos] = useState<boolean>(true);
  const [repoError, setRepoError] = useState<string | null>(null);

  // Active settings state
  const [activeSettings, setActiveSettings] = useState<RepoSettingsOut | null>(null);
  const [loadingSettings, setLoadingSettings] = useState<boolean>(false);
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const [isSaving, setIsSaving] = useState<boolean>(false);
  const [isRefreshing, setIsRefreshing] = useState<boolean>(false);

  // Sync selected repo to URL search params
  const handleSelectRepo = useCallback((repoId: string) => {
    setSelectedRepoId(repoId);
    if (typeof window !== "undefined") {
      const url = new URL(window.location.href);
      if (url.searchParams.get("repo_id") !== repoId) {
        url.searchParams.set("repo_id", repoId);
        window.history.replaceState(null, "", `${url.pathname}?${url.searchParams.toString()}`);
      }
    }
  }, []);

  // 1. Fetch Repositories
  const loadRepos = useCallback(async () => {
    setLoadingRepos(true);
    setRepoError(null);

    try {
      // First attempt FUTURE02 GET /settings/repos
      let unified: UnifiedRepoItem[] = [];
      try {
        const settingsRepos = await api.getSettingsRepos();
        if (settingsRepos && settingsRepos.length > 0) {
          unified = settingsRepos.map((r: RepoWithSettingsOut) => ({
            id: r.repo_id,
            fullName: r.repo_full_name,
            presetProfile: r.preset_profile,
          }));
        }
      } catch {
        // Fallback to standard /repos if settings/repos fails or returns 404
      }

      // If unified is still empty, fallback to GET /repos
      if (unified.length === 0) {
        const standardRepos = await api.getRepos();
        unified = standardRepos.map((r: RepoOut) => ({
          id: r.id,
          fullName: `${r.owner}/${r.name}`,
          defaultBranch: r.default_branch,
        }));
      }

      setRepos(unified);

      // Select initial repo
      if (unified.length > 0) {
        const targetId =
          initialRepoIdFromUrl && unified.some((r) => r.id === initialRepoIdFromUrl)
            ? initialRepoIdFromUrl
            : unified[0].id;
        setSelectedRepoId(targetId);
        if (typeof window !== "undefined") {
          const url = new URL(window.location.href);
          if (url.searchParams.get("repo_id") !== targetId) {
            url.searchParams.set("repo_id", targetId);
            window.history.replaceState(null, "", `${url.pathname}?${url.searchParams.toString()}`);
          }
        }
      } else {
        setSelectedRepoId(null);
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Failed to load connected repositories.";
      setRepoError(msg);
    } finally {
      setLoadingRepos(false);
    }
  }, [initialRepoIdFromUrl]);

  useEffect(() => {
    loadRepos();
  }, [loadRepos]);

  // 2. Fetch Settings for selected repository
  const loadSettingsForRepo = useCallback(async (repoId: string, manualRefresh = false) => {
    if (manualRefresh) {
      setIsRefreshing(true);
    } else {
      setLoadingSettings(true);
    }
    setSettingsError(null);

    try {
      const data = await api.getRepoSettings(repoId);
      setActiveSettings(data);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Failed to fetch repository governance settings.";
      setSettingsError(msg);
    } finally {
      setLoadingSettings(false);
      setIsRefreshing(false);
    }
  }, []);

  useEffect(() => {
    if (selectedRepoId) {
      loadSettingsForRepo(selectedRepoId);
    } else {
      setActiveSettings(null);
    }
  }, [selectedRepoId, loadSettingsForRepo]);

  // Handlers for Save and Preset
  const handleSaveSettings = useCallback(
    async (updateData: RepoSettingsUpdate): Promise<RepoSettingsOut> => {
      if (!selectedRepoId) throw new Error("No repository selected.");
      setIsSaving(true);
      try {
        const updated = await api.updateRepoSettings(selectedRepoId, updateData);
        setActiveSettings(updated);

        // Update preset profile in repos list if changed
        setRepos((prev) =>
          prev.map((r) =>
            r.id === selectedRepoId
              ? { ...r, presetProfile: updated.preset || updateData.preset || r.presetProfile }
              : r
          )
        );
        return updated;
      } finally {
        setIsSaving(false);
      }
    },
    [selectedRepoId]
  );

  const handleApplyPreset = useCallback(
    async (presetName: string): Promise<RepoSettingsOut> => {
      if (!selectedRepoId) throw new Error("No repository selected.");
      const updated = await api.applyRepoPreset(selectedRepoId, presetName);
      setActiveSettings(updated);

      // Sync preset profile in repos list
      setRepos((prev) =>
        prev.map((r) =>
          r.id === selectedRepoId ? { ...r, presetProfile: updated.preset || presetName } : r
        )
      );
      return updated;
    },
    [selectedRepoId]
  );

  // Active Repo Object
  const currentRepo = useMemo(() => {
    return repos.find((r) => r.id === selectedRepoId) || null;
  }, [repos, selectedRepoId]);

  // Select dropdown options
  const repoDropdownOptions: SelectOption[] = useMemo(() => {
    return repos.map((r) => ({
      value: r.id,
      label: r.fullName,
      description: r.presetProfile ? `Profile: ${r.presetProfile}` : undefined,
    }));
  }, [repos]);

  return (
    <AppLayout
      title="Repository Settings"
      subtitle="Governance presets, feature toggles, and operational safety boundaries"
    >
      <div className="space-y-6 max-w-7xl mx-auto pb-12">
        {/* PAGE HEADER */}
        <div className="relative overflow-hidden rounded-xl border border-zinc-800/80 bg-gradient-to-b from-[#141419]/95 via-[#0e0e13]/95 to-[#09090c]/95 p-5 sm:p-6 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_4px_24px_rgba(0,0,0,0.4)]">
          <span
            aria-hidden="true"
            className="pointer-events-none absolute -top-12 -right-12 h-36 w-36 rounded-full bg-amber-500/5 blur-3xl"
          />

          <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 relative z-10">
            <div>
              <div className="flex items-center gap-2">
                <span className="inline-flex items-center gap-1.5 rounded-full border border-amber-500/30 bg-amber-500/10 px-2.5 py-0.5 text-[10px] font-mono font-medium text-amber-300 shadow-[0_0_8px_rgba(245,158,11,0.2)]">
                  <Sliders className="h-3 w-3" />
                  Governance Control Center
                </span>
                <span className="text-[10px] font-mono text-zinc-500">Phase 6.4</span>
              </div>
              <h1 className="mt-2 text-xl sm:text-2xl font-bold tracking-tight text-zinc-100 font-mono">
                Repository Settings & Governance
              </h1>
              <p className="mt-1 text-xs sm:text-sm text-zinc-400 max-w-3xl leading-relaxed">
                Configure autonomous CI healing, multi-agent auditor inspection, isolated mirror verification, and budget caps across your repositories.
              </p>
            </div>

            {/* Quick Actions */}
            <div className="flex items-center gap-2 shrink-0">
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => {
                  if (selectedRepoId) loadSettingsForRepo(selectedRepoId, true);
                  else loadRepos();
                }}
                disabled={isRefreshing || loadingRepos || loadingSettings}
                className="text-xs text-zinc-300 border-zinc-800 bg-zinc-900/60 hover:bg-zinc-800"
              >
                <RefreshCw className={cn("h-3.5 w-3.5 mr-1.5", isRefreshing && "animate-spin")} />
                Refresh
              </Button>
              <Link href="/repos">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="text-xs text-zinc-300 border-zinc-800 bg-zinc-900/60 hover:bg-zinc-800"
                >
                  <FolderGit2 className="h-3.5 w-3.5 mr-1.5" />
                  Manage Repos
                </Button>
              </Link>
            </div>
          </div>
        </div>

        {/* TELEMETRY KPI METRIC SUMMARY CARDS */}
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3.5">
          {/* Active Repository Card */}
          <div className="rounded-lg border border-zinc-800/80 bg-gradient-to-b from-[#121216]/90 to-[#0c0c0e]/90 p-4 shadow-sm flex flex-col justify-between">
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-mono uppercase tracking-wider text-zinc-400">Target Repo</span>
              <FolderGit2 className="h-4 w-4 text-amber-400" />
            </div>
            <div className="mt-2">
              <span className="text-base sm:text-lg font-bold font-mono text-zinc-100 truncate block">
                {currentRepo ? currentRepo.fullName : "None"}
              </span>
            </div>
            <div className="mt-1 flex items-center gap-1.5 text-[11px] text-zinc-500 font-mono truncate">
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
              <span>{repos.length} connected repos</span>
            </div>
          </div>

          {/* Active Preset Profile */}
          <div className="rounded-lg border border-zinc-800/80 bg-gradient-to-b from-[#121216]/90 to-[#0c0c0e]/90 p-4 shadow-sm flex flex-col justify-between">
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-mono uppercase tracking-wider text-zinc-400">Active Profile</span>
              <Zap className="h-4 w-4 text-sky-400" />
            </div>
            <div className="mt-2">
              <span className="text-base sm:text-lg font-bold font-mono text-zinc-100 uppercase truncate block">
                {activeSettings ? activeSettings.preset || "autonomous" : "Loading..."}
              </span>
            </div>
            <div className="mt-1 text-[11px] text-zinc-500 font-mono truncate">
              Version v{activeSettings?.settings_version || 1}
            </div>
          </div>

          {/* Auditor Engine State */}
          <div className="rounded-lg border border-zinc-800/80 bg-gradient-to-b from-[#121216]/90 to-[#0c0c0e]/90 p-4 shadow-sm flex flex-col justify-between">
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-mono uppercase tracking-wider text-zinc-400">Auditor Status</span>
              <ShieldCheck className="h-4 w-4 text-emerald-400" />
            </div>
            <div className="mt-2">
              <span
                className={cn(
                  "text-base sm:text-lg font-bold font-mono truncate block",
                  activeSettings?.enable_auditor_mode ? "text-emerald-400" : "text-zinc-400"
                )}
              >
                {activeSettings?.enable_auditor_mode ? "Active Bot" : "Standby (Off)"}
              </span>
            </div>
            <div className="mt-1 text-[11px] text-zinc-500 font-mono truncate">
              Min confidence: {activeSettings?.min_confidence_threshold ?? 80}%
            </div>
          </div>

          {/* Budget Safety Bounds */}
          <div className="rounded-lg border border-zinc-800/80 bg-gradient-to-b from-[#121216]/90 to-[#0c0c0e]/90 p-4 shadow-sm flex flex-col justify-between">
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-mono uppercase tracking-wider text-zinc-400">Run Budget Cap</span>
              <DollarSign className="h-4 w-4 text-amber-400" />
            </div>
            <div className="mt-2">
              <span className="text-base sm:text-lg font-bold font-mono text-zinc-100 truncate block">
                ${activeSettings ? ((activeSettings.max_cost_per_run_cents ?? 100) / 100).toFixed(2) : "1.00"} USD
              </span>
            </div>
            <div className="mt-1 text-[11px] text-zinc-500 font-mono truncate">
              Hard stop threshold / run
            </div>
          </div>
        </div>

        {/* REPOSITORY SELECTOR BAR */}
        <div className="rounded-xl border border-zinc-800/80 bg-[#101014]/90 p-4 flex flex-col sm:flex-row sm:items-center justify-between gap-3 shadow-sm">
          <div className="flex items-center gap-2.5">
            <span className="text-xs font-mono font-semibold uppercase tracking-wider text-zinc-400 select-none">
              Select Repository:
            </span>
            <div className="w-72 sm:w-80">
              <SelectDropdown
                value={selectedRepoId || ""}
                onChange={handleSelectRepo}
                options={repoDropdownOptions}
                placeholder={loadingRepos ? "Loading repositories..." : "Choose repository..."}
                disabled={loadingRepos || repos.length === 0}
                searchable={true}
                aria-label="Repository Selector"
              />
            </div>
          </div>

          {currentRepo && (
            <div className="flex items-center gap-2 text-xs font-mono text-zinc-400">
              <GitBranch className="h-3.5 w-3.5 text-zinc-500" />
              <span>
                Default branch: <strong className="text-zinc-200">{currentRepo.defaultBranch || "main"}</strong>
              </span>
            </div>
          )}
        </div>

        {/* REPO ERROR BANNER */}
        {repoError && (
          <div
            role="alert"
            className="flex items-center justify-between p-4 rounded-xl border border-red-500/40 bg-red-950/40 text-red-200 text-xs font-medium"
          >
            <div className="flex items-center gap-2.5">
              <AlertCircle className="h-4 w-4 text-red-400 shrink-0" />
              <span>{repoError}</span>
            </div>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={loadRepos}
              className="text-xs text-red-300 border-red-800 hover:bg-red-900/50"
            >
              Retry
            </Button>
          </div>
        )}

        {/* LOADING REPOS SKELETON */}
        {loadingRepos && (
          <div className="space-y-4">
            <Skeleton className="h-10 w-full rounded-xl" />
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              <Skeleton className="h-28 w-full rounded-xl" />
              <Skeleton className="h-28 w-full rounded-xl" />
              <Skeleton className="h-28 w-full rounded-xl" />
            </div>
            <Skeleton className="h-64 w-full rounded-xl" />
          </div>
        )}

        {/* EMPTY STATE: NO REPOSITORIES CONNECTED */}
        {!loadingRepos && repos.length === 0 && (
          <div className="rounded-xl border border-zinc-800/80 bg-gradient-to-b from-[#121216]/95 to-[#0b0b0e]/95 p-8 sm:p-12 text-center max-w-xl mx-auto shadow-lg space-y-4">
            <div className="flex h-12 w-12 items-center justify-center rounded-2xl border border-zinc-800 bg-zinc-900 mx-auto text-zinc-400 shadow-inner">
              <FolderGit2 className="h-6 w-6" />
            </div>
            <div>
              <h2 className="text-base font-bold text-zinc-100 font-mono">No Repositories Connected</h2>
              <p className="text-xs text-zinc-400 mt-1 leading-relaxed">
                Connect your GitHub repositories to configure autonomous self-healing CI pipelines and multi-perspective audit bots.
              </p>
            </div>
            <Link href="/repos">
              <Button
                type="button"
                className="text-xs font-semibold px-4 bg-amber-500 hover:bg-amber-400 text-zinc-950 shadow-[0_2px_8px_rgba(245,158,11,0.3)] mt-2"
              >
                Connect Your First Repository
              </Button>
            </Link>
          </div>
        )}

        {/* LOADING SETTINGS SKELETON */}
        {!loadingRepos && repos.length > 0 && loadingSettings && (
          <div className="space-y-4">
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              <Skeleton className="h-28 w-full rounded-xl" />
              <Skeleton className="h-28 w-full rounded-xl" />
              <Skeleton className="h-28 w-full rounded-xl" />
            </div>
            <Skeleton className="h-64 w-full rounded-xl" />
            <Skeleton className="h-48 w-full rounded-xl" />
          </div>
        )}

        {/* SETTINGS ERROR BANNER */}
        {!loadingSettings && settingsError && (
          <div
            role="alert"
            className="flex items-center justify-between p-4 rounded-xl border border-red-500/40 bg-red-950/40 text-red-200 text-xs font-medium"
          >
            <div className="flex items-center gap-2.5">
              <AlertCircle className="h-4 w-4 text-red-400 shrink-0" />
              <span>{settingsError}</span>
            </div>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => {
                if (selectedRepoId) loadSettingsForRepo(selectedRepoId);
              }}
              className="text-xs text-red-300 border-red-800 hover:bg-red-900/50"
            >
              Retry
            </Button>
          </div>
        )}

        {/* MAIN SETTINGS WORKSPACE CARD */}
        {!loadingRepos && repos.length > 0 && !loadingSettings && activeSettings && currentRepo && (
          <RepoSettingsCard
            settings={activeSettings}
            repoFullName={currentRepo.fullName}
            repoId={currentRepo.id}
            onSave={handleSaveSettings}
            onApplyPreset={handleApplyPreset}
            isSaving={isSaving}
          />
        )}
      </div>
    </AppLayout>
  );
}

export default function SettingsPage() {
  return (
    <Suspense
      fallback={
        <AppLayout
          title="Repository Settings"
          subtitle="Governance presets, feature toggles, and operational safety boundaries"
        >
          <div className="space-y-6 max-w-7xl mx-auto pb-12">
            <Skeleton className="h-24 w-full rounded-xl" />
            <Skeleton className="h-12 w-full rounded-xl" />
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              <Skeleton className="h-32 w-full rounded-xl" />
              <Skeleton className="h-32 w-full rounded-xl" />
              <Skeleton className="h-32 w-full rounded-xl" />
            </div>
            <Skeleton className="h-64 w-full rounded-xl" />
          </div>
        </AppLayout>
      }
    >
      <SettingsContent />
    </Suspense>
  );
}
