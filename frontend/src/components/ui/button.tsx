import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center whitespace-nowrap rounded-[6px] text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-amber-400 disabled:pointer-events-none disabled:opacity-50 select-none",
  {
    variants: {
      variant: {
        default:
          "bg-zinc-50 text-zinc-950 hover:bg-zinc-200 active:bg-zinc-300 font-semibold shadow-none",
        secondary:
          "bg-zinc-800 text-zinc-100 hover:bg-zinc-700 active:bg-zinc-700/80 border border-zinc-700/60",
        destructive:
          "bg-red-950 text-red-300 border border-red-800/80 hover:bg-red-900/80 active:bg-red-900",
        outline:
          "border border-zinc-800 bg-transparent text-zinc-300 hover:bg-zinc-800/60 hover:text-zinc-100 active:bg-zinc-800",
        ghost:
          "text-zinc-400 hover:bg-zinc-800/60 hover:text-zinc-100 active:bg-zinc-800",
        link: "text-zinc-400 underline-offset-4 hover:underline hover:text-zinc-100",
      },
      size: {
        default: "h-8 px-3 py-1.5",
        sm: "h-7 px-2.5 text-[11px]",
        lg: "h-9 px-4 text-sm",
        icon: "h-8 w-8 p-0",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, ...props }, ref) => {
    return (
      <button
        className={cn(buttonVariants({ variant, size, className }))}
        ref={ref}
        {...props}
      />
    );
  }
);
Button.displayName = "Button";

export { Button, buttonVariants };
