"use client";

import { useEffect, useState, useCallback, useTransition, useMemo } from "react";
import { AppLayout } from "@/components/layout/app-layout";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuth } from "@/lib/auth-context";
import { api, ModelConfigOut, RepoOut, ApiError } from "@/lib/api";
import { cn } from "@/lib/utils";
import {
  Cpu,
  CheckCircle2,
  AlertCircle,
  ShieldCheck,
  GitBranch,
  Globe,
  Zap,
  Sparkles,
  Lock,
  Copy,
  Check,
  RefreshCw,
  Activity,
  ShieldAlert,
  ArrowRight,
  Search,
  X,
  Gauge,
  Layers,
  RotateCcw,
} from "lucide-react";

// Strict server-side allowlists (WORK.md:252)
const PROVIDER_OPTIONS = [
  { id: "opencode_zen", name: "OpenCode Zen", defaultModel: "nemotron-3.5-lightning-free" },
  { id: "openai", name: "OpenAI", defaultModel: "gpt-4o" },
  { id: "anthropic", name: "Anthropic", defaultModel: "claude-sonnet-4-5" },
];

const DEFAULT_MODEL_OPTIONS_BY_PROVIDER: Record<string, { id: string; name: string; tag: string }[]> = {
  opencode_zen: [
    { id: "nemotron-3.5-lightning-free", name: "Nemotron 3.5 Lightning", tag: "Default · Free" },
    { id: "laguna-s-2.1-free", name: "Laguna S 2.1", tag: "Fast · Free" },
    { id: "ling-3.0-flash-fin-free", name: "Ling 3.0 Flash Fin", tag: "Free" },
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

const PROVIDER_METADATA: Record<
  string,
  {
    name: string;
    tag: string;
    shortBadge: string;
    description: string;
    baseUrl: string;
    icon: typeof Zap;
    accentText: string;
    badgeClass: string;
  }
> = {
  opencode_zen: {
    name: "OpenCode Zen",
    tag: "Recommended · Free Tier",
    shortBadge: "Free Tier",
    description: "High-throughput inference gateway built for rapid CI fault diagnosis and automated patch synthesis.",
    baseUrl: "https://opencode.ai/zen/v1",
    icon: Zap,
    accentText: "text-amber-400",
    badgeClass: "bg-amber-400/10 border-amber-400/30 text-amber-300",
  },
  openai: {
    name: "OpenAI",
    tag: "Direct API",
    shortBadge: "Flagship",
    description: "Industry-standard multi-step reasoning models with GPT-4o for complex architectural bug fixes.",
    baseUrl: "https://api.openai.com/v1",
    icon: Cpu,
    accentText: "text-emerald-400",
    badgeClass: "bg-emerald-400/10 border-emerald-400/30 text-emerald-300",
  },
  anthropic: {
    name: "Anthropic",
    tag: "Direct API",
    shortBadge: "AST SOTA",
    description: "Specialized code reasoning via Claude 3.5 Sonnet & Claude 3.5 Haiku for nuanced test failures.",
    baseUrl: "https://api.anthropic.com/v1",
    icon: Sparkles,
    accentText: "text-orange-400",
    badgeClass: "bg-orange-400/10 border-orange-400/30 text-orange-300",
  },
};

interface ModelSpec {
  contextWindow: string;
  specialty: string;
  latencyRating: string;
  recommendedRole: string;
  tagColor: string;
  speedCategory: "fast" | "standard" | "reasoning";
  isFree?: boolean;
}

const MODEL_SPECS: Record<string, ModelSpec> = {
  "nemotron-3.5-lightning-free": {
    contextWindow: "128k Context",
    specialty: "CI Diagnostics & Root-Cause Synthesis",
    latencyRating: "~320ms TTFT",
    recommendedRole: "Default Engine",
    tagColor: "bg-amber-950/60 border-amber-700/60 text-amber-300",
    speedCategory: "standard",
    isFree: true,
  },
  "laguna-s-2.1-free": {
    contextWindow: "64k Context",
    specialty: "High-Frequency Test & Lint Healing",
    latencyRating: "~190ms TTFT",
    recommendedRole: "Ultra-Fast Patching",
    tagColor: "bg-emerald-950/60 border-emerald-700/60 text-emerald-300",
    speedCategory: "fast",
    isFree: true,
  },
  "ling-3.0-flash-fin-free": {
    contextWindow: "128k Context",
    specialty: "Multi-Language Syntax & AST Repair",
    latencyRating: "~390ms TTFT",
    recommendedRole: "Type & AST Specialist",
    tagColor: "bg-cyan-950/60 border-cyan-700/60 text-cyan-300",
    speedCategory: "standard",
    isFree: true,
  },
  "deepseek-v4-flash-free": {
    contextWindow: "128k Context",
    specialty: "Rapid Multi-Step Test Regeneration & Syntax Fixes",
    latencyRating: "~240ms TTFT",
    recommendedRole: "High-Throughput Healing",
    tagColor: "bg-blue-950/60 border-blue-700/60 text-blue-300",
    speedCategory: "fast",
    isFree: true,
  },
  "muse-spark-1.3-contributor-free": {
    contextWindow: "128k Context",
    specialty: "Open-Source Benchmark Synthesis & Verification",
    latencyRating: "~310ms TTFT",
    recommendedRole: "Community Benchmark",
    tagColor: "bg-violet-950/60 border-violet-700/60 text-violet-300",
    speedCategory: "standard",
    isFree: true,
  },
  "qwen-2.5-coder-32b-instruct-free": {
    contextWindow: "128k Context",
    specialty: "Complex Multi-File Refactoring & Git Patch Synthesis",
    latencyRating: "~350ms TTFT",
    recommendedRole: "Polyglot Specialist",
    tagColor: "bg-teal-950/60 border-teal-700/60 text-teal-300",
    speedCategory: "standard",
    isFree: true,
  },
  "deepseek-r1-distill-qwen-32b-free": {
    contextWindow: "128k Context",
    specialty: "Deep Chain-of-Thought Flaky Test & Concurrency Debugging",
    latencyRating: "~510ms TTFT",
    recommendedRole: "Deep CoT Reasoning",
    tagColor: "bg-indigo-950/60 border-indigo-700/60 text-indigo-300",
    speedCategory: "reasoning",
    isFree: true,
  },
  "llama-3.3-70b-instruct-free": {
    contextWindow: "128k Context",
    specialty: "High-Parameter Multi-Language Architectural Patches",
    latencyRating: "~440ms TTFT",
    recommendedRole: "Enterprise Scale",
    tagColor: "bg-purple-950/60 border-purple-700/60 text-purple-300",
    speedCategory: "reasoning",
    isFree: true,
  },
  "gpt-4o": {
    contextWindow: "128k Context",
    specialty: "Multi-File Architectural Patching & Full-Stack Healing",
    latencyRating: "~540ms TTFT",
    recommendedRole: "Deep Agentic Planning",
    tagColor: "bg-purple-950/60 border-purple-700/60 text-purple-300",
    speedCategory: "reasoning",
    isFree: false,
  },
  "gpt-4o-mini": {
    contextWindow: "128k Context",
    specialty: "Rapid Unit Test & Config Script Fixes",
    latencyRating: "~260ms TTFT",
    recommendedRole: "Cost-Efficient Healing",
    tagColor: "bg-emerald-950/60 border-emerald-700/60 text-emerald-300",
    speedCategory: "fast",
    isFree: false,
  },
  "claude-sonnet-4-5": {
    contextWindow: "200k Context",
    specialty: "AST Refactoring & Complex Logic Healing",
    latencyRating: "~610ms TTFT",
    recommendedRole: "Maximum Accuracy",
    tagColor: "bg-orange-950/60 border-orange-700/60 text-orange-300",
    speedCategory: "reasoning",
    isFree: false,
  },
  "claude-haiku-3-5": {
    contextWindow: "200k Context",
    specialty: "Low-Latency Code Synthesis & Fast CI Gates",
    latencyRating: "~230ms TTFT",
    recommendedRole: "High-Speed SOTA",
    tagColor: "bg-cyan-950/60 border-cyan-700/60 text-cyan-300",
    speedCategory: "fast",
    isFree: false,
  },
};

/**
 * Format model identifiers into polished, human-readable display names.
 */
function formatModelDisplayName(id: string, rawName?: string): string {
  if (rawName && rawName !== id && !rawName.endsWith("-free")) {
    return rawName;
  }

  const KNOWN_NAMES: Record<string, string> = {
    "nemotron-3.5-lightning-free": "Nemotron 3.5 Lightning",
    "laguna-s-2.1-free": "Laguna S 2.1",
    "ling-3.0-flash-fin-free": "Ling 3.0 Flash Fin",
    "deepseek-v4-flash-free": "DeepSeek V4 Flash",
    "muse-spark-1.3-contributor-free": "Muse Spark 1.3 Contributor",
    "qwen-2.5-coder-32b-instruct-free": "Qwen 2.5 Coder 32B",
    "deepseek-r1-distill-qwen-32b-free": "DeepSeek R1 Distill Qwen",
    "llama-3.3-70b-instruct-free": "Llama 3.3 70B Instruct",
    "gpt-4o": "GPT-4o Flagship",
    "gpt-4o-mini": "GPT-4o Mini",
    "claude-sonnet-4-5": "Claude Sonnet 4.5",
    "claude-haiku-3-5": "Claude Haiku 3.5",
  };

  if (KNOWN_NAMES[id]) return KNOWN_NAMES[id];

  const clean = id.replace(/-free$/, "");
  return clean
    .split("-")
    .map((word) => {
      const lower = word.toLowerCase();
      if (lower === "v4" || lower === "r1" || lower === "s" || lower === "32b" || lower === "70b") {
        return word.toUpperCase();
      }
      return word.charAt(0).toUpperCase() + word.slice(1);
    })
    .join(" ");
}

/**
 * Resolve spec metadata with intelligent heuristics for dynamic models.
 */
function getModelSpec(modelId: string, tag?: string): ModelSpec {
  if (MODEL_SPECS[modelId]) return MODEL_SPECS[modelId];

  const idLower = modelId.toLowerCase();
  const isFlash =
    idLower.includes("flash") || idLower.includes("fast") || idLower.includes("haiku") || idLower.includes("mini");
  const isReasoning =
    idLower.includes("r1") || idLower.includes("reason") || idLower.includes("70b") || idLower.includes("thought");
  const isFree = idLower.endsWith("-free");

  return {
    contextWindow: idLower.includes("200k") ? "200k Context" : "128k Context",
    specialty: isFlash
      ? "High-Throughput Fast Triage & Syntax Healing"
      : isReasoning
      ? "Deep Step-by-Step Chain-of-Thought Diagnosis"
      : tag
      ? `${tag} Inference`
      : "Automated CI Diagnosis & Fix Generation",
    latencyRating: isFlash ? "~220ms TTFT" : isReasoning ? "~490ms TTFT" : "~330ms TTFT",
    recommendedRole: isFree ? "Free Tier Verified" : "Allowlist Verified",
    tagColor: isFree
      ? "bg-emerald-950/60 border-emerald-700/60 text-emerald-300"
      : "bg-zinc-800 border-zinc-700 text-zinc-300",
    speedCategory: isFlash ? "fast" : isReasoning ? "reasoning" : "standard",
    isFree,
  };
}

export default function ModelConfigPage() {
  const { user } = useAuth();

  const [repos, setRepos] = useState<RepoOut[]>([]);
  const [selectedScope, setSelectedScope] = useState<"global" | "repo">("global");
  const [selectedRepoId, setSelectedRepoId] = useState<string>("");

  const [activeConfig, setActiveConfig] = useState<ModelConfigOut | null>(null);
  const [selectedProvider, setSelectedProvider] = useState<string>("opencode_zen");
  const [selectedModel, setSelectedModel] = useState<string>("nemotron-3.5-lightning-free");
  const [modelOptionsByProvider, setModelOptionsByProvider] = useState<
    Record<string, { id: string; name: string; tag: string }[]>
  >(DEFAULT_MODEL_OPTIONS_BY_PROVIDER);

  const [searchQuery, setSearchQuery] = useState("");
  const [speedFilter, setSpeedFilter] = useState<"all" | "free" | "fast" | "reasoning">("all");

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saveSuccess, setSaveSuccess] = useState(false);
  const [copiedUrl, setCopiedUrl] = useState(false);
  const [copiedModelId, setCopiedModelId] = useState<string | null>(null);
  const [isPending, startTransition] = useTransition();

  // Load dynamic models from backend
  useEffect(() => {
    api
      .getAvailableModels()
      .then((data) => {
        if (data) {
          setModelOptionsByProvider({
            opencode_zen: data.opencode_zen?.length ? data.opencode_zen : DEFAULT_MODEL_OPTIONS_BY_PROVIDER.opencode_zen,
            openai: data.openai?.length ? data.openai : DEFAULT_MODEL_OPTIONS_BY_PROVIDER.openai,
            anthropic: data.anthropic?.length ? data.anthropic : DEFAULT_MODEL_OPTIONS_BY_PROVIDER.anthropic,
          });
        }
      })
      .catch(() => {});
  }, []);

  // Load repos on mount
  useEffect(() => {
    api
      .getRepos()
      .then((data) => {
        setRepos(data);
        if (data.length > 0) {
          setSelectedRepoId(data[0].id);
        }
      })
      .catch(() => {});
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
    const models = modelOptionsByProvider[newProvider] || [];
    if (models.length > 0) {
      setSelectedModel(models[0].id);
    }
  };

  const handleSaveConfig = useCallback(() => {
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
        setTimeout(() => setSaveSuccess(false), 4500);
      } catch (err: unknown) {
        if (err instanceof ApiError) {
          setError(err.message);
        } else {
          setError("Failed to update active model configuration.");
        }
      }
    });
  }, [selectedProvider, selectedModel, selectedScope, selectedRepoId]);

  const isGlobalDisabled = selectedScope === "global" && !user?.is_admin;

  const isDirty = useMemo(() => {
    if (!activeConfig) return false;
    return (
      selectedProvider !== activeConfig.provider ||
      selectedModel !== activeConfig.model_name
    );
  }, [activeConfig, selectedProvider, selectedModel]);

  // Keyboard shortcut: Cmd+S / Ctrl+S to save
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "s") {
        e.preventDefault();
        if (!isGlobalDisabled && !isPending && isDirty) {
          handleSaveConfig();
        }
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isGlobalDisabled, isPending, isDirty, handleSaveConfig]);

  const handleDiscardChanges = () => {
    if (activeConfig) {
      setSelectedProvider(activeConfig.provider);
      setSelectedModel(activeConfig.model_name);
    }
  };

  const handleCopyUrl = (url: string) => {
    navigator.clipboard.writeText(url);
    setCopiedUrl(true);
    setTimeout(() => setCopiedUrl(false), 2000);
  };

  const handleCopyModelId = (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    navigator.clipboard.writeText(id);
    setCopiedModelId(id);
    setTimeout(() => setCopiedModelId(null), 1800);
  };

  const activeProviderMeta = PROVIDER_METADATA[selectedProvider] || PROVIDER_METADATA.opencode_zen;
  const currentBaseUrl = activeConfig?.base_url || activeProviderMeta.baseUrl;
  const currentRepo = repos.find((r) => r.id === selectedRepoId);

  // Normalize model list
  const currentModels = useMemo(() => {
    const options = modelOptionsByProvider[selectedProvider] || [];
    const hasSelected = options.some((m) => m.id === selectedModel);
    const list = hasSelected
      ? options
      : [{ id: selectedModel, name: selectedModel, tag: "Active" }, ...options];

    // Filter by query and speed category
    return list.filter((m) => {
      const spec = getModelSpec(m.id, m.tag);
      const friendlyName = formatModelDisplayName(m.id, m.name).toLowerCase();
      const q = searchQuery.toLowerCase().trim();

      const matchesQuery =
        !q ||
        friendlyName.includes(q) ||
        m.id.toLowerCase().includes(q) ||
        spec.specialty.toLowerCase().includes(q) ||
        spec.recommendedRole.toLowerCase().includes(q);

      if (!matchesQuery) return false;

      if (speedFilter === "free") return spec.isFree;
      if (speedFilter === "fast") return spec.speedCategory === "fast";
      if (speedFilter === "reasoning") return spec.speedCategory === "reasoning";
      return true;
    });
  }, [modelOptionsByProvider, selectedProvider, selectedModel, searchQuery, speedFilter]);

  const allProviderModelsCount = (modelOptionsByProvider[selectedProvider] || []).length;

  return (
    <AppLayout
      title="Model & Provider Configuration"
      subtitle="Manage and hot-swap active LLM inference engines globally or per repository"
      actions={
        <div className="flex items-center gap-3">
          {activeConfig && (
            <div className="hidden sm:flex items-center gap-2 rounded-full border border-zinc-800 bg-[#0c0c0e] px-3.5 py-1.5 shadow-sm">
              <span className="h-2 w-2 rounded-full bg-emerald-400 animate-pulse" />
              <span className="text-xs font-mono text-zinc-300">
                Active: <strong className="text-amber-400 font-semibold">{activeConfig.model_name}</strong>
              </span>
            </div>
          )}

          {/* Direct Topbar Save button when configuration is dirty */}
          {isDirty && !isGlobalDisabled && (
            <Button
              size="sm"
              onClick={handleSaveConfig}
              disabled={isPending}
              className="h-8 bg-gradient-to-b from-amber-400 to-amber-500 text-zinc-950 font-bold hover:from-amber-300 hover:to-amber-400 px-3 text-xs shadow-[0_1px_0_inset_rgba(255,255,255,0.35),0_2px_8px_rgba(245,158,11,0.25)] ring-1 ring-amber-400/40 gap-1.5 cursor-pointer"
            >
              {isPending ? (
                <RefreshCw className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Check className="h-3.5 w-3.5" />
              )}
              <span>Save Changes</span>
              <kbd className="hidden md:inline-block rounded bg-zinc-950/20 px-1 py-0.2 text-[10px] font-mono text-zinc-900 border border-zinc-950/30">
                ⌘S
              </kbd>
            </Button>
          )}

          <Button
            variant="outline"
            size="sm"
            onClick={() => fetchActiveConfig()}
            disabled={loading}
            className="h-8 border-zinc-700 bg-zinc-900 text-zinc-200 hover:bg-zinc-800 gap-1.5 text-xs font-medium cursor-pointer"
          >
            <RefreshCw className={cn("h-3.5 w-3.5 text-zinc-400", loading && "animate-spin text-amber-400")} />
            <span>Sync</span>
          </Button>
        </div>
      }
    >
      <div className="space-y-6 min-w-0 max-w-6xl pb-20">
        {/* Error Alert */}
        {error && (
          <div className="flex items-start gap-3 rounded-lg border border-red-800/80 bg-red-950/40 p-4 text-sm text-red-200 shadow-md animate-in fade-in">
            <AlertCircle className="h-5 w-5 shrink-0 text-red-400 mt-0.5" />
            <div className="space-y-0.5">
              <p className="font-semibold text-red-200">Configuration Error</p>
              <p className="text-red-300/90 text-xs font-mono">{error}</p>
            </div>
          </div>
        )}

        {/* Success Alert */}
        {saveSuccess && (
          <div className="flex items-center justify-between rounded-lg border border-emerald-500/60 bg-emerald-950/40 p-4 text-sm text-emerald-200 shadow-lg animate-in fade-in slide-in-from-top-2">
            <div className="flex items-center gap-3">
              <CheckCircle2 className="h-5 w-5 text-emerald-400 shrink-0" />
              <div>
                <p className="font-semibold text-emerald-300">Configuration Updated Live</p>
                <p className="text-emerald-400/90 text-xs mt-0.5">
                  Subagent pipeline will now route incoming CI failures to{" "}
                  <strong className="font-mono text-white bg-emerald-900/50 px-1.5 py-0.5 rounded border border-emerald-700">
                    {selectedModel}
                  </strong>
                </p>
              </div>
            </div>
            <span className="hidden sm:inline-flex text-xs font-mono text-emerald-300 bg-emerald-900/50 border border-emerald-700/60 px-2.5 py-1 rounded">
              Zero-Downtime Hot-Swapped
            </span>
          </div>
        )}

        {/* TOP BAR: Scope Switcher + Target Info */}
        <div className="rounded-xl border border-zinc-800 bg-[#121215] p-4 sm:p-5 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.03),0_2px_12px_rgba(0,0,0,0.3)] space-y-4">
          <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-3.5">
            {/* Scope Segmented Control */}
            <div className="flex flex-wrap items-center gap-3">
              <span className="text-xs font-semibold uppercase tracking-wider text-zinc-400 select-none">
                Scope:
              </span>
              <div className="inline-flex rounded-lg border border-zinc-800 bg-[#09090b] p-1 shadow-inner">
                <button
                  type="button"
                  onClick={() => setSelectedScope("global")}
                  className={cn(
                    "flex items-center gap-2 rounded-md px-4 py-2 text-xs sm:text-sm font-semibold transition-all cursor-pointer select-none",
                    selectedScope === "global"
                      ? "bg-zinc-800 text-amber-400 shadow-sm border border-zinc-700 font-bold"
                      : "text-zinc-400 hover:text-zinc-200"
                  )}
                >
                  <Globe className="h-4 w-4 text-amber-400" />
                  <span>Global Platform Default</span>
                  {!user?.is_admin && (
                    <span className="ml-1 rounded bg-zinc-900 border border-zinc-700 px-1.5 py-0.2 text-[10px] text-zinc-400 font-mono">
                      Admin
                    </span>
                  )}
                </button>

                <button
                  type="button"
                  onClick={() => setSelectedScope("repo")}
                  className={cn(
                    "flex items-center gap-2 rounded-md px-4 py-2 text-xs sm:text-sm font-semibold transition-all cursor-pointer select-none",
                    selectedScope === "repo"
                      ? "bg-zinc-800 text-cyan-400 shadow-sm border border-zinc-700 font-bold"
                      : "text-zinc-400 hover:text-zinc-200"
                  )}
                >
                  <GitBranch className="h-4 w-4 text-cyan-400" />
                  <span>Repository Override</span>
                  {repos.length > 0 && (
                    <span className="ml-1 rounded-full bg-cyan-950 border border-cyan-800/60 px-2 py-0.2 text-[10px] text-cyan-300 font-mono">
                      {repos.length}
                    </span>
                  )}
                </button>
              </div>
            </div>

            {/* Scope Explainer / Status Badge */}
            <div className="flex items-center gap-2">
              <span
                className={cn(
                  "text-xs font-semibold uppercase tracking-wider px-3 py-1.5 rounded-md border flex items-center gap-1.5 select-none",
                  user?.is_admin
                    ? "bg-emerald-950/40 border-emerald-800/50 text-emerald-400"
                    : "bg-zinc-900 border-zinc-800 text-zinc-400"
                )}
              >
                {user?.is_admin ? (
                  <>
                    <ShieldCheck className="h-4 w-4 text-emerald-400" />
                    <span>Admin Superuser</span>
                  </>
                ) : (
                  <>
                    <Lock className="h-3.5 w-3.5 text-zinc-400" />
                    <span>Tenant Access</span>
                  </>
                )}
              </span>
            </div>
          </div>

          {/* Elevated Non-admin notice for global scope */}
          {isGlobalDisabled && (
            <div className="rounded-lg border border-amber-500/30 bg-gradient-to-r from-amber-950/30 via-zinc-900/60 to-zinc-900/30 p-3.5 flex flex-col sm:flex-row sm:items-center justify-between gap-3 text-xs sm:text-sm text-zinc-300">
              <div className="flex items-start sm:items-center gap-3">
                <ShieldAlert className="h-5 w-5 text-amber-400 shrink-0 mt-0.5 sm:mt-0" />
                <div className="space-y-0.5">
                  <span className="font-semibold text-zinc-100">Global Cluster Defaults Are Read-Only</span>
                  <p className="text-xs text-zinc-400">
                    Switch to <strong>Repository Override</strong> to customize model routing specifically for your own repos.
                  </p>
                </div>
              </div>
              <Button
                size="sm"
                onClick={() => setSelectedScope("repo")}
                className="bg-amber-400 text-zinc-950 hover:bg-amber-300 font-semibold text-xs shrink-0 self-start sm:self-auto cursor-pointer"
              >
                <span>Switch to Repo Override</span>
                <ArrowRight className="h-3.5 w-3.5" />
              </Button>
            </div>
          )}

          {/* Repo Dropdown (when Repo Override is selected) */}
          {selectedScope === "repo" && (
            <div className="pt-3 border-t border-zinc-800/80 flex flex-col sm:flex-row sm:items-center gap-3 animate-in fade-in duration-150">
              <label className="text-xs sm:text-sm font-semibold uppercase tracking-wider text-zinc-300 shrink-0">
                Target Repository:
              </label>
              {repos.length === 0 ? (
                <div className="text-xs text-zinc-400 font-mono p-2 border border-dashed border-zinc-800 rounded bg-zinc-950">
                  No connected repositories found. Connect a repository first in the Repositories tab.
                </div>
              ) : (
                <div className="flex-1 flex flex-col sm:flex-row sm:items-center gap-3">
                  <div className="relative flex-1">
                    <select
                      value={selectedRepoId}
                      onChange={(e) => setSelectedRepoId(e.target.value)}
                      className="w-full appearance-none rounded-md border border-zinc-700 bg-[#0c0c0e] px-3.5 py-2 text-sm font-medium text-zinc-200 focus:outline-none focus:border-amber-400 focus:ring-1 focus:ring-amber-400/40 cursor-pointer shadow-sm"
                    >
                      {repos.map((r) => (
                        <option key={r.id} value={r.id} className="bg-[#121215] text-zinc-200 py-1">
                          {r.owner}/{r.name} ({r.default_branch || "main"})
                        </option>
                      ))}
                    </select>
                    <div className="pointer-events-none absolute inset-y-0 right-0 flex items-center px-3 text-zinc-400">
                      <GitBranch className="h-4 w-4 text-amber-400" />
                    </div>
                  </div>
                  {currentRepo && (
                    <span className="text-xs text-zinc-400 font-medium whitespace-nowrap">
                      Branch: <strong className="text-zinc-200 font-mono">{currentRepo.default_branch || "main"}</strong>
                      {currentRepo.language_hint && (
                        <> · Language: <strong className="text-amber-400/90 font-mono">{currentRepo.language_hint}</strong></>
                      )}
                    </span>
                  )}
                </div>
              )}
            </div>
          )}
        </div>

        {/* MAIN HERO: Provider Tabs + Models Grid */}
        <div className="rounded-xl border border-zinc-800 bg-[#121215] p-5 sm:p-6 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.03),0_4px_20px_rgba(0,0,0,0.35)] space-y-6">
          {/* Header & Provider Segmented Tabs */}
          <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 pb-4 border-b border-zinc-800">
            <div>
              <h2 className="text-base sm:text-lg font-bold text-zinc-100 flex items-center gap-2">
                <Cpu className="h-5 w-5 text-amber-400" />
                <span>Autonomous Inference Engine</span>
              </h2>
              <p className="text-xs sm:text-sm text-zinc-400 mt-0.5">
                Strict server allowlist enforced · Zero free-text injection
              </p>
            </div>

            {/* Provider Switcher Tabs */}
            <div className="inline-flex rounded-lg border border-zinc-800 bg-[#0c0c0e] p-1 shadow-inner">
              {PROVIDER_OPTIONS.map((p) => {
                const isSelected = selectedProvider === p.id;
                const meta = PROVIDER_METADATA[p.id] || PROVIDER_METADATA.opencode_zen;
                const Icon = meta.icon;
                return (
                  <button
                    key={p.id}
                    type="button"
                    onClick={() => handleProviderChange(p.id)}
                    disabled={isGlobalDisabled || isPending}
                    className={cn(
                      "flex items-center gap-2 rounded-md px-4 py-2 text-xs sm:text-sm font-semibold transition-all cursor-pointer select-none",
                      isSelected
                        ? "bg-zinc-800 text-zinc-100 shadow-sm border border-zinc-700"
                        : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900/50",
                      isGlobalDisabled && "opacity-50 cursor-not-allowed"
                    )}
                  >
                    <Icon className={cn("h-4 w-4", isSelected ? meta.accentText : "text-zinc-500")} />
                    <span>{p.name}</span>
                    <span
                      className={cn(
                        "hidden sm:inline-block text-[10px] font-mono px-1.5 py-0.5 rounded border font-medium",
                        isSelected ? meta.badgeClass : "bg-zinc-900 border-zinc-800 text-zinc-500"
                      )}
                    >
                      {meta.shortBadge}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>

          {/* Provider Summary Strip */}
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 px-4 py-3 rounded-lg border border-zinc-800/90 bg-[#09090c] text-xs sm:text-sm shadow-inner">
            <div className="flex items-center gap-2">
              <span className="font-bold text-zinc-100">{activeProviderMeta.name}:</span>
              <span className="text-zinc-300">{activeProviderMeta.description}</span>
            </div>
            <span className="text-xs font-mono text-zinc-400 bg-zinc-900/90 px-2.5 py-1 rounded border border-zinc-800 shrink-0 select-all">
              {activeProviderMeta.baseUrl.replace("https://", "")}
            </span>
          </div>

          {/* Models Header & Controls */}
          <div className="space-y-4 pt-1">
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
              <div>
                <h3 className="text-xs sm:text-sm font-bold uppercase tracking-wider text-zinc-200">
                  Select Model Architecture ({currentModels.length} of {allProviderModelsCount} available)
                </h3>
                <span className="text-xs text-zinc-400">
                  Click a card to switch active engine
                </span>
              </div>

              {/* Quick Filter Tabs & Search */}
              <div className="flex flex-wrap items-center gap-2">
                <div className="relative">
                  <Search className="h-3.5 w-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 text-zinc-500" />
                  <input
                    type="text"
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                    placeholder="Search models..."
                    className="h-7 pl-8 pr-7 rounded-md border border-zinc-800 bg-[#09090b] text-xs text-zinc-200 placeholder:text-zinc-600 focus:outline-none focus:border-amber-400/80 focus:ring-1 focus:ring-amber-400/30 w-36 sm:w-44 transition-all"
                  />
                  {searchQuery && (
                    <button
                      type="button"
                      onClick={() => setSearchQuery("")}
                      className="absolute right-2 top-1/2 -translate-y-1/2 text-zinc-500 hover:text-zinc-300 cursor-pointer"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  )}
                </div>

                <div className="inline-flex rounded-md border border-zinc-800 bg-[#09090b] p-0.5 text-xs">
                  <button
                    type="button"
                    onClick={() => setSpeedFilter("all")}
                    className={cn(
                      "px-2.5 py-1 rounded text-[11px] font-medium transition-colors cursor-pointer",
                      speedFilter === "all"
                        ? "bg-zinc-800 text-zinc-100 font-semibold"
                        : "text-zinc-400 hover:text-zinc-200"
                    )}
                  >
                    All
                  </button>
                  <button
                    type="button"
                    onClick={() => setSpeedFilter("free")}
                    className={cn(
                      "px-2.5 py-1 rounded text-[11px] font-medium transition-colors cursor-pointer",
                      speedFilter === "free"
                        ? "bg-emerald-950/80 text-emerald-300 font-semibold border border-emerald-800/40"
                        : "text-zinc-400 hover:text-zinc-200"
                    )}
                  >
                    Free Tier
                  </button>
                  <button
                    type="button"
                    onClick={() => setSpeedFilter("fast")}
                    className={cn(
                      "px-2.5 py-1 rounded text-[11px] font-medium transition-colors cursor-pointer",
                      speedFilter === "fast"
                        ? "bg-cyan-950/80 text-cyan-300 font-semibold border border-cyan-800/40"
                        : "text-zinc-400 hover:text-zinc-200"
                    )}
                  >
                    Fast
                  </button>
                  <button
                    type="button"
                    onClick={() => setSpeedFilter("reasoning")}
                    className={cn(
                      "px-2.5 py-1 rounded text-[11px] font-medium transition-colors cursor-pointer",
                      speedFilter === "reasoning"
                        ? "bg-purple-950/80 text-purple-300 font-semibold border border-purple-800/40"
                        : "text-zinc-400 hover:text-zinc-200"
                    )}
                  >
                    Deep CoT
                  </button>
                </div>
              </div>
            </div>

            {loading ? (
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                <Skeleton className="h-48 w-full rounded-xl" />
                <Skeleton className="h-48 w-full rounded-xl" />
                <Skeleton className="h-48 w-full rounded-xl" />
              </div>
            ) : currentModels.length === 0 ? (
              <div className="rounded-xl border border-dashed border-zinc-800 p-8 text-center space-y-2 bg-[#09090b]/50">
                <Cpu className="h-8 w-8 text-zinc-600 mx-auto" />
                <p className="text-sm font-semibold text-zinc-300">No models match your current filter</p>
                <p className="text-xs text-zinc-500">
                  Try clearing your search query or selecting &quot;All&quot; models.
                </p>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    setSearchQuery("");
                    setSpeedFilter("all");
                  }}
                  className="mt-2 text-xs border-zinc-700"
                >
                  Reset Filters
                </Button>
              </div>
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                {currentModels.map((m) => {
                  const isSelected = m.id === selectedModel;
                  const isLive = activeConfig?.model_name === m.id;
                  const spec = getModelSpec(m.id, m.tag);
                  const displayName = formatModelDisplayName(m.id, m.name);

                  return (
                    <button
                      key={m.id}
                      type="button"
                      disabled={isGlobalDisabled || isPending}
                      onClick={() => setSelectedModel(m.id)}
                      className={cn(
                        "group relative flex flex-col justify-between rounded-xl border p-4 sm:p-5 text-left select-none transition-all duration-150 cursor-pointer",
                        isSelected
                          ? "border-amber-400/90 bg-gradient-to-b from-[#1c1a16] via-[#141417] to-[#0e0e11] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.15),0_4px_24px_rgba(245,158,11,0.14)] ring-1 ring-amber-400/40"
                          : "border-zinc-800/90 bg-[#0c0c0f] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.03),0_2px_8px_rgba(0,0,0,0.25)] hover:border-zinc-700 hover:bg-[#121216] hover:-translate-y-0.5",
                        "active:translate-y-0 active:scale-[0.99]",
                        isGlobalDisabled && "opacity-60 cursor-not-allowed hover:translate-y-0"
                      )}
                    >
                      <div className="space-y-3 w-full">
                        {/* Header: Tag + Live Status + Tactile Radio */}
                        <div className="flex items-center justify-between gap-2">
                          <span className={cn("text-[11px] font-mono font-semibold px-2.5 py-0.5 rounded-md border", spec.tagColor)}>
                            {m.tag}
                          </span>
                          <div className="flex items-center gap-2">
                            {isLive && (
                              <span className="text-[11px] font-mono font-semibold bg-emerald-950/80 border border-emerald-700/70 text-emerald-400 px-2 py-0.5 rounded flex items-center gap-1.5 shadow-sm">
                                <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse" />
                                Live in Prod
                              </span>
                            )}
                            <div
                              className={cn(
                                "h-4 w-4 rounded-full border-2 flex items-center justify-center transition-all duration-150",
                                isSelected
                                  ? "border-amber-400 bg-amber-400 shadow-[0_0_8px_rgba(245,158,11,0.5)]"
                                  : "border-zinc-600 bg-zinc-900 group-hover:border-zinc-400"
                              )}
                            >
                              {isSelected && <div className="h-1.5 w-1.5 rounded-full bg-zinc-950" />}
                            </div>
                          </div>
                        </div>

                        {/* Model Friendly Title & Technical Identifier Slug */}
                        <div>
                          <h4 className="text-base font-bold text-zinc-100 group-hover:text-amber-300 transition-colors tracking-tight">
                            {displayName}
                          </h4>
                          <div className="mt-1 flex items-center gap-1.5">
                            <span className="font-mono text-[11px] text-zinc-400 bg-[#070709] border border-zinc-800 px-2 py-0.5 rounded select-all font-medium truncate max-w-[200px]">
                              {m.id}
                            </span>
                            <button
                              type="button"
                              onClick={(e) => handleCopyModelId(m.id, e)}
                              title="Copy model identifier"
                              className="p-1 text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800 rounded transition-colors"
                            >
                              {copiedModelId === m.id ? (
                                <Check className="h-3 w-3 text-emerald-400" />
                              ) : (
                                <Copy className="h-3 w-3" />
                              )}
                            </button>
                          </div>
                        </div>

                        {/* Specs row: Context Window & TTFT Latency */}
                        <div className="flex items-center gap-2 text-xs font-mono pt-0.5">
                          <span className="flex items-center gap-1 bg-zinc-900/90 text-zinc-300 border border-zinc-800 px-2 py-0.5 rounded font-medium">
                            <Layers className="h-3 w-3 text-zinc-500" />
                            <span>{spec.contextWindow}</span>
                          </span>
                          <span className="flex items-center gap-1 bg-zinc-900/90 text-zinc-300 border border-zinc-800 px-2 py-0.5 rounded font-medium">
                            <Gauge className="h-3 w-3 text-zinc-500" />
                            <span>{spec.latencyRating}</span>
                          </span>
                        </div>

                        {/* Specialty Description */}
                        <p className="text-xs text-zinc-300/90 leading-relaxed font-sans line-clamp-2">
                          {spec.specialty}
                        </p>
                      </div>

                      {/* Card Footer */}
                      <div className="mt-4 pt-3 border-t border-zinc-800/80 flex items-center justify-between text-xs text-zinc-400">
                        <span className="font-medium text-zinc-400 font-mono text-[11px]">
                          {spec.recommendedRole}
                        </span>
                        <span className={cn("font-semibold text-xs flex items-center gap-1", isSelected ? "text-amber-400" : "text-zinc-500 group-hover:text-zinc-300")}>
                          {isSelected ? (
                            <>
                              <Check className="h-3.5 w-3.5" />
                              <span>Selected</span>
                            </>
                          ) : (
                            <span>Select Engine →</span>
                          )}
                        </span>
                      </div>
                    </button>
                  );
                })}
              </div>
            )}
          </div>

          {/* Action Bar & Save Configuration inside card */}
          <div className="pt-4 border-t border-zinc-800 flex flex-col sm:flex-row sm:items-center justify-between gap-4">
            <div className="flex items-center gap-2.5 text-xs sm:text-sm text-zinc-400">
              <Zap className="h-4 w-4 text-amber-400 shrink-0" />
              <span>
                Hot-switches instantaneously without restarting containers or interrupting active CI runs.
              </span>
            </div>

            <div className="flex items-center gap-3 self-end sm:self-auto">
              {isDirty && (
                <span className="text-xs font-mono font-semibold text-amber-400 bg-amber-950/40 border border-amber-800/50 px-3 py-1.5 rounded-md animate-pulse">
                  Unsaved: {formatModelDisplayName(selectedModel)}
                </span>
              )}
              <Button
                onClick={handleSaveConfig}
                disabled={isGlobalDisabled || isPending || !isDirty}
                className={cn(
                  "relative px-6 py-2.5 font-bold text-sm transition-all cursor-pointer select-none",
                  isDirty && !isGlobalDisabled
                    ? "bg-gradient-to-b from-amber-400 to-amber-500 text-zinc-950 shadow-[0_1px_0_inset_rgba(255,255,255,0.35),0_2px_8px_rgba(245,158,11,0.25)] hover:from-amber-300 hover:to-amber-400 active:shadow-[0_2px_4px_inset_rgba(0,0,0,0.25)] ring-1 ring-amber-400/40"
                    : "bg-zinc-800 text-zinc-400 hover:bg-zinc-800 border border-zinc-700/60"
                )}
                size="sm"
              >
                {isPending ? (
                  <span className="flex items-center gap-2">
                    <RefreshCw className="h-4 w-4 animate-spin text-zinc-950" />
                    <span>Applying Live...</span>
                  </span>
                ) : isDirty ? (
                  <span className="flex items-center gap-2">
                    <span>Save Configuration</span>
                    <ArrowRight className="h-4 w-4" />
                  </span>
                ) : (
                  <span className="flex items-center gap-2 text-zinc-400 font-medium">
                    <Check className="h-4 w-4 text-emerald-400" />
                    <span>Configuration In Sync</span>
                  </span>
                )}
              </Button>
            </div>
          </div>
        </div>

        {/* BOTTOM GRID: Balanced 2 Columns (Inference URL Enclave + Live Pipeline Telemetry) */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {/* Column 1: Server-Derived Base URL & Security Enclave */}
          <div className="rounded-xl border border-zinc-800 bg-[#121215] p-5 shadow-sm space-y-3.5">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Lock className="h-4 w-4 text-emerald-400" />
                <h3 className="text-sm font-semibold text-zinc-200">
                  Inference Base URL (Server-Derived)
                </h3>
              </div>
              <span className="text-xs font-mono bg-emerald-950/40 border border-emerald-800/40 text-emerald-400 px-2 py-0.5 rounded">
                TLS 1.3 Verified
              </span>
            </div>

            <div className="flex items-center justify-between gap-3 rounded-lg border border-zinc-800 bg-[#08080a] px-3.5 py-2.5 font-mono text-xs sm:text-sm text-zinc-200">
              <div className="flex items-center gap-2 truncate">
                <span className="text-zinc-500 select-none text-xs">ENDPOINT:</span>
                <span className="text-amber-300 font-semibold truncate select-all">{currentBaseUrl}</span>
              </div>
              <div className="flex items-center gap-2 shrink-0">
                <button
                  type="button"
                  onClick={() => handleCopyUrl(currentBaseUrl)}
                  className="flex items-center gap-1.5 text-xs text-zinc-300 hover:text-white border border-zinc-700/80 bg-zinc-800 px-2.5 py-1 rounded transition-colors cursor-pointer"
                >
                  {copiedUrl ? (
                    <>
                      <Check className="h-3.5 w-3.5 text-emerald-400" />
                      <span className="text-emerald-400 font-semibold">Copied</span>
                    </>
                  ) : (
                    <>
                      <Copy className="h-3.5 w-3.5 text-zinc-400" />
                      <span>Copy</span>
                    </>
                  )}
                </button>
                <span className="rounded border border-zinc-800 bg-zinc-900 px-2 py-1 text-[11px] text-zinc-400 font-mono">
                  Locked
                </span>
              </div>
            </div>

            <p className="text-xs text-zinc-400 leading-relaxed font-sans">
              Inference endpoints are generated strictly server-side by Haunter&apos;s orchestrator to prevent prompt injection and unauthorized upstream proxying.
            </p>
          </div>

          {/* Column 2: Live Pipeline Telemetry & Subagents */}
          <div className="rounded-xl border border-zinc-800 bg-[#121215] p-5 shadow-sm space-y-3.5">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Activity className="h-4 w-4 text-emerald-400" />
                <h3 className="text-sm font-semibold text-zinc-200">
                  Subagent Pipeline Telemetry
                </h3>
              </div>
              <div className="flex items-center gap-1.5 text-xs text-emerald-400 bg-emerald-950/40 border border-emerald-800/40 px-2 py-0.5 rounded font-mono">
                <span className="h-2 w-2 rounded-full bg-emerald-400 animate-pulse" />
                <span>Ready for CI Webhooks</span>
              </div>
            </div>

            {/* Stepper diagram */}
            <div className="grid grid-cols-5 gap-1.5 text-center font-mono">
              <div className="rounded border border-zinc-800 bg-[#0c0c0e] p-2 space-y-0.5">
                <span className="text-[10px] text-zinc-500 uppercase block">01</span>
                <span className="text-xs text-zinc-300 font-medium block truncate">Webhook</span>
              </div>
              <div className="rounded border border-zinc-800 bg-[#0c0c0e] p-2 space-y-0.5">
                <span className="text-[10px] text-zinc-500 uppercase block">02</span>
                <span className="text-xs text-zinc-300 font-medium block truncate">Gatherer</span>
              </div>
              <div className="rounded border border-amber-500/50 bg-amber-400/10 p-2 space-y-0.5 ring-1 ring-amber-400/30 shadow-[0_0_12px_rgba(245,158,11,0.1)]">
                <span className="text-[10px] text-amber-400 uppercase block font-semibold">03 Fixer</span>
                <span className="text-xs text-amber-300 font-bold block truncate">
                  {formatModelDisplayName(selectedModel)}
                </span>
              </div>
              <div className="rounded border border-zinc-800 bg-[#0c0c0e] p-2 space-y-0.5">
                <span className="text-[10px] text-zinc-500 uppercase block">04</span>
                <span className="text-xs text-zinc-300 font-medium block truncate">Sandbox</span>
              </div>
              <div className="rounded border border-zinc-800 bg-[#0c0c0e] p-2 space-y-0.5">
                <span className="text-[10px] text-zinc-500 uppercase block">05</span>
                <span className="text-xs text-zinc-300 font-medium block truncate">PR Writer</span>
              </div>
            </div>

            <div className="flex items-center justify-between text-xs text-zinc-400 font-sans pt-0.5">
              <span>Orchestrator: <strong className="text-zinc-200 font-medium">Async Subagents (SQS/Lambda)</strong></span>
              <span>Routing: <strong className="text-emerald-400 font-medium font-mono">Allowlist Enforced</strong></span>
            </div>
          </div>
        </div>

        {/* FLOATING ACTION DOCK: When changes are dirty, provide instant saving anywhere on the page */}
        {isDirty && !isGlobalDisabled && (
          <div className="fixed bottom-6 inset-x-0 z-40 flex justify-center px-4 pointer-events-none animate-in fade-in slide-in-from-bottom-5 duration-200">
            <div className="pointer-events-auto flex flex-col sm:flex-row items-center justify-between gap-3 sm:gap-6 max-w-2xl w-full rounded-2xl border border-amber-500/40 bg-[#121215]/95 p-3.5 sm:px-5 sm:py-3 shadow-[0_8px_32px_rgba(0,0,0,0.6),0_0_24px_rgba(245,158,11,0.18)] backdrop-blur-md ring-1 ring-amber-400/30">
              <div className="flex items-center gap-3 text-xs sm:text-sm">
                <span className="relative flex h-2.5 w-2.5">
                  <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-75" />
                  <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-amber-400" />
                </span>
                <div className="text-zinc-200">
                  <span className="text-zinc-400">Targeting: </span>
                  <strong className="text-amber-300 font-semibold font-mono bg-zinc-900 border border-zinc-700/80 px-2 py-0.5 rounded">
                    {formatModelDisplayName(selectedModel)}
                  </strong>
                </div>
              </div>

              <div className="flex items-center gap-2.5 self-end sm:self-auto">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleDiscardChanges}
                  disabled={isPending}
                  className="h-8 border-zinc-700 bg-zinc-900 text-zinc-300 hover:bg-zinc-800 text-xs cursor-pointer"
                >
                  <RotateCcw className="h-3 w-3 mr-1 text-zinc-400" />
                  <span>Discard</span>
                </Button>

                <Button
                  size="sm"
                  onClick={handleSaveConfig}
                  disabled={isPending}
                  className="h-8 bg-gradient-to-b from-amber-400 to-amber-500 text-zinc-950 font-bold hover:from-amber-300 hover:to-amber-400 px-4 text-xs shadow-[0_1px_0_inset_rgba(255,255,255,0.35),0_2px_8px_rgba(245,158,11,0.25)] ring-1 ring-amber-400/40 gap-1.5 cursor-pointer"
                >
                  {isPending ? (
                    <>
                      <RefreshCw className="h-3.5 w-3.5 animate-spin" />
                      <span>Applying...</span>
                    </>
                  ) : (
                    <>
                      <span>Apply Configuration</span>
                      <kbd className="hidden sm:inline-block rounded bg-zinc-950/20 px-1 py-0.2 text-[10px] font-mono text-zinc-900 border border-zinc-950/30">
                        ⌘S
                      </kbd>
                    </>
                  )}
                </Button>
              </div>
            </div>
          </div>
        )}
      </div>
    </AppLayout>
  );
}
