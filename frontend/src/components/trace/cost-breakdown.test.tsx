import React from "react";
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { CostBreakdown } from "./cost-breakdown";
import { TraceOut } from "@/lib/api";

describe("cost-breakdown.tsx", () => {
  const mockTrace: TraceOut = {
    run: {
      id: "run_123",
      repo_id: "repo_abc",
      status: "completed",
      diagnosis_summary: "Fixed syntax error",
      created_at: "2026-03-01T12:00:00Z",
      updated_at: "2026-03-01T12:01:00Z",
    },
    steps: [
      {
        step_name: "context_gatherer",
        input_tokens: 1200,
        output_tokens: 300,
        latency_ms: 1500,
        cost_estimate: 0.0045,
        created_at: "2026-03-01T12:00:10Z",
      },
      {
        step_name: "fix_generator",
        input_tokens: 2500,
        output_tokens: 1000,
        latency_ms: 3500,
        cost_estimate: 0.0105,
        created_at: "2026-03-01T12:00:20Z",
      },
    ],
    attempts: [
      {
        attempt_number: 1,
        confidence_score: 90,
        verification_status: "pass",
        failure_reason: null,
        build_duration_ms: 4500,
        created_at: "2026-03-01T12:00:30Z",
      },
    ],
    total_cost: 0.015,
    total_latency_ms: 9500,
    failure_classification: null,
  };

  it("renders all 4 metric cards with formatted values", () => {
    render(<CostBreakdown trace={mockTrace} />);

    // Total Cost
    expect(screen.getByText("Total Cost")).toBeInTheDocument();
    expect(screen.getByText("$0.0150")).toBeInTheDocument();

    // Total Latency
    expect(screen.getByText("Total Latency")).toBeInTheDocument();
    expect(screen.getByText("9.50s")).toBeInTheDocument();

    // Total Tokens: (1200+300) + (2500+1000) = 5000
    expect(screen.getByText("Total Tokens")).toBeInTheDocument();
    expect(screen.getByText("5,000")).toBeInTheDocument();

    // Attempts: 1
    expect(screen.getByText("Attempts")).toBeInTheDocument();
    expect(screen.getByText("1")).toBeInTheDocument();
  });

  it("handles zero values and empty steps/attempts gracefully", () => {
    const emptyTrace: TraceOut = {
      run: {
        id: "run_empty",
        repo_id: "repo_empty",
        status: "pending",
        diagnosis_summary: null,
        created_at: "2026-03-01T12:00:00Z",
        updated_at: "2026-03-01T12:00:00Z",
      },
      steps: [],
      attempts: [],
      total_cost: 0,
      total_latency_ms: 0,
      failure_classification: null,
    };

    render(<CostBreakdown trace={emptyTrace} />);

    expect(screen.getByText("$0.00")).toBeInTheDocument();
    expect(screen.getByText("0ms")).toBeInTheDocument();

    const tokensCard = screen.getByText("Total Tokens").parentElement?.parentElement;
    expect(tokensCard).toHaveTextContent("0");

    const attemptsCard = screen.getByText("Attempts").parentElement?.parentElement;
    expect(attemptsCard).toHaveTextContent("0");
  });

  it("handles undefined or null token values in steps", () => {
    const traceWithNullTokens: TraceOut = {
      ...mockTrace,
      steps: [
        {
          step_name: "partial_step",
          input_tokens: undefined as any,
          output_tokens: null as any,
          latency_ms: 500,
          cost_estimate: 0,
          created_at: "2026-03-01T12:00:00Z",
        },
      ],
    };

    render(<CostBreakdown trace={traceWithNullTokens} />);
    const tokensCard = screen.getByText("Total Tokens").parentElement?.parentElement;
    expect(tokensCard).toHaveTextContent("0");
  });
});
