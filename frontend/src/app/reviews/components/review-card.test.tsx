import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import {
  ReviewCard,
} from "./review-card";
import type { CodeReviewOut } from "../types";

const mockReview: CodeReviewOut = {
  id: "rev-123",
  repo_id: "repo-1",
  repo_owner: "facebook",
  repo_name: "react",
  commit_sha: "abcdef1234567890abcdef1234567890abcdef12",
  pr_number: 42,
  pr_title: "Fix concurrent hydration mismatch",
  pr_author: "acmarke",
  pr_author_avatar: null,
  head_branch: "fix/hydration",
  base_branch: "main",
  risk_score: 80,
  summary: "Critical hydration flaw detected in client reconciliation loop.",
  status: "completed",
  input_tokens: 1200,
  output_tokens: 450,
  created_at: new Date(Date.now() - 60000).toISOString(),
  findings: [
    {
      file_path: "packages/react-dom/src/client.ts",
      line_start: 45,
      line_end: 50,
      category: "correctness",
      severity: "critical",
      critique: "State mismatch leads to infinite rerender.",
      suggested_patch: "@@ -45,2 +45,2 @@\n- oldCode\n+ newCode",
    },
  ],
};

describe("ReviewCard component", () => {
  it("renders a scored completed review with confidence gauge and telemetry facts", () => {
    const onOpen = vi.fn();
    render(<ReviewCard review={mockReview} onOpen={onOpen} />);

    expect(screen.getByText("facebook/react")).toBeInTheDocument();
    expect(screen.getByText("PR #42")).toBeInTheDocument();
    expect(screen.getAllByText("Changes Requested").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("Fix concurrent hydration mismatch")).toBeInTheDocument();
    expect(screen.getByText("1 must fix")).toBeInTheDocument();
    expect(screen.getByText("80/100")).toBeInTheDocument();
    expect(screen.getByText("1650 tok")).toBeInTheDocument();

    const inspectBtn = screen.getByRole("button", { name: /inspect audit/i });
    fireEvent.click(inspectBtn);
    expect(onOpen).toHaveBeenCalledWith("rev-123");
  });

  it("toggles inline findings accordion when findings button is clicked", () => {
    render(<ReviewCard review={mockReview} onOpen={vi.fn()} />);

    expect(screen.queryByText("Inline Findings (1)")).not.toBeInTheDocument();

    const toggleBtn = screen.getByRole("button", { name: /findings \(1\)/i });
    fireEvent.click(toggleBtn);

    expect(screen.getByText("Inline Findings (1)")).toBeInTheDocument();
    expect(screen.getByText("State mismatch leads to infinite rerender.")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /collapse/i }));
    expect(screen.queryByText("Inline Findings (1)")).not.toBeInTheDocument();
  });

  it("routes to FailedReviewCard when review status is error", () => {
    const failedReview: CodeReviewOut = {
      ...mockReview,
      id: "rev-failed",
      status: "error",
      failure_reason: "Model timeout exceeded during AST parsing",
      summary: "Model timeout exceeded during AST parsing",
      input_tokens: 0,
      output_tokens: 0,
      findings: [],
      risk_score: 0,
    };

    const onRetry = vi.fn();
    render(
      <ReviewCard review={failedReview} onOpen={vi.fn()} onRetry={onRetry} />
    );

    expect(screen.getByTestId("failed-review-card")).toBeInTheDocument();
    expect(screen.getByText("Analysis failed")).toBeInTheDocument();
    expect(
      screen.getByText("Model timeout exceeded during AST parsing")
    ).toBeInTheDocument();

    const retryBtn = screen.getByRole("button", { name: /retry/i });
    fireEvent.click(retryBtn);
    expect(onRetry).toHaveBeenCalled();
  });

  it("routes to PendingReviewCard when review status is pending", () => {
    const pendingReview: CodeReviewOut = {
      ...mockReview,
      id: "rev-pending",
      status: "pending",
      summary: "Autonomous code review queued.",
      input_tokens: 0,
      output_tokens: 0,
      findings: [],
      risk_score: 0,
    };

    const onOpen = vi.fn();
    render(<ReviewCard review={pendingReview} onOpen={onOpen} />);

    expect(screen.getByTestId("pending-review-card")).toBeInTheDocument();
    expect(screen.getAllByText("Queued").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("Autonomous code review queued.")).toBeInTheDocument();

    const auditBtn = screen.getByRole("button", { name: /full audit/i });
    fireEvent.click(auditBtn);
    expect(onOpen).toHaveBeenCalledWith("rev-pending");
  });

  it("handles publication failed error badge when analysis succeeded but publish failed", () => {
    const pubFailedReview: CodeReviewOut = {
      ...mockReview,
      id: "rev-pub-fail",
      status: "error",
      failure_reason: "Failed to publish PR review: Bad credentials",
    };

    render(<ReviewCard review={pubFailedReview} onOpen={vi.fn()} />);

    expect(screen.getByText("Publication failed")).toBeInTheDocument();
  });
});
