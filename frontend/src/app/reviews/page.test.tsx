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
      status: "open",
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

    // Click 'Actionable Patches' ribbon tab (only rev_1 has patches)
    const patchesTab = screen.getByRole("button", { name: /actionable patches/i });
    await user.click(patchesTab);

    // rev_1 should remain visible, rev_2 should be filtered out
    expect(screen.getByText("feat: autonomous remediation sentinel pipeline")).toBeInTheDocument();
    expect(screen.queryByText("docs: update API documentation")).not.toBeInTheDocument();

    // Click 'All Reviews' tab to restore
    const allTab = screen.getByRole("button", { name: /reviews scanned/i });
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

  it("keeps ribbon stats page-scoped while Reviews Scanned shows the server total", async () => {
    // Server reports 28 total but only a 2-item page is delivered: the tiles
    // must not disagree silently — page-derived tiles say "this page".
    vi.mocked(api.getCodeReviews).mockResolvedValue({ reviews: mockReviews, total: 28 });
    render(<ReviewsPage />);

    expect(await screen.findByText("feat: autonomous remediation sentinel pipeline")).toBeInTheDocument();

    const scanned = screen.getByText("Reviews Scanned").closest("button")!;
    expect(scanned).toHaveTextContent("28");

    const highRisk = screen.getByText("High Risk Flagged").closest("button")!;
    expect(highRisk).toHaveTextContent("1");
    expect(highRisk.textContent).toMatch(/this page/i);

    expect(screen.getByText("Across this page")).toBeInTheDocument();
    expect(screen.getByText(/1-click github suggestions · this page/i)).toBeInTheDocument();
  });
});
