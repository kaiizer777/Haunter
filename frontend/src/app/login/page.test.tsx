import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import LoginPage from "./page";
import { useAuth } from "@/lib/auth-context";
import { API_BASE } from "@/lib/api";

const mockReplace = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    replace: mockReplace,
    push: vi.fn(),
  }),
}));

vi.mock("@/lib/auth-context", () => ({
  useAuth: vi.fn(),
}));

describe("LoginPage (app/login/page.tsx)", () => {
  const originalLocation = window.location;

  beforeEach(() => {
    vi.clearAllMocks();
    delete (window as any).location;
    window.location = {
      href: "http://localhost:3000/login",
      pathname: "/login",
    } as any;
  });

  afterEach(() => {
    // @ts-expect-error - restore original window.location
    window.location = originalLocation;
  });

  it("renders Haunter login branding, OAuth note, and GitHub action button", () => {
    vi.mocked(useAuth).mockReturnValue({
      user: null,
      loading: false,
      error: null,
      logout: vi.fn(),
      refresh: vi.fn(),
    });

    render(<LoginPage />);

    expect(screen.getByRole("heading", { level: 1, name: "Haunter" })).toBeInTheDocument();
    expect(
      screen.getByText("Autonomous CI failure diagnosis and sandbox fix agent.")
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /continue with github/i })
    ).toBeInTheDocument();
    expect(screen.getByText(/oauth `read:user` scope only/i)).toBeInTheDocument();
    expect(screen.getByText(/haunter engine v1\.0 • multi-tenant/i)).toBeInTheDocument();
  });

  it("navigates to the GitHub OAuth authorization URL when clicking login button", async () => {
    const user = userEvent.setup();
    vi.mocked(useAuth).mockReturnValue({
      user: null,
      loading: false,
      error: null,
      logout: vi.fn(),
      refresh: vi.fn(),
    });

    render(<LoginPage />);

    const loginBtn = screen.getByRole("button", { name: /continue with github/i });
    await user.click(loginBtn);

    expect(window.location.href).toBe(`${API_BASE}/auth/login`);
    expect(mockReplace).not.toHaveBeenCalled();
  });

  it("redirects authenticated user to /runs", () => {
    vi.mocked(useAuth).mockReturnValue({
      user: {
        id: "usr_1",
        github_id: 123,
        github_username: "solodev",
        avatar_url: null,
        is_admin: false,
      },
      loading: false,
      error: null,
      logout: vi.fn(),
      refresh: vi.fn(),
    });

    render(<LoginPage />);

    expect(mockReplace).toHaveBeenCalledTimes(1);
    expect(mockReplace).toHaveBeenCalledWith("/runs");
  });

  it("does not redirect when authentication state is still loading", () => {
    vi.mocked(useAuth).mockReturnValue({
      user: null,
      loading: true,
      error: null,
      logout: vi.fn(),
      refresh: vi.fn(),
    });

    render(<LoginPage />);

    expect(mockReplace).not.toHaveBeenCalled();
  });
});
