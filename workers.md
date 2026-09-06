# Cloudflare Workers Deployment Runbook (Haunter Dashboard)

> **Audience:** Anyone deploying or maintaining the `frontend/` Next.js dashboard on Cloudflare Workers.
> **Stack Ground Truth:** The dashboard uses Next.js 16 with `output: "export"` (see `frontend/next.config.ts:4`). It is a **static SPA** that fetches data from the AWS Lambda backend over the network. It is hosted on **Cloudflare Workers Static Assets** (`frontend/wrangler.jsonc`, worker name `haunter-ci-agent`, static directory `./out`), **NOT** Cloudflare Pages and **NOT** Vercel.

---

## Architecture & Hosting Overview

1. **Cloudflare Workers Static Assets**: The frontend is hosted directly on Cloudflare Workers using the native Assets binding configured in `wrangler.jsonc`.
2. **Static SPA Export**: The Next.js build (`npm run build`) compiles all pages to pure static HTML/CSS/JS artifacts in `./out`.
3. **Lambda Backend Integration**: All dynamic functionality (trace timelines, run history, OAuth callbacks, repo management) communicates with the backend FastAPI orchestrator on AWS Lambda via `NEXT_PUBLIC_API_URL`.
4. **Single-command CLI**: Build and deploy with standard npm scripts: `npm run build` followed by `npm run deploy` (`wrangler deploy`).
5. **Real-time Observability**: Stream real-time Worker logs with `npm run tail` (`wrangler tail`).

---

## Configuration: `frontend/wrangler.jsonc`

The repository's active Workers configuration at `frontend/wrangler.jsonc`:

```jsonc
{
  "$schema": "node_modules/wrangler/config-schema.json",
  "name": "haunter-ci-agent",
  "compatibility_date": "2025-09-01",
  "compatibility_flags": ["nodejs_compat"],
  "workers_dev": true,
  "assets": {
    "directory": "./out",
    "not_found_handling": "404-page"
  },
  "observability": {
    "enabled": true
  }
}
```

**Key properties:**
- `name: "haunter-ci-agent"` — Identifies the Worker on Cloudflare.
- `compatibility_date: "2025-09-01"` — Pins the Workers runtime compatibility date.
- `compatibility_flags: ["nodejs_compat"]` — Enables Node.js runtime compatibility for polyfills.
- `workers_dev: true` — Deploys to `<worker-name>.<subdomain>.workers.dev`.
- `assets.directory: "./out"` — Points directly to the static output directory from `next build`.
- `assets.not_found_handling: "404-page"` — Serves `out/404.html` for unknown routes.
- `observability.enabled: true` — Enables Workers Logs for live log tailing and telemetry.

---

## Deployment Workflow

### Step 1 — Set Backend API URL
Because Next.js compiles static assets at build time, the backend Lambda Function URL must be present before running `npm run build`:
```bash
# In frontend/.env.local (gitignored)
NEXT_PUBLIC_API_URL=https://<lambda-function-url>.lambda-url.us-east-1.on.aws
```
> **Warning**: Ensure there is no trailing slash and no trailing whitespace.

### Step 2 — Build Static Export
```bash
cd frontend
npm run build
```
This runs `next build`, compiling the application and exporting all static pages, stubs, and assets into `frontend/out/`. Confirm that `out/` exists and contains `index.html`, `404.html`, and `_next/`.

### Step 3 — Deploy to Cloudflare Workers
```bash
npm run deploy
```
This runs `wrangler deploy`, uploading the static assets from `./out` directly to Cloudflare Workers.

### Step 4 — Verify Live Deployment
```bash
npm run tail
```
Stream live requests to confirm proper asset serving and API proxying. Open the dashboard in a browser, test GitHub OAuth login, and verify run detail queries.


---

## Environment Variables

| Variable | Type | Purpose |
|---|---|---|
| `NEXT_PUBLIC_API_URL` | Build-time (baked into bundle) | Base URL for the AWS Lambda Function URL. Set in `frontend/.env.local` before running `npm run build`. |

> **Important:** Because the dashboard is compiled as a static SPA export (`output: "export"`), `NEXT_PUBLIC_API_URL` is resolved at build time. If the backend Lambda Function URL rotates (e.g. after a redeploy), you **must** update `frontend/.env.local`, re-run `npm run build`, and redeploy with `npm run deploy`.


---

## Known gotchas (from prior Haunter deploys)

