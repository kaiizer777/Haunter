"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth-context";
import { API_BASE } from "@/lib/api";
import { TerminalSquare, ArrowRight, ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/button";

function GitHubIcon(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 16 16"
      fill="currentColor"
      {...props}
    >
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z" />
    </svg>
  );
}

export default function LoginPage() {
  const { user, loading } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!loading && user) {
      router.replace("/runs");
    }
  }, [user, loading, router]);

  const handleGitHubLogin = () => {
    window.location.href = `${API_BASE}/auth/login`;
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-[#09090b] p-4 select-none">
      {/* 320px Minimal Card */}
      <div className="w-full max-w-[340px] rounded-[8px] border border-zinc-800 bg-[#121215] p-6 shadow-2xl space-y-6">
        {/* Header */}
        <div className="space-y-2 text-center">
          <div className="mx-auto flex h-10 w-10 items-center justify-center rounded-[6px] bg-zinc-900 border border-zinc-700/80 text-amber-400">
            <TerminalSquare className="h-5 w-5" />
          </div>
          <div>
            <h1 className="text-base font-bold tracking-tight text-zinc-100 uppercase">
              Haunter
            </h1>
            <p className="text-xs text-zinc-400 mt-1">
              Autonomous CI failure diagnosis and sandbox fix agent.
            </p>
          </div>
        </div>

        {/* Action Button */}
        <div className="space-y-3 pt-2">
          <Button
            onClick={handleGitHubLogin}
            className="w-full h-10 flex items-center justify-center gap-2.5 bg-zinc-100 text-zinc-950 hover:bg-zinc-200 active:bg-zinc-300 font-semibold text-xs transition-colors rounded-[6px]"
          >
            <GitHubIcon className="h-4 w-4" />
            <span>Continue with GitHub</span>
            <ArrowRight className="h-3.5 w-3.5 ml-auto text-zinc-600" />
          </Button>

          <div className="flex items-center justify-center gap-1.5 text-[10px] text-zinc-500 font-mono">
            <ShieldCheck className="h-3 w-3 text-emerald-500/80" />
            <span>OAuth `read:user` scope only</span>
          </div>
        </div>

        {/* Minimal Footer */}
        <div className="border-t border-zinc-800/80 pt-4 text-center">
          <span className="text-[10px] text-zinc-600 font-mono">
            Haunter Engine v1.0 • Multi-Tenant
          </span>
        </div>
      </div>
    </div>
  );
}
