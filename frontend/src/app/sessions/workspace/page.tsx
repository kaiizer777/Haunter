import { Suspense } from "react";
import SessionWorkspaceClient from "./SessionWorkspaceClient";

/**
 * Static page for cloud agentic live session workspace.
 *
 * The session id is read from the `?id=<uuid>` query parameter (not the URL path)
 * so the route can be statically exported under `output: "export"` without
 * `generateStaticParams` shenanigans or Cloudflare `_redirects` rewrites.
 *
 * `useSearchParams` opts this subtree into client rendering at runtime, so it
 * must sit inside a Suspense boundary for the static export to succeed.
 */
export default function Page() {
  return (
    <Suspense fallback={null}>
      <SessionWorkspaceClient />
    </Suspense>
  );
}