These come from real failures. They will bite again if ignored.

### Lambda URL rotation

`terraform taint aws_lambda_function.X && terraform apply` assigns the Lambda a **new random URL**. Every time this happens, the dashboard's `NEXT_PUBLIC_API_URL` is stale and every API call 404s.

When you re-apply Lambda infra:

1. Get the new URL from Terraform output.
2. Update `frontend/.env.local`.
3. `npm run build` in `frontend/`.
4. `npm run deploy`.
5. Confirm the dashboard works.

**Mitigation to do later:** add a custom domain to the Lambda Function URL so it stops rotating. Not in scope for this runbook.

### OAuth callback URL drift

The GitHub OAuth App has one `Authorization callback URL`. If it points at an outdated URL and you deploy without updating it, GitHub rejects the callback with a 400. The user sees a blank page after authorizing.

**Always update the OAuth App callback URL in the same change window as a backend URL update.** Test the full round-trip before declaring done.

### Trailing spaces in env vars

`NEXT_PUBLIC_API_URL` is loaded by `src/lib/api.ts:1` with `.trim()`, but the value still ends up in a fetch URL. A trailing space in `.env.local` surfaces as a literal `%20` in the request path and a 404 from Lambda. The dashboard looks broken; the real cause is a single space character.


After setting any env var, `echo -n "$VAR" | wc -c` to confirm no trailing whitespace. (Bash-only; on PowerShell use `($VAR).Length` on the un-trimmed value.)

### Bundle size

Workers free plan has a 1 MB asset limit per file. The Workers **paid** plan ($5/mo) lifts it to 10 MB. Next.js client chunks occasionally exceed 1 MB. If a chunk fails to deploy:

- Check which file: `find out/_next -size +1M`.
- Upgrade to the Workers paid plan for that account.
- Or split the chunk (dynamic import the offending module).

---

## Follow-up: fix the skeleton + refresh UX

Independent of the hosting migration. Do this after the cutover is stable.

### Step 1 — Install SWR

```bash
cd frontend
npm i swr
```

### Step 2 — Replace skeleton-on-every-refresh with keep-previous-data

In any page that currently shows `if (loading) <Skeleton />`, switch to:

```tsx
import useSWR from 'swr'
import { api } from '@/lib/api'

const fetcher = (path: string) => api.get(path) as Promise<any>

const { data, isLoading, isValidating, mutate } = useSWR(
  '/runs',
  fetcher,
  {
    refreshInterval: 3000,
    revalidateOnFocus: true,
    keepPreviousData: true,
    dedupingInterval: 1000,
  }
)

if (isLoading) return <Skeleton />   // only on first load
// isValidating: show a tiny top-bar spinner, NOT a full skeleton
// data: always populated after first load
```

Apply to: `/runs`, `/runs/:id`, `/eval`, `/eval/:id`, `/repos`, `/config`.

### Step 3 — Optimistic UI for mutations

`switchModel`, `addRepo`, `removeRepo`, `runEval` should all use `mutate(..., { optimisticData, rollbackOnError: true })`. See the conversation transcript for the exact pattern.

### Step 4 — SSE for the run-trace page (optional, big win)

The trace page (`src/app/runs/detail/RunDetailClient.tsx`) currently fetches once. To make it live without WebSockets:

1. Add a `GET /runs/:id/stream` endpoint on Lambda that tails Postgres `LISTEN/NOTIFY` and returns `text/event-stream`.
2. The trace page opens an `EventSource` and appends lines to local state.
3. Same effect as a WebSocket, ~20 lines on each side, no new infrastructure.

This is a backend change, not a frontend one. Plan separately.

---

---

## Rollback

If a deploy introduces client issues:
1. Revert to the known good Git commit or pull from backup.
2. Ensure `NEXT_PUBLIC_API_URL` is correct in `frontend/.env.local`.
3. Re-run `npm run build`.
4. Re-run `npm run deploy` to immediately overwrite the static assets on Cloudflare Workers.

---

## TL;DR

1. Configure `wrangler.jsonc` with `assets.directory: "./out"`.
2. Configure `NEXT_PUBLIC_API_URL` in `frontend/.env.local`.
3. `npm run build` (Next.js SPA static export to `./out`).
4. `npm run deploy` (Deploy static assets to Cloudflare Workers via Wrangler).
5. `npm run tail` (Stream live logs from Cloudflare Workers).

