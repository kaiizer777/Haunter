"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth-context";
import { Skeleton } from "@/components/ui/skeleton";

export default function RootPage() {
  const { user, loading } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!loading) {
      if (user) {
        router.replace("/runs");
      } else {
        router.replace("/login");
      }
    }
  }, [user, loading, router]);

  return (
    <div className="flex min-h-screen items-center justify-center bg-[#09090b]">
      <div className="flex flex-col items-center gap-3">
        <div className="h-7 w-7 rounded-[5px] bg-zinc-900 border border-zinc-800 animate-pulse" />
        <Skeleton className="h-4 w-28" />
      </div>
    </div>
  );
}
