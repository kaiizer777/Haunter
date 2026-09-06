import React from "react";
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { TraceTimeline } from "./trace-timeline";
import { TraceOut } from "@/lib/api";

describe("trace-timeline.tsx", () => {
  const mockTrace: TraceOut = {
    run: {
      id: "run_test_001",
      repo_id: "repo_test",
      status: "completed",
      diagnosis_summary: "Diagnosis test summary",
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    },
    steps: [
      {
        step_name: "context_gatherer",
        input_tokens: 500,
        output_tokens: 250,
        latency_ms: 1200,
        cost_estimate: 0.002,
        created_at: new Date().toISOString(),
      },
      {
        step_name: "fix_generator",
        input_tokens: 1500,
        output_tokens: 600,
        latency_ms: 2400,
        cost_estimate: 0.006,
        created_at: new Date().toISOString(),
      },
      {
        step_name: "sandbox_verifier",
        input_tokens: 200,
        output_tokens: 100,
        latency_ms: 5000,
        cost_estimate: 0.001,
        created_at: new Date().toISOString(),
      },
      {
        step_name: "pr_writer",
        input_tokens: 800,
        output_tokens: 400,
        latency_ms: 1800,
        cost_estimate: 0.0035,
        created_at: new Date().toISOString(),
      },
      {
        step_name: "custom_fallback_step",
        input_tokens: 50,
        output_tokens: 20,
        latency_ms: 300,
        cost_estimate: 0.0001,
        created_at: new Date().toISOString(),
      },
    ],
    attempts: [
      {
        attempt_number: 1,
        confidence_score: 45,
        verification_status: "fail",
        failure_reason: "AssertionError: Expected 200 but got 500",
        build_duration_ms: 3200,
        created_at: new Date().toISOString(),
        patch_text: "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n-print('broken')\n+print('fixed')",
      },
      {
        attempt_number: 2,
        confidence_score: 92,
        verification_status: "pass",
        failure_reason: null,
        build_duration_ms: 2800,
        created_at: new Date().toISOString(),
        patch_text: undefined,
      },
    ],
    total_cost: 0.0126,
    total_latency_ms: 10700,
    failure_classification: null,
  };

  it("renders all pipeline steps with tokens, latency, and cost", () => {
    render(<TraceTimeline trace={mockTrace} />);

    expect(screen.getByText("context_gatherer")).toBeInTheDocument();
    expect(screen.getByText("fix_generator")).toBeInTheDocument();
    expect(screen.getByText("sandbox_verifier")).toBeInTheDocument();
    expect(screen.getByText("pr_writer")).toBeInTheDocument();
    expect(screen.getByText("custom_fallback_step")).toBeInTheDocument();

    // Check tokens display
    expect(screen.getByText("500")).toBeInTheDocument();
    expect(screen.getByText("250")).toBeInTheDocument();

    // Check latency format
    expect(screen.getByText("1.20s")).toBeInTheDocument();
    expect(screen.getByText("2.40s")).toBeInTheDocument();
  });

  it("renders attempts with verification status, confidence score, and build latency", () => {
    render(<TraceTimeline trace={mockTrace} />);

    expect(screen.getByText("Attempt #1")).toBeInTheDocument();
    expect(screen.getByText("Sandbox fail")).toBeInTheDocument();
    expect(screen.getByText("3.20s")).toBeInTheDocument();

    expect(screen.getByText("Attempt #2")).toBeInTheDocument();
    expect(screen.getByText("Sandbox pass")).toBeInTheDocument();
    expect(screen.getByText("2.80s")).toBeInTheDocument();

    // Confidence bars
    expect(screen.getByText("45%")).toBeInTheDocument();
    expect(screen.getByText("92%")).toBeInTheDocument();
  });

  it("renders failure reason and patch diff when present", () => {
    render(<TraceTimeline trace={mockTrace} />);

    // Attempt 1 has failure reason
    expect(screen.getByText("Failure Reason")).toBeInTheDocument();
    expect(
      screen.getByText("AssertionError: Expected 200 but got 500")
    ).toBeInTheDocument();

    // Attempt 1 has patch diff
    expect(
      screen.getByText("Generated Patch (Unified Diff)")
    ).toBeInTheDocument();
    expect(screen.getByText(/--- a\/app\.py/)).toBeInTheDocument();
  });

  it("does not render failure reason or patch box when absent in attempt", () => {
    const cleanAttemptTrace: TraceOut = {
      ...mockTrace,
      steps: [],
      attempts: [
        {
          attempt_number: 1,
          confidence_score: null,
          verification_status: "completed",
          failure_reason: null,
          build_duration_ms: null,
          created_at: new Date().toISOString(),
        },
      ],
    };

    render(<TraceTimeline trace={cleanAttemptTrace} />);
    expect(screen.queryByText("Failure Reason")).not.toBeInTheDocument();
    expect(
      screen.queryByText("Generated Patch (Unified Diff)")
    ).not.toBeInTheDocument();
    expect(screen.queryByText("Confidence:")).not.toBeInTheDocument();
  });

  it("renders warning badge for non-pass/non-fail verification statuses", () => {
    const pendingAttemptTrace: TraceOut = {
      ...mockTrace,
      steps: [],
      attempts: [
        {
          attempt_number: 1,
          confidence_score: 50,
          verification_status: "running",
          failure_reason: null,
          build_duration_ms: 1000,
          created_at: new Date().toISOString(),
        },
      ],
    };

    const { container } = render(<TraceTimeline trace={pendingAttemptTrace} />);
    expect(screen.getByText("Sandbox running")).toBeInTheDocument();
    // Warning variant has amber dot
    expect(container.querySelector(".bg-amber-400")).toBeInTheDocument();
  });
});
