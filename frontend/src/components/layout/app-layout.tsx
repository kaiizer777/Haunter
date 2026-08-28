"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth-context";
import { Sidebar } from "@/components/layout/sidebar";
import { Topbar } from "@/components/layout/topbar";
import { Skeleton } from "@/components/ui/skeleton";

interface AppLayoutProps {
  children: React.ReactNode;
  title: string;
  subtitle?: string;
  actions?: React.ReactNode;
}

export function AppLayout({
  children,
  title,
  subtitle,
  actions,
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
      <div className="flex min-h-screen bg-[#09090b]">
        {/* Skeleton Sidebar */}
        <div className="w-60 border-r border-zinc-800 bg-[#0c0c0e] p-4 space-y-4">
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
    <div className="min-h-screen bg-[#09090b] text-zinc-100 flex flex-row">
      {/* Fixed Sidebar */}
      <Sidebar />

      {/* Main Content Area */}
      <div className="flex-1 flex flex-col pl-60 min-h-screen">
        <Topbar title={title} subtitle={subtitle} actions={actions} />
        <main className="flex-1 p-6 overflow-y-auto">
          {children}
        </main>
      </div>
    </div>
  );
}
