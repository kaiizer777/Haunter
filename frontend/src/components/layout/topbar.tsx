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
    <header className="sticky top-0 z-30 flex h-16 w-full items-center justify-between border-b border-zinc-800/90 bg-gradient-to-b from-[#121217]/95 via-[#0e0e12]/95 to-[#09090b]/95 px-6 backdrop-blur-md shadow-[inset_0_-1px_0_rgba(255,255,255,0.04),0_4px_20px_rgba(0,0,0,0.5),0_1px_3px_rgba(0,0,0,0.3)] relative select-none">
      {/* Subtle top edge specular reflection */}
      <span
        aria-hidden="true"
        className="pointer-events-none absolute inset-x-0 top-0 h-[1px] bg-gradient-to-r from-transparent via-white/10 to-transparent"
      />

      <div className="flex items-center gap-3.5 relative z-10">
        <div>
          <h1 className="text-base font-semibold tracking-tight text-zinc-100">
            {title}
          </h1>
          {subtitle && (
            <p className="text-xs text-zinc-400 font-mono mt-0.5">{subtitle}</p>
          )}
        </div>
      </div>

      <div className="flex items-center gap-3.5 relative z-10">
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

            {/* Tactile 3D Sign Out Button (Default crimson surface styling) */}
            <Button
              variant="ghost"
              size="icon"
              onClick={logout}
              title="Sign out"
              className="h-7 w-7 rounded-[6px] text-red-400 hover:text-red-300 bg-gradient-to-b from-zinc-800/90 via-zinc-850 to-zinc-900/90 border-t border-t-red-500/60 border-x border-x-zinc-700/60 border-b border-b-zinc-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.12),0_1px_2px_rgba(0,0,0,0.3),0_0_8px_rgba(239,68,68,0.2)] hover:border-t-red-400/80 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.2),0_0_12px_rgba(239,68,68,0.35)] active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.5)] transition-all"
            >
              <LogOut className="h-3.5 w-3.5" />
            </Button>
          </div>
        )}
      </div>
    </header>
  );
}
