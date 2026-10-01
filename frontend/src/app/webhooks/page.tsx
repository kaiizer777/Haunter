"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { AppLayout } from "@/components/layout/app-layout";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { DeliveryStatusBadge } from "@/components/webhooks/delivery-status-badge";
import { api, WebhookDeliveryOut, WebhookReplayResultOut } from "@/lib/api";
import { formatRelativeTime, cn } from "@/lib/utils";
import {
  AlertCircle,
  Clock,
  Inbox,
  Radio,
  RefreshCw,
  RotateCcw,
  Webhook,
} from "lucide-react";

const PAGE_SIZE = 25;

/** Event types the ingestion handler records a health row for. */
const EVENT_FILTERS = [
  { value: "", label: "All events" },
  { value: "workflow_run", label: "workflow_run" },
  { value: "pull_request", label: "pull_request" },
  { value: "push", label: "push" },
] as const;

/**
 * Turn the handler's decision body into one line the operator can act on.
 * `status` is always the branch taken; `reason` explains a non-queue branch.
 */
function describeDecision(result: WebhookReplayResultOut): string {
  const decision = result.decision ?? {};
  const status =
    typeof decision.status === "string" ? decision.status : "unknown";
  const reason = typeof decision.reason === "string" ? decision.reason : null;
  const runId =
    typeof decision.run_id === "string" ? ` (run ${decision.run_id})` : "";
  return reason ? `${status} — ${reason}` : `${status}${runId}`;
}

