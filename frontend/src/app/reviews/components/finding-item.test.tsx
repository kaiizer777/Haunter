import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { FindingItem } from "./finding-item";
import type { ReviewFindingOut } from "../types";
import { clearStagedFixes } from "../lib/review-helpers";

describe("FindingItem component", () => {
  let writeTextMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.restoreAllMocks();
    clearStagedFixes();
    writeTextMock = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: writeTextMock },
      writable: true,
      configurable: true,
    });
  });

  const baseFinding: ReviewFindingOut = {
    file_path: "src/auth/token-service.ts",
    line_start: 45,
    line_end: 52,
    category: "security",
    severity: "critical",
    critique: "Token signing secret hardcoded in source repository.",
    symbol_name: "signJwt",
    suggested_patch: "@@ -45,3 +45,3 @@\n- const secret = 'dev_secret';\n+ const secret = process.env.JWT_SECRET;",
  };

  it("renders priority badge, category, symbol, and GitHub blob link", () => {
    render(
      <FindingItem
        finding={baseFinding}
        repoOwner="acme"
        repoName="sentinel"
        commitSha="c0ffee123456"
      />
    );

    // Badges
    expect(screen.getByText("Security")).toBeInTheDocument();
    expect(screen.getByText("Must Fix")).toBeInTheDocument();
    expect(screen.getByText("CRITICAL")).toBeInTheDocument();
    expect(screen.getByText("signJwt")).toBeInTheDocument();

    // Critique
    expect(
      screen.getByText("Token signing secret hardcoded in source repository.")
    ).toBeInTheDocument();

    // GitHub permalink
    const link = screen.getByRole("link", {
      name: /src\/auth\/token-service\.ts:45-52/i,
    });
    expect(link).toHaveAttribute(
      "href",
      "https://github.com/acme/sentinel/blob/c0ffee123456/src/auth/token-service.ts#L45-L52"
    );
  });

  it("renders architectural callout mode when line_start is 0 or non_commentable is true", () => {
    const archFinding: ReviewFindingOut & { non_commentable?: boolean } = {
      file_path: "(general)",
      line_start: 0,
      line_end: 0,
      category: "architecture",
      severity: "high",
      critique: "Repository lacks boundary between data access and presentation layers.",
      non_commentable: true,
    };

    render(
      <FindingItem
        finding={archFinding}
        repoOwner="acme"
        repoName="sentinel"
        commitSha="c0ffee123456"
      />
    );

    expect(screen.getByTestId("architectural-tag")).toHaveTextContent(
      "SYSTEM ARCHITECTURE"
    );
    expect(screen.queryByText("(general):0")).not.toBeInTheDocument();
    expect(screen.getByText("Architecture")).toBeInTheDocument();
  });

  it("does not render architectural callout for architecture category if line_start > 0 and path is valid", () => {
    const anchoredArchFinding: ReviewFindingOut = {
      file_path: "src/layer.ts",
      line_start: 22,
      line_end: 25,
      category: "architecture",
      severity: "medium",
      critique: "Layer violation: Controller directly accesses database entity.",
    };

    render(
      <FindingItem
        finding={anchoredArchFinding}
        repoOwner="acme"
        repoName="sentinel"
        commitSha="c0ffee123456"
      />
    );

    expect(screen.queryByTestId("architectural-tag")).not.toBeInTheDocument();
    expect(screen.getByText("src/layer.ts:22-25")).toBeInTheDocument();
  });

  it("copies patch to clipboard on Copy Patch click", async () => {
    render(
      <FindingItem
        finding={baseFinding}
        repoOwner="acme"
        repoName="sentinel"
        commitSha="c0ffee123456"
      />
    );

    const copyBtn = screen.getByRole("button", { name: /copy patch/i });
    fireEvent.click(copyBtn);

    expect(writeTextMock).toHaveBeenCalledWith(baseFinding.suggested_patch);
    await waitFor(() => {
      expect(screen.getByText("Copied")).toBeInTheDocument();
    });
  });

  it("toggles fix staging in localStorage on Stage Fix click", () => {
    const onToggleStageMock = vi.fn();
    render(
      <FindingItem
        finding={baseFinding}
        repoOwner="acme"
        repoName="sentinel"
        commitSha="c0ffee123456"
        onToggleStage={onToggleStageMock}
      />
    );

    const stageBtn = screen.getByRole("button", { name: /stage fix/i });
    fireEvent.click(stageBtn);

    expect(screen.getByText("Staged Fix")).toBeInTheDocument();
    expect(onToggleStageMock).toHaveBeenCalledWith(baseFinding, true);

    // Click again to un-stage
    fireEvent.click(stageBtn);
    expect(screen.getByText("Stage Fix")).toBeInTheDocument();
    expect(onToggleStageMock).toHaveBeenCalledWith(baseFinding, false);
  });

  it("collapses and expands patch diff viewer", () => {
    render(
      <FindingItem
        finding={baseFinding}
        repoOwner="acme"
        repoName="sentinel"
        commitSha="c0ffee123456"
      />
    );

    // Initially patch is visible
    expect(
      screen.getByText("- const secret = 'dev_secret';")
    ).toBeInTheDocument();

    // Toggle collapse
    const collapseToggle = screen.getByRole("button", {
      name: /Candidate Remediation Patch/i,
    });
    expect(collapseToggle).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(collapseToggle);
    expect(collapseToggle).toHaveAttribute("aria-expanded", "false");

    expect(
      screen.queryByText("- const secret = 'dev_secret';")
    ).not.toBeInTheDocument();

    // Re-expand
    fireEvent.click(collapseToggle);
    expect(collapseToggle).toHaveAttribute("aria-expanded", "true");
    expect(
      screen.getByText("- const secret = 'dev_secret';")
    ).toBeInTheDocument();
  });
});
