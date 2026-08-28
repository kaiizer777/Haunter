import { cn } from "@/lib/utils";

interface ConfidenceBarProps {
  score: number | null | undefined;
  className?: string;
}

export function ConfidenceBar({ score, className }: ConfidenceBarProps) {
  if (score === null || score === undefined) {
    return <span className="text-zinc-600 font-mono text-xs">—</span>;
  }

  // Bound to 0-100
  const normalized = Math.max(0, Math.min(100, score));

  return (
    <div className={cn("flex items-center gap-2", className)}>
      <div className="h-1.5 w-16 rounded-full bg-zinc-800 overflow-hidden">
        <div
          className={cn(
            "h-full transition-all duration-300",
            normalized >= 80
              ? "bg-emerald-400"
              : normalized >= 50
              ? "bg-amber-400"
              : "bg-red-400"
          )}
          style={{ width: `${normalized}%` }}
        />
      </div>
      <span className="font-mono text-xs text-zinc-300 w-7 text-right">
        {normalized}%
      </span>
    </div>
  );
}
