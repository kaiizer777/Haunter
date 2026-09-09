import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ReposPage from "./page";
import { api, RepoOut } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    replace: vi.fn(),
    push: vi.fn(),
  }),
  usePathname: () => "/repos",
}));

vi.mock("@/lib/auth-context", () => ({
  useAuth: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    getRepos: vi.fn(),
    removeRepo: vi.fn(),
    getAvailableRepos: vi.fn(),
    addRepo: vi.fn(),
    getModelConfig: vi.fn().mockResolvedValue({
      id: "cfg_1",
      provider: "opencode_zen",
      model_name: "nemotron-3.5-lightning-free",
      base_url: "https://opencode.ai/zen/v1",
      is_active: true,
    }),
  },
  ApiError: class ApiError extends Error {
    status: number;
    constructor(message: string, status: number) {
      super(message);
      this.name = "ApiError";
      this.status = status;
    }
  },
}));

describe("ReposPage (app/repos/page.tsx)", () => {
  const mockRepos: RepoOut[] = [
    {
      id: "repo_111",
      owner: "acme",
      name: "frontend-app",
      default_branch: "main",
      language_hint: "typescript",
      active_model_config_id: null,
      created_at: new Date(Date.now() - 3600000).toISOString(),
    },
    {
      id: "repo_222",
      owner: "acme",
      name: "backend-service",
      default_branch: "dev",
      language_hint: null,
      active_model_config_id: null,
      created_at: new Date(Date.now() - 86400000).toISOString(),
    },
  ];

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(useAuth).mockReturnValue({
      user: {
        id: "usr_admin",
        github_id: 999,
        github_username: "acme-admin",
        avatar_url: null,
        is_admin: true,
      },
      loading: false,
      error: null,
      logout: vi.fn(),
      refresh: vi.fn(),
    });
    vi.mocked(api.getRepos).mockResolvedValue(mockRepos);
    vi.mocked(api.getAvailableRepos).mockResolvedValue([]);
  });

  it("renders loading skeletons while fetching repositories", () => {
    vi.mocked(api.getRepos).mockImplementation(() => new Promise(() => {}));

    const { container } = render(<ReposPage />);

    expect(screen.getByRole("heading", { name: "Connected Repositories" })).toBeInTheDocument();
    const skeletons = container.querySelectorAll(".animate-pulse");
    expect(skeletons.length).toBeGreaterThan(0);
  });

  it("renders empty state when there are no connected repositories", async () => {
    vi.mocked(api.getRepos).mockResolvedValue([]);

    render(<ReposPage />);

    expect(await screen.findByText("No repositories connected")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /connect first repository/i })).toBeInTheDocument();
  });

  it("renders table rows with repository metadata", async () => {
    render(<ReposPage />);

    expect(await screen.findByText("acme/frontend-app")).toBeInTheDocument();
    expect(screen.getByText("acme/backend-service")).toBeInTheDocument();

    // Default branch badges
    expect(screen.getByText("main")).toBeInTheDocument();
    expect(screen.getByText("dev")).toBeInTheDocument();

    // Language hints
    expect(screen.getAllByText("typescript").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("auto-detect")).toBeInTheDocument();

    // External link to GitHub
    const githubLinks = screen.getAllByRole("link", { name: /Open repository on GitHub/i });
    expect(githubLinks[0]).toHaveAttribute("href", "https://github.com/acme/frontend-app");
    expect(githubLinks[1]).toHaveAttribute("href", "https://github.com/acme/backend-service");
  });

  it("opens AddRepoModal when 'Connect Repository' button is clicked", async () => {
    const user = userEvent.setup();
    render(<ReposPage />);

    await screen.findByText("acme/frontend-app");
    const connectBtn = screen.getByRole("button", { name: /connect repository/i });

    await user.click(connectBtn);
    expect(await screen.findByRole("heading", { name: "Connect Repository" })).toBeInTheDocument();
  });

  it("cancels repository disconnection if user declines confirmation prompt", async () => {
    const user = userEvent.setup();
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);

    render(<ReposPage />);

    await screen.findByText("acme/frontend-app");
    const deleteButtons = screen.getAllByTitle("Disconnect repo");

    await user.click(deleteButtons[0]);

    expect(confirmSpy).toHaveBeenCalledWith("Are you sure you want to disconnect acme/frontend-app?");
    expect(api.removeRepo).not.toHaveBeenCalled();
    expect(screen.getByText("acme/frontend-app")).toBeInTheDocument();
  });

  it("disconnects repository when confirmed by user", async () => {
    const user = userEvent.setup();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.mocked(api.removeRepo).mockResolvedValue(undefined);

    render(<ReposPage />);

    await screen.findByText("acme/frontend-app");
    const deleteButtons = screen.getAllByTitle("Disconnect repo");

    await user.click(deleteButtons[0]);

    expect(api.removeRepo).toHaveBeenCalledWith("repo_111");
    await waitFor(() => {
      expect(screen.queryByText("acme/frontend-app")).not.toBeInTheDocument();
    });
    expect(screen.getByText("acme/backend-service")).toBeInTheDocument();
  });

  it("shows alert when disconnect repository fails", async () => {
    const user = userEvent.setup();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const alertSpy = vi.spyOn(window, "alert").mockImplementation(() => {});
    vi.mocked(api.removeRepo).mockRejectedValue(new Error("Server error"));

    render(<ReposPage />);

    await screen.findByText("acme/frontend-app");
    const deleteButtons = screen.getAllByTitle("Disconnect repo");

    await user.click(deleteButtons[0]);

    expect(api.removeRepo).toHaveBeenCalledWith("repo_111");
    await waitFor(() => {
      expect(alertSpy).toHaveBeenCalledWith("Failed to disconnect repository.");
    });
    expect(screen.getByText("acme/frontend-app")).toBeInTheDocument();
  });

  it("renders error message if fetching repositories fails", async () => {
    vi.mocked(api.getRepos).mockRejectedValue(new Error("Failed to connect to database"));

    render(<ReposPage />);

    expect(await screen.findByText("Failed to connect to database")).toBeInTheDocument();
  });
});
