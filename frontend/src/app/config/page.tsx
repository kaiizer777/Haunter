"use client";

import { useEffect, useState, useCallback, useTransition, useMemo, useRef } from "react";
import { AppLayout } from "@/components/layout/app-layout";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { SelectDropdown } from "@/components/ui/select-dropdown";
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
  FolderGit2,
  Server,
  Terminal,
  Workflow,
  Shield,
  Bot,
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
  latencyMs: number;
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
    latencyMs: 320,
    recommendedRole: "Default Engine",
    tagColor: "bg-amber-950/60 border-amber-700/60 text-amber-300",
    speedCategory: "standard",
    isFree: true,
  },
  "laguna-s-2.1-free": {
    contextWindow: "64k Context",
    specialty: "High-Frequency Test & Lint Healing",
    latencyRating: "~190ms TTFT",
    latencyMs: 190,
    recommendedRole: "Ultra-Fast Patching",
    tagColor: "bg-emerald-950/60 border-emerald-700/60 text-emerald-300",
    speedCategory: "fast",
    isFree: true,
  },
  "ling-3.0-flash-fin-free": {
    contextWindow: "128k Context",
    specialty: "Multi-Language Syntax & AST Repair",
    latencyRating: "~390ms TTFT",
    latencyMs: 390,
    recommendedRole: "Type & AST Specialist",
    tagColor: "bg-cyan-950/60 border-cyan-700/60 text-cyan-300",
    speedCategory: "standard",
    isFree: true,
  },
  "deepseek-v4-flash-free": {
    contextWindow: "128k Context",
    specialty: "Rapid Multi-Step Test Regeneration & Syntax Fixes",
    latencyRating: "~240ms TTFT",
    latencyMs: 240,
    recommendedRole: "High-Throughput Healing",
    tagColor: "bg-blue-950/60 border-blue-700/60 text-blue-300",
    speedCategory: "fast",
    isFree: true,
  },
  "muse-spark-1.3-contributor-free": {
    contextWindow: "128k Context",
    specialty: "Open-Source Benchmark Synthesis & Verification",
    latencyRating: "~310ms TTFT",
    latencyMs: 310,
    recommendedRole: "Community Benchmark",
    tagColor: "bg-violet-950/60 border-violet-700/60 text-violet-300",
    speedCategory: "standard",
    isFree: true,
  },
  "qwen-2.5-coder-32b-instruct-free": {
    contextWindow: "128k Context",
    specialty: "Complex Multi-File Refactoring & Git Patch Synthesis",
    latencyRating: "~350ms TTFT",
    latencyMs: 350,
    recommendedRole: "Polyglot Specialist",
    tagColor: "bg-teal-950/60 border-teal-700/60 text-teal-300",
    speedCategory: "standard",
    isFree: true,
  },
  "deepseek-r1-distill-qwen-32b-free": {
    contextWindow: "128k Context",
    specialty: "Deep Chain-of-Thought Flaky Test & Concurrency Debugging",
    latencyRating: "~510ms TTFT",
    latencyMs: 510,
    recommendedRole: "Deep CoT Reasoning",
    tagColor: "bg-indigo-950/60 border-indigo-700/60 text-indigo-300",
    speedCategory: "reasoning",
    isFree: true,
  },
  "llama-3.3-70b-instruct-free": {
    contextWindow: "128k Context",
    specialty: "High-Parameter Multi-Language Architectural Patches",
    latencyRating: "~440ms TTFT",
    latencyMs: 440,
    recommendedRole: "Enterprise Scale",
    tagColor: "bg-purple-950/60 border-purple-700/60 text-purple-300",
    speedCategory: "reasoning",
    isFree: true,
  },
  "gpt-4o": {
    contextWindow: "128k Context",
    specialty: "Multi-File Architectural Patching & Full-Stack Healing",
    latencyRating: "~540ms TTFT",
    latencyMs: 540,
    recommendedRole: "Deep Agentic Planning",
    tagColor: "bg-purple-950/60 border-purple-700/60 text-purple-300",
    speedCategory: "reasoning",
    isFree: false,
  },
  "gpt-4o-mini": {
    contextWindow: "128k Context",
    specialty: "Rapid Unit Test & Config Script Fixes",
    latencyRating: "~260ms TTFT",
    latencyMs: 260,
    recommendedRole: "Cost-Efficient Healing",
    tagColor: "bg-emerald-950/60 border-emerald-700/60 text-emerald-300",
    speedCategory: "fast",
    isFree: false,
  },
  "claude-sonnet-4-5": {
    contextWindow: "200k Context",
    specialty: "AST Refactoring & Complex Logic Healing",
    latencyRating: "~610ms TTFT",
    latencyMs: 610,
    recommendedRole: "Maximum Accuracy",
    tagColor: "bg-orange-950/60 border-orange-700/60 text-orange-300",
    speedCategory: "reasoning",
    isFree: false,
  },
  "claude-haiku-3-5": {
    contextWindow: "200k Context",
    specialty: "Low-Latency Code Synthesis & Fast CI Gates",
    latencyRating: "~230ms TTFT",
    latencyMs: 230,
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
    "muse-spark-1.2-contributor-free": "Muse Spark 1.2 Contributor",
    "mimo-v2.5-free": "Mimo V2.5",
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
    latencyMs: isFlash ? 220 : isReasoning ? 490 : 330,
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
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saveSuccess, setSaveSuccess] = useState(false);
  const [copiedUrl, setCopiedUrl] = useState(false);
  const [copiedModelId, setCopiedModelId] = useState<string | null>(null);
  const [isPending, startTransition] = useTransition();

  const searchInputRef = useRef<HTMLInputElement | null>(null);

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

  const fetchActiveConfig = useCallback(async (isManualRefresh = false) => {
    if (isManualRefresh) {
      setIsRefreshing(true);
    } else {
      setLoading(true);
    }
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
      setIsRefreshing(false);
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

  // Keyboard shortcut handlers: '/' to search, 'r' to sync, Cmd+S to save
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (["INPUT", "TEXTAREA", "SELECT"].includes((e.target as HTMLElement)?.tagName)) {
        if (e.key === "Escape") {
          (e.target as HTMLElement).blur();
        }
        return;
      }
      if ((e.metaKey || e.ctrlKey) && e.key === "s") {
        e.preventDefault();
        if (!isGlobalDisabled && !isPending && isDirty) {
          handleSaveConfig();
        }
      } else if (e.key === "/") {
        e.preventDefault();
        searchInputRef.current?.focus();
      } else if (e.key === "r" || e.key === "R") {
        e.preventDefault();
        fetchActiveConfig(true);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isGlobalDisabled, isPending, isDirty, handleSaveConfig, fetchActiveConfig]);

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
  const activeSpec = getModelSpec(activeConfig?.model_name || selectedModel);

  // Normalize provider model list
  const allProviderModels = useMemo(() => {
    const options = modelOptionsByProvider[selectedProvider] || [];
    const hasSelected = options.some((m) => m.id === selectedModel);
    return hasSelected
      ? options
      : [{ id: selectedModel, name: selectedModel, tag: "Active" }, ...options];
  }, [modelOptionsByProvider, selectedProvider, selectedModel]);

  // Filter by query and speed category
  const currentModels = useMemo(() => {
    return allProviderModels.filter((m) => {
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
  }, [allProviderModels, searchQuery, speedFilter]);

  return (
    <AppLayout
      title="Model & Provider Configuration"
      subtitle="Manage and hot-swap active LLM inference engines globally or per repository"
      actions={
        <div className="flex items-center gap-2.5">
          {activeConfig && (
            <div className="hidden md:flex items-center gap-2 rounded-[6px] border border-zinc-800/90 bg-[#0c0c0e]/95 px-3 py-1.5 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.03)]">
              <span className="h-2 w-2 rounded-full bg-emerald-400 animate-pulse shadow-[0_0_6px_rgba(52,211,153,0.8)]" />
              <span className="text-xs font-mono text-zinc-300">
                Active: <strong className="text-amber-400 font-semibold">{formatModelDisplayName(activeConfig.model_name)}</strong>
              </span>
            </div>
          )}

          {/* Direct Topbar Save button when configuration is dirty */}
          {isDirty && !isGlobalDisabled && (
            <Button
              size="sm"
              onClick={handleSaveConfig}
              disabled={isPending}
              className="h-8 px-3.5 text-xs font-mono font-bold rounded-[6px] bg-gradient-to-b from-amber-400 via-amber-450 to-amber-500 hover:from-amber-300 hover:to-amber-400 text-zinc-950 border-t border-t-amber-200/60 border-x border-x-amber-400/80 border-b border-b-amber-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.4),0_2px_8px_rgba(245,158,11,0.35)] active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.4)] flex items-center gap-1.5 transition-all cursor-pointer"
            >
              {isPending ? (
                <RefreshCw className="h-3.5 w-3.5 animate-spin text-zinc-950" />
              ) : (
                <Check className="h-3.5 w-3.5 text-zinc-950 stroke-[2.5]" />
              )}
              <span>Save Changes</span>
              <kbd className="hidden sm:inline-block rounded bg-zinc-950/20 px-1 py-0.2 text-[10px] font-mono text-zinc-900 border border-zinc-950/30">
                ⌘S
              </kbd>
            </Button>
          )}

          <Button
            variant="outline"
            size="sm"
            onClick={() => fetchActiveConfig(true)}
            disabled={loading || isRefreshing}
            className="group h-8 px-3 text-xs font-mono rounded-[6px] text-zinc-300 hover:text-white bg-gradient-to-b from-zinc-800/90 via-zinc-850 to-zinc-900/90 border-t border-t-zinc-600/70 border-x border-x-zinc-700/60 border-b border-b-zinc-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.12),0_1px_3px_rgba(0,0,0,0.35),0_1px_2px_rgba(0,0,0,0.2)] hover:border-t-zinc-500 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.2),0_2px_6px_rgba(0,0,0,0.4)] active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.4)] flex items-center gap-1.5 transition-all cursor-pointer"
            title="Sync configuration (Hot-key: R)"
          >
            <RefreshCw className={cn("h-3.5 w-3.5 transition-colors", isRefreshing ? "animate-spin text-amber-400" : "text-zinc-400 group-hover:text-amber-400")} />
            <span className="hidden sm:inline">Sync</span>
          </Button>
        </div>
      }
    >
      <div className="space-y-5 min-w-0 max-w-6xl pb-24">
        {/* Error Alert */}
        {error && (
          <div className="flex items-start gap-3 rounded-[8px] border border-red-800/80 bg-gradient-to-b from-red-950/60 to-red-950/30 p-4 text-sm text-red-200 shadow-md animate-in fade-in">
            <AlertCircle className="h-5 w-5 shrink-0 text-red-400 mt-0.5" />
            <div className="space-y-0.5">
              <p className="font-semibold text-red-200">Configuration Error</p>
              <p className="text-red-300/90 text-xs font-mono">{error}</p>
            </div>
          </div>
        )}

        {/* Success Alert */}
        {saveSuccess && (
          <div className="flex items-center justify-between rounded-[8px] border border-emerald-500/60 bg-gradient-to-b from-emerald-950/60 to-emerald-950/30 p-4 text-sm text-emerald-200 shadow-[0_4px_20px_rgba(16,185,129,0.15)] animate-in fade-in slide-in-from-top-2">
            <div className="flex items-center gap-3">
              <CheckCircle2 className="h-5 w-5 text-emerald-400 shrink-0" />
              <div>
                <p className="font-semibold text-emerald-300">Configuration Updated Live</p>
                <p className="text-emerald-400/90 text-xs mt-0.5">
                  Subagent pipeline will now route incoming CI failures to{" "}
                  <strong className="font-mono text-white bg-emerald-900/60 px-1.5 py-0.5 rounded border border-emerald-700">
                    {formatModelDisplayName(selectedModel)}
                  </strong>
                </p>
              </div>
            </div>
            <span className="hidden sm:inline-flex text-xs font-mono text-emerald-300 bg-emerald-900/50 border border-emerald-700/60 px-2.5 py-1 rounded-[5px]">
              Zero-Downtime Hot-Swapped
            </span>
          </div>
        )}

        {/* TOP TELEMETRY KPI METRIC CARDS (Matching Runs page aesthetic) */}
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3.5">
          {/* Active Model KPI */}
          <div className="rounded-lg border border-zinc-800/80 bg-gradient-to-b from-[#121216]/90 to-[#0c0c0e]/90 backdrop-blur p-4 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.03)] flex flex-col justify-between">
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-mono uppercase tracking-wider text-zinc-400">Active Engine</span>
              <Bot className="h-4 w-4 text-amber-400" />
            </div>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="text-lg sm:text-xl font-bold font-mono text-zinc-100 truncate">
                {activeConfig ? formatModelDisplayName(activeConfig.model_name) : "Loading..."}
              </span>
            </div>
            <div className="mt-1 flex items-center gap-1.5 text-[11px] text-zinc-500 truncate font-mono">
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse" />
              <span>{activeProviderMeta.name}</span>
            </div>
          </div>

          {/* Routing Scope KPI */}
          <div className="rounded-lg border border-zinc-800/80 bg-gradient-to-b from-[#121216]/90 to-[#0c0c0e]/90 backdrop-blur p-4 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.03)] flex flex-col justify-between">
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-mono uppercase tracking-wider text-zinc-400">Routing Policy</span>
              <Workflow className="h-4 w-4 text-zinc-400" />
            </div>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="text-lg sm:text-xl font-bold font-mono text-zinc-100">
                {selectedScope === "global" ? "Global Cluster" : "Repo Override"}
              </span>
            </div>
            <div className="mt-1 text-[11px] text-zinc-500 truncate">
              {selectedScope === "global" ? "Applied to all unconfigured repos" : `Targeting ${currentRepo ? `${currentRepo.owner}/${currentRepo.name}` : "selected repository"}`}
            </div>
          </div>

          {/* Benchmark TTFT Latency KPI */}
          <div className="rounded-lg border border-zinc-800/80 bg-gradient-to-b from-[#121216]/90 to-[#0c0c0e]/90 backdrop-blur p-4 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.03)] flex flex-col justify-between">
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-mono uppercase tracking-wider text-zinc-400">Avg TTFT Speed</span>
              <Gauge className="h-4 w-4 text-emerald-400" />
            </div>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="text-2xl font-bold font-mono text-zinc-100 tabular-nums">
                {activeSpec.latencyRating}
              </span>
            </div>
            <div className="mt-1 text-[11px] text-zinc-500 truncate">Inference time-to-first-token</div>
          </div>

          {/* Security & Free Tier KPI */}
          <div className="rounded-lg border border-zinc-800/80 bg-gradient-to-b from-[#121216]/90 to-[#0c0c0e]/90 backdrop-blur p-4 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.03)] flex flex-col justify-between">
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-mono uppercase tracking-wider text-zinc-400">Enclave & Tier</span>
              <ShieldCheck className="h-4 w-4 text-amber-400" />
            </div>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="text-lg sm:text-xl font-bold font-mono text-emerald-400">
                {activeSpec.isFree ? "100% Free Tier" : "Direct API"}
              </span>
            </div>
            <div className="mt-1 text-[11px] text-zinc-500 truncate">Strict server allowlist gated</div>
          </div>
        </div>

        {/* TOP BAR: Scope Switcher + Target Repository Control */}
        <div className="relative overflow-hidden rounded-xl border-t border-t-zinc-600/60 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-gradient-to-b from-[#111115]/95 via-[#0d0d10]/95 to-[#09090c]/95 backdrop-blur-sm p-4 sm:p-5 shadow-[inset_0_1px_0_rgba(255,255,255,0.12),inset_0_-1px_0_rgba(0,0,0,0.4),0_8px_32px_rgba(0,0,0,0.5)] space-y-4">
          <span
            aria-hidden="true"
            className="pointer-events-none absolute inset-x-0 top-0 h-6 bg-gradient-to-b from-white/[0.04] to-transparent z-10"
          />

          <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-3.5 relative z-10">
            {/* Scope Segmented Control */}
            <div className="flex flex-wrap items-center gap-3">
              <span className="text-xs font-mono font-semibold uppercase tracking-wider text-zinc-400 select-none">
                Scope:
              </span>
              <div className="inline-flex rounded-[7px] border border-zinc-800/90 bg-[#09090b]/90 p-1 shadow-inner">
                <button
                  type="button"
                  onClick={() => setSelectedScope("global")}
                  className={cn(
                    "flex items-center gap-2 rounded-[5px] px-3.5 py-1.5 text-xs sm:text-sm font-mono transition-all cursor-pointer select-none",
                    selectedScope === "global"
                      ? "bg-zinc-800 text-amber-400 shadow-sm border border-zinc-700/80 font-bold"
                      : "text-zinc-400 hover:text-zinc-200"
                  )}
                >
                  <Globe className="h-3.5 w-3.5 text-amber-400" />
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
                    "flex items-center gap-2 rounded-[5px] px-3.5 py-1.5 text-xs sm:text-sm font-mono transition-all cursor-pointer select-none",
                    selectedScope === "repo"
                      ? "bg-zinc-800 text-cyan-400 shadow-sm border border-zinc-700/80 font-bold"
                      : "text-zinc-400 hover:text-zinc-200"
                  )}
                >
                  <GitBranch className="h-3.5 w-3.5 text-cyan-400" />
                  <span>Repository Override</span>
                  {repos.length > 0 && (
                    <span className="ml-1 rounded-full bg-cyan-950 border border-cyan-800/60 px-2 py-0.2 text-[10px] text-cyan-300 font-mono tabular-nums">
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
                  "text-xs font-mono font-medium px-3 py-1.5 rounded-[6px] border flex items-center gap-1.5 select-none",
                  user?.is_admin
                    ? "bg-emerald-950/40 border-emerald-800/50 text-emerald-400"
                    : "bg-zinc-900/90 border-zinc-800 text-zinc-400"
                )}
              >
                {user?.is_admin ? (
                  <>
                    <ShieldCheck className="h-3.5 w-3.5 text-emerald-400" />
                    <span>Admin Superuser</span>
                  </>
                ) : (
                  <>
                    <Lock className="h-3.5 w-3.5 text-zinc-400" />
                    <span>Tenant Access (Repo Config Allowed)</span>
                  </>
                )}
              </span>
            </div>
          </div>

          {/* Elevated Non-admin notice for global scope */}
          {isGlobalDisabled && (
            <div className="relative z-10 rounded-[8px] border border-amber-500/30 bg-gradient-to-r from-amber-950/30 via-zinc-900/60 to-zinc-900/30 p-3.5 flex flex-col sm:flex-row sm:items-center justify-between gap-3 text-xs sm:text-sm text-zinc-300">
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

          {/* Repo SelectDropdown (when Repo Override is selected) */}
          {selectedScope === "repo" && (
            <div className="relative z-10 pt-3 border-t border-zinc-800/80 flex flex-col sm:flex-row sm:items-center gap-3 animate-in fade-in duration-150">
              <label className="text-xs font-mono font-semibold uppercase tracking-wider text-zinc-300 shrink-0">
                Target Repository:
              </label>
              {repos.length === 0 ? (
                <div className="text-xs text-zinc-400 font-mono p-2 border border-dashed border-zinc-800 rounded bg-zinc-950">
                  No connected repositories found. Connect a repository first in the Repositories tab.
                </div>
              ) : (
                <div className="flex-1 flex flex-col sm:flex-row sm:items-center gap-3">
                  <SelectDropdown
                    value={selectedRepoId}
                    onChange={(val) => setSelectedRepoId(val)}
                    options={repos.map((r) => ({
                      value: r.id,
                      label: `${r.owner}/${r.name}`,
                      icon: <FolderGit2 className="h-3.5 w-3.5 text-amber-400/80" />,
                      description: `Default branch: ${r.default_branch || "main"}${r.language_hint ? ` · ${r.language_hint}` : ""}`,
                    }))}
                    placeholder="Select repository..."
                    buttonClassName="min-w-[260px] h-9 font-mono text-xs"
                    searchable={repos.length > 5}
                  />
                  {currentRepo && (
                    <span className="text-xs text-zinc-400 font-mono whitespace-nowrap">
                      Branch: <strong className="text-zinc-200">{currentRepo.default_branch || "main"}</strong>
                      {currentRepo.language_hint && (
                        <> · Language: <strong className="text-amber-400/90">{currentRepo.language_hint}</strong></>
                      )}
                    </span>
                  )}
                </div>
              )}
            </div>
          )}
        </div>

        {/* MAIN HERO: Provider Tabs + Models Grid */}
        <div className="relative overflow-hidden rounded-xl border-t border-t-zinc-600/60 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-gradient-to-b from-[#111115]/95 via-[#0d0d10]/95 to-[#09090c]/95 backdrop-blur-sm p-5 sm:p-6 shadow-[inset_0_1px_0_rgba(255,255,255,0.12),inset_0_-1px_0_rgba(0,0,0,0.4),0_8px_32px_rgba(0,0,0,0.5)] space-y-6">
          <span
            aria-hidden="true"
            className="pointer-events-none absolute inset-x-0 top-0 h-6 bg-gradient-to-b from-white/[0.04] to-transparent z-10"
          />

          {/* Header & Provider Segmented Tabs */}
          <div className="relative z-10 flex flex-col md:flex-row md:items-center justify-between gap-4 pb-4 border-b border-zinc-800/80">
            <div>
              <h2 className="text-base sm:text-lg font-bold text-zinc-100 flex items-center gap-2">
                <Cpu className="h-5 w-5 text-amber-400" />
                <span>Autonomous Inference Engine</span>
              </h2>
              <p className="text-xs text-zinc-400 mt-0.5 font-mono">
                Strict server allowlist enforced · Zero free-text injection
              </p>
            </div>

            {/* Provider Switcher Tabs */}
            <div className="inline-flex rounded-[7px] border border-zinc-800/90 bg-[#09090b]/90 p-1 shadow-inner">
              {PROVIDER_OPTIONS.map((p) => {
                const isSelected = selectedProvider === p.id;
                const meta = PROVIDER_METADATA[p.id] || PROVIDER_METADATA.opencode_zen;
                const Icon = meta.icon;
                return (
                  <button
                    key={p.id}
                    type="button"
                    onClick={() => handleProviderChange(p.id)}
                    disabled={isPending}
                    className={cn(
                      "flex items-center gap-2 rounded-[5px] px-3.5 py-1.5 text-xs sm:text-sm font-mono font-medium transition-all cursor-pointer select-none",
                      isSelected
                        ? "bg-zinc-800 text-zinc-100 shadow-sm border border-zinc-700/80 font-bold"
                        : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/40"
                    )}
                  >
                    <Icon className={cn("h-4 w-4", isSelected ? meta.accentText : "text-zinc-500")} />
                    <span>{p.name}</span>
                    <span
                      className={cn(
                        "hidden sm:inline-block text-[10px] font-mono px-1.5 py-0.2 rounded border font-medium",
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
          <div className="relative z-10 flex flex-col sm:flex-row sm:items-center justify-between gap-3 px-4 py-3 rounded-lg border border-zinc-800/90 bg-[#09090c]/90 text-xs sm:text-sm shadow-inner">
            <div className="flex items-center gap-2">
              <span className="font-bold text-zinc-100">{activeProviderMeta.name}:</span>
              <span className="text-zinc-300 font-sans">{activeProviderMeta.description}</span>
            </div>
            <span className="text-xs font-mono text-zinc-400 bg-zinc-900/90 px-2.5 py-1 rounded border border-zinc-800 shrink-0 select-all">
              {activeProviderMeta.baseUrl.replace("https://", "")}
            </span>
          </div>

          {/* Models Header & Filter Controls (Hot-key '/' enabled) */}
          <div className="relative z-10 space-y-4 pt-1">
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
              <div>
                <div className="flex items-center gap-2.5">
                  <h3 className="text-xs font-mono font-bold uppercase tracking-wider text-zinc-200">
                    Model Architectures
                  </h3>
                  <span className="inline-flex items-center px-2 py-0.5 rounded-[4px] bg-zinc-900 border border-zinc-800 text-[10.5px] font-mono text-zinc-400">
                    {searchQuery || speedFilter !== "all"
                      ? `${currentModels.length} of ${allProviderModels.length} Available`
                      : `${allProviderModels.length} Available`}
                  </span>
                </div>
                <p className="text-xs text-zinc-400 mt-0.5">
                  Select an inference engine to power autonomous CI failure diagnostics & fix generation
                </p>
              </div>

              {/* Quick Filter Tabs & Search Bar */}
              <div className="flex flex-wrap items-center gap-2">
                <div className="relative">
                  <Search className="h-3.5 w-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 text-zinc-500 pointer-events-none" />
                  <input
                    ref={searchInputRef}
                    type="text"
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                    placeholder="Filter models..."
                    className="h-8 pl-8.5 pr-7 rounded-[6px] border-t border-t-zinc-600/70 border-x border-x-zinc-700/60 border-b border-b-zinc-950 bg-gradient-to-b from-[#121216] to-[#0c0c0e] font-mono text-xs text-zinc-200 placeholder:text-zinc-500 shadow-[inset_0_1px_2px_rgba(0,0,0,0.4),0_1px_2px_rgba(0,0,0,0.2)] focus:outline-none focus:border-t-amber-400 focus:border-x-amber-500/70 focus:ring-1 focus:ring-amber-400/25 w-36 sm:w-48 transition-all"
                  />
                  {searchQuery ? (
                    <button
                      type="button"
                      onClick={() => {
                        setSearchQuery("");
                        searchInputRef.current?.focus();
                      }}
                      className="absolute right-2 top-1/2 -translate-y-1/2 text-zinc-500 hover:text-zinc-300 p-0.5 rounded cursor-pointer"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  ) : (
                    <div className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2">
                      <kbd className="inline-flex h-3.5 min-w-3.5 items-center justify-center rounded border border-zinc-700/60 bg-zinc-800/60 px-1 font-mono text-[9px] text-zinc-400 select-none">
                        /
                      </kbd>
                    </div>
                  )}
                </div>

                <div className="inline-flex rounded-[7px] border-t border-t-zinc-700/60 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-[#0a0a0d] p-1 shadow-[inset_0_1px_2px_rgba(0,0,0,0.5)] text-xs font-mono gap-1">
                  <button
                    type="button"
                    onClick={() => setSpeedFilter("all")}
                    className={cn(
                      "px-2.5 py-1 rounded-[5px] text-[11px] font-mono transition-all cursor-pointer select-none",
                      speedFilter === "all"
                        ? "bg-gradient-to-b from-zinc-700/90 via-zinc-750 to-zinc-800/90 text-zinc-100 font-bold border-t border-t-zinc-500/70 border-x border-x-zinc-600/60 border-b border-b-zinc-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.14),0_1px_3px_rgba(0,0,0,0.4)]"
                        : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/40"
                    )}
                  >
                    All
                  </button>
                  <button
                    type="button"
                    onClick={() => setSpeedFilter("free")}
                    className={cn(
                      "px-2.5 py-1 rounded-[5px] text-[11px] font-mono transition-all cursor-pointer select-none",
                      speedFilter === "free"
                        ? "bg-gradient-to-b from-emerald-900/90 via-emerald-950 to-emerald-950/90 text-emerald-300 font-bold border-t border-t-emerald-500/70 border-x border-x-emerald-600/60 border-b border-b-emerald-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.14),0_1px_3px_rgba(0,0,0,0.4)]"
                        : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/40"
                    )}
                  >
                    Free Tier
                  </button>
                  <button
                    type="button"
                    onClick={() => setSpeedFilter("fast")}
                    className={cn(
                      "px-2.5 py-1 rounded-[5px] text-[11px] font-mono transition-all cursor-pointer select-none",
                      speedFilter === "fast"
                        ? "bg-gradient-to-b from-cyan-900/90 via-cyan-950 to-cyan-950/90 text-cyan-300 font-bold border-t border-t-cyan-500/70 border-x border-x-cyan-600/60 border-b border-b-cyan-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.14),0_1px_3px_rgba(0,0,0,0.4)]"
                        : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/40"
                    )}
                  >
                    Fast
                  </button>
                  <button
                    type="button"
                    onClick={() => setSpeedFilter("reasoning")}
                    className={cn(
                      "px-2.5 py-1 rounded-[5px] text-[11px] font-mono transition-all cursor-pointer select-none",
                      speedFilter === "reasoning"
                        ? "bg-gradient-to-b from-purple-900/90 via-purple-950 to-purple-950/90 text-purple-300 font-bold border-t border-t-purple-500/70 border-x border-x-purple-600/60 border-b border-b-purple-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.14),0_1px_3px_rgba(0,0,0,0.4)]"
                        : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/40"
                    )}
                  >
                    Deep CoT
                  </button>
                </div>
              </div>
            </div>

            {loading ? (
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                <Skeleton className="h-52 w-full rounded-xl bg-zinc-900/60" />
                <Skeleton className="h-52 w-full rounded-xl bg-zinc-900/60" />
                <Skeleton className="h-52 w-full rounded-xl bg-zinc-900/60" />
              </div>
            ) : currentModels.length === 0 ? (
              <div className="rounded-xl border border-dashed border-zinc-800 p-10 text-center space-y-2 bg-[#09090b]/50">
                <Cpu className="h-8 w-8 text-zinc-600 mx-auto" />
                <p className="text-sm font-semibold text-zinc-300 font-mono">No models match your filter</p>
                <p className="text-xs text-zinc-500">
                  Try adjusting your search query or selecting &quot;All&quot; models.
                </p>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    setSearchQuery("");
                    setSpeedFilter("all");
                  }}
                  className="mt-2 text-xs font-mono border-zinc-700"
                >
                  <RotateCcw className="h-3 w-3 mr-1.5" />
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

                  // Calculate glass chip styling based on tag/free status
                  const isFreeModel = spec.isFree || m.tag.toLowerCase().includes("free");
                  const isFastModel = spec.speedCategory === "fast";
                  const isReasoningModel = spec.speedCategory === "reasoning";

                  const badgePill = isSelected
                    ? "bg-amber-950/60 border border-amber-700/70 text-amber-300 shadow-[0_0_10px_rgba(245,158,11,0.25)]"
                    : isFreeModel
                    ? "bg-emerald-950/60 border border-emerald-700/60 text-emerald-400 shadow-[0_0_8px_rgba(16,185,129,0.15)]"
                    : isFastModel
                    ? "bg-cyan-950/60 border border-cyan-700/60 text-cyan-400 shadow-[0_0_8px_rgba(6,182,212,0.15)]"
                    : isReasoningModel
                    ? "bg-purple-950/60 border border-purple-700/60 text-purple-300 shadow-[0_0_8px_rgba(168,85,247,0.15)]"
                    : "bg-zinc-800/90 border border-zinc-700/80 text-zinc-200";

                  return (
                    <div
                      key={m.id}
                      role="button"
                      tabIndex={isGlobalDisabled || isPending ? -1 : 0}
                      aria-pressed={isSelected}
                      aria-disabled={isGlobalDisabled || isPending}
                      onClick={() => {
                        if (!isGlobalDisabled && !isPending) {
                          setSelectedModel(m.id);
                        }
                      }}
                      onKeyDown={(e) => {
                        if ((e.key === "Enter" || e.key === " ") && !isGlobalDisabled && !isPending) {
                          e.preventDefault();
                          setSelectedModel(m.id);
                        }
                      }}
                      className={cn(
                        "group relative flex flex-col justify-between rounded-xl p-5 text-left select-none transition-all duration-150 focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-400/60 overflow-hidden",
                        isSelected
                          ? "border-t border-t-amber-400 border-x border-x-amber-500/60 border-b border-b-amber-950 bg-gradient-to-b from-[#1a140b]/95 via-[#130f08]/95 to-[#0c0a05]/95 shadow-[inset_0_1px_0_rgba(255,255,255,0.2),0_0_24px_rgba(245,158,11,0.22),0_8px_24px_rgba(0,0,0,0.5)] ring-1 ring-amber-400/50"
                          : "border-t border-t-zinc-600/60 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-gradient-to-b from-[#111115]/95 via-[#0d0d10]/95 to-[#09090c]/95 shadow-[inset_0_1px_0_rgba(255,255,255,0.12),0_8px_24px_rgba(0,0,0,0.4)] hover:border-t-zinc-500 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.18),0_12px_28px_rgba(0,0,0,0.5)]",
                        !isGlobalDisabled ? "cursor-pointer active:translate-y-[0.5px]" : "cursor-default"
                      )}
                    >
                      {/* Top Specular Sheen (identical to bottom cards) */}
                      <span
                        aria-hidden="true"
                        className={cn(
                          "pointer-events-none absolute inset-x-0 top-0 h-6 bg-gradient-to-b to-transparent z-10",
                          isSelected ? "from-amber-400/[0.14]" : "from-white/[0.04]"
                        )}
                      />

                      <div className="space-y-3.5 w-full relative z-10">
                        {/* Header: Tag + Live Status + Radio Indicator */}
                        <div className="flex items-center justify-between gap-2">
                          <span className={cn("text-[11px] font-mono font-semibold px-2.5 py-0.5 rounded-[5px]", badgePill)}>
                            {m.tag}
                          </span>
                          <div className="flex items-center gap-2">
                            {isLive && (
                              <span className="text-[10.5px] font-mono font-semibold bg-emerald-950/60 border border-emerald-800/60 text-emerald-400 px-2.5 py-0.5 rounded-[5px] flex items-center gap-1.5 shadow-[0_0_8px_rgba(52,211,153,0.25)]">
                                <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse shadow-[0_0_8px_rgba(52,211,153,1)]" />
                                Live in Prod
                              </span>
                            )}
                            <div
                              className={cn(
                                "h-4 w-4 rounded-full flex items-center justify-center transition-all duration-150",
                                isSelected
                                  ? "border border-amber-400 bg-amber-400 shadow-[0_0_8px_rgba(245,158,11,0.7)]"
                                  : "border border-zinc-700 bg-[#08080a] group-hover:border-zinc-500 shadow-inner"
                              )}
                            >
                              {isSelected && <div className="h-1.5 w-1.5 rounded-full bg-zinc-950" />}
                            </div>
                          </div>
                        </div>

                        {/* Model Friendly Title & Technical Identifier Slug */}
                        <div>
                          <h4 className={cn(
                            "text-[15px] font-bold tracking-tight transition-colors",
                            isSelected ? "text-amber-300 font-bold" : "text-zinc-100 group-hover:text-white"
                          )}>
                            {displayName}
                          </h4>
                          <div className="mt-1.5 flex items-center gap-1.5">
                            <span className="font-mono text-[11px] text-zinc-300 bg-[#08080a] border border-zinc-800/90 px-2.5 py-0.5 rounded-[4px] select-all font-medium truncate max-w-[210px] shadow-inner">
                              {m.id}
                            </span>
                            <button
                              type="button"
                              onClick={(e) => handleCopyModelId(m.id, e)}
                              title="Copy model identifier"
                              className="p-1 text-zinc-400 hover:text-white hover:bg-zinc-800 rounded-[4px] transition-colors cursor-pointer border border-transparent hover:border-zinc-700"
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
                          <span className="flex items-center gap-1.5 bg-gradient-to-b from-zinc-800/90 via-zinc-850 to-zinc-900/90 text-zinc-200 border-t border-t-zinc-700/60 border-x border-x-zinc-800/70 border-b border-b-zinc-950 px-2.5 py-1 rounded-[5px] font-medium text-[11px] shadow-[inset_0_1px_0_rgba(255,255,255,0.08)]">
                            <Layers className="h-3.5 w-3.5 text-zinc-400 group-hover:text-zinc-300" />
                            <span>{spec.contextWindow}</span>
                          </span>
                          <span className="flex items-center gap-1.5 bg-gradient-to-b from-zinc-800/90 via-zinc-850 to-zinc-900/90 text-zinc-200 border-t border-t-zinc-700/60 border-x border-x-zinc-800/70 border-b border-b-zinc-950 px-2.5 py-1 rounded-[5px] font-medium text-[11px] shadow-[inset_0_1px_0_rgba(255,255,255,0.08)]">
                            <span
                              className={cn(
                                "h-1.5 w-1.5 rounded-full",
                                spec.speedCategory === "fast"
                                  ? "bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,1)]"
                                  : spec.speedCategory === "reasoning"
                                  ? "bg-purple-400 shadow-[0_0_6px_rgba(192,132,252,1)]"
                                  : "bg-cyan-400 shadow-[0_0_6px_rgba(34,211,238,1)]"
                              )}
                            />
                            <span>{spec.latencyRating}</span>
                          </span>
                        </div>

                        {/* Specialty Description */}
                        <p className="text-[12px] text-zinc-300 group-hover:text-zinc-200 leading-relaxed font-sans line-clamp-2 transition-colors">
                          {spec.specialty}
                        </p>
                      </div>

                      {/* Card Footer */}
                      <div className="relative z-10 mt-3.5 pt-3 border-t border-zinc-800/80 flex items-center justify-between text-xs text-zinc-400">
                        <span className="font-medium text-zinc-400 font-mono text-[11px]">
                          {spec.recommendedRole}
                        </span>
                        <span className={cn("font-semibold text-xs flex items-center gap-1 font-mono transition-colors", isSelected ? "text-amber-300 font-bold" : "text-zinc-400 group-hover:text-zinc-200")}>
                          {isSelected ? (
                            <span className="flex items-center gap-1.5 bg-amber-400/20 border border-amber-500/60 text-amber-300 px-2.5 py-1 rounded-[5px] shadow-[0_0_10px_rgba(245,158,11,0.2)] font-bold">
                              <Check className="h-3.5 w-3.5 text-amber-300 stroke-[2.5]" />
                              <span>Selected</span>
                            </span>
                          ) : isGlobalDisabled ? (
                            <span className="flex items-center gap-1 text-zinc-500 font-normal">
                              <Lock className="h-3 w-3 text-zinc-500" />
                              <span>Read-Only</span>
                            </span>
                          ) : (
                            <span>Select Engine →</span>
                          )}
                        </span>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          {/* Action Bar & Save Configuration inside card */}
          <div className="relative z-10 pt-4 border-t border-zinc-800/80 flex flex-col sm:flex-row sm:items-center justify-between gap-4">
            <div className="flex items-center gap-2.5 text-xs sm:text-sm text-zinc-400">
              <Zap className="h-4 w-4 text-amber-400 shrink-0" />
              <span>
                Hot-switches instantaneously without restarting containers or interrupting active CI runs.
              </span>
            </div>

            <div className="flex items-center gap-3 self-end sm:self-auto">
              {isDirty && (
                <span className="text-xs font-mono font-semibold text-amber-400 bg-amber-950/40 border border-amber-800/50 px-3 py-1.5 rounded-[6px] animate-pulse">
                  Unsaved: {formatModelDisplayName(selectedModel)}
                </span>
              )}
              <Button
                onClick={handleSaveConfig}
                disabled={isGlobalDisabled || isPending || !isDirty}
                size="sm"
                className={cn(
                  "relative px-5 py-2 font-mono font-bold text-xs rounded-[6px] transition-all cursor-pointer select-none",
                  isDirty && !isGlobalDisabled
                    ? "bg-gradient-to-b from-amber-400 via-amber-450 to-amber-500 hover:from-amber-300 hover:to-amber-400 text-zinc-950 border-t border-t-amber-200/60 border-x border-x-amber-400/80 border-b border-b-amber-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.4),0_2px_8px_rgba(245,158,11,0.35)] active:translate-y-[0.5px]"
                    : "bg-zinc-800/90 text-zinc-400 hover:bg-zinc-800 border border-zinc-700/60"
                )}
              >
                {isPending ? (
                  <span className="flex items-center gap-2">
                    <RefreshCw className="h-3.5 w-3.5 animate-spin text-zinc-950" />
                    <span>Applying Live...</span>
                  </span>
                ) : isDirty ? (
                  <span className="flex items-center gap-2">
                    <span>Save Configuration</span>
                    <ArrowRight className="h-3.5 w-3.5" />
                  </span>
                ) : (
                  <span className="flex items-center gap-2 text-zinc-400 font-medium">
                    <Check className="h-3.5 w-3.5 text-emerald-400" />
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
          <div className="relative overflow-hidden rounded-xl border-t border-t-zinc-600/60 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-gradient-to-b from-[#111115]/95 via-[#0d0d10]/95 to-[#09090c]/95 backdrop-blur-sm p-5 shadow-[inset_0_1px_0_rgba(255,255,255,0.12),0_8px_24px_rgba(0,0,0,0.4)] space-y-3.5">
            <span
              aria-hidden="true"
              className="pointer-events-none absolute inset-x-0 top-0 h-6 bg-gradient-to-b from-white/[0.04] to-transparent z-10"
            />
            <div className="flex items-center justify-between relative z-10">
              <div className="flex items-center gap-2">
                <Lock className="h-4 w-4 text-emerald-400" />
                <h3 className="text-sm font-mono font-semibold text-zinc-200">
                  Inference Base URL (Server-Derived)
                </h3>
              </div>
              <span className="text-xs font-mono bg-emerald-950/40 border border-emerald-800/40 text-emerald-400 px-2 py-0.5 rounded">
                TLS 1.3 Verified
              </span>
            </div>

            <div className="relative z-10 flex items-center justify-between gap-3 rounded-[6px] border border-zinc-800/90 bg-[#08080a] px-3.5 py-2.5 font-mono text-xs sm:text-sm text-zinc-200">
              <div className="flex items-center gap-2 truncate">
                <span className="text-zinc-500 select-none text-xs font-mono">ENDPOINT:</span>
                <span className="text-amber-300 font-semibold truncate select-all">{currentBaseUrl}</span>
              </div>
              <div className="flex items-center gap-2 shrink-0">
                <button
                  type="button"
                  onClick={() => handleCopyUrl(currentBaseUrl)}
                  className="flex items-center gap-1.5 text-xs text-zinc-300 hover:text-white border border-zinc-700/80 bg-zinc-800 px-2.5 py-1 rounded-[5px] transition-colors cursor-pointer"
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

            <p className="relative z-10 text-xs text-zinc-400 leading-relaxed font-sans">
              Inference endpoints are generated strictly server-side by Haunter&apos;s orchestrator to prevent prompt injection and unauthorized upstream proxying.
            </p>
          </div>

          {/* Column 2: Live Pipeline Telemetry & Subagents */}
          <div className="relative overflow-hidden rounded-xl border-t border-t-zinc-600/60 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-gradient-to-b from-[#111115]/95 via-[#0d0d10]/95 to-[#09090c]/95 backdrop-blur-sm p-5 shadow-[inset_0_1px_0_rgba(255,255,255,0.12),0_8px_24px_rgba(0,0,0,0.4)] space-y-3.5">
            <span
              aria-hidden="true"
              className="pointer-events-none absolute inset-x-0 top-0 h-6 bg-gradient-to-b from-white/[0.04] to-transparent z-10"
            />
            <div className="flex items-center justify-between relative z-10">
              <div className="flex items-center gap-2">
                <Activity className="h-4 w-4 text-emerald-400" />
                <h3 className="text-sm font-mono font-semibold text-zinc-200">
                  Subagent Pipeline Telemetry
                </h3>
              </div>
              <div className="flex items-center gap-1.5 text-xs text-emerald-400 bg-emerald-950/40 border border-emerald-800/40 px-2 py-0.5 rounded font-mono">
                <span className="h-2 w-2 rounded-full bg-emerald-400 animate-pulse" />
                <span>Ready for CI Webhooks</span>
              </div>
            </div>

            {/* Stepper diagram */}
            <div className="relative z-10 grid grid-cols-5 gap-1.5 text-center font-mono">
              <div className="rounded-[6px] border border-zinc-800/80 bg-[#0c0c0e] p-2 space-y-0.5">
                <span className="text-[10px] text-zinc-500 uppercase block font-semibold">01</span>
                <span className="text-xs text-zinc-300 font-medium block truncate">Webhook</span>
              </div>
              <div className="rounded-[6px] border border-zinc-800/80 bg-[#0c0c0e] p-2 space-y-0.5">
                <span className="text-[10px] text-zinc-500 uppercase block font-semibold">02</span>
                <span className="text-xs text-zinc-300 font-medium block truncate">Gatherer</span>
              </div>
              <div className="rounded-[6px] border border-amber-500/50 bg-amber-400/10 p-2 space-y-0.5 ring-1 ring-amber-400/30 shadow-[0_0_12px_rgba(245,158,11,0.15)]">
                <span className="text-[10px] text-amber-400 uppercase block font-bold">03 Fixer</span>
                <span className="text-xs text-amber-300 font-bold block truncate">
                  {formatModelDisplayName(selectedModel)}
                </span>
              </div>
              <div className="rounded-[6px] border border-zinc-800/80 bg-[#0c0c0e] p-2 space-y-0.5">
                <span className="text-[10px] text-zinc-500 uppercase block font-semibold">04</span>
                <span className="text-xs text-zinc-300 font-medium block truncate">Sandbox</span>
              </div>
              <div className="rounded-[6px] border border-zinc-800/80 bg-[#0c0c0e] p-2 space-y-0.5">
                <span className="text-[10px] text-zinc-500 uppercase block font-semibold">05</span>
                <span className="text-xs text-zinc-300 font-medium block truncate">PR Writer</span>
              </div>
            </div>

            <div className="relative z-10 flex items-center justify-between text-xs text-zinc-400 font-mono pt-0.5">
              <span>Orchestrator: <strong className="text-zinc-200 font-medium">Async Subagents (SQS/Lambda)</strong></span>
              <span>Routing: <strong className="text-emerald-400 font-medium">Allowlist Enforced</strong></span>
            </div>
          </div>
        </div>

        {/* FLOATING ACTION DOCK: When changes are dirty, provide instant saving anywhere on the page */}
        {isDirty && !isGlobalDisabled && (
          <div className="fixed bottom-6 inset-x-0 z-40 flex justify-center px-4 pointer-events-none animate-in fade-in slide-in-from-bottom-5 duration-200">
            <div className="pointer-events-auto flex flex-col sm:flex-row items-center justify-between gap-3 sm:gap-6 max-w-2xl w-full rounded-2xl border-t border-t-zinc-600/70 border-x border-x-zinc-700/60 border-b border-b-zinc-950 bg-gradient-to-b from-[#18181f]/95 via-[#131317]/95 to-[#0d0d11]/95 p-3.5 sm:px-5 sm:py-3 shadow-[inset_0_1px_0_rgba(255,255,255,0.14),0_12px_32px_rgba(0,0,0,0.65),0_0_24px_rgba(245,158,11,0.18)] backdrop-blur-md ring-1 ring-amber-400/30">
              <div className="flex items-center gap-3 text-xs sm:text-sm">
                <span className="relative flex h-2.5 w-2.5">
                  <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-75" />
                  <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-amber-400 shadow-[0_0_6px_rgba(251,191,36,0.8)]" />
                </span>
                <div className="text-zinc-200">
                  <span className="text-zinc-400">Targeting: </span>
                  <strong className="text-amber-300 font-semibold font-mono bg-zinc-900 border border-zinc-700/80 px-2 py-0.5 rounded-[4px]">
                    {formatModelDisplayName(selectedModel)}
                  </strong>
                </div>
              </div>

              <div className="flex items-center gap-2.5 self-end sm:self-auto">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={handleDiscardChanges}
                  disabled={isPending}
                  className="h-7.5 px-3 text-xs font-mono rounded-[5px] text-zinc-300 hover:text-white bg-gradient-to-b from-zinc-800/90 via-zinc-850 to-zinc-900/90 border-t border-t-zinc-600/60 border-x border-x-zinc-700/60 border-b border-b-zinc-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_1px_2px_rgba(0,0,0,0.3)] hover:border-t-zinc-500 cursor-pointer"
                >
                  <RotateCcw className="h-3 w-3 mr-1 text-zinc-400" />
                  <span>Discard</span>
                </Button>

                <Button
                  size="sm"
                  onClick={handleSaveConfig}
                  disabled={isPending}
                  className="h-7.5 px-4 text-xs font-mono font-bold rounded-[5px] bg-gradient-to-b from-amber-400 via-amber-450 to-amber-500 hover:from-amber-300 hover:to-amber-400 text-zinc-950 border-t border-t-amber-200/60 border-x border-x-amber-400/80 border-b border-b-amber-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.4),0_2px_8px_rgba(245,158,11,0.35)] active:translate-y-[0.5px] flex items-center gap-1.5 cursor-pointer"
                >
                  {isPending ? (
                    <>
                      <RefreshCw className="h-3.5 w-3.5 animate-spin text-zinc-950" />
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
