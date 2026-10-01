import { describe, it, expect } from "vitest";
import {
  UNKNOWN_SIGNATURE,
  getRunSignature,
  groupRunsBySignature,
} from "./failure-signature";

describe("failure-signature.ts", () => {
  describe("getRunSignature", () => {
    it("returns the server-computed signature verbatim", () => {
      expect(
        getRunSignature({
          id: "r1",
          signature: "sandbox timeout at <ts> after <n>s",
        })
      ).toBe("sandbox timeout at <ts> after <n>s");
    });

    it("buckets a run with no signature as unknown", () => {
      expect(getRunSignature({ id: "r1" })).toBe(UNKNOWN_SIGNATURE);
      expect(getRunSignature({ id: "r1", signature: "" })).toBe(UNKNOWN_SIGNATURE);
      expect(getRunSignature({ id: "r1", signature: null })).toBe(UNKNOWN_SIGNATURE);
    });

    it("never recomputes the signature from raw failure text", () => {
      // The server owns normalization; a client-side copy is what let the same
      // defect exist in two languages. `failure_reason` is not on RunOut.
      const run = { id: "r1", signature: "server-sig" } as {
        id: string;
        signature: string;
        failure_reason?: string;
      };
      run.failure_reason = "totally different raw reason";
      expect(getRunSignature(run)).toBe("server-sig");
    });
  });

  describe("groupRunsBySignature", () => {
    it("groups runs sharing a signature and tracks count + sample", () => {
      const runs = [
        { id: "r1", signature: "sig-a", signature_count: 2, sample_run_id: "r1" },
        { id: "r2", signature: "sig-a", signature_count: 2, sample_run_id: "r1" },
        { id: "r3", signature: "sig-b", signature_count: 1, sample_run_id: "r3" },
      ];
      const groups = groupRunsBySignature(runs);
      expect(groups).toHaveLength(2);
      expect(groups[0]?.signature).toBe("sig-a");
      expect(groups[0]?.count).toBe(2);
      expect(groups[0]?.sampleRunId).toBe("r1");
      expect(groups[0]?.runs.map((r) => r.id)).toEqual(["r1", "r2"]);
      expect(groups[1]?.signature).toBe("sig-b");
      expect(groups[1]?.count).toBe(1);
    });

    it("sorts by count desc then signature asc", () => {
      const groups = groupRunsBySignature([
        { id: "r1", signature: "sig-b" },
        { id: "r2", signature: "sig-a" },
        { id: "r3", signature: "sig-a" },
      ]);
      expect(groups.map((g) => g.signature)).toEqual(["sig-a", "sig-b"]);
      expect(groups[0]?.runs.map((r) => r.id)).toEqual(["r2", "r3"]);
    });

    it("falls back to local grouping when server counts are missing", () => {
      const groups = groupRunsBySignature([
        { id: "r1", signature: "sig-a" },
        { id: "r2", signature: "sig-a" },
        { id: "r3", signature: "sig-b" },
      ]);
      expect(groups[0]?.count).toBe(2);
      expect(groups[0]?.sampleRunId).toBe("r1");
    });

    it("ignores a server count the members disagree about", () => {
      const groups = groupRunsBySignature([
        { id: "r1", signature: "sig-a", signature_count: 7 },
        { id: "r2", signature: "sig-a", signature_count: 1 },
      ]);
      expect(groups[0]?.count).toBe(2);
    });

    it("groups runs without a signature under unknown", () => {
      const groups = groupRunsBySignature([{ id: "r1" }, { id: "r2" }]);
      expect(groups).toHaveLength(1);
      expect(groups[0]?.signature).toBe(UNKNOWN_SIGNATURE);
      expect(groups[0]?.sampleRunId).toBe("r1");
    });

    it("returns empty groups for empty input", () => {
      expect(groupRunsBySignature([])).toEqual([]);
    });
  });
});