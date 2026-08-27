"use client";

import { useEffect, useState } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export interface AuthUser {
  id: string;
  github_username: string;
  avatar_url: string | null;
}

interface AuthState {
  user: AuthUser | null;
  loading: boolean;
  error: string | null;
}

/**
 * Fetches the currently authenticated user from FastAPI /auth/me.
 * The session is carried via an httpOnly cookie (SameSite=Lax), so we must
 * always pass `credentials: "include"` — the browser will not send cookies
 * on cross-origin requests without this.
 */
export function useAuth(): AuthState {
  const [state, setState] = useState<AuthState>({
    user: null,
    loading: true,
    error: null,
  });

  useEffect(() => {
    fetch(`${API_URL}/auth/me`, { credentials: "include" })
      .then(async (res) => {
        if (res.status === 401) {
          setState({ user: null, loading: false, error: null });
          return;
        }
        if (!res.ok) {
          throw new Error(`Unexpected status ${res.status}`);
        }
        const user: AuthUser = await res.json();
        setState({ user, loading: false, error: null });
      })
      .catch((err: unknown) => {
        setState({
          user: null,
          loading: false,
          error: err instanceof Error ? err.message : "Unknown error",
        });
      });
  }, []);

  return state;
}

/**
 * Renders a "Sign in with GitHub" link or the user's avatar + username when
 * already authenticated.  All fetches use credentials: "include" so the
 * httpOnly session cookie is forwarded to FastAPI on every request.
 */
export function LoginButton() {
  const { user, loading } = useAuth();

  if (loading) {
    return (
      <span className="inline-block h-9 w-36 animate-pulse rounded-full bg-zinc-200 dark:bg-zinc-700" />
    );
  }

  if (user) {
    return (
      <div className="flex items-center gap-3">
        {user.avatar_url && (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={user.avatar_url}
            alt={user.github_username}
            className="h-8 w-8 rounded-full ring-2 ring-zinc-300 dark:ring-zinc-600"
          />
        )}
        <span className="text-sm font-medium text-zinc-800 dark:text-zinc-200">
          {user.github_username}
        </span>
        <button
          onClick={() =>
            fetch(`${API_URL}/auth/logout`, {
              method: "POST",
              credentials: "include",
            }).then(() => window.location.reload())
          }
          className="text-sm text-zinc-500 underline hover:text-zinc-800 dark:hover:text-zinc-200"
        >
          Sign out
        </button>
      </div>
    );
  }

  return (
    <a
      href={`${API_URL}/auth/login`}
      className="inline-flex h-9 items-center gap-2 rounded-full bg-zinc-900 px-4 text-sm font-medium text-white transition-colors hover:bg-zinc-700 dark:bg-zinc-50 dark:text-zinc-900 dark:hover:bg-zinc-200"
    >
      {/* GitHub mark */}
      <svg
        aria-hidden="true"
        height="16"
        viewBox="0 0 16 16"
        width="16"
        fill="currentColor"
      >
        <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z" />
      </svg>
      Sign in with GitHub
    </a>
  );
}
