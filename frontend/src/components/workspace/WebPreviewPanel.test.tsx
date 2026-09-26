import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { WebPreviewPanel, EnvDrawer } from "./WebPreviewPanel";

const baseProps = {
  status: "ready" as const,
  previewUrl: "https://preview.example.com",
  onBoot: vi.fn(),
  onRefresh: vi.fn(),
  onRestartServer: vi.fn(),
};

describe("WebPreviewPanel.tsx", () => {
  it("renders the idle boot CTA when status is idle", () => {
    render(<WebPreviewPanel {...baseProps} status="idle" previewUrl={null} />);
    expect(screen.getByText("Launch Preview")).toBeInTheDocument();
    expect(screen.queryByTitle("Live preview")).not.toBeInTheDocument();
  });

  it("renders a sandboxed iframe in ready state", () => {
    const { container } = render(<WebPreviewPanel {...baseProps} />);
    const iframe = container.querySelector('iframe[title="Live preview"]');
    expect(iframe).toBeInTheDocument();
    expect(iframe).toHaveAttribute("src", "https://preview.example.com");
    expect(iframe).toHaveAttribute(
      "sandbox",
      "allow-scripts allow-same-origin allow-forms"
    );
  });

  it("reload remounts the iframe only and notifies via onRefresh", () => {
    const onRefresh = vi.fn();
    const { container } = render(
      <WebPreviewPanel {...baseProps} onRefresh={onRefresh} />
    );
    const before = container.querySelector('iframe[title="Live preview"]');
    fireEvent.click(screen.getByRole("button", { name: "Reload preview" }));
    expect(onRefresh).toHaveBeenCalledTimes(1);
    const after = container.querySelector('iframe[title="Live preview"]');
    expect(after).toBeInTheDocument();
    // Inner refreshNonce key remounts the iframe node itself.
    expect(after).not.toBe(before);
    expect(after).toHaveAttribute("src", "https://preview.example.com");
  });

  it("opens the env drawer and closes it on Escape", () => {
    render(<WebPreviewPanel {...baseProps} initialEnvVars={{ FOO: "1" }} />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /manage .env/i }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("autofocuses the first env key input on open", () => {
    render(<WebPreviewPanel {...baseProps} initialEnvVars={{ FOO: "1" }} />);
    fireEvent.click(screen.getByRole("button", { name: /manage .env/i }));
    const firstKey = screen.getByDisplayValue("FOO");
    expect(document.activeElement).toBe(firstKey);
  });
});

describe("EnvDrawer", () => {
  const drawerProps = {
    vars: { FOO: "1" },
    onSave: vi.fn(),
    onClose: vi.fn(),
  };

  it("syncs rows when vars identity changes externally", () => {
    const { rerender } = render(<EnvDrawer {...drawerProps} />);
    expect(screen.getByDisplayValue("FOO")).toBeInTheDocument();
    rerender(<EnvDrawer {...drawerProps} vars={{ BAR: "2" }} />);
    expect(screen.queryByDisplayValue("FOO")).not.toBeInTheDocument();
    expect(screen.getByDisplayValue("BAR")).toBeInTheDocument();
  });

  it("preserves user edits across re-renders with stable vars identity", () => {
    const vars = { FOO: "1" };
    const { rerender } = render(<EnvDrawer {...drawerProps} vars={vars} />);
    fireEvent.change(screen.getByDisplayValue("FOO"), {
      target: { value: "EDITED" },
    });
    rerender(<EnvDrawer {...drawerProps} vars={vars} />);
    expect(screen.getByDisplayValue("EDITED")).toBeInTheDocument();
  });

  it("assigns unique ids to added rows (independent removal)", () => {
    render(<EnvDrawer {...drawerProps} vars={{}} />);
    fireEvent.click(screen.getByRole("button", { name: "Add Variable" }));
    fireEvent.click(screen.getByRole("button", { name: "Add Variable" }));
    expect(screen.getAllByLabelText("Variable key")).toHaveLength(2);
    // Removing one row leaves exactly one — proves distinct React keys/ids.
    fireEvent.click(screen.getAllByLabelText("Remove variable")[0]);
    expect(screen.getAllByLabelText("Variable key")).toHaveLength(1);
  });

  it("saves trimmed non-empty entries", () => {
    const onSave = vi.fn();
    render(<EnvDrawer {...drawerProps} vars={{}} onSave={onSave} />);
    fireEvent.click(screen.getByRole("button", { name: "Add Variable" }));
    fireEvent.change(screen.getByLabelText("Variable key"), {
      target: { value: "  NEW_KEY  " },
    });
    fireEvent.change(screen.getByPlaceholderText("value"), {
      target: { value: "abc" },
    });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    expect(onSave).toHaveBeenCalledWith({ NEW_KEY: "abc" });
  });
});
