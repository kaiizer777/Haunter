import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import SessionWorkspaceClient from "./SessionWorkspaceClient";
import { api, SessionOut } from "@/lib/api";

const mockRouterReplace = vi.fn();
const mockRouterPush = vi.fn();
const mockSendChatMessage = vi.fn();
const mockStopStreaming = vi.fn();
const mockSetStagedPatches = vi.fn();

let mockUseSessionStreamState = {
  messages: [] as any[],
  stagedPatches: {} as Record<string, string>,
  sandboxStatus: null as any,
  sandboxQueued: null as any,
  sandboxProgress: null as any,
  sandboxSteps: [] as any[],
  sandboxResult: null as any,
  ciStartedAt: null as any,
  ciFinishedAt: null as any,
  isStreaming: false,
  terminalLogs: [] as string[],
  plan: [] as any[],
  pendingClarification: null as any,
  checkpoints: [] as any[],
  auditScans: [] as any[],
  activeAudit: null as any,
  sendChatMessage: mockSendChatMessage,
  stopStreaming: mockStopStreaming,
  setStagedPatches: mockSetStagedPatches,
  setMessages: vi.fn(),
  setTerminalLogs: vi.fn(),
  setPlan: vi.fn(),
  setPendingClarification: vi.fn(),
  setCheckpoints: vi.fn(),
  setAuditScans: vi.fn(),
  setActiveAudit: vi.fn(),
  setSandboxQueued: vi.fn(),
  setSandboxProgress: vi.fn(),
  setSandboxStatus: vi.fn(),
  setSandboxResult: vi.fn(),
};

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    replace: mockRouterReplace,
    push: mockRouterPush,
  }),
  useSearchParams: () => ({
    get: (key: string) => (key === "id" ? "sess_abc123" : null),
  }),
}));

vi.mock("@/components/layout/app-layout", () => ({
  AppLayout: ({
    children,
    actions,
  }: {
    children: React.ReactNode;
    actions?: React.ReactNode;
  }) => (
    <div data-testid="app-layout">
      <div data-testid="topbar-actions">{actions}</div>
      {children}
    </div>
  ),
}));

vi.mock("@/hooks/useSessionStream", () => ({
  useSessionStream: () => mockUseSessionStreamState,
}));

vi.mock("@/hooks/useWebContainer", () => ({
  useWebContainer: () => ({
    status: "idle",
    previewUrl: null,
    terminalOutput: [],
    boot: vi.fn(),
    writeFile: vi.fn(),
    writeEnvFile: vi.fn(),
    restartDevServer: vi.fn(),
    teardown: vi.fn(),
    error: null,
  }),
}));

vi.mock("@/lib/api", () => ({
  api: {
    getSession: vi.fn(),
    getAvailableModels: vi.fn(),
    verifySession: vi.fn(),
    closeSession: vi.fn(),
    commitSession: vi.fn(),
    clarifySession: vi.fn(),
  },
  ApiError: class ApiError extends Error {
    status: number;
    constructor(message: string, status: number) {
      super(message);
      this.status = status;
    }
  },
}));

