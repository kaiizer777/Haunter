import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { Button, buttonVariants } from "./button";

describe("button.tsx", () => {
  it("renders children correctly with default props", () => {
    render(<Button>Click me</Button>);
    const btn = screen.getByRole("button", { name: "Click me" });
    expect(btn).toBeInTheDocument();
    expect(btn).toHaveClass("bg-zinc-50", "text-zinc-950", "h-8", "px-3");
  });

  it("renders with secondary variant", () => {
    render(<Button variant="secondary">Secondary</Button>);
    const btn = screen.getByRole("button", { name: "Secondary" });
    expect(btn).toHaveClass("bg-zinc-800", "text-zinc-100");
  });

  it("renders with destructive variant", () => {
    render(<Button variant="destructive">Delete</Button>);
    const btn = screen.getByRole("button", { name: "Delete" });
    expect(btn).toHaveClass("bg-red-950", "text-red-300");
  });

  it("renders with outline variant", () => {
    render(<Button variant="outline">Outline</Button>);
    const btn = screen.getByRole("button", { name: "Outline" });
    expect(btn).toHaveClass("border-zinc-800", "bg-transparent", "text-zinc-300");
  });

  it("renders with ghost variant", () => {
    render(<Button variant="ghost">Ghost</Button>);
    const btn = screen.getByRole("button", { name: "Ghost" });
    expect(btn).toHaveClass("text-zinc-400");
    expect(btn).not.toHaveClass("bg-zinc-50");
  });

  it("renders with link variant", () => {
    render(<Button variant="link">Link</Button>);
    const btn = screen.getByRole("button", { name: "Link" });
    expect(btn).toHaveClass("hover:underline");
  });

  it("renders different sizes correctly", () => {
    const { rerender } = render(<Button size="sm">Small</Button>);
    let btn = screen.getByRole("button", { name: "Small" });
    expect(btn).toHaveClass("h-7", "px-2.5", "text-[11px]");

    rerender(<Button size="lg">Large</Button>);
    btn = screen.getByRole("button", { name: "Large" });
    expect(btn).toHaveClass("h-9", "px-4", "text-sm");

    rerender(<Button size="icon">Icon</Button>);
    btn = screen.getByRole("button", { name: "Icon" });
    expect(btn).toHaveClass("h-8", "w-8", "p-0");
  });

  it("handles disabled state and prevents click handler execution", () => {
    const handleClick = vi.fn();
    render(
      <Button disabled onClick={handleClick}>
        Disabled
      </Button>
    );
    const btn = screen.getByRole("button", { name: "Disabled" });
    expect(btn).toBeDisabled();
    expect(btn).toHaveClass("disabled:opacity-50");

    fireEvent.click(btn);
    expect(handleClick).not.toHaveBeenCalled();
  });

  it("fires onClick handler when enabled", () => {
    const handleClick = vi.fn();
    render(<Button onClick={handleClick}>Active</Button>);
    const btn = screen.getByRole("button", { name: "Active" });

    fireEvent.click(btn);
    expect(handleClick).toHaveBeenCalledTimes(1);
  });

  it("forwards ref to button element", () => {
    const ref = React.createRef<HTMLButtonElement>();
    render(<Button ref={ref}>Ref Target</Button>);
    expect(ref.current).toBeInstanceOf(HTMLButtonElement);
    expect(ref.current?.tagName).toBe("BUTTON");
  });

  it("merges custom className with variant styles", () => {
    render(<Button className="custom-class-test">Custom</Button>);
    const btn = screen.getByRole("button", { name: "Custom" });
    expect(btn).toHaveClass("custom-class-test");
    expect(btn).toHaveClass("bg-zinc-50");
  });

  it("buttonVariants helper generates correct class string", () => {
    const classes = buttonVariants({ variant: "destructive", size: "sm" });
    expect(classes).toContain("bg-red-950");
    expect(classes).toContain("h-7");
  });
});
