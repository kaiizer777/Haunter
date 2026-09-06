import React from "react";
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { Badge, badgeVariants } from "./badge";

describe("badge.tsx", () => {
  it("renders with default variant and children", () => {
    render(<Badge>Default Badge</Badge>);
    const badge = screen.getByText("Default Badge");
    expect(badge).toBeInTheDocument();
    expect(badge).toHaveClass(
      "inline-flex",
      "items-center",
      "rounded-full",
      "border-zinc-700/80",
      "bg-zinc-800/80",
      "text-zinc-200"
    );
  });

  it("renders secondary variant", () => {
    render(<Badge variant="secondary">Secondary</Badge>);
    const badge = screen.getByText("Secondary");
    expect(badge).toHaveClass("border-zinc-800", "bg-zinc-900", "text-zinc-400");
  });

  it("renders success variant", () => {
    render(<Badge variant="success">Passed</Badge>);
    const badge = screen.getByText("Passed");
    expect(badge).toHaveClass("border-emerald-500/30", "bg-emerald-950/50", "text-emerald-400");
  });

  it("renders warning variant", () => {
    render(<Badge variant="warning">Fallback</Badge>);
    const badge = screen.getByText("Fallback");
    expect(badge).toHaveClass("border-amber-500/30", "bg-amber-950/50", "text-amber-400");
  });

  it("renders destructive variant", () => {
    render(<Badge variant="destructive">Failed</Badge>);
    const badge = screen.getByText("Failed");
    expect(badge).toHaveClass("border-red-500/30", "bg-red-950/50", "text-red-400");
  });

  it("renders outline variant", () => {
    render(<Badge variant="outline">Outline</Badge>);
    const badge = screen.getByText("Outline");
    expect(badge).toHaveClass("border-zinc-700", "text-zinc-300", "bg-transparent");
  });

  it("forwards extra HTML attributes and merges className", () => {
    render(
      <Badge className="custom-badge" data-testid="test-badge" id="b1">
        Custom
      </Badge>
    );
    const badge = screen.getByTestId("test-badge");
    expect(badge).toHaveClass("custom-badge");
    expect(badge).toHaveAttribute("id", "b1");
  });

  it("badgeVariants helper generates expected variant classes", () => {
    const successClasses = badgeVariants({ variant: "success" });
    expect(successClasses).toContain("text-emerald-400");
    const warningClasses = badgeVariants({ variant: "warning" });
    expect(warningClasses).toContain("text-amber-400");
  });
});
