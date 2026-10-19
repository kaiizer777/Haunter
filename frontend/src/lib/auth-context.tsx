"use client";

import React, { createContext, useContext, useEffect, useState, useCallback } from "react";
import { api, AuthUser, setStoredToken, removeStoredToken } from "./api";

interface AuthContextType {
  user: AuthUser | null;
  loading: boolean;
  error: string | null;
  logout: () => Promise<void>;
  refresh: () => Promise<AuthUser | null>;
}

const AuthContext = createContext<AuthContextType>({
  user: null,
  loading: true,
  error: null,
  logout: async () => {},
  refresh: async () => null,
});

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchUser = useCallback(async (): Promise<AuthUser | null> => {
    try {
      const u = await api.getMe();
      setUser(u);
      setError(null);
      return u;
    } catch {
      setUser(null);
      return null;
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (typeof window !== "undefined") {
      try {
        const params = new URLSearchParams(window.location.search);
        const token = params.get("token");
        if (token) {
          setStoredToken(token);
          params.delete("token");
          const remainingQuery = params.toString();
          const newUrl =
            window.location.pathname +
            (remainingQuery ? `?${remainingQuery}` : "") +
            window.location.hash;
          window.history.replaceState({}, document.title, newUrl);
        }
      } catch {
        // ignore in restricted environments
      }
    }
    fetchUser();
  }, [fetchUser]);

  const logout = async () => {
    try {
      await api.logout();
    } catch {
      // ignore
    } finally {
      removeStoredToken();
      setUser(null);
      // eslint-disable-next-line @next/next/no-location-assign-relative-destination
      window.location.href = "/login";
    }
  };

  return (
    <AuthContext.Provider
      value={{
        user,
        loading,
        error,
        logout,
        refresh: fetchUser,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  return useContext(AuthContext);
}
