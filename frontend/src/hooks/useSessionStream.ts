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

import { useCallback, useRef, useState } from "react";
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

// ---------------------------------------------------------------------------
// SSE frame type discriminated union (matches server event names)
// ---------------------------------------------------------------------------

type SseEventType =
  | "thought"
  | "file_diff"
  | "tool_call"
  | "sandbox_status"
  | "terminal_output"
  | "plan_update"
  | "clarification_requested"
  | "checkpoint_created"
  | "checkpoint_restored"
  | "error"
  | "done";

interface SseFrame {
  event: SseEventType;
  data: unknown;
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export function useSessionStream(sessionId: string) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [stagedPatches, setStagedPatches] = useState<Record<string, string>>({});
  const [sandboxStatus, setSandboxStatus] = useState<SandboxStatus | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const [terminalLogs, setTerminalLogs] = useState<string[]>([]);
  const [plan, setPlan] = useState<PlanTask[]>([]);
  const [pendingClarification, setPendingClarification] = useState<WaitingInput | null>(null);
  const [checkpoints, setCheckpoints] = useState<CheckpointOut[]>([]);

  // AbortController ref so we can cancel in-flight streams on unmount / new message.
  const abortRef = useRef<AbortController | null>(null);

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
              setSandboxStatus({
                status: (d?.status as string) ?? "unknown",
                passed: (d?.passed as boolean) ?? undefined,
                logs: (d?.logs as string) ?? undefined,
                run_url: (d?.run_url as string) ?? undefined,
              });
              break;
            }
            case "terminal_output": {
              const d = frame.data as Record<string, string>;
              const chunk = d?.chunk ?? (typeof frame.data === "string" ? frame.data : "");
              if (chunk) {
                setTerminalLogs((prev) => [...prev, chunk]);
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
            case "error": {
              const errMsg =
                typeof frame.data === "string"
                  ? frame.data
                  : (frame.data as Record<string, string>)?.message ?? "Agent error";
              setMessages((prev) => [...prev, { role: "system", content: `Error: ${errMsg}` }]);
              break;
            }
            case "done": {
              // Finalise the assistant message with all accumulated content.
              // The backend emits the LLM response text via put_thought ({"delta": "..."})
              // and never emits a separate content event, so if assistantContent is empty
              // we promote the last non-heartbeat thought as the visible response.
              const HEARTBEAT = "Analyzing your request…";
              const contentThought = thoughts.filter((t) => t !== HEARTBEAT).at(-1) ?? "";
              const finalContent =
                typeof frame.data === "string"
                  ? frame.data
                  : assistantContent || contentThought;
              // Keep all thoughts in the accordion but strip the response from thoughts
              // so it doesn't display twice (once as content, once collapsed).
              const displayThoughts = thoughts.filter(
                (t) => t !== HEARTBEAT && t !== contentThought
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
        if ((err as { name?: string })?.name === "AbortError") {
          // Intentional abort — no error to surface.
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
    abortRef.current?.abort();
    setIsStreaming(false);
  }, []);

  return {
    messages,
    stagedPatches,
    sandboxStatus,
    isStreaming,
    terminalLogs,
    plan,
    pendingClarification,
    checkpoints,
    sendChatMessage,
    stopStreaming,
    setStagedPatches,
    setMessages,
    setPlan,
    setPendingClarification,
    setCheckpoints,
  };
}
