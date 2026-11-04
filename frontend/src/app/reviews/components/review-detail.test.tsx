import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ReviewDetail, renderFormattedSummary } from "./review-detail";
import type { CodeReviewOut } from "../types";

const mockDetailedReview: CodeReviewOut = {
  id: "rev-audit-456",
  repo_id: "repo-1",
  repo_owner: "facebook",
  repo_name: "react",
  commit_sha: "9876543210abcdef9876543210abcdef98765432",
  pr_number: 101,
  pr_title: "Refactor scheduler priority queue",
  pr_author: "gaearon",
  pr_author_avatar: null,
  head_branch: "refactor/scheduler",
  base_branch: "main",
  risk_score: 45,
  summary: "Comprehensive refactoring of fiber priority scheduler algorithm.",
  status: "completed",
  input_tokens: 2400,
  output_tokens: 650,
  created_at: new Date(Date.now() - 120000).toISOString(),
  applied_review_prefs: {
    prefs: {
      review_strictness: "strict",
      review_focus_areas: ["concurrency", "memory"],
    },
    settings_version: 2,
  },
  findings: [
    {
      file_path: "packages/scheduler/src/forks/Scheduler.ts",
      line_start: 120,
      line_end: 125,
      category: "security",
      severity: "high",
      critique: "Potential starvation vulnerability in timer thread.",
      suggested_patch: "@@ -120,3 +120,3 @@\n- timer()\n+ safeTimer()",
      symbol_name: "advanceTimers",
    },
    {
      file_path: "packages/scheduler/src/queue.ts",
      line_start: 34,
      line_end: 40,
      category: "performance",
      severity: "medium",
      critique: "O(n) linear heap scan can be reduced to O(log n).",
      suggested_patch: "@@ -34,2 +34,2 @@\n- scan()\n+ siftDown()",
      symbol_name: "siftDown",
    },
    {
      file_path: "packages/scheduler/src/types.ts",
      line_start: 10,
      line_end: 12,
      category: "holistic",
      severity: "low",
      critique: "Add explicit return type annotation.",
      suggested_patch: null,
      symbol_name: "PriorityLevel",
    },
  ],
};

