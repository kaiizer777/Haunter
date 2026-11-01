import { describe, it, expect } from "vitest";
import {
  describeFailureForDisplay,
  sanitizeErrorForDisplay,
  isModelVocabulary,
} from "./failure-copy";

describe("failure-copy (non-admin feature language)", () => {
  it("passes raw text through for admins", () => {
    expect(
      describeFailureForDisplay("model gpt-4o allowlisted only", "cls", true)
    ).toBe("model gpt-4o allowlisted only");
    expect(sanitizeErrorForDisplay("provider groq denied", true)).toBe(
      "provider groq denied"
    );
  });

  it("redacts model vocabulary to generic copy for non-admins", () => {
    const out = describeFailureForDisplay(
      "model nemotron-3.5 not in allowlist for provider groq",
      null,
      false
    );
    expect(out).not.toMatch(/nemotron|allowlist|groq|provider|model/i);
    expect(out).toMatch(/could not be completed/i);
  });

  it("keeps non-model failure text for non-admins", () => {
    expect(
      describeFailureForDisplay("sandbox verification timed out", null, false)
    ).toBe("sandbox verification timed out");
  });

  it("returns null when there is nothing to show", () => {
    expect(describeFailureForDisplay(null, null, false)).toBeNull();
  });

  it("does not treat ordinary -ling words as model vocabulary", () => {
    expect(isModelVocabulary("handling the request timed out")).toBe(false);
    expect(isModelVocabulary("polling for updates failed")).toBe(false);
    expect(
      describeFailureForDisplay("handling the request timed out", null, false)
    ).toBe("handling the request timed out");
  });

  it("sanitizes allowlist-shaped error text for non-admins", () => {
    expect(isModelVocabulary("allowed models: gpt-4o, claude")).toBe(true);
    expect(isModelVocabulary("sandbox verification timed out")).toBe(false);
    expect(
      sanitizeErrorForDisplay("allowed models: gpt-4o, claude", false)
    ).toMatch(/try again/i);
  });
});
