import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { describe, it, expect } from "vitest";
import {
  MAX_SIGNATURE_CHARS,
  normalizeFailureSignature,
  groupRunsBySignature,
  getRunSignature,
} from "./failure-signature";

interface Vector {
  id: string;
  why: string;
  input: string;
  expected: string;
}

interface Corpus {
  vectors: Vector[];
  mustDiffer: [string, string][];
}

const CORPUS_RELATIVE = path.join(
  "shared",
  "failure_signature_vectors.json"
);

/** Walks up from the runner cwd so the suite works from repo root or frontend/. */
function resolveCorpusPath(): string {
  let dir = process.cwd();
  for (;;) {
    const candidate = path.join(dir, CORPUS_RELATIVE);
    if (existsSync(candidate)) return candidate;
    const parent = path.dirname(dir);
    if (parent === dir) {
      throw new Error(`${CORPUS_RELATIVE} not found above ${process.cwd()}`);
    }
    dir = parent;
  }
}

// Shared verbatim with backend/tests/test_failure_signature.py, so the two
// implementations of the normalizer cannot drift apart.
const CORPUS = JSON.parse(
  readFileSync(resolveCorpusPath(), "utf8")
) as Corpus;

describe("failure-signature.ts", () => {
  describe("normalizeFailureSignature", () => {
    it("maps empty input to unknown", () => {
      expect(normalizeFailureSignature(null)).toBe("unknown");
      expect(normalizeFailureSignature(undefined)).toBe("unknown");
      expect(normalizeFailureSignature("")).toBe("unknown");
      expect(normalizeFailureSignature("   \n  ")).toBe("unknown");
    });

    it("strips commit SHAs so same error with different SHAs clusters", () => {
      const a = normalizeFailureSignature(
        "fix_generator: ValueError: bad ref abc123def456789012345678901234567890abcd in main"
      );
      const b = normalizeFailureSignature(
        "fix_generator: ValueError: bad ref ffffffffffffffffffffffffffffffffffffffff in main"
      );
      expect(a).toBe(b);
      expect(a).toContain("<sha>");
    });

    it("strips file paths and line numbers", () => {
      const a = normalizeFailureSignature(
        "verification: AssertionError in backend/app/orchestrator.py:422 failed"
      );
      const b = normalizeFailureSignature(
        "verification: AssertionError in backend/app/orchestrator.py:917 failed"
      );
      expect(a).toBe(b);
      expect(a).toContain("<path>");
    });

    it("strips timestamps", () => {
      const a = normalizeFailureSignature(
        "sandbox timeout at 2026-03-01T12:34:56Z after 120s"
      );
      const b = normalizeFailureSignature(
        "sandbox timeout at 2026-03-02T01:02:03Z after 120s"
      );
      expect(a).toBe(b);
      expect(a).toContain("<ts>");
    });

    it("keeps distinct errors distinct", () => {
      const a = normalizeFailureSignature("fix_generator: ValueError: bad patch");
      const b = normalizeFailureSignature("sandbox: TimeoutError: runner timed out");
      expect(a).not.toBe(b);
      expect(a).not.toBe("unknown");
      expect(b).not.toBe("unknown");
    });
  });

  describe("shared golden corpus", () => {
    it.each(CORPUS.vectors.map((v) => [v.id, v] as const))(
      "matches the backend for %s",
      (_id, vector) => {
        expect(normalizeFailureSignature(vector.input)).toBe(vector.expected);
      }
    );

    it("is idempotent over the whole corpus", () => {
      for (const vector of CORPUS.vectors) {
        const once = normalizeFailureSignature(vector.input);
        expect(normalizeFailureSignature(once)).toBe(once);
      }
    });

    it("keeps distinct failures distinct", () => {
      const byId = new Map(CORPUS.vectors.map((v) => [v.id, v]));
      for (const [a, b] of CORPUS.mustDiffer) {
        expect(byId.get(a)?.expected).not.toBe(byId.get(b)?.expected);
      }
    });

    it("keeps every signature within the length bound", () => {
      for (const vector of CORPUS.vectors) {
        // Counted in code points, matching the backend's str slicing.
        const codePoints = Array.from(vector.expected).length;
        expect(codePoints).toBeLessThanOrEqual(MAX_SIGNATURE_CHARS);
      }
    });

    it("truncates on a code-point boundary, not a UTF-16 boundary", () => {
      const raw = "x".repeat(MAX_SIGNATURE_CHARS - 1) + "\u{1F680} tail";
      const sig = normalizeFailureSignature(raw);
      // Array.from() means 200 code points, so the surrogate pair stays whole.
      expect(Array.from(sig)).toHaveLength(MAX_SIGNATURE_CHARS);
      expect(sig.endsWith("\u{1F680}")).toBe(true);
    });
  });

  describe("getRunSignature", () => {
    it("prefers the server-computed signature when present", () => {
      expect(
        getRunSignature({
          id: "r1",
          signature: "server-sig",
          failure_reason: "something else entirely",
        })
      ).toBe("server-sig");
    });

    it("falls back to normalizing failure_reason", () => {
      expect(
        getRunSignature({ id: "r1", failure_reason: "ValueError: bad" })
      ).toBe(normalizeFailureSignature("ValueError: bad"));
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

    it("falls back to local grouping when server fields are absent", () => {
      const runs = [
        {
          id: "r1",
          failure_reason:
            "ValueError: bad ref aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        },
        {
          id: "r2",
          failure_reason:
            "ValueError: bad ref bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        },
        { id: "r3", failure_reason: "TimeoutError: runner timed out" },
      ];
      const groups = groupRunsBySignature(runs);
      expect(groups).toHaveLength(2);
      expect(groups[0]?.runs).toHaveLength(2);
      expect(groups[0]?.sampleRunId).toBe("r1");
    });

    it("returns empty groups for empty input", () => {
      expect(groupRunsBySignature([])).toEqual([]);
    });
  });
});