import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { Switch } from "./switch";

describe("switch.tsx", () => {
  it("renders a standalone switch with role='switch' and aria-checked", () => {
    const handleChange = vi.fn();
    render(<Switch checked={false} onCheckedChange={handleChange} />);

    const switchBtn = screen.getByRole("switch");
    expect(switchBtn).toBeInTheDocument();
    expect(switchBtn).toHaveAttribute("aria-checked", "false");
    expect(switchBtn).toHaveClass("bg-zinc-800", "border-zinc-700");
  });

  it("reflects checked=true state in styling and aria-checked", () => {
    const handleChange = vi.fn();
    render(<Switch checked={true} onCheckedChange={handleChange} />);

    const switchBtn = screen.getByRole("switch");
    expect(switchBtn).toHaveAttribute("aria-checked", "true");
    expect(switchBtn).toHaveClass("bg-amber-400");
  });

  it("calls onCheckedChange with negated boolean when clicked", () => {
    const handleChange = vi.fn();
    const { rerender } = render(
      <Switch checked={false} onCheckedChange={handleChange} />
    );

    const switchBtn = screen.getByRole("switch");
    fireEvent.click(switchBtn);
    expect(handleChange).toHaveBeenCalledWith(true);

    rerender(<Switch checked={true} onCheckedChange={handleChange} />);
    fireEvent.click(switchBtn);
    expect(handleChange).toHaveBeenCalledWith(false);
  });

  it("handles keyboard navigation: Space and Enter toggle the switch", () => {
    const handleChange = vi.fn();
    render(<Switch checked={false} onCheckedChange={handleChange} />);

    const switchBtn = screen.getByRole("switch");

    // Space key
    fireEvent.keyDown(switchBtn, { key: " " });
    expect(handleChange).toHaveBeenCalledWith(true);

    // Enter key
    fireEvent.keyDown(switchBtn, { key: "Enter" });
    expect(handleChange).toHaveBeenCalledTimes(2);

    // Other keys should not toggle
    fireEvent.keyDown(switchBtn, { key: "ArrowRight" });
    expect(handleChange).toHaveBeenCalledTimes(2);
  });

  it("blocks click and key events when disabled", () => {
    const handleChange = vi.fn();
    render(<Switch checked={false} onCheckedChange={handleChange} disabled />);

    const switchBtn = screen.getByRole("switch");
    expect(switchBtn).toBeDisabled();

    fireEvent.click(switchBtn);
    expect(handleChange).not.toHaveBeenCalled();

    fireEvent.keyDown(switchBtn, { key: " " });
    expect(handleChange).not.toHaveBeenCalled();
  });

  it("renders label and description when provided", () => {
    const handleChange = vi.fn();
    render(
      <Switch
        checked={false}
        onCheckedChange={handleChange}
        label="Demo mode"
        description="Enable sandbox simulation"
        id="demo-switch"
      />
    );

    expect(screen.getByText("Demo mode")).toBeInTheDocument();
    expect(screen.getByText("Enable sandbox simulation")).toBeInTheDocument();

    // Clicking label toggles the switch
    fireEvent.click(screen.getByText("Demo mode"));
    expect(handleChange).toHaveBeenCalledWith(true);
  });

  it("does not toggle when label is clicked and disabled=true", () => {
    const handleChange = vi.fn();
    render(
      <Switch
        checked={false}
        onCheckedChange={handleChange}
        label="Disabled Toggle"
        disabled
      />
    );

    fireEvent.click(screen.getByText("Disabled Toggle"));
    expect(handleChange).not.toHaveBeenCalled();
  });

  it("renders tooltip title attribute on button", () => {
    const handleChange = vi.fn();
    render(
      <Switch
        checked={false}
        onCheckedChange={handleChange}
        tooltip="Toggle dark mode preview"
      />
    );

    expect(screen.getByRole("switch")).toHaveAttribute(
      "title",
      "Toggle dark mode preview"
    );
  });

  it("forwards ref to the switch button element", () => {
    const ref = React.createRef<HTMLButtonElement>();
    render(<Switch ref={ref} checked={false} onCheckedChange={vi.fn()} />);

    expect(ref.current).toBeInstanceOf(HTMLButtonElement);
  });
});