export default function WebhooksPage() {
  const [deliveries, setDeliveries] = useState<WebhookDeliveryOut[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [eventFilter, setEventFilter] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [replayingId, setReplayingId] = useState<string | null>(null);
  const [replayNotice, setReplayNotice] = useState<{
    tone: "success" | "error";
    message: string;
  } | null>(null);

  const fetchDeliveries = useCallback(
    async (isManualRefresh = false) => {
      if (isManualRefresh) {
        setIsRefreshing(true);
      } else {
        setLoading(true);
      }
      setError(null);
      try {
        const data = await api.getWebhookDeliveries({
          event: eventFilter || undefined,
          limit: PAGE_SIZE,
          offset,
        });
        setDeliveries(data.deliveries);
        setTotal(data.total);
      } catch (err: unknown) {
        setError(
          err instanceof Error ? err.message : "Failed to load webhook deliveries."
        );
      } finally {
        setLoading(false);
        setIsRefreshing(false);
      }
    },
    [eventFilter, offset]
  );

  useEffect(() => {
    fetchDeliveries();
  }, [fetchDeliveries]);

  const handleReplay = async (delivery: WebhookDeliveryOut) => {
    setReplayingId(delivery.id);
    setReplayNotice(null);
    try {
      const result = await api.replayWebhookDelivery(delivery.id);
      setReplayNotice({
        tone: "success",
        message: `Replay of ${delivery.delivery_id} → ${describeDecision(result)}`,
      });
      // The re-run appends its own health row, so reload the visible page.
      await fetchDeliveries(true);
    } catch (err: unknown) {
      setReplayNotice({
        tone: "error",
        message:
          err instanceof Error
            ? `Replay failed: ${err.message}`
            : "Replay failed.",
      });
    } finally {
      setReplayingId(null);
    }
  };

  const activeFilterLabel = useMemo(
    () => EVENT_FILTERS.find((f) => f.value === eventFilter)?.label,
    [eventFilter]
  );

  const hasPrev = offset > 0;
  const hasNext = offset + PAGE_SIZE < total;
  // A failed fetch must not also claim "no deliveries recorded": `deliveries`
  // is still empty at that point, so the empty state is only honest once the
  // request actually succeeded.
  const showEmpty = !loading && !error && deliveries.length === 0;

  return (
    <AppLayout
      title="Webhook Health"
      subtitle="Every GitHub delivery Haunter decided on — and replay any of them"
      actions={
        <Button
          variant="outline"
          size="sm"
          onClick={() => fetchDeliveries(true)}
          disabled={loading || isRefreshing}
          className="group h-8 px-3 text-xs font-mono rounded-[6px] text-zinc-300 hover:text-white bg-gradient-to-b from-zinc-800/90 via-zinc-850 to-zinc-900/90 border-t border-t-zinc-600/70 border-x border-x-zinc-700/60 border-b border-b-zinc-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.12),0_1px_3px_rgba(0,0,0,0.35),0_1px_2px_rgba(0,0,0,0.2)] hover:border-t-zinc-500 active:translate-y-[0.5px] transition-all cursor-pointer"
          title="Refresh delivery history"
        >
          <RefreshCw
            className={cn(
              "h-3.5 w-3.5 transition-colors",
              isRefreshing ? "animate-spin text-amber-400" : "text-zinc-400 group-hover:text-amber-400"
            )}
          />
          <span className="hidden sm:inline">Refresh</span>
        </Button>
      }
    >
      <div className="space-y-4 min-w-0 pb-6">
        {/* Error state */}
        {error && (
          <div
            role="alert"
            className="flex items-start gap-3 rounded-[7px] border border-red-900/60 bg-red-950/30 p-3.5 text-[13px] text-red-300 shadow-md"
          >
            <AlertCircle className="h-4.5 w-4.5 shrink-0 text-red-400 mt-0.5" />
            <div className="space-y-0.5">
              <p className="font-semibold text-red-200">Webhook Health Error</p>
              <p className="text-red-300/90 text-xs font-mono">{error}</p>
            </div>
          </div>
        )}

        {/* Replay outcome */}
        {replayNotice && (
          <div
            role="status"
            className={cn(
              "flex items-start gap-3 rounded-[7px] border p-3.5 text-[13px] shadow-md",
              replayNotice.tone === "success"
                ? "border-emerald-900/60 bg-emerald-950/30 text-emerald-300"
                : "border-amber-900/60 bg-amber-950/30 text-amber-300"
            )}
          >
            {replayNotice.tone === "success" ? (
              <RotateCcw className="h-4.5 w-4.5 shrink-0 text-emerald-400 mt-0.5" />
            ) : (
              <AlertCircle className="h-4.5 w-4.5 shrink-0 text-amber-400 mt-0.5" />
            )}
            <p className="text-xs font-mono break-words">{replayNotice.message}</p>
          </div>
        )}

        {/* Summary + event filter */}
        <div className="relative z-20 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-zinc-800/80 bg-[#0d0d10]/90 backdrop-blur-sm shadow-[inset_0_1px_0_0_rgba(255,255,255,0.03)] p-3">
          <div className="flex items-center gap-2.5">
            <Radio className="h-4 w-4 text-amber-400" />
            <div className="flex flex-col">
              <span className="text-[11px] font-mono uppercase tracking-wider text-zinc-400">
                Recorded deliveries
              </span>
              <span className="text-base font-bold font-mono text-zinc-100 tabular-nums">
                {total}
              </span>
            </div>
          </div>

          <div
            role="group"
            aria-label="Filter deliveries by event"
            className="flex items-center gap-1.5 p-1 rounded-lg border border-zinc-800/80 bg-[#0d0d10]/90"
          >
            {EVENT_FILTERS.map((filter) => {
              const isActive = eventFilter === filter.value;
              return (
                <button
                  key={filter.value || "all"}
                  type="button"
                  aria-pressed={isActive}
                  onClick={() => {
                    setReplayNotice(null);
                    setOffset(0);
                    setEventFilter(filter.value);
                  }}
                  className={cn(
                    "px-2.5 py-1.5 rounded-[5px] text-[11px] font-mono transition-all cursor-pointer",
                    isActive
                      ? "bg-zinc-800 text-zinc-100 font-semibold border border-zinc-700/60 shadow-sm"
                      : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/40 border border-transparent"
                  )}
                >
                  {filter.label}
                </button>
              );
            })}
          </div>
        </div>

        {/* Delivery table */}
        <div className="relative z-0 overflow-hidden rounded-xl border-t border-t-zinc-600/60 border-x border-x-zinc-800/80 border-b border-b-zinc-950 bg-gradient-to-b from-[#111115]/95 via-[#0d0d10]/95 to-[#09090c]/95 backdrop-blur-sm shadow-[inset_0_1px_0_rgba(255,255,255,0.12),inset_0_-1px_0_rgba(0,0,0,0.4),0_8px_32px_rgba(0,0,0,0.5)]">
          <span
            aria-hidden="true"
            className="pointer-events-none absolute inset-x-0 top-0 h-6 bg-gradient-to-b from-white/[0.04] to-transparent z-10"
          />

          {loading ? (
            <div className="p-4 space-y-3">
              <Skeleton className="h-10 w-full bg-zinc-900/60" />
              <Skeleton className="h-10 w-full bg-zinc-900/60" />
              <Skeleton className="h-10 w-full bg-zinc-900/60" />
            </div>
          ) : showEmpty ? (
            <div className="py-8 sm:py-10 px-6 sm:px-8 text-center border border-dashed border-zinc-800/80 m-3 sm:m-4 rounded-xl bg-[#09090b]/40">
              <div className="h-11 w-11 rounded-xl bg-amber-400/10 border border-amber-500/30 text-amber-400 flex items-center justify-center mx-auto mb-3 shadow-[0_0_16px_rgba(245,158,11,0.12)]">
                <Inbox className="h-5.5 w-5.5" />
              </div>
              <h3 className="text-base font-bold text-zinc-100 font-mono tracking-tight">
                No webhook deliveries recorded
              </h3>
              <p className="text-xs sm:text-sm text-zinc-400 mt-1 max-w-md mx-auto leading-relaxed">
                {activeFilterLabel
                  ? `No ${activeFilterLabel} deliveries for your repositories yet.`
                  : "Haunter records a row here every time GitHub delivers a webhook to a connected repository."}
              </p>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <Table>
                <TableHeader className="border-b border-zinc-800/90 bg-gradient-to-b from-[#141418] to-[#0c0c10] shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_1px_3px_rgba(0,0,0,0.35)]">
                  <TableRow className="border-b border-zinc-800/90 hover:bg-transparent">
                    <TableHead className="w-[24%] pl-4 py-3 text-[11px] font-mono tracking-wider uppercase text-zinc-500 font-medium select-none">
                      Event
                    </TableHead>
                    <TableHead className="w-[20%] px-3 py-3 text-[11px] font-mono tracking-wider uppercase text-zinc-500 font-medium select-none">
                      Repository
                    </TableHead>
                    <TableHead className="w-[18%] px-3 py-3 text-[11px] font-mono tracking-wider uppercase text-zinc-500 font-medium select-none">
                      Decision
                    </TableHead>
                    <TableHead className="w-[24%] px-3 py-3 text-[11px] font-mono tracking-wider uppercase text-zinc-500 font-medium select-none">
                      Detail
                    </TableHead>
                    <TableHead className="w-[8%] px-3 py-3 text-[11px] font-mono tracking-wider uppercase text-zinc-500 font-medium select-none">
                      Received
                    </TableHead>
                    <TableHead className="w-[14%] pr-4 py-3 text-[11px] font-mono tracking-wider uppercase text-zinc-500 font-medium select-none text-right">
                      Actions
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody className="divide-y divide-zinc-800/40">
                  {deliveries.map((delivery) => (
                    <TableRow
                      key={delivery.id}
                      className="border-b border-zinc-800/40 hover:bg-zinc-800/20 transition-colors group"
                    >
                      <TableCell className="pl-4 py-3.5">
                        <div className="flex items-center gap-2 font-mono text-xs">
                          <Webhook className="h-3.5 w-3.5 text-amber-400/80 shrink-0" />
                          <span className="text-zinc-100 font-semibold">
                            {delivery.event}
                          </span>
                        </div>
                        <div
                          className="pl-5.5 mt-1 font-mono text-[10px] text-zinc-500 truncate"
                          title={delivery.delivery_id}
                        >
                          {delivery.delivery_id}
                        </div>
                      </TableCell>

                      <TableCell className="px-3 py-3.5 font-mono text-xs text-zinc-300">
                        {delivery.repo ?? "—"}
                      </TableCell>

                      <TableCell className="px-3 py-3.5">
                        <DeliveryStatusBadge status={delivery.status} />
                        {delivery.replay_of && (
                          <span className="mt-1 block font-mono text-[10px] text-purple-300/80">
                            replay
                          </span>
                        )}
                      </TableCell>

                      <TableCell className="px-3 py-3.5 font-mono text-[11px] text-zinc-400">
                        <span className="break-words">
                          {delivery.reason ?? "—"}
                        </span>
                      </TableCell>

                      <TableCell className="px-3 py-3.5 font-mono text-xs text-zinc-400 whitespace-nowrap">
                        <span className="inline-flex items-center gap-1.5">
                          <Clock className="h-3 w-3 text-zinc-500 shrink-0" />
                          <span title={delivery.created_at}>
                            {formatRelativeTime(delivery.created_at)}
                          </span>
                        </span>
                      </TableCell>

                      <TableCell className="pr-4 py-3.5 text-right">
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => handleReplay(delivery)}
                          disabled={
                            replayingId !== null || !delivery.replayable
                          }
                          className="h-7 px-2.5 text-[11px] font-mono rounded-[5px] cursor-pointer disabled:cursor-not-allowed"
                          // Without a per-row name every button on the page
                          // reads as "Replay" and a screen-reader user cannot
                          // tell which delivery they are about to re-drive. The
                          // unretained case also announces WHY it is disabled,
                          // since a disabled control is not focusable.
                          aria-label={
                            delivery.replayable
                              ? `Replay delivery ${delivery.delivery_id}`
                              : `Delivery ${delivery.delivery_id} cannot be replayed: payload was not retained`
                          }
                          title={
                            delivery.replayable
                              ? "Re-run this delivery through the live ingestion handler"
                              : "This delivery's payload was too large to retain, so it cannot be replayed"
                          }
                        >
                          {replayingId === delivery.id ? (
                            <RefreshCw className="h-3 w-3 animate-spin" />
                          ) : (
                            <RotateCcw className="h-3 w-3" />
                          )}
                          Replay
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </div>

        {/* Pagination */}
        {!loading && total > PAGE_SIZE && (
          <div className="flex items-center justify-between gap-3 text-[11px] font-mono text-zinc-500">
            <span className="tabular-nums">
              Showing {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}
            </span>
            <div className="flex items-center gap-2">
              <Button
                variant="ghost"
                size="sm"
                disabled={!hasPrev}
                onClick={() => setOffset((prev) => Math.max(0, prev - PAGE_SIZE))}
                className="h-6 px-2 text-[11px] font-mono rounded-[4px] text-zinc-400 hover:text-zinc-200 bg-zinc-800/40 hover:bg-zinc-800/80 border border-zinc-800 hover:border-zinc-700 transition-all cursor-pointer disabled:cursor-not-allowed"
              >
                Previous
              </Button>
              <Button
                variant="ghost"
                size="sm"
                disabled={!hasNext}
                onClick={() => setOffset((prev) => prev + PAGE_SIZE)}
                className="h-6 px-2 text-[11px] font-mono rounded-[4px] text-zinc-400 hover:text-zinc-200 bg-zinc-800/40 hover:bg-zinc-800/80 border border-zinc-800 hover:border-zinc-700 transition-all cursor-pointer disabled:cursor-not-allowed"
              >
                Next
              </Button>
            </div>
          </div>
        )}
      </div>
    </AppLayout>
  );
}