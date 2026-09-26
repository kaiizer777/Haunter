"use client";

/**
 * useWebContainer — lifecycle manager for the StackBlitz WebContainer runtime.
 *
 * Boot sequence: boot() → mount(fileTree) → `npm install` → `npm run dev`
 * → `server-ready` URL. Process output streams into `terminalOutput` for the
 * existing TerminalDrawer. `writeFile` is the HMR hot path (no restart);
 * `writeEnvFile` writes `/.env` then restarts the dev server.
 *
 * Contract: `writeFile` / `writeEnvFile` throw `Error("WebContainer not
 * booted")` when called before a successful `boot()` — they never silently
 * no-op, so callers can surface the failure instead of losing edits.
 *
 * The `WebContainer` module is dynamically imported inside `boot()` so this
 * hook never touches browser-only APIs during SSR / static export.
 */

import { useCallback, useRef, useState } from "react";
import type {
  FileSystemTree,
  WebContainer as WebContainerInstance,
  WebContainerProcess,
} from "@webcontainer/api";

export type { FileSystemTree };

export type WebContainerStatus =
  | "idle"
  | "booting"
  | "mounting"
  | "installing"
  | "starting"
  | "ready"
  | "error";

export interface UseWebContainerReturn {
  status: WebContainerStatus;
  previewUrl: string | null;
  terminalOutput: string[];
  boot: (fileTree: FileSystemTree) => Promise<void>;
  writeFile: (path: string, content: string) => Promise<void>;
  writeEnvFile: (vars: Record<string, string>) => Promise<void>;
  restartDevServer: () => Promise<void>;
  teardown: () => void;
  error: string | null;
}

function serializeDotenv(vars: Record<string, string>): string {
  return Object.entries(vars)
    .map(([key, value]) => {
      const needsQuotes = /[\s#"'\n\r]/.test(value) || value.length === 0;
      if (!needsQuotes) return `${key}=${value}`;
      const escaped = value.replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/\n/g, "\\n");
      return `${key}="${escaped}"`;
    })
    .join("\n");
}

function normalizeContainerPath(path: string): string {
  return path.startsWith("/") ? path : `/${path}`;
}

