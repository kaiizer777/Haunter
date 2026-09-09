import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import EvalPage from "./page";
import { api, UserEvalMetricsOut, RunOut, RepoOut } from "@/lib/api";

// Mock next/navigation
vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: vi.fn(),
    replace: vi.fn(),
    pathname: "/eval",
  }),
  usePathname: () => "/eval",
}));

// Mock auth context
vi.mock("@/lib/auth-context", () => ({
  useAuth: () => ({
    user: { id: "u-1", github_username: "kaiizer777", is_admin: true },
    loading: false,
    logout: vi.fn(),
  }),
}));

// Mock API
vi.mock("@/lib/api", () => ({
  api: {
    getUserEvalMetrics: vi.fn(),
    getRuns: vi.fn(),
    getRepos: vi.fn(),
    runEval: vi.fn(),
    getModelConfig: vi.fn().mockResolvedValue({
      id: "cfg-1",
      provider: "opencode_zen",
      model_name: "laguna-8-2.1",
      base_url: "https://opencode.ai/zen/v1",
      is_active: true,
    }),
  },
  ApiError: class ApiError extends Error {
    status: number;
    constructor(message: string, status: number) {
      super(message);
      this.status = status;
    }
  },
}));

const mockMetrics: UserEvalMetricsOut = {
  total_runs: 4,
  healed_runs: 3,
  success_rate_pct: 75.0,
  avg_duration_seconds: 142,
  total_cost: 0.0861,
  avg_cost_per_run: 0.0215,
  confidence_calibration: [
    { bucket: "0-50%", total_attempts: 0, passed_attempts: 0, accuracy_pct: 0 },
    { bucket: "50-75%", total_attempts: 0, passed_attempts: 0, accuracy_pct: 0 },
    { bucket: "75-90%", total_attempts: 0, passed_attempts: 0, accuracy_pct: 0 },
    { bucket: "90-100%", total_attempts: 4, passed_attempts: 3, accuracy_pct: 75.0 },
  ],
};

const mockRuns: RunOut[] = [
  {
    id: "run-1",
    repo_id: "repo-1",
    github_run_id: 101,
    github_delivery_id: "del-1",
    head_sha: "abcdef1",
    head_branch: "main",
    status: "pr_opened",
    conclusion: "success",
    created_at: new Date(Date.now() - 3600000).toISOString(),
    updated_at: new Date(Date.now() - 3400000).toISOString(),
    cost: 0.0228,
  },
  {
    id: "run-2",
    repo_id: "repo-1",
    github_run_id: 102,
    github_delivery_id: "del-2",
    head_sha: "abcdef2",
    head_branch: "main",
    status: "pr_opened",
    conclusion: "success",
    created_at: new Date(Date.now() - 7200000).toISOString(),
    updated_at: new Date(Date.now() - 7000000).toISOString(),
    cost: 0.0213,
  },
];

const mockRepos: RepoOut[] = [
  {
    id: "repo-1",
    owner: "kaiizer777",
    name: "UpGrade",
    default_branch: "main",
    language_hint: "python",
    active_model_config_id: null,
    created_at: new Date().toISOString(),
  },
];

describe("eval/page.tsx", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.getUserEvalMetrics as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(mockMetrics);
    (api.getRuns as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ runs: mockRuns, total: 2 });
    (api.getRepos as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(mockRepos);
  });

  it("renders elevated metric cards with correct values and units", async () => {
    render(<EvalPage />);

    await waitFor(() => {
      expect(screen.getAllByText("75.0%").length).toBeGreaterThanOrEqual(1);
      expect(screen.getByText("3 of 4 healed")).toBeInTheDocument();
      expect(screen.getByText("2m 22s")).toBeInTheDocument();
      expect(screen.getByText("$0.0861")).toBeInTheDocument();
      expect(screen.getByText(/~\$0\.0215 \/ run/i)).toBeInTheDocument();
    });
  });

  it("renders calibration bands view and allows toggling to scatter view", async () => {
    render(<EvalPage />);

    await waitFor(() => {
      expect(screen.getByText("Calibration Bands")).toBeInTheDocument();
      expect(screen.getByText("75.0% Accuracy")).toBeInTheDocument();
      expect(screen.getByText("3 / 4 passed")).toBeInTheDocument();
    });

    // Switch to scatter view
    const scatterTab = screen.getByText(/Fixture Scatter/i);
    fireEvent.click(scatterTab);

    await waitFor(() => {
      expect(
        screen.getByText(/Confidence vs\. Sandbox Outcome Correlation/i)
      ).toBeInTheDocument();
    });
  });

  it("filters recent telemetry table based on search input", async () => {
    render(<EvalPage />);

    await waitFor(() => {
      expect(screen.getAllByText("kaiizer777/UpGrade").length).toBeGreaterThan(0);
    });

    const searchInput = screen.getByPlaceholderText(/Filter by repository or status\.\.\./i);
    fireEvent.change(searchInput, { target: { value: "non-existent-repo" } });

    await waitFor(() => {
      expect(screen.getByText(/No runs matching filter query\./i)).toBeInTheDocument();
    });
  });

  it("opens benchmark runner modal when Run Benchmark is clicked", async () => {
    render(<EvalPage />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Run Benchmark/i })).toBeInTheDocument();
    });

    const runBenchmarkBtn = screen.getByRole("button", { name: /Run Benchmark/i });
    fireEvent.click(runBenchmarkBtn);

    expect(screen.getByText(/Autonomous Eval Benchmark Runner/i)).toBeInTheDocument();
  });
});
