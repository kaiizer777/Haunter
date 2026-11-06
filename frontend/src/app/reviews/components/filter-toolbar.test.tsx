import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { FilterToolbar } from "./filter-toolbar";

describe("FilterToolbar component", () => {
  const repoOptions = [
    { value: "all", label: "All Repositories" },
    { value: "repo-1", label: "facebook/react" },
  ];

  const defaultProps = {
    searchQuery: "",
    onSearchChange: vi.fn(),
    selectedRepoId: "all",
    onRepoChange: vi.fn(),
    repoOptions,
    selectedSeverity: "all",
    onSeverityChange: vi.fn(),
    highRiskOnly: false,
    onToggleHighRisk: vi.fn(),
    hideFailed: false,
    onToggleHideFailed: vi.fn(),
    failedCount: 3,
    sortKey: "attention" as const,
    onSortChange: vi.fn(),
    isLiveSyncing: false,
  };

  it("renders search input, severity buttons, risk filter, failed toggle and sort options", () => {
    render(<FilterToolbar {...defaultProps} />);

    expect(
      screen.getByPlaceholderText("Search findings, commits, files...")
    ).toBeInTheDocument();
    expect(screen.getByText("All Repositories")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Risk ≥ 71" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Hide failed \(3\)/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Attention/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Newest/i })).toBeInTheDocument();
  });

  it("triggers search callbacks and clears input with clear button", () => {
    const onSearchChange = vi.fn();
    const { rerender } = render(
      <FilterToolbar
        {...defaultProps}
        searchQuery=""
        onSearchChange={onSearchChange}
      />
    );

    const input = screen.getByPlaceholderText("Search findings, commits, files...");
    fireEvent.change(input, { target: { value: "memory leak" } });
    expect(onSearchChange).toHaveBeenCalledWith("memory leak");

    rerender(
      <FilterToolbar
        {...defaultProps}
        searchQuery="memory leak"
        onSearchChange={onSearchChange}
      />
    );

    const clearBtn = screen.getByRole("button", { name: /clear search text/i });
    expect(clearBtn).toBeInTheDocument();
    fireEvent.click(clearBtn);
    expect(onSearchChange).toHaveBeenCalledWith("");
  });

  it("handles severity selection changes", () => {
    const onSeverityChange = vi.fn();
    render(
      <FilterToolbar
        {...defaultProps}
        selectedSeverity="all"
        onSeverityChange={onSeverityChange}
      />
    );

    const criticalBtn = screen.getByRole("button", { name: "critical" });
    fireEvent.click(criticalBtn);
    expect(onSeverityChange).toHaveBeenCalledWith("critical");
  });

  it("toggles high risk and hide failed filters", () => {
    const onToggleHighRisk = vi.fn();
    const onToggleHideFailed = vi.fn();
    render(
      <FilterToolbar
        {...defaultProps}
        onToggleHighRisk={onToggleHighRisk}
        onToggleHideFailed={onToggleHideFailed}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: "Risk ≥ 71" }));
    expect(onToggleHighRisk).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: /Hide failed \(3\)/i }));
    expect(onToggleHideFailed).toHaveBeenCalledTimes(1);
  });

  it("renders live syncing radar badge when isLiveSyncing is true", () => {
    const { rerender } = render(
      <FilterToolbar {...defaultProps} isLiveSyncing={false} />
    );
    expect(screen.queryByText("Live Syncing")).not.toBeInTheDocument();

    rerender(<FilterToolbar {...defaultProps} isLiveSyncing={true} />);
    expect(screen.getByText("Live Syncing")).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Live Syncing active" })).toBeInTheDocument();
  });

  it("handles sort changes", () => {
    const onSortChange = vi.fn();
    render(<FilterToolbar {...defaultProps} onSortChange={onSortChange} />);

    fireEvent.click(screen.getByRole("button", { name: /Newest/i }));
    expect(onSortChange).toHaveBeenCalledWith("newest");
  });
});
