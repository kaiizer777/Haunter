import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import WebhooksPage from "./page";
import { api, WebhookDeliveryOut } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    replace: vi.fn(),
    push: vi.fn(),
  }),
  usePathname: () => "/webhooks",
}));

vi.mock("@/lib/auth-context", () => ({
  useAuth: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    getWebhookDeliveries: vi.fn(),
    replayWebhookDelivery: vi.fn(),
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

function delivery(overrides: Partial<WebhookDeliveryOut> = {}): WebhookDeliveryOut {
  return {
    id: "del_1",
    event: "workflow_run",
    delivery_id: "gh-delivery-0001",
    status: "queued",
    reason: "github_run_id=555001 run_id=run_1",
    repo: "acme/frontend-app",
    repo_id: "repo_111",
    created_at: new Date(Date.now() - 120000).toISOString(),
    replayable: true,
    replay_of: null,
    ...overrides,
  };
}

describe("WebhooksPage (app/webhooks/page.tsx)", () => {
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
    vi.mocked(api.getWebhookDeliveries).mockResolvedValue({
      deliveries: [delivery()],
      total: 1,
    });
    vi.mocked(api.replayWebhookDelivery).mockResolvedValue({
      original_id: "del_1",
      replay_id: "del_2",
      delivery_id: "gh-delivery-0001",
      event: "workflow_run",
      decision: { status: "duplicate", delivery_id: "gh-delivery-0001" },
      replayed_at: new Date().toISOString(),
    });
  });

  it("renders loading skeletons while fetching deliveries", () => {
    vi.mocked(api.getWebhookDeliveries).mockImplementation(() => new Promise(() => {}));

    const { container } = render(<WebhooksPage />);

    expect(
      screen.getByRole("heading", { name: "Webhook Health" })
    ).toBeInTheDocument();
    expect(container.querySelectorAll(".animate-pulse").length).toBeGreaterThan(0);
  });

  it("renders empty state when no deliveries have been recorded", async () => {
    vi.mocked(api.getWebhookDeliveries).mockResolvedValue({ deliveries: [], total: 0 });

    render(<WebhooksPage />);

    expect(await screen.findByText("No webhook deliveries recorded")).toBeInTheDocument();
  });

  it("names the active filter in the empty state", async () => {
    const user = userEvent.setup();
    vi.mocked(api.getWebhookDeliveries).mockResolvedValue({ deliveries: [], total: 0 });

    render(<WebhooksPage />);
    await screen.findByText("No webhook deliveries recorded");

    await user.click(screen.getByRole("button", { name: "push" }));

    expect(await screen.findByText(/No push deliveries/)).toBeInTheDocument();
    expect(api.getWebhookDeliveries).toHaveBeenCalledWith({
      event: "push",
      limit: 25,
      offset: 0,
    });
  });

  it("renders event, repository, decision, reason and timestamp per row", async () => {
    render(<WebhooksPage />);

    expect(await screen.findByText("workflow_run")).toBeInTheDocument();
    expect(screen.getByText("gh-delivery-0001")).toBeInTheDocument();
    expect(screen.getByText("acme/frontend-app")).toBeInTheDocument();
    expect(screen.getByText("Queued")).toBeInTheDocument();
    expect(
      screen.getByText("github_run_id=555001 run_id=run_1")
    ).toBeInTheDocument();
    expect(screen.getByText("2m ago")).toBeInTheDocument();
  });

  it("labels non-queued decisions with their reason", async () => {
    vi.mocked(api.getWebhookDeliveries).mockResolvedValue({
      deliveries: [
        delivery({
          id: "del_9",
          status: "duplicate",
          reason: "github_run_id=555001",
        }),
      ],
      total: 1,
    });

    render(<WebhooksPage />);

    expect(await screen.findByText("Duplicate")).toBeInTheDocument();
  });

  it("disables replay for deliveries whose payload was not retained", async () => {
    vi.mocked(api.getWebhookDeliveries).mockResolvedValue({
      deliveries: [delivery({ id: "del_3", replayable: false })],
      total: 1,
    });

    render(<WebhooksPage />);

    const replayButton = await screen.findByRole("button", { name: /replay/i });
    expect(replayButton).toBeDisabled();
    expect(replayButton).toHaveAttribute(
      "title",
      expect.stringContaining("too large to retain")
    );
  });

  it("replays a delivery and reports the decision the handler reached", async () => {
    const user = userEvent.setup();
    render(<WebhooksPage />);

    await screen.findByText("gh-delivery-0001");
    await user.click(screen.getByRole("button", { name: /replay/i }));

    expect(api.replayWebhookDelivery).toHaveBeenCalledWith("del_1");
    expect(
      await screen.findByText(
        "Replay of gh-delivery-0001 → duplicate"
      )
    ).toBeInTheDocument();
  });

  it("surfaces a failed replay without dropping the row", async () => {
    const user = userEvent.setup();
    vi.mocked(api.replayWebhookDelivery).mockRejectedValue(
      new Error("This delivery was already replayed in the last 30s.")
    );

    render(<WebhooksPage />);

    await screen.findByText("gh-delivery-0001");
    await user.click(screen.getByRole("button", { name: /replay/i }));

    expect(
      await screen.findByText(
        "Replay failed: This delivery was already replayed in the last 30s."
      )
    ).toBeInTheDocument();
    expect(screen.getByText("gh-delivery-0001")).toBeInTheDocument();
  });

  it("renders an error state when fetching deliveries fails", async () => {
    vi.mocked(api.getWebhookDeliveries).mockRejectedValue(
      new Error("Failed to connect to database")
    );

    render(<WebhooksPage />);

    expect(await screen.findByText("Failed to connect to database")).toBeInTheDocument();
    // A failed request must not also claim the account has no deliveries.
    expect(screen.queryByText("No webhook deliveries recorded")).not.toBeInTheDocument();
  });

  it("gives each row's replay button a name that identifies its delivery", async () => {
    render(<WebhooksPage />);

    await screen.findByText("gh-delivery-0001");
    expect(
      screen.getByRole("button", { name: "Replay delivery gh-delivery-0001" })
    ).toBeInTheDocument();
  });

  it("explains in the accessible name why a delivery cannot be replayed", async () => {
    vi.mocked(api.getWebhookDeliveries).mockResolvedValue({
      deliveries: [delivery({ id: "del_3", replayable: false })],
      total: 1,
    });

    render(<WebhooksPage />);

    // A disabled control never receives focus, so the reason has to be in the name.
    const button = await screen.findByRole("button", {
      name: /cannot be replayed: payload was not retained/,
    });
    expect(button).toBeDisabled();
  });

  it("paginates beyond the first page and reloads after a replay", async () => {
    const user = userEvent.setup();
    const page = (offset: number) =>
      offset === 0
        ? { deliveries: [delivery()], total: 26 }
        : { deliveries: [delivery({ id: "del_p2", delivery_id: "gh-p2" })], total: 26 };
    vi.mocked(api.getWebhookDeliveries).mockImplementation((params) =>
      Promise.resolve(page(params?.offset ?? 0))
    );

    render(<WebhooksPage />);

    expect(await screen.findByText("gh-delivery-0001")).toBeInTheDocument();
    expect(screen.getByText(/Showing 1–25 of 26/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("gh-p2")).toBeInTheDocument();
    expect(api.getWebhookDeliveries).toHaveBeenCalledWith({
      event: undefined,
      limit: 25,
      offset: 25,
    });

    await user.click(screen.getByRole("button", { name: /replay/i }));
    await waitFor(() => {
      expect(api.getWebhookDeliveries).toHaveBeenCalledWith({
        event: undefined,
        limit: 25,
        offset: 25,
      });
    });
  });
});