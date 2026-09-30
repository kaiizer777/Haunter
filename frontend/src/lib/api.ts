export const API_BASE = (process.env.NEXT_PUBLIC_API_URL || "").trim() as string;
if (!API_BASE) {
  throw new Error(
    "NEXT_PUBLIC_API_URL is not set — set NEXT_PUBLIC_API_URL in frontend/.env.production and re-run `npm run build` to bake it into the static bundle (no trailing space)."
  );
}

export interface AuthUser {
  id: string;
  github_id?: number;
  github_username: string;
  avatar_url: string | null;
  is_admin?: boolean;
}

export interface RepoOut {
  id: string;
  owner: string;
  name: string;
  default_branch: string | null;
  language_hint: string | null;
  active_model_config_id: string | null;
  created_at: string;
}

export interface RepoCreate {
  owner: string;
  name: string;
  default_branch?: string | null;
  language_hint?: string | null;
}

/** Shape returned by GET /github/available-repos */
export interface AvailableRepoOut {
  owner: string;
  name: string;
  full_name: string;
  default_branch: string | null;
  language: string | null;
  private: boolean;
  updated_at: string | null;
  already_connected: boolean;
  permissions_push: boolean;
}

export interface RunOut {
  id: string;
  repo_id: string;
  github_run_id: number;
  github_delivery_id: string | null;
  head_sha: string;
  head_branch: string;
  status: string;
  conclusion: string | null;
  cost?: number;
  tokens?: number;
  created_at: string;
  updated_at: string;
}

export interface RunListOut {
  runs: RunOut[];
  total: number;
}

export interface RunStepOut {
  step_name: string;
  input_tokens: number;
  output_tokens: number;
  latency_ms: number;
  cost_estimate: number;
  created_at: string;
}

export interface AttemptOut {
  attempt_number: number;
  confidence_score: number | null;
  verification_status: string | null;
  failure_reason: string | null;
  build_duration_ms: number | null;
  created_at: string;
  patch_text?: string;
}

export interface RunSummaryOut {
  id: string;
  repo_id: string;
  status: string;
  conclusion?: string | null;
  diagnosis_summary: string | null;
  created_at: string;
  updated_at: string;
  pr_url?: string | null;
  pr_number?: number | null;
  pr_branch?: string | null;
  final_summary?: string | null;
  // Phase 15 — short redacted reason a run ended in error/fallback. Set by the
  // orchestrator on every error path. Null for successful / in-progress runs.
  failure_reason?: string | null;
}

export interface TraceOut {
  run: RunSummaryOut;
  steps: RunStepOut[];
  attempts: AttemptOut[];
  total_cost: number;
  total_latency_ms: number;
  failure_classification: string | null;
}

export interface RepoStatsOut {
  success_rate: number;
  total_runs: number;
  avg_attempts: number;
  avg_cost: number;
  avg_latency_ms: number;
}

export interface ReviewFindingOut {
  file_path: string;
  line_start: number;
  line_end: number;
  category: "security" | "logic" | "performance" | "api_compatibility" | string;
  severity: "low" | "medium" | "high" | "critical" | string;
  critique: string;
  suggested_patch?: string | null;
}

export interface CodeReviewOut {
  id: string;
  repo_id: string;
  repo_owner?: string | null;
  repo_name?: string | null;
  commit_sha: string;
  pr_number?: number | null;
  risk_score: number;
  summary: string;
  findings: ReviewFindingOut[];
  status: string;
  input_tokens: number;
  output_tokens: number;
  created_at: string;
}

export interface CodeReviewListOut {
  reviews: CodeReviewOut[];
  total: number;
}


export interface FixtureScoreItem {
  fixture_id: string;
  context_score: number;
  fix_score: number;
}

export interface EvalResultOut {
  id: string;
  run_id: string | null;
  overall_accuracy: number | null;
  model_config_id: string | null;
  created_at: string;
  context_gatherer_avg: number | null;
  fix_generator_avg: number | null;
  overall_pass_rate: number | null;
  total_fixtures: number | null;
  passed_fixtures: number | null;
  failed_fixtures: number | null;
  mode: string | null;
  provider: string | null;
  model_name: string | null;
  fixture_scores?: FixtureScoreItem[] | null;
}

