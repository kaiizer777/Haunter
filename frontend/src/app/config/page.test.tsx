import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import ModelConfigPage from "./page";
import { useAuth } from "@/lib/auth-context";
import { api, AuthUser, ModelConfigOut, RepoOut } from "@/lib/api";

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
    await screen.findByText("Automation features");
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
    vi.mocked(api.getModelConfig).mockResolvedValue(globalCfg);
    vi.mocked(api.validateCustomProvider).mockResolvedValue({
      ok: false,
      supported: false,
    } as never);
  });

  it("prunes prebuilt providers to OpenCode Go + Groq", async () => {
    render(<ModelConfigPage />);
    expect(await screen.findByText("Autonomous Inference Engine")).toBeInTheDocument();
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
    expect(api.validateCustomProvider).toHaveBeenCalledWith(
      {
        name: "my-provider",
        compat: "openai",
        base_url: "https://my-host/v1",
        api_key: "sk-test",
      },
      expect.objectContaining({ signal: expect.any(AbortSignal) })
    );
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

  it("keeps custom-provider modal closed by default and opens upon clicking Add Custom Provider", async () => {
    render(<ModelConfigPage />);
    expect(screen.queryByLabelText("Custom provider name")).not.toBeInTheDocument();
    const addBtn = await screen.findByRole("button", { name: /add custom provider/i });
    fireEvent.click(addBtn);
    expect(await screen.findByLabelText("Custom provider name")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Custom Provider" })).toBeInTheDocument();
  });

  it("closes the modal when Cancel is clicked", async () => {
    render(<ModelConfigPage />);
    const addBtn = await screen.findByRole("button", { name: /add custom provider/i });
    fireEvent.click(addBtn);
    expect(await screen.findByLabelText("Custom provider name")).toBeInTheDocument();

    const cancelBtn = screen.getByRole("button", { name: /cancel/i });
    fireEvent.click(cancelBtn);
    expect(screen.queryByLabelText("Custom provider name")).not.toBeInTheDocument();
  });

  it("displays validation error when submitting with missing required fields", async () => {
    render(<ModelConfigPage />);
    const addBtn = await screen.findByRole("button", { name: /add custom provider/i });
    fireEvent.click(addBtn);

    const validateBtn = await screen.findByRole("button", { name: /validate & discover models/i });
    fireEvent.click(validateBtn);

    expect(await screen.findByRole("alert")).toHaveTextContent("Name, base URL, and API key are required.");
    expect(api.validateCustomProvider).not.toHaveBeenCalled();
  });

  it("validates custom provider, resets form, and updates provider selection on success", async () => {
    vi.mocked(api.validateCustomProvider).mockResolvedValueOnce({
      ok: true,
      supported: true,
      provider_id: "custom_vllm",
      models: [{ id: "custom-model-1", name: "Custom Model 1", tag: "Custom" }],
    } as never);

    render(<ModelConfigPage />);
    const addBtn = await screen.findByRole("button", { name: /add custom provider/i });
    fireEvent.click(addBtn);

    fireEvent.change(await screen.findByLabelText("Custom provider name"), {
      target: { value: "vLLM Server" },
    });
    fireEvent.change(screen.getByLabelText("Custom provider base URL"), {
      target: { value: "https://vllm.internal/v1" },
    });
    fireEvent.change(screen.getByLabelText("Custom provider API key"), {
      target: { value: "sk-vllm-secret" },
    });

    fireEvent.click(screen.getByRole("button", { name: /validate & discover models/i }));

    await waitFor(() => {
      expect(screen.queryByLabelText("Custom provider name")).not.toBeInTheDocument();
    });

    expect(api.validateCustomProvider).toHaveBeenCalledWith(
      {
        name: "vLLM Server",
        compat: "openai",
        base_url: "https://vllm.internal/v1",
        api_key: "sk-vllm-secret",
      },
      expect.objectContaining({ signal: expect.any(AbortSignal) })
    );

    // Assert provider and model state were selected as expected
    expect(screen.getByRole("button", { name: /inference provider selector/i })).toHaveTextContent("vLLM Server");
    expect(screen.getByText("vLLM Server:")).toBeInTheDocument();
    expect(screen.getAllByText("Custom Model 1").length).toBeGreaterThan(0);
    expect(screen.getByRole("status")).toHaveTextContent(/discovered 1 model for vLLM Server/i);

    // Reopen modal and explicitly assert input fields were reset
    fireEvent.click(screen.getByRole("button", { name: /add custom provider/i }));
    expect(await screen.findByLabelText("Custom provider name")).toHaveValue("");
    expect(screen.getByLabelText("Custom provider base URL")).toHaveValue("");
    expect(screen.getByLabelText("Custom provider API key")).toHaveValue("");
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
  });

  it("ignores in-flight validation responses and cleans up state when modal is closed before completion", async () => {
    let capturedSignal: AbortSignal | null | undefined;
    let resolveValidation!: (value: unknown) => void;
    const pendingPromise = new Promise((resolve) => {
      resolveValidation = resolve;
    });
    vi.mocked(api.validateCustomProvider).mockImplementationOnce(
      (_data, options) => {
        capturedSignal = options?.signal;
        return pendingPromise as never;
      }
    );

    render(<ModelConfigPage />);
    const addBtn = await screen.findByRole("button", { name: /add custom provider/i });
    fireEvent.click(addBtn);

    fireEvent.change(await screen.findByLabelText("Custom provider name"), {
      target: { value: "Pending Provider" },
    });
    fireEvent.change(screen.getByLabelText("Custom provider base URL"), {
      target: { value: "https://pending.internal/v1" },
    });
    fireEvent.change(screen.getByLabelText("Custom provider API key"), {
      target: { value: "sk-pending" },
    });

    fireEvent.click(screen.getByRole("button", { name: /validate & discover models/i }));

    expect(capturedSignal).toBeDefined();
    expect(capturedSignal?.aborted).toBe(false);

    // Close the modal while validation is in flight
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));

    expect(capturedSignal?.aborted).toBe(true);

    // Resolve validation after modal was closed
    resolveValidation({
      ok: true,
      supported: true,
      provider_id: "custom_pending",
      models: [{ id: "pending-model", name: "Pending Model", tag: "Custom" }],
    });

    await waitFor(() => {
      expect(screen.queryByLabelText("Custom provider name")).not.toBeInTheDocument();
    });

    // Active provider should remain untouched, not updated to the closed pending provider
    expect(screen.getByRole("button", { name: /inference provider selector/i })).not.toHaveTextContent("Pending Provider");
  });

  it("toggles password visibility between text and password types", async () => {
    render(<ModelConfigPage />);
    const addBtn = await screen.findByRole("button", { name: /add custom provider/i });
    fireEvent.click(addBtn);

    const apiKeyInput = await screen.findByLabelText("Custom provider API key");
    expect(apiKeyInput).toHaveAttribute("type", "password");

    const showBtn = screen.getByRole("button", { name: /show api key/i });
    fireEvent.click(showBtn);
    expect(apiKeyInput).toHaveAttribute("type", "text");

    const hideBtn = screen.getByRole("button", { name: /hide api key/i });
    fireEvent.click(hideBtn);
    expect(apiKeyInput).toHaveAttribute("type", "password");
  });

  it("populates Base URL when preset button is clicked", async () => {
    render(<ModelConfigPage />);
    const addBtn = await screen.findByRole("button", { name: /add custom provider/i });
    fireEvent.click(addBtn);

    const baseUrlInput = await screen.findByLabelText("Custom provider base URL");
    expect(baseUrlInput).toHaveValue("");

    // Test Together AI preset (SSRF-safe public API)
    const togetherBtn = screen.getByRole("button", { name: /^together ai$/i });
    fireEvent.click(togetherBtn);
    expect(baseUrlInput).toHaveValue("https://api.together.xyz/v1");

    // Test OpenAI preset
    const openaiPresetBtn = screen.getByRole("button", { name: /^openai$/i });
    fireEvent.click(openaiPresetBtn);
    expect(baseUrlInput).toHaveValue("https://api.openai.com/v1");
  });

  it("switches compatibility mode between OpenAI and Anthropic using segmented control", async () => {
    render(<ModelConfigPage />);
    const addBtn = await screen.findByRole("button", { name: /add custom provider/i });
    fireEvent.click(addBtn);

    const openaiTab = await screen.findByRole("tab", { name: /openai/i });
    const anthropicTab = screen.getByRole("tab", { name: /anthropic/i });
    const compatSelect = screen.getByLabelText("Custom provider compatibility");

    expect(openaiTab).toHaveAttribute("aria-selected", "true");
    expect(anthropicTab).toHaveAttribute("aria-selected", "false");
    expect(compatSelect).toHaveValue("openai");

    fireEvent.click(anthropicTab);
    expect(openaiTab).toHaveAttribute("aria-selected", "false");
    expect(anthropicTab).toHaveAttribute("aria-selected", "true");
    expect(compatSelect).toHaveValue("anthropic");

    fireEvent.click(openaiTab);
    expect(openaiTab).toHaveAttribute("aria-selected", "true");
    expect(anthropicTab).toHaveAttribute("aria-selected", "false");
    expect(compatSelect).toHaveValue("openai");
  });

  it("renders protocol badge and copy URL button for active custom provider", async () => {
    vi.mocked(api.validateCustomProvider).mockResolvedValueOnce({
      ok: true,
      supported: true,
      provider_id: "custom_openai_ep",
      models: [{ id: "custom-ep-model", name: "Custom EP Model", tag: "Custom" }],
    } as never);

    render(<ModelConfigPage />);
    const addBtn = await screen.findByRole("button", { name: /add custom provider/i });
    fireEvent.click(addBtn);

    fireEvent.change(await screen.findByLabelText("Custom provider name"), {
      target: { value: "OpenAI Proxy" },
    });
    fireEvent.change(screen.getByLabelText("Custom provider base URL"), {
      target: { value: "https://proxy.internal/v1" },
    });
    fireEvent.change(screen.getByLabelText("Custom provider API key"), {
      target: { value: "sk-proxy-key" },
    });

    fireEvent.click(screen.getByRole("button", { name: /validate & discover models/i }));

    await waitFor(() => {
      expect(screen.queryByLabelText("Custom provider name")).not.toBeInTheDocument();
    });

    expect(screen.getByText("OpenAI Proxy:")).toBeInTheDocument();
    expect(screen.getByText("OpenAI Compatible")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /copy base url/i })).toBeInTheDocument();
  });

  it("renders model token cost badges and does not render live in prod badges", async () => {
    render(<ModelConfigPage />);
    expect(await screen.findByText("$0.14 / 1M")).toBeInTheDocument();
    expect(screen.getByText("$0.00 / 1M")).toBeInTheDocument();
    expect(screen.queryByText(/live in prod/i)).not.toBeInTheDocument();
  });

  it("applies crisp hairline white focus border styling without ring doubling to custom modal inputs", async () => {
    render(<ModelConfigPage />);
    const addBtn = await screen.findByRole("button", { name: /add custom provider/i });
    fireEvent.click(addBtn);

    const nameInput = await screen.findByLabelText("Custom provider name");
    const urlInput = screen.getByLabelText("Custom provider base URL");
    const keyInput = screen.getByLabelText("Custom provider API key");

    for (const input of [nameInput, urlInput, keyInput]) {
      expect(input.className).toContain("focus:border-white/90");
      expect(input.className).toContain("focus:border-t-white/90");
      expect(input.className).toContain("focus:border-x-white/90");
      expect(input.className).toContain("focus:border-b-white/90");
      expect(input.className).toContain("focus:ring-1");
      expect(input.className).toContain("focus:ring-white/40");
    }
  });
it("does not render server-derived base URL or subagent pipeline telemetry cards", async () => {
    render(<ModelConfigPage />);
    expect(await screen.findByText("OpenCode Go")).toBeInTheDocument();
    expect(screen.queryByText(/inference base url \(server-derived\)/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/subagent pipeline telemetry/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/tls 1\.3 verified/i)).not.toBeInTheDocument();
  });
it("does not render top scope switcher card", async () => {
    render(<ModelConfigPage />);
    expect(await screen.findByText("Autonomous Inference Engine")).toBeInTheDocument();
    expect(screen.queryByText(/Scope:/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Global Platform Default/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Repository Override/i)).not.toBeInTheDocument();
  });
});

