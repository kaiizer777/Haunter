import React from "react";
import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { AppliedPrefsCard } from "./applied-prefs-card";

describe("AppliedPrefsCard component", () => {
  it("renders default profile fallback when no preferences applied", () => {
    render(<AppliedPrefsCard appliedPrefs={null} />);

    expect(
      screen.getByText(/Default repository review profile/i)
    ).toBeInTheDocument();
  });

  it("renders strictness badge, version tag, and focus areas tags", () => {
    render(
      <AppliedPrefsCard
        appliedPrefs={{
          prefs: {
            review_strictness: "strict",
            review_focus_areas: ["Security", "SQL Injection", "Memory Leaks"],
          },
          settings_version: 4,
          truncated: false,
        }}
      />
    );

    // Strictness badge
    expect(screen.getByText("Strict")).toBeInTheDocument();
    // Settings version
    expect(screen.getByText("v4")).toBeInTheDocument();
    // Focus areas
    expect(screen.getByText("Security")).toBeInTheDocument();
    expect(screen.getByText("SQL Injection")).toBeInTheDocument();
    expect(screen.getByText("Memory Leaks")).toBeInTheDocument();
  });

  it("renders truncated badge when truncated is true", () => {
    render(
      <AppliedPrefsCard
        appliedPrefs={{
          prefs: {
            review_strictness: "medium",
            review_focus_areas: ["Performance"],
          },
          truncated: true,
        }}
      />
    );

    expect(screen.getByText("Guidelines Truncated")).toBeInTheDocument();
    expect(screen.getByText("Balanced")).toBeInTheDocument();
  });

  it("supports backend enums high and low for strictness mapping", () => {
    const { rerender } = render(
      <AppliedPrefsCard
        appliedPrefs={{
          prefs: { review_strictness: "high" },
        }}
      />
    );
    expect(screen.getByText("Strict")).toBeInTheDocument();

    rerender(
      <AppliedPrefsCard
        appliedPrefs={{
          prefs: { review_strictness: "low" },
        }}
      />
    );
    expect(screen.getByText("Lenient")).toBeInTheDocument();
  });

  it("expands and collapses checklist and guidelines text", () => {
    render(
      <AppliedPrefsCard
        appliedPrefs={{
          prefs: {
            review_strictness: "lenient",
            review_checklist: [
              "Verify all database transactions use ACID locks",
              "Check API backward compatibility",
            ],
            review_guidelines_text:
              "Prefer early returns. Ensure all public endpoints are authenticated.",
          },
        }}
      />
    );

    expect(screen.getByText("Lenient")).toBeInTheDocument();

    // Checklist initially collapsed
    expect(
      screen.queryByText("Verify all database transactions use ACID locks")
    ).not.toBeInTheDocument();

    // Toggle checklist
    const checklistToggle = screen.getByRole("button", {
      name: /Custom Review Checklist \(2\)/i,
    });
    fireEvent.click(checklistToggle);

    expect(
      screen.getByText("Verify all database transactions use ACID locks")
    ).toBeInTheDocument();
    expect(
      screen.getByText("Check API backward compatibility")
    ).toBeInTheDocument();

    // Guidelines text initially collapsed
    expect(
      screen.queryByText(/Prefer early returns/)
    ).not.toBeInTheDocument();

    // Toggle guidelines
    const guidelinesToggle = screen.getByRole("button", {
      name: /Repository Review Guidelines Preview/i,
    });
    fireEvent.click(guidelinesToggle);

    expect(
      screen.getByText(/Prefer early returns. Ensure all public endpoints are authenticated./)
    ).toBeInTheDocument();
  });
});
