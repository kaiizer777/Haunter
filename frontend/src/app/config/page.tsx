"use client";

import { useEffect, useState, useCallback, useMemo, useRef } from "react";
import { AppLayout } from "@/components/layout/app-layout";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { SelectDropdown, SelectOption } from "@/components/ui/select-dropdown";
import { Modal } from "@/components/ui/modal";
import { api, ModelConfigOut, RepoOut, ApiError, AvailableModelItem, CustomProviderCompat } from "@/lib/api";

type ConfigModelItem = AvailableModelItem & { provider: string };

import { useAuth } from "@/lib/auth-context";
import { cn } from "@/lib/utils";
import {
  Cpu,
  CheckCircle2,
  AlertCircle,
  ShieldCheck,
  GitBranch,
  Globe,
  Zap,
  Lock,
  Copy,
  Check,
  RefreshCw,
  Activity,
  ArrowRight,
  Search,
  X,
  Gauge,
  Layers,
  RotateCcw,
  FolderGit2,
  ChevronDown,
  ChevronUp,
  Plus,
  Server,
  Eye,
  EyeOff,
  Sparkles,
} from "lucide-react";

// Strict server-side allowlists (WORK.md:252)
const PROVIDER_OPTIONS = [
  { id: "opencode_go", name: "OpenCode Go", defaultModel: "deepseek-v4.1-flash" },
  { id: "groq", name: "Groq", defaultModel: "openai/gpt-oss-120b" },
];

const BASE_URL_PRESETS = [
  { label: "Together AI", value: "https://api.together.xyz/v1" },
  { label: "OpenAI", value: "https://api.openai.com/v1" },
] as const;

