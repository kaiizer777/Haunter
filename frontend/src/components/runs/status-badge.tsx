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

  switch (normalized) {
    case "completed":
    case "pr_opened":
    case "passed":
      dotColor = "bg-emerald-400";
      variant = "success";
      label = "PR Opened";
      break;

    case "fallback":
    case "fallback_commented":
      dotColor = "bg-amber-400";
      variant = "warning";
      label = "Fallback Comment";
      break;

    case "error":
    case "failed":
      dotColor = "bg-red-400";
      variant = "destructive";
      label = "Failed";
      break;

    case "pending":
      dotColor = "bg-zinc-400";
      variant = "secondary";
      label = "Pending";
      isPulsing = true;
      break;

    case "context_gathering":
      dotColor = "bg-blue-400";
      variant = "secondary";
      label = "Gathering Context";
      isPulsing = true;
      break;

    case "fix_generation":
      dotColor = "bg-amber-400";
      variant = "warning";
      label = "Generating Fix";
      isPulsing = true;
      break;

    case "verification":
    case "pending_verification":
      dotColor = "bg-purple-400";
      variant = "secondary";
      label = "Verifying Sandbox";
      isPulsing = true;
      break;

    case "pending_pr":
      dotColor = "bg-emerald-400";
      variant = "success";
      label = "Writing PR";
      isPulsing = true;
      break;

    default:
      label = status.replace(/_/g, " ");
      break;
  }

  return (
    <Badge variant={variant} className={cn("capitalize font-mono text-[10px]", className)}>
      {showDot && (
        <span
          className={cn(
            "h-1.5 w-1.5 rounded-full inline-block mr-1",
            dotColor,
            isPulsing && "animate-ping"
          )}
        />
      )}
      {label}
    </Badge>
  );
}
