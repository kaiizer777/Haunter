import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { TelemetryRibbon } from "./telemetry-ribbon";

describe("TelemetryRibbon component", () => {
  const defaultStats = {
    totalCount: 42,
    highRiskCount: 5,
    avgScore: 38,
    scoredCount: 30,
    totalPatches: 12,
  };

  it("renders all four telemetry tabs with correct stat numbers", () => {
    render(
      <TelemetryRibbon
        activeTab="all"
        onTabChange={vi.fn()}
        stats={defaultStats}
      />
    );

    expect(screen.getByText("Scanned")).toBeInTheDocument();
    expect(screen.getByText("42")).toBeInTheDocument();

    expect(screen.getByText("High-Risk ≥ 71")).toBeInTheDocument();
    expect(screen.getByText("5")).toBeInTheDocument();

    expect(screen.getByText("Avg Risk")).toBeInTheDocument();
    expect(screen.getByText("38")).toBeInTheDocument();
    expect(screen.getByText("/ 100")).toBeInTheDocument();

    expect(screen.getByText("Patches")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
  });

  it("reflects active tab states via aria-pressed", () => {
    const { rerender } = render(
      <TelemetryRibbon
        activeTab="high_risk"
        onTabChange={vi.fn()}
        stats={defaultStats}
      />
    );

    const highRiskBtn = screen.getByRole("button", { name: /high-risk/i });
    expect(highRiskBtn).toHaveAttribute("aria-pressed", "true");

    const scannedBtn = screen.getByRole("button", { name: /scanned/i });
    expect(scannedBtn).toHaveAttribute("aria-pressed", "false");

    rerender(
      <TelemetryRibbon
        activeTab="patches"
        onTabChange={vi.fn()}
        stats={defaultStats}
      />
    );

    const patchesBtn = screen.getByRole("button", { name: /patches/i });
    expect(patchesBtn).toHaveAttribute("aria-pressed", "true");
  });

  it("handles highRiskOnly prop properly", () => {
    render(
      <TelemetryRibbon
        activeTab="all"
        highRiskOnly={true}
        onTabChange={vi.fn()}
        stats={defaultStats}
      />
    );

    const highRiskBtn = screen.getByRole("button", { name: /high-risk/i });
    expect(highRiskBtn).toHaveAttribute("aria-pressed", "true");

    const scannedBtn = screen.getByRole("button", { name: /scanned/i });
    expect(scannedBtn).toHaveAttribute("aria-pressed", "false");
  });

  it("triggers onTabChange callback on tab click", () => {
    const onTabChange = vi.fn();
    render(
      <TelemetryRibbon
        activeTab="all"
        onTabChange={onTabChange}
        stats={defaultStats}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: /patches/i }));
    expect(onTabChange).toHaveBeenCalledWith("patches");

    // Avg Risk is a non-interactive stat tile, not a clickable button
    expect(
      screen.queryByRole("button", { name: /avg risk/i })
    ).not.toBeInTheDocument();
  });

  it("renders dash when scoredCount is 0", () => {
    render(
      <TelemetryRibbon
        activeTab="all"
        onTabChange={vi.fn()}
        stats={{ ...defaultStats, scoredCount: 0, avgScore: 0 }}
      />
    );

    expect(screen.getByText("No scored reviews")).toBeInTheDocument();
    expect(screen.getByText("—")).toBeInTheDocument();
  });
});
