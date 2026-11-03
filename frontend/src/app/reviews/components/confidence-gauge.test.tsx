import React from "react";
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ConfidenceGauge } from "./confidence-gauge";

describe("ConfidenceGauge component", () => {
  it("renders derived confidence and preserves exact accessible name invariant with risk band", () => {
    // Risk 15 -> (100 - 15) / 10 = 8.5 rounded = 9/10
    render(<ConfidenceGauge riskScore={15} />);

    // Accessible name invariant check
    const gaugeImg = screen.getByRole("img", {
      name: "Confidence 9 of 10 — low risk",
    });
    expect(gaugeImg).toBeInTheDocument();

    // Dual text display
    expect(screen.getByText("9")).toBeInTheDocument();
    expect(screen.getByText("/10")).toBeInTheDocument();

    // Risk band chip
    expect(screen.getByText("LOW RISK")).toBeInTheDocument();
  });

  it("renders moderate risk score with accurate accessible name and amber band", () => {
    // Risk 45 -> (100 - 45) / 10 = 5.5 rounded = 6/10
    render(<ConfidenceGauge riskScore={45} />);

    expect(
      screen.getByRole("img", { name: "Confidence 6 of 10 — elevated risk" })
    ).toBeInTheDocument();
    expect(screen.getByText("6")).toBeInTheDocument();
    expect(screen.getByText("ELEVATED")).toBeInTheDocument();
  });

  it("renders critical risk score with low confidence and critical chip", () => {
    // Risk 85 -> (100 - 85) / 10 = 1.5 rounded = 2/10
    render(<ConfidenceGauge riskScore={85} />);

    expect(
      screen.getByRole("img", { name: "Confidence 2 of 10 — critical risk" })
    ).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.getByText("/10")).toBeInTheDocument();
    expect(screen.getByText("CRITICAL")).toBeInTheDocument();
  });

  it("renders unscored neutral state when neutral=true", () => {
    render(<ConfidenceGauge riskScore={0} neutral />);

    // Unscored accessible name invariant
    expect(
      screen.getByRole("img", { name: "Confidence — of 10" })
    ).toBeInTheDocument();

    // Displays dash and omits /10
    expect(screen.getByText("—")).toBeInTheDocument();
    expect(screen.queryByText("/10")).not.toBeInTheDocument();

    // Unscored chip
    expect(screen.getByText("NO SCORE")).toBeInTheDocument();
  });

  it("respects showChip=false to omit the status chip", () => {
    render(<ConfidenceGauge riskScore={20} showChip={false} />);

    expect(
      screen.getByRole("img", { name: "Confidence 8 of 10 — low risk" })
    ).toBeInTheDocument();
    expect(screen.queryByText("LOW RISK")).not.toBeInTheDocument();
  });

  it("applies compact numeral styling when size <= 72 and standard styling when size > 72", () => {
    const { rerender } = render(<ConfidenceGauge riskScore={20} size={48} />);

    const compactNumeral = screen.getByText("8");
    const compactDenominator = screen.getByText("/10");
    expect(compactNumeral).toHaveClass("text-base");
    expect(compactDenominator).toHaveClass("text-[9px]");

    rerender(<ConfidenceGauge riskScore={20} size={84} />);

    const largeNumeral = screen.getByText("8");
    const largeDenominator = screen.getByText("/10");
    expect(largeNumeral).toHaveClass("text-2xl");
    expect(largeDenominator).toHaveClass("text-xs");
  });

  it("renders custom ariaLabel override when provided", () => {
    render(
      <ConfidenceGauge
        riskScore={50}
        size={48}
        ariaLabel="Custom Confidence Gauge Label"
      />
    );

    expect(
      screen.getByRole("img", { name: "Custom Confidence Gauge Label" })
    ).toBeInTheDocument();
  });
});
