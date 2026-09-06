import React from "react";
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ConfidenceBar } from "./confidence-bar";

describe("confidence-bar.tsx", () => {
  it("renders a dash for null score", () => {
    render(<ConfidenceBar score={null} />);
    const dash = screen.getByText("—");
    expect(dash).toBeInTheDocument();
    expect(dash).toHaveClass("text-zinc-600", "font-mono");
  });

  it("renders a dash for undefined score", () => {
    render(<ConfidenceBar score={undefined} />);
    const dash = screen.getByText("—");
    expect(dash).toBeInTheDocument();
  });

  it("renders 0% score with red bar and 0% text", () => {
    const { container } = render(<ConfidenceBar score={0} />);
    expect(screen.getByText("0%")).toBeInTheDocument();

    const bar = container.querySelector(".bg-red-400");
    expect(bar).toBeInTheDocument();
    expect(bar).toHaveStyle({ width: "0%" });
  });

  it("renders 100% score with emerald bar and 100% text", () => {
    const { container } = render(<ConfidenceBar score={100} />);
    expect(screen.getByText("100%")).toBeInTheDocument();

    const bar = container.querySelector(".bg-emerald-400");
    expect(bar).toBeInTheDocument();
    expect(bar).toHaveStyle({ width: "100%" });
  });

  it("renders 87% score with emerald bar (>=80 threshold)", () => {
    const { container } = render(<ConfidenceBar score={87} />);
    expect(screen.getByText("87%")).toBeInTheDocument();

    const bar = container.querySelector(".bg-emerald-400");
    expect(bar).toBeInTheDocument();
    expect(bar).toHaveStyle({ width: "87%" });
  });

  it("renders 65% score with amber bar (50-79 threshold)", () => {
    const { container } = render(<ConfidenceBar score={65} />);
    expect(screen.getByText("65%")).toBeInTheDocument();

    const bar = container.querySelector(".bg-amber-400");
    expect(bar).toBeInTheDocument();
    expect(bar).toHaveStyle({ width: "65%" });
  });

  it("renders 30% score with red bar (<50 threshold)", () => {
    const { container } = render(<ConfidenceBar score={30} />);
    expect(screen.getByText("30%")).toBeInTheDocument();

    const bar = container.querySelector(".bg-red-400");
    expect(bar).toBeInTheDocument();
    expect(bar).toHaveStyle({ width: "30%" });
  });

  it("clamps negative values to 0%", () => {
    const { container } = render(<ConfidenceBar score={-25} />);
    expect(screen.getByText("0%")).toBeInTheDocument();

    const bar = container.querySelector(".bg-red-400");
    expect(bar).toHaveStyle({ width: "0%" });
  });

  it("clamps values greater than 100 to 100%", () => {
    const { container } = render(<ConfidenceBar score={150} />);
    expect(screen.getByText("100%")).toBeInTheDocument();

    const bar = container.querySelector(".bg-emerald-400");
    expect(bar).toHaveStyle({ width: "100%" });
  });

  it("merges custom className into container", () => {
    const { container } = render(
      <ConfidenceBar score={75} className="custom-confidence-bar" />
    );
    expect(container.firstChild).toHaveClass("custom-confidence-bar");
  });
});
