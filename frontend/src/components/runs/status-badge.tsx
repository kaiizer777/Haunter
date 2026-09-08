import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

interface StatusBadgeProps {
  status: string;
  className?: string;
  showDot?: boolean;
}

export function StatusBadge({ status, className, showDot = true }: StatusBadgeProps) {
  const normalized = status.toLowerCase();

  let dotColor = "bg-zinc-400";
  let variant: "default" | "secondary" | "success" | "warning" | "destructive" | "outline" = "default";
  let label = status;
  let isPulsing = false;

  let colorClasses = "border-zinc-700/40 bg-zinc-800/40 text-zinc-300";

  switch (normalized) {
    case "completed":
    case "pr_opened":
    case "passed":
      dotColor = "bg-emerald-400";
      colorClasses = "border-emerald-500/20 bg-emerald-500/[0.08] text-emerald-400";
      variant = "success";
      label = "PR Opened";
      break;

    case "fallback":
    case "fallback_commented":
      dotColor = "bg-amber-400";
      colorClasses = "border-amber-500/20 bg-amber-500/[0.08] text-amber-400";
      variant = "warning";
      label = "Fallback Comment";
      break;

    case "error":
    case "failed":
      dotColor = "bg-red-400";
      colorClasses = "border-rose-500/20 bg-rose-500/[0.08] text-rose-400";
      variant = "destructive";
      label = "Failed";
      break;

    case "pending":
      dotColor = "bg-zinc-400";
      colorClasses = "border-zinc-700/40 bg-zinc-800/40 text-zinc-400";
      variant = "secondary";
      label = "Pending";
      isPulsing = true;
      break;

    case "context_gathering":
      dotColor = "bg-blue-400";
      colorClasses = "border-blue-500/20 bg-blue-500/[0.08] text-blue-400";
      variant = "secondary";
      label = "Gathering Context";
      isPulsing = true;
      break;

    case "fix_generation":
      dotColor = "bg-amber-400";
      colorClasses = "border-amber-500/20 bg-amber-500/[0.08] text-amber-400";
      variant = "warning";
      label = "Generating Fix";
      isPulsing = true;
      break;

    case "verification":
    case "pending_verification":
      dotColor = "bg-purple-400";
      colorClasses = "border-purple-500/20 bg-purple-500/[0.08] text-purple-400";
      variant = "secondary";
      label = "Verifying Sandbox";
      isPulsing = true;
      break;

    case "pending_pr":
      dotColor = "bg-emerald-400";
      colorClasses = "border-emerald-500/20 bg-emerald-500/[0.08] text-emerald-400";
      variant = "success";
      label = "Writing PR";
      isPulsing = true;
      break;

    default:
      label = status.replace(/_/g, " ");
      break;
  }

  return (
    <Badge
      variant={variant}
      className={cn(
        "h-5 px-2 py-0 text-[10px] font-mono font-medium tracking-tight rounded-[4px] border inline-flex items-center whitespace-nowrap",
        colorClasses,
        className
      )}
    >
      {showDot && (
        <span className="relative inline-flex items-center justify-center mr-1.5 shrink-0">
          {isPulsing && (
            <span
              className={cn(
                "animate-ping absolute inline-flex h-1.5 w-1.5 rounded-full opacity-75",
                dotColor
              )}
            />
          )}
          <span
            className={cn(
              "h-1.5 w-1.5 rounded-full inline-block shadow-[0_0_6px_currentColor]",
              dotColor
            )}
          />
        </span>
      )}
      {label}
    </Badge>
  );
}
