import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { Input } from "./input";

describe("input.tsx", () => {
  it("renders an input element with default type", () => {
    render(<Input placeholder="Enter repo name..." />);
    const input = screen.getByPlaceholderText("Enter repo name...");
    expect(input).toBeInTheDocument();
    expect(input.tagName).toBe("INPUT");
    expect(input).toHaveClass("bg-[#121215]", "border-zinc-800", "text-zinc-100");
  });

  it("propagates type attribute correctly", () => {
    const { rerender } = render(<Input type="password" data-testid="pwd-input" />);
    expect(screen.getByTestId("pwd-input")).toHaveAttribute("type", "password");

    rerender(<Input type="date" data-testid="pwd-input" />);
    expect(screen.getByTestId("pwd-input")).toHaveAttribute("type", "date");

    rerender(<Input type="number" data-testid="pwd-input" />);
    expect(screen.getByTestId("pwd-input")).toHaveAttribute("type", "number");
  });

  it("handles value and onChange events", () => {
    const handleChange = vi.fn();
    render(<Input value="initial" onChange={handleChange} data-testid="text-input" />);
    const input = screen.getByTestId("text-input");

    expect(input).toHaveValue("initial");
    fireEvent.change(input, { target: { value: "updated" } });
    expect(handleChange).toHaveBeenCalledTimes(1);
  });

  it("honors disabled prop and applies disabled styling", () => {
    render(<Input disabled placeholder="Disabled field" />);
    const input = screen.getByPlaceholderText("Disabled field");
    expect(input).toBeDisabled();
    expect(input).toHaveClass("disabled:cursor-not-allowed", "disabled:opacity-50");
  });

  it("forwards ref to HTMLInputElement", () => {
    const ref = React.createRef<HTMLInputElement>();
    render(<Input ref={ref} />);
    expect(ref.current).toBeInstanceOf(HTMLInputElement);
  });

  it("merges custom className with default classes", () => {
    render(<Input className="my-custom-input font-mono" data-testid="styled-input" />);
    const input = screen.getByTestId("styled-input");
    expect(input).toHaveClass("my-custom-input", "font-mono");
    expect(input).toHaveClass("bg-[#121215]");
  });
});
