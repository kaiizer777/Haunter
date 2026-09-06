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

    const buttons = screen.getAllByRole("button");
    const closeBtn = buttons[0];
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
});
