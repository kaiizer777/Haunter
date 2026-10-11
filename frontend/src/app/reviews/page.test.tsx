import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ReviewsPage from "./page";
import { api, CodeReviewOut, RepoOut } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    replace: vi.fn(),
    push: vi.fn(),
  }),
  usePathname: () => "/reviews",
}));

vi.mock("@/lib/auth-context", () => ({
  useAuth: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    getRepos: vi.fn(),
    getCodeReviews: vi.fn(),
    getReviewDetail: vi.fn(),
    getModelConfig: vi.fn().mockResolvedValue({
      id: "cfg_1",
      provider: "opencode_zen",
      model_name: "nemotron-3.5-lightning-free",
      base_url: "https://opencode.ai/zen/v1",
      is_active: true,
    }),
  },
}));

describe("ReviewsPage (app/reviews/page.tsx)", () => {
  const mockRepos: RepoOut[] = [
    {
      id: "repo_1",
      owner: "acme",
      name: "sentinel",
      default_branch: "main",
      language_hint: "typescript",
      active_model_config_id: null,
      created_at: new Date().toISOString(),
    },
  ];

  const mockReviews: CodeReviewOut[] = [
    {
      id: "rev_1",
      repo_id: "repo_1",
      repo_owner: "acme",
      repo_name: "sentinel",
      commit_sha: "abcdef1234567890",
      pr_number: 101,
      pr_title: "feat: autonomous remediation sentinel pipeline",
      pr_author: "octocat",
      pr_author_avatar: "https://avatars.githubusercontent.com/u/583231",
      head_branch: "feat/sentinel",
      base_branch: "main",
      pr_url: "https://github.com/acme/sentinel/pull/101",
      risk_score: 85,
      summary: "Critical security vulnerability detected in token parser middleware.",
      status: "completed",
      input_tokens: 1200,
      output_tokens: 350,
      created_at: new Date(Date.now() - 3600000).toISOString(),
      diff_stats: {
        additions: 45,
        deletions: 12,
        files_changed: 4,
      },
      findings: [
        {
          file_path: "src/auth/jwt.ts",
          line_start: 42,
          line_end: 48,
          category: "security",
          severity: "critical",
          critique: "Algorithm none accepted in JWT decode function allowing signature bypass.",
          symbol_name: "verifyToken",
          ast_type: "FunctionDeclaration",
          suggested_patch: "@@ -42,7 +42,7 @@\n- const decoded = jwt.decode(rawToken, { verify: false });\n+ const decoded = jwt.verify(rawToken, secret, { algorithms: ['HS256'] });",
        },
      ],
    },
    {
      id: "rev_2",
      repo_id: "repo_1",
      repo_owner: "acme",
      repo_name: "sentinel",
      commit_sha: "1234567890abcdef",
      pr_number: 102,
      pr_title: "docs: update API documentation",
      pr_author: "dev-dan",
      pr_author_avatar: null,
      head_branch: "docs/api",
      base_branch: "main",
      pr_url: "https://github.com/acme/sentinel/pull/102",
      risk_score: 15,
      summary: "Documentation changes with zero security or logic regressions.",
      status: "completed",
      input_tokens: 600,
      output_tokens: 120,
      created_at: new Date(Date.now() - 7200000).toISOString(),
      diff_stats: {
        additions: 10,
        deletions: 2,
        files_changed: 1,
      },
      findings: [],
    },
  ];

  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    window.history.replaceState({}, "", "/reviews");
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
    vi.mocked(api.getRepos).mockResolvedValue(mockRepos);
    vi.mocked(api.getCodeReviews).mockResolvedValue({
      reviews: mockReviews,
      total: mockReviews.length,
    });
    vi.mocked(api.getReviewDetail).mockImplementation((id: string) => {
      const found = mockReviews.find((r) => r.id === id);
      return found ? Promise.resolve(found) : Promise.reject(new Error("not found"));
    });

    const writeTextMock = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: {
        writeText: writeTextMock,
      },
      configurable: true,
      writable: true,
    });
  });

  it("renders PR identity correctly: repo name, PR badge, PR title, author, branch flow, and blast radius", async () => {
    render(<ReviewsPage />);

    // Verify Repository link
    const repoLinks = await screen.findAllByText("acme/sentinel");
    expect(repoLinks.length).toBeGreaterThanOrEqual(1);

    // Verify PR Badge
    expect(screen.getByText("PR #101")).toBeInTheDocument();

    // Verify PR Title
    expect(screen.getByText("feat: autonomous remediation sentinel pipeline")).toBeInTheDocument();

    // Verify Author chip
    expect(screen.getByText("@octocat")).toBeInTheDocument();

    // Verify Branch flow
    expect(screen.getByText("feat/sentinel")).toBeInTheDocument();

    // Verify Blast Radius Telemetry strip
    expect(screen.getByText("4 files")).toBeInTheDocument();
    expect(screen.getByText("+45")).toBeInTheDocument();
    expect(screen.getByText("-12")).toBeInTheDocument();
    expect(screen.getByText("Extensive")).toBeInTheDocument();
  });

  it("renders syntax-highlighted diff viewer and actionable finding buttons", async () => {
    const user = userEvent.setup();
    render(<ReviewsPage />);

    // Wait for card to appear and expand the findings accordion
    const reviewButton = await screen.findByRole("button", { name: /review \(1\)/i });
    await user.click(reviewButton);

    // Verify findings section header
    expect(screen.getByText("Actionable Findings (1)")).toBeInTheDocument();

    // Verify direct file link to GitHub lines
    const fileLink = screen.getByRole("link", { name: /src\/auth\/jwt\.ts:42-48/i });
    expect(fileLink).toHaveAttribute(
      "href",
      "https://github.com/acme/sentinel/blob/abcdef1234567890/src/auth/jwt.ts#L42-L48"
    );

    // Verify syntax-highlighted diff lines
    expect(screen.getByText(/@@ -42,7 \+42,7 @@/)).toBeInTheDocument();
    expect(screen.getByText(/- const decoded = jwt\.decode/)).toBeInTheDocument();
    expect(screen.getByText(/\+ const decoded = jwt\.verify/)).toBeInTheDocument();

    // Verify dual tactile action buttons: Copy Patch & Stage Fix
    const copyButton = screen.getByRole("button", { name: /copy patch/i });
    const stageButton = screen.getByRole("button", { name: /stage fix/i });

    expect(copyButton).toBeInTheDocument();
    expect(stageButton).toBeInTheDocument();

    // Test Copy Patch
    await user.click(copyButton);
    expect(await screen.findByText("Copied")).toBeInTheDocument();

    // Test Stage Fix
    await user.click(stageButton);
    expect(await screen.findByText("Staged Fix")).toBeInTheDocument();
  });

  it("filters reviews using the interactive Telemetry Ribbon tabs", async () => {
    const user = userEvent.setup();
    render(<ReviewsPage />);

    expect(await screen.findByText("feat: autonomous remediation sentinel pipeline")).toBeInTheDocument();
    expect(screen.getByText("docs: update API documentation")).toBeInTheDocument();

    // Click 'Patches' ribbon tab (only rev_1 has patches)
    const patchesTab = screen.getByRole("button", { name: /patches/i });
    await user.click(patchesTab);

    // rev_1 should remain visible, rev_2 should be filtered out
    expect(screen.getByText("feat: autonomous remediation sentinel pipeline")).toBeInTheDocument();
    expect(screen.queryByText("docs: update API documentation")).not.toBeInTheDocument();

    // Click 'Scanned' tab to restore
    const allTab = screen.getByRole("button", { name: /scanned/i });
    await user.click(allTab);

    expect(screen.getByText("feat: autonomous remediation sentinel pipeline")).toBeInTheDocument();
    expect(screen.getByText("docs: update API documentation")).toBeInTheDocument();
  });

  it("stage fix persists locally and never touches the clipboard (distinct from copy)", async () => {
    // NB: fireEvent, not userEvent — userEvent.setup() replaces
    // navigator.clipboard with its own stub, hiding the beforeEach mock.
    render(<ReviewsPage />);

    const reviewButton = await screen.findByRole("button", { name: /review \(1\)/i });
    fireEvent.click(reviewButton);

    const clipboardWriteText = navigator.clipboard.writeText as unknown as ReturnType<typeof vi.fn>;

    // Stage: persists locally, does NOT copy
    fireEvent.click(screen.getByRole("button", { name: /stage fix/i }));
    expect(screen.getByText("Staged Fix")).toBeInTheDocument();
    expect(clipboardWriteText).not.toHaveBeenCalled();
    expect(localStorage.getItem("haunter-staged-fixes")).toContain("src/auth/jwt.ts");

    // Copy: writes clipboard, does NOT stage anything new
    fireEvent.click(screen.getByRole("button", { name: /copy patch/i }));
    expect(clipboardWriteText).toHaveBeenCalledWith(expect.stringContaining("jwt.verify"));
    expect(await screen.findByText("Copied")).toBeInTheDocument();

    // Toggle off: unstages and removes the persisted entry
    fireEvent.click(screen.getByRole("button", { name: /staged fix/i }));
    expect(screen.getByText("Stage Fix")).toBeInTheDocument();
    expect(localStorage.getItem("haunter-staged-fixes")).not.toContain("src/auth/jwt.ts");
  });

  it("surfaces clipboard errors instead of failing silently", async () => {
    // NB: fireEvent, not userEvent (see above).
    (navigator.clipboard.writeText as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("denied")
    );
    render(<ReviewsPage />);

    const reviewButton = await screen.findByRole("button", { name: /review \(1\)/i });
    fireEvent.click(reviewButton);

    fireEvent.click(screen.getByRole("button", { name: /copy patch/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/copy failed/i);
  });

  it("shows fetch error banner with retry when the reviews request fails", async () => {
    const user = userEvent.setup();
    vi.mocked(api.getCodeReviews).mockRejectedValueOnce(new Error("network down"));
    render(<ReviewsPage />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/couldn't load code reviews/i);

    // Retry re-fires the request (beforeEach mock resolves on subsequent calls)
    await user.click(screen.getByRole("button", { name: /retry/i }));
    expect(await screen.findByText("feat: autonomous remediation sentinel pipeline")).toBeInTheDocument();
  });

  it("keeps ribbon stats page-scoped while Reviews Scanned shows the server total", async () => {    // Server reports 28 total but only a 2-item page is delivered: the tiles
    // must not disagree silently — page-derived tiles say "this page".
    vi.mocked(api.getCodeReviews).mockResolvedValue({ reviews: mockReviews, total: 28 });
    render(<ReviewsPage />);

    expect(await screen.findByText("feat: autonomous remediation sentinel pipeline")).toBeInTheDocument();

    const scanned = screen.getByText("Scanned").closest("button")!;
    expect(scanned).toHaveTextContent("28");

    const highRisk = screen.getByText(/high-risk ≥ 71/i).closest("button")!;
    expect(highRisk).toHaveTextContent("1");
    expect(highRisk.textContent).toMatch(/this page/i);

    expect(screen.getByText(/mean score · this page/i)).toBeInTheDocument();
    expect(screen.getByText(/with suggested fix · this page/i)).toBeInTheDocument();
  });

  it("overview rows show confidence and open a full audit detail view", async () => {
    const user = userEvent.setup();
    render(<ReviewsPage />);

    expect(await screen.findByText("feat: autonomous remediation sentinel pipeline")).toBeInTheDocument();
    // Confidence derived from risk: 85 -> 2/10, 15 -> 9/10
    expect(screen.getByText("2/10")).toBeInTheDocument();
    expect(screen.getByText("9/10")).toBeInTheDocument();

    await user.click(screen.getAllByRole("button", { name: /full audit/i })[0]);

    expect(await screen.findByText("PR Summary")).toBeInTheDocument();
    expect(screen.getByText("PR Confidence")).toBeInTheDocument();
    expect(screen.getByText("Blockers")).toBeInTheDocument();
    expect(screen.getByText(/structural & blast radius analysis/i)).toBeInTheDocument();
    expect(screen.getByText("Findings Summary (1)")).toBeInTheDocument();
    expect(screen.getByText("Changes Requested")).toBeInTheDocument();
    expect(window.location.search).toContain("review=rev_1");
    // Overview list is replaced by the report
    expect(screen.queryByText("docs: update API documentation")).not.toBeInTheDocument();
  });

  it("back button and Escape return to the overview", async () => {
    const user = userEvent.setup();
    render(<ReviewsPage />);

    await screen.findByText("feat: autonomous remediation sentinel pipeline");
    await user.click(screen.getAllByRole("button", { name: /full audit/i })[0]);
    expect(await screen.findByText("PR Summary")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /back to reviews/i }));
    expect(await screen.findByText("docs: update API documentation")).toBeInTheDocument();
    expect(window.location.search).not.toContain("review=");

    await user.click(screen.getAllByRole("button", { name: /full audit/i })[0]);
    expect(await screen.findByText("PR Summary")).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(await screen.findByText("docs: update API documentation")).toBeInTheDocument();
  });

  it("deep-link ?review= renders the detail report directly", async () => {
    window.history.replaceState({}, "", "/reviews?review=rev_2");
    render(<ReviewsPage />);

    expect(await screen.findByText("PR Summary")).toBeInTheDocument();
    expect(screen.getByText("Great to Merge")).toBeInTheDocument();
    expect(screen.getByText("9/10")).toBeInTheDocument();
    expect(screen.getByText(/no regressions detected/i)).toBeInTheDocument();
  });

  it("staged fixes stay visible in the detail view", async () => {
    // NB: fireEvent, not userEvent — userEvent.setup() replaces
    // navigator.clipboard with its own stub, hiding the beforeEach mock.
    render(<ReviewsPage />);

    const reviewButton = await screen.findByRole("button", { name: /review \(1\)/i });
    fireEvent.click(reviewButton);
    fireEvent.click(screen.getByRole("button", { name: /stage fix/i }));
    expect(screen.getByText("Staged Fix")).toBeInTheDocument();

    fireEvent.click(screen.getAllByRole("button", { name: /full audit/i })[0]);
    expect(await screen.findByText("PR Summary")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /staged fix/i })).toBeInTheDocument();
  });

  it("deep-link to an off-page review falls back to getReviewDetail", async () => {
    const offPage: CodeReviewOut = {
      ...mockReviews[0],
      id: "rev_offpage",
      pr_title: "fix: off-page fallback review",
    };
    vi.mocked(api.getReviewDetail).mockResolvedValue(offPage);
    window.history.replaceState({}, "", "/reviews?review=rev_offpage");
    render(<ReviewsPage />);

    expect(await screen.findByText("fix: off-page fallback review")).toBeInTheDocument();
    expect(vi.mocked(api.getReviewDetail)).toHaveBeenCalledWith("rev_offpage");
    expect(screen.getByText("PR Summary")).toBeInTheDocument();
  });

  it("rejected detail fetch shows an alert and Retry recovers", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/reviews?review=rev_missing");
    render(<ReviewsPage />);

    // Default mock rejects unknown ids.
    expect(await screen.findByRole("alert")).toHaveTextContent(/couldn't load that review/i);

    // Retry re-fires the detail request; this time it resolves.
    vi.mocked(api.getReviewDetail).mockResolvedValueOnce(mockReviews[0]);
    await user.click(screen.getByRole("button", { name: /retry/i }));
    expect(await screen.findByText("PR Summary")).toBeInTheDocument();
    expect(screen.getByText("feat: autonomous remediation sentinel pipeline")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("clears a stale detail error when back/forward lands on a cached review", async () => {
    window.history.replaceState({}, "", "/reviews?review=rev_missing");
    render(<ReviewsPage />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/couldn't load that review/i);

    window.history.replaceState({}, "", "/reviews?review=rev_2");
    window.dispatchEvent(new Event("popstate"));

    expect(await screen.findByText("Great to Merge")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("moves focus to Back when the detail opens and back to the list on close", async () => {
    const user = userEvent.setup();
    render(<ReviewsPage />);

    await screen.findByText("feat: autonomous remediation sentinel pipeline");
    await user.click(screen.getAllByRole("button", { name: /full audit/i })[0]);
    expect(await screen.findByText("PR Summary")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /back to reviews/i })).toHaveFocus();

    await user.click(screen.getByRole("button", { name: /back to reviews/i }));
    expect(await screen.findByText("docs: update API documentation")).toBeInTheDocument();
    expect(document.activeElement).not.toBe(document.body);
  });

  describe("failed analyses (honest error states)", () => {
    const failedReview: CodeReviewOut = {
      id: "rev_fail",
      repo_id: "repo_1",
      repo_owner: "acme",
      repo_name: "sentinel",
      commit_sha: "deadbeef12345678",
      pr_number: 103,
      pr_title: "feat: risky storage migration",
      pr_author: "octocat",
      pr_author_avatar: null,
      head_branch: "feat/migration",
      base_branch: "main",
      pr_url: "https://github.com/acme/sentinel/pull/103",
      risk_score: 0,
      summary: "Analysis did not complete.",
      status: "error",
      failure_reason: "All 9 free-tier models exhausted (18 total attempts)",
      input_tokens: 0,
      output_tokens: 0,
      created_at: new Date().toISOString(),
      diff_stats: null,
      findings: [],
    };

    it("renders an honest error row: collapsed by default, full telemetry on expand, no fake 10/10 or SAFE", async () => {
      const user = userEvent.setup();
      vi.mocked(api.getCodeReviews).mockResolvedValueOnce({
        reviews: [failedReview],
        total: 1,
      });
      render(<ReviewsPage />);

      // Collapsed default: identity + ERROR pill + excerpt stay, the full
      // panel (telemetry, title, Full audit) hides until expanded.
      expect(await screen.findByText("acme/sentinel")).toBeInTheDocument();
      expect(screen.getByText("PR #103")).toBeInTheDocument();
      expect(screen.getByText(/all 9 free-tier models exhausted/i)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /expand error details/i })).toBeInTheDocument();
      expect(screen.queryByText("PR Confidence")).not.toBeInTheDocument();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();

      // Expand to the full honest panel.
      await user.click(screen.getByRole("button", { name: /expand error details/i }));
      expect(screen.getByText("feat: risky storage migration")).toBeInTheDocument();

      // Honest error markers (exact: ERROR pill + Status cell; gauge + 3
      // telemetry cells in the card + Avg tile em-dash when nothing scored)
      expect(screen.getAllByText("Error")).toHaveLength(2);
      expect(screen.getByText(/all 9 free-tier models exhausted/i)).toBeInTheDocument();
      expect(screen.getAllByText("—")).toHaveLength(5);
      expect(screen.getByText("NO SCORE")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();

      // Lying UI must be gone: risk_score defaults to 0, which used to
      // render as 10/10 confidence + SAFE gauge.
      expect(screen.queryByText("10/10")).not.toBeInTheDocument();
      expect(screen.queryByText("SAFE")).not.toBeInTheDocument();
      expect(screen.queryByText("Great to Merge")).not.toBeInTheDocument();
    });

    it("shows an honest error panel with em-dash confidence for failed analyses in detail", async () => {
      const user = userEvent.setup();
      vi.mocked(api.getCodeReviews).mockResolvedValueOnce({
        reviews: [failedReview],
        total: 1,
      });
      render(<ReviewsPage />);

      expect(await screen.findByText("PR #103")).toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: /expand error details/i }));
      await user.click(screen.getByRole("button", { name: /full audit/i }));

      expect(await screen.findByRole("alert")).toHaveTextContent(
        /free-tier models exhausted/i
      );
      expect(screen.getByText("NO SCORE")).toBeInTheDocument();
      expect(screen.queryByText("PR Summary")).not.toBeInTheDocument();
      expect(screen.queryByText("10/10")).not.toBeInTheDocument();
      expect(screen.queryByText("Changes Requested")).not.toBeInTheDocument();
      expect(screen.getByText("Findings Summary (0)")).toBeInTheDocument();
    });

    it("failed-row Retry re-polls the list; detail Retry re-polls instead of no-op", async () => {
      const user = userEvent.setup();
      vi.mocked(api.getCodeReviews).mockResolvedValue({
        reviews: [failedReview],
        total: 1,
      });
      render(<ReviewsPage />);

      expect(await screen.findByText("PR #103")).toBeInTheDocument();
      const callsBefore = vi.mocked(api.getCodeReviews).mock.calls.length;
      await user.click(screen.getByRole("button", { name: /retry/i }));
      expect(vi.mocked(api.getCodeReviews).mock.calls.length).toBeGreaterThan(callsBefore);

      // Detail view: expand first (collapsed rows hide Full audit), then
      // Retry must also re-poll the GET-only list (bumping
      // detailRetry alone is a no-op for cached rows).
      await user.click(screen.getByRole("button", { name: /expand error details/i }));
      await user.click(screen.getByRole("button", { name: /full audit/i }));
      expect(await screen.findByRole("alert")).toHaveTextContent(/free-tier models exhausted/i);
      const callsMid = vi.mocked(api.getCodeReviews).mock.calls.length;
      await user.click(screen.getByRole("button", { name: /retry/i }));
      expect(vi.mocked(api.getCodeReviews).mock.calls.length).toBeGreaterThan(callsMid);
    });

    it("search matches failure_reason text on failed rows", async () => {
      const user = userEvent.setup();
      vi.mocked(api.getCodeReviews).mockResolvedValue({
        reviews: [failedReview, ...mockReviews],
        total: 3,
      });
      render(<ReviewsPage />);

      expect(await screen.findByText("PR #103")).toBeInTheDocument();
      expect(screen.getByText("docs: update API documentation")).toBeInTheDocument();

      await user.type(
        screen.getByPlaceholderText(/search findings/i),
        "free-tier models exhausted"
      );
      expect(screen.getByText("PR #103")).toBeInTheDocument();
      expect(screen.queryByText("docs: update API documentation")).not.toBeInTheDocument();
      expect(screen.queryByText("feat: autonomous remediation sentinel pipeline")).not.toBeInTheDocument();
    });

    it("completed review with a stale failure_reason still shows its verdict", async () => {
      const stale: CodeReviewOut = {
        ...mockReviews[1],
        failure_reason: "stale reason from an earlier attempt",
      };
      vi.mocked(api.getCodeReviews).mockResolvedValue({ reviews: [stale], total: 1 });
      render(<ReviewsPage />);

      expect(await screen.findByText("docs: update API documentation")).toBeInTheDocument();
      // Real verdict telemetry survives: confidence + merge verdict render,
      // and no honest-error chrome appears. (Verdict pill renders twice on
      // the overview card: PR badge row + Status cell.)
      expect(screen.getByText("9/10")).toBeInTheDocument();
      expect(screen.getAllByText("Great to Merge")).toHaveLength(2);
      expect(screen.queryByText("NO SCORE")).not.toBeInTheDocument();
      expect(screen.queryByText("free-tier models exhausted")).not.toBeInTheDocument();
    });
  });

  describe("pending/in-progress analyses (honest neutral states)", () => {
    const pendingReview: CodeReviewOut = {
      id: "rev_pending",
      repo_id: "repo_1",
      repo_owner: "acme",
      repo_name: "sentinel",
      commit_sha: "feedface12345678",
      pr_number: 104,
      pr_title: "feat: queued pipeline run",
      pr_author: "octocat",
      pr_author_avatar: null,
      head_branch: "feat/queued",
      base_branch: "main",
      pr_url: "https://github.com/acme/sentinel/pull/104",
      risk_score: 0,
      summary: "Autonomous code review queued.",
      status: "pending",
      input_tokens: 0,
      output_tokens: 0,
      created_at: new Date().toISOString(),
      diff_stats: null,
      findings: [],
    };

    it("renders a neutral Queued row: pill + em-dash confidence, no fake verdict", async () => {
      vi.mocked(api.getCodeReviews).mockResolvedValueOnce({
        reviews: [pendingReview],
        total: 1,
      });
      render(<ReviewsPage />);

      expect(await screen.findByText("PR #104")).toBeInTheDocument();
      // Neutral pill renders twice on the card: badge row + Status cell.
      expect(screen.getAllByText("Queued")).toHaveLength(2);
      expect(screen.getByText("Autonomous code review queued.")).toBeInTheDocument();

      // Lying UI must be gone: the 0 default score used to render as
      // 10/10 confidence + SAFE gauge + Great-to-Merge verdict.
      expect(screen.queryByText("10/10")).not.toBeInTheDocument();
      expect(screen.queryByText("SAFE")).not.toBeInTheDocument();
      expect(screen.queryByText("Great to Merge")).not.toBeInTheDocument();
      expect(screen.queryByText("Changes Requested")).not.toBeInTheDocument();
      expect(screen.queryByText("Review Advised")).not.toBeInTheDocument();
      expect(screen.getByText("NO SCORE")).toBeInTheDocument();
      // Honest neutral markers (exact: gauge + 3 telemetry cells in the
      // card + Avg tile em-dash when nothing scored — same count as the
      // failed-row test).
      expect(screen.getAllByText("—")).toHaveLength(5);
    });

    it("in-progress rows render Analyzing and stay out of Avg Risk", async () => {
      const running: CodeReviewOut = {
        ...pendingReview,
        id: "rev_running",
        status: "in_progress",
      };
      vi.mocked(api.getCodeReviews).mockResolvedValueOnce({
        reviews: [mockReviews[0], running],
        total: 2,
      });
      render(<ReviewsPage />);

      expect(await screen.findByText("PR #104")).toBeInTheDocument();
      expect(screen.getAllByText("Analyzing")).toHaveLength(2);
      expect(screen.queryByText("Great to Merge")).not.toBeInTheDocument();

      const avgTab = screen.getByText(/avg risk/i).closest("button")!;
      // 85 alone — the unscored 0 must not pollute the mean.
      expect(avgTab).toHaveTextContent("85");
      expect(avgTab.textContent).not.toMatch(/43/);
      expect(avgTab.textContent).toMatch(/1 scored/i);
    });

    it("pending detail shows a neutral panel with em-dash confidence", async () => {
      const user = userEvent.setup();
      vi.mocked(api.getCodeReviews).mockResolvedValueOnce({
        reviews: [pendingReview],
        total: 1,
      });
      render(<ReviewsPage />);

      expect(await screen.findByText("PR #104")).toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: /full audit/i }));

      expect(await screen.findByText("Haunter Autonomous Audit Report")).toBeInTheDocument();
      expect(screen.queryByText("10/10")).not.toBeInTheDocument();
      expect(screen.queryByText("Great to Merge")).not.toBeInTheDocument();
      expect(screen.queryByText("Changes Requested")).not.toBeInTheDocument();
      expect(screen.getByText("Findings Summary (0)")).toBeInTheDocument();
      expect(screen.getByText(/no findings yet/i)).toBeInTheDocument();
    });
  });

  it("Avg Risk excludes failed rows and discloses the scored count", async () => {
    const failedRow: CodeReviewOut = {
      id: "rev_fail",
      repo_id: "repo_1",
      repo_owner: "acme",
      repo_name: "sentinel",
      commit_sha: "deadbeef12345678",
      pr_number: 103,
      pr_title: "feat: risky storage migration",
      pr_author: "octocat",
      pr_author_avatar: null,
      head_branch: "feat/migration",
      base_branch: "main",
      pr_url: "https://github.com/acme/sentinel/pull/103",
      risk_score: 0,
      summary: "Analysis did not complete.",
      status: "error",
      failure_reason: "All 9 free-tier models exhausted (18 total attempts)",
      input_tokens: 0,
      output_tokens: 0,
      created_at: new Date().toISOString(),
      diff_stats: null,
      findings: [],
    };
    vi.mocked(api.getCodeReviews).mockResolvedValue({
      reviews: [mockReviews[0], failedRow],
      total: 2,
    });
    render(<ReviewsPage />);

    expect(await screen.findByText("feat: autonomous remediation sentinel pipeline")).toBeInTheDocument();

    const avgTab = screen.getByText(/avg risk/i).closest("button")!;
    // 85 alone — not (85+0)/2 = 43.
    expect(avgTab).toHaveTextContent("85");
    expect(avgTab.textContent).not.toMatch(/43/);
    expect(avgTab.textContent).toMatch(/1 scored/i);
  });

  it("Avg Risk shows an honest em-dash when no rows scored", async () => {
    const failedOnly: CodeReviewOut = {
      id: "rev_fail",
      repo_id: "repo_1",
      repo_owner: "acme",
      repo_name: "sentinel",
      commit_sha: "deadbeef12345678",
      pr_number: 103,
      pr_title: "feat: risky storage migration",
      pr_author: "octocat",
      pr_author_avatar: null,
      head_branch: "feat/migration",
      base_branch: "main",
      pr_url: "https://github.com/acme/sentinel/pull/103",
      risk_score: 0,
      summary: "Analysis did not complete.",
      status: "error",
      failure_reason: "All 9 free-tier models exhausted (18 total attempts)",
      input_tokens: 0,
      output_tokens: 0,
      created_at: new Date().toISOString(),
      diff_stats: null,
      findings: [],
    };
    vi.mocked(api.getCodeReviews).mockResolvedValue({ reviews: [failedOnly], total: 1 });
    render(<ReviewsPage />);

    expect(await screen.findByText("PR #103")).toBeInTheDocument();
    const avgTab = screen.getByText(/avg risk/i).closest("button")!;
    expect(avgTab.textContent).toMatch(/no scored reviews/i);
    expect(avgTab.querySelector("span.text-zinc-500")).toHaveTextContent("—");
  });

  it("unifies the high-risk bar at >= 71: ribbon label, toggle label and min_risk API param", async () => {
    const user = userEvent.setup();
    render(<ReviewsPage />);

    expect(await screen.findByText("feat: autonomous remediation sentinel pipeline")).toBeInTheDocument();
    expect(screen.getByText(/high-risk ≥ 71/i)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Risk ≥ 71" }));
    expect(vi.mocked(api.getCodeReviews)).toHaveBeenCalledWith(
      expect.objectContaining({ min_risk: 71 })
    );
  });

  it("orders detail sections like the audit comment: summary, status grid, structural analysis, findings", async () => {
    const user = userEvent.setup();
    render(<ReviewsPage />);

    await screen.findByText("feat: autonomous remediation sentinel pipeline");
    await user.click(screen.getAllByRole("button", { name: /full audit/i })[0]);

    const reportTitle = await screen.findByText("Haunter Autonomous Audit Report");
    const summary = screen.getByText("PR Summary");
    const confidence = screen.getByText("PR Confidence");
    const blockers = screen.getByText("Blockers");
    const blast = screen.getByText("Blast Radius");
    const structural = screen.getByText(/structural & blast radius analysis/i);
    const findings = screen.getByText("Findings Summary (1)");

    const follows = (a: Element, b: Element) =>
      (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
    expect(follows(reportTitle, summary)).toBe(true);
    expect(follows(summary, confidence)).toBe(true);
    expect(follows(confidence, blockers)).toBe(true);
    expect(follows(blockers, blast)).toBe(true);
    expect(follows(blast, structural)).toBe(true);
    expect(follows(structural, findings)).toBe(true);
  });

  describe("push identity parity, collapsed errors, hide-failed toggle", () => {
    // Direct push: pr_number NULL at rest (backend hardcodes pr_number=None
    // for pushes), so no PR chrome may render for this row.
    const pushReview: CodeReviewOut = {
      ...mockReviews[1],
      id: "rev_push",
      pr_number: null,
      pr_title: null,
      pr_author: null,
      pr_author_avatar: null,
      head_branch: null,
      base_branch: null,
      pr_url: null,
    };

    const failedPushRow: CodeReviewOut = {
      ...mockReviews[0],
      id: "rev_fail_push",
      pr_number: null,
      pr_title: null,
      pr_author: null,
      pr_author_avatar: null,
      head_branch: null,
      base_branch: null,
      pr_url: null,
      status: "error",
      risk_score: 0,
      failure_reason: "push-row-boom",
      input_tokens: 0,
      output_tokens: 0,
      diff_stats: null,
      findings: [],
    };

    it("push rows render an equal-weight Push @ sha pill linking to the commit", async () => {
      vi.mocked(api.getCodeReviews).mockResolvedValue({ reviews: [pushReview], total: 1 });
      render(<ReviewsPage />);

      const pill = await screen.findByRole("link", { name: /push @ 1234567/i });
      expect(pill).toHaveAttribute(
        "href",
        "https://github.com/acme/sentinel/commit/1234567890abcdef"
      );
      // Parity with the PR# pill: same text-sm font-bold pill treatment,
      // not the old tiny mono afterthought.
      expect(pill.className).toMatch(/text-sm/);
      expect(pill.className).toMatch(/font-bold/);
    });

    it("never renders a fake PR# for push rows (overview and detail)", async () => {
      const user = userEvent.setup();
      vi.mocked(api.getCodeReviews).mockResolvedValue({ reviews: [pushReview], total: 1 });
      render(<ReviewsPage />);

      expect(await screen.findByText(/push @ 1234567/i)).toBeInTheDocument();
      expect(screen.queryByText(/PR #\d+/)).not.toBeInTheDocument();

      await user.click(screen.getByRole("button", { name: /full audit/i }));
      expect(await screen.findByText(/push @ 1234567/i)).toBeInTheDocument();
      expect(screen.queryByText(/PR #\d+/)).not.toBeInTheDocument();
    });

    it("failed rows collapse by default and expand/collapse on demand", async () => {
      const user = userEvent.setup();
      vi.mocked(api.getCodeReviews).mockResolvedValue({ reviews: [failedPushRow], total: 1 });
      render(<ReviewsPage />);

      // Collapsed default: dimmed single-line row, no telemetry, no alert.
      expect(await screen.findByText(/push @ abcdef1/i)).toBeInTheDocument();
      const expand = screen.getByRole("button", { name: /expand error details/i });
      expect(expand).toHaveAttribute("aria-expanded", "false");
      expect(screen.queryByText("PR Confidence")).not.toBeInTheDocument();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();

      await user.click(expand);
      expect(screen.getByText("PR Confidence")).toBeInTheDocument();
      expect(screen.getByRole("alert")).toHaveTextContent(/push-row-boom/i);
      const collapse = screen.getByRole("button", { name: /collapse error details/i });
      expect(collapse).toHaveAttribute("aria-expanded", "true");

      await user.click(collapse);
      expect(screen.queryByText("PR Confidence")).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: /expand error details/i })).toBeInTheDocument();
    });

    it("hide-failed toggle is OFF by default and filters failed rows only when enabled", async () => {
      const user = userEvent.setup();
      vi.mocked(api.getCodeReviews).mockResolvedValue({
        reviews: [failedPushRow, ...mockReviews],
        total: 3,
      });
      render(<ReviewsPage />);

      const toggle = await screen.findByRole("button", { name: /hide failed/i });
      expect(toggle).toHaveAttribute("aria-pressed", "false");
      // Default OFF: failed row visible alongside completed rows.
      expect(screen.getByText(/push-row-boom/i)).toBeInTheDocument();
      expect(screen.getByText("docs: update API documentation")).toBeInTheDocument();

      await user.click(toggle);
      expect(screen.queryByText(/push-row-boom/i)).not.toBeInTheDocument();
      expect(screen.getByText("docs: update API documentation")).toBeInTheDocument();
      expect(screen.getByText("feat: autonomous remediation sentinel pipeline")).toBeInTheDocument();
      const show = screen.getByRole("button", { name: /show failed/i });
      expect(show).toHaveAttribute("aria-pressed", "true");

      await user.click(show);
      expect(screen.getByText(/push-row-boom/i)).toBeInTheDocument();
    });

    it("Clear Filters resets hideFailed and restores failed rows", async () => {
      const user = userEvent.setup();
      vi.mocked(api.getCodeReviews).mockResolvedValue({
        reviews: [failedPushRow, ...mockReviews],
        total: 3,
      });
      render(<ReviewsPage />);

      // hideFailed ON plus a search filter that empties the list, so the
      // empty-state Clear Filters button appears.
      await user.click(await screen.findByRole("button", { name: /hide failed/i }));
      expect(screen.queryByText(/push-row-boom/i)).not.toBeInTheDocument();
      await user.type(
        screen.getByPlaceholderText("Search findings, commits, files..."),
        "zzz-no-such-review"
      );
      expect(await screen.findByText("No Code Reviews Found")).toBeInTheDocument();

      await user.click(screen.getByRole("button", { name: /clear filters/i }));

      // Failed rows return, the toggle flips back OFF, search is cleared.
      expect(await screen.findByText(/push-row-boom/i)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /hide failed/i })).toHaveAttribute(
        "aria-pressed",
        "false"
      );
      expect(
        screen.getByPlaceholderText("Search findings, commits, files...")
      ).toHaveValue("");
    });

    it("hideFailed composes with search: a search hit on a failed row stays hidden", async () => {
      const user = userEvent.setup();
      vi.mocked(api.getCodeReviews).mockResolvedValue({
        reviews: [failedPushRow, ...mockReviews],
        total: 3,
      });
      render(<ReviewsPage />);

      await user.click(await screen.findByRole("button", { name: /hide failed/i }));
      // Query matches only the failed row's failure_reason — hideFailed still wins.
      await user.type(
        screen.getByPlaceholderText("Search findings, commits, files..."),
        "push-row-boom"
      );
      expect(await screen.findByText("No Code Reviews Found")).toBeInTheDocument();
      expect(screen.queryByText(/push-row-boom/i)).not.toBeInTheDocument();
    });
  });
});
