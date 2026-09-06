import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { api, ApiError, API_BASE } from "./api";

describe("api.ts", () => {
  const originalFetch = globalThis.fetch;
  const originalLocation = window.location;

  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    // @ts-expect-error - restoring window.location
    window.location = originalLocation;
  });

  describe("API_BASE configuration", () => {
    it("resolves API_BASE from process.env.NEXT_PUBLIC_API_URL", () => {
      expect(API_BASE).toBe("https://api.example.com");
    });
  });

  describe("ApiError class", () => {
    it("instantiates correctly with message and status", () => {
      const err = new ApiError("Not found", 404);
      expect(err).toBeInstanceOf(Error);
      expect(err.name).toBe("ApiError");
      expect(err.message).toBe("Not found");
      expect(err.status).toBe(404);
    });
  });

  describe("request core behavior", () => {
    it("adds leading slash if missing in endpoint", async () => {
      let requestedUrl = "";
      globalThis.fetch = vi.fn().mockImplementation(async (url) => {
        requestedUrl = url.toString();
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      });

      await api.get("repos");
      expect(requestedUrl).toBe("https://api.example.com/repos");

      await api.get("/repos");
      expect(requestedUrl).toBe("https://api.example.com/repos");
    });

    it("includes credentials: 'include' on every request", async () => {
      let passedOptions: RequestInit | undefined;
      globalThis.fetch = vi.fn().mockImplementation(async (_url, options) => {
        passedOptions = options;
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      });

      await api.get("/repos");
      expect(passedOptions?.credentials).toBe("include");
    });

    it("automatically sets Content-Type to application/json when body is string and header missing", async () => {
      let passedHeaders: Headers | undefined;
      globalThis.fetch = vi.fn().mockImplementation(async (_url, options) => {
        passedHeaders = options?.headers as Headers;
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      });

      await api.post("/repos", { name: "test-repo" });
      expect(passedHeaders?.get("Content-Type")).toBe("application/json");
    });

    it("preserves explicit Content-Type and custom auth headers when provided", async () => {
      let passedHeaders: Headers | undefined;
      globalThis.fetch = vi.fn().mockImplementation(async (_url, options) => {
        passedHeaders = options?.headers as Headers;
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      });

      await api.post(
        "/custom",
        "raw text",
        {
          headers: {
            "Content-Type": "text/plain",
            Authorization: "Bearer token-123",
          },
        }
      );
      expect(passedHeaders?.get("Content-Type")).toBe("text/plain");
      expect(passedHeaders?.get("Authorization")).toBe("Bearer token-123");
    });

    it("maps network fetch failure to ApiError with status 0", async () => {
      globalThis.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));

      await expect(api.get("/repos")).rejects.toThrow(ApiError);
      try {
        await api.get("/repos");
      } catch (e: any) {
        expect(e.status).toBe(0);
        expect(e.message).toContain("Network connection failure");
      }
    });

    it("returns empty object on 204 No Content", async () => {
      globalThis.fetch = vi.fn().mockResolvedValue(
        new Response(null, { status: 204 })
      );

      const res = await api.delete("/repos/123");
      expect(res).toEqual({});
    });

    it("returns parsed JSON on 200 OK", async () => {
      globalThis.fetch = vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ id: "repo-1", name: "haunter" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      );

      const data = await api.get<{ id: string; name: string }>("/repos/1");
      expect(data).toEqual({ id: "repo-1", name: "haunter" });
    });
  });

  describe("401 unauthorized handling", () => {
    it("redirects to /login and throws ApiError(401) when not on /login path", async () => {
      // Setup window.location mock
      delete (window as any).location;
      window.location = {
        pathname: "/dashboard",
        href: "https://example.com/dashboard",
      } as any;

      globalThis.fetch = vi.fn().mockResolvedValue(
        new Response("Unauthorized", { status: 401 })
      );

      await expect(api.getMe()).rejects.toThrow(ApiError);
      expect(window.location.href).toBe("/login");
    });

    it("does not redirect when already on /login page", async () => {
      delete (window as any).location;
      window.location = {
        pathname: "/login",
        href: "https://example.com/login",
      } as any;

      globalThis.fetch = vi.fn().mockResolvedValue(
        new Response("Unauthorized", { status: 401 })
      );

      await expect(api.getMe()).rejects.toThrow(
        "Session expired or unauthorized"
      );
      expect(window.location.href).toBe("https://example.com/login");
    });
  });

  describe("non-2xx error status mapping", () => {
    it("maps 5xx status to Internal server error message", async () => {
      globalThis.fetch = vi.fn().mockResolvedValue(
        new Response("Crash", { status: 500 })
      );

      try {
        await api.get("/fail");
        expect.unreachable();
      } catch (err: any) {
        expect(err).toBeInstanceOf(ApiError);
        expect(err.status).toBe(500);
        expect(err.message).toBe("Internal server error. Please try again later.");
      }
    });

    it("maps 403 status to Access denied message", async () => {
      globalThis.fetch = vi.fn().mockResolvedValue(
        new Response("Forbidden", { status: 403 })
      );

      try {
        await api.get("/secret");
        expect.unreachable();
      } catch (err: any) {
        expect(err).toBeInstanceOf(ApiError);
        expect(err.status).toBe(403);
        expect(err.message).toBe("Access denied. Insufficient permissions.");
      }
    });

    it("maps 404 status to Requested resource was not found message", async () => {
      globalThis.fetch = vi.fn().mockResolvedValue(
        new Response("Not found", { status: 404 })
      );

      try {
        await api.get("/missing");
        expect.unreachable();
      } catch (err: any) {
        expect(err).toBeInstanceOf(ApiError);
        expect(err.status).toBe(404);
        expect(err.message).toBe("Requested resource was not found.");
      }
    });

    it("maps 422 status to Invalid request payload message", async () => {
      globalThis.fetch = vi.fn().mockResolvedValue(
        new Response("Validation failed", { status: 422 })
      );

      try {
        await api.post("/invalid", {});
        expect.unreachable();
      } catch (err: any) {
        expect(err).toBeInstanceOf(ApiError);
        expect(err.status).toBe(422);
        expect(err.message).toBe("Invalid request payload. Please check your inputs.");
      }
    });

    it("extracts short detail message for other 4xx errors (e.g. 400, 409)", async () => {
      globalThis.fetch = vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({ detail: "Repo already connected" }),
          { status: 409, headers: { "Content-Type": "application/json" } }
        )
      );

      try {
        await api.post("/repos", { owner: "foo", name: "bar" });
        expect.unreachable();
      } catch (err: any) {
        expect(err.status).toBe(409);
        expect(err.message).toBe("Repo already connected");
      }
    });

    it("ignores detail containing Traceback or length >= 120 and falls back to status text", async () => {
      // With Traceback
      globalThis.fetch = vi.fn().mockResolvedValueOnce(
        new Response(
          JSON.stringify({ detail: "Traceback (most recent call last): line 1" }),
          { status: 400, headers: { "Content-Type": "application/json" } }
        )
      );

      try {
        await api.get("/bad");
        expect.unreachable();
      } catch (err: any) {
        expect(err.status).toBe(400);
        expect(err.message).toBe("Request failed with status 400");
      }

      // With length >= 120
      const longMessage = "a".repeat(125);
      globalThis.fetch = vi.fn().mockResolvedValueOnce(
        new Response(
          JSON.stringify({ detail: longMessage }),
          { status: 400, headers: { "Content-Type": "application/json" } }
        )
      );

      try {
        await api.get("/bad");
        expect.unreachable();
      } catch (err: any) {
        expect(err.status).toBe(400);
        expect(err.message).toBe("Request failed with status 400");
      }
    });

    it("handles non-JSON error bodies gracefully for other 4xx errors", async () => {
      globalThis.fetch = vi.fn().mockResolvedValue(
        new Response("plain text error", {
          status: 400,
        })
      );

      try {
        await api.get("/bad-plain");
        expect.unreachable();
      } catch (err: any) {
        expect(err.status).toBe(400);
        expect(err.message).toBe("Request failed with status 400");
      }
    });
  });

  describe("CRUD HTTP verbs", () => {
    it("api.get sends GET request", async () => {
      let method = "";
      globalThis.fetch = vi.fn().mockImplementation(async (_url, opts) => {
        method = opts.method;
        return new Response(JSON.stringify({}), { status: 200 });
      });

      await api.get("/test");
      expect(method).toBe("GET");
    });

    it("api.post sends POST request with serialized JSON body or undefined", async () => {
      let capturedBody: any;
      let capturedMethod = "";
      globalThis.fetch = vi.fn().mockImplementation(async (_url, opts) => {
        capturedMethod = opts.method;
        capturedBody = opts.body;
        return new Response(JSON.stringify({}), { status: 200 });
      });

      await api.post("/test", { a: 1 });
      expect(capturedMethod).toBe("POST");
      expect(capturedBody).toBe(JSON.stringify({ a: 1 }));

      await api.post("/empty-body");
      expect(capturedBody).toBeUndefined();
    });

    it("api.put sends PUT request with serialized JSON body or undefined", async () => {
      let capturedBody: any;
      let capturedMethod = "";
      globalThis.fetch = vi.fn().mockImplementation(async (_url, opts) => {
        capturedMethod = opts.method;
        capturedBody = opts.body;
        return new Response(JSON.stringify({}), { status: 200 });
      });

      await api.put("/test", { b: 2 });
      expect(capturedMethod).toBe("PUT");
      expect(capturedBody).toBe(JSON.stringify({ b: 2 }));

      await api.put("/empty-put");
      expect(capturedBody).toBeUndefined();
    });

    it("api.delete sends DELETE request", async () => {
      let capturedMethod = "";
      globalThis.fetch = vi.fn().mockImplementation(async (_url, opts) => {
        capturedMethod = opts.method;
        return new Response(JSON.stringify({}), { status: 200 });
      });

      await api.delete("/test/1");
      expect(capturedMethod).toBe("DELETE");
    });
  });

  describe("API endpoint wrappers", () => {
    let capturedUrl = "";
    let capturedOpts: any;

    beforeEach(() => {
      globalThis.fetch = vi.fn().mockImplementation(async (url, opts) => {
        capturedUrl = url.toString();
        capturedOpts = opts;
        return new Response(JSON.stringify({ success: true }), { status: 200 });
      });
    });

    it("api.getMe calls GET /auth/me", async () => {
      await api.getMe();
      expect(capturedUrl).toBe("https://api.example.com/auth/me");
      expect(capturedOpts.method).toBe("GET");
    });

    it("api.logout calls POST /auth/logout", async () => {
      await api.logout();
      expect(capturedUrl).toBe("https://api.example.com/auth/logout");
      expect(capturedOpts.method).toBe("POST");
    });

    it("api.getRepos calls GET /repos", async () => {
      await api.getRepos();
      expect(capturedUrl).toBe("https://api.example.com/repos");
      expect(capturedOpts.method).toBe("GET");
    });

    it("api.addRepo calls POST /repos with payload", async () => {
      const payload = { owner: "kaiizer777", name: "Haunter" };
      await api.addRepo(payload);
      expect(capturedUrl).toBe("https://api.example.com/repos");
      expect(capturedOpts.method).toBe("POST");
      expect(capturedOpts.body).toBe(JSON.stringify(payload));
    });

    it("api.removeRepo calls DELETE /repos/:id", async () => {
      await api.removeRepo("repo-xyz");
      expect(capturedUrl).toBe("https://api.example.com/repos/repo-xyz");
      expect(capturedOpts.method).toBe("DELETE");
    });

    it("api.getRepoStats calls GET /repos/:repoId/stats", async () => {
      await api.getRepoStats("repo-xyz");
      expect(capturedUrl).toBe("https://api.example.com/repos/repo-xyz/stats");
      expect(capturedOpts.method).toBe("GET");
    });

    it("api.getAvailableRepos calls GET /github/available-repos", async () => {
      await api.getAvailableRepos();
      expect(capturedUrl).toBe("https://api.example.com/github/available-repos");
      expect(capturedOpts.method).toBe("GET");
    });

    it("api.getRuns calls GET /runs without query when params empty or omitted", async () => {
      await api.getRuns();
      expect(capturedUrl).toBe("https://api.example.com/runs");

      await api.getRuns({});
      expect(capturedUrl).toBe("https://api.example.com/runs");
    });

    it("api.getRuns serializes all query params correctly", async () => {
      await api.getRuns({
        repo_id: "r1",
        status: "completed",
        from: "2026-01-01",
        to: "2026-01-31",
        limit: 20,
        offset: 40,
      });
      const parsedUrl = new URL(capturedUrl);
      expect(parsedUrl.pathname).toBe("/runs");
      expect(parsedUrl.searchParams.get("repo_id")).toBe("r1");
      expect(parsedUrl.searchParams.get("status")).toBe("completed");
      expect(parsedUrl.searchParams.get("from")).toBe("2026-01-01");
      expect(parsedUrl.searchParams.get("to")).toBe("2026-01-31");
      expect(parsedUrl.searchParams.get("limit")).toBe("20");
      expect(parsedUrl.searchParams.get("offset")).toBe("40");
    });

    it("api.getRunTrace calls GET /runs/:runId/trace", async () => {
      await api.getRunTrace("run-999");
      expect(capturedUrl).toBe("https://api.example.com/runs/run-999/trace");
    });

    it("api.getEvalResults calls GET /eval-results", async () => {
      await api.getEvalResults();
      expect(capturedUrl).toBe("https://api.example.com/eval-results");
    });

    it("api.getEvalResult calls GET /eval-results/:evalId", async () => {
      await api.getEvalResult("eval-42");
      expect(capturedUrl).toBe("https://api.example.com/eval-results/eval-42");
    });

    it("api.runEval calls POST /eval/run with provided data or empty object", async () => {
      await api.runEval({ dry_run: true });
      expect(capturedUrl).toBe("https://api.example.com/eval/run");
      expect(capturedOpts.method).toBe("POST");
      expect(capturedOpts.body).toBe(JSON.stringify({ dry_run: true }));

      await api.runEval();
      expect(capturedOpts.body).toBe(JSON.stringify({}));
    });

    it("api.getAvailableModels calls GET /config/model/available", async () => {
      await api.getAvailableModels();
      expect(capturedUrl).toBe("https://api.example.com/config/model/available");
    });

    it("api.getModelConfig calls GET /config/model with or without repo_id", async () => {
      await api.getModelConfig();
      expect(capturedUrl).toBe("https://api.example.com/config/model");

      await api.getModelConfig("repo-abc");
      expect(capturedUrl).toBe("https://api.example.com/config/model?repo_id=repo-abc");
    });

    it("api.updateModelConfig calls PUT /config/model with update payload", async () => {
      const payload = {
        provider: "opencode_zen",
        model_name: "nemotron-3.5-lightning-free",
      };
      await api.updateModelConfig(payload);
      expect(capturedUrl).toBe("https://api.example.com/config/model");
      expect(capturedOpts.method).toBe("PUT");
      expect(capturedOpts.body).toBe(JSON.stringify(payload));
    });
  });
});
