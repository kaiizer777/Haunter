import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import RunDetailClient from "./RunDetailClient";
import { api, TraceOut } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

let mockRunId: string | null = "run-uuid-001";

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    replace: vi.fn(),
    push: vi.fn(),
  }),
  usePathname: () => "/runs/detail",
  useSearchParams: () => ({
    get: (key: string) => (key === "id" ? mockRunId : null),
  }),
}));

vi.mock("@/lib/auth-context", () => ({
  useAuth: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    getRunTrace: vi.fn(),
    getModelConfig: vi.fn().mockResolvedValue({
      id: "cfg_1",
      provider: "opencode_zen",
      model_name: "nemotron-3.5-lightning-free",
      base_url: "https://opencode.ai/zen/v1",
      is_active: true,
    }),
  },
}));

describe("RunDetailClient (app/runs/detail/RunDetailClient.tsx)", () => {
  const fullMockTrace: TraceOut = {
    run: {
      id: "run-uuid-001",
      repo_id: "repo-999",
      status: "pr_opened",
      failure_reason: null,
      diagnosis_summary: "AssertionError in tests/test_core.py line 42 due to NoneType return.",
      pr_url: "https://github.com/acme/repo/pull/42",
      pr_number: 42,
      created_at: new Date(Date.now() - 600000).toISOString(),
      updated_at: new Date().toISOString(),
    },
    steps: [
      {
        step_name: "context_gatherer",
        input_tokens: 450,
        output_tokens: 150,
        latency_ms: 800,
        cost_estimate: 0.0015,
        created_at: new Date().toISOString(),
      },
      {
        step_name: "fix_generator",
        input_tokens: 1200,
        output_tokens: 300,
        latency_ms: 2200,
        cost_estimate: 0.004,
        created_at: new Date().toISOString(),
      },
    ],
    attempts: [
      {
        attempt_number: 1,
        confidence_score: 95,
        verification_status: "pass",
        failure_reason: null,
        build_duration_ms: 1800,
        created_at: new Date().toISOString(),
      },
    ],
    total_cost: 0.0055,
    total_latency_ms: 4800,
    failure_classification: null,
  };

  beforeEach(() => {
    vi.clearAllMocks();
    mockRunId = "run-uuid-001";
    vi.mocked(useAuth).mockReturnValue({
      user: {
        id: "usr_1",
        github_id: 111,
        github_username: "solodev",
        avatar_url: null,
        is_admin: false,
      },
      loading: false,
      error: null,
      logout: vi.fn(),
      refresh: vi.fn(),
    });
    vi.mocked(api.getRunTrace).mockResolvedValue(fullMockTrace);
  });

  it("renders a warning when the id query parameter is missing", async () => {
    mockRunId = null;

    render(<RunDetailClient />);

    expect(
      await screen.findByText(/open this page from a run row/i)
    ).toBeInTheDocument();
    expect(api.getRunTrace).not.toHaveBeenCalled();
  });

  it("shows loading skeletons while api.getRunTrace is in flight", () => {
    vi.mocked(api.getRunTrace).mockImplementation(() => new Promise(() => {}));

    const { container } = render(<RunDetailClient />);

    expect(screen.getByRole("heading", { name: "Run Trace & Observability" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /back to runs/i })).toHaveAttribute("href", "/runs");

    const skeletons = container.querySelectorAll(".animate-pulse");
    expect(skeletons.length).toBeGreaterThan(0);
  });

  it("renders not-found state when api.getRunTrace resolves to null", async () => {
    vi.mocked(api.getRunTrace).mockResolvedValue(null as any);

    render(<RunDetailClient />);

    expect(await screen.findByText("Run not found")).toBeInTheDocument();
    expect(
      screen.getByText(/the requested run trace does not exist or you do not have permission to view it\./i)
    ).toBeInTheDocument();
  });

  it("renders error alert banner when api.getRunTrace rejects", async () => {
    vi.mocked(api.getRunTrace).mockRejectedValue(new Error("Failed to fetch run trace timeline from server."));

    render(<RunDetailClient />);

    expect(
      await screen.findByText("Failed to fetch run trace timeline from server.")
    ).toBeInTheDocument();
  });

  it("renders happy path with header, PR link, diagnosis summary, cost breakdown, and trace timeline", async () => {
    render(<RunDetailClient />);

    // Header metadata
    expect(await screen.findByText("Run ID: run-uuid-001")).toBeInTheDocument();
    expect(screen.getByText("PR Opened")).toBeInTheDocument();

    // Pull Request Link
    const prLink = screen.getByRole("link", { name: /view pull request #42/i });
    expect(prLink).toBeInTheDocument();
    expect(prLink).toHaveAttribute("href", "https://github.com/acme/repo/pull/42");
    expect(prLink).toHaveAttribute("target", "_blank");

    // Diagnosis Summary
    expect(screen.getByText("Root Cause Diagnosis")).toBeInTheDocument();
    expect(
      screen.getByText("AssertionError in tests/test_core.py line 42 due to NoneType return.")
    ).toBeInTheDocument();

    // Trace Timeline section & steps
    expect(screen.getByText("Autonomous Execution Timeline")).toBeInTheDocument();
    expect(screen.getByText("context_gatherer")).toBeInTheDocument();
    expect(screen.getByText("fix_generator")).toBeInTheDocument();
    expect(screen.getByText("Attempt #1")).toBeInTheDocument();
    expect(screen.getByText("Sandbox pass")).toBeInTheDocument();

    // Back to runs link
    expect(screen.getByRole("link", { name: /back to runs/i })).toHaveAttribute("href", "/runs");
  });

  it("renders explicit failure reason card when run ended with failure_reason", async () => {
    const errorTrace: TraceOut = {
      ...fullMockTrace,
      run: {
        ...fullMockTrace.run,
        status: "error",
        failure_reason: "MaxAttemptsExhausted: Sandbox build timed out after 3 retries.",
        pr_url: null,
      },
    };
    vi.mocked(api.getRunTrace).mockResolvedValue(errorTrace);

    render(<RunDetailClient />);

    expect(await screen.findByText("Failure Reason")).toBeInTheDocument();
    expect(
      screen.getByText("MaxAttemptsExhausted: Sandbox build timed out after 3 retries.")
    ).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /view pull request/i })).not.toBeInTheDocument();
  });

  it("renders failure classification badge when failure_reason is null", async () => {
    const classifiedTrace: TraceOut = {
      ...fullMockTrace,
      failure_classification: "sandbox_timeout",
      run: {
        ...fullMockTrace.run,
        failure_reason: null,
      },
    };
    vi.mocked(api.getRunTrace).mockResolvedValue(classifiedTrace);

    render(<RunDetailClient />);

    expect(await screen.findByText("sandbox_timeout")).toBeInTheDocument();
  });
});
