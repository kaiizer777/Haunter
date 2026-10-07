"use client";

/**
 * useSessionStream
 *
 * Connects to POST /sessions/{id}/chat via fetch + ReadableStream so that
 * auth credentials (cookies) and the request body are both sent correctly.
 * The SSE protocol is parsed manually per the spec (event:/data:/blank-line).
 *
 * Exposed state:
 *   messages        - Chat history (user + assistant turns with thoughts/toolCalls)
 *   stagedPatches   - filename -> unified diff string for all staged files
 *   sandboxStatus   - Most recent sandbox verification result
 *   isStreaming     - True while an LLM response is in flight
 *   sendChatMessage - Submit a user message and start streaming
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { API_BASE, CheckpointOut, PlanTask, WaitingInput } from "@/lib/api";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ChatMessage {
  role: "user" | "assistant" | "system";
  content: string;
  thoughts?: string[];
  toolCalls?: ToolCallChip[];
  thoughtDurationSeconds?: number;
  model?: string;
  provider?: string;
  auditScan?: AuditCardState;
}

export interface ToolCallChip {
  name: string;
  args?: Record<string, unknown>;
}

export interface SandboxStatus {
  status: string;
  passed?: boolean;
  logs?: string;
  run_url?: string;
}

export interface AuditFinding {
  id: string;
  severity: "CRITICAL" | "HIGH" | "MEDIUM" | "LOW" | "BLOCKER" | "WARNING" | "NOTE" | string;
  category?: string;
  perspective?: string;
  file_path: string;
  line_start?: number;
  line_end?: number;
  line_number?: number;
  title?: string;
  description: string;
  suggested_fix?: string;
  remediation_diff?: string;
  can_auto_fix?: boolean;
}

export interface AuditScanStartEvent {
  scan_id?: string;
  target_label?: string;
  target_path?: string;
  perspectives?: string[];
  scan_type?: string;
  total_files_estimated?: number;
  timestamp?: number | string;
}

export interface AuditProgressEvent {
  scan_id?: string;
  completed_perspectives?: number;
  total_perspectives?: number;
  current_perspective?: string;
  status?: string;
  files_scanned?: number;
  total_files?: number;
  current_file?: string;
}

export interface AuditReportEvent {
  scan_id?: string;
  audit_id?: string;
  scan_type?: string;
  target_label?: string;
  executive_summary?: string;
  summary?: string;
  findings: AuditFinding[];
  confidence?: number;
  health_score?: number;
  remediation_diff?: string;
  publish_allowed?: boolean;
}

export interface AuditCardState {
  scan_id: string;
  target_label: string;
  perspectives?: string[];
  scan_type?: string;
  startedAt: number;
  status: "scanning" | "completed" | "failed";
  progress?: {
    completed: number;
    total: number;
    current?: string;
    statusText?: string;
  };
  report?: AuditReportEvent;
}

export interface SubagentStartEvent {
  role: string;
  task: string;
  startedAt: number;
}

export interface SubagentDoneEvent {
  role: string;
  summary: string;
  patchesModified: string[];
}

export interface SandboxQueuedEvent {
  run_url: string;
  workflow_name: string;
}

export interface SandboxProgressEvent {
  step_name: string;
  status: "in_progress" | "completed";
}

export interface SandboxResultEvent {
  passed: boolean;
  status?: string;
  exit_code?: number;
  summary?: string;
  run_url?: string;
  duration_s?: number;
  logs?: string;
}

export interface UseSessionStreamOptions {
  onSubagentStart?: (event: SubagentStartEvent) => void;
  onSubagentDone?: (event: SubagentDoneEvent) => void;
  onSandboxQueued?: (event: SandboxQueuedEvent) => void;
  onSandboxProgress?: (event: SandboxProgressEvent) => void;
  onSandboxResult?: (event: SandboxResultEvent) => void;
  onAuditScanStart?: (event: AuditScanStartEvent) => void;
  onAuditProgress?: (event: AuditProgressEvent) => void;
  onAuditReport?: (event: AuditReportEvent) => void;
}

// ---------------------------------------------------------------------------
// SSE frame type discriminated union (matches server event names)
// ---------------------------------------------------------------------------

type SseEventType =
  | "thought"
  | "file_diff"
  | "tool_call"
  | "sandbox_status"
  | "sandbox_queued"
  | "sandbox_progress"
  | "sandbox_start"
  | "sandbox_result"
  | "terminal_output"
  | "plan_update"
  | "clarification_requested"
  | "checkpoint_created"
  | "checkpoint_restored"
  | "subagent_start"
  | "subagent_done"
  | "audit_scan_start"
  | "audit_progress"
  | "audit_report"
  | "error"
  | "done";

interface SseFrame {
  event: SseEventType;
  data: unknown;
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export function useSessionStream(sessionId: string, options?: UseSessionStreamOptions) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [stagedPatches, setStagedPatches] = useState<Record<string, string>>({});
  const [sandboxStatus, setSandboxStatus] = useState<SandboxStatus | null>(null);
  const [sandboxQueued, setSandboxQueued] = useState<SandboxQueuedEvent | null>(null);
  const [sandboxProgress, setSandboxProgress] = useState<SandboxProgressEvent | null>(null);
  const [sandboxSteps, setSandboxSteps] = useState<SandboxProgressEvent[]>([]);
  const [sandboxResult, setSandboxResult] = useState<SandboxResultEvent | null>(null);
  const [ciStartedAt, setCiStartedAt] = useState<number | null>(null);
  const [ciFinishedAt, setCiFinishedAt] = useState<number | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const [terminalLogs, setTerminalLogs] = useState<string[]>([]);
  const [plan, setPlan] = useState<PlanTask[]>([]);
  const [pendingClarification, setPendingClarification] = useState<WaitingInput | null>(null);
  const [checkpoints, setCheckpoints] = useState<CheckpointOut[]>([]);
  const [auditScans, setAuditScans] = useState<AuditCardState[]>([]);
  const [activeAudit, setActiveAudit] = useState<AuditCardState | null>(null);

  // AbortController ref so we can cancel in-flight streams on unmount / new message.
  const abortRef = useRef<AbortController | null>(null);

  // Stable ref for optional subagent callbacks so sendChatMessage stays referentially stable.
  // Keep the ref-write inside an effect (not the render body) to satisfy react-hooks/refs.
  const optionsRef = useRef<UseSessionStreamOptions | undefined>(options);
  useEffect(() => {
    optionsRef.current = options;
  }, [options]);

  const sendChatMessage = useCallback(
    async (
      prompt: string,
      options?: { model?: string; provider?: string },
    ): Promise<void> => {
      if (isStreaming) return;

      // Abort any stale stream.
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;

      // Optimistically append the user message.
      setMessages((prev) => [...prev, { role: "user", content: prompt }]);
      setIsStreaming(true);

      // Track start time for thought duration calculations
      const streamStartTime = Date.now();
      let streamThoughtDuration = 0;

      // Working state for the current assistant turn being streamed.
      let thoughts: string[] = [];
      let toolCalls: ToolCallChip[] = [];
      let assistantContent = "";
      // Capture the requested model/provider for display on the response bubble.
      const requestedModel = options?.model ?? "";
      const requestedProvider = options?.provider ?? "";

      try {
        const url = `${API_BASE}/sessions/${sessionId}/chat`;
        const requestBody: Record<string, unknown> = { message: prompt };
        if (options?.model) requestBody.model = options.model;
        if (options?.provider) requestBody.provider = options.provider;

        const res = await fetch(url, {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(requestBody),
          signal: controller.signal,
        });

        if (!res.ok || !res.body) {
          const errText = await res.text().catch(() => "unknown error");
          throw new Error(`Chat endpoint error ${res.status}: ${errText}`);
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();

        // SSE parse state.
        let buffer = "";
        let currentEvent = "";
        let currentData = "";

        const flushFrame = () => {
          if (!currentData) {
            currentEvent = "";
            currentData = "";
            return;
          }
          let parsed: unknown;
          try {
            parsed = JSON.parse(currentData);
          } catch {
            parsed = currentData;
          }
          const frame: SseFrame = {
            event: (currentEvent || "message") as SseEventType,
            data: parsed,
          };
          handleFrame(frame);
          currentEvent = "";
          currentData = "";
        };

        // Incremental SSE frame handler.
        const handleFrame = (frame: SseFrame) => {
          const elapsedSec = Math.max(1, Math.round((Date.now() - streamStartTime) / 1000));
          switch (frame.event) {
            case "thought": {
              const thought =
                typeof frame.data === "string"
                  ? frame.data
                  : (frame.data as Record<string, string>)?.delta ??
                    (frame.data as Record<string, string>)?.content ?? "";
              thoughts = [...thoughts, thought];
              streamThoughtDuration = elapsedSec;
              // Update the in-progress assistant message.
              setMessages((prev) => {
                const copy = [...prev];
                const last = copy[copy.length - 1];
                if (last?.role === "assistant") {
                  copy[copy.length - 1] = {
                    ...last,
                    thoughts,
                    toolCalls,
                    content: assistantContent,
                    thoughtDurationSeconds: streamThoughtDuration,
                  };
                } else {
                  copy.push({
                    role: "assistant",
                    content: assistantContent,
                    thoughts,
                    toolCalls,
                    thoughtDurationSeconds: streamThoughtDuration,
                    model: requestedModel,
                    provider: requestedProvider,
                  });
                }
                return copy;
              });
              break;
            }
            case "tool_call": {
              const d = frame.data as Record<string, unknown>;
              const chip: ToolCallChip = {
                name: (d?.tool as string) ?? (d?.name as string) ?? "unknown_tool",
                args: (d?.args as Record<string, unknown>) ?? undefined,
              };
              toolCalls = [...toolCalls, chip];
              if (!streamThoughtDuration) streamThoughtDuration = elapsedSec;
              setMessages((prev) => {
                const copy = [...prev];
                const last = copy[copy.length - 1];
                if (last?.role === "assistant") {
                  copy[copy.length - 1] = {
                    ...last,
                    thoughts,
                    toolCalls,
                    content: assistantContent,
                    thoughtDurationSeconds: streamThoughtDuration,
                  };
                }
                return copy;
              });
              break;
            }
            case "file_diff": {
              const d = frame.data as Record<string, string>;
              const filePath = d?.file_path ?? d?.path ?? "";
              const diffText = d?.diff ?? d?.patch ?? "";
              if (filePath && diffText) {
                setStagedPatches((prev) => ({ ...prev, [filePath]: diffText }));
              }
              break;
            }
            case "sandbox_status": {
              const d = frame.data as Record<string, unknown>;
              const status = (d?.status as string) ?? "unknown";
              const passed =
                typeof d?.passed === "boolean"
                  ? (d.passed as boolean)
                  : status === "passed"
                    ? true
                    : status === "failed"
                      ? false
                      : undefined;
              // Preserve the CI run_url from the earlier sandbox_queued event
              // when this payload omits it (backend put_sandbox_status only
              // sends {status, logs}).
              const incomingRunUrl = (d?.run_url as string) ?? undefined;
              const incomingLogs = (d?.logs as string) ?? undefined;
              setSandboxStatus((prev) => ({
                status,
                passed,
                logs: incomingLogs ?? prev?.logs,
                run_url: incomingRunUrl ?? prev?.run_url,
              }));
              if (status === "passed" || status === "failed") {
                setCiFinishedAt((prev) => prev ?? Date.now());
                const result: SandboxResultEvent = {
                  passed: passed ?? status === "passed",
                  status,
                  run_url: (d?.run_url as string) ?? undefined,
                  logs: (d?.logs as string) ?? undefined,
                };
                setSandboxResult(result);
                optionsRef.current?.onSandboxResult?.(result);
              } else if (status === "queued" || status === "running") {
                setCiStartedAt((prev) => prev ?? Date.now());
              }
              break;
            }
            case "sandbox_queued": {
              const d = frame.data as Record<string, unknown>;
              const evt: SandboxQueuedEvent = {
                run_url: (d?.run_url as string) ?? "pending",
                workflow_name:
                  (d?.workflow_name as string) ??
                  (d?.workflow as string) ??
                  "auto",
              };
              setSandboxQueued(evt);
              setSandboxResult(null);
              // Fresh run — reset per-run step state so the previous run's
              // steps/progress never leak into the new run's chip.
              // terminalLogs stay append-only: they are a cross-run audit
              // buffer (capped at 1000 chunks), not per-run state.
              setSandboxProgress(null);
              setSandboxSteps([]);
              setCiStartedAt(Date.now());
              setCiFinishedAt(null);
              // Seed a queued status so the Live CI chip renders immediately,
              // even before the backend emits sandbox_status. A new queued
              // event always starts a fresh run (backend emits a trailing
              // queued with the final run_url just before sandbox_status,
              // which this correctly preserves via run_url).
              setSandboxStatus((prev) => ({
                status: "queued",
                passed: undefined,
                logs: prev?.logs,
                run_url: evt.run_url !== "pending" ? evt.run_url : prev?.run_url,
              }));
              optionsRef.current?.onSandboxQueued?.(evt);
              break;
            }
            case "sandbox_progress": {
              const d = frame.data as Record<string, unknown>;
              const rawStatus = (d?.status as string) ?? "in_progress";
              const evt: SandboxProgressEvent = {
                step_name: (d?.step_name as string) ?? "step",
                status: rawStatus === "completed" ? "completed" : "in_progress",
              };
              setSandboxProgress(evt);
              setSandboxSteps((prev) => [...prev, evt].slice(-20));
              // A step transition implies the run is live — promote queued to
              // running unless a terminal status already landed.
              setSandboxStatus((prev) => {
                if (!prev) return { status: "running" };
                if (prev.status === "passed" || prev.status === "failed") return prev;
                if (prev.status === "running") return prev;
                return { ...prev, status: "running" };
              });
              setCiStartedAt((prev) => prev ?? Date.now());
              optionsRef.current?.onSandboxProgress?.(evt);
              break;
            }
            case "sandbox_start": {
              // Legacy alias from the §4.2 architecture diagram
              // ({ run_url, branch, status }). Normalize to sandbox_queued.
              const d = frame.data as Record<string, unknown>;
              const evt: SandboxQueuedEvent = {
                run_url: (d?.run_url as string) ?? "pending",
                workflow_name:
                  (d?.workflow_name as string) ??
                  (d?.branch as string) ??
                  "auto",
              };
              setSandboxQueued(evt);
              setSandboxResult(null);
              // Fresh run — reset per-run step state so the previous run's
              // steps/progress never leak into the new run's chip.
              // terminalLogs stay append-only: they are a cross-run audit
              // buffer (capped at 1000 chunks), not per-run state.
              setSandboxProgress(null);
              setSandboxSteps([]);
              setCiStartedAt(Date.now());
              setCiFinishedAt(null);
              setSandboxStatus((prev) => ({
                status: "queued",
                passed: undefined,
                logs: prev?.logs,
                run_url: evt.run_url !== "pending" ? evt.run_url : prev?.run_url,
              }));
              optionsRef.current?.onSandboxQueued?.(evt);
              break;
            }
            case "sandbox_result": {
              // Terminal CI verdict ({ passed, exit_code, summary }).
              // Normalize into sandboxStatus so chip + badge share one source.
              const d = frame.data as Record<string, unknown>;
              const passed = Boolean(d?.passed);
              const runUrl =
                (d?.run_url as string) ??
                (d?.runUrl as string) ??
                undefined;
              const summary =
                (d?.summary as string) ??
                (d?.logs as string) ??
                undefined;
              const result: SandboxResultEvent = {
                passed,
                status: passed ? "passed" : "failed",
                exit_code:
                  typeof d?.exit_code === "number"
                    ? (d.exit_code as number)
                    : typeof d?.exitCode === "number"
                      ? (d.exitCode as number)
                      : undefined,
                summary,
                run_url: runUrl,
                duration_s:
                  typeof d?.duration_s === "number"
                    ? (d.duration_s as number)
                    : typeof d?.durationS === "number"
                      ? (d.durationS as number)
                      : undefined,
                logs: summary,
              };
              setSandboxResult(result);
              setCiFinishedAt(Date.now());
              setSandboxStatus((prev) => ({
                status: passed ? "passed" : "failed",
                passed,
                logs: summary ?? prev?.logs,
                run_url: runUrl ?? prev?.run_url,
              }));
              optionsRef.current?.onSandboxResult?.(result);
              break;
            }
            case "terminal_output": {
              const d = frame.data as Record<string, string>;
              const chunk = d?.chunk ?? (typeof frame.data === "string" ? frame.data : "");
              if (chunk) {
                // Raw GitHub Actions lines flow through this event alongside
                // local sandbox output — merge into one terminal buffer (capped
                // so a huge CI log cannot blow memory or shift layout).
                setTerminalLogs((prev) => [...prev, chunk].slice(-1000));
              }
              break;
            }
            case "plan_update": {
              const d = frame.data as Record<string, unknown>;
              const tasks = (d?.tasks as PlanTask[]) || [];
              setPlan(tasks);
              break;
            }
            case "clarification_requested": {
              const d = frame.data as Record<string, unknown>;
              const question = (d?.question as string) || "";
              const options = (d?.options as string[]) || [];
              setPendingClarification({ question, options });
              break;
            }
            case "checkpoint_created": {
              const cp = frame.data as CheckpointOut;
              if (cp?.checkpoint_id) {
                setCheckpoints((prev) => {
                  // Replace if same id exists, otherwise append. Cap at 20.
                  const filtered = prev.filter((c) => c.checkpoint_id !== cp.checkpoint_id);
                  return [...filtered, cp].slice(-20);
                });
              }
              break;
            }
            case "checkpoint_restored": {
              const d = frame.data as Record<string, unknown>;
              const restoredPatches = (d?.staged_patches as Record<string, string>) ?? {};
              // Immediately overwrite stagedPatches so Monaco editor buffers sync.
              setStagedPatches(restoredPatches);
              break;
            }
            case "subagent_start": {
              const { role, task } = frame.data as { role: string; task: string };
              optionsRef.current?.onSubagentStart?.({ role, task, startedAt: Date.now() });
              break;
            }
            case "subagent_done": {
              const { role, summary, patches_modified } = frame.data as {
                role: string;
                summary: string;
                patches_modified: string[];
              };
              optionsRef.current?.onSubagentDone?.({ role, summary, patchesModified: patches_modified ?? [] });
              break;
            }
            case "audit_scan_start": {
              const d = (frame.data as Record<string, unknown>) || {};
              const scanId = (d.scan_id as string) || `scan_${Date.now()}`;
              const targetLabel =
                (d.target_label as string) ||
                (d.target_path as string) ||
                (d.scan_type as string) ||
                "Repository";
              const rawPerspectives = Array.isArray(d.perspectives)
                ? (d.perspectives as string[])
                : d.scan_type
                  ? [d.scan_type as string]
                  : [];
              const rawTimestamp =
                typeof d.timestamp === "number"
                  ? d.timestamp
                  : typeof d.timestamp === "string"
                    ? Date.parse(d.timestamp) || Date.now()
                    : Date.now();
              const totalEst =
                rawPerspectives.length ||
                (typeof d.total_files_estimated === "number" ? d.total_files_estimated : 1);

              const scanState: AuditCardState = {
                scan_id: scanId,
                target_label: targetLabel,
                perspectives: rawPerspectives,
                scan_type: (d.scan_type as string) ?? undefined,
                startedAt: rawTimestamp,
                status: "scanning",
                progress: {
                  completed: 0,
                  total: Math.max(totalEst, 1),
                  statusText: "Initializing audit scan…",
                },
              };

              setActiveAudit(scanState);
              setAuditScans((prev) => {
                const idx = prev.findIndex((s) => s.scan_id === scanId);
                if (idx >= 0) {
                  const copy = [...prev];
                  copy[idx] = scanState;
                  return copy;
                }
                return [...prev, scanState];
              });

              setMessages((prev) => {
                const copy = [...prev];
                const last = copy[copy.length - 1];
                if (last && last.role === "assistant" && !last.auditScan) {
                  copy[copy.length - 1] = {
                    ...last,
                    auditScan: scanState,
                  };
                  return copy;
                } else if (last && last.auditScan && last.auditScan.scan_id === scanId) {
                  copy[copy.length - 1] = {
                    ...last,
                    auditScan: scanState,
                  };
                  return copy;
                }
                return [
                  ...copy,
                  {
                    role: "assistant",
                    content: "",
                    auditScan: scanState,
                  },
                ];
              });

              optionsRef.current?.onAuditScanStart?.({
                scan_id: scanId,
                target_label: targetLabel,
                target_path: (d.target_path as string) ?? undefined,
                perspectives: rawPerspectives,
                scan_type: (d.scan_type as string) ?? undefined,
                total_files_estimated:
                  typeof d.total_files_estimated === "number"
                    ? d.total_files_estimated
                    : undefined,
                timestamp: rawTimestamp,
              });
              break;
            }
            case "audit_progress": {
              const d = (frame.data as Record<string, unknown>) || {};
              const scanId = (d.scan_id as string) || "";
              const completed =
                typeof d.completed_perspectives === "number"
                  ? d.completed_perspectives
                  : typeof d.files_scanned === "number"
                    ? d.files_scanned
                    : 0;
              const total =
                typeof d.total_perspectives === "number"
                  ? d.total_perspectives
                  : typeof d.total_files === "number"
                    ? d.total_files
                    : 1;
              const current =
                (d.current_perspective as string) ||
                (d.current_file as string) ||
                "";
              const rawStatus = (d.status as string) || "";
              const statusText =
                rawStatus ||
                (current
                  ? `Analyzing ${current}…`
                  : `Scanning (${completed}/${Math.max(total, 1)})…`);

              const updateProgress = (scan: AuditCardState): AuditCardState => ({
                ...scan,
                status: "scanning",
                progress: {
                  completed,
                  total: Math.max(total, 1),
                  current,
                  statusText,
                },
              });

              setActiveAudit((prev) => (prev ? updateProgress(prev) : null));
              setAuditScans((prev) =>
                prev.map((s) =>
                  s.scan_id === scanId || (!scanId && s.status === "scanning")
                    ? updateProgress(s)
                    : s
                )
              );

              setMessages((prev) =>
                prev.map((msg) => {
                  if (
                    msg.auditScan &&
                    (msg.auditScan.scan_id === scanId ||
                      (!scanId && msg.auditScan.status === "scanning"))
                  ) {
                    return {
                      ...msg,
                      auditScan: updateProgress(msg.auditScan),
                    };
                  }
                  return msg;
                })
              );

              optionsRef.current?.onAuditProgress?.({
                scan_id: scanId || undefined,
                completed_perspectives:
                  typeof d.completed_perspectives === "number"
                    ? d.completed_perspectives
                    : undefined,
                total_perspectives:
                  typeof d.total_perspectives === "number"
                    ? d.total_perspectives
                    : undefined,
                current_perspective:
                  (d.current_perspective as string) ?? undefined,
                status: (d.status as string) ?? undefined,
                files_scanned:
                  typeof d.files_scanned === "number"
                    ? d.files_scanned
                    : undefined,
                total_files:
                  typeof d.total_files === "number" ? d.total_files : undefined,
                current_file: (d.current_file as string) ?? undefined,
              });
              break;
            }
            case "audit_report": {
              const d = (frame.data as Record<string, unknown>) || {};
              const scanId =
                (d.scan_id as string) ||
                (d.audit_id as string) ||
                `scan_${Date.now()}`;
              const findings = Array.isArray(d.findings)
                ? (d.findings as AuditFinding[])
                : [];
              const summary =
                (d.executive_summary as string) ||
                (d.summary as string) ||
                "Audit scan complete.";

              const reportEvent: AuditReportEvent = {
                scan_id: scanId,
                audit_id: (d.audit_id as string) ?? undefined,
                scan_type: (d.scan_type as string) ?? undefined,
                target_label: (d.target_label as string) ?? undefined,
                executive_summary: summary,
                summary,
                findings,
                confidence:
                  typeof d.confidence === "number" ? d.confidence : undefined,
                health_score:
                  typeof d.health_score === "number" ? d.health_score : undefined,
                remediation_diff: (d.remediation_diff as string) ?? undefined,
                publish_allowed:
                  typeof d.publish_allowed === "boolean"
                    ? d.publish_allowed
                    : undefined,
              };

              const completeScan = (scan: AuditCardState): AuditCardState => ({
                ...scan,
                status: "completed",
                report: reportEvent,
                progress: scan.progress
                  ? {
                      ...scan.progress,
                      completed: scan.progress.total,
                      statusText: "Audit complete",
                    }
                  : undefined,
              });

              setActiveAudit((prev) =>
                prev
                  ? completeScan(prev)
                  : {
                      scan_id: scanId,
                      target_label: reportEvent.target_label || "Repository Audit",
                      startedAt: Date.now(),
                      status: "completed",
                      report: reportEvent,
                    }
              );

              setAuditScans((prev) => {
                const exists = prev.some(
                  (s) => s.scan_id === scanId || s.status === "scanning"
                );
                if (!exists) {
                  return [
                    ...prev,
                    {
                      scan_id: scanId,
                      target_label: reportEvent.target_label || "Repository Audit",
                      startedAt: Date.now(),
                      status: "completed",
                      report: reportEvent,
                    },
                  ];
                }
                return prev.map((s) =>
                  s.scan_id === scanId || s.status === "scanning"
                    ? completeScan(s)
                    : s
                );
              });

              setMessages((prev) => {
                let found = false;
                const updated = prev.map((msg) => {
                  if (
                    msg.auditScan &&
                    (msg.auditScan.scan_id === scanId ||
                      msg.auditScan.status === "scanning")
                  ) {
                    found = true;
                    return {
                      ...msg,
                      auditScan: completeScan(msg.auditScan),
                    };
                  }
                  return msg;
                });
                if (!found) {
                  updated.push({
                    role: "assistant",
                    content: "",
                    auditScan: {
                      scan_id: scanId,
                      target_label: reportEvent.target_label || "Repository Audit",
                      startedAt: Date.now(),
                      status: "completed",
                      report: reportEvent,
                    },
                  });
                }
                return updated;
              });

              optionsRef.current?.onAuditReport?.(reportEvent);
              break;
            }
            case "error": {
              const d = frame.data as Record<string, unknown> | undefined;
              const errMsg =
                typeof frame.data === "string"
                  ? frame.data
                  : (d?.error as string) ?? (d?.message as string) ?? "Agent error";
              setMessages((prev) => [...prev, { role: "system", content: `Error: ${errMsg}` }]);
              break;
            }
            case "done": {
              // Finalise the assistant message with all accumulated content.
              // The backend emits the LLM response text via put_thought ({"delta": "..."})
              // and never emits a separate content event, so if assistantContent is empty
              // we promote the last non-heartbeat thought as the visible response, ONLY IF
              // it is not an intermediate tool execution thought on a multi-step turn.
              const HEARTBEAT = "Analyzing your request…";
              const nonHeartbeatThoughts = thoughts.filter((t) => t !== HEARTBEAT);

              let contentThought = "";
              if (!assistantContent && nonHeartbeatThoughts.length > 0) {
                const lastThought = nonHeartbeatThoughts.at(-1) ?? "";
                const isToolReasoning =
                  toolCalls.length > 0 &&
                  toolCalls.some(
                    (tc) =>
                      lastThought.toLowerCase().startsWith(`calling ${tc.name.toLowerCase()}`) ||
                      lastThought.toLowerCase().startsWith(`executing ${tc.name.toLowerCase()}`) ||
                      lastThought.toLowerCase().startsWith(`running ${tc.name.toLowerCase()}`) ||
                      lastThought.toLowerCase() === tc.name.toLowerCase()
                  );
                if (!isToolReasoning) {
                  contentThought = lastThought;
                }
              }

              const finalContent =
                typeof frame.data === "string"
                  ? frame.data
                  : assistantContent || contentThought;
              // Keep all thoughts in the accordion but strip the response from thoughts
              // so it doesn't display twice (once as content, once collapsed).
              const displayThoughts = thoughts.filter(
                (t) => t !== HEARTBEAT && (!contentThought || t !== contentThought)
              );
              // Use the actual model reported by the backend (reflects fallbacks).
              // Falls back to requestedModel for older deploys that don't emit model_used.
              const doneData = frame.data as Record<string, unknown>;
              const actualModel =
                (typeof doneData?.model_used === "string" && doneData.model_used)
                  ? doneData.model_used
                  : requestedModel;
              setMessages((prev) => {
                const copy = [...prev];
                const last = copy[copy.length - 1];
                if (last?.role === "assistant") {
                  copy[copy.length - 1] = {
                    ...last,
                    content: finalContent || last.content,
                    thoughts: displayThoughts.length ? displayThoughts : last.thoughts,
                    toolCalls,
                    thoughtDurationSeconds: streamThoughtDuration || last.thoughtDurationSeconds,
                    model: actualModel || last.model,
                    provider: requestedProvider || last.provider,
                  };
                } else {
                  copy.push({
                    role: "assistant",
                    content: finalContent,
                    thoughts: displayThoughts,
                    toolCalls,
                    thoughtDurationSeconds: streamThoughtDuration,
                    model: actualModel,
                    provider: requestedProvider,
                  });
                }
                return copy;
              });
              break;
            }


            default: {
              // Unknown event type — treat as assistant content token.
              if (typeof frame.data === "string") {
                assistantContent += frame.data;
              }
            }
          }
        };

        // Read the stream chunk by chunk.
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });

          // Process complete lines from buffer.
          let newlineIdx: number;
          while ((newlineIdx = buffer.indexOf("\n")) !== -1) {
            const line = buffer.slice(0, newlineIdx).replace(/\r$/, "");
            buffer = buffer.slice(newlineIdx + 1);

            if (line === "") {
              // Blank line = end of SSE frame.
              flushFrame();
            } else if (line.startsWith("event:")) {
              currentEvent = line.slice("event:".length).trim();
            } else if (line.startsWith("data:")) {
              const chunk = line.slice("data:".length).trimStart();
              currentData = currentData ? currentData + "\n" + chunk : chunk;
            }
            // id: and retry: fields are intentionally ignored.
          }
        }

        // Flush any remaining frame (stream ended without trailing blank line).
        flushFrame();
      } catch (err: unknown) {
        if ((err as { name?: string })?.name === "AbortError" || controller.signal.aborted) {
          // Intentional abort — cleanly settle streaming without error message
          return;
        }
        const msg = err instanceof Error ? err.message : "Streaming error";
        setMessages((prev) => [...prev, { role: "system", content: `Error: ${msg}` }]);
      } finally {
        setIsStreaming(false);
      }
    },
    [sessionId, isStreaming]
  );

  const stopStreaming = useCallback(() => {
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
    setIsStreaming(false);
  }, []);

  return {
    messages,
    stagedPatches,
    sandboxStatus,
    sandboxQueued,
    sandboxProgress,
    sandboxSteps,
    sandboxResult,
    ciStartedAt,
    ciFinishedAt,
    isStreaming,
    terminalLogs,
    plan,
    pendingClarification,
    checkpoints,
    auditScans,
    activeAudit,
    sendChatMessage,
    stopStreaming,
    setStagedPatches,
    setMessages,
    setTerminalLogs,
    setPlan,
    setPendingClarification,
    setCheckpoints,
    setAuditScans,
    setActiveAudit,
    setSandboxQueued,
    setSandboxProgress,
    setSandboxStatus,
    setSandboxResult,
  };
}
