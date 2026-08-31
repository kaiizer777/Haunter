export const API_BASE = (process.env.NEXT_PUBLIC_API_URL || "").trim() as string;
if (!API_BASE) {
  throw new Error(
    "NEXT_PUBLIC_API_URL is not set — set it in Cloudflare Pages → Settings → Environment variables to the Lambda function URL (no trailing space)."
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

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(
  endpoint: string,
  options: RequestInit = {}
): Promise<T> {
  const url = `${API_BASE}${endpoint.startsWith("/") ? endpoint : `/${endpoint}`}`;
  
  const headers = new Headers(options.headers || {});
  if (!headers.has("Content-Type") && options.body && typeof options.body === "string") {
    headers.set("Content-Type", "application/json");
  }

  let res: Response;
  try {
    res = await fetch(url, {
      ...options,
      headers,
      credentials: "include",
    });
  } catch (networkErr: unknown) {
    throw new ApiError(
      "Network connection failure. Please verify the backend service is running.",
      0
    );
  }

  if (res.status === 401) {
    if (typeof window !== "undefined" && !window.location.pathname.startsWith("/login")) {
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
        const errJson = await res.json();
        if (typeof errJson.detail === "string" && errJson.detail.length < 120 && !errJson.detail.includes("Traceback")) {
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
  get: <T>(endpoint: string, options?: RequestInit) =>
    request<T>(endpoint, { method: "GET", ...options }),

  post: <T>(endpoint: string, body?: unknown, options?: RequestInit) =>
    request<T>(endpoint, {
      method: "POST",
      body: body !== undefined ? JSON.stringify(body) : undefined,
      ...options,
    }),

  put: <T>(endpoint: string, body?: unknown, options?: RequestInit) =>
    request<T>(endpoint, {
      method: "PUT",
      body: body !== undefined ? JSON.stringify(body) : undefined,
      ...options,
    }),

  delete: <T>(endpoint: string, options?: RequestInit) =>
    request<T>(endpoint, { method: "DELETE", ...options }),

  // Auth endpoints
  getMe: () => api.get<AuthUser>("/auth/me"),
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

  // Eval endpoints (admin-gated on backend)
  getEvalResults: () => api.get<EvalResultOut[]>("/eval-results"),
  getEvalResult: (evalId: string) => api.get<EvalResultOut>(`/eval-results/${evalId}`),
  runEval: (data?: EvalRunRequest) => api.post<EvalResultOut>("/eval/run", data || {}),

  // Model Config endpoints
  getModelConfig: (repoId?: string) =>
    api.get<ModelConfigOut>(`/config/model${repoId ? `?repo_id=${repoId}` : ""}`),
  updateModelConfig: (data: ModelConfigUpdate) =>
    api.put<ModelConfigOut>("/config/model", data),
};
