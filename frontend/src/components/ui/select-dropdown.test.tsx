import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SelectDropdown } from "./select-dropdown";

describe("SelectDropdown (components/ui/select-dropdown.tsx)", () => {
  const options = [
    { value: "", label: "All Options" },
    { value: "opt1", label: "Option One" },
    { value: "opt2", label: "Option Two" },
  ];

  it("renders trigger with initial selected label", () => {
    render(
      <SelectDropdown
        value=""
        onChange={vi.fn()}
        options={options}
      />
    );
    expect(screen.getByRole("button")).toHaveTextContent("All Options");
  });

  it("opens menu when clicked and closes on select", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <SelectDropdown
        value=""
        onChange={onChange}
        options={options}
      />
    );

    const trigger = screen.getByRole("button");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();

    await user.click(trigger);
    expect(screen.getByRole("listbox")).toBeInTheDocument();

    const opt2 = screen.getByRole("option", { name: /option two/i });
    await user.click(opt2);

    expect(onChange).toHaveBeenCalledWith("opt2");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("closes on Escape key", async () => {
    const user = userEvent.setup();
    render(
      <SelectDropdown
        value=""
        onChange={vi.fn()}
        options={options}
      />
    );

    const trigger = screen.getByRole("button");
    await user.click(trigger);
    expect(screen.getByRole("listbox")).toBeInTheDocument();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("closes when clicking outside", async () => {
    const user = userEvent.setup();
    render(
      <div>
        <div data-testid="outside">Outside Area</div>
        <SelectDropdown
          value=""
          onChange={vi.fn()}
          options={options}
        />
      </div>
    );

    const trigger = screen.getByRole("button");
    await user.click(trigger);
    expect(screen.getByRole("listbox")).toBeInTheDocument();

    fireEvent.mouseDown(screen.getByTestId("outside"));
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });
});
