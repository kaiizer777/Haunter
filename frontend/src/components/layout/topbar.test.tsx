import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Topbar } from "./topbar";
import { useAuth } from "@/lib/auth-context";
import { AuthUser } from "@/lib/api";

vi.mock("@/lib/auth-context", () => ({
  useAuth: vi.fn(),
}));

describe("topbar.tsx", () => {
  const mockLogout = vi.fn();

  const userWithAvatar: AuthUser = {
    id: "usr_100",
    github_id: 1001,
    github_username: "octocat",
    avatar_url: "https://avatars.githubusercontent.com/u/1001",
    is_admin: false,
  };

  const userWithoutAvatar: AuthUser = {
    id: "usr_200",
    github_id: 2002,
    github_username: "ghostdev",
    avatar_url: null,
    is_admin: true,
  };

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders the title, optional subtitle, and actions slot", () => {
    vi.mocked(useAuth).mockReturnValue({
      user: null,
      loading: false,
      error: null,
      logout: mockLogout,
      refresh: vi.fn(),
    });

    render(
      <Topbar
        title="CI Diagnostics"
        subtitle="Live telemetry for test runs"
        actions={<button data-testid="export-btn">Export JSON</button>}
      />
    );

    expect(screen.getByRole("heading", { level: 1, name: "CI Diagnostics" })).toBeInTheDocument();
    expect(screen.getByText("Live telemetry for test runs")).toBeInTheDocument();
    expect(screen.getByTestId("export-btn")).toBeInTheDocument();
  });

  it("omits subtitle and actions when not provided", () => {
    vi.mocked(useAuth).mockReturnValue({
      user: null,
      loading: false,
      error: null,
      logout: mockLogout,
      refresh: vi.fn(),
    });

    render(<Topbar title="Minimal Topbar" />);

    expect(screen.getByRole("heading", { level: 1, name: "Minimal Topbar" })).toBeInTheDocument();
    expect(screen.queryByText("Live telemetry for test runs")).not.toBeInTheDocument();
  });

  it("does not render user profile or sign-out button when user is null", () => {
    vi.mocked(useAuth).mockReturnValue({
      user: null,
      loading: false,
      error: null,
      logout: mockLogout,
      refresh: vi.fn(),
    });

    render(<Topbar title="Public View" />);

    expect(screen.queryByTitle("Sign out")).not.toBeInTheDocument();
    expect(screen.queryByText("octocat")).not.toBeInTheDocument();
  });

  it("renders user avatar and username when user has an avatar_url", () => {
    vi.mocked(useAuth).mockReturnValue({
      user: userWithAvatar,
      loading: false,
      error: null,
      logout: mockLogout,
      refresh: vi.fn(),
    });

    render(<Topbar title="Dashboard" />);

    const avatar = screen.getByRole("img", { name: "octocat" });
    expect(avatar).toBeInTheDocument();
    expect(avatar).toHaveAttribute("src", "https://avatars.githubusercontent.com/u/1001");
    expect(screen.getByText("octocat")).toBeInTheDocument();
    expect(screen.getByTitle("Sign out")).toBeInTheDocument();
  });

  it("renders fallback user icon when user has null avatar_url", () => {
    vi.mocked(useAuth).mockReturnValue({
      user: userWithoutAvatar,
      loading: false,
      error: null,
      logout: mockLogout,
      refresh: vi.fn(),
    });

    render(<Topbar title="Dashboard" />);

    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(screen.getByText("ghostdev")).toBeInTheDocument();
    expect(screen.getByTitle("Sign out")).toBeInTheDocument();
  });

  it("calls logout when sign out button is clicked", async () => {
    const user = userEvent.setup();
    vi.mocked(useAuth).mockReturnValue({
      user: userWithAvatar,
      loading: false,
      error: null,
      logout: mockLogout,
      refresh: vi.fn(),
    });

    render(<Topbar title="Dashboard" />);

    const logoutBtn = screen.getByTitle("Sign out");
    await user.click(logoutBtn);

    expect(mockLogout).toHaveBeenCalledTimes(1);
  });
});
