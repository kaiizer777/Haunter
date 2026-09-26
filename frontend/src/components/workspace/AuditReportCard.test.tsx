import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { AuditReportCard } from "./AuditReportCard";
import type { AuditCardState, AuditFinding } from "@/hooks/useSessionStream";

describe("AuditReportCard.tsx", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    Object.defineProperty(navigator, "clipboard", {
      value: {
        writeText: vi.fn().mockResolvedValue(undefined),
      },
      writable: true,
      configurable: true,
    });
  });

  const mockScanningState: AuditCardState = {
    scan_id: "scan_123",
    target_label: "Repository Security Scan",
    perspectives: ["Security", "Correctness", "Performance"],
    scan_type: "security_scan",
    startedAt: 1700000000000,
    status: "scanning",
    progress: {
      completed: 2,
      total: 4,
      current: "Security Analysis",
      statusText: "Analyzing perspectives…",
    },
  };

  const mockFindings: AuditFinding[] = [
    {
      id: "SEC-001",
      severity: "CRITICAL",
      category: "Insecure Direct Object Reference",
      perspective: "Security",
      file_path: "backend/app/routers/sessions.py",
      line_start: 42,
      line_end: 45,
      title: "Missing Tenant Boundary Check on Session Lookup",
      description: "Query fetches session by primary key without scoping by authenticated tenant.",
      suggested_fix: "--- a/backend/app/routers/sessions.py\n+++ b/backend/app/routers/sessions.py\n@@ -42,2 +42,3 @@\n-query = select(Session).where(Session.id == session_id)\n+query = select(Session).where(Session.id == session_id, Session.tenant_id == current_tenant)",
    },
    {
      id: "PERF-002",
      severity: "WARNING",
      category: "Unindexed Query Filter",
      perspective: "Performance",
      file_path: "backend/app/routers/tokens.py",
      line_start: 88,
      title: "Unindexed Filter on Large Sessions Table",
      description: "Filtering without index causes sequential scan on high-volume table.",
      suggested_fix: "+Index('ix_tokens_expires', Token.expires_at)",
    },
    {
      id: "NOTE-003",
      severity: "NOTE",
      category: "Style & Best Practice",
      perspective: "Architecture",
      file_path: "frontend/src/app/page.tsx",
      line_start: 12,
      title: "Consider Using Semantic Token Variable",
      description: "Direct color hex is used instead of semantic CSS variable token.",
    },
  ];

  const mockCompletedState: AuditCardState = {
    scan_id: "scan_123",
    target_label: "Repository Security Scan",
    perspectives: ["Security", "Correctness", "Performance"],
    scan_type: "security_scan",
    startedAt: 1700000000000,
    status: "completed",
    report: {
      scan_id: "scan_123",
      audit_id: "audit_456",
      target_label: "Repository Security Scan",
      confidence: 94,
      health_score: 88,
      executive_summary: "Automated scan detected 1 critical security boundary defect and 1 performance warning.",
      findings: mockFindings,
      remediation_diff: "--- a/backend/app/routers/sessions.py\n+++ b/backend/app/routers/sessions.py\n@@ -42,2 +42,3 @@\n-query = select(Session).where(Session.id == session_id)\n+query = select(Session).where(Session.id == session_id, Session.tenant_id == current_tenant)",
    },
  };

  describe("Live Scanning State", () => {
    it("renders scanning indicator and progress percentage", () => {
      render(<AuditReportCard scan={mockScanningState} />);

      expect(screen.getByTestId("audit-scanning-view")).toBeInTheDocument();
      expect(screen.getByText("Repository Security Scan")).toBeInTheDocument();
      expect(screen.getByText("Scanning…")).toBeInTheDocument();
      expect(screen.getByText("50%")).toBeInTheDocument();
      expect(screen.getByText("2 / 4")).toBeInTheDocument();
      expect(screen.getByText("Security Analysis")).toBeInTheDocument();
    });

    it("renders perspective tags during scanning", () => {
      render(<AuditReportCard scan={mockScanningState} />);

      expect(screen.getByText("Security")).toBeInTheDocument();
      expect(screen.getByText("Correctness")).toBeInTheDocument();
      expect(screen.getByText("Performance")).toBeInTheDocument();
    });

    it("renders progress bar with correct accessibility attributes", () => {
      render(<AuditReportCard scan={mockScanningState} />);

      const progressBar = screen.getByRole("progressbar");
      expect(progressBar).toBeInTheDocument();
      expect(progressBar).toHaveAttribute("aria-valuenow", "50");
    });
  });

  describe("Completed Audit Report State", () => {
    it("renders health score ring, confidence badge, and counters", () => {
      render(<AuditReportCard scan={mockCompletedState} />);

      expect(screen.getByTestId("audit-report-view")).toBeInTheDocument();
      expect(screen.getByText("88")).toBeInTheDocument();
      expect(screen.getByText("Excellent")).toBeInTheDocument();
      expect(screen.getByText("94% Confidence")).toBeInTheDocument();
      expect(screen.getByText("Done")).toBeInTheDocument();
      expect(screen.getByText("1 Blocker")).toBeInTheDocument();
      expect(screen.getByText("1 Warning")).toBeInTheDocument();
      expect(screen.getByText("1 Note")).toBeInTheDocument();
    });

    it("renders executive summary text", () => {
      render(<AuditReportCard scan={mockCompletedState} />);

      expect(
        screen.getByText(/Automated scan detected 1 critical security boundary defect/i)
      ).toBeInTheDocument();
    });

    it("filters findings by severity", () => {
      render(<AuditReportCard scan={mockCompletedState} />);

      // Initially all 3 findings are visible
      expect(screen.getByText("SEC-001")).toBeInTheDocument();
      expect(screen.getByText("PERF-002")).toBeInTheDocument();
      expect(screen.getByText("NOTE-003")).toBeInTheDocument();

      // Click "Blockers" filter
      fireEvent.click(screen.getByRole("button", { name: "Blockers (1)" }));
      expect(screen.getByText("SEC-001")).toBeInTheDocument();
      expect(screen.queryByText("PERF-002")).not.toBeInTheDocument();
      expect(screen.queryByText("NOTE-003")).not.toBeInTheDocument();

      // Click "Warnings" filter
      fireEvent.click(screen.getByRole("button", { name: "Warnings (1)" }));
      expect(screen.queryByText("SEC-001")).not.toBeInTheDocument();
      expect(screen.getByText("PERF-002")).toBeInTheDocument();
      expect(screen.queryByText("NOTE-003")).not.toBeInTheDocument();

      // Click "Notes" filter
      fireEvent.click(screen.getByRole("button", { name: "Notes (1)" }));
      expect(screen.queryByText("SEC-001")).not.toBeInTheDocument();
      expect(screen.queryByText("PERF-002")).not.toBeInTheDocument();
      expect(screen.getByText("NOTE-003")).toBeInTheDocument();

      // Click "All" filter to restore
      fireEvent.click(screen.getByRole("button", { name: "All (3)" }));
      expect(screen.getByText("SEC-001")).toBeInTheDocument();
      expect(screen.getByText("PERF-002")).toBeInTheDocument();
      expect(screen.getByText("NOTE-003")).toBeInTheDocument();
    });

    it("toggles suggested fix diff snippet and copies diff to clipboard", async () => {
      render(<AuditReportCard scan={mockCompletedState} />);

      // Suggested fix is hidden initially
      expect(screen.queryByText("Remediation Patch")).not.toBeInTheDocument();

      // Click "View suggested fix" for SEC-001
      const toggleButtons = screen.getAllByRole("button", { name: /view suggested fix/i });
      fireEvent.click(toggleButtons[0]);

      expect(screen.getByText("Remediation Patch")).toBeInTheDocument();
      expect(screen.getByText(/Session.tenant_id == current_tenant/)).toBeInTheDocument();

      // Click copy button
      const copyButton = screen.getByRole("button", { name: /copy remediation diff/i });
      fireEvent.click(copyButton);

      expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
        mockFindings[0].suggested_fix
      );
      expect(await screen.findByText("Copied")).toBeInTheDocument();
    });

    it("triggers onStageFix and transitions to staged state when 1-click fix is clicked", async () => {
      const onStageFix = vi.fn().mockImplementation(
        () => new Promise((resolve) => setTimeout(resolve, 50))
      );
      render(<AuditReportCard scan={mockCompletedState} onStageFix={onStageFix} />);

      const stageBtn = screen.getByRole("button", {
        name: "Stage surgical fix for finding SEC-001",
      });
      fireEvent.click(stageBtn);

      // Loading state
      expect(screen.getByText("Staging Fix…")).toBeInTheDocument();

      // Resolves to Staged ✓
      await waitFor(() => {
        expect(screen.getByText("Staged ✓")).toBeInTheDocument();
      });

      expect(onStageFix).toHaveBeenCalledWith(
        mockFindings[0],
        mockFindings[0].suggested_fix
      );
    });

    it("triggers onViewDiff when Open in Editor button is clicked", () => {
      const onViewDiff = vi.fn();
      render(<AuditReportCard scan={mockCompletedState} onViewDiff={onViewDiff} />);

      const editorBtn = screen.getByRole("button", {
        name: "Open backend/app/routers/sessions.py in editor",
      });
      fireEvent.click(editorBtn);

      expect(onViewDiff).toHaveBeenCalledWith("backend/app/routers/sessions.py");
    });

    it("triggers onStageFix when global complete remediation patch is staged", async () => {
      const onStageFix = vi.fn().mockResolvedValue(undefined);
      render(<AuditReportCard scan={mockCompletedState} onStageFix={onStageFix} />);

      const globalStageBtn = screen.getByRole("button", {
        name: "Stage Global Remediation Patch",
      });
      fireEvent.click(globalStageBtn);

      await waitFor(() => {
        expect(screen.getByText("All Staged ✓")).toBeInTheDocument();
      });

      expect(onStageFix).toHaveBeenCalledWith(
        mockFindings[0],
        mockCompletedState.report!.remediation_diff
      );
    });

    it("renders clean bill of health empty state when findings is empty", () => {
      const cleanState: AuditCardState = {
        scan_id: "scan_clean",
        target_label: "Repo Audit",
        startedAt: Date.now(),
        status: "completed",
        report: {
          findings: [],
          health_score: 100,
          confidence: 99,
          summary: "Zero issues found.",
        },
      };

      render(<AuditReportCard scan={cleanState} />);

      expect(
        screen.getByText(/Zero security vulnerabilities or regressions detected! Clean bill of health./i)
      ).toBeInTheDocument();
      expect(screen.getByText("100")).toBeInTheDocument();
    });
  });
});
