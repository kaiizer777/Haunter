/**
 * Failure-signature grouping for the runs dashboard.
 *
 * The server owns signature computation (`app/failure_signature.py`) and always
 * returns `signature` on `RunOut`. This module only groups and orders runs by
 * that value.
 *
 * It deliberately does NOT re-implement normalization. An earlier revision had
 * a parallel TypeScript copy of the regex pipeline, which is how the same
 * ISO-8601 timestamp defect ended up existing in two languages at once - and a
 * client-computed signature that disagrees with the server silently breaks
 * grouping. One implementation cannot drift from itself.
 */

/** Fallback bucket for a run whose `signature` the API did not return. */
export const UNKNOWN_SIGNATURE = "unknown";

export interface SignaturableRun {
  id: string;
  signature?: string | null;
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
  return run.signature || UNKNOWN_SIGNATURE;
}

/**
 * Group runs by failure signature, sorted by count desc then signature asc.
 *
 * `count` prefers the server-computed `signature_count`, which counts every
 * run in the filtered set and so can only be >= the number of runs held on this
 * page. A smaller value means the server skipped clustering (the filtered set
 * exceeded its cap), and the page-local size is then the honest number - using
 * the sentinel directly would render every group as "x1" and sort them all
 * alike. `sampleRunId` prefers the server's `sample_run_id`, falling back to
 * the first member id.
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
    const agreed =
      serverCounts.every((c) => c !== null && c === serverCounts[0])
        ? (serverCounts[0] as number)
        : null;
    const count =
      agreed !== null && agreed >= members.length ? agreed : members.length;
    const sampleRunId = members[0]?.sample_run_id || members[0]?.id || "";
    groups.push({ signature, count, sampleRunId, runs: members });
  }
  groups.sort((a, b) => b.count - a.count || a.signature.localeCompare(b.signature));
  return groups;
}