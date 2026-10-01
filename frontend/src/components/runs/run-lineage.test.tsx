import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import RunLineage from "./run-lineage";
import { RunSummaryOut } from "@/lib/api";

vi.mock("next/link", () => ({
  __esModule: true,
  default: ({
    href,
    children,
    ...props
  }: {
    href: string;
    children?: React.ReactNode;
  } & React.AnchorHTMLAttributes<HTMLAnchorElement>) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));

function makeRun(overrides: Partial<RunSummaryOut>): RunSummaryOut {
  return {
    id: "run-1",
    repo_id: "repo-1",
    status: "error",
    diagnosis_summary: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    parent_run_id: null,
    ...overrides,
  };
}

describe("RunLineage (components/runs/run-lineage.tsx)", () => {
  it("renders nothing for a root run with no children", () => {
    const { container } = render(<RunLineage runId="run-1" parent={null} childRuns={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when the childRuns prop is omitted entirely", () => {
    const { container } = render(<RunLineage runId="run-1" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("links to the source run when the current run is a child", () => {
    const parent = makeRun({ id: "root-run", status: "fallback_commented" });

    render(<RunLineage runId="child-run" parent={parent} childRuns={[]} />);

    expect(screen.getByRole("region", { name: /run retry lineage/i })).toBeInTheDocument();
    expect(screen.getByText("Retried from")).toBeInTheDocument();
    // StatusBadge renders fallback_commented as "Fallback Comment".
    expect(screen.getByRole("link", { name: /fallback comment/i })).toHaveAttribute(
      "href",
      "/runs/detail?id=root-run"
    );
  });

  it("lists every retry child with a link and correct pluralization", () => {
    const children = [
      makeRun({ id: "child-a", status: "pr_opened", pr_number: 11, pr_url: "u" }),
      makeRun({ id: "child-b", status: "error" }),
    ];

    render(<RunLineage runId="root-run" parent={null} childRuns={children} />);

    expect(screen.getByText("2 retries of this run")).toBeInTheDocument();
    // pr_opened renders as "PR Opened", error renders as "Failed".
    expect(screen.getByRole("link", { name: /pr opened/i })).toHaveAttribute(
      "href",
      "/runs/detail?id=child-a"
    );
    expect(screen.getByRole("link", { name: /failed/i })).toHaveAttribute(
      "href",
      "/runs/detail?id=child-b"
    );
    expect(screen.getByText("PR #11")).toBeInTheDocument();
  });

  it("uses the singular form for exactly one retry", () => {
    render(
      <RunLineage runId="root-run" parent={null} childRuns={[makeRun({ id: "child-a" })]} />
    );
    expect(screen.getByText("1 retry of this run")).toBeInTheDocument();
  });

  it("marks the current run as non-navigable when it appears in the thread", () => {
    const children = [
      makeRun({ id: "run-1", status: "pending" }),
      makeRun({ id: "child-b", status: "error" }),
    ];

    render(<RunLineage runId="run-1" parent={null} childRuns={children} />);

    const current = screen.getByText("this run").closest("[aria-current]");
    expect(current).toHaveAttribute("aria-current", "true");
    // The current run is not rendered as a link back to itself (pending -> "Pending").
    expect(screen.queryByRole("link", { name: /pending/i })).not.toBeInTheDocument();
    // The sibling is.
    expect(screen.getByRole("link", { name: /failed/i })).toBeInTheDocument();
  });

  it("renders both parent and children when a run sits mid-thread", () => {
    const parent = makeRun({ id: "root-run", status: "error" });
    const children = [makeRun({ id: "child-a", status: "pr_opened" })];

    render(<RunLineage runId="mid-run" parent={parent} childRuns={children} />);

    expect(screen.getByText("Retried from")).toBeInTheDocument();
    expect(screen.getByText("1 retry of this run")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /failed/i })).toHaveAttribute(
      "href",
      "/runs/detail?id=root-run"
    );
    expect(screen.getByRole("link", { name: /pr opened/i })).toHaveAttribute(
      "href",
      "/runs/detail?id=child-a"
    );
  });
});