export interface EvalRunRequest {
  fixture_ids?: string[];
  model_config_id?: string;
  dry_run?: boolean;
  /**
   * Phase 6 — pin the eval to a known-fixable canonical fixture and the
   * default model. Use for demos and CI smoke. When true, dry_run is
   * forced to false server-side so the LLM is exercised end-to-end.
   */
  demo_mode?: boolean;
}

export interface ConfidenceBucket {
  bucket: string;
  total_attempts: number;
  passed_attempts: number;
  accuracy_pct: number;
}

export interface UserEvalMetricsOut {
  total_runs: number;
  healed_runs: number;
  success_rate_pct: number;
  avg_duration_seconds: number;
  total_cost: number;
  avg_cost_per_run: number;
  confidence_calibration: ConfidenceBucket[];
}

export interface ModelConfigOut {
  id: string;
  provider: string;
  model_name: string;
  base_url: string;
  is_active: boolean;
}

export interface ModelConfigUpdate {
  provider: string;
  model_name: string;
  repo_id?: string;
}

export interface AvailableModelItem {
  id: string;
  name: string;
  tag: string;
  context_window?: number;
}

export interface AvailableModelsOut {
  opencode_zen: AvailableModelItem[];
  openai: AvailableModelItem[];
  anthropic: AvailableModelItem[];
  groq?: AvailableModelItem[];
}


// ---------------------------------------------------------------------------
// Session types (Phase 3 -- Cloud Agentic Live Session)
// ---------------------------------------------------------------------------

export interface PlanTask {
  id: string;
  title: string;
  status: "pending" | "in_progress" | "completed" | "failed";
}

export interface WaitingInput {
  question: string;
  options: string[];
  timestamp?: string;
}

export interface CheckpointOut {
  checkpoint_id: string;
  turn: number;
  timestamp: string;
  description: string;
  staged_patches: Record<string, string>;
  history_length: number;
}

export interface SessionOut {
  id: string;
  user_id: string;
  repo_id: string;
  repo_owner: string;
  repo_name: string;
  title: string;
  status: "active" | "completed" | "closed" | "awaiting_clarification";
  branch_name: string;
  base_sha: string;
  conversation_history: Record<string, unknown>[];
  staged_patches: Record<string, string>;
  plan: PlanTask[];
  waiting_input?: WaitingInput | null;
  checkpoints: CheckpointOut[];
  created_at: string;
  updated_at: string;
}

export interface SessionListOut {
  sessions: SessionOut[];
  total: number;
}

export interface SessionCreateIn {
  repo_id: string;
  branch_name?: string | null;
  title?: string | null;
}

export interface SessionCommitIn {
  title: string;
  body?: string | null;
}

export interface SessionCommitOut {
  pr_url: string;
  pr_number: number;
  commit_sha: string;
}

export interface SandboxVerificationOut {
  status: string;
  passed: boolean;
  run_url: string | null;
  logs: string | null;
}

export interface RepoWithSettingsOut {
  repo_id: string;
  repo_full_name: string;
  preset_profile: string;
  features: {
    auto_fixer: boolean;
    auditor_mode: boolean;
    live_sessions: boolean;
    webcontainer_preview: boolean;
    ci_sandbox: boolean;
  };
  audit_triggers: {
    on_pr: boolean;
    on_ci_failure: boolean;
    on_ci_success: boolean;
    on_manual_mention: boolean;
  };
  monitored_branches: string[];
}

