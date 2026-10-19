import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { ClarificationPromptCard } from "./ClarificationPromptCard";

describe("ClarificationPromptCard (components/workspace/ClarificationPromptCard.tsx)", () => {
  it("renders active blocking prompt with question, options, and Action Required badge", () => {
    const handleSelect = vi.fn();
    render(
      <ClarificationPromptCard
        question="Which database adapter should we configure?"
        options={["PostgreSQL (Neon)", "SQLite (Hermetic)", "MySQL"]}
        isPending={true}
        onSelectOption={handleSelect}
      />
    );

    expect(screen.getByTestId("clarification-card-active")).toBeInTheDocument();
    expect(screen.getByText(/action required · agent blocked/i)).toBeInTheDocument();
    expect(screen.getByText("Which database adapter should we configure?")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /postgresql \(neon\)/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /sqlite \(hermetic\)/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /mysql/i })).toBeInTheDocument();
  });

  it("calls onSelectOption when an option button is clicked", async () => {
    const handleSelect = vi.fn().mockResolvedValue(undefined);
    render(
      <ClarificationPromptCard
        question="Select environment"
        options={["Staging", "Production"]}
        isPending={true}
        onSelectOption={handleSelect}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: /staging/i }));
    await waitFor(() => {
      expect(handleSelect).toHaveBeenCalledWith("Staging");
    });
  });

  it("renders ordering badge when total questions > 1", () => {
    render(
      <ClarificationPromptCard
        question="First question"
        options={["Option A", "Option B"]}
        index={1}
        total={3}
        isPending={true}
      />
    );

    expect(screen.getByText("Question 1 of 3")).toBeInTheDocument();
  });

  it("allows submitting a custom response via input form", async () => {
    const handleSelect = vi.fn().mockResolvedValue(undefined);
    render(
      <ClarificationPromptCard
        question="How should we proceed?"
        options={["Proceed", "Abort"]}
        isPending={true}
        onSelectOption={handleSelect}
      />
    );

    // Click to show custom input
    fireEvent.click(screen.getByRole("button", { name: /\+ type custom clarification response/i }));

    const input = screen.getByPlaceholderText(/type your instructions or answer/i);
    fireEvent.change(input, { target: { value: "Use custom adapter in src/db.ts" } });

    fireEvent.click(screen.getByRole("button", { name: /submit/i }));

    await waitFor(() => {
      expect(handleSelect).toHaveBeenCalledWith("Use custom adapter in src/db.ts");
    });
  });

  it("renders resolved state with checkmark and selected answer when isPending is false", () => {
    render(
      <ClarificationPromptCard
        question="Which database adapter should we configure?"
        options={["PostgreSQL (Neon)", "SQLite (Hermetic)"]}
        isPending={false}
        selectedAnswer="PostgreSQL (Neon)"
        index={1}
        total={2}
      />
    );

    expect(screen.getByTestId("clarification-card-resolved")).toBeInTheDocument();
    expect(screen.getByText(/clarification resolved/i)).toBeInTheDocument();
    expect(screen.getByText("PostgreSQL (Neon)")).toBeInTheDocument();
    expect(screen.getByText("Question 1 of 2")).toBeInTheDocument();
    // In resolved state, active option buttons should not be present
    expect(screen.queryByRole("button", { name: /sqlite \(hermetic\)/i })).not.toBeInTheDocument();
  });

  it("enforces maxLength of 2000 characters on custom input", () => {
    render(
      <ClarificationPromptCard
        question="How should we proceed?"
        options={["Proceed", "Abort"]}
        isPending={true}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: /\+ type custom clarification response/i }));
    const input = screen.getByPlaceholderText(/type your instructions or answer/i);
    expect(input).toHaveAttribute("maxLength", "2000");
  });

  it("re-enables option buttons if onSelectOption throws an error", async () => {
    const handleSelect = vi.fn().mockRejectedValue(new Error("Network failure"));
    render(
      <ClarificationPromptCard
        question="Select option"
        options={["Option 1", "Option 2"]}
        isPending={true}
        onSelectOption={handleSelect}
      />
    );

    const btn = screen.getByRole("button", { name: /option 1/i });
    fireEvent.click(btn);

    await waitFor(() => {
      expect(handleSelect).toHaveBeenCalledWith("Option 1");
      expect(btn).not.toBeDisabled();
    });
  });
});
