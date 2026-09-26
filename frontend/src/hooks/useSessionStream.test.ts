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
});
