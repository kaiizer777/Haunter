import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { RepoSettingsCard } from "./RepoSettingsCard";
import { RepoSettingsOut } from "@/lib/api";

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
    file_issue_on_fallback: true,
    audit_trigger_on_pr: true,
    audit_trigger_on_ci_failure: true,
    audit_trigger_on_ci_success: false,
    audit_trigger_on_manual_mention: true,
    allowed_branches: ["main", "master"],
    monitored_branches: ["main", "master"],
    ignore_draft_prs: true,
    max_cost_per_run_cents: 100,
    model_override_scope: "inherit",
    review_guidelines_text: "Follow team idioms.",
    review_strictness: "high",
    review_focus_areas: ["security", "performance"],
    review_checklist: ["tests pass", "no console.log"],
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
    expect(screen.getByText("Custom Policy")).toBeInTheDocument();

    // Feature switches
    expect(screen.getByText("Autonomous Fix Generator & Auto-PR")).toBeInTheDocument();
    expect(screen.getByText("Multi-Perspective Auditor Bot")).toBeInTheDocument();
    expect(screen.getByText("CI Mirror Sandbox Runner")).toBeInTheDocument();
    expect(screen.getByText("PR & Commit Annotations")).toBeInTheDocument();

    // Auditor triggers
    expect(screen.getByText("Trigger on Pull Requests")).toBeInTheDocument();
    expect(screen.getByText("Trigger on CI Failures")).toBeInTheDocument();

    // Operational bounds
    expect(screen.getByLabelText(/monitored branches/i)).toHaveValue("main, master");
    expect(screen.getByLabelText(/max budget cap per run/i)).toHaveValue(100);
  });

  it("calls onApplyPreset when clicking a preset card and updates UI", async () => {
    const conservativeSettings: RepoSettingsOut = {
      ...mockSettings,
      preset: "conservative",
      preset_profile: "conservative",
      enable_auto_fix: false,
      enable_auditor_mode: true,
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

    const saveBtn = screen.getByRole("button", { name: /save governance policy/i });
    expect(saveBtn).not.toBeDisabled();

    fireEvent.click(saveBtn);

    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({
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

  it("renders review preference controls with server values", () => {
    render(
      <RepoSettingsCard
        settings={mockSettings}
        repoFullName="kaiizer777/Haunter"
        repoId="repo_123"
        onSave={onSave}
        onApplyPreset={onApplyPreset}
      />
    );

    expect(screen.getByText("PR Review Preferences")).toBeInTheDocument();
    expect(screen.getByLabelText(/team review guidelines/i)).toHaveValue("Follow team idioms.");
    expect(screen.getByRole("button", { name: "Review strictness" })).toHaveTextContent("High");
    expect(screen.getByLabelText(/review focus areas/i)).toHaveValue("security, performance");
    expect(screen.getByLabelText(/review checklist/i)).toHaveValue("tests pass, no console.log");
  });

  it("changing strictness marks the form dirty and flips the preset to custom", async () => {
    render(
      <RepoSettingsCard
        settings={mockSettings}
        repoFullName="kaiizer777/Haunter"
        repoId="repo_123"
        onSave={onSave}
        onApplyPreset={onApplyPreset}
      />
    );

    const strictnessBtn = screen.getByRole("button", { name: "Review strictness" });
    fireEvent.click(strictnessBtn);

    const lowOption = await screen.findByRole("option", { name: /^low/i });
    fireEvent.click(lowOption);

    expect(screen.getByText("Unsaved policy changes")).toBeInTheDocument();
    expect(screen.getByText("custom")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Review strictness" })).toHaveTextContent("Low");
    expect(screen.getByRole("button", { name: /save governance policy/i })).not.toBeDisabled();
  });

  it("blocks save when guidelines exceed 4000 characters", () => {
    render(
      <RepoSettingsCard
        settings={mockSettings}
        repoFullName="kaiizer777/Haunter"
        repoId="repo_123"
        onSave={onSave}
        onApplyPreset={onApplyPreset}
      />
    );

    const guidelines = screen.getByLabelText(/team review guidelines/i);
    fireEvent.change(guidelines, { target: { value: "x".repeat(4001) } });

    expect(screen.getByRole("alert")).toHaveTextContent(/exceed maximum length of 4000/);
    const saveBtn = screen.getByRole("button", { name: /save governance policy/i });
    expect(saveBtn).toBeDisabled();
    fireEvent.click(saveBtn);
    expect(onSave).not.toHaveBeenCalled();
  });

  it("blocks save when focus-area list exceeds item caps", () => {
    render(
      <RepoSettingsCard
        settings={mockSettings}
        repoFullName="kaiizer777/Haunter"
        repoId="repo_123"
        onSave={onSave}
        onApplyPreset={onApplyPreset}
      />
    );

    const focus = screen.getByLabelText(/review focus areas/i);
    const saveBtn = screen.getByRole("button", { name: /save governance policy/i });

    const tooMany = Array.from({ length: 21 }, (_, i) => `topic-${i}`).join(", ");
    fireEvent.change(focus, { target: { value: tooMany } });
    expect(screen.getByRole("alert")).toHaveTextContent(/at most 20 items/);
    expect(saveBtn).toBeDisabled();

    fireEvent.change(focus, { target: { value: `ok, ${"y".repeat(201)}` } });
    expect(screen.getByRole("alert")).toHaveTextContent(/at most 200 characters/);
    expect(saveBtn).toBeDisabled();
    fireEvent.click(saveBtn);
    expect(onSave).not.toHaveBeenCalled();
  });

  it("submits review prefs in the save payload", async () => {
    onSave.mockResolvedValueOnce({ ...mockSettings, settings_version: 2 });

    render(
      <RepoSettingsCard
        settings={mockSettings}
        repoFullName="kaiizer777/Haunter"
        repoId="repo_123"
        onSave={onSave}
        onApplyPreset={onApplyPreset}
      />
    );

    fireEvent.change(screen.getByLabelText(/team review guidelines/i), {
      target: { value: "  Ship small diffs.  " },
    });
    fireEvent.change(screen.getByLabelText(/review focus areas/i), {
      target: { value: "security, reliability" },
    });
    fireEvent.change(screen.getByLabelText(/review checklist/i), {
      target: { value: "tests pass" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Review strictness" }));
    fireEvent.click(await screen.findByRole("option", { name: /^medium/i }));

    fireEvent.click(screen.getByRole("button", { name: /save governance policy/i }));

    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({
        review_guidelines_text: "Ship small diffs.",
        review_strictness: "medium",
        review_focus_areas: ["security", "reliability"],
        review_checklist: ["tests pass"],
      })
    );

    await waitFor(() => {
      expect(screen.getByText(/repository governance settings saved \(v2\)/i)).toBeInTheDocument();
    });
  });

  it("sends null guidelines when cleared and restores prefs on reset", async () => {
    onSave.mockResolvedValueOnce({ ...mockSettings, settings_version: 2 });

    render(
      <RepoSettingsCard
        settings={mockSettings}
        repoFullName="kaiizer777/Haunter"
        repoId="repo_123"
        onSave={onSave}
        onApplyPreset={onApplyPreset}
      />
    );

    const guidelines = screen.getByLabelText(/team review guidelines/i);
    fireEvent.change(guidelines, { target: { value: "   " } });

    fireEvent.click(screen.getByRole("button", { name: /save governance policy/i }));
    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({ review_guidelines_text: null })
    );

    await waitFor(() => {
      expect(screen.getByText(/repository governance settings saved \(v2\)/i)).toBeInTheDocument();
    });

    // Unsaveable edits are still restorable via Reset.
    fireEvent.change(screen.getByLabelText(/review focus areas/i), { target: { value: "oops" } });
    fireEvent.click(screen.getByRole("button", { name: /reset/i }));

    expect(screen.getByLabelText(/team review guidelines/i)).toHaveValue("Follow team idioms.");
    expect(screen.getByLabelText(/review focus areas/i)).toHaveValue("security, performance");
    expect(screen.getByLabelText(/review checklist/i)).toHaveValue("tests pass, no console.log");
    expect(screen.getByRole("button", { name: "Review strictness" })).toHaveTextContent("High");
    expect(screen.getByText("All policies in sync")).toBeInTheDocument();
  });
});
