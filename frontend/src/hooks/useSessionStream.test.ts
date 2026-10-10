import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import {
  useSessionStream,
  AuditScanStartEvent,
  AuditProgressEvent,
  AuditReportEvent,
} from "./useSessionStream";

describe("useSessionStream — Audit SSE Events Handling", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  function createMockStreamResponse(chunks: string[]) {
    const encoder = new TextEncoder();
    const stream = new ReadableStream({
      start(controller) {
        for (const chunk of chunks) {
          controller.enqueue(encoder.encode(chunk));
        }
        controller.close();
      },
    });

    return {
      ok: true,
      status: 200,
      body: stream,
      text: async () => "",
    } as unknown as Response;
  }

  it("handles audit_scan_start event and initializes audit state", async () => {
    const onAuditScanStart = vi.fn();
    const mockPayload: AuditScanStartEvent = {
      scan_id: "scan_test_01",
      target_label: "Backend Security Scan",
      perspectives: ["Security", "Correctness"],
      scan_type: "security_scan",
      timestamp: 1700000000000,
    };

    const sseChunk = [
      "event: audit_scan_start\n",
      `data: ${JSON.stringify(mockPayload)}\n\n`,
      "event: done\n",
      'data: "Finished"\n\n',
    ].join("");

    globalThis.fetch = vi.fn().mockResolvedValue(createMockStreamResponse([sseChunk]));

    const { result } = renderHook(() =>
      useSessionStream("session_123", { onAuditScanStart })
    );

    await act(async () => {
      await result.current.sendChatMessage("/security-scan");
    });

    // Check activeAudit state
    expect(result.current.activeAudit).not.toBeNull();
    expect(result.current.activeAudit?.scan_id).toBe("scan_test_01");
    expect(result.current.activeAudit?.target_label).toBe("Backend Security Scan");
    expect(result.current.activeAudit?.status).toBe("scanning");
    expect(result.current.activeAudit?.progress?.completed).toBe(0);

    // Check auditScans array
    expect(result.current.auditScans).toHaveLength(1);
    expect(result.current.auditScans[0].scan_id).toBe("scan_test_01");

    // Check onAuditScanStart callback
    expect(onAuditScanStart).toHaveBeenCalledTimes(1);
    expect(onAuditScanStart).toHaveBeenCalledWith(
      expect.objectContaining({
        scan_id: "scan_test_01",
        target_label: "Backend Security Scan",
      })
    );

    // Check inline message auditScan
    const assistantMsg = result.current.messages.find((m) => m.role === "assistant");
    expect(assistantMsg?.auditScan?.scan_id).toBe("scan_test_01");
  });

  it("handles audit_progress event and updates live scanning progress", async () => {
    const onAuditProgress = vi.fn();

    const startPayload = {
      scan_id: "scan_test_02",
      target_label: "Whole-Repo Audit",
      perspectives: ["Security", "Performance", "Correctness"],
    };

    const progressPayload: AuditProgressEvent = {
      scan_id: "scan_test_02",
      completed_perspectives: 2,
      total_perspectives: 3,
      current_perspective: "Performance",
      status: "Benchmarking query latency",
    };

    const sseChunk = [
      "event: audit_scan_start\n",
      `data: ${JSON.stringify(startPayload)}\n\n`,
      "event: audit_progress\n",
      `data: ${JSON.stringify(progressPayload)}\n\n`,
      "event: done\n",
      'data: "Finished"\n\n',
    ].join("");

    globalThis.fetch = vi.fn().mockResolvedValue(createMockStreamResponse([sseChunk]));

    const { result } = renderHook(() =>
      useSessionStream("session_123", { onAuditProgress })
    );

    await act(async () => {
      await result.current.sendChatMessage("/repo-audit");
    });

    expect(result.current.activeAudit?.progress?.completed).toBe(2);
    expect(result.current.activeAudit?.progress?.total).toBe(3);
    expect(result.current.activeAudit?.progress?.current).toBe("Performance");
    expect(result.current.activeAudit?.progress?.statusText).toBe(
      "Benchmarking query latency"
    );

    expect(onAuditProgress).toHaveBeenCalledTimes(1);
    expect(onAuditProgress).toHaveBeenCalledWith(
      expect.objectContaining({
        scan_id: "scan_test_02",
        completed_perspectives: 2,
        total_perspectives: 3,
        current_perspective: "Performance",
      })
    );
  });

  it("handles audit_report event, marks audit completed, and attaches findings", async () => {
    const onAuditReport = vi.fn();

    const startPayload = {
      scan_id: "scan_test_03",
      target_label: "Security Audit",
    };

    const reportPayload: AuditReportEvent = {
      scan_id: "scan_test_03",
      audit_id: "audit_999",
      health_score: 95,
      confidence: 98,
      executive_summary: "Scan complete with 1 minor note.",
      findings: [
        {
          id: "SEC-1",
          severity: "NOTE",
          file_path: "backend/app/auth.py",
          description: "Consider rotation limit",
        },
      ],
      remediation_diff: "--- a/backend/app/auth.py\n+++ b/backend/app/auth.py\n@@ -1 +1 @@\n-# note",
    };

    const sseChunk = [
      "event: audit_scan_start\n",
      `data: ${JSON.stringify(startPayload)}\n\n`,
      "event: audit_report\n",
      `data: ${JSON.stringify(reportPayload)}\n\n`,
      "event: done\n",
      'data: "Done"\n\n',
    ].join("");

    globalThis.fetch = vi.fn().mockResolvedValue(createMockStreamResponse([sseChunk]));

    const { result } = renderHook(() =>
      useSessionStream("session_123", { onAuditReport })
    );

    await act(async () => {
      await result.current.sendChatMessage("/security-scan");
    });

    expect(result.current.activeAudit?.status).toBe("completed");
    expect(result.current.activeAudit?.report?.health_score).toBe(95);
    expect(result.current.activeAudit?.report?.confidence).toBe(98);
    expect(result.current.activeAudit?.report?.findings).toHaveLength(1);
    expect(result.current.activeAudit?.report?.findings[0].id).toBe("SEC-1");

    expect(onAuditReport).toHaveBeenCalledTimes(1);
    expect(onAuditReport).toHaveBeenCalledWith(
      expect.objectContaining({
        scan_id: "scan_test_03",
        health_score: 95,
        confidence: 98,
      })
    );

    // Verify messages feed contains the completed audit report card
    const lastMsg = result.current.messages[result.current.messages.length - 1];
    expect(lastMsg.auditScan?.status).toBe("completed");
    expect(lastMsg.auditScan?.report?.health_score).toBe(95);
  });

  it("handles stopStreaming by aborting the stream cleanly without inserting error messages", async () => {
    let abortSignalObserved: AbortSignal | undefined;
    globalThis.fetch = vi.fn().mockImplementation((_url, init) => {
      abortSignalObserved = init?.signal;
      const stream = new ReadableStream({
        start(controller) {
          init?.signal?.addEventListener("abort", () => {
            controller.error(new DOMException("The operation was aborted.", "AbortError"));
          });
        },
      });
      return Promise.resolve({
        ok: true,
        status: 200,
        body: stream,
        text: async () => "",
      } as unknown as Response);
    });

    const { result } = renderHook(() => useSessionStream("session_123"));

    let sendPromise: Promise<void> | undefined;
    act(() => {
      sendPromise = result.current.sendChatMessage("hello");
    });

    expect(result.current.isStreaming).toBe(true);

    act(() => {
      result.current.stopStreaming();
    });

    await act(async () => {
      await sendPromise;
    });

    expect(abortSignalObserved?.aborted).toBe(true);
    expect(result.current.isStreaming).toBe(false);
    expect(result.current.messages.some((m) => m.role === "system" && m.content.includes("Error"))).toBe(false);
  });

  it("does not promote intermediate tool reasoning thoughts as final assistant content", async () => {
    const sseChunk = [
      "event: tool_call\n",
      'data: {"name": "read_file", "args": {"path": "src/main.py"}}\n\n',
      "event: thought\n",
      'data: {"delta": "Calling read_file"}\n\n',
      "event: done\n",
      "data: {}\n\n",
    ].join("");

    globalThis.fetch = vi.fn().mockResolvedValue(createMockStreamResponse([sseChunk]));

    const { result } = renderHook(() => useSessionStream("session_123"));

    await act(async () => {
      await result.current.sendChatMessage("read file");
    });

    const assistantMsg = result.current.messages.find((m) => m.role === "assistant");
    expect(assistantMsg).toBeDefined();
    expect(assistantMsg?.content).toBe("");
    expect(assistantMsg?.thoughts).toEqual(["Calling read_file"]);
    expect(assistantMsg?.toolCalls).toHaveLength(1);
    expect(assistantMsg?.toolCalls?.[0].name).toBe("read_file");
  });

  it("promotes thoughts starting with 'Calling...' as assistant content when no tool calls are present", async () => {
    const sseChunk = [
      "event: thought\n",
      'data: {"delta": "Calling this endpoint will return the session details."}\n\n',
      "event: done\n",
      "data: {}\n\n",
    ].join("");

    globalThis.fetch = vi.fn().mockResolvedValue(createMockStreamResponse([sseChunk]));

    const { result } = renderHook(() => useSessionStream("session_123"));

    await act(async () => {
      await result.current.sendChatMessage("how do I call this endpoint?");
    });

    const assistantMsg = result.current.messages.find((m) => m.role === "assistant");
    expect(assistantMsg).toBeDefined();
    expect(assistantMsg?.content).toBe("Calling this endpoint will return the session details.");
  });

  it("promotes genuine response prose starting with 'Calling...' even if unrelated tool calls exist", async () => {
    const sseChunk = [
      "event: tool_call\n",
      'data: {"name": "read_file", "args": {"path": "src/main.py"}}\n\n',
      "event: thought\n",
      'data: {"delta": "Calling this function is the standard pattern across the repo."}\n\n',
      "event: done\n",
      "data: {}\n\n",
    ].join("");

    globalThis.fetch = vi.fn().mockResolvedValue(createMockStreamResponse([sseChunk]));

    const { result } = renderHook(() => useSessionStream("session_123"));

    await act(async () => {
      await result.current.sendChatMessage("analyze pattern");
    });

    const assistantMsg = result.current.messages.find((m) => m.role === "assistant");
    expect(assistantMsg).toBeDefined();
    expect(assistantMsg?.content).toBe("Calling this function is the standard pattern across the repo.");
  });

  it("promotes thought starting with the tool name when it is long prose (>150 chars) or multi-line", async () => {
    const longProseThought =
      "Calling read_file is the standard pattern across the whole repo to inspect configuration files and ensure all environments are synced properly with verified configurations.";
    expect(longProseThought.length).toBeGreaterThan(150);

    const sseChunk = [
      "event: tool_call\n",
      'data: {"name": "read_file", "args": {"path": "src/config.py"}}\n\n',
      "event: thought\n",
      `data: ${JSON.stringify({ delta: longProseThought })}\n\n`,
      "event: done\n",
      "data: {}\n\n",
    ].join("");

    globalThis.fetch = vi.fn().mockResolvedValue(createMockStreamResponse([sseChunk]));

    const { result } = renderHook(() => useSessionStream("session_123"));

    await act(async () => {
      await result.current.sendChatMessage("inspect config");
    });

    const assistantMsg = result.current.messages.find((m) => m.role === "assistant");
    expect(assistantMsg).toBeDefined();
    expect(assistantMsg?.content).toBe(longProseThought);
  });

  it("retries on unexpected stream drop and passes Last-Event-ID to resume", async () => {
    // First chunk drops before done event with id: 10
    const chunk1 = [
      "event: thought\n",
      "id: 10\n",
      "retry: 50\n",
      'data: {"delta": "Partial thought..."}\n\n',
    ].join("");

    // Second chunk after retry delivers completion
    const chunk2 = [
      "event: thought\n",
      "id: 11\n",
      'data: {"delta": " and completed thought."}\n\n',
      "event: done\n",
      'data: {"session_id": "session_123"}\n\n',
    ].join("");

    const fetchCalls: { url: string; headers: Record<string, string> }[] = [];
    let callCount = 0;

    globalThis.fetch = vi.fn().mockImplementation((url, init) => {
      callCount++;
      fetchCalls.push({ url: String(url), headers: (init?.headers as Record<string, string>) || {} });
      if (callCount === 1) {
        return Promise.resolve(createMockStreamResponse([chunk1]));
      }
      return Promise.resolve(createMockStreamResponse([chunk2]));
    });

    const { result } = renderHook(() => useSessionStream("session_123"));

    await act(async () => {
      await result.current.sendChatMessage("hello");
    });

    expect(callCount).toBe(2);
    // Second call must include Last-Event-ID
    expect(fetchCalls[1].headers["Last-Event-ID"]).toBe("10");

    const assistantMsg = result.current.messages.find((m) => m.role === "assistant");
    expect(assistantMsg).toBeDefined();
    // The done handler promotes the last thought into the visible response
    // content and strips it from the thoughts accordion to avoid duplication.
    expect(assistantMsg?.thoughts).toEqual(["Partial thought..."]);
    expect(assistantMsg?.content).toBe(" and completed thought.");
    expect(result.current.isStreaming).toBe(false);
    expect(result.current.isReconnecting).toBe(false);
  });

  it("surfaces connection interrupted message when retries are exhausted without terminal done event", async () => {
    const chunkIncomplete = [
      "event: thought\n",
      "id: 1\n",
      "retry: 20\n",
      'data: {"delta": "Thinking..."}\n\n',
    ].join("");

    const mockFetch = vi.fn().mockImplementation(() =>
      Promise.resolve(createMockStreamResponse([chunkIncomplete]))
    );
    globalThis.fetch = mockFetch;

    const { result } = renderHook(() => useSessionStream("session_123"));

    await act(async () => {
      await result.current.sendChatMessage("hello");
    });

    // Initial request plus all three retries must have been attempted.
    expect(mockFetch).toHaveBeenCalledTimes(4);
    // When retries are exhausted without receiving 'done', a system warning is displayed
    const systemMsg = result.current.messages.find(
      (m) => m.role === "system" && m.content.includes("interrupted")
    );
    expect(systemMsg).toBeDefined();
    expect(result.current.isStreaming).toBe(false);
  });

  it("does not advance the replay cursor for an incomplete trailing frame", async () => {
    const complete = [
      "event: thought\n",
      "id: 7\n",
      "retry: 20\n",
      'data: {"delta": "Complete thought."}\n\n',
    ].join("");
    // Stream ends with a truncated frame: id received, payload cut off, no
    // trailing blank line.
    const truncated = complete + 'event: thought\nid: 8\ndata: {"delta": "truncated"\n';

    const chunk2 = [
      "event: thought\n",
      "id: 8\n",
      "retry: 20\n",
      'data: {"delta": "Full eight."}\n\n',
      "event: done\n",
      'data: {"session_id": "session_123"}\n\n',
    ].join("");

    const fetchCalls: { headers: Record<string, string> }[] = [];
    let callCount = 0;

    globalThis.fetch = vi.fn().mockImplementation((url, init) => {
      void url;
      callCount++;
      fetchCalls.push({ headers: (init?.headers as Record<string, string>) || {} });
      if (callCount === 1) {
        return Promise.resolve(createMockStreamResponse([truncated]));
      }
      return Promise.resolve(createMockStreamResponse([chunk2]));
    });

    const { result } = renderHook(() => useSessionStream("session_123"));

    await act(async () => {
      await result.current.sendChatMessage("hello");
    });

    expect(callCount).toBe(2);
    // The cursor stayed at the last COMPLETE frame, so the retry replays
    // event 8 in full instead of skipping it.
    expect(fetchCalls[1].headers["Last-Event-ID"]).toBe("7");

    const assistantMsg = result.current.messages.find((m) => m.role === "assistant");
    expect(assistantMsg).toBeDefined();
    // The truncated payload was dropped, not surfaced as a garbled thought;
    // the replayed event 8 arrives in full and (per the done handler) is
    // promoted to the visible response content.
    expect(assistantMsg?.thoughts).toEqual(["Complete thought."]);
    expect(assistantMsg?.content).toBe("Full eight.");
    expect(result.current.isStreaming).toBe(false);
  });

  it("retries resumption when the turn is alive on another worker (409)", async () => {
    const doneChunk = [
      "event: thought\n",
      "id: 9\n",
      "retry: 20\n",
      'data: {"delta": "Live thought."}\n\n',
      "event: done\n",
      'data: {"session_id": "session_123"}\n\n',
    ].join("");

    let callCount = 0;
    globalThis.fetch = vi.fn().mockImplementation((url, init) => {
      void url;
      void init;
      callCount++;
      if (callCount === 1) {
        return Promise.resolve({
          ok: false,
          status: 409,
          body: null,
          text: async () => '{"detail":"TURN_ACTIVE_ELSEWHERE: a turn is still running"}',
        } as unknown as Response);
      }
      return Promise.resolve(createMockStreamResponse([doneChunk]));
    });

    const { result } = renderHook(() => useSessionStream("session_123"));

    await act(async () => {
      await result.current.sendChatMessage("hello");
    });

    // The 409 is retryable (turn alive elsewhere), not terminal.
    expect(callCount).toBe(2);
    const assistantMsg = result.current.messages.find((m) => m.role === "assistant");
    expect(assistantMsg?.content).toBe("Live thought.");
    const systemErr = result.current.messages.find(
      (m) => m.role === "system" && m.content.startsWith("Error:")
    );
    expect(systemErr).toBeUndefined();
    expect(result.current.isStreaming).toBe(false);
  }, 15000);

  it("an aborted retry does not clear a newer stream that took over", async () => {
    const incomplete = [
      "event: thought\n",
      "id: 1\n",
      "retry: 20\n",
      'data: {"delta": "First."}\n\n',
    ].join("");
    const doneChunk = [
      "event: done\n",
      'data: {"session_id": "session_123"}\n\n',
    ].join("");

    let releaseSecond: ((v: Response) => void) | null = null;
    const secondGate = new Promise<Response>((resolve) => {
      releaseSecond = resolve;
    });
    globalThis.fetch = vi.fn().mockImplementation((url, init) => {
      void url;
      const body = JSON.parse((init?.body as string) ?? "{}") as { message?: string };
      if (body.message === "second") {
        return secondGate;
      }
      return Promise.resolve(createMockStreamResponse([incomplete]));
    });

    const { result } = renderHook(() => useSessionStream("session_123"));

    let first: Promise<void> = Promise.resolve();
    act(() => {
      first = result.current.sendChatMessage("first");
    });
    // Let the first attempt finish streaming and enter backoff.
    await act(async () => {
      await new Promise((r) => setTimeout(r, 80));
    });
    // Abort the retrying stream, then start a newer one.
    act(() => {
      result.current.stopStreaming();
    });
    let second: Promise<void> = Promise.resolve();
    act(() => {
      second = result.current.sendChatMessage("second");
    });
    // The aborted first stream settles while the second is still pending:
    // shared streaming state must still belong to the newer stream.
    await act(async () => {
      await new Promise((r) => setTimeout(r, 150));
    });
    expect(result.current.isStreaming).toBe(true);

    await act(async () => {
      if (releaseSecond !== null) {
        releaseSecond(createMockStreamResponse([doneChunk]));
      }
      await second;
      await first;
    });
    expect(result.current.isStreaming).toBe(false);
  }, 15000);
});
