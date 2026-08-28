"use client";

import { useAuth } from "@/lib/auth-context";
import { LogOut, User as UserIcon } from "lucide-react";
import { Button } from "@/components/ui/button";

interface TopbarProps {
  title: string;
  subtitle?: string;
  actions?: React.ReactNode;
}

export function Topbar({ title, subtitle, actions }: TopbarProps) {
  const { user, logout } = useAuth();

  return (
    <header className="sticky top-0 z-20 flex h-14 w-full items-center justify-between border-b border-zinc-800 bg-[#09090b]/90 px-6 backdrop-blur-sm">
      <div className="flex items-center gap-3">
        <div>
          <h1 className="text-sm font-semibold tracking-tight text-zinc-100">
            {title}
          </h1>
          {subtitle && (
            <p className="text-[11px] text-zinc-400 font-mono">{subtitle}</p>
          )}
        </div>
      </div>

      <div className="flex items-center gap-3">
        {actions}

        {/* User profile & Logout */}
        {user && (
          <div className="flex items-center gap-2 pl-2 border-l border-zinc-800">
            <div className="flex items-center gap-2">
              {user.avatar_url ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={user.avatar_url}
                  alt={user.github_username}
                  className="h-6 w-6 rounded-full border border-zinc-700 object-cover"
                />
              ) : (
                <div className="flex h-6 w-6 items-center justify-center rounded-full bg-zinc-800 text-zinc-400">
                  <UserIcon className="h-3 w-3" />
                </div>
              )}
              <span className="text-xs font-mono text-zinc-300">
                {user.github_username}
              </span>
            </div>

            <Button
              variant="ghost"
              size="icon"
              onClick={logout}
              title="Sign out"
              className="h-7 w-7 text-zinc-400 hover:text-red-400 hover:bg-zinc-800/80"
            >
              <LogOut className="h-3.5 w-3.5" />
            </Button>
          </div>
        )}
      </div>
    </header>
  );
}
