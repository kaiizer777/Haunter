import { describe, it, expect } from "vitest";
import {
  getCategoryMeta,
  getPriorityMeta,
  getSeverityMeta,
} from "./category-meta";

describe("category-meta helpers", () => {
  it("resolves category meta for security, logic/correctness, performance, architecture, holistic", () => {
    const sec = getCategoryMeta("security");
    expect(sec.label).toBe("Security");
    expect(sec.color).toContain("text-rose-400");

    const correctness = getCategoryMeta("correctness");
    expect(correctness.label).toBe("Correctness");
    expect(correctness.color).toContain("text-amber-400");

    const perf = getCategoryMeta("performance");
    expect(perf.label).toBe("Performance");
    expect(perf.color).toContain("text-cyan-400");

    const arch = getCategoryMeta("architecture");
    expect(arch.label).toBe("Architecture");
    expect(arch.color).toContain("text-indigo-400");

    const holistic = getCategoryMeta("holistic");
    expect(holistic.label).toBe("Holistic Quality");
    expect(holistic.color).toContain("text-emerald-400");

    const fallback = getCategoryMeta("custom_category");
    expect(fallback.label).toBe("custom_category");
  });

  it("resolves priority bucket meta for must_fix, should_fix, suggestion", () => {
    const mustFix = getPriorityMeta("must_fix");
    expect(mustFix.label).toBe("Must Fix");
    expect(mustFix.text).toBe("text-rose-400");
    expect(mustFix.dot).toBe("bg-rose-400");

    const shouldFix = getPriorityMeta("should_fix");
    expect(shouldFix.label).toBe("Should Fix");
    expect(shouldFix.text).toBe("text-amber-400");

    const suggestion = getPriorityMeta("suggestion");
    expect(suggestion.label).toBe("Suggestion");
    expect(suggestion.text).toBe("text-cyan-300");
  });

  it("resolves severity meta accurately", () => {
    expect(getSeverityMeta("critical").label).toBe("CRITICAL");
    expect(getSeverityMeta("high").label).toBe("HIGH");
    expect(getSeverityMeta("medium").label).toBe("MEDIUM");
    expect(getSeverityMeta("low").label).toBe("LOW");
    expect(getSeverityMeta("unknown").label).toBe("LOW");
  });
});
