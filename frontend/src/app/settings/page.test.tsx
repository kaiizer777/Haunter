import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import SettingsPage from "./page";
import { api, RepoOut, RepoSettingsOut } from "@/lib/api";

const mockSearchParamsGet = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    replace: vi.fn(),
    push: vi.fn(),
  }),
  useSearchParams: () => ({
    get: mockSearchParamsGet,
  }),
}));

vi.mock("@/components/layout/app-layout", () => ({
  AppLayout: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="app-layout">{children}</div>
  ),
}));

vi.mock("@/lib/api", () => ({
  api: {
    getSettingsRepos: vi.fn(),
    getRepos: vi.fn(),
    getRepoSettings: vi.fn(),
    updateRepoSettings: vi.fn(),
    applyRepoPreset: vi.fn(),
  },
}));

describe("SettingsPage (app/settings/page.tsx)", () => {
  const mockRepos: RepoOut[] = [
    {
      id: "repo_1",
      owner: "kaiizer777",
      name: "Haunter",
      default_branch: "main",
      language_hint: "python",
      active_model_config_id: null,
      created_at: new Date().toISOString(),
    },
    {
      id: "repo_2",
      owner: "kaiizer777",
      name: "Gengar",
      default_branch: "master",
      language_hint: "typescript",
      active_model_config_id: null,
      created_at: new Date().toISOString(),
    },
  ];

  const mockSettings: RepoSettingsOut = {
    id: "settings_1",
    repo_id: "repo_1",
    preset: "autonomous",
    preset_profile: "autonomous",
    enable_auto_fix: true,
    enable_auditor_mode: false,
    enable_sandbox_verification: true,
    enable_pr_comments: true,
    enable_live_sessions: true,
    enable_webcontainer_preview: true,
    audit_trigger_on_pr: true,
    audit_trigger_on_ci_failure: true,
    audit_trigger_on_ci_success: false,
    audit_trigger_on_manual_mention: true,
    allowed_branches: ["main"],
    ignore_draft_prs: true,
    max_cost_per_run_cents: 100,
    model_override_scope: "inherit",
    settings_version: 1,
  };

  beforeEach(() => {
    vi.clearAllMocks();
    mockSearchParamsGet.mockReturnValue(null);
    vi.mocked(api.getSettingsRepos).mockResolvedValue([]);
    vi.mocked(api.getRepos).mockResolvedValue(mockRepos);
    vi.mocked(api.getRepoSettings).mockResolvedValue(mockSettings);
  });

  it("loads repositories and updates URL query param ?repo_id=<id> on initial repo selection", async () => {
    const replaceStateSpy = vi.spyOn(window.history, "replaceState");

    render(<SettingsPage />);

    await waitFor(() => {
      expect(replaceStateSpy).toHaveBeenCalledWith(
        null,
        "",
        expect.stringContaining("repo_id=repo_1")
      );
    });
  });

  it("respects repo_id from URL search params on mount", async () => {
    mockSearchParamsGet.mockImplementation((param: string) =>
      param === "repo_id" ? "repo_2" : null
    );

    render(<SettingsPage />);

    await waitFor(() => {
      expect(api.getRepoSettings).toHaveBeenCalledWith("repo_2");
    });
  });

  it("updates URL query parameter ?repo_id=<id> via window.history.replaceState when selecting a repo", async () => {
    const replaceStateSpy = vi.spyOn(window.history, "replaceState");

    render(<SettingsPage />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /repository selector/i })).not.toBeDisabled();
    });

    const selector = screen.getByRole("button", { name: /repository selector/i });
    fireEvent.click(selector);

    await waitFor(() => {
      expect(screen.getByText("kaiizer777/Gengar")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("kaiizer777/Gengar"));

    await waitFor(() => {
      expect(api.getRepoSettings).toHaveBeenCalledWith("repo_2");
    });

    expect(replaceStateSpy).toHaveBeenCalledWith(
      null,
      "",
      expect.stringContaining("repo_id=repo_2")
    );
  });
});
