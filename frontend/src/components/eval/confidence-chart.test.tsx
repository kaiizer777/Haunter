import React from "react";
import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ConfidenceOutcomeChart, AttemptDataPoint } from "./confidence-chart";

describe("confidence-chart.tsx", () => {
  it("renders with fallback golden points when data is omitted", () => {
    const { container } = render(<ConfidenceOutcomeChart />);

    expect(
      screen.getByText(/Confidence vs\. Sandbox Outcome Correlation/i)
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Synthetic fallback data — no live eval results yet/i)
    ).toBeInTheDocument();

    // Default stats from FALLBACK_POINTS (18 points)
    const attemptsLabel = screen.getByText("Attempts:");
    expect(attemptsLabel.nextSibling).toHaveTextContent("18");

    // Renders circles for each fallback point
    const circles = container.querySelectorAll("svg g.cursor-pointer circle");
    expect(circles.length).toBe(18);
  });

  it("renders with custom data points without fallback notice", () => {
    const customData: AttemptDataPoint[] = [
      { confidence: 95, passed: true, attempt_number: 1, label: "golden_1" },
      { confidence: 30, passed: false, attempt_number: 1, label: "golden_2" },
      { confidence: 85, passed: true, attempt_number: 2, label: "golden_3" },
      { confidence: 60, passed: false, attempt_number: 1, label: "golden_4" },
    ];

    const { container } = render(<ConfidenceOutcomeChart data={customData} />);

    expect(
      screen.queryByText(/Synthetic fallback data — no live eval results yet/i)
    ).not.toBeInTheDocument();

    // Total attempts: 4
    const attemptsLabel = screen.getByText("Attempts:");
    expect(attemptsLabel.nextSibling).toHaveTextContent("4");

    // 2 passed out of 4 -> 50% pass rate
    const passRateLabel = screen.getByText("Pass Rate:");
    expect(passRateLabel.nextSibling).toHaveTextContent("50%");

    // Check circles count matches custom data
    const circles = container.querySelectorAll("svg g.cursor-pointer circle");
    expect(circles.length).toBe(4);
  });

  it("renders all four confidence calibration brackets", () => {
    render(<ConfidenceOutcomeChart />);

    expect(screen.getByText("0–25%")).toBeInTheDocument();
    expect(screen.getByText("26–50%")).toBeInTheDocument();
    expect(screen.getByText("51–75%")).toBeInTheDocument();
    expect(screen.getByText("76–100%")).toBeInTheDocument();
  });

  it("shows point details on hover and restores prompt on mouse leave", () => {
    const customData: AttemptDataPoint[] = [
      {
        confidence: 92,
        passed: true,
        attempt_number: 1,
        label: "custom_pass_test",
      },
      {
        confidence: 25,
        passed: false,
        attempt_number: 1,
        label: "custom_fail_test",
      },
    ];

    const { container } = render(<ConfidenceOutcomeChart data={customData} />);

    // Initial hint text
    expect(
      screen.getByText(/Hover over any plot coordinate to inspect attempt/i)
    ).toBeInTheDocument();

    // Hover first circle group (<g>)
    const groups = container.querySelectorAll("svg g.cursor-pointer");
    expect(groups.length).toBe(2);

    fireEvent.mouseEnter(groups[0]);
    expect(screen.getByText("custom_pass_test")).toBeInTheDocument();
    expect(screen.getByText("Confidence:").parentElement).toHaveTextContent("92%");
    expect(screen.getByText("PASSED")).toBeInTheDocument();

    // Hover second circle group
    fireEvent.mouseEnter(groups[1]);
    expect(screen.getByText("custom_fail_test")).toBeInTheDocument();
    expect(screen.getByText("Confidence:").parentElement).toHaveTextContent("25%");
    expect(screen.getByText("FAILED")).toBeInTheDocument();

    // Mouse leave resets to instruction
    fireEvent.mouseLeave(groups[1]);
    expect(
      screen.getByText(/Hover over any plot coordinate to inspect attempt/i)
    ).toBeInTheDocument();
  });

  it("handles fallback to attempt_number when label is omitted", () => {
    const customData: AttemptDataPoint[] = [
      {
        confidence: 80,
        passed: true,
        attempt_number: 4,
      },
    ];

    const { container } = render(<ConfidenceOutcomeChart data={customData} />);
    const group = container.querySelector("svg g.cursor-pointer");
    expect(group).not.toBeNull();
    if (group) {
      fireEvent.mouseEnter(group);
      expect(screen.getByText("Attempt #4")).toBeInTheDocument();
    }
  });

  it("applies custom className to wrapper", () => {
    const { container } = render(
      <ConfidenceOutcomeChart className="my-eval-chart-custom" />
    );
    expect(container.firstChild).toHaveClass("my-eval-chart-custom");
  });
});
