import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
  cn,
  formatNumber,
  formatCost,
  formatLatency,
  formatRelativeTime,
} from "./utils";

describe("utils.ts", () => {
  describe("cn", () => {
    it("returns empty string when no arguments provided", () => {
      expect(cn()).toBe("");
    });

    it("returns single class string as-is", () => {
      expect(cn("px-4")).toBe("px-4");
    });

    it("joins multiple non-conflicting classes", () => {
      expect(cn("px-4", "py-2", "text-sm")).toBe("px-4 py-2 text-sm");
    });

    it("merges conflicting Tailwind utilities with last winning", () => {
      expect(cn("p-2", "p-4")).toBe("p-4");
      expect(cn("text-red-500", "text-blue-500")).toBe("text-blue-500");
      expect(cn("px-2", "px-4", "p-6")).toBe("p-6");
    });

    it("ignores falsy values: undefined, null, false, empty string", () => {
      expect(cn("base", false, null, undefined, "", "extra")).toBe("base extra");
    });

    it("handles arrays of classes and nested arrays", () => {
      expect(cn(["btn", "btn-primary"], ["shadow", ["nested"]])).toBe(
        "btn btn-primary shadow nested"
      );
    });

    it("handles conditional objects", () => {
      expect(
        cn("btn", {
          "btn-active": true,
          "btn-disabled": false,
        })
      ).toBe("btn btn-active");
    });

    it("handles combinations of strings, booleans, objects, and arrays", () => {
      const isSelected = true;
      const isHovered = false;
      expect(
        cn("tab", isSelected && "tab-selected", isHovered && "tab-hovered", [
          "font-bold",
        ])
      ).toBe("tab tab-selected font-bold");
    });
  });

  describe("formatNumber", () => {
    it("formats integer numbers with thousands separators by default", () => {
      expect(formatNumber(1000)).toBe("1,000");
      expect(formatNumber(1000000)).toBe("1,000,000");
      expect(formatNumber(0)).toBe("0");
    });

    it("rounds to nearest integer when decimals default to 0", () => {
      expect(formatNumber(1234.56)).toBe("1,235");
      expect(formatNumber(1234.4)).toBe("1,234");
    });

    it("formats numbers with specified decimal places", () => {
      expect(formatNumber(1234.5678, 2)).toBe("1,234.57");
      expect(formatNumber(10, 2)).toBe("10.00");
      expect(formatNumber(0, 3)).toBe("0.000");
    });

    it("formats negative numbers correctly", () => {
      expect(formatNumber(-1234.5, 1)).toBe("-1,234.5");
    });
  });

  describe("formatCost", () => {
    it("returns $0.00 for null, undefined, or 0", () => {
      expect(formatCost(null)).toBe("$0.00");
      expect(formatCost(undefined)).toBe("$0.00");
      expect(formatCost(0)).toBe("$0.00");
    });

    it("returns <$0.0001 for positive costs strictly less than 0.0001", () => {
      expect(formatCost(0.00005)).toBe("<$0.0001");
      expect(formatCost(0.00001)).toBe("<$0.0001");
    });

    it("formats costs equal to or greater than 0.0001 with 4 decimals", () => {
      expect(formatCost(0.0001)).toBe("$0.0001");
      expect(formatCost(0.005)).toBe("$0.0050");
      expect(formatCost(1.23456)).toBe("$1.2346");
      expect(formatCost(25)).toBe("$25.0000");
    });
  });

  describe("formatLatency", () => {
    it("returns 0ms for null or undefined", () => {
      expect(formatLatency(null)).toBe("0ms");
      expect(formatLatency(undefined)).toBe("0ms");
    });

    it("formats latency below 1000ms as integer milliseconds", () => {
      expect(formatLatency(0)).toBe("0ms");
      expect(formatLatency(150)).toBe("150ms");
      expect(formatLatency(999)).toBe("999ms");
    });

    it("formats latency >= 1000ms as seconds with 2 decimals", () => {
      expect(formatLatency(1000)).toBe("1.00s");
      expect(formatLatency(1500)).toBe("1.50s");
      expect(formatLatency(2345)).toBe("2.35s");
      expect(formatLatency(60000)).toBe("60.00s");
    });
  });

  describe("formatRelativeTime", () => {
    const fixedNow = new Date("2026-09-06T12:00:00.000Z");

    beforeEach(() => {
      vi.useFakeTimers();
      vi.setSystemTime(fixedNow);
    });

    afterEach(() => {
      vi.useRealTimers();
    });

    it("returns 'just now' when diff < 10 seconds", () => {
      const date = new Date("2026-09-06T11:59:55.000Z"); // 5s ago
      expect(formatRelativeTime(date)).toBe("just now");
      expect(formatRelativeTime(date.toISOString())).toBe("just now");
    });

    it("returns 'Xs ago' when diff < 60 seconds", () => {
      const date = new Date("2026-09-06T11:59:30.000Z"); // 30s ago
      expect(formatRelativeTime(date)).toBe("30s ago");
      expect(formatRelativeTime(date.toISOString())).toBe("30s ago");
    });

    it("returns 'Xm ago' when diff < 60 minutes", () => {
      const date = new Date("2026-09-06T11:45:00.000Z"); // 15m ago
      expect(formatRelativeTime(date)).toBe("15m ago");
    });

    it("returns 'Xh ago' when diff < 24 hours", () => {
      const date = new Date("2026-09-06T09:00:00.000Z"); // 3h ago
      expect(formatRelativeTime(date)).toBe("3h ago");
    });

    it("returns 'Xd ago' when diff < 30 days", () => {
      const date = new Date("2026-09-01T12:00:00.000Z"); // 5d ago
      expect(formatRelativeTime(date)).toBe("5d ago");
    });

    it("returns localized month and day when diff >= 30 days", () => {
      const date = new Date("2026-07-01T12:00:00.000Z"); // >30d ago
      const expected = date.toLocaleDateString("en-US", {
        month: "short",
        day: "numeric",
      });
      expect(formatRelativeTime(date)).toBe(expected);
      expect(formatRelativeTime(date.toISOString())).toBe(expected);
    });
  });
});