export interface RepoSettingsOut {
  id?: string;
  repo_id: string;
  preset: string;
  preset_profile: string;
  enable_auto_fix: boolean;
  enable_auto_fixer?: boolean;
  enable_auditor_mode: boolean;
  enable_sandbox_verification: boolean;
  enable_ci_sandbox?: boolean;
  enable_pr_comments: boolean;
  enable_live_sessions: boolean;
  enable_webcontainer_preview: boolean;
  audit_trigger_on_pr: boolean;
  audit_trigger_on_ci_failure: boolean;
  audit_trigger_on_ci_success: boolean;
  audit_trigger_on_manual_mention: boolean;
  allowed_branches: string[];
  monitored_branches?: string[];
  ignore_draft_prs: boolean;
  max_cost_per_run_cents: number;
  model_override_scope: string;
  settings_version: number;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface RepoSettingsUpdate {
  preset?: string;
  preset_profile?: string;
  enable_auto_fix?: boolean;
  enable_auto_fixer?: boolean;
  enable_auditor_mode?: boolean;
  enable_sandbox_verification?: boolean;
  enable_ci_sandbox?: boolean;
  enable_pr_comments?: boolean;
  enable_live_sessions?: boolean;
  enable_webcontainer_preview?: boolean;
  audit_trigger_on_pr?: boolean;
  audit_trigger_on_ci_failure?: boolean;
  audit_trigger_on_ci_success?: boolean;
  audit_trigger_on_manual_mention?: boolean;
  allowed_branches?: string[];
  monitored_branches?: string[];
  ignore_draft_prs?: boolean;
  max_cost_per_run_cents?: number;
  model_override_scope?: string;
  features?: Record<string, boolean>;
  audit_triggers?: Record<string, boolean>;
}

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/**
 * Machine-readable error codes returned by GET /github/available-repos when
 * the app session is valid but the stored GitHub OAuth grant is unusable.
 * These must NEVER trigger the global 401 → /login redirect below — the
 * user is still authenticated; only the GitHub grant needs a refresh via
 * `${API_BASE}/auth/login`.
 */
export const GITHUB_TOKEN_MISSING = "github_token_missing";
export const GITHUB_TOKEN_INVALID = "github_token_invalid";

export function isGithubTokenError(detail: unknown): boolean {
  if (typeof detail !== "string") return false;
  const d = detail.toLowerCase();
  return (
    d === GITHUB_TOKEN_MISSING ||
    d === GITHUB_TOKEN_INVALID ||
    d.includes("github access token") ||
    d.includes("decrypt github") ||
    d.includes("github token revoked") ||
    d.includes("token not found") ||
    d.includes("token has expired")
  );
}

type RequestOptions = RequestInit & { silent?: boolean };

async function request<T>(
  endpoint: string,
  options: RequestOptions = {}
): Promise<T> {
  const { silent, ...fetchOptions } = options;
  const url = `${API_BASE}${endpoint.startsWith("/") ? endpoint : `/${endpoint}`}`;
  
  const headers = new Headers(fetchOptions.headers || {});
  if (!headers.has("Content-Type") && fetchOptions.body && typeof fetchOptions.body === "string") {
    headers.set("Content-Type", "application/json");
  }

  let res: Response;
  const timeoutSignal =
    typeof AbortSignal !== "undefined" && "timeout" in AbortSignal
      ? AbortSignal.timeout(15000)
      : undefined;
  const signal = fetchOptions.signal ?? timeoutSignal;

  try {
    res = await fetch(url, {
      ...fetchOptions,
      signal,
      headers,
      credentials: "include",
    });
  } catch (err) {
    if (!silent && typeof window !== "undefined") {
      console.error(`[API Network Error] ${fetchOptions.method || "GET"} ${url}:`, err);
    }
    throw new ApiError(
      "Network connection failure. Please verify the backend service is running.",
      0
    );
  }

  if (res.status === 401) {
    // GitHub-grant failures carry a machine-readable detail and must not
    // invalidate the app session: peek at the body before deciding. A true
    // session expiry ("Not authenticated" / "Invalid or expired session")
    // still redirects to /login below.
    // Mixed-deployment compat: legacy backends return 401 with human-readable
    // messages ("GitHub access token not found for user.", ...) instead of
    // the machine-readable github_token_* codes — isGithubTokenError covers
    // both. Additionally, 401s from /github/available-repos NEVER invalidate
    // the app session (the session cookie is valid; only the GitHub grant
    // needs a refresh), so they must never redirect to /login.
    const isAvailableRepos = endpoint.includes("/github/available-repos");
    try {
      const body = (await res.clone().json()) as { detail?: unknown } | null;
      if (body && isGithubTokenError(body.detail)) {
        throw new ApiError(body.detail as string, 401);
      }
      if (isAvailableRepos) {
        const detail =
          body && typeof body.detail === "string" && body.detail
            ? body.detail
            : GITHUB_TOKEN_MISSING;
        throw new ApiError(detail, res.status);
      }
    } catch (err) {
      if (err instanceof ApiError) throw err;
      if (isAvailableRepos) {
        throw new ApiError(GITHUB_TOKEN_MISSING, res.status);
      }
      // Non-JSON 401 body — fall through to the session-expiry redirect.
    }
    if (typeof window !== "undefined" && !window.location.pathname.startsWith("/login")) {
      // eslint-disable-next-line @next/next/no-location-assign-relative-destination
      window.location.href = "/login";
    }
    throw new ApiError("Session expired or unauthorized", 401);
  }

  if (res.status === 204) {
    return {} as T;
  }

  if (!res.ok) {
    let errorDetail = "An unexpected error occurred.";
    if (res.status >= 500) {
      errorDetail = "Internal server error. Please try again later.";
    } else if (res.status === 403) {
      errorDetail = "Access denied. Insufficient permissions.";
    } else if (res.status === 404) {
      errorDetail = "Requested resource was not found.";
    } else if (res.status === 422) {
      errorDetail = "Invalid request payload. Please check your inputs.";
    } else {
      try {
        const errJson = (await res.json()) as { detail?: unknown } | null;
        if (
          errJson &&
          typeof errJson.detail === "string" &&
          errJson.detail.length < 120 &&
          !errJson.detail.includes("Traceback")
        ) {
          errorDetail = errJson.detail;
        } else {
          errorDetail = `Request failed with status ${res.status}`;
        }
      } catch {
        errorDetail = `Request failed with status ${res.status}`;
      }
    }
    throw new ApiError(errorDetail, res.status);
  }

  return res.json();
}

export const api = {
  get: <T>(endpoint: string, options?: RequestOptions) =>
    request<T>(endpoint, { method: "GET", ...options }),

  post: <T>(endpoint: string, body?: unknown, options?: RequestOptions) =>
    request<T>(endpoint, {
      method: "POST",
      body: body !== undefined ? JSON.stringify(body) : undefined,
      ...options,
    }),

  put: <T>(endpoint: string, body?: unknown, options?: RequestOptions) =>
    request<T>(endpoint, {
      method: "PUT",
      body: body !== undefined ? JSON.stringify(body) : undefined,
      ...options,
    }),

  delete: <T>(endpoint: string, options?: RequestOptions) =>
    request<T>(endpoint, { method: "DELETE", ...options }),

  patch: <T>(endpoint: string, body?: unknown, options?: RequestOptions) =>
    request<T>(endpoint, {
      method: "PATCH",
      body: body !== undefined ? JSON.stringify(body) : undefined,
      ...options,
    }),

  // Auth endpoints
  getMe: () => api.get<AuthUser>("/auth/me", { silent: true }),
  logout: () => api.post<{ detail: string }>("/auth/logout"),

  // Repos endpoints
  getRepos: () => api.get<RepoOut[]>("/repos"),
  addRepo: (data: RepoCreate) => api.post<RepoOut>("/repos", data),
  removeRepo: (id: string) => api.delete<void>(`/repos/${id}`),
  getRepoStats: (repoId: string) => api.get<RepoStatsOut>(`/repos/${repoId}/stats`),
  getAvailableRepos: () => api.get<AvailableRepoOut[]>("/github/available-repos"),

  // Runs endpoints
  getRuns: (params?: {
    repo_id?: string;
    status?: string;
    from?: string;
    to?: string;
    limit?: number;
    offset?: number;
  }) => {
    const query = new URLSearchParams();
    if (params?.repo_id) query.append("repo_id", params.repo_id);
    if (params?.status) query.append("status", params.status);
    if (params?.from) query.append("from", params.from);
    if (params?.to) query.append("to", params.to);
    if (params?.limit) query.append("limit", params.limit.toString());
    if (params?.offset !== undefined) query.append("offset", params.offset.toString());
    
    const qs = query.toString();
    return api.get<RunListOut>(`/runs${qs ? `?${qs}` : ""}`);
  },

  getRunTrace: (runId: string) => api.get<TraceOut>(`/runs/${runId}/trace`),
  deleteRun: (runId: string) => api.delete<void>(`/runs/${runId}`),
  deleteRuns: (runIds: string[]) =>
    api.post<{ deleted_count: number }>("/runs/batch-delete", { run_ids: runIds }),

  // Eval endpoints (admin-gated on backend)
  getEvalResults: () => api.get<EvalResultOut[]>("/eval-results"),
  getEvalResult: (evalId: string) => api.get<EvalResultOut>(`/eval-results/${evalId}`),
  runEval: (data?: EvalRunRequest) => api.post<EvalResultOut>("/eval/run", data || {}),
  getUserEvalMetrics: () => api.get<UserEvalMetricsOut>("/eval/user-metrics"),

  // Model Config endpoints
  getAvailableModels: () =>
    api.get<AvailableModelsOut>("/config/model/available"),
  getModelConfig: (repoId?: string, options?: RequestOptions) =>
    api.get<ModelConfigOut>(`/config/model${repoId ? `?repo_id=${repoId}` : ""}`, options),
  updateModelConfig: (data: ModelConfigUpdate) =>
    api.put<ModelConfigOut>("/config/model", data),

  // Code Reviews endpoints
  getCodeReviews: (params?: {
    repo_id?: string;
    limit?: number;
    offset?: number;
    min_risk?: number;
    severity?: string;
  }) => {
    const query = new URLSearchParams();
    if (params?.repo_id) query.append("repo_id", params.repo_id);
    if (params?.limit) query.append("limit", params.limit.toString());
    if (params?.offset !== undefined) query.append("offset", params.offset.toString());
    if (params?.min_risk !== undefined) query.append("min_risk", params.min_risk.toString());
    if (params?.severity) query.append("severity", params.severity);
    const qs = query.toString();
    return api.get<CodeReviewListOut>(`/reviews${qs ? `?${qs}` : ""}`);
  },

  getRepoReviews: (
    repoId: string,
    params?: {
      limit?: number;
      offset?: number;
      min_risk?: number;
      severity?: string;
    }
  ) => {
    const query = new URLSearchParams();
    if (params?.limit) query.append("limit", params.limit.toString());
    if (params?.offset !== undefined) query.append("offset", params.offset.toString());
    if (params?.min_risk !== undefined) query.append("min_risk", params.min_risk.toString());
    if (params?.severity) query.append("severity", params.severity);
    const qs = query.toString();
    return api.get<CodeReviewListOut>(`/repos/${repoId}/reviews${qs ? `?${qs}` : ""}`);
  },

  getReviewDetail: (reviewId: string) =>
    api.get<CodeReviewOut>(`/reviews/${reviewId}`),

  // Session endpoints (Phase 3 -- Cloud Agentic Live Session)
  listSessions: (params?: { status?: string; limit?: number; offset?: number }) => {
    const query = new URLSearchParams();
    if (params?.status) query.append("status", params.status);
    if (params?.limit) query.append("limit", params.limit.toString());
    if (params?.offset !== undefined) query.append("offset", params.offset.toString());
    const qs = query.toString();
    return api.get<SessionListOut>(`/sessions${qs ? `?${qs}` : ""}`);
  },

  getSession: (sessionId: string) =>
    api.get<SessionOut>(`/sessions/${sessionId}`),

  createSession: (data: SessionCreateIn) =>
    api.post<SessionOut>("/sessions", data),

  closeSession: (sessionId: string) =>
    api.post<SessionOut>(`/sessions/${sessionId}/close`, {}),

  verifySession: (sessionId: string) =>
    api.post<SandboxVerificationOut>(`/sessions/${sessionId}/verify`),

  commitSession: (sessionId: string, data: SessionCommitIn) =>
    api.post<SessionCommitOut>(`/sessions/${sessionId}/commit`, data),

  getSessionTree: (sessionId: string) =>
    api.get<string[]>(`/sessions/${sessionId}/tree`),

  clarifySession: (sessionId: string, data: { response: string }) =>
    api.post<SessionOut>(`/sessions/${sessionId}/clarify`, data),

  restoreCheckpoint: (sessionId: string, checkpointId: string) =>
    api.post<SessionOut>(`/sessions/${sessionId}/checkpoints/${checkpointId}/restore`, {}),

  // Settings endpoints (Phase 6.4 - Granular Per-Repo Feature Governance)
  getSettingsRepos: () => api.get<RepoWithSettingsOut[]>("/settings/repos"),
  getRepoSettings: (repoId: string) => api.get<RepoSettingsOut>(`/repos/${repoId}/settings`),
  updateRepoSettings: (repoId: string, data: RepoSettingsUpdate) =>
    api.patch<RepoSettingsOut>(`/repos/${repoId}/settings`, data),
  applyRepoPreset: (repoId: string, presetName: string) =>
    api.post<RepoSettingsOut>(`/repos/${repoId}/settings/preset/${presetName}`, {}),
};