export function useWebContainer(): UseWebContainerReturn {
  const [status, setStatus] = useState<WebContainerStatus>("idle");
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [terminalOutput, setTerminalOutput] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  const containerRef = useRef<WebContainerInstance | null>(null);
  // Single-boot guard: survives React StrictMode double-invoke by caching
  // the in-flight boot promise — concurrent boot() calls share it.
  const bootPromiseRef = useRef<Promise<void> | null>(null);
  const devProcessRef = useRef<WebContainerProcess | null>(null);
  // Disposer for the current `server-ready` subscription. `startDevServer`
  // re-registers on every restart, so the previous subscription must be
  // released first — otherwise each restart leaks a stale listener.
  const serverReadyUnsubRef = useRef<(() => void) | null>(null);

  const appendOutput = useCallback((chunk: string) => {
    setTerminalOutput((prev) => [...prev, chunk]);
  }, []);

  const pipeProcessOutput = useCallback(
    (process: WebContainerProcess) => {
      // Fire-and-forget: pipeTo rejects when the process is killed on
      // restart/teardown — that is expected, not an error.
      process.output
        .pipeTo(
          new WritableStream<string>({
            write: (chunk) => {
              appendOutput(chunk);
            },
          })
        )
        .catch(() => {
          // Stream closed (process killed or container torn down). Ignore.
        });
    },
    [appendOutput]
  );

  const startDevServer = useCallback(async () => {
    const container = containerRef.current;
    if (!container) return;

    // Kill any previous dev server before spawning a fresh one.
    try {
      devProcessRef.current?.kill();
    } catch {
      // Process may already be dead — ignore.
    }
    devProcessRef.current = null;

    setStatus("starting");
    const devProcess = await container.spawn("npm", ["run", "dev"]);
    devProcessRef.current = devProcess;
    pipeProcessOutput(devProcess);

    // Re-subscribe per (re)start: release the previous `server-ready`
    // listener first so restartDevServer/writeEnvFile never accumulate one
    // subscription per restart (SHOULD-1).
    serverReadyUnsubRef.current?.();
    serverReadyUnsubRef.current = container.on("server-ready", (_port, url) => {
      setPreviewUrl(url);
      setStatus("ready");
    });

    // Surface non-zero dev-server exits that happen before server-ready.
    // Mirrors the install-failure path (setError + status "error" +
    // terminal output) so status never sticks at "starting" (SHOULD-3).
    // Identity-guarded: kills from restart/teardown repoint or clear
    // devProcessRef first, so a stale exit must not clobber the new server.
    void devProcess.exit.then((code) => {
      if (code !== 0 && devProcessRef.current === devProcess) {
        devProcessRef.current = null;
        const message = `Dev server exited with code ${code} before becoming ready.`;
        setError(message);
        setStatus("error");
        appendOutput(`\n[webcontainer] ERROR: ${message}\n`);
      }
    });
  }, [appendOutput, pipeProcessOutput]);

  const boot = useCallback(
    async (fileTree: FileSystemTree): Promise<void> => {
      // StrictMode / double-click guard: one boot per hook instance.
      if (bootPromiseRef.current) {
        return bootPromiseRef.current;
      }

      const runBoot = async (): Promise<boolean> => {
        try {
          setError(null);
          setStatus("booting");

          // Dynamic import — WebContainer uses browser-only APIs that
          // break SSR / static-export prerendering.
          const { WebContainer } = await import("@webcontainer/api");
          const container = await WebContainer.boot();
          containerRef.current = container;

          setStatus("mounting");
          await container.mount(fileTree);

          setStatus("installing");
          const installProcess = await container.spawn("npm", ["install"]);
          pipeProcessOutput(installProcess);
          const installExit = await installProcess.exit;
          if (installExit !== 0) {
            throw new Error(`npm install failed with exit code ${installExit}`);
          }

          await startDevServer();
          return true;
        } catch (err) {
          const message =
            err instanceof Error ? err.message : "Failed to boot WebContainer.";
          setError(message);
          setStatus("error");
          appendOutput(`\n[webcontainer] ERROR: ${message}\n`);
          return false;
        }
      };

      const promise = runBoot().then((ok) => {
        // Allow a retry after failure: clear the singleton guard only when
        // boot failed. A successful boot stays singleton for the hook lifetime.
        if (!ok) {
          bootPromiseRef.current = null;
        }
      });
      bootPromiseRef.current = promise;
      await promise;
    },
    [appendOutput, pipeProcessOutput, startDevServer]
  );

  const writeFile = useCallback(
    async (path: string, content: string): Promise<void> => {
      const container = containerRef.current;
      if (!container) throw new Error("WebContainer not booted");
      // Hot path for file_diff SSE sync — no dev server restart, HMR handles it.
      const normalizedPath = normalizeContainerPath(path);
      await container.fs.writeFile(normalizedPath, content);
    },
    []
  );

  const restartDevServer = useCallback(async (): Promise<void> => {
    if (!containerRef.current) return;
    setPreviewUrl(null);
    await startDevServer();
  }, [startDevServer]);

  const writeEnvFile = useCallback(
    async (vars: Record<string, string>): Promise<void> => {
      const container = containerRef.current;
      if (!container) throw new Error("WebContainer not booted");
      const content = serializeDotenv(vars);
      await container.fs.writeFile("/.env", content);
      await restartDevServer();
    },
    [restartDevServer]
  );

  const teardown = useCallback(() => {
    try {
      devProcessRef.current?.kill();
    } catch {
      // Already dead — ignore.
    }
    devProcessRef.current = null;

    serverReadyUnsubRef.current?.();
    serverReadyUnsubRef.current = null;

    try {
      containerRef.current?.teardown();
    } catch {
      // Already torn down — ignore.
    }
    containerRef.current = null;

    bootPromiseRef.current = null;
    setPreviewUrl(null);
    setStatus("idle");
  }, []);

  return {
    status,
    previewUrl,
    terminalOutput,
    boot,
    writeFile,
    writeEnvFile,
    restartDevServer,
    teardown,
    error,
  };
}

export default useWebContainer;
