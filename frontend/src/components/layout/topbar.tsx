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
    <header className="sticky top-0 z-30 flex h-16 w-full items-center justify-between border-b border-zinc-800 bg-[#09090b]/90 px-6 backdrop-blur-sm">
      <div className="flex items-center gap-3.5">
        <div>
          <h1 className="text-base font-semibold tracking-tight text-zinc-100">
            {title}
          </h1>
          {subtitle && (
            <p className="text-xs text-zinc-400 font-mono mt-0.5">{subtitle}</p>
          )}
        </div>
      </div>

      <div className="flex items-center gap-3.5">
        {actions}

        {/* User profile & Logout */}
        {user && (
          <div className="flex items-center gap-3 pl-3 border-l border-zinc-800">
            <div className="flex items-center gap-2.5">
              {user.avatar_url ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={user.avatar_url}
                  alt={user.github_username}
                  className="h-8 w-8 rounded-full border border-zinc-700 object-cover"
                />
              ) : (
                <div className="flex h-8 w-8 items-center justify-center rounded-full bg-zinc-800 text-zinc-400">
                  <UserIcon className="h-4 w-4" />
                </div>
              )}
              <span className="text-[13px] font-mono text-zinc-300">
                {user.github_username}
              </span>
            </div>

            <Button
              variant="ghost"
              size="icon"
              onClick={logout}
              title="Sign out"
              className="h-8 w-8 text-zinc-400 hover:text-red-400 hover:bg-zinc-800/80"
            >
              <LogOut className="h-4 w-4" />
            </Button>
          </div>
        )}
      </div>
    </header>
  );
}
