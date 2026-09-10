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
    // eslint-disable-next-line @next/next/no-location-assign-relative-destination
    window.location.href = `${API_BASE}/auth/login`;
  };

  return (
    <div className="relative flex min-h-screen items-center justify-center bg-[#070709] p-4 select-none overflow-hidden">
      {/* Ambient background light gradients */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute -top-40 left-1/2 -translate-x-1/2 h-96 w-96 rounded-full bg-amber-500/[0.08] blur-3xl"
      />
      <div
        aria-hidden="true"
        className="pointer-events-none absolute -bottom-32 left-1/2 -translate-x-1/2 h-80 w-[500px] rounded-full bg-amber-600/[0.04] blur-3xl"
      />
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 bg-[linear-gradient(to_right,#1f1f23_1px,transparent_1px),linear-gradient(to_bottom,#1f1f23_1px,transparent_1px)] bg-[size:4rem_4rem] [mask-image:radial-gradient(ellipse_60%_50%_at_50%_50%,#000_70%,transparent_100%)] opacity-25"
      />

      {/* 3D Tactile Login Card Container with Contact Shadow */}
      <div className="relative z-10 w-full max-w-[360px]">
        {/* Soft amber floor contact shadow */}
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-x-8 -bottom-3 h-8 bg-amber-500/15 blur-xl rounded-full"
        />

        <div className="relative w-full rounded-2xl border-t border-t-zinc-600/60 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-gradient-to-b from-[#15151a]/95 via-[#101014]/95 to-[#0a0a0d]/95 p-7 backdrop-blur-xl shadow-[inset_0_1px_0_rgba(255,255,255,0.12),inset_0_-1px_0_rgba(0,0,0,0.5),0_16px_48px_rgba(0,0,0,0.75),0_2px_6px_rgba(0,0,0,0.4)] space-y-6 overflow-hidden">
          {/* Subtle light-from-above ambient gradient sheen */}
          <span
            aria-hidden="true"
            className="pointer-events-none absolute inset-x-0 top-0 h-6 bg-gradient-to-b from-white/[0.06] to-transparent"
          />

          {/* Header & Beveled Logo Coin */}
          <div className="space-y-3 text-center relative z-10">
            <div className="relative mx-auto flex h-12 w-12 items-center justify-center rounded-xl bg-gradient-to-b from-amber-400/20 via-amber-500/10 to-amber-600/5 border-t border-t-amber-400/50 border-x border-x-amber-500/30 border-b border-b-amber-600/20 text-amber-400 shadow-[inset_0_1px_0_rgba(255,255,255,0.25),0_4px_12px_rgba(0,0,0,0.5),0_0_20px_rgba(245,158,11,0.15)] group transition-transform duration-200 hover:scale-[1.03]">
              <TerminalSquare className="h-6 w-6 text-amber-400 drop-shadow-[0_0_8px_rgba(251,191,36,0.6)] transition-transform duration-200 group-hover:scale-105" />
              <span className="absolute -top-1 -right-1 flex h-2.5 w-2.5">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-60" />
                <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-amber-400 shadow-[0_0_8px_rgba(251,191,36,0.8)]" />
              </span>
            </div>

            <div>
              <h1 className="text-base font-bold tracking-[0.14em] text-zinc-100 uppercase font-mono">
                Haunter
              </h1>
              <p className="text-xs text-zinc-400 mt-1 leading-relaxed">
                Autonomous CI failure diagnosis and sandbox fix agent.
              </p>
            </div>
          </div>

          {/* Action Button & Inlaid Chips */}
          <div className="space-y-3.5 pt-1 relative z-10">
            <Button
              onClick={handleGitHubLogin}
              className="group relative w-full h-10.5 flex items-center justify-center gap-2.5 rounded-[7px] text-zinc-950 font-bold text-xs bg-gradient-to-b from-white via-zinc-100 to-zinc-200 border-t border-t-white border-x border-x-zinc-300 border-b border-b-zinc-400 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_2px_8px_rgba(0,0,0,0.35),0_1px_2px_rgba(0,0,0,0.2)] hover:from-white hover:to-zinc-100 hover:shadow-[inset_0_1px_0_rgba(255,255,255,1),0_4px_16px_rgba(0,0,0,0.45)] active:translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.3)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400 focus-visible:ring-offset-2 focus-visible:ring-offset-[#101014] transition-all cursor-pointer overflow-hidden"
            >
              {/* Subtle hover specular sheen sweep */}
              <span
                aria-hidden="true"
                className="pointer-events-none absolute inset-0 -translate-x-full group-hover:translate-x-full bg-gradient-to-r from-transparent via-white/40 to-transparent transition-transform duration-700"
              />

              <GitHubIcon className="h-4.5 w-4.5 shrink-0 transition-transform group-hover:scale-105 relative z-10" />
              <span className="relative z-10">Continue with GitHub</span>
              <ArrowRight className="h-3.5 w-3.5 ml-auto text-zinc-600 group-hover:text-zinc-950 group-hover:translate-x-0.5 transition-all relative z-10" />
            </Button>

            <div className="flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-[6px] bg-gradient-to-b from-zinc-850/60 via-zinc-900/60 to-zinc-950/60 border border-zinc-800/80 text-[10px] text-zinc-400 font-mono shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]">
              <ShieldCheck className="h-3.5 w-3.5 text-emerald-400 drop-shadow-[0_0_6px_rgba(52,211,153,0.5)]" />
              <span>OAuth `read:user` scope only</span>
            </div>
          </div>

          {/* Minimal Footer */}
          <div className="border-t border-zinc-800/80 pt-4 text-center relative z-10">
            <div className="flex items-center justify-center gap-2">
              <span className="h-1.5 w-1.5 rounded-full bg-amber-400/80 shadow-[0_0_6px_rgba(251,191,36,0.6)]" />
              <span className="text-[10px] text-zinc-500 font-mono tracking-wide">
                Haunter Engine v1.0 • Multi-Tenant
              </span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
