import { describe, it, expect } from "vitest";
import { isRetryableStatus, RETRYABLE_RUN_STATUSES } from "./retry-run";

describe("retry-run (lib/retry-run.ts)", () => {
  it("treats every orchestrator terminal status as retryable", () => {
    // Mirrors _TERMINAL_STATUSES in app/orchestrator.py, which
    // POST /runs/{id}/retry reads to decide retryability.
    expect([...RETRYABLE_RUN_STATUSES].sort()).toEqual(
      ["completed", "error", "fallback_commented", "flaky_detected", "pr_opened"].sort()
    );
  });

  it.each(["pr_opened", "fallback_commented", "flaky_detected", "completed", "error"])(
    "returns true for settled status %s",
    (status) => {
      expect(isRetryableStatus(status)).toBe(true);
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
  ])("returns false for in-flight status %s", (status) => {
    expect(isRetryableStatus(status)).toBe(false);
  });

  it("returns false for an unknown status rather than guessing", () => {
    expect(isRetryableStatus("something_new")).toBe(false);
    expect(isRetryableStatus("")).toBe(false);
  });

  it("is case-sensitive so a stray uppercase status cannot dispatch a retry", () => {
    expect(isRetryableStatus("PR_OPENED")).toBe(false);
  });
});