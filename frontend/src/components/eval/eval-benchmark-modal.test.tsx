import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { EvalBenchmarkModal } from "./eval-benchmark-modal";
import { api } from "@/lib/api";

vi.mock("@/lib/api", () => ({
  api: {
    runEval: vi.fn(),
  },
  ApiError: class ApiError extends Error {
    status: number;
    constructor(message: string, status: number) {
      super(message);
      this.status = status;
    }
  },
}));

describe("eval-benchmark-modal.tsx", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("does not render when isOpen is false", () => {
    const { container } = render(
      <EvalBenchmarkModal isOpen={false} onClose={vi.fn()} onSuccess={vi.fn()} />
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders modal dialog and options when isOpen is true", () => {
    render(
      <EvalBenchmarkModal isOpen={true} onClose={vi.fn()} onSuccess={vi.fn()} />
    );

    expect(
      screen.getByText(/Autonomous Eval Benchmark Runner/i)
    ).toBeInTheDocument();
    expect(screen.getByText(/Canonical Demo Smoke/i)).toBeInTheDocument();
    expect(
      screen.getByText(/Full Golden Fixture Suite \(Dry Run\)/i)
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /trigger benchmark/i })
    ).toBeInTheDocument();
  });

  it("triggers demo benchmark by default and calls onSuccess upon resolution", async () => {
    const onSuccess = vi.fn();
    (api.runEval as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      id: "eval-mock-123",
      overall_accuracy: 1.0,
      passed_fixtures: 1,
      total_fixtures: 1,
    });

    render(
      <EvalBenchmarkModal isOpen={true} onClose={vi.fn()} onSuccess={onSuccess} />
    );

    const triggerBtn = screen.getByRole("button", {
      name: /trigger benchmark/i,
    });
    fireEvent.click(triggerBtn);

    expect(screen.getByText(/Executing Pipeline\.\.\./i)).toBeInTheDocument();

    await waitFor(() => {
      expect(api.runEval).toHaveBeenCalledWith({ demo_mode: true });
      expect(screen.getByText(/Benchmark Completed!/i)).toBeInTheDocument();
      expect(screen.getByText(/Accuracy:/i)).toBeInTheDocument();
      expect(onSuccess).toHaveBeenCalled();
    });
  });

  it("triggers dry run when option is selected", async () => {
    const onSuccess = vi.fn();
    (api.runEval as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      id: "eval-dry-456",
      overall_accuracy: 0.85,
      passed_fixtures: 15,
      total_fixtures: 18,
    });

    render(
      <EvalBenchmarkModal isOpen={true} onClose={vi.fn()} onSuccess={onSuccess} />
    );

    const dryRunOption = screen.getByText(/Full Golden Fixture Suite \(Dry Run\)/i);
    fireEvent.click(dryRunOption);

    const triggerBtn = screen.getByRole("button", {
      name: /trigger benchmark/i,
    });
    fireEvent.click(triggerBtn);

    await waitFor(() => {
      expect(api.runEval).toHaveBeenCalledWith({ dry_run: true });
      expect(screen.getByText(/Benchmark Completed!/i)).toBeInTheDocument();
    });
  });

  it("displays error message if benchmark run fails", async () => {
    (api.runEval as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("Runner timeout")
    );

    render(
      <EvalBenchmarkModal isOpen={true} onClose={vi.fn()} onSuccess={vi.fn()} />
    );

    const triggerBtn = screen.getByRole("button", {
      name: /trigger benchmark/i,
    });
    fireEvent.click(triggerBtn);

    await waitFor(() => {
      expect(screen.getByText(/Benchmark Error/i)).toBeInTheDocument();
      expect(screen.getByText(/Runner timeout/i)).toBeInTheDocument();
    });
  });
});
