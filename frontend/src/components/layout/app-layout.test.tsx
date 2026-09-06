import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { AppLayout } from "./app-layout";
import { useAuth } from "@/lib/auth-context";
import { AuthUser } from "@/lib/api";

const mockReplace = vi.fn();
const mockPush = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    replace: mockReplace,
    push: mockPush,
  }),
  usePathname: () => "/runs",
}));

vi.mock("@/lib/auth-context", () => ({
  useAuth: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    getModelConfig: vi.fn().mockResolvedValue({
      id: "cfg_default",
      provider: "opencode_zen",
      model_name: "nemotron-3.5-lightning-free",
      base_url: "https://opencode.ai/zen/v1",
      is_active: true,
    }),
  },
}));

describe("app-layout.tsx", () => {
  const mockUser: AuthUser = {
    id: "usr_456",
    github_id: 12345,
    github_username: "solodev",
    avatar_url: "https://avatars.githubusercontent.com/u/12345",
    is_admin: false,
  };

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders loading skeletons while auth state is loading", () => {
    vi.mocked(useAuth).mockReturnValue({
      user: null,
      loading: true,
      error: null,
      logout: vi.fn(),
      refresh: vi.fn(),
    });

    const { container } = render(
      <AppLayout title="Runs">
        <div data-testid="protected-content">Secret content</div>
      </AppLayout>
    );

    expect(screen.queryByTestId("protected-content")).not.toBeInTheDocument();
    expect(mockReplace).not.toHaveBeenCalled();
    // Skeleton blocks exist in DOM
    const skeletons = container.querySelectorAll(".animate-pulse");
    expect(skeletons.length).toBeGreaterThan(0);
  });

  it("redirects unauthenticated user to /login and returns null", () => {
    vi.mocked(useAuth).mockReturnValue({
      user: null,
      loading: false,
      error: null,
      logout: vi.fn(),
      refresh: vi.fn(),
    });

    const { container } = render(
      <AppLayout title="Runs">
        <div data-testid="protected-content">Secret content</div>
      </AppLayout>
    );

    expect(mockReplace).toHaveBeenCalledTimes(1);
    expect(mockReplace).toHaveBeenCalledWith("/login");
    expect(screen.queryByTestId("protected-content")).not.toBeInTheDocument();
    expect(container.firstChild).toBeNull();
  });

  it("renders sidebar, topbar, and main children when user is authenticated", () => {
    vi.mocked(useAuth).mockReturnValue({
      user: mockUser,
      loading: false,
      error: null,
      logout: vi.fn(),
      refresh: vi.fn(),
    });

    render(
      <AppLayout
        title="CI Runs & Diagnoses"
        subtitle="Autonomous pipeline runs"
        actions={<button data-testid="custom-action">New Run</button>}
      >
        <div data-testid="protected-content">Active CI Run List</div>
      </AppLayout>
    );

    expect(mockReplace).not.toHaveBeenCalled();

    // Topbar content
    expect(screen.getByText("CI Runs & Diagnoses")).toBeInTheDocument();
    expect(screen.getByText("Autonomous pipeline runs")).toBeInTheDocument();
    expect(screen.getByTestId("custom-action")).toBeInTheDocument();
    expect(screen.getByText("solodev")).toBeInTheDocument();

    // Main content
    expect(screen.getByTestId("protected-content")).toBeInTheDocument();
    const mainElement = screen.getByRole("main");
    expect(mainElement).toContainElement(screen.getByTestId("protected-content"));

    // Sidebar branding & nav links
    expect(screen.getByText("Haunter")).toBeInTheDocument();
    expect(screen.getByText("Autonomous CI")).toBeInTheDocument();
    expect(screen.getByText("Runs")).toBeInTheDocument();
    expect(screen.getByText("Repositories")).toBeInTheDocument();
    expect(screen.getByText("Model Config")).toBeInTheDocument();
  });

  it("renders correctly when subtitle and actions are omitted", () => {
    vi.mocked(useAuth).mockReturnValue({
      user: mockUser,
      loading: false,
      error: null,
      logout: vi.fn(),
      refresh: vi.fn(),
    });

    render(
      <AppLayout title="Custom Title">
        <p>Repo table</p>
      </AppLayout>
    );

    expect(screen.getByRole("heading", { name: "Custom Title" })).toBeInTheDocument();
    expect(screen.getByText("Repo table")).toBeInTheDocument();
  });
});
