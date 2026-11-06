import React from "react";
import { cn } from "@/lib/utils";
import { confidenceFromRisk, riskBand } from "../review-triage";

export interface ConfidenceGaugeProps {
  /** Raw 0-100 risk score reported by the backend analysis */
  riskScore: number;
  /** Outer diameter in pixels (default: 68) */
  size?: number;
  /** True for pending, suppressed, or failed runs that carry no computed verdict */
  neutral?: boolean;
  /** Whether to render the risk band status chip beneath the ring (default: true) */
  showChip?: boolean;
  /** Optional custom container class name */
  className?: string;
  /** Optional override for accessible name (defaults to "Confidence X of 10 — <band> risk", or "Confidence — of 10" when neutral) */
  ariaLabel?: string;
}

/**
 * Tactical radial confidence gauge.
 *
 * Renders model confidence on a 1-10 scale derived inversely from risk score:
 * risk 0-20 -> confidence 10-8/10 (green)
 * risk 31-70 -> confidence 7-3/10 (amber)
 * risk 71-100 -> confidence 3-1/10 (rose)
 *
 * Invariant: Accessible name defaults to `aria-label="Confidence X of 10 — <band> risk"`
 * (or `aria-label="Confidence — of 10"` when unscored/neutral).
 */
export function ConfidenceGauge({
  riskScore,
  size = 68,
  neutral = false,
  showChip = true,
  className,
  ariaLabel,
}: ConfidenceGaugeProps) {
  const strokeWidth = 5;
  const radius = (size - strokeWidth - 8) / 2;
  const circumference = 2 * Math.PI * radius;
  const clampedRisk = Math.max(0, Math.min(100, riskScore));
  const confidence = confidenceFromRisk(clampedRisk);
  const fraction = confidence / 10;
  const offset = neutral ? circumference : circumference * (1 - fraction);

  const band = riskBand(clampedRisk, !neutral);
  const compact = size <= 72;

  const accessibleName =
    ariaLabel ??
    (neutral
      ? "Confidence — of 10"
      : `Confidence ${confidence} of 10 — ${band.label.toLowerCase()} risk`);

  return (
    <div
      className={cn("flex flex-col items-center justify-center", className)}
      role="img"
      aria-label={accessibleName}
    >
      {/* Tactile circular bezel housing */}
      <div
        className="relative flex items-center justify-center rounded-full border border-zinc-800/90 bg-gradient-to-b from-[#18181b] via-[#111114] to-[#09090b] shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_2px_8px_rgba(0,0,0,0.5)]"
        style={{ width: size, height: size }}
      >
        <svg
          className="rotate-[-90deg]"
          width={size}
          height={size}
          aria-hidden="true"
        >
          {/* Subtle track background */}
          <circle
            cx={size / 2}
            cy={size / 2}
            r={radius}
            stroke="#27272a"
            strokeWidth={strokeWidth}
            fill="transparent"
          />
          {/* Active indicator arc */}
          <circle
            cx={size / 2}
            cy={size / 2}
            r={radius}
            stroke={band.stroke}
            strokeWidth={strokeWidth}
            fill="transparent"
            strokeDasharray={circumference}
            strokeDashoffset={offset}
            strokeLinecap="round"
            style={{
              transition: "stroke-dashoffset 0.6s cubic-bezier(0.16, 1, 0.3, 1)",
            }}
          />
        </svg>

        {/* Dual numeral + /10 denominator */}
        <div className="absolute inset-0 flex items-center justify-center pointer-events-none text-center">
          <div className="flex items-baseline justify-center">
            <span
              className={cn(
                "font-mono font-extrabold tabular-nums tracking-tight leading-none",
                compact ? "text-base" : "text-2xl",
                band.text
              )}
            >
              {neutral ? "—" : confidence}
            </span>
            {!neutral && (
              <span
                className={cn(
                  "ml-0.5 font-mono font-bold text-zinc-500 leading-none",
                  compact ? "text-[9px]" : "text-xs"
                )}
              >
                /10
              </span>
            )}
          </div>
        </div>
      </div>

      {/* Risk band status chip */}
      {showChip && (
        <span
          className={cn(
            "mt-1.5 rounded-[4px] border px-2 py-0.5 text-[10px] font-mono font-bold tracking-wider uppercase shadow-[inset_0_1px_0_rgba(255,255,255,0.06)]",
            band.chip
          )}
        >
          {band.chipLabel}
        </span>
      )}
    </div>
  );
}
