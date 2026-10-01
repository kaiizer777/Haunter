import Link from "next/link";
import { GitBranch, CornerDownRight, History } from "lucide-react";
import { StatusBadge } from "@/components/runs/status-badge";
import { RunSummaryOut } from "@/lib/api";
import { formatRelativeTime } from "@/lib/utils";

export interface RunLineageProps {
  /** The run this view is about. */
  runId: string;
  /** Source run, when the current run descends from one the caller can see. */
  parent?: RunSummaryOut | null;
  /** Direct children (retries / refinements), oldest first. */
  childRuns?: RunSummaryOut[];
}

/**
 * Retry / PR-refinement thread for a run.
 *
 * Renders the upstream source run and every direct descendant so a user can
 * move between the original failure and each re-dispatched attempt without
 * hunting through the runs list. Renders nothing when there is no lineage, so
 * the majority (root) runs pay no layout cost.
 *
 * The descendant list is named `childRuns` rather than `children` so it cannot
 * be confused with React's reserved `children` prop (react/no-children-prop).
 */
export default function RunLineage({ runId, parent, childRuns }: RunLineageProps) {
  const descendants = childRuns ?? [];
  if (!parent && descendants.length === 0) {
    return null;
  }

  return (
    <section
      aria-label="Run retry lineage"
      className="relative overflow-hidden rounded-[8px] border-t border-t-zinc-700/60 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-gradient-to-b from-[#141418]/95 via-[#101013]/95 to-[#0a0a0d]/95 p-4 sm:p-5 space-y-3.5 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_4px_16px_rgba(0,0,0,0.35)]"
    >
      <span
        aria-hidden="true"
        className="pointer-events-none absolute inset-x-0 top-0 h-[1px] bg-gradient-to-r from-transparent via-white/10 to-transparent"
      />

      <h3 className="text-xs font-semibold uppercase tracking-wider text-zinc-200 font-mono flex items-center gap-2">
        <History className="h-3.5 w-3.5 text-amber-400" />
        <span>Retry Thread</span>
      </h3>

      {parent && (
        <div className="space-y-1.5">
          <p className="text-[11px] font-mono text-zinc-500">Retried from</p>
          <LineageRow
            run={parent}
            currentRunId={runId}
            icon={<CornerDownRight className="h-3.5 w-3.5 text-zinc-500 shrink-0" />}
          />
        </div>
      )}

      {descendants.length > 0 && (
        <div className="space-y-1.5">
          <p className="text-[11px] font-mono text-zinc-500">
            {descendants.length === 1
              ? "1 retry of this run"
              : `${descendants.length} retries of this run`}
          </p>
          <ul className="space-y-1.5">
            {descendants.map((child) => (
              <li key={child.id}>
                <LineageRow
                  run={child}
                  currentRunId={runId}
                  icon={<GitBranch className="h-3.5 w-3.5 text-amber-400/80 shrink-0" />}
                />
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function LineageRow({
  run,
  currentRunId,
  icon,
}: {
  run: RunSummaryOut;
  currentRunId: string;
  icon: React.ReactNode;
}) {
  const isCurrent = run.id === currentRunId;
  const body = (
    <>
      {icon}
      <StatusBadge status={run.status} />
      <span className="font-mono text-[11px] text-zinc-500 tabular-nums shrink-0">
        {formatRelativeTime(run.created_at)}
      </span>
      {run.pr_url && (
        <span className="font-mono text-[11px] text-emerald-400/90 shrink-0">
          PR #{run.pr_number ?? ""}
        </span>
      )}
    </>
  );

  if (isCurrent) {
    return (
      <span
        aria-current="true"
        className="flex items-center gap-2 flex-wrap rounded-[5px] border border-amber-500/30 bg-amber-500/10 px-2.5 py-1.5 text-xs font-mono text-amber-200"
      >
        {body}
        <span className="text-[10px] uppercase tracking-wider text-amber-300/80">
          this run
        </span>
      </span>
    );
  }

  return (
    <Link
      href={`/runs/detail?id=${run.id}`}
      className="flex items-center gap-2 flex-wrap rounded-[5px] border border-zinc-800/80 bg-zinc-900/60 px-2.5 py-1.5 text-xs font-mono text-zinc-300 hover:border-amber-500/40 hover:bg-zinc-800/70 hover:text-zinc-100 transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-amber-400"
    >
      {body}
    </Link>
  );
}