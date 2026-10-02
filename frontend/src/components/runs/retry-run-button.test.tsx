import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import RetryRunButton from "./retry-run-button";
import { api, RunOut } from "@/lib/api";

vi.mock("@/lib/api", () => ({
  api: {
    retryRun: vi.fn(),
  },
}));

const CHILD: RunOut = {
  id: "child-run-uuid",
  repo_id: "repo-1",
  github_run_id: null,
  github_delivery_id: null,
  head_sha: "a".repeat(40),
  head_branch: "main",
  status: "pending",
  conclusion: "failure",
  parent_run_id: "parent-run-uuid",
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
};

describe("RetryRunButton (components/runs/retry-run-button.tsx)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it.each(["pr_opened", "fallback_commented", "flaky_detected", "completed", "error"])(
    "renders and dispatches a retry for settled status %s",
    async (status) => {
      vi.mocked(api.retryRun).mockResolvedValue(CHILD);
      const onRetried = vi.fn();

      render(<RetryRunButton runId="parent-run-uuid" status={status} onRetried={onRetried} />);

      await userEvent.click(screen.getByRole("button"));

      await waitFor(() => expect(api.retryRun).toHaveBeenCalledWith("parent-run-uuid"));
      await waitFor(() => expect(onRetried).toHaveBeenCalledWith(CHILD));
    }
  );

  it.each([
    "pending",
    "context_gathering",
    "flake_verification",
    "fix_generation",
    "verification",
    "pending_pr",
    "fallback",
  ])("renders nothing for in-flight status %s", (status) => {
    const { container } = render(
      <RetryRunButton runId="parent-run-uuid" status={status} />
    );
    expect(container).toBeEmptyDOMElement();
    expect(api.retryRun).not.toHaveBeenCalled();
  });

  it("surfaces a server error without leaving the control stuck in a loading state", async () => {
    vi.mocked(api.retryRun).mockRejectedValue(new Error("A retry is already in progress for this run."));
    const onRetried = vi.fn();

    render(<RetryRunButton runId="parent-run-uuid" status="error" onRetried={onRetried} />);

    await userEvent.click(screen.getByRole("button"));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("A retry is already in progress for this run.");
    expect(onRetried).not.toHaveBeenCalled();
    // Button re-enabled so the user can act on the failure.
    await waitFor(() =>
      expect(screen.getByRole("button")).not.toHaveAttribute("aria-busy", "true")
    );
  });

  it("works without an onRetried callback", async () => {
    vi.mocked(api.retryRun).mockResolvedValue(CHILD);

    render(<RetryRunButton runId="parent-run-uuid" status="error" />);
    await userEvent.click(screen.getByRole("button"));

    await waitFor(() => expect(api.retryRun).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("applies a custom accessible label", () => {
    render(<RetryRunButton runId="abc" status="error" aria-label="Retry this run" />);
    expect(screen.getByRole("button", { name: "Retry this run" })).toBeInTheDocument();
  });
});