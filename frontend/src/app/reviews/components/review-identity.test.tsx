import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import {
  PrPill,
  PushPill,
  IdentityPill,
  CommitShaBadge,
  BranchFlow,
  PrTitle,
} from "./review-identity";

describe("review-identity components", () => {
  let writeTextMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.restoreAllMocks();
    writeTextMock = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: writeTextMock },
      writable: true,
      configurable: true,
    });
  });

  describe("PrPill", () => {
    it("renders PR number with link to GitHub pull request", () => {
      render(
        <PrPill
          prNumber={42}
          repoOwner="acme"
          repoName="sentinel"
        />
      );

      const link = screen.getByRole("link", { name: /PR #42/i });
      expect(link).toHaveAttribute(
        "href",
        "https://github.com/acme/sentinel/pull/42"
      );
      expect(link).toHaveTextContent("PR #42");
    });

    it("renders unlinked badge when no url or repo provided", () => {
      render(<PrPill prNumber={99} />);
      expect(screen.getByText("PR #99")).toBeInTheDocument();
      expect(screen.queryByRole("link")).not.toBeInTheDocument();
    });

    it("renders unlinked badge when repoOwner is missing", () => {
      render(<PrPill prNumber={99} repoName="sentinel" />);
      expect(screen.getByText("PR #99")).toBeInTheDocument();
      expect(screen.queryByRole("link")).not.toBeInTheDocument();
    });
  });

  describe("PushPill", () => {
    it("renders Push commit pill with short SHA and link", () => {
      render(
        <PushPill
          commitSha="d3b07384d113edec49eaa6238ad5ff00"
          repoOwner="acme"
          repoName="sentinel"
        />
      );

      const link = screen.getByRole("link", { name: /Push @ d3b0738/i });
      expect(link).toHaveAttribute(
        "href",
        "https://github.com/acme/sentinel/commit/d3b07384d113edec49eaa6238ad5ff00"
      );
    });

    it("renders unlinked Push pill when repoOwner is missing", () => {
      render(
        <PushPill
          commitSha="d3b07384d113edec49eaa6238ad5ff00"
          repoName="sentinel"
        />
      );

      expect(screen.getByText("Push @ d3b0738")).toBeInTheDocument();
      expect(screen.queryByRole("link")).not.toBeInTheDocument();
    });
  });

  describe("IdentityPill", () => {
    it("renders PrPill when review has pr_number", () => {
      render(
        <IdentityPill
          review={{
            pr_number: 101,
            pr_url: "https://github.com/acme/sentinel/pull/101",
            commit_sha: "abc123456789",
            repo_name: "sentinel",
            repo_owner: "acme",
          }}
        />
      );

      expect(screen.getByText("PR #101")).toBeInTheDocument();
      expect(screen.queryByText(/Push @/i)).not.toBeInTheDocument();
    });

    it("renders PrPill when review has no pr_number but has associatedPr", () => {
      render(
        <IdentityPill
          review={{
            pr_number: null,
            commit_sha: "abc123456789",
            repo_name: "sentinel",
            repo_owner: "acme",
          }}
          associatedPr={{
            number: 202,
            url: "https://github.com/acme/sentinel/pull/202",
            title: "Fix auth deadlock",
          }}
        />
      );

      expect(screen.getByText("PR #202")).toBeInTheDocument();
    });

    it("renders PushPill when neither pr_number nor associatedPr exists", () => {
      render(
        <IdentityPill
          review={{
            pr_number: null,
            commit_sha: "e4d909c290d0fb1ca068ffaddf22cbd0add91713",
            repo_name: "sentinel",
            repo_owner: "acme",
          }}
          associatedPr={null}
        />
      );

      expect(screen.getByText("Push @ e4d909c")).toBeInTheDocument();
    });
  });

  describe("CommitShaBadge", () => {
    it("renders short SHA and copies full SHA on click", async () => {
      render(
        <CommitShaBadge
          commitSha="8f4b238318b637ee91e6f27d41f32943"
          repoOwner="acme"
          repoName="sentinel"
        />
      );

      expect(screen.getByText("8f4b238")).toBeInTheDocument();

      const copyBtn = screen.getByRole("button", {
        name: /Copy full commit SHA/i,
      });
      fireEvent.click(copyBtn);

      expect(writeTextMock).toHaveBeenCalledWith(
        "8f4b238318b637ee91e6f27d41f32943"
      );

      // Tooltip appears
      await waitFor(() => {
        expect(screen.getByRole("status")).toHaveTextContent("Copied SHA!");
      });
    });

    it("displays error alert when clipboard copy fails", async () => {
      writeTextMock.mockRejectedValue(new Error("Clipboard denied"));
      render(
        <CommitShaBadge
          commitSha="8f4b238318b637ee91e6f27d41f32943"
          repoOwner="acme"
          repoName="sentinel"
        />
      );

      const copyBtn = screen.getByRole("button", {
        name: /Copy full commit SHA/i,
      });
      fireEvent.click(copyBtn);

      await waitFor(() => {
        expect(screen.getByRole("alert")).toHaveTextContent("Copy failed");
      });
    });
  });

  describe("BranchFlow", () => {
    it("renders head branch flowing to base branch", () => {
      render(
        <BranchFlow headBranch="feat/oauth2" baseBranch="main" />
      );

      expect(screen.getByText("feat/oauth2")).toBeInTheDocument();
      expect(screen.getByText("main")).toBeInTheDocument();
    });

    it("returns null when no branches provided", () => {
      const { container } = render(<BranchFlow />);
      expect(container.firstChild).toBeNull();
    });
  });

  describe("PrTitle", () => {
    it("renders title with link when prUrl provided", () => {
      render(
        <PrTitle
          title="Refactor telemetry hooks"
          prUrl="https://github.com/acme/sentinel/pull/55"
        />
      );

      const link = screen.getByRole("link", {
        name: "Refactor telemetry hooks",
      });
      expect(link).toHaveAttribute(
        "href",
        "https://github.com/acme/sentinel/pull/55"
      );
    });
  });
});
