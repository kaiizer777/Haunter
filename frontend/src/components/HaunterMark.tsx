import { useEffect, useId, useState } from "react";
import { cn } from "@/lib/utils";
import styles from "./HaunterMark.module.css";

interface HaunterMarkProps {
  /** Outer coin diameter in px. The glyph scales proportionally inside it. */
  size?: number;
  className?: string;
}

/* ---------------------------------------------------------------------------
 * Scanline field geometry (32x32 user-unit viewBox).
 *
 * DRIFT is one full "brightness cycle" of the line stack: 4 lines x 3.2 units
 * = 12.8 units. Translating the field by exactly that amount is visually
 * identical to its start position, so the drift loop is seamless.
 * ------------------------------------------------------------------------- */
const PITCH = 3.2;
const DRIFT = 4 * PITCH; // 12.8 user units — must match haunterDrift in the CSS module.

/* Per-line opacity cycle, indexed by line number modulo this array length.
   The irregular values keep the field from reading as an even barcode, and the
   4-entry length is what makes DRIFT above land on an identical pattern. */
const LINE_ALPHA = [0.95, 0.3, 0.62, 0.16];

const FIELD_HEIGHT = 32;

/** Enough lines to cover the 32-unit box plus TWO full DRIFTs of headroom — one
 *  above y=0 and one below the fold. With only one drift of headroom the field
 *  runs out of lines partway through the loop and an empty band sweeps up into
 *  the silhouette. */
const LINE_COUNT = Math.ceil((FIELD_HEIGHT + 2 * DRIFT) / PITCH) + 1;

/* --- Ghost silhouette -----------------------------------------------------
 * Two paths, IDENTICAL SVG command structure so they interpolate cleanly:
 * both are `M` + 6 x `C` (same argument count, same order).
 *
 *   A — tall lean: crown narrow and high, waist drawn in, tail flicking left.
 *   B — fuller curl: crown wide and low, waist heavy, tail swinging right.
 *
 * These are genuinely different silhouettes (different bounding box, different
 * waist position, opposite tail direction), not a nudge — which is the point:
 * the FORM has to visibly change, not just the texture inside it.
 * ------------------------------------------------------------------------- */
const GHOST_A =
  "M16 2.6c4.3 0 7.7 3.3 7.7 7.6 0 2.7-1.2 4.4-2.5 6.3-1.3 1.9-1.8 3.6-1.3 5.6.4 1.7 1.9 3.4 1.6 5.2-.2 1.3-1.4 1.8-2.4 1-1.5-1.2-2.4-3-3.5-4.7-1.1-1.7-2.1-2.6-3.6-2.9-1.9-.4-3.6.7-4.5 2.5-.6 1.3-1.8 2.2-2.9 1.7-1-.5-1-1.9-.5-3 .7-1.6 1.2-3 1.2-4.7 0-2.1-.9-3.9-2.2-5.5C6.5 8.2 8.6 4.6 11.3 3.6 12.8 3 14.4 2.6 16 2.6Z";

const GHOST_B =
  "M16 4.2c5 0 8.7 3.6 8.4 7.9-.2 2.8-1.7 4.4-3.2 6.1-1.5 1.7-2.2 3.4-1.8 5.5.3 1.8.2 4-.7 5.6-.7 1.2-2.1 1.3-3 .3-1.3-1.5-1.9-3.4-2.6-5.2-.7-1.8-1.5-2.8-3-3.4-1.9-.8-3.8-.1-5.1 1.5-.8 1-2.2 1.5-3 .8-.8-.8-.6-2.1.2-3.1 1.1-1.4 2-2.7 2.1-4.4.1-2.1-.5-4-1.6-5.7C5.6 9.5 7.3 5.4 10 4.3 11.8 3.6 14 4.2 16 4.2Z";

/** SMIL morph period. Must equal haunterSway (11s) so the outline and the lean
 *  realign together. A is both the first and last frame, so the morph returns
 *  to its exact start geometry. */
const MORPH_DUR = "11s";

/**
 * Haunter brand mark — "Spectral Wisp" (animated form).
 *
 * The ghost is a hooded spectral presence. Three motions run off one 11s master
 * period, and two of them change the FORM rather than its interior:
 *
 *   1. The silhouette path itself morphs A -> B -> A via SMIL `<animate>` on the
 *      clip path's `d`. This is the load-bearing change: the outline is a
 *      different shape for most of the loop.
 *   2. The whole form leans on a non-uniform rotation (the tail swings further
 *      than the crown), layered outside the clip so the silhouette and the
 *      line field lean as one rigid body.
 *   3. The scanline field drifts upward through the silhouette, one 12.8-unit
 *      brightness cycle per 5.5s.
 *
 * The critical structural point: the drift and the clip live inside the SAME
 * transformed group. The scanlines are therefore locked to the moving outline —
 * they travel with the form, they do not slide behind a frozen one.
 *
 * Both `haunterDrift` and `haunterSway` are gated by prefers-reduced-motion in
 * the CSS module. The SMIL morph cannot be gated that way, so it is rendered
 * only when motion is allowed (see useReducedMotion below).
 *
 * No hard outline: the edge is defined by where the moving lines and their
 * bloom stop, which is what keeps this reading as light instead of a sticker.
 */
