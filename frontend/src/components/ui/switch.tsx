"use client";

import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * Haunter Switch — a small, dark-themed toggle matching the dashboard's
 * zinc/amber palette. Modeled after the inline checkbox used in the eval
 * modal (`frontend/src/app/eval/page.tsx`) but with a track + thumb that
 * reads as a real switch at small sizes.
 *
 * Usage:
 *   <Switch checked={enabled} onCheckedChange={setEnabled} label="Demo mode" />
 *   <Switch checked={...} onCheckedChange={...} label="..." tooltip="..." />
 */
export interface SwitchProps {
  checked: boolean;
  onCheckedChange: (next: boolean) => void;
  label?: React.ReactNode;
  /** Inline description rendered under the label in muted text. */
  description?: React.ReactNode;
  /** Tooltip text shown on hover (rendered via the native `title` attr). */
  tooltip?: string;
  disabled?: boolean;
  id?: string;
  className?: string;
}

export const Switch = React.forwardRef<HTMLButtonElement, SwitchProps>(
  function Switch(
    { checked, onCheckedChange, label, description, tooltip, disabled, id, className },
    ref,
  ) {
    const handleClick = React.useCallback(() => {
      if (!disabled) onCheckedChange(!checked);
    }, [checked, disabled, onCheckedChange]);

    const handleKeyDown = React.useCallback(
      (e: React.KeyboardEvent<HTMLButtonElement>) => {
        if (e.key === " " || e.key === "Enter") {
          e.preventDefault();
          handleClick();
        }
      },
      [handleClick],
    );

    const button = (
      <button
        ref={ref}
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label={typeof label === "string" ? label : undefined}
        title={tooltip}
        disabled={disabled}
        onClick={handleClick}
        onKeyDown={handleKeyDown}
        className={cn(
          "relative inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border transition-colors duration-150",
          "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-amber-400",
          "disabled:cursor-not-allowed disabled:opacity-50",
          checked
            ? "border-amber-400/70 bg-amber-400"
            : "border-zinc-700 bg-zinc-800",
          className,
        )}
      >
        <span
          className={cn(
            "inline-block h-3.5 w-3.5 transform rounded-full bg-zinc-950 shadow-sm transition-transform duration-150",
            checked ? "translate-x-[18px] bg-zinc-950" : "translate-x-[3px]",
          )}
        />
      </button>
    );

    if (!label && !description) {
      return button;
    }

    return (
      <div className="flex items-center gap-2.5">
        {button}
        {(label || description) && (
          <label
            htmlFor={id}
            className={cn(
              "flex flex-col leading-tight",
              disabled ? "cursor-not-allowed opacity-60" : "cursor-pointer",
            )}
            onClick={disabled ? undefined : handleClick}
          >
            {label && (
              <span className="text-xs font-medium text-zinc-200">{label}</span>
            )}
            {description && (
              <span className="text-[10px] font-mono text-zinc-500">
                {description}
              </span>
            )}
          </label>
        )}
      </div>
    );
  },
);
