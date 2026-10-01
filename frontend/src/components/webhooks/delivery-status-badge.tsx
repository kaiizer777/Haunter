import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

/**
 * Webhook decision outcomes recorded by the ingestion handler.
 *
 * Colour is never the only signal — every badge carries its label, and the
 * dot shape differs between "work was started" and "nothing was started" so
 * the distinction survives a greyscale render.
 */
const STATUS_STYLES: Record<
  string,
  { label: string; dot: string; classes: string; pulse?: boolean }
> = {
  queued: {
    label: "Queued",
    dot: "bg-emerald-400",
    classes: "border-emerald-500/20 bg-emerald-500/[0.08] text-emerald-400",
  },
  duplicate: {
    label: "Duplicate",
    dot: "bg-zinc-400",
    classes: "border-zinc-700/40 bg-zinc-800/40 text-zinc-300",
  },
  ignored: {
    label: "Ignored",
    dot: "bg-amber-400",
    classes: "border-amber-500/20 bg-amber-500/[0.08] text-amber-400",
  },
  skipped: {
    label: "Skipped",
    dot: "bg-amber-400",
    classes: "border-amber-500/20 bg-amber-500/[0.08] text-amber-400",
  },
  audit_queued: {
    label: "Audit Queued",
    dot: "bg-cyan-400",
    classes: "border-cyan-500/20 bg-cyan-500/[0.08] text-cyan-400",
    pulse: true,
  },
  replayed: {
    label: "Replayed",
    dot: "bg-purple-400",
    classes: "border-purple-500/20 bg-purple-500/[0.08] text-purple-300",
  },
};

export function DeliveryStatusBadge({
  status,
  className,
}: {
  status: string;
  className?: string;
}) {
  const style = STATUS_STYLES[status.toLowerCase()] ?? {
    label: status.replace(/_/g, " "),
    dot: "bg-zinc-500",
    classes: "border-zinc-700/40 bg-zinc-800/40 text-zinc-400",
  };

  return (
    <Badge
      data-testid={`delivery-status-${status}`}
      className={cn(
        "h-5 px-2 py-0 text-[10px] font-mono font-medium tracking-tight rounded-[4px] border inline-flex items-center whitespace-nowrap",
        style.classes,
        className
      )}
    >
      <span className="relative inline-flex items-center justify-center mr-1.5 shrink-0">
        {style.pulse && (
          <span
            aria-hidden="true"
            className={cn(
              "animate-ping absolute inline-flex h-1.5 w-1.5 rounded-full opacity-75",
              style.dot
            )}
          />
        )}
        <span
          aria-hidden="true"
          className={cn(
            "h-1.5 w-1.5 rounded-full inline-block shadow-[0_0_6px_currentColor]",
            style.dot
          )}
        />
      </span>
      {style.label}
    </Badge>
  );
}