import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import SessionsPage from "./page";
import { api, SessionOut, RepoOut } from "@/lib/api";

const mockSearchParamsGet = vi.fn();
const mockRouterReplace = vi.fn();
const mockRouterPush = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    replace: mockRouterReplace,
    push: mockRouterPush,
  }),
  useSearchParams: () => ({
    get: mockSearchParamsGet,
  }),
}));

vi.mock("@/components/layout/app-layout", () => ({
  AppLayout: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="app-layout">{children}</div>
  ),
}));

vi.mock("@/lib/api", () => ({
  api: {
    listSessions: vi.fn(),
    getRepos: vi.fn(),
    createSession: vi.fn(),
    closeSession: vi.fn(),
  },
  ApiError: class ApiError extends Error {
    status: number;
    constructor(message: string, status: number) {
      super(message);
      this.status = status;
    }
  },
}));

describe("SessionsPage (app/sessions/page.tsx)", () => {
  const mockRepos: RepoOut[] = [
    {
      id: "repo_1",
      owner: "kaiizer777",
      name: "Haunter",
      default_branch: "main",
      language_hint: "python",
      active_model_config_id: null,
      created_at: new Date().toISOString(),
    },
  ];

  const mockSession: SessionOut = {
    id: "sess_12345678",
    user_id: "usr_1",
    repo_id: "repo_1",
    repo_owner: "kaiizer777",
    repo_name: "Haunter",
    title: "Pairing on Haunter",
    status: "active",
    branch_name: "haunter/session-test1234",
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
    mockSearchParamsGet.mockReturnValue("true"); // default list view to prevent auto-redirect in base tests
    vi.mocked(api.listSessions).mockResolvedValue({
      sessions: [mockSession],
      total: 1,
    });
    vi.mocked(api.getRepos).mockResolvedValue(mockRepos);
    vi.mocked(api.createSession).mockResolvedValue(mockSession);
  });

  it("renders the sessions list and repo information", async () => {
    render(<SessionsPage />);

    await waitFor(() => {
      expect(screen.getByText("Pairing on Haunter")).toBeInTheDocument();
      expect(screen.getByText("kaiizer777/Haunter")).toBeInTheDocument();
    });
  });

  it("auto-creates a session on an isolated topic branch when no active session exists", async () => {
    mockSearchParamsGet.mockReturnValue(null); // auto-load mode
    vi.mocked(api.listSessions).mockResolvedValue({
      sessions: [],
      total: 0,
    });

    render(<SessionsPage />);

    await waitFor(() => {
      expect(api.createSession).toHaveBeenCalledWith(
        expect.objectContaining({
          repo_id: "repo_1",
          branch_name: expect.stringMatching(/^haunter\/session-[a-zA-Z0-9_-]+$/),
          title: "Pairing on Haunter",
        })
      );
      expect(mockRouterReplace).toHaveBeenCalledWith("/sessions/workspace?id=sess_12345678");
    });
  });

  it("cancels auto-load and prevents redirect when Cancel button is clicked", async () => {
    mockSearchParamsGet.mockReturnValue(null);
    let resolveCreateSession: (val: SessionOut) => void;
    const delayedCreate = new Promise<SessionOut>((resolve) => {
      resolveCreateSession = resolve;
    });
    vi.mocked(api.listSessions).mockResolvedValue({
      sessions: [],
      total: 0,
    });
    vi.mocked(api.createSession).mockReturnValue(delayedCreate);

    render(<SessionsPage />);

    const cancelBtn = screen.getByText("Cancel auto-load & view all sessions");
    fireEvent.click(cancelBtn);

    // Resolve create session after cancel was clicked
    resolveCreateSession!(mockSession);

    await waitFor(() => {
      expect(screen.getByText("Live Sessions")).toBeInTheDocument();
    });

    expect(mockRouterReplace).not.toHaveBeenCalled();
  });

  it("auto launch button creates a session on an isolated topic branch", async () => {
    mockSearchParamsGet.mockReturnValue("true");
    render(<SessionsPage />);

    await waitFor(() => {
      expect(screen.getByText("Auto Launch")).toBeInTheDocument();
    });

    const autoLaunchBtn = screen.getByText("Auto Launch");
    fireEvent.click(autoLaunchBtn);

    await waitFor(() => {
      expect(api.createSession).toHaveBeenCalledWith(
        expect.objectContaining({
          repo_id: "repo_1",
          branch_name: expect.stringMatching(/^haunter\/session-[a-zA-Z0-9_-]+$/),
        })
      );
      expect(mockRouterPush).toHaveBeenCalledWith("/sessions/workspace?id=sess_12345678");
    });
  });
});
