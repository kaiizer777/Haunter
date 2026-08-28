import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[11px] font-medium tracking-tight select-none border transition-colors",
  {
    variants: {
      variant: {
        default:
          "border-zinc-700/80 bg-zinc-800/80 text-zinc-200",
        secondary:
          "border-zinc-800 bg-zinc-900 text-zinc-400",
        success:
          "border-emerald-500/30 bg-emerald-950/50 text-emerald-400",
        warning:
          "border-amber-500/30 bg-amber-950/50 text-amber-400",
        destructive:
          "border-red-500/30 bg-red-950/50 text-red-400",
        outline:
          "border-zinc-700 text-zinc-300 bg-transparent",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return (
    <div className={cn(badgeVariants({ variant }), className)} {...props} />
  );
}

export { Badge, badgeVariants };