export function HaunterMark({ size = 44, className }: HaunterMarkProps) {
  // Two instances of this component render on the login page; useId keeps
  // their gradient/filter/clip ids unique so nothing cross-wires.
  const uid = useId().replace(/:/g, "");
  const clipId = `hw-clip-${uid}`;
  const bodyGradientId = `hw-body-${uid}`;
  const bloomFilterId = `hw-bloom-${uid}`;

  const reducedMotion = useReducedMotion();
  const glyph = Math.round(size * 0.9);

  return (
    <div
      aria-hidden="true"
      className={cn(
        "relative flex items-center justify-center overflow-hidden border-t border-t-amber-400/45 border-x border-x-amber-500/25 border-b border-b-amber-600/15",
        "bg-gradient-to-b from-amber-400/15 via-amber-500/8 to-amber-600/[0.03]",
        "shadow-[inset_0_1px_0_rgba(255,255,255,0.15),0_2px_8px_rgba(0,0,0,0.55),0_0_20px_rgba(245,158,11,0.10)]",
        className,
      )}
      style={{ width: size, height: size, borderRadius: Math.round(size * 0.28) }}
    >
      <svg
        width={glyph}
        height={glyph}
        viewBox="0 0 32 32"
        fill="none"
        xmlns="http://www.w3.org/2000/svg"
      >
        <defs>
          {/* The clipping silhouette. Its `d` is what actually animates: SMIL
              morphs A -> B -> A, so the visible OUTLINE changes shape twice per
              master loop. Omitted entirely under reduced motion, which leaves
              the complete static form A. */}
          <clipPath id={clipId}>
            <path d={GHOST_A}>
              {!reducedMotion && (
                <animate
                  attributeName="d"
                  dur={MORPH_DUR}
                  repeatCount="indefinite"
                  values={`${GHOST_A};${GHOST_B};${GHOST_A}`}
                  keyTimes="0;0.5;1"
                  calcMode="spline"
                  keySplines="0.45 0 0.55 1;0.45 0 0.55 1"
                />
              )}
            </path>
          </clipPath>

          <linearGradient
            id={bodyGradientId}
            x1="0"
            y1="3"
            x2="0"
            y2="29"
            gradientUnits="userSpaceOnUse"
          >
            <stop offset="0" stopColor="#fcd34d" stopOpacity="0.9" />
            <stop offset="0.38" stopColor="#fbbf24" stopOpacity="0.62" />
            <stop offset="1" stopColor="#f59e0b" stopOpacity="0.12" />
          </linearGradient>

          <filter id={bloomFilterId} x="-60%" y="-60%" width="220%" height="220%">
            <feGaussianBlur stdDeviation="0.7" />
          </filter>
        </defs>

        {/* `.sway` wraps EVERYTHING below — clip included. The lean and the
            morph are concentric, so the outline and the scanline field are one
            rigid body and can never drift out of register. */}
        <g className={styles.sway}>
          <g clipPath={`url(#${clipId})`}>
            {/* Bloom pass — the same scanlines, blurred and widened. Renders
                first so the crisp field sits on top of its own halo. */}
            <g filter={`url(#${bloomFilterId})`} opacity="0.75">
              <g className={styles.drift}>
                {Array.from({ length: LINE_COUNT }, (_, i) => (
                  <rect
                    key={`bloom-${i}`}
                    x="0"
                    y={-DRIFT + i * PITCH}
                    width="32"
                    height="1.9"
                    fill="#fbbf24"
                    opacity={LINE_ALPHA[i % LINE_ALPHA.length]}
                  />
                ))}
              </g>
            </g>

            {/* Crisp scanline field, clipped to the morphing silhouette */}
            <g className={styles.drift}>
              {Array.from({ length: LINE_COUNT }, (_, i) => (
                <rect
                  key={`line-${i}`}
                  x="0"
                  y={-DRIFT + i * PITCH}
                  width="32"
                  height="1.1"
                  fill={`url(#${bodyGradientId})`}
                  opacity={LINE_ALPHA[i % LINE_ALPHA.length]}
                />
              ))}
            </g>
          </g>
        </g>
      </svg>
    </div>
  );
}

/** Reads the reduced-motion preference and keeps tracking it if the user
 *  changes it while the page is open. SMIL `<animate>` is not reachable from a
 *  CSS media query, so the morph is toggled in JS instead — without this the
 *  outline would keep writhing for users who asked it not to. */
function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);

  useEffect(() => {
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    setReduced(query.matches);

    const onChange = (event: MediaQueryListEvent) => setReduced(event.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);

  return reduced;
}