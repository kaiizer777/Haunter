import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import ModelConfigPage, { formatContextWindow } from "./page";
import { useAuth } from "@/lib/auth-context";
import { api } from "@/lib/api";

const mockReplace = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mockReplace, push: vi.fn() }),
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
  ApiError: class ApiError extends Error {},
}));

describe("formatContextWindow (frontend/src/app/config/page.tsx)", () => {
  describe("Binary multiples", () => {
    it("formats 1048576 as 1M Context", () => {
      expect(formatContextWindow(1048576)).toBe("1M Context");
    });

    it("formats 262144 as 256k Context", () => {
      expect(formatContextWindow(262144)).toBe("256k Context");
    });

    it("formats 131072 as 128k Context", () => {
      expect(formatContextWindow(131072)).toBe("128k Context");
    });

    it("formats 65536 as 64k Context", () => {
      expect(formatContextWindow(65536)).toBe("64k Context");
    });

    it("formats 8192 as 8k Context", () => {
      expect(formatContextWindow(8192)).toBe("8k Context");
    });
  });

  describe("Decimal multiples", () => {
    it("formats 1000000 as 1M Context", () => {
      expect(formatContextWindow(1000000)).toBe("1M Context");
    });

    it("formats 200000 as 200k Context", () => {
      expect(formatContextWindow(200000)).toBe("200k Context");
    });

    it("formats 128000 as 128k Context", () => {
      expect(formatContextWindow(128000)).toBe("128k Context");
    });
  });

  describe("Fallback and falsy values", () => {
    it("returns Unknown Context for undefined", () => {
      expect(formatContextWindow(undefined)).toBe("Unknown Context");
    });

    it("returns Unknown Context for 0", () => {
      expect(formatContextWindow(0)).toBe("Unknown Context");
    });
  });
});

describe("ModelConfigPage admin hard-block", () => {
  const nonAdminUser = {
    id: "usr_1",
    github_username: "regular-user",
    avatar_url: null,
    is_admin: false,
  };

  it("redirects non-admins to /repos, renders Access Denied, and fetches nothing", async () => {
    vi.clearAllMocks();
    vi.mocked(useAuth).mockReturnValue({
      user: nonAdminUser,
      loading: false,
      error: null,
      logout: vi.fn(),
      refresh: vi.fn(),
    });

    render(<ModelConfigPage />);

    expect(await screen.findByText("Access Denied")).toBeInTheDocument();
    await waitFor(() => {
      expect(mockReplace).toHaveBeenCalledWith("/repos");
    });
    expect(api.getModelConfig).not.toHaveBeenCalled();
    expect(api.getRepos).not.toHaveBeenCalled();
    expect(api.getAvailableModels).not.toHaveBeenCalled();
  });
});