const DEFAULT_MODEL_OPTIONS_BY_PROVIDER: Record<string, AvailableModelItem[]> = {
  opencode_go: [
    { id: "deepseek-v4.1-flash", name: "DeepSeek V4.1 Flash", tag: "1M · Low Latency", context_window: 1000000 },
    { id: "glm-5.2", name: "GLM 5.2", tag: "1M · Reasoning", context_window: 1000000 },
    { id: "mimo-v2.6-flash", name: "Mimo V2.6 Flash", tag: "1M · Fast", context_window: 1000000 },
    { id: "nemotron-3.5-lightning-free", name: "Nemotron 3.5 Lightning", tag: "Free Tier", context_window: 131072 },
  ],
  groq: [
    { id: "openai/gpt-oss-120b", name: "GPT-OSS 120B", tag: "High Reasoning · Fallback", context_window: 131072 },
    { id: "llama-3.3-70b-versatile", name: "Llama 3.3 70B Versatile", tag: "Fast · Production", context_window: 131072 },
    { id: "llama-3.1-8b-instant", name: "Llama 3.1 8B Instant", tag: "Ultra Fast", context_window: 131072 },
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
  opencode_go: {
    name: "OpenCode Go",
    tag: "Low Latency · Direct Gateway",
    shortBadge: "Direct Gateway",
    description: "Ultra-low-latency optimized inference gateway for real-time CI patch synthesis and code verification.",
    baseUrl: "https://opencode.ai/inference/go/openai/v1",
    icon: Activity,
    accentText: "text-emerald-400",
    badgeClass: "bg-emerald-400/10 border-emerald-400/30 text-emerald-300",
  },
  groq: {
    name: "Groq",
    tag: "Ultra-Fast LPU Inference",
    shortBadge: "Direct API",
    description: "Near-instantaneous inference acceleration for high-throughput patch synthesis.",
    baseUrl: "https://api.groq.com/openai/v1",
    icon: Gauge,
    accentText: "text-rose-400",
    badgeClass: "bg-rose-400/10 border-rose-400/30 text-rose-300",
  },
};

const MODEL_CONTEXT_WINDOWS: Record<string, number> = {
  "deepseek-v4.1-flash": 1000000,
  "glm-5.2": 1000000,
  "mimo-v2.6-flash": 1000000,
  "space-bunny-free": 1048576,
  "longcat-2.5-preview-free": 1048576,
  "ling-3.0-flash-fin-free": 1048576,
  "mimo-v2.6-flash-free": 262144,
  "mimo-v2.5-free": 262144,
  "claude-sonnet-4-5": 200000,
  "claude-haiku-3-5": 200000,
  "laguna-s-2.1-free": 65536,
  "deepseek-r1-0528-free": 65536,
  "gemma2-9b-it": 8192,
  "nemotron-3.5-lightning-free": 131072,
  "nemotron-3-ultra-free": 131072,
  "jev-1.13-free": 131072,
  "deepseek-v4-flash-free": 131072,
  "muse-spark-1.3-contributor-free": 131072,
  "muse-spark-1.2-contributor-free": 131072,
  "qwen-2.5-coder-32b-instruct-free": 131072,
  "deepseek-r1-distill-qwen-32b-free": 131072,
  "llama-3.3-70b-instruct-free": 131072,
  "gpt-4o": 128000,
  "gpt-4o-mini": 128000,
  "openai/gpt-oss-120b": 131072,
  "llama-3.3-70b-versatile": 131072,
  "llama-3.1-8b-instant": 131072,
  "deepseek-r1-distill-llama-70b": 131072,
};

export function formatContextWindow(tokens?: number): string {
  if (!tokens) return "Unknown Context";
  if (tokens >= 1048576 && tokens % 1048576 === 0) {
    return `${tokens / 1048576}M Context`;
  }
  if (tokens >= 1000000 && tokens % 1000000 === 0) {
    return `${tokens / 1000000}M Context`;
  }
  if (tokens % 1000 === 0) {
    return `${tokens / 1000}k Context`;
  }
  if (tokens % 1024 === 0) {
    return `${tokens / 1024}k Context`;
  }
  if (tokens >= 1000) {
    return `${Math.round(tokens / 1000)}k Context`;
  }
  return `${tokens} Context`;
}

interface ModelSpec {
  contextWindow: string;
  specialty: string;
  latencyRating: string;
  latencyMs: number;
  recommendedRole: string;
  tagColor: string;
  speedCategory: "fast" | "standard" | "reasoning";
  isFree?: boolean;
  tokenCost: string;
}

const MODEL_SPECS: Record<string, ModelSpec> = {
  "deepseek-v4.1-flash": {
    contextWindow: "1M Context",
    specialty: "High-Throughput Low-Latency CI Diagnostic & Fix Generation",
    latencyRating: "~180ms TTFT",
    latencyMs: 180,
    recommendedRole: "Production Flash",
    tagColor: "bg-emerald-950/60 border-emerald-700/60 text-emerald-300",
    speedCategory: "fast",
    isFree: false,
    tokenCost: "$0.14 / 1M",
  },
  "glm-5.2": {
    contextWindow: "1M Context",
    specialty: "Complex Multi-Step Architecture Reasoning & Logic Healing",
    latencyRating: "~360ms TTFT",
    latencyMs: 360,
    recommendedRole: "High-Reasoning Production",
    tagColor: "bg-purple-950/60 border-purple-700/60 text-purple-300",
    speedCategory: "reasoning",
    isFree: false,
    tokenCost: "$0.35 / 1M",
  },
  "mimo-v2.6-flash": {
    contextWindow: "1M Context",
    specialty: "Ultra-Fast Codebase Search & Patch Synthesis",
    latencyRating: "~210ms TTFT",
    latencyMs: 210,
    recommendedRole: "Fast Production",
    tagColor: "bg-cyan-950/60 border-cyan-700/60 text-cyan-300",
    speedCategory: "fast",
    isFree: false,
    tokenCost: "$0.10 / 1M",
  },
  "space-bunny-free": {
    contextWindow: "1M Context",
    specialty: "High-Capacity Ultra-Long Context CI Analysis",
    latencyRating: "~330ms TTFT",
    latencyMs: 330,
    recommendedRole: "1M Free Tier",
    tagColor: "bg-emerald-950/60 border-emerald-700/60 text-emerald-300",
    speedCategory: "standard",
    isFree: true,
    tokenCost: "$0.00 / 1M",
  },
  "longcat-2.5-preview-free": {
    contextWindow: "1M Context",
    specialty: "Massive Context & Whole-Repository Trace Analysis",
    latencyRating: "~350ms TTFT",
    latencyMs: 350,
    recommendedRole: "1M Long-Context",
    tagColor: "bg-emerald-950/60 border-emerald-700/60 text-emerald-300",
    speedCategory: "standard",
    isFree: true,
    tokenCost: "$0.00 / 1M",
  },
  "ling-3.0-flash-fin-free": {
    contextWindow: "1M Context",
    specialty: "Multi-Language Syntax & Long-Context AST Repair",
    latencyRating: "~390ms TTFT",
    latencyMs: 390,
    recommendedRole: "1M AST Specialist",
    tagColor: "bg-cyan-950/60 border-cyan-700/60 text-cyan-300",
    speedCategory: "standard",
    isFree: true,
    tokenCost: "$0.00 / 1M",
  },
  "mimo-v2.6-flash-free": {
    contextWindow: "256k Context",
    specialty: "High-Throughput Fast Triage & Syntax Healing",
    latencyRating: "~220ms TTFT",
    latencyMs: 220,
    recommendedRole: "High-Throughput Healing",
    tagColor: "bg-cyan-950/60 border-cyan-700/60 text-cyan-300",
    speedCategory: "fast",
    isFree: true,
    tokenCost: "$0.00 / 1M",
  },
  "mimo-v2.5-free": {
    contextWindow: "256k Context",
    specialty: "Fast CI Log Analysis & Patch Repair",
    latencyRating: "~240ms TTFT",
    latencyMs: 240,
    recommendedRole: "Fast Healing",
    tagColor: "bg-cyan-950/60 border-cyan-700/60 text-cyan-300",
    speedCategory: "fast",
    isFree: true,
    tokenCost: "$0.00 / 1M",
  },
  "nemotron-3.5-lightning-free": {
    contextWindow: "128k Context",
    specialty: "CI Diagnostics & Root-Cause Synthesis",
    latencyRating: "~320ms TTFT",
    latencyMs: 320,
    recommendedRole: "Default Engine",
    tagColor: "bg-amber-950/60 border-amber-700/60 text-amber-300",
    speedCategory: "standard",
    isFree: true,
    tokenCost: "$0.00 / 1M",
  },
  "nemotron-3-ultra-free": {
    contextWindow: "128k Context",
    specialty: "High-Parameter Multi-Step CI Reasoning",
    latencyRating: "~360ms TTFT",
    latencyMs: 360,
    recommendedRole: "Deep Reasoning",
    tagColor: "bg-amber-950/60 border-amber-700/60 text-amber-300",
    speedCategory: "standard",
    isFree: true,
    tokenCost: "$0.00 / 1M",
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
    tokenCost: "$0.00 / 1M",
  },
  "deepseek-r1-0528-free": {
    contextWindow: "64k Context",
    specialty: "Chain-of-Thought Reasoning & Deep Flaky CI Debugging",
    latencyRating: "~490ms TTFT",
    latencyMs: 490,
    recommendedRole: "Deep CoT Reasoning",
    tagColor: "bg-purple-950/60 border-purple-700/60 text-purple-300",
    speedCategory: "reasoning",
    isFree: true,
    tokenCost: "$0.00 / 1M",
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
    tokenCost: "$0.00 / 1M",
  },
  "jev-1.13-free": {
    contextWindow: "128k Context",
    specialty: "Free Inference & Quick Patch Synthesis",
    latencyRating: "~330ms TTFT",
    latencyMs: 330,
    recommendedRole: "Free Tier Verified",
    tagColor: "bg-amber-950/60 border-amber-700/60 text-amber-300",
    speedCategory: "standard",
    isFree: true,
    tokenCost: "$0.00 / 1M",
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
    tokenCost: "$0.00 / 1M",
  },
  "muse-spark-1.2-contributor-free": {
    contextWindow: "128k Context",
    specialty: "Open-Source Benchmark Synthesis & Verification",
    latencyRating: "~330ms TTFT",
    latencyMs: 330,
    recommendedRole: "Community Benchmark",
    tagColor: "bg-violet-950/60 border-violet-700/60 text-violet-300",
    speedCategory: "standard",
    isFree: true,
    tokenCost: "$0.00 / 1M",
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
    tokenCost: "$0.00 / 1M",
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
    tokenCost: "$0.00 / 1M",
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
    tokenCost: "$0.00 / 1M",
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
    tokenCost: "$2.50 / 1M",
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
    tokenCost: "$0.15 / 1M",
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
    tokenCost: "$3.00 / 1M",
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
    tokenCost: "$0.80 / 1M",
  },
  "openai/gpt-oss-120b": {
    contextWindow: "128k Context",
    specialty: "Open Reasoning & Multi-Step CI Patch Synthesis",
    latencyRating: "~340ms TTFT",
    latencyMs: 340,
    recommendedRole: "High-Reasoning Fallback",
    tagColor: "bg-rose-950/60 border-rose-700/60 text-rose-300",
    speedCategory: "reasoning",
    isFree: false,
    tokenCost: "$0.45 / 1M",
  },
  "llama-3.3-70b-versatile": {
    contextWindow: "128k Context",
    specialty: "Production Triage & High-Throughput Fix Generation",
    latencyRating: "~190ms TTFT",
    latencyMs: 190,
    recommendedRole: "Production Triage",
    tagColor: "bg-rose-950/60 border-rose-700/60 text-rose-300",
    speedCategory: "fast",
    isFree: false,
    tokenCost: "$0.59 / 1M",
  },
  "llama-3.1-8b-instant": {
    contextWindow: "128k Context",
    specialty: "Ultra-Fast Lint, Config & Unit Test Gating",
    latencyRating: "~120ms TTFT",
    latencyMs: 120,
    recommendedRole: "Ultra-Fast Gating",
    tagColor: "bg-emerald-950/60 border-emerald-700/60 text-emerald-300",
    speedCategory: "fast",
    isFree: false,
    tokenCost: "$0.05 / 1M",
  },
  "gemma2-9b-it": {
    contextWindow: "8k Context",
    specialty: "Lightweight High-Speed Gate & Lint Filtering",
    latencyRating: "~140ms TTFT",
    latencyMs: 140,
    recommendedRole: "Ultra-Fast Gating",
    tagColor: "bg-emerald-950/60 border-emerald-700/60 text-emerald-300",
    speedCategory: "fast",
    isFree: false,
    tokenCost: "$0.07 / 1M",
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
    "deepseek-v4.1-flash": "DeepSeek V4.1 Flash",
    "glm-5.2": "GLM 5.2",
    "mimo-v2.6-flash": "Mimo V2.6 Flash",
    "nemotron-3.5-lightning-free": "Nemotron 3.5 Lightning",
    "nemotron-3-ultra-free": "Nemotron 3 Ultra",
    "laguna-s-2.1-free": "Laguna S 2.1",
    "ling-3.0-flash-fin-free": "Ling 3.0 Flash Fin",
    "deepseek-v4-flash-free": "DeepSeek V4 Flash",
    "muse-spark-1.3-contributor-free": "Muse Spark 1.3 Contributor",
    "muse-spark-1.2-contributor-free": "Muse Spark 1.2 Contributor",
    "mimo-v2.6-flash-free": "MiMo V2.6 Flash",
    "mimo-v2.5-free": "Mimo V2.5",
    "space-bunny-free": "Space Bunny",
    "longcat-2.5-preview-free": "Longcat 2.5 Preview",
    "jev-1.13-free": "Jev 1.13",
    "deepseek-r1-0528-free": "DeepSeek R1 0528",
    "qwen-2.5-coder-32b-instruct-free": "Qwen 2.5 Coder 32B",
    "deepseek-r1-distill-qwen-32b-free": "DeepSeek R1 Distill Qwen",
    "llama-3.3-70b-instruct-free": "Llama 3.3 70B Instruct",
    "gpt-4o": "GPT-4o Flagship",
    "gpt-4o-mini": "GPT-4o Mini",
    "claude-sonnet-4-5": "Claude Sonnet 4.5",
    "claude-haiku-3-5": "Claude Haiku 3.5",
    "openai/gpt-oss-120b": "GPT-OSS 120B",
    "llama-3.3-70b-versatile": "Llama 3.3 70B Versatile",
    "llama-3.1-8b-instant": "Llama 3.1 8B Instant",
    "gemma2-9b-it": "Gemma 2 9B",
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
function getModelSpec(
  modelId: string,
  tag?: string,
  contextWindowTokens?: number,
  tokenCost?: string
): ModelSpec {
  const ctxTokens = contextWindowTokens ?? MODEL_CONTEXT_WINDOWS[modelId];
  const formattedCtx = ctxTokens ? formatContextWindow(ctxTokens) : undefined;

  if (MODEL_SPECS[modelId]) {
    const spec = MODEL_SPECS[modelId];
    const resolvedCost = tokenCost || spec.tokenCost;
    if ((formattedCtx && spec.contextWindow !== formattedCtx) || (tokenCost && tokenCost !== spec.tokenCost)) {
      return { ...spec, contextWindow: formattedCtx ?? spec.contextWindow, tokenCost: resolvedCost };
    }
    return spec;
  }

  const idLower = modelId.toLowerCase();
  const isFlash =
    idLower.includes("flash") || idLower.includes("fast") || idLower.includes("haiku") || idLower.includes("mini");
  const isReasoning =
    idLower.includes("r1") || idLower.includes("reason") || idLower.includes("70b") || idLower.includes("thought");
  const isFree = idLower.endsWith("-free");

  let fallbackCtx = "Unknown Context";
  if (formattedCtx) {
    fallbackCtx = formattedCtx;
  } else if (idLower.includes("1m") || idLower.includes("bunny") || idLower.includes("longcat") || idLower.includes("ling") || idLower.includes("glm") || idLower.includes("v4.1")) {
    fallbackCtx = "1M Context";
  } else if (idLower.includes("256k") || idLower.includes("mimo")) {
    fallbackCtx = "256k Context";
  } else if (idLower.includes("200k") || idLower.includes("claude")) {
    fallbackCtx = "200k Context";
  } else if (idLower.includes("64k") || idLower.includes("laguna")) {
    fallbackCtx = "64k Context";
  } else if (idLower.includes("8k") || idLower.includes("gemma")) {
    fallbackCtx = "8k Context";
  }

  return {
    contextWindow: fallbackCtx,
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
    tokenCost:
      tokenCost ??
      (isFree
        ? "$0.00 / 1M"
        : isFlash
        ? "$0.15 / 1M"
        : isReasoning
        ? "$0.60 / 1M"
        : "$0.20 / 1M"),
  };
}

export default function ModelConfigPage() {
  const { user } = useAuth();
  const isAdmin = Boolean(user?.is_admin);
  const hasInitializedScopeRef = useRef(false);
  const activeFetchControllerRef = useRef<AbortController | null>(null);
  const activeValidateControllerRef = useRef<AbortController | null>(null);
  const activeValidateRequestRef = useRef<number>(0);

  const [repos, setRepos] = useState<RepoOut[]>([]);
  const [selectedScope, setSelectedScope] = useState<"global" | "repo">(() =>
    (user?.is_admin === false ? "repo" : "global")
  );
  const [selectedRepoId, setSelectedRepoId] = useState<string>("");

  const [activeConfig, setActiveConfig] = useState<ModelConfigOut | null>(null);
  const [globalConfig, setGlobalConfig] = useState<ModelConfigOut | null>(null);
  const [selectedProvider, setSelectedProvider] = useState<string>("opencode_go");
  const [selectedModel, setSelectedModel] = useState<string>("deepseek-v4.1-flash");
  const [modelOptionsByProvider, setModelOptionsByProvider] = useState<
    Record<string, AvailableModelItem[]>
  >(DEFAULT_MODEL_OPTIONS_BY_PROVIDER);

  const [searchQuery, setSearchQuery] = useState("");
  const [speedFilter, setSpeedFilter] = useState<"all" | "free" | "fast" | "reasoning">("all");
  const [isExpanded, setIsExpanded] = useState(false);
  const modelsGridRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    setIsExpanded(false);
  }, [selectedProvider, searchQuery, speedFilter]);

  const [loading, setLoading] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [isResetting, setIsResetting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saveSuccess, setSaveSuccess] = useState(false);
  const [copiedUrl, setCopiedUrl] = useState(false);
  const [copiedModelId, setCopiedModelId] = useState<string | null>(null);
  const [isSaving, setIsSaving] = useState(false);
  const isPending = isSaving;

  // Admin custom providers (OpenAI/Anthropic-compatible endpoints).
  const [customProviders, setCustomProviders] = useState<Record<string, { name: string; baseUrl: string; compat?: CustomProviderCompat }>>({});
  const [customName, setCustomName] = useState("");
  const [customCompat, setCustomCompat] = useState<CustomProviderCompat>("openai");
  const [customBaseUrl, setCustomBaseUrl] = useState("");
  const [customApiKey, setCustomApiKey] = useState("");
  const [showApiKey, setShowApiKey] = useState(false);
  const [customModels, setCustomModels] = useState<AvailableModelItem[]>([]);
  const [isValidatingCustom, setIsValidatingCustom] = useState(false);
  const [customError, setCustomError] = useState<string | null>(null);
  const [isCustomModalOpen, setIsCustomModalOpen] = useState(false);

  const searchInputRef = useRef<HTMLInputElement | null>(null);

  // Clean up any ongoing fetch controllers on unmount
  useEffect(() => {
    return () => {
      activeFetchControllerRef.current?.abort();
      activeValidateControllerRef.current?.abort();
      activeValidateRequestRef.current = 0;
    };
  }, []);

  // Load dynamic models from backend (admin-only: non-admins never see
  // the catalog, provider tabs, model cards, or context-window badges).
  useEffect(() => {
    if (!isAdmin) return;
    api
      .getAvailableModels()
      .then((data) => {
        if (data) {
          const raw = data as unknown as Record<string, AvailableModelItem[] | undefined>;
          setModelOptionsByProvider((prev) => {
            const next: typeof DEFAULT_MODEL_OPTIONS_BY_PROVIDER = {
              opencode_go: (raw.opencode_go ?? []).length
                ? (raw.opencode_go ?? [])
                : DEFAULT_MODEL_OPTIONS_BY_PROVIDER.opencode_go,
              groq: raw.groq?.length ? raw.groq : DEFAULT_MODEL_OPTIONS_BY_PROVIDER.groq,
            };
            for (const [key, value] of Object.entries(raw)) {
              if (key !== "opencode_go" && key !== "groq" && Array.isArray(value) && value.length) {
                next[key] = value;
              }
            }
            void prev;
            return next;
          });
        }
      })
      .catch(() => {});
  }, [isAdmin]);

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
  }, [user]);

  // If user auth resolves after repos have loaded
  useEffect(() => {
    if (!hasInitializedScopeRef.current && user && !user.is_admin && repos.length > 0) {
      setSelectedScope("repo");
      hasInitializedScopeRef.current = true;
    }
  }, [user, repos]);

  const fetchActiveConfig = useCallback(async (isManualRefresh = false) => {
    if (!isAdmin) {
      setLoading(false);
      setIsRefreshing(false);
      return;
    }
    if (activeFetchControllerRef.current) {
      activeFetchControllerRef.current.abort();
    }
    const controller = new AbortController();
    activeFetchControllerRef.current = controller;

    const targetScope = selectedScope;
    const targetRepoId = selectedRepoId;

    if (isManualRefresh) {
      setIsRefreshing(true);
    } else {
      setLoading(true);
    }
    setError(null);
    try {
      // Always resolve the platform default so repo scope can show
      // override-vs-inherit badges and offer a 1-click reset.
      let globalCfg: ModelConfigOut | null = null;
      try {
        globalCfg = await api.getModelConfig(undefined, {
          signal: controller.signal,
          silent: true,
        });
        if (controller.signal.aborted) return;
        setGlobalConfig(globalCfg);
      } catch (gErr: unknown) {
        if (gErr instanceof ApiError && (gErr.status === 403 || /admin/i.test(gErr.message))) {
          if (controller.signal.aborted) return;
          setGlobalConfig(null);
        } else {
          throw gErr;
        }
      }

      if (targetScope === "repo" && targetRepoId) {
        const repoCfg = await api.getModelConfig(targetRepoId, {
          signal: controller.signal,
          silent: true,
        });
        if (controller.signal.aborted) return;
        setActiveConfig(repoCfg);
        setSelectedProvider(repoCfg.provider);
        setSelectedModel(repoCfg.model_name);
      } else if (globalCfg) {
        setActiveConfig(globalCfg);
        setSelectedProvider(globalCfg.provider);
        setSelectedModel(globalCfg.model_name);
      } else if (targetScope === "global") {
        // Global platform default is unavailable here (non-admin 403):
        // clear stale repo state instead of showing it under Global scope.
        setActiveConfig(null);
        setSelectedProvider("opencode_go");
        setSelectedModel("deepseek-v4.1-flash");
      }
    } catch (err: unknown) {
      if (controller.signal.aborted) return;
      if (err instanceof ApiError) {
        setError(err.message);
      } else if (err instanceof Error && err.name !== "AbortError") {
        setError("Failed to load model configuration.");
      }
    } finally {
      if (!controller.signal.aborted) {
        setLoading(false);
        setIsRefreshing(false);
      }
    }
  }, [selectedScope, selectedRepoId, isAdmin]);

  // Non-admins auto-switch to repo scope; global controls stay locked.
  useEffect(() => {
    if (user && !user.is_admin && !hasInitializedScopeRef.current) {
      setSelectedScope("repo");
      hasInitializedScopeRef.current = true;
    }
  }, [user]);

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

  const handleOpenCustomModal = useCallback(() => {
    activeValidateControllerRef.current?.abort();
    activeValidateControllerRef.current = null;
    activeValidateRequestRef.current = 0;
    setCustomError(null);
    setCustomModels([]);
    setShowApiKey(false);
    setIsCustomModalOpen(true);
  }, []);

  const handleCloseCustomModal = useCallback(() => {
    activeValidateControllerRef.current?.abort();
    activeValidateControllerRef.current = null;
    activeValidateRequestRef.current = 0;
    setIsCustomModalOpen(false);
    setCustomError(null);
    setCustomModels([]);
    setIsValidatingCustom(false);
    setShowApiKey(false);
  }, []);

  // Admin custom provider: validate -> discover -> select -> persist via
  // the standard save flow. Falls back gracefully when the backend has no
  // validation endpoint (validateCustomProvider reports unsupported).
  const handleValidateCustomProvider = useCallback(async () => {
    if (!customName.trim() || !customBaseUrl.trim() || !customApiKey) {
      setCustomError("Name, base URL, and API key are required.");
      return;
    }
    setCustomError(null);
    setIsValidatingCustom(true);

    activeValidateControllerRef.current?.abort();
    const validateController = new AbortController();
    activeValidateControllerRef.current = validateController;

    const requestId = ++activeValidateRequestRef.current;

    try {
      const result = await api.validateCustomProvider(
        {
          name: customName.trim(),
          compat: customCompat,
          base_url: customBaseUrl.trim(),
          api_key: customApiKey,
        },
        { signal: validateController.signal }
      );

      // If modal was closed or a newer request was dispatched while in-flight, ignore response
      if (
        validateController.signal.aborted ||
        activeValidateRequestRef.current !== requestId
      ) {
        return;
      }

      if (!result.supported) {
        setCustomError(
          result.detail ||
            "Custom provider validation is not supported by this backend. Save directly instead."
        );
        return;
      }
      if (!result.ok) {
        setCustomError(result.detail || "Custom provider validation failed.");
        return;
      }
      const providerId =
        result.provider_id ||
        `custom_${customName.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
      const models = result.models?.length
        ? result.models
        : [{ id: customName.trim(), name: customName.trim(), tag: "Custom" }];
      setCustomModels(models);
      setModelOptionsByProvider((prev) => ({ ...prev, [providerId]: models }));
      setCustomProviders((prev) => ({
        ...prev,
        [providerId]: { name: customName.trim(), baseUrl: customBaseUrl.trim(), compat: customCompat },
      }));
      setSelectedProvider(providerId);
      setSelectedModel(models[0].id);
      setCustomName("");
      setCustomBaseUrl("");
      setCustomApiKey("");
      setShowApiKey(false);
      setIsCustomModalOpen(false);
    } catch (err: unknown) {
      if (
        validateController.signal.aborted ||
        activeValidateRequestRef.current !== requestId
      ) {
        return;
      }
      setCustomError(err instanceof ApiError ? err.message : "Custom provider validation failed.");
    } finally {
      if (activeValidateControllerRef.current === validateController) {
        activeValidateControllerRef.current = null;
      }
      if (activeValidateRequestRef.current === requestId) {
        setIsValidatingCustom(false);
      }
    }
  }, [customName, customCompat, customBaseUrl, customApiKey]);

  const effectiveTargetIsGlobal = selectedScope === "global" || !selectedRepoId;
  // Non-admin mutation controls are disabled when targeting global scope;
  // the backend remains authoritative. Repo overrides stay editable.
  const canMutateTarget = isAdmin || !effectiveTargetIsGlobal;

  const handleSaveConfig = useCallback(async () => {
    if (!isAdmin) {
      setError("Inference engine settings are managed by administrators.");
      return;
    }
    if (isSaving) return;
    if (effectiveTargetIsGlobal && !isAdmin) {
      setError(
        "Global platform default can only be modified by administrators. Switch to Repository Override to customize models for your repos."
      );
      return;
    }
    setError(null);
    setSaveSuccess(false);
    setIsSaving(true);

    try {
      const payload = {
        provider: selectedProvider,
        model_name: selectedModel,
        repo_id: selectedScope === "repo" ? selectedRepoId || undefined : undefined,
      };

      const updated = await api.updateModelConfig(payload);
      setActiveConfig(updated);
      if (selectedScope === "global") {
        setGlobalConfig(updated);
      } else if (selectedRepoId) {
        // Keep repo override tracking in sync without a full repos refetch.
        setRepos((prev) =>
          prev.map((r) =>
            r.id === selectedRepoId ? { ...r, active_model_config_id: updated.id } : r
          )
        );
      }
      setSaveSuccess(true);
      setTimeout(() => setSaveSuccess(false), 4500);
    } catch (err: unknown) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else if (err instanceof Error) {
        setError(err.message);
      } else {
        setError("Failed to update active model configuration.");
      }
    } finally {
      setIsSaving(false);
    }
  }, [isSaving, effectiveTargetIsGlobal, isAdmin, selectedProvider, selectedModel, selectedScope, selectedRepoId]);

  // 1-click "Reset to Global Default": apply the platform default
  // provider/model onto the selected repo scope.
  const handleResetToGlobalDefault = useCallback(async () => {
    if (!selectedRepoId || isResetting) return;
    setError(null);
    setSaveSuccess(false);
    setIsResetting(true);
    try {
      if (globalConfig) {
        const updated = await api.updateModelConfig({
          provider: globalConfig.provider,
          model_name: globalConfig.model_name,
          repo_id: selectedRepoId,
        });
        setActiveConfig(updated);
        setSelectedProvider(updated.provider);
        setSelectedModel(updated.model_name);
        setRepos((prev) =>
          prev.map((r) =>
            r.id === selectedRepoId ? { ...r, active_model_config_id: updated.id } : r
          )
        );
      } else {
        // Global default is not readable here (non-admin 403): drop the
        // repo override server-side so the repo inherits the default.
        await api.deleteRepoModelConfig(selectedRepoId);
        setRepos((prev) =>
          prev.map((r) =>
            r.id === selectedRepoId ? { ...r, active_model_config_id: null } : r
          )
        );
        await fetchActiveConfig(true);
      }
      setSaveSuccess(true);
      setTimeout(() => setSaveSuccess(false), 4500);
    } catch (err: unknown) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Failed to reset repo to the global default.");
      }
    } finally {
      setIsResetting(false);
    }
  }, [globalConfig, selectedRepoId, isResetting, fetchActiveConfig]);

  // Repo scope badge state: custom override vs inheriting platform default.
  // Derived from the repo response scope so non-admin overrides still read
  // as overrides when the global default is unreadable (403). Falls back to
  // value comparison for responses without scope metadata.
  const isRepoCustomOverride = useMemo(() => {
    if (selectedScope !== "repo" || !activeConfig) return false;
    if (typeof activeConfig.scope === "string" && activeConfig.scope) {
      return activeConfig.scope === "repo";
    }
    if (!globalConfig) return false;
    return (
      activeConfig.provider !== globalConfig.provider ||
      activeConfig.model_name !== globalConfig.model_name
    );
  }, [selectedScope, activeConfig, globalConfig]);

  const isDirty = useMemo(() => {
    if (!activeConfig) return false;
    return (
      selectedProvider !== activeConfig.provider ||
      selectedModel !== activeConfig.model_name
    );
  }, [activeConfig, selectedProvider, selectedModel]);

  const canSave = !isPending && isDirty && !(effectiveTargetIsGlobal && !isAdmin);
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
        if (canSave) {
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
  }, [canSave, handleSaveConfig, fetchActiveConfig]);

  const handleDiscardChanges = () => {
    setError(null);
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

  const registeredCustom = customProviders[selectedProvider];
  const activeProviderMeta = PROVIDER_METADATA[selectedProvider] ||
    (registeredCustom
      ? {
          name: registeredCustom.name,
          tag: "Custom Inference Endpoint",
          shortBadge: "Custom API",
          description: "Custom OpenAI/Anthropic-compatible endpoint. Select a discovered model below, then Save Configuration to persist.",
          baseUrl: registeredCustom.baseUrl,
          icon: Zap,
          accentText: "text-amber-400",
          badgeClass: "bg-amber-400/10 border-amber-400/30 text-amber-300",
        }
      : PROVIDER_METADATA.opencode_go);
  const currentRepo = repos.find((r) => r.id === selectedRepoId);

  const providerDropdownOptions = useMemo<SelectOption[]>(() => {
    const q = searchQuery.toLowerCase().trim();
    const allProviders = [
      ...PROVIDER_OPTIONS,
      ...Object.entries(customProviders).map(([id, c]) => ({ id, name: c.name, defaultModel: "" })),
    ];
    const all = allProviders.map((p) => {
      const meta = PROVIDER_METADATA[p.id] || PROVIDER_METADATA.opencode_go;
      const Icon = meta.icon;
      return {
        value: p.id,
        label: p.name,
        icon: <Icon className={cn("h-3.5 w-3.5 shrink-0", meta.accentText)} />,
        description: meta.tag,
      };
    });

    if (!q) return all;

    return all.filter((p) => {
      const meta = PROVIDER_METADATA[p.value] || PROVIDER_METADATA.opencode_go;
      return (
        p.value.toLowerCase().includes(q) ||
        p.label.toLowerCase().includes(q) ||
        meta.tag.toLowerCase().includes(q) ||
        meta.description.toLowerCase().includes(q) ||
        meta.shortBadge.toLowerCase().includes(q)
      );
    });
  }, [searchQuery, customProviders]);

  // Normalize provider model list for the currently selected provider
  const allProviderModels = useMemo<ConfigModelItem[]>(() => {
    const options = modelOptionsByProvider[selectedProvider] || [];
    const hasSelected = options.some((m) => m.id === selectedModel);
    const base = hasSelected
      ? options
      : [{ id: selectedModel, name: selectedModel, tag: "Active" }, ...options];
    return base.map((m) => ({ ...m, provider: selectedProvider }));
  }, [modelOptionsByProvider, selectedProvider, selectedModel]);

  // All searchable models across all providers
  const allGlobalModels = useMemo<ConfigModelItem[]>(() => {
    const list: ConfigModelItem[] = [];
    const seen = new Set<string>();

    PROVIDER_OPTIONS.forEach((p) => {
      const pModels = modelOptionsByProvider[p.id] || [];
      pModels.forEach((m) => {
        const key = `${p.id}:${m.id}`;
        if (!seen.has(key)) {
          seen.add(key);
          list.push({ ...m, provider: p.id });
        }
      });
    });

    if (!list.some((m) => m.id === selectedModel)) {
      list.unshift({
        id: selectedModel,
        name: selectedModel,
        tag: "Active",
        provider: selectedProvider,
      });
    }

    return list;
  }, [modelOptionsByProvider, selectedModel, selectedProvider]);

  // Models matching search query across all providers (before speed filter)
  const matchingSearchModels = useMemo<ConfigModelItem[]>(() => {
    const q = searchQuery.toLowerCase().trim();
    if (!q) return allProviderModels;

    const matchingProviderIds = new Set(
      PROVIDER_OPTIONS.filter((p) => {
        const meta = PROVIDER_METADATA[p.id] || PROVIDER_METADATA.opencode_go;
        return (
          p.id.toLowerCase().includes(q) ||
          p.name.toLowerCase().includes(q) ||
          meta.tag.toLowerCase().includes(q) ||
          meta.shortBadge.toLowerCase().includes(q) ||
          (p.id === "opencode_go" && (q.includes("go") || q.includes("opencode")))
        );
      }).map((p) => p.id)
    );

    return allGlobalModels.filter((m) => {
      if (matchingProviderIds.has(m.provider)) {
        return true;
      }
      const spec = getModelSpec(m.id, m.tag, m.context_window);
      const friendlyName = formatModelDisplayName(m.id, m.name).toLowerCase();
      const idLower = m.id.toLowerCase();
      const specialtyLower = spec.specialty.toLowerCase();
      const roleLower = spec.recommendedRole.toLowerCase();
      const tagLower = m.tag.toLowerCase();

      return (
        friendlyName.includes(q) ||
        idLower.includes(q) ||
        specialtyLower.includes(q) ||
        roleLower.includes(q) ||
        tagLower.includes(q)
      );
    });
  }, [searchQuery, allProviderModels, allGlobalModels]);

  // Filter by query and speed category
  const currentModels = useMemo<ConfigModelItem[]>(() => {
    const base = searchQuery.trim() ? matchingSearchModels : allProviderModels;
    return base.filter((m) => {
      const spec = getModelSpec(m.id, m.tag, m.context_window);
      if (speedFilter === "free") return spec.isFree;
      if (speedFilter === "fast") return spec.speedCategory === "fast";
      if (speedFilter === "reasoning") return spec.speedCategory === "reasoning";
      return true;
    });
  }, [searchQuery, matchingSearchModels, allProviderModels, speedFilter]);

  const DEFAULT_CARD_LIMIT = 9;
  const hasMoreModels = currentModels.length > DEFAULT_CARD_LIMIT;
  const displayedModels = useMemo(() => {
    if (isExpanded || !hasMoreModels) {
      return currentModels;
    }
    return currentModels.slice(0, DEFAULT_CARD_LIMIT);
  }, [currentModels, isExpanded, hasMoreModels]);

  if (user && !isAdmin) {
    return (
      <AppLayout
        title="Repository Features"
        subtitle="Manage automation features for your repositories"
      >
        <div className="space-y-4 min-w-0 w-full max-w-none pb-12">
          <div className="rounded-[8px] border border-zinc-800/80 bg-gradient-to-b from-[#111115]/95 to-[#09090c]/95 p-6 text-sm text-zinc-300">
            <p className="font-semibold text-zinc-100">Automation features</p>
            <p className="mt-1 text-zinc-400">
              Configure auto-fix, auditor mode, sandbox verification, audit
              triggers, monitored branches, and cost limits per repository in{" "}
              <span className="font-mono text-amber-400">Settings</span>.
              Fix automation is managed for you — contact an administrator for anything beyond these features.
            </p>
          </div>
        </div>
      </AppLayout>
    );
  }

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
          {isDirty && (
            <Button
              size="sm"
              onClick={handleSaveConfig}
              disabled={!canSave}
              className={cn(
                "h-7.5 px-3 text-[11px] font-mono font-bold rounded-[5px] flex items-center gap-1.5 transition-all",
                !canSave
                  ? "bg-zinc-800/90 text-zinc-400 border border-zinc-700/60 cursor-not-allowed"
                  : "bg-gradient-to-b from-amber-400 via-amber-450 to-amber-500 hover:from-amber-300 hover:to-amber-400 text-zinc-950 border-t border-t-amber-200/60 border-x border-x-amber-400/80 border-b border-b-amber-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.4),0_2px_8px_rgba(245,158,11,0.35)] active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.4)] cursor-pointer"
              )}
            >
              {isPending ? (
                <RefreshCw className="h-3.5 w-3.5 animate-spin text-zinc-950" />
              ) : effectiveTargetIsGlobal && !isAdmin ? (
                <Lock className="h-3.5 w-3.5 text-zinc-400" />
              ) : (
                <Check className="h-3.5 w-3.5 text-zinc-950 stroke-[2.5]" />
              )}
              <span>{effectiveTargetIsGlobal && !isAdmin ? "Admin Only" : "Save Changes"}</span>
              {!(effectiveTargetIsGlobal && !isAdmin) && (
                <kbd className="hidden sm:inline-block rounded bg-zinc-950/20 px-1 py-0.2 text-[10px] font-mono text-zinc-900 border border-zinc-950/30">
                  ⌘S
                </kbd>
              )}
            </Button>
          )}

          <Button
            variant="outline"
            size="sm"
            onClick={() => fetchActiveConfig(true)}
            disabled={loading || isRefreshing}
            className="group h-7.5 px-2.5 text-[11px] font-mono rounded-[5px] text-zinc-300 hover:text-white bg-gradient-to-b from-zinc-800/90 via-zinc-850 to-zinc-900/90 border-t border-t-zinc-600/70 border-x border-x-zinc-700/60 border-b border-b-zinc-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.12),0_1px_3px_rgba(0,0,0,0.35),0_1px_2px_rgba(0,0,0,0.2)] hover:border-t-zinc-500 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.2),0_2px_6px_rgba(0,0,0,0.4)] active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.4)] flex items-center gap-1.5 transition-all cursor-pointer"
            title="Sync configuration (Hot-key: R)"
          >
            <RefreshCw className={cn("h-3.5 w-3.5 transition-colors", isRefreshing ? "animate-spin text-amber-400" : "text-zinc-400 group-hover:text-amber-400")} />
            <span className="hidden sm:inline">Sync</span>
          </Button>
        </div>
      }
    >
      <div className="space-y-4 min-w-0 w-full max-w-none pb-12">
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

        {/* TOP BAR: Scope Switcher + Target Repository Control */}
        <div className="relative overflow-hidden rounded-xl border-t border-t-zinc-600/60 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-gradient-to-b from-[#111115]/95 via-[#0d0d10]/95 to-[#09090c]/95 backdrop-blur-sm p-3.5 sm:p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.12),inset_0_-1px_0_rgba(0,0,0,0.4),0_8px_32px_rgba(0,0,0,0.5)] space-y-3">
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
                  onClick={() => {
                    setSelectedScope("global");
                    if (!globalConfig) {
                      setActiveConfig(null);
                      setSelectedProvider("opencode_zen");
                      setSelectedModel("nemotron-3.5-lightning-free");
                    }
                  }}
                  className={cn(
                    "flex items-center gap-1.5 rounded-[5px] px-3 py-1 text-xs sm:text-[13px] font-mono transition-all cursor-pointer select-none",
                    selectedScope === "global"
                      ? "bg-zinc-800 text-amber-400 shadow-sm border border-zinc-700/80 font-bold"
                      : "text-zinc-400 hover:text-zinc-200"
                  )}
                >
                  <Globe className="h-3.5 w-3.5 text-amber-400" />
                  <span>Global Platform Default</span>
                  {!isAdmin && (
                    <span className="ml-1 inline-flex items-center gap-1 rounded bg-zinc-900 border border-zinc-700/70 px-1.5 py-0.5 text-[10px] text-zinc-400 font-mono">
                      <Lock className="h-2.5 w-2.5 text-zinc-400" />
                      <span>Admin Only</span>
                    </span>
                  )}
                </button>

                <button
                  type="button"
                  onClick={() => setSelectedScope("repo")}
                  className={cn(
                    "flex items-center gap-1.5 rounded-[5px] px-3 py-1 text-xs sm:text-[13px] font-mono transition-all cursor-pointer select-none",
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

            {/* Scope Status Badges: override vs inherit + 1-click reset */}
            <div className="flex flex-wrap items-center gap-2">
              {selectedScope === "global" ? (
                <span className="text-[11px] font-mono font-medium px-2.5 py-1 rounded-[5px] border flex items-center gap-1.5 select-none bg-emerald-950/40 border-emerald-800/50 text-emerald-400">
                  <Globe className="h-3.5 w-3.5 text-emerald-400" />
                  <span>Global Default · Applies to all repos without overrides</span>
                </span>
              ) : isRepoCustomOverride ? (
                <>
                  <span className="text-[11px] font-mono font-semibold px-2.5 py-1 rounded-[5px] border flex items-center gap-1.5 select-none bg-cyan-950/50 border-cyan-700/60 text-cyan-300 shadow-[0_0_10px_rgba(6,182,212,0.18)]">
                    <GitBranch className="h-3.5 w-3.5 text-cyan-300" />
                    <span>Custom Repo Override</span>
                  </span>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={handleResetToGlobalDefault}
                    disabled={isResetting || loading || !selectedRepoId || (!globalConfig && !isRepoCustomOverride)}
                    className="h-7 px-2.5 text-[10.5px] font-mono rounded-[5px] text-zinc-300 hover:text-white bg-gradient-to-b from-zinc-800/90 via-zinc-850 to-zinc-900/90 border border-zinc-700/60 cursor-pointer"
                    title={
                      globalConfig
                        ? `Reset to global default (${formatModelDisplayName(globalConfig.model_name)})`
                        : "Reset to global default"
                    }
                  >
                    {isResetting ? (
                      <RefreshCw className="h-3 w-3 mr-1.5 animate-spin" />
                    ) : (
                      <RotateCcw className="h-3 w-3 mr-1.5" />
                    )}
                    <span>{isResetting ? "Resetting..." : "Reset to Global Default"}</span>
                  </Button>
                </>
              ) : (
                <span className="text-[11px] font-mono font-medium px-2.5 py-1 rounded-[5px] border flex items-center gap-1.5 select-none bg-zinc-900/90 border-zinc-700/70 text-zinc-300">
                  <ShieldCheck className="h-3.5 w-3.5 text-emerald-400" />
                  <span>Inheriting Platform Default</span>
                </span>
              )}
            </div>
          </div>

          {/* Repo SelectDropdown (when Repo Override is selected) */}
          {selectedScope === "repo" && (
            <div className="relative z-10 pt-2.5 border-t border-zinc-800/80 flex flex-col sm:flex-row sm:items-center gap-2.5 animate-in fade-in duration-150">
              <label className="text-xs font-mono font-semibold uppercase tracking-wider text-zinc-300 shrink-0">
                Target Repository:
              </label>
              {repos.length === 0 ? (
                <div className="text-xs text-zinc-400 font-mono p-2 border border-dashed border-zinc-800 rounded bg-zinc-950">
                  No connected repositories found. Connect a repository first in the Repositories tab.
                </div>
              ) : (
                <div className="flex-1 flex flex-col gap-2.5">
                  <div className="flex flex-col sm:flex-row sm:items-center gap-3">
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
                      buttonClassName="min-w-[240px] h-8 font-mono text-xs"
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
                  {currentRepo && activeConfig && (globalConfig || isRepoCustomOverride) && (
                    <div className="flex flex-wrap items-center gap-2 text-xs font-mono">
                      {isRepoCustomOverride ? (
                        <span className="inline-flex items-center gap-1.5 rounded-[5px] border border-cyan-700/60 bg-cyan-950/50 px-2.5 py-1 text-cyan-300">
                          <GitBranch className="h-3 w-3" />
                          <span>
                            {currentRepo.owner}/{currentRepo.name} uses custom override:{" "}
                            <strong>{formatModelDisplayName(activeConfig.model_name)}</strong>
                          </span>
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-1.5 rounded-[5px] border border-zinc-700/70 bg-zinc-900/80 px-2.5 py-1 text-zinc-300">
                          <Globe className="h-3 w-3 text-zinc-400" />
                          <span>
                            {currentRepo.owner}/{currentRepo.name} inherits platform default:{" "}
                            <strong className="text-zinc-100">{formatModelDisplayName(globalConfig?.model_name ?? activeConfig.model_name)}</strong>
                          </span>
                        </span>
                      )}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}

          {/* Subtle Notice for Non-Admins viewing Global Scope */}
          {selectedScope === "global" && !isAdmin && (
            <div className="relative z-10 pt-2.5 border-t border-zinc-800/80 flex items-center gap-2.5 text-xs font-mono text-zinc-300 bg-zinc-900/50 rounded-lg px-3.5 py-2.5 border border-zinc-800/90 shadow-inner">
              <Lock className="h-4 w-4 text-amber-400 shrink-0" />
              <span>
                Global platform default can only be modified by administrators. Switch to Repository Override to customize models for your repos.
              </span>
            </div>
          )}
        </div>

        {/* MAIN HERO: Provider Tabs + Models Grid */}
        <div className="relative rounded-xl border-t border-t-zinc-600/60 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-gradient-to-b from-[#111115]/95 via-[#0d0d10]/95 to-[#09090c]/95 backdrop-blur-sm p-4 sm:p-5 shadow-[inset_0_1px_0_rgba(255,255,255,0.12),inset_0_-1px_0_rgba(0,0,0,0.4),0_8px_32px_rgba(0,0,0,0.5)] space-y-4.5">
          <span
            aria-hidden="true"
            className="pointer-events-none absolute inset-x-0 top-0 h-6 rounded-t-xl bg-gradient-to-b from-white/[0.04] to-transparent z-10"
          />

          {/* Header & Provider Segmented Tabs */}
          <div className="relative z-30 flex flex-col md:flex-row md:items-center justify-between gap-4 pb-4 border-b border-zinc-800/80">
            <div>
              <h2 className="text-sm sm:text-base font-bold text-zinc-100 flex items-center gap-2">
                <Cpu className="h-4.5 w-4.5 text-amber-400" />
                <span>Autonomous Inference Engine</span>
              </h2>
              <p className="text-xs text-zinc-400 mt-0.5 font-mono">
                Strict server allowlist enforced · Zero free-text injection
              </p>
            </div>

            {/* Searchbar & Provider Switcher */}
            <div className="relative z-30 flex flex-wrap sm:flex-nowrap items-center gap-2.5">
              {/* Header Dual Searchbar (Providers & Models) */}
              <div className="relative z-40">
                <Search className="h-3.5 w-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 text-zinc-500 pointer-events-none" />
                <input
                  ref={searchInputRef}
                  type="text"
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Escape" && searchQuery) {
                      setSearchQuery("");
                    }
                  }}
                  placeholder="Search providers & models..."
                  className="h-8 w-48 sm:w-64 font-mono text-xs bg-zinc-900/80 border border-zinc-700/80 rounded px-2.5 pl-8 pr-7 text-zinc-200 placeholder:text-zinc-500 focus:outline-none focus:border-amber-400 focus:ring-1 focus:ring-amber-400/25 transition-all"
                />
                {searchQuery ? (
                  <button
                    type="button"
                    onClick={() => {
                      setSearchQuery("");
                      searchInputRef.current?.focus();
                    }}
                    className="absolute right-2 top-1/2 -translate-y-1/2 text-zinc-500 hover:text-zinc-300 p-0.5 rounded cursor-pointer"
                    aria-label="Clear search query"
                  >
                    <X className="h-3 w-3" />
                  </button>
                ) : (
                  <div className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 hidden sm:block">
                    <kbd className="inline-flex h-3.5 min-w-3.5 items-center justify-center rounded border border-zinc-700/60 bg-zinc-800/60 px-1 font-mono text-[9px] text-zinc-400 select-none">
                      /
                    </kbd>
                  </div>
                )}
              </div>

              <span className="text-xs font-mono font-medium text-zinc-400 hidden sm:inline-block">
                Provider:
              </span>
              <SelectDropdown
                id="provider-selector"
                aria-label="Inference Provider Selector"
                value={selectedProvider}
                onChange={handleProviderChange}
                options={providerDropdownOptions}
                placeholder={activeProviderMeta.name}
                ignoreClickOutsideRef={searchInputRef}
                buttonClassName="min-w-[190px] sm:min-w-[210px] h-8 font-mono text-xs bg-zinc-900/80 border-zinc-700/80 hover:border-zinc-600 text-zinc-200"
                disabled={isPending || !canMutateTarget}
              />
              {isAdmin && (
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={handleOpenCustomModal}
                  className="group h-8 px-2.5 text-xs font-mono rounded-[5px] text-zinc-200 hover:text-white bg-gradient-to-b from-zinc-800/90 via-zinc-850 to-zinc-900/90 border-t border-t-zinc-600/70 border-x border-x-zinc-700/60 border-b border-b-zinc-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.1),0_1px_3px_rgba(0,0,0,0.35)] hover:border-t-zinc-500 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.18),0_2px_6px_rgba(0,0,0,0.4)] active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.4)] cursor-pointer flex items-center gap-1.5 shrink-0 transition-all"
                >
                  <Plus className="h-3.5 w-3.5 text-amber-400/90 group-hover:text-amber-300 transition-colors" />
                  <span>Add Custom Provider</span>
                </Button>
              )}
            </div>
          </div>

          {/* Provider Summary Strip */}
          <div className="relative z-10 flex flex-col sm:flex-row sm:items-center justify-between gap-3 px-3.5 py-2.5 rounded-[7px] border-t border-t-zinc-600/70 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-gradient-to-b from-[#141418]/95 via-[#0e0e12]/95 to-[#09090c]/95 text-xs sm:text-[12.5px] shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_2px_8px_rgba(0,0,0,0.35)]">
            <div className="flex flex-wrap items-center gap-2">
              {registeredCustom && (
                <div className="flex items-center gap-1.5 mr-1">
                  <span className="h-2 w-2 rounded-full bg-emerald-400 animate-pulse shadow-[0_0_6px_rgba(52,211,153,0.8)]" />
                  <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-[4px] border border-amber-500/30 bg-amber-500/10 text-[10.5px] font-mono text-amber-300 font-medium">
                    {registeredCustom.compat === "anthropic" ? (
                      <>
                        <Zap className="h-3 w-3 text-amber-400" />
                        <span>Anthropic Compatible</span>
                      </>
                    ) : (
                      <>
                        <Sparkles className="h-3 w-3 text-amber-400" />
                        <span>OpenAI Compatible</span>
                      </>
                    )}
                  </span>
                </div>
              )}
              <span className="font-bold text-zinc-100">{activeProviderMeta.name}:</span>
              <span className="text-zinc-300 font-sans">{activeProviderMeta.description}</span>
            </div>
            <div className="flex items-center gap-1.5 shrink-0">
              <span className="text-xs font-mono text-zinc-300 bg-zinc-900/90 px-2.5 py-1 rounded-[5px] border border-zinc-800/90 shrink-0 select-all shadow-[inset_0_1px_2px_rgba(0,0,0,0.4)]">
                {activeProviderMeta.baseUrl.replace("https://", "")}
              </span>
              <button
                type="button"
                onClick={() => handleCopyUrl(activeProviderMeta.baseUrl)}
                aria-label="Copy base URL"
                title="Copy base URL"
                className="h-7 w-7 rounded-[5px] flex items-center justify-center border border-zinc-800 bg-zinc-900/80 text-zinc-400 hover:text-zinc-200 hover:border-zinc-700 active:translate-y-[0.5px] transition-all cursor-pointer"
              >
                {copiedUrl ? (
                  <Check className="h-3.5 w-3.5 text-emerald-400" />
                ) : (
                  <Copy className="h-3.5 w-3.5" />
                )}
              </button>
            </div>
          </div>

          {/* Discovered models & save guidance for active custom provider */}
          {registeredCustom && (
            <div
              role="status"
              className="relative z-10 flex flex-col sm:flex-row sm:items-center justify-between gap-2.5 px-3.5 py-2.5 rounded-[7px] border-t border-t-amber-400/40 border-x border-x-amber-500/25 border-b border-b-amber-950/80 bg-gradient-to-b from-amber-950/25 via-[#13110e]/95 to-[#0b0a08]/95 text-xs font-mono text-amber-200 shadow-[inset_0_1px_0_rgba(251,191,36,0.12),0_2px_8px_rgba(0,0,0,0.3)]"
            >
              <div className="flex items-center gap-2">
                <CheckCircle2 className="h-4 w-4 text-amber-400 shrink-0" />
                <span>
                  Discovered <strong>{(modelOptionsByProvider[selectedProvider] || []).length} model{((modelOptionsByProvider[selectedProvider] || []).length === 1 ? "" : "s")}</strong> for <strong>{registeredCustom.name}</strong>. Select an architecture below and save.
                </span>
              </div>
              <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-[4px] border border-amber-500/30 bg-amber-500/10 text-[11px] text-amber-300 font-medium shadow-[inset_0_1px_0_rgba(251,191,36,0.15)] shrink-0 self-start sm:self-auto">
                <span className="h-1.5 w-1.5 rounded-full bg-amber-400 animate-pulse" />
                <span>Save Configuration to persist</span>
              </span>
            </div>
          )}

          {/* Models Header & Filter Controls (Hot-key '/' enabled) */}
          <div className="relative z-10 space-y-4 pt-1">
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
              <div>
                <div className="flex items-center gap-2.5">
                  <h3 className="text-[11px] font-mono font-bold uppercase tracking-wider text-zinc-200">
                    Model Architectures
                  </h3>
                  <span className="inline-flex items-center px-1.5 py-0.5 rounded-[4px] bg-zinc-900 border border-zinc-800 text-[9.5px] font-mono text-zinc-400">
                    {searchQuery.trim()
                      ? speedFilter !== "all"
                        ? `${currentModels.length} of ${matchingSearchModels.length} Models Found`
                        : `${currentModels.length} Model${currentModels.length === 1 ? "" : "s"} Found`
                      : speedFilter !== "all"
                      ? `${currentModels.length} of ${allProviderModels.length} Available`
                      : `${allProviderModels.length} Available`}
                  </span>
                </div>
                <p className="text-xs text-zinc-400 mt-0.5">
                  Select an inference engine to power autonomous CI failure diagnostics & fix generation
                </p>
              </div>

              {/* Quick Filter Tabs (Speed category) */}
              <div className="inline-flex rounded-[7px] border-t border-t-zinc-700/60 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-[#0a0a0d] p-1 shadow-[inset_0_1px_2px_rgba(0,0,0,0.5)] text-xs font-mono gap-1">
                  <button
                    type="button"
                    onClick={() => setSpeedFilter("all")}
                    className={cn(
                      "px-2 py-0.5 rounded-[4px] text-[10px] font-mono transition-all cursor-pointer select-none",
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
                      "px-2 py-0.5 rounded-[4px] text-[10px] font-mono transition-all cursor-pointer select-none",
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
                      "px-2 py-0.5 rounded-[4px] text-[10px] font-mono transition-all cursor-pointer select-none",
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
                      "px-2 py-0.5 rounded-[4px] text-[10px] font-mono transition-all cursor-pointer select-none",
                      speedFilter === "reasoning"
                        ? "bg-gradient-to-b from-purple-900/90 via-purple-950 to-purple-950/90 text-purple-300 font-bold border-t border-t-purple-500/70 border-x border-x-purple-600/60 border-b border-b-purple-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.14),0_1px_3px_rgba(0,0,0,0.4)]"
                        : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/40"
                    )}
                  >
                    Deep CoT
                  </button>
                </div>
              </div>

            {loading ? (
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3.5">
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
              <div ref={modelsGridRef} className="space-y-4">
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3.5">
                  {displayedModels.map((m) => {
                  const modelProvider = m.provider || selectedProvider;
                  const isSelected = m.id === selectedModel && modelProvider === selectedProvider;
                  const spec = getModelSpec(m.id, m.tag, m.context_window, m.token_cost || m.cost);
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
                      key={`${m.provider || selectedProvider}:${m.id}`}
                      role="button"
                      tabIndex={isPending || !canMutateTarget ? -1 : 0}
                      aria-pressed={isSelected}
                      aria-disabled={isPending || !canMutateTarget}
                      onClick={() => {
                        if (!isPending && canMutateTarget) {
                          setSelectedModel(m.id);
                          if (m.provider && m.provider !== selectedProvider) {
                            setSelectedProvider(m.provider);
                          }
                        }
                      }}
                      onKeyDown={(e) => {
                        if ((e.key === "Enter" || e.key === " ") && !isPending && canMutateTarget) {
                          e.preventDefault();
                          setSelectedModel(m.id);
                          if (m.provider && m.provider !== selectedProvider) {
                            setSelectedProvider(m.provider);
                          }
                        }
                      }}
                      className={cn(
                        "group relative flex flex-col justify-between rounded-xl p-4 text-left select-none transition-all duration-150 focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-400/60 overflow-hidden",
                        isSelected
                          ? "border-t border-t-amber-400 border-x border-x-amber-500/60 border-b border-b-amber-950 bg-gradient-to-b from-[#1a140b]/95 via-[#130f08]/95 to-[#0c0a05]/95 shadow-[inset_0_1px_0_rgba(255,255,255,0.2),0_0_24px_rgba(245,158,11,0.22),0_8px_24px_rgba(0,0,0,0.5)] ring-1 ring-amber-400/50"
                          : "border-t border-t-zinc-600/60 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-gradient-to-b from-[#111115]/95 via-[#0d0d10]/95 to-[#09090c]/95 shadow-[inset_0_1px_0_rgba(255,255,255,0.12),0_8px_24px_rgba(0,0,0,0.4)] hover:border-t-zinc-500 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.18),0_12px_28px_rgba(0,0,0,0.5)]",
                        "cursor-pointer active:translate-y-[0.5px]"
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
                        {/* Header: Provider badge (when cross-searching) + Tag + Live Status + Radio Indicator */}
                        <div className="flex items-center justify-between gap-2">
                          <div className="flex items-center gap-1.5 flex-wrap">
                            {searchQuery.trim() && m.provider && (
                              <span className="text-[9px] font-mono px-1.5 py-0.5 rounded bg-zinc-800/90 border border-zinc-700/70 text-zinc-300 font-medium">
                                {PROVIDER_METADATA[m.provider]?.name || m.provider}
                              </span>
                            )}
                            <span className={cn("text-[10px] font-mono font-semibold px-2 py-0.5 rounded-[4px]", badgePill)}>
                              {m.tag}
                            </span>
                          </div>
                          <div className="flex items-center gap-2">
                            <span
                              className={cn(
                                "font-mono text-[9.5px] px-2 py-0.5 rounded-[4px] border transition-colors",
                                isSelected
                                  ? "bg-amber-950/40 border-amber-800/50 text-amber-300/90"
                                  : "bg-zinc-800/80 border-zinc-700/60 text-zinc-400"
                              )}
                            >
                              {spec.tokenCost}
                            </span>
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
                            "text-sm font-bold tracking-tight transition-colors",
                            isSelected ? "text-amber-300 font-bold" : "text-zinc-100 group-hover:text-white"
                          )}>
                            {displayName}
                          </h4>
                          <div className="mt-1.5 flex items-center gap-1.5">
                            <span className="font-mono text-[10px] text-zinc-300 bg-[#08080a] border border-zinc-800/90 px-2 py-0.5 rounded-[4px] select-all font-medium truncate max-w-[210px] shadow-inner">
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
                          <span className="flex items-center gap-1.5 bg-gradient-to-b from-zinc-800/90 via-zinc-850 to-zinc-900/90 text-zinc-200 border-t border-t-zinc-700/60 border-x border-x-zinc-800/70 border-b border-b-zinc-950 px-2 py-0.5 rounded-[4px] font-medium text-[10px] shadow-[inset_0_1px_0_rgba(255,255,255,0.08)]">
                            <Layers className="h-3.5 w-3.5 text-zinc-400 group-hover:text-zinc-300" />
                            <span>{spec.contextWindow}</span>
                          </span>
                          <span className="flex items-center gap-1.5 bg-gradient-to-b from-zinc-800/90 via-zinc-850 to-zinc-900/90 text-zinc-200 border-t border-t-zinc-700/60 border-x border-x-zinc-800/70 border-b border-b-zinc-950 px-2 py-0.5 rounded-[4px] font-medium text-[10px] shadow-[inset_0_1px_0_rgba(255,255,255,0.08)]">
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
                        <p className="text-[11px] text-zinc-300 group-hover:text-zinc-200 leading-relaxed font-sans line-clamp-2 transition-colors">
                          {spec.specialty}
                        </p>
                      </div>

                      {/* Card Footer */}
                      <div className="relative z-10 mt-2.5 pt-2.5 border-t border-zinc-800/80 flex items-center justify-between text-[11px] text-zinc-400">
                        <span className="font-medium text-zinc-400 font-mono text-[10px]">
                          {spec.recommendedRole}
                        </span>
                        <span className={cn("font-semibold text-xs flex items-center gap-1 font-mono transition-colors", isSelected ? "text-amber-300 font-bold" : "text-zinc-400 group-hover:text-zinc-200")}>
                          {isSelected ? (
                            <span className="flex items-center gap-1.5 bg-amber-400/20 border border-amber-500/60 text-amber-300 px-2.5 py-1 rounded-[5px] shadow-[0_0_10px_rgba(245,158,11,0.2)] font-bold">
                              <Check className="h-3.5 w-3.5 text-amber-300 stroke-[2.5]" />
                              <span>Selected</span>
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

              {hasMoreModels && (
                <div className="flex justify-center pt-2">
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => {
                      if (isExpanded) {
                        setIsExpanded(false);
                        modelsGridRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
                      } else {
                        setIsExpanded(true);
                      }
                    }}
                    className="h-8 px-4 text-xs font-mono font-medium rounded-lg border-t border-t-zinc-600/70 border-x border-x-zinc-700/60 border-b border-b-zinc-950 bg-gradient-to-b from-[#141418] to-[#0c0c0f] hover:from-[#181820] hover:to-[#101014] text-zinc-200 hover:text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_2px_6px_rgba(0,0,0,0.3)] active:translate-y-[0.5px] transition-all cursor-pointer flex items-center gap-1.5"
                  >
                    <span>
                      {isExpanded
                        ? "View Less"
                        : `View More (${currentModels.length - DEFAULT_CARD_LIMIT} more models)`}
                    </span>
                    {isExpanded ? (
                      <ChevronUp className="h-3.5 w-3.5 text-zinc-400" />
                    ) : (
                      <ChevronDown className="h-3.5 w-3.5 text-zinc-400" />
                    )}
                  </Button>
                </div>
              )}
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
                disabled={!canSave}
                size="sm"
                className={cn(
                  "relative px-4 py-1.5 font-mono font-bold text-[11px] rounded-[5px] transition-all select-none",
                  effectiveTargetIsGlobal && !isAdmin
                    ? "bg-zinc-800/90 text-zinc-400 border border-zinc-700/60 cursor-not-allowed"
                    : isDirty
                    ? "bg-gradient-to-b from-amber-400 via-amber-450 to-amber-500 hover:from-amber-300 hover:to-amber-400 text-zinc-950 border-t border-t-amber-200/60 border-x border-x-amber-400/80 border-b border-b-amber-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.4),0_2px_8px_rgba(245,158,11,0.35)] active:translate-y-[0.5px] cursor-pointer"
                    : "bg-zinc-800/90 text-zinc-400 hover:bg-zinc-800 border border-zinc-700/60"
                )}
              >
                {isPending ? (
                  <span className="flex items-center gap-2">
                    <RefreshCw className="h-3.5 w-3.5 animate-spin text-zinc-950" />
                    <span>Saving...</span>
                  </span>
                ) : effectiveTargetIsGlobal && !isAdmin ? (
                  <span className="flex items-center gap-2 text-zinc-400 font-medium">
                    <Lock className="h-3.5 w-3.5 text-zinc-400" />
                    <span>Admin Only</span>
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

        {/* FLOATING ACTION DOCK: When changes are dirty, provide instant saving anywhere on the page */}
        {isDirty && (
          <div className="fixed bottom-6 inset-x-0 z-40 flex justify-center px-4 pointer-events-none animate-in fade-in slide-in-from-bottom-5 duration-200">
            <div className="pointer-events-auto flex flex-col sm:flex-row items-center justify-between gap-3 sm:gap-6 max-w-2xl w-full rounded-2xl border-t border-t-zinc-600/70 border-x border-x-zinc-700/60 border-b border-b-zinc-950 bg-gradient-to-b from-[#18181f]/95 via-[#131317]/95 to-[#0d0d11]/95 p-3 sm:px-4 sm:py-2.5 shadow-[inset_0_1px_0_rgba(255,255,255,0.14),0_12px_32px_rgba(0,0,0,0.65),0_0_24px_rgba(245,158,11,0.18)] backdrop-blur-md ring-1 ring-amber-400/30">
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
                  disabled={!canSave}
                  className={cn(
                    "h-7 px-3.5 text-[11px] font-mono font-bold rounded-[5px] transition-all select-none flex items-center gap-1.5",
                    effectiveTargetIsGlobal && !isAdmin
                      ? "bg-zinc-800/90 text-zinc-400 border border-zinc-700/60 cursor-not-allowed"
                      : "bg-gradient-to-b from-amber-400 via-amber-450 to-amber-500 hover:from-amber-300 hover:to-amber-400 text-zinc-950 border-t border-t-amber-200/60 border-x border-x-amber-400/80 border-b border-b-amber-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.4),0_2px_8px_rgba(245,158,11,0.35)] active:translate-y-[0.5px] cursor-pointer"
                  )}
                >
                  {isPending ? (
                    <>
                      <RefreshCw className="h-3.5 w-3.5 animate-spin text-zinc-950" />
                      <span>Saving...</span>
                    </>
                  ) : effectiveTargetIsGlobal && !isAdmin ? (
                    <>
                      <Lock className="h-3.5 w-3.5 text-zinc-400" />
                      <span>Admin Only</span>
                    </>
                  ) : (
                    <>
                      <span>Save Configuration</span>
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
        {/* Top-level Admin Custom Provider Modal to avoid backdrop-blur-sm containing block trap */}
        {/* Admin custom provider modal: name + compat + baseURL + key -> validate -> discover -> select -> persist via Save */}
        {isAdmin && (
          <Modal
            isOpen={isCustomModalOpen}
            onClose={handleCloseCustomModal}
            title="Custom Provider"
            description="Connect an OpenAI or Anthropic-compatible custom inference endpoint."
            icon={<Server className="h-4 w-4 text-amber-400" />}
            className="max-w-lg"
          >
            <div className="space-y-4.5">
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3.5">
                <label className="flex flex-col gap-1.5 text-xs font-mono text-zinc-300">
                  <span className="flex items-center justify-between">
                    <span>Name <span className="text-red-400">*</span></span>
                    <span className="text-[10px] text-zinc-500 font-sans">Identifier</span>
                  </span>
                  <input
                    aria-label="Custom provider name"
                    value={customName}
                    onChange={(e) => setCustomName(e.target.value)}
                    disabled={isPending || isValidatingCustom}
                    placeholder="my-provider"
                    className="h-9 rounded-[6px] border-t border-t-zinc-700/90 border-x border-x-zinc-800/80 border-b border-b-zinc-900 bg-[#09090c]/90 px-3 text-xs font-mono text-zinc-200 placeholder:text-zinc-600 shadow-[inset_0_1px_2px_rgba(0,0,0,0.5)] focus:outline-none focus:border-white focus:border-t-white focus:border-x-white focus:border-b-white focus:ring-1 focus:ring-white transition-all"
                  />
                </label>
                <div className="flex flex-col gap-1.5 text-xs font-mono text-zinc-300">
                  <span className="flex items-center justify-between">
                    <span>Compatibility <span className="text-red-400">*</span></span>
                    <span className="text-[10px] text-zinc-500 font-sans">Wire protocol</span>
                  </span>
                  <div className="relative flex rounded-[6px] border border-zinc-800/90 bg-[#09090c]/90 p-0.5 h-9 shadow-[inset_0_1px_2px_rgba(0,0,0,0.4)]">
                    <button
                      type="button"
                      role="tab"
                      aria-selected={customCompat === "openai"}
                      onClick={() => setCustomCompat("openai")}
                      disabled={isPending || isValidatingCustom}
                      className={cn(
                        "flex-1 flex items-center justify-center gap-1.5 rounded-[4px] text-xs font-mono font-medium transition-all cursor-pointer focus:outline-none focus-visible:ring-1 focus-visible:ring-white/40",
                        customCompat === "openai"
                          ? "bg-gradient-to-b from-zinc-800/95 via-zinc-850 to-zinc-900/95 text-white border-t border-t-zinc-600/70 border-x border-x-zinc-700/60 border-b border-b-zinc-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.12),0_1px_3px_rgba(0,0,0,0.4)]"
                          : "border-0 border-transparent bg-transparent shadow-none text-zinc-400 hover:text-zinc-200"
                      )}
                    >
                      <Sparkles className={cn("h-3.5 w-3.5", customCompat === "openai" ? "text-white" : "text-zinc-500")} />
                      <span>OpenAI</span>
                    </button>
                    <button
                      type="button"
                      role="tab"
                      aria-selected={customCompat === "anthropic"}
                      onClick={() => setCustomCompat("anthropic")}
                      disabled={isPending || isValidatingCustom}
                      className={cn(
                        "flex-1 flex items-center justify-center gap-1.5 rounded-[4px] text-xs font-mono font-medium transition-all cursor-pointer focus:outline-none focus-visible:ring-1 focus-visible:ring-white/40",
                        customCompat === "anthropic"
                          ? "bg-gradient-to-b from-zinc-800/95 via-zinc-850 to-zinc-900/95 text-white border-t border-t-zinc-600/70 border-x border-x-zinc-700/60 border-b border-b-zinc-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.12),0_1px_3px_rgba(0,0,0,0.4)]"
                          : "border-0 border-transparent bg-transparent shadow-none text-zinc-400 hover:text-zinc-200"
                      )}
                    >
                      <Zap className={cn("h-3.5 w-3.5", customCompat === "anthropic" ? "text-white" : "text-zinc-500")} />
                      <span>Anthropic</span>
                    </button>
                    <select
                      aria-label="Custom provider compatibility"
                      value={customCompat}
                      onChange={(e) => setCustomCompat(e.target.value as CustomProviderCompat)}
                      disabled={isPending || isValidatingCustom}
                      className="sr-only"
                      tabIndex={-1}
                    >
                      <option value="openai">OpenAI-compatible</option>
                      <option value="anthropic">Anthropic-compatible</option>
                    </select>
                  </div>
                </div>
                <div className="flex flex-col gap-1.5 sm:col-span-2">
                  <div className="flex items-center justify-between text-xs font-mono text-zinc-300">
                    <label htmlFor="custom-base-url" className="flex items-center gap-1">
                      <span>Base URL <span className="text-red-400">*</span></span>
                    </label>
                    <span className="text-[10px] text-zinc-500 font-sans">Must include path (e.g. /v1)</span>
                  </div>
                  <input
                    id="custom-base-url"
                    aria-label="Custom provider base URL"
                    value={customBaseUrl}
                    onChange={(e) => setCustomBaseUrl(e.target.value)}
                    disabled={isPending || isValidatingCustom}
                    placeholder="https://my-host/v1"
                    className="h-9 rounded-[6px] border-t border-t-zinc-700/90 border-x border-x-zinc-800/80 border-b border-b-zinc-900 bg-[#09090c]/90 px-3 text-xs font-mono text-zinc-200 placeholder:text-zinc-600 shadow-[inset_0_1px_2px_rgba(0,0,0,0.5)] focus:outline-none focus:border-white focus:border-t-white focus:border-x-white focus:border-b-white focus:ring-1 focus:ring-white transition-all"
                  />
                  <div className="flex flex-wrap items-center gap-1.5 mt-1">
                    <span className="text-[10px] font-mono text-zinc-500 uppercase tracking-wider mr-0.5">Presets:</span>
                    {BASE_URL_PRESETS.map((preset) => {
                      const isActive = customBaseUrl === preset.value;
                      return (
                        <button
                          key={preset.label}
                          type="button"
                          disabled={isPending || isValidatingCustom}
                          onClick={() => setCustomBaseUrl(preset.value)}
                          className={cn(
                            "px-2.5 py-0.5 rounded-[4px] border text-[11px] font-mono transition-all cursor-pointer active:translate-y-[0.5px] focus:outline-none focus-visible:ring-1 focus-visible:ring-white/40",
                            isActive
                              ? "border-t border-t-amber-400/40 border-x border-x-amber-500/30 border-b border-b-amber-600/40 bg-gradient-to-b from-amber-500/20 to-amber-950/40 text-amber-300 shadow-[inset_0_1px_0_rgba(251,191,36,0.2),0_1px_2px_rgba(0,0,0,0.3)] font-medium"
                              : "border-zinc-800/90 bg-[#0c0c0f] text-zinc-400 hover:border-zinc-700 hover:text-zinc-200 hover:bg-zinc-850/50"
                          )}
                        >
                          {preset.label}
                        </button>
                      );
                    })}
                  </div>
                </div>
                <div className="flex flex-col gap-1.5 sm:col-span-2">
                  <div className="flex items-center justify-between text-xs font-mono text-zinc-300">
                    <label htmlFor="custom-api-key" className="flex items-center gap-1">
                      <span>API Key <span className="text-red-400">*</span></span>
                    </label>
                    <span className="text-[10px] text-zinc-500 font-sans">Sent in Authorization bearer header</span>
                  </div>
                  <div className="relative">
                    <input
                      id="custom-api-key"
                      aria-label="Custom provider API key"
                      type={showApiKey ? "text" : "password"}
                      value={customApiKey}
                      onChange={(e) => setCustomApiKey(e.target.value)}
                      disabled={isPending || isValidatingCustom}
                      placeholder="sk-..."
                      className="h-9 w-full rounded-[6px] border-t border-t-zinc-700/90 border-x border-x-zinc-800/80 border-b border-b-zinc-900 bg-[#09090c]/90 pl-3 pr-10 text-xs font-mono text-zinc-200 placeholder:text-zinc-600 shadow-[inset_0_1px_2px_rgba(0,0,0,0.5)] focus:outline-none focus:border-white focus:border-t-white focus:border-x-white focus:border-b-white focus:ring-1 focus:ring-white transition-all"
                    />
                    <button
                      type="button"
                      onClick={() => setShowApiKey((prev) => !prev)}
                      disabled={isPending || isValidatingCustom}
                      aria-label={showApiKey ? "Hide API key" : "Show API key"}
                      className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded-[4px] p-1.5 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-white/40 active:translate-y-[calc(-50%+0.5px)] transition-all cursor-pointer"
                    >
                      {showApiKey ? (
                        <EyeOff className="h-3.5 w-3.5" />
                      ) : (
                        <Eye className="h-3.5 w-3.5" />
                      )}
                    </button>
                  </div>
                </div>
              </div>

              {customError && (
                <div
                  role="alert"
                  className="flex items-start gap-2.5 p-3 rounded-[6px] border-t border-t-red-500/40 border-x border-x-red-600/30 border-b border-b-red-950 bg-gradient-to-b from-red-950/40 via-red-950/25 to-[#0f0707] text-xs font-mono text-red-300 shadow-[inset_0_1px_0_rgba(239,68,68,0.15),0_2px_6px_rgba(0,0,0,0.3)]"
                >
                  <AlertCircle className="h-4 w-4 text-red-400 shrink-0 mt-0.5" />
                  <span className="leading-relaxed">{customError}</span>
                </div>
              )}

              {customModels.length > 0 && (
                <div className="space-y-2.5 p-3.5 rounded-[7px] border-t border-t-zinc-700/60 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-gradient-to-b from-[#111115]/95 via-[#0d0d10]/95 to-[#08080a]/95 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_2px_8px_rgba(0,0,0,0.4)]">
                  <div className="flex items-center justify-between text-xs font-mono">
                    <span className="text-zinc-300 font-medium flex items-center gap-1.5">
                      <CheckCircle2 className="h-3.5 w-3.5 text-amber-400" />
                      Discovered Models ({customModels.length})
                    </span>
                    <span className="text-[10.5px] font-mono text-zinc-400 bg-zinc-900/80 px-2 py-0.5 rounded border border-zinc-800">
                      Click to pre-select
                    </span>
                  </div>
                  <div className="flex flex-wrap gap-1.5 max-h-36 overflow-y-auto pt-1">
                    {customModels.map((m) => {
                      const isSelected = selectedModel === m.id;
                      return (
                        <button
                          key={m.id}
                          type="button"
                          onClick={() => setSelectedModel(m.id)}
                          disabled={isPending}
                          className={cn(
                            "group relative flex items-center gap-1.5 rounded-[5px] px-2.5 py-1 text-xs font-mono transition-all cursor-pointer active:translate-y-[0.5px]",
                            isSelected
                              ? "border-t border-t-amber-400/50 border-x border-x-amber-500/40 border-b border-b-amber-600/50 bg-gradient-to-b from-amber-500/20 via-amber-500/10 to-amber-950/30 text-amber-300 shadow-[inset_0_1px_0_rgba(251,191,36,0.2),0_1px_3px_rgba(0,0,0,0.3)] font-semibold"
                              : "border-t border-t-zinc-700/60 border-x border-x-zinc-800/70 border-b border-b-zinc-900 bg-gradient-to-b from-zinc-850 to-zinc-900/90 text-zinc-300 hover:border-t-zinc-600 hover:text-zinc-100 hover:bg-zinc-800/80 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]"
                          )}
                        >
                          {isSelected ? (
                            <Check className="h-3 w-3 text-amber-400 shrink-0" />
                          ) : (
                            <span className="h-1.5 w-1.5 rounded-full bg-zinc-600 group-hover:bg-amber-400/60 transition-colors" />
                          )}
                          <span>{m.name || m.id}</span>
                          {m.tag && (
                            <span
                              className={cn(
                                "text-[10px] px-1.5 py-0.2 rounded border font-mono",
                                isSelected
                                  ? "border-amber-500/40 bg-amber-500/15 text-amber-300/90"
                                  : "border-zinc-700/50 bg-zinc-800/80 text-zinc-400"
                              )}
                            >
                              {m.tag}
                            </span>
                          )}
                        </button>
                      );
                    })}
                  </div>
                  <p className="text-[11px] font-mono text-zinc-500 pt-0.5">
                    Select a model, then Save Configuration to persist.
                  </p>
                </div>
              )}

              <div className="flex items-center justify-end gap-2.5 pt-3 border-t border-zinc-800/80">
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={handleCloseCustomModal}
                  className="h-8.5 px-3.5 text-xs font-mono rounded-[5px] text-zinc-300 hover:text-white bg-gradient-to-b from-zinc-800/90 via-zinc-850 to-zinc-900/90 border-t border-t-zinc-600/70 border-x border-x-zinc-700/60 border-b border-b-zinc-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.1),0_1px_3px_rgba(0,0,0,0.35)] hover:border-t-zinc-500 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.18),0_2px_6px_rgba(0,0,0,0.4)] active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.4)] cursor-pointer transition-all"
                >
                  Cancel
                </Button>
                <Button
                  type="button"
                  size="sm"
                  onClick={handleValidateCustomProvider}
                  disabled={isPending || isValidatingCustom}
                  className="h-8.5 px-4 text-xs font-mono font-bold rounded-[6px] bg-gradient-to-b from-amber-400 via-amber-450 to-amber-500 hover:from-amber-300 hover:to-amber-400 text-zinc-950 border-t border-t-amber-200/70 border-x border-x-amber-400/80 border-b border-b-amber-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.45),0_2px_8px_rgba(245,158,11,0.35)] active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.4)] transition-all cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {isValidatingCustom ? (
                    <span className="flex items-center gap-2">
                      <RefreshCw className="h-3.5 w-3.5 animate-spin text-zinc-950" />
                      <span>Validating...</span>
                    </span>
                  ) : (
                    <span className="flex items-center gap-1.5">
                      <Server className="h-3.5 w-3.5 text-zinc-950 stroke-[2.2]" />
                      <span>Validate & Discover Models</span>
                    </span>
                  )}
                </Button>
              </div>
            </div>
          </Modal>
        )}
      </div>
    </AppLayout>
  );
}
