import React from "react";
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { StatusBadge } from "./status-badge";

describe("status-badge.tsx", () => {
  it("renders PR Opened for completed status", () => {
    const { container } = render(<StatusBadge status="completed" />);
    expect(screen.getByText("PR Opened")).toBeInTheDocument();
    expect(container.querySelector(".bg-emerald-400")).toBeInTheDocument();
  });

  it("renders PR Opened for pr_opened and passed statuses", () => {
    const { rerender } = render(<StatusBadge status="pr_opened" />);
    expect(screen.getByText("PR Opened")).toBeInTheDocument();

    rerender(<StatusBadge status="passed" />);
    expect(screen.getByText("PR Opened")).toBeInTheDocument();
  });

  it("renders Fallback Comment for fallback and fallback_commented statuses", () => {
    const { container, rerender } = render(<StatusBadge status="fallback" />);
    expect(screen.getByText("Fallback Comment")).toBeInTheDocument();
    expect(container.querySelector(".bg-amber-400")).toBeInTheDocument();

    rerender(<StatusBadge status="fallback_commented" />);
    expect(screen.getByText("Fallback Comment")).toBeInTheDocument();
  });

  it("renders Failed with destructive variant for error and failed statuses", () => {
    const { container, rerender } = render(<StatusBadge status="error" />);
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(container.querySelector(".bg-red-400")).toBeInTheDocument();

    rerender(<StatusBadge status="failed" />);
    expect(screen.getByText("Failed")).toBeInTheDocument();
  });

  it("renders Pending with pulsing dot", () => {
    const { container } = render(<StatusBadge status="pending" />);
    expect(screen.getByText("Pending")).toBeInTheDocument();
    expect(container.querySelector(".animate-ping")).toBeInTheDocument();
    expect(container.querySelector(".bg-zinc-400")).toBeInTheDocument();
  });

  it("renders Gathering Context with pulsing blue dot for context_gathering", () => {
    const { container } = render(<StatusBadge status="context_gathering" />);
    expect(screen.getByText("Gathering Context")).toBeInTheDocument();
    expect(container.querySelector(".animate-ping")).toBeInTheDocument();
    expect(container.querySelector(".bg-blue-400")).toBeInTheDocument();
  });

  it("renders Generating Fix with pulsing amber dot for fix_generation", () => {
    const { container } = render(<StatusBadge status="fix_generation" />);
    expect(screen.getByText("Generating Fix")).toBeInTheDocument();
    expect(container.querySelector(".animate-ping")).toBeInTheDocument();
    expect(container.querySelector(".bg-amber-400")).toBeInTheDocument();
  });

  it("renders Verifying Sandbox with pulsing purple dot for verification and pending_verification", () => {
    const { container, rerender } = render(<StatusBadge status="verification" />);
    expect(screen.getByText("Verifying Sandbox")).toBeInTheDocument();
    expect(container.querySelector(".animate-ping")).toBeInTheDocument();
    expect(container.querySelector(".bg-purple-400")).toBeInTheDocument();

    rerender(<StatusBadge status="pending_verification" />);
    expect(screen.getByText("Verifying Sandbox")).toBeInTheDocument();
  });

  it("renders Writing PR with pulsing emerald dot for pending_pr", () => {
    const { container } = render(<StatusBadge status="pending_pr" />);
    expect(screen.getByText("Writing PR")).toBeInTheDocument();
    expect(container.querySelector(".animate-ping")).toBeInTheDocument();
    expect(container.querySelector(".bg-emerald-400")).toBeInTheDocument();
  });

  it("renders default formatted label for unknown status", () => {
    render(<StatusBadge status="custom_eval_stage" />);
    expect(screen.getByText("custom eval stage")).toBeInTheDocument();
  });

  it("normalizes status casing (case-insensitive)", () => {
    render(<StatusBadge status="COMPLETED" />);
    expect(screen.getByText("PR Opened")).toBeInTheDocument();
  });

  it("hides status dot when showDot is false", () => {
    const { container } = render(<StatusBadge status="completed" showDot={false} />);
    expect(screen.getByText("PR Opened")).toBeInTheDocument();
    expect(container.querySelector(".h-1\\.5")).not.toBeInTheDocument();
  });

  it("merges custom className", () => {
    const { container } = render(
      <StatusBadge status="completed" className="my-badge-extra" />
    );
    expect(container.firstChild).toHaveClass("my-badge-extra");
  });
});
