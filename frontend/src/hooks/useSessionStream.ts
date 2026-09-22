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
import { API_BASE } from "@/lib/api";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ChatMessage {
  role: "user" | "assistant" | "system";
  content: string;
  thoughts?: string[];
  toolCalls?: ToolCallChip[];
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

  // AbortController ref so we can cancel in-flight streams on unmount / new message.
  const abortRef = useRef<AbortController | null>(null);

  const sendChatMessage = useCallback(
    async (prompt: string): Promise<void> => {
      if (isStreaming) return;

      // Abort any stale stream.
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;

      // Optimistically append the user message.
      setMessages((prev) => [...prev, { role: "user", content: prompt }]);
      setIsStreaming(true);

      // Working state for the current assistant turn being streamed.
      let thoughts: string[] = [];
      let toolCalls: ToolCallChip[] = [];
      let assistantContent = "";

      try {
        const url = `${API_BASE}/sessions/${sessionId}/chat`;
        const res = await fetch(url, {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message: prompt }),
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
          switch (frame.event) {
            case "thought": {
              const thought =
                typeof frame.data === "string"
                  ? frame.data
                  : (frame.data as Record<string, string>)?.content ?? "";
              thoughts = [...thoughts, thought];
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
                  };
                } else {
                  copy.push({
                    role: "assistant",
                    content: assistantContent,
                    thoughts,
                    toolCalls,
                  });
                }
                return copy;
              });
              break;
            }
            case "tool_call": {
              const d = frame.data as Record<string, unknown>;
              const chip: ToolCallChip = {
                name: (d?.name as string) ?? "unknown_tool",
                args: (d?.args as Record<string, unknown>) ?? undefined,
              };
              toolCalls = [...toolCalls, chip];
              setMessages((prev) => {
                const copy = [...prev];
                const last = copy[copy.length - 1];
                if (last?.role === "assistant") {
                  copy[copy.length - 1] = { ...last, thoughts, toolCalls, content: assistantContent };
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
              const finalContent =
                typeof frame.data === "string"
                  ? frame.data
                  : assistantContent;
              setMessages((prev) => {
                const copy = [...prev];
                const last = copy[copy.length - 1];
                if (last?.role === "assistant") {
                  copy[copy.length - 1] = {
                    ...last,
                    content: finalContent || last.content,
                    thoughts,
                    toolCalls,
                  };
                } else {
                  copy.push({ role: "assistant", content: finalContent, thoughts, toolCalls });
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

  return {
    messages,
    stagedPatches,
    sandboxStatus,
    isStreaming,
    sendChatMessage,
    setStagedPatches,
  };
}
