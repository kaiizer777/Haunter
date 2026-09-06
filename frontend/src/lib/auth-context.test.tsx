import React from "react";
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, act, renderHook, waitFor } from "@testing-library/react";
import { AuthProvider, useAuth } from "./auth-context";
import { api, AuthUser } from "./api";

describe("auth-context.tsx", () => {
  const originalLocation = window.location;

  beforeEach(() => {
    vi.restoreAllMocks();
    delete (window as any).location;
    window.location = {
      href: "https://example.com/dashboard",
      pathname: "/dashboard",
    } as any;
  });

  afterEach(() => {
    // @ts-expect-error - restore original window.location
    window.location = originalLocation;
  });

  describe("default context (outside provider)", () => {
    it("provides initial default values and no-op functions", async () => {
      const { result } = renderHook(() => useAuth());

      expect(result.current.user).toBeNull();
      expect(result.current.loading).toBe(true);
      expect(result.current.error).toBeNull();

      // Test default logout
      await act(async () => {
        await expect(result.current.logout()).resolves.toBeUndefined();
      });

      // Test default refresh
      await act(async () => {
        const refreshResult = await result.current.refresh();
        expect(refreshResult).toBeNull();
      });
    });
  });

  describe("AuthProvider lifecycle & states", () => {
    const mockUser: AuthUser = {
      id: "usr_123",
      github_id: 99999,
      github_username: "kaiizer777",
      avatar_url: "https://avatars.githubusercontent.com/u/99999",
      is_admin: true,
    };

    function TestConsumer() {
      const { user, loading, error, logout, refresh } = useAuth();
      return (
        <div>
          <div data-testid="status">{loading ? "loading" : "idle"}</div>
          <div data-testid="username">{user ? user.github_username : "anonymous"}</div>
          <div data-testid="error">{error ?? "none"}</div>
          <button data-testid="refresh-btn" onClick={() => refresh()}>
            Refresh
          </button>
          <button data-testid="logout-btn" onClick={() => logout()}>
            Logout
          </button>
        </div>
      );
    }

    it("displays initial loading state while api.getMe is in-flight", () => {
      // Return unresolved promise to observe loading state
      vi.spyOn(api, "getMe").mockImplementation(() => new Promise(() => {}));

      render(
        <AuthProvider>
          <TestConsumer />
        </AuthProvider>
      );

      expect(screen.getByTestId("status").textContent).toBe("loading");
      expect(screen.getByTestId("username").textContent).toBe("anonymous");
      expect(screen.getByTestId("error").textContent).toBe("none");
    });

    it("populates user and sets loading=false on successful api.getMe", async () => {
      vi.spyOn(api, "getMe").mockResolvedValue(mockUser);

      render(
        <AuthProvider>
          <TestConsumer />
        </AuthProvider>
      );

      // Wait for async effect to resolve
      await waitFor(() => {
        expect(screen.getByTestId("status")).toHaveTextContent("idle");
      });
      expect(screen.getByTestId("username")).toHaveTextContent("kaiizer777");
      expect(screen.getByTestId("error")).toHaveTextContent("none");
    });

    it("sets user=null and loading=false when api.getMe fails (e.g. 401 or network error)", async () => {
      vi.spyOn(api, "getMe").mockRejectedValue(new Error("Unauthorized"));

      render(
        <AuthProvider>
          <TestConsumer />
        </AuthProvider>
      );

      await waitFor(() => {
        expect(screen.getByTestId("status")).toHaveTextContent("idle");
      });
      expect(screen.getByTestId("username")).toHaveTextContent("anonymous");
      expect(screen.getByTestId("error")).toHaveTextContent("none");
    });

    it("refresh() successfully refetches user and updates state", async () => {
      const getMeSpy = vi
        .spyOn(api, "getMe")
        .mockRejectedValueOnce(new Error("No session")) // initial load fails
        .mockResolvedValueOnce(mockUser); // subsequent refresh succeeds

      render(
        <AuthProvider>
          <TestConsumer />
        </AuthProvider>
      );

      // Initially anonymous
      await waitFor(() => {
        expect(screen.getByTestId("status")).toHaveTextContent("idle");
      });
      expect(screen.getByTestId("username")).toHaveTextContent("anonymous");
      expect(getMeSpy).toHaveBeenCalledTimes(1);

      // Trigger refresh
      await act(async () => {
        screen.getByTestId("refresh-btn").click();
      });

      await waitFor(() => {
        expect(screen.getByTestId("username")).toHaveTextContent("kaiizer777");
      });
      expect(getMeSpy).toHaveBeenCalledTimes(2);
    });

    it("refresh() sets user to null when refetch fails", async () => {
      const getMeSpy = vi
        .spyOn(api, "getMe")
        .mockResolvedValueOnce(mockUser) // initial load succeeds
        .mockRejectedValueOnce(new Error("Session expired")); // refresh fails

      render(
        <AuthProvider>
          <TestConsumer />
        </AuthProvider>
      );

      await waitFor(() => {
        expect(screen.getByTestId("username")).toHaveTextContent("kaiizer777");
      });

      // Trigger refresh
      await act(async () => {
        screen.getByTestId("refresh-btn").click();
      });

      await waitFor(() => {
        expect(screen.getByTestId("username")).toHaveTextContent("anonymous");
      });
      expect(getMeSpy).toHaveBeenCalledTimes(2);
    });

    it("logout() calls api.logout, clears user, and redirects to /login", async () => {
      vi.spyOn(api, "getMe").mockResolvedValue(mockUser);
      const logoutSpy = vi
        .spyOn(api, "logout")
        .mockResolvedValue({ detail: "Logged out" });

      render(
        <AuthProvider>
          <TestConsumer />
        </AuthProvider>
      );

      await waitFor(() => {
        expect(screen.getByTestId("username")).toHaveTextContent("kaiizer777");
      });

      // Trigger logout
      await act(async () => {
        screen.getByTestId("logout-btn").click();
      });

      expect(logoutSpy).toHaveBeenCalledTimes(1);
      await waitFor(() => {
        expect(screen.getByTestId("username")).toHaveTextContent("anonymous");
      });
      expect(window.location.href).toBe("/login");
    });

    it("logout() catches api.logout error, clears user, and still redirects to /login", async () => {
      vi.spyOn(api, "getMe").mockResolvedValue(mockUser);
      const logoutSpy = vi
        .spyOn(api, "logout")
        .mockRejectedValue(new Error("Network failed on logout"));

      render(
        <AuthProvider>
          <TestConsumer />
        </AuthProvider>
      );

      await waitFor(() => {
        expect(screen.getByTestId("username")).toHaveTextContent("kaiizer777");
      });

      // Trigger logout despite api error
      await act(async () => {
        screen.getByTestId("logout-btn").click();
      });

      expect(logoutSpy).toHaveBeenCalledTimes(1);
      await waitFor(() => {
        expect(screen.getByTestId("username")).toHaveTextContent("anonymous");
      });
      expect(window.location.href).toBe("/login");
    });
  });
});
