import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AddRepoModal } from "./add-repo-modal";
import { api, AvailableRepoOut, RepoOut, ApiError } from "@/lib/api";

vi.mock("@/lib/api", () => ({
  api: {
    getAvailableRepos: vi.fn(),
    addRepo: vi.fn(),
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

describe("add-repo-modal.tsx", () => {
  const mockOnClose = vi.fn();
  const mockOnSuccess = vi.fn();

  const mockAvailableRepos: AvailableRepoOut[] = [
    {
      owner: "owner1",
      name: "haunter-core",
      full_name: "owner1/haunter-core",
      private: false,
      default_branch: "main",
      language: "TypeScript",
      updated_at: new Date().toISOString(),
      already_connected: false,
      permissions_push: true,
    },
    {
      owner: "owner1",
      name: "haunter-infra",
      full_name: "owner1/haunter-infra",
      private: true,
      default_branch: "master",
      language: "Python",
      updated_at: new Date(Date.now() - 86400000).toISOString(),
      already_connected: true,
      permissions_push: true,
    },
    {
      owner: "owner2",
      name: "demo-repo",
      full_name: "owner2/demo-repo",
      private: false,
      default_branch: null,
      language: null,
      updated_at: null,
      already_connected: false,
      permissions_push: true,
    },
  ];

  const mockAddedRepo: RepoOut = {
    id: "repo_new_99",
    owner: "owner1",
    name: "haunter-core",
    default_branch: "main",
    language_hint: "typescript",
    active_model_config_id: null,
    created_at: new Date().toISOString(),
  };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getAvailableRepos).mockResolvedValue(mockAvailableRepos);
    vi.mocked(api.addRepo).mockResolvedValue(mockAddedRepo);
  });

  it("does not render modal contents when isOpen=false", () => {
    render(
      <AddRepoModal
        isOpen={false}
        onClose={mockOnClose}
        onSuccess={mockOnSuccess}
      />
    );

    expect(screen.queryByText("Connect Repository")).not.toBeInTheDocument();
    expect(api.getAvailableRepos).not.toHaveBeenCalled();
  });

  it("renders loading skeleton state when isOpen=true while fetching", () => {
    vi.mocked(api.getAvailableRepos).mockImplementation(() => new Promise(() => {}));

    const { container } = render(
      <AddRepoModal
        isOpen={true}
        onClose={mockOnClose}
        onSuccess={mockOnSuccess}
      />
    );

    expect(screen.getByText("Connect Repository")).toBeInTheDocument();
    const searchInput = screen.getByPlaceholderText("Search repos...");
    expect(searchInput).toBeDisabled();

    // Skeletons are rendered
    const skeletons = container.querySelectorAll(".animate-pulse");
    expect(skeletons.length).toBeGreaterThan(0);
  });

  it("renders the list of available repositories with status and metadata", async () => {
    const { container } = render(
      <AddRepoModal
        isOpen={true}
        onClose={mockOnClose}
        onSuccess={mockOnSuccess}
      />
    );

    expect(await screen.findByText("owner1/haunter-core")).toBeInTheDocument();
    expect(screen.getByText("owner1/haunter-infra")).toBeInTheDocument();
    expect(screen.getByText("owner2/demo-repo")).toBeInTheDocument();

    // Private badge
    expect(screen.getByText("private")).toBeInTheDocument();

    // Languages and branches
    expect(screen.getByText("TypeScript")).toBeInTheDocument();
    expect(screen.getByText("Python")).toBeInTheDocument();
    expect(screen.getByText("main")).toBeInTheDocument();
    expect(screen.getByText("master")).toBeInTheDocument();

    // Already connected shows 'Connected', no connect button
    expect(screen.getByText("Connected")).toBeInTheDocument();

    // Unconnected repos have connect button with specific IDs
    expect(container.querySelector("#connect-owner1-haunter-core")).toBeInTheDocument();
    expect(container.querySelector("#connect-owner2-demo-repo")).toBeInTheDocument();
  });

  it("filters repositories based on search input query", async () => {
    const user = userEvent.setup();
    render(
      <AddRepoModal
        isOpen={true}
        onClose={mockOnClose}
        onSuccess={mockOnSuccess}
      />
    );

    await screen.findByText("owner1/haunter-core");
    const searchInput = screen.getByPlaceholderText("Search repos...");

    await user.type(searchInput, "infra");
    expect(screen.getByText("owner1/haunter-infra")).toBeInTheDocument();
    expect(screen.queryByText("owner1/haunter-core")).not.toBeInTheDocument();
    expect(screen.queryByText("owner2/demo-repo")).not.toBeInTheDocument();

    // Unmatched search
    await user.clear(searchInput);
    await user.type(searchInput, "nonexistent");
    expect(screen.getByText("No repos match your search.")).toBeInTheDocument();
  });

  it("shows empty state when no repositories are returned", async () => {
    vi.mocked(api.getAvailableRepos).mockResolvedValue([]);

    render(
      <AddRepoModal
        isOpen={true}
        onClose={mockOnClose}
        onSuccess={mockOnSuccess}
      />
    );

    expect(await screen.findByText("No repositories with push access found.")).toBeInTheDocument();
  });

  it("successfully connects a repository and closes modal", async () => {
    const user = userEvent.setup();
    const { container } = render(
      <AddRepoModal
        isOpen={true}
        onClose={mockOnClose}
        onSuccess={mockOnSuccess}
      />
    );

    await screen.findByText("owner1/haunter-core");
    const connectBtn = container.querySelector("#connect-owner1-haunter-core") as HTMLButtonElement;
    expect(connectBtn).not.toBeNull();

    await user.click(connectBtn);

    expect(api.addRepo).toHaveBeenCalledTimes(1);
    expect(api.addRepo).toHaveBeenCalledWith({
      owner: "owner1",
      name: "haunter-core",
      default_branch: "main",
      language_hint: "typescript",
    });

    expect(mockOnSuccess).toHaveBeenCalledWith(mockAddedRepo);
    expect(mockOnClose).toHaveBeenCalledTimes(1);
  });

  it("handles fallback default branch 'main' when repo.default_branch is null", async () => {
    const user = userEvent.setup();
    const { container } = render(
      <AddRepoModal
        isOpen={true}
        onClose={mockOnClose}
        onSuccess={mockOnSuccess}
      />
    );

    await screen.findByText("owner2/demo-repo");
    const connectBtn = container.querySelector("#connect-owner2-demo-repo") as HTMLButtonElement;
    expect(connectBtn).not.toBeNull();

    await user.click(connectBtn);

    expect(api.addRepo).toHaveBeenCalledWith({
      owner: "owner2",
      name: "demo-repo",
      default_branch: "main",
      language_hint: null,
    });
  });

  it("displays error banner and keeps modal open when api.addRepo fails", async () => {
    const user = userEvent.setup();
    vi.mocked(api.addRepo).mockRejectedValue(new Error("Repo already connected"));

    const { container } = render(
      <AddRepoModal
        isOpen={true}
        onClose={mockOnClose}
        onSuccess={mockOnSuccess}
      />
    );

    await screen.findByText("owner1/haunter-core");
    const connectBtn = container.querySelector("#connect-owner1-haunter-core") as HTMLButtonElement;
    expect(connectBtn).not.toBeNull();

    await user.click(connectBtn);

    expect(await screen.findByText("Repo already connected")).toBeInTheDocument();
    expect(mockOnClose).not.toHaveBeenCalled();
    expect(mockOnSuccess).not.toHaveBeenCalled();
  });

  it("renders 401 re-login warning when getAvailableRepos returns 401 ApiError", async () => {
    vi.mocked(api.getAvailableRepos).mockRejectedValue(new ApiError("Unauthorized", 401));

    render(
      <AddRepoModal
        isOpen={true}
        onClose={mockOnClose}
        onSuccess={mockOnSuccess}
      />
    );

    expect(await screen.findByText(/please re-login to grant repo access/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /re-login →/i })).toBeInTheDocument();
  });

  it("renders 429 rate limit error with retry button when getAvailableRepos returns 429", async () => {
    const user = userEvent.setup();
    vi.mocked(api.getAvailableRepos)
      .mockRejectedValueOnce(new ApiError("Rate limited", 429))
      .mockResolvedValueOnce(mockAvailableRepos);

    render(
      <AddRepoModal
        isOpen={true}
        onClose={mockOnClose}
        onSuccess={mockOnSuccess}
      />
    );

    expect(await screen.findByText(/github rate limit exceeded\. retry in 60s\./i)).toBeInTheDocument();
    const retryBtn = screen.getByRole("button", { name: /try again/i });

    await user.click(retryBtn);

    expect(api.getAvailableRepos).toHaveBeenCalledTimes(2);
    expect(await screen.findByText("owner1/haunter-core")).toBeInTheDocument();
  });

  it("renders generic error message when getAvailableRepos throws an error", async () => {
    vi.mocked(api.getAvailableRepos).mockRejectedValue(new Error("Failed to fetch available repos"));

    render(
      <AddRepoModal
        isOpen={true}
        onClose={mockOnClose}
        onSuccess={mockOnSuccess}
      />
    );

    expect(await screen.findByText("Failed to load repositories.")).toBeInTheDocument();
  });

  it("refreshes available repos when the footer Refresh button is clicked", async () => {
    const user = userEvent.setup();
    render(
      <AddRepoModal
        isOpen={true}
        onClose={mockOnClose}
        onSuccess={mockOnSuccess}
      />
    );

    await screen.findByText("owner1/haunter-core");
    const refreshBtn = screen.getByRole("button", { name: /refresh/i });

    await user.click(refreshBtn);
    expect(api.getAvailableRepos).toHaveBeenCalledTimes(2);
  });

  it("calls onClose when Escape key or close button is pressed", async () => {
    const user = userEvent.setup();
    render(
      <AddRepoModal
        isOpen={true}
        onClose={mockOnClose}
        onSuccess={mockOnSuccess}
      />
    );

    await screen.findByText("Connect Repository");
    await user.keyboard("{Escape}");
    expect(mockOnClose).toHaveBeenCalled();
  });
});
