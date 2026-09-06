import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { Sidebar } from "./sidebar";
import { useAuth } from "@/lib/auth-context";
import { api, AuthUser, ModelConfigOut } from "@/lib/api";

let currentPathname = "/runs";

vi.mock("next/navigation", () => ({
  usePathname: () => currentPathname,
}));

vi.mock("@/lib/auth-context", () => ({
  useAuth: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    getModelConfig: vi.fn(),
  },
}));

describe("sidebar.tsx", () => {
  const standardUser: AuthUser = {
    id: "usr_1",
    github_id: 111,
    github_username: "regular-user",
    avatar_url: null,
    is_admin: false,
  };

  const adminUser: AuthUser = {
    id: "usr_2",
    github_id: 222,
    github_username: "admin-user",
    avatar_url: "https://example.com/avatar.png",
    is_admin: true,
  };

  const mockModelConfig: ModelConfigOut = {
    id: "cfg_live",
    provider: "anthropic",
    model_name: "claude-sonnet-4-5",
    base_url: "https://opencode.ai/zen/v1",
    is_active: true,
  };

  beforeEach(() => {
    vi.clearAllMocks();
    currentPathname = "/runs";
    vi.mocked(api.getModelConfig).mockResolvedValue(mockModelConfig);
    vi.mocked(useAuth).mockReturnValue({
      user: standardUser,
      loading: false,
      error: null,
      logout: vi.fn(),
      refresh: vi.fn(),
    });
  });

  it("renders the brand header with link to /runs and system status", async () => {
    render(<Sidebar />);

    const brandLink = screen.getByRole("link", { name: /haunter autonomous ci/i });
    expect(brandLink).toHaveAttribute("href", "/runs");
    expect(screen.getByText("v1.0")).toBeInTheDocument();
    expect(screen.getByText("Pipeline Live")).toBeInTheDocument();
    await screen.findByTitle("claude-sonnet-4-5");
  });

  it("renders navigation links for non-admin user (hides Eval Harness)", async () => {
    render(<Sidebar />);

    const runsLink = screen.getByRole("link", { name: /runs/i });
    const reposLink = screen.getByRole("link", { name: /repositories/i });
    const configLink = screen.getByRole("link", { name: /model config/i });

    expect(runsLink).toHaveAttribute("href", "/runs");
    expect(reposLink).toHaveAttribute("href", "/repos");
    expect(configLink).toHaveAttribute("href", "/config");
    expect(screen.getByText("Live")).toBeInTheDocument();

    // Eval Harness must NOT be present for non-admin
    expect(screen.queryByText(/eval harness/i)).not.toBeInTheDocument();
    await screen.findByTitle("claude-sonnet-4-5");
  });

  it("renders Eval Harness with Admin badge when user.is_admin is true", async () => {
    vi.mocked(useAuth).mockReturnValue({
      user: adminUser,
      loading: false,
      error: null,
      logout: vi.fn(),
      refresh: vi.fn(),
    });

    render(<Sidebar />);

    const evalLink = screen.getByRole("link", { name: /eval harness/i });
    expect(evalLink).toBeInTheDocument();
    expect(evalLink).toHaveAttribute("href", "/eval");
    expect(screen.getByText("Admin")).toBeInTheDocument();
    await screen.findByTitle("claude-sonnet-4-5");
  });

  it("highlights the active link matching the current route", async () => {
    currentPathname = "/runs";
    const { rerender } = render(<Sidebar />);

    const runsLink = screen.getByRole("link", { name: /runs/i });
    const reposLink = screen.getByRole("link", { name: /repositories/i });

    expect(runsLink.className).toContain("bg-zinc-800/90");
    expect(runsLink.className).toContain("text-zinc-100");
    expect(reposLink.className).toContain("text-zinc-400");
    expect(reposLink.className).not.toContain("bg-zinc-800/90");

    await screen.findByTitle("claude-sonnet-4-5");

    // Change route to /repos
    currentPathname = "/repos";
    rerender(<Sidebar />);

    const updatedReposLink = screen.getByRole("link", { name: /repositories/i });
    const updatedRunsLink = screen.getByRole("link", { name: /runs/i });

    expect(updatedReposLink.className).toContain("bg-zinc-800/90");
    expect(updatedRunsLink.className).not.toContain("bg-zinc-800/90");
  });

  it("highlights parent route for nested subpaths (e.g. /runs/detail matches /runs)", async () => {
    currentPathname = "/runs/detail";
    render(<Sidebar />);

    const runsLink = screen.getByRole("link", { name: /runs/i });
    expect(runsLink.className).toContain("bg-zinc-800/90");
    expect(runsLink.className).toContain("text-zinc-100");
    await screen.findByTitle("claude-sonnet-4-5");
  });

  it("updates the active model name from api.getModelConfig()", async () => {
    vi.mocked(api.getModelConfig).mockResolvedValue({
      id: "cfg_free",
      provider: "opencode_zen",
      model_name: "laguna-s-2.1-free",
      base_url: "https://opencode.ai/zen/v1",
      is_active: true,
    });

    render(<Sidebar />);

    // Strips '-free' suffix: 'laguna-s-2.1-free' -> 'laguna-s-2.1'
    expect(await screen.findByText("laguna-s-2.1")).toBeInTheDocument();
  });

  it("handles api.getModelConfig() failure gracefully and keeps default model", async () => {
    vi.mocked(api.getModelConfig).mockRejectedValue(new Error("API network error"));

    render(<Sidebar />);

    // Default fallback is nemotron-3.5-lightning (stripped of -free)
    expect(screen.getByText("nemotron-3.5-lightning")).toBeInTheDocument();
  });

  it("ignores model config update if unmounted before API resolves", async () => {
    let resolvePromise: (val: ModelConfigOut) => void;
    const delayedPromise = new Promise<ModelConfigOut>((resolve) => {
      resolvePromise = resolve;
    });
    vi.mocked(api.getModelConfig).mockReturnValue(delayedPromise);

    const { unmount } = render(<Sidebar />);
    unmount();

    // Resolving after unmount should not trigger state update error
    resolvePromise!(mockModelConfig);
  });
});
