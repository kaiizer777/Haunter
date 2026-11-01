import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import ModelConfigPage from "./page";
import { useAuth } from "@/lib/auth-context";
import { api, ApiError, AuthUser, ModelConfigOut, RepoOut } from "@/lib/api";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
}));

vi.mock("@/components/layout/app-layout", () => ({
  AppLayout: ({
    children,
    title,
    subtitle,
  }: {
    children: React.ReactNode;
    title?: React.ReactNode;
    subtitle?: React.ReactNode;
  }) => (
    <div data-testid="app-layout">
      <h1>{title}</h1>
      <p>{subtitle}</p>
      {children}
    </div>
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
    deleteRepoModelConfig: vi.fn(),
    validateCustomProvider: vi.fn(),
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

describe("ModelConfigPage provider pruning + admin custom provider", () => {
  const adminUser: AuthUser = {
    id: "usr_admin",
    github_id: 1,
    github_username: "admin-user",
    avatar_url: null,
    is_admin: true,
  };

  const globalCfg: ModelConfigOut = {
    id: "cfg_global",
    provider: "opencode_go",
    model_name: "deepseek-v4.1-flash",
    base_url: "https://opencode.ai/inference/go/openai/v1",
    is_active: true,
    scope: "global",
    repo_id: null,
  };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(useAuth).mockReturnValue({
      user: adminUser,
      loading: false,
      error: null,
      logout: vi.fn(),
      refresh: vi.fn(),
    });
    vi.mocked(api.getAvailableModels).mockResolvedValue({} as never);
    vi.mocked(api.getRepos).mockResolvedValue([]);
    vi.mocked(api.getModelConfig).mockImplementation((repoId?: string) => {
      if (repoId === undefined) return Promise.resolve(globalCfg);
      return Promise.reject(new ApiError("not found", 404));
    });
    vi.mocked(api.validateCustomProvider).mockResolvedValue({
      ok: false,
      supported: false,
    } as never);
  });

  it("prunes prebuilt providers to OpenCode Go + Groq", async () => {
    render(<ModelConfigPage />);
    expect(await screen.findByText("Repository Override")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Inference Provider Selector"));
    const goOptions = await screen.findAllByText("OpenCode Go");
    expect(goOptions.length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("Groq")).toBeInTheDocument();
    expect(screen.queryByText("OpenAI")).not.toBeInTheDocument();
    expect(screen.queryByText("Anthropic")).not.toBeInTheDocument();
    expect(screen.queryByText("OpenCode Zen")).not.toBeInTheDocument();
  });

  it("shows the admin custom-provider form and falls back gracefully without a backend endpoint", async () => {
    render(<ModelConfigPage />);
    const addBtn = await screen.findByRole("button", { name: /add custom provider/i });
    expect(addBtn).toBeInTheDocument();
    fireEvent.click(addBtn);

    expect(await screen.findByLabelText("Custom provider name")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Custom provider name"), {
      target: { value: "my-provider" },
    });
    fireEvent.change(screen.getByLabelText("Custom provider base URL"), {
      target: { value: "https://my-host/v1" },
    });
    fireEvent.change(screen.getByLabelText("Custom provider API key"), {
      target: { value: "sk-test" },
    });
    fireEvent.click(screen.getByRole("button", { name: /validate & discover models/i }));
    expect(await screen.findByText(/not supported by this backend/i)).toBeInTheDocument();
    expect(api.validateCustomProvider).toHaveBeenCalledWith({
      name: "my-provider",
      compat: "openai",
      base_url: "https://my-host/v1",
      api_key: "sk-test",
    });
  });

  it("hides the admin custom-provider form from non-admins", async () => {
    vi.mocked(useAuth).mockReturnValue({
      user: {
        id: "usr_1",
        github_id: 111,
        github_username: "regular-user",
        avatar_url: null,
        is_admin: false,
      },
      loading: false,
      error: null,
      logout: vi.fn(),
      refresh: vi.fn(),
    });
    render(<ModelConfigPage />);
    expect(await screen.findByText("Automation features")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /add custom provider/i })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Custom provider name")).not.toBeInTheDocument();
    expect(screen.queryByText("Repository Override")).not.toBeInTheDocument();
  });
});
