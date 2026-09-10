"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth-context";
import { API_BASE } from "@/lib/api";
import {
  TerminalSquare,
  ArrowRight,
  ShieldCheck,
  CheckCircle2,
  GitPullRequest,
  AlertCircle,
  Cpu,
  Zap,
  Loader2,
  Layers,
} from "lucide-react";
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
  const [isRedirecting, setIsRedirecting] = useState(false);

  useEffect(() => {
    if (!loading && user) {
      router.replace("/runs");
    }
  }, [user, loading, router]);

  const handleGitHubLogin = () => {
    setIsRedirecting(true);
    // eslint-disable-next-line @next/next/no-location-assign-relative-destination
    window.location.href = `${API_BASE}/auth/login`;
  };

  return (
    <div className="relative flex h-screen max-h-screen h-dvh w-full flex-col justify-between bg-[#070709] text-zinc-100 select-none overflow-hidden font-sans">
      {/* Ambient background grid & glow fields */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute -top-40 left-1/2 -translate-x-1/2 h-[400px] w-[700px] rounded-full bg-amber-500/[0.07] blur-[120px]"
      />
      <div
        aria-hidden="true"
        className="pointer-events-none absolute top-1/4 -left-28 h-72 w-72 rounded-full bg-amber-600/[0.04] blur-[100px]"
      />
      <div
        aria-hidden="true"
        className="pointer-events-none absolute bottom-0 right-0 h-[360px] w-[360px] rounded-full bg-zinc-800/[0.06] blur-[120px]"
      />
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 bg-[linear-gradient(to_right,#1f1f24_1px,transparent_1px),linear-gradient(to_bottom,#1f1f24_1px,transparent_1px)] bg-[size:4rem_4rem] [mask-image:radial-gradient(ellipse_70%_60%_at_50%_35%,#000_65%,transparent_100%)] opacity-[0.2]"
      />

      {/* Top minimal header navigation */}
      <header className="relative z-20 flex h-12 shrink-0 w-full items-center justify-between border-b border-zinc-800/80 bg-zinc-950/50 px-6 backdrop-blur-md lg:px-12">
        <div className="flex items-center gap-2.5 ml-2 sm:ml-6 lg:ml-8">
          <div className="relative flex h-7.5 w-7.5 items-center justify-center rounded-lg bg-gradient-to-b from-amber-400/20 via-amber-500/10 to-transparent border border-amber-500/30 text-amber-400 shadow-[inset_0_1px_0_rgba(255,255,255,0.2),0_2px_8px_rgba(0,0,0,0.5)]">
            <TerminalSquare className="h-4 w-4 text-amber-400" />
          </div>
          <div className="flex items-center gap-2">
            <span className="font-mono text-[13px] font-bold tracking-[0.16em] text-zinc-100 uppercase">
              HAUNTER
            </span>
            <span className="rounded-[4px] bg-zinc-850 px-1.5 py-0.5 text-[9.5px] font-mono text-zinc-400 border border-zinc-750">
              CI/CD AGENT
            </span>
          </div>
        </div>

        <div className="flex items-center gap-4">
          <div className="hidden sm:flex items-center gap-2 rounded-full border border-zinc-800/90 bg-zinc-900/60 px-2.5 py-0.5 text-[10px] font-mono text-zinc-400 backdrop-blur-sm shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]">
            <span className="relative flex h-2 w-2">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
              <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-400" />
            </span>
            <span>All Systems Operational</span>
          </div>
        </div>
      </header>

      {/* Main hero & authentication section - strictly fits in available height */}
      <main className="relative z-10 mx-auto flex w-full max-w-6xl flex-1 min-h-0 items-center px-6 py-2 lg:px-10 overflow-hidden">
        <div className="grid w-full items-center gap-8 lg:grid-cols-12 lg:gap-12">
          
          {/* Left Column: Product Value & Live CI Intercept Console */}
          <div className="space-y-4 lg:col-span-7">
            <div className="space-y-2">
              <div className="inline-flex items-center gap-2 rounded-full border border-amber-500/30 bg-amber-500/10 px-2.5 py-0.5 text-[10px] font-mono font-medium tracking-wide text-amber-400 shadow-[0_0_12px_rgba(245,158,11,0.12)]">
                <Zap className="h-3 w-3 text-amber-400 animate-pulse" />
                <span>AUTONOMOUS WORKFLOW_RUN HEALING</span>
              </div>

              <h2 className="text-2xl font-extrabold tracking-tight text-zinc-100 sm:text-3xl lg:text-4xl leading-[1.14]">
                Your CI breaks at 2 AM.{" "}
                <span className="text-transparent bg-clip-text bg-gradient-to-r from-amber-300 via-amber-400 to-amber-500">
                  Haunter ships the verified fix.
                </span>
              </h2>

              <p className="max-w-xl text-xs sm:text-sm text-zinc-400 leading-relaxed font-normal">
                Autonomous CI failure diagnosis and sandbox fix agent for GitHub Actions. Intercepts failures, diagnoses the AST root cause, verifies in an isolated sandbox runner, and drafts review-ready PRs.
              </p>
            </div>

            {/* Live Pipeline Telemetry Card */}
            <div className="relative rounded-xl border border-zinc-800/90 bg-gradient-to-b from-[#111116] via-[#0d0d11] to-[#0a0a0d] p-3 sm:p-3.5 shadow-[0_16px_40px_rgba(0,0,0,0.65),inset_0_1px_0_rgba(255,255,255,0.06)] overflow-hidden">
              <div className="flex items-center justify-between border-b border-zinc-800/80 pb-2">
                <div className="flex items-center gap-2">
                  <div className="flex gap-1">
                    <div className="h-2 w-2 rounded-full bg-red-500/70" />
                    <div className="h-2 w-2 rounded-full bg-amber-500/70" />
                    <div className="h-2 w-2 rounded-full bg-emerald-500/70" />
                  </div>
                  <span className="font-mono text-[10px] text-zinc-400 ml-1.5">
                    trace // ci-heal-pipeline #8921
                  </span>
                </div>
                <span className="inline-flex items-center gap-1 rounded bg-emerald-500/10 px-1.5 py-0.5 font-mono text-[9px] font-medium text-emerald-400 border border-emerald-500/20">
                  <CheckCircle2 className="h-2.5 w-2.5" />
                  Verified in Sandbox
                </span>
              </div>

              {/* Timeline execution stages */}
              <div className="mt-2.5 space-y-1.5 font-mono text-[10px]">
                <div className="flex items-start gap-2 rounded-md bg-zinc-900/40 px-2 py-1.5 border border-zinc-800/50">
                  <span className="text-zinc-500 shrink-0">00:01</span>
                  <AlertCircle className="h-3 w-3 text-rose-400 shrink-0 mt-0.5" />
                  <div className="flex-1 min-w-0 truncate">
                    <span className="text-rose-300 font-semibold">CI Failure Intercepted: </span>
                    <span className="text-zinc-400">test.yml (workflow_run.failure on main)</span>
                  </div>
                </div>

                <div className="flex items-start gap-2 rounded-md bg-zinc-900/40 px-2 py-1.5 border border-zinc-800/50">
                  <span className="text-zinc-500 shrink-0">00:03</span>
                  <Layers className="h-3 w-3 text-amber-400 shrink-0 mt-0.5" />
                  <div className="flex-1 min-w-0 truncate">
                    <span className="text-amber-300 font-semibold">Context Gatherer: </span>
                    <span className="text-zinc-400">Extracted AST delta & pytest stack trace from 3,840 lines</span>
                  </div>
                </div>

                <div className="flex items-start gap-2 rounded-md bg-zinc-900/40 px-2 py-1.5 border border-zinc-800/50">
                  <span className="text-zinc-500 shrink-0">00:14</span>
                  <Cpu className="h-3 w-3 text-cyan-400 shrink-0 mt-0.5" />
                  <div className="flex-1 min-w-0 truncate">
                    <span className="text-cyan-300 font-semibold">Sandbox Verifier: </span>
                    <span className="text-zinc-400">Isolated runner executed tests in mirror repo → Exit 0 (Pass)</span>
                  </div>
                </div>

                <div className="flex items-start gap-2 rounded-md bg-zinc-900/40 px-2 py-1.5 border border-zinc-800/50">
                  <span className="text-zinc-500 shrink-0">00:19</span>
                  <GitPullRequest className="h-3 w-3 text-emerald-400 shrink-0 mt-0.5" />
                  <div className="flex-1 min-w-0 truncate">
                    <span className="text-emerald-300 font-semibold">PR Writer: </span>
                    <span className="text-zinc-400">Drafted PR #148 with fix diff, regression suite & audit trace</span>
                  </div>
                </div>
              </div>

              {/* Quick telemetry pillars */}
              <div className="mt-2.5 grid grid-cols-3 gap-2 border-t border-zinc-800/80 pt-2 text-center">
                <div>
                  <div className="text-xs sm:text-sm font-bold text-zinc-100 font-mono">&lt; 45s</div>
                  <div className="text-[9px] text-zinc-500 font-mono uppercase tracking-wider">Avg Resolution</div>
                </div>
                <div>
                  <div className="text-xs sm:text-sm font-bold text-amber-400 font-mono">100%</div>
                  <div className="text-[9px] text-zinc-500 font-mono uppercase tracking-wider">Sandbox Verified</div>
                </div>
                <div>
                  <div className="text-xs sm:text-sm font-bold text-emerald-400 font-mono">0</div>
                  <div className="text-[9px] text-zinc-500 font-mono uppercase tracking-wider">Hallucinated Diffs</div>
                </div>
              </div>
            </div>
          </div>

          {/* Right Column: Tactile Auth Card */}
          <div className="flex justify-center lg:col-span-5">
            <div className="relative w-full max-w-[375px] translate-y-4 lg:translate-y-6">
              {/* Soft amber floor contact shadow */}
              <div
                aria-hidden="true"
                className="pointer-events-none absolute inset-x-8 -bottom-3 h-8 bg-amber-500/15 blur-xl rounded-full"
              />

              {/* Elevated Card Shell */}
              <div className="relative w-full rounded-2xl border-t border-t-zinc-600/70 border-x border-x-zinc-800/90 border-b border-b-zinc-950 bg-gradient-to-b from-[#16161b]/95 via-[#101014]/95 to-[#0a0a0d]/95 p-6 backdrop-blur-2xl shadow-[inset_0_1px_0_rgba(255,255,255,0.12),inset_0_-1px_0_rgba(0,0,0,0.6),0_20px_48px_rgba(0,0,0,0.85),0_4px_10px_rgba(0,0,0,0.5)] space-y-4 overflow-hidden">
                {/* Subtle top light sheen */}
                <span
                  aria-hidden="true"
                  className="pointer-events-none absolute inset-x-0 top-0 h-5 bg-gradient-to-b from-white/[0.07] to-transparent"
                />

                {/* Header & Beveled Logo Coin */}
                <div className="space-y-2 text-center relative z-10">
                  <div className="relative mx-auto flex h-11 w-11 items-center justify-center rounded-xl bg-gradient-to-b from-amber-400/20 via-amber-500/10 to-amber-600/5 border-t border-t-amber-400/60 border-x border-x-amber-500/30 border-b border-b-amber-600/20 text-amber-400 shadow-[inset_0_1px_0_rgba(255,255,255,0.25),0_4px_12px_rgba(0,0,0,0.6),0_0_20px_rgba(245,158,11,0.2)] group transition-transform duration-200 hover:scale-[1.03]">
                    <TerminalSquare className="h-5.5 w-5.5 text-amber-400 drop-shadow-[0_0_8px_rgba(251,191,36,0.6)] transition-transform duration-200 group-hover:scale-105" />
                    <span className="absolute -top-1 -right-1 flex h-2.5 w-2.5">
                      <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-60" />
                      <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-amber-400 shadow-[0_0_8px_rgba(251,191,36,0.9)]" />
                    </span>
                  </div>

                  <div>
                    <h1 className="text-base font-bold tracking-[0.16em] text-zinc-100 uppercase font-mono">
                      Haunter
                    </h1>
                    <p className="text-[11px] text-zinc-400 mt-0.5 leading-relaxed">
                      Autonomous CI failure diagnosis and sandbox fix agent.
                    </p>
                  </div>
                </div>

                {/* Action Button & Security Assurance */}
                <div className="space-y-3.5 pt-0.5 relative z-10">
                  <Button
                    onClick={handleGitHubLogin}
                    disabled={isRedirecting}
                    className="relative w-full h-10.5 flex items-center justify-center gap-2.5 rounded-lg text-zinc-950 font-bold text-xs bg-white border border-zinc-200 shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400 focus-visible:ring-offset-2 focus-visible:ring-offset-[#101014] cursor-pointer disabled:opacity-80 active:opacity-90"
                  >
                    {isRedirecting ? (
                      <>
                        <Loader2 className="h-4 w-4 shrink-0 animate-spin text-zinc-950" />
                        <span className="text-xs font-semibold">Connecting to GitHub...</span>
                      </>
                    ) : (
                      <>
                        <GitHubIcon className="h-4.5 w-4.5 shrink-0 text-zinc-950" />
                        <span className="text-xs font-bold text-zinc-950">Continue with GitHub</span>
                        <ArrowRight className="h-3.5 w-3.5 ml-auto text-zinc-500" />
                      </>
                    )}
                  </Button>

                  {/* Scoped permission callout */}
                  <div className="rounded-lg border border-zinc-800/80 bg-zinc-900/50 p-2.5 space-y-1 shadow-[inset_0_1px_0_rgba(255,255,255,0.02)]">
                    <div className="flex items-center gap-1.5 font-mono text-[10.5px] text-zinc-300">
                      <ShieldCheck className="h-3.5 w-3.5 text-emerald-400 shrink-0 drop-shadow-[0_0_6px_rgba(52,211,153,0.5)]" />
                      <span className="font-semibold text-zinc-200">
                        OAuth `read:user` scope only
                      </span>
                    </div>
                    <p className="text-[9.5px] text-zinc-500 leading-normal pl-5">
                      OAuth only reads your username for session identity. Repository access is delegated exclusively through your explicit GitHub App installation.
                    </p>
                  </div>

                  {/* Security Guarantees */}
                  <div className="space-y-1 pt-0.5 text-[10.5px] text-zinc-400 font-mono">
                    <div className="flex items-center gap-2">
                      <CheckCircle2 className="h-3.5 w-3.5 text-amber-400/90 shrink-0" />
                      <span>Deterministic isolated sandbox execution</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <CheckCircle2 className="h-3.5 w-3.5 text-amber-400/90 shrink-0" />
                      <span>Zero token leakage or data retention</span>
                    </div>
                  </div>
                </div>

                {/* Minimal Footer Inside Card */}
                <div className="border-t border-zinc-800/80 pt-3 text-center relative z-10">
                  <div className="flex items-center justify-center gap-2">
                    <span className="h-1.5 w-1.5 rounded-full bg-amber-400/80 shadow-[0_0_6px_rgba(251,191,36,0.6)] animate-pulse" />
                    <span className="text-[9px] text-zinc-500 font-mono tracking-wide">
                      Haunter Engine v1.0 • Multi-Tenant
                    </span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </main>

      {/* Bottom Minimal Footer */}
      <footer className="relative z-20 flex h-10 shrink-0 w-full items-center justify-between border-t border-zinc-800/80 bg-zinc-950/40 px-6 text-[10px] font-mono text-zinc-500 backdrop-blur-md lg:px-12">
        <div>
          <span>Autonomous CI Healer • Enterprise Ready</span>
        </div>
        <div className="flex items-center gap-3 text-zinc-500">
          <span className="hover:text-zinc-300 transition-colors">Documentation</span>
          <span>•</span>
          <span className="hover:text-zinc-300 transition-colors">GitHub App</span>
          <span>•</span>
          <span className="hover:text-zinc-300 transition-colors">Security</span>
        </div>
      </footer>
    </div>
  );
}

