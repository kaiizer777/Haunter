import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
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
    expect(allBtn).toHaveTextContent("17");

    // Assert all 6 outcome chips are rendered with counts and aria-pressed states
    const needChangesBtn = screen.getByRole("button", { name: /Need changes/i });
    expect(needChangesBtn).toHaveAttribute("aria-pressed", "true");
    expect(needChangesBtn).toHaveTextContent("3");

    const reviewAdvisedBtn = screen.getByRole("button", { name: /Review advised/i });
    expect(reviewAdvisedBtn).toHaveAttribute("aria-pressed", "false");
    expect(reviewAdvisedBtn).toHaveTextContent("2");

    const inProgressBtn = screen.getByRole("button", { name: /In progress/i });
    expect(inProgressBtn).toHaveAttribute("aria-pressed", "false");
    expect(inProgressBtn).toHaveTextContent("1");

    const failedBtn = screen.getByRole("button", { name: /Failed/i });
    expect(failedBtn).toHaveAttribute("aria-pressed", "false");
    expect(failedBtn).toHaveTextContent("1");

    const suppressedBtn = screen.getByRole("button", { name: /Suppressed/i });
    expect(suppressedBtn).toHaveAttribute("aria-pressed", "false");
    expect(suppressedBtn).toHaveTextContent("0");
    expect(within(suppressedBtn).getByText("0")).toHaveClass("text-zinc-500");

    const cleanBtn = screen.getByRole("button", { name: /Great to merge/i });
    expect(cleanBtn).toHaveAttribute("aria-pressed", "false");
    expect(cleanBtn).toHaveTextContent("10");
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

  it("renders failed analysis retry prompt when urgent and attention are 0 but failed > 0", () => {
    const failedOnlyCounts: Record<OutcomeKey, number> = {
      changes_requested: 0,
      advisory: 0,
      in_progress: 0,
      failed: 2,
      suppressed: 0,
      clean: 4,
    };

    render(
      <TriageChips
        counts={failedOnlyCounts}
        activeOutcome={null}
        onToggle={vi.fn()}
      />
    );

    const lead = screen.getByTestId("triage-lead-sentence");
    expect(
      within(lead).getByText(/failed analyses require retry or inspection/i)
    ).toBeInTheDocument();
    expect(within(lead).getByText("2")).toBeInTheDocument();
  });

  it("mentions both in-progress and failed analyses when actionable is 0", () => {
    const counts: Record<OutcomeKey, number> = {
      changes_requested: 0,
      advisory: 0,
      in_progress: 3,
      failed: 1,
      suppressed: 0,
      clean: 0,
    };

    render(
      <TriageChips
        counts={counts}
        activeOutcome={null}
        onToggle={vi.fn()}
      />
    );

    const lead = screen.getByTestId("triage-lead-sentence");
    expect(lead).toHaveTextContent(
      "3 analyses are still running (1 failed analysis requires inspection)"
    );
  });
});
