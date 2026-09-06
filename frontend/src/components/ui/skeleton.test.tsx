import React from "react";
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { Skeleton } from "./skeleton";

describe("skeleton.tsx", () => {
  it("renders with base animation and background classes", () => {
    render(<Skeleton data-testid="skeleton-el" />);
    const el = screen.getByTestId("skeleton-el");
    expect(el).toBeInTheDocument();
    expect(el).toHaveClass("animate-pulse", "rounded-[4px]", "bg-zinc-800/60");
  });

  it("merges custom sizing and layout classes", () => {
    render(<Skeleton className="h-6 w-32 rounded-full" data-testid="skeleton-custom" />);
    const el = screen.getByTestId("skeleton-custom");
    expect(el).toHaveClass("h-6", "w-32", "rounded-full");
    expect(el).toHaveClass("animate-pulse");
  });

  it("forwards extra HTML attributes", () => {
    render(
      <Skeleton
        data-testid="skeleton-attrs"
        aria-hidden="true"
        style={{ opacity: 0.8 }}
      />
    );
    const el = screen.getByTestId("skeleton-attrs");
    expect(el).toHaveAttribute("aria-hidden", "true");
    expect(el).toHaveStyle({ opacity: 0.8 });
  });
});
