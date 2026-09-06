<!-- BEGIN:nextjs-agent-rules -->

# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` (resolved from this file's directory; in monorepos the `next` package may not be visible from the repo root) before writing any code. Heed deprecation notices.

This block is written and re-added by `next dev` — verify at `node_modules/next/dist/server/lib/generate-agent-files.js`. Removing it from a diff only re-creates the uncommitted change; committing it with your work keeps the tree clean.

<!-- END:nextjs-agent-rules -->

# AGENTS.md — Haunter Frontend Dashboard

## Project Description
The Haunter frontend is a Next.js 16 static SPA dashboard (React 19, Tailwind CSS v4, TypeScript) for monitoring and administering the Haunter autonomous CI failure diagnosis and fix agent. The dashboard provides run history, detailed step-by-step diagnostic traces, eval harness results, active repository controls, and live model switching.

## Architecture & Hosting Ground Truth
- **Frontend Hosting**: **Cloudflare Workers Static Assets** configured via `wrangler.jsonc` (Worker name: `haunter-ci-agent`, static directory: `./out`). It is **NOT** hosted on Cloudflare Pages and **NOT** hosted on Vercel.
- **Static Export**: Built with `output: "export"` in `next.config.ts`. All pages are exported as static HTML/JS/CSS assets to `./out` and served directly by Cloudflare Workers Assets.
- **Backend API**: AWS Lambda running FastAPI + Mangum via Lambda Function URL (`us-east-1`, `x86_64` architecture).
- **Authentication**: GitHub OAuth with cross-origin session cookies (`SameSite=None`, `Secure`, `HttpOnly`).
- **Sandbox Verifier**: Patch verification is performed by the GitHub Actions sandbox runner via isolated test mirror repos (`backend/app/sandbox/github_actions_runner.py`). AWS CodeBuild, GCP Cloud Build, and Local Docker runners are retired.
- **Database**: Neon Postgres.
- **LLM Engine**: OpenCode Zen (default `nemotron-3.5-lightning-free`).

## Environment Variables & Configuration
- `NEXT_PUBLIC_API_URL`: Base URL of the backend AWS Lambda Function URL.
  - **Crucial**: Because this is a static export (`output: "export"`), `NEXT_PUBLIC_API_URL` is baked into the JavaScript bundle at build time (`npm run build`).
  - Set this in `frontend/.env.local` before running `npm run build`.
  - Must not contain trailing slashes or trailing whitespace (causes URL malformation).

## Scripts & Deployment Workflow
- `npm run dev` — Start the local Next.js development server on `http://localhost:3000`.
- `npm run build` — Compile TypeScript and generate the static export in `frontend/out/`.
- `npm test` — Run the Vitest unit/component test suite (must pass with 0 errors).
- `npm run deploy` — Deploy the static assets to Cloudflare Workers via Wrangler (`wrangler deploy`).
- `npm run tail` — Stream live Cloudflare Workers request logs (`wrangler tail`).

## Quality & Engineering Standards for Frontend Agents
1. **Static Export Compatibility**: Do not introduce server runtime dependencies (`headers()`, `cookies()`, dynamic server-side rendering, or edge route handlers). All backend interaction occurs client-side via `src/lib/api.ts`.
2. **Deterministic Component Testing**: When adding or updating UI components, write/update corresponding Vitest tests in `src/**/*.test.tsx`.
3. **Keep the Next.js Agent Block**: Do not remove or alter the `<!-- BEGIN:nextjs-agent-rules -->` block at the top of this file; it is maintained by Next.js tooling.

