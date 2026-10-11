import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ReviewsPage, { __resetAssociatedPrCacheForTests } from "./page";
import { api, CodeReviewOut, RepoOut } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    replace: vi.fn(),
    push: vi.fn(),
  }),
  usePathname: () => "/reviews",
}));

/**
 * Confidence now lives in the gauge, which renders the numeral and the
 * "/10" suffix as two sibling spans (so the suffix can sit at a smaller
 * size). Plain getByText("9/10") can no longer match it. This asserts the
 * ring's accessible name instead, which is what a screen reader actually
 * announces and is unaffected by the visual split.
 */
function expectConfidence(value: number) {
  return expect(screen.getByRole("img", { name: new RegExp(`Confidence ${value} of 10`, "i") })).toBeInTheDocument();
}

vi.mock("@/lib/auth-context", () => ({
  useAuth: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    getRepos: vi.fn(),
    getCodeReviews: vi.fn(),
    getReviewDetail: vi.fn(),
    getAssociatedPr: vi.fn(),
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
    __resetAssociatedPrCacheForTests();
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
    // Default: no associated PR — push rows keep the Push pill unless a test
    // overrides this per-case below.
    vi.mocked(api.getAssociatedPr).mockResolvedValue({ associated_pr: null });

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
    expectConfidence(2);
    expectConfidence(9);

    await user.click(screen.getAllByRole("button", { name: /full audit/i })[0]);

    expect(await screen.findByText("PR Summary")).toBeInTheDocument();
    expect(screen.getByText("Merge recommendation")).toBeInTheDocument();
    expect(screen.getByText("Confidence")).toBeInTheDocument();
    expect(screen.getByText("Must-Fix")).toBeInTheDocument();
    expect(screen.getByText("Blast Radius")).toBeInTheDocument();
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
    expectConfidence(9);
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

  describe("confidence ring (0-10, not 0-100)", () => {
    it("shows confidence on a 0-10 scale and never leaks the raw risk score into the ring", async () => {
      vi.mocked(api.getCodeReviews).mockResolvedValue({
        reviews: [mockReviews[0]],
        total: 1,
      });
      render(<ReviewsPage />);

      await screen.findByText("feat: autonomous remediation sentinel pipeline");

      // Risk 85 -> confidence 2/10, announced on the ring itself.
      expectConfidence(2);

      const ring = screen.getByRole("img", { name: /confidence 2 of 10/i });
      // The ring must not be a risk dial any more.
      expect(ring.textContent).not.toContain("85");
      // The raw 0-100 score stays available as text so nothing is hidden.
      expect(screen.getByText("85/100")).toBeInTheDocument();
      // Severity is still spelled out, never colour alone.
      expect(screen.getByText("CRITICAL")).toBeInTheDocument();
    });

    it("keeps the ring neutral for a failed analysis — never a fake 10/10", async () => {
      vi.mocked(api.getCodeReviews).mockResolvedValue({
        reviews: [{ ...mockReviews[0], status: "error", failure_reason: "boom" }],
        total: 1,
      });
      render(<ReviewsPage />);

      expect(
        await screen.findByRole("img", { name: /no confidence score/i })
      ).toBeInTheDocument();
      expect(screen.queryByText("10/10")).not.toBeInTheDocument();
      expect(screen.getByText("NO SCORE")).toBeInTheDocument();
    });
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

      // Honest error markers: the failure chip + the neutral "Failed" status
      // fact, the full reason text in a scroll region, and the neutral gauge.
      expect(screen.getByText("Analysis failed")).toBeInTheDocument();
      // Scoped to the row, and exact: exactly one "Failed" status fact on the
      // card. A global count would also sweep up the triage chip of the same
      // name, which is a different element with a different job. The card is
      // located by its test id, not by a Tailwind class, so a restyle cannot
      // silently re-scope this assertion onto an ancestor.
      const failedCard = screen.getByTestId("failed-review-card");
      expect(within(failedCard).getAllByText("Failed")).toHaveLength(1);

      // The reason is no longer clipped to a 140-char excerpt. Assert the
      // rendered TEXT: an `overflow-y-auto` class check passes just as
      // happily on a box holding a single character.
      const reasonText = screen.getByText("All 9 free-tier models exhausted (18 total attempts)");
      expect(reasonText).toBeInTheDocument();
      // Layout is a separate guarantee from content, so it gets its own
      // assertion: the box must stay a bounded scroll region. Drop
      // `max-h-32 overflow-y-auto` and a long "Last errors: ..." tail is
      // clipped with no way to reach the remainder — a content-only test
      // above would still pass.
      expect(reasonText.className).toContain("max-h-32");
      expect(reasonText.className).toContain("overflow-y-auto");
      // The unscored risk slot must stay honest. Without this the row could
      // render "0/100" (risk_score really is 0 here) and every other assertion
      // would still pass.
      expect(screen.getByText("Not scored")).toBeInTheDocument();
      expect(screen.queryByText(/^0\/100$/)).not.toBeInTheDocument();
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
      expectConfidence(9);
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
      // Neutral pill renders twice on the row: identity chip + status fact.
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
      // Honest neutral marker: the row states "Not scored" rather than
      // implying a zero-risk verdict.
      expect(screen.getByText("Not scored")).toBeInTheDocument();
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

  describe("suppressed analyses (saved audit + publication skipped)", () => {
    const suppressedReview: CodeReviewOut = {
      ...mockReviews[0],
      id: "rev_sup",
      status: "suppressed",
      failure_reason: "PR comments disabled by repo settings; review published nowhere",
    };

    it("renders the saved audit like completed: verdict + findings + neutral publication indicator", async () => {
      vi.mocked(api.getCodeReviews).mockResolvedValueOnce({
        reviews: [suppressedReview],
        total: 1,
      });
      render(<ReviewsPage />);

      expect(await screen.findByText("PR #101")).toBeInTheDocument();
      // Real verdict telemetry survives the publish-skip.
      expectConfidence(2);
      expect(screen.getAllByText("Changes Requested").length).toBeGreaterThanOrEqual(1);
      // The finding count is stated once, on the findings toggle — the old
      // footer meta line repeated it alongside counts already shown above.
      expect(screen.getByRole("button", { name: /review \(1\)/i })).toBeInTheDocument();
      expect(screen.queryByText("1 finding")).not.toBeInTheDocument();
      // Neutral publish-state indicator, not an error.
      expect(screen.getByText("Publication skipped")).toBeInTheDocument();
      // Pending chrome must be gone: no neutral gauge, no in-progress copy.
      expect(screen.queryByText("NO SCORE")).not.toBeInTheDocument();
      expect(screen.queryByText(/no findings yet/i)).not.toBeInTheDocument();
      expect(screen.queryByText("Queued")).not.toBeInTheDocument();
      expect(screen.queryByText("Analyzing")).not.toBeInTheDocument();
    });

    it("suppressed detail shows the full audit with patches + indicator, no Retry", async () => {
      const user = userEvent.setup();
      vi.mocked(api.getCodeReviews).mockResolvedValueOnce({
        reviews: [suppressedReview],
        total: 1,
      });
      render(<ReviewsPage />);

      expect(await screen.findByText("PR #101")).toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: /full audit/i }));

      expect(await screen.findByText("PR Summary")).toBeInTheDocument();
      expect(screen.getByText("Critical security vulnerability detected in token parser middleware.")).toBeInTheDocument();
      expect(screen.getByText("Findings Summary (1)")).toBeInTheDocument();
      expect(screen.getByText(/jwt\.verify/)).toBeInTheDocument();
      expect(screen.getByText("Publication skipped")).toBeInTheDocument();
      expect(screen.queryByText(/no findings yet/i)).not.toBeInTheDocument();
      // Suppressed is terminal like completed — nothing failed, no Retry.
      expect(screen.queryByRole("button", { name: /^retry$/i })).not.toBeInTheDocument();
    });

    it("suppressed rows score in Avg Risk while pending rows stay out", async () => {
      const pendingRow: CodeReviewOut = {
        ...mockReviews[1],
        id: "rev_pending_avg",
        status: "pending",
        risk_score: 0,
        summary: "Autonomous code review queued.",
        findings: [],
      };
      const suppressedLow: CodeReviewOut = {
        ...mockReviews[1],
        id: "rev_sup_avg",
        status: "suppressed",
        risk_score: 15,
      };
      vi.mocked(api.getCodeReviews).mockResolvedValueOnce({
        reviews: [mockReviews[0], suppressedLow, pendingRow],
        total: 3,
      });
      render(<ReviewsPage />);

      expect(await screen.findByText("feat: autonomous remediation sentinel pipeline")).toBeInTheDocument();
      const avgTab = screen.getByText(/avg risk/i).closest("button")!;
      // (85+15)/2 = 50 — the pending 0 must not pollute the mean (would be 33).
      expect(avgTab).toHaveTextContent("50");
      expect(avgTab.textContent).not.toMatch(/33/);
      expect(avgTab.textContent).toMatch(/2 scored/i);
      // Pending row stays neutral alongside the scored suppressed row.
      expect(screen.getAllByText("Queued").length).toBeGreaterThanOrEqual(1);
      expect(screen.getByText("Publication skipped")).toBeInTheDocument();
    });

    it("unscored suppressed rows (no completion signal) render neutral, never Great-to-Merge/SAFE", async () => {
      const unscored: CodeReviewOut = {
        ...mockReviews[1],
        id: "rev_sup_unscored",
        status: "suppressed",
        risk_score: 0,
        summary: "Autonomous code review queued.",
        findings: [],
        input_tokens: 0,
        output_tokens: 0,
      };
      vi.mocked(api.getCodeReviews).mockResolvedValueOnce({
        reviews: [unscored],
        total: 1,
      });
      render(<ReviewsPage />);

      expect(await screen.findByText("PR #102")).toBeInTheDocument();
      // Neutral treatment: Suppressed pill twice (badge row + Status cell),
      // no-score gauge, em-dash telemetry.
      expect(screen.getAllByText("Suppressed")).toHaveLength(2);
      expect(screen.getByText("NO SCORE")).toBeInTheDocument();
      // The 0 default must not read as a finished audit.
      expect(screen.queryByText("10/10")).not.toBeInTheDocument();
      expect(screen.queryByText("SAFE")).not.toBeInTheDocument();
      expect(screen.queryByText("Great to Merge")).not.toBeInTheDocument();
      expect(screen.queryByText("Changes Requested")).not.toBeInTheDocument();
      expect(screen.queryByText("Review Advised")).not.toBeInTheDocument();
      // No saved audit, so no publication indicator and no unscored mean.
      expect(screen.queryByText("Publication skipped")).not.toBeInTheDocument();
      const avgTab = screen.getByText(/avg risk/i).closest("button")!;
      expect(avgTab.textContent).toMatch(/no scored reviews/i);
    });

    it("unscored suppressed detail shows the neutral panel, not the full audit", async () => {
      const user = userEvent.setup();
      const unscored: CodeReviewOut = {
        ...mockReviews[1],
        id: "rev_sup_unscored_detail",
        status: "suppressed",
        risk_score: 0,
        summary: "Autonomous code review queued.",
        findings: [],
        input_tokens: 0,
        output_tokens: 0,
      };
      vi.mocked(api.getCodeReviews).mockResolvedValueOnce({
        reviews: [unscored],
        total: 1,
      });
      render(<ReviewsPage />);

      expect(await screen.findByText("PR #102")).toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: /full audit/i }));

      expect(await screen.findByText("Haunter Autonomous Audit Report")).toBeInTheDocument();
      expect(screen.getAllByText("Suppressed").length).toBeGreaterThanOrEqual(1);
      expect(screen.queryByText("10/10")).not.toBeInTheDocument();
      expect(screen.queryByText("Great to Merge")).not.toBeInTheDocument();
      expect(screen.queryByText("Changes Requested")).not.toBeInTheDocument();
      expect(screen.queryByText("Publication skipped")).not.toBeInTheDocument();
      expect(screen.getByText(/no findings yet/i)).toBeInTheDocument();
    });

    it("clean suppressed audit (0 risk, no findings, real summary + tokens) renders scored + counts in Avg Risk", async () => {
      // The bot-finding shape: score+findings are indistinguishable from an
      // unsaved row, but the persisted LLM summary + token telemetry prove
      // analysis finished — must render the full audit, never neutral.
      const clean: CodeReviewOut = {
        ...mockReviews[1],
        id: "rev_sup_clean",
        status: "suppressed",
        risk_score: 0,
        summary: "Clean review: no blocking issues detected. Changes look safe.",
        findings: [],
        input_tokens: 800,
        output_tokens: 150,
        failure_reason: "PR comments disabled by repo settings; review published nowhere",
      };
      vi.mocked(api.getCodeReviews).mockResolvedValueOnce({
        reviews: [mockReviews[0], clean],
        total: 2,
      });
      render(<ReviewsPage />);

      expect(await screen.findByText("feat: autonomous remediation sentinel pipeline")).toBeInTheDocument();
      // Full audit chrome: verdict + confidence + publication indicator.
      expectConfidence(10);
      expect(screen.getAllByText("Great to Merge").length).toBeGreaterThanOrEqual(1);
      expect(screen.getAllByText("Publication skipped").length).toBeGreaterThanOrEqual(1);
      expect(screen.queryByText("NO SCORE")).not.toBeInTheDocument();
      // Avg Risk counts it: (85+0)/2 = 43 across 2 scored.
      const avgTab = screen.getByText(/avg risk/i).closest("button")!;
      expect(avgTab).toHaveTextContent("43");
      expect(avgTab.textContent).toMatch(/2 scored/i);
    });

    it("empty-diff suppressed audit (0 tokens, real summary) still scores", async () => {
      // code_reviewer short-circuits empty diffs with 0/0 tokens but a real
      // persisted summary — tokens alone would miss it, the summary branch
      // of the completion signal catches it.
      const emptyDiff: CodeReviewOut = {
        ...mockReviews[1],
        id: "rev_sup_emptydiff",
        status: "suppressed",
        risk_score: 0,
        summary: "No code changes detected in diff.",
        findings: [],
        input_tokens: 0,
        output_tokens: 0,
        failure_reason: "PR comments disabled by repo settings; review published nowhere",
      };
      vi.mocked(api.getCodeReviews).mockResolvedValueOnce({
        reviews: [emptyDiff],
        total: 1,
      });
      render(<ReviewsPage />);

      expect(await screen.findByText("PR #102")).toBeInTheDocument();
      expectConfidence(10);
      expect(screen.getAllByText("Great to Merge").length).toBeGreaterThanOrEqual(1);
      expect(screen.getByText("Publication skipped")).toBeInTheDocument();
      expect(screen.queryByText("NO SCORE")).not.toBeInTheDocument();
      const avgTab = screen.getByText(/avg risk/i).closest("button")!;
      expect(screen.getByTitle(/Mean of 1 scored review/)).toHaveTextContent(/^0 \/ 100$/);
      expect(avgTab.textContent).toMatch(/1 scored/i);
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
    // Reading order: report title -> summary (what the analysis found) ->
    // merge recommendation (what to do) -> confidence/blast -> structural
    // analysis -> findings. The decision panel replaces the old flat 4-cell
    // status grid.
    const summary = screen.getByText("PR Summary");
    const decision = screen.getByText("Merge recommendation");
    const confidence = screen.getByText("Confidence");
    const blast = screen.getByText("Blast Radius");
    const structural = screen.getByText(/structural & blast radius analysis/i);
    const findings = screen.getByText("Findings Summary (1)");

    const follows = (a: Element, b: Element) =>
      (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
    expect(follows(reportTitle, summary)).toBe(true);
    expect(follows(summary, decision)).toBe(true);
    expect(follows(decision, confidence)).toBe(true);
    expect(follows(confidence, blast)).toBe(true);
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
      // Expanding reveals the full failure text plus the supporting context.
      // Asserting only the absence of "PR Confidence" would still pass if the
      // panel expanded to nothing, so name the actual content.
      expect(screen.getByText(/push-row-boom/i)).toBeInTheDocument();
      expect(screen.getByText("Failure details")).toBeInTheDocument();
      expect(
        screen.getByText(/No risk score, confidence or blocker telemetry exists/i)
      ).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /copy reason/i })).toBeInTheDocument();
      // The reason box is static content mounted with the collapsed row, so it
      // is deliberately not a live region in either state.
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      const collapse = screen.getByRole("button", { name: /collapse error details/i });
      expect(collapse).toHaveAttribute("aria-expanded", "true");

      await user.click(collapse);
      expect(screen.queryByText("Failure details")).not.toBeInTheDocument();
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
      // Reviews exist but none match, so this is the filtered-empty state,
      // not the "nothing has ever been reviewed" state.
      expect(await screen.findByText("Nothing matches these filters")).toBeInTheDocument();

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
      expect(await screen.findByText("Nothing matches these filters")).toBeInTheDocument();
      expect(screen.queryByText(/push-row-boom/i)).not.toBeInTheDocument();
    });
  });

  describe("associated-PR pill swap (push rows that belong to a PR)", () => {
    // Completed push row: pr_number NULL at rest, so the card renders the
    // Push pill until the associated-PR lookup resolves.
    const pushRowForAssoc = (id: string, sha: string): CodeReviewOut => ({
      ...mockReviews[1],
      id,
      commit_sha: sha,
      pr_number: null,
      pr_title: null,
      pr_author: null,
      pr_author_avatar: null,
      head_branch: null,
      base_branch: null,
      pr_url: null,
    });

    it("resolved PR swaps the Push pill to an amber PR #n pill linking the PR URL", async () => {
      vi.mocked(api.getCodeReviews).mockResolvedValue({
        reviews: [pushRowForAssoc("rev_assoc_82", "aaaabbbbccccdddd")],
        total: 1,
      });
      vi.mocked(api.getAssociatedPr).mockResolvedValue({
        associated_pr: {
          number: 82,
          url: "https://github.com/acme/sentinel/pull/82",
          title: null,
        },
      });
      render(<ReviewsPage />);

      expect(await screen.findByText("PR #82")).toBeInTheDocument();
      expect(screen.getByRole("link", { name: /PR #82/ })).toHaveAttribute(
        "href",
        "https://github.com/acme/sentinel/pull/82"
      );
      expect(screen.queryByText(/Push @/)).not.toBeInTheDocument();
      expect(vi.mocked(api.getAssociatedPr)).toHaveBeenCalledWith("rev_assoc_82");
    });

    it("null resolution keeps the Push pill (direct-to-main push)", async () => {
      vi.mocked(api.getCodeReviews).mockResolvedValue({
        reviews: [pushRowForAssoc("rev_assoc_null", "eeeffff00001111")],
        total: 1,
      });
      vi.mocked(api.getAssociatedPr).mockResolvedValue({ associated_pr: null });
      render(<ReviewsPage />);

      expect(await screen.findByText(/Push @ eeeffff/i)).toBeInTheDocument();
      await waitFor(() =>
        expect(vi.mocked(api.getAssociatedPr)).toHaveBeenCalledWith("rev_assoc_null")
      );
      expect(screen.queryByText(/PR #\d+/)).not.toBeInTheDocument();
    });

    it("network error keeps the Push pill (silent fail, no fake PR#)", async () => {
      vi.mocked(api.getCodeReviews).mockResolvedValue({
        reviews: [pushRowForAssoc("rev_assoc_err", "2222333344445555")],
        total: 1,
      });
      vi.mocked(api.getAssociatedPr).mockRejectedValue(new Error("backend down"));
      render(<ReviewsPage />);

      expect(await screen.findByText(/Push @ 2222333/i)).toBeInTheDocument();
      await waitFor(() =>
        expect(vi.mocked(api.getAssociatedPr)).toHaveBeenCalledWith("rev_assoc_err")
      );
      expect(screen.queryByText(/PR #\d+/)).not.toBeInTheDocument();
    });

    it("keys by review id: different reviews sharing one sha fetch independently (no cross-contamination)", async () => {
      const sharedSha = "7777888899990000";
      vi.mocked(api.getCodeReviews).mockResolvedValue({
        reviews: [pushRowForAssoc("rev_assoc_a", sharedSha), pushRowForAssoc("rev_assoc_b", sharedSha)],
        total: 2,
      });
      vi.mocked(api.getAssociatedPr).mockResolvedValue({ associated_pr: null });
      render(<ReviewsPage />);

      expect(await screen.findAllByText(/Push @ 7777888/i)).toHaveLength(2);
      // One fetch per review — a sha-keyed cache would dedupe these into 1.
      await waitFor(() =>
        expect(vi.mocked(api.getAssociatedPr)).toHaveBeenCalledTimes(2)
      );
      expect(vi.mocked(api.getAssociatedPr)).toHaveBeenCalledWith("rev_assoc_a");
      expect(vi.mocked(api.getAssociatedPr)).toHaveBeenCalledWith("rev_assoc_b");
    });

    it("caches by review id: re-render fetches once, never refetches", async () => {
      vi.mocked(api.getCodeReviews).mockResolvedValue({
        reviews: [pushRowForAssoc("rev_assoc_cache", "6666777788889999")],
        total: 1,
      });
      vi.mocked(api.getAssociatedPr).mockResolvedValue({ associated_pr: null });
      const first = render(<ReviewsPage />);

      expect(await screen.findByText(/Push @ 6666777/i)).toBeInTheDocument();
      await waitFor(() =>
        expect(vi.mocked(api.getAssociatedPr)).toHaveBeenCalledTimes(1)
      );
      first.unmount();

      render(<ReviewsPage />);
      expect(await screen.findByText(/Push @ 6666777/i)).toBeInTheDocument();
      // Mount effects flush inside the findBy act cycle, so any stray
      // refetch would already be recorded — assert the count directly.
      expect(vi.mocked(api.getAssociatedPr)).toHaveBeenCalledTimes(1);
    });

    it("list Refresh retries a failed (null) lookup instead of serving the stale miss", async () => {
      // Fresh row objects per list fetch, like real API JSON payloads.
      vi.mocked(api.getCodeReviews).mockImplementation(async () => ({
        reviews: [{ ...pushRowForAssoc("rev_assoc_retry", "abcd1234abcd1234") }],
        total: 1,
      }));
      vi.mocked(api.getAssociatedPr).mockResolvedValue({ associated_pr: null });
      render(<ReviewsPage />);

      expect(await screen.findByText(/Push @ abcd123/i)).toBeInTheDocument();
      await waitFor(() =>
        expect(vi.mocked(api.getAssociatedPr)).toHaveBeenCalledTimes(1)
      );

      // Backend can resolve the PR now — Refresh must retry, not serve cache.
      vi.mocked(api.getAssociatedPr).mockResolvedValue({
        associated_pr: {
          number: 77,
          url: "https://github.com/acme/sentinel/pull/77",
          title: null,
        },
      });
      fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

      expect(await screen.findByText("PR #77")).toBeInTheDocument();
      expect(vi.mocked(api.getAssociatedPr)).toHaveBeenCalledTimes(2);
    });

    it("list Refresh keeps a resolved (success) lookup instead of refetching it", async () => {
      // Fresh row objects per list fetch, like real API JSON payloads.
      vi.mocked(api.getCodeReviews).mockImplementation(async () => ({
        reviews: [{ ...pushRowForAssoc("rev_assoc_keep", "1234abcd1234abcd") }],
        total: 1,
      }));
      vi.mocked(api.getAssociatedPr).mockResolvedValue({
        associated_pr: {
          number: 42,
          url: "https://github.com/acme/sentinel/pull/42",
          title: "Kept PR",
        },
      });
      render(<ReviewsPage />);

      expect(await screen.findByText("PR #42")).toBeInTheDocument();
      await waitFor(() =>
        expect(vi.mocked(api.getAssociatedPr)).toHaveBeenCalledTimes(1)
      );

      // Refresh delivers fresh rows but must not refetch the resolved PR.
      fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

      expect(await screen.findByText("PR #42")).toBeInTheDocument();
      // Mount effects flush inside the findBy act cycle, so any stray
      // refetch would already be recorded — assert the count directly.
      expect(vi.mocked(api.getAssociatedPr)).toHaveBeenCalledTimes(1);
    });
  });

  /**
   * Coverage for the triage-first surfaces added by the redesign. The strip,
   * the cluster banner and the sort control are ~200 lines of new UI; before
   * this block none of them had a single assertion.
   */
  describe("triage strip, failure clusters and sort", () => {
    const row = (over: Partial<CodeReviewOut> & { id: string }): CodeReviewOut => ({
      repo_id: "repo_1",
      repo_owner: "acme",
      repo_name: "sentinel",
      commit_sha: "abcdef0123456789",
      pr_number: 101,
      pr_title: "feat: sentinel",
      pr_author: "octocat",
      pr_author_avatar: null,
      head_branch: "feat/x",
      base_branch: "main",
      pr_url: "https://github.com/acme/sentinel/pull/101",
      risk_score: 0,
      summary: "summary",
      status: "completed",
      input_tokens: 0,
      output_tokens: 0,
      created_at: new Date().toISOString(),
      diff_stats: null,
      findings: [],
      ...over,
    } as CodeReviewOut);

    const blocker = row({ id: "r_block", pr_title: "alpha blocker", risk_score: 90 });
    const advisory = row({
      id: "r_adv",
      pr_title: "beta advisory",
      risk_score: 45,
    });
    const clean = row({ id: "r_clean", pr_title: "gamma clean", risk_score: 10 });
    const pending = row({ id: "r_pend", pr_title: "delta pending", status: "pending" });
    const failed = (id: string, title: string, reason: string) =>
      row({
        id,
        pr_title: title,
        status: "error",
        failure_reason: reason,
      });

    const counterOf = (strip: HTMLElement, name: RegExp) =>
      within(strip).getByRole("button", { name }).lastElementChild;

    it("counts every outcome exactly once and shows the page total", async () => {
      vi.mocked(api.getCodeReviews).mockResolvedValue({
        reviews: [blocker, advisory, clean, pending, failed("r_fail", "epsilon failed", "boom")],
        total: 5,
      });
      render(<ReviewsPage />);

      const strip = await screen.findByRole("region", { name: /review triage summary/i });
      expect(counterOf(strip, /need changes/i)).toHaveTextContent("1");
      expect(counterOf(strip, /review advised/i)).toHaveTextContent("1");
      expect(counterOf(strip, /in progress/i)).toHaveTextContent("1");
      expect(counterOf(strip, /failed/i)).toHaveTextContent("1");
      expect(counterOf(strip, /great to merge/i)).toHaveTextContent("1");
      expect(counterOf(strip, /^all/i)).toHaveTextContent("5");

      // The strip carries the shared outcome vocabulary, not a private copy.
      expect(
        within(strip).getByRole("button", { name: /need changes/i })
      ).toHaveAttribute(
        "title",
        "Must-fix findings or a critical risk score. Address before merge."
      );
      // Every other number on the page is scoped; so is this one. "In view",
      // not "This page": these counters are computed over the search / tab /
      // hide-failed filtered list, which is narrower than the page itself.
      expect(within(strip).getByText("In view")).toBeInTheDocument();
    });

    it("counts the rows the outcome chips can actually reveal", async () => {
      const user = userEvent.setup();
      vi.mocked(api.getCodeReviews).mockResolvedValue({
        reviews: [blocker, advisory, clean],
        total: 3,
      });
      render(<ReviewsPage />);
      await screen.findByRole("region", { name: /review triage summary/i });

      // A chip must never advertise rows that the current filters exclude:
      // the "Patches" ribbon tab hides every row here (none carry a patch).
      await user.click(screen.getByRole("button", { name: /patches/i }));
      expect(await screen.findByText(/nothing matches these filters/i)).toBeInTheDocument();
      const emptied = screen.getByRole("region", { name: /review triage summary/i });
      expect(counterOf(emptied, /^all/i)).toHaveTextContent("0");
      for (const label of [/need changes/i, /review advised/i, /great to merge/i]) {
        expect(counterOf(emptied, label)).toHaveTextContent("0");
      }
    });

    it("suppresses the failure banner for a single failure", async () => {
      vi.mocked(api.getCodeReviews).mockResolvedValue({
        reviews: [failed("r_f1", "first failure", "Provider quota exhausted (9 attempts)")],
        total: 1,
      });
      render(<ReviewsPage />);
      await screen.findByText("first failure");
      // One cause stated once is noise — the row already says it in full.
      expect(screen.queryByText(/failed analysis/i)).not.toBeInTheDocument();
      expect(screen.getByText("Provider quota exhausted (9 attempts)")).toBeInTheDocument();
    });

    it("shows the failure banner once the same cause hits two rows", async () => {
      vi.mocked(api.getCodeReviews).mockResolvedValue({
        reviews: [
          failed("r_f1", "first failure", "Provider quota exhausted (9 attempts)"),
          failed("r_f2", "second failure", "Provider quota exhausted (12 attempts)"),
        ],
        total: 2,
      });
      render(<ReviewsPage />);
      expect(await screen.findByText(/2 failed analyses/)).toBeInTheDocument();
      // The signature appears in the banner and on both rows.
      expect(screen.getAllByText(/Provider quota exhausted/)).toHaveLength(3);
    });

    it("reports the causes behind failures across several distinct clusters", async () => {
      vi.mocked(api.getCodeReviews).mockResolvedValue({
        reviews: [
          failed("r_f1", "a", "Provider quota exhausted (9 attempts)"),
          failed("r_f2", "b", "Provider quota exhausted (12 attempts)"),
          failed("r_f3", "c", "GitHub 404 not found"),
        ],
        total: 3,
      });
      render(<ReviewsPage />);
      expect(await screen.findByText(/3 failed analyses across 2 causes/)).toBeInTheDocument();
      // banner + the two rows that share the cause
      expect(screen.getAllByText(/Provider quota exhausted/)).toHaveLength(3);
      // banner + the one row with its own cause
      expect(screen.getAllByText(/GitHub 404 not found/)).toHaveLength(2);
    });

    it("reorders the rendered rows when the sort control changes", async () => {
      const user = userEvent.setup();
      const olderBlocker = row({
        id: "r_b",
        pr_title: "older blocker",
        risk_score: 90,
        created_at: new Date(Date.now() - 7200000).toISOString(),
      });
      const newerAdvisory = row({
        id: "r_a",
        pr_title: "newer advisory",
        risk_score: 45,
        created_at: new Date().toISOString(),
      });
      vi.mocked(api.getCodeReviews).mockResolvedValue({
        reviews: [olderBlocker, newerAdvisory],
        total: 2,
      });
      render(<ReviewsPage />);

      const blockerTitle = await screen.findByText("older blocker");
      const advisoryTitle = screen.getByText("newer advisory");
      const precedes = (a: Element, b: Element) =>
        Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);

      // Default "Attention": the merge blocker outranks the advisory row.
      expect(precedes(blockerTitle, advisoryTitle)).toBe(true);

      await user.click(screen.getByRole("button", { name: /^newest$/i }));
      // "Newest" wins on recency instead: the advisory row is the newer one.
      expect(precedes(advisoryTitle, blockerTitle)).toBe(true);

      await user.click(screen.getByRole("button", { name: /^attention$/i }));
      expect(precedes(blockerTitle, advisoryTitle)).toBe(true);
    });

    it("returns to the first page when an outcome chip or the sort control is used", async () => {
      const user = userEvent.setup();
      // total must exceed PAGE_SIZE or the pagination controls never render.
      const pageOne = blocker;
      const pageTwo = clean;
      vi.mocked(api.getCodeReviews).mockImplementation(async (params) =>
        (params?.offset ?? 0) === 0
          ? { reviews: [pageOne], total: 40 }
          : { reviews: [pageTwo], total: 40 }
      );
      render(<ReviewsPage />);

      await screen.findByText("alpha blocker");
      await user.click(screen.getByRole("button", { name: /^next$/i }));
      await waitFor(() =>
        expect(vi.mocked(api.getCodeReviews).mock.calls.at(-1)?.[0]?.offset).toBe(15)
      );
      expect(await screen.findByText("gamma clean")).toBeInTheDocument();
      expect(screen.queryByText("alpha blocker")).not.toBeInTheDocument();

      // Filtering rows 16-30 client-side would have produced an empty list
      // here; resetting to page 0 is what makes the chip find its rows.
      await user.click(screen.getByRole("button", { name: /need changes/i }));
      await waitFor(() =>
        expect(vi.mocked(api.getCodeReviews).mock.calls.at(-1)?.[0]?.offset).toBe(0)
      );
      expect(await screen.findByText("alpha blocker")).toBeInTheDocument();

      await user.click(screen.getByRole("button", { name: /^next$/i }));
      await waitFor(() =>
        expect(vi.mocked(api.getCodeReviews).mock.calls.at(-1)?.[0]?.offset).toBe(15)
      );
      await user.click(screen.getByRole("button", { name: /^newest$/i }));
      expect(vi.mocked(api.getCodeReviews).mock.calls.at(-1)?.[0]?.offset).toBe(0);
    });

    /**
     * The page index is only ever parked past the end of the result set by the
     * server shrinking UNDERNEATH it, because every filter control on this page
     * resets to page 0. Refresh does not reset: it re-polls the current page,
     * which is exactly how a user ends up on page 3 when only 20 rows remain.
     * Without the clamp that leaves the footer claiming "Showing 46 to 60 of 20
     * reviews" and Next dead-ending on an offset the server has no rows for.
     */
    it("clamps the page when a refresh shrinks the result set beneath it", async () => {
      const user = userEvent.setup();
      // `total` is mutable so the same mock can serve four full pages and then
      // report a shrunken set on the refresh.
      let total = 60;
      vi.mocked(api.getCodeReviews).mockImplementation(async (params) => {
        const offset = params?.offset ?? 0;
        const reviewsForPage =
          offset === 0
            ? blocker
            : row({ id: `r_off${offset}`, pr_title: `row at offset ${offset}`, risk_score: 10 });
        return { reviews: [reviewsForPage], total };
      });
      render(<ReviewsPage />);

      await screen.findByText("alpha blocker");
      for (let i = 1; i <= 3; i += 1) {
        await user.click(screen.getByRole("button", { name: /^next$/i }));
        await waitFor(() =>
          expect(vi.mocked(api.getCodeReviews).mock.calls.at(-1)?.[0]?.offset).toBe(15 * i)
        );
      }
      // Parked on the last page: 46-60 of 60.
      expect(screen.getByText("Showing 46 to 60 of 60 reviews")).toBeInTheDocument();

      // The dataset shrank upstream and Refresh re-polls without resetting the
      // page, so the response for offset 45 now reports only 20 rows total.
      total = 20;
      await user.click(screen.getByRole("button", { name: /^refresh$/i }));

      // Clamped to the surviving last page: 16-20 of 20, never 46-60 of 20.
      await waitFor(() =>
        expect(screen.getByText("Showing 16 to 20 of 20 reviews")).toBeInTheDocument()
      );
      expect(screen.queryByText("Showing 46 to 60 of 20 reviews")).not.toBeInTheDocument();
      // And it refetched the page it clamped to instead of leaving the dead
      // offset on screen.
      expect(vi.mocked(api.getCodeReviews).mock.calls.at(-1)?.[0]?.offset).toBe(15);
    });

    it("shows the never-reviewed empty state when the API returns no reviews", async () => {
      vi.mocked(api.getCodeReviews).mockResolvedValue({ reviews: [], total: 0 });
      render(<ReviewsPage />);

      expect(await screen.findByText("No Code Reviews Found")).toBeInTheDocument();
      expect(
        screen.getByText(/push commits or open a pull request/i)
      ).toBeInTheDocument();
      // No rows means no triage strip and no cluster banner to keep honest.
      expect(screen.queryByRole("region", { name: /review triage summary/i })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /clear filters/i })).not.toBeInTheDocument();
      // Not the sibling "filtered everything away" branch.
      expect(screen.queryByText(/nothing matches these filters/i)).not.toBeInTheDocument();
    });
  });
});
