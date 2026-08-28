"use client";

import { useEffect, useState, useCallback, useTransition } from "react";
import { AppLayout } from "@/components/layout/app-layout";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuth } from "@/lib/auth-context";
import { api, ModelConfigOut, RepoOut, ApiError } from "@/lib/api";
import { 
  Sliders, 
  Cpu, 
  CheckCircle2, 
  AlertCircle, 
  ShieldCheck, 
  GitBranch, 
  Globe, 
  Zap,
  Server
} from "lucide-react";

// Strict server-side allowlists (WORK.md:252)
const PROVIDER_OPTIONS = [
  { id: "opencode_zen", name: "OpenCode Zen", defaultModel: "nemotron-3.5-lightning-free" },
  { id: "openai", name: "OpenAI", defaultModel: "gpt-4o" },
  { id: "anthropic", name: "Anthropic", defaultModel: "claude-sonnet-4-5" },
];

const MODEL_OPTIONS_BY_PROVIDER: Record<string, { id: string; name: string; tag: string }[]> = {
  // Source of truth: backend/app/schemas.py AllowedModelName.
  // When adding a model here, mirror it there (and vice versa).
  opencode_zen: [
    { id: "nemotron-3.5-lightning-free", name: "Nemotron 3.5 Lightning", tag: "Default · Free" },
    { id: "nemotron-3-ultra-free", name: "Nemotron 3 Ultra", tag: "High-Capacity · Free" },
    { id: "hy3-free", name: "HY3", tag: "Reasoning · Free" },
    { id: "ling-3-free", name: "Ling 3", tag: "Multilingual · Free" },
    { id: "qwen-3-coder-free", name: "Qwen 3 Coder", tag: "Code · Free" },
    { id: "deepseek-r1-free", name: "DeepSeek R1", tag: "Reasoning · Free" },
    { id: "kimi-k2-free", name: "Kimi K2", tag: "Long context · Free" },
  ],
  openai: [
    { id: "gpt-4o", name: "GPT-4o", tag: "Flagship" },
    { id: "gpt-4o-mini", name: "GPT-4o Mini", tag: "Fast" },
  ],
  anthropic: [
    { id: "claude-sonnet-4-5", name: "Claude Sonnet 4.5", tag: "SOTA Fixes" },
    { id: "claude-haiku-3-5", name: "Claude Haiku 3.5", tag: "Low Latency" },
  ],
};

