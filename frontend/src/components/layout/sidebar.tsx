"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Activity,
  GitBranch,
  Sparkles,
  Sliders,
  ShieldCheck,
  TerminalSquare,
  Cpu,
} from "lucide-react";
import { api, ModelConfigOut } from "@/lib/api";
import { cn } from "@/lib/utils";

export function Sidebar() {
  const pathname = usePathname();
  const [activeModel, setActiveModel] = useState<string>("nemotron-3.5-lightning-free");

  useEffect(() => {
    let isMounted = true;
    api.getModelConfig()
      .then((cfg: ModelConfigOut) => {
        if (isMounted && cfg?.model_name) {
          setActiveModel(cfg.model_name);
        }
      })
      .catch(() => {});
    return () => {
      isMounted = false;
    };
  }, []);

  // Keyboard shortcut listener (Cmd/Ctrl/Alt + 1-4) for rapid developer navigation
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable)
      ) {
        return;
      }

      if ((e.metaKey || e.ctrlKey || e.altKey) && !e.shiftKey) {
        const keyMap: Record<string, string> = {
          "1": "/runs",
          "2": "/repos",
          "3": "/eval",
          "4": "/config",
        };
        const targetHref = keyMap[e.key];
        if (targetHref) {
          e.preventDefault();
          const targetEl = document.querySelector<HTMLAnchorElement>(
            `aside nav a[href="${targetHref}"]`
          );
          targetEl?.click();
        }
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  const navItems = [
    {
      name: "Runs",
      href: "/runs",
      icon: Activity,
    },
    {
      name: "Repositories",
      href: "/repos",
      icon: GitBranch,
    },
    {
      name: "AI Reliability & Evals",
      href: "/eval",
      icon: Sparkles,
    },
    {
      name: "Model Config",
      href: "/config",
      icon: Sliders,
    },
  ];

  return (
    <aside
      aria-label="Sidebar"
      className="fixed inset-y-0 left-0 z-30 flex w-[264px] flex-col border-r border-zinc-800/80 bg-gradient-to-b from-[#0c0c0f] via-[#0a0a0d] to-[#070709] select-none shadow-[1px_0_24px_rgba(0,0,0,0.5)] backdrop-blur-xl"
    >
      {/* Ambient decorative glow at top-left corner */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute -top-16 -left-16 h-36 w-36 rounded-full bg-amber-500/5 blur-2xl"
      />

      {/* Brand Header */}
      <div className="flex h-16 items-center justify-between px-4 border-b border-zinc-800/80 bg-zinc-950/40 backdrop-blur-md relative z-10">
        <Link
          href="/runs"
          className="flex items-center gap-3 group focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-amber-400/80 rounded-lg p-1 -ml-1 transition-all"
        >
          <div className="relative flex h-8.5 w-8.5 items-center justify-center rounded-lg bg-gradient-to-b from-amber-400/20 via-amber-500/10 to-amber-600/5 border border-amber-500/30 text-amber-400 shadow-[inset_0_1px_0_rgba(255,255,255,0.2),0_2px_8px_rgba(0,0,0,0.4)] group-hover:border-amber-400/60 group-hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.3),0_0_16px_rgba(245,158,11,0.25)] transition-all duration-200">
            <TerminalSquare className="h-4.5 w-4.5 transition-transform duration-200 group-hover:scale-105" />
          </div>
          <div className="flex flex-col">
            <div className="flex items-center gap-1.5">
              <span className="text-xs font-bold tracking-[0.14em] text-zinc-100 uppercase">
                Haunter
              </span>
              <span className="relative flex h-1.5 w-1.5">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-60" />
                <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-amber-400 shadow-[0_0_8px_rgba(251,191,36,0.9)]" />
              </span>
            </div>
            <span className="text-[11px] font-mono text-zinc-400 group-hover:text-zinc-300 transition-colors">
              Autonomous CI
            </span>
          </div>
        </Link>
        <span className="rounded-[5px] border border-zinc-700/60 bg-zinc-900/90 px-1.5 py-0.5 text-[10px] font-mono font-medium text-zinc-300 shadow-[inset_0_1px_0_rgba(255,255,255,0.06)]">
          v1.0
        </span>
      </div>

      {/* Navigation Section */}
      <nav aria-label="Main Navigation" className="flex-1 overflow-y-auto px-3 py-4 space-y-1.5">
        <div className="flex items-center justify-between px-2.5 pb-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-zinc-500 font-mono">
          <span>Navigation</span>
          <span className="text-[9px] text-zinc-600 font-mono">CI / CD</span>
        </div>
        {navItems.map((item) => {
          const isActive =
            pathname === item.href ||
            (item.href !== "/" && pathname.startsWith(item.href));
          const Icon = item.icon;

          return (
            <Link
              key={item.href}
              href={item.href}
              aria-current={isActive ? "page" : undefined}
              className={cn(
                "relative flex items-center justify-between rounded-[6px] px-3.5 py-2.5 text-sm font-medium transition-all duration-150 group select-none focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-amber-400/80 focus-visible:ring-offset-1 focus-visible:ring-offset-[#0c0c0e] active:translate-y-[0.5px]",
                isActive
                  ? "bg-zinc-800/90 text-zinc-100 font-semibold border-t border-t-zinc-600/70 border-x border-x-zinc-700/60 border-b border-b-zinc-800/80 shadow-[inset_0_1px_0_rgba(255,255,255,0.12),0_2px_5px_rgba(0,0,0,0.35)] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.4)]"
                  : "text-zinc-400 hover:text-zinc-100 hover:bg-zinc-800/40 border border-transparent hover:border-t-zinc-700/60 hover:border-x-zinc-800/60 hover:border-b-zinc-900/80 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_2px_5px_rgba(0,0,0,0.25)] hover:-translate-y-[0.5px] active:shadow-[inset_0_2px_4px_rgba(0,0,0,0.35)]"
              )}
            >
              {/* Active left indicator pip & 3D painted light depth overlay */}
              {isActive && (
                <>
                  <span className="absolute left-0 top-1/2 -translate-y-1/2 h-5.5 w-[3px] rounded-r-full bg-gradient-to-b from-amber-300 via-amber-400 to-amber-500 shadow-[0_0_8px_rgba(251,191,36,0.85)]" />
                  <span
                    aria-hidden="true"
                    className="pointer-events-none absolute inset-0 rounded-[6px] bg-gradient-to-b from-white/[0.05] via-amber-400/[0.02] to-black/[0.1] shadow-[inset_0_1px_0_rgba(255,255,255,0.1)]"
                  />
                </>
              )}

              <div className="flex items-center gap-3">
                <Icon
                  className={cn(
                    "h-4.5 w-4.5 transition-all duration-150 shrink-0",
                    isActive
                      ? "text-amber-400 drop-shadow-[0_0_8px_rgba(251,191,36,0.5)] scale-[1.02]"
                      : "text-zinc-400 group-hover:text-zinc-200 group-hover:scale-105"
                  )}
                />
                <span className="tracking-tight transition-colors duration-150">{item.name}</span>
              </div>

              {item.badge && (
                <span
                  className={cn(
                    "flex items-center gap-1 rounded-[4px] border px-1.5 py-0.5 text-[10px] font-mono font-medium",
                    isActive
                      ? "border-amber-500/30 bg-amber-500/10 text-amber-300 shadow-[0_0_6px_rgba(245,158,11,0.15)]"
                      : "border-zinc-800 bg-zinc-900/90 text-zinc-500 group-hover:text-zinc-400 group-hover:border-zinc-700/60"
                  )}
                >
                  <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse" />
                  {item.badge}
                </span>
              )}
            </Link>
          );
        })}
      </nav>

      {/* Footer System Telemetry Status */}
      <div className="p-3.5 border-t border-zinc-800/80 bg-[#09090b]/80 backdrop-blur-md">
        <div className="relative overflow-hidden rounded-xl border-t border-t-zinc-600/60 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-gradient-to-b from-[#16161b] via-[#111115] to-[#0b0b0e] p-3.5 shadow-[inset_0_1px_0_rgba(255,255,255,0.12),inset_0_-1px_0_rgba(0,0,0,0.4),0_4px_16px_rgba(0,0,0,0.5),0_1px_2px_rgba(0,0,0,0.3)] group transition-all duration-200">
          {/* Subtle light-from-above ambient gradient sheen */}
          <span
            aria-hidden="true"
            className="pointer-events-none absolute inset-x-0 top-0 h-6 bg-gradient-to-b from-white/[0.04] to-transparent"
          />

          <div className="flex items-center justify-between text-xs">
            <div className="flex items-center gap-2 text-zinc-200">
              <span className="relative flex h-2 w-2">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
                <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.8)]" />
              </span>
              <span className="font-semibold tracking-tight text-zinc-200">Pipeline Live</span>
            </div>
            <div className="flex items-center gap-1 rounded-[5px] bg-gradient-to-b from-emerald-500/15 via-emerald-500/10 to-emerald-500/5 border-t border-t-emerald-400/40 border-x border-x-emerald-500/25 border-b border-b-emerald-600/20 px-1.5 py-0.5 text-[10px] font-mono text-emerald-300 shadow-[inset_0_1px_0_rgba(255,255,255,0.15),0_1px_2px_rgba(0,0,0,0.3)]">
              <ShieldCheck className="h-3 w-3" />
              <span>99.9%</span>
            </div>
          </div>

          <div className="my-2.5 h-px w-full bg-gradient-to-r from-transparent via-zinc-700/50 to-transparent" />

          <div className="flex items-center justify-between text-[11px] font-mono text-zinc-400">
            <div className="flex items-center gap-1.5">
              <Cpu className="h-3.5 w-3.5 text-zinc-500" />
              <span>Active Model</span>
            </div>
            <span
              className="rounded-[5px] bg-gradient-to-b from-zinc-900 via-zinc-900/95 to-zinc-950 border-t border-t-zinc-600/70 border-x border-x-zinc-700/60 border-b border-b-zinc-900 px-2 py-0.5 text-zinc-200 truncate max-w-[125px] shadow-[inset_0_1px_0_rgba(255,255,255,0.1),0_1px_2px_rgba(0,0,0,0.4)] inline-block font-mono text-right font-medium hover:border-amber-500/50 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.15),0_0_8px_rgba(245,158,11,0.15)] transition-all"
              title={activeModel}
            >
              {activeModel.replace("-free", "")}
            </span>
          </div>

          <div className="mt-2.5 pt-2 border-t border-zinc-800/70 flex items-center justify-between text-[10px] font-mono text-zinc-500">
            <div className="flex items-center gap-1.5">
              <span className="h-1.5 w-1.5 rounded-full bg-amber-400/80 shadow-[0_0_6px_rgba(245,158,11,0.5)]" />
              <span>Sandbox</span>
            </div>
            <span className="text-zinc-300 rounded-[4px] bg-zinc-900/70 border border-zinc-800/80 px-1.5 py-0.5 text-[9px] font-mono shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]">
              Isolated Mirror
            </span>
          </div>
        </div>
      </div>
    </aside>
  );
}
