import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { Modal } from "./modal";

describe("modal.tsx", () => {
  it("renders null when isOpen is false", () => {
    const handleClose = vi.fn();
    const { container } = render(
      <Modal isOpen={false} onClose={handleClose} title="Hidden Modal">
        <p>Hidden Content</p>
      </Modal>
    );
    expect(container.firstChild).toBeNull();
    expect(screen.queryByText("Hidden Modal")).not.toBeInTheDocument();
  });

  it("renders title, description, and children when isOpen is true", () => {
    const handleClose = vi.fn();
    render(
      <Modal
        isOpen={true}
        onClose={handleClose}
        title="Active Modal"
        description="Modal detailed explanation"
      >
        <div data-testid="modal-body">Modal Body</div>
      </Modal>
    );

    expect(screen.getByText("Active Modal")).toBeInTheDocument();
    expect(screen.getByText("Modal detailed explanation")).toBeInTheDocument();
    expect(screen.getByTestId("modal-body")).toBeInTheDocument();
  });

  it("renders with role dialog, aria-modal, and accessible close button", () => {
    const handleClose = vi.fn();
    render(
      <Modal
        isOpen={true}
        onClose={handleClose}
        title="Accessible Dialog"
        description="Accessible description"
      >
        <div>Content</div>
      </Modal>
    );

    const dialog = screen.getByRole("dialog");
    expect(dialog).toBeInTheDocument();
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(screen.getByRole("button", { name: /close modal/i })).toBeInTheDocument();
  });

  it("does not render description paragraph when description prop is omitted", () => {
    const handleClose = vi.fn();
    render(
      <Modal isOpen={true} onClose={handleClose} title="No Description Modal">
        <div>Content</div>
      </Modal>
    );

    expect(screen.getByText("No Description Modal")).toBeInTheDocument();
    expect(screen.queryByText("undefined")).not.toBeInTheDocument();
  });

  it("calls onClose when backdrop is clicked", () => {
    const handleClose = vi.fn();
    const { container } = render(
      <Modal isOpen={true} onClose={handleClose} title="Backdrop Test">
        <p>Content</p>
      </Modal>
    );

    // Backdrop is the div with bg-black/70
    const backdrop = container.querySelector(".bg-black\\/70");
    expect(backdrop).not.toBeNull();
    if (backdrop) {
      fireEvent.click(backdrop);
      expect(handleClose).toHaveBeenCalledTimes(1);
    }
  });

  it("calls onClose when the close X button is clicked", () => {
    const handleClose = vi.fn();
    render(
      <Modal isOpen={true} onClose={handleClose} title="Close Button Test">
        <p>Content</p>
      </Modal>
    );

    const closeBtn = screen.getByRole("button", { name: /close modal/i });
    fireEvent.click(closeBtn);
    expect(handleClose).toHaveBeenCalledTimes(1);
  });

  it("calls onClose when Escape key is pressed", () => {
    const handleClose = vi.fn();
    render(
      <Modal isOpen={true} onClose={handleClose} title="Escape Test">
        <p>Content</p>
      </Modal>
    );

    fireEvent.keyDown(window, { key: "Escape" });
    expect(handleClose).toHaveBeenCalledTimes(1);
  });

  it("does not call onClose when other keys are pressed", () => {
    const handleClose = vi.fn();
    render(
      <Modal isOpen={true} onClose={handleClose} title="Key Test">
        <p>Content</p>
      </Modal>
    );

    fireEvent.keyDown(window, { key: "Enter" });
    fireEvent.keyDown(window, { key: "Tab" });
    fireEvent.keyDown(window, { key: "ArrowDown" });
    expect(handleClose).not.toHaveBeenCalled();
  });

  it("cleans up keydown event listener on unmount", () => {
    const handleClose = vi.fn();
    const { unmount } = render(
      <Modal isOpen={true} onClose={handleClose} title="Unmount Test">
        <p>Content</p>
      </Modal>
    );

    unmount();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(handleClose).not.toHaveBeenCalled();
  });

  it("applies custom className to dialog box", () => {
    const handleClose = vi.fn();
    const { container } = render(
      <Modal
        isOpen={true}
        onClose={handleClose}
        title="Custom Class Test"
        className="max-w-2xl border-amber-400"
      >
        <p>Content</p>
      </Modal>
    );

    const dialog = container.querySelector(".max-w-2xl");
    expect(dialog).toBeInTheDocument();
    expect(dialog).toHaveClass("border-amber-400");
  });

  it("auto-focuses the first input inside children when opened", () => {
    render(
      <Modal isOpen={true} onClose={vi.fn()} title="Focus Input Test">
        <input aria-label="First Field" data-testid="first-input" />
        <button>Submit</button>
      </Modal>
    );

    expect(screen.getByTestId("first-input")).toHaveFocus();
  });

  it("auto-focuses the close button when no inputs are present", () => {
    render(
      <Modal isOpen={true} onClose={vi.fn()} title="Focus Close Button Test">
        <p>No interactive inputs here</p>
      </Modal>
    );

    expect(screen.getByRole("button", { name: /close modal/i })).toHaveFocus();
  });

  it("traps focus between first and last focusable elements on Tab and Shift+Tab", () => {
    render(
      <Modal isOpen={true} onClose={vi.fn()} title="Tab Trap Test">
        <input aria-label="First Field" data-testid="first-input" />
        <button data-testid="last-button">Last Action</button>
      </Modal>
    );

    const closeBtn = screen.getByRole("button", { name: /close modal/i });
    const lastBtn = screen.getByTestId("last-button");

    // Close button is the first element in DOM order
    closeBtn.focus();
    expect(closeBtn).toHaveFocus();

    // Shift+Tab from first element wraps to last element
    fireEvent.keyDown(window, { key: "Tab", shiftKey: true });
    expect(lastBtn).toHaveFocus();

    // Tab from last element wraps to first element
    fireEvent.keyDown(window, { key: "Tab", shiftKey: false });
    expect(closeBtn).toHaveFocus();
  });

  it("restores focus to previous active element when closed", () => {
    const trigger = document.createElement("button");
    trigger.textContent = "Open Modal";
    document.body.appendChild(trigger);
    trigger.focus();
    expect(trigger).toHaveFocus();

    const { rerender } = render(
      <Modal isOpen={true} onClose={vi.fn()} title="Restore Focus Test">
        <input data-testid="modal-input" />
      </Modal>
    );

    expect(screen.getByTestId("modal-input")).toHaveFocus();

    rerender(
      <Modal isOpen={false} onClose={vi.fn()} title="Restore Focus Test">
        <input data-testid="modal-input" />
      </Modal>
    );

    expect(trigger).toHaveFocus();
    document.body.removeChild(trigger);
  });

  it("renders icon badge when icon prop is provided", () => {
    render(
      <Modal
        isOpen={true}
        onClose={vi.fn()}
        title="Icon Modal"
        icon={<span data-testid="custom-icon">★</span>}
      >
        <p>Modal Content</p>
      </Modal>
    );

    expect(screen.getByTestId("custom-icon")).toBeInTheDocument();
  });
});
