import { act, render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { HaunterMark } from "./HaunterMark";

function mockMatchMedia(matches: boolean) {
  const listeners = new Set<(e: MediaQueryListEvent) => void>();
  const mql = {
    matches,
    media: "(prefers-reduced-motion: reduce)",
    addEventListener: (_: string, cb: (e: MediaQueryListEvent) => void) => listeners.add(cb),
    removeEventListener: (_: string, cb: (e: MediaQueryListEvent) => void) => listeners.delete(cb),
  };
  vi.stubGlobal("matchMedia", vi.fn(() => mql));
  return {
    set(value: boolean) {
      mql.matches = value;
      listeners.forEach((cb) => cb({ matches: value } as MediaQueryListEvent));
    },
  };
}

describe("HaunterMark", () => {
  it("animates the silhouette with a SMIL morph so the FORM moves, not just the fill", () => {
    mockMatchMedia(false);
    const { container } = render(<HaunterMark size={44} />);

    const animate = container.querySelector("animate");
    expect(animate).not.toBeNull();
    expect(animate).toHaveAttribute("attributeName", "d");

    const values = animate!.getAttribute("values")!.split(";");
    expect(values).toHaveLength(3);
    expect(values[0]).toBe(values[2]); // A === A: start frame is the end frame

    const [a, b] = values;
    expect(a).not.toBe(b); // genuinely different silhouettes, not a no-op morph
    expect(a!.split(/[A-Za-z]/).length).toBe(b!.split(/[A-Za-z]/).length); // same command structure

    vi.unstubAllGlobals();
  });

  it("renders a complete static silhouette under prefers-reduced-motion", () => {
    mockMatchMedia(true);
    const { container } = render(<HaunterMark size={44} />);

    expect(container.querySelector("animate")).toBeNull();

    const clipped = container.querySelector("clipPath path");
    expect(clipped).not.toBeNull();
    expect(clipped!.getAttribute("d")).toMatch(/^M16 2\.6/); // shape A still present

    // The scanline field is still fully rendered — static, not stripped.
    expect(container.querySelectorAll("rect").length).toBeGreaterThan(0);

    vi.unstubAllGlobals();
  });

  it("reacts to the reduced-motion preference changing while mounted", () => {
    const media = mockMatchMedia(false);
    render(<HaunterMark size={30} />);

    const container = document.body;
    expect(container.querySelector("animate")).not.toBeNull();

    act(() => media.set(true));

    expect(container.querySelector("animate")).toBeNull();
    vi.unstubAllGlobals();
  });

  it("scales the coin and keeps it decorative", () => {
    mockMatchMedia(false);
    const { container } = render(<HaunterMark size={44} />);
    const coin = container.firstElementChild as HTMLElement;

    expect(coin).toHaveAttribute("aria-hidden", "true");
    expect(coin.style.width).toBe("44px");
    expect(coin.style.height).toBe("44px");
    // No hover scale: the mark must not grow under the cursor.
    expect(coin.className).not.toMatch(/scale/);

    vi.unstubAllGlobals();
  });
});