describe("ReviewDetail component", () => {
  it("renders audit header, decision panel, and blast radius stats", () => {
    const onClose = vi.fn();
    render(<ReviewDetail review={mockDetailedReview} onClose={onClose} />);

    expect(screen.getByText("Haunter Autonomous Audit Report")).toBeInTheDocument();
    expect(screen.getByText("facebook/react")).toBeInTheDocument();
    expect(screen.getByText("PR #101")).toBeInTheDocument();
    expect(screen.getByText("Refactor scheduler priority queue")).toBeInTheDocument();
    expect(screen.getByText("45")).toBeInTheDocument();
    expect(screen.getByText("/100")).toBeInTheDocument();
    expect(screen.getByText("Changes Requested")).toBeInTheDocument();

    // Decision counters
    expect(screen.getAllByText("Must Fix").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Should Fix").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Notes").length).toBeGreaterThanOrEqual(1);

    // Back button
    const backBtn = screen.getByRole("button", { name: /back to reviews/i });
    fireEvent.click(backBtn);
    expect(onClose).toHaveBeenCalled();
  });

  it("renders AppliedPrefsCard when repository preferences are present", () => {
    render(<ReviewDetail review={mockDetailedReview} />);

    expect(screen.getByText("Repository Review Profile")).toBeInTheDocument();
    expect(screen.getByText("Strict")).toBeInTheDocument();
    expect(screen.getByText("concurrency")).toBeInTheDocument();
  });

  it("filters findings by analytical perspective tabs", () => {
    render(<ReviewDetail review={mockDetailedReview} />);

    // Initially all 3 findings appear
    expect(
      screen.getByText("Potential starvation vulnerability in timer thread.")
    ).toBeInTheDocument();
    expect(
      screen.getByText("O(n) linear heap scan can be reduced to O(log n).")
    ).toBeInTheDocument();

    // Click Performance tab
    const perfTab = screen.getByRole("tab", { name: /performance/i });
    fireEvent.click(perfTab);

    expect(
      screen.queryByText("Potential starvation vulnerability in timer thread.")
    ).not.toBeInTheDocument();
    expect(
      screen.getByText("O(n) linear heap scan can be reduced to O(log n).")
    ).toBeInTheDocument();
  });

  it("filters findings by keyword search input", () => {
    render(<ReviewDetail review={mockDetailedReview} />);

    const searchInput = screen.getByPlaceholderText(
      "Search findings by symbol, critique or file..."
    );
    fireEvent.change(searchInput, { target: { value: "starvation" } });

    expect(
      screen.getByText("Potential starvation vulnerability in timer thread.")
    ).toBeInTheDocument();
    expect(
      screen.queryByText("O(n) linear heap scan can be reduced to O(log n).")
    ).not.toBeInTheDocument();
  });

  it("shows clean audit message when review has 0 findings", () => {
    const cleanReview: CodeReviewOut = {
      ...mockDetailedReview,
      id: "rev-clean",
      risk_score: 5,
      findings: [],
    };

    render(<ReviewDetail review={cleanReview} />);

    expect(screen.getByText("No findings recorded")).toBeInTheDocument();
  });

  it("shows high risk score empty findings message when outcome is changes_requested with 0 findings", () => {
    const highRiskEmptyReview: CodeReviewOut = {
      ...mockDetailedReview,
      id: "rev-high-risk-empty",
      risk_score: 85,
      findings: [],
    };

    render(<ReviewDetail review={highRiskEmptyReview} />);

    expect(
      screen.getByText("No inline findings recorded (High Risk Score)")
    ).toBeInTheDocument();
  });

  it("shows suppressed empty findings message when review is suppressed with 0 findings", () => {
    const suppressedReview: CodeReviewOut = {
      ...mockDetailedReview,
      id: "rev-suppressed",
      status: "suppressed",
      risk_score: 20,
      findings: [],
    };

    render(<ReviewDetail review={suppressedReview} />);

    expect(
      screen.getByText("No findings recorded for this review.")
    ).toBeInTheDocument();
  });

  it("renders honest failed review analysis state with retry button", () => {
    const failedReview: CodeReviewOut = {
      ...mockDetailedReview,
      id: "rev-failed",
      status: "error",
      failure_reason: "Sandbox execution limit reached",
      summary: "Sandbox execution limit reached",
      input_tokens: 0,
      output_tokens: 0,
      findings: [],
      risk_score: 0,
    };

    const onRetry = vi.fn();
    render(<ReviewDetail review={failedReview} onRetry={onRetry} />);

    expect(screen.getByText("Sandbox execution limit reached")).toBeInTheDocument();
    const retryBtn = screen.getByRole("button", { name: /retry/i });
    fireEvent.click(retryBtn);
    expect(onRetry).toHaveBeenCalled();
  });

  it("renders honest pending review analysis state", () => {
    const pendingReview: CodeReviewOut = {
      ...mockDetailedReview,
      id: "rev-pending",
      status: "pending",
      summary: "Autonomous code review queued.",
      findings: [],
    };

    render(<ReviewDetail review={pendingReview} />);

    expect(screen.getAllByText("Queued").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("Autonomous code review queued.")).toBeInTheDocument();
  });

  describe("renderFormattedSummary", () => {
    it("returns null for empty or null text", () => {
      expect(renderFormattedSummary(null)).toBeNull();
      expect(renderFormattedSummary("")).toBeNull();
    });

    it("formats CLI flags with leading double and single hyphens as code chips", () => {
      const { container } = render(
        <div>{renderFormattedSummary("Run with --repo kaiizer/Haunter and -f flag")}</div>
      );

      const codeElements = container.querySelectorAll("code");
      expect(codeElements).toHaveLength(2);
      expect(codeElements[0]).toHaveTextContent("--repo");
      expect(codeElements[1]).toHaveTextContent("-f");
    });

    it("formats flags at start of text and within parentheses", () => {
      const { container } = render(
        <div>{renderFormattedSummary("--repo is primary; options (--force, -v) supported")}</div>
      );

      const codeElements = container.querySelectorAll("code");
      expect(codeElements).toHaveLength(3);
      expect(codeElements[0]).toHaveTextContent("--repo");
      expect(codeElements[1]).toHaveTextContent("--force");
      expect(codeElements[2]).toHaveTextContent("-v");
    });

    it("does not split hyphenated English words", () => {
      const { container } = render(
        <div>{renderFormattedSummary("This is a built-in, user-facing feature with high-priority tasks")}</div>
      );

      const codeElements = container.querySelectorAll("code");
      expect(codeElements).toHaveLength(0);
      expect(container).toHaveTextContent(
        "This is a built-in, user-facing feature with high-priority tasks"
      );
    });

    it("formats backticks, file paths, and environment variables", () => {
      const { container } = render(
        <div>
          {renderFormattedSummary("Inspect `config.get()` in src/app/reviews/page.tsx with API_SECRET")}
        </div>
      );

      const codeElements = container.querySelectorAll("code");
      expect(codeElements).toHaveLength(3);
      expect(codeElements[0]).toHaveTextContent("config.get()");
      expect(codeElements[1]).toHaveTextContent("src/app/reviews/page.tsx");
      expect(codeElements[2]).toHaveTextContent("API_SECRET");
    });
  });
});
