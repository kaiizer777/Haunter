import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
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

  beforeEach(() => {
    vi.restoreAllMocks();
  });

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

  it("copies failure signature on click and updates icon", async () => {
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

    await act(async () => {
      fireEvent.click(copyBtn);
    });

    expect(writeTextMock).toHaveBeenCalledWith(
      "All 9 free-tier models exhausted"
    );

    await waitFor(() => {
      const copiedBtn = screen.getByRole("button", {
        name: /copied signature for all 9 free-tier models exhausted/i,
      });
      expect(copiedBtn).toBeInTheDocument();
      const svg = copiedBtn.querySelector("svg");
      expect(svg).toBeInTheDocument();
      expect(svg).toHaveClass("text-emerald-400");
    });
  });

  it("shows error feedback when clipboard copy fails", async () => {
    const writeTextMock = vi.fn().mockRejectedValue(new Error("Denied"));
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: writeTextMock },
      writable: true,
      configurable: true,
    });

    render(<FailureClusterBanner clusters={clusters} />);

    const copyBtn = screen.getByRole("button", {
      name: /copy failure signature: all 9 free-tier models exhausted/i,
    });

    await act(async () => {
      fireEvent.click(copyBtn);
    });

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "Failed to copy signature — clipboard unavailable."
    );
  });

  it("toggles '+N more' expand and collapse when more than 2 causes exist", () => {
    const multiClusters: FailureCluster[] = [
      { signature: "Err 1: Rate limit", count: 3 },
      { signature: "Err 2: Network timeout", count: 2 },
      { signature: "Err 3: Bad gateway", count: 2 },
      { signature: "Err 4: Invalid response syntax", count: 1 },
    ];

    render(<FailureClusterBanner clusters={multiClusters} />);

    // Initially expanded
    expect(screen.getByText("Err 1: Rate limit")).toBeInTheDocument();
    expect(screen.getByText("Err 2: Network timeout")).toBeInTheDocument();
    expect(screen.getByText("Err 3: Bad gateway")).toBeInTheDocument();
    expect(screen.getByText("Err 4: Invalid response syntax")).toBeInTheDocument();

    const collapseBtn = screen.getByRole("button", { name: /show less/i });
    expect(collapseBtn).toBeInTheDocument();

    // Collapse list to top 2
    fireEvent.click(collapseBtn);

    expect(screen.getByText("Err 1: Rate limit")).toBeInTheDocument();
    expect(screen.getByText("Err 2: Network timeout")).toBeInTheDocument();
    expect(screen.queryByText("Err 3: Bad gateway")).not.toBeInTheDocument();
    expect(screen.queryByText("Err 4: Invalid response syntax")).not.toBeInTheDocument();

    // Now button says "+2 more"
    const expandBtn = screen.getByRole("button", { name: /\+2 more/i });
    expect(expandBtn).toBeInTheDocument();

    // Expand again
    fireEvent.click(expandBtn);

    expect(screen.getByText("Err 3: Bad gateway")).toBeInTheDocument();
    expect(screen.getByText("Err 4: Invalid response syntax")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /show less/i })).toBeInTheDocument();
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