describe("SessionWorkspaceClient (app/sessions/workspace/SessionWorkspaceClient.tsx)", () => {
  const mockSession: SessionOut = {
    id: "sess_abc123",
    user_id: "usr_1",
    repo_id: "repo_1",
    repo_owner: "kaiizer777",
    repo_name: "Haunter",
    title: "Pairing on Haunter",
    status: "active",
    branch_name: "haunter/session-abc12345",
    base_sha: "abcd1234efgh5678",
    conversation_history: [],
    staged_patches: {},
    plan: [],
    checkpoints: [],
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  };

  beforeEach(() => {
    vi.clearAllMocks();
    mockUseSessionStreamState = {
      messages: [],
      stagedPatches: {},
      sandboxStatus: null,
      sandboxQueued: null,
      sandboxProgress: null,
      sandboxSteps: [],
      sandboxResult: null,
      ciStartedAt: null,
      ciFinishedAt: null,
      isStreaming: false,
      terminalLogs: [],
      plan: [],
      pendingClarification: null,
      checkpoints: [],
      auditScans: [],
      activeAudit: null,
      sendChatMessage: mockSendChatMessage,
      stopStreaming: mockStopStreaming,
      setStagedPatches: mockSetStagedPatches,
      setMessages: vi.fn(),
      setTerminalLogs: vi.fn(),
      setPlan: vi.fn(),
      setPendingClarification: vi.fn(),
      setCheckpoints: vi.fn(),
      setAuditScans: vi.fn(),
      setActiveAudit: vi.fn(),
      setSandboxQueued: vi.fn(),
      setSandboxProgress: vi.fn(),
      setSandboxStatus: vi.fn(),
      setSandboxResult: vi.fn(),
    };
    vi.mocked(api.getSession).mockResolvedValue(mockSession);
    vi.mocked(api.getAvailableModels).mockResolvedValue({
      opencode_zen: [],
      openai: [],
      anthropic: [],
      groq: [],
    });
    vi.mocked(api.clarifySession).mockResolvedValue(mockSession as any);
  });

  it("disables Commit & PR button when no staged patches, or when isStreaming is true", async () => {
    const { rerender } = render(<SessionWorkspaceClient sessionId="sess_abc123" />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /commit & pr/i })).toBeDisabled();
    });

    // When patches are staged, button becomes enabled
    mockUseSessionStreamState = {
      ...mockUseSessionStreamState,
      stagedPatches: { "src/auth.ts": "--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new" },
    };
    rerender(<SessionWorkspaceClient sessionId="sess_abc123" />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /commit & pr/i })).not.toBeDisabled();
    });

    // When isStreaming is true, button is disabled even with staged patches
    mockUseSessionStreamState = {
      ...mockUseSessionStreamState,
      isStreaming: true,
    };
    rerender(<SessionWorkspaceClient sessionId="sess_abc123" />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /commit & pr/i })).toBeDisabled();
    });
  });

  it("calculates 0 tokens when conversation is empty without artificial floor", async () => {
    render(<SessionWorkspaceClient sessionId="sess_abc123" />);

    await waitFor(() => {
      expect(screen.getByText("0")).toBeInTheDocument();
      expect(screen.getByText("0.0%")).toBeInTheDocument();
    });
  });

  it("wires and displays security violation badge when security violations are detected", async () => {
    mockUseSessionStreamState = {
      ...mockUseSessionStreamState,
      messages: [
        {
          role: "assistant",
          content: "Scanned files",
          toolCalls: [
            {
              name: "scan_security_vulnerabilities",
              args: {
                file_paths: ["auth.py"],
                scan_result: "Critical security violation: SQL injection vulnerability detected",
              },
            },
          ],
        },
      ],
    };

    render(<SessionWorkspaceClient sessionId="sess_abc123" />);

    await waitFor(() => {
      expect(screen.getByText("Security risk")).toBeInTheDocument();
    });

    // Dismiss security risk badge
    fireEvent.click(screen.getByText("Security risk"));

    await waitFor(() => {
      expect(screen.queryByText("Security risk")).not.toBeInTheDocument();
    });
  });

  it("handles audit report prose fix by sending chat prompt instead of corrupting stagedPatches", async () => {
    mockUseSessionStreamState = {
      ...mockUseSessionStreamState,
      messages: [
        {
          role: "assistant",
          content: "",
          auditScan: {
            scan_id: "scan_123",
            target_label: "Auth Scan",
            startedAt: Date.now(),
            status: "completed",
            report: {
              scan_id: "scan_123",
              findings: [
                {
                  id: "AUTH-1",
                  severity: "HIGH",
                  file_path: "src/auth.ts",
                  description: "Missing JWT expiration check",
                  suggested_fix: "Add exp claim validation in decode_token",
                },
              ],
            },
          },
        },
      ],
    };

    render(<SessionWorkspaceClient sessionId="sess_abc123" />);

    await waitFor(() => {
      expect(screen.getByText("Stage Surgical Fix")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("Stage Surgical Fix"));

    await waitFor(() => {
      expect(mockSendChatMessage).toHaveBeenCalledWith(
        expect.stringContaining("Apply surgical fix for finding AUTH-1 in src/auth.ts"),
        expect.anything()
      );
    });

    expect(mockSetStagedPatches).not.toHaveBeenCalled();
  });

  it("promotes ask_user_clarification into prominent active blocking prompt in transcript", async () => {
    mockUseSessionStreamState = {
      ...mockUseSessionStreamState,
      messages: [
        {
          role: "assistant",
          content: "I investigated the authentication failure. We have two implementation strategies.",
          toolCalls: [
            {
              name: "ask_user_clarification",
              args: {
                question: "Should we migrate to OAuth2 or keep password hash?",
                options: ["Migrate to OAuth2", "Keep password hash"],
              },
            },
          ],
        },
      ],
    };

    render(<SessionWorkspaceClient sessionId="sess_abc123" />);

    await waitFor(() => {
      expect(screen.getByTestId("clarification-card-active")).toBeInTheDocument();
      expect(screen.getByText(/action required · agent blocked/i)).toBeInTheDocument();
      expect(screen.getByText("Should we migrate to OAuth2 or keep password hash?")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /migrate to oauth2/i })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /keep password hash/i })).toBeInTheDocument();
    });
  });

  it("allows submitting clarification answer via option buttons directly in transcript", async () => {
    mockUseSessionStreamState = {
      ...mockUseSessionStreamState,
      messages: [
        {
          role: "assistant",
          content: "Need clarification",
          toolCalls: [
            {
              name: "ask_user_clarification",
              args: {
                question: "Pick database provider",
                options: ["Neon Postgres", "Hermetic SQLite"],
              },
            },
          ],
        },
      ],
    };

    render(<SessionWorkspaceClient sessionId="sess_abc123" />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /neon postgres/i })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: /neon postgres/i }));

    await waitFor(() => {
      expect(api.clarifySession).toHaveBeenCalledWith("sess_abc123", { response: "Neon Postgres" });
      expect(mockSendChatMessage).toHaveBeenCalledWith("Proceed with: Neon Postgres", expect.anything());
    });
  });

  it("orders and distinguishes multiple outstanding clarification questions", async () => {
    mockUseSessionStreamState = {
      ...mockUseSessionStreamState,
      messages: [
        {
          role: "assistant",
          content: "Two decisions required.",
          toolCalls: [
            {
              name: "ask_user_clarification",
              args: {
                question: "Choose database",
                options: ["Postgres", "SQLite"],
              },
            },
            {
              name: "ask_user_clarification",
              args: {
                question: "Run migrations automatically?",
                options: ["Yes", "No"],
              },
            },
          ],
        },
      ],
    };

    render(<SessionWorkspaceClient sessionId="sess_abc123" />);

    await waitFor(() => {
      expect(screen.getByText("Question 1 of 2")).toBeInTheDocument();
      expect(screen.getByText("Question 2 of 2")).toBeInTheDocument();
      expect(screen.getByText("Choose database")).toBeInTheDocument();
      expect(screen.getByText("Run migrations automatically?")).toBeInTheDocument();
    });
  });

  it("preserves update_plan chip and planning execution summary without regression", async () => {
    mockUseSessionStreamState = {
      ...mockUseSessionStreamState,
      messages: [
        {
          role: "assistant",
          content: "Here is the plan.",
          toolCalls: [
            {
              name: "update_plan",
              args: {
                completed_count: 2,
                total_count: 5,
              },
            },
          ],
        },
      ],
    };

    render(<SessionWorkspaceClient sessionId="sess_abc123" />);

    await waitFor(() => {
      expect(screen.getByText("Planning execution")).toBeInTheDocument();
      expect(screen.getByText("Updated task checklist (2/5)")).toBeInTheDocument();
    });
  });
});
