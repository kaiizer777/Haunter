import { describe, it, expect } from "vitest";
import { formatContextWindow } from "./page";

describe("formatContextWindow (frontend/src/app/config/page.tsx)", () => {
  describe("Binary multiples", () => {
    it("formats 1048576 as 1M Context", () => {
      expect(formatContextWindow(1048576)).toBe("1M Context");
    });

    it("formats 262144 as 256k Context", () => {
      expect(formatContextWindow(262144)).toBe("256k Context");
    });

    it("formats 131072 as 128k Context", () => {
      expect(formatContextWindow(131072)).toBe("128k Context");
    });

    it("formats 65536 as 64k Context", () => {
      expect(formatContextWindow(65536)).toBe("64k Context");
    });

    it("formats 8192 as 8k Context", () => {
      expect(formatContextWindow(8192)).toBe("8k Context");
    });
  });

  describe("Decimal multiples", () => {
    it("formats 1000000 as 1M Context", () => {
      expect(formatContextWindow(1000000)).toBe("1M Context");
    });

    it("formats 200000 as 200k Context", () => {
      expect(formatContextWindow(200000)).toBe("200k Context");
    });

    it("formats 128000 as 128k Context", () => {
      expect(formatContextWindow(128000)).toBe("128k Context");
    });
  });

  describe("Fallback and falsy values", () => {
    it("returns 128k Context for undefined", () => {
      expect(formatContextWindow(undefined)).toBe("128k Context");
    });

    it("returns 128k Context for 0", () => {
      expect(formatContextWindow(0)).toBe("128k Context");
    });
  });
});
