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
  TerminalSquare
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
    <aside className="fixed inset-y-0 left-0 z-30 flex w-[264px] flex-col border-r border-zinc-800 bg-[#0c0c0e] select-none">
      {/* Brand Header */}
      <div className="flex h-16 items-center justify-between px-5 border-b border-zinc-800/80">
        <Link href="/runs" className="flex items-center gap-3 group">
          <div className="flex h-8 w-8 items-center justify-center rounded-[6px] bg-zinc-900 border border-zinc-700/70 text-amber-400 group-hover:border-amber-400/80 transition-colors">
            <TerminalSquare className="h-4.5 w-4.5" />
          </div>
          <div className="flex flex-col">
            <span className="text-sm font-bold tracking-wider text-zinc-100 uppercase">
              Haunter
            </span>
            <span className="text-[11px] font-mono text-zinc-500">
              Autonomous CI
            </span>
          </div>
        </Link>
        <span className="rounded-[5px] border border-zinc-800 bg-zinc-900 px-2 py-0.5 text-[11px] font-mono text-zinc-400">
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
                "flex items-center justify-between rounded-[6px] px-3 py-2 text-[13px] font-medium transition-colors",
                isActive
                  ? "bg-zinc-800/90 text-zinc-100 font-semibold border border-zinc-700/60"
                  : "text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200"
              )}
            >
              <div className="flex items-center gap-3">
                <Icon
                  className={cn(
                    "h-4 w-4",
                    isActive ? "text-amber-400" : "text-zinc-400"
                  )}
                />
                <span>{item.name}</span>
              </div>
              {item.badge && (
                <span className="rounded-[4px] border border-zinc-800 bg-zinc-900/90 px-1.5 py-0.5 text-[10px] font-mono text-zinc-500">
                  {item.badge}
                </span>
              )}
            </Link>
          );
        })}
      </div>

      {/* Footer System Status */}
      <div className="p-3.5 border-t border-zinc-800/80 bg-[#09090b]">
        <div className="rounded-[7px] border border-zinc-800/80 bg-[#121215] p-3">
          <div className="flex items-center justify-between text-xs">
            <div className="flex items-center gap-2 text-zinc-300">
              <span className="h-2 w-2 rounded-full bg-emerald-400 animate-pulse" />
              <span className="font-medium">Pipeline Live</span>
            </div>
            <ShieldCheck className="h-3.5 w-3.5 text-zinc-500" />
          </div>
          <div className="mt-2 flex items-center justify-between text-[11px] font-mono text-zinc-500">
            <span>Active Model</span>
            <span className="text-zinc-300 truncate max-w-[125px]" title={activeModel}>
              {activeModel.replace("-free", "")}
            </span>
          </div>
        </div>
      </div>
    </aside>
  );
}