export default function ModelConfigPage() {
  const { user } = useAuth();

  const [repos, setRepos] = useState<RepoOut[]>([]);
  const [selectedScope, setSelectedScope] = useState<"global" | "repo">("global");
  const [selectedRepoId, setSelectedRepoId] = useState<string>("");

  const [activeConfig, setActiveConfig] = useState<ModelConfigOut | null>(null);
  const [selectedProvider, setSelectedProvider] = useState<string>("opencode_zen");
  const [selectedModel, setSelectedModel] = useState<string>("nemotron-3.5-lightning-free");

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saveSuccess, setSaveSuccess] = useState(false);
  const [isPending, startTransition] = useTransition();

  // Load repos on mount
  useEffect(() => {
    api.getRepos().then((data) => {
      setRepos(data);
      if (data.length > 0) {
        setSelectedRepoId(data[0].id);
      }
    }).catch(() => {});
  }, []);

  const fetchActiveConfig = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const repoIdParam = selectedScope === "repo" ? selectedRepoId : undefined;
      const cfg = await api.getModelConfig(repoIdParam);
      setActiveConfig(cfg);
      setSelectedProvider(cfg.provider);
      setSelectedModel(cfg.model_name);
    } catch (err: unknown) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Failed to load model configuration.");
      }
    } finally {
      setLoading(false);
    }
  }, [selectedScope, selectedRepoId]);

  useEffect(() => {
    fetchActiveConfig();
  }, [fetchActiveConfig]);

  // Handle provider switch -> update available model selection
  const handleProviderChange = (newProvider: string) => {
    setSelectedProvider(newProvider);
    const models = MODEL_OPTIONS_BY_PROVIDER[newProvider] || [];
    if (models.length > 0) {
      setSelectedModel(models[0].id);
    }
  };

  const handleSaveConfig = () => {
    setError(null);
    setSaveSuccess(false);

    startTransition(async () => {
      try {
        const payload = {
          provider: selectedProvider,
          model_name: selectedModel,
          repo_id: selectedScope === "repo" ? selectedRepoId : undefined,
        };

        const updated = await api.updateModelConfig(payload);
        setActiveConfig(updated);
        setSaveSuccess(true);
        setTimeout(() => setSaveSuccess(false), 4000);
      } catch (err: unknown) {
        if (err instanceof ApiError) {
          setError(err.message);
        } else {
          setError("Failed to update active model configuration.");
        }
      }
    });
  };

  const isGlobalDisabled = selectedScope === "global" && !user?.is_admin;

  return (
    <AppLayout
      title="Model & Provider Configuration"
      subtitle="Manage active LLM inference providers and models globally or per repository"
    >
      <div className="max-w-4xl space-y-6">
        {error && (
          <div className="flex items-center gap-2 rounded-[6px] border border-red-900/60 bg-red-950/30 p-3.5 text-xs text-red-300">
            <AlertCircle className="h-4 w-4 shrink-0 text-red-400" />
            <span>{error}</span>
          </div>
        )}

        {saveSuccess && (
          <div className="flex items-center gap-2 rounded-[6px] border border-emerald-800/80 bg-emerald-950/30 p-3.5 text-xs text-emerald-300 font-mono">
            <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-400" />
            <span>Configuration updated live. Next pipeline execution will use {selectedModel}.</span>
          </div>
        )}

        {/* Scope Selector (Global vs Per-Repo) */}
        <div className="rounded-[6px] border border-zinc-800 bg-[#121215] p-4 space-y-3">
          <div className="flex items-center justify-between border-b border-zinc-800 pb-2">
            <span className="text-xs font-mono font-semibold uppercase tracking-wider text-zinc-300">
              Configuration Scope
            </span>
            <span className="text-[11px] font-mono text-zinc-500">
              {user?.is_admin ? "Admin Privileges Active" : "Tenant Mode"}
            </span>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {/* Global Scope Button */}
            <button
              type="button"
              onClick={() => setSelectedScope("global")}
              className={`flex items-start gap-3 rounded-[5px] border p-3 text-left transition-colors cursor-pointer ${
                selectedScope === "global"
                  ? "border-amber-400/80 bg-zinc-900"
                  : "border-zinc-800 bg-[#09090b] hover:border-zinc-700"
              }`}
            >
              <Globe className={`h-4 w-4 mt-0.5 ${selectedScope === "global" ? "text-amber-400" : "text-zinc-500"}`} />
              <div className="space-y-0.5">
                <div className="flex items-center gap-1.5">
                  <span className="text-xs font-semibold text-zinc-200">Global Default</span>
                  {!user?.is_admin && (
                    <span className="rounded-[3px] border border-zinc-800 bg-zinc-900 px-1 py-0.2 text-[9px] font-mono text-zinc-500">
                      Admin Only
                    </span>
                  )}
                </div>
                <p className="text-[11px] text-zinc-500 font-mono">
                  Applies globally to all repositories without explicit overrides
                </p>
              </div>
            </button>

            {/* Per-Repository Scope Button */}
            <button
              type="button"
              onClick={() => setSelectedScope("repo")}
              className={`flex items-start gap-3 rounded-[5px] border p-3 text-left transition-colors cursor-pointer ${
                selectedScope === "repo"
                  ? "border-amber-400/80 bg-zinc-900"
                  : "border-zinc-800 bg-[#09090b] hover:border-zinc-700"
              }`}
            >
              <GitBranch className={`h-4 w-4 mt-0.5 ${selectedScope === "repo" ? "text-amber-400" : "text-zinc-500"}`} />
              <div className="space-y-0.5">
                <span className="text-xs font-semibold text-zinc-200">Repository Override</span>
                <p className="text-[11px] text-zinc-500 font-mono">
                  Override LLM model specifically for one connected repository
                </p>
              </div>
            </button>
          </div>

          {/* Repo Dropdown (when Repository Override is selected) */}
          {selectedScope === "repo" && (
            <div className="pt-2">
              <label className="block text-[11px] font-mono uppercase tracking-wider text-zinc-400 mb-1.5">
                Select Connected Repository:
              </label>
              {repos.length === 0 ? (
                <div className="text-xs text-zinc-500 font-mono p-2 border border-dashed border-zinc-800 rounded-[4px]">
                  No connected repositories found. Connect a repository first.
                </div>
              ) : (
                <select
                  value={selectedRepoId}
                  onChange={(e) => setSelectedRepoId(e.target.value)}
                  className="w-full rounded-[4px] border border-zinc-700 bg-zinc-900 px-3 py-2 text-xs font-mono text-zinc-200 focus:outline-none focus:border-amber-400"
                >
                  {repos.map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.owner}/{r.name} ({r.default_branch || "main"})
                    </option>
                  ))}
                </select>
              )}
            </div>
          )}

          {/* Non-admin notice for global scope */}
          {isGlobalDisabled && (
            <div className="rounded-[4px] border border-zinc-800 bg-zinc-900/60 p-2.5 text-[11px] font-mono text-zinc-400 flex items-center gap-2">
              <AlertCircle className="h-3.5 w-3.5 text-amber-400/80 shrink-0" />
              <span>Global model updates are restricted to administrators. You can configure per-repository overrides above.</span>
            </div>
          )}
        </div>

        {/* Model & Provider Switcher Form */}
        <div className="rounded-[6px] border border-zinc-800 bg-[#121215] p-5 space-y-5">
          <div className="flex items-center justify-between border-b border-zinc-800 pb-3">
            <div className="flex items-center gap-2">
              <Cpu className="h-4 w-4 text-amber-400" />
              <h3 className="text-xs font-mono font-semibold uppercase tracking-wider text-zinc-100">
                Model Selection (Server Allowlist Enforced)
              </h3>
            </div>
            <span className="text-[10px] font-mono text-zinc-500 flex items-center gap-1">
              <ShieldCheck className="h-3 w-3 text-emerald-400" />
              Zero Free-Text Injection
            </span>
          </div>

          {loading ? (
            <div className="space-y-4">
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-32" />
            </div>
          ) : (
            <div className="space-y-4">
              {/* Provider Select */}
              <div className="space-y-1.5">
                <label className="block text-xs font-mono font-medium text-zinc-300">
                  LLM Provider:
                </label>
                <select
                  value={selectedProvider}
                  onChange={(e) => handleProviderChange(e.target.value)}
                  disabled={isGlobalDisabled || isPending}
                  className="w-full rounded-[4px] border border-zinc-700 bg-zinc-900 px-3 py-2 text-xs font-mono text-zinc-200 focus:outline-none focus:border-amber-400 disabled:opacity-50 cursor-pointer"
                >
                  {PROVIDER_OPTIONS.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
              </div>

              {/* Model Select (Strictly filtered by allowlist) */}
              <div className="space-y-1.5">
                <label className="block text-xs font-mono font-medium text-zinc-300">
                  Model Architecture:
                </label>
                <select
                  value={selectedModel}
                  onChange={(e) => setSelectedModel(e.target.value)}
                  disabled={isGlobalDisabled || isPending}
                  className="w-full rounded-[4px] border border-zinc-700 bg-zinc-900 px-3 py-2 text-xs font-mono text-zinc-200 focus:outline-none focus:border-amber-400 disabled:opacity-50 cursor-pointer"
                >
                  {(MODEL_OPTIONS_BY_PROVIDER[selectedProvider] || []).map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.name} [{m.tag}]
                    </option>
                  ))}
                </select>
              </div>

              {/* Server-Derived Base URL (Read-Only) */}
              <div className="space-y-1.5">
                <label className="block text-[11px] font-mono text-zinc-400">
                  Inference Base URL (Server-Derived):
                </label>
                <div className="rounded-[4px] border border-zinc-800 bg-[#09090b] px-3 py-2 font-mono text-xs text-zinc-400 flex items-center justify-between select-text">
                  <span>{activeConfig?.base_url || "https://opencode.ai/zen/v1"}</span>
                  <span className="text-[10px] text-zinc-600">Locked</span>
                </div>
              </div>

              {/* Submit Button */}
              <div className="pt-3 border-t border-zinc-800 flex items-center justify-between">
                <div className="flex items-center gap-1.5 text-[11px] font-mono text-zinc-500">
                  <Zap className="h-3.5 w-3.5 text-amber-400" />
                  <span>Hot-switched instantly without redeploying containers.</span>
                </div>

                <Button
                  onClick={handleSaveConfig}
                  disabled={isGlobalDisabled || isPending}
                  className="bg-amber-400 text-zinc-950 hover:bg-amber-300 font-semibold text-xs"
                  size="sm"
                >
                  {isPending ? "Applying Live..." : "Save Configuration"}
                </Button>
              </div>
            </div>
          )}
        </div>

        {/* Active Configuration Live Status Card */}
        {activeConfig && (
          <div className="rounded-[6px] border border-zinc-800 bg-[#09090b] p-4 font-mono text-xs space-y-2">
            <div className="flex items-center justify-between text-zinc-400 border-b border-zinc-900 pb-2">
              <span className="uppercase tracking-wider text-[10px]">Active Live State</span>
              <div className="flex items-center gap-1.5 text-emerald-400 text-[11px]">
                <span className="h-2 w-2 rounded-full bg-emerald-400 animate-pulse" />
                <span>Ready for CI Webhooks</span>
              </div>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 pt-1">
              <div>
                <span className="text-[10px] text-zinc-500 block">Active Provider</span>
                <span className="text-zinc-200 font-semibold">{activeConfig.provider}</span>
              </div>
              <div>
                <span className="text-[10px] text-zinc-500 block">Active Model</span>
                <span className="text-amber-400 font-semibold">{activeConfig.model_name}</span>
              </div>
              <div>
                <span className="text-[10px] text-zinc-500 block">Orchestrator Mode</span>
                <span className="text-zinc-200">Async Subagents</span>
              </div>
            </div>
          </div>
        )}
      </div>
    </AppLayout>
  );
}
