import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import ModelConfigPage from "./page";
import { useAuth } from "@/lib/auth-context";
import { api, AuthUser, RepoOut } from "@/lib/api";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
}));

vi.mock("@/components/layout/app-layout", () => ({
  AppLayout: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="app-layout">{children}</div>
  ),
}));

vi.mock("@/lib/auth-context", () => ({
  useAuth: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    getAvailableModels: vi.fn(),
    getRepos: vi.fn(),
    getModelConfig: vi.fn(),
    updateModelConfig: vi.fn(),
  },
  ApiError: class ApiError extends Error {
    status: number;
    constructor(message: string, status = 500) {
      super(message);
      this.status = status;
    }
  },
}));

const MODEL_VOCAB_RE =
  /provider|opencode|openai|deepseek|llama|qwen|glm|mimo|gemma|muse|model card|context window|Default · Free|inference engine|nemotron|laguna|gpt|claude|groq|anthropic|base_url/i;

describe("ModelConfigPage non-admin: features only, zero model vocabulary", () => {
  const nonAdminUser: AuthUser = {
    id: "usr_1",
    github_id: 111,
    github_username: "regular-user",
    avatar_url: null,
    is_admin: false,
  };

  const repo = {
    id: "repo_1",
    owner: "octo",
    name: "hello",
    full_name: "octo/hello",
    active_model_config_id: null,
  } as unknown as RepoOut;

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(useAuth).mockReturnValue({
      user: nonAdminUser,
      loading: false,
      error: null,
      logout: vi.fn(),
      refresh: vi.fn(),
    });
    vi.mocked(api.getRepos).mockResolvedValue([repo]);
    vi.mocked(api.getAvailableModels).mockResolvedValue({} as never);
  });

  it("never calls getAvailableModels for is_admin=false", async () => {
    render(<ModelConfigPage />);
    await waitFor(() => {
      expect(api.getRepos).toHaveBeenCalled();
    });
    expect(api.getAvailableModels).not.toHaveBeenCalled();
    expect(api.getModelConfig).not.toHaveBeenCalled();
  });

  it("renders feature language with zero model vocabulary for non-admins", async () => {
    const { container } = render(<ModelConfigPage />);
    expect(await screen.findByText("Automation features")).toBeInTheDocument();
    const text = container.textContent ?? "";
    expect(text).not.toMatch(MODEL_VOCAB_RE);
  });
});
