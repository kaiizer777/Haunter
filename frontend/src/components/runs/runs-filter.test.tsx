import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { RunsFilter } from "./runs-filter";
import { RepoOut } from "@/lib/api";

describe("runs-filter.tsx", () => {
  const mockRepos: RepoOut[] = [
    {
      id: "repo_1",
      owner: "kaiizer777",
      name: "Haunter",
      default_branch: "main",
      language_hint: "python",
      is_active: true,
      created_at: "2026-01-01T00:00:00Z",
    },
    {
      id: "repo_2",
      owner: "octocat",
      name: "Spoon-Knife",
      default_branch: "master",
      language_hint: "c",
      is_active: true,
      created_at: "2026-01-02T00:00:00Z",
    },
  ];

  const defaultProps = {
    repos: mockRepos,
    selectedRepoId: "",
    selectedStatus: "",
    from: "",
    to: "",
    onRepoChange: vi.fn(),
    onStatusChange: vi.fn(),
    onFromChange: vi.fn(),
    onToChange: vi.fn(),
    onReset: vi.fn(),
  };

  it("renders all dropdown options and date inputs", () => {
    render(<RunsFilter {...defaultProps} />);

    // Check Filters label
    expect(screen.getByText("Filters:")).toBeInTheDocument();

    // Check repo options
    expect(screen.getByText("All Repositories")).toBeInTheDocument();
    expect(screen.getByText("kaiizer777/Haunter")).toBeInTheDocument();
    expect(screen.getByText("octocat/Spoon-Knife")).toBeInTheDocument();

    // Check status options
    expect(screen.getByText("All Statuses")).toBeInTheDocument();
    expect(screen.getByText("Completed (PR Opened)")).toBeInTheDocument();
    expect(screen.getByText("Fallback (Commented)")).toBeInTheDocument();
    expect(screen.getByText("Error")).toBeInTheDocument();
    expect(screen.getByText("Pending")).toBeInTheDocument();
    expect(screen.getByText("Context Gathering")).toBeInTheDocument();
    expect(screen.getByText("Fix Generation")).toBeInTheDocument();
    expect(screen.getByText("Verification")).toBeInTheDocument();

    // Check date inputs
    expect(screen.getByPlaceholderText("From")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("To")).toBeInTheDocument();
  });

  it("hides Clear button when no filters are active", () => {
    render(<RunsFilter {...defaultProps} />);
    expect(screen.queryByRole("button", { name: /clear/i })).not.toBeInTheDocument();
  });

  it("shows Clear button when selectedRepoId is set and calls onReset on click", () => {
    const onReset = vi.fn();
    render(<RunsFilter {...defaultProps} selectedRepoId="repo_1" onReset={onReset} />);

    const clearBtn = screen.getByRole("button", { name: /clear/i });
    expect(clearBtn).toBeInTheDocument();

    fireEvent.click(clearBtn);
    expect(onReset).toHaveBeenCalledTimes(1);
  });

  it("shows Clear button when selectedStatus is set", () => {
    render(<RunsFilter {...defaultProps} selectedStatus="completed" />);
    expect(screen.getByRole("button", { name: /clear/i })).toBeInTheDocument();
  });

  it("shows Clear button when from date is set", () => {
    render(<RunsFilter {...defaultProps} from="2026-01-01" />);
    expect(screen.getByRole("button", { name: /clear/i })).toBeInTheDocument();
  });

  it("shows Clear button when to date is set", () => {
    render(<RunsFilter {...defaultProps} to="2026-01-31" />);
    expect(screen.getByRole("button", { name: /clear/i })).toBeInTheDocument();
  });

  it("triggers onRepoChange when repo select value changes", () => {
    const onRepoChange = vi.fn();
    render(<RunsFilter {...defaultProps} onRepoChange={onRepoChange} />);

    const selectElements = screen.getAllByRole("combobox");
    const repoSelect = selectElements[0];

    fireEvent.change(repoSelect, { target: { value: "repo_2" } });
    expect(onRepoChange).toHaveBeenCalledWith("repo_2");
  });

  it("triggers onStatusChange when status select value changes", () => {
    const onStatusChange = vi.fn();
    render(<RunsFilter {...defaultProps} onStatusChange={onStatusChange} />);

    const selectElements = screen.getAllByRole("combobox");
    const statusSelect = selectElements[1];

    fireEvent.change(statusSelect, { target: { value: "fix_generation" } });
    expect(onStatusChange).toHaveBeenCalledWith("fix_generation");
  });

  it("triggers onFromChange and onToChange when date inputs change", () => {
    const onFromChange = vi.fn();
    const onToChange = vi.fn();
    render(
      <RunsFilter
        {...defaultProps}
        onFromChange={onFromChange}
        onToChange={onToChange}
      />
    );

    const fromInput = screen.getByPlaceholderText("From");
    const toInput = screen.getByPlaceholderText("To");

    fireEvent.change(fromInput, { target: { value: "2026-02-01" } });
    expect(onFromChange).toHaveBeenCalledWith("2026-02-01");

    fireEvent.change(toInput, { target: { value: "2026-02-28" } });
    expect(onToChange).toHaveBeenCalledWith("2026-02-28");
  });
});
