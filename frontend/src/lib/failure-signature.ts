/**
 * Failure-signature clustering helpers for the runs dashboard.
 *
 * Mirrors backend `app/failure_signature.py`: normalize a raw failure_reason
 * by replacing volatile tokens (UUIDs, timestamps, SHAs, paths, IPs, bare
 * numbers) with placeholders, so repeated failures with the same root cause
 * share one signature. Used as a client-side fallback when the API omits
 * `signature` (e.g. a cached payload fetched before the field existed); the
 * server-computed `signature` is authoritative whenever present.
 *
 * This file and `backend/app/failure_signature.py` are two implementations of
 * one algorithm and MUST stay behaviourally identical, so both are pinned to
 * the golden corpus in `shared/failure_signature_vectors.json` — a divergence
 * in either suite is a failing test, not a silent production bug.
 */

export const UNKNOWN_SIGNATURE = "unknown";
export const MAX_SIGNATURE_CHARS = 200;

// Normalization precedence: most-anchored pattern first, so no rule can consume
// a substring a later, broader rule needed. All patterns are case-insensitive so
// they stay correct regardless of where lowercasing happens; the normalized
// output is lowercased regardless so that `ValueError` and `valueerror` cluster
// together.
// 1. UUID  2. ISO-8601 date-time  3. IPv4[:port]  4. Windows abs path
// 5. POSIX abs path  6. file.ext:line[:col]  7. ISO date  8. clock time
// 9. bare integers  10. 7-40 hex-char run (short..full git SHA)
const UUID_RE =
  /\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/gi;
const ISO_TS_RE =
  /\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?/gi;
const IPV4_RE = /\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b/g;
const WIN_PATH_RE = /[A-Za-z]:\\(?:[^\\\s]+\\)*[^\\\s]*/g;
// The lookbehind keeps the opening slash from being matched mid-token, which
// stops `a//b/c` and `./b/c` from being shredded into a bogus <path>.
const UNIX_ABS_PATH_RE = /(?<![\w./])\/(?:[\w.-]+\/)+[\w.-]+/g;
const FILE_LINE_RE = /(?<![\w/])(?:[\w.-]+\/)*[\w.-]+\.\w+:\d+(?::\d+)?/g;
const DATE_RE = /\b\d{4}-\d{2}-\d{2}\b/g;
const TIME_RE = /\b\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?\b/g;
const NUMBER_RE = /\b\d+\b/g;
const HEX_RUN_RE = /\b[0-9a-f]{7,40}\b/gi;
const WS_RE = /\s+/g;
const EDGE_PUNCTUATION_RE = /^[-–—:;,.|\s]+|[-–—:;,.|\s]+$/g;
// Same break set as Python's str.splitlines(), which the backend uses.
const LINE_BREAK_RE = /\r\n|[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]/g;

export function normalizeFailureSignature(
  reason: string | null | undefined
): string {
  if (!reason || !reason.trim()) return UNKNOWN_SIGNATURE;
  let firstLine = "";
  for (const line of reason.split(LINE_BREAK_RE)) {
    const stripped = line.trim();
    if (stripped) {
      firstLine = stripped;
      break;
    }
  }
  if (!firstLine) return UNKNOWN_SIGNATURE;
  let text = firstLine;
  text = text
    .replace(UUID_RE, "<id>")
    .replace(ISO_TS_RE, "<ts>")
    .replace(IPV4_RE, "<ip>")
    .replace(WIN_PATH_RE, "<path>")
    .replace(UNIX_ABS_PATH_RE, "<path>")
    .replace(FILE_LINE_RE, "<path>")
    .replace(DATE_RE, "<ts>")
    .replace(TIME_RE, "<ts>")
    .replace(NUMBER_RE, "<n>")
    .replace(HEX_RUN_RE, "<sha>");
  text = text.toLowerCase().replace(WS_RE, " ");
  text = text.trim().replace(EDGE_PUNCTUATION_RE, "");
  if (!text) return UNKNOWN_SIGNATURE;
  if (text.length > MAX_SIGNATURE_CHARS) {
    // Count code points, not UTF-16 units, so a non-BMP character on the
    // boundary is kept whole (matches Python's str slicing).
    text = Array.from(text)
      .slice(0, MAX_SIGNATURE_CHARS)
      .join("")
      .trimEnd();
  }
  return text;
}

export interface SignaturableRun {
  id: string;
  signature?: string | null;
  failure_reason?: string | null;
  sample_run_id?: string | null;
  signature_count?: number | null;
}

export interface SignatureGroup<T extends SignaturableRun> {
  signature: string;
  count: number;
  sampleRunId: string;
  runs: T[];
}

export function getRunSignature<T extends SignaturableRun>(run: T): string {
  if (run.signature) return run.signature;
  return normalizeFailureSignature(run.failure_reason ?? null);
}

/**
 * Group runs by failure signature, sorted by count desc then signature asc.
 * `count` prefers the server-computed `signature_count` when all members
 * agree, otherwise falls back to the local group size. `sampleRunId` prefers
 * the server's `sample_run_id`, falling back to the first member id.
 */
export function groupRunsBySignature<T extends SignaturableRun>(
  runs: T[]
): SignatureGroup<T>[] {
  const bySig = new Map<string, T[]>();
  for (const run of runs) {
    const sig = getRunSignature(run);
    const bucket = bySig.get(sig);
    if (bucket) bucket.push(run);
    else bySig.set(sig, [run]);
  }
  const groups: SignatureGroup<T>[] = [];
  for (const [signature, members] of bySig) {
    const serverCounts = members.map((m) =>
      typeof m.signature_count === "number" ? m.signature_count : null
    );
    const count =
      serverCounts.every((c) => c !== null && c === serverCounts[0])
        ? (serverCounts[0] as number)
        : members.length;
    const sampleRunId = members[0]?.sample_run_id || members[0]?.id || "";
    groups.push({ signature, count, sampleRunId, runs: members });
  }
  groups.sort((a, b) => b.count - a.count || a.signature.localeCompare(b.signature));
  return groups;
}
