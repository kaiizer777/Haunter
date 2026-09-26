import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RepoSettingsCard } from "./RepoSettingsCard";
import { RepoSettingsOut, RepoSettingsUpdate } from "@/lib/api";

describe("RepoSettingsCard (components/settings/RepoSettingsCard.tsx)", () => {
  const mockSettings: RepoSettingsOut = {
    id: "settings_1",
    repo_id: "repo_123",
    preset: "autonomous",
    preset_profile: "autonomous",
    enable_auto_fix: true,
    enable_auto_fixer: true,
    enable_auditor_mode: false,
    enable_sandbox_verification: true,
    enable_ci_sandbox: true,
    enable_pr_comments: true,
    enable_live_sessions: true,
    enable_webcontainer_preview: true,
    enable_subagents: true,
    audit_trigger_on_pr: true,
    audit_trigger_on_ci_failure: true,
    audit_trigger_on_ci_success: false,
    audit_trigger_on_manual_mention: true,
    allowed_branches: ["main", "master"],
    monitored_branches: ["main", "master"],
    ignore_draft_prs: true,
    min_confidence_threshold: 80,
    max_cost_per_run_cents: 100,
    model_override_scope: "inherit",
    settings_version: 1,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  };

  const onSave = vi.fn();
  const onApplyPreset = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders all preset cards, feature switches, and operational safety bounds", () => {
    render(
      <RepoSettingsCard
        settings={mockSettings}
        repoFullName="kaiizer777/Haunter"
        repoId="repo_123"
        onSave={onSave}
        onApplyPreset={onApplyPreset}
      />
    );

    // Presets
    expect(screen.getByText("Autonomous DevOps")).toBeInTheDocument();
    expect(screen.getByText("Conservative Guardian")).toBeInTheDocument();
    expect(screen.getByText("Standard Dual-Engine")).toBeInTheDocument();
    expect(screen.getByText("Read-Only Auditor")).toBeInTheDocument();
    expect(screen.getByText("Live Studio Only")).toBeInTheDocument();
    expect(screen.getByText("Custom Policy")).toBeInTheDocument();

    // Feature switches
    expect(screen.getByText("Autonomous Fix Generator & Auto-PR")).toBeInTheDocument();
    expect(screen.getByText("Multi-Perspective Auditor Bot")).toBeInTheDocument();
    expect(screen.getByText("CI Mirror Sandbox Runner")).toBeInTheDocument();
    expect(screen.getByText("PR & Commit Annotations")).toBeInTheDocument();
    expect(screen.getByText("Interactive Live Cloud Sessions")).toBeInTheDocument();
    expect(screen.getByText("In-Browser WebContainer Preview")).toBeInTheDocument();
    expect(screen.getByText("Subagent Task Delegation Engine")).toBeInTheDocument();

    // Auditor triggers
    expect(screen.getByText("Trigger on Pull Requests")).toBeInTheDocument();
    expect(screen.getByText("Trigger on CI Failures")).toBeInTheDocument();

    // Operational bounds
    expect(screen.getByLabelText(/monitored branches/i)).toHaveValue("main, master");
    expect(screen.getByLabelText(/min confidence threshold/i)).toHaveValue("80");
    expect(screen.getByLabelText(/max budget cap per run/i)).toHaveValue(100);
  });

  it("calls onApplyPreset when clicking a preset card and updates UI", async () => {
    const conservativeSettings: RepoSettingsOut = {
      ...mockSettings,
      preset: "conservative",
      preset_profile: "conservative",
      enable_auto_fix: false,
      enable_auditor_mode: true,
      min_confidence_threshold: 90,
      max_cost_per_run_cents: 50,
      settings_version: 2,
    };
    onApplyPreset.mockResolvedValueOnce(conservativeSettings);

    render(
      <RepoSettingsCard
        settings={mockSettings}
        repoFullName="kaiizer777/Haunter"
        repoId="repo_123"
        onSave={onSave}
        onApplyPreset={onApplyPreset}
      />
    );

    const conservativeBtn = screen.getByRole("button", { name: /conservative guardian/i });
    fireEvent.click(conservativeBtn);

    expect(onApplyPreset).toHaveBeenCalledWith("conservative");
    await waitFor(() => {
      expect(screen.getByText(/applied 'conservative' governance preset profile/i)).toBeInTheDocument();
    });
  });

  it("modifying a feature toggle marks form as dirty and switches preset label to custom", async () => {
    render(
      <RepoSettingsCard
        settings={mockSettings}
        repoFullName="kaiizer777/Haunter"
        repoId="repo_123"
        onSave={onSave}
        onApplyPreset={onApplyPreset}
      />
    );

    // Initial state is clean
    expect(screen.getByText("All policies in sync")).toBeInTheDocument();
    const saveBtn = screen.getByRole("button", { name: /save governance policy/i });
    expect(saveBtn).toBeDisabled();

    // Toggle auto-fix switch
    const autoFixSwitch = screen.getByRole("switch", { name: "Autonomous Fix Generator & Auto-PR" });
    fireEvent.click(autoFixSwitch);

    // Form is now dirty
    expect(screen.getByText("Unsaved policy changes")).toBeInTheDocument();
    expect(saveBtn).not.toBeDisabled();
    expect(screen.getByText("custom")).toBeInTheDocument();
  });

  it("validates branch input and shows validation alert when branches are invalid", async () => {
    render(
      <RepoSettingsCard
        settings={mockSettings}
        repoFullName="kaiizer777/Haunter"
        repoId="repo_123"
        onSave={onSave}
        onApplyPreset={onApplyPreset}
      />
    );

    const branchInput = screen.getByLabelText(/monitored branches/i);
    fireEvent.change(branchInput, { target: { value: "invalid branch with space" } });

    expect(screen.getByRole("alert")).toHaveTextContent(/contains invalid characters/i);
    const saveBtn = screen.getByRole("button", { name: /save governance policy/i });
    expect(saveBtn).toBeDisabled();
  });

  it("resets dirty changes when Reset button is clicked", async () => {
    render(
      <RepoSettingsCard
        settings={mockSettings}
        repoFullName="kaiizer777/Haunter"
        repoId="repo_123"
        onSave={onSave}
        onApplyPreset={onApplyPreset}
      />
    );

    const branchInput = screen.getByLabelText(/monitored branches/i);
    fireEvent.change(branchInput, { target: { value: "release/*" } });

    expect(screen.getByText("Unsaved policy changes")).toBeInTheDocument();
    const resetBtn = screen.getByRole("button", { name: /reset/i });
    expect(resetBtn).not.toBeDisabled();

    fireEvent.click(resetBtn);

    expect(screen.getByLabelText(/monitored branches/i)).toHaveValue("main, master");
    expect(screen.getByText("All policies in sync")).toBeInTheDocument();
  });

  it("submits updated policy payload when Save Governance Policy is clicked", async () => {
    const updatedSettings: RepoSettingsOut = {
      ...mockSettings,
      min_confidence_threshold: 85,
      max_cost_per_run_cents: 150,
      settings_version: 2,
    };
    onSave.mockResolvedValueOnce(updatedSettings);

    render(
      <RepoSettingsCard
        settings={mockSettings}
        repoFullName="kaiizer777/Haunter"
        repoId="repo_123"
        onSave={onSave}
        onApplyPreset={onApplyPreset}
      />
    );

    const costInput = screen.getByLabelText(/max budget cap per run/i);
    fireEvent.change(costInput, { target: { value: "150" } });

    const confidenceSlider = screen.getByLabelText(/min confidence threshold/i);
    fireEvent.change(confidenceSlider, { target: { value: "85" } });

    const saveBtn = screen.getByRole("button", { name: /save governance policy/i });
    expect(saveBtn).not.toBeDisabled();

    fireEvent.click(saveBtn);

    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({
        min_confidence_threshold: 85,
        max_cost_per_run_cents: 150,
      })
    );

    await waitFor(() => {
      expect(screen.getByText(/repository governance settings saved \(v2\)/i)).toBeInTheDocument();
    });
  });

  it("preserves success banner when rerendering with updated settings for the same repo, but clears on repo change", async () => {
    const { rerender } = render(
      <RepoSettingsCard
        settings={mockSettings}
        repoFullName="kaiizer777/Haunter"
        repoId="repo_123"
        onSave={onSave}
        onApplyPreset={onApplyPreset}
      />
    );

    // Apply preset to trigger a success banner
    const conservativeSettings: RepoSettingsOut = {
      ...mockSettings,
      preset: "conservative",
      preset_profile: "conservative",
    };
    onApplyPreset.mockResolvedValueOnce(conservativeSettings);

    const conservativeBtn = screen.getByRole("button", { name: /conservative guardian/i });
    fireEvent.click(conservativeBtn);

    await waitFor(() => {
      expect(screen.getByText(/applied 'conservative' governance preset profile/i)).toBeInTheDocument();
    });

    // Parent re-renders with fresh settings reference for the same repo
    rerender(
      <RepoSettingsCard
        settings={{ ...conservativeSettings }}
        repoFullName="kaiizer777/Haunter"
        repoId="repo_123"
        onSave={onSave}
        onApplyPreset={onApplyPreset}
      />
    );

    // Success banner must still be present
    expect(screen.getByText(/applied 'conservative' governance preset profile/i)).toBeInTheDocument();

    // Now parent switches to a different repository
    const differentRepoSettings: RepoSettingsOut = {
      ...mockSettings,
      id: "settings_2",
      repo_id: "repo_999",
    };

    rerender(
      <RepoSettingsCard
        settings={differentRepoSettings}
        repoFullName="kaiizer777/AnotherRepo"
        repoId="repo_999"
        onSave={onSave}
        onApplyPreset={onApplyPreset}
      />
    );

    // Status banner should be cleared on repo switch
    expect(screen.queryByText(/applied 'conservative' governance preset profile/i)).not.toBeInTheDocument();
  });
});
