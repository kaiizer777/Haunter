"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth-context";
import { Sidebar } from "@/components/layout/sidebar";
import { Topbar } from "@/components/layout/topbar";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

interface AppLayoutProps {
  children: React.ReactNode;
  title: string;
  subtitle?: string;
  actions?: React.ReactNode;
  noPadding?: boolean;
  fullHeight?: boolean;
}

export function AppLayout({
  children,
  title,
  subtitle,
  actions,
  noPadding = false,
  fullHeight = false,
}: AppLayoutProps) {
  const { user, loading } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!loading && !user) {
      router.replace("/login");
    }
  }, [user, loading, router]);

  if (loading) {
    return (
      <div
        className={cn(
          "flex bg-[#09090b]",
          fullHeight ? "h-screen h-[100dvh] overflow-hidden" : "min-h-screen"
        )}
      >
        {/* Skeleton Sidebar */}
        <div className="w-[240px] border-r border-zinc-800 bg-[#0c0c0e] p-3.5 space-y-3.5">
          <div className="flex items-center gap-3">
            <Skeleton className="h-7 w-7 rounded-[5px]" />
            <Skeleton className="h-4 w-24" />
          </div>
          <div className="space-y-2 pt-6">
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-full" />
          </div>
        </div>

        {/* Skeleton Main */}
        <div className="flex-1 flex flex-col">
          <div className="h-14 border-b border-zinc-800 px-6 flex items-center justify-between">
            <Skeleton className="h-5 w-32" />
            <Skeleton className="h-6 w-20 rounded-full" />
          </div>
          <div className="p-6 space-y-4 flex-1">
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-64 w-full" />
          </div>
        </div>
      </div>
    );
  }

  if (!user) {
    return null;
  }

  return (
    <div
      className={cn(
        "bg-[#09090b] text-zinc-100 flex flex-row",
        fullHeight ? "h-screen h-[100dvh] overflow-hidden" : "min-h-screen"
      )}
    >
      {/* Fixed Sidebar */}
      <Sidebar />

      {/* Main Content Area */}
      <div
        className={cn(
          "flex-1 flex flex-col min-w-0 pl-[240px]",
          fullHeight ? "h-full overflow-hidden" : "min-h-screen"
        )}
      >
        <Topbar title={title} subtitle={subtitle} actions={actions} />
        <main
          className={cn(
            "flex-1 min-w-0",
            fullHeight
              ? "min-h-0 p-5 flex flex-col overflow-hidden"
              : noPadding
              ? "p-0 flex flex-col overflow-hidden"
              : "p-5 overflow-y-auto"
          )}
        >
          {children}
        </main>
      </div>
    </div>
  );
}
