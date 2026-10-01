/**
 * Failure-signature clustering helpers for the runs dashboard.
 *
 * Mirrors backend `app/failure_signature.py`: normalize a raw failure_reason
 * by stripping volatile tokens (commit SHAs, file paths, timestamps, IPs,
 * bare numbers) so repeated failures with the same root cause share one
 * signature. Used as a client-side fallback when the API omits `signature`
 * (e.g. stale cached payloads); the server-computed `signature` field is
 * authoritative whenever present.
 */

export const UNKNOWN_SIGNATURE = "unknown";
export const MAX_SIGNATURE_CHARS = 200;

const UUID_RE =
  /\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/gi;
const SHA40_RE = /\b[0-9a-f]{40}\b/gi;
const SHA_SHORT_RE = /\b[0-9a-f]{7,39}\b/gi;
const ISO_TS_RE =
  /\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?/g;
const DATE_RE = /\b\d{4}-\d{2}-\d{2}\b/g;
const TIME_RE = /\b\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?\b/g;
const IPV4_RE = /\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b/g;
const WIN_PATH_RE = /[A-Za-z]:\\(?:[^\\\s]+\\)*[^\\\s]*/g;
const UNIX_ABS_PATH_RE = /(?<![\w./])\/(?:[\w.-]+\/)+[\w.-]+/g;
const FILE_LINE_RE = /(?<![\w/])(?:[\w.-]+\/)*[\w.-]+\.\w+:\d+(?::\d+)?/g;
const NUMBER_RE = /\b\d+\b/g;
const WS_RE = /\s+/g;

export function normalizeFailureSignature(
  reason: string | null | undefined
): string {
  if (!reason || !reason.trim()) return UNKNOWN_SIGNATURE;
  let firstLine = "";
  for (const line of reason.split("\n")) {
    const stripped = line.trim();
    if (stripped) {
      firstLine = stripped;
      break;
    }
  }
  if (!firstLine) return UNKNOWN_SIGNATURE;
  let text = firstLine.toLowerCase();
  text = text
    .replace(UUID_RE, "<id>")
    .replace(SHA40_RE, "<sha>")
    .replace(SHA_SHORT_RE, "<sha>")
    .replace(ISO_TS_RE, "<ts>")
    .replace(DATE_RE, "<ts>")
    .replace(TIME_RE, "<ts>")
    .replace(IPV4_RE, "<ip>")
    .replace(WIN_PATH_RE, "<path>")
    .replace(UNIX_ABS_PATH_RE, "<path>")
    .replace(FILE_LINE_RE, "<path>")
    .replace(NUMBER_RE, "<n>")
    .replace(WS_RE, " ")
    .trim()
    .replace(/^[-–—:;,.|\s]+|[-–—:;,.|\s]+$/g, "");
  if (!text) return UNKNOWN_SIGNATURE;
  if (text.length > MAX_SIGNATURE_CHARS) text = text.slice(0, MAX_SIGNATURE_CHARS).trimEnd();
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
    const sampleRunId =
      members[0]?.sample_run_id || members[0]?.id || "";
    groups.push({ signature, count, sampleRunId, runs: members });
  }
  groups.sort((a, b) => b.count - a.count || a.signature.localeCompare(b.signature));
  return groups;
}
