import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { TriageChips } from "./triage-chips";
import type { OutcomeKey } from "../review-triage";

describe("TriageChips component", () => {
  const counts: Record<OutcomeKey, number> = {
    changes_requested: 3,
    advisory: 2,
    in_progress: 1,
    failed: 1,
    suppressed: 0,
    clean: 10,
  };

  it("renders lead sentence with actionable summary and failed count", () => {
    render(
      <TriageChips
        counts={counts}
        activeOutcome={null}
        onToggle={vi.fn()}
      />
    );

    expect(screen.getByText("5")).toBeInTheDocument();
    expect(screen.getByText(/reviews need a decision before merge/i)).toBeInTheDocument();
    expect(screen.getByText(/failed to analyse/i)).toBeInTheDocument();
  });

  it("renders all triage chips with their respective counts and accessible attributes", () => {
    render(
      <TriageChips
        counts={counts}
        activeOutcome="changes_requested"
        onToggle={vi.fn()}
      />
    );

    const allBtn = screen.getByRole("button", { name: /^All/i });
    expect(allBtn).toHaveAttribute("aria-pressed", "false");

    const needChangesBtn = screen.getByRole("button", { name: /Need changes/i });
    expect(needChangesBtn).toHaveAttribute("aria-pressed", "true");
    expect(needChangesBtn).toHaveTextContent("3");

    const reviewAdvisedBtn = screen.getByRole("button", { name: /Review advised/i });
    expect(reviewAdvisedBtn).toHaveAttribute("aria-pressed", "false");
    expect(reviewAdvisedBtn).toHaveTextContent("2");
  });

  it("triggers onToggle callback with key or null when toggled", () => {
    const onToggle = vi.fn();
    render(
      <TriageChips
        counts={counts}
        activeOutcome="changes_requested"
        onToggle={onToggle}
      />
    );

    // Clicking active chip toggles it off (null)
    fireEvent.click(screen.getByRole("button", { name: /Need changes/i }));
    expect(onToggle).toHaveBeenCalledWith(null);

    // Clicking an inactive chip activates it
    fireEvent.click(screen.getByRole("button", { name: /Review advised/i }));
    expect(onToggle).toHaveBeenCalledWith("advisory");

    // Clicking All sets outcome to null
    fireEvent.click(screen.getByRole("button", { name: /^All/i }));
    expect(onToggle).toHaveBeenCalledWith(null);
  });

  it("renders proper lead sentence when only in_progress analyses exist", () => {
    const zeroActionableCounts: Record<OutcomeKey, number> = {
      changes_requested: 0,
      advisory: 0,
      in_progress: 2,
      failed: 0,
      suppressed: 0,
      clean: 5,
    };

    render(
      <TriageChips
        counts={zeroActionableCounts}
        activeOutcome={null}
        onToggle={vi.fn()}
      />
    );

    expect(screen.getByText(/analyses are still running/i)).toBeInTheDocument();
  });

  it("renders 'Nothing needs a decision right now' when clean/idle", () => {
    const cleanCounts: Record<OutcomeKey, number> = {
      changes_requested: 0,
      advisory: 0,
      in_progress: 0,
      failed: 0,
      suppressed: 1,
      clean: 5,
    };

    render(
      <TriageChips
        counts={cleanCounts}
        activeOutcome={null}
        onToggle={vi.fn()}
      />
    );

    expect(screen.getByText("Nothing needs a decision right now")).toBeInTheDocument();
  });
});
