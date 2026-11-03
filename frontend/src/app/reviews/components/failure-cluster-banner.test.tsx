import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { FailureClusterBanner } from "./failure-cluster-banner";
import type { FailureCluster } from "../review-triage";

describe("FailureClusterBanner component", () => {
  const clusters: FailureCluster[] = [
    {
      signature: "All 9 free-tier models exhausted",
      count: 4,
    },
    {
      signature: "Request timed out after 30000ms",
      count: 2,
    },
  ];

  it("renders when there are two or more failures", () => {
    render(<FailureClusterBanner clusters={clusters} />);

    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(screen.getByText("6 failed analyses across 2 causes")).toBeInTheDocument();
    expect(screen.getByText("All 9 free-tier models exhausted")).toBeInTheDocument();
    expect(screen.getByText("Request timed out after 30000ms")).toBeInTheDocument();
  });

  it("returns null when total failures < 2 or empty", () => {
    const { container, rerender } = render(<FailureClusterBanner clusters={[]} />);
    expect(container.firstChild).toBeNull();

    rerender(
      <FailureClusterBanner
        clusters={[{ signature: "Single error", count: 1 }]}
      />
    );
    expect(container.firstChild).toBeNull();
  });

  it("copies failure signature on click", async () => {
    const writeTextMock = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: writeTextMock },
      writable: true,
      configurable: true,
    });

    render(<FailureClusterBanner clusters={clusters} />);

    const copyBtn = screen.getByRole("button", {
      name: /copy failure signature: all 9 free-tier models exhausted/i,
    });
    fireEvent.click(copyBtn);

    expect(writeTextMock).toHaveBeenCalledWith(
      "All 9 free-tier models exhausted"
    );
  });

  it("calls onDismiss when dismiss button is clicked", () => {
    const onDismiss = vi.fn();
    render(<FailureClusterBanner clusters={clusters} onDismiss={onDismiss} />);

    const dismissBtn = screen.getByRole("button", {
      name: /dismiss failure clusters banner/i,
    });
    fireEvent.click(dismissBtn);

    expect(onDismiss).toHaveBeenCalled();
  });
});
