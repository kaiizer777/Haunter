import React from "react";
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ConfidenceGauge } from "./confidence-gauge";

describe("ConfidenceGauge component", () => {
  it("renders derived confidence and preserves exact accessible name invariant", () => {
    // Risk 15 -> (100 - 15) / 10 = 8.5 rounded = 9/10
    render(<ConfidenceGauge riskScore={15} />);

    // Accessible name invariant check
    const gaugeImg = screen.getByRole("img", { name: "Confidence 9 of 10" });
    expect(gaugeImg).toBeInTheDocument();

    // Dual text display
    expect(screen.getByText("9")).toBeInTheDocument();
    expect(screen.getByText("/10")).toBeInTheDocument();

    // Risk band chip
    expect(screen.getByText("LOW RISK")).toBeInTheDocument();
  });

  it("renders critical risk score with low confidence and critical chip", () => {
    // Risk 85 -> (100 - 85) / 10 = 1.5 rounded = 2/10
    render(<ConfidenceGauge riskScore={85} />);

    expect(
      screen.getByRole("img", { name: "Confidence 2 of 10" })
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
      screen.getByRole("img", { name: "Confidence 8 of 10" })
    ).toBeInTheDocument();
    expect(screen.queryByText("LOW RISK")).not.toBeInTheDocument();
  });

  it("renders compact size and custom ariaLabel override", () => {
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
