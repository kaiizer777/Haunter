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
      badge: "Live",
    },
    {
      name: "Model Config",
      href: "/config",
      icon: Sliders,
      badge: "Live",
    },
  ];

  return (
    <aside className="fixed inset-y-0 left-0 z-30 flex w-[264px] flex-col border-r border-zinc-800/80 bg-[#0c0c0e] select-none shadow-[1px_0_12px_rgba(0,0,0,0.4)]">
      {/* Brand Header */}
      <div className="flex h-16 items-center justify-between px-5 border-b border-zinc-800/80">
        <Link href="/runs" className="flex items-center gap-3 group">
          <div className="flex h-8.5 w-8.5 items-center justify-center rounded-[7px] bg-gradient-to-b from-amber-400/15 to-amber-500/5 border border-amber-500/30 text-amber-400 shadow-[0_0_12px_rgba(245,158,11,0.15)] group-hover:border-amber-400/60 group-hover:shadow-[0_0_16px_rgba(245,158,11,0.25)] transition-all">
            <TerminalSquare className="h-4.5 w-4.5" />
          </div>
          <div className="flex flex-col">
            <div className="flex items-center gap-1.5">
              <span className="text-sm font-bold tracking-wider text-zinc-100 uppercase">
                Haunter
              </span>
              <span className="h-1.5 w-1.5 rounded-full bg-amber-400/80 shadow-[0_0_6px_rgba(245,158,11,0.8)]" />
            </div>
            <span className="text-[11px] font-mono text-zinc-500 group-hover:text-zinc-400 transition-colors">
              Autonomous CI
            </span>
          </div>
        </Link>
        <span className="rounded-[5px] border border-zinc-800 bg-zinc-900/90 px-2 py-0.5 text-[11px] font-mono font-medium text-zinc-400 shadow-sm">
          v1.0
        </span>
      </div>

      {/* Navigation Links */}
      <div className="flex-1 overflow-y-auto px-3.5 py-4 space-y-1.5">
        <div className="px-2.5 pb-2 text-[11px] font-semibold uppercase tracking-wider text-zinc-500 font-mono">
          Navigation
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
              className={cn(
                "relative flex items-center justify-between rounded-[6px] px-3 py-2 text-[13px] font-medium transition-all duration-150 group",
                isActive
                  ? "bg-zinc-800/90 text-zinc-100 font-semibold border border-zinc-700/60 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.05)]"
                  : "text-zinc-400 hover:bg-zinc-900/80 hover:text-zinc-200 border border-transparent"
              )}
            >
              {/* Active left indicator pip */}
              {isActive && (
                <span className="absolute left-0 top-1/2 -translate-y-1/2 h-4 w-1 rounded-r-full bg-amber-400 shadow-[0_0_8px_rgba(251,191,36,0.8)]" />
              )}

              <div className="flex items-center gap-3">
                <Icon
                  className={cn(
                    "h-4 w-4 transition-transform duration-150 group-hover:scale-105",
                    isActive
                      ? "text-amber-400 drop-shadow-[0_0_6px_rgba(251,191,36,0.4)]"
                      : "text-zinc-400 group-hover:text-zinc-200"
                  )}
                />
                <span>{item.name}</span>
              </div>
              {item.badge && (
                <span
                  className={cn(
                    "flex items-center gap-1 rounded-[4px] border px-1.5 py-0.5 text-[10px] font-mono font-medium",
                    isActive
                      ? "border-amber-500/30 bg-amber-500/10 text-amber-300"
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
      </div>

      {/* Footer System Status */}
      <div className="p-3.5 border-t border-zinc-800/80 bg-[#09090b]">
        <div className="rounded-lg border border-zinc-800/90 bg-gradient-to-b from-[#131317] to-[#0c0c0f] p-3 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.03),0_2px_8px_rgba(0,0,0,0.3)]">
          <div className="flex items-center justify-between text-xs">
            <div className="flex items-center gap-2 text-zinc-300">
              <span className="relative flex h-2 w-2">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
                <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.8)]" />
              </span>
              <span className="font-medium tracking-tight">Pipeline Live</span>
            </div>
            <ShieldCheck className="h-3.5 w-3.5 text-emerald-400/80" />
          </div>

          <div className="mt-2.5 flex items-center justify-between text-[11px] font-mono text-zinc-500">
            <span>Active Model</span>
            <span
              className="rounded-[4px] bg-zinc-900/90 border border-zinc-700/60 px-1.5 py-0.5 text-zinc-200 truncate max-w-[125px] shadow-sm inline-block font-mono"
              title={activeModel}
            >
              {activeModel.replace("-free", "")}
            </span>
          </div>
        </div>
      </div>
    </aside>
  );
}
